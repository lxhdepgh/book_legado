"""
书源页面 - 使用 Tabs 合并管理视图和导入视图
"""
from __future__ import annotations

import flet as ft

from app.services.source_service import SourceService
from app.views.import_view import ImportView
from app.views.manage_view import ManageView
from app.views.check_view import CheckView


class BookSourceView(ft.Column):
    """书源 Tab 页：管理 + 导入"""

    def __init__(self, service: SourceService, on_imported=None):
        super().__init__(spacing=0, expand=True)
        self.service = service
        self._on_imported_cb = on_imported

        # 子视图
        self.manage_view = ManageView(service)
        self.import_view = ImportView(
            service,
            on_imported=self._on_import_tab_done,
        )
        self.check_view = CheckView(service)

        # 顶部 Tab 栏
        tab_bar = ft.TabBar(
            tabs=[
                ft.Tab(label="管理", icon=ft.Icons.LIBRARY_BOOKS),
                ft.Tab(label="导入", icon=ft.Icons.FILE_DOWNLOAD),
                ft.Tab(label="检测", icon=ft.Icons.FACT_CHECK_OUTLINED),
            ],
        )

        # 内容区
        tab_view = ft.TabBarView(
            expand=True,
            controls=[
                self.manage_view,
                self.import_view,
                self.check_view,
            ],
        )

        self._tabs = ft.Tabs(
            selected_index=0,
            length=3,
            expand=True,
            on_change=self._on_tab_change,
            content=ft.Column(
                spacing=0,
                expand=True,
                controls=[tab_bar, tab_view],
            ),
        )

        self.controls = [self._tabs]

    @property
    def file_picker(self):
        return self.import_view.file_picker

    def _on_tab_change(self, e):
        """Tab 切换事件"""
        if self._tabs.selected_index == 0:
            self.manage_view._refresh()

    def _on_import_tab_done(self, result: dict):
        """导入完成后切回管理 Tab"""
        self._tabs.selected_index = 0
        self.manage_view._refresh()
        self.update()

        if self._on_imported_cb:
            self._on_imported_cb(result)
