"""Whole-book offline cache orchestration, modelled after Legado CacheBook."""
from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import dataclass
from pathlib import Path

from app.services.reading_cache import ReadingCache
from app.services.reading_engine import LegadoReadingEngine


@dataclass
class BookCacheProgress:
    total: int = 0
    cached: int = 0
    failed: int = 0
    current: str = ""
    running: bool = True


class WholeBookCacheService:
    """Cache every readable chapter, skip existing files and retry failures."""

    def __init__(self, cache_dir: str | Path, state_dir: str | Path,
                 engine_factory=None, retries: int = 3, concurrency: int = 4):
        self.cache = ReadingCache(cache_dir)
        self.state_dir = Path(state_dir)
        self.engine_factory = engine_factory or (
            lambda: LegadoReadingEngine(state_dir=self.state_dir, background=True))
        self.retries = max(1, int(retries))
        self.concurrency = max(1, min(8, int(concurrency)))

    async def cache_book(self, source, book, on_progress=None) -> BookCacheProgress:
        catalog_engine = self.engine_factory()
        engines = [catalog_engine]
        progress = BookCacheProgress()
        try:
            self._prepare_engine(catalog_engine, book)
            chapters = self.cache.load_catalog(source.bookSourceUrl, book.url)
            if not chapters:
                chapters = await catalog_engine.chapters(source, book)
                self.cache.save_catalog(source.bookSourceUrl, book.url, chapters)
            readable = [item for item in chapters if not item.is_volume and item.url]
            progress.total = len(readable)
            self.cache.pin_book(source.bookSourceUrl, book.url)
            queue = asyncio.Queue()
            for index, chapter in enumerate(readable):
                if self.cache.has_content(source.bookSourceUrl, book.url, chapter.url):
                    progress.cached += 1
                    await self._notify(on_progress, progress)
                    continue
                next_url = readable[index + 1].url if index + 1 < len(readable) else ""
                queue.put_nowait((chapter, next_url))

            worker_count = min(self.concurrency, queue.qsize())
            for _ in range(max(0, worker_count - 1)):
                worker_engine = self.engine_factory()
                self._prepare_engine(worker_engine, book)
                engines.append(worker_engine)
            if worker_count:
                await asyncio.gather(*(
                    self._worker(engine, queue, source, book, progress, on_progress)
                    for engine in engines[:worker_count]))
            progress.running = False
            progress.current = ""
            await self._notify(on_progress, progress)
            return progress
        finally:
            for engine in engines:
                await engine.aclose()

    @staticmethod
    def _prepare_engine(engine, book) -> None:
        engine.current_book = copy.deepcopy(book)
        engine.js_variables.update(getattr(book, "variables", {}) or {})

    async def _worker(self, engine, queue, source, book, progress,
                      on_progress) -> None:
        while True:
            try:
                chapter, next_url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            progress.current = chapter.name
            last_error = None
            try:
                for attempt in range(self.retries):
                    try:
                        document = await engine.content_document(source, chapter, next_url)
                        await self._cache_images(engine, source, document)
                        self.cache.save_content(
                            source.bookSourceUrl, book.url, chapter.url, document)
                        progress.cached += 1
                        last_error = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if attempt + 1 < self.retries:
                            await asyncio.sleep(0.5)
                if last_error is not None:
                    progress.failed += 1
                await self._notify(on_progress, progress)
            finally:
                queue.task_done()

    @staticmethod
    async def _cache_images(engine, source, document) -> None:
        """Embed remote chapter images so comics remain readable offline."""
        for block in document.blocks:
            if block.kind != "image" or not isinstance(block.value, str):
                continue
            raw_value = block.value.strip()
            if not raw_value or raw_value.startswith("data:"):
                continue
            payload = await engine.fetch_media(raw_value, source,
                                               extra_headers=block.headers)
            if not payload:
                raise RuntimeError(f"图片缓存为空: {raw_value}")
            block.value = payload

    @staticmethod
    async def _notify(callback, progress):
        if callback is None:
            return
        result = callback(progress)
        if inspect.isawaitable(result):
            await result
