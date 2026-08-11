import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import check_service
from app.models.book_source import BookSource
from app.services.legado_js import LegadoJsRuntime
from app.services.rule_engine import ExploreCategory, ReaderEngine


def _source(**overrides):
    values = {
        "bookSourceName": "speed-test",
        "bookSourceUrl": "https://example.com",
        "searchUrl": "/search?key={{key}}",
        "ruleSearch": {}, "ruleExplore": {}, "ruleBookInfo": {},
        "ruleToc": {}, "ruleContent": {}, "header": "", "enabled": True,
        "enabledExplore": True, "exploreUrl": "分类::/list",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CheckSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sources_use_independent_engines_concurrently(self):
        active = 0
        peak = 0

        async def fake_search(source, engine, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.08)
            active -= 1
            return ({"search_status": "no_results", "book_count": 0,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None}, [])

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        started = time.perf_counter()
        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            await check_service.check_sources(
                [_source(enabledExplore=False, exploreUrl="") for _ in range(2)],
                concurrency=2, timeout=1)
        elapsed = time.perf_counter() - started

        self.assertEqual(peak, 2)
        self.assertLess(elapsed, 0.14)

    async def test_categories_use_full_rule_engine(self):
        engine = SimpleNamespace(
            explore=AsyncMock(return_value=[SimpleNamespace(name="book")]),
            js=SimpleNamespace(close=lambda: None))
        category_resolver = AsyncMock(
            return_value=[ExploreCategory("分类", "/list")])
        with patch.object(
                check_service, "resolve_explore_categories",
                category_resolver):
            results = await check_service.check_categories(
                _source(), timeout=1, engine=engine)

        self.assertTrue(results[0]["valid"])
        engine.explore.assert_awaited_once()
        category_resolver.assert_awaited_once_with(
            unittest.mock.ANY, runtime=engine.js)

    async def test_reading_chain_checks_detail_catalog_and_content(self):
        book = SimpleNamespace(name="book", url="https://example/book")
        chapter = SimpleNamespace(
            name="chapter", url="https://example/chapter", is_volume=False)
        document = SimpleNamespace(
            blocks=[SimpleNamespace(kind="text", value="body")], raw_content="body")
        engine = SimpleNamespace(
            book_info=AsyncMock(return_value=book),
            chapters=AsyncMock(return_value=[chapter]),
            content_document=AsyncMock(return_value=document))

        result = await check_service._reading_chain_check(
            _source(), engine, [book], timeout=1)

        self.assertEqual(result["reading_status"], "ok")
        self.assertEqual(result["reading_stage"], "complete")
        self.assertEqual(result["chapter_count"], 1)
        engine.book_info.assert_awaited_once()
        engine.chapters.assert_awaited_once()
        engine.content_document.assert_awaited_once()

    def test_result_classification_distinguishes_reading_failures(self):
        self.assertEqual(check_service._classify_result({
            "search_status": "ok", "tag_valid": 0,
            "reading_status": "rule_error", "status": "ok",
        }), "content_unavailable")
        self.assertEqual(check_service._classify_result({
            "search_status": "ok", "tag_valid": 0,
            "reading_status": "ok", "status": "ok",
        }), "fully_usable")

    async def test_explore_document_reuses_downloaded_response(self):
        engine = ReaderEngine()
        engine._request = AsyncMock(return_value=(
            '<a href="/book/1">Book</a>', "text/html",
            "https://example.com/list"))
        engine.explore_document = AsyncMock(return_value=[])
        source = _source(ruleSearch={"bookList": "a"})
        try:
            await engine.explore(source, ExploreCategory("分类", "/list"))
        finally:
            engine.js.close()

        engine._request.assert_awaited_once()
        engine.explore_document.assert_awaited_once_with(
            source, '<a href="/book/1">Book</a>', "text/html",
            "https://example.com/list", 1)


class LocalHotPathTests(unittest.TestCase):
    def test_source_cached_text_tracks_field_changes(self):
        source = BookSource(bookSourceName="Old", bookSourceUrl="https://old.test")
        self.assertIn("old", source.search_text)
        self.assertEqual(source.short_url, "old.test")
        source.bookSourceName = "New"
        source.bookSourceUrl = "https://new.test/path"
        self.assertIn("new", source.search_text)
        self.assertNotIn("old\n", source.search_text)
        self.assertEqual(source.short_url, "new.test")

    def test_script_normalization_cache_reuses_result(self):
        LegadoJsRuntime._clean_script.cache_clear()
        script = "@js: const value = 1; value"
        self.assertEqual(LegadoJsRuntime._clean_script(script),
                         LegadoJsRuntime._clean_script(script))
        self.assertGreaterEqual(LegadoJsRuntime._clean_script.cache_info().hits, 1)


if __name__ == "__main__":
    unittest.main()
