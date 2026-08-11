"""Rule-engine-driven, layered validation for Legado book sources."""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from app.services.legado_js import JsBridgeError
from app.services.rule_engine import UnsupportedRule, resolve_explore_categories
from app.services.reading_engine import LegadoReadingEngine


CHECK_CONCURRENCY = 8
TAG_PAGE_CONCURRENCY = 4
JS_CHECK_CONCURRENCY = 2
SOURCE_TAG_TIMEOUT = 60
READING_CHAIN_TIMEOUT = 30


def _error_text(exc: Exception, limit: int = 180) -> str:
    return str(exc or exc.__class__.__name__).strip()[:limit]


def _error_kind(exc: Exception) -> str:
    message = _error_text(exc).lower()
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
                await response.content.read(1024)
                elapsed = (time.perf_counter() - started) * 1000
                status = "ok" if response.status < 500 else "err"
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


async def check_categories(source, session=None, sem=None, timeout=10,
                           engine=None):
    """Validate explore categories with the same engine used by real reading."""
    del session  # Retained for API compatibility with older callers/tests.
    own_engine = engine is None
    engine = engine or LegadoReadingEngine(timeout=timeout)
    categories = await resolve_explore_categories(source, runtime=engine.js)
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
        return output
    finally:
        if own_engine:
            engine.js.close()


async def _reading_chain_check(source, engine, candidates, timeout: int) -> dict:
    result = {"reading_status": "skip", "reading_stage": "none",
              "reading_error": None, "chapter_count": None,
              "sample_book": None, "sample_chapter": None}
    book = next((item for item in candidates if getattr(item, "url", "")), None)
    if book is None:
        return result
    result["sample_book"] = str(getattr(book, "name", "") or "")
    try:
        result["reading_stage"] = "detail"
        book = await asyncio.wait_for(engine.book_info(source, book), timeout=timeout)
        result["sample_book"] = str(getattr(book, "name", "") or result["sample_book"])
        result["reading_stage"] = "catalog"
        chapters = await asyncio.wait_for(engine.chapters(source, book), timeout=timeout)
        readable = next((item for item in chapters
                         if not item.is_volume and getattr(item, "url", "")), None)
        result["chapter_count"] = len([item for item in chapters if not item.is_volume])
        if readable is None:
            raise RuntimeError("目录中没有可读取章节")
        result["sample_chapter"] = str(readable.name or "")
        result["reading_stage"] = "content"
        document = await asyncio.wait_for(
            engine.content_document(source, readable, need_save=False), timeout=timeout)
        if not _has_readable_content(document):
            raise RuntimeError("正文解析结果为空")
        result.update(reading_status="ok", reading_stage="complete")
    except Exception as exc:
        result["reading_status"] = _error_kind(exc)
        result["reading_error"] = _error_text(exc)
    return result


def _classify_result(result: dict) -> str:
    search_ok = result.get("search_status") == "ok"
    explore_ok = result.get("tag_valid", 0) > 0
    reading = result.get("reading_status")
    uncertain = {"timeout", "rate_limited", "network_error", "js_error",
                 "unsupported"}
    if reading == "ok":
        return "fully_usable"
    if reading in uncertain:
        return "unknown"
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
    worker_limit = max(1, min(int(concurrency), CHECK_CONCURRENCY))
    network_sem = asyncio.Semaphore(worker_limit)
    engine_sem = asyncio.Semaphore(min(worker_limit, TAG_PAGE_CONCURRENCY))
    js_sem = asyncio.Semaphore(JS_CHECK_CONCURRENCY)
    connector = aiohttp.TCPConnector(
        ssl=False, limit=worker_limit, limit_per_host=3, ttl_dns_cache=300)
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) "
                             "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
    results = []

    async with aiohttp.ClientSession(
            connector=connector, headers=headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        async def check_source(source, index):
            probe_task = asyncio.create_task(
                _probe_source(source, session, network_sem, timeout))
            engine = LegadoReadingEngine(timeout=timeout)
            limiter = js_sem if _source_uses_js(source) else engine_sem
            try:
                async with limiter:
                    search, search_books = await _engine_search_check(
                        source, engine, timeout + 3)
                    tags, tag_timeout = [], False
                    if (getattr(source, "enabledExplore", True) and
                            str(getattr(source, "exploreUrl", "") or "").strip()):
                        try:
                            tags = await asyncio.wait_for(
                                check_categories(source, timeout=min(timeout, 10),
                                                 engine=engine),
                                timeout=SOURCE_TAG_TIMEOUT)
                        except asyncio.TimeoutError:
                            tag_timeout = True
                    candidates = list(search_books)
                    for tag in tags:
                        candidates.extend(tag.pop("books", []))
                        for page in tag.get("page_results", []):
                            page.pop("books", None)
                    chain = await _reading_chain_check(
                        source, engine, candidates, READING_CHAIN_TIMEOUT)
                result = {"idx": index, "name": source.bookSourceName,
                          "raw_url": source.bookSourceUrl, **await probe_task,
                          **search, **chain, "category_results": tags,
                          "tag_check_timeout": tag_timeout,
                          "tag_check_js_skipped": False}
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
