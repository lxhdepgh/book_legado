"""ContentSearchService：书内全文检索（M2，契约 §5 / 设计 §1.14）。

- FTS5 虚表 ``content_search`` 在 on_start 运行时 ``CREATE VIRTUAL TABLE IF NOT EXISTS``，
  **不进版本化迁移**（虚表随索引重建，契约 §9 末段）；
- 索引只进主库（红线 8，无散落文件），文本来源复用 reader-cache 已缓存章节；
- **一行 = 一个自然段**（不是引擎给的一个 text block）：正文整章常常只有一两个
  ``<p>``，按块建行等于"章级偏移"，命中段落根本定位不到；切段与段首偏移都取自
  位置契约那把尺（:func:`app.core.text_layout.chapter_paragraphs` 配
  :func:`~app.core.text_layout.paragraph_offsets`，底层仍是
  :func:`~app.core.text_layout.paragraph_lines`；红线 §7.5：视图不许 import 服务，
  两边都够得着的只有 core）；
- 未缓存章节不建索引（rebuild 时跳过，调用方应提示"先缓存"）；
- 建索引的触发点是书内搜索第一次用（:meth:`ensure_index`），不是整书缓存收尾；
- 命中返回 chapter_index + 段序号 para_index + 段首字符偏移，供阅读器跳转。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService
from app.core.text_layout import chapter_paragraphs, paragraph_offsets
from app.services.engine.reading_engine import display_blocks
from app.services.reading_cache import ReadingCache

logger = logging.getLogger(__name__)

_FTS_TABLE = "content_search"
#: 建行粒度：块级索引没有 para_index 列，跳段落时无从取值——见到旧表就整表重建
#: （索引是派生数据，重建由 :meth:`ensure_index` 按需完成，一次性的）。
_PARAGRAPH_COLUMN = "para_index"


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
            schema = f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(" \
                     "book_url UNINDEXED," \
                     " chapter_index UNINDEXED," \
                     " offset UNINDEXED," \
                     f" {_PARAGRAPH_COLUMN} UNINDEXED," \
                     " text," \
                     " tokenize='trigram')"
            if self._has_paragraph_column(conn):
                return
            conn.execute(schema)
        except sqlite3.OperationalError as exc:  # 该 sqlite 未编译 FTS5
            self._fts_available = False
            logger.warning("FTS5 不可用，书内全文检索降级为空结果：%s", exc)

    @staticmethod
    def _has_paragraph_column(conn) -> bool:
        """旧表（块级索引）没有 para_index 列：整表拆掉重建。

        先探一眼再建：虚表已经在时 ``CREATE`` 抛 "no such column: rowid"，那条真错
        会被同一个 ``except`` 认成"这台 sqlite 没有 FTS5"，把功能整本降级掉。
        拆表不丢东西——索引是派生数据，下一次书内搜索就按 :meth:`ensure_index`
        重新建（实测 200 章 × 8 段 74 ms）。
        """
        columns = [str(row[1]) for row in
                   conn.execute(f"PRAGMA table_info({_FTS_TABLE})").fetchall()]
        if not columns:           # 表还不存在
            return False
        if _PARAGRAPH_COLUMN in columns:
            return True
        conn.execute(f"DROP TABLE {_FTS_TABLE}")
        return False

    # ------------------------------------------------------------------ #
    async def rebuild_index(self, book_url: str, source_url: str = "") -> int:
        """对已缓存章节重建索引，写入行数；未缓存章节跳过。

        这段必须离开调用循环跑：主库连接是 autocommit，在协程里逐块 INSERT 就是
        逐块建 journal 再 fsync。本机 200 章 × 8 块（1600 行）两臂对照
        （``.probe/fts_rebuild_freeze_attr.py``）：改前那种"就在循环上逐条写"
        4965 ms（≈3103 µs/行），其间该醒 993 拍的 5 ms 心跳**一拍都没醒**；现在这条
        （线程池 + 一条事务 + executemany）74 ms（≈46 µs/行），心跳照常在跳。
        形状照 ``search_model`` 里 ``asyncio.to_thread(self.store.insert, items)``
        那条既有先例。
        """
        return await asyncio.to_thread(self._rebuild_sync, book_url, source_url)

    def _rebuild_sync(self, book_url: str, source_url: str) -> int:
        book_url = str(book_url or "")
        conn = self._conn()
        if conn is None or not self._fts_available or not book_url:
            return 0
        rows: list[tuple] = []
        if source_url and self._cache is not None:
            for chapter in self._cache.load_catalog(source_url, book_url) or []:
                if getattr(chapter, "is_volume", False) or not getattr(chapter, "url", ""):
                    continue
                document = self._cache.load_content(source_url, book_url, chapter.url)
                if document is None:  # 未缓存章节不建索引
                    continue
                index = int(getattr(chapter, "index", 0) or 0)
                # 段序号与段首偏移都取自**位置契约那把尺**（app.core.text_layout）：
                # 命中跳转要把 para_index/offset 交回阅读器定位，而它落进度用的是
                # 同一把尺导出的字符偏移。自己数一遍（旧写法 ``running += len(line)``）
                # 每段少算一个换行格，跳过去就偏一段——偏差还随段数线性增长。
                paragraphs = chapter_paragraphs(display_blocks(document))
                offsets = paragraph_offsets(paragraphs)
                for paragraph, line in enumerate(paragraphs):
                    rows.append((book_url, index, float(offsets[paragraph]),
                                 paragraph, line))
        # 清旧索引与写新索引是**一笔**：中途失败不会留下"半本书"的索引，
        # 而 transaction() 在别人事务里只加入、不会把别人夹在中间的写一起回滚。
        with conn.transaction():
            conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE book_url=?", (book_url,))
            if rows:
                conn.executemany(
                    f"INSERT INTO {_FTS_TABLE}"
                    "(book_url, chapter_index, offset, para_index, text)"
                    " VALUES(?,?,?,?,?)", rows)
        return len(rows)

    async def ensure_index(self, book_url: str, source_url: str = "") -> int:
        """缓存里已有的章节都得在索引里；缺了才重建，返回写入行数。

        判据只有一条：**已缓存的章节数 > 索引覆盖的章节数**。反向不一致（缓存被
        限额清理掉、索引还留着）不触发重建——那几条命中跳过去只是重新取一次正文，
        不是查不到东西。
        """
        book_url = str(book_url or "")
        conn = self._conn()
        if conn is None or not self._fts_available or not book_url:
            return 0
        if self._cache is None or not source_url:
            # 没有缓存可依据就不建：rebuild 会先删旧索引，此时重建等于把这本书的
            # 索引清成 0 行——"少几章"比"整本查不到"好得多。
            return 0
        cached = await asyncio.to_thread(
            self._cache.cache_counts, source_url, book_url)
        indexed = await asyncio.to_thread(self._indexed_chapters, conn, book_url)
        # 只在"缓存比索引多"时重建。反向不一致（缓存被限额清理掉、索引还留着）不
        # 重建也不清：rebuild 先删后写，缓存空了就等于把整本索引清成 0 行，而留着
        # 的几条命中跳过去只是重新取一次正文。
        if cached[0] <= indexed:
            return 0
        return await self.rebuild_index(book_url, source_url)

    @staticmethod
    def _indexed_chapters(conn, book_url: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT chapter_index) FROM {_FTS_TABLE} "
            f"WHERE book_url=?", (book_url,)).fetchone()
        return int(row[0]) if row is not None else 0

    async def search(self, book_url: str, keyword: str) -> list[dict]:
        """检索某书，返回命中列表：chapter_index / para_index / 段首偏移 / 摘要。"""
        conn = self._conn()
        keyword = str(keyword or "").strip()
        if conn is None or not self._fts_available or not keyword:
            return []
        # trigram 分词器对 >=3 字符的子串匹配良好；更短的查询（常见于中文
        # 双字词）FTS MATCH 无法命中，退化为 LIKE 扫描以保证可用。
        if len(keyword) < 3:
            rows = conn.execute(
                f"SELECT chapter_index, para_index, offset, snippet({_FTS_TABLE}, 4, "
                f"'<mark>', '</mark>', '…', 16) FROM {_FTS_TABLE} "
                f"WHERE book_url=? AND text LIKE ? "
                f"ORDER BY chapter_index, offset LIMIT 200",
                (str(book_url or ""), f"%{keyword}%")).fetchall()
        else:
            phrase = '"' + keyword.replace('"', " ") + '"'
            rows = conn.execute(
                f"SELECT chapter_index, para_index, offset, snippet({_FTS_TABLE}, 4, "
                f"'<mark>', '</mark>', '…', 16) FROM {_FTS_TABLE} "
                f"WHERE book_url=? AND {_FTS_TABLE} MATCH ? "
                f"ORDER BY chapter_index, offset LIMIT 200",
                (str(book_url or ""), phrase)).fetchall()
        return [
            {
                "chapter_index": int(row[0]),
                "para_index": int(row[1]),
                "offset": float(row[2]),
                "snippet": row[3],
            }
            for row in rows
        ]

    async def search_ensured(self, book_url: str, keyword: str,
                             source_url: str = "") -> dict:
        """书内搜索的一发面：先按需建索引，再查，并回"为什么可能是 0 条"。

        把这两步拆给客户端是留陷阱：索引还没建时 ``search`` 回空列表，跨进程那侧
        看起来与"这本书里没有这个词"一模一样（记忆 ``zero-counts-need-a-positive-control``）。
        所以这一发把 ``indexed_chapters``（索引覆盖了几章）与 ``fts_available``
        （这台机器有没有 FTS5）一起报回去，界面据此说"只搜了已缓存的 N 章"。
        """
        written = await self.ensure_index(book_url, source_url)
        hits = await self.search(book_url, keyword)
        conn = self._conn()
        indexed = (await asyncio.to_thread(self._indexed_chapters, conn, book_url)
                   if conn is not None and self._fts_available else 0)
        return {
            "hits": hits,
            "written": int(written),
            "indexedChapters": int(indexed),
            "ftsAvailable": bool(conn is not None and self._fts_available),
        }
