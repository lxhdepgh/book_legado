"""BookmarkService：用户书签 CRUD（M2，契约 §4 / 设计 §1.13）。

存储落 StorageService ``bookmarks`` 表（v2 迁移新增）；与自动续读进度
（``books`` 表）是两条独立数据，不合并。增删即发 ``BookmarkChangedEvent``。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from app.core.events import BookmarkChangedEvent
from app.core.lifecycle import Service
from app.core.storage import StorageService

logger = logging.getLogger(__name__)


class BookmarkService(Service):
    """书签：增 / 删 / 列表，发布变更事件。"""

    name = "BookmarkService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage=None) -> None:
        super().__init__()
        self._storage = storage
        self._lock = threading.Lock()

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        logger.info("BookmarkService 已启动")

    def _publish(self, event):
        if self.bus is None:
            return []
        return self.bus.publish(event)

    def _conn(self) -> sqlite3.Connection | None:
        return self._storage.db if self._storage is not None else None

    # ------------------------------------------------------------------ #
    async def add_bookmark(self, book_url: str, chapter_index: int,
                           offset: float, summary: str) -> int:
        """新增书签，返回书签 id；发布 op="add" 事件。"""
        book_url = str(book_url or "")
        now = time.time()
        bookmark_id = 0
        conn = self._conn()
        if conn is not None:
            with self._lock:
                cursor = conn.execute(
                    "INSERT INTO bookmarks(book_url, chapter_index, offset,"
                    " summary, created_at) VALUES(?,?,?,?,?)",
                    (book_url, int(chapter_index), float(offset),
                     str(summary or ""), now))
                bookmark_id = int(cursor.lastrowid)
        self._publish(BookmarkChangedEvent(book_url=book_url, op="add"))
        return bookmark_id

    async def remove_bookmark(self, book_url: str, bookmark_id: int) -> bool:
        """删除书签；发布 op="remove" 事件（无论是否删到都发，便于视图刷新）。"""
        conn = self._conn()
        changed = False
        if conn is not None:
            with self._lock:
                cursor = conn.execute(
                    "DELETE FROM bookmarks WHERE book_url=? AND id=?",
                    (str(book_url or ""), int(bookmark_id)))
                changed = cursor.rowcount > 0
        self._publish(BookmarkChangedEvent(book_url=str(book_url or ""), op="remove"))
        return changed

    async def list_bookmarks(self, book_url: str) -> list[dict]:
        """按书列出书签（按创建时间升序）。"""
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT id, book_url, chapter_index, offset, summary, created_at"
                " FROM bookmarks WHERE book_url=? ORDER BY created_at ASC, id ASC",
                (str(book_url or ""),)).fetchall()
        return [
            {
                "bookmark_id": int(row[0]),
                "book_url": row[1],
                "chapter_index": int(row[2]),
                "offset": float(row[3]),
                "summary": row[4],
                "created_at": float(row[5]),
            }
            for row in rows
        ]
