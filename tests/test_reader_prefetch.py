"""章节后台预取的服务面护栏：预取范围、同章去重、失败与作废（§5.5）。

预取是"读到底不等待"的另一半：章尾自动接上的那一章必须早就在读者手里，
否则弱网下每次接章都是一次可感知的停顿。这里只验服务面（ReadingService），
控制器侧的触发与复用见 tests/test_reader_continuous_scroll.py。

全程离线：引擎是替身，不发真实网络请求（见记忆 legado-live-scan-av-block）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.engine_worker import engine_hub
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.reading import reading_service as rs
from app.services.reading.reading_service import (
    PREFETCH_CHAPTER_COUNT,
    ReadingService,
)
from app.services.reading_cache import ReadingCache

SOURCE_URL = "http://127.0.0.1:1"
BOOK_URL = "http://127.0.0.1:1/book/1"

SOURCE = SimpleNamespace(bookSourceUrl=SOURCE_URL)


def _catalog(count: int, volume_at: int = -1) -> list[ChapterItem]:
    """造一份目录：``volume_at`` 位置放一个卷标题（预取必须跳过它）。"""
    items: list[ChapterItem] = []
    for index in range(count):
        volume = index == volume_at
        items.append(ChapterItem(
            f"卷{index // 10 + 1}" if volume else f"第{index + 1}章",
            "" if volume else f"{BOOK_URL}/c{index + 1}",
            index=index, is_volume=volume))
    return items


class _Engine:
    """引擎替身：记录每一章被请求了几次；``gate`` 让请求停在半路。"""

    def __init__(self, catalog, *, failing=(), gate: bool = False):
        self.catalog = list(catalog)
        self.failing = {str(url) for url in failing}
        self.failed: set[str] = set()
        self.gate = gate
        self.calls: list[str] = []
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    async def chapters(self, source, book):
        return list(self.catalog)

    async def content_document(self, source, chapter, next_url=""):
        url = str(chapter.url)
        self.calls.append(url)
        if self.gate:
            self.started.setdefault(url, asyncio.Event()).set()
            await self.release.setdefault(url, asyncio.Event()).wait()
        # 只撞一次墙：第二次读者的正常请求要能成——这正是"失败没被记住"的那一面。
        if url in self.failing and url not in self.failed:
            self.failed.add(url)
            raise RuntimeError("站点挂了")
        return ChapterContent(title=chapter.name,
                              blocks=[ContentBlock("text", chapter.name)])

    def release_all(self) -> None:
        for event in self.release.values():
            event.set()


async def _until(predicate, timeout: float = 2.0) -> bool:
    """等一个后台状态成立：预取轮是 ensure_future 起来的，不能假设它跑在第几拍。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class _Rig:
    def __init__(self, reading, storage, settings, root):
        self.reading = reading
        self.storage = storage
        self.settings = settings
        self.root = root

    async def close(self) -> None:
        self.reading.stop_prefetch()
        await self.reading.on_stop()
        await self.settings.on_stop()
        await self.storage.stop()
        shutil.rmtree(self.root, ignore_errors=True)


async def _open(engine: _Engine, book_url: str = BOOK_URL) -> tuple[_Rig, object]:
    """真 StorageService/ReadingCache + 替身引擎，开一本书拿到会话。"""
    root = Path(tempfile.mkdtemp(prefix="prefetch_"))
    os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = str(root / "data")
    os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = str(root / "cache")
    storage = StorageService()
    await storage.start()
    settings = SettingsService(storage=storage, engine=engine_hub())
    await settings.on_start()
    reading = ReadingService(
        rule_engine=engine, storage=storage, settings=settings,
        source_service=None, cache=ReadingCache(storage.paths.cache))
    session = await reading.open_book(
        SOURCE, BookItem(name="书名", url=book_url))
    return _Rig(reading, storage, settings, root), session


def _key(chapter) -> tuple[str, str, str]:
    return (SOURCE_URL, BOOK_URL, str(chapter.url))


def test_prefetch_pulls_the_next_batch_and_stops_at_the_limit():
    """预取范围：当前章之后 PREFETCH_CHAPTER_COUNT 章，跳过卷标题，不多取一章。"""
    async def main():
        catalog = _catalog(PREFETCH_CHAPTER_COUNT + 4, volume_at=2)
        engine = _Engine(catalog)
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            wanted = [str(c.url) for c in catalog[1:]
                      if not c.is_volume and c.url][:PREFETCH_CHAPTER_COUNT]
            assert await _until(lambda: len(engine.calls) >= len(wanted)), \
                f"预取没跑满一批：{engine.calls}"
            await asyncio.sleep(0.1)   # 再多给一拍：越界的那一章会在这里露头
            assert engine.calls == wanted, f"预取的章节不对或越过了上限：{engine.calls}"
            assert rig.reading._prefetch_cache, "预取结果没进副本"
        finally:
            await rig.close()

    asyncio.run(main())


def test_a_chapter_already_being_prefetched_is_not_requested_twice():
    """读者翻到的正是预取在跑的那一章：接上同一个请求，不另开第二条。"""
    async def main():
        catalog = _catalog(6)
        engine = _Engine(catalog, gate=True)
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            assert await _until(lambda: catalog[1].url in engine.started), \
                "预取根本没开始"
            reading_now = asyncio.ensure_future(
                rig.reading.load_content(catalog[1], append=True))
            await asyncio.sleep(0.1)
            assert engine.calls.count(catalog[1].url) == 1, "同一章被请求了两遍"
            engine.release_all()
            document = await asyncio.wait_for(reading_now, 2)
            assert document.blocks[0].value == catalog[1].name
            assert engine.calls.count(catalog[1].url) == 1
        finally:
            engine.release_all()
            await rig.close()

    asyncio.run(main())


def test_cancelling_the_reader_does_not_kill_the_shared_request():
    """读者那一趟被取消（换章/切页）不该带走预取也在等的请求：跑完照样进副本。"""
    async def main():
        catalog = _catalog(6)
        engine = _Engine(catalog, gate=True)
        rig, session = await _open(engine)
        try:
            leaving = asyncio.ensure_future(rig.reading.load_content(catalog[1]))
            assert await _until(lambda: catalog[1].url in engine.started)
            await rig.reading.prefetch_chapters(session.chapters[0])
            leaving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await leaving
            engine.release_all()
            assert await _until(
                lambda: _key(catalog[1]) in rig.reading._prefetch_cache), \
                "跑完的结果没进副本"
            assert engine.calls.count(catalog[1].url) == 1, "取消把共享请求一起带走了"
        finally:
            engine.release_all()
            await rig.close()

    asyncio.run(main())


def test_a_failed_prefetch_is_retried_when_the_reader_arrives():
    """预取撞墙不影响阅读：真翻到这一章时照常重新请求，不把失败当结果。"""
    async def main():
        catalog = _catalog(4)
        engine = _Engine(catalog, failing=[catalog[1].url])
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            assert await _until(
                lambda: engine.calls.count(catalog[1].url) >= 1), "预取没开始"
            assert await _until(lambda: rig.reading._prefetch_task.done()), \
                "预取失败后这一轮没停下"
            document = await rig.reading.load_content(catalog[1], append=True)
            assert document.blocks[0].value == catalog[1].name
            assert engine.calls.count(catalog[1].url) == 2, "失败后没重试"
        finally:
            await rig.close()

    asyncio.run(main())


def test_stop_prefetch_abandons_the_rest_of_the_round():
    """叫停只停"还没开始的"：在途那一章照跑完，后面的章节一章不多要。"""
    async def main():
        catalog = _catalog(8)
        engine = _Engine(catalog, gate=True)
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            assert await _until(lambda: catalog[1].url in engine.started)
            rig.reading.stop_prefetch()
            engine.release_all()
            assert await _until(
                lambda: _key(catalog[1]) in rig.reading._prefetch_cache)
            await asyncio.sleep(0.1)
            assert engine.calls == [catalog[1].url], f"叫停后还在往后取：{engine.calls}"
        finally:
            await rig.close()

    asyncio.run(main())


def test_switching_books_drops_the_previous_prefetch_copies():
    """换书：上一本的副本与轮子一起作废，别让两本书的正文挤在同一份预算里。"""
    async def main():
        catalog = _catalog(6)
        engine = _Engine(catalog)
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            assert await _until(lambda: rig.reading._prefetch_cache)
            await rig.reading.open_book(
                SOURCE, BookItem(name="另一本", url=f"{BOOK_URL}/2"))
            assert rig.reading._prefetch_cache == {}
            assert rig.reading._prefetch_task is None
        finally:
            await rig.close()

    asyncio.run(main())


def test_over_budget_only_drops_the_copy_not_the_disk_cache(monkeypatch):
    """内存预算超了只是不留副本：磁盘缓存照写，等于安静地退回旧行为。"""
    async def main():
        catalog = _catalog(3)
        engine = _Engine(catalog)
        monkeypatch.setattr(rs, "PREFETCH_CACHE_LIMIT", 1)
        rig, session = await _open(engine)
        try:
            await rig.reading.prefetch_chapters(session.chapters[0])
            assert await _until(lambda: engine.calls), "预取根本没跑"
            assert rig.reading._prefetch_cache == {}, "超预算还留副本"
            assert await _until(lambda: rig.reading._cache.load_content(
                SOURCE_URL, BOOK_URL, engine.calls[0]) is not None), "预取的结果没落盘"
        finally:
            await rig.close()

    asyncio.run(main())
