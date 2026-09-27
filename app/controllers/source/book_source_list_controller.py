"""BookSourceListController：书源列表视图的事件订阅与服务编排（§7.4）。

订阅 :class:`SourceChangedEvent` 自动刷新列表；启用停用/删除/导入导出全部
落到 SourceService 真实方法（导出写 ``<data>/exports/书源-<时间戳>.json``），
分组/状态过滤在控制器内基于最近一次全量快照完成（不反复打服务）。
编辑跳编辑器经 NavigationHost 的载荷注入通道（``load_source(record)``）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import flet as ft

from app.core.events import SourceChangedEvent
from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:  # 并行线未交付时退化为占位类型，测试用替身覆盖
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover - 仅在兄弟服务未落盘时触发
    class SourceService:  # type: ignore[no-redef]
        pass


class BookSourceListController(BaseController):
    """书源列表 / 分组 / 启用停用 / 导入导出 / 检测入口。"""

    destination = "sources.list"

    #: 跨视图导航回调声明（动作名 → 目的地）：NavigationHost.show 装配时把
    #: 对应回调槽绑定为导航切换（视图间零互引，§7.2）。
    nav_actions: dict[str, str] = {
        "on_open_import": "sources.import",
        "on_open_editor": "sources.editor",
        "on_open_check": "sources.check",
        "on_open_repair": "sources.repair",
    }

    def on_attach(self) -> None:
        self._source = self._safe_service(SourceService)
        # 装配处可注入跨视图导航回调（不互引视图类）
        self.on_open_editor = None     # (record: dict) -> None
        self.on_open_import = None    # () -> None
        self.on_open_check = None      # () -> None
        self.on_open_repair = None     # () -> None

        self._records: list = []       # 最近一次全量快照（模型对象）
        self._query = ""
        self._group = ""
        self._state = ""
        self._selected_urls: set[str] = set()  # 批量选择（按 bookSourceUrl）

        self._view.on_refresh = self.refresh
        self._view.on_search = self._on_search
        self._view.on_group_changed = self._on_group_changed
        self._view.on_state_filter = self._on_state_filter
        self._view.on_toggle_enabled = self.toggle_enabled
        self._view.on_edit = self.edit_source
        self._view.on_delete = self.request_delete
        self._view.on_import_json = lambda e: self._fire(self.on_open_import)
        self._view.on_export_json = self.export_json
        self._view.on_start_check = lambda e: self._fire(self.on_open_check)
        self._view.on_start_repair = lambda e: self._fire(self.on_open_repair)
        # -- 批量选择（对齐旧 manage_view） --
        self._view.on_selection_changed = self._on_selection_changed
        self._view.on_select_all = self.select_all
        self._view.on_clear_selection = self.clear_selection
        self._view.on_batch_enable = lambda e: self._batch_set_enabled(True)
        self._view.on_batch_disable = lambda e: self._batch_set_enabled(False)
        self._view.on_batch_delete = self.request_batch_delete

        self.subscribe(SourceChangedEvent, self._on_source_changed)
        self.refresh()

    # ------------------------------------------------------------------ #
    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 数据加载 / 渲染
    # ------------------------------------------------------------------ #
    def refresh(self, _=None) -> None:
        if self._source is None:
            self._view.set_status("SourceService 未就绪（并行集成中）")
            return
        future = self.ui_call(self._source.list_sources())
        future.add_done_callback(self.on_done(self._render_sources))

    def _render_sources(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            records = future.result() or []
        except Exception as exc:
            self._view.set_status(f"加载书源失败：{exc}")
            return
        self._records = list(records)
        groups = sorted({g.strip() for s in self._records
                         for g in str(getattr(s, "bookSourceGroup", "") or "")
                         .split(",") if g.strip()})
        self._view.set_groups(groups)
        self._apply_filters()
        enabled = sum(1 for s in self._records
                      if getattr(s, "enabled", False))
        self._view.set_stats({
            "total": len(self._records), "enabled": enabled,
            "disabled": len(self._records) - enabled, "groups": len(groups)})

    def _apply_filters(self) -> None:
        view = self._view
        if view is None:
            return
        query = self._query.lower()
        result = []
        for source in self._records:
            if self._state == "enabled" and not getattr(source, "enabled", False):
                continue
            if self._state == "disabled" and getattr(source, "enabled", False):
                continue
            group = str(getattr(source, "bookSourceGroup", "") or "")
            if self._group and self._group not in [
                    g.strip() for g in group.split(",")]:
                continue
            if query:
                haystack = " ".join((
                    getattr(source, "bookSourceName", "") or "",
                    getattr(source, "bookSourceUrl", "") or "",
                    group,
                    getattr(source, "bookSourceComment", "") or "")).lower()
                if query not in haystack:
                    continue
            result.append(source)
        view.set_selected(self._selected_urls)
        view.render_sources(result)
        view.update_batch_bar(len(self._selected_urls))

    def _on_search(self, query: str) -> None:
        self._query = str(query or "").strip()
        self._apply_filters()

    def _on_group_changed(self, group: str) -> None:
        self._group = str(group or "").strip()
        self._apply_filters()

    def _on_state_filter(self, mode: str) -> None:
        self._state = str(mode or "").strip()
        self._apply_filters()

    async def _on_source_changed(self, event: SourceChangedEvent) -> None:
        self.refresh()

    # ------------------------------------------------------------------ #
    # 行动作
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_dict(record) -> dict:
        """记录归一：dict 原样；BookSource 模型转 to_dict()。"""
        if isinstance(record, dict):
            return record
        to_dict = getattr(record, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return dict(record)

    def toggle_enabled(self, record, enabled: bool) -> None:
        record = self._as_dict(record)
        url = record.get("bookSourceUrl")
        if not url or self._source is None:
            return
        future = self.ui_call(self._source.set_enabled(url, bool(enabled)))
        future.add_done_callback(self.on_done(
            lambda f: self._after_toggle(url, enabled, f)))

    def _after_toggle(self, url: str, enabled: bool, future) -> None:
        try:
            ok = bool(future.result())
        except Exception as exc:
            self._view.set_status(f"切换启用状态失败：{exc}")
            return
        if not ok:
            self._view.set_status("书源已不存在，切换未生效")
        self.refresh()

    def request_delete(self, record) -> None:
        """删除确认对话框：删除是持久化动作，必须二次确认。"""
        record = self._as_dict(record)
        page = self._page()
        url = record.get("bookSourceUrl", "")
        name = record.get("bookSourceName", "") or url
        if not url or page is None or self._source is None:
            return
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text(f"删除书源「{name}」？"),
            content=ft.Text("删除后该书源的所有规则将被移除，此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledTonalButton(
                    "删除", icon=ft.Icons.DELETE_FOREVER,
                    on_click=lambda e: self._do_delete(url, name, page)),
            ])
        page.show_dialog(dialog)

    def _do_delete(self, url: str, name: str, page) -> None:
        try:
            page.pop_dialog()
        except Exception:
            pass
        future = self.ui_call(self._source.delete_source(url))
        future.add_done_callback(self.on_done(
            lambda f: self._after_delete(name, f)))

    def _after_delete(self, name: str, future) -> None:
        try:
            ok = bool(future.result())
        except Exception as exc:
            self._view.set_status(f"删除失败：{exc}")
            return
        self._view.set_status(f"已删除：{name}" if ok else f"未找到书源：{name}")
        self.refresh()

    def export_json(self, _=None) -> None:
        """导出全部书源 JSON 到 ``<data>/exports/``（无模态保存框，路径回显）。"""
        if self._source is None:
            self._view.set_status("SourceService 未就绪，未导出")
            return
        self._view.set_status("正在导出书源…")
        future = self.ui_call(self._source.export_sources_json())
        future.add_done_callback(self.on_done(self._after_export))

    def _after_export(self, future) -> None:
        try:
            text = future.result()
        except Exception as exc:
            self._view.set_status(f"导出失败：{exc}")
            return
        try:
            from app.core.storage import StorageService
            data_dir = self._hub.get(StorageService).paths.data
        except Exception:
            data_dir = Path(".")
        out_dir = Path(str(data_dir)) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"书源-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            path.write_text(json.dumps(
                json.loads(text), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:
            self._view.set_status(f"导出写盘失败：{exc}")
            return
        self._view.set_status(f"已导出 {path}")

    def edit_source(self, record) -> None:
        record = self._as_dict(record)
        if self.on_open_editor is not None:
            self.on_open_editor(record)
        else:
            self._view.set_status(f"编辑《{record.get('bookSourceName', '')}》（编辑器路由由装配处绑定）")

    # ------------------------------------------------------------------ #
    # 批量选择 / 批量操作（对齐旧 manage_view：启用/禁用/删除/取消选择）
    # ------------------------------------------------------------------ #
    def _filtered_urls(self) -> list[str]:
        """当前筛选结果的全部 bookSourceUrl（与视图渲染一致）。"""
        return [str(getattr(s, "bookSourceUrl", "") or "") for s in self._records
                if self._match_filters(s)]

    def _match_filters(self, source) -> bool:
        if self._state == "enabled" and not getattr(source, "enabled", False):
            return False
        if self._state == "disabled" and getattr(source, "enabled", False):
            return False
        group = str(getattr(source, "bookSourceGroup", "") or "")
        if self._group and self._group not in [g.strip() for g in group.split(",")]:
            return False
        if self._query:
            haystack = " ".join((
                getattr(source, "bookSourceName", "") or "",
                getattr(source, "bookSourceUrl", "") or "",
                group,
                getattr(source, "bookSourceComment", "") or "")).lower()
            if self._query.lower() not in haystack:
                return False
        return True

    def _on_selection_changed(self, url: str, checked: bool) -> None:
        url = str(url or "")
        if not url:
            return
        if checked:
            self._selected_urls.add(url)
        else:
            self._selected_urls.discard(url)
        self._view.set_selected(self._selected_urls)
        self._view.update_batch_bar(len(self._selected_urls))

    def select_all(self, _=None) -> None:
        """全选当前筛选结果（旧实现"全选筛选结果"）。"""
        urls = [u for u in self._filtered_urls() if u]
        self._selected_urls = set(urls)
        self._sync_selection_ui()

    def clear_selection(self, _=None) -> None:
        self._selected_urls.clear()
        self._sync_selection_ui()

    def _sync_selection_ui(self) -> None:
        view = self._view
        if view is None:
            return
        view.set_selected(self._selected_urls)
        view.render_sources(self._current_filtered())
        view.update_batch_bar(len(self._selected_urls))

    def _current_filtered(self) -> list:
        return [s for s in self._records if self._match_filters(s)]

    def _batch_set_enabled(self, enabled: bool) -> None:
        if self._source is None or not self._selected_urls:
            return
        urls = sorted(self._selected_urls)
        future = self.ui_call(self._source.batch_set_enabled(urls, enabled))
        future.add_done_callback(self.on_done(
            lambda f: self._after_batch_toggle(enabled, f)))

    def _after_batch_toggle(self, enabled: bool, future) -> None:
        try:
            changed = int(future.result() or 0)
        except Exception as exc:
            self._view.set_status(f"批量{'启用' if enabled else '停用'}失败：{exc}")
            return
        self._view.set_status(f"已{'启用' if enabled else '停用'} {changed} 条")
        self._selected_urls.clear()
        self.refresh()

    def request_batch_delete(self, _=None) -> None:
        """批量删除确认（删除不可撤销，必须二次确认）。"""
        page = self._page()
        if page is None or self._source is None or not self._selected_urls:
            return
        urls = sorted(self._selected_urls)
        names = self._names_of(urls)
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text(f"删除 {len(urls)} 条书源？"),
            content=ft.Text(f"将删除：{names}\n此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.FilledTonalButton(
                    "确认删除", color=ft.Colors.RED, icon=ft.Icons.DELETE_FOREVER,
                    on_click=lambda e: self._do_batch_delete(urls, page)),
            ])
        page.show_dialog(dialog)

    def _names_of(self, urls: list[str]) -> str:
        names = []
        for url in urls[:3]:
            for s in self._records:
                if str(getattr(s, "bookSourceUrl", "") or "") == url:
                    names.append(getattr(s, "bookSourceName", "") or url)
                    break
            else:
                names.append(url)
        text = "、".join(names)
        if len(urls) > 3:
            text += f" 等 {len(urls)} 条"
        return text

    def _do_batch_delete(self, urls: list[str], page) -> None:
        try:
            page.pop_dialog()
        except Exception:
            pass
        future = self.ui_call(self._source.batch_delete(urls))
        future.add_done_callback(self.on_done(self._after_batch_delete))

    def _after_batch_delete(self, future) -> None:
        try:
            removed = int(future.result() or 0)
        except Exception as exc:
            self._view.set_status(f"批量删除失败：{exc}")
            return
        self._view.set_status(f"已删除 {removed} 条书源")
        self._selected_urls.clear()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _page(self):
        try:
            return self._view.page
        except Exception:
            return None
