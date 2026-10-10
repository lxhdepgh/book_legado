"""BookshelfView v2：对齐 Dart 版本的现代化书架（AppBar + FailureBanner + ListTile）。

红线（§7.5）：本文件**不 import 任何 app.services.***。记录以 dict 注入。
"""
from __future__ import annotations

import flet as ft

from ..common import EmptyState, FailureBanner


class BookshelfRow(ft.ListTile):
    """书架行（对齐 Dart 的 BookshelfRow）。

    三处"看得见的事实"是 G1 判据的比对照：
    - 行标题 = name
    - 行副标题 = 作者 · 读到 章名（缺一段就少一段）
    - trailing = 换源按钮（本地书不给）
    """

    def __init__(self, record: dict, on_open=None, on_switch=None) -> None:
        super().__init__()
        self._record = record
        self.on_open = on_open
        self.on_switch = on_switch

        # 封面占位符
        cover_url = record.get("coverUrl") or record.get("cover_url")
        if cover_url:
            self.leading = ft.Container(
                width=48,
                height=64,
                bgcolor=ft.Colors.GREY_300,
                border_radius=4,
                content=ft.Image(src=cover_url, fit=ft.ImageFit.COVER, error_content=ft.Icon(ft.Icons.BOOK)),
            )
        else:
            self.leading = ft.Container(
                width=48,
                height=64,
                bgcolor=ft.Colors.GREY_300,
                border_radius=4,
                content=ft.Icon(ft.Icons.BOOK, color=ft.Colors.GREY_600),
            )

        # 标题和副标题
        self.title = ft.Text(record.get("name", "未知"), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        
        author = record.get("author") or ""
        chapter = record.get("lastChapter") or record.get("last_chapter") or ""
        subtitle_parts = [p for p in [author, chapter] if p]
        self.subtitle = ft.Text(" · ".join(subtitle_parts), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS) if subtitle_parts else None

        # trailing：换源按钮（本地书不给）
        is_local = record.get("origin", "").startswith("localsource://")
        if not is_local:
            self.trailing = ft.IconButton(
                ft.Icons.SWAP_HORIZ,
                tooltip="换源",
                on_click=lambda e: self.on_switch(self._record) if self.on_switch else None,
            )

        # 点击开读
        self.on_click = lambda e: self.on_open(self._record) if self.on_open else None


class BookshelfViewV2(ft.Column):
    """书架 v2：AppBar + FailureBanner + ListView（对齐 Dart 架构）。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_open_book = None          # (record: dict) -> None
        self.on_refresh = None            # () -> None
        self.on_filter = None              # (query, sort, group) -> None
        self.on_toggle_layout = None      # () -> None
        self.on_request_switch = None     # (record: dict) -> None
        self.on_import_local = None       # () -> None

        # 本地书导入用
        self.file_picker = ft.FilePicker()

        # ------------------------------------------------------------------ #
        # AppBar（对齐 Dart 的 Scaffold.appBar）
        # ------------------------------------------------------------------ #
        self._layout_icon = ft.Icons.GRID_VIEW
        self._appbar = ft.AppBar(
            title=ft.Text("书架"),
            actions=[
                ft.IconButton(ft.Icons.UPLOAD_FILE, tooltip="导入本地书籍", on_click=lambda e: self._fire(self.on_import_local)),
                ft.IconButton(
                    key="bookshelf.layout_toggle",
                    icon=self._layout_icon,
                    tooltip="切换列表/网格",
                    on_click=self._toggle_layout,
                ),
                ft.IconButton(
                    key="bookshelf.refresh",
                    icon=ft.Icons.REFRESH,
                    tooltip="刷新",
                    on_click=lambda e: self._fire(self.on_refresh),
                ),
            ],
        )

        # ------------------------------------------------------------------ #
        # 错误横幅
        # ------------------------------------------------------------------ #
        self._failure_banner = FailureBanner()

        # ------------------------------------------------------------------ #
        # 筛选工具栏（简化版：搜索 + 排序 + 分组）
        # ------------------------------------------------------------------ #
        self._query = ft.TextField(
            hint_text="搜索书名或作者",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            height=42,
            expand=True,
            on_change=lambda e: self._emit_filter(),
        )
        self._group = ft.Dropdown(
            value="__all__",
            dense=True,
            expand=True,
            options=[ft.dropdown.Option("__all__", "全部")],
            on_select=lambda e: self._emit_filter(),
        )
        self._sort = ft.Dropdown(
            value="read",
            dense=True,
            expand=True,
            options=[
                ft.dropdown.Option("read", "最近阅读"),
                ft.dropdown.Option("update", "最近更新"),
                ft.dropdown.Option("name", "书名"),
            ],
            on_select=lambda e: self._emit_filter(),
        )
        self._filter_bar = ft.Row(
            controls=[self._query, self._group, self._sort],
            spacing=8,
        )
        self._filter_host = ft.Container(
            padding=ft.Padding(8, 4, 8, 4),
            content=self._filter_bar,
            visible=True,  # 默认显示
        )

        # ------------------------------------------------------------------ #
        # 状态行
        # ------------------------------------------------------------------ #
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._status_host = ft.Container(
            visible=False,
            padding=ft.Padding(12, 4, 12, 8),
            content=self._status,
        )

        # ------------------------------------------------------------------ #
        # 列表 / 网格
        # ------------------------------------------------------------------ #
        self._list = ft.ListView(expand=True, spacing=4)
        self._grid = ft.GridView(
            expand=True,
            max_extent=190,
            child_aspect_ratio=0.72,
            spacing=8,
            run_spacing=8,
            visible=False,
        )

        # ------------------------------------------------------------------ #
        # 空状态
        # ------------------------------------------------------------------ #
        self._empty_state = EmptyState(
            icon=ft.Icons.BOOK_OUTLINED,
            title="书架为空",
            subtitle="点击下方导入按钮添加书籍",
        )
        self._empty_host = ft.Container(
            content=self._empty_state,
            alignment=ft.Alignment.CENTER,
            expand=True,
            visible=False,
        )

        # ------------------------------------------------------------------ #
        # 组装
        # ------------------------------------------------------------------ #
        self.controls = [
            self._appbar,
            self._failure_banner,
            self._filter_host,
            self._status_host,
            ft.Stack(
                controls=[
                    self._list,
                    self._grid,
                    self._empty_host,
                ],
            ),
        ]
        self._layout = "list"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _emit_filter(self):
        self._fire(self.on_filter, self._query.value, self._sort.value, self._group.value)

    def _toggle_layout(self, e):
        if self._layout == "list":
            self._layout = "grid"
            self._list.visible = False
            self._grid.visible = True
            self._layout_icon = ft.Icons.VIEW_LIST
        else:
            self._layout = "list"
            self._list.visible = True
            self._grid.visible = False
            self._layout_icon = ft.Icons.GRID_VIEW
        
        # 更新 AppBar 图标
        self._appbar.actions[1].icon = self._layout_icon
        self.update()

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_books(self, books: list[dict]) -> None:
        """设置书架书籍列表。"""
        # 清空列表
        self._list.controls.clear()
        self._grid.controls.clear()

        if not books:
            self._empty_host.visible = True
            self._status_host.visible = False
        else:
            self._empty_host.visible = False
            
            # 填充列表视图
            for book in books:
                row = BookshelfRow(
                    record=book,
                    on_open=self.on_open_book,
                    on_switch=self.on_request_switch,
                )
                self._list.controls.append(row)

            # 填充网格视图
            for book in books:
                card = ft.Card(
                    content=ft.Container(
                        padding=8,
                        content=ft.Column(
                            controls=[
                                ft.Container(
                                    height=120,
                                    bgcolor=ft.Colors.GREY_300,
                                    border_radius=4,
                                    content=ft.Image(
                                        src=book.get("coverUrl") or book.get("cover_url"),
                                        fit=ft.ImageFit.COVER,
                                        error_content=ft.Icon(ft.Icons.BOOK),
                                    ) if book.get("coverUrl") or book.get("cover_url") else ft.Icon(ft.Icons.BOOK, size=48),
                                ),
                                ft.Text(book.get("name", "未知"), max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, size=13),
                            ],
                            spacing=4,
                        ),
                    ),
                    on_click=lambda e, b=book: self.on_open_book(b) if self.on_open_book else None,
                )
                self._grid.controls.append(card)

        # 更新状态行
        self.set_status_text(f"书架共 {len(books)} 本")

    def set_status_text(self, text: str) -> None:
        """设置状态行文本。"""
        self._status.value = text or ""
        self._status_host.visible = bool(text)

    def show_error(self, message: str) -> None:
        """显示错误横幅。"""
        self._failure_banner.show(message)

    def hide_error(self) -> None:
        """隐藏错误横幅。"""
        self._failure_banner.hide()

    def show_loading(self) -> None:
        """显示加载状态（禁用刷新按钮）。"""
        self._appbar.actions[2].enabled = False

    def hide_loading(self) -> None:
        """隐藏加载状态（启用刷新按钮）。"""
        self._appbar.actions[2].enabled = True
