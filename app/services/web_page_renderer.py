"""Answer a rule that needs a rendered page, asking for help only if the page demands it.

A rule carrying a ``webView`` URL option or a ``webJs`` script cannot be
answered by plain HTTP: the page builds its content with JavaScript, or it is
behind a challenge.  Legado hands those to a real WebView.  This module is the
equivalent and owns every stateful part of that conversation — the silent
built-in browser attempt, the in-app WebView (Android/iOS/macOS), the screen
cast of a real browser on the platforms that have no WebView control, the wait
loop for ``webJs`` rules, the ``sourceRegex``/``overrideUrlRegex`` sniffer
modes, and the captcha dialog.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid

import flet as ft

from app import platform_support
from app.services.block_pages import block_reason
from app.services.headless_browser import BROWSER, available_fetcher
from app.services.interactive_browser import VIEWPORT, InteractiveBrowser


def looks_like_gate(html: str) -> bool:
    """True when a rendered page is still a wall instead of the article.

    A login form is the one gate a page keeps after rendering: the challenge
    interstitials are already recognised by the shared block-page detector.
    """
    if not html.strip():
        return True
    if block_reason(html):
        return True
    return bool(re.search(r"""(?i)<input[^>]+type\s*=\s*["']?password""", html))


class WebPageRenderer:
    """Fetch a page through a browser; fall back to asking the user to clear a gate."""

    def __init__(self, page_provider, set_status=None):
        self._page_provider = page_provider
        self._set_status = set_status or (lambda _text: None)
        self._view = None
        self._stream_view = None
        self._typed_text = None
        self._desktop = None
        self._future = None
        self._marker = ""
        self._capture_pending = False
        self._rule_mode = False
        self._script = ""
        self._delay = 0
        self._source_regex = ""
        self._override_regex = ""
        self._html = ""
        self._initial_url = ""
        self._verification_future = None
        self._verification_input = None
        self._verification_dialog = None

    def cancel(self) -> None:
        """Drop an in-flight verification: the view holding it is going away."""
        if self._future and not self._future.done():
            self._future.cancel()

    async def render(self, url, source, options=None) -> dict:
        """A ``webview_fetcher``: render the page, or raise if nobody clears it."""
        options = options or {}
        rendered = await self._render_silently(url, options)
        if rendered is not None:
            return rendered
        page = self._page_provider()
        if self._future and not self._future.done():
            raise RuntimeError("已有一个网页验证任务正在进行")
        self._future = asyncio.get_running_loop().create_future()
        self._marker = f"__LEGADO_HTML_{uuid.uuid4().hex}__"
        self._rule_mode = str(options.get("mode") or "") == "webJs"
        self._script = str(options.get("webJs") or options.get("javaScript") or "")
        self._delay = int(options.get("webViewDelayTime") or 0)
        self._source_regex = str(options.get("sourceRegex") or "")
        self._override_regex = str(options.get("overrideUrlRegex") or "")
        self._html = str(options.get("html") or "")
        self._initial_url = str(url)
        if platform_support.supports_in_app_webview(page):
            import flet_webview as ftw
            self._view = ftw.WebView(
                url=url, expand=True,
                on_page_ended=self._on_page_ended,
                on_console_message=self._on_console_message,
                on_web_resource_error=self._on_resource_error)
            dialog_content = ft.Container(width=400, height=620, content=self._view)
        elif platform_support.is_desktop_page(page):
            dialog_content = await self._start_stream(url, options)
        else:
            raise RuntimeError("当前平台没有可用的网页验证组件")
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"网页验证 · {getattr(source, 'bookSourceName', '')}"),
            content=dialog_content,
            actions=[
                ft.TextButton("读取当前页面", on_click=self._capture_now),
                ft.TextButton("取消", on_click=self._cancel_dialog),
            ],
        )
        page.show_dialog(dialog)
        self._set_status("请在网页中完成验证；通过后会自动继续解析")
        try:
            if self._rule_mode:
                return await self._await_rule_result()
            return await self._future
        finally:
            if self._desktop:
                await self._desktop.close()
                self._desktop = None
            self._future = None
            self._view = None
            self._stream_view = None
            self._typed_text = None
            self._capture_pending = False
            self._rule_mode = False
            self._script = ""
            self._delay = 0
            self._source_regex = ""
            self._override_regex = ""
            self._html = ""
            self._initial_url = ""

    async def verification_code(self, image, source) -> str:
        """A ``verification_fetcher``: show the image captcha java.getVerificationCode() asked for."""
        page = self._page_provider()
        if self._verification_future and not self._verification_future.done():
            raise RuntimeError("已有验证码输入任务正在进行")
        self._verification_future = asyncio.get_running_loop().create_future()
        image_src = str(image or "")
        if (image_src and "://" not in image_src and
                not image_src.startswith(("data:", "/"))):
            image_src = "data:image/png;base64," + image_src
        self._verification_input = ft.TextField(
            label="验证码", autofocus=True, on_submit=self._submit_verification)
        controls = []
        if image_src:
            controls.append(ft.Image(src=image_src, height=160, fit=ft.BoxFit.CONTAIN))
        controls.append(self._verification_input)
        self._verification_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"输入验证码 · {getattr(source, 'bookSourceName', '')}"),
            content=ft.Container(width=360, content=ft.Column(controls, tight=True)),
            actions=[
                ft.TextButton("取消", on_click=self._cancel_verification),
                ft.Button("确定", on_click=self._submit_verification),
            ],
        )
        page.show_dialog(self._verification_dialog)
        try:
            return await self._verification_future
        finally:
            self._verification_future = None
            self._verification_input = None
            self._verification_dialog = None

    # ── desktop: a live cast instead of an in-app WebView control ────────

    async def _start_stream(self, url, options):
        """Show the page as a live cast and forward the reader's input to it."""
        if available_fetcher() is None:
            raise RuntimeError("内置 Chromium 内核不可用，无法在应用内打开验证页"
                               "（python -m playwright install chromium）")
        self._stream_view = ft.Image(
            src="", width=VIEWPORT["width"], height=VIEWPORT["height"],
            fit=ft.BoxFit.FILL)
        self._typed_text = ft.TextField(
            label="输入到网页的文字", width=250, dense=True,
            on_submit=self._submit_typed_text)
        self._desktop = InteractiveBrowser(on_frame=self._push_frame)
        await self._desktop.start(url, options)
        return ft.Container(width=VIEWPORT["width"], content=ft.Column([
            ft.GestureDetector(
                content=self._stream_view,
                on_tap_down=self._on_stream_tap,
                on_secondary_tap_down=self._on_stream_secondary_tap,
                on_scroll=self._on_stream_scroll,
                on_pan_start=self._on_stream_pan_start,
                on_pan_update=self._on_stream_pan_update,
                on_pan_end=self._on_stream_pan_end,
                on_pan_cancel=self._on_stream_pan_end),
            ft.Text("点击网页输入框后，在这里输入并回车；拖动可操作滑块",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([self._typed_text,
                    ft.Button("输入", on_click=self._submit_typed_text)],
                   alignment=ft.MainAxisAlignment.CENTER),
        ], tight=True))

    def _push_frame(self, data: bytes) -> None:
        view = self._stream_view
        if view is None or not data:
            return
        view.src = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        page = self._page_provider()
        if page is not None:
            try:
                page.update()
            except Exception:
                pass

    def _stream_point(self, event):
        """A cast coordinate from a Flet gesture position, or None."""
        view, browser = self._stream_view, self._desktop
        position = getattr(event, "local_position", None)
        if view is None or browser is None or position is None:
            return None
        width, height = browser.cast_viewport()
        return (position.x * width / float(view.width),
                position.y * height / float(view.height))

    def _run_on_stream(self, action, *args) -> None:
        page = self._page_provider()
        if self._desktop is None or page is None:
            return
        page.run_task(action, *args)

    def _on_stream_tap(self, event):
        self._tap_stream(event, "left")

    def _on_stream_secondary_tap(self, event):
        self._tap_stream(event, "right")

    def _tap_stream(self, event, button: str) -> None:
        point = self._stream_point(event)
        if point is not None:
            self._run_on_stream(self._desktop.tap, point[0], point[1], button)

    def _on_stream_scroll(self, event):
        delta = getattr(event, "scroll_delta", None)
        point = self._stream_point(event)
        if delta is not None and point is not None:
            self._run_on_stream(self._desktop.scroll, point[0], point[1],
                                delta.x, delta.y)

    def _on_stream_pan_start(self, event):
        point = self._stream_point(event)
        if point is not None:
            self._run_on_stream(self._desktop.press, point[0], point[1])

    def _on_stream_pan_update(self, event):
        point = self._stream_point(event)
        if point is not None:
            self._run_on_stream(self._desktop.move_to, point[0], point[1])

    def _on_stream_pan_end(self, _event):
        self._run_on_stream(self._desktop.release)

    def _submit_typed_text(self, _):
        field, page = self._typed_text, self._page_provider()
        text = str(getattr(field, "value", "") or "")
        if not text or self._desktop is None or page is None:
            return
        field.value = ""
        page.update()
        page.run_task(self._desktop.type_text, text)

    async def _render_silently(self, url, options):
        """Render through the built-in browser first: that needs no human.

        The visible window stays reserved for what genuinely needs one, so a
        challenge page or a login form still comes back as a gate and falls
        through to it.
        """
        fetcher = available_fetcher()
        if fetcher is None:
            return None
        try:
            rendered = await fetcher(url, None, options)
        except Exception:
            return None
        if looks_like_gate(str(rendered.get("body") or "")):
            return None
        return rendered

    async def _await_rule_result(self):
        """BackstageWebView-style auto evaluation loop for @webjs: rules."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while loop.time() < deadline:
            if self._future and self._future.done():
                break
            if self._desktop:
                await self._capture_desktop()
            elif self._view and not self._capture_pending:
                await self._capture_dom()
            await asyncio.sleep(0.6)
        if self._future and self._future.done():
            return await self._future
        raise RuntimeError("webjs 脚本执行超时，页面可能需要人工验证")

    def _on_page_ended(self, _):
        if self._view and not self._capture_pending:
            self._capture_pending = True
            self._page_provider().run_task(self._capture_dom)

    def _capture_now(self, _):
        page = self._page_provider()
        if self._desktop:
            page.run_task(self._capture_desktop)
        elif self._view:
            page.run_task(self._capture_dom)

    async def _capture_desktop(self):
        if not self._desktop or not self._future:
            return
        try:
            response = await self._desktop.capture(
                self._script, self._delay, self._html, self._source_regex,
                self._override_regex)
            html = response.get("body", "")
            if self._rule_mode:
                js_result = response.get("jsResult")
                if isinstance(js_result, str) and \
                        js_result.startswith("__LEGADO_JS_ERROR__:"):
                    if not self._future.done():
                        self._future.set_exception(RuntimeError(
                            js_result[len("__LEGADO_JS_ERROR__:")]))
                    self._page_provider().pop_dialog()
                    return
                if js_result is None:
                    return
                if not self._future.done():
                    await self._hand_over_cookies(response)
                    self._future.set_result(response)
                self._page_provider().pop_dialog()
                return
            waiting_for_source = bool(self._source_regex and
                                      not response.get("source"))
            if ("Just a moment" in html or "challenge-platform" in html or
                    waiting_for_source):
                self._set_status("网页验证尚未完成，请完成验证后再次读取页面")
                return
            if not self._future.done():
                await self._hand_over_cookies(response)
                self._future.set_result(response)
            self._page_provider().pop_dialog()
        except Exception as exc:
            self._set_status(f"读取内置浏览器页面失败：{exc}")

    @staticmethod
    async def _hand_over_cookies(response) -> None:
        """Let the silent profile reuse the clearance this window just earned.

        The two browsers cannot share a profile — Chromium locks it to the
        process holding it — so a gate solved here would otherwise be asked
        again on the next silent render.
        """
        await BROWSER.adopt_cookies(response.get("cookies") or [])

    async def _capture_dom(self):
        if not self._view or not self._future:
            return
        try:
            if self._html:
                html_value, self._html = self._html, ""
                await self._view.run_javascript(
                    "document.open();document.write(" +
                    json.dumps(html_value, ensure_ascii=False) +
                    ");document.close()")
            if self._script:
                script, self._script = self._script, ""
                await self._view.run_javascript(script)
                if self._delay:
                    await asyncio.sleep(max(0, self._delay) / 1000)
            # The extension does not return JS values, so use its console event
            # as the same kind of bridge that Legado exposes to WebView scripts.
            script = ("console.log('" + self._marker + "' + btoa(unescape("
                      "encodeURIComponent(JSON.stringify({html:document.documentElement.outerHTML,"
                      "url:location.href,resources:performance.getEntriesByType('resource')"
                      ".map(e=>e.name),jsResult:(typeof window.__legadoJsResult"
                      "===undefined?null:window.__legadoJsResult)})))))")
            await self._view.run_javascript(script)
        finally:
            self._capture_pending = False

    def _select_payload(self, payload):
        from app.services.engine.java_regex import compile_pattern
        html = str(payload.get("html") or "")
        final_url = str(payload.get("url") or self._initial_url)
        result = {"body": html, "headers": {"content-type": "text/html"},
                  "status": 200, "url": final_url}
        if payload.get("jsResult") is not None:
            result["jsResult"] = payload.get("jsResult")
        if self._source_regex:
            pattern = compile_pattern(self._source_regex)
            matched = next((str(item) for item in payload.get("resources") or []
                            if pattern.fullmatch(str(item))), "")
            if not matched:
                return None
            result.update(body=matched, source=matched,
                          url=self._initial_url or final_url)
        if self._override_regex:
            if not compile_pattern(self._override_regex).fullmatch(final_url):
                return None
            result.update(body=final_url, overrideUrl=final_url,
                          url=self._initial_url or final_url)
        return result

    async def _on_console_message(self, event):
        message = getattr(event, "message", "") or ""
        if not self._future or self._future.done() or not message.startswith(
                self._marker):
            return
        try:
            payload = json.loads(base64.b64decode(
                message[len(self._marker):]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return
        response = self._select_payload(payload)
        html = str(payload.get("html") or "")
        waiting_for_source = response is None
        if ("Just a moment" in html or "challenge-platform" in html or
                waiting_for_source):
            self._set_status("网页验证尚未完成，请完成验证后点击“读取当前页面”")
            return
        self._future.set_result(response)
        self._page_provider().pop_dialog()

    def _on_resource_error(self, event):
        self._set_status(f"网页加载失败：{getattr(event, 'data', '') or 'WebView 资源错误'}")

    def _cancel_dialog(self, _):
        if self._future and not self._future.done():
            self._future.set_exception(RuntimeError("用户取消网页验证"))
        self._page_provider().pop_dialog()

    def _submit_verification(self, _):
        if self._verification_future and not self._verification_future.done():
            value = self._verification_input.value if self._verification_input else ""
            self._verification_future.set_result(str(value or "").strip())
        if self._verification_dialog:
            self._page_provider().pop_dialog()

    def _cancel_verification(self, _):
        if self._verification_future and not self._verification_future.done():
            self._verification_future.set_exception(RuntimeError("验证码输入已取消"))
        if self._verification_dialog:
            self._page_provider().pop_dialog()
