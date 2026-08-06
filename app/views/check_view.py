"""书源连通性与搜索可用性检测页面。"""
from __future__ import annotations

import flet as ft

from app.services.check_service import check_sources


class CheckView(ft.Column):
    def __init__(self, service):
        super().__init__(expand=True, spacing=12)
        self.service = service
        self._running = False
        self._results = []
        self._summary = ft.Text("尚未检测", color=ft.Colors.GREY_600)
        self._progress = ft.ProgressBar(value=0)
        self._rows = ft.ListView(expand=True, spacing=4)
        self._enabled_only = ft.Checkbox(label="仅检测已启用书源", value=True)
        self._start = ft.Button("开始检测", icon=ft.Icons.PLAY_ARROW, on_click=self._start_check)
        self.controls = [
            ft.Container(padding=16, content=ft.Column([
                ft.Text("书源检测", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("使用 50 个异步协程检测服务器、搜索规则和分类标签。JS、登录、签名类规则会标记为不支持。", color=ft.Colors.GREY_600),
                ft.Row([self._enabled_only, self._start]), self._progress, self._summary,
            ])),
            ft.Divider(height=1),
            ft.Container(expand=True, padding=ft.Padding(16, 0, 16, 12), content=self._rows),
        ]

    def _start_check(self, e):
        if self._running:
            return
        self.page.run_task(self._run_check)

    async def _run_check(self):
        self._running = True
        self._start.disabled = True
        self._rows.controls.clear()
        self._progress.value = 0
        sources = [s for s in self.service.sources if s.enabled] if self._enabled_only.value else list(self.service.sources)
        self._summary.value = f"准备检测 {len(sources)} 个书源…"
        self.update()

        def receive(result, done, total):
            self._progress.value = done / max(total, 1)
            l1 = result["status"].upper()
            l2 = (result.get("search_status") or "-").upper()
            count = result.get("book_count")
            detail = f"连通 {l1} · 搜索 {l2}"
            if result.get("search_status") == "js_needed" and result.get("search_method"):
                detail += f"（{result['search_method']}）"
            if count is not None and count >= 0:
                detail += f" · {count} 本"
            if result.get("response_time_ms") is not None:
                detail += f" · {result['response_time_ms']:.0f} ms"
            categories = result.get("category_results", [])
            if categories:
                valid_categories = sum(1 for item in categories if item.get("valid"))
                detail += f" · 分类 {valid_categories}/{len(categories)}"
            color = ft.Colors.GREEN if result["status"] == "ok" and result.get("search_status") == "ok" else ft.Colors.ORANGE
            self._rows.controls.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.CHECK_CIRCLE if color == ft.Colors.GREEN else ft.Icons.INFO_OUTLINE, color=color),
                title=ft.Text(result["name"]), subtitle=ft.Text(detail),
            ))
            self._summary.value = f"检测进度：{done}/{total}"
            self.update()

        try:
            self._results = await check_sources(sources, on_result=receive)
            for source, result in zip(sources, self._results):
                self.service.category_checks[source.bookSourceUrl] = result.get("category_results", [])
            available = sum(1 for r in self._results if r["status"] == "ok" and r.get("search_status") == "ok")
            reachable = sum(1 for r in self._results if r["status"] == "ok")
            category_total = sum(len(r.get("category_results", [])) for r in self._results)
            category_valid = sum(sum(1 for item in r.get("category_results", []) if item.get("valid"))
                                 for r in self._results)
            self._summary.value = (f"检测完成：服务器可达 {reachable}/{len(sources)}，"
                                   f"搜索可用 {available}/{len(sources)}，"
                                   f"分类可用 {category_valid}/{category_total}")
        except Exception as exc:
            self._summary.value = f"检测失败：{exc}"
        finally:
            self._running = False
            self._start.disabled = False
            self.update()
