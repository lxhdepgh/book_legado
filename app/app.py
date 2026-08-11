"""
Flet 应用主配置 - 主题、导航、页面路由
"""
from __future__ import annotations

from pathlib import Path

import flet as ft

from app.services.source_service import SourceService
from app.services.theme_service import ThemeService
from app.ui_feedback import show_snack_bar
from app.views.book_source_view import BookSourceView
from app.views.reading_view import ReadingView
from app.views.settings_view import SettingsView


class BookSourceApp:
    """书源管理 Flet 应用"""

    def __init__(self, resource_dir: str | Path):
        self.resource_dir = Path(resource_dir)
        self.service = SourceService(self.resource_dir)
        self.theme_service = ThemeService(self.service.data_dir)
        self.theme_service.load()

        # 页面引用
        self._book_source_view: BookSourceView | None = None
        self._reading_view: ReadingView | None = None
        self._page: ft.Page | None = None

        # 当前选中的底栏索引
        self._current_index = 1
        self._reader_immersive = False

    def build(self, page: ft.Page):
        """构建应用 (flet.app target)"""
        self._page = page

        # 应用配置
        page.title = "Legado 书源管理工具"
        self.theme_service.apply(page)
        page.padding = 0
        page.spacing = 0
        self._configure_window(page)

        # 初始化数据
        try:
            self.service.load()
        except FileNotFoundError:
            # 首次运行没有书源文件，创建空列表
            self.service.sources = []
        except Exception as e:
            show_snack_bar(page,
                ft.SnackBar(content=ft.Text(f"加载书源失败: {e}"),
                            bgcolor=ft.Colors.RED_100))

        # 创建视图
        self._discover_view = ReadingView(self.service, discovery_only=True)
        self._reading_view = ReadingView(
            self.service, on_section_change=self._on_reading_section_change,
            on_open_settings=lambda: self._navigate_to(2))
        self._book_source_view = BookSourceView(
            self.service,
            on_imported=self._on_imported,
            discover_view=self._discover_view,
        )
        self._settings_view = SettingsView(
            self.theme_service,
            on_preview=self._preview_appearance,
            on_saved=self._appearance_message,
            reader_view=self._reading_view,
        )

        # 主页容器
        content = ft.Container(
            content=self._reading_view,
            expand=True,
        )
        self._content_container = content

        destinations = [
            ("书源", ft.Icons.LIBRARY_BOOKS_OUTLINED, ft.Icons.LIBRARY_BOOKS),
            ("阅读", ft.Icons.MENU_BOOK_OUTLINED, ft.Icons.MENU_BOOK),
            ("配置", ft.Icons.SETTINGS_OUTLINED, ft.Icons.SETTINGS),
        ]
        nav_bar = ft.NavigationBar(
            selected_index=1,
            on_change=self._on_nav_change,
            destinations=[ft.NavigationBarDestination(
                label=label, icon=icon, selected_icon=selected_icon)
                for label, icon, selected_icon in destinations],
        )
        self._bottom_nav = nav_bar
        self._side_nav = ft.NavigationRail(
            selected_index=1, min_width=64, min_extended_width=176,
            use_indicator=True, on_change=self._on_nav_change,
            destinations=[ft.NavigationRailDestination(
                label=label, icon=icon, selected_icon=selected_icon)
                for label, icon, selected_icon in destinations],
        )
        self._side_divider = ft.VerticalDivider(width=1)
        self._navigation_shell = ft.SafeArea(
            expand=True, content=ft.Row(expand=True, spacing=0, controls=[
                self._side_nav, self._side_divider, content,
            ]))

        # 页面布局
        page.add(
            self._navigation_shell,
            nav_bar,
        )

        # FilePicker 作为服务注册到页面（Flet 0.86.x 服务化架构）
        page.services.append(self._book_source_view.file_picker)
        page.on_resize = self._on_page_resize
        page.on_keyboard_event = self._on_keyboard_event
        self._apply_navigation_layout()
        page.update()

    @staticmethod
    def _configure_window(page: ft.Page):
        """Apply window geometry only where the platform owns a desktop window."""
        desktop = {
            ft.PagePlatform.WINDOWS, ft.PagePlatform.MACOS,
            ft.PagePlatform.LINUX,
        }
        if page.platform not in desktop:
            return
        page.window.width = 1100
        page.window.height = 780
        page.window.min_width = 720
        page.window.min_height = 600

    def _on_nav_change(self, e):
        """导航切换"""
        self._navigate_to(e.control.selected_index)

    def _navigate_to(self, index):
        if index == self._current_index:
            return
        previous_index = self._current_index
        self._current_index = index
        if index != 1:
            self._reader_immersive = False
        self._bottom_nav.selected_index = index
        self._side_nav.selected_index = index

        # Leaving the settings page must not retain an unsaved live preview.
        if previous_index == 2:
            self._settings_view.cancel_preview()

        if index == 0:
            self._content_container.content = self._book_source_view
        elif index == 1:
            self._content_container.content = self._reading_view
            self._reader_immersive = self._reading_view._section == "reader"
        elif index == 2:
            self._settings_view.sync_from_saved()
            self._content_container.content = self._settings_view

        self._apply_navigation_layout()
        self._page.update()

    def _on_page_resize(self, _=None):
        self._apply_navigation_layout()
        if self._page:
            self._page.update()

    def _on_keyboard_event(self, event):
        if (self._current_index == 1 and self._reading_view is not None and
                self._reading_view._section == "reader"):
            self._reading_view._on_reader_key(event)

    def _apply_navigation_layout(self):
        if not self._page:
            return
        width = float(self._page.width or self._page.window.width or 430)
        compact = width < 600
        self._bottom_nav.visible = compact and not self._reader_immersive
        self._side_nav.visible = not compact and not self._reader_immersive
        self._side_divider.visible = self._side_nav.visible
        self._side_nav.extended = width >= 900
        self._side_nav.label_type = (
            ft.NavigationRailLabelType.NONE if self._side_nav.extended
            else ft.NavigationRailLabelType.SELECTED)

    def _on_reading_section_change(self, section: str):
        immersive = section == "reader" and self._current_index == 1
        if immersive == self._reader_immersive:
            return
        self._reader_immersive = immersive
        self._apply_navigation_layout()

    def _preview_appearance(self, settings):
        """Apply a temporary preview without touching persisted settings."""
        self.theme_service.apply(self._page, settings)
        self._page.update()

    def _appearance_message(self, message: str):
        self.theme_service.apply(self._page)
        show_snack_bar(self._page, ft.SnackBar(content=ft.Text(message)))
        self._page.update()

    def _on_imported(self, result: dict):
        """导入完成后的回调 - 显示提示（Tab 切换由 BookSourceView 内部处理）"""
        show_snack_bar(self._page,
            ft.SnackBar(
                content=ft.Text(
                    f"导入完成: 新增 {result['imported']} 条"
                    + (f", 重复 {result['duplicates']} 条" if result.get('duplicates') else "")
                ),
                bgcolor=ft.Colors.GREEN_100,
            )
        )
