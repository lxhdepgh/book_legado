import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import block_pages, check_service
from app.models.book_source import BookSource, RULE_FIELDS
from app.services.engine.legado_js import JsContext, LegadoJsRuntime
from app.services.engine.rule_engine import (ExploreCategory, ReaderEngine,
                                      RequestConnectionError,
                                      RequestTimeoutError)


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


async def _noop_aclose():
    """检测/缓存用的引擎替身没有连接池要释放。"""
    pass


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
                self.aclose = _noop_aclose

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
                self.aclose = _noop_aclose

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
                self.aclose = _noop_aclose

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
                self.aclose = _noop_aclose

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
                self.aclose = _noop_aclose

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

            async def aclose(self):
                pass

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
                self.aclose = _noop_aclose

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
                self.aclose = _noop_aclose

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
        from app.services.engine.legado_rule_parser import RuleParseError
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
                self.aclose = _noop_aclose

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
        # 主机级传输失败不该被算成书源规则写错了。
        self.assertEqual(check_service._error_kind(
            RequestTimeoutError("连接超时：")), "timeout")
        self.assertEqual(check_service._error_kind(
            RequestConnectionError("连接失败：拒绝连接")), "network_error")
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
            await engine.aclose()

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


class RuleCompileCacheTests(unittest.TestCase):
    """规则文本相同的编译工作不许按节点数量重做一遍。"""

    def setUp(self):
        from app.services.engine import legado_rule_parser as parser
        self.parser = parser
        for cached in (parser._cached_jsoup_plan, parser._split_rule_expression,
                       parser._jsoup_part):
            cached.cache_clear()

    def test_selector_plan_is_compiled_once_per_selector_text(self):
        parser = self.parser
        compiles = []
        original = parser._jsoup_plan

        def counting(selector, context=None):
            compiles.append(selector)
            return original(selector, context)

        parser._jsoup_plan = counting
        try:
            left = parser.parse_markup(
                "<ul><li><b class='n'>甲</b></li><li><b class='n'>乙</b></li></ul>")
            right = parser.parse_markup(
                "<ul><li><b class='n'>丙</b></li></ul>")
            selector = "b.n"
            nodes = []
            for item in left.select("li"):
                for _ in range(3):
                    nodes.extend(parser.select_jsoup(item, selector))
            second = parser.select_jsoup(right.select("li")[0], selector)
        finally:
            parser._jsoup_plan = original
        self.assertEqual(len(compiles), 1,
                         "同一串选择器被重新编译了 %d 次" % len(compiles))
        self.assertEqual([node.get_text() for node in nodes],
                         ["甲"] * 3 + ["乙"] * 3)
        # 缓存的是"怎么匹配"，不是匹配结果：换了 DOM 必须换答案。
        self.assertEqual([node.get_text() for node in second], ["丙"])

    def test_expression_split_survives_caller_mutation(self):
        parser = self.parser
        parts, operator = parser.split_rule_expression("a@b@c", ("@",), code=False)
        parts.pop()               # jsoup_values 会弹掉尾段
        again, again_operator = parser.split_rule_expression("a@b@c", ("@",),
                                                            code=False)
        self.assertEqual(again, ["a", "b", "c"])
        self.assertEqual(again_operator, "@")


class SharedConnectorPoolTests(unittest.IsolatedAsyncioTestCase):
    """池里的引擎共享一条连接池，淘汰一个不能关掉别人在用的套接字。"""

    @staticmethod
    def _source(url: str):
        return SimpleNamespace(bookSourceUrl=url, enabledCookieJar=True)

    async def test_pool_hands_every_engine_the_same_connector(self):
        from app.services.engine.rule_engine_service import RuleEngineService
        service = RuleEngineService(pool_size=4, timeout=5)
        await service.on_start()
        sessions = []
        try:
            for index in range(3):
                engine = await service._get_engine(self._source(
                    f"https://s{index}.test"))
                sessions.append(engine._session_for(self._source(
                    f"https://s{index}.test"), "", False))
        finally:
            for session in sessions:
                await session.close()
            await service.on_stop()
        connectors = {session._connector for session in sessions}
        self.assertEqual(len(connectors), 1, "引擎各自建了连接池")
        self.assertTrue(all(session._connector_owner is False
                            for session in sessions),
                        "会话认领了共享 connector，关会话就会关全池")

    async def test_eviction_does_not_close_the_shared_connector(self):
        """被淘汰引擎的会话关掉时，共享连接池必须活着留给别的源用。"""
        from app.services.engine.rule_engine_service import RuleEngineService
        service = RuleEngineService(pool_size=2, timeout=5)
        await service.on_start()
        sessions = []
        try:
            for url in ("https://a.test", "https://b.test"):
                engine = await service._get_engine(self._source(url))
                sessions.append(engine._session_for(self._source(url), "", False))
            connector = sessions[0]._connector
            self.assertIs(sessions[1]._connector, connector)
            # 第三个源进场把 a 挤出去，a 的引擎随之 aclose()。
            await service._get_engine(self._source("https://c.test"))
            await asyncio.sleep(0.05)     # 淘汰是 ensure_future 投递的
            self.assertNotIn("https://a.test", service._pool)
            self.assertTrue(sessions[0].closed, "被淘汰引擎的会话没关")
            self.assertFalse(connector.closed,
                             "淘汰冷门源把全池共用的热连接一起关掉了")
            self.assertFalse(sessions[1].closed)
            self.assertIs(sessions[1]._connector, connector)
        finally:
            for session in sessions:
                if not session.closed:
                    await session.close()
            await service.on_stop()
        self.assertTrue(connector.closed)
        self.assertEqual(service._shared_connectors, {})

    async def test_shared_connector_keeps_the_dns_ip_resolver(self):
        """共享池必须带上 dnsIp resolver，否则走这条路的书源钉表形同失效。

        只做类型断言，不调 `resolve()`——少了 resolver 时它会去查真 DNS，
        护栏就变成联网用例了。
        """
        from app.services.engine import rule_engine as RE
        from app.services.engine.rule_engine_service import RuleEngineService
        service = RuleEngineService(pool_size=2, timeout=5)
        await service.on_start()
        try:
            shared = service._shared_connector()
            self.assertIsInstance(shared._resolver, RE._DnsIpResolver,
                                  "共享 connector 没挂 dnsIp resolver，钉表被绕过")
            engine = await service._get_engine(self._source("https://d.test"))
            own = engine._session_for(self._source("https://d.test"), "", False)
            self.assertIsInstance(own._connector._resolver, RE._DnsIpResolver)
        finally:
            await service.on_stop()

    async def test_socks_proxy_keeps_its_own_connector(self):
        """代理会话仍然自建 connector：共享池只服务直连，别把代理塞进公共池。"""
        from app.services.engine.rule_engine_service import RuleEngineService
        service = RuleEngineService(pool_size=2, timeout=5)
        await service.on_start()
        try:
            engine = await service._get_engine(self._source("https://p.test"))
            source = self._source("https://p.test")
            plain = engine._session_for(source, "", False)
            via_proxy = engine._session_for(
                source, "socks5://127.0.0.1:1080", True)
            self.assertIsNot(plain._connector, via_proxy._connector)
            self.assertEqual(list(service._shared_connectors.values()),
                             [plain._connector])
        finally:
            await via_proxy.close()
            await plain.close()
            await service.on_stop()


class JsoupFastPathTests(unittest.TestCase):
    """``tag``/``.class`` 后代链的 find_all 快路径必须与 SoupSieve 逐节点同解。

    快路径的收益来自"少做事"：它绕开通用匹配机，所以任何一点语义偏移都会变成
    取值错书而不报错。护栏因此按**节点身份**（``id``）而不是文本比对，并且要同时
    钉住两件事——两条路径结果一致（差分），以及快路径**确实被走到**（否则整条
    测试可以在快路径退化成"永远不用"时假绿）。
    """

    # (markup, content_type)：规整 HTML / 标签未闭合 / 大小写混排 / 两条 XML 路
    DOCUMENTS = [
        ("<html><body><ul class='list'><li class='item'><a href='/1' class='title'>甲</a>"
         "</li><li class='item'><span class='author'>乙</span></li></ul>"
         "<div id='main'><p class='item'>丙</p>"
         "<div class='Box'><b class='N'>丁</b></div></div></body></html>", ""),
        ("<div class=list><li><p>x<div class=Box><span class='item'>y", ""),
        ("<UL CLASS='Book'><LI><A HREF='#'>a</A></LI><LI CLASS='item'>b</LI></UL>", ""),
        ("<?xml version='1.0'?><items><Item class='Book'><title>甲</title></Item>"
         "<item class='book'><title>乙</title></item></items>", "application/xml"),
        ("<items><Item class='Book'><title>甲</title></Item>"
         "<item class='book'><title>乙</title></item></items>", "text/xml"),
    ]
    # 该走快路径的形状（纯后代链 + tag/.class）。`item`/`LI` 钉标签名大小写，
    # `li.Item` 钉"紧跟在单词字符后的 .class 保持区分大小写"那条复刻判据。
    FASTABLE = ["li", ".item", "li.item", "ul li", ".list .item", "div p",
                "LI", "ul .title", ".Box b", "div li", "item", "li.Item"]
    # 必须留在原路的形状：属性、#id、伪类、组合符、列表、通配、正则后缀
    GATED = ["li[class=item]", "#main", "li:nth-child(2)", ".item:contains(甲)",
             "ul > li", "li, p", "a[href='/1']", "[class]", ".item:not(.title)",
             "li:matches(甲|乙)", ".item[class~=x]", "*"]

    def setUp(self):
        from app.services.engine import legado_rule_parser as parser
        self.parser = parser
        for cached in (parser._cached_jsoup_plan,):
            cached.cache_clear()

    def test_fast_path_matches_soupsieve_node_for_node(self):
        parser = self.parser
        fast_used = 0
        for markup, content_type in self.DOCUMENTS:
            document = parser.parse_markup(markup, content_type)
            contexts = [document] + document.find_all(True)
            for selector in self.FASTABLE:
                plan = parser._jsoup_plan(selector)
                if not plan.fast:
                    continue          # 形状没进快路径，由 gated 那条测试负责
                fast_used += 1
                for context in contexts:
                    expected = [id(node) for node in parser._css_select(context, plan)]
                    actual = [id(node) for node in parser._fast_select(context, plan.fast)]
                    self.assertEqual(
                        expected, actual,
                        "快路径与 SoupSieve 结果不同（%s / %s / context=%r）"
                        % (selector, markup[:40], getattr(context, "name", None)))
        # "该发生的仍然发生"：快路径若整体失效，上面的循环会一路 continue 假绿。
        self.assertGreaterEqual(fast_used, len(self.FASTABLE),
                               "没有任何选择器进入快路径，差分是空转")

    def test_select_jsoup_actually_avoids_soupsieve_for_simple_shapes(self):
        parser = self.parser
        document = parser.parse_markup(self.DOCUMENTS[0][0])
        target = document.select_one("ul")
        expected = parser._css_select(target, parser._jsoup_plan(".list .item"))
        self.assertTrue(expected, "样本没有命中，这条测试会是空转")

        def forbidden(*args, **kwargs):
            raise AssertionError("快路径没生效，纯 tag+class 链仍然调了 SoupSieve")

        original = parser._css_select
        parser._css_select = forbidden
        try:
            self.assertEqual([id(node) for node in parser.select_jsoup(target, ".list .item")],
                             [id(node) for node in expected])
            self.assertEqual([node.get_text() for node in parser.select_jsoup(document, "li.item")],
                             ["甲", "乙"])
        finally:
            parser._css_select = original

    def test_gated_shapes_stay_on_the_general_path(self):
        parser = self.parser
        for selector in self.GATED:
            self.assertEqual(parser._jsoup_plan(selector).fast, (),
                             "形状 %r 不该进快路径" % selector)
        # 闸门不能把结果一起改掉：属性选择器照旧取到值。
        document = parser.parse_markup(self.DOCUMENTS[0][0])
        self.assertEqual([node.get_text() for node in parser.select_jsoup(document, "li[class=item]")],
                         ["甲", "乙"])

    def test_xml_builder_keeps_tag_names_case_sensitive(self):
        """真 XML builder 上 SoupSieve 的标签名区分大小写，快路径必须让位。

        ``parse_markup`` 的两条 XML 路都建在 html.parser 上（匹配不区分大小写），
        所以这条走的是 bs4 的 ``xml`` builder：它代表"以后换成真 XML 建树"时
        唯一会被快路径悄悄改掉的语义。
        """
        from bs4 import BeautifulSoup
        parser = self.parser
        document = BeautifulSoup(
            "<items><Item class='Book'>甲</Item><item class='book'>乙</item></items>", "xml")
        plan = parser._jsoup_plan("item")
        self.assertTrue(plan.fast, "样本形状没进快路径，这条差分是空转")
        self.assertEqual([node.name for node in parser._fast_select(document, plan.fast)],
                         ["Item", "item"])
        self.assertEqual([node.name for node in parser.select_jsoup(document, "item")],
                         ["item"],
                         "真 XML 文档上快路径把标签名大小写吞掉了，与 SoupSieve 不同解")


class RuleParsingOffLoopTests(unittest.IsolatedAsyncioTestCase):
    """带 JS 的取值段不许在引擎循环线程上跑。

    同步 `js.evaluate()` 是"提交给 legado-quickjs 线程 + 原地等 future"，等的期间
    **整条服务循环停在那里**。本机实测（`.probe/gil_park_probe.py`，5ms 心跳）：
    循环线程上同步求值一次 200ms 把心跳顶开 **204ms**，而同一次求值改成 await 或
    挪到别的线程只有 **11ms**——QuickJS 的原生 eval 期间不攥 GIL，这段时间本该还给循环。
    8 源并发检测里一条 `@js:` 正文规则 = 123ms 循环占用
    （`.probe/sweep_loop_park.py`，改后 0ms）。

    护栏因此钉"**这条取值没在循环线程上发生**"，而不是钉心跳时长（跨机不可复现）。
    """

    async def asyncSetUp(self):
        # 引擎构造里有 `aiohttp.CookieJar()`，它要一个正在跑的循环——只能放在 asyncSetUp。
        self.engine = ReaderEngine()
        self.loop_ident = threading.get_ident()

    async def _where(self, gate):
        """跑一次 `_parse_off_loop`，返回它实际落在哪条线程（循环 / 别处）。"""
        seen = []

        def parse():
            seen.append(threading.get_ident())
            return "结果"

        value = await self.engine._parse_off_loop(gate, parse)
        self.assertEqual(value, "结果", "取值结果被包装层吃掉了")
        self.assertEqual(len(seen), 1, f"取值没跑或跑了多遍：{seen}")
        return seen[0]

    async def test_a_js_rule_does_not_run_on_the_loop_thread(self):
        ident = await self._where("@js:result")
        self.assertNotEqual(ident, self.loop_ident, "JS 取值仍把引擎循环钉死")

    async def test_a_selector_only_rule_stays_on_the_loop_thread(self):
        # 反空转：闸门若整体失效（一律跳线程），这条会红——线程 hop 比它省下的等待还贵。
        ident = await self._where("p@text")
        self.assertEqual(ident, self.loop_ident, "纯选择器规则不该跳线程")

    async def test_the_gate_recognises_every_shape_that_waits_on_quickjs(self):
        cases = {
            "@js:result": True,                 # 显式 JS 段
            "<js>result</js>": True,            # Legado 的另一种写法
            "/search?q={{key}}": True,          # 模板体也是 @js: 求值（_field_value:1542）
            "h1@text@js:result": True,          # 选择器后面接 JS
            "p@text": False,
            "$.name": False,
            "@css:#content@html": False,
            "h1@text##(\\\\d+)": False,
            "": False,
        }
        for rule, parks in cases.items():
            with self.subTest(rule=rule):
                ident = await self._where(rule)
                off_loop = ident != self.loop_ident
                self.assertEqual(off_loop, parks,
                                 f"{'该跳线程却没跳' if parks else '不该跳线程却跳了'}：{rule}")

    async def test_two_js_batches_on_one_engine_do_not_interleave(self):
        """跳线程必须自带一把引擎锁：共享状态原本靠"取值从不让出循环"天然原子。

        `js_variables`、`java.put` 的书/章变量、`_source_cookie_jars` 都是引擎实例上的
        普通字典；换源扫描一台引擎服务多个源，两段取值交错就跑就串味。
        """
        events = []

        def batch(tag):
            events.append(("in", tag))
            time.sleep(0.05)
            events.append(("out", tag))
            return tag

        await asyncio.gather(self.engine._parse_off_loop("@js:a", batch, "a"),
                             self.engine._parse_off_loop("@js:b", batch, "b"))
        self.assertEqual(len(events), 4, f"有一批没跑：{events}")
        self.assertEqual(events[1], ("out", events[0][1]),
                         f"两段取值在共享状态上交错：{events}")

    async def test_a_js_content_rule_is_extracted_off_loop_with_the_same_text(self):
        """接线证据：真阅读链路里的 `@js:` 正文规则确实走了这条道，且结果一字不变。

        只看 `_parse_off_loop` 自己绿是不够的——机制在、没人调用照样全速 park
        （见记忆 refactor-regression-orphan-methods）。
        """
        from app.services.engine.reading_engine import LegadoReadingEngine, ChapterItem

        rule = "@js:(function(){return '正文段落。';})()"
        source = SimpleNamespace(bookSourceUrl="https://x",
                                 ruleContent={"content": rule},
                                 to_dict=lambda: {"bookSourceUrl": "https://x"})

        class Engine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                return "<div id='c'>离线样本</div>", "text/html", "https://x/chapter"

        threads = []
        orig_evaluate = LegadoJsRuntime.evaluate

        def record(self, script, context):
            threads.append(threading.get_ident())
            return orig_evaluate(self, script, context)

        chapter = ChapterItem("Chapter", "https://x/chapter")
        LegadoJsRuntime.evaluate = record
        try:
            engine = Engine()
            document = await engine.content_document(source, chapter, need_save=False)
        finally:
            LegadoJsRuntime.evaluate = orig_evaluate
        self.assertTrue(threads, "这条正文规则压根没求值，测试是空的")
        self.assertNotIn(self.loop_ident, threads,
                         "阅读链路的 JS 取值仍在引擎循环上")
        self.assertIn("正文段落", document.plain_text)

        # 同一引擎退回"不跳线程"的老路，文本必须逐字节一致：跳线程只改调度，不改语义。
        async def passthrough(self, gate, parse, *args, **kwargs):
            return parse(*args, **kwargs)

        with patch.object(ReaderEngine, "_parse_off_loop", passthrough):
            baseline = await Engine().content_document(source, chapter, need_save=False)
        self.assertEqual(document.plain_text, baseline.plain_text)
        self.assertEqual(document.raw_content, baseline.raw_content)


class QuickJsRuntimeOverlapTests(unittest.TestCase):
    """一个 quickjs runtime 在任一时刻只能待在同一条线程里。

    这才是 `LegadoJsRuntime._QUICKJS_LOCK` 守的不变量，也是静态审计那两处裸
    `quickjs.Context()` 判定为"不必收进锁/dispatcher"的依据。本机实测
    （python-quickjs 1.19.4，全部离线子进程）：

    - 两条线程**同时**进同一个 Context：6/6 崩，
      `Assertion failed: !runtime_data->thread_state, file module.c, line 83`，
      rc=3221226505（0xC0000409 fast-fail，进程当场没了，不是可捕获异常）
      —— `.probe/qjs_shared_context_cross_thread.py`；
    - 每线程各自新建 Context 并发创建+求值，同时唯一 QuickJS 线程在跑真规则：
      4×2000 次 ×3 遍（另加纯 quickjs 面 4×2000×3）0 崩——独立 Context 各带一个
      JSRuntime，彼此没有共享原生状态 —— `.probe/qjs_concurrent_context.py`；
    - 同一个 Context **先后**换线程（2000 次交接 / 2000 轮交替）：存活
      —— `.probe/qjs_context_thread_handover.py`。

    所以闸门钉的是"**重叠**"：既不是"换了线程"，也不是"新建 Context"（合法，且比
    走 dispatcher 快 5.0 倍——600 段预编译 108 ms vs 541 ms；"只加锁不走 dispatcher"
    更是双输：108→158 ms，前台最大等待还从 11.8 ms 顶到 34.4 ms，
    `.probe/audit_js_serialize_cost.py`）。同线程重入是 JS→Python 回调→再 eval
    的正常形状，归 `JS_REENTRY_LIMIT` 管，不在这里拦。
    第二条断言"谁建的 Context 谁用到死"（先后换线程也算红）不是安全线而是设计线：
    上面第 3 条实测说明换线程本身活得下来，但一次交接正是"把 Context 提到模块级
    缓存给审计线程共用"那个错修法的第一步，等它变成第 1 条就晚了。
    """

    @staticmethod
    def _audit_source(index: int) -> dict:
        return {
            "bookSourceUrl": f"https://audit{index}.test",
            "bookSourceName": f"审计并发样本{index}",
            "ruleSearch": {
                "bookList": f"<js>var a{index}=1;a{index}</js>",
                "name": "@js:(function(){return '甲'})()",
                "bookUrl": "$.url",
            },
            "ruleContent": {"content": "@js:(function(){return '正文'})()"},
        }

    @staticmethod
    def _runtime_spy():
        """把 `quickjs.Context` 换成记账代理，返回 (补丁, 记账, 原构造器)。

        `_quickjs.Context` 不是可子类化的 C 类型（实测 `TypeError: type
        '_quickjs.Context' is not an acceptable base type`），只能整一个转发代理。
        记账三笔：`created`＝建过多少个 runtime，`active`＝此刻这份 runtime 在
        哪几条线程里（重叠即红，那是真会带走进程的一种），`users`＝这份 runtime
        前后被哪些线程碰过（先后换线程实测安全，但意味着有人把 Context 漏出了
        它自己的线程——生产里没有这种形状，见下）。
        """
        import quickjs

        lock = threading.Lock()
        state = {"created": 0, "calls": 0, "active": {}, "users": {}}

        def enter(key):
            tid = threading.get_ident()
            with lock:
                state["calls"] += 1
                users = state["active"].setdefault(key, {})
                for other in users:
                    if other != tid:
                        # 先拦再进：真进去就是上面那三种崩法之一，pytest 连进程
                        # 一起丢，红都报不出来。同线程重入放行。
                        raise AssertionError(
                            "同一个 quickjs runtime 被两条线程同时进入："
                            f"{other} 与 {tid}")
                users[tid] = users.get(tid, 0) + 1
                state["users"].setdefault(key, set()).add(tid)

        def leave(key):
            tid = threading.get_ident()
            with lock:
                users = state["active"][key]
                users[tid] -= 1
                if users[tid] == 0:
                    del users[tid]
                if not users:
                    state["active"].pop(key, None)

        class Spied:
            __slots__ = ("_real", "_key")

            def __init__(self, real, key):
                self._real = real
                self._key = key

            def __getattr__(self, name):
                attr = getattr(self._real, name)
                if not callable(attr):
                    return attr

                def guarded(*args, **kwargs):
                    enter(self._key)
                    try:
                        return attr(*args, **kwargs)
                    finally:
                        leave(self._key)
                return guarded

        original = quickjs.Context

        def factory(*args, **kwargs):
            real = original(*args, **kwargs)
            with lock:
                state["created"] += 1
                # 键必须是这份 runtime 自己的序号：`id(real)` 会在对象回收后被复用，
                # 而审计一次就造上百个短命 Context，复用会把两条无关线程拼成"同一个人"。
                state["users"].setdefault(state["created"], set()).add(
                    threading.get_ident())
                return Spied(real, state["created"])

        return factory, state, original

    def test_audit_and_engine_never_enter_one_runtime_together(self):
        """检测面形状：4 条审计线程 + 2 条引擎求值线程同时跑，不许有谁撞 runtime。

        审计走 `asyncio.to_thread`（生产里就是检测流水线每源一条），QuickJS 求值
        走 dispatcher 唯一线程；两侧共用一份 `quickjs.Context` 才是要防的事。
        """
        from app.services.sources import compat_audit_service

        factory, state, original = self._runtime_spy()
        import quickjs
        runtime = LegadoJsRuntime()
        source = BookSource(bookSourceName="共享 jsLib", bookSourceUrl="https://jslib.test",
                            jsLib="var __lib = 1;")
        errors = []
        barrier = threading.Barrier(6)

        def audit_worker(index):
            try:
                barrier.wait()
                for _ in range(15):
                    compat_audit_service._audit_one(self._audit_source(index))
            except BaseException as exc:  # noqa: BLE001 —— 线程里的红要带回主线程
                errors.append(exc)

        def engine_worker(offset):
            try:
                barrier.wait()
                for i in range(12):
                    runtime.evaluate(f"@js:__lib + {offset} + {i}",
                                     JsContext(source=source))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = ([threading.Thread(target=audit_worker, args=(n,)) for n in range(4)]
                   + [threading.Thread(target=engine_worker, args=(n,)) for n in range(2)])
        quickjs.Context = factory
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            quickjs.Context = original

        self.assertEqual(errors, [], f"并发面撞车：{errors[:3]}")
        # 反空转①：审计每源 3 段 JS × 4 线程 × 15 轮，替身必须真的全都见过。
        self.assertGreaterEqual(state["created"], 4 * 15 * 3,
                                "替身没记到审计新建的 Context，闸门是空转")
        self.assertGreater(state["calls"], 0, "一次 Context 方法都没被调到")
        # 设计线：每份 runtime 只该被建它的那条线程用到底（审计没外流、没复用）。
        handovers = [sorted(str(t) for t in tids)
                     for tids in state["users"].values() if len(tids) > 1]
        self.assertEqual(handovers, [],
                         "同一个 Context 被两条线程用过，共享面漏在锁/dispatcher 之外")
        # 反空转②：引擎的 jsLib Context 是按 key 缓存复用的，"同一 runtime 反复进"
        # 这个危险形状必须真的被跑到；runtime 数 == 调用数就说明只测了审计一半。
        self.assertLess(len(state["users"]), state["calls"],
                        "引擎缓存的共享 Context 没被复用到，闸门只测了一半")

    def test_the_gate_fires_when_two_threads_really_share_a_runtime(self):
        """闸门自己也要被验一次：拿真 quickjs 造一次重叠，必须红而不是静默通过。

        不改生产代码制造红——那会顺带把进程崩掉；这里只驱动记账层，让一条线程
        真的在原生 eval 里停住（纯 JS 长循环，QuickJS 求值期间不攥 GIL，实测旁路
        心跳最大间隔 2.0 ms，见 `.probe/qjs_gil_held.py`），另一条线程再进同一个
        runtime。
        """
        import quickjs

        factory, state, original = self._runtime_spy()
        quickjs.Context = factory
        try:
            shared = quickjs.Context()
            caught = []
            busy = ("(function(){var s=0;for(var i=0;i<12000000;i++){s+=(i*3^7)}"
                    "return s})()")

            def hog():
                shared.eval(busy)

            hog_thread = threading.Thread(target=hog)
            hog_thread.start()
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline and state["calls"] == 0:
                time.sleep(0.002)
            self.assertEqual(state["calls"], 1, "长循环没进到记账层里，测试是空的")
            try:
                shared.eval("1+1")
            except AssertionError as exc:
                caught.append(str(exc))
            hog_thread.join()
        finally:
            quickjs.Context = original

        self.assertEqual(len(caught), 1, f"两条线程同进一个 runtime 却没被拦住：{caught}")
        self.assertIn("同时进入", caught[0], "拦下它的不是这条闸门（措辞不对）")


class SourceBudgetCoversSearchStageTests(unittest.IsolatedAsyncioTestCase):
    """单源的 45 s 天花板必须**含搜索段**（任务 #76①）。

    改前 `deadline = perf_counter() + SOURCE_CHECK_BUDGET` 写在
    `_engine_search_check` **之后**，所以搜索段拿的是自己那套 `timeout + 3`，
    天花板从来管不到它。本机实测（`.probe/check_budget_and_js_gate.py`，127.0.0.1
    假源，旋钮按同比例缩小：timeout=3、预算 2 s、搜索挂 1.5 s、正文挂 8 s）：
    起点在搜索之后时单源墙钟 **3743 ms**，超出预算的 1743 ms 恰好就是搜索段自己
    的 1588 ms；起点前移后 **2754 ms**，剩下的 754 ms 是 `_remaining` 的 1 s 下限，
    不是漏。同旋钮下的健康源 1803 ms、结论仍 fully_usable（前移不误伤）。
    """

    async def _run(self, budget: float, search_hang: float) -> tuple:
        """用真实 `_engine_search_check` 跑一条单源，返回 (墙钟, 结果, 收到的 timeout)。"""
        received = {}
        real_search = check_service._engine_search_check

        async def spy_search(source, engine, timeout):
            received["timeout"] = timeout
            return await real_search(source, engine, timeout)

        class HangingEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)
                self.aclose = _noop_aclose

            async def search(self, source, keyword):
                await asyncio.sleep(search_hang)
                return []

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1, "error": None}

        with patch.object(check_service, "_engine_search_check", spy_search), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "LegadoReadingEngine", HangingEngine), \
                patch.object(check_service, "SOURCE_CHECK_BUDGET", budget):
            started = time.perf_counter()
            results = await check_service.check_sources(
                [_source(enabledExplore=False, exploreUrl="")],
                concurrency=1, timeout=1)
            elapsed = time.perf_counter() - started
        return elapsed, results[0], received.get("timeout")

    async def test_search_stage_gets_the_remaining_budget_not_its_own_cap(self):
        """接线证据：搜索段的 timeout 是 `_remaining(deadline, …)` 算出来的。"""
        elapsed, result, stage_timeout = await self._run(budget=0.2, search_hang=0.01)
        # 预算 0.2s 时 `_remaining` 落到 1.0s 下限；关键是它**不再**等于 timeout+3=4。
        self.assertIsNotNone(stage_timeout, "搜索段没收到预算化的 timeout")
        self.assertLess(stage_timeout, 1.5,
                        f"搜索段还在拿自己的 timeout+3（{stage_timeout}），预算脱钩")
        self.assertEqual(result["search_status"], "no_results",
                         "搜索段被预算切掉了：紧预算不该改变能跑完的搜索的结论")

    async def test_hanging_search_cannot_outlive_the_source_budget(self):
        """反空转 + 真墙钟：挂死的搜索被预算切掉，而不是把 4s 全等满。"""
        elapsed, result, _ = await self._run(budget=1.0, search_hang=30)
        self.assertEqual(result["search_status"], "timeout",
                         "搜索黑洞没被判成超时，这格没压到预算")
        self.assertLess(elapsed, 2.5,
                        f"单源墙钟 {elapsed:.2f}s 越过了 1s 预算——预算仍不含搜索段")


class BareTemplateFastPathTests(unittest.IsolatedAsyncioTestCase):
    """`{{key}}`/`{{page}}` 不该再发 QuickJS 求值（任务 #76②）。

    取证链（全部 127.0.0.1 离线，`.probe/check_budget_and_js_gate.py` +
    `.probe/template_bare_var_fastpath.py`，A/B 交替取中位）：

    - 一条规则里**零** `@js:`、只用 `{{key}}/{{page}}` 拼 URL 的源，检测里照旧发
      7 次求值，其中 2 次就是这两个裸名；
    - 把这 2 次消掉：单源 253 ms → 242 ms（dispatcher 93.5 → 65.4 ms），整轮 16 条
      这样的源 concurrency=8 时 1699 ms → 1285 ms（求值 112 → 80）——大头在并发栏，
      因为 dispatcher 是单线程串行的；
    - **换一种修法被实测否决**：把它们改判进 js_sem(2) 窄道，同一轮 1285 ms →
      1518 ms（串行资源没少，只多了排队）。所以 `_source_uses_js` 维持原判，
      修的是"别再发求值"。

    安全前提：这两个是 Legado 唯一直接从请求上下文绑进 bootstrap 的裸标识符
    （legado_js.py:966-967 `var key = json.dumps(context.key)`、
    `var page = int(context.page)`）。JSON 往返对 str 恒等；`key` 不是 str 时
    JS 会绑成 `null`（渲染成空串），Python 侧是 `"None"`，**不同值**，所以必须回落。
    """

    async def asyncSetUp(self):
        self.engine = ReaderEngine()
        self.source = SimpleNamespace(bookSourceUrl="https://bare.example")

    async def asyncTearDown(self):
        await self.engine.aclose()

    def _js_counter(self):
        """数 dispatcher 上的求值；返回 (记账, 撤销)。"""
        state = {"n": 0, "scripts": []}
        rt = LegadoJsRuntime
        orig = rt._evaluate_on_quickjs_thread

        def inner(self, script, context, *a, **kw):
            state["n"] += 1
            state["scripts"].append(str(script)[:40])
            return orig(self, script, context, *a, **kw)

        rt._evaluate_on_quickjs_thread = inner
        return state, (lambda: setattr(rt, "_evaluate_on_quickjs_thread", orig))

    async def test_bare_key_and_page_render_without_touching_quickjs(self):
        """钉"没发生"：这两个裸名一次求值都不该发出去。"""
        state, undo = self._js_counter()
        try:
            rendered = await self.engine._render_templates(
                "/s?q={{key}}&p={{page}}", self.source, "我的", 3)
        finally:
            undo()
        self.assertEqual(rendered, "/s?q=我的&p=3", "替出来的文本变了")
        self.assertEqual(state["n"], 0,
                         f"裸变量仍在发求值：{state['scripts']}")

    async def test_non_bare_shapes_still_go_through_quickjs(self):
        """反空转：快路径不能把整条模板通道关掉——只关那两个裸名。"""
        state, undo = self._js_counter()
        try:
            rendered = await self.engine._render_templates(
                "/s?a={{page+1}}&b={{'' + key}}", self.source, "k", 1)
        finally:
            undo()
        self.assertGreaterEqual(state["n"], 2,
                                f"非裸形状被顺手短路了（求值 {state['n']} 次）")
        self.assertEqual(rendered, "/s?a=2&b=k")

    async def test_fast_path_matches_the_js_answer(self):
        """逐字对拍：把快路径关掉，两栏必须一模一样。

        这条才是真不变量——它钉的是"bootstrap 的绑定值 == Python 手里的值"，
        哪天 legado_js 改了 `key`/`page` 的绑法（比如加 trim、换类型），对拍就红。
        """
        cases = [("我的", 3), ("", 1), ("a b&c=d", 0), ("书名", -2),
                 ('x"y\\z', 12), ("\u4e2d\u200b文", 7)]
        # 存取都要走 __dict__：`ReaderEngine._bare_template_value` 拿到的是**解绑后的
        # 函数**，把它直接赋回去就变成一个普通方法——下一次 `self._bare_template_value`
        # 会多带一个 self，生产代码当场 TypeError（第一格绿、后面全红正是这个形状）。
        descriptor = ReaderEngine.__dict__["_bare_template_value"]

        def forced_js(expression, k, p):   # 关掉快路径 = 走原求值
            return None
        for key, page in cases:
            with self.subTest(key=key, page=page):
                fast = await self.engine._render_templates(
                    "/s?q={{key}}&p={{page}}", self.source, key, page)
                ReaderEngine._bare_template_value = staticmethod(forced_js)
                try:
                    slow = await self.engine._render_templates(
                        "/s?q={{key}}&p={{page}}", self.source, key, page)
                finally:
                    ReaderEngine._bare_template_value = descriptor
                self.assertEqual(fast, slow)

    async def test_non_string_key_falls_back_instead_of_rendering_none(self):
        """`key=None` 在 JS 里绑成 `null` → 渲染空串；Python 的 `str(None)` 是 "None"。

        这条钉的是回落必须发生：少一次求值不值当用改语义来换。
        """
        rendered = await self.engine._render_templates(
            "/s?q={{key}}", self.source, None, 1)
        self.assertEqual(rendered, "/s?q=",
                         "非 str 的 key 没回落到求值路径，替出了 Python 的字面量")

    def test_js_lane_gate_does_not_chase_bare_templates(self):
        """闸门维持原判：模板源不进 js_sem(2)（改判实测 1.9× 慢）。"""
        template_only = SimpleNamespace(
            header="", searchUrl="https://t.example/s?q={{key}}&p={{page}}",
            exploreUrl="", ruleSearch={"bookList": "ul li"}, ruleExplore={},
            ruleBookInfo={}, ruleToc={}, ruleContent={})
        self.assertFalse(check_service._source_uses_js(template_only),
                         "把模板源改判进窄道是实测否决的修法（1285→1518 ms）")
        real_js = SimpleNamespace(
            header="", searchUrl="https://t.example/s?q=@js:key",
            exploreUrl="", ruleSearch={}, ruleExplore={}, ruleBookInfo={},
            ruleToc={}, ruleContent={})
        self.assertTrue(check_service._source_uses_js(real_js),
                        "闸门本身失效了：真 @js: 源没进窄道")

    def test_js_lane_gate_walks_the_model_section_table(self):
        """闸门不另写段清单：每个 RULE_FIELDS 段里的 JS 标记都得看得见。

        `check_service` 原先手写五段、少了 `ruleReview`——同一个 schema 知识在仓库里的
        第三份拷贝（另两份：模型 `RULE_FIELDS`、审计）。语料实测这条目前无后果
        （`resource/` 的 917 条快照里 0 条带 `ruleReview`），所以这里钉的是"别再退回
        手写清单"：漏一段 → 那一格的子用例红。
        """
        markers = ("@js:x", "<js>x</js>", "{{java.log(1)}}", "{{source.x}}", "webView")
        for section in RULE_FIELDS:
            for marker in markers:
                with self.subTest(section=section, marker=marker[:12]):
                    fields = {name: {} for name in RULE_FIELDS}
                    fields[section] = {"content": marker}
                    source = SimpleNamespace(header="", searchUrl="", exploreUrl="",
                                             **fields)
                    self.assertTrue(check_service._source_uses_js(source),
                                    f"{section} 里的 {marker[:12]} 没被闸门看到")

    def test_js_lane_gate_still_silent_on_marker_free_sections(self):
        """反空转：上面的循环不能靠"段里有东西就算 JS 源"过关。"""
        fields = {name: {"content": "text@text"} for name in RULE_FIELDS}
        source = SimpleNamespace(header="User-Agent: x", searchUrl="/s?key={{key}}",
                                 exploreUrl="分类::/l", **fields)
        self.assertFalse(check_service._source_uses_js(source),
                         "全段无标记却被判成 JS 源：闸门的判据被放宽成了存在性")


if __name__ == "__main__":
    unittest.main()
