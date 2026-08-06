"""常见 Legado 规则的轻量执行器（不执行 JavaScript）。"""
from __future__ import annotations

import asyncio
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from test_book_sources_async import (encode_url, jsonpath_get, parse_search_url,
                                     simplify_legado_selector, usable_non_js_rule)
from app.services.legado_js import LegadoJsRuntime, JsContext, split_js_rule
from app.services.legado_rule_parser import RuleParseError, evaluate, split_balanced


class UnsupportedRule(RuntimeError):
    pass


@dataclass
class BookItem:
    name: str
    author: str = ""
    intro: str = ""
    url: str = ""
    cover_url: str = ""


@dataclass
class ChapterItem:
    name: str
    url: str


@dataclass
class ExploreCategory:
    title: str
    url: str


def parse_explore_categories(source) -> list[ExploreCategory]:
    """提取 Legado exploreUrl 中的分类标签（JSON 数组或“标题::URL”格式）。"""
    raw = (source.exploreUrl or "").strip()
    if not raw or _is_js(raw):
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
    raise RuntimeError(f"无法解析列表规则：{rule or '未配置'}")


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


def extract_values(node, rule: str, kind: str, base_url: str = "") -> list[str]:
    """从一个列表容器按书源字段规则提取全部值，用于网站把多条记录包进单容器的情况。"""
    if not rule or kind != "html" or _is_js(rule):
        return []
    for candidate in _rule_candidates(rule):
        selector_rule = candidate.split("##", 1)[0]
        try:
            targets = evaluate(node, selector_rule, kind)
        except RuleParseError:
            continue
        values = []
        for target in targets:
            value = (target.get_text(" ", strip=True) if hasattr(target, "get_text") else str(target))
            value = value.strip()
            if base_url and value and value.startswith(("/", "./", "../", "//")):
                value = urljoin(base_url, value)
            if value:
                values.append(value)
        if values:
            return values
    return []


def extract_value(node, rule: str, kind: str, base_url: str = "") -> str:
    if not rule:
        return ""
    if _is_js(rule):
        raise UnsupportedRule("字段规则需要 JavaScript 引擎")
    # JSON 字段拼接模板，如 https://site/book/{{$.id}}
    if kind == "json" and "{{" in rule:
        def replace_field(match):
            values = _json_values(node, match.group(1).strip())
            return "" if not values else str(values[0])
        value = re.sub(r"\{\{([^{}]+)\}\}", replace_field, rule)
        return urljoin(base_url, value) if base_url else value

    candidates = _rule_candidates(rule) or [rule]
    value = ""
    for candidate in candidates:
        value = _extract_single_value(node, candidate, kind)
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
    value = re.sub(r"##.*$", "", value).strip()
    url_hint = any(key in rule.lower() for key in ("url", "href", "src"))
    relative_hint = value.startswith(("/", "./", "../", "//"))
    if base_url and value and (url_hint or relative_hint) and not value.startswith(("data:", "javascript:")):
        value = urljoin(base_url, value)
    return value


def _extract_single_value(node, rule: str, kind: str) -> str:
    selector_rule = rule.split("##", 1)[0]
    value = ""
    try:
        values = evaluate(node, selector_rule, kind)
    except RuleParseError:
        values = []
    if values:
        first = values[0]
        value = first.get_text(" ", strip=True) if hasattr(first, "get_text") else str(first)
    # 常见 ##正则替换：正文前是提取规则，此处至少完成删除匹配项。
    if "##" in rule:
        pieces = rule.split("##")
        if len(pieces) >= 2 and pieces[1]:
            try:
                value = re.sub(pieces[1], pieces[2] if len(pieces) > 2 else "", value)
            except re.error:
                pass
    return value.strip()


class ReaderEngine:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.js = LegadoJsRuntime(timeout)
        self.js_variables = {}
        self.headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)

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

    async def _request(self, request: dict, source=None) -> tuple[str, str, str]:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        last_error = None
        for attempt in range(3):
            connector = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
            try:
                headers = dict(self.headers)
                if source is not None:
                    headers.update(self._source_headers(source))
                request_headers = request.get("headers") or {}
                if isinstance(request_headers, str):
                    try: request_headers = json.loads(request_headers)
                    except json.JSONDecodeError: request_headers = {}
                headers.update(request_headers if isinstance(request_headers, dict) else {})
                async with aiohttp.ClientSession(headers=headers, connector=connector,
                                                 cookie_jar=self.cookie_jar) as session:
                    method = request.get("method", "GET")
                    kwargs = {"timeout": timeout, "ssl": False, "allow_redirects": True}
                    if method == "POST":
                        kwargs["data"] = request.get("body") or ""
                    async with session.request(method, encode_url(request["url"]), **kwargs) as resp:
                        resp.raise_for_status()
                        charset = request.get("charset") or resp.charset
                        raw = await resp.read()
                        return raw.decode(charset or "utf-8", errors="replace"), resp.content_type or "", str(resp.url)
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError,
                    asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"连接失败（已重试 3 次）：{last_error}")

    @staticmethod
    def _field_parts(rule: str) -> tuple[str, str]:
        value = rule or ""
        marker = value.find("@js:")
        if marker > 0:
            return value[:marker].strip(), value[marker:].strip()
        marker = value.find("<js>")
        if marker > 0 and "</js>" in value[marker:]:
            return value[:marker].strip(), value[marker:value.find("</js>", marker) + 5].strip()
        return value, ""

    def _field_value(self, node, rule: str, kind: str, source, base_url: str,
                     key: str = "", page: int = 1) -> str:
        selector, post_js = self._field_parts(rule)
        value = extract_value(node, selector, kind, base_url)
        if post_js:
            context = JsContext(
                source=source, key=key, page=page, result=value, base_url=base_url,
                variables=self.js_variables,
                get_string=lambda nested: extract_value(node, nested, kind, base_url),
            )
            transformed = self.js.evaluate(post_js, context)
            value = "" if transformed is None else str(transformed)
        return value

    def _book_from_node(self, node, kind: str, rules: dict, base_url: str,
                        source=None, key: str = "", page: int = 1) -> BookItem:
        def field(primary, fallback, with_base=False):
            try:
                value = self._field_value(node, primary or fallback, kind, source,
                                          base_url if with_base else "", key, page)
                if value or not primary:
                    return value
            except (UnsupportedRule, RuleParseError):
                pass
            return self._field_value(node, fallback, kind, source,
                                     base_url if with_base else "", key, page)

        return BookItem(
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

    def _books_from_nodes(self, nodes: list, kind: str, rules: dict, base_url: str,
                          source=None, key: str = "", page: int = 1) -> list[BookItem]:
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
        return [self._book_from_node(node, kind, rules, base_url, source, key, page)
                for node in nodes[:100]]

    async def search(self, source, keyword: str, page: int = 1) -> list[BookItem]:
        req = parse_search_url(_expand_page_template(source.searchUrl, page),
                               source.bookSourceUrl, keyword, page)
        if not req:
            raw = (source.searchUrl or "").strip()
            if _is_js(raw) or "{{" in raw and ("java." in raw or "source." in raw):
                js_value = await asyncio.to_thread(
                    self.js.evaluate, raw,
                    JsContext(source=source, key=keyword, page=page,
                              base_url=source.bookSourceUrl, variables=self.js_variables),
                )
                if not isinstance(js_value, str) or not js_value.strip():
                    raise UnsupportedRule("搜索 JS 没有返回有效请求地址")
                req = parse_search_url(js_value, source.bookSourceUrl, keyword, page)
            if not req:
                raise RuntimeError("搜索地址格式暂时无法解析")
        text, ctype, final_url = await self._request(req, source)
        rules = source.ruleSearch or {}
        list_rule = rules.get("bookList", "")
        js_rule, remaining_rule = split_js_rule(list_rule)
        if js_rule:
            transformed = await asyncio.to_thread(
                self.js.evaluate, js_rule,
                JsContext(source=source, key=keyword, page=page, result=text,
                          base_url=final_url, variables=self.js_variables),
            )
            text = transformed if isinstance(transformed, str) else json.dumps(transformed, ensure_ascii=False)
            ctype = "application/json" if text.lstrip().startswith(("[", "{")) else "text/html"
            list_rule = remaining_rule
        nodes, kind = select_nodes(text, ctype, list_rule)
        books = self._books_from_nodes(nodes, kind, rules, final_url, source, keyword, page)
        return [b for b in books if b.name and b.url and not _is_pagination_item(b.name, b.url)]

    async def explore(self, source, category: ExploreCategory, page: int = 1) -> list[BookItem]:
        url = _resolve_explore_url(category.url, source.bookSourceUrl, page)
        text, ctype, final_url = await self._request({"url": url, "method": "GET"}, source)
        # Legado permits explore fields to inherit the search rules. Many sources
        # only override one or two explore fields instead of duplicating the map.
        rules = dict(source.ruleSearch or {})
        rules.update({key: value for key, value in (source.ruleExplore or {}).items() if value})
        nodes, kind = select_nodes(text, ctype, rules.get("bookList", ""))
        books = self._books_from_nodes(nodes, kind, rules, final_url, source, "", page)
        return [book for book in books
                if book.name and book.url and not _is_pagination_item(book.name, book.url)]

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
