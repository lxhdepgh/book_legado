#!/usr/bin/env python3
"""Batch-check real Legado search -> detail -> TOC -> content chains."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from app.models.book_source import BookSource
from app.services.reading_engine import LegadoReadingEngine


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def category(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "network"
    if type(exc).__name__ == "ClientResponseError":
        return "network"
    text = str(exc).lower()
    if any(value in text for value in (
            "interactive browser", "webview", "startbrowser",
            "verification", "captcha")):
        return "interactive"
    if any(value in text for value in (
            "timeout", "cannot connect", "connection", "dns", "ssl",
            "连接失败", "clientresponseerror", "forbidden", "not found",
            "bad request", "internal server error", "method not allowed",
            "http 4", "http 5",
            "server disconnected")):
        return "network"
    return "engine_or_rule"


async def check_source(index: int, source: BookSource, timeout: float) -> dict:
    engine = LegadoReadingEngine()
    keyword = str((source.ruleSearch or {}).get("checkKeyWord") or "斗破苍穹")
    result = {"index": index, "source": source.bookSourceName,
              "keyword": keyword, "stage": "search"}
    if not str(source.searchUrl or "").strip():
        result["status"] = "source_config"
        result["error"] = "missing searchUrl"
        await engine.aclose()
        return result
    try:
        books = await asyncio.wait_for(engine.search(source, keyword), timeout)
        result["books"] = len(books)
        if not books:
            result["status"] = "no_results"
            return result
        book = books[0]
        result["stage"] = "detail"
        await asyncio.wait_for(engine.book_info(source, book), timeout)
        result["stage"] = "toc"
        chapters = await asyncio.wait_for(engine.chapters(source, book), timeout)
        result["chapters"] = len(chapters)
        interaction_rules = json.dumps(
            {"book": book.url, "toc": source.ruleToc or {},
             "content": source.ruleContent or {}},
            ensure_ascii=False).lower()
        requires_interaction = any(marker in interaction_rules for marker in (
            "webview", "startbrowser"))
        if not chapters and requires_interaction:
            result["status"] = "interactive"
            result["error"] = "empty TOC requires webView"
            return result
        readable = next((chapter for chapter in chapters
                         if not chapter.is_volume and chapter.url), None)
        if readable is None:
            result["status"] = "no_chapters"
            return result
        result["stage"] = "content"
        document = await asyncio.wait_for(
            engine.content_document(source, readable), timeout)
        result["blocks"] = len(document.blocks)
        result["text_chars"] = len(document.plain_text)
        if document.blocks:
            result["status"] = "ok"
        elif requires_interaction:
            result["status"] = "interactive"
            result["error"] = "empty content requires webView"
        else:
            result["status"] = "empty_content"
    except Exception as exc:  # A batch runner must report and continue.
        status = category(exc)
        if status == "engine_or_rule" and "startbrowser" in json.dumps(
                {"search": source.searchUrl, "book": source.ruleBookInfo or {},
                 "toc": source.ruleToc or {}, "content": source.ruleContent or {}},
                ensure_ascii=False).lower():
            status = "interactive"
        result["status"] = status
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        await engine.aclose()
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="书源 JSON 路径")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output")
    args = parser.parse_args()

    sources = BookSource.from_json(Path(args.source))
    selected = list(enumerate(
        sources[args.offset:args.offset + args.limit], start=args.offset + 1))
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def limited(index, source):
        async with semaphore:
            value = await check_source(index, source, args.timeout)
            print(json.dumps(value, ensure_ascii=False), flush=True)
            return value

    results = await asyncio.gather(*(limited(i, s) for i, s in selected))
    summary = Counter(value["status"] for value in results)
    report = {"offset": args.offset, "limit": len(selected),
              "summary": dict(summary), "results": results}
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": dict(summary)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
