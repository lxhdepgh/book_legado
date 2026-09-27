"""BookshelfView：书架（网格/列表、封面、最近阅读卡、整书缓存管理、换封面、移除确认）。

自旧单体 ``ReadingView`` 的 shelf 职责拆出（§7.2）。换源入口保留为行管理菜单里
的一项——具体换源对话框由 :class:`~app.views.reading.source_switch_dialog.SourceSwitchDialog`
承载，其编排并入 :class:`~app.controllers.bookshelf_controller.BookshelfController`。

红线（§7.5）：本文件**不 import 任何 app.services.***。记录以 dict 注入。
"""
from __future__ import annotations

import flet as ft


class BookshelfView(ft.Column):
    """书架：工具栏（搜索/分组/排序/导入本地书/布局/刷新）+ 最近阅读卡 + 列表/网格切换。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_open_book = None          # (record: dict) -> None
        self.on_refresh = None            # () -> None
        self.on_filter = None              # (query, sort, group) -> None
        self.on_toggle_layout = None      # () -> None
        self.on_request_switch = None     # (record: dict) -> None
        self.on_toggle_cache = None       # (record: dict) -> None
        self.on_cancel_cache = None       # (task_id: str) -> None
        self.on_request_remove = None     # (record: dict) -> None
        self.on_request_cover = None      # (record: dict) -> None（挑一张本地图片当封面）
        self.on_import_local = None       # () -> None

        # 本地书导入用（navigation.py 把它 append 进 page.services，非 overlay）
        self.file_picker = ft.FilePicker()

        # ------------------------------------------------------------------ #
        # 工具栏
        # ------------------------------------------------------------------ #
        self._query = ft.TextField(
            hint_text="搜索书名或作者", prefix_icon=ft.Icons.SEARCH,
            dense=True, height=42, expand=True,
            on_change=lambda e: self._emit_filter())
        self._group = ft.Dropdown(
            value="__all__", width=130, dense=True,
            options=[ft.dropdown.Option("__all__", "全部")], on_select=lambda e: self._emit_filter())
        self._sort = ft.Dropdown(
            value="read", width=150, dense=True,
            options=[
                ft.dropdown.Option("read", "最近阅读"),
                ft.dropdown.Option("update", "最近更新"),
                ft.dropdown.Option("name", "书名"),
            ], on_select=lambda e: self._emit_filter())
        self._layout_btn = ft.IconButton(
            ft.Icons.GRID_VIEW, tooltip="切换列表/网格",
            on_click=lambda e: self._fire(self.on_toggle_layout))
        self._refresh_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="刷新书架目录",
            on_click=lambda e: self._fire(self.on_refresh))
        self._import_btn = ft.IconButton(
            ft.Icons.UPLOAD_FILE, tooltip="导入本地书籍（txt / epub / pdf）",
            on_click=lambda e: self._fire(self.on_import_local))
        self._toolbar = ft.Container(
            padding=ft.Padding(8, 4, 8, 4),
            content=ft.ResponsiveRow([
                self._query, self._group, self._sort,
                self._import_btn, self._layout_btn, self._refresh_btn,
            ], columns=12, spacing=6, run_spacing=4))

        # 最近阅读卡（ProgressSavedEvent 驱动，Controller 调 set_continue_reading）
        self._continue_host = ft.Container(visible=False)

        # 状态行（整书缓存进度/换源结果等，常驻底部）
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        # 在途整书缓存任务（Controller 从 DownloadService 的快照渲染，
        # 切页回来也看得见；每条一行、各自可取消，不是一行"最新进度"）
        self._task_host = ft.Column(tight=True, spacing=4, visible=False)

        # 列表 / 网格两份容器，切 layout 时换可见性
        self._list = ft.ListView(expand=True, spacing=4)
        self._grid = ft.GridView(
            expand=True, max_extent=190, child_aspect_ratio=0.72,
            spacing=8, run_spacing=8, visible=False)

        self.controls = [
            self._toolbar,
            self._continue_host,
            self._task_host,
            ft.Divider(height=1),
            self._list,
            self._grid,
            ft.Container(padding=ft.Padding(12, 2, 12, 6), content=self._status),
        ]
        self._layout = "list"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _emit_filter(self):
        self._fire(self.on_filter, self._query.value,
                   self._sort.value, self._group.value)

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_status_text(self, text: str) -> None:
        self._status.value = text or ""

    def set_cache_tasks(self, tasks: list[dict]) -> None:
        """渲染在途整书缓存任务：``{task_id, name, done, total, progress}``。

        空列表时整块隐藏（不留一个灰框占位）。每条一行——两本书并行缓存时
        一行"最新进度"说不清是哪本。
        """
        self._task_host.controls.clear()
        self._task_host.visible = bool(tasks)
        for task in tasks or []:
            task_id = str(task.get("task_id", ""))
            total = int(task.get("total") or 0)
            done = int(task.get("done") or 0)
            percent = int(float(task.get("progress") or 0) * 100)
            self._task_host.controls.append(ft.Container(
                padding=ft.Padding(12, 2, 12, 2),
                bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.PRIMARY),
                content=ft.Row([
                    ft.Icon(ft.Icons.DOWNLOADING, size=18,
                            color=ft.Colors.GREY_700),
                    ft.Text(str(task.get("name") or "（未知书）"),
                            size=13, expand=True, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.ProgressBar(value=percent / 100, width=120),
                    ft.Text(f"{percent}%" + (f" {done}/{total}章" if total else ""),
                            size=12, color=ft.Colors.GREY_600),
                    ft.TextButton("取消", on_click=lambda e, t=task_id:
                                  self._fire(self.on_cancel_cache, t)),
                ], spacing=8, alignment=ft.MainAxisAlignment.START)))

    def render_books(self, records: list[dict]) -> None:
        """渲染书架记录；记录含 name/author/url/source_url/chapter_index 等。"""
        self._list.controls.clear()
        self._grid.controls.clear()
        if not records:
            self._list.controls.append(self._empty_control())
            return
        for record in records:
            self._list.controls.append(self._row(record))
            self._grid.controls.append(self._cell(record))

    def _empty_control(self) -> ft.Control:
        return ft.Container(
            padding=40, alignment=ft.Alignment.CENTER,
            content=ft.Column([
                ft.Icon(ft.Icons.LIBRARY_BOOKS_OUTLINED, size=48,
                        color=ft.Colors.GREY_400),
                ft.Text("书架是空的：去「找书」淘一本，或点工具栏「导入本地书籍」",
                        color=ft.Colors.GREY_500),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8))

    def _cover_control(self, record: dict, *, width: float, height: float,
                       fallback: ft.Control) -> ft.Control:
        """有封面地址就上 ``ft.Image``，取不到（离线/防盗链 403/路径不可读）回落图标。

        视图只把 ``cover_url`` 当字符串交给 Flet，不判断它是本地文件还是远端 URL
        ——认地址是控制器与服务的事（§7.5 红线：这里不 import services、不碰磁盘）。
        ``error_content`` 挂的就是原来那个图标：所以最坏情况等于本行没这次改动。
        """
        src = str(record.get("cover_url") or "")
        if not src:
            return fallback
        return ft.Image(src=src, width=width, height=height,
                        fit=ft.BoxFit.COVER, border_radius=6,
                        error_content=fallback)

    def _row(self, record: dict) -> ft.Control:
        icon = ft.Icon(ft.Icons.FOLDER_OPEN if record.get("local")
                       else ft.Icons.BOOK)
        return ft.ListTile(
            leading=ft.Container(
                width=44, alignment=ft.Alignment.CENTER,
                content=self._cover_control(record, width=40, height=56,
                                            fallback=icon)),
            title=ft.Text(record.get("name", ""), max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(
                " · ".join(p for p in (record.get("author", ""),
                                       record.get("chapter_name", "")) if p),
                size=11, color=ft.Colors.GREY_500, max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS),
            on_click=lambda e, r=record: self._fire(self.on_open_book, r),
            on_long_press=lambda e, r=record: self._open_manage(r),
        )

    def _cell(self, record: dict) -> ft.Control:
        icon = ft.Icon(ft.Icons.FOLDER_OPEN if record.get("local")
                       else ft.Icons.BOOK, size=40,
                       color=ft.Colors.ON_SURFACE_VARIANT)
        return ft.Container(
            border_radius=8, padding=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=lambda e, r=record: self._fire(self.on_open_book, r),
            on_long_press=lambda e, r=record: self._open_manage(r),
            content=ft.Column([
                ft.Container(
                    height=132, alignment=ft.Alignment.CENTER,
                    content=self._cover_control(record, width=110, height=132,
                                                fallback=icon)),
                ft.Text(record.get("name", ""), size=12, max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    def set_layout(self, mode: str) -> None:
        """list / grid 切换（换源入口在行管理菜单里，不在此）。"""
        self._layout = mode
        self._list.visible = mode == "list"
        self._grid.visible = mode == "grid"
        self._layout_btn.icon = (ft.Icons.VIEW_LIST if mode == "grid"
                                  else ft.Icons.GRID_VIEW)

    def set_continue_reading(self, book: dict | None) -> None:
        """最近阅读卡（ProgressSavedEvent 驱动）；None 隐藏。"""
        if not book:
            self._continue_host.visible = False
            return
        self._continue_host.visible = True
        self._continue_host.content = ft.Container(
            padding=ft.Padding(12, 8, 12, 8), border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=lambda e, b=book: self._fire(self.on_open_book, b),
            content=ft.Row([
                ft.Icon(ft.Icons.HISTORY),
                ft.Column([
                    ft.Text("继续阅读", size=11, color=ft.Colors.GREY_600),
                    ft.Text(book.get("name", ""), weight=ft.FontWeight.W_500),
                ], spacing=2, expand=True),
                ft.Icon(ft.Icons.PLAY_ARROW),
            ], spacing=10))

    # ------------------------------------------------------------------ #
    # 行管理菜单（换源 / 整书缓存 / 换封面 / 移除）——动作回调由 Controller 注入
    # ------------------------------------------------------------------ #
    def _open_manage(self, record: dict) -> None:
        """长按弹出管理动作菜单（按钮即回调，不在视图内做业务）。"""
        dialog = self.build_manage_dialog(record)
        self._show_dialog(dialog)

    def build_manage_dialog(self, record: dict) -> ft.AlertDialog:
        name = record.get("name", "")
        rows = []
        # 本地书没有规则源（换源无从谈起），正文也已经在磁盘上（无需整书缓存）；
        # 是不是本地书由控制器判定后随记录注入，视图不认得 localsource://。
        if not record.get("local"):
            rows.append(
                ft.ListTile(leading=ft.Icon(ft.Icons.SWAP_HORIZ),
                            title=ft.Text("换源"),
                            on_click=lambda e, r=record: self._close_and(
                                self.on_request_switch, r)))
            rows.append(
                ft.ListTile(leading=ft.Icon(ft.Icons.FILE_DOWNLOAD),
                            title=ft.Text("整书缓存 / 取消缓存"),
                            on_click=lambda e, r=record: self._close_and(
                                self.on_toggle_cache, r)))
        # 封面本地书也能换（导入的 epub 没封面时更需要），所以这一行不分本地/联网。
        rows.append(
            ft.ListTile(leading=ft.Icon(ft.Icons.IMAGE_OUTLINED),
                        title=ft.Text("更换封面"),
                        on_click=lambda e, r=record: self._close_and(
                            self.on_request_cover, r)))
        rows.append(
            ft.ListTile(leading=ft.Icon(ft.Icons.DELETE_OUTLINE),
                        title=ft.Text("移出书架", color=ft.Colors.RED_400),
                        on_click=lambda e, r=record: self._close_and(
                            self.on_request_remove, r)))
        return ft.AlertDialog(
            modal=True,
            title=ft.Text(name, size=16, weight=ft.FontWeight.BOLD),
            content=ft.Column(rows, tight=True, width=320),
            actions=[ft.TextButton("关闭", on_click=lambda e: self._close_dialog())],
        )

    def confirm_remove(self, record: dict, on_confirm=None) -> ft.AlertDialog:
        """移出书架确认框。"""
        extra = ("只移出书架，磁盘上的文件与已解析的缓存都不会删除"
                 if record.get("local") else
                 "仅移除记录，不删除本地缓存")
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("移出书架"),
            content=ft.Text(f"确定把《{record.get('name', '')}》移出书架吗？"
                            f"（{extra}）"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dialog()),
                ft.TextButton("移出", on_click=lambda e, r=record: self._close_and(
                    on_confirm, r)),
            ],
        )

    # 对话框挂载细节（page 由 Flet 注入；离线未挂载时静默） --------------- #
    def _page(self):
        try:
            return self.page
        except Exception:
            return None

    def _show_dialog(self, dialog: ft.AlertDialog) -> None:
        page = self._page()
        if page is not None:
            page.show_dialog(dialog)

    def _close_dialog(self) -> None:
        page = self._page()
        if page is not None:
            page.pop_dialog()

    def _close_and(self, callback, record):
        self._close_dialog()
        self._fire(callback, record)
