"""书籍搜索、目录与正文阅读页面。"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import aiohttp
import flet as ft

from app import platform_support
from app.services.block_pages import SiteBlockedError
from app.services.public_suffix import effective_tld_plus_one
from app.services.rule_engine import (UnsupportedRule, parse_explore_categories,
                                      resolve_explore_categories, BookItem,
                                      RequestConnectionError,
                                      RequestTimeoutError, display_intro,
                                      html_to_text)
from app.services.rule_repair import rule_needs_script
from app.views.rule_repair_ui import diagnose_and_offer
from app.services.reading_engine import (ContentEmptyError, LegadoReadingEngine,
                                         render_use_html_spans)
from app.services.reading_cache import ReadingCache
from app.services.search_model import SearchBookStore, SearchModel
from app.services.headless_browser import available_fetcher
from app.services.book_cache_service import WholeBookCacheService
from app.services.reading_progress import ReadingProgressStore, ReadingSession
from app.services.web_page_renderer import WebPageRenderer
from app.ui_feedback import page_is_alive, show_snack_bar

# 找书与换源共用这一套：一轮要跑几百上千个互不相干的站点，并发可以比检测高，
# 但单个站点慢或不响应不该拖住整轮。
SWITCH_CONCURRENCY = 32
SWITCH_PUBLISH_INTERVAL = 0.5
# 连接阶段（DNS+TCP+TLS）单独限额，挂死的域名 5 秒就出局，正文仍可用满总时长。
SWITCH_TIMEOUT = 10
SWITCH_CONNECT_TIMEOUT = 5
# 搜索结果按 (书名, 作者) 缓存，供换源列表秒开；过期时间对齐 Legado 的一天。
SEARCH_STORE_TTL_MS = 24 * 60 * 60 * 1000
SEARCH_SCOPE_ALL = "__all__"
SEARCH_SCOPE_CURRENT = "__current__"
SEARCH_SCOPE_GROUP = "group:"


def _host_of(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return (urlsplit(raw if "//" in raw else f"//{raw}").hostname or "").lower()


def _site_token(value) -> str:
    """站点品牌词：`qbtr.me` 与 `www.qbtr.org` 都得到 `qbtr`。"""
    host = _host_of(value)
    if not host or host.replace(".", "").isdigit():
        return ""
    labels = [label for label in effective_tld_plus_one(host).split(".") if label]
    return labels[0] if len(labels) >= 2 else ""


def _source_judged_invalid(source) -> bool:
    """分组里带“失效/校验超时”说明上次检测已判它不可用。"""
    checker = getattr(source, "getInvalidGroupNames", None)
    try:
        return bool(checker()) if checker else False
    except Exception:
        return False


def _switch_failure_reason(exc: Exception) -> str:
    """把一次书源搜索失败归一成用户看得懂的短原因。"""
    if "UnsupportedJavaApiError" in str(exc):
        return "接口未模拟"
    if isinstance(exc, UnsupportedRule):
        return "规则不支持"
    if isinstance(exc, SiteBlockedError):
        # 本机安全软件或站点反爬给了拦截页，书源规则本身没问题。
        return "被拦截"
    if isinstance(exc, RequestTimeoutError):
        return "超时"
    if isinstance(exc, RequestConnectionError):
        return "网络错误"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "超时"
    if isinstance(exc, aiohttp.ClientError):
        return "网络错误"
    return "规则或解析错误"


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total < 3600:
        return f"{total // 60}:{total % 60:02d}"
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _intro_text(intro) -> str:
    """A book intro as readable text, whatever renderer it asked for."""
    kind, content = display_intro(intro)
    if kind == "text":
        return content
    return content.strip() if kind == "md" else html_to_text(content)


def _intro_control(intro) -> ft.Control:
    """The widget a book intro needs: markdown gets a renderer, the rest text."""
    kind, content = display_intro(intro)
    if kind == "md":
        return ft.Markdown(content, selectable=True,
                           extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED)
    text = content if kind == "text" else html_to_text(content)
    return ft.Text(text or "暂无简介", selectable=True)


class _EngineLease:
    """Hand an engine's lifetime to the requests still using it.

    A prefetch task that gets abandoned must not close the engine its fetch is
    still running on: the reader's own page turn may be waiting on that very
    fetch.  The last request to finish closes it.
    """

    def __init__(self, engine):
        self.engine = engine
        self._users = 0
        self._closed = False

    def acquire(self):
        self._users += 1
        return self.engine

    async def release(self):
        self._users -= 1
        if self._users <= 0 and not self._closed:
            self._closed = True
            await self.engine.aclose()


class _ResultFlow:
    """一轮结果的状态：书、页码、是否还有下一页，以及承载它们的列表。

    找书页把发现分类的浏览结果和关键词搜索的结果并排放置，两份状态各自独立，
    一边刷新不该把另一边的列表清空。
    """

    def __init__(self, on_scroll):
        self.list_view = ft.ListView(expand=True, spacing=4,
                                     on_scroll=on_scroll, scroll_interval=250)
        self.books = []
        self.page = 1
        self.has_more = True
        self.loading_more = False
        self.category = 0

    def reset(self):
        self.books = []
        self.page = 1
        self.has_more = True
        self.list_view.controls.clear()

    def append(self, new_books):
        existing = {book.url for book in self.books}
        added = []
        for book in new_books:
            if book.url not in existing:
                self.books.append(book)
                existing.add(book.url)
                added.append(book)
        return added


class ReadingView(ft.Column):
    DIRECTORY_BATCH_SIZE = 50
    PREFETCH_CHAPTER_COUNT = 10
    PREFETCH_CACHE_LIMIT = 64 * 1024 * 1024

    def __init__(self, service, discovery_only=False, on_section_change=None,
                 on_open_settings=None):
        super().__init__(spacing=0, expand=True)
        self.service = service
        self._on_section_change = on_section_change
        self._on_open_settings = on_open_settings
        self.reading_cache = ReadingCache(getattr(service, "cache_dir", service.data_dir))
        self.whole_book_cache = WholeBookCacheService(
            getattr(service, "cache_dir", service.data_dir), service.data_dir)
        self.progress_store = ReadingProgressStore(service)
        self.reading_session = ReadingSession()
        self._whole_book_tasks = {}
        self.discovery_only = discovery_only
        self.renderer = WebPageRenderer(
            lambda: platform_support.attached_page(self),
            self._set_webview_hint)
        self.engine = LegadoReadingEngine(
            webview_fetcher=self.renderer.render,
            verification_fetcher=self.renderer.verification_code,
            state_dir=service.data_dir,
        )
        self._current_chapter = None
        self._current_book = None
        self._pending_scroll_offset = 0.0
        self._pending_scroll_ratio = 0.0
        self._pending_document_offset = 0.0
        self._pending_scroll_maximum = 0.0
        self._scroll_save_generation = 0
        self._reader_preferences_path = service.data_dir / "reader-layout.json"
        self._reader_preferences = self._load_reader_preferences()
        self._font_size = int(self._reader_preferences["font_size"])
        self._reader_dialog = None
        self._reading_loading = False
        self._restoring_scroll = False
        self._prefetch_task = None
        self._chapter_cache = {}
        # 同一章节只发一次请求：翻页撞上还没回来的那一份就一起等。
        self._content_flight = {}
        self._content_generation = 0
        self._continuous_loaded = set()
        self._pending_append_starts = {}
        self._auto_scroll_armed = True
        self.chapters = []
        self.current_source = None
        self.categories = []
        self._category_load_id = 0
        self._category_cache = {}
        self._category_pending = set()
        self._expanded_sources = set()
        self._source_chips = {}
        self._source_icons = {}
        self._discovery_page = "sources"
        self._chapter_visible_count = 0
        self._visible_chapters = []
        self._directory_reversed = False
        self._book_detail_dialog = None
        self._detail_book = None
        self._book_open_generation = 0
        self._book_open_task = None
        # Progress text of a background task.  Switching tabs only hides this
        # view (app.py swaps the content container), so the work keeps running
        # and its last message must come back with the tab.
        self._background_status = None
        self._section = "shelf"
        self._detail_resume = None
        self._bookshelf_update_task = None
        self._source_switch_task = None
        self._source_switch_dialog = None
        self._source_switch_results = None
        self._source_switch_status = None
        self._source_switch_query = None
        self._source_switch_stop = None
        self._source_switch_search = None
        self._source_switch_progress = None
        self._source_switch_generation = 0
        self._source_switch_priority = {}
        self._search_task = None
        self._search_id = 0
        self._search_key = ""
        self._search_generation = 0
        self._search_completed = 0
        self._search_total = 0
        self._search_failures = collections.Counter()
        self._search_found = False
        self._search_started_at = 0.0
        self._search_last_publish = 0.0
        self._rendered_search_count = -1
        self._search_model_instance = None
        self._search_store_instance = None
        self._bookshelf_layout = "list"
        self._bookshelf_filter_generation = 0
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
        self._search_scope_value = SEARCH_SCOPE_ALL
        self._search_scope_options = [(SEARCH_SCOPE_ALL, "全部书源")]
        self._search_scope_label = ft.Text(
            "全部书源", size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._search_scope_button = ft.Container(
            padding=ft.Padding(12, 7, 12, 7), border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=self._open_scope_dialog,
            content=ft.Row([
                ft.Icon(ft.Icons.TUNE, size=16,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("搜索范围", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([self._search_scope_label], expand=True),
                ft.Icon(ft.Icons.EXPAND_MORE, size=16,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self._keyword = ft.TextField(label="书名或关键词", expand=True, on_submit=self._search)
        self._search_btn = ft.Button("搜索", icon=ft.Icons.SEARCH, on_click=self._search)
        self._search_stop = ft.TextButton(
            "停止", icon=ft.Icons.STOP, visible=False,
            on_click=self._stop_search)
        self._login_btn = ft.TextButton(
            "登录书源", icon=ft.Icons.LOGIN, visible=False,
            on_click=self._show_source_login)
        self._login_fields = {}
        self._login_dialog = None
        self._status = ft.Text("选择书源浏览分类，或直接搜索书名", color=ft.Colors.GREY_600)
        self._search_progress = ft.ProgressBar(value=0, visible=False)
        # 浏览与搜索各占一份列表：来回切换时谁也不会把谁的结果清掉。
        self._browse_flow = _ResultFlow(on_scroll=self._on_browse_scroll)
        self._search_flow = _ResultFlow(on_scroll=self._on_search_scroll)
        self._category_host = ft.Column(spacing=0)
        self._source_list = ft.ListView(expand=True, spacing=2)
        self._source_chips = {}
        self._browse_title = ft.Text("", size=16, weight=ft.FontWeight.W_500,
                                     expand=True, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS)
        self._sources_page = ft.Column(expand=True, spacing=0, controls=[
            ft.Container(padding=12, content=ft.Column([
                ft.Text("找书", size=28, weight=ft.FontWeight.BOLD),
                ft.Container(
                    padding=ft.Padding(12, 9, 12, 9), border_radius=10,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    on_click=self._open_search_page,
                    content=ft.Row([
                        ft.Icon(ft.Icons.SEARCH, size=18,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        ft.Text("搜索书名或作者", expand=True,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ], spacing=8)),
                ft.Row([
                    ft.Text("书源", size=13, weight=ft.FontWeight.W_500),
                    ft.IconButton(ft.Icons.REFRESH, tooltip="刷新书源列表",
                                  on_click=lambda e: self.refresh_sources()),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ], spacing=8)),
            ft.Divider(height=1),
            self._source_list,
        ])
        self._browse_page = ft.Column(
            expand=True, visible=False, spacing=0, controls=[
                ft.Container(padding=ft.Padding(4, 2, 6, 2), content=ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书源列表",
                                  on_click=self._back_to_sources),
                    self._browse_title,
                    self._login_btn,
                ], spacing=4)),
                self._category_host,
                ft.Divider(height=1),
                self._browse_flow.list_view,
            ])
        self._search_page = ft.Column(
            expand=True, visible=False, spacing=0, controls=[
                ft.Container(padding=ft.Padding(4, 2, 8, 2), content=ft.Row([
                    ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书源列表",
                                  on_click=self._back_to_sources),
                    self._keyword, self._search_btn, self._search_stop,
                ], spacing=4)),
                ft.Container(padding=ft.Padding(12, 0, 12, 6),
                             content=self._search_scope_button),
                self._search_progress,
                self._search_flow.list_view,
            ])
        self._discover = ft.Stack(
            expand=True,
            controls=[self._sources_page, self._browse_page, self._search_page])
        self._bookshelf = ft.ListView(expand=True, spacing=4)
        self._bookshelf_grid = ft.GridView(
            expand=True, max_extent=190, child_aspect_ratio=0.72,
            spacing=8, run_spacing=8)
        self._bookshelf_render_generation = 0
        self._continue_reading_host = ft.Container(visible=False)
        self._bookshelf_host = ft.Column(
            expand=True, spacing=0, controls=[
                ft.Container(padding=ft.Padding(8, 4, 8, 4), content=ft.ResponsiveRow([
                    self._bookshelf_query, self._bookshelf_group,
                    self._bookshelf_sort,
                    self._bookshelf_layout_btn, self._bookshelf_refresh_btn,
                ], columns=12, spacing=6, run_spacing=4)),
                self._continue_reading_host,
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
        self._content_host = ft.Column(
            spacing=float(self._reader_preferences["paragraph_spacing"]))
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
            visible=False,
            padding=ft.Padding(6, 2, 6, 2),
            content=ft.Row([
                self._top_prev_btn,
                ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书籍详情",
                              on_click=self._show_directory),
                ft.IconButton(ft.Icons.FORMAT_LIST_BULLETED, tooltip="目录",
                              on_click=self._open_reader_directory),
                self._reader_progress,
                ft.IconButton(ft.Icons.TEXT_DECREASE, tooltip="减小字号",
                              on_click=lambda e: self._change_font_size(-1)),
                ft.Row([ft.Text("字号", size=11), self._font_size_label],
                       spacing=3, tight=True),
                ft.IconButton(ft.Icons.TEXT_INCREASE, tooltip="增大字号",
                              on_click=lambda e: self._change_font_size(1)),
                ft.IconButton(ft.Icons.TEXT_FIELDS, tooltip="阅读设置",
                              on_click=self._open_reader_settings),
                self._top_next_btn,
            ], spacing=0),
        )
        self._reader_document = ft.Container(
            width=float(self._reader_preferences["max_width"]),
            padding=ft.Padding(16, 20, 16, 28),
            on_click=self._on_reader_tap,
            content=ft.Column([
                self._title, ft.Divider(), self._content_host,
            ], spacing=12),
        )
        self._reader = ft.Column(
            expand=True, scroll=ft.ScrollMode.AUTO,
            scroll_interval=200, on_scroll=self._on_reader_scroll,
            controls=[ft.Row(
                [self._reader_document],
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.START)],
        )
        self._reader_status_bar = ft.Container(
            visible=bool(self._reader_preferences["show_status"]),
            padding=ft.Padding(12, 2, 12, 4),
            content=ft.Text(
                "", size=11, text_align=ft.TextAlign.CENTER,
                color=ft.Colors.ON_SURFACE_VARIANT),
        )
        self._reader_interaction = self._reader
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
                self._reader_toolbar, self._reader_interaction,
                self._reader_status_bar,
            ])
        self._section_host = ft.Stack(
            expand=True,
            controls=[self._shelf_page, self._detail_page, self._reader_page])
        if discovery_only:
            # 找书页三块内容（书源列表 / 分类浏览 / 搜索）叠在同一个位置上，
            # 状态行固定在底部，切换子页时任务进度不会跟着消失。
            self.controls = [
                self._discover,
                ft.Container(padding=ft.Padding(12, 2, 12, 6), content=self._status),
            ]
            self._set_discovery_page("sources")
        else:
            self._page_title = ft.Text(
                "我的书架", size=28, weight=ft.FontWeight.BOLD)
            self._reading_header = ft.Container(
                padding=12, content=ft.Column([
                    self._page_title, self._status,
                ]))
            self.controls = [
                self._reading_header,
                self._section_host,
            ]
        self._apply_reader_preferences()

    def _load_reader_preferences(self):
        defaults = {
            "font_size": 16, "line_height": 1.7,
            "paragraph_spacing": 12, "max_width": 760,
            "indent": True, "text_align": "start",
            "background": "follow", "show_status": True,
            "shortcuts": {
                "previous": "left", "next": "right",
                "page_up": "pageup", "page_down": "pagedown",
                "directory": "f", "settings": "comma",
                "close": "escape",
            },
        }
        try:
            value = json.loads(self._reader_preferences_path.read_text(
                encoding="utf-8"))
            if isinstance(value, dict):
                defaults.update({key: value[key] for key in defaults if key in value})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            defaults["font_size"] = max(13, min(28, int(defaults["font_size"])))
        except (TypeError, ValueError):
            defaults["font_size"] = 16
        try:
            defaults["line_height"] = max(
                1.3, min(2.2, float(defaults["line_height"])))
        except (TypeError, ValueError):
            defaults["line_height"] = 1.7
        try:
            defaults["paragraph_spacing"] = max(
                4, min(24, float(defaults["paragraph_spacing"])))
        except (TypeError, ValueError):
            defaults["paragraph_spacing"] = 12
        try:
            defaults["max_width"] = float(defaults["max_width"])
        except (TypeError, ValueError):
            defaults["max_width"] = 760.0
        if defaults["max_width"] not in {620.0, 700.0, 760.0}:
            defaults["max_width"] = 760.0
        if defaults["text_align"] not in {"start", "justify"}:
            defaults["text_align"] = "start"
        if defaults["background"] not in {
                "follow", "paper", "eye", "night", "eink"}:
            defaults["background"] = "follow"
        defaults["indent"] = bool(defaults["indent"])
        defaults["show_status"] = bool(defaults["show_status"])
        shortcut_defaults = {
            "previous": "left", "next": "right",
            "page_up": "pageup", "page_down": "pagedown",
            "directory": "f", "settings": "comma",
            "close": "escape",
        }
        shortcuts = defaults.get("shortcuts")
        if not isinstance(shortcuts, dict):
            shortcuts = {}
        migrated = {"ctrl+f": "f", "ctrl+comma": "comma"}
        defaults["shortcuts"] = {
            name: migrated.get(
                str(shortcuts.get(name) or binding).lower(),
                str(shortcuts.get(name) or binding).lower())
            for name, binding in shortcut_defaults.items()
        }
        return defaults

    def _save_reader_preferences(self):
        try:
            self._reader_preferences_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._reader_preferences_path.with_suffix(".tmp")
            temp.write_text(json.dumps(
                self._reader_preferences, ensure_ascii=False, indent=2),
                encoding="utf-8")
            temp.replace(self._reader_preferences_path)
        except OSError:
            pass

    def _apply_reader_preferences(self):
        preferences = self._reader_preferences
        self._font_size = int(preferences["font_size"])
        self._font_size_label.value = str(self._font_size)
        self._content_host.spacing = float(preferences["paragraph_spacing"])
        self._reader_document.width = float(preferences["max_width"])
        self._reader_status_bar.visible = bool(preferences["show_status"])
        palette = {
            "follow": (None, None), "paper": ("#F5EEDC", "#332F27"),
            "eye": ("#DCE8D5", "#243025"), "night": ("#151515", "#D8D8D8"),
            "eink": ("#FFFFFF", "#000000"),
        }
        background, foreground = palette[preferences["background"]]
        self._reader_document.bgcolor = background
        self._title.color = foreground
        alignment = (ft.TextAlign.JUSTIFY
                     if preferences["text_align"] == "justify"
                     else ft.TextAlign.START)
        for control in self._content_host.controls:
            role = getattr(control, "data", None)
            if role not in {"reader-text", "reader-heading"}:
                continue
            control.size = self._font_size + (2 if role == "reader-heading" else 0)
            control.style = ft.TextStyle(
                height=(1.45 if role == "reader-heading"
                        else float(preferences["line_height"])))
            control.text_align = alignment
            control.color = foreground
            if role == "reader-text":
                raw = str(control.value or "").lstrip("　")
                if preferences["indent"] and not raw.startswith(
                        ("•", "·", "- ", "— ")):
                    control.value = "　　" + raw
                elif not preferences["indent"]:
                    control.value = raw
        self._save_reader_preferences()

    def _set_reader_preference(self, key, value):
        self._reader_preferences[key] = value
        self._apply_reader_preferences()
        try:
            self.update()
        except RuntimeError:
            pass

    def _set_reader_shortcut(self, action, binding):
        shortcuts = self._reader_preferences["shortcuts"]
        binding = str(binding or "").lower()
        previous = shortcuts.get(action, "")
        for name, value in shortcuts.items():
            if name != action and value == binding:
                shortcuts[name] = previous
                break
        shortcuts[action] = binding
        self._save_reader_preferences()

    @staticmethod
    def _normalize_reader_key(value):
        aliases = {
            "arrow left": "left", "arrowleft": "left",
            "arrow right": "right", "arrowright": "right",
            "arrow up": "up", "arrowup": "up",
            "arrow down": "down", "arrowdown": "down",
            "page up": "pageup", "page down": "pagedown",
            " ": "space", ",": "comma", "keyf": "f",
            "keycomma": "comma", "esc": "escape",
            "control left": "ctrl", "control right": "ctrl",
            "controlleft": "ctrl", "controlright": "ctrl",
            "ctrl left": "ctrl", "ctrl right": "ctrl",
            "shift left": "shift", "shift right": "shift",
            "shiftleft": "shift", "shiftright": "shift",
            "alt left": "alt", "alt right": "alt",
            "altleft": "alt", "altright": "alt",
        }
        key = str(value or "").strip().lower().replace("_", " ")
        if key.startswith("key ") and len(key) == 5:
            key = key[-1]
        return aliases.get(key, key)

    def _reader_key_binding(self, event):
        return self._normalize_reader_key(getattr(event, "key", ""))

    def _on_reader_key_down(self, event):
        key = self._normalize_reader_key(getattr(event, "key", ""))
        if key in {"ctrl", "shift", "alt"}:
            return
        self._on_reader_key(event)

    def _on_reader_key_repeat(self, event):
        key = self._normalize_reader_key(getattr(event, "key", ""))
        if key not in {"ctrl", "shift", "alt"}:
            self._on_reader_key(event)

    def did_mount(self):
        if self.discovery_only:
            # 找书页从书源列表开始：用户自己挑一个源进去看分类，不自动翻第一个源。
            self.refresh_sources()
            return
        # Tab switches reuse this view object, so the mount path is also the
        # "came back into view" path: refresh the cards (offline badges follow
        # the live task table) and re-show the progress of work that ran while
        # the shelf was hidden.
        self._render_bookshelf()
        self._restore_background_status()

    def will_unmount(self):
        # Unmount here means "hidden", not "closed": the app keeps this view
        # alive and swaps it back in.  Only work that depends on a control in
        # this subtree may be cancelled, otherwise a tab switch would kill
        # whole-book caching, catalog loading and chapter prefetch.
        self.renderer.cancel()
        self._persist_scroll_position()


    def refresh_sources(self, update=True):
        """重排书源列表：能用（启用且有规则）的书源按书源顺序列出。"""
        enabled = [s for s in self.service.sources
                   if s.enabled and (s.searchUrl or s.exploreUrl)]
        self._source_chips = {}
        self._source_icons = {}
        self._source_list.controls = [self._source_row(source)
                                      for source in enabled]
        if not enabled:
            self.current_source = None
        elif self.current_source not in enabled:
            self.current_source = enabled[0]
        self._update_login_button()
        self._refresh_search_scope()
        self._rebuild_category_tabs()
        if update:
            self.update()

    def _set_discovery_page(self, page_name):
        """找书页的三个子页：书源列表 / 分类浏览 / 搜索。"""
        self._discovery_page = page_name
        self._sources_page.visible = page_name == "sources"
        self._browse_page.visible = page_name == "browse"
        self._search_page.visible = page_name == "search"

    def _source_row(self, source):
        """书源列表的一行：点一下展开发现分类，右键/长按出菜单。"""
        url = str(source.bookSourceUrl or "")
        expanded = url in self._expanded_sources
        chevron = ft.Icon(ft.Icons.EXPAND_MORE if expanded
                         else ft.Icons.CHEVRON_RIGHT, size=18)
        chips = ft.Column(spacing=6, visible=expanded, controls=[])
        self._source_icons[url] = chevron
        self._source_chips[url] = chips
        if expanded:
            chips.controls = self._source_chip_controls(source)
        return ft.GestureDetector(
            on_tap=lambda e, s=source: self._toggle_source_expand(s),
            on_secondary_tap_down=lambda e, s=source: self._show_source_menu(s),
            on_long_press=lambda e, s=source: self._show_source_menu(s),
            content=ft.Container(
                padding=ft.Padding(12, 6, 12, 6),
                content=ft.Column([
                    ft.Row([
                        chevron,
                        ft.Column([
                            ft.Text(source.display_name, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    weight=ft.FontWeight.W_500),
                            ft.Text(self._source_subtitle(source), size=11,
                                    color=ft.Colors.GREY_600, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ], spacing=2, expand=True),
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    chips,
                ], spacing=4),
            ))

    def _toggle_source_expand(self, source):
        """点开一行：展开它的发现分类，再点收回。"""
        url = str(source.bookSourceUrl or "")
        if url in self._expanded_sources:
            self._expanded_sources.discard(url)
            chips = self._source_chips.get(url)
            if chips is not None:
                chips.visible = False
            chevron = self._source_icons.get(url)
            if chevron is not None:
                chevron.icon = ft.Icons.CHEVRON_RIGHT
            self.update()
            return
        self._expanded_sources.add(url)
        self.current_source = source
        self._update_login_button()
        chevron = self._source_icons.get(url)
        if chevron is not None:
            chevron.icon = ft.Icons.EXPAND_MORE
        chips = self._source_chips.get(url)
        if chips is not None:
            chips.controls = self._source_chip_controls(source)
            chips.visible = True
        if self._source_is_search_only(source):
            self._status.value = "该书源仅支持关键词搜索，请输入书名或作者"
        self.update()

    def _source_chip_controls(self, source):
        """一行里摊开的分类标签；还没解析过就先去解析。"""
        if self._source_is_search_only(source):
            return [ft.Row([
                ft.Text("仅支持关键词搜索", size=12, color=ft.Colors.GREY_600),
                ft.Button("用此书源搜索", icon=ft.Icons.SEARCH, height=34,
                          on_click=lambda e, s=source:
                          self._search_with_source(s)),
            ])]
        url = str(source.bookSourceUrl or "")
        categories = self._category_cache.get(url)
        if categories is None:
            if url not in self._category_pending:
                self._category_pending.add(url)
                self.page.run_task(self._load_categories, source)
            return [ft.Text("正在解析分类…", size=12, color=ft.Colors.GREY_600)]
        if not categories:
            return [ft.Text("没有可用的发现分类", size=12, color=ft.Colors.GREY_600)]
        return [ft.Row(
            [ft.Chip(label=ft.Text(category.title), height=34,
                     on_click=lambda e, s=source, c=category:
                     self._open_category(s, c))
             for category in categories],
            wrap=True, spacing=6, run_spacing=6)]

    def _fill_source_chips(self, source):
        """分类解析完就补进列表页那一行（若它还开着）。"""
        url = str(source.bookSourceUrl or "")
        chips = self._source_chips.get(url)
        if chips is None or url not in self._expanded_sources:
            return
        chips.controls = self._source_chip_controls(source)

    def _open_category(self, source, category):
        """进分类浏览页：先拿标签建 Tab，再跑第一页。"""
        self.current_source = source
        self._update_login_button()
        self._cancel_prefetch(clear_cache=True)
        self._category_load_id += 1
        self._browse_flow.reset()
        self._rebuild_category_tabs(
            self._category_cache.get(str(source.bookSourceUrl or "")) or
            self._checked_categories(source))
        self._browse_title.value = source.display_name
        self._set_discovery_page("browse")
        index = next((position for position, item in enumerate(self.categories)
                      if item.url == category.url), 0)
        self.page.run_task(self._run_explore, index, True)

    def _back_to_sources(self, _=None):
        self._set_discovery_page("sources")
        self.update()

    def _open_search_page(self, _=None, source=None):
        """打开搜索页；带上书源就是把这一轮限定在它身上。"""
        if source is not None:
            self.current_source = source
            self._update_login_button()
            self._set_search_scope(SEARCH_SCOPE_CURRENT)
            self._status.value = f"搜索范围：{source.display_name}，请输入书名或作者"
        self._set_discovery_page("search")
        self.update()

    def _search_with_source(self, source):
        self._open_search_page(source=source)

    def _show_source_menu(self, source):
        """右键/长按书源行：列出对这一个书源能做的事。"""
        actions = [ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())]
        if self.engine.login_available(source):
            actions.append(ft.TextButton(
                "登录书源", icon=ft.Icons.LOGIN,
                on_click=lambda e, s=source: self._login_from_menu(s)))
        actions.append(ft.Button(
            "用此书源搜索", icon=ft.Icons.SEARCH,
            on_click=lambda e, s=source: self._search_from_menu(s)))
        dialog = ft.AlertDialog(
            modal=True, title=ft.Text(source.display_name),
            content=ft.Container(width=420, content=ft.Column([
                ft.Text(source.bookSourceUrl or "", size=11,
                        color=ft.Colors.GREY_600, max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS),
                ft.Text(f"状态：{'启用' if source.enabled else '停用'}"
                        f" · 分组：{source.display_group}"),
                ft.Text("该书源仅支持关键词搜索" if self._source_is_search_only(source)
                        else "点一下这一行可以展开它的发现分类"),
            ], tight=True, spacing=8)),
            actions=actions)
        self.page.show_dialog(dialog)

    def _login_from_menu(self, source):
        self.page.pop_dialog()
        self.current_source = source
        self._update_login_button()
        self.page.run_task(self._open_source_login, source)

    def _search_from_menu(self, source):
        self.page.pop_dialog()
        self._open_search_page(source=source)

    @staticmethod
    def _source_is_search_only(source) -> bool:
        """只有搜索规则、没有发现分类的书源。"""
        return (bool(str(source.searchUrl or "").strip()) and
                not str(source.exploreUrl or "").strip())

    @classmethod
    def _source_subtitle(cls, source) -> str:
        """列表行副标题：站点、分组，以及“只能搜”这个标记。"""
        parts = [str(source.short_url or ""), str(source.bookSourceGroup or "")]
        if cls._source_is_search_only(source):
            parts.append("仅搜索")
        return " · ".join(part for part in parts if part)

    def _update_login_button(self):
        source = self.current_source
        self._login_btn.visible = bool(
            source and self.engine.login_available(source))

    def _refresh_search_scope(self):
        """搜索范围选项：全部 / 各分组 / 仅当前书源，保持已选值有效。"""
        previous = str(self._search_scope_value or SEARCH_SCOPE_ALL)
        groups = []
        if hasattr(self.service, "get_groups"):
            try:
                groups = list(self.service.get_groups())
            except Exception:
                groups = []
        options = [(SEARCH_SCOPE_ALL, "全部书源"),
                   (SEARCH_SCOPE_CURRENT, "仅当前书源")]
        options.extend((f"{SEARCH_SCOPE_GROUP}{group}", f"分组：{group}")
                       for group in groups)
        self._search_scope_options = options
        valid = {key for key, _ in options}
        self._search_scope_value = (previous if previous in valid
                                    else SEARCH_SCOPE_ALL)
        self._search_scope_label.value = self._search_scope_text()

    def _search_scope_text(self) -> str:
        for key, label in self._search_scope_options:
            if key == self._search_scope_value:
                return label
        return "全部书源"

    def _set_search_scope(self, value):
        self._search_scope_value = value
        self._search_scope_label.value = self._search_scope_text()

    def _open_scope_dialog(self, _=None):
        """搜索范围入口：和 Legado 一样用对话框选，不在页面上摆一个下拉框。"""

        def choose(key):
            def handler(_):
                self._set_search_scope(key)
                self.page.pop_dialog()
                self._scope_changed()
            return handler

        dialog = ft.AlertDialog(
            modal=True, title=ft.Text("搜索范围"),
            content=ft.Container(width=380, content=ft.Column(
                tight=True, spacing=0, controls=[
                    ft.ListTile(
                        dense=True, data=key, title=ft.Text(label),
                        selected=key == self._search_scope_value,
                        on_click=choose(key))
                    for key, label in self._search_scope_options])),
            actions=[ft.TextButton("关闭",
                                   on_click=lambda e: self.page.pop_dialog())])
        self.page.show_dialog(dialog)

    def _scope_changed(self, e=None):
        count = len(self._search_scope_sources())
        self._status.value = f"搜索范围：{count} 个书源，可开始搜索"
        self.update()

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
        """解析一个书源的发现分类：列表页展开的标签和浏览页的 Tab 共用这一份。"""
        if source is None:
            return
        url = str(source.bookSourceUrl or "")
        self._category_pending.discard(url)
        if not str(source.exploreUrl or "").strip():
            if (source is self.current_source and
                    (load_id is None or load_id == self._category_load_id)):
                self._rebuild_category_tabs([])
                self._status.value = "该书源仅支持关键词搜索，请输入书名或作者"
                self.update()
            return
        try:
            categories = await resolve_explore_categories(source, self.engine.js)
        except Exception as exc:
            if source is self.current_source:
                self._show_error(
                    str(exc)[:100], label="分类标签解析失败",
                    retry=lambda: self.page.run_task(
                        self._load_categories, source))
                self.update()
            return
        categories = self._checked_categories(source, categories)
        self._category_cache[url] = categories
        self._fill_source_chips(source)
        if (source is not self.current_source or
                (load_id is not None and load_id != self._category_load_id)):
            return
        self._rebuild_category_tabs(categories)
        if self.categories and run_first:
            await self._run_explore(0, True)
        elif not self.categories:
            self._set_status("该书源的发现分类未生成可用入口，可使用关键词搜索")
        self.update()

    def _checked_categories(self, source, categories=None):
        """把检测结果套到分类上：只隐藏它证明坏掉的，其余照旧展示。"""
        values = list(categories if categories is not None else
                      (parse_explore_categories(source) if source else []))
        checked = getattr(self.service, "category_checks", {}).get(
            source.bookSourceUrl if source else "")
        if checked is None:
            return values
        # 检测只抽样了几个分类，所以这个列表不是完整审计：它证明坏掉的先不摆出来，
        # 没抽到的保持原样。
        broken_urls = {item["url"] for item in checked
                       if not item.get("valid")}
        return [category for category in values
                if category.url not in broken_urls]

    def _rebuild_category_tabs(self, categories=None):
        self.categories = self._checked_categories(self.current_source, categories)
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
        flow = self._browse_flow
        if index < 0 or index >= len(self.categories):
            return
        if flow.loading_more:
            return
        if reset:
            flow.reset()
            flow.category = index
        category = self.categories[index]
        supports_paging = any(token in category.url for token in
                              ("{{page}}", "{{page-1}}", "{{page+1}}", "<,"))
        flow.loading_more = True
        self._set_busy(f"正在加载“{category.title}”第 {flow.page} 页…")
        try:
            new_books = await self.engine.explore(self.current_source, category, flow.page)
            added_books = flow.append(new_books)
            added = len(added_books)
            flow.has_more = supports_paging and bool(new_books) and added > 0
            if flow.has_more:
                flow.page += 1
            self._append_book_controls(flow, added_books)
            paging_text = "，可继续下拉加载" if flow.has_more else (
                "，固定榜单无后续分页" if not supports_paging else "，已加载全部结果"
            )
            self._set_status(f"{category.title}：找到 {len(flow.books)} 本书{paging_text}")
            self._set_discovery_page("browse")
        except Exception as exc:
            self._show_error(
                exc, retry=lambda: self.page.run_task(
                    self._run_explore, index, False))
            flow.has_more = False
        finally:
            flow.loading_more = False
        self.update()

    def _search(self, e):
        keyword = str(self._keyword.value or "").strip()
        if not keyword:
            self._status.value = "请输入书名或关键词"
            self.update()
            return
        sources = self._search_scope_sources()
        if not sources:
            self._status.value = "搜索范围内没有可用书源（书源需启用且带搜索规则）"
            self.update()
            return
        self._start_search(keyword, sources)

    def _search_scope_sources(self):
        """搜索范围：全部 / 分组 / 仅当前书源，按书源顺序排队。"""
        scope = str(self._search_scope_value or SEARCH_SCOPE_ALL)
        if scope == SEARCH_SCOPE_CURRENT:
            sources = [self.current_source] if self.current_source else []
        else:
            sources = [source for source in self.service.sources
                       if source.enabled and str(source.searchUrl or "").strip()]
            if scope.startswith(SEARCH_SCOPE_GROUP):
                group = scope[len(SEARCH_SCOPE_GROUP):]
                sources = [source for source in sources
                           if getattr(source, "hasGroup", None)
                           and source.hasGroup(group)]
        return sorted(sources, key=lambda source: (
            int(getattr(source, "customOrder", 0) or 0),
            str(source.bookSourceName or "")))

    def _start_search(self, keyword, sources):
        """新一轮全书源搜索：清空上一轮，按范围里的书源各搜一次。"""
        self._cancel_search_task()
        self._search_generation += 1
        self._search_id = int(time.time() * 1000)
        self._search_key = keyword
        self._search_model_instance = self._search_model(self._search_generation)
        self._search_flow.reset()
        self._set_search_running(True)
        self._status.value = f"正在搜索 {len(sources)} 个书源…"
        self._set_discovery_page("search")
        self._search_task = self.page.run_task(
            self._run_search, self._search_generation, keyword, sources,
            self._search_id)
        self.update()

    def _load_more_search(self):
        """加载下一页：沿用同一个 search id，SearchModel 自己 page++ 并接着合并。"""
        flow = self._search_flow
        if flow.loading_more or not flow.has_more:
            return
        if not self._search_key or self._search_model_instance is None:
            return
        self._set_search_running(True)
        self._search_task = self.page.run_task(
            self._run_search, self._search_generation, self._search_key, None,
            self._search_id)

    def _stop_search(self, _=None):
        if self._search_task is None:
            return
        # 先让这一轮作废，再取消任务：收尾的协程看到 generation 变了就不再回写界面。
        self._search_generation += 1
        self._cancel_search_task()
        self._search_flow.has_more = False
        self._set_search_running(False)
        self._status.value = f"搜索已停止，已找到 {len(self._search_flow.books)} 本"
        # 停止后既没有"加载更多"也没有"已经到底"，只留已经拿到的结果。
        self._render_search_results(with_footer=False)
        self.update()

    def _cancel_search_task(self):
        task = self._search_task
        self._search_task = None
        if task is not None and not task.done():
            # 取消会顺着 await 传进 SearchModel 的 worker，在飞的请求一起收掉。
            task.cancel()

    def _set_search_running(self, running):
        self._search_flow.loading_more = running
        self._search_stop.visible = running
        self._search_btn.visible = not running
        self._search_progress.visible = running
        if running:
            self._search_progress.value = 0

    def _search_model(self, generation):
        """一次搜索一个模型实例，闭包带着这一轮的 generation 做进度回写。"""
        async def search_one(source, keyword, page):
            return await self._search_one_source(source, keyword, page, generation)
        return SearchModel(
            search_one, thread_count=SWITCH_CONCURRENCY,
            store=self._search_store())

    async def _search_one_source(self, source, keyword, page, generation):
        """一个搜索位：独立引擎 + 短超时，失败只计数，不打断整轮。"""
        engine = self._background_engine(
            timeout=SWITCH_TIMEOUT, connect_timeout=SWITCH_CONNECT_TIMEOUT)
        try:
            items = await engine.search(source, keyword, page)
            if items:
                self._search_found = True
            return items
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._search_failures[_switch_failure_reason(exc)] += 1
            raise
        finally:
            await engine.aclose()
            # 结束的轮次不再回写：generation 变了说明用户已经换了关键词或停了搜索。
            if generation == self._search_generation:
                self._search_completed += 1
                self._publish_search()

    async def _run_search(self, generation, keyword, sources, search_id):
        self._search_completed = 0
        self._search_failures = collections.Counter()
        self._search_found = False
        self._search_started_at = time.monotonic()
        self._search_last_publish = 0.0
        self._rendered_search_count = -1
        model = self._search_model_instance
        if sources is None:
            sources = list(model.sources)
        self._search_total = len(sources)
        try:
            await model.search(search_id, keyword, sources)
        finally:
            # 被停止或被新一轮接替时 generation 已经变了，收尾就不该再回写界面；
            # 取消照常往外抛，任务是取消的这一点对调用方仍然是实话。
            if generation == self._search_generation:
                self._search_task = None
                self._search_flow.has_more = self._search_found
                self._set_search_running(False)
                self._publish_search(finished=True)

    def _publish_search(self, finished=False):
        """把最新合并结果与进度刷到界面上；节流避免逐条重绘整张列表。"""
        now = time.monotonic()
        if not finished and now - self._search_last_publish < SWITCH_PUBLISH_INTERVAL:
            return
        self._search_last_publish = now
        flow = self._search_flow
        model = self._search_model_instance
        if model is not None:
            flow.books = list(model.books)
        if finished or len(flow.books) != self._rendered_search_count:
            self._rendered_search_count = len(flow.books)
            self._render_search_results()
        self._status.value = self._switch_status_text(
            self._search_completed, self._search_total, flow.books,
            self._search_failures, running=not finished,
            elapsed=now - self._search_started_at, noun="本")
        self._search_progress.value = (
            self._search_completed / self._search_total
            if self._search_total else 0)
        self.update()

    def _render_search_results(self, with_footer=True):
        flow = self._search_flow
        flow.list_view.controls = [
            self._book_control(book) for book in flow.books]
        if with_footer:
            self._append_result_footer(flow)

    def _append_result_footer(self, flow):
        if flow.has_more:
            footer = ft.Button("加载更多", icon=ft.Icons.EXPAND_MORE,
                               on_click=lambda e, target=flow:
                               self._load_more(target))
            footer.data = "result-footer"
            flow.list_view.controls.append(footer)
        elif flow.books:
            footer = ft.Text("已经到底了", text_align=ft.TextAlign.CENTER,
                             color=ft.Colors.GREY_500)
            footer.data = "result-footer"
            flow.list_view.controls.append(footer)

    def _source_by_url(self, url):
        for source in self.service.sources:
            if str(source.bookSourceUrl or "") == str(url or ""):
                return source
        return None

    def _source_for_book(self, book):
        """搜索结果来自哪个书源由书自己的 origin 决定，而不是当前下拉框。"""
        return (self._source_by_url(getattr(book, "origin", ""))
                or self.current_source)

    def _source_label(self, book):
        origins = book.origin_set()
        if not origins:
            return ""
        source = self._source_by_url(origins[0])
        name = (str(getattr(source, "bookSourceName", "") or "")
                if source else (book.origin_name or _host_of(origins[0])))
        if len(origins) > 1:
            return f"{name} 等 {len(origins)} 个书源"
        return name

    def _search_store(self):
        """找书与换源共享的本地结果库：Legado 的 searchBooks 表。"""
        if self._search_store_instance is None:
            store = SearchBookStore(self.service.data_dir / "search-books.sqlite3")
            # 和 Legado 启动时一样，清掉一天前的搜索结果。
            store.clear_expired(
                int(time.time() * 1000) - SEARCH_STORE_TTL_MS)
            self._search_store_instance = store
        return self._search_store_instance

    def _book_control(self, book):
        cover = (ft.Image(src=book.cover_url, width=42, height=56,
                          fit=ft.BoxFit.COVER,
                          error_content=ft.Icon(ft.Icons.BROKEN_IMAGE))
                 if book.cover_url else ft.Icon(ft.Icons.BOOK_OUTLINED))
        # 全书源结果里每本书属于它自己的书源，封面解码要用那个书源的规则。
        source = self._source_for_book(book)
        if (book.cover_url and source and
                getattr(source, "coverDecodeJs", "")):
            self.page.run_task(self._decode_book_cover, book, cover, source)
        return ft.ListTile(
            leading=cover, title=ft.Text(book.name),
            subtitle=ft.Text(" · ".join(
                x for x in [book.author, self._source_label(book),
                            _intro_text(book.intro)[:80]] if x)),
            trailing=ft.IconButton(
                ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="加入书架",
                on_click=lambda e, selected=book: self._add_to_bookshelf(selected),
            ),
            on_click=lambda e, selected=book: self._show_book_detail(selected),
        )

    def _show_book_detail(self, book):
        source = self._source_for_book(book)
        if not source:
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
        self.page.run_task(self._load_book_detail, book, source)

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
            _intro_control(book.intro),
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

    async def _decode_book_cover(self, book, image, source=None):
        try:
            image.src = await self.engine.decode_cover(
                source or self._source_for_book(book), book.cover_url)
            book.cover_url = image.src
            image.update()
        except Exception:
            # The original URL remains visible if a source decoder fails.
            return

    def _add_to_bookshelf(self, book):
        source = self._source_for_book(book)
        if not source:
            return
        added = self.service.add_to_bookshelf(source, book)
        self._set_status(f"《{book.name}》已加入书架" if added else
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
        recent = max(
            self.service.bookshelf,
            key=lambda item: int((self.service.reading_state(item) or {}).get(
                "last_read_at") or 0), default=None)
        recent_state = self.service.reading_state(recent) if recent else None
        self._continue_reading_host.visible = bool(
            recent and recent_state and recent_state.get("chapter_name"))
        if self._continue_reading_host.visible:
            total = max(0, int(recent.get("totalChapterNum", 0) or 0))
            chapter_index = max(0, int(recent_state.get("chapter_index", 0) or 0))
            progress = min(1.0, (chapter_index + 1) / total) if total else 0.0
            self._continue_reading_host.padding = ft.Padding(8, 4, 8, 8)
            self._continue_reading_host.content = ft.Card(content=ft.Container(
                padding=12,
                on_click=lambda e, selected=recent: self._open_shelf_item(selected),
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE),
                        ft.Column([
                            ft.Text("继续阅读", size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT),
                            ft.Text(recent.get("name") or "未命名",
                                    weight=ft.FontWeight.BOLD),
                            ft.Text(recent_state.get("chapter_name") or "",
                                    size=12, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ], expand=True, spacing=1),
                        self._cache_status_control(recent, compact=True),
                        ft.Icon(ft.Icons.CHEVRON_RIGHT),
                    ]),
                    ft.ProgressBar(value=progress, height=3,
                                   semantics_label="全书阅读进度",
                                   semantics_value=round(progress * 100)),
                ], spacing=8)))
        list_controls = [self._shelf_control(item) for item in items]
        grid_controls = [self._shelf_grid_control(item) for item in items]
        if not self.service.bookshelf:
            list_controls = [self._empty_bookshelf_control()]
            grid_controls = [self._empty_bookshelf_control()]
        # Replacing the keyed collection root prevents Flet 0.86 from retaining
        # the old tail item when two navigation/render updates arrive close
        # together. Child keys alone are insufficient for this client diff case.
        self._bookshelf_render_generation += 1
        generation = self._bookshelf_render_generation
        self._bookshelf = ft.ListView(
            key=f"bookshelf-list-root-{generation}", expand=True, spacing=4,
            controls=list_controls)
        self._bookshelf_grid = ft.GridView(
            key=f"bookshelf-grid-root-{generation}", expand=True,
            max_extent=190, child_aspect_ratio=0.72, spacing=8, run_spacing=8,
            controls=grid_controls)
        self._bookshelf_host.controls[-1] = (
            self._bookshelf_grid if self._bookshelf_layout == "grid"
            else self._bookshelf)
        # Only the visible line is replaced here: a running background task
        # keeps its own text in `_background_status` so did_mount can put it
        # back when the tab returns.
        self._status.value = (f"书架中有 {len(self.service.bookshelf)} 本书"
                              + (f"，当前显示 {len(items)} 本" if query else ""))

    @staticmethod
    def _empty_bookshelf_control():
        return ft.Container(
            padding=32,
            content=ft.Column([
                ft.Icon(ft.Icons.MENU_BOOK, size=52, color=ft.Colors.GREY_400),
                ft.Text("书架还是空的", size=18),
                ft.Text("请到“书源 → 找书”中选择书籍并加入书架",
                        color=ft.Colors.GREY_600),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _filter_bookshelf(self, event):
        if getattr(event, "control", None) is self._bookshelf_query:
            self._bookshelf_filter_generation += 1
            try:
                self.page.run_task(
                    self._filter_bookshelf_after_delay,
                    self._bookshelf_filter_generation)
                return
            except RuntimeError:
                pass
        self._render_bookshelf()
        self.update()

    async def _filter_bookshelf_after_delay(self, generation):
        await asyncio.sleep(0.25)
        if generation != self._bookshelf_filter_generation:
            return
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
        total_chapters = max(0, int(item.get("totalChapterNum", 0) or 0))
        chapter_index = max(0, int(reading.get("chapter_index", 0) or 0))
        book_progress = (min(1.0, (chapter_index + 1) / total_chapters)
                         if total_chapters and reading.get("chapter_name") else 0.0)
        details = list(filter(None, [item.get("author"), item.get("source_name")]))
        if reading.get("chapter_name"):
            details.append(f"上次读到：{reading['chapter_name']}")
        if reading.get("scroll_ratio"):
            details.append(f"本章 {float(reading['scroll_ratio']):.0%}")
        unread = max(0, int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1)
        if unread:
            details.append(f"未读 {unread} 章")
        if self._shelf_source_missing(item):
            details.append("书源已失效")
        if item.get("updateError"):
            details.append("更新失败")
        cache_key = self._book_task_key(item)
        if cache_key in self._whole_book_tasks:
            details.append("正在缓存全本")
        elif item.get("cacheTotalChapterCount"):
            details.append(
                f"已缓存 {item.get('cachedChapterCount', 0)}/"
                f"{item.get('cacheTotalChapterCount', 0)} 章")
        return ft.GestureDetector(
            key=self._shelf_control_key(item, "list"),
            on_secondary_tap_down=lambda e, selected=item: self._show_shelf_manage(selected),
            on_long_press=lambda e, selected=item: self._show_shelf_manage(selected),
            content=ft.ListTile(
            leading=(ft.Image(src=item.get("cover_url"), width=42, height=56,
                              fit=ft.BoxFit.COVER)
                     if item.get("cover_url") else ft.Icon(ft.Icons.BOOK)),
            title=ft.Text(item.get("name") or "未命名"),
            subtitle=ft.Column([
                ft.Text(" · ".join(details)),
                ft.ProgressBar(
                    value=book_progress, height=2,
                    visible=book_progress > 0,
                    semantics_label="全书阅读进度",
                    semantics_value=round(book_progress * 100)),
            ], spacing=3),
            trailing=ft.Row([
                self._cache_status_control(item),
                ft.Container(
                    visible=unread > 0, padding=ft.Padding(7, 2, 7, 2),
                    border_radius=12, bgcolor=ft.Colors.PRIMARY_CONTAINER,
                    content=ft.Text(str(unread), size=11,
                                    color=ft.Colors.ON_PRIMARY_CONTAINER)),
                ft.IconButton(
                    ft.Icons.MORE_VERT, tooltip="书籍管理",
                    on_click=lambda e, selected=item: self._show_shelf_manage(selected)),
            ], tight=True),
            on_click=lambda e, selected=item: self._open_shelf_item(selected),
        ))

    def _shelf_grid_control(self, item):
        reading = self.service.reading_state(item) or {}
        total_chapters = max(0, int(item.get("totalChapterNum", 0) or 0))
        chapter_index = max(0, int(reading.get("chapter_index", 0) or 0))
        book_progress = (min(1.0, (chapter_index + 1) / total_chapters)
                         if total_chapters and reading.get("chapter_name") else 0.0)
        unread = max(0, int(item.get("totalChapterNum", 0) or 0) -
                     int(item.get("durChapterIndex", 0) or 0) - 1)
        cover = (ft.Image(src=item.get("cover_url"), height=150,
                          width=float("inf"), fit=ft.BoxFit.COVER)
                 if item.get("cover_url") else
                 ft.Container(height=150, alignment=ft.Alignment.CENTER,
                              content=ft.Icon(ft.Icons.BOOK, size=54)))
        return ft.GestureDetector(
            key=self._shelf_control_key(item, "grid"),
            on_secondary_tap_down=lambda e, selected=item: self._show_shelf_manage(selected),
            on_long_press=lambda e, selected=item: self._show_shelf_manage(selected),
            content=ft.Card(content=ft.Container(
            padding=8, on_click=lambda e, selected=item: self._open_shelf_item(selected),
            content=ft.Column([
                ft.Stack([cover, ft.Container(
                    visible=unread > 0, right=4, top=4, padding=5,
                    border_radius=12, bgcolor=ft.Colors.PRIMARY_CONTAINER,
                    content=ft.Text(str(unread), size=11,
                                    color=ft.Colors.ON_PRIMARY_CONTAINER))]),
                ft.Text(item.get("name") or "未命名", max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        weight=ft.FontWeight.BOLD),
                ft.Text(reading.get("chapter_name") or item.get("author") or "",
                        size=11, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ft.ProgressBar(
                    value=book_progress, height=2, visible=book_progress > 0,
                    semantics_label="全书阅读进度",
                    semantics_value=round(book_progress * 100)),
                self._cache_status_control(item, compact=True),
                ft.Row([ft.Text(self._shelf_source_label(item), size=10, expand=True,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.IconButton(ft.Icons.MORE_VERT, icon_size=18,
                                      on_click=lambda e, selected=item:
                                      self._show_shelf_manage(selected))], spacing=0),
            ], spacing=4))))

    def _cache_status_control(self, item, compact=False):
        key = self._book_task_key(item)
        cached = max(0, int(item.get("cachedChapterCount", 0) or 0))
        total = max(0, int(item.get("cacheTotalChapterCount", 0) or
                           item.get("totalChapterNum", 0) or 0))
        if key in self._whole_book_tasks:
            label, icon = "缓存中", ft.Icons.DOWNLOADING
            background, foreground = (ft.Colors.PRIMARY_CONTAINER,
                                      ft.Colors.ON_PRIMARY_CONTAINER)
        elif total > 0 and cached >= total:
            label, icon = "已离线", ft.Icons.DOWNLOAD_DONE
            background, foreground = (ft.Colors.PRIMARY_CONTAINER,
                                      ft.Colors.ON_PRIMARY_CONTAINER)
        elif cached > 0:
            label, icon = f"{cached}/{total or '?'}", ft.Icons.CLOUD_DOWNLOAD_OUTLINED
            background, foreground = (ft.Colors.SURFACE_CONTAINER,
                                      ft.Colors.PRIMARY)
        else:
            label, icon = "未缓存", ft.Icons.CLOUD_OUTLINED
            background, foreground = (ft.Colors.SURFACE_CONTAINER,
                                      ft.Colors.ON_SURFACE_VARIANT)
        return ft.Container(
            tooltip=(label if compact else f"离线状态：{label}"),
            padding=ft.Padding(6, 2, 6, 2), border_radius=10,
            bgcolor=background,
            content=ft.Row(tight=True, spacing=3, controls=[
                ft.Icon(icon, size=13, color=foreground),
                ft.Text(label, size=10, color=foreground),
            ]))

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
        record = self.service.find_bookshelf_item(item) or item
        self._cancel_whole_book_cache(record)
        key = self.service.bookshelf_key(record)
        self.service.remove_from_bookshelf(*key)
        self.reading_cache.remove_book(*key)
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
                ft.Text(f"书源：{self._shelf_source_label(item) or '未知'}"),
                ft.Text("原书源已失效，请先切换书源", color=ft.Colors.RED_600,
                        visible=self._shelf_source_missing(item)),
                ft.Text(f"阅读进度：{reading.get('chapter_name') or '尚未阅读'}"),
                ft.Text(f"目录：{item.get('totalChapterNum', 0)} 章 · 未读 {unread} 章"),
                ft.Text(
                    f"离线缓存：{item.get('cachedChapterCount', 0)}/"
                    f"{item.get('cacheTotalChapterCount', 0) or item.get('totalChapterNum', 0)} 章"),
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
                ft.TextButton(
                    "停止缓存" if self._book_task_key(item) in self._whole_book_tasks
                    else "缓存全本",
                    icon=(ft.Icons.STOP if self._book_task_key(item) in self._whole_book_tasks
                          else ft.Icons.DOWNLOAD),
                    on_click=lambda e, selected=item:
                    self._toggle_whole_book_cache(selected)),
                ft.TextButton("保存分组", on_click=lambda e, selected=item,
                              field=group_input: self._save_shelf_group(selected, field)),
                ft.TextButton("刷新目录", on_click=lambda e, selected=item:
                              self._refresh_one_from_dialog(selected)),
                ft.Button("移出书架", color=ft.Colors.RED,
                          on_click=lambda e, selected=item:
                          self._remove_from_manage(selected)),
            ])
        self.page.show_dialog(dialog)

    def _shelf_source(self, item):
        """A shelf entry's live book source, or None once it left the list."""
        key = str(item.get("source_url") or item.get("origin") or "")
        if not key:
            return None
        return next((source for source in self.service.sources
                     if source.bookSourceUrl == key), None)

    def _shelf_source_missing(self, item) -> bool:
        """True when the origin is gone, e.g. after importing another corpus."""
        return self._shelf_source(item) is None

    def _shelf_source_label(self, item) -> str:
        name = str(item.get("source_name") or "")
        if not self._shelf_source_missing(item):
            return name
        return f"{name}（已失效）" if name else "书源已失效"

    def _switch_candidates(self, item) -> list:
        """候选书源：同站同域名的排最前，检测已判失效的排最后。"""
        book_url = item.get("url") or item.get("bookUrl")
        hosts = {host for host in (
            _host_of(item.get("source_url") or item.get("origin")),
            _host_of(book_url)) if host}
        tokens = {token for token in (
            _site_token(item.get("source_url") or item.get("origin")),
            _site_token(book_url)) if token}
        origin = str(item.get("source_url") or item.get("origin") or "")

        def priority(source):
            host = _host_of(source.bookSourceUrl)
            score = 4 if host and host in hosts else 0
            if _site_token(source.bookSourceUrl) in tokens:
                score += 2
            if _source_judged_invalid(source):
                score -= 3
            return (-score, str(source.bookSourceName or "").casefold())

        return sorted(
            (source for source in self.service.sources
             if getattr(source, "enabled", False) and source.searchUrl and
             source.bookSourceUrl != origin),
            key=priority)

    def _switch_start_hint(self, item) -> str:
        candidates = self._switch_candidates(item)
        if not candidates:
            return "没有其他可搜索的书源，请先导入或启用书源"
        return (f"将搜索全部 {len(candidates)} 个已启用书源，"
                "同站同域名的排在最前。")

    def _show_source_switch(self, item):
        self.page.pop_dialog()
        self._source_switch_query = ft.TextField(
            label="搜索书名", value=item.get("name") or "",
            autofocus=True, on_submit=lambda e: self._start_source_switch(item))
        self._source_switch_status = ft.Text(
            self._switch_start_hint(item), color=ft.Colors.GREY_600)
        self._source_switch_priority = {
            str(source.bookSourceUrl or ""): index
            for index, source in enumerate(self._switch_candidates(item))}
        self._source_switch_results = ft.ListView(expand=True, spacing=4)
        self._source_switch_progress = ft.ProgressBar(value=0, visible=False)
        self._source_switch_stop = ft.TextButton(
            "停止搜索", icon=ft.Icons.STOP, visible=False,
            on_click=self._stop_source_switch)
        self._source_switch_search = ft.Button(
            "搜索其他书源", icon=ft.Icons.SEARCH,
            on_click=lambda e: self._start_source_switch(item))
        self._source_switch_dialog = ft.AlertDialog(
            modal=True, title=ft.Text("切换书源"),
            content=ft.Container(width=620, height=520, content=ft.Column([
                self._source_switch_query,
                self._source_switch_status,
                self._source_switch_progress,
                ft.Divider(height=1),
                self._source_switch_results,
            ], spacing=8)),
            actions=[
                ft.TextButton("关闭", on_click=self._close_source_switch),
                self._source_switch_stop,
                self._source_switch_search,
            ])
        self.page.show_dialog(self._source_switch_dialog)
        self._seed_source_switch(item)

    def _seed_source_switch(self, item):
        """打开对话框先摆上本地缓存的结果，Legado 换源列表就是这么秒开的。

        缓存来自之前的整本搜索（找书或换源），站点可达性未知，所以只当候选；
        用户点“搜索其他书源”才会真的联网核对，并把这一轮结果覆盖回去。
        """
        try:
            stored = self._search_store().list_by_name_author(
                str(item.get("name") or ""), str(item.get("author") or ""))
        except Exception:
            return
        origin = str(item.get("source_url") or item.get("origin") or "")
        seeded = []
        for book in stored:
            source = self._source_by_url(getattr(book, "origin", ""))
            if source is None or not getattr(source, "enabled", False):
                continue
            if not str(getattr(source, "searchUrl", "") or "").strip():
                continue
            if str(source.bookSourceUrl or "") == origin:
                continue
            seeded.append((source, book))
        if not seeded:
            return
        self._render_source_switch_results(item, seeded)
        self._source_switch_status.value = (
            f"本地已有 {len(seeded)} 个结果，点“搜索其他书源”可联网核对")

    def _set_switch_running(self, running):
        if self._source_switch_stop is not None:
            self._source_switch_stop.visible = running
        if self._source_switch_search is not None:
            # 搜索期间再点一次没有意义（会被直接忽略），不如让位给停止按钮。
            self._source_switch_search.visible = not running
        if self._source_switch_progress is not None:
            self._source_switch_progress.visible = running
            if running:
                self._source_switch_progress.value = 0

    def _stop_source_switch(self, _=None):
        if self._source_switch_task and not self._source_switch_task.done():
            self._source_switch_task.cancel()
        self._source_switch_task = None
        # 这一轮的结果不再属于界面，工作协程也该停手。
        self._source_switch_generation += 1
        self._source_switch_status.value = "搜索已停止"
        self._set_switch_running(False)
        self.update()

    def _close_source_switch(self, _=None):
        if self._source_switch_task and not self._source_switch_task.done():
            self._source_switch_task.cancel()
        self._source_switch_task = None
        self._source_switch_generation += 1
        self.page.pop_dialog()

    def _start_source_switch(self, item):
        if self._source_switch_task and not self._source_switch_task.done():
            return
        keyword = str(self._source_switch_query.value or "").strip()
        if not keyword:
            self._source_switch_status.value = "请输入要搜索的书名"
            self.update()
            return
        batch = self._switch_candidates(item)
        self._source_switch_priority = {
            str(source.bookSourceUrl or ""): index
            for index, source in enumerate(batch)}
        self._source_switch_generation += 1
        self._set_switch_running(True)
        self._source_switch_task = self.page.run_task(
            self._run_source_switch, item, keyword, batch,
            self._source_switch_generation)
        self.update()

    def _switch_status_text(self, completed, total, matches, failures,
                            *, running, elapsed=0.0, noun="个结果"):
        if running:
            head = f"正在搜索 {completed}/{total} 个书源"
            if completed and elapsed >= 1:
                # 剩余量按已完成的平均速度外推；开头几个还没跑完时给不出估计。
                remaining = elapsed / completed * (total - completed)
                head += f"（已用 {_clock(elapsed)}，预计还需 {_clock(remaining)}）"
        else:
            head = f"搜索完成：检查 {total} 个书源"
            if elapsed >= 1:
                head += f"，用时 {_clock(elapsed)}"
        text = f"{head}，找到 {len(matches)} {noun}"
        failed = sum(failures.values())
        if failed:
            detail = "、".join(f"{reason} {count}"
                               for reason, count in failures.most_common(3))
            text += f"（{failed} 个书源搜索失败：{detail}）"
        return text

    async def _run_source_switch(self, item, keyword, candidates, generation):
        if generation != self._source_switch_generation:
            return
        matches = []
        failures = collections.Counter()
        completed = 0
        rendered_matches = -1
        started_at = time.monotonic()
        queue = asyncio.Queue()
        for source in candidates:
            queue.put_nowait(source)
        if self._source_switch_results is not None:
            self._source_switch_results.controls.clear()
        self._source_switch_status.value = self._switch_status_text(
            0, len(candidates), matches, failures, running=True)
        self.update()

        def publish(finished):
            nonlocal rendered_matches
            if generation != self._source_switch_generation:
                return
            # 结果列表只跟命中数有关：没有新命中就不重建上百个 ListTile。
            if len(matches) != rendered_matches:
                rendered_matches = len(matches)
                self._render_source_switch_results(item, matches)
            self._source_switch_status.value = self._switch_status_text(
                completed, len(candidates), matches, failures,
                running=not finished, elapsed=time.monotonic() - started_at)
            if self._source_switch_progress is not None:
                self._source_switch_progress.value = (
                    completed / len(candidates) if candidates else 0)
            self.update()

        async def worker():
            nonlocal completed, last_publish
            while True:
                try:
                    source = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                engine = self._background_engine(
                    timeout=SWITCH_TIMEOUT,
                    connect_timeout=SWITCH_CONNECT_TIMEOUT)
                try:
                    books = await engine.search(source, keyword, 1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 静默吞掉失败会让"找不到书"和"书源坏了"看起来一样。
                    failures[_switch_failure_reason(exc)] += 1
                else:
                    if books:
                        # 落库和界面筛选无关：这一轮没命中的书，下次换源还用得上。
                        await asyncio.to_thread(
                            self._search_store().insert, books)
                    matches.extend(
                        (source, book) for book in books
                        if keyword.casefold() in str(book.name or "").casefold())
                finally:
                    await engine.aclose()
                    completed += 1
                    # 全量书源可能有上千个，逐条重绘结果列表会把界面拖住。
                    now = time.monotonic()
                    if (completed == len(candidates) or
                            now - last_publish >= SWITCH_PUBLISH_INTERVAL):
                        last_publish = now
                        publish(False)

        last_publish = time.monotonic()
        workers = [asyncio.create_task(worker())
                   for _ in range(min(SWITCH_CONCURRENCY, len(candidates)))]
        try:
            try:
                await asyncio.gather(*workers)
            finally:
                # 关闭对话框或选中结果都会取消父任务；工作协程必须一起停，
                # 否则它们会继续联网并把结果写回已经消失的列表。
                for task in workers:
                    if not task.done():
                        task.cancel()
            publish(True)
        finally:
            if generation == self._source_switch_generation:
                self._source_switch_task = None
                self._set_switch_running(False)
            self.update()

    def _render_source_switch_results(self, item, matches):
        """Legado 换源列表的排序：书名对得上最前，其次作者，再次书源优先级。"""
        wanted_name = str(item.get("name") or "").casefold()
        wanted_author = str(item.get("author") or "").casefold()
        priority = getattr(self, "_source_switch_priority", {}) or {}

        def rank(pair):
            source, book = pair
            exact_name = str(book.name or "").casefold() == wanted_name
            # Legado 默认不校验作者，只在作者能对上时把它当加分项。
            same_author = bool(wanted_author) and wanted_author in str(
                book.author or "").casefold()
            return (not exact_name, not same_author,
                    priority.get(str(source.bookSourceUrl or ""), len(priority)),
                    str(source.bookSourceName or "").casefold())

        ordered = sorted(matches, key=rank)
        self._source_switch_results.controls = [ft.ListTile(
            leading=ft.Icon(ft.Icons.LANGUAGE),
            title=ft.Text(book.name or "未命名"),
            subtitle=ft.Text(" · ".join(filter(None, [
                source.bookSourceName, _host_of(source.bookSourceUrl),
                book.author, book.last_chapter]))),
            trailing=ft.Button(
                "切换", on_click=lambda e, selected_source=source,
                selected_book=book: self._confirm_source_switch(
                    item, selected_source, selected_book)),
        ) for source, book in ordered[:100]]

    def _confirm_source_switch(self, item, source, book):
        if self._source_switch_task and not self._source_switch_task.done():
            self._source_switch_task.cancel()
        self._source_switch_task = None
        self._source_switch_generation += 1
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
        # 对话期间书架可能已经被重载或重绘，写入必须落在在架记录上。
        record = self.service.find_bookshelf_item(item)
        if record is None:
            self.page.pop_dialog()
            self._render_bookshelf()
            self.update()
            self._show_notice("切换失败：这本书已不在书架中")
            return
        old_source = str(record.get("source_url") or "")
        old_url = str(record.get("url") or "")
        self._cancel_whole_book_cache(record)
        try:
            switched = self.service.switch_bookshelf_source(record, source, book)
        except Exception as exc:
            self.page.pop_dialog()
            self._render_bookshelf()
            self.update()
            self._show_error(exc, label="切换书源失败")
            return
        if switched:
            self.reading_cache.remove_book(old_source, old_url)
            message = f"已切换到书源：{source.bookSourceName}"
        else:
            message = "切换失败：书架中已有这本书的该书源版本"
        self.page.pop_dialog()
        self._render_bookshelf()
        self.update()
        # 书架重绘会覆盖状态行，提示必须放在重绘之后。
        self._show_notice(message)

    def _save_shelf_group(self, item, field):
        self.service.update_bookshelf_options(
            item.get("source_url", ""), item.get("url", ""),
            groupName=str(field.value or "").strip())
        self.page.pop_dialog()
        self._render_bookshelf()
        self.update()

    def _clear_shelf_cache(self, item):
        task = self._whole_book_tasks.get(self._book_task_key(item))
        if task and not task.done():
            task.cancel()
        self.reading_cache.remove_book(
            item.get("source_url", ""), item.get("url", ""))
        self.page.pop_dialog()
        self.service.update_bookshelf_options(
            item.get("source_url", ""), item.get("url", ""),
            cachedChapterCount=0, cacheTotalChapterCount=0,
            cacheErrorCount=0, cacheUpdatedAt=0)
        self._status.value = f"《{item.get('name') or ''}》缓存已清除"
        self._render_bookshelf()
        self.update()

    @staticmethod
    def _book_task_key(item):
        return (str(item.get("source_url") or ""), str(item.get("url") or ""))

    def _cancel_whole_book_cache(self, item):
        task = self._whole_book_tasks.get(self._book_task_key(item))
        if task and not task.done():
            task.cancel()

    async def _restart_whole_book_cache(self, item):
        """Retry entry point: run_task rejects the synchronous toggle handler."""
        key = self._book_task_key(item)
        task = self._whole_book_tasks.get(key)
        if task and not task.done():
            return
        self._whole_book_tasks[key] = self.page.run_task(
            self._run_whole_book_cache, item)
        self._render_bookshelf()
        self.update()

    def _toggle_whole_book_cache(self, item):
        self.page.pop_dialog()
        key = self._book_task_key(item)
        task = self._whole_book_tasks.get(key)
        if task and not task.done():
            task.cancel()
            self._set_busy(f"正在停止《{item.get('name') or ''}》缓存…")
        else:
            self._whole_book_tasks[key] = self.page.run_task(
                self._run_whole_book_cache, item)
        self._render_bookshelf()
        self.update()

    async def _run_whole_book_cache(self, item):
        key = self._book_task_key(item)
        source = next((value for value in self.service.sources
                       if value.bookSourceUrl == item.get("source_url")), None)
        if source is None:
            self._show_notice("缓存失败：原书源不存在")
            self._whole_book_tasks.pop(key, None)
            self.update()
            return
        book = self._book_from_shelf(item)
        last_display = [-1]

        def progress_changed(progress):
            if (progress.cached + progress.failed == progress.total or
                    progress.cached + progress.failed - last_display[0] >= 10):
                last_display[0] = progress.cached + progress.failed
                self._set_busy(
                    f"正在缓存《{book.name}》：{progress.cached}/"
                    f"{progress.total}，失败 {progress.failed}"
                    + (f" · {progress.current}" if progress.current else ""))

        self._set_busy(f"正在准备《{book.name}》目录与离线缓存…")
        try:
            result = await self.whole_book_cache.cache_book(
                source, book, progress_changed)
            self.service.update_bookshelf_options(
                *key, cachedChapterCount=result.cached,
                cacheTotalChapterCount=result.total,
                cacheErrorCount=result.failed, cacheUpdatedAt=int(time.time()))
            self._set_status(
                f"《{book.name}》缓存完成：{result.cached}/{result.total} 章"
                + (f"，失败 {result.failed} 章，可再次缓存重试" if result.failed else ""))
        except asyncio.CancelledError:
            cached, total = self.reading_cache.cache_counts(*key)
            self.service.update_bookshelf_options(
                *key, cachedChapterCount=cached,
                cacheTotalChapterCount=total)
            self._set_status(f"《{book.name}》缓存已停止：{cached}/{total} 章")
        except Exception as exc:
            self._show_error(
                exc, label=f"《{book.name}》缓存失败",
                retry=lambda: self.page.run_task(
                    self._restart_whole_book_cache, item))
        finally:
            self._whole_book_tasks.pop(key, None)
            self._background_status = None
            self._render_bookshelf()
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
                self._set_busy(
                    f"正在刷新书架 {index}/{len(candidates)}："
                    f"{item.get('name') or '未命名'}")
                await self._refresh_shelf_item(item, False)
        finally:
            self._bookshelf_refresh_btn.disabled = False
            self._background_status = None
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
        update_engine = self._background_engine()
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
                self._set_status(f"《{book.name}》目录已更新，共 {len(chapters)} 章")
        except Exception as exc:
            self.service.update_bookshelf_check(
                source.bookSourceUrl, book.url, error=str(exc))
            if show_result:
                self._show_error(
                    exc, retry=lambda: self.page.run_task(
                        self._refresh_shelf_item, item, show_result))
        finally:
            await update_engine.aclose()
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
        source = self._shelf_source(item)
        if source is None:
            # 死路会让人以为书废了：这里直接给出换源入口。
            self._show_notice(
                "原书源已失效，无法加载这本书，请先切换书源",
                action="切换书源",
                on_action=lambda: self._show_source_switch(item))
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
        self._restoring_scroll = False
        self.engine.current_book = None
        self.engine.js_variables.clear()
        self._current_book = None
        self._current_chapter = None
        self.chapters = []
        self._visible_chapters = []
        self._continuous_loaded.clear()
        self._pending_append_starts.clear()
        self._pending_scroll_offset = 0.0
        self._pending_scroll_ratio = 0.0
        self._pending_document_offset = 0.0
        self._pending_scroll_maximum = 0.0
        self.reading_session.reset()
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
        self._detail_intro.value = _intro_text(book.intro) or "暂无简介"
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
        self._set_busy(f"正在打开《{book.name}》")

    def _cancel_shelf_open(self, _):
        self._book_open_generation += 1
        if self._book_open_task and not self._book_open_task.done():
            self._book_open_task.cancel()
        self._back_to_shelf(None)

    def _show_section(self, section):
        self._section = section
        if section == "reader" and self._is_desktop_reader():
            # Keep desktop controls in a stable layout. Showing/hiding this
            # row changes the viewport height and can make Flet restore the
            # offset captured when the book was first opened.
            self._reader_toolbar.visible = True
        self._shelf_page.visible = section == "shelf"
        self._detail_page.visible = section == "detail"
        self._reader_page.visible = section == "reader"
        if self._on_section_change:
            self._on_section_change(section)
        if hasattr(self, "_page_title"):
            self._reading_header.visible = section != "reader"
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
        # The detail page can stay mounted for the whole reading session. Do
        # not reuse the snapshot captured when the book was first opened.
        self._refresh_detail_resume()
        chapter = self._resume_chapter(self._detail_resume)
        if chapter is None:
            chapter = next((item for item in self.chapters
                            if not item.is_volume and item.url), None)
        if chapter is not None and not self._reading_loading:
            offset = (float((self._detail_resume or {}).get("scroll_offset") or 0)
                      if self._resume_chapter(self._detail_resume) is chapter else 0)
            self.page.run_task(self._run_content, chapter, False, offset)

    def _append_book_controls(self, flow, books):
        """追加新结果，只替换列表末尾的翻页提示。"""
        controls = flow.list_view.controls
        if controls and getattr(controls[-1], "data", None) == "result-footer":
            controls.pop()
        controls.extend(self._book_control(book) for book in books)
        self._append_result_footer(flow)

    def _on_browse_scroll(self, e):
        self._maybe_load_more(e, self._browse_flow)

    def _on_search_scroll(self, e):
        self._maybe_load_more(e, self._search_flow)

    def _maybe_load_more(self, e, flow):
        pixels = getattr(e, "pixels", 0) or 0
        maximum = getattr(e, "max_scroll_extent", 0) or 0
        if (flow.has_more and not flow.loading_more and maximum and
                maximum - pixels < 320):
            self._load_more(flow)

    def _load_more(self, flow, e=None):
        if flow.loading_more or not flow.has_more:
            return
        if flow is self._browse_flow:
            self.page.run_task(self._run_explore, flow.category, False)
        else:
            self._load_more_search()

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
                self._show_error(
                    exc, retry=lambda: self.page.run_task(
                        self._run_chapters, book, None, True))
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
        self._pending_append_starts.clear()
        self.reading_session.bind(
            self.current_source.bookSourceUrl, book.url, chapters,
            self._detail_resume)
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
        self._detail_intro.value = _intro_text(book.intro) or "暂无简介"
        if book.cover_url:
            self._detail_cover.content = ft.Image(
                src=book.cover_url, width=76, height=104,
                fit=ft.BoxFit.COVER,
                error_content=ft.Icon(ft.Icons.BROKEN_IMAGE))
        self._set_status(f"《{book.name}》共 {len(chapters)} 章，"
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
        self._refresh_detail_resume()
        saved_chapter = self._resume_chapter(self._detail_resume)
        restore_offset = (
            float((self._detail_resume or {}).get("scroll_offset", 0) or 0)
            if saved_chapter is chapter else 0.0)
        self.page.run_task(
            self._run_content, chapter, False, restore_offset)

    async def _run_content(self, chapter, append=False, restore_offset=0.0,
                           open_generation=None):
        if (open_generation is not None and
                open_generation != self._book_open_generation):
            return
        if chapter.is_volume:
            return
        if self._current_chapter is not None and self._current_chapter is not chapter:
            self._persist_scroll_position()
            self._pending_scroll_offset = 0.0
            self._pending_scroll_ratio = 0.0
        self._cancel_prefetch()
        # 后发起的加载接管界面，先发起的那次静默让位（它的请求照常落地进缓存）。
        self._content_generation += 1
        generation = self._content_generation
        self._reading_loading = True
        self._restoring_scroll = not append
        self._set_busy(f"正在加载 {chapter.name}…")
        try:
            next_chapter_url = ""
            if 0 <= chapter.index < len(self.chapters) - 1:
                next_chapter_url = next((item.url for item in self.chapters[chapter.index + 1:]
                                         if not item.is_volume and item.url), "")
            content, content_from_cache = await self._chapter_document(
                chapter, next_chapter_url)
            if (generation != self._content_generation or
                    (open_generation is not None and
                     open_generation != self._book_open_generation)):
                return
            rendered = await self._render_content(content)
            if generation != self._content_generation:
                # 切段在工作线程里跑，回来时可能已经被更晚的翻页顶掉。
                return
            chapter_text_length = sum(
                len(str(control.value or ""))
                for control in rendered
                if getattr(control, "data", None) in
                {"reader-text", "reader-heading"})
            if append:
                chapter_start = float(
                    self._pending_append_starts.pop(
                        chapter.url, self._pending_document_offset))
                self.reading_session.mount(
                    chapter, chapter_start, chapter_text_length)
                self._content_host.controls.extend([
                    ft.Divider(height=32),
                    ft.Text(chapter.name, size=22, weight=ft.FontWeight.BOLD),
                    *rendered,
                ])
            else:
                self._title.value = content.title or chapter.name
                self._content_host.controls = rendered
                self._continuous_loaded = {chapter.url}
                self.reading_session.reset(
                    self.current_source.bookSourceUrl,
                    self._current_book.url, self.chapters,
                    self.reading_session.position)
                self.reading_session.mount(
                    chapter, 0.0, chapter_text_length)
                self._auto_scroll_armed = True
            if not self._content_host.controls:
                self._content_host.controls = [ft.Text("正文解析结果为空")]
            self._continuous_loaded.add(chapter.url)
            self._current_chapter = chapter
            saved_position = (
                self._detail_resume
                if self._resume_chapter(self._detail_resume) is chapter else {})
            self.reading_session.enter(
                chapter, self._font_size,
                offset=(0.0 if append else float(restore_offset or 0)),
                ratio=(0.0 if append else float(
                    (saved_position or {}).get("scroll_ratio", 0) or 0)),
                character_offset=(0 if append else int(
                    (saved_position or {}).get("chapter_pos", 0) or 0)))
            current_book = getattr(self.engine, "current_book", None)
            if current_book is not None:
                current_book.variables = dict(self.engine.js_variables)
                self.service.update_bookshelf_state(
                    self.current_source.bookSourceUrl, current_book.url,
                    variables=current_book.variables, book=current_book)
            self.progress_store.save(
                self.reading_session.position, chapter, len(self.chapters))
            self._update_chapter_navigation()
            self._set_status(f"{chapter.name} · 已从缓存打开"
                             if content_from_cache else chapter.name)
            self._show_section("reader")
        except Exception as exc:
            if generation == self._content_generation:
                label, action = self._repair_action(exc, chapter)
                self._show_error(
                    exc, retry=lambda: self._load_content(chapter),
                    action=label, on_action=action)
        if generation != self._content_generation:
            # 更晚的加载已经接管界面：这次既不回写状态也不滚动。
            return
        # Mount the new document while input remains locked. Restoring the
        # saved offset after unlocking lets an early wheel event race with the
        # final scroll_to(0), which visibly snaps the chapter back to its top.
        try:
            self.update()
            if not append:
                target_offset = max(0.0, float(restore_offset or 0))
                self._pending_scroll_offset = target_offset
                self._pending_document_offset = target_offset
                # A frozen desktop build may acknowledge the control update
                # before Flutter has measured the newly mounted paragraphs.
                # Yield one frame so a non-zero target is not clamped to the
                # temporary zero scroll extent.
                if target_offset:
                    await asyncio.sleep(0.05)
                await self._scroll_reader_to(target_offset)
                if target_offset:
                    # Paragraph/image measurement can continue after the first
                    # frame in a frozen desktop build. Confirm once more after
                    # layout settles so the target cannot be clamped to zero.
                    await asyncio.sleep(0.15)
                    await self._scroll_reader_to(target_offset)
        finally:
            self._restoring_scroll = False
            self._reading_loading = False
            self.update()
        if self._current_chapter is chapter and self._ui_alive():
            self._prefetch_task = self.page.run_task(
                self._prefetch_next_chapters, chapter)

    def _content_key(self, chapter):
        book = self._current_book
        return (getattr(self.current_source, "bookSourceUrl", ""),
                getattr(book, "url", ""), chapter.url)

    def _remember_document(self, key, document):
        """Keep a fetched chapter in memory while the prefetch budget allows."""
        size = self._document_size(document)
        cached = sum(self._document_size(item)
                     for item in self._chapter_cache.values())
        if cached + size > self.PREFETCH_CACHE_LIMIT:
            return
        self._chapter_cache[key] = document

    async def _chapter_document(self, chapter, next_chapter_url="", lease=None):
        """The document for one chapter, requested at most once at a time.

        翻页时旧请求不会被取消：它跑完照样写进缓存，只有渲染被放弃——这正是
        “回到这一章还在”的那份数据。
        """
        key = self._content_key(chapter)
        cached = self._chapter_cache.get(key)
        if cached is not None:
            return cached, True
        pending = self._content_flight.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch_chapter_document(
                chapter, next_chapter_url,
                lease.engine if lease is not None else self.engine, key, lease))
            self._content_flight[key] = pending
            pending.add_done_callback(
                lambda task, key=key: self._forget_content_flight(key, task))
        # shield：等待方被取消不该把请求一起带走。
        return await asyncio.shield(pending)

    async def _fetch_chapter_document(self, chapter, next_chapter_url,
                                      engine, key, lease=None):
        source = self.current_source
        book = self._current_book
        if lease is not None:
            lease.acquire()
        try:
            if book is not None:
                document = await asyncio.to_thread(
                    self.reading_cache.load_content,
                    source.bookSourceUrl, book.url, chapter.url)
                if document is not None:
                    self._remember_document(key, document)
                    return document, True
            document = await engine.content_document(
                source, chapter, next_chapter_url)
            if book is not None:
                await asyncio.to_thread(
                    self.reading_cache.save_content,
                    source.bookSourceUrl, book.url, chapter.url, document)
            self._remember_document(key, document)
            return document, False
        finally:
            if lease is not None:
                await lease.release()

    def _forget_content_flight(self, key, task):
        if self._content_flight.get(key) is task:
            self._content_flight.pop(key, None)
        if not task.cancelled():
            # 没人等在结果上时也要取走异常，否则 asyncio 会记一条警告。
            task.exception()

    def _cancel_content_flight(self):
        for task in self._content_flight.values():
            task.cancel()
        self._content_flight.clear()

    def _cancel_prefetch(self, clear_cache=False):
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None
        if clear_cache:
            self._chapter_cache.clear()
            self._cancel_content_flight()

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
        """顺序预取后续章节，避免并发修改 Legado 规则变量。

        预取任务本身可以被放弃，它发出的请求不行：请求交给带引用计数的引擎，
        跑完照样进缓存，阅读页翻到这一章时直接接上这份结果。
        """
        source = self.current_source
        candidates = self._next_readable_chapters(chapter, self.PREFETCH_CHAPTER_COUNT)
        lease = _EngineLease(self._background_engine())
        lease.engine.current_book = getattr(self.engine, "current_book", None)
        lease.engine.js_variables.update(self.engine.js_variables)
        # 自己也算一个持有者：任务被取消时不能让计数归零，把还在跑的请求的
        # 引擎顺手关掉。
        lease.acquire()
        try:
            for candidate in candidates:
                if (source is not self.current_source or
                        self._content_key(candidate) in self._chapter_cache):
                    continue
                following = self._next_readable_chapters(candidate, 1)
                next_url = following[0].url if following else ""
                await self._chapter_document(candidate, next_url, lease=lease)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 预加载失败不能影响当前章节阅读，实际翻页时仍会正常请求。
            return
        finally:
            await lease.release()

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
            percent = int(position * 100 / max(1, len(self.chapters)))
            self._reader_status_bar.content.value = (
                f"{self._current_chapter.name} · {position}/{len(self.chapters)} · {percent}%")
        else:
            self._reader_progress.value = "未选择章节"
            self._reader_status_bar.content.value = ""

    def _change_font_size(self, delta):
        new_size = max(13, min(28, self._font_size + int(delta)))
        if new_size == self._font_size:
            return
        self._reader_preferences["font_size"] = new_size
        self._apply_reader_preferences()
        self.update()

    def _toggle_reader_chrome(self, _=None):
        self._reader_toolbar.visible = not self._reader_toolbar.visible
        # Updating the complete reading view can cause Flet to reconcile the
        # scrollable document and restore an older offset.  The tap only
        # changes this overlay, so keep the update local to the toolbar.
        try:
            self._reader_toolbar.update()
        except RuntimeError:
            # Unit tests and the short interval before the first mount do not
            # yet have a page-bound toolbar.
            pass

    def _is_desktop_reader(self):
        return platform_support.is_desktop_page(
            platform_support.attached_page(self))

    def _on_reader_tap(self, event):
        # A desktop primary-button click is commonly used to select text or
        # reveal controls.  Treating the left/right part of the document as a
        # chapter button makes an ordinary click unexpectedly replace the
        # continuously appended document with the previous chapter.
        if self._is_desktop_reader():
            # Do not mutate any desktop reader layout from a primary click.
            # The toolbar is permanently available on desktop.
            return
        position = getattr(event, "local_position", None)
        x = float(getattr(position, "x", 0) or 0)
        # local_position is relative to the reading document, not the page.
        width = float(self._reader_document.width or self.page.width or 430)
        if x and x < width * 0.22:
            self._previous_chapter(None)
        elif x and x > width * 0.78:
            self._next_chapter(None)
        else:
            self._toggle_reader_chrome()

    def _open_reader_directory(self, _=None):
        if not self.chapters:
            return
        query = ft.TextField(
            hint_text="搜索章节", prefix_icon=ft.Icons.SEARCH,
            autofocus=True, dense=True)
        chapter_list = ft.ListView(height=440, spacing=1)
        title = ft.Text(f"目录 · {len(self.chapters)} 章")

        def choose(chapter):
            self.page.pop_dialog()
            self._reader_dialog = None
            self._load_content(chapter)

        def render(_=None):
            keyword = str(query.value or "").strip().lower()
            chapters = [item for item in self.chapters
                        if not item.is_volume and
                        (not keyword or keyword in item.name.lower())]
            if self._directory_reversed:
                chapters.reverse()
            chapter_list.controls = [ft.ListTile(
                dense=True, title=ft.Text(item.name),
                selected=item is self._current_chapter,
                leading=(ft.Icon(ft.Icons.PLAY_ARROW, size=18)
                         if item is self._current_chapter else None),
                on_click=lambda e, selected=item: choose(selected))
                for item in chapters[:500]]
            if self._reader_dialog:
                title.value = f"目录 · {len(chapters)} 章"
                try:
                    self._reader_dialog.update()
                except RuntimeError:
                    pass

        query.on_change = render
        reverse = ft.IconButton(
            ft.Icons.SWAP_VERT, tooltip="正序/倒序",
            on_click=lambda e: self._toggle_reader_dialog_order(render))
        panel = ft.Container(
            width=460, padding=12, content=ft.Column([
                ft.Row([title, ft.IconButton(
                    ft.Icons.CLOSE, tooltip="关闭",
                    on_click=self._close_reader_dialog)]),
                ft.Row([query, reverse]), chapter_list,
            ], tight=True))
        if float(self.page.width or 430) < 600:
            self._reader_dialog = ft.BottomSheet(
                content=panel, show_drag_handle=True, use_safe_area=True)
        else:
            self._reader_dialog = ft.AlertDialog(
                modal=False, alignment=ft.Alignment.CENTER_RIGHT,
                inset_padding=ft.Padding(24, 24, 24, 24), content=panel)
        render()
        self.page.show_dialog(self._reader_dialog)

    def _toggle_reader_dialog_order(self, render):
        self._directory_reversed = not self._directory_reversed
        render()

    def _open_reader_settings(self, _=None):
        preferences = self._reader_preferences

        def slider(label, key, minimum, maximum, divisions):
            return ft.Column([
                ft.Text(label, size=12),
                ft.Slider(
                    value=float(preferences[key]), min=minimum, max=maximum,
                    divisions=divisions, label="{value}",
                    on_change=lambda e, name=key: self._set_reader_preference(
                        name, float(e.control.value))),
            ], spacing=0)

        width = ft.Dropdown(
            label="正文宽度", value=str(int(preferences["max_width"])),
            options=[ft.dropdown.Option("620", "窄"),
                     ft.dropdown.Option("700", "标准"),
                     ft.dropdown.Option("760", "宽")],
            on_select=lambda e: self._set_reader_preference(
                "max_width", float(e.control.value)))
        alignment = ft.Dropdown(
            label="文本对齐", value=preferences["text_align"],
            options=[ft.dropdown.Option("start", "左对齐"),
                     ft.dropdown.Option("justify", "两端对齐")],
            on_select=lambda e: self._set_reader_preference(
                "text_align", e.control.value))
        background = ft.Dropdown(
            label="阅读背景", value=preferences["background"],
            options=[ft.dropdown.Option("follow", "跟随主题"),
                     ft.dropdown.Option("paper", "纸张"),
                     ft.dropdown.Option("eye", "护眼"),
                     ft.dropdown.Option("night", "夜间"),
                     ft.dropdown.Option("eink", "墨水屏")],
            on_select=lambda e: self._set_reader_preference(
                "background", e.control.value))
        indent = ft.Switch(
            label="段首缩进", value=bool(preferences["indent"]),
            on_change=lambda e: self._set_reader_preference(
                "indent", bool(e.control.value)))
        status = ft.Switch(
            label="显示底部阅读状态", value=bool(preferences["show_status"]),
            on_change=lambda e: self._set_reader_preference(
                "show_status", bool(e.control.value)))
        panel = ft.Container(width=420, padding=12, content=ft.Column([
                ft.Row([ft.Text("阅读设置", size=20, weight=ft.FontWeight.BOLD),
                        ft.IconButton(ft.Icons.CLOSE, tooltip="关闭",
                                      on_click=self._close_reader_dialog)]),
                slider("字号", "font_size", 13, 28, 15),
                slider("行距", "line_height", 1.3, 2.2, 9),
                slider("段距", "paragraph_spacing", 4, 24, 10),
                ft.ResponsiveRow([width, alignment], columns=12),
                background, indent, status,
            ], tight=True))
        if float(self.page.width or 430) < 600:
            self._reader_dialog = ft.BottomSheet(
                content=panel, show_drag_handle=True, use_safe_area=True,
                scrollable=True)
        else:
            self._reader_dialog = ft.AlertDialog(
                modal=False, alignment=ft.Alignment.CENTER_RIGHT,
                inset_padding=ft.Padding(24, 24, 24, 24),
                scrollable=True, content=panel)
        width.col = 6
        alignment.col = 6
        self.page.show_dialog(self._reader_dialog)

    def _close_reader_dialog(self, _=None):
        if self._reader_dialog:
            self.page.pop_dialog()
            self._reader_dialog = None

    def _on_reader_key(self, event):
        binding = self._reader_key_binding(event)
        shortcuts = self._reader_preferences["shortcuts"]
        if binding == shortcuts["directory"]:
            self._open_reader_directory()
        elif binding == shortcuts["settings"] and self._on_open_settings:
            self._on_open_settings()
        elif binding == shortcuts["previous"]:
            self._previous_chapter(None)
        elif binding == shortcuts["next"]:
            self._next_chapter(None)
        elif binding == shortcuts["page_up"]:
            self.page.run_task(self._reader.scroll_to, delta=-560, duration=120)
        elif binding == shortcuts["page_down"]:
            self.page.run_task(self._reader.scroll_to, delta=560, duration=120)
        elif binding == shortcuts["close"]:
            if self._reader_dialog:
                self._close_reader_dialog()
            else:
                self._show_directory(None)
                self.update()

    def _previous_chapter(self, _):
        # 正在加载时按翻页也照常受理：后发起的请求接管界面，先发起的那次
        # 让位但仍然把结果写进缓存，不再出现“点了没反应”。
        chapter = self._adjacent_chapter(-1)
        if chapter:
            self.page.run_task(self._run_content, chapter, False)

    def _next_chapter(self, _):
        chapter = self._adjacent_chapter(1)
        if chapter:
            self.page.run_task(self._run_content, chapter, False)

    def _show_directory(self, _):
        # Treat leaving the reader as a progress commit boundary, including
        # sessions whose final movement did not emit another scroll callback.
        self._persist_scroll_position()
        self._refresh_detail_resume()
        self._show_section("detail")
        self.update()

    def _refresh_detail_resume(self):
        if self._current_book is None or self.current_source is None:
            return self._detail_resume
        position = self.progress_store.load(
            self.current_source.bookSourceUrl, self._current_book.url)
        if position is not None:
            self._detail_resume = position.as_reading()
        return self._detail_resume

    def _on_reader_scroll(self, event):
        pixels = getattr(event, "pixels", 0) or 0
        maximum = getattr(event, "max_scroll_extent", 0) or 0
        # Mounting a replacement document emits a transient zero-offset event
        # before the saved offset is restored. It is layout noise, not user
        # progress, and must not overwrite the saved chapter position.
        if self._restoring_scroll:
            return
        if self._current_chapter is None:
            return
        self._pending_document_offset = max(0.0, float(pixels))
        self._pending_scroll_maximum = max(0.0, float(maximum))
        position, visible_chapter = self.reading_session.track(
            self._pending_document_offset, self._pending_scroll_maximum,
            self._font_size)
        if visible_chapter is not None and visible_chapter is not self._current_chapter:
            self._current_chapter = visible_chapter
            self._update_chapter_navigation()
        self._pending_scroll_offset = position.chapter_offset
        self._pending_scroll_ratio = position.chapter_ratio
        # Scroll callbacks are already throttled by ``scroll_interval``.  Save
        # the latest callback synchronously so closing the window or tapping
        # the reading surface cannot leave the debounced, older record on
        # disk.  The delayed save remains as a final settled-position write.
        self._persist_scroll_position()
        self._scroll_save_generation += 1
        self.page.run_task(
            self._save_scroll_position_after_delay,
            self._scroll_save_generation)
        if not maximum:
            return
        # Loading only blocks another chapter request. It must never block
        # position tracking: the mounted document remains scrollable while a
        # following chapter is fetched.
        if self._reading_loading:
            return
        distance = float(maximum) - float(pixels)
        if distance > 500:
            self._auto_scroll_armed = True
            return
        if distance < 180 and self._auto_scroll_armed and not self._reading_loading:
            chapter = self._adjacent_chapter(1)
            if chapter and chapter.url not in self._continuous_loaded:
                self._auto_scroll_armed = False
                self._pending_append_starts[chapter.url] = float(maximum)
                self.page.run_task(self._run_content, chapter, True)

    async def _save_scroll_position_after_delay(self, generation):
        await asyncio.sleep(0.5)
        if generation == self._scroll_save_generation:
            self._persist_scroll_position()

    def _persist_scroll_position(self):
        if (self._current_chapter is None or self._current_book is None or
                self.current_source is None):
            return
        position = self.reading_session.position
        if position.chapter_url != self._current_chapter.url:
            position = self.reading_session.enter(
                self._current_chapter, self._font_size,
                self._pending_scroll_offset, self._pending_scroll_ratio)
        updated = self.progress_store.save(
            position, self._current_chapter, len(self.chapters))
        if updated:
            self._refresh_detail_resume()

    def _content_plan(self, document):
        """把正文切成段落数据——纯计算，不碰 Flet 控件。

        一章几千行的正则切分留在事件循环上就是几十上百毫秒的卡顿，所以这段
        交给线程跑；循环里只保留构造控件这一步。
        """
        indent = bool(self._reader_preferences["indent"])
        plan = []
        for block in document.blocks:
            if block.kind == "text":
                plan.append(("text", self._novel_paragraphs(
                    render_use_html_spans(block.value), indent)))
            else:
                plan.append((block.kind, block))
        return plan

    async def _render_content(self, document):
        plan = await asyncio.to_thread(self._content_plan, document)
        return self._content_controls(plan, document.image_style)

    def _content_controls(self, plan, image_style=""):
        controls = []
        image_style = str(image_style or "").upper()
        current_book = getattr(self.engine, "current_book", None)
        configured_style = str((getattr(current_book, "read_config", {}) or {}).get(
            "imageStyle", "") or "").upper()
        if configured_style:
            image_style = configured_style
        for kind, block in plan:
            if kind == "text":
                for value, is_heading in block:
                    controls.append(ft.Text(
                        value,
                        selectable=False,
                        size=self._font_size + (2 if is_heading else 0),
                        weight=(ft.FontWeight.W_600 if is_heading else None),
                        style=ft.TextStyle(
                            height=(1.45 if is_heading else float(
                                self._reader_preferences["line_height"]))),
                        text_align=(ft.TextAlign.JUSTIFY
                                    if self._reader_preferences["text_align"] == "justify"
                                    else ft.TextAlign.START),
                        data="reader-heading" if is_heading else "reader-text",
                    ))
            elif kind == "image":
                options = self._image_render_options(image_style, self._font_size)
                controls.append(ft.Image(src=block.value,
                    error_content=ft.Text("图片加载失败"), **options))
            elif kind == "audio":
                import flet_audio as fta
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
            elif kind == "video":
                import flet_video as ftv
                media = ftv.VideoMedia(resource=block.value, http_headers=block.headers or None)
                controls.append(ftv.Video(
                    playlist=[media], title=block.title or "视频", autoplay=False,
                    aspect_ratio=16 / 9, controls=ftv.AdaptiveVideoControls(),
                ))
        return controls

    HEADING_PATTERN = re.compile(
        r"^(?:第[0-9０-９零〇一二三四五六七八九十百千万两]+"
        r"[章节卷回部篇集]|卷[0-9０-９零〇一二三四五六七八九十百千万两]+|"
        r"序章|序言|前言|楔子|引子|后记|尾声|番外)(?:\s|$|[：:、.-])")
    INDENT_KEEP_PREFIXES = ("　　", "•", "·", "- ", "— ")

    @classmethod
    def _novel_paragraphs(cls, value, indent=False):
        """Turn source text into readable novel paragraphs without changing data."""
        normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\xa0", " ")
        paragraphs = []
        for raw_line in normalized.split("\n"):
            line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
            if not line:
                continue
            is_heading = (len(line) <= 48 and
                          bool(cls.HEADING_PATTERN.match(line)))
            if (indent and not is_heading and
                    not line.startswith(cls.INDENT_KEEP_PREFIXES)):
                line = f"　　{line.lstrip('　')}"
            paragraphs.append((line, is_heading))
        return paragraphs

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
        """Progress of a running task; re-shown when the tab comes back."""
        self._background_status = text
        self._status.value = text
        self.update()

    def _set_status(self, text):
        """Final message of a task: nothing left to restore on re-entry."""
        self._background_status = None
        self._status.value = text

    def _set_webview_hint(self, text):
        """Hint from the page renderer: a task may still be running behind it."""
        self._status.value = text
        self.update()

    def _background_engine(self, timeout=None, connect_timeout=None):
        """An engine for work the reader did not directly ask for.

        It gets the built-in browser only: opening a verification dialog over
        whatever the reader is doing would be worse than failing the task.
        """
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if connect_timeout is not None:
            kwargs["connect_timeout"] = connect_timeout
        return LegadoReadingEngine(webview_fetcher=available_fetcher(),
                                   state_dir=self.service.data_dir,
                                   background=True, **kwargs)

    def _restore_background_status(self):
        if self._background_status:
            self._status.value = self._background_status

    def _ui_alive(self) -> bool:
        """关窗之后没有界面可写：后台任务应当就此收手。"""
        try:
            page = self.page
        except RuntimeError:
            return False
        return page_is_alive(page)

    def update(self, *args, **kwargs):
        """关窗销毁会话后，界面写不进去也没有人看，收尾任务不必再抛错。"""
        try:
            super().update(*args, **kwargs)
        except RuntimeError:
            if self._ui_alive():
                raise

    async def _scroll_reader_to(self, offset):
        """scroll_to 是要客户端应答的 invoke 调用：窗口没了就没人应答。"""
        try:
            await self._reader.scroll_to(offset=offset, duration=0)
        except (RuntimeError, TimeoutError):
            pass

    def _show_notice(self, message, retry=None, action=None, on_action=None):
        """Inline status line plus a dismissible SnackBar; errors add 重试."""
        self._background_status = None
        self._status.value = message
        try:
            page = self.page
        except RuntimeError:
            return
        if not page_is_alive(page):
            return
        snack = ft.SnackBar(content=ft.Text(message, max_lines=3))
        label = action or ("重试" if retry is not None else None)
        handler = on_action if on_action is not None else retry
        if label and handler is not None:
            def _run(_event=None):
                try:
                    page.pop_dialog()
                except RuntimeError:
                    pass
                handler()
            snack.action = label
            snack.on_action = _run
        show_snack_bar(page, snack)

    def _show_error(self, exc, retry=None, label=None, action=None, on_action=None):
        prefix = label or ("当前书源暂不支持" if isinstance(exc, UnsupportedRule)
                           else "操作失败")
        self._show_notice(f"{prefix}：{exc}", retry=retry,
                          action=action, on_action=on_action)

    def _repair_action(self, exc, chapter):
        """正文规则失配时，错误提示里直接给一条自愈入口。

        重试只是把同一条规则再跑一次；规则没坏的时候才轮到它。
        """
        if (not isinstance(exc, ContentEmptyError) or
                self.current_source is None or
                rule_needs_script(self.current_source)):
            return None, None
        return ("查找正文规则",
                lambda: self.page.run_task(self._repair_content_rule, chapter))

    def _repair_samples(self, chapter):
        """复核样本：同书另外两章，尽量避开相邻页。"""
        readable = [item for item in self.chapters
                    if not item.is_volume and item.url and item is not chapter]
        if not readable:
            return []
        step = max(1, len(readable) // 3)
        return readable[step::step][:2] or readable[:2]

    async def _repair_content_rule(self, chapter):
        """读到空正文时：本地挖候选、抽样复核，确认后写回书源再重读。"""
        source = self.current_source
        if source is None:
            return
        self._set_busy("正在诊断正文规则…")
        try:
            await diagnose_and_offer(
                self.page, self.service, source, chapter,
                book=self._current_book,
                extra_chapters=self._repair_samples(chapter),
                on_stage=self._set_busy,
                on_applied=lambda: self.page.run_task(
                    self._reload_after_repair, chapter))
        finally:
            self._restore_background_status()

    async def _reload_after_repair(self, chapter):
        self._cancel_prefetch(clear_cache=True)
        self._load_content(chapter)
