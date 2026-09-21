"""书源连通性、搜索能力和分类标签检测页面。"""
from __future__ import annotations

import csv
import io
import time

import flet as ft

from app.services import check_service
from app.services.check_service import (CHECK_CONCURRENCY, CHECK_MAX_CONCURRENCY,
                                        CHECK_TAG_CATEGORIES,
                                        READING_CHAIN_CANDIDATES,
                                        TAG_SOURCE_CONCURRENCY, check_sources)
from app.ui_feedback import show_snack_bar


CHECK_STATE_LABELS = {
    "fully_usable": "完整可用",
    "list_usable": "列表可用",
    "search_only": "仅搜索可用",
    "explore_only": "仅发现可用",
    "content_unavailable": "阅读链路失败",
    "unknown": "待确认",
    "blocked": "访问被拦截",
    "unusable": "不可用",
}
# 被本机杀软或站点 WAF 拦截的主机不代表书源失效，重测只会再次触发拦截。
FAILED_CHECK_STATES = {"content_unavailable", "unknown", "unusable"}
CHECK_SAMPLE_SIZE = 50

READING_STAGE_LABELS = {
    "detail": "详情",
    "catalog": "目录",
    "content": "正文",
    "complete": "正文",
}
# 其余阅读状态沿用英文枚举，只有需要解释的一项换成中文。
READING_STATUS_LABELS = {"needs_browser": "需浏览器未抽检"}


class CheckView(ft.Column):
    RESULT_UPDATE_BATCH_SIZE = 10
    # The bar must keep moving while fewer than a batch of rows is
    # pending (or while a state filter hides every result).
    PROGRESS_REFRESH_SECONDS = 0.3

    def _safe_update(self):
        """Keep background detection alive while this view is unmounted."""
        try:
            self.update()
        except RuntimeError:
            pass

    def __init__(self, service, file_picker=None):
        super().__init__(expand=True, spacing=12)
        self.service = service
        self.file_picker = file_picker
        self._running = False
        self._results = {}
        self._retry_targets = None
        self._summary = ft.Text("尚未检测", color=ft.Colors.GREY_600)
        self._progress = ft.ProgressBar(value=0)
        self._rows = ft.ListView(expand=True, spacing=4)
        self._enabled_only = ft.Checkbox(label="仅检测已启用书源", value=True)
        self._scope = ft.Dropdown(
            label="检测范围", width=190, value="sample",
            options=[
                ft.dropdown.Option("sample", f"抽样前 {CHECK_SAMPLE_SIZE} 个"),
                ft.dropdown.Option("all", "全部书源"),
            ])
        self._auto_disable = ft.Checkbox(
            label="检测后自动停用确认不可用的书源", value=False,
            on_change=self._on_auto_disable_change)
        self._concurrency = ft.Dropdown(
            label="并发书源", width=130, value=str(CHECK_CONCURRENCY),
            options=[ft.dropdown.Option(str(value), str(value))
                     for value in (4, CHECK_CONCURRENCY, 12, CHECK_MAX_CONCURRENCY)])
        self._start = ft.Button("开始检测", icon=ft.Icons.PLAY_ARROW,
                                on_click=self._start_check)
        self._state_filter = ft.Dropdown(
            label="状态筛选", width=170, value="all",
            options=[ft.dropdown.Option("all", "全部状态")] + [
                ft.dropdown.Option(key, label)
                for key, label in CHECK_STATE_LABELS.items()],
            on_select=self._render_rows)
        self._sort_select = ft.Dropdown(
            label="排序", width=160, value="order",
            options=[
                ft.dropdown.Option("order", "检测顺序"),
                ft.dropdown.Option("respond", "响应时间↑"),
                ft.dropdown.Option("state", "按可用性"),
            ],
            on_select=self._render_rows)
        self._retry_button = ft.FilledTonalButton(
            "重测失败项", icon=ft.Icons.REPLAY,
            on_click=self._retry_failed)
        self._export_button = ft.FilledTonalButton(
            "导出结果", icon=ft.Icons.DOWNLOAD,
            on_click=self._export_results)
        self.controls = [
            ft.Container(padding=16, content=ft.Column([
                ft.Text("书源检测", size=28, weight=ft.FontWeight.BOLD),
                ft.Text(
                    f"检测服务器、搜索、分类，并抽检详情、目录和正文"
                    f"（每源最多试 {READING_CHAIN_CANDIDATES} 本，单本异常不判整源死刑）；"
                    f"分类每源最多抽检前 {CHECK_TAG_CATEGORIES} 个、命中一个可用分类即止，"
                    f"最多 {TAG_SOURCE_CONCURRENCY} 个书源同时抽检分类，"
                    "其余阶段按并发书源数并行；单个书源内部的规则请求是串行的，"
                    "同一时刻的真实请求数不超过并发书源数的两倍（含连通性探测）。"
                    "搜索已证明主机不可达时，同主机的分类与正文抽检会直接跳过，"
                    "不重复等待超时。"
                    f"默认按列表顺序抽样前 {CHECK_SAMPLE_SIZE} 个书源；"
                    "检测会向每个书源主机发真实请求，部分站点已被安全软件标记，"
                    "全量检测前请先确认。默认只生成结果，不修改书源状态。"
                    "被本机安全软件或站点 WAF 拦截的主机会很快记为“访问被拦截”，"
                    "不再等待各阶段超时。",
                    color=ft.Colors.GREY_600,
                ),
                self._enabled_only,
                self._auto_disable,
                # A wrapped Row lays children out with unbounded main-axis
                # width, so an expanded spacer here breaks the whole header
                # column (including the progress bar below it).
                ft.Row([self._start, self._scope, self._concurrency,
                        self._retry_button, self._export_button,
                        self._state_filter, self._sort_select],
                       wrap=True, run_spacing=8, spacing=8),
                self._progress,
                self._summary,
            ])),
            ft.Divider(height=1),
            ft.Container(expand=True, padding=ft.Padding(16, 0, 16, 12),
                         content=self._rows),
        ]

    def _start_check(self, _):
        if self._running:
            return
        candidates = ([source for source in self.service.sources if source.enabled]
                      if self._enabled_only.value else list(self.service.sources))
        notices = []
        if self._scope.value == "all" and len(candidates) > CHECK_SAMPLE_SIZE:
            notices.append(
                f"将向全部 {len(candidates)} 个书源主机发起真实请求。部分站点已被安全软件标记为风险域名，"
                "本机可能弹出拦截告警；被拦截的书源会单独记为“访问被拦截”，不判为书源不可用。")
        if self._auto_disable.value:
            notices.append("检测过程中，明确判定为不可用的书源会被停用。限流、超时和需要 JS 验证的书源不会被停用。")
        if not notices:
            self.page.run_task(self._run_check)
            return
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("全量检测会连接所有书源主机" if len(notices) == 1
                          and not self._auto_disable.value else "开始检测前请确认"),
            content=ft.Column([ft.Text(text) for text in notices],
                              spacing=8, width=420),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.Button("开始检测", color=ft.Colors.RED if self._auto_disable.value else None,
                          on_click=self._confirm_start_check),
            ])
        self.page.show_dialog(dialog)

    def _on_auto_disable_change(self, _):
        self._start.content = ("检测并自动停用" if self._auto_disable.value
                               else "开始检测")
        self._safe_update()

    def _concurrency_value(self):
        try:
            return int(self._concurrency.value or CHECK_CONCURRENCY)
        except (TypeError, ValueError):
            return CHECK_CONCURRENCY

    def _confirm_start_check(self, _):
        self.page.pop_dialog()
        self.page.run_task(self._run_check)

    def _retry_failed(self, _):
        if self._running:
            return
        failed_urls = {result.get("raw_url")
                       for result in self._results.values()
                       if result.get("check_state") in FAILED_CHECK_STATES}
        targets = [source for source in self.service.sources
                   if source.bookSourceUrl in failed_urls]
        if not targets:
            self._show_notice("没有需要重测的失败/待确认书源")
            return
        self._retry_targets = targets
        self.page.run_task(self._run_check)

    def _show_notice(self, message):
        try:
            page = self.page
        except RuntimeError:
            return
        show_snack_bar(page, ft.SnackBar(content=ft.Text(message)))

    async def _run_check(self):
        self._running = True
        self._start.disabled = True
        retry_targets = self._retry_targets
        self._retry_targets = None
        if not retry_targets:
            self._rows.controls.clear()
            self._results = {}
        self._progress.value = 0
        sources = (retry_targets if retry_targets else
                   ([source for source in self.service.sources if source.enabled]
                    if self._enabled_only.value else list(self.service.sources)))
        if not retry_targets and self._scope.value == "sample":
            sources = sources[:CHECK_SAMPLE_SIZE]
        self._summary.value = (f"重测 {len(sources)} 个失败书源…" if retry_targets
                               else f"准备检测 {len(sources)} 个书源…"
                               + ("" if retry_targets or self._scope.value == "all"
                                  else "（抽样，可在“检测范围”改为全部）"))
        self._safe_update()
        pending_rows = []
        latest_progress = [0, len(sources)]
        last_render = [0.0]

        def push_updates(force=False):
            """Refresh the bar even when result rows are still batched up."""
            now = time.monotonic()
            if (not force and len(pending_rows) < self.RESULT_UPDATE_BATCH_SIZE and
                    now - last_render[0] < self.PROGRESS_REFRESH_SECONDS):
                return
            last_render[0] = now
            if pending_rows:
                self._rows.controls.extend(pending_rows)
                pending_rows.clear()
            done, total = latest_progress
            self._progress.value = done / max(total, 1)
            self._summary.value = f"检测进度：{done}/{total}"
            self._safe_update()

        def receive(result, done, total):
            latest_progress[:] = [done, total]
            self._store_result(result)
            if self._matches_filter(result):
                pending_rows.append(self._build_row(result))
            push_updates()

        try:
            results = await check_sources(
                sources, concurrency=self._concurrency_value(),
                on_result=receive,
                auto_disable=bool(self._auto_disable.value))
            for result in results:
                self._store_result(result)
            push_updates(force=True)
            by_url = {source.bookSourceUrl: source for source in sources}
            all_results = sorted(self._results.values(),
                                 key=lambda result: result.get("_order", 0))
            for result in all_results:
                source = by_url.get(result.get("raw_url"))
                if source:
                    self.service.category_checks[source.bookSourceUrl] = result.get(
                        "category_results", [])
            disabled_count = sum(1 for result in all_results
                                 if result.get("auto_disabled"))
            if disabled_count:
                self.service.save()
            available = sum(1 for result in all_results
                            if result.get("search_status") == "ok")
            readable = sum(1 for result in all_results
                           if result.get("check_state") == "fully_usable")
            reachable = sum(1 for result in all_results if result["status"] == "ok")
            tag_total = sum(result.get("tag_total", 0) for result in all_results)
            tag_valid = sum(result.get("tag_valid", 0) for result in all_results)
            uncertain = sum(1 for result in all_results
                            if result.get("check_state") == "unknown")
            blocked = sum(1 for result in all_results
                          if result.get("check_state") == "blocked")
            self._summary.value = (
                f"检测完成：服务器可达 {reachable}/{len(sources)}，"
                f"完整可读 {readable}/{len(all_results)}，"
                f"搜索可用 {available}/{len(all_results)}，"
                f"标签可用 {tag_valid}/{tag_total}，"
                f"{'自动停用 ' + str(disabled_count) + ' 个，' if self._auto_disable.value else ''}"
                f"待确认 {uncertain} 个"
                + (f"，访问被拦截 {blocked} 个（本机杀软或站点 WAF 阻断，"
                   "未判为书源失效）" if blocked else ""))
        except Exception as exc:
            push_updates(force=True)
            self._show_check_error(exc)
        finally:
            self._running = False
            self._start.disabled = False
            self._render_rows()

    def _show_check_error(self, exc):
        """Grey summary line stays, but the failure gets a retryable SnackBar."""
        message = f"检测失败：{exc}"
        self._summary.value = message
        try:
            page = self.page
        except RuntimeError:
            return

        def _retry(_=None):
            try:
                page.pop_dialog()
            except RuntimeError:
                pass
            if not self._running:
                self._retry_targets = None
                page.run_task(self._run_check)

        show_snack_bar(page, ft.SnackBar(
            content=ft.Text(message, max_lines=3),
            action="重试", on_action=_retry))
        self._safe_update()

    # ── 结果存储、过滤与排序 ──

    def _store_result(self, result):
        key = result.get("raw_url") or result.get("url") or result.get("name")
        existing = self._results.get(key)
        if existing is not None:
            result.setdefault("_order", existing.get("_order", 0))
        else:
            result["_order"] = len(self._results)
        self._results[key] = result

    def _result_for(self, source_url):
        return self._results.get(source_url)

    def _visible_results(self):
        rows = sorted(self._results.values(),
                      key=lambda row: row.get("_order", 0))
        state = self._state_filter.value or "all"
        if state != "all":
            rows = [row for row in rows if row.get("check_state", "unknown") == state]
        sort = self._sort_select.value or "order"
        if sort == "respond":
            rows.sort(key=lambda row: row.get("response_time_ms")
                      if row.get("response_time_ms") is not None else float("inf"))
        elif sort == "state":
            order = ["fully_usable", "list_usable", "search_only", "explore_only",
                     "content_unavailable", "unknown", "blocked", "unusable"]
            rows.sort(key=lambda row: (
                order.index(row.get("check_state", "unknown"))
                if row.get("check_state", "unknown") in order else len(order),
                row.get("_order", 0)))
        return rows

    def _matches_filter(self, result):
        state_filter = self._state_filter.value or "all"
        return state_filter == "all" or result.get("check_state", "unknown") == state_filter

    def _render_rows(self, _=None):
        self._rows.controls = [self._build_row(result)
                               for result in self._visible_results()]
        self._safe_update()

    @staticmethod
    def detail_text(result):
        server = result["status"].upper()
        search = (result.get("search_status") or "-").upper()
        detail = f"连通 {server} · 搜索 {search}"
        state = result.get("check_state", "unknown")
        detail += f" · {CHECK_STATE_LABELS.get(state, state)}"
        count = result.get("book_count")
        if count is not None and count >= 0:
            detail += f" · 搜索结果 {count} 本"
        if result.get("response_time_ms") is not None:
            detail += f" · {result['response_time_ms']:.0f} ms"
        if result.get("tag_total"):
            detail += f" · 标签 {result['tag_valid']}/{result['tag_total']}"
            broken_pages = sum(
                1 for item in result.get("category_results", [])
                if item.get("pagination_valid") is False
            )
            if broken_pages:
                detail += f" · 翻页失败 {broken_pages}"
        if result.get("tag_check_timeout"):
            detail += (f" · 标签检测超过 {check_service.SOURCE_TAG_TIMEOUT} 秒"
                       "（保留启用）")
        elif result.get("tag_check_skipped"):
            reason = ("主机被拦截" if result.get("status") == "blocked"
                      else "搜索已证明主机不可达")
            detail += f" · 分类未抽检（{reason}）"
        reading_stage = result.get("reading_stage")
        tried = result.get("reading_tried") or 0
        sample = result.get("sample_book")
        sample_note = f"《{sample}》" if sample else ""
        if result.get("reading_status") == "ok":
            detail += (f" · 阅读抽检通过{sample_note}"
                       f"（{result.get('chapter_count', 0)} 章"
                       f"{'，试 ' + str(tried) + ' 本' if tried > 1 else ''}）")
        elif result.get("reading_status") == "needs_browser":
            stage_label = READING_STAGE_LABELS.get(reading_stage, reading_stage)
            detail += (f" · {stage_label}需内置浏览器渲染，未抽检{sample_note}")
        elif reading_stage not in {None, "none"}:
            stage_label = READING_STAGE_LABELS.get(reading_stage, reading_stage)
            detail += (f" · {stage_label}失败{sample_note}："
                       f"{result.get('reading_error') or '未知错误'}")
        if result.get("auto_disabled"):
            detail += f" · 已自动停用（{result.get('disable_reason', '不可用')}）"
        elif result.get("check_state") == "unknown":
            detail += " · 待确认（限流、超时或 JS 规则，不停用）"
        elif state == "blocked":
            detail += f" · {result.get('error') or '访问被拦截'}"
        return detail

    def _build_row(self, result):
        state = result.get("check_state")
        healthy = state == "fully_usable"
        color = (ft.Colors.GREEN if healthy else
                 ft.Colors.GREY if state == "blocked" else ft.Colors.ORANGE)
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.CHECK_CIRCLE if healthy else
                            ft.Icons.BLOCK if state == "blocked"
                            else ft.Icons.INFO_OUTLINE,
                            color=color),
            title=ft.Text(result["name"]),
            subtitle=ft.Text(self.detail_text(result)),
        )

    # ── 导出 ──

    async def _export_results(self, _=None):
        if not self._results:
            self._show_notice("还没有检测结果可导出")
            return
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["书源名称", "书源URL", "状态", "服务器", "搜索",
                         "搜索结果数", "响应时间(ms)", "标签可用",
                         "阅读抽检", "详情", "自动停用"])
        for result in self._visible_results():
            state = result.get("check_state", "unknown")
            tag_total = result.get("tag_total", 0)
            writer.writerow([
                result.get("name", ""),
                result.get("raw_url", ""),
                CHECK_STATE_LABELS.get(state, state),
                result.get("status", ""),
                result.get("search_status", ""),
                result.get("book_count", ""),
                "" if result.get("response_time_ms") is None
                else round(result["response_time_ms"]),
                "" if not tag_total else
                f"{result.get('tag_valid', 0)}/{tag_total}",
                READING_STATUS_LABELS.get(
                    result.get("reading_status", ""),
                    result.get("reading_status", "")),
                self.detail_text(result),
                "是" if result.get("auto_disabled") else "",
            ])
        payload = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        name = time.strftime("书源检测结果_%Y%m%d_%H%M%S.csv")
        target = None
        if self.file_picker is not None:
            try:
                target = await self.file_picker.save_file(
                    dialog_title="导出检测结果", file_name=name,
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["csv"])
            except Exception:
                target = None
            if target is None:
                return  # user cancelled
        else:
            directory = getattr(self.service, "data_dir", None)
            from pathlib import Path
            target = str(Path(directory or ".") / name)
        try:
            with open(target, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            self._show_notice(f"导出失败：{exc}")
            return
        self._show_notice(f"已导出 {len(self._results)} 条结果：{target}")
