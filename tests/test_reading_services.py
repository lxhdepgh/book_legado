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


def _source(url="src-1"):
    """可搜书源替身：search_books 只派发 enabled 且配了 searchUrl 的源。"""
    return SimpleNamespace(bookSourceUrl=url, bookSourceName="源",
                           enabled=True, searchUrl="/q={{key}}",
                           bookSourceGroup="")


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
            on_progress=lambda done, tot, fails: progress.append(
                (done, tot, fails)))

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
    assert sum(done == tot for done, tot, _ in progress) == 1, \
        "中间进度按 0.5s 节流，不是每源刷一次"
    assert progress[-1][2] >= 1, "失败要计数并回给界面"


def test_search_store_seeds_expire_on_start(env):
    """Legado 启动清过期种子：搜索结果只保留一天，跨会话的旧命中不该再当候选。"""
    from app.services.search_model import SearchBookStore

    store = SearchBookStore(conn=env.storage.db)
    store.insert([BookItem(name="旧书", author="甲", url="old-1", origin="src-1")])
    env.storage.db.execute(
        "UPDATE search_books SET saved_at=0 WHERE book_url='old-1'")
    env.storage.db.commit()
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
