"""Concurrent, layered validation for Legado book sources."""
from __future__ import annotations

import asyncio
import json
import re
import aiohttp

from test_book_sources_async import (classify_js_bridge_error, parse_search_url,
                                     test_one)
from app.services.legado_js import JsBridgeError
from app.services.rule_engine import (UnsupportedRule, _resolve_explore_url,
                                      resolve_explore_categories, select_nodes)
from app.services.reading_engine import LegadoReadingEngine


CHECK_CONCURRENCY = 80
TAG_PAGE_CONCURRENCY = 80
JS_CHECK_CONCURRENCY = 4
SOURCE_TAG_TIMEOUT = 60
# The CPython quickjs extension can abort the whole Windows process when a
# Python bridge callback fails inside a large, cancellable batch.  Bulk checks
# must never take the desktop app down, so native JS rules are reported as
# needing interactive verification.  JS remains available in the reader,
# where execution is scoped to one user action.
SAFE_BULK_CHECK_NO_NATIVE_JS = True


async def _engine_search_check(source, timeout: int) -> dict:
    """Run JavaScript-dependent searches through the real Python rule engine."""
    engine = LegadoReadingEngine(timeout=timeout)
    keyword = str((getattr(source, "ruleSearch", {}) or {}).get(
        "checkKeyWord") or "我的")
    try:
        books = await asyncio.wait_for(engine.search(source, keyword), timeout=timeout + 3)
        return {"search_status": "ok" if books else "no_results",
                "book_count": len(books), "search_keyword": keyword,
                "search_method": "python-legado-engine", "search_error": None}
    except UnsupportedRule as exc:
        return {"search_status": "js_needed", "book_count": None,
                "search_keyword": keyword, "search_method": "interactive-required",
                "search_error": str(exc)}
    except JsBridgeError as exc:
        return {"search_status": classify_js_bridge_error(str(exc)),
                "book_count": None, "search_keyword": keyword,
                "search_method": "python-legado-engine",
                "search_error": str(exc)[:240]}
    except asyncio.TimeoutError:
        return {"search_status": "timeout", "book_count": None,
                "search_keyword": keyword, "search_method": "python-legado-engine",
                "search_error": "TIMEOUT"}
    except Exception as exc:
        return {"search_status": "error", "book_count": None,
                "search_keyword": keyword, "search_method": "python-legado-engine",
                "search_error": str(exc)[:160]}
    finally:
        engine.js.close()


def _source_headers(source) -> dict[str, str]:
    raw = getattr(source, "header", "") or ""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    try:
        value = json.loads(raw)
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _category_request(source, raw_url: str, page: int) -> dict:
    """Parse GET/POST category URLs with the same grammar as searchUrl."""
    if re.search(r"<[^<>]*,[^<>]*>", raw_url):
        return {"url": _resolve_explore_url(raw_url, source.bookSourceUrl, page),
                "method": "GET", "body": None, "headers": {}}
    request = parse_search_url(raw_url, source.bookSourceUrl, "", page)
    if request:
        return request
    return {"url": _resolve_explore_url(raw_url, source.bookSourceUrl, page),
            "method": "GET", "body": None, "headers": {}}


async def _fetch_tag_page(source, category, page, session, global_sem, source_sem,
                          timeout, get_engine=None, prepared_rules=None,
                          prepared_headers=None) -> dict:
    result = {"page": page, "valid": False, "status_code": None,
              "item_count": 0, "content_type": "", "error": None,
              "attempts": 0, "persistent_rate_limit": False}
    try:
        rules = prepared_rules
        if rules is None:
            rules = dict(getattr(source, "ruleSearch", None) or {})
            rules.update({k: v for k, v in
                          (getattr(source, "ruleExplore", None) or {}).items() if v})
        dynamic = any((
            str(getattr(source, "header", "") or "").strip().startswith(("@js:", "<js>")),
            str(category.url or "").strip().startswith(("@js:", "<js>")),
            "java." in str(category.url or "") or "source." in str(category.url or ""),
            str(rules.get("bookList", "")).strip().startswith(("@js:", "<js>")),
        ))
        if dynamic and get_engine is not None:
            if SAFE_BULK_CHECK_NO_NATIVE_JS:
                result["error"] = "JS 规则已跳过（请在阅读页单独验证）"
                return result
            engine = await get_engine()
            async with global_sem, source_sem:
                books = await asyncio.wait_for(
                    engine.explore(source, category, page), timeout=timeout + 3)
            result.update(valid=bool(books), item_count=len(books), attempts=1,
                          content_type="engine")
            if not books:
                result["error"] = "规则引擎解析结果为 0"
            return result
        request = _category_request(source, category.url, page)
        headers = dict(prepared_headers) if prepared_headers is not None else _source_headers(source)
        request_headers = request.get("headers") or {}
        if isinstance(request_headers, str):
            try: request_headers = json.loads(request_headers)
            except json.JSONDecodeError: request_headers = {}
        if isinstance(request_headers, dict):
            headers.update({str(k): str(v) for k, v in request_headers.items()})
        method = str(request.get("method") or "GET").upper()
        kwargs = {"headers": headers, "ssl": False, "allow_redirects": True,
                  "timeout": aiohttp.ClientTimeout(total=timeout)}
        if method == "POST":
            kwargs["data"] = request.get("body") or ""
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        body = ""
        final_url = request["url"]
        for attempt in range(3):
            result["attempts"] = attempt + 1
            async with global_sem, source_sem:
                async with session.request(method, request["url"], **kwargs) as response:
                    result["status_code"] = response.status
                    result["content_type"] = response.headers.get("Content-Type", "")
                    final_url = str(response.url)
                    body = await response.text(errors="replace")
                    retry_after = response.headers.get("Retry-After", "")
            if result["status_code"] != 429:
                break
            if attempt < 2:
                try: delay = min(float(retry_after), 8.0)
                except (TypeError, ValueError): delay = 1.5 * (attempt + 1)
                await asyncio.sleep(delay)
        if result["status_code"] >= 400:
            if result["status_code"] == 429:
                result["persistent_rate_limit"] = True
                result["error"] = "HTTP 429（重试 3 次后仍被限流）"
            else:
                result["error"] = f"HTTP {result['status_code']}"
            return result
        list_rule = rules.get("bookList", "")
        nodes, _ = select_nodes(body, result["content_type"], list_rule)
        result["item_count"] = len(nodes)
        result["valid"] = len(nodes) > 0
        if not result["valid"] and get_engine is not None:
            engine = await get_engine()
            async with global_sem, source_sem:
                books = await asyncio.wait_for(
                    engine.explore_document(
                        source, body, result["content_type"], final_url, page),
                    timeout=timeout + 3)
            result["item_count"] = len(books)
            result["valid"] = bool(books)
            if books:
                result["error"] = None
                result["content_type"] = "engine-fallback"
        if not result["valid"]:
            result["error"] = "列表规则匹配结果为 0"
    except UnsupportedRule as exc:
        result["error"] = f"UNSUPPORTED: {exc}"
    except asyncio.TimeoutError:
        result["error"] = "TIMEOUT"
    except aiohttp.ClientResponseError as exc:
        result["status_code"] = exc.status
        result["error"] = f"HTTP {exc.status}"
    except (aiohttp.ClientError, OSError) as exc:
        result["error"] = f"NETWORK: {str(exc)[:100]}"
    except Exception as exc:
        result["error"] = f"PARSE: {str(exc)[:120]}"
    return result


async def check_categories(source, session, sem, timeout=10):
    """Validate every tag and, when paged, validate its second page as well."""
    # Static category definitions need no rule engine.  Most sources finish on
    # the fast parser, so defer the comparatively expensive engine allocation
    # until a fallback is actually required.
    categories = await resolve_explore_categories(source)
    engine = None
    engine_lock = asyncio.Lock()

    async def get_engine():
        nonlocal engine
        if engine is None:
            async with engine_lock:
                if engine is None:
                    engine = LegadoReadingEngine(timeout=timeout)
        return engine

    rules = dict(getattr(source, "ruleSearch", None) or {})
    rules.update({k: v for k, v in
                  (getattr(source, "ruleExplore", None) or {}).items() if v})
    headers = _source_headers(source)

    # Keep high concurrency across different sources, but never burst several
    # tag/page requests at the same site. This is the main trigger for 429s.
    source_sem = asyncio.Semaphore(1)

    async def check_one(category):
        pages = ([1, 2] if ("{{page" in category.url or
                            re.search(r"<[^<>]*,[^<>]*>", category.url)) else [1])
        page_results = await asyncio.gather(*(
            _fetch_tag_page(source, category, page, session, sem, source_sem,
                            timeout, get_engine, rules, headers)
            for page in pages
        ))
        first = page_results[0]
        valid = first["valid"]
        # Page 2 diagnoses lazy loading but does not invalidate a working page 1.
        pagination_valid = page_results[1]["valid"] if len(page_results) > 1 else None
        return {
            "title": category.title,
            "url": category.url,
            "valid": valid,
            "status_code": first["status_code"],
            "item_count": first["item_count"],
            "error": first["error"],
            "pagination_valid": pagination_valid,
            "page_results": page_results,
        }

    try:
        return await asyncio.gather(*(check_one(category) for category in categories))
    finally:
        if engine is not None:
            engine.js.close()


async def check_sources(sources, concurrency=CHECK_CONCURRENCY, timeout=12,
                        on_result=None, auto_disable=False):
    source_sem = asyncio.Semaphore(concurrency)
    tag_sem = asyncio.Semaphore(max(concurrency, TAG_PAGE_CONCURRENCY))
    js_sem = asyncio.Semaphore(JS_CHECK_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=max(concurrency, TAG_PAGE_CONCURRENCY),
                                     limit_per_host=4, ttl_dns_cache=300)
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
    results = []
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(connector=connector, headers=headers,
                                     timeout=timeout_config,
                                     cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        async def check_source(source, index):
            search_rule = source.searchUrl or ""
            uses_js = (search_rule.strip().startswith(("@js:", "<js>")) or
                       "java." in search_rule or "source." in search_rule)
            source_data = source.to_dict()
            book_list_uses_js = str((source.ruleSearch or {}).get(
                "bookList") or "").strip().startswith(("@js:", "<js>"))
            if SAFE_BULK_CHECK_NO_NATIVE_JS and book_list_uses_js:
                source_data = dict(source_data)
                source_data["ruleSearch"] = dict(source.ruleSearch or {})
                source_data["ruleSearch"]["bookList"] = ""
            # Category validation is independent from connectivity/search.
            # Start it now so network waits overlap instead of adding together.
            dynamic_explore = str(source.exploreUrl or "").strip().startswith(
                ("@js:", "<js>"))
            tag_check_js_skipped = bool(
                SAFE_BULK_CHECK_NO_NATIVE_JS and dynamic_explore)
            category_task = None
            category_task_start = 0.0
            if (source.enabledExplore and source.exploreUrl and
                    not tag_check_js_skipped):
                category_task_start = asyncio.get_running_loop().time()
                category_task = asyncio.create_task(check_categories(
                    source, session, tag_sem, min(timeout, 8)))
            if uses_js:
                async with js_sem:
                    result = await test_one(source_data, index, session,
                                            source_sem, timeout)
            else:
                result = await test_one(source_data, index, session,
                                        source_sem, timeout)
            if SAFE_BULK_CHECK_NO_NATIVE_JS and (uses_js or book_list_uses_js):
                result.update(search_status="js_needed", book_count=None,
                              search_method="interactive-required",
                              search_error="批量检测已跳过原生 JS，请在阅读页单独验证")
            elif uses_js and result.get("search_status") in {
                    "js_needed", "parse_error", "error", "no_results"}:
                async with js_sem:
                    result.update(await _engine_search_check(source, timeout))
            # Tag validation is independent: a source can have broken search but valid explore.
            result["tag_check_timeout"] = False
            result["tag_check_js_skipped"] = tag_check_js_skipped
            if category_task is not None:
                try:
                    elapsed = category_task.get_loop().time() - category_task_start
                    remaining = max(0.001, SOURCE_TAG_TIMEOUT - elapsed)
                    result["category_results"] = await asyncio.wait_for(
                        category_task, timeout=remaining)
                except asyncio.TimeoutError:
                    result["category_results"] = []
                    result["tag_check_timeout"] = True
            else:
                result["category_results"] = []
            tags = result["category_results"]
            result["tag_total"] = len(tags)
            result["tag_valid"] = sum(1 for item in tags if item["valid"])
            result["tag_invalid"] = result["tag_total"] - result["tag_valid"]
            search_status = result.get("search_status")
            search_usable = search_status == "ok"
            explore_usable = result["tag_valid"] > 0
            result["usable"] = search_usable or explore_usable
            uncertain_search = (search_status in {"js_needed", "timeout", "no_results", "skip", None} or
                                "429" in str(result.get("error") or "") or
                                "429" in str(result.get("search_method") or ""))
            uncertain_tags = any(
                item.get("error") == "TIMEOUT" or
                str(item.get("error") or "").startswith("NETWORK:")
                for item in tags
            ) or result["tag_check_timeout"]
            persistent_rate_limit = (result["tag_valid"] == 0 and any(
                page.get("persistent_rate_limit")
                for item in tags for page in item.get("page_results", [])
            ))
            if result.get("status") == "err":
                result["check_state"] = "unusable"
            elif result["usable"]:
                result["check_state"] = "usable"
            elif persistent_rate_limit:
                result["check_state"] = "unusable"
            elif uncertain_search or uncertain_tags:
                result["check_state"] = "unknown"
            else:
                result["check_state"] = "unusable"
            result["auto_disabled"] = False
            if (auto_disable and result["check_state"] == "unusable" and
                    source.enabled):
                source.enabled = False
                result["auto_disabled"] = True
                if result["status"] != "ok":
                    result["disable_reason"] = f"服务器不可用：{result.get('error') or result['status']}"
                elif persistent_rate_limit:
                    result["disable_reason"] = "HTTP 429：降低并发并重试 3 次后仍持续限流"
                elif result.get("search_status"):
                    result["disable_reason"] = f"搜索不可用：{result['search_status']}，且无有效标签"
                else:
                    result["disable_reason"] = "没有可用的搜索或分类标签"
            return result

        queue = asyncio.Queue()
        for i, source in enumerate(sources):
            queue.put_nowait((source, i + 1))

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
                        "search_status": "error", "error": str(exc)[:160],
                        "category_results": [], "tag_total": 0, "tag_valid": 0,
                        "tag_invalid": 0, "usable": False, "check_state": "unknown",
                        "auto_disabled": False, "tag_check_timeout": False,
                    }
                finally:
                    queue.task_done()
                results.append(result)
                if on_result:
                    on_result(result, len(results), len(sources))

        workers = [asyncio.create_task(worker())
                   for _ in range(min(concurrency, len(sources)))]
        await asyncio.gather(*workers)
    return sorted(results, key=lambda result: result["idx"])
