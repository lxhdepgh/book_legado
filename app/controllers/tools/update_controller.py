"""UpdateController：更新检查页编排（M5，§1.17 / 契约 §9）。

检查新版本经 UpdateService.check_update；发现版本由 UpdateAvailableEvent 驱动渲染；
下载只落 downloads/（不做静默自替换，用户手动安装）。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.update_service import UpdateService
except Exception:  # pragma: no cover - 并行线未落盘
    class UpdateService:  # type: ignore[no-redef]
        pass

try:
    from app.core.events import UpdateAvailableEvent
except Exception:  # pragma: no cover - 事件随并行线落地
    UpdateAvailableEvent = None  # type: ignore[assignment]


class UpdateController(BaseController):
    """更新：检查 / 版本提示 / 下载。"""

    destination = "tools.update"

    def on_attach(self) -> None:
        self._svc = self._safe_service(UpdateService)
        self._latest_info = None
        self._view.on_check = self.check
        self._view.on_download = self.download
        if UpdateAvailableEvent is not None:
            self.subscribe(UpdateAvailableEvent, self._on_available)
        current = getattr(self._svc, "current_version", "") if self._svc else ""
        if current:
            self._view.set_current_version(str(current))

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def check(self) -> None:
        if self._svc is None:
            self._view.set_status("UpdateService 未就绪")
            return
        self._view.set_status("正在检查更新…")
        future = self.ui_call(self._svc.check_update())
        future.add_done_callback(self.on_done(self._after_check))

    def _after_check(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            info = future.result()
        except Exception as exc:
            self._view.set_status(f"检查失败：{exc}")
            return
        if not info:
            self._latest_info = None
            self._view.set_up_to_date()
            return
        self._latest_info = info
        self._view.show_update(
            str(getattr(info, "version", "")),
            str(getattr(info, "notes", "")),
            str(getattr(info, "url", "")))

    def download(self, url: str = "") -> None:
        # 服务签名是 download(info: UpdateInfo)——传字符串会当场 AttributeError
        # （"str object has no attribute url"），下载对象以最近一次检查结果为准。
        info = self._latest_info
        if info is None:
            self._view.set_status("请先检查更新，发现新版本后才能下载")
            return
        if self._svc is None:
            self._view.set_status("UpdateService 未就绪")
            return
        self._view.set_status("正在下载到下载目录…")
        future = self.ui_call(self._svc.download(info))
        future.add_done_callback(self.on_done(self._after_download))

    def _after_download(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            path = future.result()
        except Exception as exc:
            self._view.set_status(f"下载失败：{exc}")
            return
        self._view.set_status(f"已下载 → {path}（请手动安装）")

    async def _on_available(self, event) -> None:
        self._latest_info = getattr(event, "info", None) or event
        self._view.show_update(
            str(getattr(event, "version", "")),
            str(getattr(event, "notes", "")),
            str(getattr(event, "url", "")))
