"""Legado-compatible catalogue and chapter-content loading pipeline."""
from __future__ import annotations

import asyncio
import ast
import aiohttp
import base64
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
import json
import re
from urllib.parse import urljoin, urlsplit
from yarl import URL

from bs4 import BeautifulSoup

from test_book_sources_async import parse_request_config, parse_search_url
from app.services.legado_js import JsContext, split_js_rule
from app.services.legado_rule_parser import evaluate
from app.services.rule_engine import (BookItem, ChapterItem, ReaderEngine,
                                      _apply_regex_suffix,
                                      _java_replacement, extract_value,
                                      extract_values)




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

    @property
    def plain_text(self) -> str:
        return "\n".join(str(block.value) for block in self.blocks if block.kind == "text")


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
    document = ChapterContent()

    def append_text(text: str):
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

    if "<" in value and ">" in value:
        soup = BeautifulSoup(re.sub(r"<br\s*/?>", "\n", value, flags=re.I), "html.parser")
        media_tags = soup.find_all(["img", "audio", "video", "source"])
        for tag in media_tags:
            src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
            if not src:
                continue
            preceding = "".join(str(item) for item in tag.previous_siblings)
            append_text(BeautifulSoup(preceding, "html.parser").get_text("\n", strip=True))
            for sibling in list(tag.previous_siblings):
                sibling.extract()
            hinted = "image" if tag.name == "img" else tag.name
            url, headers = _media_request(src, base_url)
            kind = _media_kind(url, hinted if hinted != "source" else "")
            if kind:
                document.blocks.append(ContentBlock(kind, url, headers, tag.get("title", "")))
            tag.extract()
        append_text(soup.get_text("\n", strip=True))
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
        return re.sub(pattern, _java_replacement(replacement), value, flags=re.I)
    except re.error:
        return value


class LegadoReadingEngine(ReaderEngine):
    """Adds detail init, paged TOC, chapter context and paged content."""

    def __init__(self, timeout: int = 15, webview_fetcher=None,
                 verification_fetcher=None, state_dir=None):
        super().__init__(timeout, state_dir=state_dir)
        self.webview_fetcher = webview_fetcher
        self.verification_fetcher = verification_fetcher

    async def _replace_content_stages(self, value: str, rule: str, source,
                                      chapter, context=None) -> str:
        """Apply every replaceRegex regex/JS stage to the previous result."""
        remaining = str(rule or "").strip()
        result = str(value)
        root, kind, final_url = context or (None, "html", chapter.url)
        while remaining:
            if remaining.startswith("<js>") and "</js>" in remaining:
                end = remaining.find("</js>") + 5
                script, remaining = remaining[:end], remaining[end:].strip()
                body = script[4:-5].strip()
                if body.startswith("##"):
                    result = _apply_replace_regex(result, body)
                    continue
                transformed = await asyncio.to_thread(
                    self.js.evaluate, script,
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
                transformed = await asyncio.to_thread(
                    self.js.evaluate, remaining,
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

    def _bind_interaction(self, source, loop):
        if self.webview_fetcher:
            self.js.browser_fetcher = lambda url, _title="": (
                asyncio.run_coroutine_threadsafe(
                    self.webview_fetcher(url, source), loop).result())
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
        if not raw:
            return str(cover_url)
        if not script.startswith(("@js:", "<js>")):
            script = "@js:" + script
        decoded = await asyncio.to_thread(
            self.js.evaluate, script,
            JsContext(source=source, src=str(cover_url), result=list(raw),
                      base_url=str(cover_url), variables=self.js_variables),
        )
        if isinstance(decoded, str):
            if decoded.startswith(("data:", "http://", "https://")):
                return decoded
            try:
                payload = base64.b64decode(decoded, validate=True)
            except Exception:
                payload = decoded.encode("latin-1", errors="ignore")
        elif isinstance(decoded, (bytes, bytearray)):
            payload = bytes(decoded)
        elif isinstance(decoded, (list, tuple)):
            payload = bytes(int(value) & 0xff for value in decoded)
        else:
            return str(cover_url)
        mime = "image/png" if payload.startswith(b"\x89PNG") else (
            "image/webp" if payload.startswith(b"RIFF") else "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    async def _resolve_rule_url(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        self._bind_interaction(source, asyncio.get_running_loop())
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
        use_webview = bool(request.get("webView") or request.get("webview"))
        if use_webview and self.webview_fetcher:
            return await self._fetch_browser_response(request["url"], source, request)
        try:
            response = await super()._request(request, source)
            login_check = str(getattr(source, "loginCheckJs", "") or "").strip()
            if not login_check or not self.last_response:
                return response
            response_url = str(self.last_response.get("url") or request.get("url") or response[2])
            aiohttp_cookies = self.cookie_jar.filter_cookies(URL(response_url))
            cookie_header = "; ".join(
                f"{name}={morsel.value}" for name, morsel in aiohttp_cookies.items())
            self.js.import_cookie_header(response_url, cookie_header)
            loop = asyncio.get_running_loop()
            self._bind_interaction(source, loop)
            try:
                checked = await asyncio.to_thread(
                    self.js.evaluate, login_check,
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
                self._import_js_cookies(str(data.get("url") or response_url))
                return str(data.get("body", "")), ctype, str(data.get("url") or response[2])
            self._import_js_cookies(response_url)
            return response
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403 and self.webview_fetcher:
                return await self._fetch_browser_response(request["url"], source, request)
            raise

    async def _fetch_browser_response(self, url: str, source=None, options=None):
        try:
            value = await self.webview_fetcher(url, source, options or {})
        except TypeError:
            # Preserve adapters written against the earlier two-argument API.
            value = await self.webview_fetcher(url, source)
        if isinstance(value, dict):
            body = str(value.get("body") or "")
            final_url = str(value.get("url") or url)
            headers = value.get("headers") if isinstance(value.get("headers"), dict) else {}
            content_type = str(headers.get("content-type") or "text/html").split(";", 1)[0]
            cookie = str(value.get("cookie") or "")
            if cookie:
                parsed = SimpleCookie()
                parsed.load(cookie)
                self.cookie_jar.update_cookies(
                    {name: morsel.value for name, morsel in parsed.items()},
                    response_url=URL(final_url))
                self.js.import_cookie_header(final_url, cookie)
            self.last_response = {
                "body": body, "bodyBytes": list(body.encode("utf-8")),
                "headers": {str(k).lower(): str(v) for k, v in headers.items()},
                "status": int(value.get("status") or 200), "url": final_url,
            }
            return body, content_type, final_url
        if isinstance(value, (list, tuple)):
            body = str(value[0] if value else "")
            content_type = str(value[1] if len(value) > 1 else "text/html")
            final_url = str(value[2] if len(value) > 2 else url)
            return body, content_type, final_url
        return str(value or ""), "text/html", str(url)

    def _import_js_cookies(self, url: str) -> None:
        parsed = SimpleCookie()
        parsed.load(self.js.export_cookie_header(url))
        if parsed:
            self.cookie_jar.update_cookies(
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
        loop = asyncio.get_running_loop()
        self._bind_interaction(source, loop)
        try:
            result = await asyncio.to_thread(
                self.js.evaluate, script,
                JsContext(source=source, result="", base_url=chapter.url,
                          variables=self.js_variables,
                          book=getattr(self, "current_book", None), chapter=chapter),
            )
        finally:
            self._unbind_interaction()
        return result

    async def login_schema(self, source) -> list[dict]:
        """Parse Legado's JavaScript-object loginUi declaration."""
        definition = str(getattr(source, "loginUi", "") or "").strip()
        if not definition.startswith("["):
            return []
        script = (
            "@js:(function(){var ui=eval('(' + source.loginUi + ')');"
            "return JSON.stringify({ui:ui,values:infoMap});})()"
        )
        value = await asyncio.to_thread(
            self.js.evaluate, script,
            JsContext(source=source, base_url=getattr(source, "bookSourceUrl", ""),
                      variables=self.js_variables),
        )
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, dict) or not isinstance(parsed.get("ui"), list):
            return []
        stored = parsed.get("values") if isinstance(parsed.get("values"), dict) else {}
        result = []
        for item in parsed["ui"]:
            if isinstance(item, dict):
                item = dict(item)
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
        ui = str(getattr(source, "loginUi", "") or "").lstrip()
        login = str(getattr(source, "loginUrl", "") or "")
        return (ui.startswith("[") and bool(login.strip())) or bool(cls.login_target(source))

    async def execute_login_action(self, source, action: str = "",
                                   values: dict | None = None):
        """Execute a loginUi action in the same scope as source.loginUrl."""
        values_json = json.dumps(values or {}, ensure_ascii=False)
        login_url = str(getattr(source, "loginUrl", "") or "").strip()
        # A source without a custom login UI uses its URL as a browser login.
        target = self.login_target(source)
        if not action and target:
            if not self.webview_fetcher:
                raise RuntimeError("该书源需要网页登录，但当前平台未配置网页交互")
            return await self.webview_fetcher(target, source)
        script = "@js:" + (
            f"var __values={values_json};"
            "Object.keys(__values).forEach(function(k){infoMap[k]=__values[k]});"
            "infoMap.save();"
            "var __login=String(source.loginUrl||'').trim();"
            "if(__login && !/^https?:\\/\\//i.test(__login)){eval(__login);}" 
            f"eval({json.dumps(str(action or ''), ensure_ascii=False)});"
        )
        self._bind_interaction(source, asyncio.get_running_loop())
        try:
            return await asyncio.to_thread(
                self.js.evaluate, script,
                JsContext(source=source,
                          base_url=getattr(source, "bookSourceUrl", ""),
                          variables=self.js_variables),
            )
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
                    body = (decoded.hex() if options.get("type") not in (None, "")
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
                return json.loads(text), "json"
            except json.JSONDecodeError:
                pass
        return BeautifulSoup(text, "html.parser"), "html"

    def _strict_value(self, node, rule: str, kind: str, source, base_url: str,
                      book=None, chapter=None, raw_result=None,
                      is_url: bool = False, collect_all: bool = False) -> str:
        if not rule:
            return ""
        if "{{" in rule or "@get:" in rule or "@put:" in rule:
            value = self._field_value(node, rule, kind, source, base_url,
                                      book=book, chapter=chapter,
                                      raw_result=raw_result)
            if is_url:
                value = _resolve_rule_url(value, base_url)
            return value
        selector, post_js = self._field_parts(rule)
        if post_js:
            value = self._field_value(node, rule, kind, source, base_url,
                                      book=book, chapter=chapter,
                                      raw_result=raw_result)
            if is_url:
                value = _resolve_rule_url(value, base_url)
            return value
        if kind == "json" and "{" in rule:
            return extract_value(node, rule, kind, base_url)
        selector_parts = selector.split("##")
        selector = selector_parts[0]
        regex_suffix = ("##" + "##".join(selector_parts[1:])
                        if len(selector_parts) > 1 else "")
        if not selector and regex_suffix:
            initial = raw_result if raw_result is not None else (
                json.dumps(node, ensure_ascii=False) if kind == "json" else str(node))
            return _apply_regex_suffix(str(initial), regex_suffix).strip()
        try:
            values = evaluate(node, selector, kind)
        except Exception:
            values = []
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
                    value = str(item)
                if value.strip():
                    rendered.append(value.strip())
            return _apply_regex_suffix("\n".join(rendered), regex_suffix).strip()
        first = values[0]
        if is_url and hasattr(first, "get"):
            value = (first.get("href") or first.get("src") or
                     first.get("data-src") or first.get("value") or "")
        else:
            value = first.get_text(" ", strip=True) if hasattr(first, "get_text") else str(first)
        if regex_suffix:
            value = _apply_regex_suffix(value, regex_suffix)
        if is_url:
            value = _resolve_rule_url(value, base_url)
        return value.strip()

    def _next_page_values(self, node, rule: str, kind: str, source,
                          base_url: str, book=None, chapter=None,
                          raw_result=None, next_chapter_url="") -> list[str]:
        """Evaluate a Legado next-page rule without collapsing URL arrays."""
        selector, post_js = self._field_parts(rule)
        if post_js:
            result = self._field_value(
                node, rule, kind, source, base_url, book=book,
                chapter=chapter, raw_result=raw_result,
                title=getattr(chapter, "name", "") if chapter else "",
                next_chapter_url=next_chapter_url)
        elif selector:
            try:
                values = extract_values(node, selector, kind, base_url)
            except Exception:
                values = []
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
                          base_url: str, book=None, chapter=None) -> tuple[list, str]:
        value = (rule or "").strip()
        reverse = value.startswith("-")
        if value.startswith(("-", "+")):
            value = value[1:].strip()
        nodes, kind = [], "html"
        while True:
            js_rule, remaining = split_js_rule(value)
            if js_rule:
                transformed = await asyncio.to_thread(
                    self.js.evaluate, js_rule,
                    JsContext(source=source, result=text, base_url=base_url,
                              variables=self.js_variables, book=book, chapter=chapter),
                )
                text = transformed if isinstance(transformed, str) else json.dumps(
                    transformed, ensure_ascii=False)
                content_type = ("application/json" if isinstance(transformed, (dict, list))
                                or text.lstrip().startswith(("{", "[")) else "text/html")
                value = remaining
                if value:
                    continue
                root, kind = self._root(text, content_type)
                nodes = root if isinstance(root, list) else [root]
                break

            root, kind = self._root(text, content_type)
            selector, suffix = self._field_parts(value)
            try:
                nodes = evaluate(root, selector, kind) if selector else (
                    root if isinstance(root, list) else [root])
            except Exception:
                nodes = []
            if not suffix:
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
            transformed = await asyncio.to_thread(
                self.js.evaluate, script,
                JsContext(source=source, result=js_result, base_url=base_url,
                          variables=self.js_variables, book=book, chapter=chapter,
                          src=text, result_html_nodes=kind == "html"),
            )
            text = transformed if isinstance(transformed, str) else json.dumps(
                transformed, ensure_ascii=False)
            content_type = ("application/json" if isinstance(transformed, (dict, list))
                            or text.lstrip().startswith(("{", "[")) else "text/html")
            if value:
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
            root, kind = self._root(text, content_type)
            nodes = root if isinstance(root, list) else [root]
            break
        if not nodes and kind == "html":
            for selector in (".mulu_list li", ".chapter-list li", "#list dd",
                             "main li:has(a[href*='/book/'])",
                             "li:has(a[href*='/book/'])", "[class*='chapter'] li",
                             "a[href*='/chapter/']",
                             "a[href*='/book/']"):
                nodes = root.select(selector)
                if len(nodes) >= 2:
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
        nodes, kind = await self._list_nodes(
            text, content_type, init, source, final_url, book=book)
        if nodes:
            root = nodes[0]
            text = (json.dumps(root, ensure_ascii=False)
                    if kind == "json" else str(root))
        return root, kind, text

    def _apply_book_info(self, source, book, root, kind, raw_text, base_url):
        rules = source.ruleBookInfo or {}
        mappings = {
            "author": "author", "intro": "intro", "coverUrl": "cover_url",
            "kind": "kind", "lastChapter": "last_chapter",
            "wordCount": "word_count", "downloadUrls": "download_urls",
        }
        for rule_name, attribute in mappings.items():
            rule = rules.get(rule_name)
            if not rule:
                continue
            value = self._strict_value(
                root, rule, kind, source, base_url, book=book,
                raw_result=raw_text, is_url=rule_name in {"coverUrl", "downloadUrls"})
            if value:
                setattr(book, attribute, value)
        rename = str(rules.get("canReName") or "").strip().lower()
        if rules.get("name"):
            value = self._strict_value(
                root, rules["name"], kind, source, base_url,
                book=book, raw_result=raw_text)
            if value and (not book.name or rename in {"1", "true", "rename"}):
                book.name = value
        book.variables = dict(self.js_variables)

    async def book_info(self, source, book: BookItem) -> BookItem:
        """Load book-detail rules without fetching the table of contents."""
        self.js_variables.update(getattr(book, "variables", {}) or {})
        detail_text, detail_type, detail_url = await self._request_value(
            book.url, source, source.bookSourceUrl)
        detail_root, detail_kind, detail_text = await self._initialize_detail(
            source, book, detail_text, detail_type, detail_url)
        self._apply_book_info(
            source, book, detail_root, detail_kind, detail_text, detail_url)
        self.current_book = book
        return book

    @staticmethod
    def _automatic_next_page(root, base_url: str, content_page: bool = False) -> str:
        """Conservative fallback for sources which omit a next-page rule."""
        if not hasattr(root, "find_all"):
            return ""
        candidates = []
        for anchor in root.find_all("a", href=True):
            label = re.sub(r"\s+", "", anchor.get_text(" ", strip=True))
            rel = " ".join(anchor.get("rel", []))
            classes = anchor.get("class", [])
            class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
            ident = f"{anchor.get('id', '')} {class_text}".lower()
            page_label = bool(re.fullmatch(
                r"(?:下一页|下页|下一頁|下頁|继续阅读|繼續閱讀|›|»|>)", label))
            is_next = ((rel.lower() == "next" and not content_page) or page_label or
                       bool(re.search(r"(?:^|[-_])(next|page-next)(?:$|[-_])", ident)))
            if not is_next:
                continue
            # A chapter-navigation link must never be merged into the current chapter.
            if content_page and re.search(r"下一章|下章|后一章|後一章", label):
                continue
            raw_href = anchor.get("href", "").strip()
            href = urljoin(base_url, raw_href)
            if raw_href and not raw_href.lower().startswith(("javascript:", "#")):
                candidates.append(href)
        return candidates[0] if candidates else ""

    async def chapters(self, source, book: BookItem) -> list[ChapterItem]:
        self.js_variables.update(getattr(book, "variables", {}) or {})
        detail_text, detail_type, detail_url = await self._request_value(
            book.url, source, source.bookSourceUrl)
        detail_root, detail_kind, detail_text = await self._initialize_detail(
            source, book, detail_text, detail_type, detail_url)
        info_rule = source.ruleBookInfo or {}
        self._apply_book_info(
            source, book, detail_root, detail_kind, detail_text, detail_url)
        toc_rule = source.ruleToc or {}
        list_rule = toc_rule.get("chapterList") or ".chapter-list li||#list li||.listmain dd||main li"
        toc_url_rule = info_rule.get("tocUrl", "")
        toc_url = self._strict_value(
            detail_root, toc_url_rule, detail_kind, source, detail_url,
            book=book, raw_result=detail_text, is_url=True,
        ) if toc_url_rule else detail_url
        if isinstance(toc_url, str) and toc_url.strip().lower().startswith(
                ("javascript:", "void(")):
            toc_url = ""
        # Some legacy sources mistakenly use the chapter-list selector as
        # tocUrl. Legado keeps parsing the detail document in that case; do not
        # navigate into the first chapter and then search for a TOC there.
        if toc_url_rule and toc_url and toc_url != detail_url:
            try:
                detail_nodes = evaluate(detail_root, list_rule.lstrip("+-").strip(),
                                        detail_kind)
            except Exception:
                detail_nodes = []
            chapter_url_rule = toc_rule.get("chapterUrl") or (
                "url||href||chapterUrl" if detail_kind == "json" else "a@href")
            detail_chapter_urls = set()
            for node in detail_nodes[:50]:
                if getattr(node, "name", "") == "a" and node.get("href"):
                    value = urljoin(detail_url, node.get("href"))
                else:
                    value = self._strict_value(
                        node, chapter_url_rule, detail_kind, source,
                        detail_url, book=book, is_url=True)
                if value:
                    detail_chapter_urls.add(value)
            if toc_url in detail_chapter_urls:
                toc_url = detail_url
        if toc_url_rule and not toc_url and detail_kind == "html":
            anchor = (detail_root.select_one("a[href*='/chapters/']") or
                      next((a for a in detail_root.find_all("a", href=True)
                            if "全部章节" in a.get_text() or "所有章节" in a.get_text()), None))
            if anchor is not None:
                toc_url = urljoin(detail_url, anchor.get("href", ""))
        if isinstance(toc_url, str) and toc_url.strip().lower().startswith(
                ("javascript:", "void(")):
            toc_url = ""
        toc_url = toc_url or detail_url
        book.toc_url = toc_url
        pre_update = str(toc_rule.get("preUpdateJs") or "").strip()
        if pre_update:
            if not pre_update.startswith(("@js:", "<js>")):
                pre_update = "@js:" + pre_update
            self._bind_interaction(source, asyncio.get_running_loop())
            try:
                await asyncio.to_thread(
                    self.js.evaluate, pre_update,
                    JsContext(source=source, result=toc_url, base_url=toc_url,
                              variables=self.js_variables, book=book))
            finally:
                self._unbind_interaction()
            toc_url = book.toc_url or toc_url
        chapters, visited_pages = [], set()
        pending_pages = [toc_url]
        for _ in range(30):
            if not pending_pages:
                break
            current_page = pending_pages.pop(0)
            current_key = _media_request(current_page, detail_url)[0]
            if not current_key or current_key in visited_pages:
                continue
            visited_pages.add(current_key)
            text, ctype, final_url = await self._request_value(
                current_page, source, detail_url)
            nodes, kind = await self._list_nodes(
                text, ctype, list_rule, source, final_url, book=book)
            for node in nodes:
                name = self._strict_value(
                    node, toc_rule.get("chapterName") or
                    ("name||title" if kind == "json" else "a@text||.title@text"),
                    kind, source, final_url, book=book)
                url = self._strict_value(
                    node, toc_rule.get("chapterUrl") or
                    ("url||href||chapterUrl" if kind == "json" else "a@href"),
                    kind, source, final_url, book=book, is_url=True)
                if isinstance(url, str) and url.strip().lower().startswith(
                        ("javascript:", "void(")):
                    url = ""
                if kind == "html" and hasattr(node, "get_text"):
                    anchor = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
                    if not name:
                        name = node.get_text(" ", strip=True)
                    if (anchor is not None and
                            (not url or url.lstrip().startswith("<") or
                             re.search(r"%3[cC](?:li|a|div)", url))):
                        url = urljoin(final_url, anchor.get("href", ""))
                        option_match = re.search(
                            r'(,\s*\{[\s\S]*\})(?:###)?$',
                            str(toc_rule.get("chapterUrl") or ""))
                        if option_match:
                            url += option_match.group(1)
                if isinstance(url, str) and url.strip().lower().startswith(
                        ("javascript:", "void(")):
                    url = ""
                chapter = ChapterItem(
                    name=name, url=url,
                    tag=self._field_value(node, toc_rule.get("updateTime", ""), kind,
                                          source, final_url, book=book)
                    if toc_rule.get("updateTime") else "",
                )
                def truthy(rule_name):
                    if not toc_rule.get(rule_name):
                        return False
                    raw = self._strict_value(
                        node, toc_rule[rule_name], kind, source, final_url,
                        book=book)
                    return _legado_truthy(raw)
                chapter.is_volume = truthy("isVolume")
                chapter.is_vip = truthy("isVip")
                chapter.is_pay = truthy("isPay")
                if chapter.name:
                    chapter.url = chapter.url or ("" if chapter.is_volume else final_url)
                    chapter.variables = dict(self.js_variables)
                    chapters.append(chapter)

            page_root, page_kind = self._root(text, ctype)
            next_rule = toc_rule.get("nextTocUrl", "")
            next_urls = (self._next_page_values(
                page_root, next_rule, page_kind, source, final_url,
                book=book, raw_result=text) if next_rule else [
                    self._automatic_next_page(page_root, final_url)
                ])
            for next_url in next_urls:
                key = _media_request(next_url, final_url)[0]
                if (key and key not in visited_pages and
                        all(_media_request(item, final_url)[0] != key
                            for item in pending_pages)):
                    pending_pages.append(next_url)

        unique, seen = [], set()
        for chapter in chapters:
            key = (chapter.name, chapter.url)
            # Legado trusts ruleToc.chapterList. A source may intentionally
            # expose pages as readable entries (reviews, image sets, etc.);
            # filtering names such as "第001页" changes source semantics.
            if key in seen:
                continue
            seen.add(key)
            chapter.index = len(unique)
            unique.append(chapter)
        book.total_chapter_num = len(unique)
        self.current_book = book
        return unique

    async def content_document(self, source, chapter: ChapterItem,
                               next_chapter_url: str = "") -> ChapterContent:
        self.js_variables.update(getattr(chapter, "variables", {}) or {})
        content_rule = source.ruleContent or {}
        rule = content_rule.get("content") or ".content@html||#content@html||article@html||main@html"
        next_rule = content_rule.get("nextContentUrl", "")
        pending_urls, visited, parts = [chapter.url], set(), []
        document_title = ""
        replace_context = None
        next_chapter_key = _media_request(next_chapter_url, chapter.url)[0] if next_chapter_url else ""
        for _ in range(20):
            if not pending_urls:
                break
            current_url = pending_urls.pop(0)
            current_key = _media_request(current_url, chapter.url)[0]
            if (not current_key or current_key in visited or
                    next_chapter_key and current_key == next_chapter_key):
                continue
            visited.add(current_key)
            text, ctype, final_url = await self._request_value(
                current_url, source, chapter.url, request_defaults={
                    "webJs": content_rule.get("webJs", ""),
                    "sourceRegex": content_rule.get("sourceRegex", ""),
                })
            root, kind = self._root(text, ctype)
            if replace_context is None:
                replace_context = (root, kind, final_url)
            if not document_title and content_rule.get("title"):
                document_title = self._strict_value(
                    root, content_rule["title"], kind, source, final_url,
                    book=getattr(self, "current_book", None), chapter=chapter,
                    raw_result=text)
            value = self._strict_value(
                root, rule, kind, source, final_url,
                book=getattr(self, "current_book", None), chapter=chapter,
                raw_result=text, collect_all=True)
            if not value and kind == "html":
                target = None
                for selector in ("#chapterContent", ".content_txt", ".read-article",
                                 ".read-content", "article", "main"):
                    target = root.select_one(selector)
                    if target is not None and len(target.get_text(strip=True)) >= 80:
                        break
                if target is not None:
                    value = target.get_text("\n", strip=True)
            if value:
                parts.append(value)
            next_urls = (self._next_page_values(
                root, next_rule, kind, source, final_url,
                book=getattr(self, "current_book", None), chapter=chapter,
                raw_result=text, next_chapter_url=next_chapter_url) if next_rule else [
                    self._automatic_next_page(root, final_url, content_page=True)
                ])
            for next_url in next_urls:
                key = _media_request(next_url, final_url)[0]
                if (key and key not in visited and key != next_chapter_key and
                        all(_media_request(item, final_url)[0] != key for item in pending_urls)):
                    pending_urls.append(next_url)

        joined = "\n".join(parts)
        replace_rule = str(content_rule.get("replaceRegex", "") or "").strip()
        joined = await self._replace_content_stages(
            joined, replace_rule, source, chapter, replace_context)
        # Common navigation notice, also used by the source shown in the UI.
        joined = re.sub(
            r"[（(]?\s*本章未完[，,、 ]*请点击下[一]?页继续阅读\s*[）)]?",
            "", joined, flags=re.I,
        )
        document = parse_content_blocks(joined, chapter.url)
        document.title = document_title
        if document_title:
            chapter.name = document_title
        document.image_style = str(content_rule.get("imageStyle") or "")
        await self._apply_media_headers(document, source)
        await self._load_protected_images(document, source)
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

    async def _load_protected_images(self, document: ChapterContent, source):
        """Flet Image has no request-header API; fetch protected images as bytes."""
        source_headers = await self._resolved_source_headers(
            source, getattr(source, "bookSourceUrl", ""))
        image_decode = str((getattr(source, "ruleContent", {}) or {}).get(
            "imageDecode", "") or "").strip()
        protected = [block for block in document.blocks
                     if block.kind == "image" and
                     (block.headers or source_headers or image_decode) and
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
                cookie_jar=self.cookie_jar, timeout=timeout) as session:
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
                    decoded = await asyncio.to_thread(
                        self.js.evaluate, image_decode,
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
                except Exception:
                    # Keep the downloaded bytes so a permissive decoder can still
                    # display sources whose optional imageDecode is outdated.
                    pass

    async def content(self, source, chapter: ChapterItem,
                      next_chapter_url: str = "") -> str:
        """Compatibility API for callers that only understand plain text."""
        return (await self.content_document(
            source, chapter, next_chapter_url)).plain_text
