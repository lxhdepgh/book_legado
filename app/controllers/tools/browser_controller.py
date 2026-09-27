"""BrowserController：通用浏览器编排（M5，§1.19 / 契约 §11）。

内置浏览器在桌面端**没有**应用内 WebView 控件（`platform_support.supports_in_app_webview`
在 Windows/Linux 为 False），能"渲染网页"的唯一通道是 CDP 投屏会话：
BrowserService 开一个有头 Chromium（窗口停在桌面外），把画面帧推回视图，
点击/滚动/拖拽/输入再换算成坐标回灌。此前这里只调 ``fetch()``（无头取 DOM 字符串）
并把返回值直接丢掉，所以点「访问」只有一行状态文字、页面区永远空白。

线程边界（本文件的全部难点）：

- 内核句柄活在 **EngineHub 循环**上，视图只拿会话号，一切动作经服务协程派发；
- ``on_frame`` 回调落在引擎循环上 → 帧必须编组回**界面循环**才能改控件
  （:meth:`BaseController.ui_call`），并且只画最新一帧（12fps 不该刷出 12 个任务）；
- 手势按 FIFO 顺序串行发出（滑块拖动靠 press→move→release 的次序，乱序即失效），
  所以走一条"队列 + 单个泵"，而不是每下手势各派一个协程。

切页 :meth:`on_detach` 必须关掉会话：有头 Chromium 留在服务池里就是没人管的孤儿窗口。
"""
from __future__ import annotations

import logging
from collections import deque

from app.controllers.base import BaseController
from app.services.legado_url import fix_url

logger = logging.getLogger(__name__)

try:
    from app.services.tools.browser_service import BrowserService
except Exception:  # pragma: no cover - 并行线未落盘
    class BrowserService:  # type: ignore[no-redef]
        pass


class BrowserController(BaseController):
    """浏览器：导航/前进后退/重载 + 投屏画面与手势回灌。"""

    destination = "tools.browser"

    def on_attach(self) -> None:
        self._browser = self._safe_service(BrowserService)
        self._history: list[str] = []
        self._pos = -1
        self._sid: str | None = None      # 当前投屏会话号（None＝没有打开的页面）
        self._pending_frame: bytes | None = None
        self._painting = False
        self._gestures: deque = deque()
        self._pumping = False
        self._view.on_navigate = self.navigate
        self._view.on_back = self.back
        self._view.on_forward = self.forward
        self._view.on_reload = self.reload
        self._view.on_close = self.close_page
        self._view.on_cast_tap = self._on_tap
        self._view.on_cast_scroll = self._on_scroll
        self._view.on_cast_pan = self._on_pan
        self._view.on_cast_type = self._on_type

    def on_detach(self) -> None:
        # 视图走了：会话留着就是有头内核的孤儿窗口（服务池要等 on_stop 才回收）。
        sid, self._sid = self._sid, None
        self._pending_frame = None
        self._gestures.clear()
        if sid is not None and self._browser is not None:
            self.ui_call(self._close_session(sid))

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #
    def navigate(self, url: str) -> None:
        # 补协议在这里做一次：内核那边同样会补（headless_browser），但历史栈
        # 与地址栏要存归一后的地址，否则前进后退列表里躺着一堆打不开的裸域名。
        url = fix_url(url or "")
        if not url:
            return
        if self._browser is None:
            self._view.set_status("BrowserService 未就绪")
            return
        self._view.set_loading(True, "加载中…")
        if self._sid is None:
            self.ui_call(self._open(url))
        else:
            self.ui_call(self._goto(self._sid, url))

    async def _open(self, url: str) -> None:
        try:
            sid = await self._browser.open_page(url, on_frame=self._push_frame)
        except Exception as exc:
            self._report_failure(url, exc)
            return
        self._sid = sid
        if self._view is None:            # 开会话途中被切页：detach 之后不再回来
            self.ui_call(self._close_session(sid))
            return
        self._view.begin_cast()
        await self._sync_viewport(sid)
        self._after_navigate(url)

    async def _goto(self, sid: str, url: str, push: bool = True) -> None:
        try:
            await self._browser.navigate_page(sid, url)
        except Exception as exc:
            self._report_failure(url, exc)
            return
        if self._view is not None:
            self._after_navigate(url, push)

    async def _sync_viewport(self, sid: str) -> None:
        """把内核回报的投屏尺寸交给视图，坐标换算才落在画面上。"""
        try:
            size = await self._browser.cast_viewport(sid)
        except Exception:
            return
        if self._view is not None and size:
            self._view.set_cast_size(int(size[0]), int(size[1]))

    def _report_failure(self, url: str, exc: Exception) -> None:
        """内核缺失/站点失败都不崩，但要给出能照着做的下一步。"""
        logger.warning("浏览器加载失败 %s: %s", url, exc)
        if self._view is None:
            return
        text = str(exc) or type(exc).__name__
        self._view.set_loading(False)
        self._view.set_status(f"加载失败：{text}")
        if "playwright" in text or "内核" in text:
            self._view.end_cast("内置 Chromium 内核不可用："
                                "python -m playwright install chromium")

    def _after_navigate(self, url: str, push: bool = True) -> None:
        if self._view is None:            # 视图已 detach：在途结果丢弃
            return
        if push:
            # 截断前进栈后压入历史。回到历史里的某一页**不能**走这条路，
            # 否则 pos 还停在旧位置，栈被 `history[:pos+1]` 截掉，前进键就此失灵。
            self._history = self._history[: self._pos + 1]
            self._history.append(url)
            self._pos = len(self._history) - 1
        self._view.set_address(url)
        self._view.set_nav_enabled(self._pos > 0, self._pos < len(self._history) - 1)
        self._view.set_loading(False)
        self._view.set_status("加载完成")

    def back(self) -> None:
        if self._pos > 0:
            self._pos -= 1
            self._goto_history()

    def forward(self) -> None:
        if self._pos < len(self._history) - 1:
            self._pos += 1
            self._goto_history()

    def reload(self) -> None:
        if 0 <= self._pos < len(self._history):
            self._goto_history()

    def _goto_history(self) -> None:
        """跳到栈里现有的某一页：只换页面，不动历史栈（前进/后退/重载共用）。"""
        url = self._history[self._pos]
        self._view.set_address(url)
        self._view.set_nav_enabled(self._pos > 0, self._pos < len(self._history) - 1)
        if self._sid is None:             # 没有会话：地址栏回显即可
            return
        self._view.set_loading(True, "加载中…")
        self.ui_call(self._goto(self._sid, url, push=False))

    def close_page(self, _=None) -> None:
        sid, self._sid = self._sid, None
        self._gestures.clear()
        if self._view is not None:
            self._view.end_cast("页面已关闭")
            self._view.set_status("就绪")
            self._view.set_nav_enabled(False, False)
        if sid is not None and self._browser is not None:
            self.ui_call(self._close_session(sid))

    async def _close_session(self, sid: str) -> None:
        try:
            await self._browser.close_page(sid)
        except Exception as exc:
            logger.warning("关闭内置浏览器会话失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 帧：引擎循环 → 界面循环（只画最新一帧）
    # ------------------------------------------------------------------ #
    def _push_frame(self, data: bytes) -> None:
        """BrowserService 的 ``on_frame``（在引擎循环上被回调）。"""
        if self._view is None or not data:
            return
        self._pending_frame = data
        if self._painting:
            return
        self._painting = True
        self.ui_call(self._paint())

    async def _paint(self) -> None:
        try:
            while self._pending_frame is not None:
                data, self._pending_frame = self._pending_frame, None
                if self._view is None:
                    return
                self._view.render_frame(data)
                self.flush()
        finally:
            self._painting = False

    # ------------------------------------------------------------------ #
    # 手势：一条队列串行发出（滑块拖动依赖 press→move→release 的次序）
    # ------------------------------------------------------------------ #
    def _on_tap(self, x: float, y: float, button: str) -> None:
        self._enqueue("tap", x, y, button)

    def _on_scroll(self, x: float, y: float, delta_x: float, delta_y: float) -> None:
        self._enqueue("scroll", x, y, delta_x, delta_y)

    def _on_pan(self, kind: str, x: float, y: float) -> None:
        self._enqueue(kind if kind != "move_to" else "move_to", x, y)

    def _on_type(self, text: str) -> None:
        self._enqueue("type_text", text)

    def _enqueue(self, action: str, *args) -> None:
        if self._sid is None or self._browser is None:
            return
        self._gestures.append((action, args))
        if not self._pumping:
            self._pumping = True
            self.ui_call(self._pump())

    async def _pump(self) -> None:
        try:
            while self._gestures:
                action, args = self._gestures.popleft()
                if self._sid is None:
                    break
                try:
                    await self._browser.cast_gesture(self._sid, action, *args)
                except Exception as exc:
                    logger.warning("投屏手势 %s 失败：%s", action, exc)
        finally:
            self._pumping = False
