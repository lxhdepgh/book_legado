"""Rule-engine-driven, layered validation for Legado book sources."""
from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

from app.services.block_pages import SiteBlockedError, block_reason
from app.services.compat_audit import scan_missing_java_packages, source_texts
from app.services.headless_browser import BrowserUnavailable, available_fetcher
from app.services.legado_js import JsBridgeError
from app.services.rule_engine import UnsupportedRule, resolve_explore_categories
from app.services.reading_engine import LegadoReadingEngine


CHECK_CONCURRENCY = 8
CHECK_MAX_CONCURRENCY = 16
TAG_PAGE_CONCURRENCY = 4
# Explore categories are the slowest stage (one request per category, several
# categories per source).  They get their own narrow pool so they cannot idle
# the workers that still have a search or reading chain to run.
TAG_SOURCE_CONCURRENCY = 4
JS_CHECK_CONCURRENCY = 2
SOURCE_TAG_TIMEOUT = 20
CHECK_TAG_CATEGORIES = 6
# A host intercepted by local security software answers with a block page, so
# waiting for the rule stages only burns their timeouts.  Interception is local
# and answers in milliseconds, so this budget only needs to cover scheduling
# noise; a later block-page verdict still classifies the source correctly.
BLOCKED_PROBE_BUDGET = 1.5
# A source whose home page is a slow blackhole must not keep the source open
# after every rule stage has already finished.
PROBE_JOIN_BUDGET = 3.0
READING_CHAIN_TIMEOUT = 15
READING_CHAIN_CANDIDATES = 3
# Upper bound for the whole reading chain of one source.  Three candidates at
# the per-stage timeout each would otherwise hold a worker for minutes.
READING_CHAIN_BUDGET = 30
# Hard ceiling for everything one source may spend after its search stage, so a
# single half-dead site cannot extend the tail of a long run.
SOURCE_CHECK_BUDGET = 45
# Explore and reading hit the same host the search stage just failed to reach,
# so a host-level search error already answers what they would ask.
HOST_UNREACHABLE_KINDS = {"network_error", "timeout", "js_error", "rate_limited"}
# Book-level flukes (paywalled or promotional entries) should not condemn the
# whole source, so the chain retries with the next candidate on these errors.
RETRYABLE_CHAIN_ERRORS = {"正文解析结果为空", "目录中没有可读取章节"}


def _error_text(exc: Exception, limit: int = 180) -> str:
    return str(exc or exc.__class__.__name__).strip()[:limit]


def _remaining(deadline: float, cap: float) -> float:
    """Stage timeout that never lets the source outlive its total budget."""
    return max(1.0, min(float(cap), deadline - time.perf_counter()))


def _error_kind(exc: Exception) -> str:
    message = _error_text(exc).lower()
    if isinstance(exc, SiteBlockedError):
        return "blocked"
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in message:
        return "timeout"
    if isinstance(exc, UnsupportedRule):
        return "unsupported"
    if isinstance(exc, JsBridgeError):
        return "js_error"
    if "429" in message or "rate limit" in message:
        return "rate_limited"
    if isinstance(exc, (aiohttp.ClientError, OSError)):
        return "network_error"
    return "rule_error"


def _source_uses_js(source) -> bool:
    values = [
        getattr(source, "header", ""), getattr(source, "searchUrl", ""),
        getattr(source, "exploreUrl", ""),
    ]
    for name in ("ruleSearch", "ruleExplore", "ruleBookInfo", "ruleToc",
                 "ruleContent"):
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
    engine = engine or LegadoReadingEngine(timeout=timeout)
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
            engine.js.close()


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


async def _reading_chain_check(source, engine, candidates, timeout,
                               budget=READING_CHAIN_BUDGET) -> dict:
    result = {"reading_status": "skip", "reading_stage": "none",
              "reading_error": None, "chapter_count": None,
              "sample_book": None, "sample_chapter": None,
              "reading_tried": 0}
    books = [item for item in candidates if getattr(item, "url", "")]
    if not books:
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
            readable = next((item for item in chapters
                             if not item.is_volume and getattr(item, "url", "")), None)
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
                        on_result=None, auto_disable=False):
    worker_limit = max(1, min(int(concurrency), CHECK_MAX_CONCURRENCY))
    network_sem = asyncio.Semaphore(worker_limit)
    engine_sem = asyncio.Semaphore(worker_limit)
    tag_sem = asyncio.Semaphore(min(worker_limit, TAG_SOURCE_CONCURRENCY))
    js_sem = asyncio.Semaphore(JS_CHECK_CONCURRENCY)
    connector = aiohttp.TCPConnector(
        ssl=False, limit=worker_limit, limit_per_host=3, ttl_dns_cache=300)
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) "
                             "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
    results = []

    async with aiohttp.ClientSession(
            connector=connector, headers=headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        browser_fetcher = available_fetcher()

        async def check_source(source, index):
            probe_task = asyncio.create_task(
                _probe_source(source, session, network_sem, timeout))
            # 缺口判定要走一次 JS 模拟面清单，放线程里跑，别卡住事件循环。
            java_gaps = asyncio.create_task(
                asyncio.to_thread(_unsupported_java_packages, source))
            engine = LegadoReadingEngine(timeout=timeout,
                                         webview_fetcher=browser_fetcher)
            limiter = js_sem if _source_uses_js(source) else engine_sem
            try:
                probe = await _locally_blocked_probe(probe_task, timeout)
                if probe is not None:
                    search, search_books = {
                        "search_status": "skip", "book_count": None,
                        "search_keyword": "", "search_method": "rule-engine",
                        "search_error": None}, []
                    tags, tag_timeout, tag_skipped = [], False, True
                    chain = {"reading_status": "skip", "reading_stage": "none",
                             "reading_error": None, "chapter_count": None,
                             "sample_book": None, "sample_chapter": None,
                             "reading_tried": 0}
                else:
                    async with limiter:
                        search, search_books = await _engine_search_check(
                            source, engine, timeout + 3)
                        deadline = time.perf_counter() + SOURCE_CHECK_BUDGET
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
                                                timeout=min(timeout, 10),
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
                        chain = await _reading_chain_check(
                            source, engine, candidates, READING_CHAIN_TIMEOUT,
                            budget=_remaining(deadline, READING_CHAIN_BUDGET))
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
                engine.js.close()
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
                result["disable_reason"] = "搜索、发现与阅读链路均明确不可用"
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
