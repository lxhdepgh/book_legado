"""#86 护栏：书内搜索跳到**命中那一段**，不是章首。

四味药，每条都配反空转对照：

1. **建行粒度**。索引原来按引擎给的 text block 建行，而纯文字的一章常常只有一个
   block（``parse_content_blocks`` 把整章正文收成一条），于是"块首偏移"≈"章首偏移"，
   再准的跳转也只能落到章首。这条钉住"一行 = 一个自然段"。
2. **一把尺子**。视图切段（``novel_paragraphs``）与索引切段（``paragraph_lines``）必须
   段数、顺序、文本三样都对齐——否则第 k 段这个数在两边根本不是同一个东西。
3. **像素模型**。按段折行算高度、再用客户端实测的版心渲染高等比缩放：对话密集的
   书（前 100 段每段一二十字）七个测点最坏差 **0.09 段**，同一条文档按字符占比折算
   最坏差 **13.34 段**；长度均匀的书上两把尺分别 0.58 / 0.71 段（看不出差）。真值取自
   ``.probe/offset_shape_attr.py`` 的现场读数，逐点对照见 ``.probe/prod_pixel_error_from_truth.py``。
4. **接线**。搜索条点一条命中 → 整篇换成那一章 → 滚到第 k 段；量不到渲染尺寸时
   退回章首（改前的行为），不许抛、不许不动。

全程本地临时目录 + 合成章节，不联网、不读 ``resource/``、不开浏览器。
"""
from __future__ import annotations

import asyncio
import contextlib
import random
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
from app.core.text_layout import paragraph_lines
from app.controllers.reader_controller import ReaderController
from app.services.app_paths import AppPaths
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.services.reading_cache import ReadingCache
from app.views.reading.reader_view import ReaderView

SOURCE = "src-86"
BOOK = "book-para"
WORD = "琵琶行"

#: 取证六现场读数：同一窗口（版心宽 728px）、同一挂法（只挂前 k 段）下客户端
#: 报上来的渲染高。两形状各七个测点，用来把模型钉在实测上。
DIALOGUE_HOST = (728.0, 38607.0)
DIALOGUE_TRUTH = {10: 378.0, 30: 1158.0, 60: 2328.0, 90: 3498.0,
                  120: 7152.0, 180: 17565.0, 240: 28167.0}
UNIFORM_HOST = (728.0, 38769.0)
UNIFORM_TRUTH = {10: 1323.0, 30: 3858.0, 60: 7647.0, 90: 11652.0,
                 120: 15846.0, 180: 23262.0, 240: 30867.0}


@pytest.fixture()
def storage():
    root = Path(tempfile.mkdtemp(prefix="para86_"))
    svc = StorageService(AppPaths(data=root / "data", cache=root / "cache"))
    asyncio.run(svc.on_start())
    try:
        yield svc
    finally:
        asyncio.run(svc.on_stop())
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def cache(storage):
    return ReadingCache(storage.paths.cache)


@pytest.fixture()
def svc(storage, cache):
    cs = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(cs.on_start())
    assert cs._fts_available, "这台机器的 sqlite 没编 FTS5，测不出索引行为"
    return cs


def save_chapter(cache: ReadingCache, index: int, value: str) -> None:
    """把一章（单个 text block，多段）写进缓存，并把它登记进目录。"""
    catalog = [item for item in (cache.load_catalog(SOURCE, BOOK) or [])
               if int(getattr(item, "index", -1)) != index]
    catalog.append(ChapterItem(name=f"第{index}章", url=f"ch{index}", index=index))
    cache.save_catalog(SOURCE, BOOK, sorted(catalog, key=lambda c: int(c.index)))
    cache.save_content(SOURCE, BOOK, f"ch{index}",
                       ChapterContent(title=f"第{index}章",
                                      blocks=[ContentBlock("text", value)]))


# --------------------------------------------------------------------------- #
# 1. 建行粒度：一行 = 一个自然段
# --------------------------------------------------------------------------- #
THREE_PARAGRAPHS = (f"{WORD}的第一段叙述。\n\n\t {WORD}的第二段带空白行。\n"
                    f"　　已经缩进过的第三段{WORD}收尾。")


def test_one_block_with_three_paragraphs_becomes_three_rows(svc, cache):
    """整章只有一个 block 也要出三行，para_index 连续、offset 是段首字符偏移。"""
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    assert asyncio.run(svc.rebuild_index(BOOK, SOURCE)) == 3

    rows = svc._conn().execute(
        "SELECT para_index, offset, text FROM content_search"
        " WHERE book_url=? ORDER BY para_index", (BOOK,)).fetchall()
    lines = paragraph_lines(THREE_PARAGRAPHS)
    assert [int(r[0]) for r in rows] == [0, 1, 2], f"段序号不连续：{rows}"
    assert [r[2] for r in rows] == lines, "索引存的段文本与切段结果不一致"
    assert [float(r[1]) for r in rows] == [0.0, float(len(lines[0])),
                                           float(len(lines[0]) + len(lines[1]))]


def test_search_reports_the_paragraph_that_contains_the_keyword(svc, cache):
    """命中第 3 段就必须报 para_index=2；报 0 就是"跳章首"的老形状。"""
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    asyncio.run(svc.rebuild_index(BOOK, SOURCE))
    hits = asyncio.run(svc.search(BOOK, "收尾"))
    assert len(hits) == 1, f"应只命中第三段，实际 {hits}"
    assert int(hits[0]["para_index"]) == 2, hits[0]


def test_block_level_rows_would_be_caught(svc, cache):
    """反空转：按块建行的旧形状必须过不了上面两条判据。

    旧版一条 block 一行、没有 para_index 列——如果"粒度"其实没被检查，这条会一起通过。
    """
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    conn = svc._conn()
    with conn.transaction():
        conn.execute("DELETE FROM content_search WHERE book_url=?", (BOOK,))
        conn.execute("INSERT INTO content_search(book_url, chapter_index, offset,"
                     " para_index, text) VALUES(?,?,?,0,?)",
                     (BOOK, 0, 0.0, THREE_PARAGRAPHS))
    shape = asyncio.run(svc.search(BOOK, "收尾"))
    assert len(shape) == 1, "埋点本身要真写进去，否则这条在测空气"
    assert int(shape[0]["para_index"]) == 0, "按块建行居然也报出了段序号——判据失效"
    assert svc._rebuild_sync(BOOK, SOURCE) == 3, "同一份数据段级重建该出 3 行"


# --------------------------------------------------------------------------- #
# 2. 视图与索引共用一把尺子
# --------------------------------------------------------------------------- #
LEGACY_SCHEMA = ("CREATE VIRTUAL TABLE content_search USING fts5("
                 "book_url UNINDEXED, chapter_index UNINDEXED, offset UNINDEXED,"
                 " text, tokenize='trigram')")


def test_legacy_block_level_table_is_replaced_not_reused(storage, cache):
    """旧库里的 content_search 没有 para_index 列：拆掉重建。

    放任旧表在，``INSERT`` 会抛 ``no such column: para_index``，而那个
    ``OperationalError`` 被同一句 ``except`` 认成"这台 sqlite 没编 FTS5"——
    书内搜索整本降级成空结果。索引是派生数据，拆了由 :meth:`ensure_index` 补回来。
    """
    storage.execute(LEGACY_SCHEMA)
    storage.execute("INSERT INTO content_search(book_url, chapter_index, offset,"
                    " text) VALUES(?,?,?,?)",
                    (BOOK, 0, 0.0, f"{WORD} 旧索引里的一整块"))
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    rebuilt = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(rebuilt.on_start())
    assert rebuilt._fts_available, "旧表形状被误判成'这台机器没有 FTS5'"
    assert asyncio.run(rebuilt.ensure_index(BOOK, SOURCE)) == 3, "旧表没被拆掉重建成段级"
    assert int(rebuilt._conn().execute(
        "SELECT COUNT(*) FROM content_search").fetchone()[0]) == 3


def test_paragraph_schema_table_is_never_dropped_on_start(storage, cache):
    """反空转：形状已经对的时候，on_start 一条数据都不许动。

    上一条若靠"无条件拆表"来成立，这条会一起红。
    """
    cs = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(cs.on_start())
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    asyncio.run(cs.rebuild_index(BOOK, SOURCE))
    survivor = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(survivor.on_start())
    assert int(survivor._conn().execute(
        "SELECT COUNT(*) FROM content_search").fetchone()[0]) == 3, "启动把建好的索引清了"


def test_view_mounts_exactly_the_indexed_paragraphs(svc, cache):
    """屏幕上第 k 段 == 索引里第 k 段：段数与文本都不能分叉。

    ``novel_paragraphs(indent=True)`` 会加段首全角空格，切段口径却必须一致，
    所以断言比的是"去掉缩进后逐段相等"。
    """
    save_chapter(cache, 0, THREE_PARAGRAPHS)
    written = asyncio.run(svc.rebuild_index(BOOK, SOURCE))
    view = ReaderView()
    plan = view.content_plan(SimpleNamespace(
        image_style="", blocks=[ContentBlock("text", value)
                                for value in (THREE_PARAGRAPHS,)]))
    mounted = [line for kind, payload, _ in plan if kind == "text"
               for line, _heading in payload]
    assert written == len(mounted) == 3, (f"索引 {written} 行 / 挂载 {len(mounted)} 段")
    indexed = [row[0] for row in conn_rows(svc)]
    assert [text.lstrip("　") for text in mounted] == indexed


def conn_rows(svc: ContentSearchService) -> list[tuple]:
    return svc._conn().execute(
        "SELECT text FROM content_search WHERE book_url=?"
        " ORDER BY para_index", (BOOK,)).fetchall()


def test_empty_and_whitespace_only_block_contributes_no_rows(svc, cache):
    """空 block、纯空白 block 不许占段号：占了就整章错位。"""
    value = f"\n \t \n{WORD}只有一段真正文。\n  \n\n"
    save_chapter(cache, 0, value)
    asyncio.run(svc.rebuild_index(BOOK, SOURCE))
    assert [int(r[0]) for r in conn_index_offsets(svc)] == [0]
    view = ReaderView()
    assert len(view.novel_paragraphs(value, True)) == 1


def conn_index_offsets(svc: ContentSearchService) -> list[tuple]:
    return svc._conn().execute(
        "SELECT para_index FROM content_search WHERE book_url=?"
        " ORDER BY para_index", (BOOK,)).fetchall()


# --------------------------------------------------------------------------- #
# 3. 像素模型：按段折行 + 实测渲染高等比缩放
# --------------------------------------------------------------------------- #
def paragraphs_of(shape: str, total: int = 300) -> list[str]:
    """取证六那两个形状，同一随机种子。

    ``dialogue``：前 100 段每段一二十字（网文开头的对话常态），之后整墙叙述——
    任意前缀的"像素/字"都不是常数（1.40 vs 0.74），字符占比那把尺就是断在这里的。
    ``uniform``：每段 80~260 字，长度均匀（字符占比在这形状上恰好看不出问题）。
    """
    rng = random.Random(20260929)
    out = []
    for i in range(total):
        if shape == "dialogue":
            chars = rng.choice([12, 16, 20, 24, 28, 32] if i < 100
                               else [160, 200, 240, 280, 320])
        else:
            chars = rng.choice([80, 120, 160, 200, 260])
        body = "".join(rng.choice("字句读纸张看灯下山风月夜声光影花雪路桥水田家园")
                       for _ in range(chars))
        out.append(f"第{i}段{body}")
    return out


def _mount(shape: str, host: tuple[float, float]) -> ReaderView:
    view = ReaderView()
    paras = paragraphs_of(shape)
    view.render_plan(view.content_plan(ChapterContent(
        title="测试章",
        blocks=[ContentBlock("text", para) for para in paras])),
        append=False, chapter_name="")
    assert view.paragraph_count() == len(paras), "挂载段数与文档对不上，测的不是这篇"
    # 客户端布局完自己报尺寸（取证四），生产里就是这个入口
    view._on_content_size(SimpleNamespace(width=host[0], height=host[1]))
    return view


@pytest.mark.parametrize("shape,host,truth", [
    ("dialogue", DIALOGUE_HOST, DIALOGUE_TRUTH),
    ("uniform", UNIFORM_HOST, UNIFORM_TRUTH),
])
def test_paragraph_pixel_lands_within_a_paragraph_of_measured_truth(
        shape, host, truth):
    """七个测点 × 两形状：模型预测的段首 y 要落在实测一段高以内。"""
    view = _mount(shape, host)
    allowance = host[1] / 300.0
    for count, measured in truth.items():
        pixel = view.paragraph_pixel(count)
        assert pixel is not None
        assert abs(pixel - measured) <= allowance, (
            f"{shape} 第 {count} 段顶：预测 {pixel:.0f}px，实测 {measured:.0f}px，"
            f"容差 {allowance:.0f}px")


def test_character_ratio_would_be_caught_by_the_same_truth():
    """反空转：同一批真值下，"字符占比"那把尺在对话形状上差到 13.3 段。

    上一条要是宽容到谁都能过（比如只测长度均匀的文档），这条会一起红。
    """
    view = _mount("dialogue", DIALOGUE_HOST)
    paras = paragraphs_of("dialogue")
    lengths = [len(line) for para in paras
               for line, _h in view.novel_paragraphs(para, True)]
    allowance = DIALOGUE_HOST[1] / 300.0
    worst_ratio = 0.0
    for count, measured in DIALOGUE_TRUTH.items():
        ratio = DIALOGUE_HOST[1] * (sum(lengths[:count]) / sum(lengths))
        worst_ratio = max(worst_ratio, abs(ratio - measured) / allowance)
        assert abs(view.paragraph_pixel(count) - measured) < abs(ratio - measured), (
            f"第 {count} 段：折行模型 {view.paragraph_pixel(count):.0f}px 不比"
            f"字符占比 {ratio:.0f}px 准")
    assert worst_ratio > 5, (
        f"字符占比在这份真值上最坏只差 {worst_ratio:.1f} 段，测不出两把尺的区别")


def test_replacing_the_document_drops_the_stale_measured_height():
    """整篇换章后不许再用上一张文档的高度定位；追加（读到底接下一章）不清。"""
    view = _mount("uniform", UNIFORM_HOST)
    assert view.paragraph_pixel(10) is not None, "挂载后没拿到尺寸，下面测的不是这件事"

    view.render_plan(view.content_plan(ChapterContent(
        title="另一章", blocks=[ContentBlock("text", "短得多的一章正文。")])),
        append=False, chapter_name="")
    assert view.paragraph_pixel(0) is None, "换了整篇文档却还带着上一张的渲染高"

    view._on_content_size(SimpleNamespace(width=728.0, height=2000.0))
    assert view.paragraph_pixel(0) == 0.0
    view.render_plan(view.content_plan(ChapterContent(
        title="再接一章", blocks=[ContentBlock("text", "又一段正文。")])),
        append=True, chapter_name="再接一章")
    assert view.paragraph_pixel(0) == 0.0, "追加不该把已经量到的尺寸清零"


def test_paragraph_pixel_is_monotonic_and_needs_a_measurement():
    """没量到渲染尺寸就返回 None（不许瞎滚）；量到之后 y 随段号单调不减。"""
    view = ReaderView()
    view.render_plan(view.content_plan(ChapterContent(
        title="测试章", blocks=[ContentBlock("text", para)
                                for para in paragraphs_of("uniform", 60)])),
        append=False, chapter_name="")
    assert view.paragraph_pixel(30) is None, "还没量到尺寸就该说不知道"
    assert view.scroll_to_paragraph(30) is False

    view._on_content_size(SimpleNamespace(width=728.0, height=12000.0))
    ys = [view.paragraph_pixel(i) for i in range(0, 60)]
    assert ys[0] == 0.0
    assert all(b >= a for a, b in zip(ys, ys[1:])), f"段号越大 y 反而越小：{ys}"
    assert ys[-1] <= 12000.0


# --------------------------------------------------------------------------- #
# 4. 接线：点命中 → 换章 → 滚到那段
# --------------------------------------------------------------------------- #
class _JumpSpyView(ReaderView):
    """记录段级定位的调用（子类当替身，不改生产面）。"""

    def __init__(self) -> None:
        super().__init__()
        self.jump_attempts: list[int] = []
        self.measured = True

    def scroll_to_paragraph(self, paragraph_index: int) -> bool:
        self.jump_attempts.append(int(paragraph_index))
        return bool(self.measured)


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


@contextlib.contextmanager
def mounted_reader(storage, cache: ReadingCache, value: str, view: ReaderView):
    """装一台"书已经打开、正文挂在屏上"的阅读器（视图与服务都是真的）。"""
    save_chapter(cache, 0, value)
    cs = ContentSearchService(storage=storage, cache=cache)
    asyncio.run(cs.on_start())
    session = SimpleNamespace(source_url=SOURCE, book_url=BOOK,
                              chapters=[SimpleNamespace(url="ch0", index=0)],
                              viewports={}, position=None)
    document = ChapterContent(title="第0章", blocks=[ContentBlock("text", value)])
    reading = SimpleNamespace(
        current_session=session, stop_prefetch=lambda: None,
        load_content=AsyncMock(return_value=document),
        load_chapters=AsyncMock(return_value=[]),
        prefetch_chapters=AsyncMock(),
        get_reader_preference=AsyncMock(return_value=None),
        save_reader_preference=AsyncMock(), save_progress=AsyncMock(),
        staged_record=lambda: None)
    hub = SimpleNamespace(
        bus=SimpleNamespace(subscribe=lambda *a, **k: (lambda: None),
                            publish=lambda e: []),
        get=lambda stype: {ReadingService: reading,
                           BookmarkService: SimpleNamespace(
                               list_bookmarks=AsyncMock(return_value=[])),
                           ContentSearchService: cs}[stype])
    controller = ReaderController(hub)
    controller.attach(view)
    controller._book_url = BOOK
    controller._readable = [{"name": "第0章", "url": "ch0"}]

    saved = ui_loop()
    loop, thread = _start_ui_loop()
    set_ui_loop(loop)
    try:
        yield controller, cs
    finally:
        set_ui_loop(saved)
        controller.detach()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def test_clicking_a_hit_scrolls_to_that_paragraph_not_the_chapter_top(storage, cache):
    """端到端：搜索 → 点第三段那条命中 → 视图被要求滚到第 2 段。"""
    view = _JumpSpyView()
    with mounted_reader(storage, cache, THREE_PARAGRAPHS, view) as (ctl, cs):
        rendered = view.content_search_bar._results.controls
        ctl.search_in_book(WORD)          # 真实入口：搜索条那颗按钮
        assert _wait_until(lambda: len(rendered) == 3), (
            f"三条命中没渲染出来（{len(rendered)} 条），状态文案="
            f"{view.content_search_bar._result_count.value!r}")
        payload = asyncio.run(cs.search(BOOK, WORD))
        assert sorted(int(h["para_index"]) for h in payload) == [0, 1, 2]

        ctl.jump_to_hit(payload[2])
        assert _wait_until(lambda: view.jump_attempts), "点命中后一次段级定位都没发起"
        assert view.jump_attempts[0] == 2, f"滚错了段：{view.jump_attempts}"
        assert view.paragraph_count() == 3, "章没换成就绪，正文段数不对"


def test_jump_without_measured_height_still_opens_the_chapter(storage, cache):
    """量不到渲染尺寸时退回章首（改前的行为），不许抛也不许停在原章。"""
    view = _JumpSpyView()
    view.measured = False
    with mounted_reader(storage, cache, THREE_PARAGRAPHS, view) as (ctl, cs):
        assert asyncio.run(cs.ensure_index(BOOK, SOURCE)) == 3
        payload = asyncio.run(cs.search(BOOK, WORD))
        ctl.jump_to_hit(payload[2])
        assert _wait_until(lambda: len(view.jump_attempts) >= 3), (
            f"补帧没补满：{view.jump_attempts}")
        assert all(attempt == 2 for attempt in view.jump_attempts)
        assert view.paragraph_count() == 3, "整篇替换没发生，还挂着旧的正文"
