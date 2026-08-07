"""书源连通性、搜索能力和分类标签检测页面。"""
from __future__ import annotations

import flet as ft

from app.services.check_service import CHECK_CONCURRENCY, TAG_PAGE_CONCURRENCY, check_sources


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
        self._start = ft.Button("开始检测", icon=ft.Icons.PLAY_ARROW,
                                on_click=self._start_check)
        self.controls = [
            ft.Container(padding=16, content=ft.Column([
                ft.Text("书源检测", size=28, weight=ft.FontWeight.BOLD),
                ft.Text(
                    f"使用 {CHECK_CONCURRENCY} 个书源协程、最多 {TAG_PAGE_CONCURRENCY} 个标签页协程，"
                    "检测服务器、搜索规则、每个分类标签及第二页加载；无可用搜索且无有效标签的书源会自动停用。",
                    color=ft.Colors.GREY_600,
                ),
                ft.Row([self._enabled_only, self._start], wrap=True, run_spacing=8),
                self._progress,
                self._summary,
            ])),
            ft.Divider(height=1),
            ft.Container(expand=True, padding=ft.Padding(16, 0, 16, 12),
                         content=self._rows),
        ]

    def _start_check(self, _):
        if not self._running:
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
            if result.get("auto_disabled"):
                detail += f" · 已自动停用（{result.get('disable_reason', '不可用')}）"
            elif result.get("check_state") == "unknown":
                detail += " · 待确认（限流、超时或 JS 规则，不停用）"
            healthy = (result["status"] == "ok" and
                       result.get("search_status") == "ok" and
                       result.get("tag_invalid", 0) == 0)
            color = ft.Colors.GREEN if healthy else ft.Colors.ORANGE
            pending_rows.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.CHECK_CIRCLE if healthy else ft.Icons.INFO_OUTLINE,
                                color=color),
                title=ft.Text(result["name"]),
                subtitle=ft.Text(detail),
            ))
            flush_results()

        try:
            self._results = await check_sources(sources, on_result=receive)
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
                            if result["status"] == "ok" and
                            result.get("search_status") == "ok")
            reachable = sum(1 for result in self._results if result["status"] == "ok")
            tag_total = sum(result.get("tag_total", 0) for result in self._results)
            tag_valid = sum(result.get("tag_valid", 0) for result in self._results)
            uncertain = sum(1 for result in self._results
                            if result.get("check_state") == "unknown")
            self._summary.value = (
                f"检测完成：服务器可达 {reachable}/{len(sources)}，"
                f"搜索可用 {available}/{len(sources)}，标签可用 {tag_valid}/{tag_total}，"
                f"自动停用 {disabled_count} 个，待确认 {uncertain} 个"
            )
        except Exception as exc:
            flush_results(force=True)
            self._summary.value = f"检测失败：{exc}"
        finally:
            self._running = False
            self._start.disabled = False
            self.update()
