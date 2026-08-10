"""Python port of Legado's multi-source ``SearchModel`` lifecycle."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import asdict, fields
from contextlib import contextmanager
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from app.services.rule_engine import BookItem, merge_search_items


class SearchBookStore:
    """Small SQLite equivalent of the Room ``searchBooks`` table."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS search_books ("
                "book_url TEXT PRIMARY KEY, name TEXT NOT NULL, author TEXT NOT NULL, "
                "origin TEXT NOT NULL, origin_order INTEGER NOT NULL, "
                "saved_at INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS search_books_name_author "
                       "ON search_books(name, author, origin_order)")
            db.execute("CREATE INDEX IF NOT EXISTS search_books_origin "
                       "ON search_books(origin)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(payload: str) -> BookItem | None:
        try:
            value = json.loads(payload)
            allowed = {item.name for item in fields(BookItem)}
            return BookItem(**{key: item for key, item in value.items()
                               if key in allowed})
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def insert(self, books: Iterable[BookItem]) -> None:
        now = int(time.time() * 1000)
        rows = [(book.url, book.name, book.author, book.origin,
                 int(book.origin_order or 0), now,
                 json.dumps(asdict(book), ensure_ascii=False,
                            separators=(",", ":")))
                for book in books]
        if not rows:
            return
        with self._lock, self._connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO search_books "
                "(book_url,name,author,origin,origin_order,saved_at,payload) "
                "VALUES (?,?,?,?,?,?,?)", rows)

    def get(self, book_url: str) -> BookItem | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload FROM search_books WHERE book_url=?",
                             (str(book_url),)).fetchone()
        return self._decode(row[0]) if row else None

    def first_by_name_author(self, name: str, author: str) -> BookItem | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT payload FROM search_books WHERE name=? AND author=? "
                "ORDER BY origin_order LIMIT 1", (str(name), str(author))).fetchone()
        return self._decode(row[0]) if row else None

    def clear(self, name: str, author: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM search_books WHERE name=? AND author=?",
                       (str(name), str(author)))

    def clear_expired(self, before_ms: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM search_books WHERE saved_at < ?",
                       (int(before_ms),))


class SearchModel:
    """Stateful, bounded multi-source search matching fixed SearchModel.kt.

    A different ``search_id`` cancels the previous generation and starts page
    one. Reusing the same id increments the page. Sources are consumed by at
    most ``min(thread_count, 9)`` workers and each source has the historical
    30-second timeout.
    """

    MAX_THREAD = 9

    def __init__(self, search_one: Callable[[object, str, int],
                                             Awaitable[list[BookItem]]], *,
                 thread_count: int = 16, precision: bool = False,
                 store: SearchBookStore | None = None,
                 on_update: Callable[[list[BookItem]], object] | None = None,
                 on_start: Callable[[], object] | None = None,
                 on_finish: Callable[[bool], object] | None = None,
                 on_cancel: Callable[[Exception | None], object] | None = None):
        self.search_one = search_one
        self.thread_count = int(thread_count)
        self.precision = bool(precision)
        self.store = store
        self.on_update = on_update
        self.on_start = on_start
        self.on_finish = on_finish
        self.on_cancel = on_cancel
        self.search_id = 0
        self.page = 1
        self.key = ""
        self.sources: list[object] = []
        self.books: list[BookItem] = []
        self._tasks: set[asyncio.Task] = set()
        self._generation = 0
        self._state_lock = asyncio.Lock()

    async def _notify(self) -> None:
        if self.on_update is None:
            return
        value = self.on_update(list(self.books))
        if asyncio.iscoroutine(value):
            await value

    @staticmethod
    async def _emit(callback, *args) -> None:
        if callback is None:
            return
        value = callback(*args)
        if asyncio.iscoroutine(value):
            await value

    async def search(self, search_id: int, key: str,
                     sources: Iterable[object] | None = None) -> list[BookItem]:
        await self._emit(self.on_start)
        async with self._state_lock:
            if int(search_id) != self.search_id:
                if not key:
                    self._cancel_tasks()
                    self._generation += 1
                    self.search_id = 0
                    await self._emit(self.on_cancel, None)
                    return []
                self._cancel_tasks()
                self._generation += 1
                self.search_id = int(search_id)
                self.page = 1
                self.key = str(key)
                self.sources = list(sources or [])
                self.books = []
                await self._notify()
                if not self.sources:
                    await self._emit(
                        self.on_cancel, RuntimeError("启用书源为空"))
                    return []
            else:
                self.page += 1
                if sources is not None:
                    self.sources = list(sources)
            generation, page = self._generation, self.page
            queue = asyncio.Queue()
            for source in self.sources:
                queue.put_nowait(source)

        async def worker():
            while True:
                try:
                    source = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    items = await asyncio.wait_for(
                        self.search_one(source, self.key, page), timeout=30.0)
                    async with self._state_lock:
                        if generation != self._generation or search_id != self.search_id:
                            return
                        if self.store is not None:
                            await asyncio.to_thread(self.store.insert, items)
                        self.books = merge_search_items(
                            self.books, items, self.key, self.precision)
                        await self._notify()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Fixed SearchModel reports source failures only through
                    # task completion and continues scheduling later sources.
                    pass
                finally:
                    queue.task_done()

        count = min(max(0, self.thread_count), self.MAX_THREAD, len(self.sources))
        tasks = {asyncio.create_task(worker()) for _ in range(count)}
        self._tasks.update(tasks)
        try:
            await asyncio.gather(*tasks)
        finally:
            self._tasks.difference_update(tasks)
        if generation == self._generation:
            await self._emit(self.on_finish, not self.books)
        return list(self.books) if generation == self._generation else []

    def _cancel_tasks(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def cancel(self) -> None:
        self._cancel_tasks()
        self._generation += 1
        self.search_id = 0
        await self._emit(self.on_cancel, None)

    async def close(self) -> None:
        await self.cancel()
