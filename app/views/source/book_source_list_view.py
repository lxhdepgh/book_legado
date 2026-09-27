"""BookSourceListView：书源列表 / 分组 / 启用停用 / 导入导出 / 检测入口（M3，契约 §9）。

自旧冻结视图 ``manage_view.py`` 的列表/分组/批量操作职责拆出（行为参照，只读不改旧文件）。
本视图是**轻壳**：只渲染 dict 记录与发出回调，不做任何业务——
数据经所属 :class:`~app.controllers.source.book_source_list_controller.BookSourceListController`
从 ServiceHub 取服务注入（红线 §7.5：本文件零 app.services.* import）。

记录结构（dict）：bookSourceName / bookSourceUrl / bookSourceGroup / enabled。
"""
from __future__ import annotations

import flet as ft


class BookSourceListView(ft.Column):
    """书源管理：搜索/分组/状态过滤 + 卡片列表 + 批量操作 + 导入导出/检测入口。"""

    #: 每批懒加载行数（对齐旧 manage_view.LOAD_BATCH_SIZE 体感）。
    BATCH_SIZE = 50

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        #: 当前选中书源 url 集合（视图渲染态；Controller 维护）
        self._selected: set[str] = set()

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
        # -- 批量选择（对齐旧 manage_view 选择模式） --
        self.on_selection_changed = None    # (url: str, checked: bool) -> None
        self.on_select_all = None           # () -> None
        self.on_clear_selection = None      # () -> None
        self.on_batch_enable = None         # () -> None
        self.on_batch_disable = None        # () -> None
        self.on_batch_delete = None         # () -> None

        # ------------------------------------------------------------------ #
        # 工具栏：搜索 / 分组 / 状态过滤 / 导入导出 / 检测入口
        # ------------------------------------------------------------------ #
        self._search = ft.TextField(
            hint_text="搜索书源名称 / URL / 分组 / 备注",
            prefix_icon=ft.Icons.SEARCH, dense=True, height=44, expand=True,
            on_change=lambda e: self._fire(self.on_search, self._search.value))
        self._group = ft.Dropdown(
            value="", hint_text="全部分组", dense=True, width=150,
            options=[ft.dropdown.Option("", "全部分组")],
            on_select=lambda e: self._fire(self.on_group_changed, self._group.value))
        self._state = ft.Dropdown(
            value="", dense=True, width=130,
            options=[
                ft.dropdown.Option("", "全部状态"),
                ft.dropdown.Option("enabled", "仅启用"),
                ft.dropdown.Option("disabled", "仅停用"),
            ],
            on_select=lambda e: self._fire(self.on_state_filter, self._state.value))
        self._refresh_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="刷新",
            on_click=lambda e: self._fire(self.on_refresh))
        self._select_all_btn = ft.OutlinedButton(
            "全选筛选结果", icon=ft.Icons.SELECT_ALL,
            tooltip="选中当前筛选结果中的全部书源",
            on_click=lambda e: self._fire(self.on_select_all))
        self._import_btn = ft.OutlinedButton(
            "导入", icon=ft.Icons.FILE_OPEN,
            on_click=lambda e: self._fire(self.on_import_json, e))
        self._export_btn = ft.OutlinedButton(
            "导出", icon=ft.Icons.UPLOAD,
            on_click=lambda e: self._fire(self.on_export_json, e))
        self._check_btn = ft.FilledTonalButton(
            "检测", icon=ft.Icons.FACT_CHECK_OUTLINED,
            on_click=lambda e: self._fire(self.on_start_check, e))
        self._repair_btn = ft.FilledTonalButton(
            "自愈", icon=ft.Icons.HEALING_OUTLINED,
            on_click=lambda e: self._fire(self.on_start_repair, e))
        self._toolbar = ft.Container(
            padding=ft.Padding(12, 10, 12, 6),
            content=ft.Column(spacing=6, controls=[
                # 过滤行：搜索 / 分组 / 状态 / 刷新（12 列网格均分）
                ft.ResponsiveRow([
                    ft.Container(self._search, col={"xs": 12, "sm": 6, "lg": 7}),
                    ft.Container(self._group, col={"xs": 6, "sm": 3, "lg": 2}),
                    ft.Container(self._state, col={"xs": 6, "sm": 2, "lg": 2}),
                    ft.Container(self._refresh_btn,
                                 col={"xs": 6, "sm": 1, "lg": 1}),
                ], columns=12, spacing=6, run_spacing=4),
                # 操作行：全选 / 导入 / 导出 / 检测 / 自愈（wrap 防窄窗裁剪）
                ft.Row([
                    self._select_all_btn, self._import_btn,
                    self._export_btn, self._check_btn,
                    self._repair_btn,
                ], spacing=8, wrap=True),
            ]))
        # 批量操作栏（有选中时显示，对齐旧 manage_view._batch_bar）
        self._batch_count = ft.Text("已选择 0 项", size=13)
        self._batch_bar = ft.Container(
            visible=False,
            padding=ft.Padding(12, 2, 12, 2),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=8,
            content=ft.Row([
                self._batch_count,
                ft.FilledTonalButton(
                    "启用", icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                    on_click=lambda e: self._fire(self.on_batch_enable)),
                ft.FilledTonalButton(
                    "禁用", icon=ft.Icons.CANCEL_OUTLINED,
                    on_click=lambda e: self._fire(self.on_batch_disable)),
                ft.FilledTonalButton(
                    "删除", icon=ft.Icons.DELETE_OUTLINE,
                    color=ft.Colors.RED,
                    on_click=lambda e: self._fire(self.on_batch_delete)),
                ft.OutlinedButton(
                    "取消选择", icon=ft.Icons.CLOSE,
                    on_click=lambda e: self._fire(self.on_clear_selection)),
            ], spacing=8, wrap=True))

        # 列表区（记录以 dict 注入，视图不持有业务对象）
        self._cards = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)

        # 状态栏
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        self.controls = [
            self._toolbar,
            self._batch_bar,
            ft.Divider(height=1),
            ft.Container(expand=True, padding=ft.Padding(12, 0, 12, 0),
                         content=self._cards),
            ft.Container(padding=ft.Padding(12, 4, 12, 6), content=self._status),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_dict(record) -> dict:
        """记录归一：dict 原样；BookSource 模型转 to_dict()（服务返回模型对象）。"""
        if isinstance(record, dict):
            return record
        to_dict = getattr(record, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return dict(record)

    def render_sources(self, records: list) -> None:
        """渲染书源记录列表（dict 或 BookSource 模型均兼容，视图侧归一）。"""
        self._cards.controls.clear()
        if not records:
            self._cards.controls.append(self._empty())
            return
        for record in records:
            self._cards.controls.append(self._row(self._as_dict(record)))

    def set_groups(self, groups: list[str]) -> None:
        """刷新分组下拉选项。"""
        current = self._group.value
        self._group.options = [ft.dropdown.Option("", "全部分组")] + [
            ft.dropdown.Option(g, g) for g in (groups or [])]
        if current in (groups or []):
            self._group.value = current

    def set_stats(self, stats: dict) -> None:
        """状态栏统计（total/enabled/disabled/groups/filtered）。"""
        if not stats:
            return
        self._status.value = (
            f"共 {stats.get('total', 0)} 条 | 启用 {stats.get('enabled', 0)} | "
            f"停用 {stats.get('disabled', 0)} | {stats.get('groups', 0)} 个分组")

    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    # ------------------------------------------------------------------ #
    def _empty(self) -> ft.Control:
        return ft.Container(
            padding=40, alignment=ft.Alignment.CENTER,
            content=ft.Column([
                ft.Icon(ft.Icons.LIBRARY_BOOKS_OUTLINED, size=48,
                        color=ft.Colors.GREY_400),
                ft.Text("书源列表为空，点「导入」从 JSON 文件或 URL 添加",
                        color=ft.Colors.GREY_500),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8))

    def _row(self, record: dict) -> ft.Control:
        record = self._as_dict(record)  # 回调闭包也归一（BookSource 模型兼容）
        name = record.get("bookSourceName") or record.get("bookSourceUrl", "")
        url = record.get("bookSourceUrl", "")
        group = record.get("bookSourceGroup") or ""
        enabled = bool(record.get("enabled", True))
        return ft.Card(
            content=ft.Container(
                padding=ft.Padding(10, 6, 10, 6),
                content=ft.Row([
                    ft.Checkbox(
                        value=url in self._selected, tooltip="选择以批量操作",
                        on_change=lambda e, u=url: self._fire(
                            self.on_selection_changed, u, e.control.value)),
                    ft.Column([
                        ft.Text(name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                weight=ft.FontWeight.W_500,
                                color=ft.Colors.GREY_400 if not enabled else None),
                        ft.Text(" · ".join(p for p in (group, self._short_url(url)) if p),
                                size=11, color=ft.Colors.GREY_600, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=2, expand=True),
                    ft.IconButton(ft.Icons.EDIT, tooltip="编辑规则",
                                  on_click=lambda e, r=record: self._fire(self.on_edit, r)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="删除书源",
                                  on_click=lambda e, r=record: self._fire(self.on_delete, r)),
                    ft.Switch(
                        value=enabled, tooltip="启用/停用",
                        on_change=lambda e, r=record: self._fire(
                            self.on_toggle_enabled, r, e.control.value)),
                ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)))

    # ------------------------------------------------------------------ #
    # 批量选择渲染接口
    # ------------------------------------------------------------------ #
    def set_selected(self, urls: set) -> None:
        """Controller 维护选中集后回写视图渲染态。"""
        self._selected = set(str(u) for u in (urls or []) if u)

    def update_batch_bar(self, count: int) -> None:
        """刷新批量栏（count>0 显示，附带选中数）。"""
        self._batch_count.value = f"已选择 {count} 项"
        self._batch_bar.visible = int(count or 0) > 0

    @staticmethod
    def _short_url(url: str, limit: int = 48) -> str:
        url = str(url or "")
        return url if len(url) <= limit else url[:limit - 1] + "…"
