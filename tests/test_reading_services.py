"""M2 阅读域服务层测试（不依赖 Flet，契约 §10）。

覆盖：生命周期 / schema v2 表 / 书签 CRUD / 进度落库与恢复 / 事件发布 /
搜索编排 / 整书缓存 / 下载任务 / 书内 FTS 检索 / image 书能力。

数据目录经环境变量重定向到 tmp_path，StorageService/SettingsService 用真实实例，
引擎协作全部用 stub，不发真实网络请求。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import (
    BookmarkChangedEvent,
    DownloadProgressEvent,
    ProgressSavedEvent,
    ReadingSessionChangedEvent,
    SearchFinishedEvent,
)
from app.core.lifecycle import ServiceState
from app.core.settings import SettingsService
from app.core.storage import SCHEMA_VERSION, StorageService
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.reading.book_cache_service import BookCacheService
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.download_service import DownloadService
from app.services.reading.image_book_service import ImageBookService
from app.services.reading.reading_service import ReadingPosition, ReadingService


# --------------------------------------------------------------------------- #
# 测试夹具与替身
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(monkeypatch):
    """起一个真实 StorageService + SettingsService（短路径 tmp 数据根），返回句柄。

    说明：reader-cache 落地为 ``<cache>/reader-cache/<64hex>/content/<64hex>.json``，
    若用 pytest 默认 tmp_path（嵌套在项目深层 basetemp 下）会叠加 Windows MAX_PATH
    (260) 导致写文件失败；这里改用系统短临时目录，贴近真实安装路径长度。
    """
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="m2read_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))
    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    settings = SettingsService(storage=storage, engine=engine)
    asyncio.run(settings.on_start())
    bus = EventBus(engine=engine)
    # 捕获 publish 产生的 future，便于同步断言事件。
    pending: list = []
    orig_publish = bus.publish

    def spy(ev):
        fs = orig_publish(ev)
        pending.extend(fs)
        return fs

    bus.publish = spy  # type: ignore[method-assign]
    try:
        yield SimpleNamespace(
            engine=engine, storage=storage, settings=settings, bus=bus,
            pending=pending, root=root)
    finally:
        asyncio.run(settings.on_stop())
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _drain(pending) -> None:
    for f in list(pending):
        f.result(timeout=5)
    pending.clear()


def _book(url="book-1", name="书名", author="作者"):
    return SimpleNamespace(url=url, name=name, author=author,
                           cover_url="", kind="text", variables={})


def _source(url="src-1", group="", enabled=True, search_url="/q={{key}}"):
    """可搜书源替身：search_books 只派发 enabled 且配了 searchUrl 的源。"""
    return SimpleNamespace(bookSourceUrl=url, bookSourceName="源",
                           enabled=enabled, searchUrl=search_url,
                           bookSourceGroup=group)


class _StubEngine:
    """ReadingService 协作替身：目录/正文/搜索均不联网。"""

    def __init__(self, chapters=None, content=None, search_items=None):
        self._chapters = chapters or []
        self._content = content
        self._search_items = search_items or []
        self.content_calls: list = []

    async def chapters(self, source, book):
        return list(self._chapters)

    async def content_document(self, source, chapter, next_url=""):
        self.content_calls.append(chapter)
        return self._content

    async def search(self, source, keyword, page=1):
        return list(self._search_items)


# --------------------------------------------------------------------------- #
# 1. schema v2 表结构
# --------------------------------------------------------------------------- #
def test_schema_v2_tables_exist(env):
    # v3（audit_log，M3 书源域审计历史）在 v2 之上追加，向后兼容；v2 表结构不变。
    assert SCHEMA_VERSION >= 2
    conn = env.storage.db
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "books" in tables
    assert "bookmarks" in tables
    cols = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
    for expected in ("book_url", "source_url", "name", "author", "kind",
                    "cover_url", "chapter_index", "scroll_offset",
                    "scroll_ratio", "chapter_pos", "font_size",
                    "in_bookshelf", "shelf_group", "updated_at"):
        assert expected in cols, f"books 缺列 {expected}"


# --------------------------------------------------------------------------- #
# 2. ReadingService 生命周期
# --------------------------------------------------------------------------- #
def test_reading_service_lifecycle(env):
    svc = ReadingService(rule_engine=_StubEngine(), storage=env.storage,
                         settings=env.settings)
    svc.bus = env.bus
    asyncio.run(svc.start())
    assert svc.status.state is ServiceState.RUNNING
    assert svc.current_book is None
    assert isinstance(svc.current_session.position, ReadingPosition)
    asyncio.run(svc.stop())
    assert svc.status.state is ServiceState.DISPOSED


# --------------------------------------------------------------------------- #
# 3. 进度落库与恢复
# --------------------------------------------------------------------------- #
def test_save_and_restore_position_roundtrip(env):
    svc = ReadingService(rule_engine=_StubEngine(), storage=env.storage,
                         settings=env.settings)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    svc._current_source = _source()
    book = _book("book-p")
    svc._current_book = book

    pos = ReadingPosition(
        source_url="src-1", book_url="book-p", chapter_url="c0",
        chapter_name="序章", chapter_index=3, chapter_offset=120.5,
        chapter_ratio=0.4, character_offset=500, font_size=18,
        updated_at=1000)
    asyncio.run(svc.save_progress(3, pos))

    restored = asyncio.run(svc.restore_position(book))
    assert restored is not None
    assert restored.chapter_index == 3
    assert abs(restored.chapter_offset - 120.5) < 1e-6
    assert abs(restored.chapter_ratio - 0.4) < 1e-6
    assert restored.character_offset == 500
    assert restored.font_size == 18

    # 无记录返回 None
    assert asyncio.run(svc.restore_position(_book("no-such"))) is None


# --------------------------------------------------------------------------- #
# 4. ProgressSavedEvent 发布
# --------------------------------------------------------------------------- #
def test_progress_saved_event_published(env):
    svc = ReadingService(rule_engine=_StubEngine(), storage=env.storage,
                         settings=env.settings)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    svc._current_source = _source()
    svc._current_book = _book("book-ev")
    pos = ReadingPosition(source_url="s", book_url="book-ev", chapter_index=1)
    asyncio.run(svc.save_progress(1, pos))
    _drain(env.pending)
    # 通过重新订阅更稳妥：在新订阅后再发一次并断言
    fired: list[ProgressSavedEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(ProgressSavedEvent, collector)
    asyncio.run(svc.save_progress(2, ReadingPosition(
        source_url="s", book_url="book-ev", chapter_index=2)))
    _drain(env.pending)
    assert len(fired) == 1
    assert fired[0].book_url == "book-ev"
    assert fired[0].chapter_index == 2
    assert fired[0].position["scroll_ratio"] == 0.0


# --------------------------------------------------------------------------- #
# 5. 书签 CRUD
# --------------------------------------------------------------------------- #
def test_bookmark_crud(env):
    svc = BookmarkService(storage=env.storage)
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    bid = asyncio.run(svc.add_bookmark("book-bm", chapter_index=2,
                                       offset=30.0, summary="读到这里"))
    assert bid > 0
    items = asyncio.run(svc.list_bookmarks("book-bm"))
    assert len(items) == 1
    assert items[0]["summary"] == "读到这里"
    assert items[0]["chapter_index"] == 2

    assert asyncio.run(svc.remove_bookmark("book-bm", bid)) is True
    assert asyncio.run(svc.list_bookmarks("book-bm")) == []


# --------------------------------------------------------------------------- #
# 6. BookmarkChangedEvent 发布
# --------------------------------------------------------------------------- #
def test_bookmark_changed_event(env):
    svc = BookmarkService(storage=env.storage)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    fired: list[BookmarkChangedEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(BookmarkChangedEvent, collector)
    bid = asyncio.run(svc.add_bookmark("book-ev", 0, 0.0, "x"))
    asyncio.run(svc.remove_bookmark("book-ev", bid))
    _drain(env.pending)
    ops = [e.op for e in fired if e.book_url == "book-ev"]
    assert ops == ["add", "remove"]


# --------------------------------------------------------------------------- #
# 7. 阅读器偏好并入 SettingsService
# --------------------------------------------------------------------------- #
def test_reader_preference_roundtrip(env):
    svc = ReadingService(rule_engine=_StubEngine(), storage=env.storage,
                         settings=env.settings)
    asyncio.run(svc.on_start())
    asyncio.run(svc.save_reader_preference("font_size", 22))
    assert asyncio.run(svc.get_reader_preference("font_size")) == 22
    # 键带 reader. 前缀落库
    assert env.settings.get("reader.font_size") == 22
    assert asyncio.run(svc.get_reader_preference("missing", "d")) == "d"


# --------------------------------------------------------------------------- #
# 8. open_book 会话 + ReadingSessionChangedEvent
# --------------------------------------------------------------------------- #
def test_open_book_session_and_event(env):
    chapters = [ChapterItem(name="序", url="c0", index=0),
                ChapterItem(name="第一章", url="c1", index=1)]
    engine = _StubEngine(chapters=chapters)
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings)
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    fired: list[ReadingSessionChangedEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(ReadingSessionChangedEvent, collector)
    session = asyncio.run(svc.open_book(_source(), _book("book-open")))
    _drain(env.pending)

    assert len(session.chapters) == 2
    assert svc.current_book.url == "book-open"
    assert len(fired) == 1
    assert fired[0].book_url == "book-open"


# --------------------------------------------------------------------------- #
# 9. 目录缓存命中
# --------------------------------------------------------------------------- #
def test_load_chapters_cache_then_engine(env):
    chapters = [ChapterItem(name="序", url="c0", index=0)]
    engine = _StubEngine(chapters=chapters)
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings)
    asyncio.run(svc.on_start())
    svc._current_source = _source()
    book = _book("book-cache-toc")

    first = asyncio.run(svc.load_chapters(book))
    assert len(first) == 1
    # 第二次走缓存，引擎不再被调用
    engine._chapters = []
    second = asyncio.run(svc.load_chapters(book))
    assert len(second) == 1


# --------------------------------------------------------------------------- #
# 10. 搜索编排 + SearchFinishedEvent
# --------------------------------------------------------------------------- #
def test_search_books_publishes_search_finished(env):
    items = [BookItem(name="天龙八部", author="金庸", url="b1",
                      origin="src-1"),
             BookItem(name="天龙八部续", author="金庸", url="b2",
                      origin="src-2")]
    engine = _StubEngine(search_items=items)
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings)
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    fired: list[SearchFinishedEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(SearchFinishedEvent, collector)
    result = asyncio.run(svc.search_books("天龙", [_source()], search_id=7))
    _drain(env.pending)
    assert len(result) == 2
    # 逐源增量（finished=False）先到，最后一条 finished=True 收口带全部结果
    assert all(ev.search_id == "7" for ev in fired)
    settled = [ev for ev in fired if ev.finished]
    assert len(settled) == 1
    assert len(settled[0].items) == 2
    partials = [len(ev.items) for ev in fired if not ev.finished]
    assert partials == sorted(partials)      # 增量只增不减，界面不会回退


# --------------------------------------------------------------------------- #
# 10.5 换源扫描：有界并发 + 每源超时 + 进度反馈
# --------------------------------------------------------------------------- #
def test_switch_source_scans_concurrently_with_timeout(env, monkeypatch):
    from app.services.engine.rule_engine import RequestConnectionError
    from app.services.reading import reading_service as rs
    monkeypatch.setattr(rs, "SWITCH_TIMEOUT", 0.2)

    total = 40
    slow, broken = {"src-7"}, {"src-9"}
    sources = [SimpleNamespace(bookSourceUrl=f"src-{i}", bookSourceName=f"源{i}",
                               enabled=True, searchUrl="/q")
               for i in range(total)]

    class _ScanEngine:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.seen: list[str] = []

        async def search(self, source, keyword, page=1):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.seen.append(source.bookSourceUrl)
            try:
                if source.bookSourceUrl in slow:
                    await asyncio.sleep(5)      # 远超 SWITCH_TIMEOUT：必须由扫描切断
                if source.bookSourceUrl in broken:
                    raise RequestConnectionError("拒绝连接")
                return [BookItem(name=keyword, author="作者",
                                 url=f"b-{source.bookSourceUrl}",
                                 origin=source.bookSourceUrl)]
            finally:
                self.active -= 1

    engine = _ScanEngine()
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings)
    asyncio.run(svc.on_start())
    progress: list[tuple] = []

    async def scan():
        return await svc.switch_source(
            {"name": "书名", "author": "作者", "url": "b-src-1",
             "source_url": "src-1"},
            sources, keyword="书名",
            on_progress=lambda done, tot, fails, starved: progress.append(
                (done, tot, fails, starved)))

    started = time.monotonic()
    matches = asyncio.run(scan())
    elapsed = time.monotonic() - started

    assert len(engine.seen) == total, "每个候选源都要扫到"
    assert 1 < engine.max_active <= rs.SWITCH_CONCURRENCY, \
        f"应并发扫描且受上限约束，实测峰值 {engine.max_active}"
    assert elapsed < 5, "慢源被单源超时切断，不拖整轮"
    # 单源失败只计数不中止：39 个源命中、1 个失败（src-7 超时、src-9 连接错）
    assert len(matches) == total - 2
    assert progress and progress[-1][0] == total
    assert sum(done == tot for done, tot, *_ in progress) == 1, \
        "中间进度按 0.5s 节流，不是每源刷一次"
    assert progress[-1][2] >= 1, "失败要计数并回给界面"
    assert progress[-1][3] == 0, "空等网络不是本机的错，不该记成繁忙"


def test_switch_timeout_separates_machine_busy_from_slow_source(env, monkeypatch):
    """同一个"超时"要拆成两种错：源慢，还是我们把本机解析挤爆了。

    判据是超时窗口内**本线程 CPU 占墙钟的比例**（:func:`_switch_starved`），所以
    这条把它做成三路差分：前两个源都由同一个 ``asyncio.wait_for`` 掐断、都由
    ``_switch_failure_reason`` 归一成同一个"超时"，唯一差别是一个只在空等处时间、
    另一个在事件循环上烧 CPU；第三个源烧完 CPU 后抛真错，用来钉"繁忙"只改判超时，
    不许把网络错误也一起洗成"本机忙"。混成一个数时界面写"38 个失败"，用户去删的
    却是 38 个好源，所以失败数必须**排除**繁忙数。
    """
    from app.services.reading import reading_service as rs
    from app.services.engine.rule_engine import RequestConnectionError
    monkeypatch.setattr(rs, "SWITCH_TIMEOUT", 0.3)
    monkeypatch.setattr(rs, "SWITCH_CONCURRENCY", 1)   # 串行：三个窗口互不掺时间

    sources = [SimpleNamespace(bookSourceUrl=f"src-{i}", bookSourceName=f"源{i}",
                               enabled=True, searchUrl="/q") for i in (1, 2, 3)]

    class _Engine:
        async def search(self, source, keyword, page=1):
            if source.bookSourceUrl == "src-1":
                await asyncio.sleep(5)               # 纯空等：慢的是对面那台站
                return []
            if source.bookSourceUrl == "src-2":
                until = time.perf_counter() + 5      # 远超 SWITCH_TIMEOUT：必被掐断
                while time.perf_counter() < until:
                    sum(i * i for i in range(3000))  # 忙的是我们自己的解析线程
                    await asyncio.sleep(0)           # 让出一次，超时才掐得断
                return []
            until = time.perf_counter() + 0.1        # 在超时**之内**跑完：不是超时
            while time.perf_counter() < until:
                sum(i * i for i in range(3000))
                await asyncio.sleep(0)
            raise RequestConnectionError("拒绝连接")

    svc = ReadingService(rule_engine=_Engine(), storage=env.storage,
                         settings=env.settings)
    asyncio.run(svc.on_start())
    progress: list[tuple] = []

    asyncio.run(svc.switch_source(
        {"name": "书名", "source_url": "src-0"}, sources, keyword="书名",
        on_progress=lambda done, tot, fails, starved: progress.append(
            (done, tot, fails, starved))))

    assert progress[-1] == (3, 3, 2, 1), (
        f"空等的那个和真错的那个都算失败，只有烧 CPU 的超时算繁忙（实测 {progress[-1]}）")


def test_switch_never_runs_more_workers_than_the_cap(env, monkeypatch):
    """「有界并发」的全部含义在这个上限上：漏了它，20 个源会一起涌进唯一的事件循环线程。

    断言写成等号而不是 ``<=``：只钉"没超过"的话，把 worker 数写成 1 也算绿，
    而那时候整轮扫描要跑几十秒——上限的意义是**用满**它。
    """
    from app.services.reading import reading_service as rs
    monkeypatch.setattr(rs, "SWITCH_CONCURRENCY", 3)

    total = 20
    sources = [SimpleNamespace(bookSourceUrl=f"src-{i}", bookSourceName=f"源{i}",
                               enabled=True, searchUrl="/q") for i in range(total)]

    class _Engine:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.seen: list[str] = []

        async def search(self, source, keyword, page=1):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.seen.append(source.bookSourceUrl)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.active -= 1
            return []

    engine = _Engine()
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings)
    asyncio.run(svc.on_start())
    asyncio.run(svc.switch_source({"name": "书名", "source_url": "src-0"},
                                  sources, keyword="书名"))

    assert engine.max_active == 3, f"并发峰值应是上限值，实测 {engine.max_active}"
    assert len(engine.seen) == total, "每个候选源都要扫到"


def test_search_store_seeds_expire_on_start(env):
    """Legado 启动清过期种子：搜索结果只保留一天，跨会话的旧命中不该再当候选。"""
    from app.services.search_model import SearchBookStore

    store = SearchBookStore(conn=env.storage.db)
    store.insert([BookItem(name="旧书", author="甲", url="old-1", origin="src-1")])
    env.storage.db.execute(
        "UPDATE search_books SET saved_at=0 WHERE book_url='old-1'")
    # 不 commit：主库是 isolation_level=None（autocommit），这条 UPDATE 已经落盘；
    # 而裸 commit() 落在别人未提交的事务上会替别人提交半成品。
    assert store.get("old-1") is not None

    svc = ReadingService(rule_engine=_StubEngine(), storage=env.storage,
                         settings=env.settings, search_store=store)
    asyncio.run(svc.on_start())
    assert store.get("old-1") is None, "启动应清掉超过 TTL 的种子行"


# --------------------------------------------------------------------------- #
# 11. 书内 FTS 检索
# --------------------------------------------------------------------------- #
def test_content_search_rebuild_and_match(env):
    from app.services.reading_cache import ReadingCache
    cache = ReadingCache(env.storage.paths.cache)
    chapters = [ChapterItem(name="一", url="ch1", index=0),
                ChapterItem(name="二", url="ch2", index=1)]
    cache.save_catalog("src-1", "book-fts", chapters)
    cache.save_content("src-1", "book-fts", "ch1", ChapterContent(
        blocks=[ContentBlock("text", "苹果很好吃 梨子也不错")], title="一"))
    cache.save_content("src-1", "book-fts", "ch2", ChapterContent(
        blocks=[ContentBlock("text", "今天天气晴朗")], title="二"))

    svc = ContentSearchService(storage=env.storage, cache=cache)
    asyncio.run(svc.on_start())
    written = asyncio.run(svc.rebuild_index("book-fts", source_url="src-1"))
    assert written >= 1
    hits = asyncio.run(svc.search("book-fts", "苹果"))
    assert hits, "应命中含苹果的章节"
    assert hits[0]["chapter_index"] == 0
    # 未缓存章节不建索引：搜 ch2 关键词之外的词命中数为 0
    sunny = asyncio.run(svc.search("book-fts", "天气"))
    assert len(sunny) == 1


# --------------------------------------------------------------------------- #
# 12. 整书缓存（假引擎 + 真实 reader-cache）
# --------------------------------------------------------------------------- #
class _FakeCacheEngine:
    def __init__(self, chapters):
        self._chapters = chapters
        self.js_variables: dict = {}
        self.current_book = None
        self.closed = False

    async def chapters(self, source, book):
        return list(self._chapters)

    async def content_document(self, source, chapter, next_url=""):
        return ChapterContent(
            blocks=[ContentBlock("text", f"正文-{chapter.index}")],
            title=chapter.name)

    async def aclose(self):
        self.closed = True


def test_book_cache_service_caches_chapters(env):
    chapters = [ChapterItem(name="一", url="ch1", index=0),
                ChapterItem(name="二", url="ch2", index=1)]
    svc = BookCacheService(
        storage=env.storage,
        engine_factory=lambda: _FakeCacheEngine(chapters))
    asyncio.run(svc.on_start())
    result = asyncio.run(svc.cache_book(_source(), _book("book-cache-whole")))
    assert result.total == 2
    assert result.cached == 2
    assert result.failed == 0
    assert svc.is_pinned("src-1", "book-cache-whole")


# --------------------------------------------------------------------------- #
# 13. 下载任务注册 / 进度事件 / 取消
# --------------------------------------------------------------------------- #
class _StubBookCache:
    def __init__(self):
        self.started = False

    async def cache_book(self, source, book, on_progress=None):
        self.started = True

        class P:
            total = 2
            cached = 0
            failed = 0
            current = ""
            running = True

        p = P()
        p.cached = 1
        if on_progress:
            on_progress(p)
        p.cached = 2
        p.running = False
        if on_progress:
            on_progress(p)
        return p


def test_download_enqueue_and_progress(env):
    cache = _StubBookCache()
    svc = DownloadService(book_cache=cache)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    fired: list[DownloadProgressEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(DownloadProgressEvent, collector)

    async def scenario():
        tid = svc.enqueue("whole_cache", "book-dl",
                          {"source": _source(), "book": _book("book-dl")})
        await asyncio.sleep(0.05)  # 让 ensure_future 任务跑完
        return tid

    tid = asyncio.run(scenario())
    _drain(env.pending)
    assert cache.started is True
    tasks = svc.list_tasks()
    assert any(t.task_id == tid and t.status == "done" for t in tasks)
    assert any(e.task_id == tid for e in fired)
    # task_report 同步总数/完成数
    assert svc.task_report["total"] >= 1


def test_download_cancel(env):
    svc = DownloadService(book_cache=_StubBookCache())
    asyncio.run(svc.on_start())
    tid = svc.enqueue("whole_cache", "book-x", {})
    assert svc.cancel(tid) is True
    tasks = {t.task_id: t for t in svc.list_tasks()}
    assert tasks[tid].status == "cancelled"


# --------------------------------------------------------------------------- #
# 14. image 书服务能力
# --------------------------------------------------------------------------- #
def test_image_book_service_chapter_images_and_filter(env):
    content = ChapterContent(blocks=[
        ContentBlock("text", "正文"),
        ContentBlock("image", "https://cdn.example.com/a.png"),
        ContentBlock("image", "data:image/png;base64,inline"),
    ])
    engine = _StubEngine(content=content)
    svc = ImageBookService(engine=engine)
    asyncio.run(svc.on_start())
    urls = asyncio.run(svc.chapter_images(_book(), ChapterItem(name="c", url="c0")))
    assert urls == ["https://cdn.example.com/a.png"]

    # 色彩滤镜：Pillow 灰度 / 电子墨水
    from PIL import Image
    img = Image.new("RGB", (4, 4), (255, 0, 0))
    gray = ImageBookService.apply_color_filter(img, "gray")
    assert gray.mode == "RGB"
    ink = ImageBookService.apply_color_filter(img, "ink")
    assert ink.mode == "RGB"


# --------------------------------------------------------------------------- #
# 15. 搜索范围口径（search.scopes 的唯一算法来源）
# --------------------------------------------------------------------------- #
def _scope_corpus():
    """四种形状各一条：无组 / 空白组 / 跨两组 / 组里只有停用源。

    刻意不造"每条源只属一个组"的语料：那种形状下 `__ungrouped__` 与"各组并集"
    会给出同一个答案，一个把未分组判成全部书源的实现也能全绿（记忆
    measurement-corpus-must-not-be-self-proving）。
    """
    return [
        _source("ung", group=""),
        _source("blank", group="  "),
        _source("both", group="B组,A组"),
        _source("offgrp", group="C组", enabled=False),
        _source("nosearch", group="", search_url="  "),
    ]


def test_scope_ungrouped_is_not_the_same_as_all():
    sources = _scope_corpus()
    names = lambda pool: [s.bookSourceUrl for s in pool]

    everything = names(ReadingService.searchable_sources(sources, "__all__"))
    ungrouped = names(ReadingService.searchable_sources(sources, "__ungrouped__"))

    # 三条可搜源；"未分组"只能拿到那两条没有任何组名的。
    assert everything == ["ung", "blank", "both"]
    assert ungrouped == ["ung", "blank"]
    # 反面的反面：把哨兵当成"不过滤"的实现，这两栏会长成一样。
    assert ungrouped != everything

    # 空值与 __all__ 同义（控制器那边传过空串来）。
    assert names(ReadingService.searchable_sources(sources, "")) == everything
    # 组名两侧都 strip：源侧 "B组,A组"、筛选侧 " A组 " 命中同一条。
    assert names(ReadingService.searchable_sources(sources, " A组 ")) == ["both"]
    # 不存在的组是"一条都不搜"，不是"退化成全部"。
    assert ReadingService.searchable_sources(sources, "D组") == []


def test_scope_table_lists_every_selectable_scope_with_searchable_counts():
    rows = ReadingService.scope_table(_scope_corpus())

    # 顺序 = 下拉顺序：两个哨兵在前，组名排序去重在后（flet 那侧同样 sorted(set)）。
    assert [r["scope"] for r in rows] == ["__all__", "__ungrouped__",
                                          "A组", "B组", "C组"]
    assert [r["count"] for r in rows] == [3, 2, 1, 1, 0]
    # C组 那一格是 0：**可选范围来自全部源，条数来自可搜源**。把两者混成一个口径
    # （先过滤再列组）会让"C组"整个从下拉里消失，用户看到的是"我的组没了"。

    # 空白组不算一个组：分组名集合里没有 ""，也没有 "  "。
    assert all(r["scope"].strip() for r in rows)

    # 没有源就没有组：只剩两个哨兵，各 0 条。
    assert ReadingService.scope_table([]) == [
        {"scope": "__all__", "count": 0}, {"scope": "__ungrouped__", "count": 0}]
