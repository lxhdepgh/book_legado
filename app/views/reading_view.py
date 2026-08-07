"""书籍搜索、目录与正文阅读页面。"""
from __future__ import annotations

import asyncio
import base64
import re
import uuid

import flet as ft
import flet_audio as fta
import flet_video as ftv
import flet_webview as ftw

from app.services.rule_engine import (UnsupportedRule, parse_explore_categories,
                                      resolve_explore_categories, BookItem)
from app.services.reading_engine import LegadoReadingEngine
from app.services.desktop_browser import EdgeCdpSession


class ReadingView(ft.Column):
    DIRECTORY_BATCH_SIZE = 50
    PREFETCH_CHAPTER_COUNT = 10
    PREFETCH_CACHE_LIMIT = 64 * 1024 * 1024

    def __init__(self, service, discovery_only=False):
        super().__init__(spacing=0, expand=True)
        self.service = service
        self.discovery_only = discovery_only
        self.engine = LegadoReadingEngine(
            webview_fetcher=self._fetch_with_webview,
            verification_fetcher=self._get_verification_code,
            state_dir=service.data_dir,
        )
        self._webview = None
        self._desktop_webview = None
        self._webview_future = None
        self._verification_future = None
        self._verification_input = None
        self._verification_dialog = None
        self._webview_marker = ""
        self._webview_capture_pending = False
        self._webview_script = ""
        self._webview_delay = 0
        self._webview_source_regex = ""
        self._current_chapter = None
        self._reading_loading = False
        self._prefetch_task = None
        self._chapter_cache = {}
        self._continuous_loaded = set()
        self._auto_scroll_armed = True
        self.books = []
        self.chapters = []
        self.current_source = None
        self.categories = []
        self._result_page = 1
        self._result_mode = None
        self._active_category = 0
        self._loading_more = False
        self._category_load_id = 0
        self._chapter_visible_count = 0
        self._has_more = True
        self._book_detail_dialog = None
        self._detail_book = None
        self._source = ft.Dropdown(label="书源", on_select=self._source_changed)
        self._keyword = ft.TextField(label="书名或关键词", expand=True, on_submit=self._search)
        self._search_btn = ft.Button("搜索", icon=ft.Icons.SEARCH, on_click=self._search)
        self._login_btn = ft.TextButton(
            "登录书源", icon=ft.Icons.LOGIN, visible=False,
            on_click=self._show_source_login)
        self._login_fields = {}
        self._login_dialog = None
        self._status = ft.Text("选择一个书源后搜索", color=ft.Colors.GREY_600)
        self._results = ft.ListView(expand=True, spacing=4, on_scroll=self._on_result_scroll,
                                    scroll_interval=250)
        self._category_host = ft.Column(spacing=0)
        self._discover = ft.Column(expand=True, spacing=0, controls=[self._category_host, self._results])
        self._bookshelf = ft.ListView(expand=True, spacing=4)
        self._chapters = ft.ListView(
            expand=True, spacing=2, on_scroll=self._on_chapter_scroll,
            scroll_interval=200,
        )
        self._title = ft.Text("正文", size=22, weight=ft.FontWeight.BOLD)
        self._content_host = ft.Column(spacing=12)
        self._prev_chapter_btn = ft.Button("上一章", icon=ft.Icons.CHEVRON_LEFT,
                                           disabled=True, on_click=self._previous_chapter)
        self._directory_btn = ft.Button("目录", icon=ft.Icons.MENU_BOOK,
                                        on_click=self._show_directory)
        self._next_chapter_btn = ft.Button("下一章", icon=ft.Icons.CHEVRON_RIGHT,
                                           disabled=True, on_click=self._next_chapter)
        self._reader_nav = ft.Row(
            [self._prev_chapter_btn, self._directory_btn, self._next_chapter_btn],
            alignment=ft.MainAxisAlignment.CENTER,
            wrap=True,
        )
        self._reader = ft.ListView(
            expand=True, scroll_interval=200, on_scroll=self._on_reader_scroll,
            controls=[self._title, ft.Divider(), self._content_host,
                      ft.Divider(), self._reader_nav],
        )
        self._tabs = ft.Tabs(length=3, expand=True, content=ft.Column(expand=True, controls=[
            ft.TabBar(tabs=[ft.Tab(label="书架"), ft.Tab(label="目录"), ft.Tab(label="阅读")]),
            ft.TabBarView(expand=True, controls=[self._bookshelf, self._chapters, self._reader]),
        ]))
        self._source.col = {"xs": 12, "sm": 5}
        self._keyword.col = {"xs": 8, "sm": 5}
        self._search_btn.col = {"xs": 4, "sm": 2}
        discovery_header = ft.Container(padding=12, content=ft.Column([
                ft.Text("找书", size=28, weight=ft.FontWeight.BOLD),
                ft.ResponsiveRow(
                    [self._source, self._keyword, self._search_btn],
                    columns=12, spacing=8, run_spacing=8,
                ),
                ft.Row([self._status, self._login_btn],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ]))
        if discovery_only:
            self.controls = [discovery_header, self._discover]
        else:
            self.controls = [
                ft.Container(padding=12, content=ft.Column([
                    ft.Text("我的书架", size=28, weight=ft.FontWeight.BOLD),
                    self._status,
                ])),
                self._tabs,
            ]

    def did_mount(self):
        if self.discovery_only:
            self.refresh_sources()
            self.page.run_task(self._load_categories, self.current_source, True)
        else:
            self._render_bookshelf()

    def will_unmount(self):
        if self._webview_future and not self._webview_future.done():
            self._webview_future.cancel()
        if self._verification_future and not self._verification_future.done():
            self._verification_future.cancel()
        self._cancel_prefetch(clear_cache=True)

    async def _get_verification_code(self, image, source):
        """Display the image captcha requested by java.getVerificationCode()."""
        if self._verification_future and not self._verification_future.done():
            raise RuntimeError("已有验证码输入任务正在进行")
        loop = asyncio.get_running_loop()
        self._verification_future = loop.create_future()
        image_src = str(image or "")
        if (image_src and "://" not in image_src and
                not image_src.startswith(("data:", "/"))):
            image_src = "data:image/png;base64," + image_src
        self._verification_input = ft.TextField(
            label="验证码", autofocus=True, on_submit=self._submit_verification)
        controls = []
        if image_src:
            controls.append(ft.Image(src=image_src, height=160, fit=ft.BoxFit.CONTAIN))
        controls.append(self._verification_input)
        self._verification_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"输入验证码 · {getattr(source, 'bookSourceName', '')}"),
            content=ft.Container(width=360, content=ft.Column(controls, tight=True)),
            actions=[
                ft.TextButton("取消", on_click=self._cancel_verification),
                ft.Button("确定", on_click=self._submit_verification),
            ],
        )
        self.page.show_dialog(self._verification_dialog)
        try:
            return await self._verification_future
        finally:
            self._verification_future = None
            self._verification_input = None
            self._verification_dialog = None

    def _submit_verification(self, _):
        if self._verification_future and not self._verification_future.done():
            value = self._verification_input.value if self._verification_input else ""
            self._verification_future.set_result(str(value or "").strip())
        if self._verification_dialog:
            self.page.pop_dialog()

    def _cancel_verification(self, _):
        if self._verification_future and not self._verification_future.done():
            self._verification_future.set_exception(RuntimeError("验证码输入已取消"))
        if self._verification_dialog:
            self.page.pop_dialog()

    def _supports_webview(self):
        return self.page.platform in {
            ft.PagePlatform.ANDROID, ft.PagePlatform.ANDROID_TV,
            ft.PagePlatform.IOS, ft.PagePlatform.MACOS,
        }

    async def _fetch_with_webview(self, url, source, options=None):
        """Legado-style WebView fallback: render JS, then return final DOM HTML."""
        if self._webview_future and not self._webview_future.done():
            raise RuntimeError("已有一个网页验证任务正在进行")
        loop = asyncio.get_running_loop()
        self._webview_future = loop.create_future()
        self._webview_marker = f"__LEGADO_HTML_{uuid.uuid4().hex}__"
        options = options or {}
        self._webview_script = str(options.get("webJs") or "")
        self._webview_delay = int(options.get("webViewDelayTime") or 0)
        self._webview_source_regex = str(options.get("sourceRegex") or "")
        if self.page.platform == ft.PagePlatform.WINDOWS:
            self._desktop_webview = EdgeCdpSession(
                self.service.data_dir / "edge-webview-profile")
            await self._desktop_webview.start(url)
            dialog_content = ft.Container(
                width=400, height=150,
                content=ft.Column([
                    ft.Icon(ft.Icons.OPEN_IN_NEW, size=42),
                    ft.Text("验证页面已在 Microsoft Edge 中打开。完成登录或人机验证后，返回此处读取页面。"),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER))
        elif self._supports_webview():
            self._webview = ftw.WebView(
                url=url, expand=True,
                on_page_ended=self._webview_page_ended,
                on_console_message=self._webview_console_message,
                on_web_resource_error=self._webview_resource_error)
            dialog_content = ft.Container(width=400, height=620, content=self._webview)
        else:
            raise RuntimeError("当前平台没有可用的网页验证组件")
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"网页验证 · {getattr(source, 'bookSourceName', '')}"),
            content=dialog_content,
            actions=[
                ft.TextButton("读取当前页面", on_click=self._capture_webview_now),
                ft.TextButton("取消", on_click=self._cancel_webview),
            ],
        )
        self.page.show_dialog(dialog)
        self._status.value = "请在网页中完成验证；通过后会自动继续解析"
        self.update()
        try:
            return await self._webview_future
        finally:
            if self._desktop_webview:
                await self._desktop_webview.close()
                self._desktop_webview = None
            self._webview_future = None
            self._webview = None
            self._webview_capture_pending = False
            self._webview_script = ""
            self._webview_delay = 0
            self._webview_source_regex = ""

    def _webview_page_ended(self, _):
        if self._webview and not self._webview_capture_pending:
            self._webview_capture_pending = True
            self.page.run_task(self._capture_webview_dom)

    def _capture_webview_now(self, _):
        if self._desktop_webview:
            self.page.run_task(self._capture_desktop_webview)
        elif self._webview:
            self.page.run_task(self._capture_webview_dom)

    async def _capture_desktop_webview(self):
        if not self._desktop_webview or not self._webview_future:
            return
        try:
            response = await self._desktop_webview.capture(
                self._webview_script, self._webview_delay)
            html = response.get("body", "")
            waiting_for_source = not self._webview_source_ready(html)
            if ("Just a moment" in html or "challenge-platform" in html or
                    waiting_for_source):
                self._status.value = "网页验证尚未完成，请完成验证后再次读取页面"
                self.update()
                return
            if not self._webview_future.done():
                self._webview_future.set_result(response)
            self.page.pop_dialog()
        except Exception as exc:
            self._status.value = f"读取 Edge 页面失败：{exc}"
            self.update()

    async def _capture_webview_dom(self):
        if not self._webview or not self._webview_future:
            return
        try:
            if self._webview_script:
                script, self._webview_script = self._webview_script, ""
                await self._webview.run_javascript(script)
                if self._webview_delay:
                    await asyncio.sleep(max(0, self._webview_delay) / 1000)
            # The extension does not return JS values, so use its console event
            # as the same kind of bridge that Legado exposes to WebView scripts.
            script = (
                "console.log('" + self._webview_marker + "' + "
                "btoa(unescape(encodeURIComponent(document.documentElement.outerHTML))))"
            )
            await self._webview.run_javascript(script)
        finally:
            self._webview_capture_pending = False

    def _webview_source_ready(self, html):
        if not self._webview_source_regex:
            return True
        try:
            return re.search(self._webview_source_regex, html) is not None
        except re.error:
            return True

    async def _webview_console_message(self, event):
        message = getattr(event, "message", "") or ""
        if not self._webview_future or self._webview_future.done() or not message.startswith(
                self._webview_marker):
            return
        try:
            html = base64.b64decode(message[len(self._webview_marker):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return
        waiting_for_source = not self._webview_source_ready(html)
        if ("Just a moment" in html or "challenge-platform" in html or
                waiting_for_source):
            self._status.value = "网页验证尚未完成，请完成验证后点击“读取当前页面”"
            self.update()
            return
        final_url = await self._webview.get_current_url() or ""
        self._webview_future.set_result((html, "text/html", final_url))
        self.page.pop_dialog()

    def _webview_resource_error(self, event):
        self._status.value = f"网页加载失败：{getattr(event, 'data', '') or 'WebView 资源错误'}"
        self.update()

    def _cancel_webview(self, _):
        if self._webview_future and not self._webview_future.done():
            self._webview_future.set_exception(RuntimeError("用户取消网页验证"))
        self.page.pop_dialog()

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
        self._update_login_button()
        self._rebuild_category_tabs()
        if update:
            self.update()

    def _source_changed(self, e):
        if self._source.value is not None:
            self.current_source = self._source_map[int(self._source.value)]
            self._update_login_button()
            self._cancel_prefetch(clear_cache=True)
            self._reset_results()
            self._category_load_id += 1
            self._rebuild_category_tabs([])
            self._status.value = "正在解析分类标签…"
            self.update()
            self.page.run_task(self._load_categories, self.current_source, True,
                               self._category_load_id)

    def _update_login_button(self):
        source = self.current_source
        self._login_btn.visible = bool(
            source and self.engine.login_available(source))

    def _show_source_login(self, _):
        if self.current_source:
            self.page.run_task(self._open_source_login, self.current_source)

    async def _open_source_login(self, source):
        try:
            schema = await self.engine.login_schema(source)
            if not schema:
                await self.engine.execute_login_action(source)
                self._status.value = "网页登录已完成"
                self.update()
                return
            self._login_fields = {}
            controls = []
            for index, item in enumerate(schema):
                field_type = str(item.get("type", "text")).lower()
                name = str(item.get("name") or f"field_{index}")
                if field_type == "button":
                    controls.append(ft.Button(
                        name, on_click=lambda e, action=str(item.get("action", "")):
                        self.page.run_task(self._run_login_action, source, action)))
                    continue
                if field_type == "select":
                    options = item.get("values") or item.get("items") or item.get("chars") or []
                    control = ft.Dropdown(
                        label=name,
                        options=[ft.dropdown.Option(str(value)) for value in options],
                        value=str(item.get("value", item.get("default", ""))) or None)
                else:
                    control = ft.TextField(
                        label=name,
                        value=str(item.get("value", item.get("default", "")) or ""),
                        password=field_type == "password",
                        can_reveal_password=field_type == "password")
                self._login_fields[name] = control
                controls.append(control)
            self._login_dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"书源登录 · {source.display_name}"),
                content=ft.Container(
                    width=420, height=min(600, max(160, len(controls) * 58)),
                    content=ft.ListView(controls=controls, spacing=8)),
                actions=[ft.TextButton("关闭", on_click=self._close_login_dialog)])
            self.page.show_dialog(self._login_dialog)
        except Exception as exc:
            self._show_error(exc)

    async def _run_login_action(self, source, action):
        values = {name: control.value for name, control in self._login_fields.items()}
        try:
            result = await self.engine.execute_login_action(source, action, values)
            self._status.value = "登录操作已执行" + (f"：{result}" if result else "")
            self.update()
        except Exception as exc:
            self._show_error(exc)

    def _close_login_dialog(self, _):
        if self._login_dialog:
            self.page.pop_dialog()
            self._login_dialog = None

    async def _load_categories(self, source, run_first=False, load_id=None):
        if source is None:
            return
        try:
            categories = await resolve_explore_categories(source, self.engine.js)
        except Exception as exc:
            if source is self.current_source:
                self._status.value = f"分类标签解析失败：{str(exc)[:100]}"
                self.update()
            return
        if (source is not self.current_source or
                (load_id is not None and load_id != self._category_load_id)):
            return
        self._rebuild_category_tabs(categories)
        if self.categories and run_first:
            await self._run_explore(0, True)
        elif not self.categories:
            self._status.value = "该书源没有可解析的分类标签，可使用关键词搜索"
        self.update()

    def _rebuild_category_tabs(self, categories=None):
        self.categories = (categories if categories is not None else
                           (parse_explore_categories(self.current_source)
                            if self.current_source else []))
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
            added_books = self._append_books(new_books)
            added = len(added_books)
            self._has_more = supports_paging and bool(new_books) and added > 0
            if self._has_more:
                self._result_page += 1
            self._append_book_controls(added_books)
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
            added_books = self._append_books(new_books)
            self._has_more = bool(new_books)
            if self._has_more:
                self._result_page += 1
            self._append_book_controls(added_books)
            self._status.value = f"找到 {len(self.books)} 本书，点击书籍查看详情"
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
        added = []
        for book in new_books:
            if book.url not in existing:
                self.books.append(book)
                existing.add(book.url)
                added.append(book)
        return added

    def _book_control(self, book):
        cover = (ft.Image(src=book.cover_url, width=42, height=56,
                          fit=ft.BoxFit.COVER,
                          error_content=ft.Icon(ft.Icons.BROKEN_IMAGE))
                 if book.cover_url else ft.Icon(ft.Icons.BOOK_OUTLINED))
        if (book.cover_url and self.current_source and
                getattr(self.current_source, "coverDecodeJs", "")):
            self.page.run_task(self._decode_book_cover, book, cover)
        return ft.ListTile(
            leading=cover, title=ft.Text(book.name),
            subtitle=ft.Text(" · ".join(x for x in [book.author, book.intro[:80]] if x)),
            trailing=ft.IconButton(
                ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="加入书架",
                on_click=lambda e, selected=book: self._add_to_bookshelf(selected),
            ),
            on_click=lambda e, selected=book: self._show_book_detail(selected),
        )

    def _show_book_detail(self, book):
        if not self.current_source:
            return
        self._detail_book = book
        self._book_detail_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(book.name or "书籍详情"),
            content=ft.Container(
                width=420, height=220,
                content=ft.Column([
                    ft.ProgressRing(), ft.Text("正在加载书籍详情…"),
                ], alignment=ft.MainAxisAlignment.CENTER,
                   horizontal_alignment=ft.CrossAxisAlignment.CENTER)),
            actions=[ft.TextButton("关闭", on_click=self._close_book_detail)],
        )
        self.page.show_dialog(self._book_detail_dialog)
        self.page.run_task(self._load_book_detail, book, self.current_source)

    async def _load_book_detail(self, book, source):
        error = ""
        try:
            await self.engine.book_info(source, book)
            if book.cover_url and getattr(source, "coverDecodeJs", ""):
                book.cover_url = await self.engine.decode_cover(source, book.cover_url)
        except Exception as exc:
            error = str(exc)
        if self._detail_book is not book or self._book_detail_dialog is None:
            return
        cover = (ft.Image(
            src=book.cover_url, width=110, height=150, fit=ft.BoxFit.COVER,
            error_content=ft.Icon(ft.Icons.BROKEN_IMAGE, size=48))
                 if book.cover_url else
                 ft.Container(
                     width=110, height=150, alignment=ft.Alignment.CENTER,
                     content=ft.Icon(ft.Icons.MENU_BOOK, size=54)))
        metadata = [
            ("作者", book.author), ("分类", book.kind),
            ("字数", book.word_count), ("最新章节", book.last_chapter),
        ]
        meta_controls = [
            ft.Text(f"{label}：{value}", selectable=True)
            for label, value in metadata if value
        ] or [ft.Text("暂无其他书籍信息", color=ft.Colors.GREY_600)]
        controls = [
            ft.Row(
                [cover, ft.Column(meta_controls, expand=True, spacing=7)],
                vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Divider(),
            ft.Text("简介", weight=ft.FontWeight.BOLD),
            ft.Text(book.intro or "暂无简介", selectable=True),
        ]
        if error:
            controls.append(ft.Text(
                f"完整详情加载失败，已显示搜索结果中的信息。\n{error}",
                color=ft.Colors.ORANGE_700, size=12))
        self._book_detail_dialog.title = ft.Text(book.name or "书籍详情")
        self._book_detail_dialog.content = ft.Container(
            width=420, height=430,
            content=ft.ListView(controls=controls, spacing=8))
        self._book_detail_dialog.actions = [
            ft.TextButton("关闭", on_click=self._close_book_detail),
            ft.Button(
                "加入书架", icon=ft.Icons.BOOKMARK_ADD_OUTLINED,
                on_click=lambda e, selected=book:
                self._add_detail_to_bookshelf(selected)),
        ]
        self._book_detail_dialog.update()

    def _add_detail_to_bookshelf(self, book):
        self._add_to_bookshelf(book)
        self._close_book_detail(None)

    def _close_book_detail(self, _):
        if self._book_detail_dialog:
            self.page.pop_dialog()
        self._book_detail_dialog = None
        self._detail_book = None

    async def _decode_book_cover(self, book, image):
        try:
            image.src = await self.engine.decode_cover(
                self.current_source, book.cover_url)
            book.cover_url = image.src
            image.update()
        except Exception:
            # The original URL remains visible if a source decoder fails.
            return

    def _add_to_bookshelf(self, book):
        if not self.current_source:
            return
        added = self.service.add_to_bookshelf(self.current_source, book)
        self._status.value = (f"《{book.name}》已加入书架" if added else
                              f"《{book.name}》已在书架中")
        self.update()

    def refresh_bookshelf(self, update=True):
        self.service.load_bookshelf()
        self._render_bookshelf()
        if update:
            self.update()

    def _render_bookshelf(self):
        self._bookshelf.controls = [self._shelf_control(item)
                                    for item in self.service.bookshelf]
        if not self.service.bookshelf:
            self._bookshelf.controls = [ft.Container(
                padding=32,
                content=ft.Column([
                    ft.Icon(ft.Icons.MENU_BOOK, size=52, color=ft.Colors.GREY_400),
                    ft.Text("书架还是空的", size=18),
                    ft.Text("请到“书源 → 找书”中选择书籍并加入书架",
                            color=ft.Colors.GREY_600),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            )]
        self._status.value = f"书架中有 {len(self.service.bookshelf)} 本书"

    def _shelf_control(self, item):
        return ft.ListTile(
            leading=(ft.Image(src=item.get("cover_url"), width=42, height=56,
                              fit=ft.BoxFit.COVER)
                     if item.get("cover_url") else ft.Icon(ft.Icons.BOOK)),
            title=ft.Text(item.get("name") or "未命名"),
            subtitle=ft.Text(" · ".join(filter(None, [item.get("author"),
                                                       item.get("source_name")]))),
            trailing=ft.IconButton(
                ft.Icons.DELETE_OUTLINE, tooltip="移出书架",
                on_click=lambda e, selected=item: self._remove_shelf_item(selected),
            ),
            on_click=lambda e, selected=item: self._open_shelf_item(selected),
        )

    def _remove_shelf_item(self, item):
        self.service.remove_from_bookshelf(item.get("source_url", ""), item.get("url", ""))
        self._render_bookshelf()
        self.update()

    def _open_shelf_item(self, item):
        source = next((source for source in self.service.sources
                       if source.bookSourceUrl == item.get("source_url")), None)
        if source is None:
            self._status.value = "原书源已被删除，无法加载这本书"
            self.update()
            return
        self.current_source = source
        book = BookItem(
            name=item.get("name", ""), author=item.get("author", ""),
            intro=item.get("intro", ""), url=item.get("url", ""),
            cover_url=item.get("cover_url", ""), toc_url=item.get("toc_url", ""),
            kind=item.get("kind", ""), last_chapter=item.get("last_chapter", ""),
            word_count=item.get("word_count", ""),
            download_urls=item.get("download_urls", ""),
            variables=item.get("variables") or {},
        )
        self.page.run_task(self._run_chapters, book)

    def _append_book_controls(self, books):
        """追加新结果，只替换列表末尾的翻页提示。"""
        if self._results.controls and getattr(self._results.controls[-1], "data", None) == "result-footer":
            self._results.controls.pop()
        self._results.controls.extend(self._book_control(book) for book in books)
        if self._has_more:
            footer = ft.Button("加载更多", icon=ft.Icons.EXPAND_MORE,
                               on_click=self._load_more)
            footer.data = "result-footer"
            self._results.controls.append(footer)
        elif self.books:
            footer = ft.Text("已经到底了", text_align=ft.TextAlign.CENTER,
                             color=ft.Colors.GREY_500)
            footer.data = "result-footer"
            self._results.controls.append(footer)

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
            added_books = self._append_books(new_books)
            self._has_more = bool(new_books) and bool(added_books)
            if self._has_more:
                self._result_page += 1
            self._append_book_controls(added_books)
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
            self._cancel_prefetch(clear_cache=True)
            self.chapters = await self.engine.chapters(self.current_source, book)
            self.service.update_bookshelf_state(
                self.current_source.bookSourceUrl, book.url,
                variables=getattr(book, "variables", {}) or self.engine.js_variables,
                book=book)
            self._current_chapter = None
            self._continuous_loaded.clear()
            self._chapters.controls.clear()
            self._chapter_visible_count = 0
            self._append_chapter_controls()
            self._status.value = (f"《{book.name}》共 {len(self.chapters)} 章，"
                                  f"已加载 {self._chapter_visible_count} 章")
            self._tabs.selected_index = 1
        except Exception as exc:
            self._show_error(exc)
        self.update()

    def _chapter_control(self, chapter):
        locked = chapter.is_vip and not chapter.is_pay
        pay_action = str((getattr(self.current_source, "ruleContent", {}) or {}).get(
            "payAction", "") or "").strip()
        return ft.ListTile(
            title=ft.Text(chapter.name), dense=True,
            leading=ft.Icon(ft.Icons.FOLDER_OUTLINED) if chapter.is_volume else None,
            trailing=(ft.IconButton(
                ft.Icons.LOCK_OUTLINE, tooltip="购买或解锁章节",
                on_click=lambda e, selected=chapter: self.page.run_task(
                    self._run_pay_action, selected))
                      if locked and pay_action else
                      ft.Icon(ft.Icons.LOCK_OUTLINE) if locked else None),
            on_click=(None if chapter.is_volume else
                      lambda e, selected=chapter: self._load_content(selected)),
        )

    async def _run_pay_action(self, chapter):
        self._set_busy(f"正在处理 {chapter.name}…")
        try:
            result = await self.engine.execute_pay_action(self.current_source, chapter)
            self._status.value = str(result or "操作已完成，请重新打开章节")
        except Exception as exc:
            self._show_error(exc)
        self.update()

    def _append_chapter_controls(self):
        old_count = self._chapter_visible_count
        self._chapter_visible_count = min(
            old_count + self.DIRECTORY_BATCH_SIZE, len(self.chapters))
        self._chapters.controls.extend(
            self._chapter_control(self.chapters[i])
            for i in range(old_count, self._chapter_visible_count)
        )

    def _on_chapter_scroll(self, event):
        pixels = getattr(event, "pixels", 0) or 0
        maximum = getattr(event, "max_scroll_extent", 0) or 0
        if (self._chapter_visible_count >= len(self.chapters) or not maximum or
                maximum - pixels >= 320):
            return
        self._append_chapter_controls()
        self._status.value = (f"目录共 {len(self.chapters)} 章，"
                              f"已加载 {self._chapter_visible_count} 章")
        self.update()

    def _load_content(self, chapter):
        self.page.run_task(self._run_content, chapter, False)

    async def _run_content(self, chapter, append=False):
        if self._reading_loading or chapter.is_volume:
            return
        self._cancel_prefetch()
        self._reading_loading = True
        self._set_busy(f"正在加载 {chapter.name}…")
        try:
            next_chapter_url = ""
            if 0 <= chapter.index < len(self.chapters) - 1:
                next_chapter_url = next((item.url for item in self.chapters[chapter.index + 1:]
                                         if not item.is_volume and item.url), "")
            content = self._chapter_cache.pop(chapter.url, None)
            if content is None:
                content = await self.engine.content_document(
                    self.current_source, chapter, next_chapter_url)
            rendered = self._render_content(content)
            if append:
                self._content_host.controls.extend([
                    ft.Divider(height=32),
                    ft.Text(chapter.name, size=22, weight=ft.FontWeight.BOLD),
                    *rendered,
                ])
            else:
                self._title.value = content.title or chapter.name
                self._content_host.controls = rendered
                self._continuous_loaded = {chapter.url}
                self._auto_scroll_armed = True
            if not self._content_host.controls:
                self._content_host.controls = [ft.Text("正文解析结果为空")]
            self._continuous_loaded.add(chapter.url)
            self._current_chapter = chapter
            current_book = getattr(self.engine, "current_book", None)
            if current_book is not None:
                current_book.variables = dict(self.engine.js_variables)
                self.service.update_bookshelf_state(
                    self.current_source.bookSourceUrl, current_book.url,
                    variables=current_book.variables, chapter=chapter,
                    book=current_book)
            self._update_chapter_navigation()
            self._status.value = chapter.name
            self._tabs.selected_index = 2
        except Exception as exc:
            self._show_error(exc)
        finally:
            self._reading_loading = False
        self.update()
        if self._current_chapter is chapter:
            self._prefetch_task = self.page.run_task(self._prefetch_next_chapters, chapter)
        if not append:
            await self._reader.scroll_to(offset=0, duration=150)

    def _cancel_prefetch(self, clear_cache=False):
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None
        if clear_cache:
            self._chapter_cache.clear()

    def _next_readable_chapters(self, chapter, limit):
        try:
            start = self.chapters.index(chapter) + 1
        except ValueError:
            start = chapter.index + 1
        return [item for item in self.chapters[start:]
                if not item.is_volume and item.url][:limit]

    @staticmethod
    def _document_size(document):
        return sum(len(block.value) if isinstance(block.value, bytes)
                   else len(str(block.value).encode("utf-8"))
                   for block in document.blocks)

    async def _prefetch_next_chapters(self, chapter):
        """顺序预取后续章节，避免并发修改 Legado 规则变量。"""
        source = self.current_source
        candidates = self._next_readable_chapters(chapter, self.PREFETCH_CHAPTER_COUNT)
        prefetch_engine = LegadoReadingEngine(state_dir=self.service.data_dir)
        prefetch_engine.current_book = getattr(self.engine, "current_book", None)
        prefetch_engine.js_variables.update(self.engine.js_variables)
        cache_size = sum(self._document_size(item) for item in self._chapter_cache.values())
        try:
            for candidate in candidates:
                if source is not self.current_source or candidate.url in self._chapter_cache:
                    continue
                following = self._next_readable_chapters(candidate, 1)
                next_url = following[0].url if following else ""
                document = await prefetch_engine.content_document(source, candidate, next_url)
                size = self._document_size(document)
                if cache_size + size > self.PREFETCH_CACHE_LIMIT:
                    break
                self._chapter_cache[candidate.url] = document
                cache_size += size
        except asyncio.CancelledError:
            raise
        except Exception:
            # 预加载失败不能影响当前章节阅读，实际翻页时仍会正常请求。
            return
        finally:
            prefetch_engine.js.close()

    def _adjacent_chapter(self, direction):
        if not self._current_chapter or not self.chapters:
            return None
        try:
            position = self.chapters.index(self._current_chapter)
        except ValueError:
            position = self._current_chapter.index
        index = position + direction
        while 0 <= index < len(self.chapters):
            candidate = self.chapters[index]
            if not candidate.is_volume and candidate.url:
                return candidate
            index += direction
        return None

    def _update_chapter_navigation(self):
        self._prev_chapter_btn.disabled = self._adjacent_chapter(-1) is None
        self._next_chapter_btn.disabled = self._adjacent_chapter(1) is None

    def _previous_chapter(self, _):
        chapter = self._adjacent_chapter(-1)
        if chapter and not self._reading_loading:
            self.page.run_task(self._run_content, chapter, False)

    def _next_chapter(self, _):
        chapter = self._adjacent_chapter(1)
        if chapter and not self._reading_loading:
            self.page.run_task(self._run_content, chapter, False)

    def _show_directory(self, _):
        self._tabs.selected_index = 1
        self.update()

    def _on_reader_scroll(self, event):
        pixels = getattr(event, "pixels", 0) or 0
        maximum = getattr(event, "max_scroll_extent", 0) or 0
        if not maximum:
            return
        distance = maximum - pixels
        if distance > 500:
            self._auto_scroll_armed = True
            return
        if distance < 180 and self._auto_scroll_armed and not self._reading_loading:
            chapter = self._adjacent_chapter(1)
            if chapter and chapter.url not in self._continuous_loaded:
                self._auto_scroll_armed = False
                self.page.run_task(self._run_content, chapter, True)

    def _render_content(self, document):
        controls = []
        for block in document.blocks:
            if block.kind == "text":
                controls.append(ft.Text(str(block.value), selectable=True, size=16))
            elif block.kind == "image":
                controls.append(ft.Image(
                    src=block.value, fit=ft.BoxFit.CONTAIN,
                    error_content=ft.Text("图片加载失败"),
                ))
            elif block.kind == "audio":
                player = fta.Audio(src=block.value, autoplay=False)
                controls.append(ft.Card(content=ft.Container(padding=12, content=ft.Column([
                    player,
                    ft.Text(block.title or "音频"),
                    ft.Row([
                        ft.IconButton(ft.Icons.PLAY_ARROW, tooltip="播放",
                                      on_click=lambda e, p=player: self.page.run_task(p.play)),
                        ft.IconButton(ft.Icons.PAUSE, tooltip="暂停",
                                      on_click=lambda e, p=player: self.page.run_task(p.pause)),
                    ]),
                ]))))
            elif block.kind == "video":
                media = ftv.VideoMedia(resource=block.value, http_headers=block.headers or None)
                controls.append(ftv.Video(
                    playlist=[media], title=block.title or "视频", autoplay=False,
                    aspect_ratio=16 / 9, controls=ftv.AdaptiveVideoControls(),
                ))
        return controls

    def _set_busy(self, text):
        self._status.value = text
        self.update()

    def _show_error(self, exc):
        prefix = "当前书源暂不支持" if isinstance(exc, UnsupportedRule) else "操作失败"
        self._status.value = f"{prefix}：{exc}"
