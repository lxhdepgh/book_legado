"""Built-in headless Chromium, the desktop stand-in for Legado's backstage WebView.

Legado renders ``{"webView":true}`` URLs and ``ruleContent.webJs`` wait scripts in
an Android WebView (``help/http/BackstageWebView.kt``).  Plain HTTP cannot read
such a page, so this module drives a real browser instead: navigate, poll the
wait script exactly like ``EvalJsRunnable`` does, then hand back the final DOM,
the cookies and, when asked, the URL of a matched resource request.

The browser lives in its own thread with its own event loop, so any caller may
await it from the application loop or from a worker thread without sharing a
loop with Flet.  A single persistent profile serves the whole process: starting
Chromium costs about a second, and a verification solved once has to survive the
next request.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from app.services.app_paths import default_app_paths
from app.services.java_regex import compile_pattern


# BackstageWebView.getJs() falls back to this when the rule carries no script.
DEFAULT_SCRIPT = "document.documentElement.outerHTML"
# EvalJsRunnable retries once a second and gives up after 30 rounds.
POLL_INTERVAL = 1.0
POLL_RETRIES = 30
# Images never reach the rule engine and BackstageWebView blocks them too
# (settings.blockNetworkImage), so dropping them keeps a fetch as cheap as the
# Android one.
BLOCKED_RESOURCE_TYPES = {"image", "media"}
PROFILE_DIR_NAME = "chromium-profile"


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is missing, so no page can be rendered."""


def browsers_path() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(root) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def chromium_installed() -> bool:
    """Cheap presence check for the UI; launching is left to the first fetch."""
    root = browsers_path()
    if not root.is_dir():
        return False
    return any(child.is_dir() and child.name.startswith("chromium")
               for child in root.iterdir())


def default_user_agent() -> str:
    """A plain Chrome UA.

    Playwright advertises ``HeadlessChrome`` in the default UA, which most sites
    treat as a reason to serve a challenge page instead of the article.  The
    request engine already sends an ordinary browser UA; the built-in browser
    does the same.
    """
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def cookie_header(cookies: list[dict], url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    values = []
    for item in cookies or []:
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if not domain or host == domain or host.endswith("." + domain):
            values.append(f"{item.get('name', '')}={item.get('value', '')}")
    return "; ".join(values)


def _with_base(html: str, base_url: str) -> str:
    """Mirror ``WebView.loadDataWithBaseURL``: the markup keeps its origin."""
    if not base_url or re.search(r"(?i)<base\b", html[:4096]):
        return html
    tag = f'<base href="{base_url}">'
    if re.search(r"(?i)<head[^>]*>", html):
        return re.sub(r"(?i)<head[^>]*>", lambda match: match.group(0) + tag,
                      html, count=1)
    return tag + html


class HeadlessBrowser:
    def __init__(self, profile_dir=None, timeout: float = 20.0,
                 concurrency: int = 2, user_agent: str = ""):
        self.profile_dir = Path(profile_dir) if profile_dir else (
            default_app_paths().cache / PROFILE_DIR_NAME)
        self.timeout = float(timeout)
        self.concurrency = max(1, int(concurrency))
        self.user_agent = user_agent or default_user_agent()
        self._semaphore: asyncio.Semaphore | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._context = None
        self._playwright = None

    # ── session plumbing ────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="headless-browser", daemon=True)
            self._thread.start()
        self._ready.wait(30)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    async def _ensure_browser(self):
        if self._context is not None:
            return self._context
        if self._playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "缺少 playwright，无法渲染网页（pip install playwright）") from exc
            self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options = {
            "user_data_dir": str(self.profile_dir),
            "headless": True,
            "ignore_https_errors": True,
            "user_agent": self.user_agent,
            "viewport": {"width": 1280, "height": 800},
            "args": ["--mute-audio"],
        }
        try:
            # The full build runs the new headless mode; the headless shell is
            # a different download and is not what a normal profile installs.
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chromium", **options)
        except Exception as exc:
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **options)
            except Exception:
                await self._shutdown_locked()
                raise BrowserUnavailable(
                    "Chromium 内核不可用，请执行 python -m playwright install chromium"
                    f"（{type(exc).__name__}: {exc}）") from exc
        return self._context

    async def _shutdown_locked(self) -> None:
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def fetch(self, url, options=None) -> dict:
        """Render one URL and return a browser-response mapping."""
        self._ensure_thread()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise BrowserUnavailable("内置浏览器线程未启动")
        future = asyncio.run_coroutine_threadsafe(
            self._fetch(url, dict(options or {})), loop)
        return await asyncio.wrap_future(future)

    async def _fetch(self, url: str, options: dict) -> dict:
        context = await self._ensure_browser()
        script = str(options.get("javaScript") or options.get("webJs") or "").strip()
        mode = str(options.get("mode") or "")
        source_regex = str(options.get("sourceRegex") or "")
        override_regex = str(options.get("overrideUrlRegex") or "")
        header_map = {str(name): str(value)
                      for name, value in (options.get("headerMap") or {}).items()}
        user_agent = header_map.get("User-Agent") or header_map.get("user-agent")
        html = str(options.get("html") or options.get("initialHtml") or "")
        timeout = float(options.get("timeout") or self.timeout)
        delay_ms = int(options.get("webViewDelayTime") or 0)
        if self._semaphore is None:
            # Created on the loop that owns the browser, so a cancelled fetch
            # releases the slot instead of dropping it.
            self._semaphore = asyncio.Semaphore(self.concurrency)
        page = None
        async with self._semaphore:
            try:
                page = await context.new_page()
                if user_agent:
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Network.setUserAgentOverride",
                                   {"userAgent": user_agent})
                if header_map:
                    await page.set_extra_http_headers(header_map)
                await page.route("**/*", self._route_handler)
                match = self._watch_requests(page, source_regex, override_regex)
                page.set_default_timeout(timeout * 1000)
                base_url = str(options.get("initialUrl") or url or "")
                document_url = base_url if html else ""
                if html:
                    await page.set_content(_with_base(html, base_url),
                                           wait_until="domcontentloaded")
                elif url:
                    await page.goto(url, wait_until="domcontentloaded")
                value = None
                if match is not None:
                    # Sniffer mode: BackstageWebView answers with the matched
                    # URL, never with the DOM, and waits for it to appear.
                    matched = None
                    try:
                        matched = await asyncio.wait_for(match, timeout)
                    except asyncio.TimeoutError:
                        pass
                    if matched is not None and matched[0] == "source":
                        return await self._response(page, url, options,
                                                    body=matched[1],
                                                    source=matched[1])
                    if matched is not None and matched[0] == "override":
                        return await self._response(page, url, options,
                                                    body=matched[1],
                                                    override_url=matched[1])
                    return await self._response(page, url, options,
                                                document_url=document_url)
                if script or mode == "webJs":
                    value = await self._poll_script(
                        page, script or DEFAULT_SCRIPT, mode, timeout, delay_ms)
                return await self._response(page, url, options, js_result=value,
                                            script_given=bool(script),
                                            document_url=document_url)
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

    @staticmethod
    async def _route_handler(route) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    def _watch_requests(page, source_regex: str, override_regex: str):
        """Resolve with the first resource/navigation URL a regex full-matches."""
        if not source_regex and not override_regex:
            return None
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        source_pattern = compile_pattern(source_regex) if source_regex else None
        override_pattern = compile_pattern(override_regex) if override_regex else None

        def record(request):
            if future.done():
                return
            target = str(request.url)
            if (override_pattern is not None and request.is_navigation_request()
                    and override_pattern.fullmatch(target)):
                future.set_result(("override", target))
            elif source_pattern is not None and source_pattern.fullmatch(target):
                future.set_result(("source", target))

        page.on("request", record)
        return future

    async def _poll_script(self, page, script: str, mode: str, timeout: float,
                           delay_ms: int):
        """BackstageWebView.EvalJsRunnable: retry once a second until useful."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(timeout, POLL_INTERVAL * POLL_RETRIES)
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                value = await page.evaluate(script)
            except Exception as exc:
                raise RuntimeError(f"网页脚本执行失败：{exc}") from exc
            if delay_ms:
                await asyncio.sleep(max(0, delay_ms) / 1000)
            if mode == "webJs":
                stored = await page.evaluate(
                    "window.__legadoJsResult===undefined?null:window.__legadoJsResult")
                if stored is not None:
                    return stored
            elif value is not None and value != "" and value != "null":
                return value
            if loop.time() >= deadline:
                raise TimeoutError("js执行超时")

    async def _response(self, page, url: str, options: dict, body: str | None = None,
                        source: str = "", override_url: str = "",
                        js_result=None, script_given: bool = False,
                        document_url: str = "") -> dict:
        # loadDataWithBaseURL reports the base URL as the page address, while
        # about:blank is what the browser itself would say.
        final_url = document_url or str(page.url or url or "")
        if body is None:
            body = str(await page.evaluate(
                "document.documentElement.outerHTML") or "")
        cookies = []
        try:
            cookies = await page.context.cookies(final_url or None)
        except Exception:
            pass
        result = {
            "body": body, "headers": {"content-type": "text/html"},
            "status": 200, "url": final_url,
            "cookie": cookie_header(cookies, final_url or url),
        }
        if source:
            # BackstageWebView answers with the resource URL as both body and
            # source and keeps the requested URL as the response URL.
            result.update(body=body, source=source, url=str(url or final_url))
        elif override_url:
            result.update(body=body, overrideUrl=override_url,
                          url=str(options.get("url") or url or final_url))
        if script_given or js_result is not None:
            result["jsResult"] = js_result
        return result

    # ── lifecycle ───────────────────────────────────────────────────────

    async def aclose(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                self._shutdown_locked(), loop))
        except Exception:
            pass

    def close(self) -> None:
        """Terminate the browser from a synchronous shutdown hook."""
        loop, thread = self._loop, self._thread
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._shutdown_locked(), loop).result(10)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(3)
        self._thread = None


BROWSER = HeadlessBrowser()
# Registered here rather than in the GUI entry point: the browser is a process
# wide resource, so whichever code path started it should not be the only one
# able to stop it.
atexit.register(BROWSER.close)


async def headless_fetch(url, source=None, options=None) -> dict:
    """``webview_fetcher`` for the reading engine and the check pipeline.

    The second argument is the source (or a window title when the JS bridge
    calls ``java.startBrowserAwait``); the page itself comes from ``options``.
    """
    del source
    return await BROWSER.fetch(url, options)


def available_fetcher():
    """The built-in browser as a ``webview_fetcher``, or None when unusable.

    A fetcher that cannot launch would turn every webView stage into a launch
    error, so a missing Chromium is reported exactly like running without a
    browser at all.
    """
    return headless_fetch if chromium_installed() else None


def close_headless_browser() -> None:
    BROWSER.close()