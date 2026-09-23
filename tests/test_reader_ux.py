import json
import asyncio
import base64
import collections
import threading
import unittest
import flet as ft
from aiohttp import web
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, PropertyMock

from app.models.book_source import BookSource
from app.services.reading_cache import ReadingCache
from app.services.app_paths import default_app_paths
from app.services.reading_engine import (ChapterContent, ContentBlock,
                                         _format_content_page,
                                         parse_content_blocks)
from app.services.rule_engine import (BookItem, ChapterItem, ExploreCategory,
                                      RequestConnectionError,
                                      RequestTimeoutError, UnsupportedRule)
from app.services.block_pages import SiteBlockedError
from app.services.source_service import SourceService
from app.services.interactive_browser import VIEWPORT
from app.views.reading_view import (SEARCH_SCOPE_CURRENT, SEARCH_SCOPE_GROUP,
                                    SWITCH_CONNECT_TIMEOUT, SWITCH_TIMEOUT,
                                    ReadingView, _intro_control, _intro_text,
                                    _switch_failure_reason)
from app.services.web_page_renderer import WebPageRenderer, looks_like_gate
from app.app import BookSourceApp
from app.views.settings_view import SettingsView


class ReaderInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        service = SimpleNamespace(
            data_dir=Path(self.directory.name), sources=[], bookshelf=[],
            category_checks={})
        self.view = ReadingView(service)

    async def asyncTearDown(self):
        await self.view.engine.aclose()
        self.directory.cleanup()

    async def test_reader_toolbar_is_immersive_and_keeps_core_actions(self):
        self.assertEqual(len(self.view._reader_toolbar.content.controls), 9)
        self.assertFalse(self.view._reader_toolbar.visible)
        self.assertEqual(self.view._font_size, 16)
        self.assertIs(self.view._reader_interaction, self.view._reader)
        self.assertIsInstance(self.view._reader, ft.Column)
        self.assertEqual(self.view._reader.scroll, ft.ScrollMode.AUTO)
        self.assertIsNotNone(self.view._reader_document.on_click)

    async def test_shelf_rows_open_manage_menu_on_secondary_tap_and_long_press(self):
        item = {"name": "Book", "url": "https://x/book",
                "source_url": "https://x"}
        self.view.service.reading_state = lambda selected: None
        row = self.view._shelf_control(item)
        card = self.view._shelf_grid_control(item)
        self.assertIsInstance(row, ft.GestureDetector)
        self.assertIsInstance(card, ft.GestureDetector)
        self.assertEqual(row.key, self.view._shelf_control_key(item, "list"))
        self.assertEqual(card.key, self.view._shelf_control_key(item, "grid"))
        with patch.object(self.view, "_show_shelf_manage") as manage:
            row.on_secondary_tap_down(object())
            row.on_long_press(object())
            card.on_secondary_tap_down(object())
        self.assertEqual(manage.call_count, 3)
        self.assertTrue(all(call.args[0] is item
                            for call in manage.call_args_list))

    async def test_error_feedback_is_snack_bar_with_retry_action(self):
        shown = []
        page = SimpleNamespace(
            show_dialog=shown.append, pop_dialog=lambda: None,
            run_task=lambda coro: coro.close())
        retries = []
        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._show_error(RuntimeError("网络错误"),
                                  retry=lambda: retries.append(1))
        snack = shown[0]
        self.assertIsInstance(snack, ft.SnackBar)
        self.assertEqual(snack.content.value, "操作失败：网络错误")
        self.assertEqual(snack.action, "重试")
        self.assertEqual(self.view._status.value, "操作失败：网络错误")
        snack.on_action(None)
        self.assertEqual(retries, [1])

    async def test_error_feedback_without_page_stays_inline(self):
        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          side_effect=RuntimeError("not mounted")):
            self.view._show_error(UnsupportedRule("缺引擎"))
        self.assertEqual(self.view._status.value, "当前书源暂不支持：缺引擎")

    def _marked_intro_book(self, marker, payload, prefix=""):
        book = BookItem("七侠镇", author="牛马狂飙",
                        url="https://m.lvsexs.com/xs/1.html")
        book.intro = f"{prefix}{marker}{payload}</{marker[1:]}"
        return book

    async def _open_detail_dialog(self, book):
        page = SimpleNamespace(show_dialog=lambda _dialog: None,
                               pop_dialog=lambda: None,
                               run_task=lambda _handler, *_args: None)
        self.view.current_source = SimpleNamespace(
            bookSourceName="绿色小说", bookSourceUrl="https://m.lvsexs.com",
            coverDecodeJs="")

        async def book_info(_source, target):
            return target

        self.view.engine.book_info = book_info
        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._show_book_detail(book)
            # A dialog that was never mounted cannot push its own update.
            self.view._book_detail_dialog.update = lambda: None
            await self.view._load_book_detail(book, self.view.current_source)
        return self.view._book_detail_dialog.content.content.controls[-1]

    async def test_book_detail_dialog_renders_a_useweb_intro_instead_of_markup(self):
        """<useweb> 简介是给网页渲染器用的卡片，不能把它的样式表当简介读。"""
        book = self._marked_intro_book("<useweb>", (
            "\n<style>.card{color:red}</style>\n"
            '<div class="card"><div class="title">七侠镇</div>'
            '<div class="intro-text">正文简介</div></div>\n'), prefix="  ")

        intro = await self._open_detail_dialog(book)

        self.assertIsInstance(intro, ft.Text)
        self.assertEqual(intro.value, "　　七侠镇\n　　正文简介")
        self.assertTrue(intro.selectable)
        self.assertNotIn("<style>", intro.value)
        self.assertNotIn("color:red", intro.value)

    async def test_book_detail_dialog_renders_markdown_intros(self):
        book = self._marked_intro_book("<md>", "# 简介\n\n- **正文**")

        intro = await self._open_detail_dialog(book)

        self.assertIsInstance(intro, ft.Markdown)
        self.assertEqual(intro.value, "# 简介\n\n- **正文**")
        self.assertEqual(intro.extension_set,
                         ft.MarkdownExtensionSet.GITHUB_FLAVORED)

    async def test_plain_intro_without_markup_still_reads_as_before(self):
        book = BookItem("Book", author="作者", url="https://x.test/book")
        book.intro = "<p>第一段</p><br>第二段"

        intro = await self._open_detail_dialog(book)

        self.assertIsInstance(intro, ft.Text)
        self.assertEqual(intro.value, "　　第一段\n　　第二段")

    async def test_result_row_and_shelf_preview_never_show_intro_markup(self):
        book = self._marked_intro_book("<useweb>", (
            "<style>.card{color:red}</style><div>绿野仙踪</div>"))
        self.view.update = lambda: None

        subtitle = self.view._book_control(book).subtitle
        self.view._show_shelf_book_detail(book)

        for text in (subtitle.value, self.view._detail_intro.value):
            self.assertIn("绿野仙踪", text)
            self.assertNotIn("<style>", text)
            self.assertNotIn("color:red", text)

    async def test_text_novel_content_uses_paragraph_reading_layout(self):
        document = ChapterContent([ContentBlock(
            "text",
            "第一章 初见\r\n\r\n 第一段正文。\n第二段正文。\n\n")])

        controls = await self.view._render_content(document)

        self.assertEqual([control.value for control in controls], [
            "第一章 初见", "　　第一段正文。", "　　第二段正文。"])
        self.assertEqual(controls[0].data, "reader-heading")
        self.assertEqual(controls[0].size, self.view._font_size + 2)
        self.assertEqual(controls[0].style.height, 1.45)
        self.assertEqual(controls[1].data, "reader-text")
        self.assertEqual(controls[1].style.height, 1.7)
        self.assertTrue(all(not control.selectable for control in controls))

    async def test_paragraph_splitting_leaves_the_event_loop_alone(self):
        document = ChapterContent([ContentBlock("text", "第一章 初见\n正文。")])
        loop_thread = threading.current_thread()
        threads = []
        original = ReadingView._novel_paragraphs

        def spy(value, indent=False):
            threads.append(threading.current_thread())
            return original(value, indent)

        with patch.object(ReadingView, "_novel_paragraphs",
                          staticmethod(spy)):
            controls = await self.view._render_content(document)

        self.assertEqual([control.value for control in controls],
                         ["第一章 初见", "　　正文。"])
        self.assertTrue(threads)
        self.assertNotEqual(threads[0], loop_thread)

    async def test_use_html_spans_in_chapters_render_as_text(self):
        page = _format_content_page(
            '<p>前 <usehtml><style>x{}</style><b>加粗</b>尾</usehtml>后</p>',
            "https://x.test/c/1")
        document = parse_content_blocks(page, "https://x.test/c/1")

        controls = await self.view._render_content(document)

        self.assertEqual([control.value for control in controls], ["　　前 加粗尾后"])

    async def test_font_size_preserves_text_heading_hierarchy(self):
        self.view._content_host.controls = await self.view._render_content(
            ChapterContent([ContentBlock("text", "序章\n正文")]))
        self.view.update = lambda: None

        self.view._change_font_size(2)

        self.assertEqual(self.view._content_host.controls[0].size, 20)
        self.assertEqual(self.view._content_host.controls[1].size, 18)
        self.assertEqual(len(self.view._reader.controls), 1)
        self.assertIs(self.view._reader_document.content.controls[0], self.view._title)
        self.assertIs(self.view._reader_document.content.controls[2],
                      self.view._content_host)
        self.assertEqual(self.view._reader_document.width, 760)

    async def test_reader_preferences_persist_and_apply(self):
        self.view.update = lambda: None
        self.view._set_reader_preference("line_height", 2.0)
        self.view._set_reader_preference("max_width", 620.0)

        reopened = ReadingView(self.view.service)
        try:
            self.assertEqual(reopened._reader_preferences["line_height"], 2.0)
            self.assertEqual(reopened._reader_document.width, 620.0)
        finally:
            await reopened.engine.aclose()

    async def test_invalid_reader_preferences_fall_back_safely(self):
        self.view._reader_preferences_path.write_text(
            '{"font_size":"bad","line_height":null,"max_width":999}',
            encoding="utf-8")
        reopened = ReadingView(self.view.service)
        try:
            self.assertEqual(reopened._font_size, 16)
            self.assertEqual(reopened._reader_preferences["line_height"], 1.7)
            self.assertEqual(reopened._reader_document.width, 760)
        finally:
            await reopened.engine.aclose()

    async def test_reader_tap_zones_keep_middle_for_chrome(self):
        page = SimpleNamespace(width=1000, platform=ft.PagePlatform.ANDROID)
        with patch.object(type(self.view), "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._previous_chapter = unittest.mock.Mock()
            self.view._next_chapter = unittest.mock.Mock()
            self.view.update = lambda: None
            offset = lambda x: SimpleNamespace(local_position=SimpleNamespace(x=x))

            self.view._on_reader_tap(offset(100))
            self.view._on_reader_tap(offset(900))
            self.view._on_reader_tap(offset(500))

        self.view._previous_chapter.assert_called_once()
        self.view._next_chapter.assert_called_once()
        self.assertTrue(self.view._reader_toolbar.visible)

    async def test_desktop_primary_click_never_changes_chapter(self):
        page = SimpleNamespace(
            width=1000, platform=ft.PagePlatform.WINDOWS)
        with patch.object(type(self.view), "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._previous_chapter = unittest.mock.Mock()
            self.view._next_chapter = unittest.mock.Mock()
            self.view._toggle_reader_chrome = unittest.mock.Mock()
            event = SimpleNamespace(
                local_position=SimpleNamespace(x=10))

            self.view._on_reader_tap(event)

        self.view._toggle_reader_chrome.assert_not_called()
        self.view._previous_chapter.assert_not_called()
        self.view._next_chapter.assert_not_called()

    async def test_desktop_reader_toolbar_stays_visible_without_layout_toggle(self):
        page = SimpleNamespace(
            width=1000, platform=ft.PagePlatform.WINDOWS)
        self.view._reader_toolbar.visible = False
        with patch.object(type(self.view), "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._show_section("reader")

        self.assertTrue(self.view._reader_toolbar.visible)

    async def test_reader_chrome_toggle_does_not_update_scroll_document(self):
        self.view.update = unittest.mock.Mock()
        self.view._reader_toolbar.update = unittest.mock.Mock()

        self.view._toggle_reader_chrome()

        self.view._reader_toolbar.update.assert_called_once_with()
        self.view.update.assert_not_called()

    async def test_reader_scroll_persists_before_debounced_save(self):
        chapter = ChapterItem("one", "https://source/1", index=0)
        self.view._current_chapter = chapter
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view._content_host.controls = [
            ft.Text("x" * 100, data="reader-text")]
        self.view.reading_session.bind(
            self.view.current_source.bookSourceUrl,
            self.view._current_book.url, [chapter])
        self.view.reading_session.mount(chapter, 0.0, 100)
        self.view.service.update_bookshelf_state = unittest.mock.Mock()
        scheduled = []
        page = SimpleNamespace(
            run_task=lambda function, *args: scheduled.append((function, args)))

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._on_reader_scroll(SimpleNamespace(
                pixels=400, max_scroll_extent=1000))

        self.view.service.update_bookshelf_state.assert_called_once()
        kwargs = self.view.service.update_bookshelf_state.call_args.kwargs
        self.assertEqual(kwargs["scroll_offset"], 400.0)
        self.assertEqual(kwargs["scroll_ratio"], 0.4)
        self.assertEqual(kwargs["chapter_pos"], 40)

    async def test_continuous_scroll_persists_visible_chapter_local_position(self):
        first = ChapterItem("six", "https://source/6", index=5)
        second = ChapterItem("hundred", "https://source/100", index=99)
        self.view.chapters = [first, second]
        self.view._current_chapter = second
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view.reading_session.bind(
            self.view.current_source.bookSourceUrl,
            self.view._current_book.url, self.view.chapters)
        self.view.reading_session.mount(first, 0.0, 100)
        self.view.reading_session.mount(second, 1000.0, 200)
        self.view.service.update_bookshelf_state = unittest.mock.Mock()
        page = SimpleNamespace(run_task=lambda *_args: None)

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._on_reader_scroll(SimpleNamespace(
                pixels=1500, max_scroll_extent=2000))

        kwargs = self.view.service.update_bookshelf_state.call_args.kwargs
        self.assertIs(kwargs["chapter"], second)
        self.assertEqual(kwargs["scroll_offset"], 500.0)
        self.assertEqual(kwargs["scroll_ratio"], 0.5)
        self.assertEqual(kwargs["chapter_pos"], 100)

    async def test_scroll_during_next_chapter_load_still_persists_position(self):
        chapter = ChapterItem("one", "https://source/1", index=0)
        self.view.chapters = [chapter]
        self.view._current_chapter = chapter
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view.reading_session.bind(
            self.view.current_source.bookSourceUrl,
            self.view._current_book.url, [chapter])
        self.view.reading_session.mount(chapter, 0.0, 1000)
        self.view.service.update_bookshelf_state = unittest.mock.Mock(
            return_value=True)
        self.view._reading_loading = True
        page = SimpleNamespace(run_task=lambda *_args: None)

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._on_reader_scroll(SimpleNamespace(
                pixels=500, max_scroll_extent=1000))

        kwargs = self.view.service.update_bookshelf_state.call_args.kwargs
        self.assertEqual(kwargs["scroll_offset"], 500.0)
        self.assertEqual(kwargs["scroll_ratio"], 0.5)
        self.assertEqual(kwargs["chapter_pos"], 500)

    async def test_layout_zero_event_during_restore_cannot_overwrite_progress(self):
        chapter = ChapterItem("one", "https://source/1", index=0)
        self.view._current_chapter = chapter
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view.reading_session.bind(
            self.view.current_source.bookSourceUrl,
            self.view._current_book.url, [chapter], {
                "chapter_url": chapter.url, "chapter_index": 0,
                "scroll_offset": 840.0, "scroll_ratio": 0.4,
                "chapter_pos": 400,
            })
        self.view.service.update_bookshelf_state = unittest.mock.Mock()
        self.view._restoring_scroll = True

        self.view._on_reader_scroll(SimpleNamespace(
            pixels=0, max_scroll_extent=2000))

        self.view.service.update_bookshelf_state.assert_not_called()
        self.assertEqual(
            self.view.reading_session.position.chapter_offset, 840.0)

    async def test_reader_shortcuts_can_be_rebound_and_swap_conflicts(self):
        self.view._set_reader_shortcut("previous", "right")

        shortcuts = self.view._reader_preferences["shortcuts"]
        self.assertEqual(shortcuts["previous"], "right")
        self.assertEqual(shortcuts["next"], "left")

        reopened = ReadingView(self.view.service)
        try:
            self.assertEqual(
                reopened._reader_preferences["shortcuts"], shortcuts)
        finally:
            await reopened.engine.aclose()

    async def test_reader_key_handler_uses_custom_binding(self):
        self.view._reader_preferences["shortcuts"]["previous"] = "home"
        self.view._previous_chapter = unittest.mock.Mock()

        self.view._on_reader_key(SimpleNamespace(
            key="Home", ctrl=False, control=False))

        self.view._previous_chapter.assert_called_once_with(None)

    async def test_reader_normalizes_windows_physical_key_name(self):
        self.view._open_reader_directory = unittest.mock.Mock()

        self.view._on_reader_key_down(SimpleNamespace(key="Key F"))

        self.view._open_reader_directory.assert_called_once_with()

    async def test_reader_listener_handles_repeat_events(self):
        self.view._previous_chapter = unittest.mock.Mock()

        self.view._on_reader_key_repeat(SimpleNamespace(key="Arrow Left"))

        self.view._previous_chapter.assert_called_once_with(None)

    async def test_bookshelf_shows_recent_continue_card(self):
        item = {"name": "Book", "source_url": "source", "url": "book",
                "totalChapterNum": 10}
        reading = {"chapter_name": "Chapter 3", "chapter_index": 2,
                   "last_read_at": 100}
        self.view.service.bookshelf = [item]
        self.view.service.reading_state = lambda selected: reading

        self.view._render_bookshelf()

        self.assertTrue(self.view._continue_reading_host.visible)
        row = self.view._continue_reading_host.content.content.content.controls[0]
        labels = row.controls[1].controls
        self.assertEqual(labels[1].value, "Book")
        self.assertEqual(labels[2].value, "Chapter 3")
        progress = self.view._continue_reading_host.content.content.content.controls[1]
        self.assertEqual(progress.semantics_value, 30)
        self.assertIsInstance(progress.semantics_value, int)

    async def test_remount_rerenders_shelf_and_restores_running_progress(self):
        item = {"name": "Book", "source_url": "https://source",
                "url": "https://book", "totalChapterNum": 10}
        self.view.service.bookshelf = [item]
        self.view.service.reading_state = lambda _item: None
        self.view.update = lambda: None
        self.view._whole_book_tasks[self.view._book_task_key(item)] = object()
        self.view._set_busy("正在缓存《Book》：3/10，失败 0")

        self.view.did_mount()

        self.assertEqual("正在缓存《Book》：3/10，失败 0", self.view._status.value)
        badge = self.view._bookshelf.controls[0].content.trailing.controls[0]
        self.assertEqual("缓存中", badge.content.controls[1].value)

    async def test_remount_keeps_shelf_summary_without_background_work(self):
        self.view.service.bookshelf = []
        self.view.update = lambda: None

        self.view.did_mount()

        self.assertEqual("书架中有 0 本书", self.view._status.value)

    async def test_unmount_cancels_only_webview_work(self):
        class FakeTask:
            def __init__(self):
                self.cancelled = False
                self.finished = False

            def done(self):
                return self.finished

            def cancel(self):
                self.cancelled = True

        key = ("https://source", "https://book")
        cache_task, book_task, prefetch_task = FakeTask(), FakeTask(), FakeTask()
        self.view._whole_book_tasks[key] = cache_task
        self.view._book_open_task = book_task
        self.view._prefetch_task = prefetch_task
        self.view._chapter_cache["chapter"] = "cached"
        webview = asyncio.get_running_loop().create_future()
        self.view.renderer._future = webview

        self.view.will_unmount()

        self.assertFalse(cache_task.cancelled)
        self.assertFalse(book_task.cancelled)
        self.assertFalse(prefetch_task.cancelled)
        self.assertEqual({"chapter": "cached"}, self.view._chapter_cache)
        self.assertTrue(webview.cancelled())

    async def test_bookshelf_refresh_uses_stable_unique_control_keys(self):
        first = {"name": "One", "source_url": "https://source-a",
                 "url": "https://book/1"}
        second = {"name": "Two", "source_url": "https://source-a",
                  "url": "https://book/2"}
        self.view.service.bookshelf = [first, second]
        self.view.service.reading_state = lambda _item: None

        self.view._render_bookshelf()
        list_keys = [control.key for control in self.view._bookshelf.controls]
        grid_keys = [control.key for control in self.view._bookshelf_grid.controls]
        first_list_root = self.view._bookshelf.key
        self.view._render_bookshelf()

        self.assertEqual(2, len(set(list_keys)))
        self.assertEqual(2, len(set(grid_keys)))
        self.assertEqual(list_keys,
                         [control.key for control in self.view._bookshelf.controls])
        self.assertEqual(grid_keys,
                         [control.key for control in self.view._bookshelf_grid.controls])
        self.assertNotEqual(first_list_root, self.view._bookshelf.key)
        self.assertIs(self.view._bookshelf_host.controls[-1], self.view._bookshelf)

    async def test_bookshelf_render_count_always_matches_persisted_items(self):
        self.view.service.reading_state = lambda _item: None
        for count in range(1, 6):
            self.view.service.bookshelf.append({
                "name": f"Book {count}", "source_url": "https://source",
                "url": f"https://book/{count}"})
            self.view._render_bookshelf()
            self.assertEqual(count, len(self.view._bookshelf.controls))
            self.assertEqual(count, len(self.view._bookshelf_grid.controls))

    async def test_search_only_source_is_labeled_without_category_parse_error(self):
        source = BookSource(
            bookSourceName="二三小说", bookSourceUrl="https://source.test",
            searchUrl="/search?key={{key}}", exploreUrl="", enabled=True)
        self.view.service.sources = [source]
        self.view.current_source = source
        self.view.update = lambda: None

        self.view.refresh_sources(update=False)
        await self.view._load_categories(source)

        # 列表里那一行自己说明它只能搜，不再靠下拉框的标签兜底。
        self.assertEqual("source.test · 仅搜索",
                         self.view._source_subtitle(source))
        self.assertIn("仅搜索", self._subtitle_text(self.view._source_list))
        self.assertEqual("该书源仅支持关键词搜索，请输入书名或作者",
                         self.view._status.value)
        self.assertEqual([], self.view._category_host.controls)

    async def test_sampled_category_check_only_hides_proven_broken_tabs(self):
        source = BookSource(
            bookSourceName="站点", bookSourceUrl="https://source.test",
            searchUrl="/search?key={{key}}",
            exploreUrl="分类一::/l1\n分类二::/l2\n分类三::/l3", enabled=True)
        self.view.service.sources = [source]
        self.view.current_source = source
        self.view.update = lambda: None
        # Detection samples a few categories and stops at the first working
        # one, so an unsampled category is unknown rather than broken.
        self.view.service.category_checks = {"https://source.test": [
            {"url": "/l1", "title": "分类一", "valid": True},
            {"url": "/l2", "title": "分类二", "valid": False},
        ]}

        self.view.refresh_sources(update=False)
        await self.view._load_categories(source)

        tab_bar = self.view._category_host.controls[0].content.controls[0]
        self.assertEqual(["分类一", "分类三"],
                         [tab.label for tab in tab_bar.tabs])

    @staticmethod
    def _subtitle_text(host) -> str:
        """把书源列表里渲染出来的副标题文本拼起来，供断言使用。"""
        texts = []
        for row in host.controls:
            column = row.content.content
            texts.append(column.controls[0].controls[1].controls[1].value)
        return " | ".join(texts)

    async def test_cache_badges_distinguish_states_with_semantic_theme_colors(self):
        item = {"source_url": "https://source", "url": "https://book",
                "totalChapterNum": 10}
        uncached = self.view._cache_status_control(item)
        self.assertEqual("未缓存", uncached.content.controls[1].value)
        self.assertEqual(ft.Colors.SURFACE_CONTAINER, uncached.bgcolor)

        item.update(cachedChapterCount=4, cacheTotalChapterCount=10)
        partial = self.view._cache_status_control(item)
        self.assertEqual("4/10", partial.content.controls[1].value)
        self.assertEqual(ft.Colors.PRIMARY,
                         partial.content.controls[0].color)

        item["cachedChapterCount"] = 10
        complete = self.view._cache_status_control(item)
        self.assertEqual("已离线", complete.content.controls[1].value)
        self.assertEqual(ft.Colors.PRIMARY_CONTAINER, complete.bgcolor)

        self.view._whole_book_tasks[self.view._book_task_key(item)] = object()
        running = self.view._cache_status_control(item)
        self.assertEqual("缓存中", running.content.controls[1].value)

    async def test_cache_retry_registers_a_tracked_task(self):
        item = {"name": "Book", "source_url": "https://source",
                "url": "https://book"}
        started = []

        class FakePage:
            @staticmethod
            def run_task(handler, *args):
                started.append(handler)
                return SimpleNamespace(done=lambda: False)

        self.view.update = lambda: None
        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=FakePage()):
            await self.view._restart_whole_book_cache(item)

        self.assertEqual([self.view._run_whole_book_cache], started)
        self.assertIn(self.view._book_task_key(item),
                      self.view._whole_book_tasks)

    async def test_cache_retry_ignores_an_already_running_task(self):
        item = {"name": "Book", "source_url": "https://source",
                "url": "https://book"}
        key = self.view._book_task_key(item)
        running = SimpleNamespace(done=lambda: False)
        self.view._whole_book_tasks[key] = running

        class FakePage:
            @staticmethod
            def run_task(handler, *args):
                raise AssertionError("must not start a second cache task")

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=FakePage()):
            await self.view._restart_whole_book_cache(item)

        self.assertIs(running, self.view._whole_book_tasks[key])

    async def test_image_style_maps_default_full_and_text_layout(self):
        self.assertEqual(ReadingView._image_render_options("DEFAULT", 18),
                         {"fit": ft.BoxFit.SCALE_DOWN})
        full = ReadingView._image_render_options("full", 18)
        self.assertEqual(full["fit"], ft.BoxFit.FIT_WIDTH)
        self.assertEqual(full["width"], float("inf"))
        text = ReadingView._image_render_options("TEXT", 18)
        self.assertEqual(text, {"height": 18.0, "fit": ft.BoxFit.CONTAIN})

    async def test_image_tag_with_legado_request_options_keeps_headers(self):
        document = parse_content_blocks(
            '<img src="https://img.test/1.jpg,{"headers":{"Referer":"https://source/"}}">',
            "https://source/chapter")

        self.assertEqual(len(document.blocks), 1)
        self.assertEqual(document.blocks[0].kind, "image")
        self.assertEqual(document.blocks[0].value, "https://img.test/1.jpg")
        self.assertEqual(document.blocks[0].headers, {"Referer": "https://source/"})

    async def test_webview_sniffer_matches_full_resource_and_override_urls(self):
        renderer = WebPageRenderer(lambda: None)
        renderer._initial_url = "https://host/start"
        renderer._source_regex = r"https://cdn/.+\.m3u8"
        result = renderer._select_payload({
            "html": "<main>page</main>", "url": "https://host/page",
            "resources": ["https://cdn/video.m3u8"]})
        self.assertEqual(result["source"], "https://cdn/video.m3u8")
        self.assertEqual(result["body"], "https://cdn/video.m3u8")
        self.assertEqual(result["url"], "https://host/start")

        renderer._source_regex = ""
        renderer._override_regex = r"https://host/final/\d+"
        result = renderer._select_payload({
            "html": "", "url": "https://host/final/42", "resources": []})
        self.assertEqual(result["overrideUrl"], "https://host/final/42")

    async def test_reader_scroll_at_bottom_loads_next_chapter_once(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self.view.chapters = [first, second]
        self.view._current_chapter = first
        self.view._continuous_loaded = {first.url}
        scheduled = []
        page = SimpleNamespace(
            run_task=lambda function, *args: scheduled.append((function, args)))

        event = SimpleNamespace(pixels=950, max_scroll_extent=1000)
        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._on_reader_scroll(event)
            self.view._on_reader_scroll(event)

        chapter_loads = [args for function, args in scheduled
                         if function == self.view._run_content]
        self.assertEqual(chapter_loads, [(second, True)])

    async def test_mobile_hierarchy_combines_detail_and_catalog(self):
        self.assertEqual(self.view._section, "shelf")
        self.assertIn(self.view._directory_toolbar,
                      self.view._detail_page.controls)
        self.assertIn(self.view._chapters, self.view._detail_page.controls)
        self.view.update = lambda: None
        self.view._show_section("reader")
        self.view._show_directory(None)
        self.assertEqual(self.view._section, "detail")
        self.assertTrue(self.view._detail_page.visible)
        self.assertFalse(self.view._reader_page.visible)

    async def test_return_to_directory_commits_and_refreshes_resume_state(self):
        chapter = ChapterItem("hundred", "https://source/100", index=99)
        book = BookItem("book", url="https://book")
        source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        item = {
            "source_url": source.bookSourceUrl,
            "url": book.url,
            "reading": {"chapter_url": chapter.url,
                        "chapter_index": chapter.index,
                        "scroll_offset": 321.0},
        }
        self.view._current_book = book
        self.view._current_chapter = chapter
        self.view.current_source = source
        self.view.chapters = [chapter]
        self.view.service.bookshelf = [item]
        self.view.service.update_bookshelf_state = unittest.mock.Mock(
            return_value=True)
        self.view.service.reading_state = lambda selected: dict(
            selected["reading"])
        self.view._detail_resume = {
            "chapter_url": "https://source/6", "chapter_index": 5}
        self.view.update = lambda: None

        self.view._show_directory(None)

        self.view.service.update_bookshelf_state.assert_called_once()
        self.assertEqual(self.view._detail_resume["chapter_url"], chapter.url)
        self.assertEqual(self.view._detail_resume["scroll_offset"], 321.0)

    async def test_resume_prefers_saved_url_then_falls_back_to_index(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self.view.chapters = [first, second]
        self.assertIs(
            self.view._resume_chapter({
                "chapter_url": "https://source/2", "chapter_index": 0}),
            second)
        self.assertIs(
            self.view._resume_chapter({"chapter_index": 0}), first)

    async def test_directory_click_on_saved_chapter_restores_chapter_offset(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self.view.chapters = [first, second]
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view.service.bookshelf = [{
            "source_url": "https://source", "url": "https://book",
            "reading": {"chapter_url": second.url,
                        "chapter_index": 1, "scroll_offset": 840.0},
        }]
        self.view.service.reading_state = lambda item: dict(item["reading"])
        scheduled = []
        page = SimpleNamespace(
            run_task=lambda function, *args: scheduled.append((function, args)))

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._load_content(second)

        self.assertEqual(scheduled, [
            (self.view._run_content, (second, False, 840.0))])

    async def test_directory_click_on_other_chapter_starts_at_top(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self.view.chapters = [first, second]
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        self.view.service.bookshelf = [{
            "source_url": "https://source", "url": "https://book",
            "reading": {"chapter_url": second.url,
                        "chapter_index": 1, "scroll_offset": 840.0},
        }]
        self.view.service.reading_state = lambda item: dict(item["reading"])
        scheduled = []
        page = SimpleNamespace(
            run_task=lambda function, *args: scheduled.append((function, args)))

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._load_content(first)

        self.assertEqual(scheduled, [
            (self.view._run_content, (first, False, 0.0))])

    async def test_font_size_is_bounded(self):
        self.view.update = lambda: None
        self.view._change_font_size(100)
        self.assertEqual(self.view._font_size, 28)
        self.view._change_font_size(-100)
        self.assertEqual(self.view._font_size, 13)

    async def test_cached_catalog_opens_without_network_reload(self):
        source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        book = BookItem("book", url="https://book")
        chapters = [ChapterItem("one", "https://source/1", index=0)]
        self.view.current_source = source
        self.view.reading_cache.save_catalog(source.bookSourceUrl, book.url, chapters)
        self.view.engine.chapters = AsyncMock(
            side_effect=AssertionError("catalog network request must not run"))
        self.view.update = lambda: None

        await self.view._run_chapters(book)

        self.assertEqual([item.url for item in self.view.chapters],
                         [chapters[0].url])
        self.view.engine.chapters.assert_not_awaited()

    async def test_stale_bookshelf_open_cannot_replace_newer_book(self):
        source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        older = BookItem("older", url="https://book/older")
        newer = BookItem("newer", url="https://book/newer")
        self.view.current_source = source
        self.view._book_open_generation = 2
        self.view._current_book = newer
        self.view.engine.chapters = AsyncMock(
            side_effect=AssertionError("stale request must stop before loading"))

        await self.view._run_chapters(
            older, open_generation=1)

        self.assertIs(self.view._current_book, newer)
        self.view.engine.chapters.assert_not_awaited()

    async def test_two_cached_books_can_resume_consecutively(self):
        source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")
        first = BookItem("first", url="https://book/first")
        second = BookItem("second", url="https://book/second")
        first_chapter = ChapterItem("first chapter", "https://source/first", index=0)
        second_chapter = ChapterItem("second chapter", "https://source/second", index=0)
        self.view.current_source = source
        self.view.reading_cache.save_catalog(
            source.bookSourceUrl, first.url, [first_chapter])
        self.view.reading_cache.save_catalog(
            source.bookSourceUrl, second.url, [second_chapter])
        self.view._run_content = AsyncMock()
        self.view.update = lambda: None

        self.view._book_open_generation = 1
        await self.view._run_chapters(
            first, {"chapter_url": first_chapter.url}, open_generation=1)
        self.view._reset_book_session()
        self.view._book_open_generation = 2
        await self.view._run_chapters(
            second, {"chapter_url": second_chapter.url}, open_generation=2)

        self.assertEqual(self.view._run_content.await_count, 2)
        self.assertEqual(self.view._run_content.await_args_list[0].args[0].url,
                         first_chapter.url)
        self.assertEqual(self.view._run_content.await_args_list[1].args[0].url,
                         second_chapter.url)

    async def test_switching_to_uncached_book_clears_old_book_ui(self):
        self.view._current_book = BookItem("old book", url="https://old")
        self.view._current_chapter = ChapterItem("old chapter", "https://old/1")
        self.view.chapters = [self.view._current_chapter]
        self.view._title.value = "old chapter"
        self.view._directory_status.value = "old book · 10 章"
        self.view._reader_progress.value = "2/10 · old chapter"
        self.view._content_host.controls = [SimpleNamespace(value="old body")]

        self.view._reset_book_session("new book")

        self.assertIsNone(self.view._current_book)
        self.assertIsNone(self.view._current_chapter)
        self.assertEqual(self.view.chapters, [])
        self.assertEqual(self.view._title.value, "new book")
        self.assertIn("new book", self.view._directory_status.value)
        self.assertEqual(self.view._reader_progress.value, "未加载章节")
        self.assertNotIn(
            "old body", str(self.view._content_host.controls[0]))

    async def test_directory_can_filter_and_reverse(self):
        self.view._current_book = BookItem("book", url="https://book")
        self.view.chapters = [
            ChapterItem("Chapter One", "1"),
            ChapterItem("Special Two", "2"),
        ]
        self.view._directory_query.value = "special"
        self.view._apply_directory_filter()
        self.assertEqual([item.name for item in self.view._visible_chapters],
                         ["Special Two"])
        self.view._directory_query.value = ""
        self.view._directory_reversed = True
        self.view._apply_directory_filter()
        self.assertEqual([item.url for item in self.view._visible_chapters],
                         ["2", "1"])


class _ContentEngineStub:
    """按章节放行正文请求，用来观察谁在等谁。"""

    def __init__(self):
        self.calls = []
        self.gates = {}
        # 等待方会先取事件再等：按需生成，免得抢在请求启动前拿不到。
        self.started = collections.defaultdict(asyncio.Event)
        self.closed = 0
        self.current_book = None
        self.js_variables = {}

    def gate(self, url):
        gate = asyncio.Event()
        self.gates[url] = gate
        return gate

    async def content_document(self, source, chapter, next_chapter_url=""):
        self.calls.append(chapter.url)
        self.started[chapter.url].set()
        gate = self.gates.get(chapter.url)
        if gate is not None:
            await gate.wait()
        return ChapterContent([ContentBlock("text", f"{chapter.name} 正文")])

    async def aclose(self):
        self.closed += 1


class ChapterFetchPipelineTests(unittest.IsolatedAsyncioTestCase):
    """请求归缓存，渲染才归界面：翻页、取消、预取都得守住这条线。"""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.service = SimpleNamespace(
            data_dir=Path(self.directory.name), sources=[], bookshelf=[],
            category_checks={})
        self.view = ReadingView(self.service)
        self.view.engine = _ContentEngineStub()
        self.requested = []
        self.scheduled = []
        self.page = SimpleNamespace(
            run_task=self._schedule, width=1000,
            platform=ft.PagePlatform.WINDOWS)
        self.view.update = lambda: None
        self.view._reader.scroll_to = AsyncMock()
        self.service.update_bookshelf_state = unittest.mock.Mock(
            return_value=False)

    def _schedule(self, function, *args):
        self.requested.append((function, args))
        task = asyncio.ensure_future(function(*args))
        self.scheduled.append(task)
        return task

    def _open_book(self, chapters):
        self.view.chapters = chapters
        self.view._current_book = BookItem("book", url="https://book")
        self.view.current_source = BookSource(
            bookSourceName="source", bookSourceUrl="https://source")

    def _read_cached(self, chapter):
        return self.view.reading_cache.load_content(
            "https://source", "https://book", chapter.url)

    async def asyncTearDown(self):
        for task in self.scheduled:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.scheduled, return_exceptions=True)
        await self.view.engine.aclose()
        self.directory.cleanup()

    async def test_a_chapter_requested_twice_is_fetched_once(self):
        chapter = ChapterItem("one", "https://source/1", index=0)
        self._open_book([chapter])
        gate = self.view.engine.gate(chapter.url)

        first = asyncio.ensure_future(self.view._chapter_document(chapter))
        await self.view.engine.started[chapter.url].wait()
        second = asyncio.ensure_future(self.view._chapter_document(chapter))
        await asyncio.sleep(0.05)

        self.assertEqual(len(self.view._content_flight), 1)
        gate.set()
        (one, _), (two, _) = await first, await second

        self.assertEqual(self.view.engine.calls, [chapter.url])
        self.assertIs(one, two)

    async def test_a_superseded_chapter_still_reaches_the_cache(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self._open_book([first, second])
        gate = self.view.engine.gate(first.url)

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=self.page):
            stale = asyncio.ensure_future(self.view._run_content(first))
            await self.view.engine.started[first.url].wait()
            await self.view._run_content(second)
            gate.set()
            await stale

        self.assertIs(self.view._current_chapter, second)
        self.assertEqual(self.view._title.value, second.name)
        self.assertIsNotNone(self._read_cached(first))

    async def test_cancelling_the_render_keeps_the_request_running(self):
        chapter = ChapterItem("one", "https://source/1", index=0)
        self._open_book([chapter])
        gate = self.view.engine.gate(chapter.url)

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=self.page):
            render = asyncio.ensure_future(self.view._run_content(chapter))
            await self.view.engine.started[chapter.url].wait()
            render.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await render
            gate.set()
            await asyncio.gather(*list(self.view._content_flight.values()))

        self.assertIsNotNone(self._read_cached(chapter))
        self.assertEqual(self.view.engine.calls, [chapter.url])

    async def test_an_abandoned_prefetch_still_lands_in_the_cache(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self._open_book([first, second])
        prefetch_engine = _ContentEngineStub()
        self.view._background_engine = lambda **_kwargs: prefetch_engine
        gate = prefetch_engine.gate(second.url)

        prefetch = asyncio.ensure_future(
            self.view._prefetch_next_chapters(first))
        await prefetch_engine.started[second.url].wait()
        prefetch.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await prefetch

        self.assertEqual(prefetch_engine.closed, 0)
        gate.set()
        await asyncio.gather(*list(self.view._content_flight.values()))

        self.assertEqual(prefetch_engine.closed, 1)
        self.assertIsNotNone(self._read_cached(second))

    async def test_a_page_turn_during_a_load_is_not_swallowed(self):
        first = ChapterItem("one", "https://source/1", index=0)
        second = ChapterItem("two", "https://source/2", index=1)
        self._open_book([first, second])
        self.view._current_chapter = first
        self.view._reading_loading = True

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=self.page):
            self.view._next_chapter(None)
            await asyncio.sleep(0.05)

        self.assertEqual([(function.__name__, args)
                          for function, args in self.requested
                          if function.__name__ == "_run_content"],
                         [("_run_content", (second, False))])


class _SwitchEngineStub:
    """Stands in for the per-source engine the sweep builds for every source."""

    def __init__(self, search):
        self.search = search
        self.js = SimpleNamespace(close=lambda: None)

    async def aclose(self):
        self.js.close()


class _SwitchSearchStub:
    """Canned results per source URL; a gate parks workers for stop tests."""

    def __init__(self, books=None, errors=None, gate=None):
        self.books = books or {}
        self.errors = errors or {}
        self.gate = gate
        self.started = 0
        self.cancelled = 0
        self.calls = []

    async def search(self, source, keyword, page=1):
        self.started += 1
        self.calls.append((source.bookSourceUrl, page))
        try:
            if self.gate is not None:
                await self.gate.wait()
            failure = self.errors.get(source.bookSourceUrl)
            if failure is not None:
                raise failure
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return list(self.books.get(source.bookSourceUrl, []))


class _SwitchPageStub:
    def __init__(self):
        self.dialogs = []
        self.tasks = []

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None

    def run_task(self, handler, *args):
        task = asyncio.ensure_future(handler(*args))
        self.tasks.append(task)
        return task


class SourceSwitchTests(unittest.IsolatedAsyncioTestCase):
    """换源流程：候选排序、分批、可中断，以及失效书源下的落库。"""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.service = SourceService(self.root, data_dir=self.root / "data")
        self.view = ReadingView(self.service)
        self.view.update = lambda: None
        self.page = _SwitchPageStub()
        patcher = patch.object(ReadingView, "page", new_callable=PropertyMock,
                               return_value=self.page)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.view.engine.aclose)
        self.addCleanup(self.directory.cleanup)

    def _shelve(self, *, origin="https://dead.example",
                url="https://dead.example/book/1"):
        self.assertTrue(self.service.add_to_bookshelf(
            BookSource(bookSourceName="旧书源", bookSourceUrl=origin),
            BookItem("Book", author="作者", url=url)))
        return self.service.bookshelf[0]

    @staticmethod
    def _target(url, name="可用书源", group=None):
        return BookSource(bookSourceName=name, bookSourceUrl=url,
                          searchUrl=f"{url}/search?key=${{key}}",
                          bookSourceGroup=group)

    def _install(self, stub):
        self.view._background_engine = lambda **_kwargs: _SwitchEngineStub(
            stub.search)
        return stub

    async def _settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_candidates_rank_the_same_brand_first_and_report_failures(self):
        item = self._shelve(origin="https://qbtr.me",
                            url="https://www.qbtr.cc/book/1")
        self.service.sources = [
            self._target("https://other.test", name="无关书源"),
            self._target("https://zzz.test", name="已失效书源", group="失效, 正常"),
            self._target("https://www.qbtr.org", name="全本同人小说网"),
        ]
        self.assertEqual(
            [source.bookSourceUrl
             for source in self.view._switch_candidates(item)],
            ["https://www.qbtr.org", "https://other.test", "https://zzz.test"])
        self.assertEqual(
            self.view._switch_start_hint(item),
            "将搜索全部 3 个已启用书源，同站同域名的排在最前。")

        stub = self._install(_SwitchSearchStub(
            books={"https://www.qbtr.org": [BookItem(
                "Book", author="作者", url="https://www.qbtr.org/book/1",
                last_chapter="chapter 9")]},
            errors={"https://other.test": UnsupportedRule("no such rule")}))
        self.view._show_source_switch(item)
        self.view._start_source_switch(item)
        await self.view._source_switch_task

        self.assertEqual(stub.started, 3)
        self.assertEqual(
            self.view._source_switch_status.value,
            "搜索完成：检查 3 个书源，找到 1 个结果（1 个书源搜索失败：规则不支持 1）")
        results = self.view._source_switch_results.controls
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title.value, "Book")
        self.assertEqual(results[0].subtitle.value,
                         "全本同人小说网 · www.qbtr.org · 作者 · chapter 9")

    async def test_every_candidate_is_searched_in_one_run(self):
        item = self._shelve()
        self.service.sources = [
            self._target(f"https://s{index:02d}.test", name=f"源{index:02d}")
            for index in range(40)]
        stub = self._install(_SwitchSearchStub(
            books={"https://s39.test": [BookItem("Book",
                                                 url="https://s39.test/book/1")]}))

        self.view._show_source_switch(item)
        self.view._start_source_switch(item)
        await self.view._source_switch_task

        self.assertEqual(stub.started, 40)
        self.assertEqual(
            self.view._source_switch_status.value,
            "搜索完成：检查 40 个书源，找到 1 个结果")
        self.assertEqual(len(self.view._source_switch_results.controls), 1)
        self.assertEqual(self.view._source_switch_progress.value, 1.0)
        self.assertFalse(self.view._source_switch_progress.visible)
        self.assertTrue(self.view._source_switch_search.visible)

    async def test_sweep_hands_each_engine_the_sweep_timeouts(self):
        """一轮扫描里每个书源都要自己一份短超时，否则挂死域名会占满工作位。"""
        item = self._shelve()
        self.service.sources = [self._target("https://slow.test", name="慢源")]
        seen = []

        def factory(**kwargs):
            seen.append(kwargs)
            return _SwitchEngineStub(_SwitchSearchStub().search)

        self.view._background_engine = factory
        self.view._show_source_switch(item)
        self.view._start_source_switch(item)
        await self.view._source_switch_task

        self.assertEqual(seen, [{"timeout": SWITCH_TIMEOUT,
                                 "connect_timeout": SWITCH_CONNECT_TIMEOUT}])
        # 单个站点顶多占住 5 秒连接 + 10 秒总时长，而不是默认的 15 秒一把梭。
        self.assertLess(SWITCH_CONNECT_TIMEOUT, SWITCH_TIMEOUT)

    async def test_sweep_runs_the_real_engine_against_a_live_source(self):
        """真引擎跑一遍：短超时不能把本来能用的回环书源判死。"""
        async def handler(_request):
            return web.Response(
                text='<ul><li><a href="/book/1">Book</a><i>作者</i></li></ul>',
                content_type="text/html")

        app = web.Application()
        app.router.add_get("/search", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        item = self._shelve()
        self.service.sources = [BookSource(
            bookSourceName="回环源", bookSourceUrl=base,
            searchUrl=base + "/search?key={{key}}",
            ruleSearch={"bookList": "li", "name": "a@text", "author": "i@text",
                        "bookUrl": "a@href"})]

        self.view._show_source_switch(item)
        self.view._start_source_switch(item)
        await self.view._source_switch_task

        results = self.view._source_switch_results.controls
        self.assertEqual([tile.title.value for tile in results], ["Book"])
        self.assertIn("127.0.0.1", results[0].subtitle.value)
        self.assertEqual(self.view._source_switch_status.value,
                         "搜索完成：检查 1 个书源，找到 1 个结果")

    def test_status_line_reports_elapsed_and_remaining_time(self):
        failures = collections.Counter()
        text = self.view._switch_status_text(
            200, 400, [], failures, running=True, elapsed=100.0)
        self.assertEqual(text, "正在搜索 200/400 个书源（已用 1:40，预计还需 1:40）"
                               "，找到 0 个结果")
        # 开头几个还没跑完时给不出速度，不硬猜。
        self.assertEqual(
            self.view._switch_status_text(1, 400, [], failures,
                                          running=True, elapsed=0.2),
            "正在搜索 1/400 个书源，找到 0 个结果")
        self.assertEqual(
            self.view._switch_status_text(400, 400, [], failures,
                                          running=False, elapsed=95.0),
            "搜索完成：检查 400 个书源，用时 1:35，找到 0 个结果")
        # 小时级的全量扫描也要读得懂。
        self.assertEqual(
            self.view._switch_status_text(0, 4000, [], failures,
                                          running=False, elapsed=3672.0),
            "搜索完成：检查 4000 个书源，用时 1:01:12，找到 0 个结果")

    def test_failure_reasons_keep_timeout_network_and_blocking_apart(self):
        """“规则或解析错误”只该留给真正读不懂的来源。"""
        self.assertEqual(_switch_failure_reason(RequestTimeoutError("x")), "超时")
        self.assertEqual(_switch_failure_reason(RequestConnectionError("x")),
                         "网络错误")
        self.assertEqual(_switch_failure_reason(SiteBlockedError("本机安全软件")),
                         "被拦截")
        self.assertEqual(_switch_failure_reason(UnsupportedRule("没有该规则")),
                         "规则不支持")
        self.assertEqual(_switch_failure_reason(RuntimeError("正文解析结果为空")),
                         "规则或解析错误")

    async def test_stopping_cancels_every_running_worker(self):
        item = self._shelve()
        self.service.sources = [
            self._target(f"https://s{index}.test", name=f"源{index}")
            for index in range(4)]
        stub = self._install(_SwitchSearchStub(gate=asyncio.Event()))

        self.view._show_source_switch(item)
        self.view._start_source_switch(item)
        task = self.view._source_switch_task
        await self._settle()
        self.assertEqual(stub.started, 4)
        self.assertTrue(self.view._source_switch_progress.visible)
        self.assertTrue(self.view._source_switch_stop.visible)
        self.assertFalse(self.view._source_switch_search.visible)

        self.view._stop_source_switch()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._settle()

        self.assertEqual(stub.cancelled, 4)
        self.assertEqual(self.view._source_switch_status.value, "搜索已停止")
        self.assertIsNone(self.view._source_switch_task)
        self.assertFalse(self.view._source_switch_progress.visible)
        self.assertFalse(self.view._source_switch_stop.visible)
        self.assertTrue(self.view._source_switch_search.visible)

    async def test_apply_switch_targets_the_live_record_after_a_reload(self):
        item = self._shelve()
        target = self._target("https://alive.example")
        self.service.sources = [target]
        self.service.update_bookshelf_state(
            item["source_url"], item["url"],
            chapter=ChapterItem("chapter 8", "https://dead.example/c/8",
                                index=7),
            scroll_offset=200, scroll_ratio=0.4, font_size=20)
        # 换源对话期间书架可能已重载：界面手里还是那份旧字典。
        self.service.load_bookshelf()
        self.assertIsNot(item, self.service.bookshelf[0])
        self.view._show_source_switch(item)

        self.view._apply_source_switch(
            item, target, BookItem("Book", author="作者",
                                   url="https://alive.example/book/1",
                                   last_chapter="chapter 30"))

        record = self.service.bookshelf[0]
        self.assertEqual(record["source_url"], "https://alive.example")
        self.assertEqual(record["source_name"], "可用书源")
        self.assertEqual(record["reading"]["chapter_index"], 7)
        self.assertEqual(record["reading"]["chapter_url"], "")
        self.assertEqual(record["reading"]["scroll_offset"], 0.0)
        self.assertEqual(record["reading"]["scroll_ratio"], 0.0)
        self.assertEqual(record["reading"]["font_size"], 20)
        self.assertEqual(self.view._status.value, "已切换到书源：可用书源")
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)

        reopened = SourceService(self.root, data_dir=self.root / "data")
        reopened.load_bookshelf()
        self.assertEqual(reopened.bookshelf[0]["origin"],
                         "https://alive.example")

    async def test_apply_switch_reports_a_book_that_left_the_shelf(self):
        item = self._shelve()
        self.assertTrue(self.service.remove_from_bookshelf(
            item["source_url"], item["url"]))
        self.view._show_source_switch(item)

        self.view._apply_source_switch(
            item, self._target("https://alive.example"),
            BookItem("Book", url="https://alive.example/book/1"))

        self.assertEqual(self.view._status.value, "切换失败：这本书已不在书架中")
        self.assertEqual(self.service.bookshelf, [])

    async def test_apply_switch_refuses_a_target_already_on_the_shelf(self):
        item = self._shelve()
        target = self._target("https://alive.example")
        self.service.sources = [target]
        self.service.add_to_bookshelf(
            target, BookItem("Book", url="https://alive.example/book/1"))
        self.view._show_source_switch(item)

        self.view._apply_source_switch(
            item, target, BookItem("Book", url="https://alive.example/book/1"))

        self.assertEqual(self.view._status.value,
                         "切换失败：书架中已有这本书的该书源版本")
        self.assertEqual(len(self.service.bookshelf), 2)
        self.assertEqual(self.service.bookshelf[0]["source_url"],
                         "https://dead.example")

    async def test_opening_a_dead_sources_book_offers_the_switch_dialog(self):
        item = self._shelve()
        self.view._open_shelf_item(item)

        snack = self.page.dialogs[-1]
        self.assertIsInstance(snack, ft.SnackBar)
        self.assertEqual(snack.action, "切换书源")
        self.assertEqual(self.view._status.value,
                         "原书源已失效，无法加载这本书，请先切换书源")

        self.page.dialogs.clear()
        snack.on_action(None)

        dialog = self.page.dialogs[-1]
        self.assertIsInstance(dialog, ft.AlertDialog)
        self.assertEqual(dialog.title.value, "切换书源")

    async def test_a_dead_source_is_labelled_on_the_shelf_row(self):
        item = self._shelve()
        self.assertTrue(self.view._shelf_source_missing(item))
        self.assertEqual(self.view._shelf_source_label(item), "旧书源（已失效）")

        row = self.view._shelf_control(item)
        self.assertIn("书源已失效", row.content.subtitle.controls[0].value)


class DiscoveryPageTests(unittest.IsolatedAsyncioTestCase):
    """找书页：默认是书源列表，点开一行看分类，搜索单独占一页。"""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.service = SourceService(self.root, data_dir=self.root / "data")
        self.view = ReadingView(self.service, discovery_only=True)
        self.view.update = lambda: None
        self.page = _SwitchPageStub()
        patcher = patch.object(ReadingView, "page", new_callable=PropertyMock,
                               return_value=self.page)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.view.engine.aclose)
        self.addCleanup(self.directory.cleanup)

    @staticmethod
    def _source(url, name, explore="", search=True, enabled=True):
        return BookSource(
            bookSourceName=name, bookSourceUrl=url,
            searchUrl=f"{url}/search?key=${{key}}" if search else "",
            exploreUrl=explore, enabled=enabled)

    def _rows(self):
        return self.view._source_list.controls

    @staticmethod
    def _row_title(row) -> str:
        """书源列表一行的标题组件在图标、标题列之后。"""
        return row.content.content.controls[0].controls[1].controls[0].value

    async def test_usable_sources_are_listed_and_the_source_dropdown_is_gone(self):
        self.service.sources = [
            self._source("https://a.test", "甲站", explore="分类一::/l1"),
            self._source("https://b.test", "乙站"),
            self._source("https://c.test", "丙站", search=False),
            self._source("https://d.test", "丁站", explore="分类一::/l1",
                         enabled=False),
        ]

        self.view.refresh_sources(update=False)

        self.assertFalse(hasattr(self.view, "_source"))
        self.assertEqual(2, len(self._rows()))
        self.assertEqual("sources", self.view._discovery_page)
        self.assertEqual(["甲站", "乙站"],
                         [self._row_title(row) for row in self._rows()])
        # 当前书源跟着列表走，用于“仅当前书源”这个范围。
        self.assertEqual("https://a.test", self.view.current_source.bookSourceUrl)

    async def test_expanding_a_row_shows_its_categories_until_collapsed(self):
        source = self._source("https://a.test", "甲站",
                              explore="分类一::/l1\n分类二::/l2")
        self.service.sources = [source]
        self.view.refresh_sources(update=False)
        chips = self.view._source_chips["https://a.test"]

        self.assertFalse(chips.visible)
        self.view._toggle_source_expand(source)
        await asyncio.sleep(0)

        self.assertTrue(chips.visible)
        self.assertEqual(["分类一", "分类二"],
                         [chip.label.value for chip in chips.controls[0].controls])
        self.assertEqual("https://a.test", self.view.current_source.bookSourceUrl)

        self.view._toggle_source_expand(source)
        self.assertFalse(chips.visible)

    async def test_a_search_only_row_expands_into_a_search_shortcut(self):
        source = self._source("https://a.test", "甲站")
        self.service.sources = [source]
        self.view.refresh_sources(update=False)

        self.view._toggle_source_expand(source)

        chips = self.view._source_chips["https://a.test"]
        self.assertTrue(chips.visible)
        button = chips.controls[0].controls[1]
        self.assertEqual("用此书源搜索", button.content)
        button.on_click(None)

        self.assertEqual("search", self.view._discovery_page)
        self.assertEqual(SEARCH_SCOPE_CURRENT, self.view._search_scope_value)
        self.assertEqual("仅当前书源", self.view._search_scope_label.value)
        self.assertTrue(self.view._search_page.visible)
        self.assertFalse(self.view._sources_page.visible)

    async def test_the_row_menu_searches_with_that_source(self):
        source = self._source("https://a.test", "甲站", explore="分类一::/l1")
        self.service.sources = [source]
        self.view.refresh_sources(update=False)

        self.view._show_source_menu(source)
        dialog = self.page.dialogs[-1]
        self.assertEqual("甲站", dialog.title.value)
        search_action = dialog.actions[-1]
        self.assertEqual("用此书源搜索", search_action.content)
        search_action.on_click(None)

        self.assertEqual("search", self.view._discovery_page)
        self.assertEqual(SEARCH_SCOPE_CURRENT, self.view._search_scope_value)
        self.assertIs(source, self.view.current_source)

    async def test_browsing_a_category_does_not_clear_the_search_results(self):
        source = self._source("https://a.test", "甲站", explore="分类一::/l1")
        self.service.sources = [source]
        self.view.refresh_sources(update=False)
        book = BookItem("寻秦记", url="https://a.test/book/1",
                        origin="https://a.test")
        self.view._search_flow.append([book])
        self.view.engine.explore = AsyncMock(return_value=[])

        self.view._open_category(source, ExploreCategory("分类一", "/l1"))
        await asyncio.sleep(0)
        await asyncio.gather(*self.page.tasks)

        self.assertEqual([book], self.view._search_flow.books)
        self.assertIsNot(self.view._browse_flow.list_view,
                         self.view._search_flow.list_view)
        self.assertEqual("browse", self.view._discovery_page)
        self.assertEqual("甲站", self.view._browse_title.value)
        self.assertEqual(["/l1"], [call.args[1].url for call in
                                   self.view.engine.explore.await_args_list])
        self.assertEqual("分类一：找到 0 本书，固定榜单无后续分页",
                         self.view._status.value)

    async def test_a_running_search_survives_going_back_to_the_source_list(self):
        source = self._source("https://a.test", "甲站")
        self.service.sources = [source]
        stub = _SwitchSearchStub(books={
            "https://a.test": [BookItem("寻秦记", url="https://a.test/book/1",
                                        origin="https://a.test")]})
        self.view._background_engine = lambda **_kwargs: _SwitchEngineStub(
            stub.search)
        self.view.refresh_sources(update=False)
        self.view._set_search_scope(SEARCH_SCOPE_CURRENT)
        self.view._keyword.value = "寻秦记"
        self.view._search(None)

        self.view._back_to_sources()
        await self.view._search_task

        self.assertEqual("sources", self.view._discovery_page)
        self.assertEqual(["寻秦记"],
                         [book.name for book in self.view._search_flow.books])
        self.assertEqual("搜索完成：检查 1 个书源，找到 1 本",
                         self.view._status.value)


class AllSourceSearchTests(unittest.IsolatedAsyncioTestCase):
    """全书源找书：范围内的书源各搜一次，按书合并、可停止、结果落库。"""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.service = SourceService(self.root, data_dir=self.root / "data")
        self.view = ReadingView(self.service)
        self.view.update = lambda: None
        self.page = _SwitchPageStub()
        patcher = patch.object(ReadingView, "page", new_callable=PropertyMock,
                               return_value=self.page)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.view.engine.aclose)
        self.addCleanup(self.directory.cleanup)
        self.service.sources = [
            self._source("https://a.test", "甲源", group="精品"),
            self._source("https://b.test", "乙源", group="精品"),
            self._source("https://c.test", "丙源"),
        ]
        self.view.refresh_sources(update=False)

    @staticmethod
    def _source(url, name, group=None):
        return BookSource(bookSourceName=name, bookSourceUrl=url,
                          searchUrl=f"{url}/search?key=${{key}}",
                          bookSourceGroup=group)

    def _install(self, stub):
        self.view._background_engine = lambda **_kwargs: _SwitchEngineStub(
            stub.search)
        return stub

    async def _settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    def _start(self, keyword, stub):
        self._install(stub)
        self.view._keyword.value = keyword
        self.view._search(None)
        return self.view._search_task

    @staticmethod
    def _book(url, name="寻秦记", author="黄易"):
        return BookItem(name, author=author, url=f"{url}/book/1", origin=url)

    async def test_every_source_in_scope_is_searched_and_books_are_merged(self):
        stub = _SwitchSearchStub(books={
            "https://a.test": [self._book("https://a.test")],
            "https://b.test": [self._book("https://b.test")],
        })
        self._start("寻秦记", stub)
        await self.view._search_task

        self.assertEqual(sorted(stub.calls), [
            ("https://a.test", 1), ("https://b.test", 1), ("https://c.test", 1)])
        # 同一本书来自两个书源，合并成一条并记下两处来源。
        self.assertEqual([book.name for book in self.view._search_flow.books], ["寻秦记"])
        self.assertEqual(sorted(self.view._search_flow.books[0].origin_set()),
                         ["https://a.test", "https://b.test"])
        self.assertEqual(self.view._status.value, "搜索完成：检查 3 个书源，找到 1 本")
        self.assertEqual(self.view._search_progress.value, 1.0)
        self.assertFalse(self.view._search_progress.visible)

    async def test_a_failing_source_is_counted_without_stopping_the_sweep(self):
        stub = _SwitchSearchStub(
            books={"https://a.test": [self._book("https://a.test")]},
            errors={"https://b.test": UnsupportedRule("no such rule")})
        self._start("寻秦记", stub)
        await self.view._search_task

        self.assertEqual([book.name for book in self.view._search_flow.books], ["寻秦记"])
        self.assertEqual(
            self.view._status.value,
            "搜索完成：检查 3 个书源，找到 1 本（1 个书源搜索失败：规则不支持 1）")

    async def test_scope_can_narrow_to_the_current_source_or_one_group(self):
        stub = _SwitchSearchStub()
        self._install(stub)

        self.view._set_search_scope(SEARCH_SCOPE_CURRENT)
        self.view._keyword.value = "寻秦记"
        self.view._search(None)
        await self.view._search_task
        self.assertEqual(stub.calls, [("https://a.test", 1)])

        # 分组范围只跑该组的书源，选项由书源列表刷新时补齐。
        self.assertIn(f"{SEARCH_SCOPE_GROUP}精品",
                      [key for key, _ in self.view._search_scope_options])
        self.view._set_search_scope(f"{SEARCH_SCOPE_GROUP}精品")
        self.assertEqual(sorted(source.bookSourceUrl
                                for source in self.view._search_scope_sources()),
                         ["https://a.test", "https://b.test"])

    async def test_the_scope_entry_is_a_dialog_not_a_page_dropdown(self):
        stub = _SwitchSearchStub()
        self._install(stub)

        self.view._open_scope_dialog()
        dialog = self.page.dialogs[-1]
        self.assertEqual("搜索范围", dialog.title.value)
        rows = dialog.content.content.controls
        self.assertEqual([key for key, _ in self.view._search_scope_options],
                         [row.data for row in rows])
        self.assertFalse(hasattr(self.view, "_search_scope_dropdown"))

        group_row = next(row for row in rows
                         if row.data == f"{SEARCH_SCOPE_GROUP}精品")
        group_row.on_click(None)

        self.assertEqual(f"{SEARCH_SCOPE_GROUP}精品",
                         self.view._search_scope_value)
        self.assertEqual("分组：精品", self.view._search_scope_label.value)
        self.assertIn("个书源", self.view._status.value)

    async def test_search_reports_progress_and_can_be_stopped_midway(self):
        stub = _SwitchSearchStub(gate=asyncio.Event())
        task = self._start("寻秦记", stub)
        await self._settle()

        self.assertTrue(self.view._search_progress.visible)
        self.assertTrue(self.view._search_stop.visible)
        self.assertFalse(self.view._search_btn.visible)
        self.assertIn("正在搜索", self.view._status.value)

        self.view._stop_search()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self._settle()

        self.assertEqual(stub.cancelled, 3)
        self.assertEqual(self.view._status.value, "搜索已停止，已找到 0 本")
        self.assertFalse(self.view._search_progress.visible)
        self.assertFalse(self.view._search_stop.visible)
        self.assertTrue(self.view._search_btn.visible)

    async def test_load_more_asks_the_same_search_id_for_the_next_page(self):
        stub = _SwitchSearchStub(books={
            "https://a.test": [BookItem(
                "寻秦记 第一部", author="黄易", url="https://a.test/book/1")]})
        self._start("寻秦记", stub)
        await self.view._search_task
        self.assertTrue(self.view._search_flow.has_more)

        self.view._load_more_search()
        await self.view._search_task

        pages = collections.Counter(page for _, page in stub.calls)
        self.assertEqual(pages, {1: 3, 2: 3})

    async def test_results_are_stored_and_the_switch_dialog_opens_them_offline(self):
        stub = _SwitchSearchStub(books={
            "https://a.test": [self._book("https://a.test")],
            "https://b.test": [self._book("https://b.test")],
        })
        self._start("寻秦记", stub)
        await self.view._search_task
        self.assertIsNotNone(
            self.view._search_store().first_by_name_author("寻秦记", "黄易"))

        # 换源列表打开就用本地结果，不再联网。
        self.view._background_engine = lambda **_kwargs: self.fail(
            "换源列表不该为本地结果联网")
        self.view._show_source_switch({
            "name": "寻秦记", "author": "黄易", "url": "https://dead.test/book/1",
            "source_url": "https://dead.test", "source_name": "旧源"})

        tiles = self.view._source_switch_results.controls
        self.assertEqual([tile.title.value for tile in tiles], ["寻秦记", "寻秦记"])
        self.assertEqual(self.view._source_switch_status.value,
                         "本地已有 2 个结果，点“搜索其他书源”可联网核对")

    async def test_result_rows_name_their_own_source_and_rank_exact_names_first(self):
        stub = _SwitchSearchStub(books={
            "https://a.test": [self._book("https://a.test")],
            "https://b.test": [self._book("https://b.test")],
        })
        self._start("寻秦记", stub)
        await self.view._search_task

        merged = self.view._search_flow.books[0]
        self.assertIn("等 2 个书源",
                      self.view._book_control(merged).subtitle.value)
        # 结果行认的是书自己的来源，而不是下拉框里当前选中的书源。
        self.assertIn(self.view._source_for_book(merged),
                      self.service.sources[:2])

        item = {"name": "寻秦记", "author": "黄易", "url": "https://dead.test/1",
                "source_url": "https://dead.test"}
        self.view._show_source_switch(item)
        self.view._render_source_switch_results(item, [
            (self.view.service.sources[2], BookItem("寻秦记 后传", url="u1")),
            (self.view.service.sources[1], BookItem("寻秦记", url="u2")),
            (self.view.service.sources[0], BookItem("寻秦记", url="u3")),
        ])
        self.assertEqual(
            [tile.title.value
             for tile in self.view._source_switch_results.controls],
            # 书名完全一致的两条在前，同分时按候选顺序（甲源在乙源之前）。
            ["寻秦记", "寻秦记", "寻秦记 后传"])


class PersistentReadingTests(unittest.TestCase):
    def test_settings_places_appearance_under_extensible_configuration_tabs(self):
        with TemporaryDirectory() as directory:
            from app.services.theme_service import ThemeService
            service = ThemeService(directory)
            service.load()
            view = SettingsView(service, lambda _value: None, lambda _value: None)
            self.assertEqual(2, view.tabs.length)
            self.assertIs(view.tabs.content.controls[1].controls[0],
                          view.appearance_view)
            self.assertIs(view.tabs.content.controls[1].controls[1],
                          view.shortcut_help_view)

    def test_settings_adds_reader_layout_as_second_tab(self):
        with TemporaryDirectory() as directory:
            from app.services.theme_service import ThemeService
            service = ThemeService(directory)
            service.load()
            reader = SimpleNamespace(
                _reader_preferences={
                    "font_size": 16, "line_height": 1.7,
                    "paragraph_spacing": 12, "max_width": 760,
                    "indent": True, "text_align": "start",
                    "background": "follow", "show_status": True,
                    "shortcuts": {
                        "previous": "left", "next": "right",
                        "page_up": "pageup", "page_down": "pagedown",
                        "directory": "f", "settings": "comma",
                        "close": "escape"}},
                _set_reader_preference=lambda key, value: None,
                _set_reader_shortcut=lambda action, binding: None)
            view = SettingsView(
                service, lambda _value: None, lambda _value: None,
                reader_view=reader)

            self.assertEqual(3, view.tabs.length)
            self.assertIs(view.tabs.content.controls[1].controls[1],
                          view.reader_layout_view)
            self.assertIs(view.tabs.content.controls[1].controls[2],
                          view.shortcut_help_view)
            self.assertEqual(7, len(view.shortcut_help_view.shortcut_fields))
            options = view.shortcut_help_view.shortcut_fields[
                "page_up"].options
            values = [option.key for option in options]
            self.assertIn("up", values)
            self.assertIn("down", values)

    def test_shortcut_help_lists_keyboard_and_mouse_wheel(self):
        with TemporaryDirectory() as directory:
            from app.services.theme_service import ThemeService
            service = ThemeService(directory)
            service.load()
            view = SettingsView(service, lambda _value: None, lambda _value: None)
            labels = [
                control.leading.content.value
                for control in view.shortcut_help_view.controls
                if isinstance(control, ft.ListTile)]
            self.assertIn("F", labels)
            self.assertIn(",", labels)
            self.assertIn("滚轮", labels)

    def test_platform_paths_never_depend_on_executable_directory(self):
        home = Path("/users/tester")
        windows = default_app_paths(
            platform="win32", environ={"LOCALAPPDATA": "C:/Local"}, home=home)
        macos = default_app_paths(platform="darwin", environ={}, home=home)
        linux = default_app_paths(
            platform="linux", environ={"XDG_DATA_HOME": "/data",
                                        "XDG_CACHE_HOME": "/cache"}, home=home)

        self.assertEqual(Path("C:/Local/LegadoBookSourceCheck"), windows.data)
        self.assertEqual(windows.data / "cache", windows.cache)
        self.assertEqual(home / "Library/Application Support/LegadoBookSourceCheck",
                         macos.data)
        self.assertEqual(home / "Library/Caches/LegadoBookSourceCheck", macos.cache)
        self.assertEqual(Path("/data/LegadoBookSourceCheck"), linux.data)
        self.assertEqual(Path("/cache/LegadoBookSourceCheck"), linux.cache)

    def test_packaged_seed_is_copied_and_changes_save_to_user_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            resource = root / "readonly-resource"
            data = root / "user-data"
            cache = root / "user-cache"
            resource.mkdir()
            seed_path = resource / "bookSource.json"
            seed = [{"bookSourceName": "seed", "bookSourceUrl": "https://seed"}]
            seed_path.write_text(json.dumps(seed), encoding="utf-8")

            service = SourceService(resource, data_dir=data, cache_dir=cache)
            service.load()
            service.sources.append(BookSource(
                bookSourceName="imported", bookSourceUrl="https://imported"))
            service.save()

            self.assertEqual(seed, json.loads(seed_path.read_text(encoding="utf-8")))
            self.assertEqual(service.sources_path, Path(service.current_path))
            reopened = SourceService(resource, data_dir=data, cache_dir=cache)
            reopened.load()
            self.assertEqual(["https://seed", "https://imported"],
                             [source.bookSourceUrl for source in reopened.sources])

    def test_old_reader_cache_is_copied_to_new_cache_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_file = root / "data" / "reader-cache" / "book" / "catalog.json"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("{}", encoding="utf-8")
            service = SourceService(
                root / "resource", data_dir=root / "data",
                cache_dir=root / "cache")
            self.assertTrue((service.cache_dir / "reader-cache" /
                             "book" / "catalog.json").exists())

    def test_desktop_window_uses_wide_layout_and_mobile_keeps_native_size(self):
        desktop = SimpleNamespace(
            platform=ft.PagePlatform.WINDOWS, window=SimpleNamespace(
                width=None, height=None, min_width=None, min_height=None))
        BookSourceApp._configure_window(desktop)
        self.assertEqual((desktop.window.width, desktop.window.height), (1100, 780))
        self.assertEqual(
            (desktop.window.min_width, desktop.window.min_height), (720, 600))

        mobile = SimpleNamespace(
            platform=ft.PagePlatform.ANDROID, window=SimpleNamespace(
                width=None, height=None, min_width=None, min_height=None))
        BookSourceApp._configure_window(mobile)
        self.assertEqual(
            (mobile.window.width, mobile.window.height,
             mobile.window.min_width, mobile.window.min_height),
            (None, None, None, None))

    def test_app_defaults_to_reading_navigation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict("os.environ", {
                    "LEGADO_BOOKSOURCE_DATA_DIR": str(root / "data"),
                    "LEGADO_BOOKSOURCE_CACHE_DIR": str(root / "cache")}, clear=False):
                app = BookSourceApp(root)
            self.assertEqual(app._current_index, 1)

    def test_page_keyboard_event_dispatches_only_in_reader(self):
        with TemporaryDirectory() as directory:
            app = BookSourceApp(Path(directory))
        reader = SimpleNamespace(
            _section="reader", _on_reader_key=unittest.mock.Mock())
        app._reading_view = reader
        app._current_index = 1
        event = SimpleNamespace(key="Page Down")

        app._on_keyboard_event(event)
        reader._on_reader_key.assert_called_once_with(event)

        reader._section = "detail"
        app._on_keyboard_event(event)
        reader._on_reader_key.assert_called_once_with(event)

    def test_app_navigation_adapts_without_rebuilding_views(self):
        with TemporaryDirectory() as directory:
            app = BookSourceApp(Path(directory))
        app._bottom_nav = SimpleNamespace(visible=None)
        app._side_nav = SimpleNamespace(
            visible=None, extended=None, label_type=None)
        app._side_divider = SimpleNamespace(visible=None)
        app._page = SimpleNamespace(
            width=430, window=SimpleNamespace(width=1100))

        app._apply_navigation_layout()
        self.assertTrue(app._bottom_nav.visible)
        self.assertFalse(app._side_nav.visible)

        app._page.width = 1100
        app._apply_navigation_layout()
        self.assertFalse(app._bottom_nav.visible)
        self.assertTrue(app._side_nav.visible)
        self.assertTrue(app._side_nav.extended)

        app._reader_immersive = True
        app._apply_navigation_layout()
        self.assertFalse(app._bottom_nav.visible)
        self.assertFalse(app._side_nav.visible)

    def test_bookshelf_refresh_and_group_state_survive_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            source = BookSource(
                bookSourceName="source", bookSourceUrl="https://source")
            book = BookItem("book", url="https://book")
            service.add_to_bookshelf(source, book)
            service.update_bookshelf_check(
                source.bookSourceUrl, book.url,
                latest_title="chapter 8", total_chapters=8)
            service.update_bookshelf_options(
                source.bookSourceUrl, book.url, groupName="追更")

            reopened = SourceService(root, data_dir=root / "data")
            reopened.load_bookshelf()
            item = reopened.bookshelf[0]
            self.assertEqual(item["latestChapterTitle"], "chapter 8")
            self.assertEqual(item["lastCheckCount"], 8)
            self.assertEqual(item["groupName"], "追更")

    def test_switch_bookshelf_source_keeps_user_progress_and_group(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            old_source = BookSource(
                bookSourceName="old", bookSourceUrl="https://old")
            new_source = BookSource(
                bookSourceName="new", bookSourceUrl="https://new")
            old_book = BookItem("book", author="author", url="https://old/book")
            new_book = BookItem(
                "book", author="author", url="https://new/book",
                last_chapter="chapter 20")
            chapter = ChapterItem("chapter 8", "https://old/chapter/8", index=7)
            self.assertTrue(service.add_to_bookshelf(old_source, old_book))
            service.update_bookshelf_options(
                old_source.bookSourceUrl, old_book.url, groupName="追更", order=9)
            service.update_bookshelf_state(
                old_source.bookSourceUrl, old_book.url, chapter=chapter,
                scroll_offset=200, scroll_ratio=0.4, font_size=20)
            item = service.bookshelf[0]

            self.assertTrue(service.switch_bookshelf_source(
                item, new_source, new_book))

            self.assertEqual(item["source_url"], new_source.bookSourceUrl)
            self.assertEqual(item["source_name"], new_source.bookSourceName)
            self.assertEqual(item["url"], new_book.url)
            self.assertEqual(item["groupName"], "追更")
            self.assertEqual(item["order"], 9)
            self.assertEqual(item["reading"]["chapter_index"], 7)
            self.assertEqual(item["reading"]["chapter_url"], "")
            self.assertEqual(item["reading"]["font_size"], 20)
            self.assertEqual(item["reading"]["scroll_offset"], 0.0)
            self.assertEqual(item["reading"]["scroll_ratio"], 0.0)

            reopened = SourceService(root, data_dir=root / "data")
            reopened.load_bookshelf()
            self.assertEqual(reopened.bookshelf[0]["origin"], "https://new")

    def test_switch_bookshelf_source_rejects_duplicate_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            old_source = BookSource(
                bookSourceName="old", bookSourceUrl="https://old")
            new_source = BookSource(
                bookSourceName="new", bookSourceUrl="https://new")
            old_book = BookItem("book", url="https://old/book")
            new_book = BookItem("book", url="https://new/book")
            service.add_to_bookshelf(old_source, old_book)
            service.add_to_bookshelf(new_source, new_book)

            self.assertFalse(service.switch_bookshelf_source(
                service.bookshelf[0], new_source, new_book))
            self.assertEqual(service.bookshelf[0]["source_url"], "https://old")

    def test_switch_bookshelf_source_resets_stale_cache_counters(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            old_source = BookSource(
                bookSourceName="old", bookSourceUrl="https://old")
            new_source = BookSource(
                bookSourceName="new", bookSourceUrl="https://new")
            old_book = BookItem("book", url="https://old/book")
            service.add_to_bookshelf(old_source, old_book)
            service.update_bookshelf_options(
                old_source.bookSourceUrl, old_book.url,
                cachedChapterCount=1902, cacheTotalChapterCount=2247,
                cacheErrorCount=3, cacheUpdatedAt=123)
            record = service.bookshelf[0]

            self.assertTrue(service.switch_bookshelf_source(
                record, new_source, BookItem("book", url="https://new/book")))

            self.assertEqual(record["cachedChapterCount"], 0)
            self.assertEqual(record["cacheTotalChapterCount"], 0)
            self.assertEqual(record["cacheErrorCount"], 0)
            self.assertEqual(record["cacheUpdatedAt"], 0)

    def test_find_bookshelf_item_resolves_reloaded_alias_records(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            source = BookSource(
                bookSourceName="source", bookSourceUrl="https://source")
            book = BookItem("book", url="https://book")
            service.add_to_bookshelf(source, book)
            stale = service.bookshelf[0]

            service.load_bookshelf()
            live = service.find_bookshelf_item(stale)
            self.assertIs(live, service.bookshelf[0])
            self.assertIsNot(live, stale)
            self.assertIs(service.find_bookshelf_item(
                {"origin": "https://source", "bookUrl": "https://book"}), live)

            service.remove_from_bookshelf("https://source", "https://book")
            self.assertIsNone(service.find_bookshelf_item(stale))

    def test_catalog_and_content_survive_cache_reopen(self):
        with TemporaryDirectory() as directory:
            cache = ReadingCache(directory)
            chapters = [
                ChapterItem("one", "https://source/1", index=0),
                ChapterItem("two", "https://source/2", index=1, is_vip=True),
            ]
            document = ChapterContent(
                [ContentBlock("text", "body"),
                 ContentBlock("image", b"\x89PNG", {"Referer": "x"})],
                title="one")
            cache.save_catalog("https://source", "https://book", chapters)
            cache.save_content(
                "https://source", "https://book", chapters[0].url, document)

            reopened = ReadingCache(directory)
            loaded_chapters = reopened.load_catalog(
                "https://source", "https://book")
            loaded_document = reopened.load_content(
                "https://source", "https://book", chapters[0].url)

            self.assertEqual(
                [(item.name, item.url) for item in loaded_chapters],
                [(item.name, item.url) for item in chapters])
            self.assertEqual(loaded_document.title, "one")
            self.assertEqual(loaded_document.blocks[1].value, b"\x89PNG")

    def test_scroll_position_and_font_survive_service_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = SourceService(root, data_dir=root / "data")
            source = BookSource(
                bookSourceName="source", bookSourceUrl="https://source")
            book = BookItem("book", url="https://book")
            chapter = ChapterItem(
                "chapter", "https://source/chapter", index=7)
            self.assertTrue(service.add_to_bookshelf(source, book))
            self.assertTrue(service.update_bookshelf_state(
                source.bookSourceUrl, book.url, chapter=chapter,
                scroll_offset=842.5, scroll_ratio=0.42, font_size=21))

            reopened = SourceService(root, data_dir=root / "data")
            reopened.load_bookshelf()
            reading = reopened.bookshelf[0]["reading"]
            self.assertEqual(reading["chapter_url"], chapter.url)
            self.assertEqual(reading["scroll_offset"], 842.5)
            self.assertEqual(reading["scroll_ratio"], 0.42)
            self.assertEqual(reading["font_size"], 21)
            self.assertGreater(reading["last_read_at"], 0)
            item = reopened.bookshelf[0]
            self.assertEqual(item["bookUrl"], book.url)
            self.assertEqual(item["origin"], source.bookSourceUrl)
            self.assertEqual(item["durChapterIndex"], chapter.index)
            self.assertEqual(item["durChapterPos"], 0)
            self.assertEqual(item["durChapterTitle"], chapter.name)
            self.assertGreater(item["durChapterTime"], 0)


class BuiltinBrowserFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The rendering step that runs before the visible window is offered."""

    async def _render(self, fetcher, options=None):
        with patch("app.services.web_page_renderer.available_fetcher",
                   return_value=fetcher):
            return await WebPageRenderer(lambda: None)._render_silently(
                "https://site.example/c", options or {})

    async def test_a_rendered_chapter_is_used_without_asking_the_user(self):
        fetcher = AsyncMock(return_value={"body": "<p>chapter</p>"})
        rendered = await self._render(fetcher, {"mode": "webJs"})
        self.assertEqual(rendered, {"body": "<p>chapter</p>"})
        fetcher.assert_awaited_once_with("https://site.example/c", None,
                                         {"mode": "webJs"})

    async def test_a_challenge_page_still_asks_for_the_visible_window(self):
        for body in ("",
                     "<html><body>Attention Required! | Cloudflare</body></html>",
                     "<html><body><form><input type='password'></form></body></html>"):
            with self.subTest(body=body):
                fetcher = AsyncMock(return_value={"body": body})
                self.assertIsNone(await self._render(fetcher))

    async def test_a_missing_kernel_leaves_the_visible_window_as_the_fallback(self):
        self.assertIsNone(await self._render(None))

    async def test_a_launch_failure_falls_back_instead_of_breaking_the_read(self):
        fetcher = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertIsNone(await self._render(fetcher))

    def test_a_rendered_article_is_not_mistaken_for_a_login_form(self):
        article = ("<html><body><p>" + "正文。" * 2000
                   + "</p><a href='/login'>登录</a></body></html>")
        self.assertFalse(looks_like_gate(article))


class _FakeCastBrowser:
    """Records what the dialog asked the screen cast to do."""

    def __init__(self):
        self.started = []
        self.input = []
        self.viewport = (400, 480)
        self.closed = False
        self.response = {"body": "<p>chapter</p>", "cookies": [{"name": "cf",
                                                                "value": "1"}]}

    async def start(self, url, options=None):
        self.started.append((url, options))

    def cast_viewport(self):
        return self.viewport

    async def tap(self, x, y, button="left"):
        self.input.append(("tap", x, y, button))

    async def press(self, x, y):
        self.input.append(("press", x, y))

    async def move_to(self, x, y):
        self.input.append(("move", x, y))

    async def release(self):
        self.input.append(("release",))

    async def scroll(self, x, y, delta_x, delta_y):
        self.input.append(("scroll", x, y, delta_x, delta_y))

    async def type_text(self, text):
        self.input.append(("type", text))

    async def capture(self, *_args):
        return self.response

    async def close(self):
        self.closed = True


class ScreencastDialogTests(unittest.IsolatedAsyncioTestCase):
    """The desktop fallback: a cast page inside the dialog, driven by gestures."""

    def setUp(self):
        self.browser = _FakeCastBrowser()
        self.statuses = []
        self.dialogs = []
        self.tasks = []

        def run_task(function, *args):
            task = asyncio.ensure_future(function(*args))
            self.tasks.append(task)
            return task

        self.page = SimpleNamespace(
            platform=ft.PagePlatform.WINDOWS,
            show_dialog=self.dialogs.append,
            pop_dialog=lambda: self.dialogs.pop(),
            run_task=run_task,
            update=lambda: None)
        self.renderer = WebPageRenderer(lambda: self.page, self.statuses.append)

    def _patches(self, fetcher=object()):
        return [
            patch("app.services.web_page_renderer.available_fetcher",
                  return_value=fetcher),
            patch("app.services.web_page_renderer.InteractiveBrowser",
                  lambda **_kwargs: self.browser),
        ]

    async def _start_stream(self, url="https://host/gate", options=None):
        with self._patches()[0], self._patches()[1]:
            return await self.renderer._start_stream(url, options or {})

    async def test_the_dialog_carries_a_live_cast_an_input_row_and_a_hint(self):
        content = await self._start_stream(
            options={"headerMap": {"Referer": "https://host/"}})
        self.assertEqual(self.browser.started,
                         [("https://host/gate",
                           {"headerMap": {"Referer": "https://host/"}})])
        self.assertEqual(content.width, VIEWPORT["width"])
        detector = content.content.controls[0]
        self.assertIsInstance(detector, ft.GestureDetector)
        self.assertIsInstance(detector.content, ft.Image)
        self.assertEqual(detector.on_tap_down, self.renderer._on_stream_tap)
        self.assertEqual(detector.on_secondary_tap_down,
                         self.renderer._on_stream_secondary_tap)
        self.assertEqual(detector.on_scroll, self.renderer._on_stream_scroll)
        self.assertEqual(detector.on_pan_start,
                         self.renderer._on_stream_pan_start)
        self.assertEqual(detector.on_pan_update,
                         self.renderer._on_stream_pan_update)
        self.assertEqual(detector.on_pan_end, self.renderer._on_stream_pan_end)
        row = content.content.controls[2]
        self.assertEqual(row.controls[0], self.renderer._typed_text)
        self.assertEqual(row.controls[1].content, "输入")

    async def test_a_cast_frame_becomes_a_jpeg_data_uri(self):
        await self._start_stream()
        self.renderer._push_frame(b"\xff\xd8jpeg")
        self.assertEqual(
            self.renderer._stream_view.src,
            "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())

    async def test_gestures_are_scaled_from_the_control_onto_the_cast(self):
        await self._start_stream()
        # The control is half the cast, so a gesture arrives at twice the point.
        view = self.renderer._stream_view
        view.width, view.height = 200, 240
        event = SimpleNamespace(local_position=ft.Offset(10, 20))
        self.renderer._on_stream_tap(event)
        self.renderer._on_stream_scroll(
            SimpleNamespace(local_position=ft.Offset(10, 20),
                            scroll_delta=ft.Offset(0, 40)))
        self.renderer._on_stream_pan_start(event)
        self.renderer._on_stream_pan_update(event)
        self.renderer._on_stream_pan_end(event)
        await asyncio.sleep(0)
        self.assertEqual(self.browser.input, [
            ("tap", 20.0, 40.0, "left"),
            ("scroll", 20.0, 40.0, 0.0, 40.0),
            ("press", 20.0, 40.0),
            ("move", 20.0, 40.0),
            ("release",)])

    async def test_a_secondary_tap_reaches_the_page_as_a_right_click(self):
        await self._start_stream()
        self.renderer._on_stream_secondary_tap(
            SimpleNamespace(local_position=ft.Offset(5, 6)))
        await asyncio.sleep(0)
        self.assertEqual(self.browser.input, [("tap", 5.0, 6.0, "right")])

    async def test_typed_text_is_sent_and_the_field_is_cleared(self):
        await self._start_stream()
        self.renderer._typed_text.value = "1234"
        self.renderer._submit_typed_text(None)
        await asyncio.sleep(0)
        self.assertEqual(self.renderer._typed_text.value, "")
        self.assertEqual(self.browser.input, [("type", "1234")])

    async def test_an_empty_submission_is_not_sent(self):
        await self._start_stream()
        self.renderer._typed_text.value = ""
        self.renderer._submit_typed_text(None)
        await asyncio.sleep(0)
        self.assertEqual(self.browser.input, [])

    async def test_a_read_page_hands_its_cookies_to_the_silent_browser(self):
        await self._start_stream()
        self.renderer._future = asyncio.get_running_loop().create_future()
        adopt = AsyncMock()
        with patch("app.services.web_page_renderer.BROWSER",
                   SimpleNamespace(adopt_cookies=adopt)):
            await self.renderer._capture_desktop()
        adopt.assert_awaited_once_with([{"name": "cf", "value": "1"}])
        self.assertEqual(self.renderer._future.result()["body"], "<p>chapter</p>")
        self.assertEqual(self.dialogs, [])

    async def test_an_unsolved_gate_keeps_the_dialog_open(self):
        await self._start_stream()
        self.renderer._future = asyncio.get_running_loop().create_future()
        self.browser.response = {"body": "<html><title>Just a moment</title>",
                                 "cookies": []}
        adopt = AsyncMock()
        with patch("app.services.web_page_renderer.BROWSER",
                   SimpleNamespace(adopt_cookies=adopt)):
            await self.renderer._capture_desktop()
        self.assertFalse(self.renderer._future.done())
        adopt.assert_not_awaited()
        self.assertIn("尚未完成", self.statuses[-1])

    async def test_a_failed_read_is_reported_instead_of_crashing_the_dialog(self):
        await self._start_stream()
        self.renderer._future = asyncio.get_running_loop().create_future()
        self.browser.capture = AsyncMock(side_effect=RuntimeError("page gone"))
        await self.renderer._capture_desktop()
        self.assertFalse(self.renderer._future.done())
        self.assertEqual(self.statuses[-1], "读取内置浏览器页面失败：page gone")

    async def test_the_cast_is_closed_once_the_page_has_been_read(self):
        with self._patches()[0], self._patches()[1], \
                patch.object(WebPageRenderer, "_render_silently",
                             AsyncMock(return_value=None)):
            task = asyncio.create_task(self.renderer.render(
                "https://host/gate", SimpleNamespace(bookSourceName="Test"), {}))
            for _ in range(10):
                if self.dialogs:
                    break
                await asyncio.sleep(0)
            await self.renderer._capture_desktop()
            result = await task
        self.assertEqual(result["body"], "<p>chapter</p>")
        self.assertTrue(self.browser.closed)
        self.assertIsNone(self.renderer._desktop)

    async def test_a_missing_kernel_says_how_to_install_it(self):
        with self._patches(fetcher=None)[0]:
            with self.assertRaises(RuntimeError) as caught:
                await self.renderer._start_stream("https://host/gate", {})
        self.assertIn("playwright install chromium", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
