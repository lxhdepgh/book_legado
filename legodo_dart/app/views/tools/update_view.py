"""UpdateView：更新检查页（M5，§1.17 / 契约 §9 / §13）。

检查新版本 → 提示版本/更新说明 → 下载安装包到 downloads/（**不做静默自替换**，
用户手动安装）。**零 app.services.* import**：动作经 Controller 调 UpdateService；
版本信息由 UpdateAvailableEvent 驱动渲染。
"""
from __future__ import annotations

import flet as ft


class UpdateView(ft.Column):
    """更新：检查按钮 + 版本说明 + 下载按钮。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_check = None           # () -> None
        self.on_download = None        # (url: str) -> None

        self._title = ft.Text("检查更新", size=20, weight=ft.FontWeight.BOLD)
        self._current = ft.Text("当前版本：未知", size=12, color=ft.Colors.GREY_600)
        self._check_btn = ft.FilledButton(
            "检查更新", icon=ft.Icons.REFRESH,
            on_click=lambda e: self._fire(self.on_check))
        self._info = ft.Container(visible=False, padding=10,
                                 border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                                 border_radius=6)
        self._version = ft.Text("", size=16, weight=ft.FontWeight.W_600)
        self._notes = ft.Text("", size=13, selectable=True)
        self._download_btn = ft.FilledButton(
            "下载到下载目录", icon=ft.Icons.DOWNLOAD, visible=False,
            on_click=lambda e: self._fire(self.on_download, self._url_value))
        self._url_value = ""
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        self._info.content = ft.Column([
            self._version, self._notes, self._download_btn], spacing=6)

        self.controls = [
            self._title,
            self._current,
            ft.Row([self._check_btn]),
            self._info,
            self._status,
        ]

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_current_version(self, version: str) -> None:
        self._current.value = f"当前版本：{version or '未知'}"

    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def show_update(self, version: str, notes: str, url: str) -> None:
        """渲染可用更新（UpdateAvailableEvent 驱动）。"""
        self._info.visible = True
        self._version.value = f"发现新版本：{version or ''}"
        self._notes.value = notes or "（暂无更新说明）"
        self._url_value = url or ""
        self._download_btn.visible = bool(url)
        self._status.value = ""

    def set_up_to_date(self, text: str = "已是最新版本") -> None:
        self._info.visible = False
        self._url_value = ""
        self._status.value = text
