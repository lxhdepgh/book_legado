"""ContentSearchBar：书内搜索条（§7.2 工具条，接 ContentSearchService）。

自旧单体 ``ReadingView`` 中未独立成型的书内检索能力（M2 新增，§1.14）落成独立
搜索条。搜索条只渲染：输入框 + 搜索按钮 + 命中列表；检索由
:class:`~app.controllers.reader_controller.ReaderController` 经
``ContentSearchService.search(book_url, keyword)`` 完成，命中后回调 on_jump 跳转。

红线：本文件**不 import app.services.***；命中以 dict 列表注入。
"""
from __future__ import annotations

import flet as ft


class ContentSearchBar(ft.Column):
    """书内搜索条：输入行 + 状态 + 命中列表。"""

    def __init__(self) -> None:
        super().__init__(spacing=4)

        # -- 回调槽 --
        self.on_search = None     # (keyword: str) -> None
        self.on_jump = None       # (hit: dict) -> None
        self.on_close = None      # () -> None

        self._keyword = ft.TextField(
            hint_text="书内搜索关键词", dense=True, height=40, expand=True,
            prefix_icon=ft.Icons.SEARCH,
            on_submit=lambda e: self._fire(self.on_search, self._keyword.value))
        self._result_count = ft.Text("", size=11, color=ft.Colors.GREY_500)
        self._results = ft.ListView(
            expand=True, spacing=1, height=240, auto_scroll=False)
        self.controls = [
            ft.Row([
                self._keyword,
                ft.IconButton(ft.Icons.SEARCH, tooltip="搜索",
                              on_click=lambda e: self._fire(
                                  self.on_search, self._keyword.value)),
                ft.IconButton(ft.Icons.CLOSE, tooltip="关闭",
                              on_click=lambda e: self._fire(self.on_close)),
            ], spacing=4),
            self._result_count,
            self._results,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        self._result_count.value = text or ""

    def render_results(self, hits: list[dict]) -> None:
        """渲染命中；hits 项：{chapter_index, offset, snippet}。"""
        self._results.controls.clear()
        if not hits:
            self.set_status("无命中（未缓存章节需先缓存整本再建索引）")
            return
        self.set_status(f"共 {len(hits)} 处命中")
        for hit in hits:
            snippet = (hit.get("snippet", "") or "").replace("<mark>", "").replace("</mark>", "")
            self._results.controls.append(ft.ListTile(
                dense=True,
                leading=ft.Icon(ft.Icons.NUMBERS, size=16),
                title=ft.Text(f"第 {int(hit.get('chapter_index', 0)) + 1} 章",
                              size=12),
                subtitle=ft.Text(snippet, size=11, max_lines=2,
                                 overflow=ft.TextOverflow.ELLIPSIS),
                on_click=lambda e, h=hit: self._fire(self.on_jump, h),
            ))
