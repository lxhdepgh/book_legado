"""FontView：字体管理（M5，§1.9 / 契约 §8 / §13）。

系统字体枚举（只读）+ 用户字体增删 + 当前选中字体。当前选中字体落 SettingsService
键 ``reader.font_family``（与阅读器排版偏好同键），由 Controller 写。
**零 app.services.* import**。
"""
from __future__ import annotations

import flet as ft


class FontView(ft.Column):
    """字体：系统字体列表 + 用户字体区 + 选中态。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_scan_system = None     # () -> None
        self.on_add_font = None        # () -> None（Controller 弹文件选择）
        self.on_remove_font = None    # (font_id: str) -> None
        self.on_select = None          # (family: str) -> None

        self._title = ft.Text("字体", size=20, weight=ft.FontWeight.BOLD)
        self._scan_btn = ft.OutlinedButton(
            "扫描系统字体", icon=ft.Icons.REFRESH,
            on_click=lambda e: self._fire(self.on_scan_system))
        self._add_btn = ft.FilledButton(
            "添加字体文件", icon=ft.Icons.FILE_OPEN,
            on_click=lambda e: self._fire(self.on_add_font))
        self._current = ft.Text("当前字体：系统", size=13, color=ft.Colors.GREY_700)
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._rows = ft.ListView(expand=True, spacing=2)
        # 文件选择走 page.services 通道（navigation 自动挂载，见 §7.4 FilePicker）。
        self.file_picker = ft.FilePicker()

        self.controls = [
            self._title,
            ft.Row([self._scan_btn, self._add_btn], spacing=8),
            self._current,
            ft.Container(expand=True, content=self._rows),
            self._status,
        ]

    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_current(self, family: str) -> None:
        self._current.value = f"当前字体：{family or '系统'}"

    def render_fonts(self, system_fonts: list[dict],
                     user_fonts: list[dict], current: str = "") -> None:
        """渲染字体行；font 含 id/name/family/path；current 高亮选中。"""
        self._rows.controls = []
        for group_title, fonts, removable in (
                ("系统字体", list(system_fonts or []), False),
                ("用户字体", list(user_fonts or []), True)):
            self._rows.controls.append(ft.Text(
                group_title, size=12, weight=ft.FontWeight.W_600,
                color=ft.Colors.GREY_600))
            for font in fonts:
                family = str(font.get("family", font.get("name", "")))
                self._rows.controls.append(ft.ListTile(
                    key=str(font.get("id", family)) or None,
                    selected=(family == current and bool(current)),
                    title=ft.Text(font.get("name", family), font_family=family or None),
                    subtitle=ft.Text(str(font.get("path", "")), size=11,
                                     color=ft.Colors.GREY_500, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS),
                    trailing=(ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE, tooltip="移除",
                        on_click=lambda e, fid=str(font.get("id", "")):
                        self._fire(self.on_remove_font, fid))
                        if removable else None),
                    on_click=lambda e, fam=family: self._fire(self.on_select, fam),
                ))
