import json
import unittest
import flet as ft
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, PropertyMock

from app.models.book_source import BookSource
from app.services.reading_cache import ReadingCache
from app.services.app_paths import default_app_paths
from app.services.reading_engine import (ChapterContent, ContentBlock,
                                         parse_content_blocks)
from app.services.rule_engine import BookItem, ChapterItem
from app.services.source_service import SourceService
from app.views.reading_view import ReadingView
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
        self.view.engine.js.close()
        self.directory.cleanup()

    async def test_reader_toolbar_is_immersive_and_keeps_core_actions(self):
        self.assertEqual(len(self.view._reader_toolbar.content.controls), 9)
        self.assertFalse(self.view._reader_toolbar.visible)
        self.assertEqual(self.view._font_size, 16)
        self.assertIs(self.view._reader_interaction, self.view._reader)
        self.assertIsInstance(self.view._reader, ft.Column)
        self.assertEqual(self.view._reader.scroll, ft.ScrollMode.AUTO)
        self.assertIsNotNone(self.view._reader_document.on_click)

    async def test_text_novel_content_uses_paragraph_reading_layout(self):
        document = ChapterContent([ContentBlock(
            "text",
            "第一章 初见\r\n\r\n 第一段正文。\n第二段正文。\n\n")])

        controls = self.view._render_content(document)

        self.assertEqual([control.value for control in controls], [
            "第一章 初见", "　　第一段正文。", "　　第二段正文。"])
        self.assertEqual(controls[0].data, "reader-heading")
        self.assertEqual(controls[0].size, self.view._font_size + 2)
        self.assertEqual(controls[0].style.height, 1.45)
        self.assertEqual(controls[1].data, "reader-text")
        self.assertEqual(controls[1].style.height, 1.7)
        self.assertTrue(all(not control.selectable for control in controls))

    async def test_font_size_preserves_text_heading_hierarchy(self):
        self.view._content_host.controls = self.view._render_content(
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
            reopened.engine.js.close()

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
            reopened.engine.js.close()

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
            reopened.engine.js.close()

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

    async def test_remount_reuses_existing_bookshelf_controls(self):
        self.view._bookshelf_rendered = True
        self.view._render_bookshelf = unittest.mock.Mock()

        self.view.did_mount()

        self.view._render_bookshelf.assert_not_called()

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
        source = SimpleNamespace(
            enabled=True, searchUrl="/search?key={{key}}", exploreUrl="",
            display_name="二三小说", bookSourceUrl="https://source")
        self.view.service.sources = [source]
        self.view.current_source = source
        self.view.update = lambda: None

        self.view.refresh_sources(update=False)
        await self.view._load_categories(source)

        self.assertEqual("二三小说 · 仅搜索", self.view._source.options[0].text)
        self.assertEqual("该书源仅支持关键词搜索，请输入书名或作者",
                         self.view._status.value)
        self.assertEqual([], self.view._category_host.controls)

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
        self.view._webview_initial_url = "https://host/start"
        self.view._webview_source_regex = r"https://cdn/.+\.m3u8"
        self.view._webview_override_regex = ""
        result = self.view._select_webview_payload({
            "html": "<main>page</main>", "url": "https://host/page",
            "resources": ["https://cdn/video.m3u8"]})
        self.assertEqual(result["source"], "https://cdn/video.m3u8")
        self.assertEqual(result["body"], "https://cdn/video.m3u8")
        self.assertEqual(result["url"], "https://host/start")

        self.view._webview_source_regex = ""
        self.view._webview_override_regex = r"https://host/final/\d+"
        result = self.view._select_webview_payload({
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


if __name__ == "__main__":
    unittest.main()
