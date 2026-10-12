"""Stream a real browser into the app window when a page insists on a human.

Legado hands a challenge page it cannot pass on its own to an Android WebView
and lets the reader solve it there.  Windows and Linux have no in-app WebView
control, so this module drove nothing: the page had to open in a browser window
of its own.  Instead, a headed Chromium is parked off the desktop, its screen
cast is piped into Flet as JPEG frames, and taps, scrolls and typed text travel
back over CDP input events — the reader works inside the app window.

The page's own profile is separate from the built-in headless one (a Chromium
profile is locked by the process holding it), so ``HeadlessBrowser`` has to be
handed the cookies a solved challenge produced once the gate opens.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.services.app_paths import default_app_paths
from app.services.headless_browser import (BrowserUnavailable, cookie_header,
                                           default_user_agent, with_base_url)


# A phone-sized viewport: the cast then arrives at the exact size of the control
# showing it, so a tap needs no rescaling and a captcha gets exact coordinates.
VIEWPORT = {"width": 400, "height": 480}
# The window has to paint, but it does not have to be in the way.  Parked off
# the desktop it still renders as long as Chromium is told not to treat it as
# hidden, which is the difference between a live cast and a frozen first frame.
WINDOW_ARGS = [
    "--window-position=-32000,-32000",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--mute-audio",
]
# Chromium answers every frame it is allowed to paint; the dialog needs only
# enough of them to look live, and each one costs a Flet update.
FRAME_INTERVAL = 1 / 12
FRAME_QUALITY = 70
PROFILE_DIR_NAME = "chromium-interactive-profile"


class InteractiveBrowser:
    """One headed Chromium page, cast into the app and driven from it."""

    def __init__(self, profile_dir=None, timeout: float = 30.0,
                 on_frame=None, user_agent: str = ""):
        self.profile_dir = Path(profile_dir) if profile_dir else (
            default_app_paths().cache / PROFILE_DIR_NAME)
        self.timeout = float(timeout)
        self.on_frame = on_frame
        self.user_agent = user_agent or default_user_agent()
        self._playwright = None
        self._context = None
        self._page = None
        self._started = False
        self._initial_url = ""
        self._frame_size = (0, 0)
        self._last_frame_at = 0.0

    # ── session plumbing ────────────────────────────────────────────────

    async def start(self, url: str, options: dict | None = None) -> None:
        """Open ``url`` and start casting it; frames begin when the page paints."""
        options = dict(options or {})
        self._initial_url = str(url or "")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "缺少 playwright，无法在内置浏览器中打开验证页"
                "（pip install -r requirements-desktop.txt）") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "ignore_https_errors": True,
            "user_agent": self.user_agent,
            "viewport": dict(VIEWPORT),
            "args": list(WINDOW_ARGS),
        }
        try:
            # Same preference order as the headless browser: the full build runs
            # the current mode, the bundled shell is a separate download.
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chromium", **launch)
        except Exception:
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch)
            except Exception as exc:
                await self.close()
                raise BrowserUnavailable(
                    "Chromium 内核不可用，请执行 python -m playwright install chromium"
                    f"（{type(exc).__name__}: {exc}）") from exc
        self._context.set_default_timeout(self.timeout * 1000)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        await self._apply_headers(options.get("headerMap") or {})
        if url:
            await self._page.goto(url, wait_until="domcontentloaded")
        await self._page.screencast.start(
            on_frame=self._push_frame, size=dict(VIEWPORT), quality=FRAME_QUALITY)
        self._started = True

    async def goto(self, url: str) -> None:
        """跳到新地址，会话不重开（前进/后退省掉一次内核启动）。"""
        if self._page is None:
            raise BrowserUnavailable("交互式浏览器未启动")
        self._initial_url = str(url or "")
        if url:
            await self._page.goto(str(url), wait_until="domcontentloaded")

    async def _apply_headers(self, header_map: dict) -> None:
        headers = {str(name): str(value) for name, value in (header_map or {}).items()}
        if not headers:
            return
        await self._page.set_extra_http_headers(headers)
        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        if user_agent:
            cdp = await self._context.new_cdp_session(self._page)
            await cdp.send("Network.setUserAgentOverride", {"userAgent": user_agent})

    def cast_viewport(self) -> tuple[int, int]:
        """The page size inside the cast, so a tap can be scaled onto it."""
        return self._frame_size if self._frame_size[0] else (
            VIEWPORT["width"], VIEWPORT["height"])

    def _push_frame(self, frame) -> None:
        if self.on_frame is None:
            return
        now = time.monotonic()
        if now - self._last_frame_at < FRAME_INTERVAL:
            return
        self._last_frame_at = now
        data = frame.get("data") if isinstance(frame, dict) else None
        width = int((frame or {}).get("viewportWidth") or 0)
        height = int((frame or {}).get("viewportHeight") or 0)
        if width and height:
            self._frame_size = (width, height)
        if data:
            self.on_frame(bytes(data))

    # ── input, forwarded from the Flet control ──────────────────────────

    async def tap(self, x: float, y: float, button: str = "left") -> None:
        if self._page is not None:
            await self._page.mouse.click(float(x), float(y), button=button)

    async def scroll(self, x: float, y: float, delta_x: float, delta_y: float) -> None:
        """A wheel gesture: the pointer has to be over the target first."""
        if self._page is None:
            return
        await self._page.mouse.move(float(x), float(y))
        await self._page.mouse.wheel(float(delta_x), float(delta_y))

    async def type_text(self, text: str) -> None:
        if self._page is not None and text:
            await self._page.keyboard.type(str(text))

    async def press(self, x: float, y: float, button: str = "left") -> None:
        """Begin a drag: a wheel gesture cannot move a slider handle."""
        if self._page is None:
            return
        await self._page.mouse.move(float(x), float(y))
        await self._page.mouse.down(button=button)

    async def move_to(self, x: float, y: float) -> None:
        if self._page is not None:
            await self._page.mouse.move(float(x), float(y))

    async def release(self) -> None:
        if self._page is not None:
            await self._page.mouse.up()

    # ── reading the page back ───────────────────────────────────────────

    async def capture(self, script: str = "", delay_ms: int = 0, html: str = "",
                      source_regex: str = "", override_regex: str = "") -> dict:
        """Read the live page in the same shape the headless browser answers in."""
        page = self._page
        if page is None:
            raise BrowserUnavailable("交互式浏览器未启动")
        if html:
            await page.set_content(with_base_url(html, self._initial_url or page.url),
                                   wait_until="domcontentloaded")
        script_value = None
        if script:
            script_value = await page.evaluate(str(script))
            if delay_ms:
                await asyncio.sleep(max(0, delay_ms) / 1000)
        # The webjs protocol stores its answer on window.__legadoJsResult after
        # any later navigation cleared the return value.
        stored = await page.evaluate(
            "window.__legadoJsResult===undefined?null:window.__legadoJsResult")
        if stored is not None:
            script_value = stored
        state = await page.evaluate(
            "({html:document.documentElement.outerHTML,url:location.href,"
            "resources:performance.getEntriesByType('resource').map(e=>e.name)})")
        state = state or {}
        final_url = str(state.get("url") or page.url or self._initial_url)
        cookies = []
        try:
            cookies = await self._context.cookies(final_url or None)
        except Exception:
            pass
        result = {
            "body": str(state.get("html") or ""),
            "headers": {"content-type": "text/html"},
            "status": 200, "url": final_url,
            "cookie": cookie_header(cookies, final_url),
            # Not part of a Legado response: the headless profile needs these to
            # reuse a challenge the reader has just passed.
            "cookies": cookies,
        }
        if script:
            result["jsResult"] = script_value
        from app.services.engine.java_regex import compile_pattern
        if source_regex:
            pattern = compile_pattern(source_regex)
            matched = next((str(item) for item in state.get("resources") or []
                            if pattern.fullmatch(str(item))), "")
            if matched:
                result.update(body=matched, source=matched,
                              url=self._initial_url or final_url)
        if override_regex and compile_pattern(override_regex).fullmatch(final_url):
            result.update(body=final_url, overrideUrl=final_url,
                          url=self._initial_url or final_url)
        return result

    async def close(self) -> None:
        page, context, playwright = self._page, self._context, self._playwright
        self._page = self._context = self._playwright = None
        if page is not None and self._started:
            self._started = False
            try:
                await page.screencast.stop()
            except Exception:
                pass
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
