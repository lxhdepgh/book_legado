"""#88 护栏：续读位置存**章内段序号**，裸像素只作为老记录的回退。

像素属于当时那一套装尺，段序号才是跨会话还认得回来的数。三轮现场取证（本地探针，
零联网）给出这一轮要钉住的东西：

* 取证一 ``.probe/pos88_cross_ruler_attr.py``（flet web 面 + Playwright 实测两套装尺的
  版心高 38607px@16 / 57614px@20，同一篇 300 段对话体，300 个测点全跑）：存档 16 号、
  重开 20 号时**裸像素**这条坐标中位差 22 段、最大 73 段；同套装尺重开才是 0 段。
  现成的两条代理坐标也不行——``chapter_pos``（字符偏移）连同尺都差 7 段，
  ``scroll_ratio`` 的分母是视口裁剪后的最大可滚偏移，换装尺照样漂。
* 取证二 ``.probe/pos88_chapter_local_attr.py``：连续阅读时屏幕上是"本章 + 自动接来的
  后章"一长串，视图报的是**整篇**段号。用户在第二章第 40 段时整篇段号是 340，重开只剩
  第二章（300 段）会 clamp 到第 299 段——**差 259 段**；存章内段号差 0 段。
* 取证三 ``.probe/pos88_reopen_attr.py``（真 StorageService + 真 ReadingService + 真视图
  + 真控制器，16 号读到第 140 段落库，改 20 号重开）：老记录（段序号 NULL，按像素回）
  落在第 **123** 段（差 **17** 段），带段序号的记录落在第 **140** 段（差 **0** 段）。

所以判据分两面。服务面：v8 迁移在真 v7 库上补**可空**列且老行读回 NULL（不回填，回填
需要当时的版心高而那一栏没存过）；段序号在 books 表与 ReadingPosition 里往返；会话把
整篇段号减成章内的、减不出负数、量不到尺寸时落 None 而不是造假值。界面面：开书时段
序号接进回位、回位用尺寸事件挂单（刚挂完正文量不到版心高）、追加两章后落库的必须是章
内段号、老记录仍然只走像素。

全程本地临时目录 + 合成章节，不联网、不读 ``resource/``、不开浏览器。段顶与版心高的
真值取自 #86/#87 的现场读数（``DIALOGUE_TRUTH``、``PREFIX_TOP_20``）。
"""
from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.storage import StorageService
from app.controllers.reader_controller import ReaderController
from app.services.app_paths import AppPaths
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import (
    ReadingPosition, ReadingService, ReadingSession, _paragraph_or_none)
from app.storage.schema import MIGRATIONS, SCHEMA_VERSION, _migrate_v8
from app.views.reading.reader_view import ReaderView

SOURCE = "http://127.0.0.1:1"
BOOK = "http://127.0.0.1:1/book/88"
ANCHOR = 240
#: 同一篇 300 段对话体、同一版心宽 728px 下客户端报来的渲染高（#86/#87 现场读数）。
HOST_16 = (728.0, 38607.0)
HOST_20 = (728.0, 57614.0)
#: 16 号那套装尺下第 240 段的段顶（浏览器实测）——存档时该写进 scroll_offset 的像素。
PIXEL_TOP_16 = 28167.0
#: 20 号那套装尺下第 240 段的段顶（浏览器实测），用来验按段回位的落点不是模型自洽。
PIXEL_TOP_20 = 41730.0


def paragraphs_of(total: int = 300) -> list[str]:
    """#86/#87 那份对话形状：前 100 段一二十字，之后整墙叙述（同一随机种子）。

    均匀长度的文档换字号几乎不挪段顶，那种样本验不出"像素不是不变量"。
    """
    rng = random.Random(20260929)
    out = []
    for i in range(total):
        chars = rng.choice([12, 16, 20, 24, 28, 32] if i < 100
                           else [160, 200, 240, 280, 320])
        body = "".join(rng.choice("字句读纸张看灯下山风月夜声光影花雪路桥水田家园")
                       for _ in range(chars))
        out.append(f"第{i}段{body}")
    return out


def dialogue_document() -> ChapterContent:
    return ChapterContent(title="测试章",
                          blocks=[ContentBlock("text", para)
                                  for para in paragraphs_of()])


def short_document(marker: str, count: int) -> ChapterContent:
    return ChapterContent(title=marker,
                          blocks=[ContentBlock("text", f"{marker}第{i}段，一行正文。")
                                  for i in range(count)])


def _size(width: float, height: float) -> SimpleNamespace:
    return SimpleNamespace(width=width, height=height)


def _scroll(pixels: float, maximum: float) -> SimpleNamespace:
    return SimpleNamespace(pixels=pixels, max_scroll_extent=maximum)


# --------------------------------------------------------------------------- #
# 一、存储面：v8 迁移只补一列，可空，老行读回 NULL
# --------------------------------------------------------------------------- #
def _books_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(books)")}


def _build_v7_database(path: Path) -> sqlite3.Connection:
    """手搭一个真 v7 库：跑 1..7 版迁移，并把 user_version 写成 7。"""
    conn = sqlite3.connect(path)
    for version in range(1, SCHEMA_VERSION):
        MIGRATIONS[version](conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.commit()
    return conn


def test_v8_adds_a_nullable_column_and_leaves_old_rows_unknown(tmp_path):
    """v7 库里没这一列 → 补上；老行读回 NULL；同一次迁移重跑不报错也不丢数据。"""
    conn = _build_v7_database(tmp_path / "main.db")
    try:
        assert "chapter_paragraph" not in _books_columns(conn), (
            "v7 里居然已经有 chapter_paragraph——那'补列'这一步就没被跑到，"
            "上面那条断言测的是空气")
        conn.execute(
            "INSERT INTO books(book_url, source_url, name, chapter_index,"
            " scroll_offset) VALUES(?,?,?,?,?)",
            (BOOK, SOURCE, "老记录", 3, 1234.5))
        conn.commit()

        _migrate_v8(conn)
        conn.commit()
        assert "chapter_paragraph" in _books_columns(conn)
        assert conn.execute(
            "SELECT name, chapter_index, scroll_offset, chapter_paragraph"
            " FROM books WHERE book_url=?", (BOOK,)).fetchone() == (
            "老记录", 3, 1234.5, None), "老行必须读回 NULL（= 未知），别的列一个不许动"

        _migrate_v8(conn)      # 已经升过的库上重跑：duplicate column 就是启动崩
        conn.commit()
        assert _books_columns(conn) >= {"chapter_paragraph", "scroll_offset"}
    finally:
        conn.close()


def test_a_real_v7_database_upgrades_and_still_restores_by_pixels(tmp_path):
    """真升级路径：v7 库文件交给 StorageService，启动后升到 v8 并读得出老进度。"""
    paths = AppPaths(data=tmp_path / "data", cache=tmp_path / "cache")
    paths.data.mkdir(parents=True)
    conn = _build_v7_database(paths.data / "legado.db")
    conn.execute(
        "INSERT INTO books(book_url, source_url, name, chapter_index, scroll_offset,"
        " scroll_ratio, font_size, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (BOOK, SOURCE, "老记录", 2, 900.0, 0.4, 16, 100))
    conn.commit()
    conn.close()

    storage = StorageService(paths)
    asyncio.run(storage.on_start())
    try:
        assert storage.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "chapter_paragraph" in _books_columns(storage.raw)
        reading = ReadingService(storage=storage)
        position = asyncio.run(reading.restore_position(SimpleNamespace(url=BOOK)))
        assert position is not None, "升级把老进度读没了"
        assert position.chapter_paragraph is None, (
            f"老记录凭空多出段序号 {position.chapter_paragraph}——"
            "回填出来的数没有依据，重开会按它跳到别的段")
        assert (position.chapter_index, position.chapter_offset) == (2, 900.0)

        # 升级之后新写的进度带上段序号，重开就认得它
        asyncio.run(reading.save_progress(2, ReadingPosition(
            source_url=SOURCE, book_url=BOOK, chapter_index=2,
            chapter_offset=900.0, chapter_paragraph=41, font_size=20)))
        again = asyncio.run(reading.restore_position(SimpleNamespace(url=BOOK)))
        assert again.chapter_paragraph == 41
        assert again.chapter_offset == 900.0, "像素那一栏还在：老口径不能丢"
    finally:
        asyncio.run(storage.on_stop())


def test_paragraph_index_round_trips_including_the_unknown(tmp_path):
    """存 41 回来是 41，存 None 回来是 None：0 和"不知道"必须是两回事。"""
    paths = AppPaths(data=tmp_path / "data", cache=tmp_path / "cache")
    storage = StorageService(paths)
    asyncio.run(storage.on_start())
    reading = ReadingService(storage=storage)
    try:
        for value in (41, 0, None):
            asyncio.run(reading.save_progress(1, ReadingPosition(
                source_url=SOURCE, book_url=BOOK, chapter_index=1,
                chapter_offset=10.0, chapter_paragraph=value)))
            restored = asyncio.run(
                reading.restore_position(SimpleNamespace(url=BOOK)))
            assert restored.chapter_paragraph == value, (
                f"存 {value!r} 回来 {restored.chapter_paragraph!r}")
    finally:
        asyncio.run(storage.on_stop())


def test_reading_json_tolerates_a_missing_or_rubbish_paragraph():
    """老 JSON（没这个键）、负数、字符串垃圾一律成 None；正常值与 as_reading 往返。"""
    base = {"chapter_index": 2, "scroll_offset": 5.0}
    assert ReadingPosition.from_reading(SOURCE, BOOK, base).chapter_paragraph is None
    for junk in (-1, "", "十四", None):
        assert _paragraph_or_none(junk) is None, junk
        assert ReadingPosition.from_reading(
            SOURCE, BOOK, dict(base, chapter_paragraph=junk)).chapter_paragraph is None
    saved = ReadingPosition.from_reading(SOURCE, BOOK, dict(base, chapter_paragraph=7))
    assert saved.chapter_paragraph == 7
    assert saved.as_reading()["chapter_paragraph"] == 7
    assert ReadingPosition.from_reading(
        SOURCE, BOOK, saved.as_reading()).chapter_paragraph == 7


# --------------------------------------------------------------------------- #
# 二、会话换算：整篇段号 → 章内段号
# --------------------------------------------------------------------------- #
def _two_chapter_session() -> tuple[ReadingSession, list[ChapterItem]]:
    chapters = [ChapterItem("第一章", "ch1", index=0),
                ChapterItem("第二章", "ch2", index=1)]
    session = ReadingSession()
    session.bind(SOURCE, BOOK, chapters)
    # 连续阅读：第一章 6 段挂在上面，第二章的起点在整篇文档的第 6 段之后
    session.mount(chapters[0], 0.0, 600, 0)
    session.mount(chapters[1], 600.0, 600, 6)
    return session, chapters


def test_track_stores_the_paragraph_within_the_chapter():
    """滚进第二章第 2 段（整篇第 8 段）：落库的必须是 2，不是 8。

    取证二：整篇段号 340 存进去、重开只剩第二章时 clamp 到 299——差 259 段。
    """
    session, chapters = _two_chapter_session()
    position, visible = session.track(700.0, 1200.0, 20, 8)
    assert visible is chapters[1] and position.chapter_url == "ch2"
    assert position.chapter_paragraph == 2, (
        f"章内第 2 段存成了 {position.chapter_paragraph}——那是整篇口径，"
        "重开时这个数会指到别的章里去")
    assert position.chapter_paragraph != 8


def test_track_never_goes_negative_and_never_invents_zero():
    """起点减穿了钳在 0；视图量不到尺寸给 None 时不许写 0（0 是"第一段"）。"""
    session, _chapters = _two_chapter_session()
    assert session.track(700.0, 1200.0, 20, 3)[0].chapter_paragraph == 0
    assert session.track(700.0, 1200.0, 20, None)[0].chapter_paragraph is None
    assert session.track(100.0, 1200.0, 20, None)[0].chapter_paragraph is None
    # 反空转：第一章里读数照常（起点 0，减了个寂寞但值是对的）
    assert session.track(100.0, 1200.0, 20, 3)[0].chapter_paragraph == 3


def test_enter_carries_the_paragraph_so_an_idle_reopen_keeps_it():
    """回到那一段但用户一滚都不滚：enter 带上的段序号不许被下一次落库抹成未知。"""
    session, chapters = _two_chapter_session()
    assert session.enter(chapters[1], 20, offset=100.0, paragraph=40).chapter_paragraph == 40
    assert session.enter(chapters[1], 20, offset=100.0).chapter_paragraph is None

    session.mount(chapters[0], 0.0, 600)     # 不传段起点：默认 0
    assert session.viewports["ch1"].paragraph_start == 0, (
        "mount 的段起点默认必须是 0——整篇只挂一章时它就在第 0 段")


# --------------------------------------------------------------------------- #
# 三、界面面：开书 → 回位（段序号优先，像素只在老记录上）
# --------------------------------------------------------------------------- #
class _RestoreSpyView(ReaderView):
    """记账视图：回位走的是段号还是像素。生产逻辑照跑（子类委托，不是替面）。"""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[int] = []
        self.pixels: list[float] = []

    def anchor_paragraph(self, paragraph_index: int) -> bool:
        self.anchors.append(int(paragraph_index))
        return super().anchor_paragraph(paragraph_index)

    def restore_scroll(self, offset: float) -> None:
        self.pixels.append(float(offset))
        return super().restore_scroll(offset)


class _FakeReading:
    """阅读服务的记账替面：会话是真 ReadingSession，正文是真 ChapterContent，没有网络。"""

    def __init__(self, chapters, documents, position=None):
        self.current_source = SimpleNamespace(bookSourceUrl=SOURCE)
        self.current_session = ReadingSession()
        self.current_session.bind(SOURCE, BOOK, chapters, position)
        self._documents = documents
        self.saved: list[tuple[int, ReadingPosition]] = []

    async def open_book(self, source, book, shelve=True):
        return self.current_session

    async def load_content(self, chapter, append=False):
        return self._documents[str(chapter.url)]

    async def prefetch_chapters(self, chapter):
        return None

    def stop_prefetch(self):
        return None

    async def save_progress(self, chapter_index, position):
        self.saved.append((int(chapter_index), position))

    async def get_reader_preference(self, key, default=None):
        return default

    async def save_reader_preference(self, key, value):
        return None

    def staged_record(self):
        return SimpleNamespace(name="书名", url=BOOK, author="", kind="",
                               cover_url="")


class _Hub:
    def __init__(self, reading):
        self.bus = None
        self._services = {ReadingService: reading, BookmarkService: SimpleNamespace(),
                          ContentSearchService: SimpleNamespace()}

    def get(self, stype):
        return self._services[stype]


def _reader(reading) -> ReaderController:
    return ReaderController(_Hub(reading))


def test_reopen_with_a_paragraph_index_lands_on_that_paragraph(tmp_path):
    """16 号存档第 240 段 → 20 号重开：按段回位，落点确实是那一段（取证三 0 段）。

    刚挂完正文量不到版心高，回位必须挂在尺寸事件上（不是轮帧猜像素）；尺寸一来
    恰好滚一次，落点还要对上浏览器现场量到的段顶 41730px（容差=该段自己的高）。
    """
    chapters = [ChapterItem("第一章", "ch1", index=0)]
    saved = ReadingPosition(source_url=SOURCE, book_url=BOOK, chapter_index=0,
                            chapter_offset=PIXEL_TOP_16, chapter_paragraph=ANCHOR,
                            font_size=16)
    reading = _FakeReading(chapters, {"ch1": dialogue_document()}, saved)
    view = _RestoreSpyView()
    view.apply_preferences({"font_size": 20})
    controller = _reader(reading)
    controller.attach(view)

    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))

    assert view.anchors and set(view.anchors) == {ANCHOR}, (
        f"回位没走段号：{view.anchors}")
    assert view._relocate_pending is not None, (
        "尺寸还没报上来就放弃了——那一趟滚的是猜出来的像素")
    assert view._relocate_pending[0] == ANCHOR

    view._on_content_size(_size(*HOST_20))
    landed = view.paragraph_at_pixel(view.pixels[-1])
    assert landed == ANCHOR, f"20 号重开落在第 {landed} 段，不是存档那一段"
    step = view._paragraph_tops()[ANCHOR] - view._paragraph_tops()[ANCHOR - 1]
    assert abs(view.pixels[-1] - PIXEL_TOP_20) <= step, (
        f"落点 {view.pixels[-1]:.0f}px 与浏览器实测段顶 {PIXEL_TOP_20:.0f}px "
        f"差过这一段的高（{step:.0f}px），回位的不是那一段")
    controller.detach()


def test_idle_reopen_persists_the_paragraph_it_was_restored_to():
    """回到那一段、用户一滚都不滚就退出：落库还得是 40，不许抹成未知。

    挂载时的 ``enter`` 是唯一写这份位置的入口（``track`` 只在滚动事件里跑），
    它不带段号就等于"开了一本书再合上"把上次存的段序号清掉。
    """
    chapters = [ChapterItem("第一章", "ch1", index=0)]
    saved = ReadingPosition(source_url=SOURCE, book_url=BOOK, chapter_index=0,
                            chapter_offset=1000.0, chapter_paragraph=40)
    reading = _FakeReading(chapters, {"ch1": short_document("甲", 20)}, saved)
    view = _RestoreSpyView()
    controller = _reader(reading)
    controller.attach(view)

    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))
    asyncio.run(controller._persist_position())
    controller.detach()

    assert reading.saved[-1][1].chapter_paragraph == 40, (
        f"没人滚却把段序号写没了：{reading.saved[-1][1].chapter_paragraph!r}")


def test_legacy_record_restores_by_pixels_and_never_anchors():
    """老记录（段序号 NULL）：只按像素回，一次段级定位都不许发起。

    像素这条腿换了装尺就是错的（取证三：差 17 段），但它至少是存档时量到的数——
    比拿一个没依据的段号瞎猜要好，所以老记录不许被"顺手补个 0"。
    """
    chapters = [ChapterItem("第一章", "ch1", index=0)]
    saved = ReadingPosition(source_url=SOURCE, book_url=BOOK, chapter_index=0,
                            chapter_offset=888.0, chapter_paragraph=None)
    reading = _FakeReading(chapters, {"ch1": short_document("甲", 20)}, saved)
    view = _RestoreSpyView()
    controller = _reader(reading)
    controller.attach(view)

    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))
    controller.detach()

    assert view.anchors == [], f"老记录没有段号，不该走段级回位：{view.anchors}"
    assert view.pixels and set(view.pixels) == {888.0}
    assert view._relocate_pending is None


def test_first_paragraph_keeps_the_pixel_even_when_the_index_exists():
    """第 0 段 + 像素：那 200px 本来就落在第一段里，按段回位反倒把人弹回章首（#87 口径）。"""
    chapters = [ChapterItem("第一章", "ch1", index=0)]
    saved = ReadingPosition(source_url=SOURCE, book_url=BOOK, chapter_index=0,
                            chapter_offset=200.0, chapter_paragraph=0)
    reading = _FakeReading(chapters, {"ch1": short_document("甲", 20)}, saved)
    view = _RestoreSpyView()
    controller = _reader(reading)
    controller.attach(view)

    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))
    controller.detach()

    assert view.anchors == []
    assert set(view.pixels) == {200.0}
    # 视图自己也得守这条口径：控制器漏判一次也不该把人从第一段中间弹回章首
    assert view.anchor_paragraph(0) is False
    assert view._relocate_pending is None, "第 0 段的段顶就是 0，挂单只会滚回章首"


def test_a_scroll_without_a_measured_ruler_saves_no_paragraph():
    """量不到版心高时滚了一下：段序号落"未知"，像素照旧存——不拿猜出来的段号糊弄。"""
    chapters = [ChapterItem("第一章", "ch1", index=0)]
    reading = _FakeReading(chapters, {"ch1": dialogue_document()})
    view = _RestoreSpyView()
    controller = _reader(reading)
    controller.attach(view)
    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))

    view._content_size = (728.0, 0.0)      # 宽度有、渲染高还没报上来
    controller._restoring_scroll = False
    view._on_document_scroll(_scroll(1200.0, 30000.0))
    position = reading.current_session.position
    assert position.chapter_paragraph is None, (
        f"没量到尺寸却存了段号 {position.chapter_paragraph}")
    assert position.chapter_offset == 1200.0
    asyncio.run(controller._persist_position())
    assert reading.saved[-1][1].chapter_paragraph is None
    controller.detach()


def test_appended_chapter_saves_a_chapter_local_index():
    """读到第二章并滚进它的第 2 段：屏幕上整篇段号是 8，落库必须是章内的 2。

    三重证据都在这里：视图数得出已挂了几个段（paragraph_count）、控制器把它当成
    挂载起点（_mount_chapter 的 paragraph_start）、会话减完才写进位置。
    """
    chapters = [ChapterItem("第一章", "ch1", index=0),
                ChapterItem("第二章", "ch2", index=1)]
    documents = {"ch1": short_document("甲", 6), "ch2": short_document("乙", 6)}
    reading = _FakeReading(chapters, documents)
    view = _RestoreSpyView()
    controller = _reader(reading)
    controller.attach(view)

    asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                      BookItem(name="书名", url=BOOK)))
    after_first = view.paragraph_count()
    view._on_content_size(_size(728.0, 600.0))
    controller._pending_document_offset = 590.0     # 触发续读时记下的本章起点
    asyncio.run(controller._run_chapter(
        controller._readable[1], chapters[1], append=True))

    viewport = reading.current_session.viewports["ch2"]
    assert viewport.paragraph_start == after_first, (
        f"第二章的段起点记成 {viewport.paragraph_start}，屏幕上前面已经挂了 "
        f"{after_first} 段——减错起点就是把整篇段号往库里存")

    # 追加完的整篇更高：测点要落在第二章里（像素必须过本章起点），否则选中的
    # 视口还是第一章，这条就在测"没滚进第二章"这件事。
    height = viewport.document_start * 2
    view._on_content_size(_size(728.0, height))
    target = viewport.paragraph_start + 2
    pixels = view.paragraph_pixel(target)
    assert pixels > viewport.document_start, (
        f"测点像素 {pixels:.0f} 还没进第二章（起点 {viewport.document_start}）")
    controller._restoring_scroll = False
    view._on_document_scroll(_scroll(pixels, height))

    position = reading.current_session.position
    assert position.chapter_url == "ch2"
    assert view.paragraph_at_pixel(pixels) == target, "测点没落在预期段上，这条在测空气"
    assert position.chapter_paragraph == 2, (
        f"整篇第 {target} 段该存成第二章的第 2 段，实际存了 "
        f"{position.chapter_paragraph}")
    asyncio.run(controller._persist_position())
    assert reading.saved[-1][1].chapter_paragraph == 2
    controller.detach()
