"""常见 Legado 规则的轻量执行器（不执行 JavaScript）。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urljoin

import aiohttp
from bs4 import BeautifulSoup

from test_book_sources_async import (encode_url, jsonpath_get, parse_search_url,
                                     simplify_legado_selector, usable_non_js_rule)
from app.services.legado_js import (JsBridgeError, JsContext, LegadoJsRuntime,
                                    split_js_rule)
from app.services.legado_rule_parser import RuleParseError, evaluate, split_balanced
from app.services.request_compat import escape_form, escape_url_query


class UnsupportedRule(RuntimeError):
    pass


@dataclass
class BookItem:
    name: str
    author: str = ""
    intro: str = ""
    url: str = ""
    cover_url: str = ""
    toc_url: str = ""
    kind: str = ""
    last_chapter: str = ""
    word_count: str = ""
    download_urls: str = ""
    origin: str = ""
    origin_name: str = ""
    type: int = 0
    total_chapter_num: int = 0
    dur_chapter_index: int = 0
    dur_chapter_title: str = ""
    can_update: bool = True
    custom_intro: str = ""
    custom_tag: str = ""
    custom_cover_url: str = ""
    charset: str = ""
    group: int = 0
    latest_chapter_time: int = 0
    last_check_time: int = 0
    last_check_count: int = 0
    dur_chapter_pos: int = 0
    dur_chapter_time: int = 0
    order: int = 0
    origin_order: int = 0
    read_config: dict = field(default_factory=dict)
    info_html: str = ""
    toc_html: str = ""
    variables: dict = field(default_factory=dict)


@dataclass
class ChapterItem:
    name: str
    url: str
    index: int = 0
    is_volume: bool = False
    is_vip: bool = False
    is_pay: bool = False
    tag: str = ""
    base_url: str = ""
    book_url: str = ""
    resource_url: str = ""
    start: int | None = None
    end: int | None = None
    variables: dict = field(default_factory=dict)


@dataclass
class ExploreCategory:
    title: str
    url: str


def parse_explore_categories(source, value=None) -> list[ExploreCategory]:
    """提取静态或已经过 JS 求值的 Legado 分类标签。"""
    raw_value = source.exploreUrl if value is None else value
    if isinstance(raw_value, (list, dict)):
        data = raw_value if isinstance(raw_value, list) else [raw_value]
        entries = [(str(item.get("title", "")).strip(), str(item.get("url", "")).strip())
                   for item in data if isinstance(item, dict)]
        return [ExploreCategory(title, url) for title, url in entries if title and url]
    raw = str(raw_value or "").strip()
    if not raw:
        return []
    entries = []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            entries = [(str(item.get("title", "")).strip(), str(item.get("url", "")).strip())
                       for item in data if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    else:
        for line in raw.splitlines():
            if "::" in line:
                title, url = line.split("::", 1)
                entries.append((title.strip(), url.strip()))
    # url 为空的条目通常只是视觉分组标题，不是可点击分类。
    return [ExploreCategory(title, url) for title, url in entries if title and url]


async def resolve_explore_categories(source, runtime: LegadoJsRuntime | None = None) -> list[ExploreCategory]:
    """解析分类配置，并在需要时通过 Legado JS 兼容层生成分类。"""
    raw = (source.exploreUrl or "").strip()
    if not raw:
        return []
    if not _is_js(raw):
        return parse_explore_categories(source)
    own_runtime = runtime is None
    runtime = runtime or LegadoJsRuntime()
    try:
        value = await asyncio.to_thread(
            runtime.evaluate, raw,
            JsContext(source=source, base_url=source.bookSourceUrl),
        )
    finally:
        if own_runtime:
            runtime.close()
    return parse_explore_categories(source, value)


def _resolve_explore_url(raw: str, base_url: str, page: int = 1) -> str:
    if _is_js(raw) or "{{cookie." in raw or "{{java." in raw:
        raise UnsupportedRule("分类地址需要 JavaScript、Cookie 或动态签名")
    url = _expand_page_template(raw, page)
    # Legado 的 /list/<,index_{{page}}.html> 简单分页写法：第一页取逗号前，后续取逗号后。
    def conditional(match):
        first, later = match.group(1).split(",", 1)
        return first if page == 1 else later
    url = re.sub(r"<([^<>]*,[^<>]*)>", conditional, url)
    if "{{" in url:
        raise UnsupportedRule("分类地址包含暂不支持的动态模板")
    return encode_url(urljoin(base_url.rstrip("/") + "/", url))


def _expand_page_template(value: str, page: int) -> str:
    value = value.replace("{{page}}", str(page)).replace("{{page-1}}", str(max(page - 1, 0)))
    value = value.replace("{{page+1}}", str(page + 1))
    return value


def _is_js(value: str) -> bool:
    value = (value or "").strip()
    return value.startswith(("@js:", "<js")) or "java." in value


def _split_rule(rule: str) -> tuple[str, str]:
    rule = (rule or "").strip()
    if _is_js(rule):
        raise UnsupportedRule("规则需要 JavaScript 引擎")
    # XPath 内部大量使用 @class、@href；不能按 Legado CSS 的 @ 分隔符拆开。
    # XPath 末尾 /@href、/@src 会由 _xpath_to_css() 单独识别。
    if rule.startswith("//"):
        return rule, ""
    if rule.startswith("@css:"):
        rule = rule[5:].strip()
    if rule.startswith(("@JSON:", "@Json:")):
        rule = rule.split(":", 1)[1].strip()
    if "@" in rule:
        selector, attr = rule.rsplit("@", 1)
        return selector.strip(), attr.strip()
    return rule, ""


def _css_selector(rule: str) -> tuple[str, int | None]:
    """把常见 tag./class./id. 链式规则转换为 CSS，并识别末尾索引。"""
    rule = rule.strip()
    index = None
    match = re.search(r"\.(-?\d+)$", rule)
    if match:
        index = int(match.group(1))
        rule = rule[:match.start()]
    parts = rule.split("@")
    converted = []
    for part in parts:
        part = part.strip()
        if part.startswith("tag."):
            converted.append(part[4:])
        elif part.startswith("class."):
            converted.append("." + part[6:].replace(" ", "."))
        elif part.startswith("id."):
            converted.append("#" + part[3:])
        elif part:
            converted.append(simplify_legado_selector(part) or part)
    return " ".join(converted), index


def _xpath_to_css(rule: str) -> tuple[str, str]:
    """转换常见的 //tag[@class='x']/a/@href 子集。"""
    attr = ""
    if rule.endswith("/text()"):
        rule = rule[:-7]
        attr = "text"
    else:
        match = re.search(r"/@([\w-]+)$", rule)
        if match:
            attr = match.group(1)
            rule = rule[:match.start()]
    # 保留 XPath 轴：单斜杠是直接子级，双斜杠是任意后代。
    chunks = re.findall(r"(//|/)([^/]+)", rule)
    css = []
    axes = []
    for axis, chunk in chunks:
        tag = re.match(r"[\w*-]+", chunk)
        item = (tag.group(0) if tag else "*")
        class_match = re.search(r"@class=['\"]([^'\"]+)", chunk)
        contains_match = re.search(r"contains\(@class,\s*['\"]([^'\"]+)", chunk)
        id_match = re.search(r"@id=['\"]([^'\"]+)", chunk)
        if class_match:
            item += "".join("." + c for c in class_match.group(1).split())
        elif contains_match:
            item += "." + contains_match.group(1)
        if id_match:
            item += "#" + id_match.group(1)
        css.append(item)
        axes.append(axis)
    if not css:
        return "", attr
    result = css[0]
    for index in range(1, len(css)):
        result += (" > " if axes[index] == "/" else " ") + css[index]
    return result, attr


def _json_values(data, rule: str) -> list:
    selector, _ = _split_rule(rule)
    if not selector:
        return data if isinstance(data, list) else [data]
    values = jsonpath_get(data, selector)
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    return values


def _rule_candidates(rule: str) -> list[str]:
    """Legado 使用 || 表示候选规则，逐个尝试而不是截断丢弃。"""
    return split_balanced(rule or "", "||")


def select_nodes(text: str, content_type: str, rule: str) -> tuple[list, str]:
    rule = usable_non_js_rule(rule)
    if _is_js(rule):
        raise UnsupportedRule("列表规则需要 JavaScript 引擎")
    if "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
            values = evaluate(data, rule, "json") if rule else (data if isinstance(data, list) else [data])
            if values:
                return values, "json"
        except (json.JSONDecodeError, TypeError):
            pass
    soup = BeautifulSoup(text, "html.parser")
    try:
        nodes = evaluate(soup, rule, "html")
        if nodes:
            return nodes, "html"
    except RuleParseError:
        pass
    # 无规则或规则失效时，按常见书籍卡片结构智能推断。
    for selector in (".bookbox", ".book-item", ".booklist li", ".listbg", ".novel-item",
                     ".search-result li", "article", "main li"):
        nodes = soup.select(selector)
        if len(nodes) >= 2:
            return nodes, "html"
    # Outdated sources are common. Infer a repeated record container, but only
    # after the source's own rule has produced no nodes.
    groups = {}
    for item in soup.select("li:has(a[href])"):
        if item.find_parent(("nav", "header", "footer")):
            continue
        parent = item.parent
        groups.setdefault(id(parent), (parent, []))[1].append(item)
    if groups:
        inferred = max(groups.values(), key=lambda pair: len(pair[1]))[1]
        if len(inferred) >= 3:
            return inferred, "html"
    # A valid selector with no matching records is an ordinary empty search,
    # not an engine failure. Syntax is checked independently by corpus audit.
    return [], "html"


_PAGE_LABEL_RE = re.compile(
    r"^(?:第?\s*\d+\s*页?|\d+|上一页|下一页|上页|下页|首页|尾页|末页|跳转|更多|»|«|>|<)$",
    re.I,
)


def _is_pagination_item(name: str, url: str) -> bool:
    """识别被列表规则误选中的分页导航。"""
    label = re.sub(r"\s+", "", name or "")
    if not _PAGE_LABEL_RE.match(label):
        return False
    lowered = (url or "").lower()
    return bool(re.search(
        r"(?:[?&/]page(?:num)?[=/]|index[_-]?\d+|/page/\d+|(?:list|index)[_-]\d+\.html)",
        lowered,
    ))


def _is_action_url(url: str) -> bool:
    return str(url or "").strip().lower().startswith(("javascript:", "void("))


def _java_replacement(value: str) -> str:
    """Translate Java/Rhino ``$1`` capture references for Python ``re``."""
    return re.sub(r"\$(\d+)", r"\\g<\1>", str(value or ""))


def _capture_value(node, rule: str) -> str | None:
    """Render Regex-mode records in fields such as ``$3`` or ``url/$1``."""
    if not isinstance(node, (list, tuple)) or not re.search(r"\$\d+", str(rule)):
        return None
    values = ["" if value is None else str(value) for value in node]
    return re.sub(
        r"\$(\d+)",
        lambda match: (values[int(match.group(1))]
                       if int(match.group(1)) < len(values) else ""),
        str(rule),
    )


def extract_values(node, rule: str, kind: str, base_url: str = "") -> list[str]:
    """从一个列表容器按书源字段规则提取全部值，用于网站把多条记录包进单容器的情况。"""
    if not rule or _is_js(rule):
        return []
    captured = _capture_value(node, rule)
    if captured is not None:
        value = captured.strip()
        if base_url and value.startswith(("/", "./", "../", "//", "#")):
            value = urljoin(base_url, value)
        return [value] if value else []
    for candidate in _rule_candidates(rule):
        pieces = candidate.split("##")
        selector_rule = pieces[0]
        try:
            targets = evaluate(node, selector_rule, kind)
        except RuleParseError:
            continue
        values = []
        for target in targets:
            value = (target.get_text(" ", strip=True) if hasattr(target, "get_text") else str(target))
            if len(pieces) >= 2 and pieces[1]:
                try:
                    value = re.sub(pieces[1], _java_replacement(
                        pieces[2] if len(pieces) > 2 else ""), value)
                except re.error:
                    pass
            value = value.strip()
            if base_url and value and value.startswith(("/", "./", "../", "//", "#")):
                value = urljoin(base_url, value)
            if value:
                values.append(value)
        if values:
            return values
    return []


def _apply_regex_suffix(value: str, suffix: str) -> str:
    pieces = str(suffix or "").split("##")
    if len(pieces) < 2 or not pieces[1]:
        return str(value)
    try:
        return re.sub(pieces[1], _java_replacement(
            pieces[2] if len(pieces) > 2 else ""), str(value),
                      count=1 if len(pieces) > 3 else 0)
    except re.error:
        return str(value)


def extract_value(node, rule: str, kind: str, base_url: str = "") -> str:
    if not rule:
        return ""
    if "{baseUrl}" in rule:
        return rule.replace("{baseUrl}", base_url).strip()
    captured = _capture_value(node, rule)
    if captured is not None:
        captured = captured.strip()
        if base_url and captured.startswith(("/", "./", "../", "//", "#")):
            captured = urljoin(base_url, captured)
        return captured
    if _is_js(rule):
        raise UnsupportedRule("字段规则需要 JavaScript 引擎")
    # JSON 字段拼接模板，如 https://site/book/{{$.id}}
    if "{{@@" in rule:
        def replace_nested(match):
            nested = match.group(1).strip()
            values = extract_values(node, nested, kind, base_url)
            return "" if not values else str(values[0])
        value = re.sub(r"\{\{@@(.*?)\}\}", replace_nested, rule)
        value = value.strip()
        return urljoin(base_url, value) if base_url else value

    if kind == "json" and ("{{" in rule or "{$" in rule):
        def replace_field(match):
            expression = (match.group(1) or match.group(2) or "").strip()
            values = _json_values(node, expression)
            return "" if not values else str(values[0])
        value = re.sub(r"\{\{([^{}]+)\}\}|\{(\$[^{}]+)\}", replace_field, rule)
        return urljoin(base_url, value) if base_url else value

    candidates = _rule_candidates(rule) or [rule]
    value = ""
    for candidate in candidates:
        value = _extract_single_value(node, candidate, kind,
                                      first_only=bool(base_url))
        if value:
            break
    # 智能字段回退。
    if not value and kind == "html":
        selector = "a"
        if any(key in rule.lower() for key in ("cover", "src", "img")):
            selector = "img"
        target = node.select_one(selector) if hasattr(node, "select_one") else None
        if target:
            if any(key in rule.lower() for key in ("url", "href")):
                value = target.get("href", "")
            elif any(key in rule.lower() for key in ("cover", "src", "img")):
                value = target.get("data-src") or target.get("data-original") or target.get("src", "")
            else:
                value = target.get("title") or target.get_text(" ", strip=True)
    value = value.strip()
    url_hint = any(key in rule.lower() for key in ("url", "href", "src"))
    relative_hint = value.startswith(("/", "./", "../", "//", "#"))
    if base_url and value and (url_hint or relative_hint) and not value.startswith(("data:", "javascript:")):
        value = urljoin(base_url, value)
    return value


def _extract_single_value(node, rule: str, kind: str,
                          first_only: bool = False) -> str:
    selector_rule = rule.split("##", 1)[0]
    value = ""
    # Older Legado sources often use a bare attribute name (``onclick``,
    # ``href`` or ``style``) to read that attribute from the current element.
    if (kind == "html" and hasattr(node, "get") and
            re.fullmatch(r"[A-Za-z_:][-A-Za-z0-9_:.]*", selector_rule.strip())):
        value = str(node.get(selector_rule.strip()) or "")
    try:
        values = [] if value else evaluate(node, selector_rule, kind)
    except RuleParseError:
        values = []
    if values:
        rendered = [item.get_text(" ", strip=True) if hasattr(item, "get_text")
                    else str(item) for item in values]
        value = rendered[0] if first_only else "\n".join(rendered)
    # 常见 ##正则替换：正文前是提取规则，此处至少完成删除匹配项。
    if "##" in rule:
        pieces = rule.split("##")
        if len(pieces) >= 2 and pieces[1]:
            try:
                value = re.sub(pieces[1], _java_replacement(
                    pieces[2] if len(pieces) > 2 else ""), value)
            except re.error:
                pass
    return value.strip()


class ReaderEngine:
    def __init__(self, timeout: int = 15, state_dir=None):
        self.timeout = timeout
        self.state_dir = state_dir
        self.js = LegadoJsRuntime(timeout, state_dir=state_dir)
        self.js_variables = {}
        self.headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)
        self.last_response = None
        self._rate_states = {}

    async def _respect_concurrent_rate(self, source) -> None:
        """Apply Legado ``concurrentRate`` before starting a network call.

        ``N/T`` permits N starts per T milliseconds. A bare number is the
        minimum interval in milliseconds between request starts.
        """
        raw = str(getattr(source, "concurrentRate", "") or "").strip()
        if not raw:
            return
        source_key = str(getattr(source, "bookSourceUrl", "") or id(source))
        state = self._rate_states.get(source_key)
        if state is None:
            state = {"lock": asyncio.Lock(), "starts": deque(), "last": 0.0}
            self._rate_states[source_key] = state
        try:
            if "/" in raw:
                count_text, window_text = raw.split("/", 1)
                count = max(1, int(count_text))
                window = max(0, int(window_text)) / 1000.0
            else:
                count, window = 1, max(0, int(raw)) / 1000.0
        except ValueError:
            return
        async with state["lock"]:
            now = time.monotonic()
            if "/" not in raw:
                target = state["last"] + window
                # Windows timers can wake asyncio.sleep early. Re-check the
                # monotonic deadline so concurrentRate is a real lower bound.
                while now < target:
                    await asyncio.sleep(target - now)
                    now = time.monotonic()
                state["last"] = time.monotonic()
                return
            starts = state["starts"]
            while starts and now - starts[0] >= window:
                starts.popleft()
            while len(starts) >= count:
                # Leave a small Windows scheduler margin so the caller's
                # observable request start cannot slip below the configured
                # window when earlier waiters resume late.
                await asyncio.sleep(max(
                    0.001, window + 0.005 - (now - starts[0])))
                now = time.monotonic()
                while starts and now - starts[0] >= window:
                    starts.popleft()
            starts.append(time.monotonic())

    @staticmethod
    def _source_headers(source) -> dict:
        raw = getattr(source, "header", "") or ""
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            value = json.loads(raw) if raw.strip() else {}
            return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
        except (json.JSONDecodeError, AttributeError):
            return {}

    async def _resolved_source_headers(self, source, base_url="") -> dict:
        raw = getattr(source, "header", "") or ""
        if isinstance(raw, str) and raw.strip().startswith(("@js:", "<js>")):
            value = await asyncio.to_thread(
                self.js.evaluate, raw.strip(),
                JsContext(source=source, base_url=base_url or source.bookSourceUrl,
                          variables=self.js_variables))
            if isinstance(value, dict):
                return {str(key): str(item) for key, item in value.items()}
            try:
                value = json.loads(str(value or ""))
                return ({str(key): str(item) for key, item in value.items()}
                        if isinstance(value, dict) else {})
            except json.JSONDecodeError:
                return {}
        return self._source_headers(source)

    async def _request(self, request: dict, source=None) -> tuple[str, str, str]:
        request = await self._apply_request_option_js(request, source)
        request_url = str(request.get("url") or "")
        if request_url.startswith("data:"):
            header, _, payload = request_url.partition(",")
            try:
                raw = (base64.b64decode(payload) if ";base64" in header.lower()
                       else unquote_to_bytes(payload))
            except (ValueError, binascii.Error):
                raw = b""
            charset = str(request.get("charset") or "utf-8")
            if charset.lower() in ("gbk", "gb2312", "gb_2312-80"):
                charset = "gb18030"
            text = (raw.hex() if request.get("type") not in (None, "") else
                    raw.decode(charset, errors="replace"))
            content_type = header[5:].split(";", 1)[0] or "text/plain"
            self.last_response = {
                "body": text, "bodyBytes": list(raw),
                "headers": {"content-type": content_type},
                "status": 200, "url": request_url}
            return text, content_type, request_url
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        if source is not None:
            await self._respect_concurrent_rate(source)
        last_error = None
        retry_value = request.get("retry")
        retry_count = max(0, min(10, int(2 if retry_value is None else retry_value)))
        for attempt in range(retry_count + 1):
            connector = aiohttp.TCPConnector(ssl=False)
            try:
                headers = dict(self.headers)
                proxy = request.get("proxy")
                if source is not None:
                    source_headers = await self._resolved_source_headers(
                        source, str(request.get("url") or ""))
                    for name in list(source_headers):
                        if name.lower() == "proxy":
                            proxy = proxy or source_headers.pop(name)
                    headers.update(source_headers)
                request_headers = request.get("headers") or {}
                if isinstance(request_headers, str):
                    try: request_headers = json.loads(request_headers)
                    except json.JSONDecodeError: request_headers = {}
                headers.update(request_headers if isinstance(request_headers, dict) else {})
                request_url = request["url"]
                method = str(request.get("method", "GET")).upper()
                if str(request.get("charset") or "").lower() == "escape":
                    request_url = escape_url_query(request_url)
                if method == "POST":
                    body = str(request.get("body") or "").lstrip()
                    content_type = ("application/json; charset=UTF-8"
                                    if body.startswith(("{", "[")) else
                                    "application/x-www-form-urlencoded; charset=UTF-8")
                    headers.setdefault("Content-Type", content_type)
                async with aiohttp.ClientSession(headers=headers, connector=connector,
                                                 cookie_jar=self.cookie_jar) as session:
                    kwargs = {"timeout": timeout, "ssl": False, "allow_redirects": True}
                    if proxy:
                        proxy = str(proxy).strip()
                        kwargs["proxy"] = (proxy if "://" in proxy else "http://" + proxy)
                    if method == "POST":
                        body = request.get("body") or ""
                        charset_name = str(request.get("charset") or "utf-8")
                        if charset_name.lower() == "escape":
                            if not str(body).lstrip().startswith(("{", "[", "<")):
                                body = escape_form(str(body))
                                charset_name = "ascii"
                            else:
                                charset_name = "utf-8"
                        if charset_name.lower() in ("gbk", "gb2312", "gb_2312-80"):
                            charset_name = "gb18030"
                        kwargs["data"] = (body.encode(charset_name, errors="replace")
                                          if isinstance(body, str) else body)
                    async with session.request(method, encode_url(request_url), **kwargs) as resp:
                        if not (source is not None and getattr(source, "loginCheckJs", "")):
                            resp.raise_for_status()
                        charset = request.get("charset") or resp.charset
                        raw = await resp.read()
                        meta = re.search(
                            br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", raw[:4096], re.I)
                        if meta and not request.get("charset"):
                            charset = meta.group(1).decode("ascii", errors="ignore")
                        if (charset or "").lower() in ("gbk", "gb2312", "gb_2312-80"):
                            charset = "gb18030"
                        text = (raw.hex() if request.get("type") not in (None, "") else
                                raw.decode(charset or "utf-8", errors="replace"))
                        self.last_response = {
                            "body": text,
                            "bodyBytes": list(raw),
                            "headers": {str(k).lower(): str(v) for k, v in resp.headers.items()},
                            "status": int(resp.status),
                            "url": str(resp.url),
                        }
                        return text, resp.content_type or "", str(resp.url)
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError,
                    asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retry_count:
                    await asyncio.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"连接失败（已尝试 {retry_count + 1} 次）：{last_error}")

    async def _apply_request_option_js(self, request: dict, source=None) -> dict:
        script = str(request.get("js") or "").strip()
        if not script or request.get("_jsApplied"):
            return request
        prepared = dict(request)
        context = JsContext(
            source=source, result=str(prepared.get("url") or ""),
            base_url=str(prepared.get("url") or ""), variables=self.js_variables,
            request_headers=(prepared.get("headers")
                             if isinstance(prepared.get("headers"), dict) else {}),
        )
        state = await asyncio.to_thread(self.js.evaluate_url_option, script, context)
        if state.get("url"):
            prepared["url"] = str(state["url"])
        if isinstance(state.get("headers"), dict):
            prepared["headers"] = {str(k): str(v) for k, v in state["headers"].items()}
        prepared["_jsApplied"] = True
        return prepared

    @staticmethod
    def _field_parts(rule: str) -> tuple[str, str]:
        value = rule or ""
        if value.strip().startswith(("@js:", "<js>")):
            return "", value.strip()
        marker = value.find("@js:")
        if marker > 0:
            return value[:marker].strip(), value[marker:].strip()
        marker = value.find("<js>")
        if marker > 0 and "</js>" in value[marker:]:
            # Keep the complete suffix. Legado rules may contain several JS,
            # regex and selector stages and each stage consumes the previous result.
            return value[:marker].strip(), value[marker:].strip()
        return value, ""

    def _field_value(self, node, rule: str, kind: str, source, base_url: str,
                     key: str = "", page: int = 1, book=None, chapter=None,
                     raw_result=None, title: str = "",
                     next_chapter_url: str = "") -> str:
        rule = rule or ""
        put_rule = ""
        put_marker = rule.find("@put:")
        if put_marker >= 0:
            put_rule = rule[put_marker + 5:].strip()
            rule = rule[:put_marker].strip()
        if "{{" in rule and "{{@@" not in rule:
            def render_expression(match):
                expression = match.group(1).strip()
                lowered_expression = expression.lower()
                if expression.startswith("@@"):
                    return extract_value(
                        node, expression[2:], kind, base_url)
                if (expression.startswith("@") and not
                        lowered_expression.startswith((
                            "@js:", "@json:", "@xpath:", "@css:"))):
                    return extract_value(
                        node, expression[1:], kind, base_url)
                if (expression.startswith(("$", "//")) or
                        lowered_expression.startswith(("@json:", "@xpath:", "@css:"))):
                    return extract_value(node, expression, kind, base_url)
                context = JsContext(
                    source=source, key=key, page=page,
                    result=raw_result if raw_result is not None else (
                        node if kind == "json" else str(node)),
                    base_url=base_url, variables=self.js_variables,
                    get_string=lambda nested: extract_value(node, nested, kind, base_url),
                    get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                    get_elements=lambda nested: [str(item) for item in evaluate(node, nested, kind)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                )
                # Rhino/Legado evaluates template bodies as scripts, so forms
                # such as ``{{key; java.put('k', key)}}`` are valid and return
                # the completion value of their final statement.
                value = self.js.evaluate(f"@js:{expression}", context)
                return "" if value is None else str(value)

            rendered = re.sub(r"\{\{([\s\S]*?)\}\}", render_expression, rule)
            if (rule.strip().startswith(("@js:", "<js>")) or
                    rendered.find("@js:") > 0):
                rule = rendered
            else:
                js_marker = rendered.find("<js>")
                if js_marker >= 0 and "</js>" in rendered[js_marker:]:
                    literal, stages = rendered[:js_marker], rendered[js_marker:]
                    value = self._field_value(
                        node, stages, kind, source, base_url, key, page,
                        book, chapter, literal)
                    if put_rule:
                        self._put_rule_values(node, put_rule, kind, source, base_url,
                                              key, page, book, chapter, raw_result)
                    return value.strip()
                pieces = rendered.split("##")
                value = pieces[0]
                if len(pieces) > 1 and pieces[1]:
                    try:
                        value = re.sub(pieces[1], _java_replacement(
                            pieces[2] if len(pieces) > 2 else ""), value,
                                       count=1 if len(pieces) > 3 else 0)
                    except re.error:
                        pass
                if put_rule:
                    self._put_rule_values(node, put_rule, kind, source, base_url,
                                          key, page, book, chapter, raw_result)
                return value.strip()
        stripped_rule = rule.strip()
        if stripped_rule.startswith("<js>") and "</js>" in stripped_rule:
            end = stripped_rule.find("</js>") + 5
            script, remaining = stripped_rule[:end], stripped_rule[end:].strip()
            script_body = script[4:-5].strip()
            initial_result = raw_result if raw_result is not None else (
                node if kind == "json" else str(node))
            if script_body.startswith("##"):
                transformed = _apply_regex_suffix(str(initial_result), script_body)
                if not remaining:
                    return transformed.strip()
                transformed_text = transformed
                try:
                    transformed_root = json.loads(transformed_text)
                    transformed_kind = "json"
                except (json.JSONDecodeError, TypeError):
                    transformed_root = BeautifulSoup(transformed_text, "html.parser")
                    transformed_kind = "html"
                return self._field_value(
                    transformed_root, remaining, transformed_kind, source, base_url,
                    key, page, book, chapter, transformed_text, title,
                    next_chapter_url)
            context = JsContext(
                source=source, key=key, page=page,
                result=initial_result,
                base_url=base_url, variables=self.js_variables,
                get_string=lambda nested: extract_value(node, nested, kind, base_url),
                get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                get_elements=lambda nested: [str(item) for item in evaluate(node, nested, kind)],
                book=book, chapter=chapter, title=title,
                next_chapter_url=next_chapter_url,
                result_html_nodes=(raw_result is None and kind == "html" and
                                   hasattr(node, "select")),
            )
            transformed = self.js.evaluate(script, context)
            if not remaining:
                return "" if transformed is None else str(transformed)
            if remaining.startswith("##"):
                return _apply_regex_suffix("" if transformed is None else str(transformed),
                                           remaining).strip()
            transformed_text = (json.dumps(transformed, ensure_ascii=False)
                                if not isinstance(transformed, str) else transformed)
            try:
                transformed_root = json.loads(transformed_text)
                transformed_kind = "json"
            except (json.JSONDecodeError, TypeError):
                transformed_root = BeautifulSoup(transformed_text, "html.parser")
                transformed_kind = "html"
            return self._field_value(
                transformed_root, remaining, transformed_kind, source, base_url,
                key, page, book, chapter, transformed_text, title,
                next_chapter_url,
            )
        get_matches = list(re.finditer(r"@get:\{([^{}]+)\}", rule))
        if get_matches:
            value = re.sub(
                r"@get:\{([^{}]+)\}",
                lambda match: str(self.js_variables.get(match.group(1), "")), rule)
            if value.strip().startswith("@js:"):
                # @get is preprocessing inside a JavaScript stage, not the
                # terminal value of the field rule. Continue into the normal
                # JS pipeline after interpolation.
                rule = value
            else:
                pieces = value.split("##")
                value = pieces[0]
                if len(pieces) >= 2 and pieces[1]:
                    try:
                        value = re.sub(pieces[1], _java_replacement(
                            pieces[2] if len(pieces) > 2 else ""), value)
                    except re.error:
                        pass
                if put_rule:
                    self._put_rule_values(node, put_rule, kind, source, base_url,
                                          key, page, book, chapter, raw_result)
                return value.strip()

        selector, post_js = self._field_parts(rule)
        value = ((raw_result if raw_result is not None else node)
                 if not selector and post_js else extract_value(node, selector, kind, base_url))
        remaining = post_js.strip()
        while remaining:
            if remaining.startswith("<js>") and "</js>" in remaining:
                end = remaining.find("</js>") + 5
                script = remaining[:end]
                remaining = remaining[end:].strip()
                body = script[4:-5].strip()
                if body.startswith("##"):
                    value = _apply_regex_suffix(str(value), body)
                    continue
                context = JsContext(
                    source=source, key=key, page=page, result=value,
                    base_url=base_url, variables=self.js_variables,
                    get_string=lambda nested: extract_value(node, nested, kind, base_url),
                    get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                    get_elements=lambda nested: [str(item) for item in evaluate(node, nested, kind)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(not selector and raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                )
                transformed = self.js.evaluate(script, context)
                value = "" if transformed is None else transformed
                continue
            if remaining.startswith("@js:"):
                script, regex_suffix = remaining, ""
                if "##" in remaining:
                    script, regex_body = remaining.split("##", 1)
                    regex_suffix = "##" + regex_body
                context = JsContext(
                    source=source, key=key, page=page, result=value,
                    base_url=base_url, variables=self.js_variables,
                    get_string=lambda nested: extract_value(node, nested, kind, base_url),
                    get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                    get_elements=lambda nested: [str(item) for item in evaluate(node, nested, kind)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(not selector and raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                )
                value = self.js.evaluate(script, context)
                if regex_suffix:
                    value = _apply_regex_suffix(
                        "" if value is None else str(value), regex_suffix)
                remaining = ""
                continue
            # A selector after JavaScript analyzes the transformed payload.
            transformed_text = (value if isinstance(value, str) else
                                json.dumps(value, ensure_ascii=False))
            try:
                transformed_root = json.loads(transformed_text)
                transformed_kind = "json"
            except (json.JSONDecodeError, TypeError):
                transformed_root = BeautifulSoup(str(transformed_text), "html.parser")
                transformed_kind = "html"
            value = self._field_value(
                transformed_root, remaining, transformed_kind, source, base_url,
                key, page, book, chapter, transformed_text, title,
                next_chapter_url)
            remaining = ""
        if base_url and isinstance(value, str) and value.startswith(("/", "./", "../", "//")):
            value = urljoin(base_url, value)
        if put_rule:
            self._put_rule_values(node, put_rule, kind, source, base_url,
                                  key, page, book, chapter, raw_result)
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    def _put_rule_values(self, node, put_rule: str, kind: str, source,
                         base_url: str, key="", page=1, book=None,
                         chapter=None, raw_result=None):
        body = put_rule.strip()
        if body.startswith("{") and body.endswith("}"):
            body = body[1:-1]
        for item in split_balanced(body, ","):
            if ":" not in item:
                continue
            name, nested_rule = item.split(":", 1)
            name = name.strip().strip("'\"")
            nested_rule = nested_rule.strip().strip("'\"")
            if not name:
                continue
            self.js_variables[name] = self._field_value(
                node, nested_rule, kind, source, base_url, key, page,
                book, chapter, raw_result)

    def _book_from_node(self, node, kind: str, rules: dict, base_url: str,
                        source=None, key: str = "", page: int = 1) -> BookItem:
        def field(primary, fallback, with_base=False):
            try:
                value = self._field_value(node, primary or fallback, kind, source,
                                          base_url if with_base else "", key, page)
                if value or not primary:
                    return value
            except (UnsupportedRule, RuleParseError, JsBridgeError):
                pass
            return self._field_value(node, fallback, kind, source,
                                     base_url if with_base else "", key, page)

        book = BookItem(
            name=field(rules.get("name"), "name||title||bookName" if kind == "json" else
                       ".book-name@text||.title@text||h3@text||h2@text||a@title||a@text"),
            author=field(rules.get("author"), "author||authorName" if kind == "json" else
                         ".author@text||.book-author@text"),
            intro=field(rules.get("intro"), "intro||description||desc" if kind == "json" else
                        ".intro@text||.description@text||p@text"),
            url=field(rules.get("bookUrl"), "bookUrl||url||href" if kind == "json" else
                      "a@href", True),
            cover_url=field(rules.get("coverUrl"), "coverUrl||cover||image" if kind == "json" else
                            "img@data-src||img@data-original||img@src", True),
        )
        book.variables = dict(self.js_variables)
        return book

    def _books_from_nodes(self, nodes: list, kind: str, rules: dict, base_url: str,
                          source=None, key: str = "", page: int = 1,
                          list_put: str = "") -> list[BookItem]:
        # 严格规则只命中一个容器时，按字段规则批量提取并按位置组合。
        if kind == "html" and len(nodes) == 1:
            names = extract_values(nodes[0], rules.get("name", ""), kind)
            urls = extract_values(nodes[0], rules.get("bookUrl", ""), kind, base_url)
            if len(names) >= 2 and len(urls) >= 2:
                authors = extract_values(nodes[0], rules.get("author", ""), kind)
                intros = extract_values(nodes[0], rules.get("intro", ""), kind)
                covers = extract_values(nodes[0], rules.get("coverUrl", ""), kind, base_url)
                books = []
                for index in range(min(len(names), len(urls), 100)):
                    books.append(BookItem(
                        name=names[index], url=urls[index],
                        author=authors[index] if index < len(authors) else "",
                        intro=intros[index] if index < len(intros) else "",
                        cover_url=covers[index] if index < len(covers) else "",
                    ))
                return books
        books = []
        for node in nodes[:100]:
            if list_put:
                self._put_rule_values(node, list_put, kind, source, base_url,
                                      key, page, raw_result=str(node))
            books.append(self._book_from_node(
                node, kind, rules, base_url, source, key, page))
        return books

    @staticmethod
    def _list_rule_parts(rule: str) -> tuple[str, str]:
        marker = str(rule or "").find("@put:")
        if marker < 0:
            return str(rule or ""), ""
        return str(rule or "")[:marker].strip(), str(rule or "")[marker + 5:].strip()

    async def _list_rule_pipeline(self, text: str, content_type: str, rule: str,
                                  source, base_url: str, key: str = "",
                                  page: int = 1) -> tuple[list, str]:
        """Run every selector/JS stage in a Legado list rule in order."""
        value = str(rule or "").strip()
        def js_context_result(rule_text):
            try:
                root = (json.loads(text) if "json" in (content_type or "").lower()
                        else BeautifulSoup(text, "html.parser"))
                root_kind = "json" if isinstance(root, (dict, list)) else "html"
                return [str(item) for item in evaluate(
                    root, str(rule_text), root_kind)]
            except (json.JSONDecodeError, RuleParseError, TypeError, ValueError):
                return []

        reverse = value.startswith("-")
        if value.startswith(("-", "+")):
            value = value[1:].strip()
        nodes, kind = [], "html"
        while True:
            if ("json" in (content_type or "").lower() and
                    not str(text or "").strip()):
                nodes, kind = [], "json"
                break
            js_rule, remaining = split_js_rule(value)
            if js_rule:
                transformed = await asyncio.to_thread(
                    self.js.evaluate, js_rule,
                    JsContext(source=source, key=key, page=page, result=text,
                              base_url=base_url, variables=self.js_variables,
                              get_elements=js_context_result))
                text = (transformed if isinstance(transformed, str) else
                        json.dumps(transformed, ensure_ascii=False))
                content_type = ("application/json" if isinstance(transformed, (dict, list))
                                or text.lstrip().startswith(("{", "[")) else "text/html")
                value = remaining
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
                root, kind = ((json.loads(text), "json") if "json" in content_type else
                              (BeautifulSoup(text, "html.parser"), "html"))
                nodes = root if isinstance(root, list) else [root]
                break

            if "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
                try:
                    root, kind = json.loads(text), "json"
                except json.JSONDecodeError:
                    root, kind = BeautifulSoup(text, "html.parser"), "html"
            else:
                root, kind = BeautifulSoup(text, "html.parser"), "html"
            selector, suffix = self._field_parts(value)
            nodes = evaluate(root, selector, kind) if selector else (
                root if isinstance(root, list) else [root])
            if not suffix:
                break
            if suffix.startswith("<js>") and "</js>" in suffix:
                end = suffix.find("</js>") + 5
                script, value = suffix[:end], suffix[end:].strip()
            else:
                script, value = suffix, ""
            stage_result = [str(item) for item in nodes] if kind == "html" else nodes
            transformed = await asyncio.to_thread(
                self.js.evaluate, script,
                JsContext(source=source, key=key, page=page, result=stage_result,
                          base_url=base_url, variables=self.js_variables,
                          result_html_nodes=kind == "html"))
            text = (transformed if isinstance(transformed, str) else
                    json.dumps(transformed, ensure_ascii=False))
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
            root, kind = ((json.loads(text), "json") if "json" in content_type else
                          (BeautifulSoup(text, "html.parser"), "html"))
            nodes = root if isinstance(root, list) else [root]
            break
        if reverse:
            nodes.reverse()
        return nodes, kind

    async def search(self, source, keyword: str, page: int = 1) -> list[BookItem]:
        raw = await self._resolve_rule_url(source.searchUrl, source, keyword, page)
        req = parse_search_url(raw, source.bookSourceUrl, keyword, page)
        if not req:
            if not req:
                raise RuntimeError("搜索地址格式暂时无法解析")
        text, ctype, final_url = await self._request(req, source)
        rules = source.ruleSearch or {}
        list_rule = rules.get("bookList", "")
        list_rule, list_put = self._list_rule_parts(list_rule)
        nodes, kind = await self._list_rule_pipeline(
            text, ctype, list_rule, source, final_url, keyword, page)
        books = self._books_from_nodes(
            nodes, kind, rules, final_url, source, keyword, page, list_put)
        return [b for b in books if b.name and b.url and not _is_action_url(b.url)
                and not _is_pagination_item(b.name, b.url)]

    async def explore(self, source, category: ExploreCategory, page: int = 1) -> list[BookItem]:
        # Legado conditional paging, e.g. /list/<,index_{{page}}.html>, must be
        # expanded before generic URL encoding or '<' and '>' become path text.
        category_url = await self._resolve_rule_url(category.url, source, page=page)
        if category_url.strip().startswith(("@js:", "<js>")):
            js_value = await asyncio.to_thread(
                self.js.evaluate, category_url,
                JsContext(source=source, page=page,
                          base_url=source.bookSourceUrl,
                          variables=self.js_variables),
            )
            if isinstance(js_value, dict):
                request = dict(js_value)
                request["url"] = urljoin(source.bookSourceUrl,
                                         str(request.get("url", "")))
                request.setdefault("method", "GET")
            else:
                request = parse_search_url(str(js_value or ""),
                                           source.bookSourceUrl, "", page)
                if not request:
                    raise UnsupportedRule("Explore JS did not return a valid request")
        elif re.search(r"<[^<>]*,[^<>]*>", category_url):
            request = {"url": _resolve_explore_url(
                category_url, source.bookSourceUrl, page), "method": "GET"}
        else:
            request = parse_search_url(category_url, source.bookSourceUrl, "", page)
        if not request:
            request = {"url": _resolve_explore_url(category_url, source.bookSourceUrl, page),
                       "method": "GET"}
        text, ctype, final_url = await self._request(request, source)
        # Legado permits explore fields to inherit the search rules. Many sources
        # only override one or two explore fields instead of duplicating the map.
        rules = dict(source.ruleSearch or {})
        rules.update({key: value for key, value in (source.ruleExplore or {}).items() if value})
        list_rule = rules.get("bookList", "")
        list_rule, list_put = self._list_rule_parts(list_rule)
        nodes, kind = await self._list_rule_pipeline(
            text, ctype, list_rule, source, final_url, "", page)
        books = self._books_from_nodes(
            nodes, kind, rules, final_url, source, "", page, list_put)
        return [book for book in books
                if book.name and book.url and not _is_action_url(book.url)
                and not _is_pagination_item(book.name, book.url)]

    async def _render_templates(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        """Evaluate Legado's inline ``{{ JavaScript }}`` URL expressions."""
        rendered = str(value or "")
        for _ in range(32):
            match = re.search(r"\{\{([\s\S]*?)\}\}", rendered)
            if not match:
                break
            expression = match.group(1).strip()
            result = await asyncio.to_thread(
                self.js.evaluate, f"@js:{expression}",
                JsContext(source=source, key=key, page=page,
                          base_url=source.bookSourceUrl,
                          variables=self.js_variables),
            )
            rendered = rendered[:match.start()] + str(result or "") + rendered[match.end():]
        return rendered

    async def _resolve_rule_url(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        """Execute leading JS stages, then return the following request rule."""
        rendered = await self._render_templates(value, source, key, page)
        for _ in range(16):
            text = rendered.strip()
            if text.startswith("<js>") and "</js>" in text:
                end = text.find("</js>") + 5
                script, remaining = text[:end], text[end:].strip()
                result = await asyncio.to_thread(
                    self.js.evaluate, script,
                    JsContext(source=source, key=key, page=page,
                              base_url=source.bookSourceUrl,
                              variables=self.js_variables),
                )
                rendered = (remaining.replace("@result", str(result or ""))
                            if remaining else str(result or ""))
                if not remaining:
                    break
                continue
            if text.startswith("@js:"):
                result = await asyncio.to_thread(
                    self.js.evaluate, text,
                    JsContext(source=source, key=key, page=page,
                              base_url=source.bookSourceUrl,
                              variables=self.js_variables),
                )
                return await self._render_templates(
                    str(result or ""), source, key, page)
            break
        # AnalyzeUrl executes JS stages embedded after a normal request rule.
        # The preceding text is exposed as `result`; this is commonly used to
        # perform a POST with java.connect() and return its redirect URL.
        marker = re.search(r"(?:^|\n)\s*@js:\s*", rendered, flags=re.I)
        if marker and marker.start() > 0:
            prefix = rendered[:marker.start()].strip()
            script = "@js:" + rendered[marker.end():]
            result = await asyncio.to_thread(
                self.js.evaluate, script,
                JsContext(source=source, key=key, page=page, result=prefix,
                          base_url=source.bookSourceUrl,
                          variables=self.js_variables),
            )
            return await self._render_templates(
                str(result or ""), source, key, page)
        marker_pos = rendered.find("<js>")
        if marker_pos > 0:
            end = rendered.find("</js>", marker_pos)
            if end >= 0:
                prefix = rendered[:marker_pos].strip()
                script = rendered[marker_pos:end + 5]
                suffix = rendered[end + 5:].strip()
                result = await asyncio.to_thread(
                    self.js.evaluate, script,
                    JsContext(source=source, key=key, page=page, result=prefix,
                              base_url=source.bookSourceUrl,
                              variables=self.js_variables),
                )
                value = (suffix.replace("@result", str(result or ""))
                         if suffix else str(result or ""))
                return await self._render_templates(value, source, key, page)
        return await self._render_templates(rendered, source, key, page)

    async def chapters(self, source, book: BookItem) -> list[ChapterItem]:
        init = (source.ruleBookInfo or {}).get("init", "")
        if _is_js(init):
            raise UnsupportedRule("详情初始化规则需要 JavaScript 引擎")
        detail_req = {"url": book.url, "method": "GET"}
        detail_text, detail_type, detail_url = await self._request(detail_req, source)
        toc_rule = source.ruleToc or {}
        toc_url_rule = (source.ruleBookInfo or {}).get("tocUrl", "")
        detail_root = (json.loads(detail_text) if "json" in detail_type and
                       detail_text.lstrip().startswith(("{", "[")) else
                       BeautifulSoup(detail_text, "html.parser"))
        detail_kind = "json" if isinstance(detail_root, (dict, list)) else "html"
        toc_url = self._field_value(detail_root, toc_url_rule, detail_kind,
                                    source, detail_url) if toc_url_rule else detail_url
        if not toc_url:
            toc_url = detail_url
        text, ctype, final_url = await self._request({"url": toc_url, "method": "GET"}, source)
        chapter_list_rule = toc_rule.get("chapterList") or ".chapter-list li||#list li||.listmain dd||main li"
        nodes, kind = select_nodes(text, ctype, chapter_list_rule)
        chapters = [ChapterItem(
            self._field_value(n, toc_rule.get("chapterName") or
                              ("name||title" if kind == "json" else "a@text||.title@text"),
                              kind, source, final_url),
            self._field_value(n, toc_rule.get("chapterUrl") or
                              ("url||href||chapterUrl" if kind == "json" else "a@href"),
                              kind, source, final_url),
        ) for n in nodes]
        return [c for c in chapters
                if c.name and c.url and not _is_pagination_item(c.name, c.url)]

    async def content(self, source, chapter: ChapterItem) -> str:
        text, ctype, final_url = await self._request({"url": chapter.url, "method": "GET"}, source)
        rule = (source.ruleContent or {}).get("content") or ".content@html||#content@html||article@html||main@html"
        root = json.loads(text) if "json" in ctype and text.lstrip().startswith(("{", "[")) else BeautifulSoup(text, "html.parser")
        value = self._field_value(root, rule,
                                  "json" if isinstance(root, (dict, list)) else "html",
                                  source, final_url)
        value = re.sub(r"\s*<br\s*/?>\s*", "\n", value, flags=re.I)
        if "<" in value and ">" in value:
            value = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
        return value.strip()
