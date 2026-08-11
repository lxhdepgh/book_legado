import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.book_cache_service import WholeBookCacheService
from app.services.reading_cache import ReadingCache
from app.services.reading_engine import ChapterContent, ContentBlock
from app.services.rule_engine import BookItem, ChapterItem


class _Js:
    def close(self):
        pass


class _Engine:
    def __init__(self):
        self.js = _Js()
        self.js_variables = {}
        self.current_book = None
        self.calls = []
        self.attempts = {}
        self.last_response = None

    async def chapters(self, _source, _book):
        return [ChapterItem("One", "https://book/1", index=0),
                ChapterItem("Volume", "", index=1, is_volume=True),
                ChapterItem("Two", "https://book/2", index=2)]

    async def content_document(self, _source, chapter, _next_url=""):
        self.calls.append(chapter.url)
        self.attempts[chapter.url] = self.attempts.get(chapter.url, 0) + 1
        if chapter.url.endswith("/2") and self.attempts[chapter.url] < 2:
            raise RuntimeError("temporary")
        blocks = [ContentBlock("text", chapter.name)]
        if chapter.url.endswith("/1"):
            blocks.append(ContentBlock(
                "image", "https://img/1.jpg,{\"headers\":{\"Referer\":\"x\"}}"))
        return ChapterContent(blocks, title=chapter.name)

    async def _request(self, request, _source):
        self.last_response = {"bodyBytes": list(b"image-bytes")}
        self.image_request = request
        return {"body": "", "url": request["url"]}


class WholeBookCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_all_skips_volumes_retries_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = _Engine()
            service = WholeBookCacheService(
                Path(directory) / "cache", Path(directory) / "data",
                engine_factory=lambda: engine, retries=3)
            source = SimpleNamespace(bookSourceUrl="https://source")
            book = BookItem("Book", url="https://book")
            progress = []

            result = await service.cache_book(
                source, book,
                lambda value: progress.append((value.cached, value.failed)))

            self.assertEqual((2, 2, 0),
                             (result.cached, result.total, result.failed))
            self.assertEqual(2, engine.attempts["https://book/2"])
            cache = ReadingCache(Path(directory) / "cache")
            self.assertEqual((2, 2), cache.cache_counts(source.bookSourceUrl, book.url))
            self.assertTrue((cache._book_dir(source.bookSourceUrl, book.url) /
                             ".offline").exists())
            cached_doc = cache.load_content(
                source.bookSourceUrl, book.url, "https://book/1")
            self.assertEqual(b"image-bytes", cached_doc.blocks[1].value)
            self.assertEqual("x", engine.image_request["headers"]["Referer"])

            calls_before = len(engine.calls)
            resumed = await service.cache_book(source, book)
            self.assertEqual(2, resumed.cached)
            self.assertEqual(calls_before, len(engine.calls))
            self.assertEqual((2, 0), progress[-1])


if __name__ == "__main__":
    unittest.main()
