"""离线本地假书源端到端链路（书源导入 → 搜索 → 分类浏览 → 开书 → 正文）。

用户报的"搜索/阅读/书源链路出错"跨了三层：SourceService 落库、ReadingService 编排、
RuleEngineService/引擎解析。逐层用替身各测各的，接起来仍然可能断，所以这里把
**真实服务 + 真实引擎**串起来跑一遍；网络全部指向 127.0.0.1 的一次性 aiohttp 假源，
零外网（批量访问真实书源会被杀软拦截，见记忆 legodo-live-scan-av-block）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.reading.reading_service import ReadingService
from app.services.reading_cache import ReadingCache
from app.services.sources.source_service import SourceService

SEARCH_HTML = """
<ul class="list">
  <li><a href="/book/1.html">秘境测试</a><i>作者甲</i></li>
  <li><a href="/book/2.html">秘境测试贰</a><i>作者乙</i></li>
</ul>
"""
BOOK_HTML = """
<h1>秘境测试</h1><div class="intro">一本用于离线链路验证的书。</div>
<a class="toc" href="/toc/1.html">目录</a>
"""
TOC_HTML = """
<dl><dd><a href="/ch/1.html">第一章 起</a></dd>
<dd><a href="/ch/2.html">第二章 承</a></dd></dl>
"""
CONTENT_HTML = """
<div id="content"><p>第一章第一段正文。</p><p>第一章第二段正文。</p></div>
"""


def _source_json(base: str) -> str:
    return json.dumps([{
        "bookSourceName": "本地假源",
        "bookSourceUrl": base,
        "bookSourceType": 0,
        "enabled": True,
        "enabledExplore": True,
        "searchUrl": base + "/search?q={{key}}",
        "exploreUrl": "玄幻奇幻::" + base + "/list?page={{page}}",
        "ruleSearch": {"bookList": "ul.list li", "name": "a@text",
                       "bookUrl": "a@href", "author": "i@text"},
        "ruleExplore": {"bookList": "ul.list li", "name": "a@text",
                        "bookUrl": "a@href"},
        "ruleBookInfo": {"name": "h1@text", "intro": ".intro@text",
                         "tocUrl": "a.toc@href"},
        "ruleToc": {"chapterList": "dl dd", "chapterName": "a@text",
                    "chapterUrl": "a@href"},
        "ruleContent": {"content": "#content@html"},
    }], ensure_ascii=False)


def _index_handler(log: list[str]):
    """假源路由：把来路记进 ``log``——后台预取到底发了什么，只有服务端数得出来。"""

    async def _index(request):
        log.append(request.path)
        page = request.path.strip("/") or "search"
        if page.startswith("book"):
            return web.Response(text=BOOK_HTML, content_type="text/html")
        if page.startswith("toc"):
            return web.Response(text=TOC_HTML, content_type="text/html")
        if page.startswith("ch"):
            return web.Response(text=CONTENT_HTML, content_type="text/html")
        return web.Response(text=SEARCH_HTML, content_type="text/html")

    return _index


class OfflineSourceChainTests(unittest.IsolatedAsyncioTestCase):
    """一条链：导入的书源能被搜到、能浏览分类、能开书读到正文。"""

    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="chain_"))
        os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = str(self.root / "data")
        os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = str(self.root / "cache")

        self.storage = StorageService()
        await self.storage.start()
        self.settings = SettingsService(storage=self.storage)
        await self.settings.on_start()

        self.engine = RuleEngineService(state_dir=self.root / "engine")
        await self.engine.on_start()

        self.source_service = SourceService(
            resource_dir=self.root / "resource", storage=self.storage,
            settings=self.settings, engine=self.engine)
        await self.source_service.on_start()

        self.reading = ReadingService(
            rule_engine=self.engine, storage=self.storage,
            settings=self.settings, source_service=self.source_service)
        await self.reading.on_start()

        app = web.Application()
        self.requests: list[str] = []
        app.router.add_get("/{tail:.*}", _index_handler(self.requests))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()
        await self.reading.on_stop()
        await self.engine.on_stop()
        await self.settings.on_stop()
        await self.storage.stop()
        for key in ("LEGADO_BOOKSOURCE_DATA_DIR", "LEGADO_BOOKSOURCE_CACHE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ #
    async def test_import_then_search_browse_read(self):
        count, errors = await self.source_service.import_sources_json(
            _source_json(self.base))
        self.assertEqual(count, 1, f"导入失败：{errors}")

        sources = await self.source_service.list_sources()
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertTrue(source.enabled)

        pool = ReadingService.searchable_sources(sources)
        self.assertEqual([s.bookSourceUrl for s in pool], [self.base])

        hits = await self.reading.search_books("秘境", pool, search_id=1)
        self.assertEqual([book.name for book in hits],
                         ["秘境测试", "秘境测试贰"])
        self.assertTrue(hits[0].url.startswith(self.base))
        self.assertEqual(hits[0].origin, self.base)

        categories = await self.reading.explore_categories(source)
        self.assertEqual([c["title"] for c in categories], ["玄幻奇幻"])
        browsed = await self.reading.explore_books(source, categories[0], page=1)
        self.assertTrue(browsed)
        # 引擎要的是 ExploreCategory 对象：传 url 字符串会让整条分类浏览崩在引擎里
        from app.services.engine.rule_engine import ExploreCategory
        as_object = await self.reading.explore_books(
            source, ExploreCategory("玄幻奇幻", categories[0]["url"]), page=1)
        self.assertTrue(as_object)

        book = await self._searched_book()
        session = await self.reading.open_book(source, book, shelve=False)
        self.assertEqual([c.name for c in session.chapters],
                         ["第一章 起", "第二章 承"])

        document = await self.reading.load_content(session.chapters[0])
        text = "".join(block.value for block in document.blocks
                       if block.kind == "text")
        self.assertIn("第一章第一段正文", text)

    async def test_prefetch_reads_ahead_so_the_chapter_end_does_not_wait(self):
        """真引擎 + 本地假源：挂上第一章后第二章在后台就被取回，翻到时不再发请求。

        替身引擎只能证明"调用序列对"，证明不了预取真的走通了规则解析与连接复用——
        这一条把整条链（引擎、会话、缓存）串起来数服务端的请求。
        """
        await self.source_service.import_sources_json(_source_json(self.base))
        book = await self._searched_book()
        source = (await self.source_service.list_sources())[0]
        session = await self.reading.open_book(source, book, shelve=False)
        self.assertEqual([c.name for c in session.chapters],
                         ["第一章 起", "第二章 承"])

        self.requests.clear()
        await self.reading.load_content(session.chapters[0])
        await self.reading.prefetch_chapters(session.chapters[0])

        for _ in range(100):
            if any(path.startswith("/ch/2") for path in self.requests):
                break
            await asyncio.sleep(0.05)
        self.assertTrue(any(path.startswith("/ch/2") for path in self.requests),
                        f"预取没把第二章拉回来：{self.requests}")

        arrived = list(self.requests)
        document = await self.reading.load_content(session.chapters[1], append=True)
        text = "".join(block.value for block in document.blocks
                       if block.kind == "text")
        self.assertIn("第一段正文", text)
        self.assertEqual(self.requests, arrived,
                         "预取到的章节被翻到时又发了一次请求")

    async def test_staged_trial_book_promoted_when_shelved(self):
        """试读（notShelf）只暂存：书架看不到，点"加入书架"才转正入架。"""
        await self.source_service.import_sources_json(_source_json(self.base))
        book = await self._searched_book()
        source = (await self.source_service.list_sources())[0]

        await self.reading.open_book(source, book, shelve=False)
        self.assertEqual(await self.source_service.list_bookshelf(), [])
        staged = await self.source_service.list_bookshelf(include_staged=True)
        self.assertEqual([item.get("name") for item in staged], [book.name])

        # 同键已有暂存行：入架动作要把它转正，而不是当重复被拒（点了没反应）
        self.assertTrue(await self.source_service.shelve_book(source, book))
        shelf = await self.source_service.list_bookshelf()
        self.assertEqual([item.get("name") for item in shelf], [book.name])
        # 已在架再点是幂等的：不再产生第二行，也不报"新增"
        self.assertFalse(await self.source_service.shelve_book(source, book))
        self.assertEqual(len(await self.source_service.list_bookshelf()), 1)
        self.assertFalse(await self.source_service.promote_book(
            source.bookSourceUrl, book.url))

    async def test_exit_trial_book_promote_keeps_progress(self):
        """试读退出选「放入书架」：原地转正，进度与目录缓存都留着。"""
        await self.source_service.import_sources_json(_source_json(self.base))
        book = await self._searched_book()
        source = (await self.source_service.list_sources())[0]
        session = await self.reading.open_book(source, book, shelve=False)

        record = self.reading.staged_record()
        self.assertIsNotNone(record, "试读会话应能回拿到暂存记录（退出时要问一声）")
        self.assertEqual(record.get("name"), book.name)

        await self.reading.save_progress(1, session.position)
        self.assertTrue(await self.reading.promote_staged_book())
        shelf = await self.source_service.list_bookshelf()
        self.assertEqual([item.get("name") for item in shelf], [book.name])
        # 转正后不该再被问，也不该把进度丢掉
        self.assertIsNone(self.reading.staged_record())
        self.assertIsNotNone(await self.reading.restore_position(book))
        self.assertTrue(await self.reading.load_chapters(book))

    async def test_exit_trial_book_discard_clears_progress_and_cache(self):
        """试读退出选「不放入」：记录、books 表进度行、目录缓存一起清掉。"""
        await self.source_service.import_sources_json(_source_json(self.base))
        book = await self._searched_book()
        source = (await self.source_service.list_sources())[0]
        session = await self.reading.open_book(source, book, shelve=False)
        await self.reading.save_progress(1, session.position)
        cache = ReadingCache(self.storage.paths.cache)
        self.assertTrue(cache.load_catalog(source.bookSourceUrl, book.url),
                        "开书时应已落目录缓存")

        # 收尾顺序与阅读器控制器一致：先按当前会话删干净，再关会话
        self.assertTrue(await self.reading.discard_staged_book())
        await self.reading.close_book(save_progress=False)

        self.assertEqual(
            await self.source_service.list_bookshelf(include_staged=True), [],
            "暂存记录没删掉")
        self.assertIsNone(self.storage.db.execute(
            "SELECT 1 FROM books WHERE book_url=?", (book.url,)).fetchone(),
            "进度行残留在 books 表：删了记录等于没删")
        self.assertIsNone(cache.load_catalog(source.bookSourceUrl, book.url),
                          "目录缓存没跟着清")

    async def _searched_book(self):
        """走一遍真实搜索，拿第一条结果当待读的书（快照即开书入参）。"""
        pool = ReadingService.searchable_sources(
            await self.source_service.list_sources())
        hits = await self.reading.search_books("秘境", pool, search_id=2)
        return hits[0]


if __name__ == "__main__":
    unittest.main()
