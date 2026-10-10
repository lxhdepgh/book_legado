"""FontView：字体管理（M5，§1.9 / 契约 §8 / §13）。

系统字体枚举（只读）+ 用户字体增删 + 当前选中字体。当前选中字体落 SettingsService
键 ``reader.font_family``（与阅读器排版偏好同键），由 Controller 写。
**零 app.services.* import**。

架构优化：
- 使用 ft.AppBar 替代简单 Column 标题
- 集成 LoadingSpinner 显示加载状态
- 所有 IconButton 添加 key 属性（提升测试友好性）
"""
from __future__ import annotations

import flet as ft

from ..common import LoadingSpinner


class FontView(ft.Column):
    """字体：系统字体列表 + 用户字体区 + 选中态。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_scan_system = None     # () -> None
        self.on_add_font = None        # () -> None（Controller 弹文件选择）
        self.on_remove_font = None    # (font_id: str) -> None
        self.on_select = None          # (family: str) -> None

        # -- AppBar（对齐 Dart 架构） --
        self._scan_btn = ft.IconButton(
            key="font.scan",
            icon=ft.Icons.REFRESH,
            tooltip="扫描系统字体",
            on_click=lambda e: self._fire(self.on_scan_system))
        self._add_btn = ft.IconButton(
            key="font.add",
            icon=ft.Icons.ADD,
            tooltip="添加字体文件",
            on_click=lambda e: self._fire(self.on_add_font))
        
        self._appbar = ft.AppBar(
            title=ft.Text("字体"),
            actions=[self._scan_btn, self._add_btn],
        )

        # -- 内容区 --
        self._current = ft.Text("当前字体：系统", size=13, color=ft.Colors.GREY_700)
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._rows = ft.ListView(expand=True, spacing=2)
        
        # -- 加载指示器 --
        self._spinner = LoadingSpinner(size=40)
        
        # 文件选择走 page.services 通道（navigation 自动挂载，见 §7.4 FilePicker）。
        self.file_picker = ft.FilePicker()

        self.controls = [
            self._appbar,
            ft.Container(
                padding=ft.Padding(16, 8, 16, 8),
                content=self._current,
            ),
            ft.Container(expand=True, content=self._rows),
            self._spinner,
            ft.Container(
                padding=ft.Padding(16, 4, 16, 8),
                content=self._status,
            ),
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

    def show_loading(self) -> None:
        """显示加载状态（禁用按钮 + 显示旋转器）。"""
        self._spinner.show()
        self._scan_btn.enabled = False
        self._add_btn.enabled = False
    
    def hide_loading(self) -> None:
        """隐藏加载状态（启用按钮 + 隐藏旋转器）。"""
        self._spinner.hide()
        self._scan_btn.enabled = True
        self._add_btn.enabled = True

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
                        key=f"font.remove.{font.get('id', '')}",
                        icon=ft.Icons.DELETE_OUTLINE, 
                        tooltip="移除",
                        on_click=lambda e, fid=str(font.get("id", "")):
                        self._fire(self.on_remove_font, fid))
                        if removable else None),
                    on_click=lambda e, fam=family: self._fire(self.on_select, fam),
                ))
