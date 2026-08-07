import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import check_service
from app.models.book_source import BookSource
from app.services.legado_js import LegadoJsRuntime
from app.services.rule_engine import ExploreCategory
from app.services.rule_engine import ReaderEngine


def _source(**overrides):
    values = {
        "bookSourceName": "speed-test",
        "bookSourceUrl": "https://example.com",
        "searchUrl": "/search?key={{key}}",
        "ruleSearch": {},
        "ruleExplore": {},
        "header": "",
        "enabled": True,
        "enabledExplore": True,
        "exploreUrl": "分类::/list",
    }
    values.update(overrides)
    source = SimpleNamespace(**values)
    source.to_dict = lambda: dict(values)
    return source


class CheckSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_category_network_waits_overlap(self):
        async def fake_test_one(source, index, session, sem, timeout):
            await asyncio.sleep(0.08)
            return {
                "idx": index, "name": source["bookSourceName"],
                "raw_url": source["bookSourceUrl"], "status": "ok",
                "search_status": "ok", "book_count": 1, "error": None,
            }

        async def fake_categories(source, session, sem, timeout=10):
            await asyncio.sleep(0.08)
            return []

        started = time.perf_counter()
        with patch.object(check_service, "test_one", fake_test_one), patch.object(
                check_service, "check_categories", fake_categories):
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)
        elapsed = time.perf_counter() - started

        self.assertEqual(results[0]["check_state"], "usable")
        self.assertLess(elapsed, 0.14)

    async def test_static_categories_do_not_eagerly_create_reading_engine(self):
        async def fast_page(source, category, page, session, global_sem,
                            source_sem, timeout, get_engine=None,
                            prepared_rules=None, prepared_headers=None):
            return {
                "page": page, "valid": True, "status_code": 200,
                "item_count": 1, "content_type": "text/html", "error": None,
                "attempts": 1, "persistent_rate_limit": False,
            }

        with patch.object(check_service, "resolve_explore_categories",
                          AsyncMock(return_value=[ExploreCategory("分类", "/list")])), \
                patch.object(check_service, "_fetch_tag_page", fast_page), \
                patch.object(check_service, "LegadoReadingEngine") as engine_type:
            results = await check_service.check_categories(
                _source(), session=object(), sem=asyncio.Semaphore(1), timeout=1)

        self.assertTrue(results[0]["valid"])
        engine_type.assert_not_called()

    async def test_explore_fallback_can_reuse_downloaded_document(self):
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
