"""G3-D2 的连续阅读参照表（``tests/gateway/projection/reader.continuous.json``）。

§9 的 G3 判据第一条是"同一本书翻页落点与 flet 版可比（按字符偏移断言）"。C1~C3 量的是
**一页一章**：那一把尺的零点天然就是本章第一段，减不减起点都是同一个数。flet 不是这么读的
——读到章尾它把下一章**接在同一篇文档后面**（``_on_reader_scroll`` → ``_run_chapter(append=True)``），
于是屏幕上同时挂着 2~3 章，视图报的是**整篇**段号，而落库的必须是**章内**段号与**章内**字符偏移
（#88 取证二实测：不减起点差 259 段）。这一张表钉的就是那一步折算。

**答案全部由 flet 自己的函数现场读回**，一个都不许手写：

- 挂载顺序与"这一章在整篇里的段起点"：真 ``ReaderController`` 走真 ``_on_reader_scroll``
  被滚动事件触发追加，起点是它自己从 ``ReaderView.paragraph_count()`` 数出来的
  （``reader_controller.py:425``），落在真 ``ReadingSession.viewports`` 里；
- 表里的探针：像素由 flet 自己的 ``paragraph_pixel`` 给出（**输入**），滚进去之后
  读 ``session.position`` 的 ``chapter_index`` / ``chapter_paragraph`` / ``character_offset``
  （**答案**）——那是 ``ReadingSession.track`` 减完起点、按本章段首偏移算出来的那三个数。

语料为什么是 ``/g3cont`` 那一族而不是现成的 ``/g3``：``chapter_html`` 对每一章用同一串
``lengths``，两章的段数与段长**全同**，那时"减错了起点"与"减对了"会算出同一个
``character_offset``（记忆 ``measurement-corpus-must-not-be-self-proving``）。所以连续这一族
三章各一种形状——12 短段 / 3 长段带一张插图 / 7 段且首行是标题：段数 12、3、7 互不相同，
段起点因此是 0、12、15 三个不同的数；而第 2 章那张插图**占一行不产段**，按"屏上几行"
数起点的人会算成 0、12、18，一眼就见分晓。

重跑这张表：``LEGODO_WRITE_CONTINUOUS_PROJECTION=1 python -m pytest tests/test_reader_continuous_projection.py```
（表是快照，改了 flet 的排版却没重跑，第一条守卫就红。）

全程离线：正文由仓库里的语料过**纯管线**（不发请求），阅读服务是记账替面但会话是
真 ``ReadingSession``、正文是真 ``ChapterContent``（记忆 ``probe-stand-in-must-not-be-the-measured-workload``：
这里被测的是折算，不是取文）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "gateway"))

import fake_source_reader as fx  # noqa: E402
from app.core.text_layout import (chapter_paragraphs,  # noqa: E402
                                  paragraph_at_offset, paragraph_offsets)
from app.controllers.reader_controller import ReaderController  # noqa: E402
from app.services.engine.reading_engine import (  # noqa: E402
    _format_content_page, parse_content_blocks, process_book_content)
from app.services.engine.rule_engine import BookItem, ChapterItem  # noqa: E402
from app.services.reading.reading_service import (  # noqa: E402
    ReadingPosition, ReadingService, ReadingSession)
from app.services.reading.bookmark_service import BookmarkService  # noqa: E402
from app.services.reading.content_search import ContentSearchService  # noqa: E402
from app.views.reading.reader_view import ReaderView  # noqa: E402

PROJECTION = ROOT / "tests/gateway/projection"
TABLE = PROJECTION / "reader.continuous.json"

SOURCE = f"http://127.0.0.1:{fx.CANONICAL_PORT}/g3cont"
BOOK_URL = f"{SOURCE}/book/1.html"
#: 追加时喂给视图的渲染高（**输入**：那是客户端量给自己看的像素，Dart 侧另有一套）。
#: 分三档是因为章要一章一章长出来，每接一章文档就高一截。
STAGE_HEIGHTS = (9000.0, 18000.0, 24000.0)
WIDTH = 728.0
FONT_SIZE = 16
#: 探针落在整篇段号的哪几格：章内第 0 段、章中、章尾、以及**接缝**那两格
#: （第 12 段是第 2 章的第 0 段，第 15 段是第 3 章的第 0 段——接缝最容易减错起点）。
PROBE_PARAGRAPHS = (0, 5, 11, 12, 13, 14, 15, 17, 21)


def _corpus_document(index: int, *, body: str | None = None):
    """语料那一章过一遍引擎的纯管线，交出**渲染前**的那一份 ``ChapterContent``。

    渲染（``display_document``）故意不在这里做：真控制器自己会渲染一次
    （``_display_document``，:658 那条 ``_content_plan`` 顺带算段首偏移），
    替它做就等于把被测那条臂的输入提前洗好了。
    """
    chapter_url = f"{SOURCE}/ch/{index}.html"
    page = BeautifulSoup(body if body is not None else fx.cont_chapter_html(index),
                         "html.parser")
    value = str(page.select_one(".list"))       # ``@html`` 取外层 str(node)
    book = BookItem(name="连续阅读书", author="作者戊", url=BOOK_URL)
    chapter = ChapterItem(name=f"第{index}章 连续章", url=chapter_url, index=index - 1)
    joined, title = process_book_content(_format_content_page(value, chapter_url),
                                         book, chapter)
    document = parse_content_blocks(joined, chapter_url)
    document.title = title
    return document


def _documents(overrides: dict[int, str] | None = None):
    documents = {f"{SOURCE}/ch/{i}.html": _corpus_document(i)
                 for i in range(1, fx.CONT_CHAPTER_COUNT + 1)}
    for index, body in (overrides or {}).items():
        documents[f"{SOURCE}/ch/{index}.html"] = _corpus_document(index, body=body)
    return documents


class _AppendReading:
    """阅读服务的记账替面：会话是**真** ``ReadingSession``，正文是**真** ``ChapterContent``。

    只替掉取文那一层（网络），不替折算：``load_content`` 的调用次数是记了账的，
    "接来的那一章真被取过、且只取过一次"这句话要有非 0 的对照
    （记忆 ``zero-counts-need-a-positive-control``）。
    """

    def __init__(self, chapters, documents) -> None:
        self.current_source = SimpleNamespace(bookSourceUrl=SOURCE)
        self.current_session = ReadingSession()
        self.current_session.bind(SOURCE, BOOK_URL, chapters, None)
        self._documents = documents
        self.content_calls: list[str] = []
        self.saved: list[tuple[int, ReadingPosition]] = []

    async def open_book(self, source, book, shelve=True):
        return self.current_session

    async def load_content(self, chapter, append=False):
        url = str(chapter.url)
        self.content_calls.append(url)
        return self._documents[url]

    async def prefetch_chapters(self, chapter):
        return None

    def stop_prefetch(self) -> None:
        return None

    async def save_progress(self, chapter_index, position):
        self.saved.append((int(chapter_index), position))

    async def get_reader_preference(self, key, default=None):
        return default

    async def save_reader_preference(self, key, value):
        return None


class _Hub:
    def __init__(self, reading) -> None:
        self.bus = None
        self._services = {ReadingService: reading,
                          BookmarkService: SimpleNamespace(),
                          ContentSearchService: SimpleNamespace()}

    def get(self, stype):
        return self._services[stype]


def _settled(predicate, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _size(width: float, height: float) -> SimpleNamespace:
    return SimpleNamespace(width=width, height=height)


def _scroll(pixels: float, maximum: float) -> SimpleNamespace:
    return SimpleNamespace(pixels=pixels, max_scroll_extent=maximum)


def _report(view: ReaderView, height: float) -> None:
    """把"客户端量到的渲染高"报给视图：没有它，段顶像素与段号都量不出来。"""
    view._on_content_size(_size(WIDTH, height))
    assert view.paragraph_count() > 0, "报尺寸时屏幕上一段正文都没有"


def flet_continuous_table(*, documents=None) -> dict:
    """开书 → 滚到底接下一章（两回）→ 按整篇段号逐个探针滚进去，读回 flet 的答案。

    返回的那张表里，``chapters`` 与 ``probes`` 的每一个数都是从真 ``ReaderView`` /
    真 ``ReadingSession`` / 真 ``ReaderController`` 身上读的，不是这张表自己算的。
    """
    chapters = [ChapterItem(name=f"第{i}章 连续章", url=f"{SOURCE}/ch/{i}.html",
                            index=i - 1) for i in range(1, fx.CONT_CHAPTER_COUNT + 1)]
    reading = _AppendReading(chapters, documents or _documents())
    view = ReaderView()
    controller = ReaderController(_Hub(reading))
    controller.attach(view)
    session = reading.current_session
    try:
        asyncio.run(controller.open_book(SimpleNamespace(bookSourceUrl=SOURCE),
                                         BookItem(name="连续阅读书", url=BOOK_URL)))
        assert _settled(lambda: len(session.viewports) == 1), "开书没挂出第一章"

        # 追加：不发内部函数调参，走真滚动事件——"到底了"这句话由 flet 自己判
        # （离底 < 180px 且已武装，``_APPEND_TRIGGER_DISTANCE``）。迟滞那条线是
        # flet 自己的设计：先要一发离底 > 500px 的事件把"自动续读"**布防**，
        # 贴底那一发才会真的接章（``_APPEND_REARM_DISTANCE``），所以每一档发两拍。
        append_trail: list[dict] = []
        for stage, height in enumerate(STAGE_HEIGHTS, start=1):
            _report(view, height)
            before = [str(url) for url in session.viewports]
            controller._restoring_scroll = False       # 挂载噪音那一拍不算用户读到哪儿
            controller._on_reader_scroll(_scroll(height - 600.0, height))
            controller._on_reader_scroll(_scroll(height - 100.0, height))
            expected = min(stage + 1, fx.CONT_CHAPTER_COUNT)
            assert _settled(lambda: len(session.viewports) == expected), \
                (f"滚到第 {stage} 档的底部没把章接上（视口 {len(session.viewports)} 个，"
                 f"该 {expected} 个）")
            append_trail.append({
                "stage": stage,
                "rearm_pixels": height - 600.0,
                "scroll_pixels": height - 100.0,
                "max_scroll_extent": height,
                "mounted_before": before,
                "mounted_after": [str(url) for url in session.viewports],
            })

        ordered = sorted(session.viewports.values(),
                         key=lambda item: item.document_start)
        rows = []
        for viewport in ordered:
            chapter = viewport.chapter
            rows.append({
                "chapter_url": str(chapter.url),
                "chapter_index": int(chapter.index),
                "chapter_name": str(chapter.name),
                "paragraph_start": viewport.paragraph_start,
                "paragraph_count": len(viewport.paragraph_offsets),
                "offsets": list(viewport.paragraph_offsets),
                "text_length": viewport.text_length,
                "document_start": viewport.document_start,
            })

        height = STAGE_HEIGHTS[-1]
        probes = []
        for paragraph in PROBE_PARAGRAPHS:
            pixel = view.paragraph_pixel(paragraph)
            assert pixel is not None, "量不到装尺，这一发探针测的是空气"
            # track 在 ``_on_reader_scroll`` 里是同步跑的（落库那一发才是节流协程），
            # 所以这一发滚完读到的就是这一发的答案。
            controller._on_reader_scroll(_scroll(pixel, height))
            position = session.position
            probes.append({
                "document_paragraph": paragraph,
                # **不 round**：Dart 那侧要拿这一个像素把 ratio 重算一遍来比对 `track`
                # 的算术，round 到 3 位就把它引入的误差算进了判据里。
                "flet_pixel": float(pixel),
                "answer": {
                    "chapter_index": position.chapter_index,
                    "chapter_url": position.chapter_url,
                    "chapter_paragraph": position.chapter_paragraph,
                    "character_offset": position.character_offset,
                    # 不 round：这是 flet 用的那一个 float，表里要能拿它把偏移重算回去。
                    "chapter_ratio": position.chapter_ratio,
                },
            })

        return {
            "corpus": {
                "family": "/g3cont",
                "source_url": SOURCE,
                "book_url": BOOK_URL,
                "keyword": fx.CONT_KEYWORD,
                "chapters": [str(chapter.url) for chapter in chapters],
                "note": ("正文 = tests/gateway/fake_source_reader.py 的 cont_chapter_html(i) "
                         "过引擎纯管线（@html→_format_content_page→process_book_content→"
                         "parse_content_blocks），渲染那一步由真控制器自己做。"),
            },
            "layout": {"width": WIDTH, "font_size": FONT_SIZE,
                       "stage_heights": list(STAGE_HEIGHTS),
                       "indent": bool(view._preferences["indent"]),
                       "line_height": float(view._preferences["line_height"]),
                       "paragraph_spacing": float(view._preferences["paragraph_spacing"])},
            "append_trail": append_trail,
            "chapters": rows,
            "document": {
                "paragraph_total": view.paragraph_count(),
                "screen_controls": len(view._content_host.controls),
            },
            "probes": probes,
            "content_calls": sorted(set(reading.content_calls)),
        }
    finally:
        controller.detach()


def _table_on_disk() -> dict | None:
    if not TABLE.exists():
        return None
    return json.loads(TABLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def table() -> dict:
    """表缺了要**报红**而不是跳过：一屋子 skip 看起来和全绿一样（记忆 ``zero-counts-need-a-positive-control``）。"""
    on_disk = _table_on_disk()
    if on_disk is None:
        pytest.fail(f"参照表没落地：{TABLE}——先跑 docstring 里那条 "
                    "LEGODO_WRITE_CONTINUOUS_PROJECTION=1 的命令")
    return on_disk


def _write_table(table: dict) -> None:
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    TABLE.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")


# --------------------------------------------------------------------------- #
# 一、表还对着当前的 flet
# --------------------------------------------------------------------------- #
def test_the_table_is_what_flet_computes_now():
    """现算一遍与文件比——改了 flet 的排版/折算却没重跑探针，这里红。"""
    live = flet_continuous_table()
    on_disk = _table_on_disk()
    if os.environ.get("LEGODO_WRITE_CONTINUOUS_PROJECTION") == "1":
        _write_table(live)
        on_disk = live
    assert on_disk is not None, (
        f"参照表没落地：{TABLE}——先跑 "
        "LEGODO_WRITE_CONTINUOUS_PROJECTION=1 python -m pytest tests/test_reader_continuous_projection.py")
    assert live == on_disk, (
        "连续阅读参照表与当前 flet 不一致：flet 的 append/折算改过了，表没重跑。\n"
        f"磁盘上 chapters={[(row['chapter_url'][-9:], row['paragraph_start']) for row in on_disk['chapters']]}\n"
        f"现     算 chapters={[(row['chapter_url'][-9:], row['paragraph_start']) for row in live['chapters']]}")


# --------------------------------------------------------------------------- #
# 二、起点是谁数的：UI 计数器与那把尺必须数出同一个数
# --------------------------------------------------------------------------- #
def test_paragraph_start_matches_the_ruler_not_the_line_count(table):
    """段起点必须等于**前面各章的段数之和**（那把尺），不是屏上行数。

    第 2 章挂了一张插图：它占一行、不产段。按行累加的人给第 3 章的起点是 18 而不是
    15——所以这条断言要连着 ``screen_controls`` 一起看：屏上控件比段多，多的就是
    分隔线、章名与那张图。
    """
    rows = table["chapters"]
    assert [row["paragraph_start"] for row in rows] == [
        0, rows[0]["paragraph_count"],
        rows[0]["paragraph_count"] + rows[1]["paragraph_count"]], (
        "段起点不是前面各章段数之和——起点数错了，落库的章内段号就跟着错")
    assert [row["paragraph_count"] for row in rows] == [12, 3, 7], (
        f"语料那三种形状变了：{[row['paragraph_count'] for row in rows]}")
    # 正对照：屏上的控件确实比段多，否则"按行数"这条错路与正确路是同形，比不出什么。
    assert table["document"]["screen_controls"] > table["document"]["paragraph_total"], (
        "屏上控件数不大于段数，那这条守卫退化成自证")
    # 起点三个不同的数：全相同时"减没减起点"读起来一样（记忆 measurement-corpus-must-not-be-self-proving）
    assert len({row["paragraph_start"] for row in rows}) == len(rows)


def test_document_paragraph_total_is_the_sum_of_chapters(table):
    """整篇段数 = 各章段数之和 = 视图自己数出来的那一个数。"""
    total = sum(row["paragraph_count"] for row in table["chapters"])
    assert total == table["document"]["paragraph_total"] == 22


# --------------------------------------------------------------------------- #
# 三、探针答案必须落回本章那把尺
# --------------------------------------------------------------------------- #
def test_every_probe_answers_in_the_chapter_it_landed_in(table):
    """每一发探针：整篇段号减成章内段号、章内段号查出章内字符偏移，两头都要对得上。

    这里刻意**不读 flet 的常量**：偏移从表里的 ``offsets`` 列（flet 挂在视口上的那一份）
    与 ``chapter_ratio``（flet 自己量到的段内比例）重算一遍，等号必须成立。
    段首是事实，段内那一截是估算且不许越过段尾；最后一段没有下界，只认段首。
    """
    by_url = {row["chapter_url"]: row for row in table["chapters"]}
    for probe in table["probes"]:
        answer = probe["answer"]
        row = by_url[answer["chapter_url"]]
        offsets = row["offsets"]
        local = answer["chapter_paragraph"]
        assert probe["document_paragraph"] == row["paragraph_start"] + local, (
            f"整篇第 {probe['document_paragraph']} 段减完起点不是 {local}："
            f"{row['paragraph_start']} 那一条起点用错了章")
        assert 0 <= local < len(offsets), f"章内段号越界：{local} / {len(offsets)}"
        assert answer["character_offset"] == offsets[local] + int(
            ((offsets[local + 1] - offsets[local] - 1) if local + 1 < len(offsets) else 0)
            * answer["chapter_ratio"]), (
            f"探针 {probe} 的字符偏移不是本章那把尺算出来的")
        # 估算不许把人送出本段：回位必须还认这一段
        paragraphs = chapter_paragraphs(
            _displayed(row["chapter_url"]).blocks)
        assert paragraph_at_offset(paragraphs, answer["character_offset"])[0] == local, (
            f"存进去的偏移回位时不指回第 {local} 段")
        assert answer["chapter_index"] == row["chapter_index"]


def _displayed(chapter_url: str):
    """表里那一章的**显示形态**（渲染后）：与真控制器走的是同一个函数。

    渲染只在第一次算，之后复用——这张表有 9 个探针和两条守卫都要读段表，反复渲染
    一章只是把守卫跑慢，不会让它更真。
    """
    from app.services.engine.reading_engine import display_document
    if chapter_url not in _DISPLAY_CACHE:
        _DISPLAY_CACHE[chapter_url] = display_document(_documents()[chapter_url])
    return _DISPLAY_CACHE[chapter_url]


_DISPLAY_CACHE: dict = {}


def test_the_last_paragraph_of_a_chapter_never_guesses(table):
    """最后一段没有下界可减：段内比例再大，字符偏移也只能是段首。

    第 2 章只有 3 段，整篇第 14 段就是它的最后一段——那一条比例不是 0 而偏移仍等于段首，
    这一格是"估算不越界"那句话在连续阅读里的形状。
    """
    row = next(item for item in table["chapters"] if item["paragraph_count"] == 3)
    probe = next(item for item in table["probes"]
                 if item["answer"]["chapter_url"] == row["chapter_url"]
                 and item["answer"]["chapter_paragraph"] == 2)
    assert probe["answer"]["chapter_ratio"] > 0.0, "这一发探针比例是 0，测不到'不越界'"
    assert probe["answer"]["character_offset"] == row["offsets"][2]


def test_the_seam_paragraphs_belong_to_the_next_chapter(table):
    """接缝那两格（整篇第 12、15 段）必须归下一章，且都落在章内第 0 段。"""
    by_paragraph = {item["document_paragraph"]: item["answer"]
                    for item in table["probes"]}
    rows = table["chapters"]
    for expected_start, expected_chapter in ((rows[1]["paragraph_start"], 1),
                                             (rows[2]["paragraph_start"], 2)):
        answer = by_paragraph[expected_start]
        assert answer["chapter_index"] == expected_chapter, (
            f"整篇第 {expected_start} 段没归到第 {expected_chapter} 章：{answer}")
        assert answer["chapter_paragraph"] == 0
        assert answer["character_offset"] == 0, "章内第 0 段的偏移必须是 0"


# --------------------------------------------------------------------------- #
# 四、正对照：动正文，答案必须跟着动
# --------------------------------------------------------------------------- #
def test_feeding_a_changed_corpus_moves_the_answers():
    """往第 2 章的**第二段**多打 5 个字：本章后面的段首偏移与探针答案必须跟着走。

    不许动的那一头同样要说清楚：第 1 章自己的起点与偏移一个字都不该变——那才叫尺，
    不然改一处全屋都动，比对就退化成"两边都读同一份常量"。
    """
    shifted = flet_continuous_table(documents=_documents({
        2: fx.cont_chapter_html(2).replace("连续第2章第2段", "连续第2章第2段啊啊啊啊啊")}))
    base = flet_continuous_table()
    first = next(row for row in shifted["chapters"] if row["chapter_index"] == 0)
    assert first["offsets"] == next(
        row for row in base["chapters"] if row["chapter_index"] == 0)["offsets"], \
        "改的是第 2 章，第 1 章的段首偏移跟着动了——尺不是章内的"
    second = next(row for row in shifted["chapters"] if row["chapter_index"] == 1)
    base_second = next(row for row in base["chapters"] if row["chapter_index"] == 1)
    # 改的是第 2 章第 2 段：它**自己**的段首没动（那段之前一个字没多），动的只有它后面的。
    assert second["offsets"][:2] == base_second["offsets"][:2]
    assert second["offsets"][2:] == [item + 5 for item in base_second["offsets"][2:]], (
        f"多打 5 个字没让后面的段首整体右移：{second['offsets']}")
    assert second["text_length"] == base_second["text_length"] + 5
    # 第 3 章的**起点**仍是前面各章段数之和（段数没变），但它自己的探针答案要挪。
    third_probe = [item["answer"]["character_offset"] for item in shifted["probes"]
                   if item["answer"]["chapter_index"] == 2]
    base_third = [item["answer"]["character_offset"] for item in base["probes"]
                  if item["answer"]["chapter_index"] == 2]
    assert len(third_probe) == len(base_third) > 0


# --------------------------------------------------------------------------- #
# 五、这一张表测的是"真接了章"，不是手工挂了三章
# --------------------------------------------------------------------------- #
def test_the_chapters_were_appended_by_scrolling(table):
    """追加由滚动事件触发：三章各被取一次，且**没有任何一章是整篇替换挂上来的**。

    ``append_trail`` 是每一档滚动之后屏幕上挂了哪几章的账：如果第 2 章是被"换整篇"
    挂上来的，那一档前面第 1 章就没了（``_mount_chapter(reset=True)`` 会清空视口表）。
    """
    trail = table["append_trail"]
    # 三档滚动之后屏幕上挂着 2、3、3 章：最后一档已经读到账上最后一章，
    # "到底接下一章"那一臂必须**没有下一章可接**也不出错（`_adjacent_chapter` 回 None）。
    assert [len(row["mounted_after"]) for row in trail] == [2, 3, 3], (
        f"每一档挂了几章的账不对：{[len(row['mounted_after']) for row in trail]}")
    assert [len(row["mounted_before"]) for row in trail] == [1, 2, 3]
    assert all(len(row["mounted_after"]) >= len(row["mounted_before"])
               for row in trail), "有一档把已经挂上的章换掉了：那不是追加"
    assert table["content_calls"] == sorted(table["corpus"]["chapters"]), (
        "有章节没被取过，或取了目录里没有的那一章")
    # 正对照：接章那一路一次都没回顶部（像素起点单调递增）
    starts = [row["document_start"] for row in table["chapters"]]
    assert starts == sorted(starts) and starts[0] == 0.0 and len(set(starts)) == 3, (
        f"视口起点不单调：{starts}")


def test_table_shape_is_what_the_dart_arm_reads(table):
    """表本身的形状守卫：Dart 那侧按这些键读，键名/类型漂了就当红。"""
    assert set(table) >= {"corpus", "layout", "append_trail", "chapters",
                          "document", "probes", "content_calls"}
    for row in table["chapters"]:
        assert set(row) == {"chapter_url", "chapter_index", "chapter_name",
                            "paragraph_start", "paragraph_count", "offsets",
                            "text_length", "document_start"}
        assert isinstance(row["offsets"], list) and row["offsets"][0] == 0
        assert row["offsets"] == paragraph_offsets(
            chapter_paragraphs(_displayed(row["chapter_url"]).blocks)), (
            f"{row['chapter_url']} 挂在视口上的偏移与那把尺不一致")
    for probe in table["probes"]:
        assert set(probe) == {"document_paragraph", "flet_pixel", "answer"}
        assert set(probe["answer"]) == {"chapter_index", "chapter_url",
                                        "chapter_paragraph", "character_offset",
                                        "chapter_ratio"}
    assert [item["document_paragraph"] for item in table["probes"]] == list(PROBE_PARAGRAPHS)
