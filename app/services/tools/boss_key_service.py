"""BossKeyService：老板键——一键把整个窗口藏掉，点托盘图标回来。

为什么是"藏"不是"最小化"：最小化之后任务栏上还留着一格，标题栏缩略图还在，
老板键要的那件事（看起来这台机器上没有在开小差）一件都没做成。探针实测
（第七十三轮，桌面 FLET_APP + ctypes 读窗口样式）：

* ``page.window.visible = False`` → ``IsWindowVisible`` 与 ``WS_VISIBLE`` 同时转假，
  任务栏那格**消失**，且 ``visible = True`` 原样回得来；
* ``page.window.minimized = True`` → ``IsIconic`` 转真，但窗口还在任务栏；
* ``page.window.skip_task_bar = True`` → 样式位（WS_EX_TOOLWINDOW / APPWINDOW）
  **一位没动**：这条路在这套客户端上根本不生效，不能拿它当"从任务栏摘掉"。

所以形状是：``visible=False`` + 一只**只在藏窗期间存在**的托盘图标当唯一回来的入口。
图标挂不上就**不藏**（顺序是先挂图标再藏窗）：窗口消失又没有任何入口，用户只会
以为程序崩了直接强杀，读过的进度全丢——比功能没做成严重得多。

非 Windows / 没装 pywin32 → :attr:`available` 为 False，功能降级成"设置页上写着
本机没有托盘能力"，其它一切照旧。窗口与托盘都不 import flet（服务层红线），
page 由装配根按 :meth:`set_page_provider` 注入（内置浏览器同一套，见
``main._wire_browser_page``）。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.engine_worker import call_on_ui
from app.core.lifecycle import Service
from app.core.settings import SettingsService
from app.core.shortcuts import (BOSS_KEY_ENABLED, BOSS_KEY_SHORTCUT,
                                DEFAULT_BOSS_KEY, chord_of, format_binding,
                                parse_binding)
from app.services.tools.win_tray import WinTray

logger = logging.getLogger(__name__)

#: 托盘提示文字（藏窗期间用户在通知区唯一能看到的一句话）。
_TRAY_TIP = "Legado 书源管理工具：点这里回来"

#: 回窗到发出 toFront 之间等多久（秒）。客户端把 toFront 做在收到之后，桌面探针
#: 量到那次 ShowWindow 落在我们发出后 **0.09 ~ 0.23 秒**（0 秒间隔那一臂两轮的读数），
#: 所以这道关要等过它才落：等太短，"这一拍到底还不还回窗口"就还没定。
_FRONT_DELAY = 0.3


class BossKeyService(Service):
    """老板键：偏好读写 + 藏窗/回窗编排 + 托盘生命周期。"""

    name = "BossKeyService"
    dependencies: tuple[type[Service], ...] = (SettingsService,)

    def __init__(self, tray_factory=None) -> None:
        super().__init__()
        self._page_provider = None
        self._tray_factory = tray_factory or WinTray
        self._tray = None
        self._hidden = False
        #: 上一次失败的原因（成功时清空）；设置页拿它写状态行。
        self.error = ""
        self._settings: SettingsService | None = None

    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self.hub is not None:
            try:
                self._settings = self.hub.require(SettingsService)
            except Exception:
                logger.debug("SettingsService 未就绪，老板键用默认值", exc_info=True)

    async def on_stop(self) -> None:
        self._close_tray()

    # ------------------------------------------------------------------ #
    # 注入面（装配根调用）
    # ------------------------------------------------------------------ #
    def set_page_provider(self, provider) -> None:
        """注入"取 flet page"的可调用（服务层零 flet，同 BrowserService）。"""
        self._page_provider = provider

    # ------------------------------------------------------------------ #
    # 偏好
    # ------------------------------------------------------------------ #
    def _get(self, key: str, default):
        if self._settings is None:
            return default
        try:
            value = self._settings.get(key, default)
        except Exception:
            logger.debug("读老板键偏好 %s 失败", key, exc_info=True)
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._get(BOSS_KEY_ENABLED, True))

    @property
    def shortcut(self) -> str:
        """规范组合串（``"ctrl+alt+h"``）；库里存的是用户写法，这里当场收编。"""
        stored = str(self._get(BOSS_KEY_SHORTCUT, DEFAULT_BOSS_KEY) or "")
        return parse_binding(stored) or DEFAULT_BOSS_KEY

    def set_preferences(self, *, enabled: bool | None = None,
                        shortcut: str | None = None) -> None:
        """写偏好（设置页点"保存"时调；两条都能单独改）。"""
        if self._settings is None:
            self.error = "SettingsService 未就绪，老板键偏好没处落"
            return
        if enabled is not None:
            self._settings.set(BOSS_KEY_ENABLED, bool(enabled))
        if shortcut is not None:
            self._settings.set(BOSS_KEY_SHORTCUT, parse_binding(shortcut))

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """这台机器上老板键到底能不能用（平台 + 依赖 + page 三样齐）。"""
        return WinTray.available() and self._page_provider is not None

    @property
    def hidden(self) -> bool:
        return self._hidden

    @property
    def status_text(self) -> str:
        if not WinTray.available():
            return "本机没有托盘能力（老板键只在 Windows + pywin32 下生效）"
        if self._page_provider is None:
            return "还没拿到窗口（装配根未注入 page）"
        return f"当前绑定：{format_binding(self.shortcut)}" + (
            "（已停用）" if not self.enabled else "")

    # ------------------------------------------------------------------ #
    # 键盘入口（装配根把 page.on_keyboard_event 串到这里）
    # ------------------------------------------------------------------ #
    def handle_key(self, event) -> bool:
        """命中老板键就藏窗并返回 True（这一拍不再往下分发）。"""
        if not self.enabled or not self.available or self._hidden:
            return False
        if chord_of(event) != self.shortcut:
            return False
        return self.hide()

    # ------------------------------------------------------------------ #
    # 藏窗 / 回窗
    # ------------------------------------------------------------------ #
    def hide(self) -> bool:
        """先挂托盘图标、再藏窗口。图标挂不上就什么都不做（见模块文档）。"""
        if self._hidden:
            return True
        self.error = ""
        tray = self._tray_factory(_TRAY_TIP, self._on_tray_activate)
        if not tray.start():
            self.error = f"托盘图标挂不上：{tray.error}"
            logger.warning("老板键没藏窗口，因为%s", self.error)
            return False
        self._tray = tray
        self._apply_visibility(False)
        self._hidden = True
        return True

    def restore(self) -> bool:
        """先回窗、再摘图标：图标比窗口先没，用户会以为程序消失了。"""
        if not self._hidden:
            return True
        self.error = ""
        self._apply_visibility(True)
        self._hidden = False
        self._close_tray()
        return True

    def _on_tray_activate(self) -> None:
        """托盘回调：**跑在托盘线程上**，控件只认界面循环，所以必须编组回去。"""
        _run_on_ui(self.restore)

    def _apply_visibility(self, visible: bool) -> None:
        if self._page_provider is None:
            self.error = "还没拿到窗口（装配根未注入 page）"
            return
        try:
            page = self._page_provider()
        except Exception as exc:
            self.error = f"取窗口失败：{type(exc).__name__}: {exc}"
            return
        if page is None:
            self.error = "取到的窗口是空的"
            return
        try:
            window = page.window
            window.visible = bool(visible)
            if visible:
                # 藏起来的窗口不会自己抢回前台：不 to_front 的话，用户点了图标
                # 只看到"图标闪了一下"，窗口还在其它程序后面。
                self._front(window)
            page.update()
        except Exception as exc:
            self.error = f"{'回' if visible else '藏'}窗失败：{type(exc).__name__}: {exc}"
            logger.warning("%s", self.error)

    def _front(self, window) -> None:
        """把窗口提到最前——**等一下再发**，发之前按藏窗标志把关。

        ``window.to_front()`` 在 0.86 是**协程**（裸调用只会得到 "coroutine was never
        awaited"，窗口一动没动），所以有 running loop 才排；没有 loop（无头测试）就跳过
        ——那不是错误。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        future = asyncio.ensure_future(self._front_later(window), loop=loop)
        future.add_done_callback(_log_to_front)

    async def _front_later(self, window) -> None:
        """等 ``_FRONT_DELAY`` 再发 toFront；这段时间里用户又按了老板键就**不发**。

        为什么不能当场发（桌面探针 0 秒间隔那一臂，两轮都复现）：客户端把 toFront
        做在**收到之后**，所以它晚于其后那条 ``visible=False`` 落地——时间线读出
        "0.05 藏、0.09 起一路亮"，而 ``hidden`` 仍是 True。用户侧的样子是"按了老板键，
        窗口闪一下又赖着不走"，而且标志说藏着之后 :meth:`handle_key` 每拍直接 return，
        键盘再按也没反应，只剩托盘图标能回来。
        反过来"补一次藏"我试过，**没用**：``to_front()`` 那条协程在消息**发出去**时就完成了，
        补的藏落在真 ShowWindow 之前，客户端照样把窗口叫亮（改后重跑，0.23 起一路亮）。
        所以这里认标志、在发之前把关。
        """
        await asyncio.sleep(_FRONT_DELAY)
        if self._hidden:
            return
        to_front = getattr(window, "to_front", None)
        if to_front is None:
            return
        await to_front()

    # ------------------------------------------------------------------ #
    def _close_tray(self) -> None:
        tray, self._tray = self._tray, None
        if tray is None:
            return
        try:
            tray.stop()
        except Exception:
            logger.debug("托盘收尾失败", exc_info=True)


def _log_to_front(future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.warning("窗口提到最前失败：%s", exc)


def _run_on_ui(fn) -> None:
    """把碰窗口的动作挪到界面循环上（契约 §5.2，实现见 :func:`call_on_ui`）。"""
    call_on_ui(fn)
