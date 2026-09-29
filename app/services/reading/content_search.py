"""ContentSearchService：书内全文检索（M2，契约 §5 / 设计 §1.14）。

- FTS5 虚表 ``content_search`` 在 on_start 运行时 ``CREATE VIRTUAL TABLE IF NOT EXISTS``，
  **不进版本化迁移**（虚表随索引重建，契约 §9 末段）；
- 索引只进主库（红线 8，无散落文件），文本来源复用 reader-cache 已缓存章节；
- 未缓存章节不建索引（rebuild 时跳过，调用方应提示"先缓存"）；
- 命中返回 chapter_index + 偏移，供阅读器跳转。
"""
from __future__ import annotations

import logging
import sqlite3

from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService
from app.services.reading_cache import ReadingCache

logger = logging.getLogger(__name__)

_FTS_TABLE = "content_search"


class ContentSearchService(Service):
    """对已缓存章节建 FTS5 索引并按关键词检索。"""

    name = "ContentSearchService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage=None, cache: ReadingCache | None = None) -> None:
        super().__init__()
        self._storage = storage
        self._cache = cache
        self._fts_available = True

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._cache is None and self._storage is not None:
            self._cache = ReadingCache(self._storage.paths.cache)
        self._ensure_fts_table()

    def _conn(self) -> LockedConnection | None:
        return self._storage.db if self._storage is not None else None

    def _ensure_fts_table(self) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
                "book_url UNINDEXED,"
                " chapter_index UNINDEXED,"
                " offset UNINDEXED,"
                " text,"
                " tokenize='trigram')")
        except sqlite3.OperationalError as exc:  # 该 sqlite 未编译 FTS5
            self._fts_available = False
            logger.warning("FTS5 不可用，书内全文检索降级为空结果：%s", exc)

    # ------------------------------------------------------------------ #
    async def rebuild_index(self, book_url: str, source_url: str = "") -> int:
        """对已缓存章节重建索引，写入行数；未缓存章节跳过。"""
        book_url = str(book_url or "")
        conn = self._conn()
        if conn is None or not self._fts_available or not book_url:
            return 0
        # 先清掉该书旧索引（虚表无 TRUNCATE 优化，DELETE 重建）。
        conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE book_url=?", (book_url,))
        if not source_url or self._cache is None:
            return 0
        chapters = self._cache.load_catalog(source_url, book_url) or []
        written = 0
        for chapter in chapters:
            if getattr(chapter, "is_volume", False) or not getattr(chapter, "url", ""):
                continue
            document = self._cache.load_content(source_url, book_url, chapter.url)
            if document is None:  # 未缓存章节不建索引
                continue
            running = 0
            for block in document.blocks:
                if block.kind != "text" or not isinstance(block.value, str):
                    continue
                text = block.value
                conn.execute(
                    f"INSERT INTO {_FTS_TABLE}"
                    "(book_url, chapter_index, offset, text) VALUES(?,?,?,?)",
                    (book_url, int(getattr(chapter, "index", 0) or 0),
                     float(running), text))
                running += len(text)
                written += 1
        return written

    async def search(self, book_url: str, keyword: str) -> list[dict]:
        """检索某书，返回命中列表：chapter_index / offset / 摘要。"""
        conn = self._conn()
        keyword = str(keyword or "").strip()
        if conn is None or not self._fts_available or not keyword:
            return []
        # trigram 分词器对 >=3 字符的子串匹配良好；更短的查询（常见于中文
        # 双字词）FTS MATCH 无法命中，退化为 LIKE 扫描以保证可用。
        if len(keyword) < 3:
            rows = conn.execute(
                f"SELECT chapter_index, offset, snippet({_FTS_TABLE}, 3, "
                f"'<mark>', '</mark>', '…', 16) FROM {_FTS_TABLE} "
                f"WHERE book_url=? AND text LIKE ? "
                f"ORDER BY chapter_index, offset LIMIT 200",
                (str(book_url or ""), f"%{keyword}%")).fetchall()
        else:
            phrase = '"' + keyword.replace('"', " ") + '"'
            rows = conn.execute(
                f"SELECT chapter_index, offset, snippet({_FTS_TABLE}, 3, "
                f"'<mark>', '</mark>', '…', 16) FROM {_FTS_TABLE} "
                f"WHERE book_url=? AND {_FTS_TABLE} MATCH ? "
                f"ORDER BY chapter_index, offset LIMIT 200",
                (str(book_url or ""), phrase)).fetchall()
        return [
            {
                "chapter_index": int(row[0]),
                "offset": float(row[1]),
                "snippet": row[2],
            }
            for row in rows
        ]
