"""正文规则自愈：从抓到的样本页里挖候选选择器，回放验证后再提议改动。

候选由页面的正文密度生成，能不能用交给真规则引擎在同一份 HTML 上跑完整的
正文链路判定——候选靠试，不靠猜。提议前还要过同书其它章节页的抽样复核，
改动由用户确认后才写回书源。整个过程只在抓样本页时出网，回放不发新请求。
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.models.book_source import BookSource
from app.services.block_pages import block_reason
from app.services.engine.reading_engine import ChapterContent, LegadoReadingEngine
from app.services.engine.rule_engine import ChapterItem

# 样本页至少要有这么多汉字，否则说明抓到的是拦截页或渲染前的空壳。
CONTENT_FIX_MIN_CHARS = 200
# 复核页放宽：短章节也算有效样本。
CONFIRM_FIX_MIN_CHARS = 80
MAX_CANDIDATES = 32
MAX_REGION_DEPTH = 3
MAX_EXTRA_SAMPLES = 2
MAX_CONFIRM_TRIES = 3
# 后缀按"正文最干净"排前面：段落文本比整段文本更稳，整段文本又比原始 HTML
# 少带无关标记。
SELECTOR_SUFFIXES = ("@p@text", "@text", "@div@text", "@html")

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CLASS_RE = re.compile(r"[A-Za-z_][\w-]*")
_SCRIPT_MARKERS = ("@js:", "<js", "{{", "webview", "webjs")
_SKIP_TAGS = ("script", "style", "noscript", "iframe", "svg", "form",
              "nav", "header", "footer", "aside")


def _cjk_count(text: str) -> int:
    return sum(1 for _ in _CJK_RE.finditer(text))


@dataclass(frozen=True)
class RepairSample:
    """一条复核样本：哪一章、解析出多少字、开头长什么样。"""

    chapter: str
    url: str
    chars: int
    snippet: str


@dataclass(frozen=True)
class RuleFix:
    """一条待确认的规则改动。"""

    source_name: str
    source_url: str
    field: str
    old_value: str
    new_value: str
    samples: tuple[RepairSample, ...]
    page_url: str
    note: str = ""


@dataclass(frozen=True)
class RepairReport:
    """诊断结果：要么给出提议，要么给出为什么给不出来。"""

    fix: RuleFix | None
    reason: str = ""
    tried: int = 0
    pages: int = 0
    original_error: str = ""
    reached: str = "start"


def rule_needs_script(source) -> bool:
    """正文规则依赖 JS 或网页渲染时，选择器自愈无从下手。"""
    rules = getattr(source, "ruleContent", None) or {}
    if not isinstance(rules, dict):
        return True
    if str(rules.get("webJs") or "").strip():
        return True
    content = str(rules.get("content") or "")
    if not content.strip():
        return False
    lowered = content.lower()
    if "@js:" in lowered or "<js" in lowered:
        return True
    if "@webjs" in lowered or "<webjs" in lowered:
        return True
    return bool(re.search(r"\{\{|webView|webJs", content, re.I))


def merge_content_rule(old: str, new: str) -> str:
    """把兜底选择器追加成新的候选分支，原规则原样留在前面。"""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        return new
    if not new or new in old:
        return old
    return f"{old}||{new}"


def extract_candidates(html: str,
                       min_chars: int = CONTENT_FIX_MIN_CHARS) -> list[str]:
    """按正文密度反推 Legado 选择器候选，由内到外铺平（调试与提示用）。"""
    return [candidate for group in candidate_regions(html, min_chars)
            for candidate in group]


def candidate_regions(html: str,
                      min_chars: int = CONTENT_FIX_MIN_CHARS) -> list[list[str]]:
    return _mine(html, min_chars)[0]


def region_text_length(html: str,
                       min_chars: int = CONTENT_FIX_MIN_CHARS) -> int:
    """候选区域自身的汉字数，用来卡"解析出来的正文"长度是否走样。"""
    return _mine(html, min_chars)[1]


def _mine(html: str, min_chars: int) -> tuple[list[list[str]], int]:
    """一次解析同时给出候选分组和正文区域的汉字数。

    候选按区域分组：正文块本身在前，外层容器在后。分组是有序的——越紧的容器
    越可能是真容器，先在里面找；找不到再往外退。外层盒子多出来的那点文字
    （面包屑、侧栏）不该赢过更贴合的正文块。
    """
    soup = _readable_soup(html)
    region = _best_region(soup, min_chars)
    if region is None:
        return [], 0
    region_chars = _cjk_count(region.get_text(" ", strip=True))
    groups: list[list[str]] = []
    seen: set[str] = set()
    for element in _region_chain(region):
        group: list[str] = []
        for selector in _selector_forms(element):
            for suffix in SELECTOR_SUFFIXES:
                candidate = f"{selector}{suffix}"
                if candidate in seen:
                    continue
                seen.add(candidate)
                group.append(candidate)
        if group:
            groups.append(group)
        if sum(len(item) for item in groups) >= MAX_CANDIDATES:
            break
    return groups, region_chars


def page_text_length(html: str) -> int:
    """整页可见汉字数：太少说明抓到的是拦截页或渲染前的空壳。"""
    return _cjk_count(_readable_soup(html).get_text(" ", strip=True))


def _readable_soup(html: str) -> BeautifulSoup:
    # 挖候选只求快和稳：html.parser 比规则引擎用的 html5lib 便宜得多，而每条
    # 候选都要过真引擎回放，两边解析结果不一致时会被回放直接筛掉。
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    return soup


def _region_stats(element) -> tuple[int, int]:
    text = element.get_text(" ", strip=True)
    linked = sum(_cjk_count(link.get_text(" ", strip=True))
                 for link in element.find_all("a"))
    return _cjk_count(text), linked


def _best_region(soup: BeautifulSoup, min_chars: int):
    """正文区域 = 汉字最多、链接占比低的那块，再向下收到最紧的容器。"""
    best, best_cjk = None, 0
    for element in soup.find_all(True):
        if element.name in {"html", "body", "head"}:
            continue
        cjk, linked = _region_stats(element)
        if cjk < min_chars or linked * 4 > cjk:
            continue
        if cjk > best_cjk:
            best, best_cjk = element, cjk
    while best is not None:
        children = [(child, _cjk_count(child.get_text(" ", strip=True)))
                    for child in best.find_all(True, recursive=False)]
        if not children:
            break
        child, child_cjk = max(children, key=lambda item: item[1])
        if child_cjk < best_cjk * 0.8:
            break
        best, best_cjk = child, child_cjk
    return best


def _region_chain(region) -> list:
    """正文块本身优先，再回看几层祖先——真容器偶尔是外面那层。"""
    chain = [region]
    ancestor = getattr(region, "parent", None)
    depth = 0
    while ancestor is not None and depth < MAX_REGION_DEPTH:
        if getattr(ancestor, "name", None) in {None, "html", "body"}:
            break
        chain.append(ancestor)
        ancestor = ancestor.parent
        depth += 1
    return chain


def _selector_forms(element) -> list[str]:
    forms: list[str] = []
    tag = getattr(element, "name", None)
    if not tag or tag in {"html", "body", "[document]"}:
        return forms
    ident = str(element.get("id") or "").strip()
    if ident and _CLASS_RE.fullmatch(ident):
        forms.append(f"#{ident}")
    classes = [str(value) for value in (element.get("class") or [])
               if _CLASS_RE.fullmatch(str(value))]
    for name in classes[:2]:
        forms.append(f".{name}")
        forms.append(f"{tag}.{name}")
    if len(classes) >= 2:
        forms.append(f".{classes[0]}.{classes[1]}")
    if not forms:
        forms.append(tag)
    return forms


def _same_page(url: str, target: str) -> bool:
    """按路径比对：站点 302 换域名后，规则里的请求地址仍是旧域名。"""
    path = urlsplit(str(url or "")).path.rstrip("/")
    return bool(path) and path == urlsplit(str(target or "")).path.rstrip("/")


class ProbeEngine(LegadoReadingEngine):
    """诊断引擎：真抓样本页，随后把请求口焊死在这次响应上。

    回放因此不产生新请求，候选规则可以反复试；真实出网次数记在 ``fetches``
    里，调用方和测试都能据此确认没有额外访问。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fetches = 0
        self._replay: tuple[str, str, str] | None = None

    def arm(self, page: tuple[str, str, str] | None) -> None:
        self._replay = page

    async def fetch_page(self, url: str, source=None) -> tuple[str, str, str]:
        """真抓一页（会出网）。回放闸门打开时临时放行。"""
        armed, self._replay = self._replay, None
        self.fetches += 1
        try:
            return await self._request_transport({"url": url}, source=source)
        finally:
            self._replay = armed

    async def _request_transport(self, request: dict,
                                 source=None) -> tuple[str, str, str]:
        url = str(request.get("url") or "")
        if self._replay is None or url.startswith("data:"):
            return await super()._request_transport(request, source)
        if _same_page(url, self._replay[2]):
            return self._replay
        # 同一章节的后续页在回放里拿不到内容：宁可少一段，也不要拿样本页
        # 冒充下一页，把候选的得分灌水。
        return "", "text/html", url

    async def _load_protected_images(self, document, source,
                                     cache_all: bool = False) -> None:
        # 诊断只关心规则能不能解析出正文，取图片字节既不必要又要出网。
        return None


@dataclass
class _Page:
    """一份已抓到的样本页及其正文体量。"""

    chapter: ChapterItem
    html: str
    content_type: str
    url: str
    chars: int

    @property
    def triple(self) -> tuple[str, str, str]:
        return self.html, self.content_type, self.url


async def _load_page(engine: ProbeEngine, source, chapter: ChapterItem,
                     on_stage: Callable[[str], None] | None) -> tuple[_Page | None, str]:
    if on_stage:
        on_stage(f"正在抓取样本页 {chapter.name}…")
    try:
        html, content_type, final_url = await engine.fetch_page(chapter.url, source)
    except Exception as exc:
        return None, f"抓取样本页失败：{_error_text(exc)}"
    reason = block_reason(html, (getattr(engine, "last_response", None) or {}).get("status"))
    if reason:
        return None, f"样本页被拦截：{reason}"
    # 大页面的解析是纯 CPU 活，放在线程里别卡住界面。
    chars = await asyncio.to_thread(page_text_length, html)
    return _Page(chapter, html, content_type or "text/html", final_url, chars), ""


async def _run_rule(engine: ProbeEngine, source, chapter: ChapterItem,
                    page: _Page) -> tuple[ChapterContent | None, str]:
    engine.arm(page.triple)
    timeout = max(5.0, float(getattr(engine, "timeout", 15) or 15) + 5)
    try:
        document = await asyncio.wait_for(
            engine.content_document(source, chapter, "", need_save=False),
            timeout=timeout)
    except asyncio.TimeoutError:
        return None, "规则执行超时"
    except Exception as exc:
        return None, _error_text(exc)
    return document, ""


def _measure(document: ChapterContent) -> tuple[int, str, int]:
    parts, images = [], 0
    for block in document.blocks:
        if block.kind == "text":
            parts.append(str(block.value or ""))
        elif block.kind in {"image", "audio", "video"}:
            images += 1
    text = "\n".join(parts).strip()
    return _cjk_count(text), text, images


def _with_content_rule(source, value: str) -> BookSource:
    rules = dict(getattr(source, "ruleContent", None) or {})
    rules["content"] = value
    return BookSource.from_dict({**source.to_dict(), "ruleContent": rules})


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def diagnose_content_rule(
        source, chapter: ChapterItem, *,
        extra_chapters: Iterable[ChapterItem] = (),
        book=None,
        engine: ProbeEngine | None = None,
        timeout: int = 15,
        on_stage: Callable[[str], None] | None = None) -> RepairReport:
    """诊断一条书源的正文规则，给出可确认的替换提议。

    只在抓样本页时出网：首次抓失败章节页，再抓 1-2 页同书章节做复核。
    """
    rules = dict(getattr(source, "ruleContent", None) or {})
    old_rule = str(rules.get("content") or "").strip()
    if not old_rule:
        return RepairReport(None, reason="书源没有正文规则，没有可修补的选择器")
    if rule_needs_script(source):
        return RepairReport(
            None, reason="正文规则里有 JS 或网页渲染内容，选择器自愈不适用")

    own_engine = engine is None
    engine = engine or ProbeEngine(timeout=timeout, background=True)
    try:
        if book is not None:
            engine.current_book = book
        page, error = await _load_page(engine, source, chapter, on_stage)
        if page is None:
            return RepairReport(None, reason=error, reached="fetch")
        if page.chars < CONTENT_FIX_MIN_CHARS:
            return RepairReport(
                None, reached="fetch",
                reason=(f"样本页里只有 {page.chars} 字正文，"
                        "可能是拦截页或需要内置浏览器渲染"),
                pages=1)

        if on_stage:
            on_stage("正在从页面里找正文候选…")
        groups, region_chars = await asyncio.to_thread(
            _mine, page.html, CONTENT_FIX_MIN_CHARS)
        if not groups:
            return RepairReport(
                None, pages=1, reached="mine",
                reason=(f"页面里没有找到成段的正文文本（{page.chars} 字，"
                        "可能是导航或版权声明）"))
        candidates = [candidate for group in groups for candidate in group]

        minimum = min(CONTENT_FIX_MIN_CHARS, max(1, region_chars // 3))
        original, original_error = await _run_rule(
            engine, _with_content_rule(source, old_rule), chapter, page)
        original_chars = _measure(original)[0] if original is not None else 0
        if original_chars >= minimum:
            # 本地重放里原规则就解析出了正文，这次报错复现不出来：旧分支会一直
            # 赢在 || 前面，改什么都验证不了效果，不如老实说没复现。
            return RepairReport(
                None, pages=1, original_error=original_error, reached="replay",
                reason=(f"本地重放里原规则已解析出 {original_chars} 字，"
                        "没复现出这次报错，可能是站点临时波动，"
                        "重新打开这一章试试"))

        pages = [page]
        for extra in list(extra_chapters)[:MAX_EXTRA_SAMPLES]:
            extra_page, error = await _load_page(engine, source, extra, on_stage)
            if extra_page is None or extra_page.chars < CONFIRM_FIX_MIN_CHARS:
                continue
            pages.append(extra_page)

        if on_stage:
            on_stage(f"正在回放 {len(candidates)} 条候选规则…")
        tried = 0
        failure = ""
        for group in groups:
            passed = []
            for candidate in group:
                measured = await _measure_candidate(
                    engine, source, chapter, candidate, page,
                    region_chars, minimum)
                tried += 1
                if measured is not None:
                    passed.append(candidate)
            if not passed:
                continue
            if on_stage:
                on_stage(f"正在用 {len(pages)} 页复核候选…")
            for candidate in passed[:MAX_CONFIRM_TRIES]:
                fix, failure = await _confirm_candidate(
                    engine, source, old_rule, candidate, page, pages, minimum)
                if fix is not None:
                    return RepairReport(
                        fix, tried=tried, pages=len(pages),
                        original_error=original_error, reached="confirm")
        if failure:
            return RepairReport(
                None, tried=tried, pages=len(pages),
                original_error=original_error, reached="confirm",
                reason=failure)
        return RepairReport(
            None, tried=tried, pages=len(pages),
            original_error=original_error, reached="verify",
            reason=await _no_candidate_reason(
                engine, source, chapter, page, candidates, rules))
    finally:
        if own_engine:
            await engine.aclose()


async def _confirm_candidate(engine: ProbeEngine, source, old_rule: str,
                             candidate: str, page: _Page, pages: list,
                             minimum: int) -> tuple[RuleFix | None, str]:
    """把候选接在原规则后面逐页复核；全都解析出正文才算数。"""
    merged = merge_content_rule(old_rule, candidate)
    samples = []
    for sample_page in pages:
        document, error = await _run_rule(
            engine, _with_content_rule(source, merged),
            sample_page.chapter, sample_page)
        name = sample_page.chapter.name
        if document is None:
            return None, f"候选 {candidate} 在《{name}》上失败：{error}"
        chars, text, _ = _measure(document)
        floor = minimum if sample_page is page else min(CONFIRM_FIX_MIN_CHARS, minimum)
        if chars < floor:
            return None, f"候选 {candidate} 在《{name}》上只解析出 {chars} 字"
        samples.append(RepairSample(name, sample_page.chapter.url, chars,
                                    _snippet(text)))
    return RuleFix(str(getattr(source, "bookSourceName", "") or ""),
                   str(getattr(source, "bookSourceUrl", "") or ""),
                   "content", old_rule, merged, tuple(samples), page.url,
                   note=_fix_note(candidate, len(pages))), ""


async def _measure_candidate(engine: ProbeEngine, source, chapter: ChapterItem,
                             candidate: str, page: _Page, region_chars: int,
                             minimum: int):
    document, _ = await _run_rule(
        engine, _with_content_rule(source, candidate), chapter, page)
    if document is None:
        return None
    chars, text, images = _measure(document)
    if chars < minimum:
        return None
    # 选择器匹配到的范围必须和正文块本身对得上：太短是取到了片段，太长
    # 是顺手把导航、推荐位也带了进来。
    if region_chars and not (region_chars * 0.3 <= chars <= region_chars * 3):
        return None
    return chars, text, images


async def _no_candidate_reason(engine: ProbeEngine, source, chapter: ChapterItem,
                               page: _Page, candidates: list[str],
                               rules: dict) -> str:
    """候选全灭时，尽量说清是不是被替换规则删空了。"""
    pattern = str(rules.get("replaceRegex") or "").strip()
    if pattern:
        stripped = dict(rules)
        stripped["replaceRegex"] = ""
        for candidate in candidates[:MAX_CONFIRM_TRIES]:
            blank = BookSource.from_dict({**source.to_dict(), "ruleContent": {
                **stripped, "content": candidate}})
            document, _ = await _run_rule(engine, blank, chapter, page)
            if (document is not None and
                    _measure(document)[0] >= CONTENT_FIX_MIN_CHARS):
                return (f"页面里有正文，但候选都被 ruleContent.replaceRegex "
                        f"删空了：{pattern}")
    return f"没找到能解析出正文的选择器（试了 {len(candidates)} 个候选）"


def _snippet(text: str, limit: int = 60) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _fix_note(candidate: str, pages: int) -> str:
    scope = "仅按 1 页样本" if pages <= 1 else f"已过 {pages} 页复核"
    return f"原规则保留在前，新增兜底分支 {candidate}（{scope}）"
