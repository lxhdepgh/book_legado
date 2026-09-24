"""Legado-compatible catalogue and chapter-content loading pipeline."""
from __future__ import annotations

import asyncio
import ast
import aiohttp
import base64
from dataclasses import dataclass, field
import html
from http.cookies import SimpleCookie
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from yarl import URL

from bs4 import BeautifulSoup

from app.services.legado_js import JsContext, split_js_rule
from app.services.legado_rule_parser import (evaluate, is_json_text,
                                             parse_json_smart, parse_markup)
from app.services.legado_url import parse_request_config, parse_search_url
from app.services.java_regex import search as java_regex_search, sub as java_regex_sub
from app.services.source_rule import compile_rule_program
from app.services.rule_engine import (BookItem, ChapterItem, ReaderEngine,
                                      _apply_regex_suffix,
                                      _format_book_author,
                                      _format_book_intro,
                                      _format_book_name,
                                      _format_word_count,
                                      _book_type,
                                      _legado_value_string,
                                      extract_value,
                                      extract_values,
                                      html_to_text)


class TocEmptyError(RuntimeError):
    """Historical TocEmptyException observable equivalent."""


class ContentEmptyError(RuntimeError):
    """Historical ContentEmptyException observable equivalent."""




@dataclass(slots=True)
class ContentBlock:
    """A rendered chapter unit, equivalent to Legado's text/media split."""

    kind: str
    value: str | bytes
    headers: dict[str, str] = field(default_factory=dict)
    title: str = ""


@dataclass(slots=True)
class ChapterContent:
    blocks: list[ContentBlock] = field(default_factory=list)
    title: str = ""
    image_style: str = ""
    raw_content: str = ""

    @property
    def plain_text(self) -> str:
        return "\n".join(str(block.value) for block in self.blocks if block.kind == "text")


def _replace_rule_value(rule, name, default=None):
    return (rule.get(name, default) if isinstance(rule, dict)
            else getattr(rule, name, default))


def _apply_purify_rules(value: str, rules) -> str:
    """Apply historical ReplaceRule records in database order."""
    result = str(value)
    for rule in rules or []:
        if not _replace_rule_value(rule, "isEnabled", True):
            continue
        pattern = str(_replace_rule_value(rule, "pattern", "") or "")
        if not pattern:
            continue
        replacement = str(_replace_rule_value(rule, "replacement", "") or "")
        try:
            candidate = (java_regex_sub(pattern, replacement, result)
                         if _replace_rule_value(rule, "isRegex", True)
                         else result.replace(pattern, replacement))
        except re.error:
            continue
        result = candidate
    return result


def chapter_display_title(chapter: ChapterItem, title_rules=None,
                          use_replace: bool = True) -> str:
    """Python port of BookChapter.getDisplayTitle's rule-visible portion."""
    title = re.sub(r"[\r\n]+", "", str(chapter.name or ""))
    if use_replace:
        for rule in title_rules or []:
            before = title
            title = _apply_purify_rules(title, [rule])
            # Legado refuses a title rule that erases the entire title.
            if not title.strip():
                title = before
    return title


def process_book_content(value: str, book: BookItem | None,
                         chapter: ChapterItem) -> tuple[str, str]:
    """Port ContentProcessor stages used by the desktop reader.

    The desktop page already renders the chapter title above the content, so
    this corresponds to ContentProcessor.getContent(includeTitle=false).
    """
    config = dict(getattr(book, "read_config", {}) or {}) if book else {}
    use_replace = bool(config.get("useReplaceRule", True))
    title_rules = config.get("titleReplaceRules") or []
    content_rules = config.get("contentReplaceRules") or []
    display_title = chapter_display_title(chapter, title_rules, use_replace)
    result = str(value)

    # ContentProcessor first removes a leading copy of the raw chapter title;
    # if purification changed that title, it tries the displayed form second.
    book_name = re.escape(str(getattr(book, "name", "") or ""))
    for candidate in dict.fromkeys((str(chapter.name or ""), display_title)):
        if not candidate:
            continue
        title_pattern = re.escape(candidate)
        title_pattern = re.sub(r"(?:\\ |\s)+", r"\\s*", title_pattern)
        prefix = rf"^(?:\s|\p{{P}}|{book_name})*{title_pattern}\s*"
        match = java_regex_search(prefix, result)
        if match:
            result = result[match.end():]
            break

    if use_replace and content_rules:
        result = "\n".join(line.strip() for line in result.splitlines())
        result = _apply_purify_rules(result, content_rules)

    # Kotlin trims <= U+0020 and the ideographic space and discards blanks.
    lines = [line.strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"
                        "\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13"
                        "\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d"
                        "\x1e\x1f\x20　") for line in result.splitlines()]
    lines = [line for line in lines if line]
    indent = str(config.get("paragraphIndent", "") or "")
    if indent:
        lines = [indent + line for line in lines]
    return "\n".join(lines), display_title


_MEDIA_EXTENSIONS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg"},
    "audio": {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac"},
    "video": {"mp4", "m3u8", "webm", "mkv", "mov", "flv", "ts", "m4v"},
}


def _legado_truthy(value) -> bool:
    """Match Legado's boolean rule: only explicit false-like text is false."""
    text = "" if value is None else str(value).strip()
    return bool(text) and text.lower() != "null" and not re.fullmatch(
        r"(?:false|no|not|0)", text, flags=re.I)


def _media_request(value: str, base_url: str) -> tuple[str, dict[str, str]]:
    """Read Legado's `url,{request options}` form without losing headers."""
    value = (value or "").strip()
    url, options = value, {}
    for marker in (",{", ",{\""):
        index = value.rfind(marker)
        if index > 0:
            try:
                options = parse_request_config(value[index + 1:]) or {}
                url = value[:index]
                break
            except (json.JSONDecodeError, TypeError):
                pass
    headers = options.get("headers", {}) if isinstance(options, dict) else {}
    return urljoin(base_url, url.strip()), {str(k): str(v) for k, v in headers.items()}


def _resolve_rule_url(value: str, base_url: str) -> str:
    """Resolve every scheme-less Legado URL, including bare `2.html`."""
    value = str(value or "").strip()
    if not value or not base_url:
        return value
    url_part = value.split(",{", 1)[0].strip()
    if urlsplit(url_part).scheme:
        return value
    return urljoin(base_url, value)


_CONTENT_IMAGE_PATTERN = re.compile(
    r'<img[^>]*\ssrc\s*=\s*"([^"{>]*\{(?:[^{}]|\{[^}>]+\})+\})"[^>]*>|'
    r'<img[^>]*\sdata-[^=>]*=\s*"([^">]*)"[^>]*>|'
    r'<img[^>]*\ssrc\s*=\s*"([^">]*)"[^>]*>', re.I)


_USE_HTML_PATTERN = re.compile(r"<usehtml>([\s\S]*?)</usehtml>")


def render_use_html_spans(value: str) -> str:
    """Show the spans BookContent kept verbatim instead of their markup.

    ``_format_content_page`` stashes ``<usehtml>`` spans so the sanitizer cannot
    touch them, and Legado hands each one to an HTML renderer
    (``TextChapterLayout.setTypeHtml``).  Displaying the span as it is would put
    the markup, and any stylesheet it carries, in front of the reader.
    """
    return _USE_HTML_PATTERN.sub(
        lambda match: html_to_text(match.group(1)), str(value or ""))


def _format_content_page(value: str, redirect_url: str) -> str:
    """Port BookContent's per-page HtmlFormatter.formatKeepImg pipeline."""
    text = str(value or "")
    # AppConfig.adaptSpecialStyle (default true): <usehtml> spans are stashed
    # behind placeholders so the sanitizer leaves them verbatim, then restored.
    preserved: dict[str, str] = {}

    def stash(match: re.Match) -> str:
        key = f"{{usehtml_{len(preserved)}}}"
        preserved[key] = match.group(0)
        return key

    text = _USE_HTML_PATTERN.sub(stash, text)
    text = re.sub(r"(?:&nbsp;)+", " ", text)
    text = re.sub(r"&ensp;|&emsp;", " ", text)
    text = re.sub(r"&thinsp;|&zwnj;|&zwj;", "", text)
    text = re.sub(r"</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>", "\n", text)
    text = re.sub(r"<!--[^>]*-->", "", text)
    text = re.sub(r"</?(?!img)[a-zA-Z]+(?=[ >])[^<>]*>", "", text)
    text = re.sub(r"\s*\n+\s*", "\n　　", text)
    text = re.sub(r"^[\n\s]+", "　　", text)
    text = re.sub(r"[\n\s]+$", "", text)

    def image_tag(match: re.Match) -> str:
        source = match.group(1) or match.group(2) or match.group(3) or ""
        suffix = ""
        option = re.search(r"\s*,\s*(?=\{)", source)
        if match.group(1) and option:
            suffix = "," + source[option.end():]
            source = source[:option.start()]
        return f'<img src="{urljoin(redirect_url, source) + suffix}">'

    text = html.unescape(_CONTENT_IMAGE_PATTERN.sub(image_tag, text))
    for key, original in preserved.items():
        text = text.replace(key, original)
    return text


def _media_kind(url: str, hinted: str = "") -> str:
    if hinted in _MEDIA_EXTENSIONS:
        return hinted
    extension = urlsplit(url.split(",{", 1)[0]).path.rsplit(".", 1)[-1].lower()
    for kind, extensions in _MEDIA_EXTENSIONS.items():
        if extension in extensions:
            return kind
    return ""


def parse_content_blocks(value: str, base_url: str) -> ChapterContent:
    """Parse mixed HTML and direct media URLs while retaining document order."""
    document = ChapterContent(raw_content=str(value or ""))

    def append_text(text: str):
        # BookContent delays HTML4 unescaping until after HTML structure has
        # been formatted, so escaped tags remain visible text rather than nodes.
        text = html.unescape(text or "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            document.blocks.append(ContentBlock("text", text))

    stripped = value.strip()
    if stripped.startswith("["):
        try:
            media_list = json.loads(stripped)
        except json.JSONDecodeError:
            media_list = None
        if isinstance(media_list, list):
            for item in media_list:
                raw = (item.get("url") or item.get("src") if isinstance(item, dict)
                       else item)
                if not isinstance(raw, str):
                    continue
                url, headers = _media_request(raw, base_url)
                kind = _media_kind(url) or "image"
                document.blocks.append(ContentBlock(kind, url, headers))
            return document

    image_tags = list(_CONTENT_IMAGE_PATTERN.finditer(value))
    if image_tags:
        cursor = 0
        for match in image_tags:
            append_text(value[cursor:match.start()])
            source = match.group(1) or match.group(2) or match.group(3) or ""
            url, headers = _media_request(source, base_url)
            document.blocks.append(ContentBlock("image", url, headers))
            cursor = match.end()
        append_text(value[cursor:])
    else:
        text_lines = []
        for line in value.splitlines():
            candidate = line.strip()
            url, headers = _media_request(candidate, base_url)
            kind = _media_kind(url) if re.match(r"^(?:https?:)?//|^/", candidate) else ""
            if kind:
                append_text("\n".join(text_lines))
                text_lines.clear()
                document.blocks.append(ContentBlock(kind, url, headers))
            else:
                text_lines.append(line)
        append_text("\n".join(text_lines))
    return document


def _apply_replace_regex(value: str, rule: str) -> str:
    """Apply Legado ruleContent.replaceRegex deletion/replacement syntax."""
    rule = (rule or "").strip()
    if not rule:
        return value
    if rule.startswith("##"):
        rule = rule[2:]
    pieces = rule.split("##")
    pattern = pieces[0]
    replacement = pieces[1] if len(pieces) > 1 else ""
    if not pattern:
        return value
    try:
        return java_regex_sub(pattern, replacement, value, flags=re.I)
    except re.error:
        # AnalyzeRule.replaceRegex falls back to Kotlin String.replace when
        # the source supplied an invalid regular expression.
        return value.replace(pattern, replacement)


class LegadoReadingEngine(ReaderEngine):
    """Adds detail init, paged TOC, chapter context and paged content."""

    def __init__(self, timeout: int = 15, webview_fetcher=None,
                 verification_fetcher=None, state_dir=None,
                 connect_timeout: float | None = None, background: bool = False):
        super().__init__(timeout, state_dir=state_dir,
                         connect_timeout=connect_timeout, background=background)
        self.webview_fetcher = webview_fetcher
        self.verification_fetcher = verification_fetcher
        # The engine may run on its own loop (app/services/engine_worker.py).
        # The WebView and verification callbacks drive widgets, so they must run
        # on the loop that owns those widgets, not on the engine's loop.
        self.interaction_loop = None
        self.current_chapters: list[ChapterItem] = []
        self.js.reget_book_handler = self._js_reget_book
        self.js.refresh_book_handler = self._js_refresh_book

    def _js_refresh_book(self, source, book) -> None:
        if source is None or book is None:
            return
        asyncio.run(self.book_info(source, book))

    def _js_reget_book(self, source, book) -> None:
        if source is None or book is None:
            return

        async def refresh():
            matches = await self.search(source, str(getattr(book, "name", "")), 1)
            author = str(getattr(book, "author", "") or "")
            match = next((item for item in matches
                          if str(getattr(item, "name", "")) == str(
                              getattr(book, "name", "")) and
                          str(getattr(item, "author", "") or "") == author), None)
            if match is None:
                raise RuntimeError(
                    f"未搜索到 {getattr(book, 'name', '')}({author}) 书籍")
            book.url = match.url
            variables = getattr(match, "variables", None)
            if isinstance(variables, dict):
                getattr(book, "variables", {}).update(variables)
            await self.book_info(source, book)

        asyncio.run(refresh())

    @staticmethod
    def _url_option_uses_webview(value) -> bool:
        # AnalyzeUrl.UrlOption.useWebView uses a Kotlin `when`, not generic
        # truthiness.  Numeric zero is therefore enabled, while only these
        # four exact representations are disabled.
        if value is None or value is False:
            return False
        if isinstance(value, str) and value in {"", "false"}:
            return False
        return True

    async def _expand_rule_templates(self, value: str, source, chapter,
                                     content: str, base_url: str) -> str:
        """Expand ``{{ }}`` as SourceRule.makeUpRule does: the body is a JS
        expression evaluated with the current content bound as ``result``, which
        is how ``##{{chapter.title}}`` becomes a literal replacement pattern."""
        rendered = str(value or "")
        for _ in range(20):
            match = re.search(r"\{\{([\s\S]*?)\}\}", rendered)
            if not match:
                break
            try:
                evaluated = await self.js.evaluate_async(
                    f"@js:{match.group(1).strip()}",
                    JsContext(source=source, result=content, base_url=base_url,
                              variables=self.js_variables,
                              book=getattr(self, "current_book", None),
                              chapter=chapter))
            except Exception:
                break
            rendered = (rendered[:match.start()] +
                        self._template_result_text(evaluated) +
                        rendered[match.end():])
        return rendered

    async def _replace_content_stages(self, value: str, rule: str, source,
                                      chapter, context=None) -> str:
        """Apply every replaceRegex regex/JS stage to the previous result."""
        remaining = str(rule or "").strip()
        result = str(value)
        root, kind, final_url = context or (None, "html", chapter.url)
        if "{{" in remaining:
            remaining = await self._expand_rule_templates(
                remaining, source, chapter, result, final_url)
        while remaining:
            if remaining.startswith("<js>") and "</js>" in remaining:
                end = remaining.find("</js>") + 5
                script, remaining = remaining[:end], remaining[end:].strip()
                body = script[4:-5].strip()
                if body.startswith("##"):
                    result = _apply_replace_regex(result, body)
                    continue
                transformed = await self.js.evaluate_async(
                    script,
                    JsContext(
                        source=source, result=result, base_url=final_url,
                        variables=self.js_variables,
                        get_string=(lambda nested: extract_value(
                            root, nested, kind, final_url)) if root is not None else None,
                        get_string_list=(lambda nested: extract_values(
                            root, nested, kind, final_url)) if root is not None else None,
                        book=getattr(self, "current_book", None), chapter=chapter,
                    ))
                result = "" if transformed is None else str(transformed)
                continue
            if remaining.startswith("@js:"):
                transformed = await self.js.evaluate_async(
                    remaining,
                    JsContext(source=source, result=result, base_url=final_url,
                              variables=self.js_variables,
                              book=getattr(self, "current_book", None),
                              chapter=chapter))
                result, remaining = ("" if transformed is None else str(transformed)), ""
                continue
            markers = [position for position in (
                remaining.find("<js>"), remaining.find("@js:")) if position >= 0]
            end = min(markers) if markers else len(remaining)
            regex_stage, remaining = remaining[:end].strip(), remaining[end:].strip()
            result = _apply_replace_regex(result, regex_stage)
        return result.strip()

    async def _save_current_catalog(self, source) -> None:
        if (self.state_dir is None or not self.current_chapters or
                not getattr(self, "current_book", None)):
            return
        from app.services.reading_cache import ReadingCache
        try:
            await asyncio.to_thread(
                ReadingCache(self.state_dir).save_catalog,
                source.bookSourceUrl, self.current_book.url,
                self.current_chapters)
        except OSError:
            pass

    def _interaction_target_loop(self):
        """The loop that owns the widgets behind the interaction callbacks."""
        return self.interaction_loop or asyncio.get_running_loop()

    async def _call_ui(self, func, *args):
        """Await a widget-owning coroutine on the loop that owns the widgets."""
        loop = self.interaction_loop
        if loop is None or loop is asyncio.get_running_loop():
            return await func(*args)
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(func(*args), loop))

    def _bind_interaction(self, source, loop):
        if self.webview_fetcher:
            self.js.browser_fetcher = lambda url, _title="", options=None: (
                asyncio.run_coroutine_threadsafe(
                    self.webview_fetcher(url, source, options or {}), loop).result())
        if self.verification_fetcher:
            self.js.verification_code_provider = lambda image: (
                asyncio.run_coroutine_threadsafe(
                    self.verification_fetcher(image, source), loop).result())

    def _unbind_interaction(self):
        self.js.browser_fetcher = None
        self.js.verification_code_provider = None

    async def decode_cover(self, source, cover_url: str) -> str:
        """Apply source.coverDecodeJs to downloaded cover bytes.

        Legado exposes the encrypted response as ``result`` (ByteArray) and the
        original image address as ``src``.  Returning a data URI lets every UI
        backend display the decoded image without a source-specific handler.
        """
        script = str(getattr(source, "coverDecodeJs", "") or "").strip()
        if not script or not cover_url or str(cover_url).startswith("data:"):
            return str(cover_url or "")
        await self._request({"url": str(cover_url), "method": "GET"}, source)
        raw = bytes((self.last_response or {}).get("bodyBytes") or [])
        if not script.startswith(("@js:", "<js>")):
            script = "@js:" + script
        try:
            decoded = await self.js.evaluate_async(
                script,
                JsContext(source=source, src=str(cover_url), result=list(raw),
                          base_url=str(cover_url), variables=self.js_variables),
            )
            if isinstance(decoded, (bytes, bytearray)):
                payload = bytes(decoded)
            elif isinstance(decoded, (list, tuple)):
                payload = bytes(int(value) & 0xff for value in decoded)
            else:
                # ImageUtils.decode casts the script result to ByteArray in
                # runCatching; strings and every other type become null.
                return ""
        except Exception:
            return ""
        mime = "image/png" if payload.startswith(b"\x89PNG") else (
            "image/webp" if payload.startswith(b"RIFF") else "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    async def fetch_media(self, value: str, source, base_url: str = "",
                          extra_headers=None) -> bytes:
        """Download one binary resource, honouring the headers a rule attached.

        Callers pass the raw rule value because the ``url,{request options}``
        form is Legado's, not theirs: parsing it and reading the response body
        are both the engine's business.
        """
        url, headers = _media_request(value, base_url or value)
        headers.update({str(k): str(v) for k, v in (extra_headers or {}).items()})
        await self._request({"url": url, "method": "GET", "headers": headers}, source)
        return bytes((self.last_response or {}).get("bodyBytes") or [])

    async def _resolve_rule_url(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        self._bind_interaction(source, self._interaction_target_loop())
        try:
            resolved = await super()._resolve_rule_url(value, source, key, page)
            # Remote search/explore scripts commonly return a new AnalyzeUrl
            # template containing {{page}} or another inline expression.  It
            # must pass through template expansion just like a literal rule.
            if "{{" in resolved:
                resolved = await self._render_templates(
                    resolved, source, key=key, page=page)
            return resolved
        finally:
            self._unbind_interaction()

    async def _request(self, request: dict, source=None) -> tuple[str, str, str]:
        request = await self._apply_request_option_js(request, source)
        webview_value = (request["webView"] if "webView" in request else
                         request.get("webview"))
        use_webview = self._url_option_uses_webview(webview_value)
        try:
            if use_webview and self.webview_fetcher:
                browser_options = dict(request)
                if str(request.get("method") or "GET").upper() == "POST":
                    preliminary = await super()._request(request, source)
                    browser_options["html"] = preliminary[0]
                    browser_options["initialHtml"] = preliminary[0]
                    browser_options["initialUrl"] = preliminary[2]
                    response = await self._fetch_browser_response(
                        preliminary[2], source, browser_options)
                else:
                    response = await self._fetch_browser_response(
                        request["url"], source, browser_options)
            else:
                response = await super()._request(request, source)
            login_check = str(getattr(source, "loginCheckJs", "") or "").strip()
            if not login_check or not self.last_response:
                return response
            response_url = str(self.last_response.get("url") or request.get("url") or response[2])
            aiohttp_cookies = self._cookie_jar_for_source(source).filter_cookies(
                URL(response_url))
            cookie_header = "; ".join(
                f"{name}={morsel.value}" for name, morsel in aiohttp_cookies.items())
            self.js.import_cookie_header(response_url, cookie_header)
            self._bind_interaction(source, self._interaction_target_loop())
            try:
                checked = await self.js.evaluate_async(
                    login_check,
                    JsContext(
                        source=source, result=self.last_response["body"],
                        base_url=request.get("url", response[2]),
                        variables=self.js_variables,
                        response_data=self.last_response,
                    ),
                )
            finally:
                self._unbind_interaction()
            try:
                data = json.loads(checked) if isinstance(checked, str) else checked
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and "body" in data:
                headers = data.get("headers") or {}
                ctype = str(headers.get("content-type") or response[1]).split(";", 1)[0]
                self.last_response = data
                self._import_js_cookies(str(data.get("url") or response_url), source)
                return str(data.get("body", "")), ctype, str(data.get("url") or response[2])
            self._import_js_cookies(response_url, source)
            return response
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403 and self.webview_fetcher:
                return await self._fetch_browser_response(request["url"], source, request)
            raise

    async def _fetch_browser_response(self, url: str, source=None, options=None):
        try:
            value = await self._call_ui(self.webview_fetcher, url, source,
                                        options or {})
        except TypeError:
            # Preserve adapters written against the earlier two-argument API.
            value = await self._call_ui(self.webview_fetcher, url, source)
        if isinstance(value, dict):
            body = str(value.get("body") or "")
            final_url = str(value.get("url") or url)
            headers = value.get("headers") if isinstance(value.get("headers"), dict) else {}
            content_type = str(headers.get("content-type") or "text/html").split(";", 1)[0]
            cookie = str(value.get("cookie") or "")
            cookie_tag = str(getattr(source, "bookSourceUrl", "") or url)
            if "cookie" in value:
                jar = self._cookie_jar_for_source(source)
                cookie_host = URL(cookie_tag).host
                if cookie_host:
                    jar.clear_domain(cookie_host)
                parsed = SimpleCookie()
                parsed.load(cookie)
                self.js.store_set_cookie(cookie_tag, cookie)
                if parsed:
                    jar.update_cookies(
                        {name: morsel.value for name, morsel in parsed.items()},
                        response_url=URL(cookie_tag))
            self.last_response = {
                "body": body, "bodyBytes": list(body.encode("utf-8")),
                "headers": {str(k).lower(): str(v) for k, v in headers.items()},
                "headersMulti": {
                    str(k).lower(): ([str(item) for item in v]
                                     if isinstance(v, (list, tuple)) else [str(v)])
                    for k, v in headers.items()
                },
                "status": int(value.get("status") or 200),
                "message": str(value.get("message") or "OK"),
                "protocol": str(value.get("protocol") or "http/1.1"),
                "responseKind": "str", "url": final_url,
            }
            return body, content_type, final_url
        if isinstance(value, (list, tuple)):
            body = str(value[0] if value else "")
            content_type = str(value[1] if len(value) > 1 else "text/html")
            final_url = str(value[2] if len(value) > 2 else url)
            return body, content_type, final_url
        return str(value or ""), "text/html", str(url)

    def _import_js_cookies(self, url: str, source=None) -> None:
        parsed = SimpleCookie()
        parsed.load(self.js.export_cookie_header(url))
        if parsed:
            self._cookie_jar_for_source(source).update_cookies(
                {name: morsel.value for name, morsel in parsed.items()},
                response_url=URL(url),
            )

    async def execute_pay_action(self, source, chapter: ChapterItem):
        """Execute ruleContent.payAction with the current book/chapter state."""
        script = str((getattr(source, "ruleContent", {}) or {}).get(
            "payAction", "") or "").strip()
        if not script:
            return None
        if not script.startswith(("@js:", "<js>")):
            script = "@js:" + script
        self._bind_interaction(source, self._interaction_target_loop())
        try:
            result = await self.js.evaluate_async(
                script,
                JsContext(source=source, result="", base_url=chapter.url,
                          variables=self.js_variables,
                          book=getattr(self, "current_book", None), chapter=chapter),
            )
        finally:
            self._unbind_interaction()
        return result

    async def _stored_login_values(self, source) -> dict:
        raw = await self.js.evaluate_async(
            "@js:JSON.stringify(source.getLoginInfoMap())",
            JsContext(source=source, variables=self.js_variables))
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, json.JSONDecodeError):
            parsed = None
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _login_ui_items(value) -> list[dict]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return []
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    async def login_schema(self, source) -> list[dict]:
        """Parse Legado's loginUi declaration: JSON text or @js:/<js> script."""
        definition = str(getattr(source, "loginUi", "") or "").strip()
        if not definition:
            return []
        stored = await self._stored_login_values(source)
        ui: list[dict] = []
        if definition.startswith("["):
            ui = self._login_ui_items(definition)
        if not ui:
            code = definition
            if definition.startswith("@js:"):
                code = definition[4:]
            elif definition.startswith("<js>"):
                end = definition.rfind("<")
                code = definition[4:end] if end >= 4 else definition[4:]
            # SourceLoginDialog.evalUiJs pre-evaluates getLoginJs() and binds the
            # dialog's current values as ``result``.  A URL loginUrl is data, not
            # a script, so it is left alone.
            login_js = self._login_script(source).strip()
            prefix = ("" if not login_js or re.match(r"^https?://", login_js, flags=re.I)
                      else login_js + "\n")
            script = ("@js:" + prefix + "(function(){return eval(" +
                      json.dumps(code, ensure_ascii=False) + ");})()")
            value = await self.js.evaluate_async(
                script,
                JsContext(source=source, result=stored,
                          base_url=getattr(source, "bookSourceUrl", ""),
                          variables=self.js_variables))
            ui = self._login_ui_items(value)
        result = []
        for item in ui:
            name = str(item.get("name", ""))
            if name in stored:
                item["value"] = stored[name]
            result.append(item)
        return result

    @staticmethod
    def login_target(source) -> str:
        """Resolve plain, relative and {url: ...} Legado login declarations."""
        value = str(getattr(source, "loginUrl", "") or "").strip()
        if value.startswith("{"):
            try:
                value = str((json.loads(value) or {}).get("url") or "").strip()
            except (TypeError, json.JSONDecodeError):
                return ""
        if not value or value.lower() == "null":
            return ""
        if value.startswith(("/", "http://", "https://")):
            return urljoin(getattr(source, "bookSourceUrl", ""), value)
        return ""

    @classmethod
    def login_available(cls, source) -> bool:
        """Legado shows the login menu whenever loginUrl is not blank."""
        return bool(str(getattr(source, "loginUrl", "") or "").strip())

    @staticmethod
    def _login_script(source) -> str:
        value = str(getattr(source, "loginUrl", "") or "")
        if value.startswith("@js:"):
            return value[4:]
        if value.startswith("<js>"):
            end = value.rfind("<")
            return value[4:end] if end >= 4 else value[4:]
        return value

    async def execute_login_action(self, source, action: str = "",
                                   values: dict | None = None):
        """Execute a loginUi button without persisting its current fields."""
        if re.match(r"^https?://", str(action or ""), flags=re.I):
            if self.webview_fetcher:
                return await self._call_ui(self.webview_fetcher, str(action),
                                           source)
            self.js._start_browser(str(action), "")
            return None
        login_url = self._login_script(source).strip()
        # A source without a custom login UI uses its URL as a browser login.
        target = self.login_target(source)
        if not action and target:
            if not self.webview_fetcher:
                raise RuntimeError("该书源需要网页登录，但当前平台未配置网页交互")
            return await self._call_ui(self.webview_fetcher, target, source)
        script = "@js:" + (
            f"var __login={json.dumps(login_url, ensure_ascii=False)};"
            "if(__login && !/^https?:\\/\\//i.test(__login)){eval(__login);}" 
            f"eval({json.dumps(str(action or ''), ensure_ascii=False)});"
        )
        self._bind_interaction(source, self._interaction_target_loop())
        try:
            return await self.js.evaluate_async(
                script,
                JsContext(source=source, result=dict(values or {}),
                          base_url=getattr(source, "bookSourceUrl", ""),
                          variables=self.js_variables),
            )
        finally:
            self._unbind_interaction()

    async def submit_login(self, source, values: dict | None = None):
        """Port SourceLoginDialog OK: persist fields, then invoke login()."""
        values = dict(values or {})
        if not values:
            return await self.js.evaluate_async(
                "@js:source.removeLoginInfo()",
                JsContext(source=source, variables=self.js_variables))
        payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        login_js = self._login_script(source).strip()
        script = (
            "@js:source.putLoginInfo(" + json.dumps(payload, ensure_ascii=False) + ");\n" +
            login_js + "\nif(typeof login==='function'){login.apply(this);}" +
            "else{throw('Function login not implements!!!')}"
        )
        self._bind_interaction(source, self._interaction_target_loop())
        try:
            return await self.js.evaluate_async(
                script,
                JsContext(source=source,
                          base_url=getattr(source, "bookSourceUrl", ""),
                          variables=self.js_variables))
        finally:
            self._unbind_interaction()

    async def _request_value(self, value: str, source, base_url: str = "",
                             page: int = 1, request_defaults=None) -> tuple[str, str, str]:
        value = re.sub(r"@get:\{([^{}]+)\}",
                       lambda match: str(self.js_variables.get(match.group(1), "")), value)
        # Legado uses data URIs as virtual request bodies (for example to pass
        # an encoded book/chapter id between rule stages).  The optional
        # trailing request-options object is metadata, not part of the payload.
        if value.lstrip().startswith("data:"):
            raw = value.strip()
            option_at = raw.rfind(",{\"")
            data_url = raw[:option_at] if option_at > raw.find(",") else raw
            header, separator, payload = data_url.partition(",")
            if separator:
                try:
                    decoded = (base64.b64decode(payload)
                               if ";base64" in header.lower() else
                               payload.encode("utf-8"))
                    options = (parse_request_config(raw[option_at + 1:])
                               if option_at > raw.find(",") else {}) or {}
                    body = (decoded.hex() if options.get("type") is not None
                            else decoded.decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    body = ""
                return body, "text/plain; charset=utf-8", raw
        request = parse_search_url(value, base_url or source.bookSourceUrl, "", page)
        if not request:
            request = {"url": urljoin(base_url or source.bookSourceUrl, value), "method": "GET"}
        for name, default in (request_defaults or {}).items():
            if default not in (None, ""):
                request.setdefault(name, default)
        return await self._request(request, source)

    @staticmethod
    def _root(text: str, content_type: str):
        if "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
            try:
                return parse_json_smart(text), "json"
            except json.JSONDecodeError:
                pass
        return parse_markup(text, content_type), "html"

    def _strict_value(self, node, rule: str, kind: str, source, base_url: str,
                      book=None, chapter=None, raw_result=None,
                      is_url: bool = False, collect_all: bool = False,
                      unescape_html: bool = True, rule_name: str = "") -> str:
        if not rule:
            return ""
        if "{{" in rule or "@get:" in rule or "@put:" in rule:
            value = self._field_value(node, rule, kind, source, base_url,
                                      book=book, chapter=chapter,
                                      raw_result=raw_result,
                                      unescape_html=unescape_html,
                                      rule_name=rule_name)
            if is_url:
                value = _resolve_rule_url(value, base_url)
            return value
        selector, post_js = self._field_parts(rule, kind)
        if post_js:
            value = self._field_value(node, rule, kind, source, base_url,
                                      book=book, chapter=chapter,
                                      raw_result=raw_result,
                                      unescape_html=unescape_html,
                                      rule_name=rule_name)
            if is_url:
                value = _resolve_rule_url(value, base_url)
            return value
        if kind == "json" and "{" in rule:
            return extract_value(node, rule, kind, base_url, unescape_html)
        selector_parts = selector.split("##")
        selector = selector_parts[0]
        regex_suffix = ("##" + "##".join(selector_parts[1:])
                        if len(selector_parts) > 1 else "")
        if not selector and regex_suffix:
            initial = raw_result if raw_result is not None else (
                json.dumps(node, ensure_ascii=False) if kind == "json" else str(node))
            return _apply_regex_suffix(str(initial), regex_suffix).strip()
        # Older Legado sources commonly use a bare attribute name when the
        # chapterList node is already the target element (for example
        # chapterUrl="href"). Jsoup reads that as node.attr("href"); treating
        # it as a descendant selector loses the URL.
        if (kind == "html" and hasattr(node, "get") and selector and
                re.fullmatch(r"[A-Za-z_:][\w:.-]*", selector)):
            direct = node.get(selector)
            if direct is not None:
                value = _legado_value_string(direct)
                if regex_suffix:
                    value = _apply_regex_suffix(value, regex_suffix)
                if is_url:
                    value = _resolve_rule_url(value, base_url)
                value = value.strip()
                return html.unescape(value) if unescape_html else value
        values = evaluate(node, selector, kind)
        if not values:
            return ""
        if collect_all:
            rendered = []
            for item in values:
                if hasattr(item, "get_text"):
                    # @html/@all already return strings. Element results should
                    # retain paragraph boundaries just like Legado getString.
                    value = item.get_text("\n", strip=True)
                else:
                    value = _legado_value_string(item)
                if value.strip():
                    rendered.append(value.strip())
            value = _apply_regex_suffix("\n".join(rendered), regex_suffix).strip()
            return html.unescape(value) if unescape_html else value
        first = values[0]
        if is_url and hasattr(first, "get"):
            value = (first.get("href") or first.get("src") or
                     first.get("data-src") or first.get("value") or "")
        else:
            value = (first.get_text(" ", strip=True) if hasattr(first, "get_text")
                     else _legado_value_string(first))
        if regex_suffix:
            value = _apply_regex_suffix(value, regex_suffix)
        if is_url:
            value = _resolve_rule_url(value, base_url)
        value = value.strip()
        return html.unescape(value) if unescape_html else value

    def _next_page_values(self, node, rule: str, kind: str, source,
                          base_url: str, book=None, chapter=None,
                          raw_result=None, next_chapter_url="") -> list[str]:
        """Evaluate a Legado next-page rule without collapsing URL arrays."""
        selector, post_js = self._field_parts(rule, kind)
        if post_js:
            result = self._field_value(
                node, rule, kind, source, base_url, book=book,
                chapter=chapter, raw_result=raw_result,
                title=getattr(chapter, "name", "") if chapter else "",
                next_chapter_url=next_chapter_url)
        elif selector:
            values = extract_values(node, selector, kind, base_url)
            result = values if len(values) != 1 else (values[0] if values else "")
        else:
            result = raw_result if raw_result is not None else node
        if isinstance(result, str):
            value = result.strip()
            if value.startswith("["):
                try:
                    decoded = json.loads(value)
                    result = decoded if isinstance(decoded, list) else [value]
                except json.JSONDecodeError:
                    try:
                        decoded = ast.literal_eval(value)
                        result = decoded if isinstance(decoded, list) else [value]
                    except (ValueError, SyntaxError):
                        result = [value]
            else:
                result = [value]
        elif not isinstance(result, (list, tuple)):
            result = [result] if result else []
        urls = []
        for item in result:
            value = str(item or "").strip()
            if not value or value.lower().startswith(("javascript:", "void(")):
                continue
            urls.append(urljoin(base_url, value) if value.startswith(
                ("/", "./", "../", "//", "#")) else value)
        return urls

    async def _list_nodes(self, text: str, content_type: str, rule: str, source,
                          base_url: str, book=None, chapter=None,
                          preserve_element: bool = False) -> tuple[list, str]:
        def element_kind(item) -> str:
            if isinstance(item, dict) or (
                    isinstance(item, list) and
                    (not item or all(isinstance(entry, dict) for entry in item))):
                return "json"
            if isinstance(item, str):
                try:
                    parse_json_smart(item)
                    return "json"
                except (json.JSONDecodeError, TypeError):
                    pass
            return "html"

        value = (rule or "").strip()
        if not value:
            return [], self._root(text, content_type)[1]
        original_text = text
        try:
            parse_json_smart(text)
            analyzer_kind = "json"
        except (json.JSONDecodeError, TypeError):
            analyzer_kind = "json" if (
                "json" in (content_type or "").lower() and not text.strip()
            ) else "html"
        current_result = text
        pending_root = None
        reverse = value.startswith("-")
        if value.startswith(("-", "+")):
            value = value[1:].strip()
        nodes, kind = [], "html"
        while True:
            js_rule, remaining = split_js_rule(value)
            if js_rule:
                transformed = await self.js.evaluate_async(
                    js_rule,
                    JsContext(source=source, result=current_result,
                              base_url=base_url, src=original_text,
                              variables=self.js_variables, book=book, chapter=chapter),
                )
                current_result = transformed
                text = transformed if isinstance(transformed, str) else json.dumps(
                    transformed, ensure_ascii=False)
                value = remaining
                if preserve_element and not value:
                    return transformed, element_kind(transformed)
                if value:
                    program = compile_rule_program(
                        value, is_json=analyzer_kind == "json")
                    if program.stages and program.stages[0].mode not in {"js", "webjs"}:
                        root, kind, _ = self._payload_for_compiled_stage(
                            transformed, value, analyzer_kind)
                        pending_root = (root, kind)
                    continue
                root, kind = self._root(text, content_type)
                nodes = root if isinstance(root, list) else [root]
                break

            if pending_root is not None:
                root, kind = pending_root
                pending_root = None
            else:
                root, kind = self._root(text, content_type)
            selector, suffix = self._field_parts(value, kind)
            # Book list and chapter list mirror AnalyzeRule.getElements, where
            # every '@' segment is a selector.  bookInfo.init mirrors
            # getElement: Jsoup/XPath still return element lists, but the JSON
            # stage reads one object through getObject instead of getList.
            nodes = evaluate(
                root, selector, kind,
                list_context=not (preserve_element and kind == "json"),
            ) if selector else (root if isinstance(root, list) else [root])
            if not suffix:
                if preserve_element:
                    program = compile_rule_program(
                        value, is_json=analyzer_kind == "json")
                    terminal_mode = program.stages[-1].mode if program.stages else "default"
                    return ((nodes[0] if len(nodes) == 1 else nodes), "json") \
                        if terminal_mode == "json" else (nodes, kind)
                break

            # Execute one post-selector JS stage, then continue parsing its
            # output with the rest of the rule. This preserves list objects for
            # scripts using result.toArray()/map/filter.
            if suffix.startswith("<js>") and "</js>" in suffix:
                end = suffix.find("</js>") + 5
                script, value = suffix[:end], suffix[end:].strip()
            else:
                script, value = suffix, ""
            js_result = [str(item) for item in nodes] if kind == "html" else nodes
            transformed = await self.js.evaluate_async(
                script,
                JsContext(source=source, result=js_result, base_url=base_url,
                          variables=self.js_variables, book=book, chapter=chapter,
                          src=original_text, result_html_nodes=kind == "html"),
            )
            current_result = transformed
            text = transformed if isinstance(transformed, str) else json.dumps(
                transformed, ensure_ascii=False)
            if preserve_element and not value:
                return transformed, element_kind(transformed)
            if value:
                program = compile_rule_program(
                    value, is_json=analyzer_kind == "json")
                if program.stages and program.stages[0].mode not in {"js", "webjs"}:
                    root, kind, _ = self._payload_for_compiled_stage(
                        transformed, value, analyzer_kind)
                    pending_root = (root, kind)
                continue
            if (isinstance(transformed, list) and transformed and
                    all(isinstance(item, dict) and "__markup" in item
                        for item in transformed)):
                transformed = [str(item["__markup"]) for item in transformed]
            if (isinstance(transformed, list) and transformed and
                    all(isinstance(item, str) and item.lstrip().startswith("<")
                        for item in transformed)):
                nodes = [BeautifulSoup(item, "html.parser").find()
                         for item in transformed]
                nodes = [item for item in nodes if item is not None]
                kind = "html"
                break
            if isinstance(transformed, list) and not transformed:
                nodes, kind = [], "html"
                break
            if isinstance(transformed, dict) or (
                    isinstance(transformed, list) and
                    all(isinstance(item, dict) for item in transformed)):
                nodes = (transformed if isinstance(transformed, list)
                         else [transformed])
                kind = "json"
                break
            if (isinstance(transformed, list) and transformed and
                    all(isinstance(item, str) and is_json_text(item)
                        for item in transformed)):
                # AnalyzeRule.getElements keeps a JS list as-is and every field
                # rule re-reads setContent(item); JSON elements stay strings and
                # are resolved by the JSONPath analyzer.
                nodes, kind = list(transformed), "json"
                break
            root, kind = self._root(text, content_type)
            nodes = root if isinstance(root, list) else [root]
            break
        if reverse:
            nodes.reverse()
        return nodes, kind

    async def _initialize_detail(self, source, book, text, content_type, final_url):
        root, kind = self._root(text, content_type)
        init = ((source.ruleBookInfo or {}).get("init") or "").strip()
        if not init:
            return root, kind, text
        if init.startswith("@put:"):
            self._field_value(root, init, kind, source, final_url,
                              book=book, raw_result=text)
            return root, kind, text
        root, kind = await self._list_nodes(
            text, content_type, init, source, final_url, book=book,
            preserve_element=True)
        if root is None:
            raise AssertionError("内容不可空（Content cannot be null）")
        if kind == "json":
            text = root if isinstance(root, str) else json.dumps(
                root, ensure_ascii=False)
        elif isinstance(root, (list, tuple)):
            text = "\n".join(str(item) for item in root)
        else:
            text = str(root)
        return root, kind, text

    def _apply_book_info(self, source, book, root, kind, raw_text, base_url,
                         redirect_url=None, original_body=None,
                         can_rename: bool = True):
        rules = source.ruleBookInfo or {}
        redirect_url = redirect_url or base_url
        original_body = raw_text if original_body is None else original_body

        kind_rule = rules.get("kind")
        if kind_rule:
            try:
                value = self._strict_value(
                    root, kind_rule, kind, source, redirect_url, book=book,
                    raw_result=raw_text, collect_all=True,
                    rule_name="ruleBookInfo.kind")
                values = [item for item in value.splitlines() if item]
                if values:
                    book.kind = ",".join(values)
            except Exception:
                pass

        cover_rule = rules.get("coverUrl")
        if cover_rule:
            try:
                value = self._strict_value(
                    root, cover_rule, kind, source, redirect_url, book=book,
                    raw_result=raw_text, is_url=True,
                    rule_name="ruleBookInfo.coverUrl")
                if value:
                    book.cover_url = value
            except Exception:
                pass
        optional_formatters = {
            "intro": ("intro", _format_book_intro),
            "wordCount": ("word_count", _format_word_count),
            "lastChapter": ("latest_chapter_title", str),
        }
        for rule_name, (attribute, formatter) in optional_formatters.items():
            rule = rules.get(rule_name)
            if not rule:
                continue
            try:
                value = formatter(self._strict_value(
                    root, rule, kind, source, redirect_url, book=book,
                    raw_result=raw_text, rule_name=f"ruleBookInfo.{rule_name}"))
            except Exception:
                continue
            if value:
                setattr(book, attribute, value)
                if rule_name == "lastChapter":
                    book.last_chapter = value
        rename = can_rename and bool(str(rules.get("canReName") or "").strip())
        if rules.get("name"):
            value = _format_book_name(self._strict_value(
                root, rules["name"], kind, source, redirect_url,
                book=book, raw_result=raw_text, rule_name="ruleBookInfo.name"))
            if value and (not book.name or rename):
                book.name = value
        if rules.get("author"):
            value = _format_book_author(self._strict_value(
                root, rules["author"], kind, source, redirect_url,
                book=book, raw_result=raw_text, rule_name="ruleBookInfo.author"))
            if value and (not book.author or rename):
                book.author = value

        if book.type & 128:
            rule = rules.get("downloadUrls")
            urls = self._next_page_values(
                root, rule, kind, source, redirect_url, book=book,
                raw_result=raw_text) if rule else []
            if not urls:
                raise RuntimeError("下载链接为空")
            book.download_urls = urls
        else:
            rule = rules.get("tocUrl")
            value = self._strict_value(
                root, rule, kind, source, redirect_url, book=book,
                raw_result=raw_text, is_url=True,
                rule_name="ruleBookInfo.tocUrl") if rule else ""
            book.toc_url = value or base_url
            if book.toc_url == base_url:
                book.toc_html = original_body
        # Detail rules put chapter?:book scoped values directly onto the book
        # entity; absorb engine-bag values without discarding those writes.
        getattr(book, "variables", {}).update(self.js_variables)

    async def book_info(self, source, book: BookItem) -> BookItem:
        """Load book-detail rules without fetching the table of contents."""
        self.js_variables.update(getattr(book, "variables", {}) or {})
        book.type = _book_type(source)
        if book.info_html:
            detail_text, detail_url = book.info_html, book.url
            detail_type = ("application/json" if detail_text.lstrip().startswith(
                ("{", "[")) else "text/html")
        else:
            detail_text, detail_type, detail_url = await self._request_value(
                book.url, source, source.bookSourceUrl)
        original_body = detail_text
        detail_root, detail_kind, detail_text = await self._initialize_detail(
            source, book, detail_text, detail_type, detail_url)
        self._apply_book_info(
            source, book, detail_root, detail_kind, detail_text, book.url,
            detail_url, original_body)
        self.current_book = book
        return book

    async def chapters(self, source, book: BookItem) -> list[ChapterItem]:
        self.js_variables.update(getattr(book, "variables", {}) or {})
        book.type = _book_type(source)
        detail_url = book.url
        if not book.toc_url:
            if book.info_html:
                detail_text, detail_url = book.info_html, book.url
                detail_type = ("application/json" if detail_text.lstrip().startswith(
                    ("{", "[")) else "text/html")
            else:
                detail_text, detail_type, detail_url = await self._request_value(
                    book.url, source, source.bookSourceUrl)
            original_body = detail_text
            detail_root, detail_kind, detail_text = await self._initialize_detail(
                source, book, detail_text, detail_type, detail_url)
            self._apply_book_info(
                source, book, detail_root, detail_kind, detail_text, book.url,
                detail_url, original_body)
        toc_rule = source.ruleToc or {}
        list_rule = str(toc_rule.get("chapterList") or "")
        reverse_rule = list_rule.startswith("-")
        if list_rule.startswith(("-", "+")):
            list_rule = list_rule[1:].strip()
        toc_url = book.toc_url or book.url
        pre_update = str(toc_rule.get("preUpdateJs") or "").strip()
        if pre_update:
            if not pre_update.startswith(("@js:", "<js>")):
                pre_update = "@js:" + pre_update
            self._bind_interaction(source, self._interaction_target_loop())
            try:
                await self.js.evaluate_async(
                    pre_update,
                    JsContext(source=source, result=toc_url, base_url=toc_url,
                              variables=self.js_variables, book=book))
            finally:
                self._unbind_interaction()
            toc_url = book.toc_url or toc_url
        chapters, visited_pages = [], set()
        prefetched_toc = None
        if toc_url == book.url and book.toc_html:
            toc_type = ("application/json" if book.toc_html.lstrip().startswith(
                ("{", "[")) else "text/html")
            prefetched_toc = (book.toc_html, toc_type, toc_url)
        pending_pages = [(toc_url, prefetched_toc, True)]
        while pending_pages:
            current_page, prefetched, discover_next = pending_pages.pop(0)
            current_key = _media_request(current_page, detail_url)[0]
            if not current_key or current_key in visited_pages:
                continue
            visited_pages.add(current_key)
            text, ctype, final_url = (prefetched if prefetched is not None else
                await self._request_value(current_page, source, detail_url))
            nodes, kind = await self._list_nodes(
                text, ctype, list_rule, source, final_url, book=book)
            for page_index, node in enumerate(nodes):
                name = self._strict_value(
                    node, str(toc_rule.get("chapterName") or ""),
                    kind, source, final_url, book=book,
                    rule_name="ruleToc.chapterName")
                url = self._strict_value(
                    node, str(toc_rule.get("chapterUrl") or ""),
                    kind, source, final_url, book=book, is_url=True,
                    rule_name="ruleToc.chapterUrl")
                if isinstance(url, str) and url.strip().lower().startswith(
                        ("javascript:", "void(")):
                    url = ""
                chapter = ChapterItem(
                    name=name, url=url,
                    base_url=final_url,
                    book_url=book.url,
                    tag=self._field_value(node, toc_rule.get("updateTime", ""), kind,
                                          source, final_url, book=book,
                                          rule_name="ruleToc.updateTime")
                    if toc_rule.get("updateTime") else "",
                )
                def truthy(rule_name):
                    if not toc_rule.get(rule_name):
                        return False
                    raw = self._strict_value(
                        node, toc_rule[rule_name], kind, source, final_url,
                        book=book, rule_name=f"ruleToc.{rule_name}")
                    return _legado_truthy(raw)
                chapter.is_volume = truthy("isVolume")
                chapter.is_vip = truthy("isVip")
                chapter.is_pay = truthy("isPay")
                if chapter.name:
                    chapter.url = chapter.url or (
                        chapter.name + str(page_index)
                        if chapter.is_volume else final_url)
                    chapters.append(chapter)

            page_root, page_kind = self._root(text, ctype)
            next_rule = toc_rule.get("nextTocUrl", "")
            next_urls = (self._next_page_values(
                page_root, next_rule, page_kind, source, final_url,
                book=book, raw_result=text)
                if next_rule and discover_next else [])
            accepted = []
            pending_keys = {
                _media_request(item[0], final_url)[0] for item in pending_pages}
            for next_url in next_urls:
                key = _media_request(next_url, final_url)[0]
                if key and key not in visited_pages and key not in pending_keys:
                    accepted.append(next_url)
                    pending_keys.add(key)
            if len(accepted) == 1:
                pending_pages.append((accepted[0], None, True))
            elif accepted:
                responses = await asyncio.gather(*(
                    self._request_value(item, source, detail_url)
                    for item in accepted))
                pending_pages.extend(
                    (item, response, False)
                    for item, response in zip(accepted, responses))

            # Some legacy sources incorrectly resolve ruleBookInfo.tocUrl to
            # the first chapter even though the chapter list lives on the book
            # detail page. Legado users can encounter this after a site layout
            # change or with an old saved tocUrl. Retry the detail page once,
            # only when the declared TOC produced no usable entries.
            if not chapters and not pending_pages:
                fallback_url = book.url
                fallback_key = _media_request(fallback_url, detail_url)[0]
                if fallback_key and fallback_key not in visited_pages:
                    pending_pages.append((fallback_url, None, True))

        if not chapters:
            raise TocEmptyError("chapter list is empty")
        if not reverse_rule:
            chapters.reverse()
        unique, seen = [], set()
        for chapter in chapters:
            key = chapter.url
            # Legado trusts ruleToc.chapterList. A source may intentionally
            # expose pages as readable entries (reviews, image sets, etc.);
            # filtering names such as "第001页" changes source semantics.
            if key in seen:
                continue
            seen.add(key)
            unique.append(chapter)
        reverse_toc = bool((getattr(book, "read_config", {}) or {}).get(
            "reverseToc", False))
        if not reverse_toc:
            unique.reverse()
        for index, chapter in enumerate(unique):
            chapter.index = index
        format_js = str(toc_rule.get("formatJs") or "").strip()
        if format_js:
            if not format_js.startswith(("@js:", "<js>")):
                format_js = "@js:" + format_js
            format_context = JsContext(
                source=source, host_kind="format", base_url=toc_url,
                rule_data_present=False,
            )
            for index, chapter in enumerate(unique, 1):
                format_context.index = index
                format_context.chapter = chapter
                format_context.title = chapter.name
                format_context.result = chapter.name
                try:
                    value = await self.js.evaluate_async(
                        format_js, format_context)
                except Exception:
                    # Legado logs formatJs failures and keeps the original title.
                    continue
                if value is not None:
                    chapter.name = str(value)
        now = int(time.time() * 1000)
        previous_total = book.total_chapter_num
        if unique:
            book.latest_chapter_title = unique[-1].name
            current = (unique[book.dur_chapter_index]
                       if 0 <= book.dur_chapter_index < len(unique) else unique[-1])
            book.dur_chapter_title = current.name
        if previous_total < len(unique):
            book.last_check_count = len(unique) - previous_total
            book.latest_chapter_time = now
        book.last_check_time = now
        book.total_chapter_num = len(unique)
        self.current_book = book
        self.current_chapters = unique
        return unique

    async def content_document(self, source, chapter: ChapterItem,
                               next_chapter_url: str = "",
                               need_save: bool = True) -> ChapterContent:
        # AnalyzeRule.put routes chapter-scoped values onto the chapter entity
        # itself; the engine bag is ruleData-level only, so nothing is merged
        # from chapter variables here.
        if not next_chapter_url and self.current_chapters:
            following = next((item.url for item in self.current_chapters
                              if item.index == chapter.index + 1), "")
            next_chapter_url = following or next((
                item.url for item in self.current_chapters if item.index == 0), "")
        content_rule = source.ruleContent or {}
        rule = str(content_rule.get("content") or "")
        # WebBook.getContentAwait bypasses BookContent entirely in these two
        # historical branches, including title parsing and persistence.
        if not rule:
            return ChapterContent(
                blocks=[ContentBlock("text", chapter.url)], raw_content=chapter.url)
        if chapter.is_volume and chapter.url.startswith(chapter.name):
            return ChapterContent(
                blocks=[ContentBlock("text", chapter.tag or "")],
                raw_content=chapter.tag or "")
        next_rule = content_rule.get("nextContentUrl", "")
        # A single next URL is a chain and is followed recursively. Multiple
        # URLs are independent pages: Legado requests them concurrently,
        # preserves declaration order, and does not discover another level.
        pending_urls, visited, parts = [(chapter.url, None, True)], set(), []
        document_title = ""
        replace_context = None
        first_body = None
        next_chapter_key = _media_request(next_chapter_url, chapter.url)[0] if next_chapter_url else ""
        while pending_urls:
            current_url, prefetched, discover_next = pending_urls.pop(0)
            current_key = _media_request(current_url, chapter.url)[0]
            if not current_key or current_key in visited:
                continue
            visited.add(current_key)
            request_defaults = {
                "webJs": content_rule.get("webJs", ""),
                "sourceRegex": content_rule.get("sourceRegex", ""),
            }
            text, ctype, final_url = (prefetched if prefetched is not None else
                await self._request_value(
                    current_url, source, chapter.url,
                    request_defaults=request_defaults))
            root, kind = self._root(text, ctype)
            if replace_context is None:
                replace_context = (root, kind, final_url)
                first_body = text
            if not document_title and content_rule.get("title"):
                try:
                    document_title = self._strict_value(
                        root, content_rule["title"], kind, source, final_url,
                        book=getattr(self, "current_book", None), chapter=chapter,
                        raw_result=text, rule_name="ruleContent.title")
                except Exception:
                    document_title = ""
                if document_title:
                    chapter.name = document_title
                    await self._save_current_catalog(source)
            value = self._strict_value(
                root, rule, kind, source, final_url,
                book=getattr(self, "current_book", None), chapter=chapter,
                raw_result=text, collect_all=True, unescape_html=False,
                rule_name="ruleContent.content")
            value = _format_content_page(value, final_url)
            if value:
                parts.append(value)
            next_urls = (self._next_page_values(
                root, next_rule, kind, source, final_url,
                book=getattr(self, "current_book", None), chapter=chapter,
                raw_result=text, next_chapter_url=next_chapter_url)
                if next_rule and discover_next else [])
            accepted = []
            pending_keys = {
                _media_request(item[0], final_url)[0] for item in pending_urls}
            for next_url in next_urls:
                key = _media_request(next_url, final_url)[0]
                if (key and key not in visited and key != next_chapter_key and
                        key not in pending_keys):
                    accepted.append(next_url)
                    pending_keys.add(key)
            if len(accepted) == 1:
                pending_urls.append((accepted[0], None, True))
            elif accepted:
                responses = await asyncio.gather(*(
                    self._request_value(
                        item, source, chapter.url,
                        request_defaults=request_defaults)
                    for item in accepted))
                pending_urls.extend(
                    (item, response, False)
                    for item, response in zip(accepted, responses))

        # BookContent: subContent is analyzed against the first page and its
        # text is appended unformatted (online books take the raw add branch).
        sub_rule = str(content_rule.get("subContent", "") or "").strip()
        if sub_rule and replace_context is not None:
            sub_value = self._strict_value(
                replace_context[0], sub_rule, replace_context[1], source,
                replace_context[2],
                book=getattr(self, "current_book", None), chapter=chapter,
                raw_result=first_body, rule_name="ruleContent.subContent")
            if sub_value:
                parts.append(sub_value)
        joined = "\n".join(parts)
        replace_rule = str(content_rule.get("replaceRegex", "") or "").strip()
        joined = await self._replace_content_stages(
            joined, replace_rule, source, chapter, replace_context)
        book_content = joined
        joined, display_title = process_book_content(
            joined, getattr(self, "current_book", None), chapter)
        if not chapter.is_volume and not joined.strip():
            raise ContentEmptyError("chapter content is empty")
        document = parse_content_blocks(joined, chapter.url)
        # WebBook.getContent returns BookContent's string.  The block list is
        # the later ContentProcessor/UI projection and may have purify rules.
        document.raw_content = book_content
        document.title = display_title
        document.image_style = str(content_rule.get("imageStyle") or "")
        await self._apply_media_headers(document, source)
        await self._load_protected_images(document, source)
        if self.state_dir is not None and getattr(self, "current_book", None):
            from app.services.reading_cache import ReadingCache
            cache = ReadingCache(self.state_dir)
            # Updating a parsed title is a chapter-database side effect and is
            # independent of needSave in fixed BookContent.
            await self._save_current_catalog(source)
            if need_save:
                try:
                    cached_document = ChapterContent(
                        blocks=[ContentBlock(
                            block.kind, block.value, dict(block.headers), block.title)
                            for block in document.blocks],
                        title=document.title, image_style=document.image_style,
                        raw_content=document.raw_content)
                    await self._load_protected_images(
                        cached_document, source, cache_all=True)
                    await asyncio.to_thread(
                        cache.save_content, source.bookSourceUrl,
                        self.current_book.url, chapter.url, cached_document)
                except Exception:
                    # BookHelp.saveContent catches persistence failures and
                    # still returns the successfully parsed chapter.
                    pass
        return document

    async def _apply_media_headers(self, document: ChapterContent, source):
        """Give audio/video requests the headers Legado sends for the source."""
        media = [block for block in document.blocks
                 if block.kind in {"audio", "video"}]
        if not media:
            return
        source_headers = await self._resolved_source_headers(
            source, getattr(source, "bookSourceUrl", ""))
        for block in media:
            merged = dict(source_headers)
            merged.update(block.headers or {})
            block.headers = merged

    async def _load_protected_images(self, document: ChapterContent, source,
                                     cache_all: bool = False):
        """Flet Image has no request-header API; fetch protected images as bytes."""
        source_headers = await self._resolved_source_headers(
            source, getattr(source, "bookSourceUrl", ""))
        image_decode = str((getattr(source, "ruleContent", {}) or {}).get(
            "imageDecode", "") or "").strip()
        protected = [block for block in document.blocks
                     if block.kind == "image" and
                     (cache_all or block.headers or source_headers or image_decode) and
                     isinstance(block.value, str)]
        if not protected:
            return
        protected_urls = {id(block): block.value for block in protected}
        headers = dict(self.headers)
        headers.update(source_headers)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        connector = aiohttp.TCPConnector(ssl=False, limit=4)
        async with aiohttp.ClientSession(
                headers=headers, connector=connector,
                cookie_jar=self._cookie_jar_for_source(source), timeout=timeout) as session:
            semaphore = asyncio.Semaphore(4)

            async def load(block):
                try:
                    if image_decode and str(block.value).count("#") >= 2:
                        block.value = b""
                        return
                    async with semaphore:
                        async with session.get(
                                block.value, headers=block.headers or None,
                                ssl=False) as response:
                            response.raise_for_status()
                            block.value = await response.read()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if image_decode:
                        block.value = b""

            await asyncio.gather(*(load(block) for block in protected))
        if image_decode:
            for block in protected:
                if not isinstance(block.value, bytes):
                    continue
                try:
                    decoded = await self.js.evaluate_async(
                        image_decode,
                        JsContext(
                            source=source, result=block.value,
                            src=protected_urls[id(block)],
                            base_url=protected_urls[id(block)],
                            variables=self.js_variables,
                            book=getattr(self, "current_book", None),
                        ),
                    )
                    if isinstance(decoded, list):
                        block.value = bytes((int(item) & 255) for item in decoded)
                    else:
                        block.value = b""
                except Exception:
                    # ImageUtils.decode uses runCatching(...).getOrNull(): a
                    # thrown script or non-ByteArray result is a decode failure.
                    block.value = b""

    async def content(self, source, chapter: ChapterItem,
                      next_chapter_url: str = "",
                      need_save: bool = True) -> str:
        """Compatibility API for callers that only understand plain text."""
        return (await self.content_document(
            source, chapter, next_chapter_url, need_save)).raw_content
