import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.book_source import BookSource
from app.services.reading_cache import ReadingCache
from app.services.reading_engine import ChapterContent, ContentBlock
from app.services.rule_engine import BookItem, ChapterItem
from app.services.source_service import SourceService
from app.views.reading_view import ReadingView
from app.app import BookSourceApp


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

    async def test_reader_toolbar_keeps_navigation_and_font_controls_visible(self):
        self.assertEqual(len(self.view._reader_toolbar.content.controls), 8)
        self.assertEqual(self.view._font_size, 16)
        self.assertIn(self.view._reader_bookshelf_btn,
                      self.view._reader_nav.controls)
        self.assertIn(self.view._directory_btn,
                      self.view._reader_nav.controls)

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
    def test_app_defaults_to_reading_navigation(self):
        with TemporaryDirectory() as directory:
            app = BookSourceApp(Path(directory))
            self.assertEqual(app._current_index, 1)

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
