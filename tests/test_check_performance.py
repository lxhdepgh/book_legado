import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import block_pages, check_service
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

    async def test_category_sampling_caps_checked_categories(self):
        engine = SimpleNamespace(
            explore=AsyncMock(return_value=[SimpleNamespace(name="book")]),
            js=SimpleNamespace(close=lambda: None))
        category_resolver = AsyncMock(return_value=[
            ExploreCategory(f"分类{index}", f"/list{index}")
            for index in range(5)])
        with patch.object(
                check_service, "resolve_explore_categories",
                category_resolver):
            results = await check_service.check_categories(
                _source(), timeout=1, engine=engine, max_categories=2)

        self.assertEqual(2, len(results))
        self.assertEqual(2, engine.explore.await_count)

    async def test_category_sampling_stops_at_first_valid_category(self):
        engine = SimpleNamespace(
            explore=AsyncMock(return_value=[SimpleNamespace(name="book")]),
            js=SimpleNamespace(close=lambda: None))
        category_resolver = AsyncMock(return_value=[
            ExploreCategory(f"分类{index}", f"/list{index}")
            for index in range(5)])
        with patch.object(
                check_service, "resolve_explore_categories",
                category_resolver):
            results = await check_service.check_categories(
                _source(), timeout=1, engine=engine, max_categories=5,
                stop_after_valid=True)

        self.assertEqual(1, len(results))
        self.assertTrue(results[0]["valid"])
        engine.explore.assert_awaited_once()

    async def test_blocked_categories_are_still_walked_to_the_sample_cap(self):
        async def explore(source, category, page=1):
            if str(category.title) == "分类2":
                return [SimpleNamespace(name="book")]
            return []

        engine = SimpleNamespace(explore=explore,
                                 js=SimpleNamespace(close=lambda: None))
        category_resolver = AsyncMock(return_value=[
            ExploreCategory(f"分类{index}", f"/list{index}")
            for index in range(5)])
        with patch.object(
                check_service, "resolve_explore_categories",
                category_resolver):
            results = await check_service.check_categories(
                _source(), timeout=1, engine=engine, max_categories=5,
                stop_after_valid=True)

        # A source whose categories are dead needs the samples: that is the
        # evidence for the verdict, so the walk only stops on a success.
        self.assertEqual(3, len(results))
        self.assertEqual([item["valid"] for item in results],
                         [False, False, True])

    async def test_tag_stage_skipped_when_search_proves_host_unreachable(self):
        called = []

        async def fake_search(source, engine, timeout):
            return ({"search_status": "network_error", "book_count": None,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": "无法连接到主机"}, [])

        async def fake_categories(*args, **kwargs):
            called.append("categories")
            return []

        async def fake_probe(*args, **kwargs):
            return {"status": "err", "status_code": None,
                    "response_time_ms": 1, "error": "Cannot connect to host"}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "check_categories", fake_categories), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)

        self.assertEqual([], called)
        self.assertTrue(results[0]["tag_check_skipped"])
        self.assertEqual("unknown", results[0]["check_state"])

    async def test_tag_stage_still_runs_when_only_the_rules_failed(self):
        called = []

        async def fake_search(source, engine, timeout):
            return ({"search_status": "rule_error", "book_count": None,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": "选择器无匹配"}, [])

        async def fake_categories(*args, **kwargs):
            called.append("categories")
            return []

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "check_categories", fake_categories), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)

        # A broken search rule still leaves a reachable host, so explore is
        # the only remaining way to prove the source works.
        self.assertEqual(["categories"], called)
        self.assertFalse(results[0]["tag_check_skipped"])

    async def test_probe_join_gives_up_on_a_blackholed_home_page(self):
        async def fake_probe(*args, **kwargs):
            await asyncio.sleep(30)
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        async def fake_search(source, engine, timeout):
            return ({"search_status": "ok", "book_count": 1,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None},
                    [SimpleNamespace(name="book", url="https://example/book")])

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine), \
                patch.object(check_service, "PROBE_JOIN_BUDGET", 0.05):
            started = time.perf_counter()
            results = await check_service.check_sources(
                [_source(enabledExplore=False, exploreUrl="")],
                concurrency=1, timeout=1)
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 5)
        self.assertEqual("unknown", results[0]["status"])
        self.assertIn("没有响应", results[0]["error"])

    async def test_slow_source_cannot_outlive_its_total_budget(self):
        async def fake_search(source, engine, timeout):
            return ({"search_status": "ok", "book_count": 1,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None},
                    [SimpleNamespace(name="book", url="https://example/book")])

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        async def slow_category(*args, **kwargs):
            await asyncio.sleep(30)
            return []

        class SlowEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

            async def book_info(self, source, book):
                await asyncio.sleep(30)

            async def chapters(self, source, book):
                await asyncio.sleep(30)

            async def content_document(self, source, chapter, need_save=False):
                await asyncio.sleep(30)

        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "check_categories", slow_category), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", SlowEngine), \
                patch.object(check_service, "SOURCE_CHECK_BUDGET", 0.15), \
                patch.object(check_service, "SOURCE_TAG_TIMEOUT", 0.05), \
                patch.object(check_service, "READING_CHAIN_TIMEOUT", 0.05), \
                patch.object(check_service, "READING_CHAIN_BUDGET", 0.05):
            started = time.perf_counter()
            await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 2)

    async def test_reading_chain_honours_the_caller_budget(self):
        books = [SimpleNamespace(name=f"b{i}", url=f"https://example/b{i}")
                 for i in range(3)]

        class HangingEngine:
            js = SimpleNamespace(close=lambda: None)

            async def book_info(self, source, book):
                await asyncio.sleep(30)

            async def chapters(self, source, book):
                await asyncio.sleep(30)

            async def content_document(self, source, chapter, need_save=False):
                await asyncio.sleep(30)

        started = time.perf_counter()
        result = await check_service._reading_chain_check(
            _source(), HangingEngine(), books, timeout=30, budget=0.05)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 2)
        self.assertEqual("timeout", result["reading_status"])

    async def test_slow_explore_pool_does_not_serialize_searches(self):
        search_peak = 0
        search_active = 0
        tag_peak = 0
        tag_active = 0

        async def fake_search(source, engine, timeout):
            nonlocal search_peak, search_active
            search_active += 1
            search_peak = max(search_peak, search_active)
            await asyncio.sleep(0.05)
            search_active -= 1
            return ({"search_status": "no_results", "book_count": 0,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None}, [])

        async def fake_categories(source, session=None, sem=None, timeout=10,
                                  engine=None, max_categories=None, **kwargs):
            nonlocal tag_peak, tag_active
            tag_active += 1
            tag_peak = max(tag_peak, tag_active)
            await asyncio.sleep(0.2)
            tag_active -= 1
            return []

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        sources = [_source() for _ in range(8)]
        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "check_categories", fake_categories), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            await check_service.check_sources(
                sources, concurrency=check_service.CHECK_CONCURRENCY,
                timeout=1)

        self.assertEqual(check_service.CHECK_CONCURRENCY, search_peak)
        self.assertEqual(check_service.TAG_SOURCE_CONCURRENCY, tag_peak)

    async def test_locally_blocked_host_skips_rule_stages(self):
        called = []

        async def fake_search(source, engine, timeout):
            called.append("search")
            return ({"search_status": "no_results", "book_count": 0,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None}, [])

        async def fake_categories(*args, **kwargs):
            called.append("categories")
            return []

        async def fake_probe(*args, **kwargs):
            return {"status": "blocked", "status_code": 200,
                    "response_time_ms": 1,
                    "error": "本机安全软件已拦截该站点（火绒）"}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)

        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "check_categories", fake_categories), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)

        self.assertEqual([], called)
        self.assertEqual("blocked", results[0]["check_state"])
        self.assertEqual("skip", results[0]["reading_status"])

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
        self.assertEqual(result["reading_tried"], 1)
        engine.book_info.assert_awaited_once()
        engine.chapters.assert_awaited_once()
        engine.content_document.assert_awaited_once()

    def _multi_book_engine(self, content_outcomes, chapters=None):
        good = SimpleNamespace(
            blocks=[SimpleNamespace(kind="text", value="body")], raw_content="body")
        empty = SimpleNamespace(blocks=[], raw_content="")
        documents = [good if outcome == "ok" else empty
                     for outcome in content_outcomes]
        async def content_document(source, chapter, need_save=False):
            return documents.pop(0)
        return SimpleNamespace(
            book_info=AsyncMock(side_effect=lambda s, b: b),
            chapters=AsyncMock(return_value=chapters or [SimpleNamespace(
                name="chapter", url="https://example/chapter", is_volume=False)]),
            content_document=content_document)

    async def test_reading_chain_retries_next_book_when_content_empty(self):
        books = [SimpleNamespace(name=f"b{i}", url=f"https://example/b{i}")
                 for i in range(3)]
        engine = self._multi_book_engine(["empty", "ok"])

        result = await check_service._reading_chain_check(
            _source(), engine, books, timeout=5)

        self.assertEqual(result["reading_status"], "ok")
        self.assertEqual(result["reading_tried"], 2)
        self.assertEqual(result["sample_book"], "b1")
        self.assertIsNone(result["reading_error"])

    async def test_reading_chain_stops_on_deterministic_rule_error(self):
        from app.services.legado_rule_parser import RuleParseError
        books = [SimpleNamespace(name=f"b{i}", url=f"https://example/b{i}")
                 for i in range(3)]
        engine = SimpleNamespace(
            book_info=AsyncMock(
                side_effect=RuleParseError("无法解析 JSoup 规则：$.data.list")),
            chapters=AsyncMock(), content_document=AsyncMock())

        result = await check_service._reading_chain_check(
            _source(), engine, books, timeout=5)

        self.assertEqual(result["reading_status"], "rule_error")
        self.assertEqual(result["reading_tried"], 1)
        engine.chapters.assert_not_awaited()

    async def test_reading_chain_caps_candidates_and_total_time(self):
        books = [SimpleNamespace(name=f"b{i}", url=f"https://example/b{i}")
                 for i in range(6)]
        engine = self._multi_book_engine(["empty"] * 6)

        result = await check_service._reading_chain_check(
            _source(), engine, books, timeout=5)

        self.assertEqual(result["reading_tried"],
                         check_service.READING_CHAIN_CANDIDATES)
        self.assertEqual(result["reading_status"], "rule_error")

    async def test_browser_only_content_is_reported_as_unverified(self):
        # 霹雳书屋 renders its chapter body from script, so the empty document a
        # plain HTTP check sees says nothing about the source.
        books = [SimpleNamespace(name="b0", url="https://example/b0")]
        engine = self._multi_book_engine(["empty"])
        engine.webview_fetcher = None
        source = _source(ruleContent={
            "content": "#chapterContent .jsChapterWrapper p@html",
            "webJs": "(()=>{return document.title})()"})

        result = await check_service._reading_chain_check(
            source, engine, books, timeout=5)

        self.assertEqual(result["reading_status"], "needs_browser")
        self.assertEqual(result["reading_stage"], "content")
        self.assertEqual("search_only", check_service._classify_result({
            "search_status": "ok", "tag_valid": 0, **result}))

    async def test_browser_backed_check_still_fails_a_broken_content_rule(self):
        books = [SimpleNamespace(name="b0", url="https://example/b0")]
        engine = self._multi_book_engine(["empty"])
        engine.webview_fetcher = AsyncMock()
        source = _source(ruleContent={"content": "#body p@html",
                                      "webJs": "(()=>{return 1})()"})

        result = await check_service._reading_chain_check(
            source, engine, books, timeout=5)

        self.assertEqual(result["reading_status"], "rule_error")

    async def test_the_check_pipeline_hands_the_builtin_browser_to_the_engine(self):
        # The check has to build its own engines; forgetting the fetcher here
        # would silently turn every webView stage back into "需浏览器未抽检".
        seen = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                seen.update(kwargs)
                self.js = SimpleNamespace(close=lambda: None)

        async def fake_search(source, engine, timeout):
            return ({"search_status": "no_results", "book_count": 0,
                     "search_keyword": "test", "search_method": "rule-engine",
                     "search_error": None}, [])

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        fetcher = AsyncMock()
        with patch.object(check_service, "available_fetcher",
                          return_value=fetcher), \
                patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            await check_service.check_sources(
                [_source(enabledExplore=False, exploreUrl="")],
                concurrency=1, timeout=1)

        self.assertIs(seen.get("webview_fetcher"), fetcher)

    def test_locally_blocked_host_is_not_counted_as_source_failure(self):
        block_page = ("<html><body><h1>拦截提示</h1>"
                      "<p>火绒安全软件已阻止你访问该网站</p></body></html>")
        self.assertEqual(block_pages.block_reason(block_page),
                         "本机安全软件已拦截该站点（火绒）")
        normal = ("<div class='result'><a href='/book/1'>书名</a></div>" * 100
                  + "若页面被拦截请刷新重试")
        self.assertIsNone(block_pages.block_reason(normal))
        self.assertEqual(check_service._classify_result({
            "status": "blocked", "search_status": "no_results", "tag_valid": 0,
            "reading_status": "skip"}), "blocked")

    def test_remote_waf_page_is_read_as_interception(self):
        # 半夏小说 answers every path with this page; parsing it yields no books,
        # which used to be classified as "search only usable".
        cloudflare = ("<!DOCTYPE html><html><head>"
                      "<title>Attention Required! | Cloudflare</title>"
                      "</head><body><h1>Sorry, you have been blocked</h1>"
                      "<p>You are unable to access xbanxia.cc</p></body></html>")
        self.assertEqual(block_pages.block_reason(cloudflare, 403),
                         "目标站点拦截了本次访问（Cloudflare，HTTP 403）")
        seen = block_pages.block_reason(
            cloudflare.replace("Attention Required! | Cloudflare", "Notice"), 403)
        self.assertEqual(seen, "目标站点拦截了本次访问（Cloudflare，HTTP 403）")
        # The same wording inside real content is not interception.
        article = ("<html><body><p>我只是被 Cloudflare 误伤的路人，网上有人说 "
                   "Attention Required! | Cloudflare 很难受。</p></body></html>")
        self.assertIsNone(block_pages.block_reason(article, 200))
        # A challenge page arrives as a redirect/5xx, never as a plain 200.
        challenge = ("<html><head><title>Just a moment...</title></head>"
                     "<body>Checking your browser before accessing xbanxia.cc"
                     "</body></html>")
        self.assertIsNone(block_pages.block_reason(challenge, 200))
        self.assertEqual(block_pages.block_reason(challenge, 503),
                         "目标站点拦截了本次访问（Cloudflare 质询页，HTTP 503）")

    def test_blocked_stages_are_reported_as_interception(self):
        self.assertEqual(check_service._error_kind(
            block_pages.SiteBlockedError("目标站点拦截了本次访问（Cloudflare，HTTP 403）")),
            "blocked")
        self.assertEqual(check_service._classify_result({
            "status": "ok", "status_code": 403, "search_status": "no_results",
            "tag_valid": 0, "reading_status": "skip"}), "blocked")
        self.assertEqual(check_service._classify_result({
            "status": "ok", "status_code": 200, "search_status": "blocked",
            "tag_valid": 0, "reading_status": "skip"}), "blocked")
        # A source whose search really works keeps its verdict even if the home
        # page is protected.
        self.assertEqual(check_service._classify_result({
            "status": "err", "status_code": 403, "search_status": "ok",
            "tag_valid": 0, "reading_status": "skip"}), "search_only")

    async def test_rule_stage_raises_on_interception_page(self):
        engine = ReaderEngine()
        cloudflare = ("<html><head><title>Attention Required! | Cloudflare"
                      "</title></head><body>Sorry, you have been blocked"
                      "</body></html>")

        async def transport(request, source=None):
            engine.last_response = {"status": 403, "body": cloudflare}
            return cloudflare, "text/html", str(request.get("url"))

        engine._request_transport = transport
        with self.assertRaises(block_pages.SiteBlockedError) as caught:
            await engine._request({"url": "https://www.xbanxia.cc/"})
        self.assertIn("Cloudflare", str(caught.exception))

    def test_result_classification_distinguishes_reading_failures(self):
        self.assertEqual(check_service._classify_result({
            "search_status": "ok", "tag_valid": 0,
            "reading_status": "rule_error", "status": "ok",
        }), "content_unavailable")
        self.assertEqual(check_service._classify_result({
            "search_status": "ok", "tag_valid": 0,
            "reading_status": "ok", "status": "ok",
        }), "fully_usable")

    def test_keyword_miss_is_not_classified_unusable(self):
        self.assertEqual(check_service._classify_result({
            "search_status": "no_results", "tag_valid": 0,
            "reading_status": "skip", "status": "ok",
        }), "search_only")

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
