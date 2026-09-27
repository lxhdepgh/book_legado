"""DownloadService：下载/整书缓存任务队列与进度事件（M2，契约 §7 / 设计 §3.5）。

- 整书缓存任务统一经本服务注册 / 取消，执行仍由 BookCacheService；
- 进度经 ``DownloadProgressEvent`` 发布（服务层只发原始事件，0.5s 节流在 Controller）；
- ``status.progress`` 同步任务总数 / 完成数。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.core.events import DownloadProgressEvent
from app.core.engine_worker import engine_hub
from app.core.lifecycle import Service
from app.services.reading.book_cache_service import BookCacheService

logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    """一个下载/缓存任务的可观测快照。"""

    task_id: str
    kind: str
    book_url: str
    status: str = "queued"  # queued/running/done/cancelled/failed
    progress: float = 0.0
    speed: float = 0.0
    total: int = 0
    done: int = 0
    failed: int = 0
    created_at: float = field(default_factory=time.time)
    handle: object = None  # 运行期 asyncio 任务句柄（不对外暴露语义）


class DownloadService(Service):
    """任务注册中心：负责编排进度事件，不直接抓内容。"""

    name = "DownloadService"
    dependencies: tuple[type[Service], ...] = (BookCacheService,)

    def __init__(self, book_cache=None) -> None:
        super().__init__()
        self._book_cache = book_cache
        self._tasks: dict[str, TaskInfo] = {}

    async def on_start(self) -> None:
        if self._book_cache is None and self.hub is not None:
            from app.services.reading.book_cache_service import BookCacheService
            self._book_cache = self.hub.require(BookCacheService)
        logger.info("DownloadService 已启动")

    async def on_stop(self) -> None:
        for info in self._tasks.values():
            handle = info.handle
            if handle is not None and not getattr(handle, "done", lambda: True)():
                handle.cancel()
        self._tasks.clear()

    def _publish(self, event):
        if self.bus is None:
            return []
        return self.bus.publish(event)

    # ------------------------------------------------------------------ #
    def enqueue(self, kind: str, book_url: str, payload: dict | None = None) -> str:
        """注册一个任务并（整书缓存类）立即启动执行，返回 task_id。"""
        task_id = uuid.uuid4().hex
        info = TaskInfo(task_id=task_id, kind=str(kind or ""),
                        book_url=str(book_url or ""))
        self._tasks[task_id] = info
        payload = payload or {}
        if info.kind == "whole_cache" and self._book_cache is not None:
            source = payload.get("source")
            book = payload.get("book")
            if source is not None and book is not None:
                # B3：经 EngineHub 守护 loop 提交（run_coroutine_threadsafe），
                # 不再 asyncio.ensure_future（后者依赖调用线程当前 loop，UI 线程无
                # loop 时抛 RuntimeError 被吞，有 loop 时又错跑到 UI loop）。
                info.handle = engine_hub().submit(
                    self._run_cache(task_id, source, book))
        return task_id

    def cancel(self, task_id: str) -> bool:
        """取消任务；已结束的任务幂等返回 False。"""
        info = self._tasks.get(task_id)
        if info is None:
            return False
        handle = info.handle
        if handle is not None and not handle.done():
            handle.cancel()
        info.status = "cancelled"
        return True

    def list_tasks(self) -> list[TaskInfo]:
        """当前任务列表（浅拷贝，调用方不影响内部状态）。"""
        return [
            TaskInfo(
                task_id=info.task_id, kind=info.kind, book_url=info.book_url,
                status=info.status, progress=info.progress, speed=info.speed,
                total=info.total, done=info.done, failed=info.failed,
                created_at=info.created_at)
            for info in self._tasks.values()
        ]

    @property
    def task_report(self) -> dict:
        """任务总数/完成数/运行数快照（契约 §7：同步任务统计）。"""
        completed = sum(1 for t in self._tasks.values()
                        if t.status in ("done", "cancelled", "failed"))
        return {
            "total": len(self._tasks),
            "completed": completed,
            "running": sum(1 for t in self._tasks.values()
                           if t.status == "running"),
        }

    # ------------------------------------------------------------------ #
    async def _run_cache(self, task_id: str, source, book) -> None:
        info = self._tasks[task_id]
        info.status = "running"

        def hook(progress):
            info.total = int(progress.total)
            info.done = int(progress.cached)
            info.failed = int(progress.failed)
            info.progress = (info.done / info.total) if info.total else 0.0
            self._publish(DownloadProgressEvent(
                task_id=task_id, kind=info.kind,
                progress=info.progress, speed=info.speed))

        try:
            await self._book_cache.cache_book(source, book, on_progress=hook)
            info.status = "done" if info.failed == 0 else "failed"
            info.progress = 1.0
        except asyncio.CancelledError:
            info.status = "cancelled"
            raise
        except Exception as exc:  # 单任务失败不拖垮下载中心
            info.status = "failed"
            logger.warning("整书缓存任务 %s 失败：%s", task_id, exc)
        finally:
            self._publish(DownloadProgressEvent(
                task_id=task_id, kind=info.kind,
                progress=info.progress, speed=info.speed))
