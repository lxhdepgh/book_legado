"""常见 Legado 规则的轻量执行器（不执行 JavaScript）。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import html
import hashlib
import io
import json
import re
import struct
import threading
import time
import warnings
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urljoin

import aiohttp
from aiohttp_socks import ProxyConnector
from bs4 import BeautifulSoup, UnicodeDammit, XMLParsedAsHTMLWarning
from multidict import CIMultiDict

from app.services.block_pages import SiteBlockedError, block_reason
from app.services.legado_js import (JsBridgeError, JsContext, LegadoJsRuntime,
                                    split_js_rule)
from app.services.legado_rule_parser import (JavaArrayNull, JavaKeySet,
                                             RuleParseError, evaluate,
                                             is_json_text, parse_json_smart,
                                             parse_markup,
                                             simplify_legado_selector,
                                             split_balanced, usable_non_js_rule)
from app.services.legado_url import encode_url, parse_search_url
from app.services.java_regex import (search as java_regex_search,
                                     sub as java_regex_sub,
                                     translate_replacement)
from app.services.icu_charset import detect_charset
from app.services.request_compat import (encode_form_fields,
                                         encode_url_query_fields, escape_form,
                                         escape_url_query)
from app.services.source_rule import compile_rule_program, strip_put_rules


class UnsupportedRule(RuntimeError):
    pass


class RequestConnectionError(RuntimeError):
    """The host never answered, so the rules were not even consulted.

    Callers that sweep hundreds of sources need to tell a dead host apart from
    a broken rule: the first one says nothing about the source definition.
    """


class RequestTimeoutError(RequestConnectionError):
    """The host did not finish the exchange before its time budget ran out."""


def _trim_ascii_space(value: str) -> str:
    return re.sub(r"^[\x00-\x20]+|[\x00-\x20]+$", "", str(value or ""))


def _format_book_name(value: str) -> str:
    space = r"[ \t\n\x0B\f\r]"
    return _trim_ascii_space(re.sub(
        rf"{space}+作{space}*者.*|{space}+\S+{space}+著", "", str(value or "")))


def _format_book_author(value: str) -> str:
    space = r"[ \t\n\x0B\f\r]"
    return _trim_ascii_space(re.sub(
        rf"^{space}*作{space}*者[:： \t\n\x0B\f\r]+|{space}+著", "", str(value or "")))


def _format_word_count(value: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"-?[0-9]+", text):
        return text
    number = int(text)
    if not -(2 ** 31) <= number < 2 ** 31:
        raise ValueError("word count exceeds Java Int")
    if number <= 0:
        return ""
    if number <= 10000:
        return f"{number}字"
    # Kotlin evaluates this historical expression through Float before
    # DecimalFormat, so halfway values can sit just below their decimal form.
    quotient = struct.unpack("!f", struct.pack("!f", number / 10000))[0]
    rendered = f"{quotient:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}万字"


_RAW_INTRO_PREFIXES = ("<usehtml>", "<md>", "<useweb>")
# Legado's viewers hand the tagged payloads to Android's HTML renderer, which
# never paints the body of a style or script block.  A <useweb> card carries its
# whole stylesheet, so the text conversion has to drop it.
_STYLE_BLOCK_PATTERN = re.compile(
    r"<(style|script)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE)


def _strip_html_tags(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"(?:&nbsp;)+", " ", text)
    text = re.sub(r"&ensp;|&emsp;", " ", text)
    text = re.sub(r"&thinsp;|&zwnj;|&zwj;", "", text)
    text = re.sub(r"</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>", "\n", text)
    text = re.sub(r"<!--[^>]*-->", "", text)
    text = re.sub(r"</?[a-zA-Z]+(?=[ >])[^<>]*>", "", text)
    text = re.sub(r"\s*\n+\s*", "\n　　", text)
    text = re.sub(r"^[\n\s]+", "　　", text)
    return re.sub(r"[\n\s]+$", "", text)


def _format_book_intro(value: str) -> str:
    text = str(value or "")
    # BookInfo.analyzeBookInfo: a raw intro tagged for special rendering is
    # stored verbatim, bypassing HtmlFormatter entirely.
    trimmed = text.lstrip()
    if trimmed.startswith(_RAW_INTRO_PREFIXES):
        return trimmed
    return _strip_html_tags(text)


def html_to_text(value: str) -> str:
    """Render markup as readable text the way the viewers' HTML path does."""
    return _strip_html_tags(_STYLE_BLOCK_PATTERN.sub("", str(value or "")))


_INTRO_RENDERERS = (("<usehtml>", "html"), ("<md>", "md"), ("<useweb>", "web"))


def display_intro(value) -> tuple[str, str]:
    """Split a stored intro into the renderer it asks for and that renderer's input.

    Mirrors the branches BookInfoActivity takes on ``book.getDisplayIntro()``:
    ``<usehtml>`` goes to an HTML text view, ``<md>`` to a markdown engine and
    ``<useweb>`` to a WebView, each unwrapped by substring(prefix, lastIndexOf("<")).
    The kind is one of ``text``/``html``/``md``/``web``; only ``text`` content is
    already formatted for reading, the others are raw payloads.
    """
    text = str(value or "").lstrip()
    for marker, kind in _INTRO_RENDERERS:
        if not text.startswith(marker):
            continue
        end = text.rfind("<")
        if end < len(marker):
            # A tagged intro without its closing tag cannot be unwrapped.
            return "text", text
        return kind, text[len(marker):end]
    return "text", _format_book_intro(text)


def _book_type(source) -> int:
    """Map legacy BookSourceType to fixed bitwise BookType."""
    return {0: 8, 1: 32, 2: 64, 3: 8 | 128}.get(
        int(getattr(source, "bookSourceType", 0) or 0), 8)


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
    latest_chapter_title: str = ""
    word_count: str = ""
    download_urls: list[str] | None = None
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
    origins: list[str] = field(default_factory=list)

    def add_origin(self, origin: str) -> None:
        value = str(origin or "")
        if value and value not in self.origins:
            self.origins.append(value)

    def origin_set(self) -> list[str]:
        values = list(self.origins)
        if self.origin and self.origin not in values:
            values.insert(0, self.origin)
        return values


def merge_search_items(existing: list[BookItem], new_items: list[BookItem],
                       search_key: str, precision: bool = False) -> list[BookItem]:
    """Port SearchModel.mergeItems, including origin aggregation and ordering."""
    equal, tags, contains, other = [], [], [], []

    def category(book: BookItem):
        if book.name == search_key or book.author == search_key:
            return equal
        # 分类名命中关键词也算相关：搜索词经常是"玄幻"这类题材而不是书名。
        if search_key and search_key in str(book.kind or ""):
            return tags
        if search_key in book.name or search_key in book.author:
            return contains
        return other

    for book in existing:
        book.origins = book.origin_set()
        category(book).append(book)
    for book in new_items:
        target = category(book)
        if target is other and precision:
            continue
        duplicate = next((item for item in target
                          if item.name == book.name and item.author == book.author), None)
        if duplicate is not None:
            duplicate.add_origin(book.origin)
        else:
            book.origins = book.origin_set()
            target.append(book)
    equal.sort(key=lambda item: len(item.origin_set()), reverse=True)
    tags.sort(key=lambda item: len(item.origin_set()), reverse=True)
    contains.sort(key=lambda item: len(item.origin_set()), reverse=True)
    return equal + tags + contains + ([] if precision else other)


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
    start_fragment_id: str | None = None
    end_fragment_id: str | None = None
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
    return (value.lower().startswith(("@js:", "@webjs:")) or
            value.startswith("<js") or "java." in value)


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
    selector = str(rule or "").strip()
    if selector.lower().startswith("@json:"):
        selector = selector.split(":", 1)[1].strip()
    if not selector:
        return data if isinstance(data, list) else [data]
    values = evaluate(data, selector, "json")
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
            data = parse_json_smart(text)
            values = evaluate(data, rule, "json") if rule else (data if isinstance(data, list) else [data])
            if values:
                return values, "json"
        except (json.JSONDecodeError, TypeError):
            pass
    soup = parse_markup(text, content_type)
    nodes = evaluate(soup, rule, "html", list_context=True)
    if nodes:
        return nodes, "html"
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


def _java_replacement(value: str) -> str:
    """Translate Java/Rhino ``$1`` capture references for Python ``re``."""
    return translate_replacement(value)


def _legado_value_string(value) -> str:
    """Render JSONPath values like Jayway/json-smart and Java toString()."""
    if value is None:
        return ""
    if isinstance(value, JavaArrayNull):
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, ET.Element):
        return ET.tostring(value, encoding="unicode")
    if isinstance(value, JavaKeySet):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{key}={_java_map_child_string(item)}"
            for key, item in value.items()) + "}"
    if isinstance(value, list):
        return _json_smart_array_string(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _json_smart_array_string(value) -> str:
    def render(item):
        if item is None: return "null"
        if isinstance(item, bool): return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, dict):
            return "{" + ",".join(
                json.dumps(str(key), ensure_ascii=False) + ":" + render(child)
                for key, child in item.items()) + "}"
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(render(child) for child in item) + "]"
        return str(item)
    return "[" + ",".join(render(item) for item in value) + "]"


def _java_map_child_string(value) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, dict): return _legado_value_string(value)
    if isinstance(value, list): return _json_smart_array_string(value)
    return str(value)


def _replace_jsonpath_embeds(template: str, node) -> str:
    """Replace balanced ``{$...}`` expressions as Legado RuleAnalyzer does."""
    result, cursor = [], 0
    while True:
        start = template.find("{$.", cursor)
        while start > 0 and template[start - 1] == "{":
            start = template.find("{$.", start + 2)
        if start < 0:
            result.append(template[cursor:])
            break
        result.append(template[cursor:start])
        quote, escaped, depth, end = "", False, 1, -1
        for index in range(start + 1, len(template)):
            char = template[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char in "[({":
                depth += 1
            elif char in "]) }".replace(" ", ""):
                depth -= 1
                if char == "}" and depth == 0:
                    end = index
                    break
        if end < 0:
            result.append(template[start:])
            break
        values = _json_values(node, template[start + 1:end].strip())
        result.append("" if not values else _legado_value_string(values[0]))
        cursor = end + 1
    return "".join(result)


def _split_put_rules(rule: str) -> tuple[str, str]:
    return strip_put_rules(rule)


def _capture_value(node, rule: str) -> str | None:
    """Render Regex-mode records in fields such as ``$3`` or ``url/$1``."""
    rule_text = str(rule)
    capture_part, separator, suffix = rule_text.partition("##")
    if not re.search(r"\$\d{1,2}", capture_part):
        return None
    if not isinstance(node, list):
        # SourceRule.makeUpRule only casts to List<String?>; on another result
        # type capture tokens remain literal instead of being consumed.
        return rule_text
    values = ["" if value is None else str(value) for value in node]
    rendered = re.sub(
        r"\$(\d{1,2})",
        lambda match: (match.group(0) if int(match.group(1)) == 0 else
                       values[int(match.group(1))]
                       if int(match.group(1)) < len(values) else ""),
        capture_part,
    )
    return rendered + (separator + suffix if separator else "")


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
        targets = evaluate(node, selector_rule, kind)
        values = []
        for target in targets:
            value = (target.get_text(" ", strip=True) if hasattr(target, "get_text")
                     else _legado_value_string(target))
            if len(pieces) >= 2 and pieces[1]:
                try:
                    value = java_regex_sub(
                        pieces[1], pieces[2] if len(pieces) > 2 else "", value)
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
    pattern = pieces[1]
    replacement = pieces[2] if len(pieces) > 2 else ""
    rendered_replacement = _java_replacement(replacement)
    if len(pieces) > 3:
        try:
            match = java_regex_search(pattern, str(value))
            if match is None:
                return ""
            return java_regex_sub(pattern, replacement, match.group(0), count=1)
        except re.error:
            # AnalyzeRule returns the replacement when a replace-first pattern
            # itself is invalid.
            return replacement
    try:
        return java_regex_sub(pattern, replacement, str(value))
    except re.error:
        return str(value).replace(pattern, replacement)


def extract_value(node, rule: str, kind: str, base_url: str = "",
                  unescape_html: bool = True) -> str:
    decode = html.unescape if unescape_html else (lambda value: value)
    if not rule:
        return ""
    if "{baseUrl}" in rule:
        return decode(rule.replace("{baseUrl}", base_url)).strip()
    captured = _capture_value(node, rule)
    if captured is not None:
        captured = decode(captured).strip()
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
        value = decode(value).strip()
        return urljoin(base_url, value) if base_url else value

    if kind == "json" and ("{{" in rule or "{$" in rule):
        rule = _replace_jsonpath_embeds(rule, node)
        def replace_field(match):
            expression = (match.group(1) or "").strip()
            values = _json_values(node, expression)
            return "" if not values else _legado_value_string(values[0])
        value = re.sub(r"\{\{([^{}]+)\}\}", replace_field, rule)
        value = decode(value)
        return urljoin(base_url, value) if base_url else value

    candidates = _rule_candidates(rule) or [rule]
    value = ""
    for candidate in candidates:
        value = _extract_single_value(node, candidate, kind,
                                      first_only=bool(base_url))
        if value:
            break
    value = decode(value).strip()
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
    values = [] if value else evaluate(node, selector_rule, kind)
    if values:
        rendered = [item.get_text(" ", strip=True) if hasattr(item, "get_text")
                    else _legado_value_string(item) for item in values]
        value = rendered[0] if first_only else "\n".join(rendered)
    # 常见 ##正则替换：正文前是提取规则，此处至少完成删除匹配项。
    if "##" in rule:
        pieces = rule.split("##")
        if len(pieces) >= 2 and pieces[1]:
            value = _apply_regex_suffix(value, "##" + "##".join(pieces[1:]))
    return value.strip()


class ReaderEngine:
    _global_rate_states = {}
    _global_rate_lock = threading.Lock()

    def __init__(self, timeout: int = 15, state_dir=None,
                 connect_timeout: float | None = None, background: bool = False):
        self.timeout = timeout
        # None keeps aiohttp's historical behaviour: only the total budget
        # applies.  Sweeps pass a value so a host that blackholes the SYN
        # cannot hold a worker for the whole total budget.
        self.connect_timeout = connect_timeout
        self.state_dir = state_dir
        self.js = LegadoJsRuntime(timeout, state_dir=state_dir)
        self.js.background = background
        self.js_variables = {}
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36")}
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)
        self._source_cookie_jars = {}
        self._sessions = {}
        self.last_response = None

    def _cookie_jar_for_source(self, source=None):
        if source is None:
            return aiohttp.DummyCookieJar()
        if getattr(source, "enabledCookieJar", True) is not True:
            return aiohttp.DummyCookieJar()
        key = str(getattr(source, "bookSourceUrl", "") or id(source))
        jar = self._source_cookie_jars.get(key)
        if jar is None:
            jar = aiohttp.CookieJar(unsafe=True)
            path = self._source_cookie_path(key)
            if path is not None:
                try:
                    jar.load(path)
                except (FileNotFoundError, OSError, ValueError):
                    pass
            self._source_cookie_jars[key] = jar
        return jar

    def _session_for(self, source, normalized_proxy: str, socks_proxy: bool):
        """Reuse one session per cookie jar and proxy, like OkHttp's client.

        One session per request re-resolved DNS and re-dialled TCP/TLS every
        time; the wire headers still carry the same keep-alive defaults, but
        they now describe a socket that actually survives between requests.
        """
        if (source is None or
                getattr(source, "enabledCookieJar", True) is not True):
            jar_key = ""
        else:
            jar_key = str(getattr(source, "bookSourceUrl", "") or id(source))
        key = (jar_key, normalized_proxy)
        session = self._sessions.get(key)
        if session is None or session.closed:
            connector = (ProxyConnector.from_url(normalized_proxy, ssl=False)
                         if socks_proxy else aiohttp.TCPConnector(ssl=False))
            session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=self._cookie_jar_for_source(source),
                auto_decompress=False)
            self._sessions[key] = session
        return session

    async def aclose(self) -> None:
        """Release pooled HTTP sessions and the JS runtime."""
        for session in list(self._sessions.values()):
            if not session.closed:
                await session.close()
        self._sessions.clear()
        self.js.close()

    @staticmethod
    def _merge_cookie_header(stored: str, request_cookie: str | None) -> str:
        values: dict[str, str] = {}
        for declaration in (stored, request_cookie):
            if declaration is None:
                continue
            for pair in str(declaration).split(";"):
                key, separator, value = pair.partition("=")
                if not separator:
                    continue
                key, value = key.strip(), value.strip()
                if key and value:
                    values[key] = value
        return "; ".join(f"{key}={value}" for key, value in values.items())

    def _source_cookie_path(self, source_key: str) -> Path | None:
        if not self.state_dir:
            return None
        return (Path(self.state_dir) / "cookies" /
                (hashlib.sha256(source_key.encode()).hexdigest() + ".jar"))

    def _persist_source_cookie_jar(self, source) -> None:
        if (source is None or
                getattr(source, "enabledCookieJar", True) is not True):
            return
        key = str(getattr(source, "bookSourceUrl", "") or id(source))
        path = self._source_cookie_path(key)
        jar = self._source_cookie_jars.get(key)
        if path is None or jar is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        jar.save(path)

    async def _respect_concurrent_rate(self, source) -> None:
        """Apply Legado ``concurrentRate`` before starting a network call.

        ``N/T`` permits N starts per T milliseconds. A bare number is the
        minimum interval in milliseconds between request starts.
        """
        raw = str(getattr(source, "concurrentRate", "") or "").strip()
        if not raw:
            return
        source_key = str(getattr(source, "bookSourceUrl", "") or id(source))
        try:
            if "/" in raw:
                count_text, window_text = raw.split("/", 1)
                # Fixed AnalyzeUrl 3.23.110211 initializes frequency to 1,
                # then rejects only when `frequency > count`. Consequently
                # N/T observably permits the initial start plus N more starts.
                count = max(1, int(count_text)) + 1
                window = max(0, int(window_text)) / 1000.0
            else:
                count, window = 1, max(0, int(raw)) / 1000.0
        except ValueError:
            return
        with self._global_rate_lock:
            state = self._global_rate_states.setdefault(
                source_key, {"starts": deque(), "last": 0.0})
            now = time.monotonic()
            if "/" not in raw:
                target = state["last"] + window
                reserved = max(now, target)
                state["last"] = reserved
            else:
                starts = state["starts"]
                while starts and now - starts[0] >= window:
                    starts.popleft()
                reserved = (starts[-count] + window
                            if len(starts) >= count else now)
                starts.append(reserved)
        while True:
            remaining = reserved - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _acquire_concurrent_rate(self, source):
        """Acquire the fixed AnalyzeUrl fetch record for a real request."""
        if source is None:
            return None
        raw = str(getattr(source, "concurrentRate", "") or "").strip()
        if not raw or raw == "0":
            return None
        if "/" in raw:
            await self._respect_concurrent_rate(source)
            return None
        try:
            interval = max(0, int(raw)) / 1000.0
        except ValueError:
            return None
        source_key = str(getattr(source, "bookSourceUrl", "") or id(source))
        while True:
            with self._global_rate_lock:
                state = self._global_rate_states.get(source_key)
                if state is None:
                    self._global_rate_states[source_key] = {
                        "active": 1, "time": time.monotonic()}
                    return source_key
                state.setdefault("active", 0)
                state.setdefault("time", state.get("last", time.monotonic()))
                now = time.monotonic()
                if state["active"] > 0:
                    # Fixed fetchStart waits the complete configured interval
                    # whenever another request still owns the record.
                    wait_time = interval
                else:
                    wait_time = max(0.0, state["time"] + interval - now)
                    if wait_time <= 0:
                        state["time"] = now
                        state["active"] = 1
                        return source_key
            await asyncio.sleep(wait_time if wait_time > 0 else 0)

    def _release_concurrent_rate(self, token) -> None:
        if token is None:
            return
        with self._global_rate_lock:
            state = self._global_rate_states.get(token)
            if state is not None:
                state["active"] = max(0, int(state.get("active", 0)) - 1)

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

    @staticmethod
    def _normalize_proxy(proxy: object) -> str:
        """Parse the fixed getProxyClient proxy grammar.

        Historical authenticated values use ``scheme://host:port@user@pass``
        rather than the conventional user-info position. aiohttp expects the
        latter, so only the transport representation is rearranged.
        """
        value = str(proxy or "").strip()
        match = re.search(
            r"(http|socks4|socks5)://(.*):(\d{2,5})(@.*@.*)?", value)
        if not match:
            raise ValueError(f"invalid Legado proxy: {value}")
        scheme, host, port, credentials = match.groups()
        auth = ""
        if credentials:
            fields = credentials.split("@")
            username, password = fields[1], fields[2]
            auth = (quote(username, safe="") + ":" +
                    quote(password, safe="") + "@")
        transport_scheme = "http" if scheme == "http" else scheme
        return f"{transport_scheme}://{auth}{host}:{port}"

    async def _resolved_source_headers(self, source, base_url="") -> dict:
        # AnalyzeUrl initializes with source.getHeaderMap(true), not a direct
        # parse of source.header.  This also evaluates dynamic declarations,
        # supplies the default UA and overlays cached login headers.
        value = await self.js.evaluate_async(
            "@js:source.getHeaderMap(true)",
            JsContext(source=source, base_url=str(
                getattr(source, "bookSourceUrl", "") or base_url or ""),
                      variables=self.js_variables))
        if isinstance(value, dict):
            return {str(key): str(item) for key, item in value.items()}
        return {}

    async def _request(self, request: dict, source=None) -> tuple[str, str, str]:
        request = await self._apply_request_option_js(request, source)
        rate_token = await self._acquire_concurrent_rate(source)
        try:
            response = await self._request_transport(request, source)
        finally:
            self._release_concurrent_rate(rate_token)
        # A WAF/anti-bot page or a local security notice is not content: every
        # rule reading it silently yields an empty result, which is how a blocked
        # source ends up looking like a source with nothing to show.
        reason = block_reason(
            response[0], (self.last_response or {}).get("status"))
        if reason:
            raise SiteBlockedError(reason)
        return response

    async def _request_transport(self, request: dict, source=None) -> tuple[str, str, str]:
        request_url = str(request.get("url") or "")
        if request_url.startswith("data:"):
            header, _, payload = request_url.partition(",")
            try:
                raw = (base64.b64decode(payload) if ";base64" in header.lower()
                       else unquote_to_bytes(payload))
            except (ValueError, binascii.Error):
                raw = b""
            charset = str(request.get("charset") or "utf-8")
            text = (raw.hex() if request.get("type") is not None else
                    raw.decode(charset, errors="replace"))
            content_type = header[5:].split(";", 1)[0] or "text/plain"
            self.last_response = {
                "body": text, "bodyBytes": list(raw),
                "headers": {"content-type": content_type},
                "headersMulti": {"content-type": [content_type]},
                "status": 200, "message": "OK", "protocol": "http/1.1",
                "responseKind": "str", "url": request_url}
            return text, content_type, request_url
        timeout = aiohttp.ClientTimeout(total=self.timeout,
                                        connect=self.connect_timeout)
        retry_count = self._request_retry_count(request)
        attempts = retry_count + 1
        attempt = 0
        stale_retried = False
        while attempt < attempts:
            attempt += 1
            try:
                headers = dict(self.headers)
                proxy = None
                if source is not None:
                    source_headers = await self._resolved_source_headers(
                        source, str(request.get("url") or ""))
                    for name in list(source_headers):
                        if name == "proxy":
                            proxy = proxy or source_headers.pop(name)
                    headers.update(source_headers)
                normalized_proxy = self._normalize_proxy(proxy) if proxy else ""
                socks_proxy = normalized_proxy.startswith(("socks4://", "socks5://"))
                session = self._session_for(source, normalized_proxy, socks_proxy)
                request_headers = request.get("headers") or {}
                if isinstance(request_headers, str):
                    try: request_headers = json.loads(request_headers)
                    except json.JSONDecodeError: request_headers = {}
                headers.update(request_headers if isinstance(request_headers, dict) else {})
                method = str(request.get("method", "GET")).upper()
                if method == "POST" and not any(
                        str(name).lower() == "content-type" for name in headers):
                    body = str(request.get("body") or "").lstrip()
                    headers["Content-Type"] = self._default_post_content_type(body)
                # Fixed HttpHelper's application interceptor removes the UA
                # sentinel and then appends these three headers on every call.
                for name in list(headers):
                    if name.lower() == "user-agent" and headers[name] == "null":
                        headers.pop(name)
                request_url = request["url"]
                shared_cookie = self.js.export_cookie_header(request_url)
                merged_cookie = self._merge_cookie_header(
                    shared_cookie, headers.get("Cookie"))
                if merged_cookie:
                    headers["Cookie"] = merged_cookie
                wire_headers = CIMultiDict(headers)
                wire_headers.add("Keep-Alive", "300")
                wire_headers.add("Connection", "Keep-Alive")
                wire_headers.add("Cache-Control", "no-cache")
                transparent_gzip = ("Accept-Encoding" not in wire_headers and
                                    "Range" not in wire_headers)
                if transparent_gzip:
                    wire_headers.add("Accept-Encoding", "gzip")
                cookie_enabled = (source is not None and
                                  getattr(source, "enabledCookieJar", True) is True)
                if str(request.get("charset") or "").lower() == "escape":
                    request_url = escape_url_query(request_url)
                else:
                    request_url = encode_url_query_fields(
                        request_url, str(request.get("charset") or "utf-8"))
                kwargs = {"timeout": timeout, "ssl": False, "allow_redirects": True}
                # aiohttp natively handles HTTP proxying, including CONNECT
                # tunnels for HTTPS. SOCKS must be installed as the session
                # connector and must not also be passed as request.proxy.
                if normalized_proxy and not socks_proxy:
                    kwargs["proxy"] = normalized_proxy
                if method == "POST":
                    body = request.get("body") or ""
                    kwargs["data"] = self._encode_request_body(
                        body, request.get("charset") or "utf-8",
                        headers.get("Content-Type", ""))
                async with session.request(
                        method, encode_url(request_url),
                        headers=wire_headers, **kwargs) as resp:
                    raw = await resp.read()
                    response_headers = {
                        str(k).lower(): str(v) for k, v in resp.headers.items()}
                    response_headers_multi = {
                        str(name).lower(): [str(item) for item in (
                            resp.headers.getall(name, [])
                            if hasattr(resp.headers, "getall") else
                            [resp.headers[name]])]
                        for name in dict(resp.headers.items())
                    }
                    if (transparent_gzip and
                            response_headers.get("content-encoding", "").lower() == "gzip"):
                        raw = gzip.decompress(raw)
                        response_headers.pop("content-encoding", None)
                        response_headers.pop("content-length", None)
                        response_headers_multi.pop("content-encoding", None)
                        response_headers_multi.pop("content-length", None)
                    if cookie_enabled:
                        self.js.save_response_cookies(str(resp.url), resp.headers)
                    self._persist_source_cookie_jar(source)
                    if request.get("type") is not None:
                        text = raw.hex()
                    else:
                        decoded_raw = self._uncompress_response_body(
                            raw, resp.headers.get("Content-Type", ""))
                        text = self._decode_response_body(decoded_raw, resp.charset)
                        text = self._normalize_response_text(
                            text, resp.headers.get("Content-Type", ""))
                    self.last_response = {
                        "body": text,
                        "bodyBytes": list(raw),
                        "headers": response_headers,
                        "headersMulti": response_headers_multi,
                        "status": int(resp.status),
                        "message": str(getattr(resp, "reason", "") or ""),
                        "protocol": (f"http/{resp.version.major}.{resp.version.minor}"
                                     if getattr(resp, "version", None) else "http/1.1"),
                        "responseKind": "str",
                        "url": str(resp.url),
                    }
                    if 200 <= int(resp.status) < 300 or attempt >= attempts:
                        return text, resp.content_type or "", str(resp.url)
            except aiohttp.ServerDisconnectedError as exc:
                # OkHttp re-dials and replays the call when a pooled keep-alive
                # connection turns out to be closed already; without the extra
                # attempt a healthy site would look broken once we reuse sockets.
                if stale_retried:
                    raise RequestConnectionError(f"连接失败：{exc}") from exc
                stale_retried = True
                attempts += 1
            except aiohttp.ClientConnectorError as exc:
                raise RequestConnectionError(f"连接失败：{exc}") from exc
            except asyncio.TimeoutError as exc:
                # aiohttp 的超时异常字符串通常是空的，照抄只会得到“连接超时：”。
                limit = timeout.total or timeout.connect
                detail = str(exc) or (f"{limit:g} 秒无响应" if limit else "响应超时")
                raise RequestTimeoutError(f"连接超时：{detail}") from exc
        raise RuntimeError("request loop ended without a response")

    @staticmethod
    def _request_retry_count(request: dict) -> int:
        """Match AnalyzeUrl.UrlOption: absent retry means no extra attempt."""
        try:
            return int(request.get("retry") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _decode_response_body(raw: bytes, response_charset: str | None = None) -> str:
        """Decode like OkHttp ResponseBody.text(); URL-option charset is unrelated."""
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        charset = response_charset
        if not charset:
            charset = ReaderEngine._html_declared_encoding(raw)
        if not charset:
            charset = (detect_charset(raw) or
                       UnicodeDammit(raw, is_html=True).original_encoding or "utf-8")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._:-]*", str(charset)):
            raise LookupError(f"illegal charset name: {charset}")
        return raw.decode(charset, errors="replace")

    @staticmethod
    def _html_declared_encoding(raw: bytes) -> str | None:
        """Port EncodingDetect.getHtmlEncode's Jsoup meta-tag pass."""
        try:
            with warnings.catch_warnings():
                # Jsoup parses every response as HTML, XML prologue included, so
                # switching to an XML parser here would change extraction.
                warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
                document = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
            for meta in document.find_all("meta"):
                charset = str(meta.get("charset") or "")
                if charset:
                    return charset
                content = str(meta.get("content") or "")
                http_equiv = str(meta.get("http-equiv") or "")
                if http_equiv.lower() != "content-type":
                    continue
                lowered = content.lower()
                if "charset" in lowered:
                    # Keep the historical index arithmetic, including its odd
                    # result for whitespace between `charset` and `=`.
                    charset = content[lowered.index("charset") + len("charset="):]
                else:
                    charset = content[lowered.find(";") + 1:]
                if charset:
                    return charset
        except Exception:
            return None
        return None

    @staticmethod
    def _default_post_content_type(body: str) -> str:
        if str(body or "").lstrip().startswith(("{", "[", "<")):
            return "application/json; charset=UTF-8"
        return "application/x-www-form-urlencoded; charset=UTF-8"

    @staticmethod
    def _encode_request_body(body, charset="utf-8", content_type="") -> bytes:
        text = str(body or "")
        if not text.lstrip().startswith(("{", "[", "<")):
            if str(charset or "").lower() == "escape":
                return escape_form(text).encode("ascii", errors="replace")
            return encode_form_fields(
                text, str(charset or "utf-8")).encode("ascii")
        match = re.search(
            r"charset\s*=\s*[\"']?([^;\s\"']+)",
            str(content_type or ""), re.I)
        body_charset = match.group(1) if match else "utf-8"
        return text.encode(body_charset, errors="replace")

    @staticmethod
    def _uncompress_response_body(raw: bytes, content_type: str) -> bytes:
        if str(content_type or "").split(";", 1)[0].strip().lower() != "application/zip":
            return raw
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                first = next((item for item in archive.infolist() if not item.is_dir()), None)
                return archive.read(first) if first is not None else b""
        except (OSError, ValueError, zipfile.BadZipFile):
            return raw

    @staticmethod
    def _normalize_response_text(text: str, content_type: str) -> str:
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if (media_type in {"application/xml", "text/xml", "application/xhtml+xml"}
                and not text.lstrip().lower().startswith("<?xml")):
            return '<?xml version="1.0"?>' + text
        return text

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
        state = await self.js.evaluate_url_option_async(script, context)
        if state.get("url"):
            prepared["url"] = str(state["url"])
        if isinstance(state.get("headers"), dict):
            prepared["headers"] = {str(k): str(v) for k, v in state["headers"].items()}
        prepared["_jsApplied"] = True
        return prepared

    @staticmethod
    def _field_parts(rule: str, kind: str = "") -> tuple[str, str]:
        value = rule or ""
        return compile_rule_program(
            value, is_json=kind == "json").selector_and_suffix()

    @staticmethod
    def _rhino_result_string(value) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, dict):
            return "[object Object]"
        if isinstance(value, (list, tuple)):
            return ",".join(ReaderEngine._rhino_result_string(item)
                            if isinstance(item, (dict, list, tuple)) else
                            "" if item is None else str(item)
                            for item in value)
        return str(value)

    @classmethod
    def _payload_for_compiled_stage(cls, transformed, remaining: str,
                                    analyzer_kind: str):
        program = compile_rule_program(
            remaining, is_json=analyzer_kind == "json")
        mode = program.stages[0].mode if program.stages else "default"
        if mode in {"js", "webjs"}:
            return transformed, analyzer_kind, transformed
        if mode == "json":
            if isinstance(transformed, (dict, list)):
                return transformed, "json", transformed
            text = cls._rhino_result_string(transformed)
            return parse_json_smart(text), "json", transformed
        text = cls._rhino_result_string(transformed)
        return parse_markup(text), "html", text

    def _field_value(self, node, rule: str, kind: str, source, base_url: str,
                     key: str = "", page: int = 1, book=None, chapter=None,
                     raw_result=None, title: str = "",
                     next_chapter_url: str = "",
                     unescape_html: bool = True, rule_name: str = "") -> str:
        rule, put_rule = _split_put_rules(rule or "")
        if put_rule:
            self._put_rule_values(node, put_rule, kind, source, base_url,
                                  key, page, book, chapter, raw_result)
        if "{{" in rule and "{{@@" not in rule:
            def render_expression(match):
                expression = match.group(1).strip()
                lowered_expression = expression.lower()
                if expression.startswith("@@"):
                    return extract_value(
                        node, expression[2:], kind, base_url, unescape_html)
                if (expression.startswith("@") and not
                        lowered_expression.startswith((
                            "@js:", "@json:", "@xpath:", "@css:"))):
                    return extract_value(
                        node, expression[1:], kind, base_url, unescape_html)
                if (expression.startswith(("$", "//")) or
                        lowered_expression.startswith(("@json:", "@xpath:", "@css:"))):
                    return extract_value(node, expression, kind, base_url,
                                         unescape_html)
                context = JsContext(
                    source=source, key=key, page=page,
                    result=raw_result if raw_result is not None else (
                        node if kind == "json" else str(node)),
                    base_url=base_url, variables=self.js_variables,
                    get_string=lambda nested: extract_value(node, nested, kind, base_url),
                    get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                    get_elements=lambda nested: [_legado_value_string(item)
                                                  for item in evaluate(
                                                      node, nested, kind,
                                                      list_context=True)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                    rule_name=rule_name,
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
                        book, chapter, literal, unescape_html=unescape_html)
                    return value.strip()
                pieces = rendered.split("##")
                value = pieces[0]
                if len(pieces) > 1 and pieces[1]:
                    try:
                        value = java_regex_sub(
                            pieces[1], pieces[2] if len(pieces) > 2 else "", value,
                            count=1 if len(pieces) > 3 else 0)
                    except re.error:
                        pass
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
                    transformed_root = parse_markup(transformed_text)
                    transformed_kind = "html"
                return self._field_value(
                    transformed_root, remaining, transformed_kind, source, base_url,
                    key, page, book, chapter, transformed_text, title,
                    next_chapter_url, unescape_html, rule_name)
            context = JsContext(
                source=source, key=key, page=page,
                result=initial_result,
                base_url=base_url, variables=self.js_variables,
                get_string=lambda nested: extract_value(node, nested, kind, base_url),
                get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                get_elements=lambda nested: [_legado_value_string(item)
                                              for item in evaluate(
                                                  node, nested, kind,
                                                  list_context=True)],
                book=book, chapter=chapter, title=title,
                next_chapter_url=next_chapter_url,
                result_html_nodes=(raw_result is None and kind == "html" and
                                   hasattr(node, "select")),
                rule_name=rule_name,
            )
            transformed = self.js.evaluate(script, context)
            if not remaining:
                return "" if transformed is None else str(transformed)
            if remaining.startswith("##"):
                return _apply_regex_suffix("" if transformed is None else str(transformed),
                                           remaining).strip()
            transformed_root, transformed_kind, stage_result = (
                self._payload_for_compiled_stage(transformed, remaining, kind))
            return self._field_value(
                transformed_root, remaining, transformed_kind, source, base_url,
                key, page, book, chapter, stage_result, title,
                next_chapter_url, unescape_html, rule_name,
            )
        get_matches = list(re.finditer(r"@get:\{([^{}]+)\}", rule))
        if get_matches:
            value = re.sub(
                r"@get:\{([^{}]+)\}",
                lambda match: self._get_variable(match.group(1), book, chapter),
                rule)
            stripped_value = value.strip()
            js_positions = [position for position in
                            (stripped_value.find("@js:"), stripped_value.find("<js>"))
                            if position >= 0]
            js_position = min(js_positions) if js_positions else -1
            if js_position == 0:
                # @get is preprocessing inside a JavaScript stage, not the
                # terminal value of the field rule. Continue into the normal
                # JS pipeline after interpolation.
                rule = stripped_value
            elif js_position > 0:
                # splitSourceRule passes the completed literal/get stage as
                # ``result`` to the following JavaScript SourceRule.
                return self._field_value(
                    node, stripped_value[js_position:], kind, source, base_url,
                    key, page, book, chapter,
                    stripped_value[:js_position].strip(), title,
                    next_chapter_url, unescape_html, rule_name)
            else:
                pieces = stripped_value.split("##")
                value = pieces[0]
                if len(pieces) >= 2 and pieces[1]:
                    try:
                        value = java_regex_sub(
                            pieces[1], pieces[2] if len(pieces) > 2 else "", value)
                    except re.error:
                        pass
                return value.strip()

        selector, post_js = self._field_parts(rule, kind)
        value = ((raw_result if raw_result is not None else node)
                 if not selector and post_js else extract_value(
                     node, selector, kind, base_url, unescape_html))
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
                    get_elements=lambda nested: [_legado_value_string(item)
                                                  for item in evaluate(
                                                      node, nested, kind,
                                                      list_context=True)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(not selector and raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                    rule_name=rule_name,
                )
                transformed = self.js.evaluate(script, context)
                value = "" if transformed is None else transformed
                continue
            if remaining.lower().startswith("@webjs:"):
                script, regex_suffix = remaining, ""
                if "##" in remaining:
                    script, regex_body = script.split("##", 1)
                    regex_suffix = "##" + regex_body
                context = JsContext(
                    source=source, key=key, page=page, result=value,
                    base_url=base_url, variables=self.js_variables,
                    get_string=lambda nested: extract_value(node, nested, kind, base_url),
                    get_string_list=lambda nested: extract_values(node, nested, kind, base_url),
                    get_elements=lambda nested: [_legado_value_string(item)
                                                  for item in evaluate(
                                                      node, nested, kind,
                                                      list_context=True)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    src=(raw_result if isinstance(raw_result, str) else
                         (str(node) if kind == "html" else "")),
                    rule_name=rule_name,
                )
                value = self.js.evaluate(script, context)
                if regex_suffix:
                    value = _apply_regex_suffix(
                        "" if value is None else str(value), regex_suffix)
                remaining = ""
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
                    get_elements=lambda nested: [_legado_value_string(item)
                                                  for item in evaluate(
                                                      node, nested, kind,
                                                      list_context=True)],
                    book=book, chapter=chapter, title=title,
                    next_chapter_url=next_chapter_url,
                    result_html_nodes=(not selector and raw_result is None and kind == "html" and
                                       hasattr(node, "select")),
                    rule_name=rule_name,
                )
                value = self.js.evaluate(script, context)
                if regex_suffix:
                    value = _apply_regex_suffix(
                        "" if value is None else str(value), regex_suffix)
                remaining = ""
                continue
            # A selector after JavaScript analyzes the transformed payload.
            transformed_root, transformed_kind, stage_result = (
                self._payload_for_compiled_stage(value, remaining, kind))
            value = self._field_value(
                transformed_root, remaining, transformed_kind, source, base_url,
                key, page, book, chapter, stage_result, title,
                next_chapter_url, unescape_html)
            remaining = ""
        if base_url and isinstance(value, str) and value.startswith(("/", "./", "../", "//")):
            value = urljoin(base_url, value)
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
            self._put_variable(
                name, self._field_value(
                    node, nested_rule, kind, source, base_url, key, page,
                    book, chapter, raw_result), book, chapter)

    @staticmethod
    def _variable_owner(book, chapter):
        """AnalyzeRule scope order: chapter, then book (the rule data)."""
        for owner in (chapter, book):
            variables = getattr(owner, "variables", None) if owner is not None else None
            if isinstance(variables, dict):
                return variables
        return None

    def _put_variable(self, name: str, value: str, book=None,
                      chapter=None) -> str:
        # AnalyzeRule.put: chapter ?: book ?: ruleData ?: source preferences.
        target = self._variable_owner(book, chapter)
        if target is None:
            self.js_variables[name] = value
        else:
            target[name] = value
        return value

    def _get_variable(self, name: str, book=None, chapter=None) -> str:
        # AnalyzeRule.get: bookName/title special cases, then the
        # chapter ?: book ?: ruleData ?: source-preferences lookup chain.
        # Values found on an entity never fall through even when empty.
        if name == "bookName" and book is not None:
            return str(getattr(book, "name", "") or "")
        if name == "title" and chapter is not None:
            return str(getattr(chapter, "name", "") or "")
        for owner in (chapter, book):
            variables = getattr(owner, "variables", None) if owner is not None else None
            if isinstance(variables, dict) and variables.get(name) is not None:
                value = str(variables[name])
                if value != "":
                    return value
        for key in (name, "source:data:" + name):
            if key in self.js_variables and self.js_variables[key] is not None:
                value = str(self.js_variables[key])
                if value != "":
                    return value
        return ""

    def _book_from_node(self, node, kind: str, rules: dict, base_url: str,
                        source=None, key: str = "", page: int = 1,
                        rule_prefix: str = "ruleSearch") -> BookItem:
        def field(name, with_base=False, tolerate=False):
            primary = rules.get(name)
            if not primary:
                return ""
            if tolerate:
                try:
                    return self._field_value(
                        node, primary, kind, source,
                        base_url if with_base else "", key, page,
                        rule_name=f"{rule_prefix}.{name}")
                except (UnsupportedRule, RuleParseError, JsBridgeError):
                    return ""
            return self._field_value(
                node, primary, kind, source,
                base_url if with_base else "", key, page,
                rule_name=f"{rule_prefix}.{name}")

        name = _format_book_name(field("name"))
        author = _format_book_author(field("author"))
        intro = _format_book_intro(field("intro", tolerate=True))
        url = field("bookUrl", with_base=True) or base_url
        cover_url = field("coverUrl", with_base=True, tolerate=True)
        kind_value = field("kind", tolerate=True)
        last_chapter = field("lastChapter", tolerate=True)
        try:
            word_count = _format_word_count(field("wordCount", tolerate=True))
        except (ValueError, OverflowError):
            word_count = ""
        book = BookItem(
            name=name, author=author, intro=intro, url=url,
            cover_url=cover_url, kind=kind_value,
            last_chapter=last_chapter,
            latest_chapter_title=last_chapter, word_count=word_count,
            origin=str(getattr(source, "bookSourceUrl", "") or ""),
            origin_name=str(getattr(source, "bookSourceName", "") or ""),
            type=_book_type(source),
            origin_order=int(getattr(source, "customOrder", 0) or 0),
        )
        book.variables = dict(self.js_variables)
        return book

    def _books_from_nodes(self, nodes: list, kind: str, rules: dict, base_url: str,
                          source=None, key: str = "", page: int = 1,
                          list_put: str = "",
                          rule_prefix: str = "ruleSearch") -> list[BookItem]:
        # 严格规则只命中一个容器时，按字段规则批量提取并按位置组合。
        if kind == "html" and len(nodes) == 1:
            names = extract_values(nodes[0], rules.get("name", ""), kind)
            urls = extract_values(nodes[0], rules.get("bookUrl", ""), kind, base_url)
            if len(names) >= 2 and len(urls) >= 2:
                authors = extract_values(nodes[0], rules.get("author", ""), kind)
                intros = extract_values(nodes[0], rules.get("intro", ""), kind)
                covers = extract_values(nodes[0], rules.get("coverUrl", ""), kind, base_url)
                books = []
                for index in range(min(len(names), len(urls))):
                    books.append(BookItem(
                        name=names[index], url=urls[index],
                        author=authors[index] if index < len(authors) else "",
                        intro=intros[index] if index < len(intros) else "",
                        cover_url=covers[index] if index < len(covers) else "",
                    ))
                return books
        books = []
        for node in nodes:
            if list_put:
                self._put_rule_values(node, list_put, kind, source, base_url,
                                      key, page, raw_result=str(node))
            books.append(self._book_from_node(
                node, kind, rules, base_url, source, key, page,
                rule_prefix))
        return books

    @staticmethod
    def _list_rule_parts(rule: str) -> tuple[str, str]:
        return _split_put_rules(rule)

    async def _list_rule_pipeline(self, text: str, content_type: str, rule: str,
                                  source, base_url: str, key: str = "",
                                  page: int = 1,
                                  rule_name: str = "") -> tuple[list, str]:
        """Run every selector/JS stage in a Legado list rule in order."""
        value = str(rule or "").strip()
        original_text = text
        original_content_type = content_type
        if "json" in (content_type or "").lower() and not text.strip():
            analyzer_kind = "json"
        else:
            try:
                parse_json_smart(text)
                analyzer_kind = "json"
            except (json.JSONDecodeError, TypeError):
                analyzer_kind = "html"
        current_result = text
        pending_root = None
        def js_context_result(rule_text):
            try:
                root = (parse_json_smart(original_text)
                        if "json" in (original_content_type or "").lower()
                        else parse_markup(original_text, original_content_type))
                root_kind = "json" if isinstance(root, (dict, list)) else "html"
                # java.getElement/getElements mirror AnalyzeRule.getElement(s).
                return [_legado_value_string(item) for item in evaluate(
                    root, str(rule_text), root_kind, list_context=True)]
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
                transformed = await self.js.evaluate_async(
                    js_rule,
                    JsContext(source=source, key=key, page=page,
                              result=current_result,
                              base_url=base_url, variables=self.js_variables,
                              get_elements=js_context_result,
                              rule_name=rule_name))
                current_result = transformed
                text = (transformed if isinstance(transformed, str) else
                        json.dumps(transformed, ensure_ascii=False))
                value = remaining
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
                    nodes = [parse_markup(item).find()
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
                    # AnalyzeRule.getElements returns the JS list untouched and
                    # BookList re-runs every field rule on setContent(item), so a
                    # JSON-looking element keeps its string form and is read by
                    # the JSONPath analyzer.
                    nodes, kind = list(transformed), "json"
                    break
                root, kind = ((parse_json_smart(text), "json") if "json" in content_type else
                              (parse_markup(text, content_type), "html"))
                nodes = root if isinstance(root, list) else [root]
                break

            if pending_root is not None:
                root, kind = pending_root
                pending_root = None
            elif "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
                try:
                    root, kind = parse_json_smart(text), "json"
                except json.JSONDecodeError:
                    root, kind = parse_markup(text, content_type), "html"
            else:
                root, kind = parse_markup(text, content_type), "html"
            selector, suffix = self._field_parts(value, kind)
            nodes = evaluate(root, selector, kind, list_context=True) if selector else (
                root if isinstance(root, list) else [root])
            if not suffix:
                break
            if suffix.startswith("<js>") and "</js>" in suffix:
                end = suffix.find("</js>") + 5
                script, value = suffix[:end], suffix[end:].strip()
            else:
                script, value = suffix, ""
            stage_result = [str(item) for item in nodes] if kind == "html" else nodes
            transformed = await self.js.evaluate_async(
                script,
                JsContext(source=source, key=key, page=page, result=stage_result,
                          base_url=base_url, variables=self.js_variables,
                          result_html_nodes=kind == "html",
                          rule_name=rule_name))
            current_result = transformed
            text = (transformed if isinstance(transformed, str) else
                    json.dumps(transformed, ensure_ascii=False))
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
                nodes = [parse_markup(item).find()
                         for item in transformed]
                nodes = [item for item in nodes if item is not None]
                kind = "html"
                break
            if isinstance(transformed, dict) or (
                    isinstance(transformed, list) and
                    all(isinstance(item, dict) for item in transformed)):
                nodes = (transformed if isinstance(transformed, list)
                         else [transformed])
                kind = "json"
                break
            root, kind = ((parse_json_smart(text), "json") if "json" in content_type else
                          (parse_markup(text, content_type), "html"))
            nodes = root if isinstance(root, list) else [root]
            break
        if reverse:
            nodes.reverse()
        return nodes, kind

    async def search(self, source, keyword: str, page: int = 1) -> list[BookItem]:
        raw = await self._resolve_rule_url(source.searchUrl, source, keyword, page)
        req = parse_search_url(raw, source.bookSourceUrl, keyword, page)
        if not req:
            raise RuntimeError("搜索地址格式暂时无法解析")
        text, ctype, final_url = await self._request(req, source)
        pattern = str(getattr(source, "bookUrlPattern", "") or "")
        if pattern and re.fullmatch(pattern, final_url):
            book = await self._detail_book_from_response(
                source, text, ctype, final_url, str(req.get("url") or final_url))
            return [book] if book.name else []
        rules = source.ruleSearch or {}
        list_rule = rules.get("bookList", "")
        list_rule, list_put = self._list_rule_parts(list_rule)
        nodes, kind = await self._list_rule_pipeline(
            text, ctype, list_rule, source, final_url, keyword, page,
            rule_name="ruleSearch.bookList")
        if not nodes and not pattern:
            # Legado guesses that a page whose list rule matched nothing is a
            # detail page ("列表为空,按详情页解析").  The guess can be wrong;
            # Legado then lets the exception fall through to its search caller,
            # which reports no books.  Do the same here so an empty result
            # page never reads as a source failure.
            try:
                book = await self._detail_book_from_response(
                    source, text, ctype, final_url,
                    str(req.get("url") or final_url))
            except Exception:
                return []
            return [book] if book.name else []
        books = self._books_from_nodes(
            nodes, kind, rules, final_url, source, keyword, page, list_put)
        distinct = []
        seen_urls = set()
        for book in books:
            if book.name and book.url not in seen_urls:
                distinct.append(book)
                seen_urls.add(book.url)
        return distinct

    async def _detail_book_from_response(self, source, text: str,
                                         content_type: str, final_url: str,
                                         book_url: str) -> BookItem:
        book = BookItem(
            "", url=book_url, origin=str(getattr(source, "bookSourceUrl", "") or ""),
            origin_name=str(getattr(source, "bookSourceName", "") or ""),
            type=_book_type(source),
            origin_order=int(getattr(source, "customOrder", 0) or 0),
            info_html=text,
        )
        if hasattr(self, "_initialize_detail") and hasattr(self, "_apply_book_info"):
            root, kind, raw_text = await self._initialize_detail(
                source, book, text, content_type, final_url)
            self._apply_book_info(
                source, book, root, kind, raw_text, book_url, final_url, text,
                can_rename=False)
            return book
        root = (parse_json_smart(text) if "json" in content_type.lower()
                else parse_markup(text, content_type))
        kind = "json" if isinstance(root, (dict, list)) else "html"
        parsed = self._book_from_node(
            root, kind, getattr(source, "ruleBookInfo", {}) or {}, final_url,
            source, rule_prefix="ruleBookInfo")
        parsed.url, parsed.info_html = book_url, text
        return parsed

    async def explore(self, source, category: ExploreCategory, page: int = 1) -> list[BookItem]:
        # Legado conditional paging, e.g. /list/<,index_{{page}}.html>, must be
        # expanded before generic URL encoding or '<' and '>' become path text.
        category_url = await self._resolve_rule_url(category.url, source, page=page)
        if category_url.strip().startswith(("@js:", "<js>")):
            js_value = await self.js.evaluate_async(
                category_url,
                JsContext(source=source, host_kind="url", page=page,
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
        return await self.explore_document(
            source, text, ctype, final_url, page)

    async def explore_document(self, source, text: str, ctype: str,
                               final_url: str, page: int = 1) -> list[BookItem]:
        """Parse an already downloaded explore page with the full rule engine."""
        # Legado permits explore fields to inherit the search rules. Many sources
        # only override one or two explore fields instead of duplicating the map.
        rules = dict(source.ruleSearch or {})
        rules.update({key: value for key, value in (source.ruleExplore or {}).items() if value})
        list_rule = rules.get("bookList", "")
        list_rule, list_put = self._list_rule_parts(list_rule)
        list_scope = ("ruleExplore"
                      if (source.ruleExplore or {}).get("bookList")
                      else "ruleSearch")
        nodes, kind = await self._list_rule_pipeline(
            text, ctype, list_rule, source, final_url, "", page,
            rule_name=f"{list_scope}.bookList")
        if not nodes and not str(getattr(source, "bookUrlPattern", "") or ""):
            # Same empty-list guess as the search path, with the same
            # fallback: an unreadable guess means this category has no books.
            try:
                book = await self._detail_book_from_response(
                    source, text, ctype, final_url, final_url)
            except Exception:
                return []
            return [book] if book.name else []
        books = self._books_from_nodes(
            nodes, kind, rules, final_url, source, "", page, list_put,
            rule_prefix=list_scope)
        distinct = []
        seen_urls = set()
        for book in books:
            if book.name and book.url not in seen_urls:
                distinct.append(book)
                seen_urls.add(book.url)
        return distinct

    async def _render_templates(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        """Evaluate Legado's inline ``{{ JavaScript }}`` URL expressions."""
        rendered = str(value or "")
        seen = set()
        while rendered not in seen:
            seen.add(rendered)
            match = re.search(r"\{\{([\s\S]*?)\}\}", rendered)
            if not match:
                break
            expression = match.group(1).strip()
            result = await self.js.evaluate_async(
                f"@js:{expression}",
                JsContext(source=source, host_kind="url", key=key, page=page,
                          base_url=source.bookSourceUrl,
                          variables=self.js_variables),
            )
            rendered = (rendered[:match.start()] +
                        self._template_result_text(result) +
                        rendered[match.end():])
        return rendered

    @staticmethod
    def _template_result_text(result) -> str:
        """Keep numeric zero in URL templates without changing other falsey values.

        The previous ``str(result or "")`` behavior intentionally rendered
        ``None``, ``False`` and empty containers as blank text.  JavaScript
        pagination expressions, however, commonly produce numeric zero on the
        first page.  Handle only that numeric case to avoid broad compatibility
        changes in existing source rules.
        """
        if (isinstance(result, (int, float)) and
                not isinstance(result, bool) and result == 0):
            return "0"
        return str(result or "")

    async def _resolve_rule_url(self, value: str, source, key: str = "",
                                page: int = 1) -> str:
        """Run fixed-version ``AnalyzeUrl.analyzeJs`` stage semantics."""
        program = compile_rule_program(value)
        if not any(stage.mode in {"js", "webjs"} for stage in program.stages):
            return await self._render_templates(value, source, key, page)

        # AnalyzeUrl starts with the complete rule as result.  Every non-JS
        # fragment replaces @result, while every JS fragment receives the
        # preceding value as its `result` binding.
        result = str(value or "")
        for stage in program.stages:
            if stage.mode not in {"js", "webjs"}:
                fragment = stage.raw.strip()
                if fragment:
                    result = fragment.replace("@result", result)
                continue
            evaluated = await self.js.evaluate_async(
                stage.raw,
                JsContext(source=source, host_kind="url", key=key, page=page, result=result,
                          base_url=source.bookSourceUrl,
                          variables=self.js_variables),
            )
            result = str(evaluated)
        return await self._render_templates(result, source, key, page)

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
                       parse_markup(detail_text, detail_type))
        detail_kind = "json" if isinstance(detail_root, (dict, list)) else "html"
        toc_url = self._field_value(detail_root, toc_url_rule, detail_kind,
                                    source, detail_url) if toc_url_rule else detail_url
        if not toc_url:
            toc_url = detail_url
        text, ctype, final_url = await self._request({"url": toc_url, "method": "GET"}, source)
        chapter_list_rule = str(toc_rule.get("chapterList") or "")
        nodes, kind = select_nodes(text, ctype, chapter_list_rule)
        chapters = [ChapterItem(
            self._field_value(n, str(toc_rule.get("chapterName") or ""),
                              kind, source, final_url),
            self._field_value(n, str(toc_rule.get("chapterUrl") or ""),
                              kind, source, final_url),
        ) for n in nodes]
        return [c for c in chapters
                if c.name and c.url and not _is_pagination_item(c.name, c.url)]

    async def content(self, source, chapter: ChapterItem) -> str:
        text, ctype, final_url = await self._request({"url": chapter.url, "method": "GET"}, source)
        rule = str((source.ruleContent or {}).get("content") or "")
        root = (parse_json_smart(text) if "json" in ctype and
                text.lstrip().startswith(("{", "[")) else
                parse_markup(text, ctype))
        value = self._field_value(root, rule,
                                  "json" if isinstance(root, (dict, list)) else "html",
                                  source, final_url)
        value = re.sub(r"\s*<br\s*/?>\s*", "\n", value, flags=re.I)
        if "<" in value and ">" in value:
            value = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
        return value.strip()
