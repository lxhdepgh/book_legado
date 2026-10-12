"""Rule-engine-driven, layered validation for Legado book sources."""
from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

from app.models.book_source import RULE_FIELDS
from app.services.block_pages import SiteBlockedError, block_reason
from app.services.compat_audit import scan_missing_java_packages, source_texts
from app.services.headless_browser import BrowserUnavailable, available_fetcher
from app.services.engine.legado_js import JsBridgeError
from app.services.engine.rule_engine import (RequestConnectionError, RequestTimeoutError,
                                      UnsupportedRule, resolve_explore_categories)
from app.services.engine.reading_engine import LegadoReadingEngine


# 方案1：提高并发数（从8提升到16，适配现代多核CPU）
CHECK_CONCURRENCY = 16
CHECK_MAX_CONCURRENCY = 32  # 上限翻倍，允许高配机器跑得更快
TAG_PAGE_CONCURRENCY = 8  # 分类页并发也提升
# Explore categories are the slowest stage (one request per category, several
# categories per source).  They get their own narrow pool so they cannot idle
# the workers that still have a search or reading chain to run.
TAG_SOURCE_CONCURRENCY = 8  # 从4提升到8
JS_CHECK_CONCURRENCY = 4  # JS执行并发从2提升到4（现代JS引擎更快）
SOURCE_TAG_TIMEOUT = 15  # 缩短分类超时（从20秒降到15秒）
CHECK_TAG_CATEGORIES = 6
# A host intercepted by local security software answers with a block page, so
# waiting for the rule stages only burns their timeouts.  Interception is local
# and answers in milliseconds, so this budget only needs to cover scheduling
# noise; a later block-page verdict still classifies the source correctly.
BLOCKED_PROBE_BUDGET = 1.5
# A source whose home page is a slow blackhole must not keep the source open
# after every rule stage has already finished.
PROBE_JOIN_BUDGET = 3.0
READING_CHAIN_TIMEOUT = 12  # 缩短阅读链超时（从15秒降到12秒）
READING_CHAIN_CANDIDATES = 3
# Upper bound for the whole reading chain of one source.  Three candidates at
# the per-stage timeout each would otherwise hold a worker for minutes.
READING_CHAIN_BUDGET = 25  # 缩短总预算（从30秒降到25秒）
# Hard ceiling for everything one source may spend, search stage included, so a
# single half-dead site cannot extend the tail of a long run.
SOURCE_CHECK_BUDGET = 35  # 缩短单源总预算（从45秒降到35秒）
# Explore and reading hit the same host the search stage just failed to reach,
# so a host-level search error already answers what they would ask.
HOST_UNREACHABLE_KINDS = {"network_error", "timeout", "js_error", "rate_limited"}
# Book-level flukes (paywalled or promotional entries) should not condemn the
# whole source, so the chain retries with the next candidate on these errors.
RETRYABLE_CHAIN_ERRORS = {"正文解析结果为空", "目录中没有可读取章节"}
#: 检测深度。``full`` 一路验到正文；``list`` 验到列表链路就收（九态结论停在
#: search_only / explore_only / list_usable 那一层，不会把"没验过"说成"验过不行"）。
#: 分级的价值量在 `.probe/check_tier_depth.py`（16/24 条健康源、127.0.0.1 假源）：
#: 阅读链占两段规则耗时 74.7%，每源 5 次 HTTP 里 3 次是它发的；两档深度交替跑
#: 24 源/轮 × 5 轮：full 638 ms vs list 284 ms（省 55.6% 墙钟、60% 请求）。
#: 为什么主站连通**不能**当门：真库 917 条快照里 61 条（6.7%）的 searchUrl 与
#: bookSourceUrl 不同主机、13 条的 bookSourceUrl 连 http(s) 都不是；同探针实测
#: "主站拒连 + 搜索正文全通"的源现在判 fully_usable，加硬门就把它打死。
CHECK_DEPTH_FULL = "full"
CHECK_DEPTH_LIST = "list"
CHECK_DEPTHS = {CHECK_DEPTH_FULL, CHECK_DEPTH_LIST}


def _error_text(exc: Exception, limit: int = 180) -> str:
    return str(exc or exc.__class__.__name__).strip()[:limit]


def _remaining(deadline: float, cap: float) -> float:
    """Stage timeout that never lets the source outlive its total budget."""
    return max(1.0, min(float(cap), deadline - time.perf_counter()))


def _error_kind(exc: Exception) -> str:
    message = _error_text(exc).lower()
    if isinstance(exc, SiteBlockedError):
        return "blocked"
    if isinstance(exc, (RequestTimeoutError, asyncio.TimeoutError)) or "timeout" in message:
        return "timeout"
    if isinstance(exc, UnsupportedRule):
        return "unsupported"
    if isinstance(exc, JsBridgeError):
        return "js_error"
    # JS 运行时错误（浏览器上下文缺失、全局变量未定义等）
    js_runtime_patterns = [
        "cannot read property", "not a function", "is not defined",
        "undefined is not", "expecting value", "json.parse",
        "typeerror:", "referenceerror:"
    ]
    if any(pattern in message for pattern in js_runtime_patterns):
        return "js_error"
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if isinstance(exc, (RequestConnectionError, aiohttp.ClientError, OSError)):
        return "network_error"
    return "rule_error"


def _source_uses_js(source) -> bool:
    """Pick the narrow JS lane for sources whose rules call @js: directly.

    Bare ``{{key}}``/``{{page}}`` templates are deliberately **not** markers:
    they do reach QuickJS, but pulling them into js_sem(2) measured slower for a
    round of 16 such sources (1285 ms -> 1518 ms, A/B interleaved in `.probe/
    template_bare_var_fastpath.py`) — the dispatcher is the serial resource
    either way, so narrowing its admission only adds queueing.  The fix for
    those evals was to stop making them (see
    `ReaderEngine._bare_template_value`), not to re-classify the source.

    The section loop walks the model's ``RULE_FIELDS`` instead of naming the
    sections here: this used to be a hand-written five-section list with no
    ``ruleReview`` — a third copy of the same schema knowledge in this repo.
    Measured on the 917-source snapshot under `resource/` the gap is currently
    inert (0 sources carry ``ruleReview`` at all), so switching costs no behavior
    today; it only removes the chance of a future section being classified as
    JS-free.
    """
    values = [
        getattr(source, "header", ""), getattr(source, "searchUrl", ""),
        getattr(source, "exploreUrl", ""),
    ]
    for name in RULE_FIELDS:
        values.extend((getattr(source, name, {}) or {}).values())
    return any(
        marker in str(value or "")
        for value in values
        for marker in ("@js:", "<js>", "{{java.", "{{source.", "webView")
    )


def _unsupported_java_packages(source) -> list[str]:
    """接口缺失是书源自身的属性，不依赖网络，规则跑之前就能判定。"""
    data = source.to_dict() if hasattr(source, "to_dict") else source
    return scan_missing_java_packages(source_texts(data))


def _has_readable_content(document) -> bool:
    for block in getattr(document, "blocks", []) or []:
        if block.kind in {"image", "audio", "video"} and block.value:
            return True
        if block.kind == "text" and str(block.value or "").strip():
            return True
    return bool(str(getattr(document, "raw_content", "") or "").strip())


async def _probe_source(source, session, semaphore, timeout: int) -> dict:
    """Diagnose basic connectivity without deciding whether rules are usable."""
    url = str(getattr(source, "bookSourceUrl", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"status": "skip", "status_code": None,
                "response_time_ms": None, "error": None}
    started = time.perf_counter()
    try:
        async with semaphore:
            async with session.get(
                    url, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                head = await response.content.read(8192)
                elapsed = (time.perf_counter() - started) * 1000
                marker = block_reason(
                    head.decode("utf-8", errors="replace"), response.status)
                if marker:
                    return {"status": "blocked", "status_code": response.status,
                            "response_time_ms": elapsed, "error": marker}
                # A rejected home page is not a working host; only a 2xx/3xx
                # answer proves the site answered us.
                status = "ok" if response.status < 400 else "err"
                return {"status": status, "status_code": response.status,
                        "response_time_ms": elapsed,
                        "error": None if status == "ok" else f"HTTP {response.status}"}
    except Exception as exc:
        return {"status": "err", "status_code": None,
                "response_time_ms": (time.perf_counter() - started) * 1000,
                "error": _error_text(exc)}


async def _engine_search_check(source, engine, timeout: int) -> tuple[dict, list]:
    keyword = str((getattr(source, "ruleSearch", {}) or {}).get(
        "checkKeyWord") or "我的")
    if not str(getattr(source, "searchUrl", "") or "").strip():
        return ({"search_status": "skip", "book_count": None,
                 "search_keyword": keyword, "search_method": "rule-engine",
                 "search_error": None}, [])
    try:
        books = await asyncio.wait_for(
            engine.search(source, keyword), timeout=timeout)
        return ({"search_status": "ok" if books else "no_results",
                 "book_count": len(books), "search_keyword": keyword,
                 "search_method": "rule-engine", "search_error": None}, books)
    except Exception as exc:
        return ({"search_status": _error_kind(exc), "book_count": None,
                 "search_keyword": keyword, "search_method": "rule-engine",
                 "search_error": _error_text(exc)}, [])


async def _locally_blocked_probe(probe_task, timeout):
    """Return the probe result when it already proves a local interception.

    The probe runs concurrently with the rule stages; waiting a short budget
    for it lets a blocked host skip search/explore/reading entirely, while a
    slow probe still falls through to the normal path.
    """
    budget = max(0.0, min(float(BLOCKED_PROBE_BUDGET), float(timeout)))
    done, _ = await asyncio.wait({probe_task}, timeout=budget)
    if probe_task in done and (probe_task.result() or {}).get("status") == "blocked":
        return probe_task.result()
    return None


async def _await_probe(probe_task, timeout):
    """Collect the probe verdict, giving up on a home page that never answers.

    The rule stages are already done at this point, so a host that swallows the
    probe request must not keep the source (and its worker) open any longer.
    """
    try:
        return await asyncio.wait_for(probe_task, timeout=PROBE_JOIN_BUDGET)
    except asyncio.TimeoutError:
        return {"status": "unknown", "status_code": None,
                "response_time_ms": None,
                "error": f"站点首页 {PROBE_JOIN_BUDGET:.0f} 秒内没有响应"}


async def check_categories(source, session=None, sem=None, timeout=10,
                           engine=None, max_categories=None,
                           stop_after_valid=False):
    """Validate explore categories with the same engine used by real reading.

    With ``stop_after_valid`` the loop ends at the first category that parses:
    for a connectivity verdict one working category already proves the explore
    chain reaches the host, and the remaining samples would only add latency.
    Failing categories are still walked to the sample cap, because that is when
    the extra samples carry the evidence.
    """
    del session  # Retained for API compatibility with older callers/tests.
    own_engine = engine is None
    engine = engine or LegadoReadingEngine(timeout=timeout, background=True)
    categories = await resolve_explore_categories(source, runtime=engine.js)
    if max_categories:
        categories = categories[:max_categories]
    semaphore = sem or asyncio.Semaphore(TAG_PAGE_CONCURRENCY)

    async def check_page(category, page):
        result = {"page": page, "valid": False, "status_code": None,
                  "item_count": 0, "content_type": "rule-engine",
                  "error": None, "attempts": 1,
                  "persistent_rate_limit": False, "books": []}
        try:
            async with semaphore:
                books = await asyncio.wait_for(
                    engine.explore(source, category, page), timeout=timeout)
            result.update(valid=bool(books), item_count=len(books), books=books)
            if not books:
                result["error"] = "规则引擎未解析到书籍"
        except Exception as exc:
            kind = _error_kind(exc)
            result["error"] = f"{kind}: {_error_text(exc)}"
            result["persistent_rate_limit"] = kind == "rate_limited"
        return result

    output = []
    try:
        # One engine carries Legado variables and cookies, so categories/pages
        # are deliberately sequential inside a source. Sources remain parallel.
        for category in categories:
            pages = ([1, 2] if ("{{page" in category.url or
                                re.search(r"<[^<>]*,[^<>]*>", category.url))
                     else [1])
            page_results = []
            for page in pages:
                page_results.append(await check_page(category, page))
            first = page_results[0]
            output.append({
                "title": category.title, "url": category.url,
                "valid": first["valid"], "status_code": first["status_code"],
                "item_count": first["item_count"], "error": first["error"],
                "pagination_valid": (page_results[1]["valid"]
                                     if len(page_results) > 1 else None),
                "page_results": page_results,
                "books": first["books"],
            })
            if stop_after_valid and first["valid"]:
                break
        return output
    finally:
        if own_engine:
            await engine.aclose()


def _stage_needs_browser(value) -> bool:
    """True when a stage can only be read through Legado's backstage WebView.

    Two shapes mean the same thing: a ``{"webView":true}`` suffix on the URL a
    rule produces, and the ``webJs`` field of ``ruleContent``, which Legado
    hands to BackstageWebView as the wait script.  Neither is meaningful over
    plain HTTP, so a headless check cannot judge such a stage.
    """
    if value in (None, "", {}, []):
        return False
    if isinstance(value, (dict, list)):
        # json.dumps escapes the inner quotes of a URL-option suffix.
        text = json.dumps(value, ensure_ascii=False).replace('\\"', '"')
    else:
        text = str(value)
    return bool(re.search(r'"\s*web_?view\s*"\s*:\s*true', text, re.I) or
                re.search(r'"\s*webJs\s*"\s*:\s*"[^"]', text))


def _browser_only_stages(source) -> set:
    """Reading-chain stages whose rules require the built-in browser."""
    stages = set()
    toc = getattr(source, "ruleToc", {}) or {}
    if isinstance(toc, dict) and _stage_needs_browser(toc):
        stages.add("catalog")
    if _stage_needs_browser(getattr(source, "ruleContent", {}) or {}):
        stages.add("content")
    return stages


def _chain_stub(reason: str) -> dict:
    """阅读链**没试**时的结果（reason 说明为什么没试）。

    九态分类把 reading_status == skip 当成"这一段没验证过"，结论停在列表
    已经证明的那一级；但 blocked 与快速检测两种"没试"的原因不同，界面上得能分开，
    否则用户看见的都是同一句"仅搜索可用"，不知道有一条是被主站拦了、有一条是
    本轮压根没验正文。
    """
    return {"reading_status": "skip", "reading_stage": "none",
            "reading_error": None, "chapter_count": None,
            "sample_book": None, "sample_chapter": None,
            "sample_chapter_urls": [], "reading_tried": 0,
            "reading_check_skipped": reason}


async def _reading_chain_check(source, engine, candidates, timeout,
                               budget=READING_CHAIN_BUDGET) -> dict:
    result = {"reading_status": "skip", "reading_stage": "none",
              "reading_error": None, "chapter_count": None,
              "sample_book": None, "sample_chapter": None,
              "sample_chapter_urls": [], "reading_tried": 0,
              "reading_check_skipped": None}
    books = [item for item in candidates if getattr(item, "url", "")]
    if not books:
        # 没候选不代表正文链路坏了，只代表前面的段没给出可点的书。
        result["reading_check_skipped"] = "no_candidates"
        return result
    browser_stages = _browser_only_stages(source)
    has_browser = getattr(engine, "webview_fetcher", None) is not None
    books = books[:READING_CHAIN_CANDIDATES]
    result["sample_book"] = str(getattr(books[0], "name", "") or "")
    deadline = (time.perf_counter()
                + max(1.0, min(float(budget), timeout * len(books))))
    for book in books:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            result["reading_status"] = "timeout"
            result["reading_error"] = "阅读链路抽检总耗时超限"
            break
        result["reading_tried"] += 1
        stage_timeout = min(timeout, remaining)
        try:
            result["reading_stage"] = "detail"
            book = await asyncio.wait_for(
                engine.book_info(source, book), timeout=stage_timeout)
            result["sample_book"] = str(getattr(book, "name", "")
                                        or result["sample_book"])
            result["reading_stage"] = "catalog"
            chapters = await asyncio.wait_for(
                engine.chapters(source, book), timeout=stage_timeout)
            readable_chapters = [item for item in chapters
                                 if not item.is_volume and getattr(item, "url", "")]
            # 目录已经在手上，顺手把正文样本页地址留给规则自愈用。
            result["sample_chapter_urls"] = [item.url
                                             for item in readable_chapters[:3]]
            readable = next(iter(readable_chapters), None)
            result["chapter_count"] = len([item for item in chapters if not item.is_volume])
            if readable is None:
                raise RuntimeError("目录中没有可读取章节")
            result["sample_chapter"] = str(readable.name or "")
            result["reading_stage"] = "content"
            document = await asyncio.wait_for(
                engine.content_document(source, readable, need_save=False),
                timeout=stage_timeout)
            if not _has_readable_content(document):
                raise RuntimeError("正文解析结果为空")
            result.update(reading_status="ok", reading_stage="complete",
                          reading_error=None)
            return result
        except Exception as exc:
            kind = _error_kind(exc)
            # 这一阶段在无浏览器环境下按 HTTP 结果判断，正文由脚本渲染时必然为
            # 空。判成"正文不可用"会把一条本机验证不了的链路报成书源失效。
            if (kind == "rule_error" and result["reading_stage"] in browser_stages
                    and (not has_browser or isinstance(exc, BrowserUnavailable))):
                result["reading_status"] = "needs_browser"
                result["reading_error"] = ("该阶段需要内置浏览器渲染，"
                                           "本次检测未启用浏览器")
                break
            result["reading_status"] = kind
            result["reading_error"] = _error_text(exc)
            if (kind == "rule_error" and
                    (result["reading_error"] or "").split("：")[-1].strip()
                    not in RETRYABLE_CHAIN_ERRORS):
                break
    return result


def _classify_result(result: dict) -> str:
    # A keyword miss still proves the search chain parsed; only an executed
    # rule that produced books counts as fully verified downstream though.
    search_ok = result.get("search_status") in {"ok", "no_results"}
    explore_ok = result.get("tag_valid", 0) > 0
    reading = result.get("reading_status")
    uncertain = {"timeout", "rate_limited", "network_error", "js_error",
                 "unsupported"}
    if reading == "needs_browser":
        # 正文链路要渲染后才拿得到，本次检测没开浏览器，等于没验证过这一段；
        # 结论只能停在列表链路已经证明的那一级。
        reading = "skip"
    if reading == "ok":
        return "fully_usable"
    if reading in uncertain:
        return "unknown"
    # 拦截页会被当成"搜索无结果"，必须排在 search_ok 判定之前；只有真的解析出
    # 书籍或标签，才能证明本机确实访问到了该站点。远端 WAF 拦截（Cloudflare
    # 403 等）由规则阶段抛出，和本机杀软拦截归为同一类结论。
    if (result.get("status") == "blocked" or
            result.get("search_status") == "blocked" or reading == "blocked"):
        return "blocked"
    # 首页被拒绝访问（401/403/429）而规则又没解析出任何东西时，站点是拦了我们
    # 而不是书源失效；把它和"完全没有响应"区分开，用户才知道要换线路或代理。
    if (result.get("status_code") in {401, 403, 429} and
            result.get("search_status") != "ok" and not explore_ok):
        return "blocked"
    if search_ok or explore_ok:
        return "content_unavailable" if reading not in {"skip", None} else (
            "search_only" if search_ok and not explore_ok else
            "explore_only" if explore_ok and not search_ok else "list_usable")
    if (result.get("search_status") in uncertain or
            result.get("tag_check_timeout") or result.get("status") == "err"):
        return "unknown"
    return "unusable"


async def check_sources(sources, concurrency=CHECK_CONCURRENCY, timeout=12,
                        on_result=None, auto_disable=False,
                        depth=CHECK_DEPTH_FULL):
    """批量检测书源（支持分段超时、连接池复用、DNS缓存）。
    
    方案2实现：分段超时策略
    - 连通性探测：timeout * 0.25（默认3秒）
    - 搜索链路：timeout * 0.5（默认6秒）  
    - 阅读链路：timeout * 0.75（默认9秒）
    
    方案3实现：连接池优化
    - limit_per_host 从3提升到6，允许同域名更多并发
    - enable_cleanup_closed=True 自动清理关闭的连接
    
    方案4实现：DNS缓存增强
    - ttl_dns_cache 从300秒提升到600秒（10分钟）
    - 减少重复DNS查询开销
    """
    depth = str(depth or CHECK_DEPTH_FULL).strip().lower()
    if depth not in CHECK_DEPTHS:
        raise ValueError(f"未知的检测深度：{depth!r}（可用：{sorted(CHECK_DEPTHS)}）")
    list_depth = depth == CHECK_DEPTH_LIST
    worker_limit = max(1, min(int(concurrency), CHECK_MAX_CONCURRENCY))
    network_sem = asyncio.Semaphore(worker_limit)
    engine_sem = asyncio.Semaphore(worker_limit)
    tag_sem = asyncio.Semaphore(min(worker_limit, TAG_SOURCE_CONCURRENCY))
    js_sem = asyncio.Semaphore(JS_CHECK_CONCURRENCY)
    
    # 方案2：计算分段超时
    probe_timeout = max(2.0, timeout * 0.25)  # 连通性探测最少2秒
    search_timeout = max(4.0, timeout * 0.5)  # 搜索链路最少4秒
    reading_timeout = max(8.0, timeout * 0.75)  # 阅读链路最少8秒
    
    # 方案3+4：优化连接池和DNS缓存
    connector = aiohttp.TCPConnector(
        ssl=False, 
        limit=worker_limit, 
        limit_per_host=6,  # 从3提升到6，同域名可更多并发
        ttl_dns_cache=600,  # 从300提升到600秒（10分钟）
        enable_cleanup_closed=True,  # 自动清理关闭的连接
        force_close=False)  # 保持连接复用
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) "
                             "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
    results = []

    async with aiohttp.ClientSession(
            connector=connector, headers=headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        browser_fetcher = available_fetcher()

        async def check_source(source, index):
            # 方案2：使用分段超时
            probe_task = asyncio.create_task(
                _probe_source(source, session, network_sem, probe_timeout))
            # 缺口判定要走一次 JS 模拟面清单，放线程里跑，别卡住事件循环。
            java_gaps = asyncio.create_task(
                asyncio.to_thread(_unsupported_java_packages, source))
            # 每源一条引擎、**一条自己的连接池**，这是量出来的结论，不是没想过共享
            # （`.probe/engine_reuse_attr.py`）：真实形状下各源是不同主机，共享池一次
            # 握手都省不下来（8 源 16 次 vs 16 次）；唯一省下来的形状（多条源挤同一
            # 域名）反而更慢——共享让 limit_per_host 变成跨源排队，14 次拨号降到 8 次
            # 却把墙钟从 700ms 顶到 746ms。引擎本身构造/关闭合计 ~9ms/8 源。
            # 方案2：使用阅读链路的分段超时
            engine = LegadoReadingEngine(timeout=reading_timeout,
                                         webview_fetcher=browser_fetcher,
                                         background=True)
            limiter = js_sem if _source_uses_js(source) else engine_sem
            try:
                probe = await _locally_blocked_probe(probe_task, probe_timeout)
                if probe is not None:
                    search, search_books = {
                        "search_status": "skip", "book_count": None,
                        "search_keyword": "", "search_method": "rule-engine",
                        "search_error": None}, []
                    tags, tag_timeout, tag_skipped = [], False, True
                    chain = _chain_stub("probe_blocked")
                else:
                    async with limiter:
                        # The ceiling starts here, **before** the search stage, so a
                        # black-hole search cannot add its own timeout on top of the
                        # budget.  It must stay inside the limiter: queue wait behind
                        # js_sem is not this source's fault and must not eat it.
                        # 本机实测（`.probe/check_budget_and_js_gate.py`，旋钮按同比例
                        # 缩小：timeout=3、预算 2s、搜索挂 1.5s、正文挂 8s）：起点在搜索
                        # 之后时单源墙钟 3743 ms（超出恰好等于搜索段的 1588 ms），起点
                        # 前移后 2754 ms，剩下 0.75 s 是 _remaining 的 1s 下限，不是漏。
                        deadline = time.perf_counter() + SOURCE_CHECK_BUDGET
                        # 方案2：使用搜索链路的分段超时
                        search, search_books = await _engine_search_check(
                            source, engine, _remaining(deadline, search_timeout))
                        tags, tag_timeout, tag_skipped = [], False, False
                        if (getattr(source, "enabledExplore", True) and
                                str(getattr(source, "exploreUrl", "") or "").strip()):
                            if search.get("search_status") in HOST_UNREACHABLE_KINDS:
                                # Explore would ask the same unreachable host the
                                # same question the search stage already failed.
                                tag_skipped = True
                            else:
                                try:
                                    async with tag_sem:
                                        tags = await asyncio.wait_for(
                                            check_categories(
                                                source,
                                                timeout=min(search_timeout, 10),
                                                engine=engine,
                                                max_categories=CHECK_TAG_CATEGORIES,
                                                stop_after_valid=True),
                                            timeout=_remaining(
                                                deadline, SOURCE_TAG_TIMEOUT))
                                except asyncio.TimeoutError:
                                    tag_timeout = True
                        candidates = list(search_books)
                        for tag in tags:
                            candidates.extend(tag.pop("books", []))
                            for page in tag.get("page_results", []):
                                page.pop("books", None)
                        # 快速深度到这一段就收：阅读链是唯一会翻倍的段（最多 3 个
                        # 候选 × detail/catalog/content），实测占两段规则耗时 74.7%。
                        chain = (_chain_stub(CHECK_DEPTH_LIST) if list_depth else
                                 await _reading_chain_check(
                                     source, engine, candidates,
                                     READING_CHAIN_TIMEOUT,
                                     budget=_remaining(deadline,
                                                       READING_CHAIN_BUDGET)))
                    probe = await _await_probe(probe_task, timeout)
                result = {"idx": index, "name": source.bookSourceName,
                          "raw_url": source.bookSourceUrl, **probe,
                          **search, **chain, "category_results": tags,
                          "tag_check_timeout": tag_timeout,
                          "tag_check_skipped": tag_skipped,
                          "unsupported_java_packages": await java_gaps}
            finally:
                if not probe_task.done():
                    probe_task.cancel()
                await engine.aclose()
            result["tag_total"] = len(result["category_results"])
            result["tag_valid"] = sum(
                1 for item in result["category_results"] if item["valid"])
            result["tag_invalid"] = result["tag_total"] - result["tag_valid"]
            result["check_state"] = _classify_result(result)
            result["usable"] = result["check_state"] in {
                "fully_usable", "search_only", "explore_only", "list_usable"}
            result["auto_disabled"] = False
            # Only deterministic complete failures are safe to disable. A
            # reachable list whose detail/catalog/content broke stays enabled.
            if (auto_disable and result["check_state"] == "unusable" and
                    getattr(source, "enabled", False)):
                source.enabled = False
                result["auto_disabled"] = True
                result["disable_reason"] = (
                    "搜索与发现链路均明确不可用" if list_depth else
                    "搜索、发现与阅读链路均明确不可用")
            return result

        queue = asyncio.Queue()
        for index, source in enumerate(sources, 1):
            queue.put_nowait((source, index))

        async def worker():
            while True:
                try:
                    source, index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await check_source(source, index)
                except Exception as exc:
                    result = {
                        "idx": index, "name": source.bookSourceName,
                        "raw_url": source.bookSourceUrl, "status": "err",
                        "search_status": "rule_error", "error": _error_text(exc),
                        "category_results": [], "tag_total": 0, "tag_valid": 0,
                        "tag_invalid": 0, "usable": False,
                        "check_state": "unknown", "reading_status": "skip",
                        "reading_stage": "none", "reading_error": None,
                        "reading_check_skipped": "stage_error",
                        "auto_disabled": False, "tag_check_timeout": False,
                        "tag_check_skipped": False,
                        "unsupported_java_packages": await asyncio.to_thread(
                            _unsupported_java_packages, source),
                    }
                finally:
                    queue.task_done()
                results.append(result)
                if on_result:
                    on_result(result, len(results), len(sources))

        workers = [asyncio.create_task(worker())
                   for _ in range(min(worker_limit, len(sources)))]
        await asyncio.gather(*workers)
    return sorted(results, key=lambda item: item["idx"])
