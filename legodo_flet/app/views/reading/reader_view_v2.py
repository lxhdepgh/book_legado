"""ReaderView v2：对齐 Dart 架构（Drawer 目录 + BottomSheet 搜索 + FAB 朗读控制）。

红线（§7.5）：本文件**不 import 任何 app.services.***。正文以 dict 注入。
"""
from __future__ import annotations

import flet as ft


class ReaderViewV2(ft.Column):
    """阅读器 v2：AppBar + Drawer(目录) + 正文区 + BottomSheet(搜索)。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_open_toc = None             # () -> None（打开目录 Drawer）
        self.on_open_chapter = None         # (index: int) -> None
        self.on_save_progress = None        # (position: int) -> None
        self.on_open_search = None          # () -> None（打开搜索 BottomSheet）
        self.on_open_bookmarks = None       # () -> None
        self.on_speak = None                # () -> None（朗读）
        self.on_prev_chapter = None         # () -> None
        self.on_next_chapter = None         # () -> None
        self.on_change_font_size = None     # (delta: int) -> None

        # ------------------------------------------------------------------ #
        # AppBar
        # ------------------------------------------------------------------ #
        self._chapter_title = ft.Text("章节标题", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._appbar = ft.AppBar(
            title=self._chapter_title,
            leading=ft.IconButton(ft.Icons.MENU, tooltip="目录", on_click=lambda e: self._fire(self.on_open_toc)),
            actions=[
                ft.IconButton(ft.Icons.SEARCH, tooltip="搜索", on_click=lambda e: self._fire(self.on_open_search)),
                ft.IconButton(ft.Icons.BOOKMARK_BORDER, tooltip="书签", on_click=lambda e: self._fire(self.on_open_bookmarks)),
                ft.PopupMenuButton(
                    items=[
                        ft.PopupMenuItem(text="上一章", on_click=lambda e: self._fire(self.on_prev_chapter)),
                        ft.PopupMenuItem(text="下一章", on_click=lambda e: self._fire(self.on_next_chapter)),
                        ft.PopupMenuItem(text="调整字号", on_click=self._show_font_dialog),
                    ],
                ),
            ],
        )

        # ------------------------------------------------------------------ #
        # 正文区
        # ------------------------------------------------------------------ #
        self._content_text = ft.Text("", size=16, selectable=True)
        self._content_host = ft.Container(
            content=ft.ListView(
                controls=[self._content_text],
                expand=True,
                padding=ft.Padding(16, 16, 16, 16),
            ),
            expand=True,
        )

        # ------------------------------------------------------------------ #
        # 进度条
        # ------------------------------------------------------------------ #
        self._progress_slider = ft.Slider(min=0, max=100, value=0, divisions=100)
        self._progress_label = ft.Text("0%", size=12)
        self._progress_bar = ft.Row([
            self._progress_slider,
            self._progress_label,
        ])

        # ------------------------------------------------------------------ #
        # Floating Action Button（朗读控制）
        # ------------------------------------------------------------------ #
        self._fab = ft.FloatingActionButton(
            ft.Icons.VOLUME_UP,
            tooltip="朗读",
            on_click=lambda e: self._fire(self.on_speak),
        )

        # ------------------------------------------------------------------ #
        # 组装
        # ------------------------------------------------------------------ #
        self.controls = [
            self._appbar,
            self._content_host,
            self._progress_bar,
        ]

        # Drawer 和 BottomSheet 由 page 级别管理（见 navigation.py）
        self.drawer = None
        self.bottom_sheet = None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_chapter(self, title: str, content: str, position: int = 0, total: int = 100) -> None:
        """设置章节内容。"""
        self._chapter_title.value = title
        self._content_text.value = content
        
        # 更新进度
        progress = (position / total * 100) if total > 0 else 0
        self._progress_slider.value = progress
        self._progress_label.value = f"{progress:.0f}%"
        
        self.update()

    def set_toc_drawer(self, chapters: list[dict]) -> None:
        """设置目录 Drawer。"""
        toc_list = ft.ListView(
            controls=[
                ft.ListTile(
                    title=ft.Text(ch.get("title", "")),
                    subtitle=ft.Text(f"第 {i+1} 章"),
                    on_click=lambda e, idx=i: self._fire(self.on_open_chapter, idx),
                )
                for i, ch in enumerate(chapters)
            ],
        )
        
        self.drawer = ft.NavigationDrawer(
            controls=[
                ft.Container(padding=16, child=ft.Text("目录", size=20, weight=ft.FontWeight.BOLD)),
                ft.Divider(),
                toc_list,
            ],
        )

    def show_search_bottom_sheet(self, on_search=None, on_close=None) -> None:
        """显示搜索 BottomSheet。"""
        search_field = ft.TextField(
            hint_text="搜索关键词",
            prefix_icon=ft.Icons.SEARCH,
            autofocus=True,
            on_submit=lambda e: self._fire(on_search, search_field.value) if on_search else None,
        )
        
        self.bottom_sheet = ft.BottomSheet(
            content=ft.Container(
                padding=16,
                height=200,
                content=ft.Column([
                    search_field,
                    ft.ElevatedButton("关闭", on_click=lambda e: self._fire(on_close) if on_close else None),
                ]),
            ),
            enable_drag=True,
            show_drag_handle=True,
        )

    def hide_search_bottom_sheet(self) -> None:
        """隐藏搜索 BottomSheet。"""
        self.bottom_sheet = None
        self.update()

    def _show_font_dialog(self, e) -> None:
        """显示字号调整对话框。"""
        def adjust(delta: int):
            if self.on_change_font_size:
                self.on_change_font_size(delta)
        
        dlg = ft.AlertDialog(
            title=ft.Text("调整字号"),
            content=ft.Row([
                ft.IconButton(ft.Icons.REMOVE, on_click=lambda e: adjust(-1)),
                ft.Text("字号", size=16),
                ft.IconButton(ft.Icons.ADD, on_click=lambda e: adjust(1)),
            ]),
            actions=[
                ft.TextButton("关闭", on_click=lambda e: setattr(self.page, "dialog", None) if hasattr(self, "page") and self.page else None),
            ],
        )
        
        if hasattr(self, "page") and self.page:
            self.page.dialog = dlg
            self.page.update()

    def update_progress(self, position: int, total: int) -> None:
        """更新阅读进度。"""
        progress = (position / total * 100) if total > 0 else 0
        self._progress_slider.value = progress
        self._progress_label.value = f"{progress:.0f}%"
        self.update()
