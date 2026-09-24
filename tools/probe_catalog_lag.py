"""量「加载目录」期间事件循环被卡多久，并把长阻塞归因到具体调用。

跑法：python -u tools/probe_catalog_lag.py [章节数]
- 心跳任务每 10ms 采一次事件循环，记录每次间隔的绝对起止时间；
- 开书链路的每一步（含引擎内部）都套上时间片；
- 每段窗口结束时打印：墙钟、阻塞合计、>80ms 的间隔各自的时刻与归属、
  以及时间片按名字汇总的耗时排行。
"""
import asyncio
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import PropertyMock, patch

from aiohttp import web

from app.models.book_source import BookSource
from app.services.rule_engine import BookItem
from app.services.source_service import SourceService
from app.views.reading_view import ReadingView

CHAPTERS = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
BIG_GAP = 0.08
T0 = time.perf_counter()


def stamp(moment):
    return moment - T0


def toc_page(count):
    items = "".join(
        f"<li><a href='/c/{i}'>{i}. 第{i}章 标题够长一点更像真实目录</a>"
        f"<span class='t'>2026-09-{i % 28 + 1:02d}</span></li>"
        for i in range(count))
    return f"<html><body><div id='info'><h1>书名</h1></div><ul id='list'>{items}</ul></body></html>"


class Spans:
    """每步的 (名字, 起, 止)；同一进程内 perf_counter 可直接互比。"""

    def __init__(self):
        self.records = []

    def add(self, name, t0, t1):
        self.records.append((name, t0, t1))

    def wrap_sync(self, owner, attr, name):
        original = getattr(owner, attr)

        def wrapper(*a, **k):
            t0 = time.perf_counter()
            try:
                return original(*a, **k)
            finally:
                self.add(name, t0, time.perf_counter())
        setattr(owner, attr, wrapper)

    def wrap_async(self, owner, attr, name):
        original = getattr(owner, attr)

        async def wrapper(*a, **k):
            t0 = time.perf_counter()
            try:
                return await original(*a, **k)
            finally:
                self.add(name, t0, time.perf_counter())
        setattr(owner, attr, wrapper)

    def cover(self, t0, t1):
        hits = []
        for name, s0, s1 in self.records:
            overlap = min(s1, t1) - max(s0, t0)
            if overlap > 0:
                hits.append((s1 - s0, overlap, name))
        return [(name, overlap / (t1 - t0)) for _, overlap, name in
                sorted(hits, reverse=True)[:3]]

    def drain(self, title):
        totals = {}
        for name, t0, t1 in self.records:
            total, count = totals.get(name, (0.0, 0))
            totals[name] = (total + t1 - t0, count + 1)
        print(f"  {title} 时间片排行（含嵌套，仅供参考顺序）：")
        for name, (total, count) in sorted(totals.items(),
                                           key=lambda kv: -kv[1][0])[:8]:
            print(f"    {total * 1000:8.1f}ms  ×{count:<5d} {name}")
        self.records.clear()


class Ticker:
    """10ms 采样事件循环，记录每次间隔的绝对起止时间与当时所处的阶段。"""

    def __init__(self):
        self.rows = []
        self.phase = "start"

    async def run(self):
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            self.rows.append((now - last, last, now, self.phase))
            last = now

    def mark(self, phase):
        self.phase = phase

    def reset(self):
        self.rows.clear()

    def blocked(self):
        return sum(gap for gap, *_ in self.rows if gap >= BIG_GAP)

    def drain(self, title, spans=None):
        worst = max(self.rows, key=lambda row: row[0], default=(0, 0, 0, "-"))
        print(f"  {title}：最大单次间隔 {worst[0] * 1000:.0f}ms"
              f"（阶段 {worst[3]}）")
        for gap, t0, t1, phase in self.rows:
            if gap < BIG_GAP:
                continue
            who = " ← ".join(
                f"{name}（占{share:.0%}）" for name, share in spans.cover(t0, t1)
            ) if spans else "?"
            print(f"    阻塞 {gap * 1000:6.0f}ms  {stamp(t0):7.2f}→{stamp(t1):7.2f}"
                  f"  阶段 {phase}\n        覆盖：{who or '无'}")
        self.rows.clear()


def instrument(view, spans):
    spans.wrap_async(view, "_run_chapters", "_run_chapters(任务)")
    spans.wrap_async(view.engine, "chapters", "engine.chapters")
    spans.wrap_async(view.engine, "_request_value", "engine._request_value")
    spans.wrap_async(view.engine, "_initialize_detail", "engine._initialize_detail")
    spans.wrap_async(view.engine, "_list_nodes", "engine._list_nodes")
    spans.wrap_sync(view.engine, "_apply_book_info", "engine._apply_book_info")
    spans.wrap_sync(view.engine, "_root", "engine._root")
    spans.wrap_sync(view.engine, "_strict_value", "engine._strict_value")
    spans.wrap_sync(view.engine, "_field_value", "engine._field_value")
    spans.wrap_sync(view.reading_cache, "load_catalog", "cache.load_catalog")
    spans.wrap_sync(view.reading_cache, "save_catalog", "cache.save_catalog")
    spans.wrap_sync(view.service, "add_to_bookshelf", "service.add_to_bookshelf")
    spans.wrap_sync(view.service, "update_bookshelf_state",
                    "service.update_bookshelf_state")
    spans.wrap_sync(view, "_apply_chapters", "view._apply_chapters")
    spans.wrap_sync(view, "_apply_directory_filter", "view._apply_directory_filter")
    spans.wrap_sync(view, "_append_chapter_controls", "view._append_chapter_controls")
    spans.wrap_sync(view, "_reset_book_session", "view._reset_book_session")
    spans.wrap_sync(view, "_show_book", "view._show_book")
    spans.wrap_sync(view, "_show_shelf_book_detail", "view._show_shelf_book_detail")
    spans.wrap_sync(view, "_set_busy", "view._set_busy")
    spans.wrap_sync(view, "_persist_scroll_position", "view._persist_scroll_position")


async def measure(label, view, source, book, ticker, spans, shelve):
    ticker.reset()
    ticker.mark(f"{label}·开书")
    started = time.perf_counter()
    view.open_book(source, book, shelve=shelve)
    task = view._book_open_task
    ticker.mark(f"{label}·等目录")
    while not task.done():
        await asyncio.sleep(0.02)
    elapsed = time.perf_counter() - started
    # 被饿住的心跳要等循环空闲才补记，先让出去一次再读，否则会漏报阻塞。
    await asyncio.sleep(0.05)
    print(f"{label}：开书到目录就绪 {elapsed:.2f}s"
          f"（循环阻塞 {ticker.blocked() * 1000:.0f}ms）")
    ticker.drain(f"{label} 循环", spans)
    spans.drain(f"{label}")


async def main():
    async def detail(request):
        return web.Response(text=toc_page(CHAPTERS), content_type="text/html")

    app = web.Application()
    app.router.add_get("/book/1", detail)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    source = BookSource.from_dict({
        "bookSourceName": "probe", "bookSourceUrl": base, "enabled": True,
        "enabledCookieJar": False,
        "ruleBookInfo": {"name": "#info h1@text"},
        "ruleToc": {"chapterList": "#list@li", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
    })

    with TemporaryDirectory() as directory:
        root = Path(directory)
        service = SourceService(root, data_dir=root / "data",
                                cache_dir=root / "cache")
        service.sources = [source]
        view = ReadingView(service)
        view.update = lambda: None
        view._chapters.update = lambda: None
        view._reader.scroll_to = lambda **kwargs: asyncio.sleep(0)
        page = type("Page", (), {"width": 1000, "platform": None,
                                 "run_task": staticmethod(
                                     lambda fn, *a: asyncio.ensure_future(fn(*a))),
                                 "show_dialog": staticmethod(lambda d: None),
                                 "pop_dialog": staticmethod(lambda: None)})()
        ticker = Ticker()
        spans = Spans()
        instrument(view, spans)
        meter = asyncio.create_task(ticker.run())

        async def chapters_only(mark, probe_book):
            ticker.reset()
            ticker.mark(mark)
            started = time.perf_counter()
            chapters = await view.engine.chapters(source, probe_book)
            wall = time.perf_counter() - started
            await asyncio.sleep(0.05)
            print(f"{mark}：{len(chapters)} 章 / {wall:.2f}s"
                  f"（循环阻塞 {ticker.blocked() * 1000:.0f}ms）")
            ticker.drain(f"{mark} 循环", spans)
            spans.drain(mark)
            return chapters

        with patch.object(ReadingView, "page", new_callable=PropertyMock,
                          return_value=page):
            try:
                await asyncio.sleep(0.2)
                view.current_source = source
                chapters = await chapters_only(
                    "① 首调 chapters", BookItem(
                        "书名", url=f"{base}/book/1",
                        origin=source.bookSourceUrl))

                ticker.reset()
                ticker.mark("② 只建目录控件")
                started = time.perf_counter()
                view._apply_chapters(BookItem("书名", url=f"{base}/book/1",
                                              origin=source.bookSourceUrl),
                                     chapters, "probe")
                wall = time.perf_counter() - started
                await asyncio.sleep(0.05)
                print(f"② _apply_chapters：{wall:.2f}s"
                      f"（循环阻塞 {ticker.blocked() * 1000:.0f}ms）")
                ticker.drain("② 循环", spans)
                spans.drain("②")

                await chapters_only("③ 再直调一次 chapters", BookItem(
                    "书名", url=f"{base}/book/1", origin=source.bookSourceUrl))

                for index, shelve in enumerate((True, False), 1):
                    book = BookItem("书名", url=f"{base}/book/1",
                                    origin=source.bookSourceUrl)
                    await measure(f"第{index}轮 shelve={shelve}", view, source,
                                  book, ticker, spans, shelve)
                    view.reading_cache.remove_book(source.bookSourceUrl, book.url)
            finally:
                meter.cancel()
                await view.engine.aclose()
    await runner.cleanup()


asyncio.run(main())
