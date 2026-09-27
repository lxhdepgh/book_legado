"""连续滚动自动续章的集成护栏：真 ReadingService + 真缓存 + 真 ReaderController。

单元测试（tests/test_views_reading_m2.py）用替身钉住了控制器的编排时序，这个文件
补上只有真服务才暴露得出来的三件事：

1. ``load_content(chapter, append=True)`` 与 reader-cache 的相互作用——追加走的是
   "跳过缓存读、照常写缓存"那条臂，两章内容都得进缓存且互不覆盖；
2. 进度落库跟得上：自动接上第二章之后 books 表里的 chapter_index 必须是第二章，
   否则下次打开书回到第一章，等于白读；
3. 续读定位模型（ReadingSession 视口表）在真会话上成立：track() 能把文档偏移
   换算回正确的章节，标题与工具栏跟着走；
4. 挂出一章会触发服务侧后台预取（章尾接的那一章已经在手里），滚动下发走的是
   真协程而不是被丢掉的对象。

全程离线：引擎协作用替身，不发真实网络请求（见记忆 legado-live-scan-av-block）。
"""
from __future__ import annotations

import asyncio
import gc
import shutil
import tempfile
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.engine_worker import engine_hub
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.reading_cache import ReadingCache
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.controllers.reader_controller import ReaderController
from app.views.reading.reader_view import ReaderView

SOURCE_URL = "http://127.0.0.1:1"
BOOK_URL = "http://127.0.0.1:1/book/1"

# 中间夹一卷标题：续章必须跳过它，进度计数也不把卷标题算进"共几章"。
CHAPTERS = [
    ChapterItem("第一章 出海", f"{BOOK_URL}/c1", index=0, is_volume=False),
    ChapterItem("卷二", "", index=1, is_volume=True),
    ChapterItem("第二章 靠岸", f"{BOOK_URL}/c2", index=2, is_volume=False),
]
FIRST, VOLUME, SECOND = CHAPTERS


class _StubEngine:
    """引擎替身：按章节返回正文，并记录被请求了几次（防重复追加）。"""

    def __init__(self):
        self.content_calls: list[str] = []

    async def chapters(self, source, book):
        return list(CHAPTERS)

    async def content_document(self, source, chapter, next_url=""):
        self.content_calls.append(chapter.url)
        return ChapterContent(
            title=chapter.name,
            blocks=[ContentBlock("text", f"{chapter.name}的正文内容")])


class _FakeHub:
    """控制器只需 hub.get(类型) 与 hub.bus（None：不发事件也不断言事件）。"""

    def __init__(self, services):
        self.bus = None
        self._services = services

    def get(self, stype):
        return self._services[stype]


@pytest.fixture()
def rig(monkeypatch):
    """真 StorageService/SettingsService + 真 ReadingService + 真视图/控制器。

    数据根用系统短临时目录：reader-cache 的落盘路径本身很深，pytest 默认的
    嵌套 basetemp 会撞上 Windows MAX_PATH。
    """
    root = Path(tempfile.mkdtemp(prefix="reader_scroll_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))
    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    settings = SettingsService(storage=storage, engine=engine)
    asyncio.run(settings.on_start())
    reading = ReadingService(
        rule_engine=_StubEngine(), storage=storage, settings=settings,
        source_service=None, cache=ReadingCache(storage.paths.cache))
    view = ReaderView()
    # 书签 / 书内搜索在本文件里不会被触发：控制器只是从 hub 取句柄，给空壳即可，
    # 真句柄会把 SQLite 书签表引进来，测的就不是续读了。
    controller = ReaderController(_FakeHub({
        ReadingService: reading,
        BookmarkService: SimpleNamespace(),
        ContentSearchService: SimpleNamespace(),
    }))
    controller.attach(view)
    try:
        yield SimpleNamespace(view=view, controller=controller, reading=reading,
                              storage=storage, root=root)
    finally:
        controller.detach()
        asyncio.run(reading.on_stop())
        asyncio.run(settings.on_stop())
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _settled(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _open(rig) -> None:
    rig.controller.ui_call(rig.controller.open_book(
        SimpleNamespace(bookSourceUrl=SOURCE_URL),
        BookItem(name="书名", url=BOOK_URL)))
    assert _settled(lambda: rig.controller._chapter_index == 0 and _idle(rig)), \
        "开书没挂出第一章"


def _idle(rig) -> bool:
    """首屏/追加**真收工**：`_chapter_index` 在 `_run_chapter` 的 try 里就落了（
    `reader_controller.py:457`），标题与两个标记要到 :458 与 finally :479 才写完。
    只等前者，下一步读标题/发滚动就撞在窗口里。"""
    return not rig.controller._reading_loading and not rig.controller._restoring_scroll


def _scroll(rig, pixels, maximum) -> None:
    """模拟一次真实滚动事件（视图那条 on_scroll 也接到同一个处理器上）。

    发之前先等加载收工：挂载那一阵 `_restoring_scroll` / `_reading_loading` 还立着，
    `_on_reader_scroll` 在 :827 与 :845 两处整段早退，这一发就被当成挂载噪音丢了。
    真用户的手指是连续的，收工之后必然还有一发落在处理里；用例只发**一发**，
    丢了就永远等不到——单跑绿、全量套件负载一高（窗口变长）偶发红。
    """
    assert _settled(lambda: _idle(rig)), "滚动前没等到加载收工"
    rig.controller._on_reader_scroll(SimpleNamespace(
        pixels=pixels, max_scroll_extent=maximum))


def _texts(rig) -> list[str]:
    """屏幕上的文本（剥掉段首缩进：这里要盯的是"哪几章挂出来了"）。"""
    return [str(getattr(c, "value", "") or "").lstrip("　")
            for c in rig.view._content_host.controls
            if getattr(c, "value", "")]


def _mounted(rig, chapter) -> bool:
    return chapter.url in rig.reading.current_session.viewports


def _saved_chapter_index(rig):
    row = rig.storage.db.execute(
        "SELECT chapter_index FROM books WHERE book_url=?", (BOOK_URL,)).fetchone()
    return row[0] if row else None


def test_open_book_mounts_only_the_first_chapter(rig):
    _open(rig)
    assert [vp.chapter.url for vp in
            rig.reading.current_session.viewports.values()] == [FIRST.url]
    assert _texts(rig) == ["第一章 出海的正文内容"]
    assert rig.view._title.value == "第一章 出海"


def test_bottom_of_chapter_appends_the_next_one(rig):
    _open(rig)
    _scroll(rig, 900, 1000)          # 距底 100px → 触发续章
    assert _settled(lambda: _mounted(rig, SECOND)), "读到底没有自动接下一章"
    # 追加的形态：已读章节还在，新章以"分隔线 + 章名 + 正文"接在文末
    assert _texts(rig) == ["第一章 出海的正文内容", "第二章 靠岸",
                           "第二章 靠岸的正文内容"]
    viewport = rig.reading.current_session.viewports[SECOND.url]
    assert viewport.document_start == 1000.0, "新章起点没记成追加前的文档总高"


def test_append_never_scrolls_the_reader_back_to_the_top(rig):
    """自动续章只往文末接东西：任何一次 restore_scroll 都会把用户弹回顶部。"""
    _open(rig)
    calls: list[float] = []
    rig.view.restore_scroll = lambda offset: calls.append(offset)
    _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND))
    _scroll(rig, 1500, 2000)         # 滚进第二章：只重新布防，不再追加
    time.sleep(0.3)
    assert calls == [], f"续章过程中把正文滚回了偏移 {calls}"
    assert rig.reading._engine.content_calls.count(SECOND.url) == 1


def test_progress_follows_the_appended_chapter(rig):
    _open(rig)
    _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND))
    _scroll(rig, 1500, 2000)         # 可见章节换成第二章
    assert _settled(lambda: rig.controller._chapter_index == 1)
    assert rig.view._title.value == "第二章 靠岸"
    assert rig.view._status_bar.content.value == "第二章 靠岸 · 2/2 · 100%"
    _scroll(rig, 1600, 2000)         # 再滚一次：0.5s 节流失效后落库
    assert _settled(lambda: _saved_chapter_index(rig) == 1), \
        f"进度还停在第一章：{_saved_chapter_index(rig)}"


def test_appended_chapter_is_cached_separately(rig):
    _open(rig)
    _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND))
    cache = rig.reading._cache
    for chapter in (FIRST, SECOND):
        cached = cache.load_content(SOURCE_URL, BOOK_URL, chapter.url)
        assert cached is not None, f"{chapter.name} 没进缓存"
        assert cached.blocks[0].value == f"{chapter.name}的正文内容"


def test_inertial_scroll_never_appends_the_same_chapter_twice(rig):
    """同一位置连甩四下只接一章。

    这份 rig 只有两章可读（`CHAPTERS` 里那个是卷标题），所以"防御全拆 → 一路往后
    走到第三、第四、第五种"那个症状在这里**天生看不见**（`_adjacent_chapter(1)` 到
    头就返回 None）。带牙齿的那条在 `test_views_reading_m2.py`
    ::`ReaderAutoContinueTests::test_a_chapter_is_never_appended_twice`（三章 rig，
    去布防 + 已挂集合两道一起拆才红）。
    """
    _open(rig)
    for _ in range(4):
        _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND))
    time.sleep(0.3)
    assert rig.reading._engine.content_calls.count(SECOND.url) == 1, \
        "惯性滚动把同一章接了好几遍"


def test_reopening_the_same_book_does_not_restart_the_load(rig):
    """连点两次「开始阅读」：第二趟不该把目录和首章再跑一遍。"""
    _open(rig)
    engine = rig.reading._engine
    firsts = engine.content_calls.count(FIRST.url)
    rig.controller.ui_call(rig.controller.open_book(
        SimpleNamespace(bookSourceUrl=SOURCE_URL),
        BookItem(name="书名", url=BOOK_URL))).result(timeout=8)
    time.sleep(0.3)
    assert engine.content_calls.count(FIRST.url) == firsts, \
        "第二趟开书把首章又取了一遍，两趟并发会互相换掉正文"


def test_open_book_after_leaving_the_reader_stops_quietly(rig):
    """目录加载途中切了页：视图已摘，这一趟要静默退出而不是抛 AttributeError。"""
    rig.controller.detach()
    rig.controller.ui_call(rig.controller.open_book(
        SimpleNamespace(bookSourceUrl=SOURCE_URL),
        BookItem(name="书名", url=BOOK_URL))).result(timeout=8)


def test_volume_title_is_skipped_when_looking_for_the_next_chapter(rig):
    _open(rig)
    _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND))
    assert not _mounted(rig, VOLUME), "卷标题被当成一章去取了"
    assert rig.controller._view._progress_label.value == "1/2 · 第一章 出海" or \
        rig.controller._view._progress_label.value == "2/2 · 第二章 靠岸"


# --------------------------------------------------------------------------- #
# 预取（§5.5 另一半）：挂出一章就把后面几章拉回来，章尾续读才不用等网络。
# --------------------------------------------------------------------------- #
def test_opening_a_chapter_prefetches_the_next_one_without_showing_it(rig):
    """预取只进缓存，不挂屏幕：读者还没读到的章节不能自己冒出来。"""
    _open(rig)
    engine = rig.reading._engine
    assert _settled(lambda: SECOND.url in engine.content_calls), "开章没触发后台预取"
    time.sleep(0.2)
    assert _texts(rig) == ["第一章 出海的正文内容"], "预取把没读到的章节挂上了屏幕"


def test_appending_a_prefetched_chapter_does_not_request_it_again(rig):
    """章尾接的正是预取到的那一章：一次请求都不该再发。"""
    _open(rig)
    engine = rig.reading._engine
    assert _settled(lambda: SECOND.url in engine.content_calls)
    _scroll(rig, 900, 1000)
    assert _settled(lambda: _mounted(rig, SECOND)), "预取到了却没接上章"
    time.sleep(0.3)
    assert engine.content_calls.count(SECOND.url) == 1, \
        "章尾又去要了一遍已经预取到的章节"


def test_second_restore_scroll_frame_is_dropped_after_detach(rig):
    """续读定位补的第二帧回来时页面可能已经切走了：这一帧要安静丢掉。"""
    _open(rig)
    chapter = rig.controller._readable[0]
    raw = next(c for c in rig.reading.current_session.chapters
               if c.url == chapter["url"])
    frames: list[float] = []

    def restore(offset):
        frames.append(offset)
        rig.controller.detach()     # 第一帧落地的同一刻用户切了页

    rig.view.restore_scroll = restore
    rig.controller.ui_call(rig.controller._run_chapter(
        chapter, raw, append=False, restore_offset=320.0)).result(timeout=8)
    assert len(frames) == 1, f"detach 之后还往正文补了第二帧：{frames}"


def test_leaving_the_reader_stops_the_background_prefetch(rig):
    """切页不是退出阅读，但后台轮子得停下：引擎不该替看不见的书继续拉章节。"""
    _open(rig)
    assert _settled(lambda: rig.reading._prefetch_task is not None)
    rig.controller.detach()
    task = rig.reading._prefetch_task
    assert task is None or _settled(lambda: task.cancelled() or task.done()), \
        "detach 之后预取轮还在跑"


# --------------------------------------------------------------------------- #
# 滚动下发：0.86 的 ``scroll_to`` 是协程，调用完就丢等于没滚 + RuntimeWarning；
# 而 ``Page.run_task`` 只收**协程函数**，把协程对象直接递过去会被它当场 TypeError。
# 两条约束都得钉住，否则界面看起来"什么都没发生"。
# --------------------------------------------------------------------------- #
class _FletLikePage:
    """按 Flet 的规矩记账的假 page：只认协程函数，收到协程对象就 TypeError。"""

    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def run_task(self, handler, *args, **kwargs):
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError("handler must be a coroutine function")
        self.tasks.append(asyncio.ensure_future(handler(*args, **kwargs)))


def test_scroll_helpers_really_scroll_once_the_page_runs_them(monkeypatch):
    view = ReaderView()
    calls: list[dict] = []
    page = _FletLikePage()

    class _Spy:
        async def scroll_to(self, **kwargs):
            calls.append(kwargs)

    view._reader_scroll = _Spy()                        # type: ignore[assignment]
    monkeypatch.setattr(ReaderView, "page",
                        property(lambda self: page), raising=False)

    async def main():
        view.restore_scroll(320.0)
        view.scroll_by(-560)
        await asyncio.gather(*page.tasks)

    asyncio.run(main())
    assert calls == [{"offset": 320.0, "duration": 0},
                     {"delta": -560, "duration": 120}], \
        "滚动协程没被真跑起来（续读定位与翻页会表现为点了不动）"


def test_unmounted_view_does_not_leak_an_unawaited_scroll_coroutine():
    """没上页面时协程必须被处理掉，否则控制台全是 never-awaited 的 RuntimeWarning。"""
    view = ReaderView()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        view.restore_scroll(120.0)
        view.scroll_by(300)
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)], \
        "滚动协程被丢掉了"
