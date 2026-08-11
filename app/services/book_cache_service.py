"""Whole-book offline cache orchestration, modelled after Legado CacheBook."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path

from app.services.reading_cache import ReadingCache
from app.services.reading_engine import LegadoReadingEngine, _media_request


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
                 engine_factory=None, retries: int = 3):
        self.cache = ReadingCache(cache_dir)
        self.state_dir = Path(state_dir)
        self.engine_factory = engine_factory or (
            lambda: LegadoReadingEngine(state_dir=self.state_dir))
        self.retries = max(1, int(retries))

    async def cache_book(self, source, book, on_progress=None) -> BookCacheProgress:
        engine = self.engine_factory()
        progress = BookCacheProgress()
        try:
            engine.current_book = book
            engine.js_variables.update(getattr(book, "variables", {}) or {})
            chapters = self.cache.load_catalog(source.bookSourceUrl, book.url)
            if not chapters:
                chapters = await engine.chapters(source, book)
                self.cache.save_catalog(source.bookSourceUrl, book.url, chapters)
            readable = [item for item in chapters if not item.is_volume and item.url]
            progress.total = len(readable)
            self.cache.pin_book(source.bookSourceUrl, book.url)
            for index, chapter in enumerate(readable):
                progress.current = chapter.name
                if self.cache.has_content(source.bookSourceUrl, book.url, chapter.url):
                    progress.cached += 1
                    await self._notify(on_progress, progress)
                    continue
                next_url = readable[index + 1].url if index + 1 < len(readable) else ""
                last_error = None
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
            progress.running = False
            progress.current = ""
            await self._notify(on_progress, progress)
            return progress
        finally:
            engine.js.close()

    @staticmethod
    async def _cache_images(engine, source, document) -> None:
        """Embed remote chapter images so comics remain readable offline."""
        for block in document.blocks:
            if block.kind != "image" or not isinstance(block.value, str):
                continue
            raw_value = block.value.strip()
            if not raw_value or raw_value.startswith("data:"):
                continue
            url, option_headers = _media_request(raw_value, raw_value)
            headers = dict(option_headers)
            headers.update(block.headers or {})
            await engine._request(
                {"url": url, "method": "GET", "headers": headers}, source)
            payload = bytes((engine.last_response or {}).get("bodyBytes") or [])
            if not payload:
                raise RuntimeError(f"图片缓存为空: {url}")
            block.value = payload

    @staticmethod
    async def _notify(callback, progress):
        if callback is None:
            return
        result = callback(progress)
        if inspect.isawaitable(result):
            await result
