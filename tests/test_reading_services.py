"""M2 阅读域服务层测试（不依赖 Flet，契约 §10）。

覆盖：生命周期 / schema v2 表 / 书签 CRUD / 进度落库与恢复 / 事件发布 /
搜索编排 / 整书缓存 / 下载任务 / 书内 FTS 检索 / image 书能力。

数据目录经环境变量重定向到 tmp_path，StorageService/SettingsService 用真实实例，
引擎协作全部用 stub，不发真实网络请求。
"""
from __future__ import annotations

import asyncio
import json
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
    SourceSwitchFinishedEvent,
    SourceSwitchProgressEvent,
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


# --------------------------------------------------------------------------- #
# 10.6 换源的协议门面（G3-D3：switch.* 那一族的服务侧形状）
#
# 这一族钉四件事：**排队规则只有一份**（过滤 + 同站排序都在服务里，界面与 RPC 共用）、
# **job 的三种收尾都发 finished**（扫完 / 叫停 / 起扫前出错——少一种，跨进程那侧的
# 转圈就永远停不下来）、**合作式停止保留已扫出的候选**，以及**快照里没有内部持有者**。
# 事件一律走真 :class:`EventBus`：进度若只存在于服务里的一个数上，那条 ``/ws``
# 就没有第二条路（记忆 ``ui-progress-must-travel-as-events``）。
# --------------------------------------------------------------------------- #
def _sw_source(url, *, group="", enabled=True, search_url="/q", name=None):
    return SimpleNamespace(bookSourceUrl=url, bookSourceName=name or url,
                           enabled=enabled, searchUrl=search_url,
                           bookSourceGroup=group)


class _SwEngine:
    """换源扫描的引擎替身：按源回指定行，并记下**被叫到过**的源。

    ``seen`` 是这一族的关键读数——"停用的源没被扫"这件事只能由"引擎没被叫到"来判，
    由候选条数反推会让"扫了但没命中"与"没扫"混成同一个数。
    """

    def __init__(self, rows=None, *, delay: float = 0.0):
        self.rows = rows or {}
        self.delay = delay
        self.seen: list[str] = []

    async def search(self, source, keyword, page=1):
        self.seen.append(source.bookSourceUrl)
        if self.delay:
            await asyncio.sleep(self.delay)
        return list(self.rows.get(source.bookSourceUrl, []))


class _SwSources:
    """SourceService 替身：库里有哪几条源、按 url 取回哪一条、写回成没成。"""

    def __init__(self, sources, *, raise_list: bool = False):
        self.sources = list(sources)
        self.switch_calls: list = []
        self.raise_list = raise_list

    async def list_sources(self):
        if self.raise_list:
            raise RuntimeError("书库读不到")
        return list(self.sources)

    async def load_source(self, url):
        for source in self.sources:
            if source.bookSourceUrl == url:
                return source
        return None

    def switch_bookshelf_source(self, record, source, book):
        self.switch_calls.append((record, source, book))
        return True


def _sw_service(env, sources, engine) -> ReadingService:
    svc = ReadingService(rule_engine=engine, storage=env.storage,
                         settings=env.settings, source_service=sources)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    return svc


def _collect(sink: list):
    async def handler(event):
        sink.append(event)
    return handler


def test_order_switch_sources_is_the_one_filter_rule() -> None:
    """过滤三条 + 同站优先：这一格是全库唯一的一条排队规则。"""
    sources = [
        _sw_source("http://own.example.com/a"),                       # 当前源：排掉
        _sw_source("http://off.example.com/b", enabled=False),        # 停用：排掉
        _sw_source("http://nosearch.example.com/c", search_url="   "),  # 无搜索规则
        _sw_source("http://www.own.example.com/d"),                   # 与当前同站
        _sw_source("http://other.example.com/e"),
    ]
    pool = ReadingService.order_switch_sources(sources, "http://own.example.com/a")
    assert [s.bookSourceUrl for s in pool] == [
        "http://www.own.example.com/d", "http://other.example.com/e"]
    # 正对照：同站那一格**确实**是因为同站才排前的——换一个 own_url，同两条行的
    # 先后就反过来（那时只剩字典序）。少了这一发，"排前"可能是字典序顺手给的。
    swapped = ReadingService.order_switch_sources(sources, "http://other.example.com/e")
    assert [s.bookSourceUrl for s in swapped] == [
        "http://own.example.com/a", "http://www.own.example.com/d"]


def test_order_switch_sources_group_absent_scans_nothing() -> None:
    """分组打错**不回退到全部启用源**：回退等于用一个错组名把整库联网扫一遍。"""
    sources = [_sw_source("src-a", group="甲"), _sw_source("src-b", group="乙")]
    assert [s.bookSourceUrl for s in
            ReadingService.order_switch_sources(sources, "", "甲")] == ["src-a"]
    assert ReadingService.order_switch_sources(sources, "", "打错的组") == []


def test_switch_scan_scans_only_the_queue_and_delivers_ranked_candidates(env) -> None:
    """一轮扫描：进度与收尾都带客户端那个 switch_id，被过滤的源一次都没被叫到。"""
    sources = [
        _sw_source("src-own"),
        _sw_source("src-a", group="换源", name="源甲"),
        _sw_source("src-b", group="换源", name="源乙"),
        _sw_source("src-c", group="换源", name="源丙"),
        _sw_source("src-off", group="换源", enabled=False, name="源停"),
        _sw_source("src-no", group="换源", search_url="", name="源无规则"),
    ]
    hit = lambda name, author, url, origin: BookItem(  # noqa: E731
        name=name, author=author, url=url, origin=origin)
    engine = _SwEngine({
        "src-a": [hit("换源目标书", "作者戊", "b-a", "src-a"),
                  hit("换源目标书 卷二", "作者戊", "b-a2", "src-a")],
        "src-b": [hit("换源目标书", "作者己", "b-b", "src-b")],
        "src-c": [hit("旁书一本", "作者庚", "b-c", "src-c")],
        # 这两条源**会**命中：它们出现在替身的行表里，却绝不能出现在引擎被叫到的名单里。
        "src-off": [hit("换源目标书", "作者戊", "b-off", "src-off")],
        "src-no": [hit("换源目标书", "作者戊", "b-no", "src-no")],
    })
    svc = _sw_service(env, _SwSources(sources), engine)
    progress, finished = [], []
    env.bus.subscribe(SourceSwitchProgressEvent, _collect(progress))
    env.bus.subscribe(SourceSwitchFinishedEvent, _collect(finished))
    run = asyncio.run(svc.switch_scan(
        {"name": "换源目标书", "author": "作者戊", "url": "b-own",
         "source_url": "src-own", "origin": "src-own"},
        switch_id=42, group="换源"))
    _drain(env.pending)

    assert sorted(engine.seen) == ["src-a", "src-b", "src-c"], "停用的与没规则的没被扫"
    assert run["total"] == 3 and run["done"] == 3
    assert len(finished) == 1, "收尾必须恰好一条：两条会让界面的计数抖一下"
    event = finished[0]
    assert event.switch_id == "42" and event.cancelled is False and event.error == ""
    # 排序三判据各有一行证据：精确同名 + 作者对得上 > 精确同名 > 只包含关键词
    assert [(c["source_url"], c["book"]["url"]) for c in event.candidates] == [
        ("src-a", "b-a"), ("src-b", "b-b"), ("src-a", "b-a2")]
    assert progress and all(p.switch_id == "42" and p.total == 3 for p in progress)
    assert progress[-1].done == 3 and progress[-1].failures == 0
    snapshot = asyncio.run(svc.switch_snapshot())
    assert snapshot["candidates"] == event.candidates, "快照与事件必须是同一批候选"


def test_switch_scan_group_absent_yields_an_empty_round(env) -> None:
    """打错的分组：``total=0``、引擎一次没被叫到、仍然有一条 finished。"""
    engine = _SwEngine({})
    svc = _sw_service(env, _SwSources([_sw_source("src-a", group="甲")]), engine)
    finished = []
    env.bus.subscribe(SourceSwitchFinishedEvent, _collect(finished))
    run = asyncio.run(svc.switch_scan(
        {"name": "书名", "url": "u"}, switch_id=1, group="不存在的组"))
    _drain(env.pending)
    assert engine.seen == [] and run["total"] == 0 and run["candidates"] == []
    assert len(finished) == 1 and finished[0].error == ""


def test_switch_scan_still_delivers_finished_when_the_sweep_raises(env) -> None:
    """起扫前就出错也要发 finished：不发，界面上那个转圈就永远停不下来。"""
    svc = _sw_service(env, _SwSources([], raise_list=True), _SwEngine({}))
    finished, progress = [], []
    env.bus.subscribe(SourceSwitchFinishedEvent, _collect(finished))
    env.bus.subscribe(SourceSwitchProgressEvent, _collect(progress))
    run = asyncio.run(svc.switch_scan({"name": "书名", "url": "u"}, switch_id=3))
    _drain(env.pending)
    assert len(finished) == 1 and finished[0].candidates == []
    assert "书库读不到" in finished[0].error
    assert run["running"] is False and progress == []


def test_switch_cancel_keeps_the_candidates_already_scanned(env, monkeypatch) -> None:
    """合作式叫停：已扫出的候选留下，剩下的源不再取，``done < total``。"""
    from app.services.reading import reading_service as rs
    monkeypatch.setattr(rs, "SWITCH_CONCURRENCY", 1)
    sources = [_sw_source(f"src-{i:02d}", group="甲") for i in range(20)]
    engine = _SwEngine({s.bookSourceUrl: [BookItem(
        name="书名", author="作者", url=f"b-{s.bookSourceUrl}",
        origin=s.bookSourceUrl)] for s in sources}, delay=0.01)
    svc = _sw_service(env, _SwSources(sources), engine)

    async def drive():
        task = asyncio.ensure_future(
            svc.switch_scan({"name": "书名", "url": "u"}, switch_id=5, group="甲"))
        while len(engine.seen) < 3:
            await asyncio.sleep(0.005)
        assert svc.switch_cancel() == {"cancelled": True}
        return await task

    run = asyncio.run(drive())
    assert run["cancelled"] is True and run["done"] < run["total"]
    assert 0 < len(run["candidates"]) < run["total"], "停止不能把已扫出的候选一起扔了"
    assert len(engine.seen) < 20, "叫停之后还在取新源 = 那一发停止没接上线"
    # 正对照（反向）：没有轮次在跑时那一发回 False，界面别写"已停止"
    assert svc.switch_cancel() == {"cancelled": False}


def test_a_second_scan_takes_over_the_round_already_running(env, monkeypatch) -> None:
    """一轮只跑一条：新的一轮先合作式叫停旧的，两边的 finished 各自带自己的号。"""
    from app.services.reading import reading_service as rs
    monkeypatch.setattr(rs, "SWITCH_CONCURRENCY", 1)
    sources = [_sw_source(f"src-{i:02d}", group="甲") for i in range(20)]
    engine = _SwEngine({s.bookSourceUrl: [BookItem(
        name="书名", author="作者", url=f"b-{s.bookSourceUrl}",
        origin=s.bookSourceUrl)] for s in sources}, delay=0.01)
    svc = _sw_service(env, _SwSources(sources), engine)
    record = {"name": "书名", "url": "u"}

    async def drive():
        first = asyncio.ensure_future(svc.switch_scan(record, switch_id=1, group="甲"))
        while len(engine.seen) < 2:
            await asyncio.sleep(0.005)
        second = asyncio.ensure_future(svc.switch_scan(record, switch_id=2, group="甲"))
        return await first, await second

    first_run, second_run = asyncio.run(drive())
    assert first_run["cancelled"] is True and second_run["cancelled"] is False
    assert asyncio.run(svc.switch_snapshot())["switch_id"] == "2"
    # 两份收尾分得清是谁的：混成一个号，界面就会把上一轮的迟到结果当本轮渲染
    assert {first_run["switch_id"], second_run["switch_id"]} == {"1", "2"}


def test_switch_snapshot_is_plain_json_without_the_internal_gate(env) -> None:
    """快照里不许带内部持有者：那个收口闸门只归服务自己用。"""
    svc = _sw_service(env, _SwSources([_sw_source("src-a", group="甲")]),
                      _SwEngine({}))
    asyncio.run(svc.switch_scan({"name": "书名", "url": "u"}, switch_id=9, group="甲"))
    _drain(env.pending)
    snapshot = asyncio.run(svc.switch_snapshot())
    assert "finished" not in snapshot
    wire = json.dumps(snapshot, ensure_ascii=False)
    assert "Event" not in wire, f"闸门漏进了 JSON 面：{wire[:120]}"
    # 正对照：这一发里确实有一个非空的内部键在别处（服务对象上），只是不上协议面
    assert svc._switch_run["finished"] is not None


def test_apply_switch_resolves_the_source_and_restores_the_book(env) -> None:
    """写回那一发：源按 url 取回对象，书快照在服务边界还原成 BookItem。"""
    sources = [_sw_source("src-a")]
    store = _SwSources(sources)
    svc = _sw_service(env, store, _SwEngine({}))
    out = asyncio.run(svc.apply_switch(
        {"url": "b-old", "source_url": "src-old"}, "src-a",
        {"name": "书", "author": "作者", "url": "b-a"}))
    assert out == {"applied": True, "source_url": "src-a"}
    _, source, book = store.switch_calls[0]
    assert source is sources[0], "按 url 取回的必须是库里那条源对象"
    assert isinstance(book, BookItem) and book.url == "b-a", (
        "字典没还原就在写回里取空串——引擎和书架都从这一格读 url")
    # 源不存在时**抛穿**而不是回 False：三种失败成因不能折成一句"没换成"
    with pytest.raises(ValueError):
        asyncio.run(svc.apply_switch({"url": "x"}, "src-missing", {"name": "书"}))


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
