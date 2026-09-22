"""Offline tests for the built-in browser.

Most cases drive a fake Playwright page so the BackstageWebView contract can be
checked without a network.  The last class renders a ``data:`` URL through the
real Chromium and is skipped when the kernel is not installed.
"""
import asyncio
import re
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import headless_browser
from app.services.headless_browser import (
    DEFAULT_SCRIPT, HeadlessBrowser, _with_base, cookie_header,
    default_user_agent, headless_fetch)


class _FakeRequest:
    def __init__(self, url, *, navigation=False, resource_type="document"):
        self.url = url
        self._navigation = navigation
        self.resource_type = resource_type

    def is_navigation_request(self):
        return self._navigation


class _FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _FakePage:
    """Records what the fetch asked the page to do."""

    def __init__(self, html="<html><body>done</body></html>",
                 url="https://site.example/page", script_values=(),
                 web_js_value=None, cookies=None, on_goto=None):
        self.html = html
        self.url = url
        self.script_values = list(script_values)
        self.web_js_value = web_js_value
        self.cookie_jar = list(cookies or [])
        self.on_goto = on_goto
        self.evaluated = []
        self.headers = None
        self.default_timeout = None
        self.route_pattern = None
        self.route_handler = None
        self.listener = None
        self.closed = False
        self.context = SimpleNamespace(cookies=self.cookies)

    async def cookies(self, url=None):
        self.cookie_url = url
        return self.cookie_jar

    async def new_page(self):
        return self

    async def set_extra_http_headers(self, headers):
        self.headers = dict(headers)

    async def route(self, pattern, handler):
        self.route_pattern = pattern
        self.route_handler = handler

    def on(self, event, handler):
        self.listener = handler

    def set_default_timeout(self, value):
        self.default_timeout = value

    async def set_content(self, html, wait_until=None):
        self.content_html = html
        self.content_wait = wait_until

    async def goto(self, url, wait_until=None):
        self.goto_url = url
        # A real page reports the navigated address, and loadDataWithBaseURL
        # keeps about:blank, which is why the fetch reports the base URL itself.
        self.url = url
        if self.on_goto is not None:
            self.on_goto(self)

    async def evaluate(self, script):
        self.evaluated.append(script)
        if "__legadoJsResult" in script:
            return self.web_js_value
        if script == DEFAULT_SCRIPT or "outerHTML" in script:
            return self.html
        if self.script_values:
            return self.script_values.pop(0)
        return self.html

    async def close(self):
        self.closed = True

    def emit(self, request):
        if self.listener is not None:
            self.listener(request)


class _Fakecdp:
    def __init__(self):
        self.sent = []

    async def send(self, method, params):
        self.sent.append((method, params))


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.cdp = _Fakecdp()

    async def new_page(self):
        return self.page

    async def new_cdp_session(self, page):
        return self.cdp


class _BrowserTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs ``fetch`` against a fake page, so no Chromium is launched."""

    page_kwargs: dict = {}

    def setUp(self):
        self.profile = TemporaryDirectory()
        self.browser = HeadlessBrowser(profile_dir=self.profile.name, timeout=5)
        self.addCleanup(self.profile.cleanup)
        self.addCleanup(self.browser.close)

    async def run_fetch(self, url="https://site.example/a", options=None, **page_kwargs):
        merged = dict(self.page_kwargs)
        merged.update(page_kwargs)
        page = _FakePage(**merged)
        context = _FakeContext(page)
        with patch.object(self.browser, "_ensure_browser",
                          AsyncMock(return_value=context)):
            result = await self.browser.fetch(url, options or {})
        return page, context, result

    def test_the_browser_is_created_apart_from_the_caller_loop(self):
        self.assertIsNone(self.browser._thread)

    async def test_an_unavailable_kernel_is_reported_as_browser_unavailable(self):
        with patch.object(self.browser, "_ensure_browser",
                          AsyncMock(side_effect=headless_browser.BrowserUnavailable("no"))):
            with self.assertRaises(headless_browser.BrowserUnavailable):
                await self.browser.fetch("https://site.example/a", {})

    async def test_default_fetch_returns_the_final_dom_and_cookies(self):
        page, _, result = await self.run_fetch(
            cookies=[{"name": "sid", "value": "1", "domain": "site.example"}])
        self.assertEqual(page.goto_url, "https://site.example/a")
        self.assertEqual(result["body"], "<html><body>done</body></html>")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["url"], "https://site.example/a")
        self.assertEqual(result["cookie"], "sid=1")
        self.assertTrue(page.closed)
        self.assertEqual(page.route_pattern, "**/*")

    async def test_injected_html_is_reported_under_the_base_url(self):
        page, _, result = await self.run_fetch(
            options={"html": "<html><head><title>t</title></head>"
                             "<body>seed</body></html>",
                     "initialUrl": "https://site.example/base"})
        self.assertEqual(page.content_html,
                         '<html><head><base href="https://site.example/base">'
                         '<title>t</title></head><body>seed</body></html>')
        self.assertEqual(result["url"], "https://site.example/base")

    async def test_the_source_header_and_user_agent_reach_the_page(self):
        page, context, _ = await self.run_fetch(
            options={"headerMap": {"User-Agent": "UA-1", "Referer": "https://r.example"}})
        self.assertEqual(page.headers, {"User-Agent": "UA-1",
                                        "Referer": "https://r.example"})
        self.assertEqual(context.cdp.sent,
                         [("Network.setUserAgentOverride", {"userAgent": "UA-1"})])

    async def test_the_wait_script_retries_until_the_page_answers(self):
        with patch.object(headless_browser, "POLL_INTERVAL", 0.01):
            _, _, result = await self.run_fetch(
                options={"javaScript": "window.__ready"},
                script_values=["null", "", "<p>ready</p>"])
        self.assertEqual(result["jsResult"], "<p>ready</p>")

    async def test_the_wait_script_gives_up_with_the_legado_message(self):
        with patch.object(headless_browser, "POLL_INTERVAL", 0.01), \
                patch.object(headless_browser, "POLL_RETRIES", 3):
            with self.assertRaises(TimeoutError) as caught:
                await self.run_fetch(options={"javaScript": "window.__never"},
                                     script_values=["null"] * 5)
        self.assertEqual(str(caught.exception), "js执行超时")

    async def test_web_js_mode_reads_the_value_the_script_stored(self):
        with patch.object(headless_browser, "POLL_INTERVAL", 0.01):
            _, _, result = await self.run_fetch(
                options={"webJs": "window.__legadoJsResult = '<p>x</p>'",
                         "mode": "webJs"},
                script_values=["ignored"], web_js_value="<p>x</p>")
        self.assertEqual(result["jsResult"], "<p>x</p>")

    async def test_a_source_regex_answers_with_the_matched_resource_url(self):
        matched = "https://cdn.example/book/1.json"

        def after_goto(page):
            page.emit(_FakeRequest(matched, resource_type="xhr"))

        _, _, result = await self.run_fetch(
            options={"sourceRegex": r"https://cdn\.example/.*\.json"},
            on_goto=after_goto)
        self.assertEqual(result["source"], matched)
        self.assertEqual(result["body"], matched)

    async def test_an_override_regex_answers_with_the_navigation_target(self):
        target = "https://site.example/chapter/2"

        def after_goto(page):
            page.emit(_FakeRequest(target, navigation=True))

        _, _, result = await self.run_fetch(
            options={"overrideUrlRegex": r"https://site\.example/chapter/.*"},
            on_goto=after_goto)
        self.assertEqual(result["overrideUrl"], target)

    async def test_images_are_aborted_like_block_network_image(self):
        route = _FakeRoute(_FakeRequest("https://site.example/a.png",
                                        resource_type="image"))
        await self.browser._route_handler(route)
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)
        route = _FakeRoute(_FakeRequest("https://site.example/a.js",
                                        resource_type="script"))
        await self.browser._route_handler(route)
        self.assertTrue(route.continued)


class _HelperTests(unittest.TestCase):
    def test_cookie_header_covers_the_host_and_its_subdomains(self):
        cookies = [{"name": "a", "value": "1", "domain": ".site.example"},
                   {"name": "b", "value": "2", "domain": "site.example"},
                   {"name": "c", "value": "3", "domain": "other.example"}]
        self.assertEqual(cookie_header(cookies, "https://m.site.example/x"),
                         "a=1; b=2")
        self.assertEqual(cookie_header(cookies, "https://other.example/x"), "c=3")

    def test_a_base_tag_is_injected_only_once(self):
        self.assertEqual(_with_base('<html><head></head><body></body></html>',
                                    "https://site.example/x"),
                         '<html><head><base href="https://site.example/x">'
                         '</head><body></body></html>')
        self.assertEqual(_with_base('<html><head><base href="/y"></head></html>',
                                    "https://site.example/x"),
                         '<html><head><base href="/y"></head></html>')
        self.assertEqual(_with_base("<p>bare</p>", "https://site.example/x"),
                         '<base href="https://site.example/x"><p>bare</p>')
        self.assertEqual(_with_base("<p>bare</p>", ""), "<p>bare</p>")

    def test_the_user_agent_does_not_advertise_headless_chrome(self):
        self.assertNotIn("Headless", default_user_agent())

    def test_a_missing_kernel_disables_the_fetcher_instead_of_failing(self):
        with patch.object(headless_browser, "chromium_installed",
                          return_value=True):
            self.assertIs(headless_browser.available_fetcher(), headless_fetch)
        with patch.object(headless_browser, "chromium_installed",
                          return_value=False):
            self.assertIsNone(headless_browser.available_fetcher())
        root = headless_browser.browsers_path()
        self.assertTrue(re.fullmatch(r"ms-playwright$", root.name))


@unittest.skipUnless(headless_browser.chromium_installed(),
                     "Chromium 内核未安装")
class _RealRenderTests(unittest.IsolatedAsyncioTestCase):
    """One real launch, no network: a data URL is rendered by Chromium."""

    async def asyncSetUp(self):
        self.profile = TemporaryDirectory()
        self.browser = HeadlessBrowser(profile_dir=self.profile.name, timeout=30)
        self.addCleanup(self._teardown)

    async def _teardown(self):
        await self.browser.aclose()
        self.profile.cleanup()

    async def test_a_script_written_dom_comes_back(self):
        page = ("data:text/html,<body><p id=x>seed</p>"
                "<script>document.getElementById('x').textContent='built'</script>")
        result = await self.browser.fetch(page, {})
        self.assertIn("built", result["body"])

    async def test_injected_html_runs_page_scripts_under_the_base_url(self):
        result = await self.browser.fetch(
            "https://site.example/book",
            {"html": "<body><p id=y>a</p><script>"
                     "document.getElementById('y').textContent="
                     "location.origin + '/b'</script>"})
        self.assertIn("https://site.example/b", result["body"])
        self.assertEqual(result["url"], "https://site.example/book")

    async def test_a_wait_script_is_polled_and_its_value_returned(self):
        result = await self.browser.fetch(
            "data:text/html,<body><p>t</p></body>",
            {"javaScript": "document.querySelector('p').textContent",
             "webViewDelayTime": 0})
        self.assertEqual(result["jsResult"], "t")


if __name__ == "__main__":
    unittest.main()
