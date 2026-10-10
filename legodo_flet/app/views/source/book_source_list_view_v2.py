"""BookSourceListView v2：使用 DataTable + PaginatedDataTable（对齐 Dart 架构）。

红线（§7.5）：本文件**不 import 任何 app.services.***。记录以 dict 注入。
"""
from __future__ import annotations

import flet as ft


class BookSourceDataSource(ft.DataTableSource):
    """书源数据源（用于 PaginatedDataTable，支持虚拟滚动）。"""

    def __init__(self, sources: list[dict], parent) -> None:
        super().__init__()
        self._sources = sources
        self._parent = parent

    def get_row(self, index: int) -> ft.DataRow | None:
        if index < 0 or index >= len(self._sources):
            return None
        
        record = self._sources[index]
        return self._parent._build_data_row(record)

    @property
    def row_count(self) -> int:
        return len(self._sources)


class BookSourceListViewV2(ft.Column):
    """书源管理 v2：AppBar + PaginatedDataTable + 批量操作栏。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        #: 当前选中书源 url 集合（视图渲染态；Controller 维护）
        self._selected: set[str] = set()
        self._all_sources: list[dict] = []  # 全部数据源

        # -- 回调槽（Controller 填充；视图绝不自行取服务） --
        self.on_refresh = None              # () -> None
        self.on_search = None               # (query: str) -> None
        self.on_group_changed = None        # (group: str) -> None
        self.on_state_filter = None         # (mode: str) -> None
        self.on_toggle_enabled = None       # (record: dict, enabled: bool) -> None
        self.on_edit = None                 # (record: dict) -> None
        self.on_delete = None               # (record: dict) -> None
        self.on_import_json = None          # () -> None
        self.on_export_json = None          # () -> None
        self.on_start_check = None          # () -> None
        self.on_start_repair = None         # () -> None
        # -- 批量选择 --
        self.on_selection_changed = None    # (url: str, checked: bool) -> None
        self.on_select_all = None           # () -> None
        self.on_clear_selection = None      # () -> None
        self.on_batch_enable = None         # () -> None
        self.on_batch_disable = None        # () -> None
        self.on_batch_delete = None         # () -> None

        # ------------------------------------------------------------------ #
        # AppBar（对齐 Dart 的 Scaffold.appBar）
        # ------------------------------------------------------------------ #
        self._search_query = ""
        self._appbar = ft.AppBar(
            title=ft.Text("书源管理"),
            actions=[
                ft.IconButton(ft.Icons.FILE_OPEN, tooltip="导入", on_click=lambda e: self._fire(self.on_import_json)),
                ft.IconButton(ft.Icons.UPLOAD, tooltip="导出", on_click=lambda e: self._fire(self.on_export_json)),
                ft.IconButton(ft.Icons.FACT_CHECK_OUTLINED, tooltip="检测", on_click=lambda e: self._fire(self.on_start_check)),
                ft.IconButton(ft.Icons.HEALING_OUTLINED, tooltip="自愈", on_click=lambda e: self._fire(self.on_start_repair)),
                ft.IconButton(ft.Icons.REFRESH, tooltip="刷新", on_click=lambda e: self._fire(self.on_refresh)),
            ],
        )

        # ------------------------------------------------------------------ #
        # 筛选工具栏（简化版）
        # ------------------------------------------------------------------ #
        self._search_field = ft.TextField(
            hint_text="搜索书名 / URL / 分组",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            height=42,
            expand=True,
            on_submit=lambda e: self._apply_filter(),
        )
        self._group_dropdown = ft.Dropdown(
            value="",
            hint_text="全部分组",
            dense=True,
            expand=True,
            options=[ft.dropdown.Option("", "全部分组")],
            on_select=lambda e: self._apply_filter(),
        )
        self._state_dropdown = ft.Dropdown(
            value="",
            dense=True,
            expand=True,
            options=[
                ft.dropdown.Option("", "全部状态"),
                ft.dropdown.Option("enabled", "仅启用"),
                ft.dropdown.Option("disabled", "仅停用"),
            ],
            on_select=lambda e: self._apply_filter(),
        )
        self._filter_bar = ft.Row(
            controls=[self._search_field, self._group_dropdown, self._state_dropdown],
            spacing=8,
        )
        self._filter_host = ft.Container(
            padding=ft.Padding(12, 8, 12, 8),
            content=self._filter_bar,
        )

        # ------------------------------------------------------------------ #
        # 批量操作栏
        # ------------------------------------------------------------------ #
        self._batch_count = ft.Text("已选择 0 项", size=13)
        self._batch_bar = ft.Container(
            visible=False,
            padding=ft.Padding(12, 4, 12, 4),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Row([
                self._batch_count,
                ft.FilledTonalButton(
                    "启用", icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                    on_click=lambda e: self._fire(self.on_batch_enable)),
                ft.FilledTonalButton(
                    "禁用", icon=ft.Icons.CANCEL_OUTLINED,
                    on_click=lambda e: self._fire(self.on_batch_disable)),
                ft.FilledTonalButton(
                    "删除", icon=ft.Icons.DELETE_OUTLINE, color=ft.Colors.RED,
                    on_click=lambda e: self._fire(self.on_batch_delete)),
                ft.OutlinedButton(
                    "取消选择", icon=ft.Icons.CLOSE,
                    on_click=lambda e: self._fire(self.on_clear_selection)),
            ], spacing=8, wrap=True),
        )

        # ------------------------------------------------------------------ #
        # PaginatedDataTable（支持分页、排序、虚拟滚动）
        # ------------------------------------------------------------------ #
        self._data_source = BookSourceDataSource([], self)
        self._table = ft.PaginatedDataTable(
            header=ft.Text("书源列表"),
            columns=[
                ft.DataColumn(
                    ft.Text("名称"),
                    on_sort=lambda col, asc: self._sort_by_name(asc),
                ),
                ft.DataColumn(
                    ft.Text("URL"),
                    numeric=True,
                ),
                ft.DataColumn(
                    ft.Text("分组"),
                    on_sort=lambda col, asc: self._sort_by_group(asc),
                ),
                ft.DataColumn(
                    ft.Text("状态"),
                ),
                ft.DataColumn(
                    ft.Text("操作"),
                ),
            ],
            source=self._data_source,
            rows_per_page=50,
            available_rows_per_page=[25, 50, 100],
            column_spacing=16,
            horizontal_margin=12,
        )

        # ------------------------------------------------------------------ #
        # 空状态
        # ------------------------------------------------------------------ #
        from ..common import EmptyState
        self._empty_state = EmptyState(
            icon=ft.Icons.LIBRARY_BOOKS_OUTLINED,
            title="暂无书源",
            subtitle="点击导入按钮添加书源",
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
            self._filter_host,
            self._batch_bar,
            ft.Stack(
                controls=[
                    self._table,
                    self._empty_host,
                ],
            ),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _apply_filter(self):
        """应用筛选条件。"""
        query = self._search_field.value or ""
        group = self._group_dropdown.value or ""
        state = self._state_dropdown.value or ""
        
        # 通知 Controller
        if self.on_search:
            self.on_search(query)
        if self.on_group_changed:
            self.on_group_changed(group)
        if self.on_state_filter:
            self.on_state_filter(state)

    def _sort_by_name(self, ascending: bool):
        """按名称排序。"""
        self._all_sources.sort(key=lambda s: s.get("bookSourceName", ""), reverse=not ascending)
        self._refresh_table()

    def _sort_by_group(self, ascending: bool):
        """按分组排序。"""
        self._all_sources.sort(key=lambda s: s.get("bookSourceGroup", ""), reverse=not ascending)
        self._refresh_table()

    # ------------------------------------------------------------------ #
    # 构建 DataRow
    # ------------------------------------------------------------------ #
    def _build_data_row(self, record: dict) -> ft.DataRow:
        """构建单行数据。"""
        url = record.get("bookSourceUrl", "")
        name = record.get("bookSourceName", "未知")
        group = record.get("bookSourceGroup", "")
        enabled = record.get("enabled", True)

        is_selected = url in self._selected

        return ft.DataRow(
            cells=[
                ft.DataCell(
                    ft.Row([
                        ft.Checkbox(value=is_selected, on_change=lambda e, u=url: self._toggle_selection(u, e.data == "true")),
                        ft.Text(name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ]),
                ),
                ft.DataCell(ft.Text(url, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, selectable=True)),
                ft.DataCell(ft.Text(group)),
                ft.DataCell(self._status_chip(enabled)),
                ft.DataCell(self._action_buttons(record)),
            ],
        )

    def _status_chip(self, enabled: bool) -> ft.Control:
        """状态标签。"""
        from ..common import StatusChip
        return StatusChip(label="启用" if enabled else "禁用", color="green" if enabled else "grey")

    def _action_buttons(self, record: dict) -> ft.Control:
        """操作按钮。"""
        url = record.get("bookSourceUrl", "")
        enabled = record.get("enabled", True)

        return ft.Row([
            ft.IconButton(
                ft.Icons.EDIT,
                tooltip="编辑",
                on_click=lambda e, r=record: self._fire(self.on_edit, r),
            ),
            ft.IconButton(
                ft.Icons.DELETE,
                tooltip="删除",
                on_click=lambda e, r=record: self._fire(self.on_delete, r),
            ),
            ft.Switch(
                value=enabled,
                on_change=lambda e, r=record: self._fire(self.on_toggle_enabled, r, e.data == "true"),
            ),
        ], spacing=0)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def render_sources(self, records: list) -> None:
        """渲染书源记录列表。"""
        self._all_sources = [self._as_dict(r) for r in records]
        self._data_source = BookSourceDataSource(self._all_sources, self)
        self._table.source = self._data_source
        
        # 更新空状态
        self._empty_host.visible = len(self._all_sources) == 0
        
        # 更新批量操作栏
        self._update_batch_bar()
        
        self.update()

    def set_groups(self, groups: list[str]) -> None:
        """刷新分组下拉选项。"""
        current = self._group_dropdown.value
        self._group_dropdown.options = [ft.dropdown.Option("", "全部分组")] + [
            ft.dropdown.Option(g, g) for g in (groups or [])
        ]
        if current in (groups or []):
            self._group_dropdown.value = current
        self.update()

    def set_stats(self, stats: dict) -> None:
        """状态栏统计（PaginatedDataTable 自带计数，此处可省略）。"""
        pass

    def set_status(self, text: str) -> None:
        """设置状态文本（未使用）。"""
        pass

    def _refresh_table(self):
        """刷新表格数据源。"""
        self._data_source = BookSourceDataSource(self._all_sources, self)
        self._table.source = self._data_source
        self.update()

    # ------------------------------------------------------------------ #
    # 批量选择
    # ------------------------------------------------------------------ #
    def _toggle_selection(self, url: str, checked: bool) -> None:
        """切换单个书源选中状态。"""
        if checked:
            self._selected.add(url)
        else:
            self._selected.discard(url)
        
        if self.on_selection_changed:
            self.on_selection_changed(url, checked)
        
        self._update_batch_bar()

    def select_all(self) -> None:
        """全选当前显示的书源。"""
        for record in self._all_sources:
            url = record.get("bookSourceUrl", "")
            self._selected.add(url)
            if self.on_selection_changed:
                self.on_selection_changed(url, True)
        
        self._update_batch_bar()
        self._refresh_table()

    def clear_selection(self) -> None:
        """清空选择。"""
        self._selected.clear()
        self._update_batch_bar()
        self._refresh_table()

    def _update_batch_bar(self) -> None:
        """更新批量操作栏可见性和计数。"""
        count = len(self._selected)
        self._batch_count.value = f"已选择 {count} 项"
        self._batch_bar.visible = count > 0
        self.update()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_dict(record) -> dict:
        """记录归一：dict 原样；模型转 to_dict()。"""
        if isinstance(record, dict):
            return record
        to_dict = getattr(record, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return dict(record)
