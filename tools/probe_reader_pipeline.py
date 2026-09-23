"""离线量阅读页在慢请求下的事件循环延迟（真引擎 + 回环假书源）。

跑法：python tools/probe_reader_pipeline.py
"""
import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

from aiohttp import web

from app.models.book_source import BookSource
from app.services.rule_engine import BookItem, ChapterItem
from app.views.reading_view import ReadingView

DELAY = 2.0
PAGE = ("<html><body><div id='content'>{}</div></body></html>")


async def main():
    async def content(request):
        await asyncio.sleep(DELAY)
        name = request.match_info["n"]
        body = "<br>".join(f"第{i}行正文，足够长的一段文字用来切段。" for i in range(60))
        return web.Response(text=PAGE.format(f"<h3>第{name}章</h3>{body}"),
                            content_type="text/html")

    app = web.Application()
    app.router.add_get("/c/{n}", content)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    source = BookSource.from_dict({
        "bookSourceName": "probe", "bookSourceUrl": base, "enabled": True,
        "enabledCookieJar": False,
        "ruleContent": {"content": "#content@html"},
    })
    book = BookItem("book", url=f"{base}/book/1")
    first = ChapterItem("one", f"{base}/c/1", index=0)
    second = ChapterItem("two", f"{base}/c/2", index=1)

    with TemporaryDirectory() as directory:
        service = SimpleNamespace(
            data_dir=Path(directory), sources=[source], bookshelf=[],
            category_checks={})
        view = ReadingView(service)
        view.current_source = source
        view._current_book = book
        view.chapters = [first, second]
        view.update = lambda: None
        view._show_error = lambda exc, retry=None: print("错误：", repr(exc))
        view._reader.scroll_to = AsyncMock()
        service.update_bookshelf_state = lambda *a, **k: False
        page = SimpleNamespace(width=1000, platform=None,
                               run_task=lambda fn, *a: asyncio.ensure_future(fn(*a)))
        gaps = []

        async def ticker():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.1)

            started = time.perf_counter()
            await view._run_content(first)
            single = time.perf_counter() - started

            gaps.clear()
            started = time.perf_counter()
            stale = asyncio.ensure_future(view._run_content(first))
            await asyncio.sleep(0.3)
            await view._run_content(second)
            turn = time.perf_counter() - started
            await stale
            tick.cancel()

        cached = view.reading_cache.load_content(
            source.bookSourceUrl, book.url, first.url)
        print(f"单章 2s 请求：{single:.2f}s；循环最大间隔 "
              f"{max(gaps) * 1000:.0f}ms")
        print(f"请求中翻页：翻到第二章用了 {turn:.2f}s（含 0.3s 等待），"
              f"被顶掉的第一章落盘={'是' if cached else '否'}")
        await view.engine.aclose()
    await runner.cleanup()


asyncio.run(main())
