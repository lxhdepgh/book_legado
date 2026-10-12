"""BookCacheService：整书离线缓存外壳（M2，契约 §6 / 设计 §3.5）。

内层 :class:`WholeBookCacheService` 自 ``old_code/app/services/book_cache_service.py``
**原样迁入，逻辑零改动**：书内 4 并发 / 断点续传 / 图片内联 / 钉住不淘汰全部保留，
仅 import 路径机械适配到新树。外层 :class:`BookCacheService` 是
:class:`~app.core.lifecycle.Service` 外壳：

- 实例参数（cache 根 / state_dir）改由 StorageService 提供；
- 钉住集合 M2 在服务内维护（M6 收敛 StorageService.pin）；
- 进度上报保留为服务内部钩子 ``on_progress``，对外进度事件由 DownloadService 发布。
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.lifecycle import Service
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.reading_cache import ReadingCache

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 内层：自 old_code 原样迁入（逻辑零改动，仅模块归属变化）。
# --------------------------------------------------------------------------- #
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
        self.engine_factory = engine_factory
        self.retries = max(1, int(retries))
        self.concurrency = max(1, min(8, int(concurrency)))

    async def cache_book(self, source, book, on_progress=None) -> BookCacheProgress:
        from app.services.engine.reading_engine import LegadoReadingEngine
        factory = self.engine_factory or (
            lambda: LegadoReadingEngine(state_dir=self.state_dir, background=True))
        catalog_engine = factory()
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
                worker_engine = factory()
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


# --------------------------------------------------------------------------- #
# 外层 Service 外壳
# --------------------------------------------------------------------------- #
class BookCacheService(Service):
    """整书缓存的生命周期外壳 + 实例参数来源切换。"""

    name = "BookCacheService"
    dependencies: tuple[type[Service], ...] = (RuleEngineService,)

    def __init__(self, storage=None, rule_engine=None, engine_factory=None,
                 retries: int = 3, concurrency: int = 4,
                 state_dir=None, cache_root=None) -> None:
        super().__init__()
        self._storage = storage
        self._rule_engine = rule_engine  # 预留 M3 切到 RuleEngineService 池
        self._engine_factory = engine_factory
        self._retries = retries
        self._concurrency = concurrency
        self._state_dir = state_dir
        self._cache_root = cache_root
        self._inner: WholeBookCacheService | None = None
        # M2 服务内钉住集合（M6 收敛为 StorageService.pin）。
        self._pinned: set[tuple[str, str]] = set()

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._rule_engine is None and self.hub is not None:
            from app.services.engine.rule_engine_service import RuleEngineService
            self._rule_engine = self.hub.require(RuleEngineService)
        cache_root = self._cache_root
        if cache_root is None and self._storage is not None:
            # ReadingCache 会在给定根下追加 "reader-cache"，故给缓存根目录，
            # 磁盘布局与旧行为一致。
            cache_root = self._storage.paths.cache
        state_dir = self._state_dir
        if state_dir is None and self._storage is not None:
            state_dir = self._storage.paths.data
        self._inner = WholeBookCacheService(
            cache_root, state_dir, engine_factory=self._engine_factory,
            retries=self._retries, concurrency=self._concurrency)
        logger.info("BookCacheService 已启动")

    async def on_stop(self) -> None:
        self._inner = None

    async def cache_book(self, source, book, on_progress=None) -> BookCacheProgress:
        """缓存整本书；完成后登记钉住（不参与容量淘汰）。"""
        if self._inner is None:
            raise RuntimeError("BookCacheService 尚未启动")
        result = await self._inner.cache_book(source, book, on_progress)
        self._pinned.add((
            getattr(source, "bookSourceUrl", "") or "",
            getattr(book, "url", "") or ""))
        return result

    def is_pinned(self, source_url: str, book_url: str) -> bool:
        return (str(source_url or ""), str(book_url or "")) in self._pinned
