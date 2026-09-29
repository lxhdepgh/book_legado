"""#85 护栏：书内 FTS 索引重建＝一次提交、离开调用循环，并且真的有人触发它。

三条各钉一味药，每条都配"坏形状埋点"证明断言不是空转：

1. **一次提交**。主库连接是 ``isolation_level=None``（autocommit），在协程里逐块
   INSERT 就是逐块建 journal 再 fsync：本机 200 章 × 8 块（1600 行）实测 4965 ms
   全冻在界面循环上（循环上该醒 993 拍的心跳一拍都没醒），改成一条事务 + 一批写
   是 74 ms。这条用 ``set_trace_callback`` 数语句形状，而不是数耗时——耗时在 CI
   上是噪声。
2. **离开调用循环**。批量写不许落在调用协程所在线程上（界面循环就那条线程）。
   从存储侧取证：重写 ``StorageService`` 的三条写入口，记下各自跑在哪个线程，
   再和循环线程比。
3. **触发面**。``rebuild_index`` 曾经就是第四十一/四十四轮量过的"孤儿方法"形状：
   五个测试替身里都挂着它，生产树里零调用方，于是书内搜索永远"无命中（未缓存章节
   需先缓存整本再建索引）"。这条从真实入口（工具条点搜索）打到底，视图与服务都真。

全程本地临时目录 + 合成章节，不联网、不读 ``resource/``。
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.engine_worker import set_ui_loop, ui_loop
from app.core.storage import StorageService
from app.controllers.reader_controller import ReaderController
from app.services.app_paths import AppPaths
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.services.reading_cache import ReadingCache
from app.views.reading.reader_view import ReaderView

SOURCE = "src-1"
BOOK = "book-fts"
CHAPTERS = 6
BLOCKS = 4
ROWS = CHAPTERS * BLOCKS
WORD = "琵琶行"


@pytest.fixture()
def storage():
    root = Path(tempfile.mkdtemp(prefix="fts85_"))
    svc = StorageService(AppPaths(data=root / "data", cache=root / "cache"))
    asyncio.run(svc.on_start())
    try:
        yield svc
    finally:
        asyncio.run(svc.on_stop())
        shutil.rmtree(root, ignore_errors=True)


def seed_book(cache: ReadingCache, chapters: int = CHAPTERS) -> None:
    """埋一本"已缓存"的书：每章 BLOCKS 块，块里都含 WORD。"""
    cache.save_catalog(SOURCE, BOOK, [
        ChapterItem(name=f"第{i}章", url=f"ch{i}", index=i)
        for i in range(chapters)])
    for i in range(chapters):
        cache.save_content(SOURCE, BOOK, f"ch{i}", ChapterContent(
            title=f"第{i}章",
            blocks=[ContentBlock("text", f"{WORD} 第{i}章第{j}段 " * 8)
                    for j in range(BLOCKS)]))


@pytest.fixture()
def cache(storage):
    rc = ReadingCache(storage.paths.cache)
    seed_book(rc)
    return rc


@pytest.fixture()
def svc(storage, cache):
    cs = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(cs.on_start())
    assert cs._fts_available, "这台机器的 sqlite 没编 FTS5，测不出索引行为"
    return cs


def _row_count(cs: ContentSearchService) -> int:
    return int(cs._conn().execute(
        "SELECT COUNT(*) FROM content_search WHERE book_url=?", (BOOK,)).fetchone()[0])


# --------------------------------------------------------------------------- #
# 1. 一次提交
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def traced(storage):
    """记录主库上真实发出过的语句文本（``raw`` 就是留给测试内省的口子）。"""
    seen: list[str] = []
    conn = storage.raw
    conn.set_trace_callback(lambda sql: seen.append(sql))
    try:
        yield seen
    finally:
        conn.set_trace_callback(None)


def txn_shape(traces: list[str]) -> dict:
    """把一批 trace 归约成判据：BEGIN/COMMIT 各几条、INSERT 是否全在两者之间。"""
    up = [t.strip().upper() for t in traces]
    begins = [i for i, t in enumerate(up) if t.startswith("BEGIN")]
    commits = [i for i, t in enumerate(up) if t.startswith("COMMIT")]
    inserts = [i for i, t in enumerate(up)
               if t.startswith("INSERT") and "CONTENT_SEARCH" in t]
    return {"begins": len(begins), "commits": len(commits), "inserts": len(inserts),
            "single_txn": len(begins) == 1 and len(commits) == 1,
            "inserts_inside_txn": bool(begins and commits) and all(
                begins[0] < i < commits[-1] for i in inserts)}


def _rebuild_per_row(cs: ContentSearchService) -> int:
    """改前那一版（埋点用）：在调用线程上逐块 ``execute(INSERT)``，不包事务。"""
    conn = cs._conn()
    conn.execute("DELETE FROM content_search WHERE book_url=?", (BOOK,))
    written = 0
    for chapter in cs._cache.load_catalog(SOURCE, BOOK) or []:
        document = cs._cache.load_content(SOURCE, BOOK, chapter.url)
        if document is None:
            continue
        running = 0
        for block in document.blocks:
            if block.kind != "text" or not isinstance(block.value, str):
                continue
            conn.execute(
                "INSERT INTO content_search(book_url, chapter_index, offset, text)"
                " VALUES(?,?,?,?)", (BOOK, int(chapter.index), float(running),
                                     block.value))
            running += len(block.value)
            written += 1
    return written


class TestOneCommitPerRebuild:
    def test_batch_writes_happen_in_a_single_transaction(self, svc, storage):
        """生产形状：整批一笔事务，DELETE 与 INSERT 都在 BEGIN…COMMIT 里。"""
        with traced(storage) as traces:
            written = asyncio.run(svc.rebuild_index(BOOK, SOURCE))
        shape = txn_shape(traces)
        assert written == ROWS, f"应写入 {ROWS} 行，实际 {written}"
        assert _row_count(svc) == ROWS, "落库行数与 written 不符：写入被吞了"
        assert shape["single_txn"], (
            f"整批重建只该有一笔事务，实际 BEGIN {shape['begins']} / "
            f"COMMIT {shape['commits']}（trace={traces}）")
        assert shape["inserts_inside_txn"], (
            f"INSERT 落在了 BEGIN…COMMIT 之外（trace={traces}）")

    def test_per_row_shape_is_caught_not_mimicked(self, svc, storage):
        """反空转：同一段取证必须把"逐条 execute"判成违规。

        上一条若靠"什么都不写"或"BEGIN 恰好数到 1"来成立，这条会一起通过。
        """
        with traced(storage) as traces:
            written = _rebuild_per_row(svc)
        shape = txn_shape(traces)
        assert written == ROWS, "埋点本身要真的写完，否则这条在测空气"
        assert not (shape["single_txn"] and shape["inserts_inside_txn"]), (
            "逐块 INSERT 居然也过了单事务判据——判据失效")
        assert shape["inserts"] == ROWS, (
            f"埋点没造出 {ROWS} 条独立 INSERT（实际 {shape['inserts']}）")


# --------------------------------------------------------------------------- #
# 2. 不许压在调用循环上
# --------------------------------------------------------------------------- #
class _IdentSpyStorage(StorageService):
    """记录每条写语句跑在哪个线程上（判"离开循环"用真实写入口，不 mock 机制）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.record = False
        self.write_idents: list[tuple[str, int]] = []

    def _note(self, verb: str, sql: str) -> None:
        if self.record and sql.strip().upper().startswith(("INSERT", "DELETE")):
            self.write_idents.append((verb, threading.get_ident()))

    def execute(self, sql, params=()):
        self._note("execute", sql)
        return super().execute(sql, params)

    def executemany(self, sql, seq):
        self._note("executemany", sql)
        return super().executemany(sql, seq)


@pytest.fixture()
def spy_storage():
    """一套 _IdentSpyStorage + 两章小书（数据根用短临时目录，避开 Windows MAX_PATH）。"""
    root = Path(tempfile.mkdtemp(prefix="fts85i_"))
    storage = _IdentSpyStorage(AppPaths(data=root / "data", cache=root / "cache"))
    asyncio.run(storage.on_start())
    try:
        cache = ReadingCache(storage.paths.cache)
        seed_book(cache, chapters=2)
        cs = ContentSearchService(storage=storage, cache=cache)
        asyncio.run(cs.on_start())
        yield storage, cache, cs
    finally:
        asyncio.run(storage.on_stop())
        shutil.rmtree(root, ignore_errors=True)


def test_rebuild_does_not_run_on_the_calling_loop(spy_storage):
    """协程里 await 的那次重建，写语句一条都不许落在循环线程上。"""
    storage, cache, cs = spy_storage
    loop_thread: list[int] = []

    async def go():
        loop_thread.append(threading.get_ident())
        storage.record = True
        return await cs.rebuild_index(BOOK, SOURCE)

    assert asyncio.run(go()) == 2 * BLOCKS
    assert storage.write_idents, "一条写语句都没记到，判据在测空气"
    on_loop = [item for item in storage.write_idents
               if item[1] == loop_thread[0]]
    assert not on_loop, f"这些写仍压在界面循环所在的线程上：{on_loop}"


# --------------------------------------------------------------------------- #
# 3. ensure_index 的判据
# --------------------------------------------------------------------------- #
def test_ensure_index_builds_once_and_adds_newly_cached_chapters(svc):
    """空索引→建；缓存没长→不重建；又缓存了新章→补齐。"""
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == ROWS
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == 0, "没新东西却重建了"
    assert _row_count(svc) == ROWS

    cache = svc._cache
    cache.save_catalog(SOURCE, BOOK, cache.load_catalog(SOURCE, BOOK) +
                       [ChapterItem(name="第7章", url="ch6", index=6)])
    cache.save_content(SOURCE, BOOK, "ch6", ChapterContent(
        blocks=[ContentBlock("text", f"{WORD} 新缓存的第七章")], title="第7章"))
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == ROWS + 1, \
        "新章只有 1 块，所以是 24 + 1"
    hits = asyncio.run(svc.search(BOOK, WORD))
    assert {int(h["chapter_index"]) for h in hits} == set(range(CHAPTERS + 1)), (
        f"新缓存的一章没进索引：{sorted({int(h['chapter_index']) for h in hits})}")


def test_ensure_index_never_wipes_the_index_when_cache_shrinks(svc):
    """缓存被限额清理掉时不许重建：那会把整本索引清成 0 行。"""
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == ROWS
    svc._cache.remove_book(SOURCE, BOOK)          # 缓存 0 章 < 索引 6 章
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == 0
    assert _row_count(svc) == ROWS, "少缓存反而把索引清掉了"
    assert asyncio.run(svc.search(BOOK, WORD)), "索引不该是空的"


def test_ensure_index_without_source_url_keeps_existing_index(svc):
    """拿不到 source_url（本地书/会话丢了）时既不动索引，也不清它。"""
    assert asyncio.run(svc.ensure_index(BOOK, SOURCE)) == ROWS
    assert asyncio.run(svc.ensure_index(BOOK, "")) == 0
    assert _row_count(svc) == ROWS


# --------------------------------------------------------------------------- #
# 4. 触发面：工具条点一次搜索，打到底
# --------------------------------------------------------------------------- #
def _start_ui_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(5)
    return loop, thread


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_in_book_search_from_the_toolbar_actually_finds_text(storage):
    """书内搜索是活的：点一次搜索，索引自己长出来，命中渲染进搜索条。

    正面证据走 ``ReaderController.search_in_book``（工具条那个按钮）经 ui_call 编组
    到界面循环上执行，视图与服务都是真的——不是直接调内部协程。"服务面齐全、界面
    永远无命中"正是改前的形状。
    """
    cs = ContentSearchService(storage=storage, cache=ReadingCache(storage.paths.cache))
    asyncio.run(cs.on_start())
    seed_book(cs._cache)
    session = SimpleNamespace(source_url=SOURCE, book_url=BOOK, chapters=[],
                              viewports={}, position=None)
    reading = SimpleNamespace(
        current_session=session, stop_prefetch=lambda: None,
        get_reader_preference=AsyncMock(return_value=None),
        save_reader_preference=AsyncMock(), save_progress=AsyncMock(),
        load_chapters=AsyncMock(return_value=[]), staged_record=lambda: None)
    hub = SimpleNamespace(
        bus=SimpleNamespace(subscribe=lambda *a, **k: (lambda: None),
                            publish=lambda e: []),
        get=lambda stype: {ReadingService: reading, BookmarkService: SimpleNamespace(
            list_bookmarks=AsyncMock(return_value=[])),
            ContentSearchService: cs}[stype])
    view = ReaderView()
    controller = ReaderController(hub)
    controller.attach(view)
    controller._book_url = BOOK

    saved = ui_loop()
    loop, thread = _start_ui_loop()
    set_ui_loop(loop)
    try:
        assert asyncio.run(cs.search(BOOK, WORD)) == [], "开测前索引必须是空的"
        controller.search_in_book(WORD)          # 真实入口，fire-and-forget
        assert _wait_until(lambda: _row_count(cs) == ROWS), "点搜索没把索引建出来"
        rendered = view.content_search_bar._results.controls
        assert _wait_until(lambda: len(rendered) == ROWS), (
            f"命中没渲染进搜索条（{len(rendered)} 条）；状态文案="
            f"{view.content_search_bar._result_count.value!r}")
    finally:
        set_ui_loop(saved)
        controller.detach()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
