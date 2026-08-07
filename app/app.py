"""
Flet 应用主配置 - 主题、导航、页面路由
"""
from __future__ import annotations

from pathlib import Path

import flet as ft

from app.services.source_service import SourceService
from app.views.book_source_view import BookSourceView
from app.views.reading_view import ReadingView


class BookSourceApp:
    """书源管理 Flet 应用"""

    def __init__(self, resource_dir: str | Path):
        self.resource_dir = Path(resource_dir)
        self.service = SourceService(self.resource_dir)

        # 页面引用
        self._book_source_view: BookSourceView | None = None
        self._reading_view: ReadingView | None = None
        self._page: ft.Page | None = None

        # 当前选中的底栏索引
        self._current_index = 1

    def build(self, page: ft.Page):
        """构建应用 (flet.app target)"""
        self._page = page

        # 应用配置
        page.title = "Legado 书源管理工具"
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = ft.Theme(
            color_scheme_seed=ft.Colors.BLUE,
            use_material3=True,
        )
        page.padding = 0
        page.spacing = 0
        # Desktop uses a phone-like portrait viewport. Mobile platforms use
        # the physical screen size and safely ignore these desktop defaults.
        page.window.width = 430
        page.window.height = 820
        page.window.min_width = 360
        page.window.min_height = 600

        # 初始化数据
        try:
            self.service.load()
        except FileNotFoundError:
            # 首次运行没有书源文件，创建空列表
            self.service.sources = []
        except Exception as e:
            page.show_snack_bar(
                ft.SnackBar(content=ft.Text(f"加载书源失败: {e}"),
                            bgcolor=ft.Colors.RED_100))

        # 创建视图
        self._discover_view = ReadingView(self.service, discovery_only=True)
        self._reading_view = ReadingView(self.service)
        self._book_source_view = BookSourceView(
            self.service,
            on_imported=self._on_imported,
            discover_view=self._discover_view,
        )

        # 主页容器
        content = ft.Container(
            content=self._reading_view,
            expand=True,
        )
        self._content_container = content

        # 底栏导航
        nav_bar = ft.NavigationBar(
            selected_index=1,
            on_change=self._on_nav_change,
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.LIBRARY_BOOKS_OUTLINED,
                    selected_icon=ft.Icons.LIBRARY_BOOKS,
                    label="书源",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.MENU_BOOK_OUTLINED,
                    selected_icon=ft.Icons.MENU_BOOK,
                    label="阅读",
                ),
            ],
        )

        # 页面布局
        page.add(
            ft.Column(
                spacing=0,
                expand=True,
                controls=[
                    content,
                ],
            ),
            nav_bar,
        )

        # FilePicker 作为服务注册到页面（Flet 0.86.x 服务化架构）
        page.services.append(self._book_source_view.file_picker)
        page.update()

    def _on_nav_change(self, e):
        """导航切换"""
        index = e.control.selected_index
        if index == self._current_index:
            return
        self._current_index = index

        if index == 0:
            self._content_container.content = self._book_source_view
        elif index == 1:
            # 此时 ReadingView 尚未挂载，不能调用 update。
            # 更新数据后由下方的 page.update() 一次性完成挂载和渲染。
            self._reading_view.refresh_bookshelf(update=False)
            self._content_container.content = self._reading_view

        self._page.update()

    def _on_imported(self, result: dict):
        """导入完成后的回调 - 显示提示（Tab 切换由 BookSourceView 内部处理）"""
        self._page.show_snack_bar(
            ft.SnackBar(
                content=ft.Text(
                    f"导入完成: 新增 {result['imported']} 条"
                    + (f", 重复 {result['duplicates']} 条" if result.get('duplicates') else "")
                ),
                bgcolor=ft.Colors.GREEN_100,
            )
        )
