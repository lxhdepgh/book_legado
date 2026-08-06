"""书籍搜索、目录与正文阅读页面。"""
from __future__ import annotations

import flet as ft

from app.services.rule_engine import ReaderEngine, UnsupportedRule, parse_explore_categories


class ReadingView(ft.Column):
    def __init__(self, service):
        super().__init__(spacing=0, expand=True)
        self.service = service
        self.engine = ReaderEngine()
        self.books = []
        self.chapters = []
        self.current_source = None
        self.categories = []
        self._result_page = 1
        self._result_mode = None
        self._active_category = 0
        self._loading_more = False
        self._has_more = True
        self._source = ft.Dropdown(label="书源", width=300, on_select=self._source_changed)
        self._keyword = ft.TextField(label="书名或关键词", expand=True, on_submit=self._search)
        self._search_btn = ft.Button("搜索", icon=ft.Icons.SEARCH, on_click=self._search)
        self._status = ft.Text("选择一个书源后搜索", color=ft.Colors.GREY_600)
        self._results = ft.ListView(expand=True, spacing=4, on_scroll=self._on_result_scroll,
                                    scroll_interval=250)
        self._category_host = ft.Column(spacing=0)
        self._discover = ft.Column(expand=True, spacing=0, controls=[self._category_host, self._results])
        self._chapters = ft.ListView(expand=True, spacing=2)
        self._title = ft.Text("正文", size=22, weight=ft.FontWeight.BOLD)
        self._content = ft.Text("请选择章节", selectable=True, size=16)
        self._reader = ft.ListView(expand=True, controls=[self._title, ft.Divider(), self._content])
        self._tabs = ft.Tabs(length=3, expand=True, content=ft.Column(expand=True, controls=[
            ft.TabBar(tabs=[ft.Tab(label="搜索结果"), ft.Tab(label="目录"), ft.Tab(label="阅读")]),
            ft.TabBarView(expand=True, controls=[self._discover, self._chapters, self._reader]),
        ]))
        self.controls = [
            ft.Container(padding=16, content=ft.Column([
                ft.Text("阅读", size=28, weight=ft.FontWeight.BOLD),
                ft.Row([self._source, self._keyword, self._search_btn]), self._status,
            ])), self._tabs,
        ]

    def did_mount(self):
        self.refresh_sources()
        if self.categories:
            self.page.run_task(self._run_explore, 0)

    def refresh_sources(self, update=True):
        enabled = [s for s in self.service.sources if s.enabled and (s.searchUrl or s.exploreUrl)]
        self._source.options = [ft.dropdown.Option(str(i), s.display_name) for i, s in enumerate(enabled)]
        self._source_map = enabled
        if enabled and (self._source.value is None or int(self._source.value) >= len(enabled)):
            self._source.value = "0"
            self.current_source = enabled[0]
        elif not enabled:
            self._source.value = None
            self.current_source = None
        self._rebuild_category_tabs()
        if update:
            self.update()

    def _source_changed(self, e):
        if self._source.value is not None:
            self.current_source = self._source_map[int(self._source.value)]
            self._reset_results()
            self._rebuild_category_tabs()
            if self.categories:
                self.page.run_task(self._run_explore, 0, True)
            else:
                self._status.value = "该书源没有可解析的分类标签，可使用关键词搜索"
            self.update()

    def _rebuild_category_tabs(self):
        self.categories = parse_explore_categories(self.current_source) if self.current_source else []
        checked = getattr(self.service, "category_checks", {}).get(
            self.current_source.bookSourceUrl if self.current_source else ""
        )
        if checked is not None:
            valid_urls = {item["url"] for item in checked if item.get("valid")}
            self.categories = [category for category in self.categories if category.url in valid_urls]
        if not self.categories:
            self._category_host.controls = []
            return
        tab_bar = ft.TabBar(
            scrollable=True,
            tabs=[ft.Tab(label=category.title) for category in self.categories],
        )
        # 分类只共享一个结果列表；TabBarView 使用轻量占位控件承接选择状态。
        tab_view = ft.TabBarView(height=1, controls=[ft.Container() for _ in self.categories])
        self._category_tabs = ft.Tabs(
            selected_index=0,
            length=len(self.categories),
            on_change=self._category_changed,
            content=ft.Column(spacing=0, controls=[tab_bar, tab_view]),
        )
        self._category_host.controls = [self._category_tabs]

    def _category_changed(self, e):
        self.page.run_task(self._run_explore, self._category_tabs.selected_index, True)

    async def _run_explore(self, index, reset=True):
        if index < 0 or index >= len(self.categories):
            return
        if self._loading_more:
            return
        if reset:
            self._reset_results()
            self._active_category = index
            self._result_mode = "explore"
        category = self.categories[index]
        supports_paging = any(token in category.url for token in
                              ("{{page}}", "{{page-1}}", "{{page+1}}", "<,"))
        self._loading_more = True
        self._set_busy(f"正在加载“{category.title}”第 {self._result_page} 页…")
        try:
            new_books = await self.engine.explore(self.current_source, category, self._result_page)
            added = self._append_books(new_books)
            self._has_more = supports_paging and bool(new_books) and added > 0
            if self._has_more:
                self._result_page += 1
            self._render_books()
            paging_text = "，可继续下拉加载" if self._has_more else (
                "，固定榜单无后续分页" if not supports_paging else "，已加载全部结果"
            )
            self._status.value = f"{category.title}：找到 {len(self.books)} 本书{paging_text}"
            self._tabs.selected_index = 0
        except Exception as exc:
            self._show_error(exc)
            self._has_more = False
        finally:
            self._loading_more = False
        self.update()

    def _search(self, e):
        if not self.current_source or not (self._keyword.value or "").strip():
            self._status.value = "请选择书源并输入关键词"
            self.update()
            return
        self.page.run_task(self._run_search)

    async def _run_search(self):
        if self._loading_more:
            return
        self._reset_results()
        self._result_mode = "search"
        self._loading_more = True
        self._set_busy("正在搜索…")
        try:
            new_books = await self.engine.search(self.current_source, self._keyword.value.strip(), self._result_page)
            self._append_books(new_books)
            self._has_more = bool(new_books)
            if self._has_more:
                self._result_page += 1
            self._render_books()
            self._status.value = f"找到 {len(self.books)} 本书，点击书籍加载目录"
            self._tabs.selected_index = 0
        except Exception as exc:
            self._show_error(exc)
            self._has_more = False
        finally:
            self._loading_more = False
        self.update()

    def _reset_results(self):
        self.books = []
        self._result_page = 1
        self._has_more = True
        self._results.controls.clear()

    def _append_books(self, new_books):
        existing = {book.url for book in self.books}
        added = 0
        for book in new_books:
            if book.url not in existing:
                self.books.append(book)
                existing.add(book.url)
                added += 1
        return added

    def _render_books(self):
        self._results.controls = [ft.ListTile(
            leading=ft.Icon(ft.Icons.BOOK_OUTLINED), title=ft.Text(book.name),
            subtitle=ft.Text(" · ".join(x for x in [book.author, book.intro[:80]] if x)),
            on_click=lambda e, selected=book: self._load_book(selected),
        ) for book in self.books]
        if self._has_more:
            self._results.controls.append(ft.Button("加载更多", icon=ft.Icons.EXPAND_MORE,
                                                    on_click=self._load_more))
        elif self.books:
            self._results.controls.append(ft.Text("已经到底了", text_align=ft.TextAlign.CENTER,
                                                  color=ft.Colors.GREY_500))

    def _on_result_scroll(self, e):
        pixels = getattr(e, "pixels", 0) or 0
        maximum = getattr(e, "max_scroll_extent", 0) or 0
        if self._has_more and not self._loading_more and maximum and maximum - pixels < 320:
            self._load_more()

    def _load_more(self, e=None):
        if self._loading_more or not self._has_more:
            return
        if self._result_mode == "explore":
            self.page.run_task(self._run_explore, self._active_category, False)
        elif self._result_mode == "search":
            self.page.run_task(self._run_search_more)

    async def _run_search_more(self):
        if self._loading_more:
            return
        self._loading_more = True
        self._set_busy(f"正在加载搜索结果第 {self._result_page} 页…")
        try:
            new_books = await self.engine.search(self.current_source, self._keyword.value.strip(), self._result_page)
            added = self._append_books(new_books)
            self._has_more = bool(new_books) and added > 0
            if self._has_more:
                self._result_page += 1
            self._render_books()
            self._status.value = f"已加载 {len(self.books)} 本书"
        except Exception as exc:
            self._show_error(exc)
            self._has_more = False
        finally:
            self._loading_more = False
            self.update()

    def _load_book(self, book):
        self.page.run_task(self._run_chapters, book)

    async def _run_chapters(self, book):
        self._set_busy(f"正在加载《{book.name}》目录…")
        try:
            self.chapters = await self.engine.chapters(self.current_source, book)
            self._chapters.controls = [ft.ListTile(
                title=ft.Text(c.name), dense=True,
                on_click=lambda e, chapter=c: self._load_content(chapter),
            ) for c in self.chapters]
            self._status.value = f"《{book.name}》共 {len(self.chapters)} 章"
            self._tabs.selected_index = 1
        except Exception as exc:
            self._show_error(exc)
        self.update()

    def _load_content(self, chapter):
        self.page.run_task(self._run_content, chapter)

    async def _run_content(self, chapter):
        self._set_busy(f"正在加载 {chapter.name}…")
        try:
            content = await self.engine.content(self.current_source, chapter)
            self._title.value = chapter.name
            self._content.value = content or "正文解析结果为空"
            self._status.value = chapter.name
            self._tabs.selected_index = 2
        except Exception as exc:
            self._show_error(exc)
        self.update()

    def _set_busy(self, text):
        self._status.value = text
        self.update()

    def _show_error(self, exc):
        prefix = "当前书源暂不支持" if isinstance(exc, UnsupportedRule) else "操作失败"
        self._status.value = f"{prefix}：{exc}"
