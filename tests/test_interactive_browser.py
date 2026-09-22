"""Offline tests for the in-app cast browser, plus one real Chromium pass.

The unit cases drive a fake Playwright page, so the input plumbing and the
response shape are checked without launching anything.  The last class casts a
``data:`` URL through the real kernel and is skipped when it is not installed.
"""
import asyncio
import unittest
from tempfile import TemporaryDirectory

from app.services import headless_browser
from app.services.interactive_browser import VIEWPORT, InteractiveBrowser


class _FakeMouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def click(self, x, y, button="left"):
        self.calls.append(("click", x, y, button))

    async def down(self, button="left"):
        self.calls.append(("down", button))

    async def up(self):
        self.calls.append(("up",))

    async def wheel(self, delta_x, delta_y):
        self.calls.append(("wheel", delta_x, delta_y))


class _FakeKeyboard:
    def __init__(self):
        self.calls = []

    async def type(self, text):
        self.calls.append(("type", text))


class _FakeScreencast:
    def __init__(self):
        self.calls = []
        self.on_frame = None

    async def start(self, onFrame=None, size=None, quality=None):
        self.calls.append(("start", dict(size or {}), quality))
        self.on_frame = onFrame

    async def stop(self):
        self.calls.append(("stop",))


class _FakePage:
    def __init__(self, url="https://site.example/gate", html="<main>gate</main>",
                 resources=(), stored=None, script_value=None):
        self.url = url
        self.html = html
        self.resources = list(resources)
        self.stored = stored
        self.script_value = script_value
        self.evaluated = []
        self.goto_url = None
        self.content = None
        self.headers = None
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.screencast = _FakeScreencast()

    async def goto(self, url, wait_until=None):
        self.goto_url = url
        self.url = url

    async def set_extra_http_headers(self, headers):
        self.headers = dict(headers)

    async def set_content(self, html, wait_until=None):
        self.content = html
        self.content_wait = wait_until

    async def evaluate(self, script):
        self.evaluated.append(script)
        if "__legadoJsResult" in script:
            return self.stored
        if "getEntriesByType" in script:
            return {"html": self.html, "url": self.url,
                    "resources": self.resources}
        return self.script_value


class _FakeContext:
    def __init__(self, page, cookies=()):
        self.pages = [page]
        self.cookie_jar = list(cookies)
        self.default_timeout = None
        self.closed = False

    def set_default_timeout(self, value):
        self.default_timeout = value

    async def cookies(self, url=None):
        self.cookie_url = url
        return self.cookie_jar

    async def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _started(page=None, *, cookies=(), on_frame=None):
    """A browser whose session is already open, so no Chromium is launched."""
    page = page or _FakePage()
    browser = InteractiveBrowser(profile_dir="unused", on_frame=on_frame)
    browser._page = page
    browser._context = _FakeContext(page, cookies)
    browser._playwright = _FakePlaywright()
    browser._started = True
    browser._initial_url = page.url
    return browser


class _CastFrameTests(unittest.IsolatedAsyncioTestCase):
    """A Chromium screencast arrives as a dict of JPEG bytes plus its size."""

    def _frame(self, data=b"\xff\xd8jpeg", width=400, height=480):
        return {"data": data, "timestamp": 1.0,
                "viewportWidth": width, "viewportHeight": height}

    async def test_a_frame_reaches_the_dialog_as_jpeg_bytes(self):
        frames = []
        browser = _started(on_frame=frames.append)
        browser._push_frame(self._frame())
        self.assertEqual(frames, [b"\xff\xd8jpeg"])
        self.assertEqual(browser.cast_viewport(), (400, 480))

    async def test_frames_are_throttled_to_the_dialog_refresh_rate(self):
        frames = []
        browser = _started(on_frame=frames.append)
        browser._push_frame(self._frame())
        browser._push_frame(self._frame(b"\xff\xd8second"))
        self.assertEqual(frames, [b"\xff\xd8jpeg"])

    async def test_the_cast_size_falls_back_to_the_requested_viewport(self):
        browser = _started()
        self.assertEqual(browser.cast_viewport(),
                         (VIEWPORT["width"], VIEWPORT["height"]))


class _CastInputTests(unittest.IsolatedAsyncioTestCase):
    """Gestures in the dialog become CDP input events on the page."""

    async def test_a_tap_is_a_click_at_the_cast_coordinate(self):
        browser = _started()
        await browser.tap(12, 34)
        await browser.tap(56, 78, "right")
        self.assertEqual(browser._page.mouse.calls,
                         [("click", 12.0, 34.0, "left"),
                          ("click", 56.0, 78.0, "right")])

    async def test_a_drag_presses_moves_and_releases_the_pointer(self):
        browser = _started()
        await browser.press(10, 20)
        await browser.move_to(30, 40)
        await browser.release()
        self.assertEqual(browser._page.mouse.calls,
                         [("move", 10.0, 20.0), ("down", "left"),
                          ("move", 30.0, 40.0), ("up",)])

    async def test_a_wheel_gesture_moves_the_pointer_over_the_target_first(self):
        browser = _started()
        await browser.scroll(5, 6, 0, 240)
        self.assertEqual(browser._page.mouse.calls,
                         [("move", 5.0, 6.0), ("wheel", 0.0, 240.0)])

    async def test_typed_text_reaches_the_focused_element(self):
        browser = _started()
        await browser.type_text("1234")
        await browser.type_text("")
        self.assertEqual(browser._page.keyboard.calls, [("type", "1234")])


class _CastCaptureTests(unittest.IsolatedAsyncioTestCase):
    """Reading the live page back must look like any other browser response."""

    async def test_capture_returns_the_dom_the_url_and_the_cookies(self):
        page = _FakePage(html="<main>opened</main>", url="https://site.example/after",
                         stored="<p>js</p>")
        browser = _started(page, cookies=[
            {"name": "cf", "value": "1", "domain": "site.example"},
            {"name": "other", "value": "2", "domain": "elsewhere.test"}])
        result = await browser.capture("document.title")
        self.assertEqual(result["body"], "<main>opened</main>")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["headers"], {"content-type": "text/html"})
        self.assertEqual(result["url"], "https://site.example/after")
        self.assertEqual(result["cookie"], "cf=1")
        self.assertEqual(result["jsResult"], "<p>js</p>")
        # The headless profile needs the raw jar to reuse the clearance.
        self.assertEqual(result["cookies"], [
            {"name": "cf", "value": "1", "domain": "site.example"},
            {"name": "other", "value": "2", "domain": "elsewhere.test"}])

    async def test_capture_answers_with_a_sniffed_resource_url(self):
        page = _FakePage(resources=["https://cdn.example/book/1.json",
                                    "https://site.example/app.js"])
        browser = _started(page)
        result = await browser.capture(
            "", 0, "", r"https://cdn\.example/book/\d+\.json", "")
        self.assertEqual(result["source"], "https://cdn.example/book/1.json")
        self.assertEqual(result["body"], "https://cdn.example/book/1.json")
        self.assertEqual(result["url"], "https://site.example/gate")

    async def test_capture_answers_with_the_navigation_override_url(self):
        page = _FakePage(url="https://site.example/gate/final")
        browser = _started(page)
        result = await browser.capture("", 0, "", "",
                                       r"https://site\.example/gate/final")
        self.assertEqual(result["overrideUrl"], "https://site.example/gate/final")
        self.assertEqual(result["body"], "https://site.example/gate/final")

    async def test_injected_html_keeps_its_origin(self):
        page = _FakePage()
        browser = _started(page)
        await browser.capture("", 0, "<p>seed</p>")
        self.assertEqual(page.content,
                         '<base href="https://site.example/gate"><p>seed</p>')

    async def test_capture_without_a_page_reports_an_unavailable_browser(self):
        browser = InteractiveBrowser(profile_dir="unused")
        with self.assertRaises(headless_browser.BrowserUnavailable):
            await browser.capture()


class _CastLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_stops_the_cast_and_releases_the_session(self):
        browser = _started()
        page, context, playwright = browser._page, browser._context, browser._playwright
        await browser.close()
        self.assertEqual(page.screencast.calls, [("stop",)])
        self.assertTrue(context.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(browser._page)
        self.assertIsNone(browser._context)
        self.assertIsNone(browser._playwright)

    async def test_a_closed_session_ignores_further_input(self):
        browser = _started()
        await browser.close()
        await browser.tap(1, 2)
        await browser.scroll(1, 2, 0, 10)
        await browser.press(1, 2)
        await browser.move_to(3, 4)
        await browser.release()
        await browser.type_text("x")


@unittest.skipUnless(headless_browser.chromium_installed(),
                     "Chromium 内核未安装")
class _RealCastTests(unittest.IsolatedAsyncioTestCase):
    """One real headed launch, no network: the cast and the input both work."""

    async def asyncSetUp(self):
        self.profile = TemporaryDirectory(ignore_cleanup_errors=True)
        self.frames = []
        self.browser = InteractiveBrowser(
            profile_dir=self.profile.name, timeout=30,
            on_frame=self.frames.append)
        self.addCleanup(self._teardown)

    async def _teardown(self):
        await self.browser.close()
        self.profile.cleanup()

    async def _wait_for_frames(self, count=1, timeout=10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.frames) < count and loop.time() < deadline:
            await asyncio.sleep(0.1)

    async def test_the_page_is_cast_and_a_tap_lands_on_it(self):
        await self.browser.start(
            "data:text/html,<body style='margin:0'>"
            "<div id=hit style='width:400px;height:80px;background:silver' "
            "onclick=\"this.textContent='TAPPED'\">tap me</div></body>", {})
        await self._wait_for_frames()
        self.assertTrue(self.frames[0].startswith(b"\xff\xd8"),
                        "帧不是 JPEG 数据")

        before = await self.browser.capture()
        self.assertIn("tap me", before["body"])

        await self.browser.tap(20, 20)
        await asyncio.sleep(0.3)
        after = await self.browser.capture()
        self.assertIn("TAPPED", after["body"])


if __name__ == "__main__":
    unittest.main()
