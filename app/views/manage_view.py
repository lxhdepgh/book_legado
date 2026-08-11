"""
书源管理页面 - 浏览、搜索、批量操作书源
"""
from __future__ import annotations

import asyncio
import flet as ft

from app.ui_feedback import show_snack_bar

from app.models.book_source import BookSource
from app.services.source_service import SourceService

class ManageView(ft.Column):
    """书源管理页面"""

    LOAD_BATCH_SIZE = 50
    SEARCH_DEBOUNCE_SECONDS = 0.18

    def __init__(self, service: SourceService):
        super().__init__(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True,
                         on_scroll=self._on_scroll, scroll_interval=150)
        self.service = service
        self._filtered: list[BookSource] = []
        self._page = 0
        self._selected = set()  # 选中行的索引集合（在 _filtered 中的位置）
        self._select_all = False
        self._visible_count = self.LOAD_BATCH_SIZE
        self._search_generation = 0

        # ── 顶部控制栏 ──
        self._search_field = ft.TextField(
            hint_text="搜索书源名称/URL/分组/备注...",
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._on_search,
            expand=True,
            height=48,
        )
        self._group_filter = ft.Dropdown(
            hint_text="全部分组",
            options=[],
            on_select=self._on_group_filter,
            height=48,
        )
        self._enabled_filter = ft.Dropdown(
            hint_text="全部状态",
            options=[
                ft.dropdown.Option("", "全部状态"),
                ft.dropdown.Option("enabled", "仅启用"),
                ft.dropdown.Option("disabled", "仅禁用"),
            ],
            on_select=self._on_enabled_filter,
            height=48,
        )

        # 预创建 Ref
        self._batch_count_ref = ft.Ref[ft.Text]()

        # ── 批量操作栏 ──
        self._batch_bar = ft.Row(
            visible=False,
            spacing=10,
            run_spacing=8,
            wrap=True,
            controls=[
                ft.Text("已选择 0 项", ref=self._batch_count_ref),
                ft.FilledTonalButton("启用", icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                                     on_click=self._batch_enable),
                ft.FilledTonalButton("禁用", icon=ft.Icons.CANCEL_OUTLINED,
                                     on_click=self._batch_disable),
                ft.FilledTonalButton("删除", icon=ft.Icons.DELETE_OUTLINE,
                                     on_click=self._batch_delete,
                                     color=ft.Colors.RED),
                ft.OutlinedButton("取消选择", on_click=self._clear_selection),
            ],
        )

        # ── 选择状态与移动端卡片列表 ──
        self._select_all_checkbox = ft.Checkbox(on_change=self._on_select_all)
        self._source_cards = ft.Column(spacing=6)

        # ── 状态栏 ──
        self._status_bar = ft.Container(
            padding=ft.Padding(16,8,16,8),
            content=ft.Text("加载中...", size=12, color=ft.Colors.GREY_600),
        )

        # ── 主布局 ──
        self.controls = [
            # 顶部控制区
            ft.Container(
                padding=ft.Padding(16,16,16,8),
                content=ft.Column([
                    ft.Row([
                        ft.Text("书源管理", size=28, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        ft.OutlinedButton(
                            "全选筛选结果",
                            icon=ft.Icons.SELECT_ALL,
                            tooltip="选中当前筛选结果中的全部书源",
                            on_click=self._select_all_sources,
                        ),
                        ft.IconButton(ft.Icons.REFRESH, tooltip="刷新",
                                      on_click=self._refresh),
                    ]),
                    ft.ResponsiveRow([
                        ft.Container(self._search_field, col={"xs": 12, "sm": 6}),
                        ft.Container(self._group_filter, col={"xs": 7, "sm": 4}),
                        ft.Container(self._enabled_filter, col={"xs": 5, "sm": 2}),
                    ], columns=12, spacing=8, run_spacing=8),
                ]),
            ),
            # 批量操作栏
            ft.Container(
                padding=ft.Padding(16,0,16,4),
                content=self._batch_bar,
            ),
            # 卡片列表更适合应用默认的手机宽度，避免横向滚动隐藏关键操作。
            ft.Container(
                expand=True,
                padding=ft.Padding(16,0,16,0),
                content=self._source_cards,
            ),
            # 状态栏
            self._status_bar,
        ]

        # 初始加载 - 在 did_mount 中完成
        self._page = 0

    def did_mount(self):
        """页面挂载后刷新"""
        self._refresh()

    def _refresh(self, e=None):
        """刷新数据"""
        self._filtered = self.service.sources.copy()
        self._visible_count = self.LOAD_BATCH_SIZE
        self._apply_filters(update_groups=True)
        self._render_page()

    # ── 过滤 ──

    def _apply_filters(self, update_groups=False):
        """应用当前过滤条件"""
        kw = self._search_field.value or ""
        group = self._group_filter.value or ""
        enabled_only = self._enabled_filter.value == "enabled"
        disabled_only = self._enabled_filter.value == "disabled"

        result = self.service.sources

        if kw:
            kw_lower = kw.lower()
            result = [s for s in result if kw_lower in s.search_text]

        if group:
            result = [s for s in result if s.bookSourceGroup == group]

        if enabled_only:
            result = [s for s in result if s.enabled]
        elif disabled_only:
            result = [s for s in result if not s.enabled]

        self._filtered = result

        # 更新分组过滤选项
        if update_groups:
            self._update_group_options()

    def _update_group_options(self):
        current_val = self._group_filter.value
        groups = self.service.get_groups()
        self._group_filter.options = [
            ft.dropdown.Option("", "全部分组"),
        ] + [ft.dropdown.Option(g, g) for g in groups]
        if current_val and current_val in groups:
            self._group_filter.value = current_val
        else:
            self._group_filter.value = ""
        self._group_filter.update()

    def _on_search(self, e):
        self._search_generation += 1
        generation = self._search_generation
        if self.page is not None:
            self.page.run_task(self._apply_search_after_delay, generation)
            return
        self._apply_search()

    async def _apply_search_after_delay(self, generation):
        """Collapse rapid keystrokes into one table rebuild."""
        await asyncio.sleep(self.SEARCH_DEBOUNCE_SECONDS)
        if generation == self._search_generation:
            self._apply_search()

    def _apply_search(self):
        self._page = 0
        self._visible_count = self.LOAD_BATCH_SIZE
        self._clear_selection(render=False)
        self._apply_filters()
        self._render_page()

    def _on_group_filter(self, e):
        self._page = 0
        self._visible_count = self.LOAD_BATCH_SIZE
        self._clear_selection(render=False)
        self._apply_filters()
        self._render_page()

    def _on_enabled_filter(self, e):
        self._page = 0
        self._visible_count = self.LOAD_BATCH_SIZE
        self._clear_selection(render=False)
        self._apply_filters()
        self._render_page()

    # ── 渲染表格 ──

    def _render_page(self):
        """只渲染当前已触发加载的数据。"""
        total = len(self._filtered)
        start = 0
        page_data = self._filtered[:self._visible_count]
        all_indices = set(range(total))
        self._select_all_checkbox.value = bool(all_indices) and all_indices.issubset(self._selected)

        self._source_cards.controls = [
            self._build_card(i, source) for i, source in enumerate(page_data)]

        self._update_status_bar()

        self.update()

    def _build_card(self, idx_in_filtered: int, source: BookSource):
        is_selected = idx_in_filtered in self._selected
        return ft.Card(content=ft.Container(
            padding=ft.Padding(10, 6, 10, 6),
            bgcolor=ft.Colors.BLUE_50 if is_selected else None,
            content=ft.Row([
                ft.Checkbox(
                    value=is_selected,
                    tooltip=f"选择 {source.display_name}",
                    on_change=lambda e, idx=idx_in_filtered:
                    self._on_row_check(idx, e)),
                ft.Column([
                    ft.Text(source.display_name, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            weight=ft.FontWeight.W_500,
                            color=(ft.Colors.GREY_400
                                   if not source.enabled else None)),
                    ft.Text(
                        " · ".join(value for value in
                                   [source.short_url, source.display_group]
                                   if value),
                        size=11, color=ft.Colors.GREY_600, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=2, expand=True),
                ft.Switch(
                    value=source.enabled,
                    tooltip="启用或停用书源",
                    on_change=lambda e, selected=source:
                    self._toggle_enabled(selected, e.control.value)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER)))

    def _on_scroll(self, e):
        """接近底部时追加下一批，不重建已经显示的行。"""
        pixels = getattr(e, "pixels", 0) or 0
        maximum = getattr(e, "max_scroll_extent", 0) or 0
        if (self._visible_count >= len(self._filtered) or not maximum or
                maximum - pixels >= 400):
            return
        old_count = self._visible_count
        self._visible_count = min(old_count + self.LOAD_BATCH_SIZE, len(self._filtered))
        self._source_cards.controls.extend(
            self._build_card(i, self._filtered[i])
            for i in range(old_count, self._visible_count)
        )
        self._update_status_bar()
        self.update()

    def _update_status_bar(self):
        stats = self.service.get_statistics()
        self._status_bar.content = ft.Text(
            f"共 {stats['total']} 条 | 启用 {stats['enabled']} 条"
            f" | 禁用 {stats['disabled']} 条 | {stats['groups']} 个分组"
            f" | 当前过滤: {len(self._filtered)} 条"
            f" | 已加载: {min(self._visible_count, len(self._filtered))} 条",
            size=12, color=ft.Colors.GREY_600,
        )

    def _toggle_enabled(self, source: BookSource, desired=None):
        """保存单个书源状态；失败时回滚，避免界面与文件不一致。"""
        previous = source.enabled
        source.enabled = (not previous if desired is None else bool(desired))
        try:
            self.service.save()
        except Exception as exc:
            source.enabled = previous
            try:
                page = self.page
            except RuntimeError:
                page = None
            if page:
                show_snack_bar(page, ft.SnackBar(
                    content=ft.Text(f"状态保存失败，已恢复原值：{exc}")))
        self._render_page()

    # ── 选择逻辑 ──

    def _select_all_sources(self, _=None):
        """通过顶部按钮选中完整筛选结果，不受懒加载显示数量限制。"""
        self._selected = set(range(len(self._filtered)))
        self._select_all_checkbox.value = bool(self._filtered)
        self._update_batch_bar(update=False)
        self._render_page()

    def _on_row_check(self, idx_in_filtered, e):
        """显式复选框选择，兼容不触发 DataRow 选择事件的 Flet 版本。"""
        if e.control.value:
            self._selected.add(idx_in_filtered)
        else:
            self._selected.discard(idx_in_filtered)
        self._update_batch_bar(update=False)
        self._render_page()

    def _on_select_all(self, e):
        """选择或取消选择当前筛选结果中的全部书源。"""
        start = 0
        end = len(self._filtered)
        if e.control.value:
            self._selected.update(range(start, end))
        else:
            self._selected.difference_update(range(start, end))
        self._update_batch_bar(update=False)
        self._render_page()

    def _clear_selection(self, e=None, render=True):
        self._selected.clear()
        self._update_batch_bar(update=not render)
        if render:
            self._render_page()

    def _update_batch_bar(self, update=True):
        count = len(self._selected)
        self._batch_bar.visible = count > 0
        self._batch_count_ref.current.value = f"已选择 {count} 项"
        if update:
            self.update()

    # ── 批处理操作 ──

    def _batch_enable(self, e):
        sources = [self._filtered[i] for i in self._selected if i < len(self._filtered)]
        previous = {id(source): source.enabled for source in sources}
        self.service.batch_enable(sources)
        self._finish_batch_action(previous_enabled=previous)

    def _batch_disable(self, e):
        sources = [self._filtered[i] for i in self._selected if i < len(self._filtered)]
        previous = {id(source): source.enabled for source in sources}
        self.service.batch_disable(sources)
        self._finish_batch_action(previous_enabled=previous)

    def _batch_delete(self, e):
        if not self._selected:
            return
        sources = [self._filtered[i] for i in self._selected if i < len(self._filtered)]
        names = "、".join(source.display_name for source in sources[:3])
        if len(sources) > 3:
            names += f" 等 {len(sources)} 条"
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"删除 {len(sources)} 条书源？"),
            content=ft.Text(f"将删除：{names}\n此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda event: self.page.pop_dialog()),
                ft.Button("确认删除", color=ft.Colors.RED,
                          on_click=lambda event, selected=sources:
                          self._confirm_batch_delete(selected)),
            ])
        self.page.show_dialog(dialog)

    def _confirm_batch_delete(self, sources):
        self.page.pop_dialog()
        previous_sources = list(self.service.sources)
        self.service.batch_delete(sources)
        self._finish_batch_action(previous_sources=previous_sources)

    def _finish_batch_action(self, previous_sources=None, previous_enabled=None):
        """批量修改后重建过滤列表、分页并持久化。"""
        self._selected.clear()
        self._batch_bar.visible = False
        self._apply_filters(update_groups=True)
        self._page = 0
        self._visible_count = self.LOAD_BATCH_SIZE
        try:
            self.service.save()
        except Exception as exc:
            if previous_sources is not None:
                self.service.sources = previous_sources
            if previous_enabled is not None:
                for source in self.service.sources:
                    if id(source) in previous_enabled:
                        source.enabled = previous_enabled[id(source)]
            self._apply_filters(update_groups=True)
            if self.page:
                show_snack_bar(self.page, ft.SnackBar(content=ft.Text(
                    f"操作未保存，已恢复原状态：{exc}")))
        self._render_page()
