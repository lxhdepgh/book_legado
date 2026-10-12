"""WebBridgeController：网络桥设置页编排（M5 §1.15 的界面承接）。

写动作全部走 :class:`WebBridgeService` 的向导方法（``set_enabled`` / ``set_port`` /
``regenerate_token``）——它们才是"改设置 + 收敛监听态"的那条路；界面直接写
SettingsService 的键只会留下一个开关开着、端口其实没听的状态。

每次动作都返回**一帧新状态**（含实际绑定端口），所以失败也要把状态读回来：
开关停在"已打开"而其实没监听，比直接报错更坑用户。读状态与报错在同一次派发里
完成，分两趟就会互相盖掉对方的日志。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.web_bridge_service import WebBridgeService
except Exception:  # pragma: no cover - 并行线未落盘时视图仍可构造
    class WebBridgeService:  # type: ignore[no-redef]
        pass


class WebBridgeController(BaseController):
    """网络桥：开关 / 端口 / 令牌 / 状态刷新。"""

    destination = "tools.webbridge"

    def on_attach(self) -> None:
        self._svc = self._service_or_none()
        self._view.on_toggle_enabled = self.set_enabled
        self._view.on_save_port = self.save_port
        self._view.on_regenerate = self.regenerate_token
        self._view.on_refresh = self.refresh
        self.refresh()

    def _service_or_none(self):
        try:
            return self.service(WebBridgeService)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def refresh(self, _=None) -> None:
        self._dispatch(None, "状态已刷新", quiet=True)

    def set_enabled(self, enabled: bool) -> None:
        want = bool(enabled)
        self._dispatch(lambda: self._svc.set_enabled(want),
                       "网络桥已启用" if want else "网络桥已关闭")

    def save_port(self, port) -> None:
        self._dispatch(lambda: self._svc.set_port(port), "端口已保存")

    def regenerate_token(self) -> None:
        self._dispatch(lambda: self._svc.regenerate_token(), "已换新令牌")

    # ------------------------------------------------------------------ #
    def _dispatch(self, factory, ok_text: str, quiet: bool = False) -> None:
        if self._svc is None:
            self._view.set_result(False, "WebBridgeService 未就绪")
            return
        if factory is None:
            coro = self._read_status()
        else:
            coro = self._call(factory, quiet)
        future = self.ui_call(coro)
        future.add_done_callback(
            self.on_done(lambda f: self._after(f, ok_text if not quiet else "")))

    async def _read_status(self):
        try:
            return await self._svc.bridge_status(), ""
        except Exception as exc:
            logger.warning("网桥状态读取失败：%s", exc)
            return None, str(exc)

    async def _call(self, factory, quiet: bool):
        try:
            return await factory(), ""
        except Exception as exc:
            # 服务里的原因（端口被占、端口不是数字、没有设置服务）就是用户要看的那句。
            logger.warning("网桥操作失败：%s", exc)
            status = None
            try:
                status = await self._svc.bridge_status()
            except Exception:  # 连状态都读不到：只报错，界面维持原样
                pass
            return status, str(exc)

    def _after(self, future, ok_text: str) -> None:
        try:
            status, error = future.result()
        except Exception as exc:  # 派发本身炸了（比如替身没有那个方法）
            if self._view is not None:
                self._view.set_result(False, str(exc))
            return
        if self._view is None:  # 在途时用户已切页
            return
        if status is not None:
            self._view.set_status(status)
        if error:
            self._view.set_result(False, error)
        elif ok_text:
            self._view.set_result(True, ok_text)
