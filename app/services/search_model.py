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

from app.services.engine.rule_engine import BookItem, merge_search_items


class SearchBookStore:
    """Small SQLite equivalent of the Room ``searchBooks`` table.

    M6 介质垫片（契约 §3）：构造可传 ``conn=主库连接`` 让 store 直接读写主库
    ``search_books`` 表（schema v6 已建表），不再自建 ``search-books.sqlite3``；
    不传 conn 时行为与旧版完全一致（文件模式）。SearchModel 业务逻辑零改动。
    """

    def __init__(self, path: str | Path | None = None, *, conn=None):
        self._external = conn  # M6：注入主库连接垫片；None 则走文件模式
        self._lock = threading.RLock()
        if conn is not None:
            # 主库表由 schema v6 建立，此处不建文件、不建表。
            self.path = None
            return
        if path is None:
            raise ValueError("文件模式需传入 path，或改用 conn 注入主库连接")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        if self._external is not None:
            # 共享主库连接（StorageService 以 isolation_level=None autocommit 打开）：
            # 仅借用，不关闭、不提交、不回滚。
            yield self._external
            return
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

    @contextmanager
    def _batch(self, db):
        """一批行只提交一次。

        主库连接是 autocommit（``isolation_level=None``），逐行 INSERT 就是逐行
        建 journal 再 fsync：实测 400 行 1141ms，包成一条事务 11.6ms（**98×**），
        而换源扫描每个源都要落一次种子——那是整轮里最大的一块。

        注入的是主库时（``LockedConnection``）整批交给 ``transaction()``：BEGIN 到
        COMMIT 期间占住主库 ``_db_lock``，别人的写插不进这笔事务，也就不会被这里
        可能的 ROLLBACK 一起带走（第四十二轮量过的丢行形状）。已在别人事务里时
        ``transaction()`` 只加入，不抢先收尾。文件模式仍是自建连接的 BEGIN。
        """
        from app.core.storage import LockedConnection
        if isinstance(db, LockedConnection):
            with db.transaction():
                yield
            return
        if db.in_transaction:
            yield
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def insert(self, books: Iterable[BookItem]) -> None:
        now = int(time.time() * 1000)
        rows = [(book.url, book.name, book.author, book.origin,
                 int(book.origin_order or 0), now,
                 json.dumps(asdict(book), ensure_ascii=False,
                            separators=(",", ":")))
                for book in books]
        if not rows:
            return
        with self._lock, self._connect() as db, self._batch(db):
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

    def list_by_name_author(self, name: str, author: str = "",
                            limit: int = 500) -> list[BookItem]:
        """Legado's changeSourceByGroup: every stored row for one book.

        An empty author means "any author", like Legado's ``like '%%'``.
        """
        sql = "SELECT payload FROM search_books WHERE name=?"
        params: list = [str(name)]
        if str(author or "").strip():
            sql += " AND author LIKE ?"
            params.append(f"%{author}%")
        sql += " ORDER BY origin_order, book_url LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [book for book in (self._decode(row[0]) for row in rows)
                if book is not None]

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
    most ``min(thread_count, MAX_THREAD)`` workers and each source has the
    historical 30-second timeout.
    """

    # Legado 的 9 只是 OkHttp 派发线程数，搜索 flow 本身按 threadCount（默认 32）
    # 在飞；我们的 worker 直接持有请求，所以上限应该落在 flow 这一层。
    MAX_THREAD = 32

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
                        self.books = merge_search_items(
                            self.books, items, self.key, self.precision)
                        await self._notify()
                    if self.store is not None:
                        # 落库只是给换源列表备用，写失败不该吞掉已经合并好的这一条。
                        try:
                            await asyncio.to_thread(self.store.insert, items)
                        except Exception:
                            pass
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
