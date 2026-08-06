"""供 Flet 调用的批量书源检测服务。"""
from __future__ import annotations

import asyncio
import aiohttp

from test_book_sources_async import test_one
from app.services.rule_engine import parse_explore_categories, _resolve_explore_url, UnsupportedRule


CHECK_CONCURRENCY = 50


async def check_categories(source, session, sem, timeout=8):
    """检查书源的每个发现分类；明确的 HTTP/解析错误会标记为无效。"""
    categories = parse_explore_categories(source)

    async def check_one(category):
        item = {"title": category.title, "url": category.url, "valid": True,
                "status_code": None, "error": None}
        try:
            url = _resolve_explore_url(category.url, source.bookSourceUrl, 1)
            async with sem:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                       ssl=False, allow_redirects=True) as response:
                    item["status_code"] = response.status
                    item["valid"] = response.status < 400
                    if not item["valid"]:
                        item["error"] = f"HTTP {response.status}"
        except UnsupportedRule as exc:
            item["valid"] = False
            item["error"] = str(exc)
        except asyncio.TimeoutError:
            # 超时可能只是站点慢，不据此删除标签。
            item["error"] = "检测超时（暂时保留）"
        except aiohttp.ClientResponseError as exc:
            item["status_code"] = exc.status
            item["valid"] = False
            item["error"] = f"HTTP {exc.status}"
        except Exception as exc:
            # 临时网络故障不应造成误删。
            item["error"] = str(exc)[:80]
        return item

    return await asyncio.gather(*(check_one(category) for category in categories))


async def check_sources(sources, concurrency=CHECK_CONCURRENCY, timeout=12, on_result=None):
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}
    results = []
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async def check_source(source, index):
            result = await test_one(source.to_dict(), index, session, sem, timeout)
            if result["status"] == "ok":
                result["category_results"] = await check_categories(source, session, sem)
            else:
                result["category_results"] = []
            return result

        tasks = [check_source(source, i + 1) for i, source in enumerate(sources)]
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            if on_result:
                on_result(result, len(results), len(tasks))
    return sorted(results, key=lambda r: r["idx"])
