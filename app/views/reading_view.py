"""书籍搜索、目录与正文阅读页面。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid

import flet as ft
import flet_audio as fta
import flet_video as ftv
import flet_webview as ftw

from app.services.rule_engine import (UnsupportedRule, parse_explore_categories,
                                      resolve_explore_categories, BookItem)
from app.services.reading_engine import LegadoReadingEngine
from app.services.reading_cache import ReadingCache
from app.services.desktop_browser import EdgeCdpSession


class ReadingView(ft.Column):
    DIRECTORY_BATCH_SIZE = 50
    PREFETCH_CHAPTER_COUNT = 10
    PREFETCH_CACHE_LIMIT = 64 * 1024 * 1024

    def __init__(self, service, discovery_only=False):
        super().__init__(spacing=0, expand=True)
        self.service = service
        self.reading_cache = ReadingCache(service.data_dir)
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
        self._webview_override_regex = ""
        self._webview_html = ""
        self._webview_initial_url = ""
        self._current_chapter = None
        self._current_book = None
        self._pending_scroll_offset = 0.0
        self._pending_scroll_ratio = 0.0
        self._scroll_save_generation = 0
        self._font_size = 16
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
        self._visible_chapters = []
        self._directory_reversed = False
        self._has_more = True
        self._book_detail_dialog = None
        self._detail_book = None
        self._book_open_generation = 0
        self._book_open_task = None
        self._section = "shelf"
        self._detail_resume = None
        self._bookshelf_update_task = None
        self._source_switch_task = None
        self._source_switch_dialog = None
        self._source_switch_results = None
        self._source_switch_status = None
        self._source_switch_query = None
        self._bookshelf_layout = "list"
        self._bookshelf_query = ft.TextField(
            hint_text="搜索书名或作者", prefix_icon=ft.Icons.SEARCH,
            dense=True, height=42, expand=True,
            on_change=self._filter_bookshelf)
        self._bookshelf_sort = ft.Dropdown(
            value="read", width=150, dense=True,
            options=[
                ft.dropdown.Option("read", "最近阅读"),
                ft.dropdown.Option("update", "最近更新"),
                ft.dropdown.Option("name", "书名"),
                ft.dropdown.Option("combined", "综合排序"),
                ft.dropdown.Option("manual", "手动排序"),
            ], on_select=self._filter_bookshelf)
        self._bookshelf_group = ft.Dropdown(
            value="__all__", width=130, dense=True,
            options=[ft.dropdown.Option("__all__", "全部")],
            on_select=self._filter_bookshelf)
        self._bookshelf_layout_btn = ft.IconButton(
            ft.Icons.GRID_VIEW, tooltip="切换列表/网格",
            on_click=self._toggle_bookshelf_layout)
        self._bookshelf_refresh_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="刷新书架目录",
            on_click=self._refresh_all_books)
        self._bookshelf_query.col = {"xs": 12, "sm": 5}
        self._bookshelf_group.col = {"xs": 4, "sm": 2}
        self._bookshelf_sort.col = {"xs": 4, "sm": 3}
        self._bookshelf_layout_btn.col = {"xs": 2, "sm": 1}
        self._bookshelf_refresh_btn.col = {"xs": 2, "sm": 1}
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
        self._bookshelf_grid = ft.GridView(
            expand=True, max_extent=190, child_aspect_ratio=0.72,
            spacing=8, run_spacing=8)
        self._bookshelf_host = ft.Column(
            expand=True, spacing=0, controls=[
                ft.Container(padding=ft.Padding(8, 4, 8, 4), content=ft.ResponsiveRow([
                    self._bookshelf_query, self._bookshelf_group,
                    self._bookshelf_sort,
                    self._bookshelf_layout_btn, self._bookshelf_refresh_btn,
                ], columns=12, spacing=6, run_spacing=4)),
                ft.Divider(height=1), self._bookshelf,
            ])
        self._chapters = ft.ListView(
            expand=True, spacing=2, on_scroll=self._on_chapter_scroll,
            scroll_interval=200,
        )
        self._directory_status = ft.Text("尚未打开书籍", size=12, expand=True)
        self._directory_query = ft.TextField(
            hint_text="搜索章节", prefix_icon=ft.Icons.SEARCH,
            dense=True, height=42, expand=True,
            on_change=self._filter_directory)
        self._directory_reverse_btn = ft.IconButton(
            ft.Icons.SWAP_VERT, tooltip="倒序/正序",
            on_click=self._toggle_directory_order)
        self._directory_toolbar = ft.Container(
            padding=ft.Padding(8, 2, 8, 2),
            content=ft.Column([
                ft.Row([
                    self._directory_status,
                    ft.IconButton(ft.Icons.REFRESH, tooltip="刷新目录",
                                  on_click=self._refresh_directory),
                ]),
                ft.Row([self._directory_query, self._directory_reverse_btn]),
            ], spacing=2),
        )
        self._title = ft.Text("正文", size=22, weight=ft.FontWeight.BOLD)
        self._content_host = ft.Column(spacing=12)
        self._reader_progress = ft.Text(
            "未选择章节", size=12, expand=True, text_align=ft.TextAlign.CENTER)
        self._top_prev_btn = ft.IconButton(
            ft.Icons.CHEVRON_LEFT, tooltip="上一章", disabled=True,
            on_click=self._previous_chapter)
        self._top_next_btn = ft.IconButton(
            ft.Icons.CHEVRON_RIGHT, tooltip="下一章", disabled=True,
            on_click=self._next_chapter)
        self._font_size_label = ft.Text(str(self._font_size), size=12)
        self._reader_toolbar = ft.Container(
            padding=ft.Padding(6, 2, 6, 2),
            content=ft.Row([
                self._top_prev_btn,
                ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书籍详情与目录",
                              on_click=self._show_directory),
                self._reader_progress,
                ft.IconButton(ft.Icons.TEXT_DECREASE, tooltip="减小字号",
                              on_click=lambda e: self._change_font_size(-1)),
                ft.Row([ft.Text("字号", size=11), self._font_size_label],
                       spacing=3, tight=True),
                ft.IconButton(ft.Icons.TEXT_INCREASE, tooltip="增大字号",
                              on_click=lambda e: self._change_font_size(1)),
                self._top_next_btn,
            ], spacing=0),
        )
        self._reader = ft.ListView(
            expand=True, scroll_interval=200, on_scroll=self._on_reader_scroll,
            controls=[self._title, ft.Divider(), self._content_host],
        )
        self._detail_title = ft.Text("书籍详情", size=22, weight=ft.FontWeight.BOLD,
                                     expand=True)
        self._detail_author = ft.Text("", color=ft.Colors.GREY_700)
        self._detail_intro = ft.Text("", max_lines=4,
                                     overflow=ft.TextOverflow.ELLIPSIS)
        self._detail_cover = ft.Container(
            width=76, height=104, alignment=ft.Alignment.CENTER,
            content=ft.Icon(ft.Icons.BOOK, size=42))
        self._detail_continue_btn = ft.Button(
            "继续阅读", icon=ft.Icons.PLAY_ARROW, disabled=True,
            on_click=self._continue_from_detail)
        self._shelf_page = ft.Column(
            expand=True, spacing=0, visible=True,
            controls=[self._bookshelf_host])
        self._detail_page = ft.Column(
            expand=True, spacing=0, visible=False, controls=[
                ft.Container(padding=ft.Padding(4, 2, 8, 2), content=ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书架",
                                  on_click=self._back_to_shelf),
                    self._detail_title,
                    self._detail_continue_btn,
                ])),
                ft.Container(padding=ft.Padding(12, 4, 12, 10), content=ft.Row([
                    self._detail_cover,
                    ft.Column([self._detail_author, self._detail_intro],
                              expand=True, spacing=6),
                ], vertical_alignment=ft.CrossAxisAlignment.START)),
                ft.Divider(height=1), self._directory_toolbar,
                ft.Divider(height=1), self._chapters,
            ])
        self._reader_page = ft.Column(
            expand=True, spacing=0, visible=False, controls=[
                self._reader_toolbar, ft.Divider(height=1), self._reader,
            ])
        self._section_host = ft.Stack(
            expand=True,
            controls=[self._shelf_page, self._detail_page, self._reader_page])
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
            self._page_title = ft.Text(
                "我的书架", size=28, weight=ft.FontWeight.BOLD)
            self.controls = [
                ft.Container(padding=12, content=ft.Column([
                    self._page_title,
                    self._status,
                ])),
                self._section_host,
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
        if self._book_open_task and not self._book_open_task.done():
            self._book_open_task.cancel()
        self._persist_scroll_position()

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
        self._webview_script = str(options.get("webJs") or
                                   options.get("javaScript") or "")
        self._webview_delay = int(options.get("webViewDelayTime") or 0)
        self._webview_source_regex = str(options.get("sourceRegex") or "")
        self._webview_override_regex = str(options.get("overrideUrlRegex") or "")
        self._webview_html = str(options.get("html") or "")
        self._webview_initial_url = str(url)
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
            self._webview_override_regex = ""
            self._webview_html = ""
            self._webview_initial_url = ""

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
                self._webview_script, self._webview_delay,
                self._webview_html, self._webview_source_regex,
                self._webview_override_regex)
            html = response.get("body", "")
            waiting_for_source = bool(self._webview_source_regex and
                                      not response.get("source"))
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
            if self._webview_html:
                html_value, self._webview_html = self._webview_html, ""
                await self._webview.run_javascript(
                    "document.open();document.write(" +
                    json.dumps(html_value, ensure_ascii=False) +
                    ");document.close()")
            if self._webview_script:
                script, self._webview_script = self._webview_script, ""
                await self._webview.run_javascript(script)
                if self._webview_delay:
                    await asyncio.sleep(max(0, self._webview_delay) / 1000)
            # The extension does not return JS values, so use its console event
            # as the same kind of bridge that Legado exposes to WebView scripts.
            script = ("console.log('" + self._webview_marker + "' + btoa(unescape("
                      "encodeURIComponent(JSON.stringify({html:document.documentElement.outerHTML,"
                      "url:location.href,resources:performance.getEntriesByType('resource')"
                      ".map(e=>e.name)})))))")
            await self._webview.run_javascript(script)
        finally:
            self._webview_capture_pending = False

    def _select_webview_payload(self, payload):
        from app.services.java_regex import compile_pattern
        html = str(payload.get("html") or "")
        final_url = str(payload.get("url") or self._webview_initial_url)
        result = {"body": html, "headers": {"content-type": "text/html"},
                  "status": 200, "url": final_url}
        if self._webview_source_regex:
            pattern = compile_pattern(self._webview_source_regex)
            matched = next((str(item) for item in payload.get("resources") or []
                            if pattern.fullmatch(str(item))), "")
            if not matched:
                return None
            result.update(body=matched, source=matched,
                          url=self._webview_initial_url or final_url)
        if self._webview_override_regex:
            if not compile_pattern(self._webview_override_regex).fullmatch(final_url):
                return None
            result.update(body=final_url, overrideUrl=final_url,
                          url=self._webview_initial_url or final_url)
        return result

    async def _webview_console_message(self, event):
        message = getattr(event, "message", "") or ""
        if not self._webview_future or self._webview_future.done() or not message.startswith(
                self._webview_marker):
            return
        try:
            payload = json.loads(base64.b64decode(
                message[len(self._webview_marker):]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
        response = self._select_webview_payload(payload)
        html = str(payload.get("html") or "")
        waiting_for_source = response is None
        if ("Just a moment" in html or "challenge-platform" in html or
                waiting_for_source):
            self._status.value = "网页验证尚未完成，请完成验证后点击“读取当前页面”"
            self.update()
            return
        self._webview_future.set_result(response)
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
            self._show_section("shelf")
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
            self._show_section("shelf")
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
        query = str(self._bookshelf_query.value or "").strip().lower()
        groups = sorted({str(item.get("groupName") or "").strip()
                         for item in self.service.bookshelf
                         if str(item.get("groupName") or "").strip()})
        group_value = self._bookshelf_group.value or "__all__"
        self._bookshelf_group.options = [
            ft.dropdown.Option("__all__", "全部"),
            ft.dropdown.Option("__unread__", "未读"),
            ft.dropdown.Option("__error__", "更新失败"),
            ft.dropdown.Option("__none__", "未分组"),
            *[ft.dropdown.Option(group, group) for group in groups],
        ]
        items = [item for item in self.service.bookshelf if not query or query in
                 f"{item.get('name', '')} {item.get('author', '')}".lower()]
        if group_value == "__unread__":
            items = [item for item in items if
                     int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1 > 0]
        elif group_value == "__error__":
            items = [item for item in items if item.get("updateError")]
        elif group_value == "__none__":
            items = [item for item in items if not item.get("groupName")]
        elif group_value != "__all__":
            items = [item for item in items
                     if item.get("groupName") == group_value]
        sort_mode = self._bookshelf_sort.value or "read"
        if sort_mode == "name":
            items.sort(key=lambda item: str(item.get("name") or "").casefold())
        elif sort_mode == "update":
            items.sort(key=lambda item: int(item.get("latestChapterTime", 0) or 0),
                       reverse=True)
        elif sort_mode == "manual":
            items.sort(key=lambda item: int(item.get("order", 0) or 0))
        elif sort_mode == "combined":
            items.sort(key=lambda item: max(
                int(item.get("durChapterTime", 0) or 0),
                int(item.get("latestChapterTime", 0) or 0)), reverse=True)
        else:
            items.sort(key=lambda item: int((self.service.reading_state(item) or {}).get(
                "last_read_at") or 0), reverse=True)
        self._bookshelf.controls = [self._shelf_control(item) for item in items]
        self._bookshelf_grid.controls = [self._shelf_grid_control(item) for item in items]
        if not self.service.bookshelf:
            empty = ft.Container(
                padding=32,
                content=ft.Column([
                    ft.Icon(ft.Icons.MENU_BOOK, size=52, color=ft.Colors.GREY_400),
                    ft.Text("书架还是空的", size=18),
                    ft.Text("请到“书源 → 找书”中选择书籍并加入书架",
                            color=ft.Colors.GREY_600),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            )
            self._bookshelf.controls = [empty]
            self._bookshelf_grid.controls = [empty]
        self._status.value = (f"书架中有 {len(self.service.bookshelf)} 本书"
                              + (f"，当前显示 {len(items)} 本" if query else ""))

    def _filter_bookshelf(self, _):
        self._render_bookshelf()
        self.update()

    def _toggle_bookshelf_layout(self, _):
        self._bookshelf_layout = (
            "grid" if self._bookshelf_layout == "list" else "list")
        content = (self._bookshelf_grid
                   if self._bookshelf_layout == "grid" else self._bookshelf)
        self._bookshelf_host.controls[-1] = content
        self._bookshelf_layout_btn.icon = (
            ft.Icons.VIEW_LIST if self._bookshelf_layout == "grid"
            else ft.Icons.GRID_VIEW)
        self.update()

    def _shelf_control(self, item):
        reading = self.service.reading_state(item) or {}
        details = list(filter(None, [item.get("author"), item.get("source_name")]))
        if reading.get("chapter_name"):
            details.append(f"上次读到：{reading['chapter_name']}")
        if reading.get("scroll_ratio"):
            details.append(f"本章 {float(reading['scroll_ratio']):.0%}")
        unread = max(0, int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1)
        if unread:
            details.append(f"未读 {unread} 章")
        if item.get("updateError"):
            details.append("更新失败")
        return ft.ListTile(
            key=self._shelf_control_key(item, "list"),
            leading=(ft.Image(src=item.get("cover_url"), width=42, height=56,
                              fit=ft.BoxFit.COVER)
                     if item.get("cover_url") else ft.Icon(ft.Icons.BOOK)),
            title=ft.Text(item.get("name") or "未命名"),
            subtitle=ft.Text(" · ".join(details)),
            trailing=ft.Row([
                ft.Container(
                    visible=unread > 0, padding=ft.Padding(7, 2, 7, 2),
                    border_radius=12, bgcolor=ft.Colors.BLUE_100,
                    content=ft.Text(str(unread), size=11)),
                ft.IconButton(
                    ft.Icons.MORE_VERT, tooltip="书籍管理",
                    on_click=lambda e, selected=item: self._show_shelf_manage(selected)),
            ], tight=True),
            on_click=lambda e, selected=item: self._open_shelf_item(selected),
        )

    def _shelf_grid_control(self, item):
        reading = self.service.reading_state(item) or {}
        unread = max(0, int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1)
        cover = (ft.Image(src=item.get("cover_url"), height=150,
                          width=float("inf"), fit=ft.BoxFit.COVER)
                 if item.get("cover_url") else
                 ft.Container(height=150, alignment=ft.Alignment.CENTER,
                              content=ft.Icon(ft.Icons.BOOK, size=54)))
        return ft.Card(key=self._shelf_control_key(item, "grid"), content=ft.Container(
            padding=8, on_click=lambda e, selected=item: self._open_shelf_item(selected),
            content=ft.Column([
                ft.Stack([cover, ft.Container(
                    visible=unread > 0, right=4, top=4, padding=5,
                    border_radius=12, bgcolor=ft.Colors.BLUE_100,
                    content=ft.Text(str(unread), size=11))]),
                ft.Text(item.get("name") or "未命名", max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        weight=ft.FontWeight.BOLD),
                ft.Text(reading.get("chapter_name") or item.get("author") or "",
                        size=11, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row([ft.Text(item.get("source_name") or "", size=10, expand=True,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.IconButton(ft.Icons.MORE_VERT, icon_size=18,
                                      on_click=lambda e, selected=item:
                                      self._show_shelf_manage(selected))], spacing=0),
            ], spacing=4)))

    @staticmethod
    def _shelf_control_key(item, layout):
        """Return a stable Flet reconciliation key for one persisted book."""
        identity = "\n".join((
            str(item.get("source_url") or item.get("origin") or ""),
            str(item.get("url") or item.get("bookUrl") or ""),
        ))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"bookshelf-{layout}-{digest}"

    def _remove_shelf_item(self, item):
        self.service.remove_from_bookshelf(item.get("source_url", ""), item.get("url", ""))
        self.reading_cache.remove_book(
            item.get("source_url", ""), item.get("url", ""))
        self._render_bookshelf()
        self.update()

    def _confirm_remove_shelf_item(self, item):
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("移出书架"),
            content=ft.Text(
                f"确定将《{item.get('name') or '未命名'}》移出书架吗？\n"
                "该书的阅读缓存也会一并清除。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.Button(
                    "移出", color=ft.Colors.RED,
                    on_click=lambda e, selected=item:
                    self._remove_shelf_item_confirmed(selected)),
            ],
        )
        self.page.show_dialog(dialog)

    def _remove_shelf_item_confirmed(self, item):
        self.page.pop_dialog()
        self._remove_shelf_item(item)

    def _show_shelf_manage(self, item):
        reading = self.service.reading_state(item) or {}
        unread = max(0, int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1)
        group_input = ft.TextField(
            label="分组", value=str(item.get("groupName") or ""),
            hint_text="留空表示未分组")
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text(item.get("name") or "书籍管理"),
            content=ft.Container(width=400, content=ft.Column([
                ft.Text(f"作者：{item.get('author') or '未知'}"),
                ft.Text(f"书源：{item.get('source_name') or '未知'}"),
                ft.Text(f"阅读进度：{reading.get('chapter_name') or '尚未阅读'}"),
                ft.Text(f"目录：{item.get('totalChapterNum', 0)} 章 · 未读 {unread} 章"),
                group_input,
                ft.Text(item.get("updateError") or "", color=ft.Colors.RED_600,
                        visible=bool(item.get("updateError"))),
            ], tight=True, spacing=8)),
            actions=[
                ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog()),
                ft.TextButton("切换书源", icon=ft.Icons.SWAP_HORIZ,
                              on_click=lambda e, selected=item:
                              self._show_source_switch(selected)),
                ft.TextButton("清除缓存", on_click=lambda e, selected=item:
                              self._clear_shelf_cache(selected)),
                ft.TextButton("保存分组", on_click=lambda e, selected=item,
                              field=group_input: self._save_shelf_group(selected, field)),
                ft.TextButton("刷新目录", on_click=lambda e, selected=item:
                              self._refresh_one_from_dialog(selected)),
                ft.Button("移出书架", color=ft.Colors.RED,
                          on_click=lambda e, selected=item:
                          self._remove_from_manage(selected)),
            ])
        self.page.show_dialog(dialog)

    def _show_source_switch(self, item):
        self.page.pop_dialog()
        self._source_switch_query = ft.TextField(
            label="搜索书名", value=item.get("name") or "",
            autofocus=True, on_submit=lambda e: self._start_source_switch(item))
        self._source_switch_status = ft.Text(
            "将在其他已启用书源中搜索同名书籍。",
            color=ft.Colors.GREY_600)
        self._source_switch_results = ft.ListView(expand=True, spacing=4)
        self._source_switch_dialog = ft.AlertDialog(
            modal=True, title=ft.Text("切换书源"),
            content=ft.Container(width=620, height=520, content=ft.Column([
                self._source_switch_query,
                self._source_switch_status,
                ft.Divider(height=1),
                self._source_switch_results,
            ], spacing=8)),
            actions=[
                ft.TextButton("关闭", on_click=self._close_source_switch),
                ft.Button("搜索其他书源", icon=ft.Icons.SEARCH,
                          on_click=lambda e: self._start_source_switch(item)),
            ])
        self.page.show_dialog(self._source_switch_dialog)

    def _close_source_switch(self, _=None):
        if self._source_switch_task and not self._source_switch_task.done():
            self._source_switch_task.cancel()
        self._source_switch_task = None
        self.page.pop_dialog()

    def _start_source_switch(self, item):
        if self._source_switch_task and not self._source_switch_task.done():
            return
        keyword = str(self._source_switch_query.value or "").strip()
        if not keyword:
            self._source_switch_status.value = "请输入要搜索的书名"
            self.update()
            return
        self._source_switch_task = self.page.run_task(
            self._run_source_switch, item, keyword)

    async def _run_source_switch(self, item, keyword):
        sources = [source for source in self.service.sources
                   if source.enabled and source.searchUrl and
                   source.bookSourceUrl != item.get("source_url")]
        self._source_switch_results.controls.clear()
        self._source_switch_status.value = f"正在搜索 0/{len(sources)} 个书源…"
        self.update()
        queue = asyncio.Queue()
        for source in sources:
            queue.put_nowait(source)
        matches = []
        completed = 0

        async def worker():
            nonlocal completed
            while True:
                try:
                    source = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                engine = LegadoReadingEngine(state_dir=self.service.data_dir)
                try:
                    books = await engine.search(source, keyword, 1)
                    for book in books:
                        if keyword.casefold() in str(book.name or "").casefold():
                            matches.append((source, book))
                except Exception:
                    pass
                finally:
                    engine.js.close()
                    queue.task_done()
                    completed += 1
                    if completed % 5 == 0 or completed == len(sources):
                        self._render_source_switch_results(item, matches)
                        self._source_switch_status.value = (
                            f"正在搜索 {completed}/{len(sources)} 个书源，"
                            f"找到 {len(matches)} 个结果…")
                        self.update()

        try:
            workers = [asyncio.create_task(worker())
                       for _ in range(min(6, len(sources)))]
            if workers:
                await asyncio.gather(*workers)
            self._render_source_switch_results(item, matches)
            self._source_switch_status.value = (
                f"搜索完成：检查 {len(sources)} 个书源，找到 {len(matches)} 个结果")
        except asyncio.CancelledError:
            self._source_switch_status.value = "搜索已取消"
            raise
        finally:
            self._source_switch_task = None
            self.update()

    def _render_source_switch_results(self, item, matches):
        def rank(pair):
            source, book = pair
            exact_name = str(book.name or "").casefold() == str(item.get("name") or "").casefold()
            same_author = bool(item.get("author")) and (
                str(book.author or "").casefold() == str(item.get("author") or "").casefold())
            return (not exact_name, not same_author, source.bookSourceName)

        ordered = sorted(matches, key=rank)
        self._source_switch_results.controls = [ft.ListTile(
            leading=ft.Icon(ft.Icons.LANGUAGE),
            title=ft.Text(book.name or "未命名"),
            subtitle=ft.Text(" · ".join(filter(None, [
                source.bookSourceName, book.author, book.last_chapter]))),
            trailing=ft.Button(
                "切换", on_click=lambda e, selected_source=source,
                selected_book=book: self._confirm_source_switch(
                    item, selected_source, selected_book)),
        ) for source, book in ordered[:100]]

    def _confirm_source_switch(self, item, source, book):
        if self._source_switch_task and not self._source_switch_task.done():
            self._source_switch_task.cancel()
        self.page.pop_dialog()
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text("确认切换书源"),
            content=ft.Text(
                f"将《{item.get('name') or '未命名'}》从“{item.get('source_name') or '未知'}”"
                f"切换到“{source.bookSourceName}”。\n"
                "阅读章节将按原章节序号在新目录中续接，旧书源缓存会被清除。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.Button("确认切换", on_click=lambda e:
                          self._apply_source_switch(item, source, book)),
            ])
        self.page.show_dialog(dialog)

    def _apply_source_switch(self, item, source, book):
        old_source = item.get("source_url", "")
        old_url = item.get("url", "")
        try:
            changed = self.service.switch_bookshelf_source(item, source, book)
            if not changed:
                self._status.value = "切换失败：该书源版本已在书架中"
            else:
                self.reading_cache.remove_book(old_source, old_url)
                self._status.value = f"已切换到书源：{source.bookSourceName}"
        except Exception as exc:
            self._status.value = f"切换书源失败：{exc}"
        self.page.pop_dialog()
        self._render_bookshelf()
        self.update()

    def _save_shelf_group(self, item, field):
        self.service.update_bookshelf_options(
            item.get("source_url", ""), item.get("url", ""),
            groupName=str(field.value or "").strip())
        self.page.pop_dialog()
        self._render_bookshelf()
        self.update()

    def _clear_shelf_cache(self, item):
        self.reading_cache.remove_book(
            item.get("source_url", ""), item.get("url", ""))
        self.page.pop_dialog()
        self._status.value = f"《{item.get('name') or ''}》缓存已清除"
        self.update()

    def _remove_from_manage(self, item):
        self.page.pop_dialog()
        self._confirm_remove_shelf_item(item)

    def _refresh_one_from_dialog(self, item):
        self.page.pop_dialog()
        self.page.run_task(self._refresh_shelf_item, item, True)

    def _refresh_all_books(self, _):
        if self._bookshelf_update_task and not self._bookshelf_update_task.done():
            return
        self._bookshelf_update_task = self.page.run_task(
            self._run_refresh_bookshelf)

    async def _run_refresh_bookshelf(self):
        candidates = [item for item in list(self.service.bookshelf)
                      if item.get("canUpdate", True)]
        self._bookshelf_refresh_btn.disabled = True
        try:
            for index, item in enumerate(candidates, 1):
                self._status.value = (
                    f"正在刷新书架 {index}/{len(candidates)}："
                    f"{item.get('name') or '未命名'}")
                self.update()
                await self._refresh_shelf_item(item, False)
        finally:
            self._bookshelf_refresh_btn.disabled = False
            self._render_bookshelf()
            self.update()

    async def _refresh_shelf_item(self, item, show_result=False):
        source = next((source for source in self.service.sources
                       if source.bookSourceUrl == item.get("source_url")), None)
        if source is None:
            self.service.update_bookshelf_check(
                item.get("source_url", ""), item.get("url", ""),
                error="原书源已删除")
            return
        book = self._book_from_shelf(item)
        update_engine = LegadoReadingEngine(state_dir=self.service.data_dir)
        try:
            chapters = await update_engine.chapters(source, book)
            latest = next((chapter.name for chapter in reversed(chapters)
                           if not chapter.is_volume), "")
            await asyncio.to_thread(
                self.reading_cache.save_catalog,
                source.bookSourceUrl, book.url, chapters)
            self.service.update_bookshelf_state(
                source.bookSourceUrl, book.url,
                variables=getattr(book, "variables", {}) or update_engine.js_variables,
                book=book)
            self.service.update_bookshelf_check(
                source.bookSourceUrl, book.url,
                latest_title=latest, total_chapters=len(chapters))
            if show_result:
                self._status.value = f"《{book.name}》目录已更新，共 {len(chapters)} 章"
        except Exception as exc:
            self.service.update_bookshelf_check(
                source.bookSourceUrl, book.url, error=str(exc))
            if show_result:
                self._show_error(exc)
        finally:
            update_engine.js.close()
            if show_result:
                self._render_bookshelf()
                self.update()

    @staticmethod
    def _book_from_shelf(item):
        return BookItem(
            name=item.get("name", ""), author=item.get("author", ""),
            intro=item.get("intro", ""), url=item.get("url", ""),
            cover_url=item.get("cover_url", ""), toc_url=item.get("toc_url", ""),
            kind=item.get("kind", ""), last_chapter=item.get("last_chapter", ""),
            word_count=item.get("word_count", ""),
            download_urls=item.get("download_urls", ""),
            variables=item.get("variables") or {})

    def _open_shelf_item(self, item):
        # Persist the old book before replacing any of its context. Previously
        # the second book became current first, so the first book's chapter was
        # accidentally saved into the second one's progress record.
        self._persist_scroll_position()
        source = next((source for source in self.service.sources
                       if source.bookSourceUrl == item.get("source_url")), None)
        if source is None:
            self._status.value = "原书源已被删除，无法加载这本书"
            self.update()
            return
        self._reset_book_session(book_name=item.get("name") or "")
        self.current_source = source
        book = self._book_from_shelf(item)
        self._book_open_generation += 1
        generation = self._book_open_generation
        if self._book_open_task and not self._book_open_task.done():
            self._book_open_task.cancel()
        reading = self.service.reading_state(item)
        self._detail_resume = reading
        self._show_shelf_book_detail(book)
        self._book_open_task = self.page.run_task(
            self._run_chapters, book, reading,
            False, generation)

    def _reset_book_session(self, book_name=""):
        self._cancel_prefetch(clear_cache=True)
        self._reading_loading = False
        self.engine.current_book = None
        self.engine.js_variables.clear()
        self._current_book = None
        self._current_chapter = None
        self.chapters = []
        self._visible_chapters = []
        self._continuous_loaded.clear()
        self._pending_scroll_offset = 0.0
        self._pending_scroll_ratio = 0.0
        self._chapters.controls.clear()
        # Internal state and visible state must change together. A book without
        # cache may wait on the network, but it must never expose the previous
        # book's directory, content or navigation while waiting.
        self._directory_query.value = ""
        self._directory_status.value = (
            f"《{book_name}》目录尚未加载" if book_name else "尚未打开书籍")
        self._title.value = book_name or "正文"
        self._content_host.controls = [ft.Container(
            padding=24, alignment=ft.Alignment.CENTER,
            content=ft.Text(
                f"《{book_name}》正文尚未加载" if book_name else "尚未打开正文",
                color=ft.Colors.GREY_600))]
        self._reader_progress.value = "未加载章节"
        for button in (self._top_prev_btn, self._top_next_btn):
            button.disabled = True

    def _show_shelf_book_detail(self, book):
        """Enter the combined book-info/catalog page before loading its TOC."""
        self._detail_book = book
        self._detail_title.value = book.name or "书籍详情"
        self._detail_author.value = " · ".join(
            value for value in [book.author, getattr(self.current_source,
                                                     "bookSourceName", "")]
            if value) or "作者未知"
        self._detail_intro.value = book.intro or "暂无简介"
        self._detail_cover.content = (
            ft.Image(src=book.cover_url, width=76, height=104,
                     fit=ft.BoxFit.COVER,
                     error_content=ft.Icon(ft.Icons.BROKEN_IMAGE))
            if book.cover_url else ft.Icon(ft.Icons.BOOK, size=42))
        self._detail_continue_btn.disabled = True
        self._detail_continue_btn.text = (
            "继续阅读" if self._detail_resume else "开始阅读")
        self._directory_status.value = f"《{book.name}》· 正在加载目录…"
        self._show_section("detail")
        self._status.value = f"正在打开《{book.name}》"
        self.update()

    def _cancel_shelf_open(self, _):
        self._book_open_generation += 1
        if self._book_open_task and not self._book_open_task.done():
            self._book_open_task.cancel()
        self._back_to_shelf(None)

    def _show_section(self, section):
        self._section = section
        self._shelf_page.visible = section == "shelf"
        self._detail_page.visible = section == "detail"
        self._reader_page.visible = section == "reader"
        if hasattr(self, "_page_title"):
            self._page_title.value = {
                "shelf": "我的书架",
                "detail": "书籍详情",
                "reader": "阅读",
            }.get(section, "我的书架")

    def _back_to_shelf(self, _):
        self._persist_scroll_position()
        self._book_open_generation += 1
        if self._book_open_task and not self._book_open_task.done():
            self._book_open_task.cancel()
        self._show_section("shelf")
        self._render_bookshelf()
        self.update()

    def _continue_from_detail(self, _):
        chapter = self._resume_chapter(self._detail_resume)
        if chapter is None:
            chapter = next((item for item in self.chapters
                            if not item.is_volume and item.url), None)
        if chapter is not None and not self._reading_loading:
            offset = (float((self._detail_resume or {}).get("scroll_offset") or 0)
                      if self._resume_chapter(self._detail_resume) is chapter else 0)
            self.page.run_task(self._run_content, chapter, False, offset)

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

    def _refresh_directory(self, _):
        if self._current_book is not None:
            self.page.run_task(
                self._run_chapters, self._current_book, None, True)

    async def _run_chapters(self, book, resume=None, force_refresh=False,
                            open_generation=None):
        if (open_generation is not None and
                open_generation != self._book_open_generation):
            return
        self._current_book = book
        if resume and resume.get("font_size") is not None:
            self._font_size = max(13, min(28, int(resume["font_size"])))
            self._font_size_label.value = str(self._font_size)
        if not force_refresh:
            cached = await asyncio.to_thread(
                self.reading_cache.load_catalog,
                self.current_source.bookSourceUrl, book.url)
            if not self._is_current_book_open(book, open_generation):
                return
            if cached:
                self.engine.current_book = book
                self.engine.js_variables.update(getattr(book, "variables", {}) or {})
                self._apply_chapters(book, cached, "已从本地缓存加载")
                resume_chapter = self._resume_chapter(resume)
                if resume_chapter is not None:
                    await self._run_content(
                        resume_chapter, False,
                        float((resume or {}).get("scroll_offset") or 0),
                        open_generation)
                else:
                    self.update()
                return
        self._set_busy(f"正在加载《{book.name}》目录…")
        try:
            self._cancel_prefetch(clear_cache=True)
            chapters = await self.engine.chapters(self.current_source, book)
            if not self._is_current_book_open(book, open_generation):
                return
            await asyncio.to_thread(
                self.reading_cache.save_catalog,
                self.current_source.bookSourceUrl, book.url, chapters)
            self.service.update_bookshelf_state(
                self.current_source.bookSourceUrl, book.url,
                variables=getattr(book, "variables", {}) or self.engine.js_variables,
                book=book)
            self._apply_chapters(book, chapters, "目录已刷新")
            resume_chapter = self._resume_chapter(resume)
            if resume_chapter is not None:
                await self._run_content(
                    resume_chapter, False,
                    float((resume or {}).get("scroll_offset") or 0),
                    open_generation)
        except Exception as exc:
            if self._is_current_book_open(book, open_generation):
                self._show_error(exc)
                self._directory_status.value = (
                    f"《{book.name}》目录加载失败，可点击刷新重试")
        self.update()

    def _is_current_book_open(self, book, open_generation):
        if open_generation is not None and open_generation != self._book_open_generation:
            return False
        return self._current_book is book

    def _apply_chapters(self, book, chapters, state_text):
        self.chapters = chapters
        self._current_chapter = None
        self._continuous_loaded.clear()
        self._chapters.controls.clear()
        self._chapter_visible_count = 0
        self._apply_directory_filter()
        self._directory_status.value = (
            f"{book.name} · {len(chapters)} 章 · {state_text}")
        self._detail_title.value = book.name or "书籍详情"
        self._detail_author.value = " · ".join(
            value for value in [book.author, getattr(self.current_source,
                                                     "bookSourceName", "")]
            if value) or "作者未知"
        self._detail_intro.value = book.intro or "暂无简介"
        if book.cover_url:
            self._detail_cover.content = ft.Image(
                src=book.cover_url, width=76, height=104,
                fit=ft.BoxFit.COVER,
                error_content=ft.Icon(ft.Icons.BROKEN_IMAGE))
        self._status.value = (f"《{book.name}》共 {len(chapters)} 章，"
                              f"已加载 {self._chapter_visible_count} 章")
        self._show_section("detail")
        self._detail_continue_btn.disabled = not any(
            not item.is_volume and item.url for item in chapters)
        if (self.current_source is not None and
                hasattr(self.service, "update_bookshelf_state")):
            self.service.update_bookshelf_state(
                self.current_source.bookSourceUrl, book.url,
                total_chapters=len(chapters))

    def _filter_directory(self, _):
        self._apply_directory_filter()
        self.update()

    def _toggle_directory_order(self, _):
        self._directory_reversed = not self._directory_reversed
        self._apply_directory_filter()
        self.update()

    def _apply_directory_filter(self):
        keyword = str(self._directory_query.value or "").strip().lower()
        visible = [chapter for chapter in self.chapters
                   if not keyword or keyword in chapter.name.lower()]
        self._visible_chapters = list(reversed(visible)) if self._directory_reversed else visible
        self._chapters.controls.clear()
        self._chapter_visible_count = 0
        self._append_chapter_controls()
        suffix = f" · 筛选 {len(visible)} 章" if keyword else ""
        direction = " · 倒序" if self._directory_reversed else ""
        if self._current_book is not None:
            self._directory_status.value = (
                f"{self._current_book.name} · {len(self.chapters)} 章{suffix}{direction}")

    def _resume_chapter(self, reading):
        """Resolve a saved position after refreshing the book directory."""
        if not reading or not self.chapters:
            return None
        chapter_url = str(reading.get("chapter_url") or "")
        if chapter_url:
            chapter = next((item for item in self.chapters
                            if item.url == chapter_url and not item.is_volume), None)
            if chapter is not None:
                return chapter
        try:
            index = int(reading.get("chapter_index", -1))
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(self.chapters):
            chapter = self.chapters[index]
            if not chapter.is_volume and chapter.url:
                return chapter
        return None

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
            old_count + self.DIRECTORY_BATCH_SIZE, len(self._visible_chapters))
        self._chapters.controls.extend(
            self._chapter_control(self._visible_chapters[i])
            for i in range(old_count, self._chapter_visible_count)
        )

    def _on_chapter_scroll(self, event):
        pixels = getattr(event, "pixels", 0) or 0
        maximum = getattr(event, "max_scroll_extent", 0) or 0
        if (self._chapter_visible_count >= len(self._visible_chapters) or not maximum or
                maximum - pixels >= 320):
            return
        self._append_chapter_controls()
        self._status.value = (f"目录共 {len(self.chapters)} 章，"
                              f"已加载 {self._chapter_visible_count} 章")
        self.update()

    def _load_content(self, chapter):
        self.page.run_task(self._run_content, chapter, False)

    async def _run_content(self, chapter, append=False, restore_offset=0.0,
                           open_generation=None):
        if (open_generation is not None and
                open_generation != self._book_open_generation):
            return
        if self._reading_loading or chapter.is_volume:
            return
        if self._current_chapter is not None and self._current_chapter is not chapter:
            self._persist_scroll_position()
            self._pending_scroll_offset = 0.0
            self._pending_scroll_ratio = 0.0
        self._cancel_prefetch()
        self._reading_loading = True
        self._set_busy(f"正在加载 {chapter.name}…")
        try:
            next_chapter_url = ""
            if 0 <= chapter.index < len(self.chapters) - 1:
                next_chapter_url = next((item.url for item in self.chapters[chapter.index + 1:]
                                         if not item.is_volume and item.url), "")
            content = self._chapter_cache.pop(chapter.url, None)
            content_from_cache = content is not None
            if content is None and self._current_book is not None:
                content = await asyncio.to_thread(
                    self.reading_cache.load_content,
                    self.current_source.bookSourceUrl,
                    self._current_book.url, chapter.url)
                content_from_cache = content is not None
            if content is None:
                content = await self.engine.content_document(
                    self.current_source, chapter, next_chapter_url)
                if self._current_book is not None:
                    await asyncio.to_thread(
                        self.reading_cache.save_content,
                        self.current_source.bookSourceUrl,
                        self._current_book.url, chapter.url, content)
            if (open_generation is not None and
                    open_generation != self._book_open_generation):
                return
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
            self._status.value = (f"{chapter.name} · 已从缓存打开"
                                  if content_from_cache else chapter.name)
            self._show_section("reader")
        except Exception as exc:
            self._show_error(exc)
        finally:
            self._reading_loading = False
        self.update()
        if self._current_chapter is chapter:
            self._prefetch_task = self.page.run_task(self._prefetch_next_chapters, chapter)
        if not append:
            target_offset = max(0.0, float(restore_offset or 0))
            self._pending_scroll_offset = target_offset
            await self._reader.scroll_to(offset=target_offset, duration=150)

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
                if self._current_book is not None:
                    await asyncio.to_thread(
                        self.reading_cache.save_content,
                        source.bookSourceUrl, self._current_book.url,
                        candidate.url, document)
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
        previous_disabled = self._adjacent_chapter(-1) is None
        next_disabled = self._adjacent_chapter(1) is None
        self._top_prev_btn.disabled = previous_disabled
        self._top_next_btn.disabled = next_disabled
        if self._current_chapter in self.chapters:
            position = self.chapters.index(self._current_chapter) + 1
            self._reader_progress.value = (
                f"{position}/{len(self.chapters)} · {self._current_chapter.name}")
        else:
            self._reader_progress.value = "未选择章节"

    def _change_font_size(self, delta):
        new_size = max(13, min(28, self._font_size + int(delta)))
        if new_size == self._font_size:
            return
        self._font_size = new_size
        self._font_size_label.value = str(new_size)
        for control in self._content_host.controls:
            if getattr(control, "data", None) == "reader-text":
                control.size = new_size
        self.update()

    def _previous_chapter(self, _):
        chapter = self._adjacent_chapter(-1)
        if chapter and not self._reading_loading:
            self.page.run_task(self._run_content, chapter, False)

    def _next_chapter(self, _):
        chapter = self._adjacent_chapter(1)
        if chapter and not self._reading_loading:
            self.page.run_task(self._run_content, chapter, False)

    def _show_directory(self, _):
        self._show_section("detail")
        self.update()

    def _on_reader_scroll(self, event):
        pixels = getattr(event, "pixels", 0) or 0
        maximum = getattr(event, "max_scroll_extent", 0) or 0
        if self._current_chapter is None:
            return
        self._pending_scroll_offset = max(0.0, float(pixels))
        self._pending_scroll_ratio = (
            min(1.0, self._pending_scroll_offset / float(maximum))
            if maximum else 0.0)
        self._scroll_save_generation += 1
        self.page.run_task(
            self._save_scroll_position_after_delay,
            self._scroll_save_generation)
        if not maximum:
            return
        distance = float(maximum) - float(pixels)
        if distance > 500:
            self._auto_scroll_armed = True
            return
        if distance < 180 and self._auto_scroll_armed and not self._reading_loading:
            chapter = self._adjacent_chapter(1)
            if chapter and chapter.url not in self._continuous_loaded:
                self._auto_scroll_armed = False
                self.page.run_task(self._run_content, chapter, True)

    async def _save_scroll_position_after_delay(self, generation):
        await asyncio.sleep(0.5)
        if generation == self._scroll_save_generation:
            self._persist_scroll_position()

    def _persist_scroll_position(self):
        if (self._current_chapter is None or self._current_book is None or
                self.current_source is None):
            return
        text_length = sum(
            len(str(control.value or ""))
            for control in self._content_host.controls
            if getattr(control, "data", None) == "reader-text")
        # Legado persists the first visible character position. Flet exposes
        # pixels, so use the scroll ratio to retain the same layout-independent
        # semantic position, while also keeping the exact desktop pixel offset.
        chapter_pos = int(text_length * self._pending_scroll_ratio)
        self.service.update_bookshelf_state(
            self.current_source.bookSourceUrl,
            self._current_book.url,
            chapter=self._current_chapter,
            scroll_offset=self._pending_scroll_offset,
            scroll_ratio=self._pending_scroll_ratio,
            font_size=self._font_size,
            chapter_pos=chapter_pos,
            total_chapters=len(self.chapters))

    def _render_content(self, document):
        controls = []
        image_style = str(document.image_style or "").upper()
        current_book = getattr(self.engine, "current_book", None)
        configured_style = str((getattr(current_book, "read_config", {}) or {}).get(
            "imageStyle", "") or "").upper()
        if configured_style:
            image_style = configured_style
        for block in document.blocks:
            if block.kind == "text":
                text = ft.Text(
                    str(block.value), selectable=True, size=self._font_size)
                text.data = "reader-text"
                controls.append(text)
            elif block.kind == "image":
                options = self._image_render_options(image_style, self._font_size)
                controls.append(ft.Image(src=block.value,
                    error_content=ft.Text("图片加载失败"), **options))
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

    @staticmethod
    def _image_render_options(image_style, font_size):
        """Map Book.imgStyleDefault/FULL/TEXT to the desktop renderer."""
        style = str(image_style or "").upper()
        if style == "FULL":
            return {"width": float("inf"), "fit": ft.BoxFit.FIT_WIDTH}
        if style == "TEXT":
            return {"height": float(font_size), "fit": ft.BoxFit.CONTAIN}
        return {"fit": ft.BoxFit.SCALE_DOWN}

    def _set_busy(self, text):
        self._status.value = text
        self.update()

    def _show_error(self, exc):
        prefix = "当前书源暂不支持" if isinstance(exc, UnsupportedRule) else "操作失败"
        self._status.value = f"{prefix}：{exc}"
