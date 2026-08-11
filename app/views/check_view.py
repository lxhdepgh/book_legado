"""书源连通性、搜索能力和分类标签检测页面。"""
from __future__ import annotations

import flet as ft

from app.services.check_service import CHECK_CONCURRENCY, TAG_PAGE_CONCURRENCY, check_sources


CHECK_STATE_LABELS = {
    "fully_usable": "完整可用",
    "list_usable": "列表可用",
    "search_only": "仅搜索可用",
    "explore_only": "仅发现可用",
    "content_unavailable": "阅读链路失败",
    "unknown": "待确认",
    "unusable": "不可用",
}

READING_STAGE_LABELS = {
    "detail": "详情",
    "catalog": "目录",
    "content": "正文",
    "complete": "正文",
}


class CheckView(ft.Column):
    RESULT_UPDATE_BATCH_SIZE = 10

    def __init__(self, service):
        super().__init__(expand=True, spacing=12)
        self.service = service
        self._running = False
        self._results = []
        self._summary = ft.Text("尚未检测", color=ft.Colors.GREY_600)
        self._progress = ft.ProgressBar(value=0)
        self._rows = ft.ListView(expand=True, spacing=4)
        self._enabled_only = ft.Checkbox(label="仅检测已启用书源", value=True)
        self._auto_disable = ft.Checkbox(
            label="检测后自动停用确认不可用的书源", value=False,
            on_change=self._on_auto_disable_change)
        self._start = ft.Button("开始检测", icon=ft.Icons.PLAY_ARROW,
                                on_click=self._start_check)
        self.controls = [
            ft.Container(padding=16, content=ft.Column([
                ft.Text("书源检测", size=28, weight=ft.FontWeight.BOLD),
                ft.Text(
                    f"使用 {CHECK_CONCURRENCY} 个书源协程、最多 {TAG_PAGE_CONCURRENCY} 个标签页协程，"
                    "使用当前规则引擎检测服务器、搜索、分类，并抽检详情、目录和正文。默认只生成结果，不修改书源状态。",
                    color=ft.Colors.GREY_600,
                ),
                self._enabled_only,
                self._auto_disable,
                ft.Row([self._start], wrap=True, run_spacing=8),
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
        if self._auto_disable.value:
            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("检测并自动停用书源？"),
                content=ft.Text(
                    "检测过程中，明确判定为不可用的书源会被停用。限流、超时和需要 JS 验证的书源不会被停用。"),
                actions=[
                    ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                    ft.Button("开始检测并自动停用", color=ft.Colors.RED,
                              on_click=self._confirm_start_check),
                ])
            self.page.show_dialog(dialog)
            return
        self.page.run_task(self._run_check)

    def _on_auto_disable_change(self, _):
        self._start.text = ("检测并自动停用" if self._auto_disable.value
                            else "开始检测")
        self.update()

    def _confirm_start_check(self, _):
        self.page.pop_dialog()
        self.page.run_task(self._run_check)

    async def _run_check(self):
        self._running = True
        self._start.disabled = True
        self._rows.controls.clear()
        self._progress.value = 0
        sources = ([source for source in self.service.sources if source.enabled]
                   if self._enabled_only.value else list(self.service.sources))
        self._summary.value = f"准备检测 {len(sources)} 个书源…"
        self.update()
        pending_rows = []
        latest_progress = [0, len(sources)]

        def flush_results(force=False):
            if not pending_rows or (not force and
                                    len(pending_rows) < self.RESULT_UPDATE_BATCH_SIZE):
                return
            self._rows.controls.extend(pending_rows)
            pending_rows.clear()
            done, total = latest_progress
            self._progress.value = done / max(total, 1)
            self._summary.value = f"检测进度：{done}/{total}"
            self.update()

        def receive(result, done, total):
            latest_progress[:] = [done, total]
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
                detail += " · 标签检测超过 60 秒（保留启用）"
            reading_stage = result.get("reading_stage")
            if result.get("reading_status") == "ok":
                detail += f" · 阅读抽检通过（{result.get('chapter_count', 0)} 章）"
            elif reading_stage not in {None, "none"}:
                stage_label = READING_STAGE_LABELS.get(reading_stage, reading_stage)
                detail += f" · {stage_label}失败：{result.get('reading_error') or '未知错误'}"
            if result.get("auto_disabled"):
                detail += f" · 已自动停用（{result.get('disable_reason', '不可用')}）"
            elif result.get("check_state") == "unknown":
                detail += " · 待确认（限流、超时或 JS 规则，不停用）"
            healthy = result.get("check_state") == "fully_usable"
            color = ft.Colors.GREEN if healthy else ft.Colors.ORANGE
            pending_rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.CHECK_CIRCLE if healthy else ft.Icons.INFO_OUTLINE,
                                color=color),
                title=ft.Text(result["name"]),
                subtitle=ft.Text(detail),
            ))
            flush_results()

        try:
            self._results = await check_sources(
                sources, on_result=receive,
                auto_disable=bool(self._auto_disable.value))
            flush_results(force=True)
            by_url = {source.bookSourceUrl: source for source in sources}
            for result in self._results:
                source = by_url.get(result.get("raw_url"))
                if source:
                    self.service.category_checks[source.bookSourceUrl] = result.get(
                        "category_results", [])
            disabled_count = sum(1 for result in self._results if result.get("auto_disabled"))
            if disabled_count:
                self.service.save()
            available = sum(1 for result in self._results
                            if result.get("search_status") == "ok")
            readable = sum(1 for result in self._results
                           if result.get("check_state") == "fully_usable")
            reachable = sum(1 for result in self._results if result["status"] == "ok")
            tag_total = sum(result.get("tag_total", 0) for result in self._results)
            tag_valid = sum(result.get("tag_valid", 0) for result in self._results)
            uncertain = sum(1 for result in self._results
                            if result.get("check_state") == "unknown")
            self._summary.value = (
                f"检测完成：服务器可达 {reachable}/{len(sources)}，"
                f"完整可读 {readable}/{len(sources)}，搜索可用 {available}/{len(sources)}，"
                f"标签可用 {tag_valid}/{tag_total}，"
                f"{'自动停用 ' + str(disabled_count) + ' 个，' if self._auto_disable.value else ''}"
                f"待确认 {uncertain} 个"
            )
        except Exception as exc:
            flush_results(force=True)
            self._summary.value = f"检测失败：{exc}"
        finally:
            self._running = False
            self._start.disabled = False
            self.update()
