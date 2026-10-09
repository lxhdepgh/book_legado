"""G3 位置契约（设计 §9 门 G3 的第一条判据）：翻页落点存**章内字符偏移**，
那个数由"落在第几段"**算**出来，不是由像素**猜**出来。

为什么这一轮动产品代码：#88 的取证一（``tests/test_reader_position_paragraph.py``
头部，现场探针 ``.probe/pos88_cross_ruler_attr.py``）实测老的 ``chapter_pos`` 是
``实测字数 × 像素比例`` 插出来的，**连同尺都差 7 段**。同一套 flet 里它只是"差不多"
的续读估算，还能用；迁移 G3 之后它是 flet 与 Dart **比对落点的那把尺**——一把两侧
各猜一次的尺比出来的"一致"是自证。段号是跨装尺唯一还认得回来的数，所以偏移改由段号
导出（段首是事实，段内那一截按像素比例估且**不越过段尾**）。

三笔账，每笔钉的是"没发生"而不是"没失败"（记忆 ``guard-tests-assert-absence``）：

1. **同一把尺**：``chapter_paragraphs`` 切出来的段必须与视图挂出来的段逐字相等。
   这条不能拿"两边都调 paragraph_lines"糊弄——视图那侧还多一步段首缩进（每段 +2 字），
   拿缩进后的段算偏移，整章偏移会系统性漂，所以同时钉"坐标取自不缩进的那一份"。
2. **可逆**：``offset_of_paragraph`` 与 ``paragraph_at_offset`` 互为正反函数，
   段间那一格换行归**前一段的段尾**（归错的话同一次滚动会在压着换行那一帧把人
   弹到下一段开头），两端都要 clamp，空章回 (0, 0) 而不是造假段。
3. **口径只有一份**：``<usehtml>`` 的渲染会改字数。它原来在控制器里做，现在收进
   ``reading_engine.display_blocks``，控制器只剩一次转呼。这一条用"控制器源码里
   再也数不出第二次渲染"来钉——两侧各渲染一遍正是本门要防的那件事。

语料用真形状（带 ``<usehtml>`` 标记、夹媒体块、空行、行首行尾空白），不拿三段假文
凑数：段长均匀时 clamp 与越界那几条能自证（记忆
``measurement-corpus-must-not-be-self-proving``），这里刻意让各段长短不齐，并留下一发
"标记渲染之后段长真的变了"的正对照。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.controllers.reader_controller import ReaderController
from app.core.text_layout import (
    chapter_paragraphs, chapter_text_length, offset_of_paragraph,
    paragraph_at_offset, paragraph_lines, paragraph_offsets)
from app.services.engine.reading_engine import (
    ChapterContent, ContentBlock, display_blocks, display_document)
from app.services.reading.reading_service import ReadingSession
from app.views.reading.reader_view import ReaderView

#: 一段带标记的正文：渲染前含 ``<usehtml>`` 与它的样式，渲染后只剩字。
USE_HTML = '<usehtml><span style="color:#333">第0段　标记会被渲染掉</span></usehtml>'
#: 故意长短不齐，并夹一条空行与一条首尾空白行。
RAW_PARAGRAPHS = [
    USE_HTML,
    "第1段　长短不齐是给正反函数准备的：这一行故意比下一行长很多，"
    "因为段长均匀的话 clamp 与越界那几条会被自证。",
    "",
    "　第2段　前面那行是空行，切段要整条丢掉。",
    "　第3段　行首行尾空白要去掉，段内连续空格要压成一个。　　中间　　这里压一下。　",
]
TAIL = "第4段　媒体块不产段：它挂在正文之间但不数段。"


def _raw_document() -> ChapterContent:
    return ChapterContent(
        title="契约章",
        blocks=[
            ContentBlock("text", "\n\n".join(RAW_PARAGRAPHS)),
            ContentBlock("image", "http://127.0.0.1:1/a.png", title="插图"),
            ContentBlock("text", TAIL),
        ],
        raw_content="原文")


def _display_paragraphs() -> list[str]:
    return chapter_paragraphs(display_blocks(_raw_document()))


def _view(indent: bool) -> ReaderView:
    view = ReaderView()
    view._preferences = dict(view._preferences, indent=indent)
    return view


def _plan_lines(view: ReaderView, document) -> list[str]:
    """视图真正挂出来的正文段（标题算段，媒体块不算）。

    视图吃的是**可显示形态**的文档，测试要照同一条路走：控制器给视图的是
    ``display_document(document)``，喂原文的话这里会比的是两把不同的尺。
    """
    return [line for kind, payload, _meta in view.content_plan(document)
            if kind == "text" for line, _heading in payload]


# --------------------------------------------------------------------------- #
# 一、同一把尺：段切分与视图逐字相等，且坐标不吃缩进
# --------------------------------------------------------------------------- #
def test_chapter_paragraphs_equal_the_rendered_paragraphs():
    shown = display_document(_raw_document())
    assert chapter_paragraphs(shown.blocks) == _plan_lines(_view(indent=False), shown), (
        "坐标用的段列表与视图挂出来的不是同一把尺——落库偏移指向的段"
        "与屏幕上的第几段从此不是一回事")


def test_the_coordinate_ruler_is_the_unindented_one():
    """缩进每段多两个字：拿显示段算偏移会整体漂，这条把两件事分开钉住。"""
    shown = display_document(_raw_document())
    offsets = paragraph_offsets(chapter_paragraphs(shown.blocks))
    indented = _plan_lines(_view(indent=True), shown)
    plain = _plan_lines(_view(indent=False), shown)
    assert len(indented) == len(plain) == len(offsets), "缩进不该改段数"
    assert indented != plain, "这轮语料没触到缩进分支，这条在测空气"
    for index in range(len(plain)):
        assert offset_of_paragraph(plain, index) == offsets[index]
        if index:
            assert offset_of_paragraph(indented, index) != offsets[index], (
                f"第 {index} 段的偏移是按缩进后的段算的——每段多两个字，整章系统性漂")


def test_media_and_blank_lines_do_not_make_paragraphs():
    blocks = display_blocks(_raw_document())
    paragraphs = chapter_paragraphs(blocks)
    assert all("<usehtml>" not in text and "<span" not in text for text in paragraphs)
    assert all(text and "\n" not in text for text in paragraphs)
    assert any("插图" in str(getattr(b, "title", "")) for b in blocks), "媒体块被丢了"
    # 媒体块的**值**也不许混进段里：它的 title 是给人看的，它的值是一个 URL。
    joined = "\n".join(paragraphs)
    assert "127.0.0.1" not in joined, "媒体块的地址被当成一段正文数进了坐标"
    assert len(paragraphs) == len([line for line in _plan_lines(
        _view(indent=False), display_document(_raw_document()))])


def test_usehtml_rendering_changes_the_offsets_positive_control():
    """正对照：不渲染就直接算偏移的话，偏移会变。少这一发，上面那条相等可能自证。"""
    rendered = paragraph_offsets(_display_paragraphs())
    raw = paragraph_offsets(chapter_paragraphs(_raw_document().blocks))
    assert rendered != raw, "<usehtml> 没让字数变，这一轮它不构成口径差"
    assert paragraph_at_offset(_display_paragraphs(), rendered[1]) == (1, 0)


# --------------------------------------------------------------------------- #
# 二、正反函数：可逆、边界归属、clamp、空章
# --------------------------------------------------------------------------- #
def test_offset_and_paragraph_are_inverses():
    paragraphs = _display_paragraphs()
    offsets = paragraph_offsets(paragraphs)
    assert offsets[0] == 0
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets), (
        "段首要单调且不重复")
    for index, text in enumerate(paragraphs):
        assert offset_of_paragraph(paragraphs, index) == offsets[index]
        assert paragraph_at_offset(paragraphs, offsets[index]) == (index, 0), (
            f"第 {index} 段段首回不来")
        for step in (1, len(text) // 2, len(text) - 1, len(text)):
            if step < 1:
                continue
            landed, local = paragraph_at_offset(paragraphs, offsets[index] + step)
            assert landed == index, f"段内第 {step} 字跳到了第 {landed} 段"
            assert local == min(step, len(text))


def test_the_joiner_slot_belongs_to_the_previous_paragraph():
    paragraphs = _display_paragraphs()
    offsets = paragraph_offsets(paragraphs)
    index = len(paragraphs) - 1
    landed, local = paragraph_at_offset(paragraphs, offsets[index] - 1)
    assert landed == index - 1, (
        f"压着换行那一格被判成了第 {index} 段，回位会把人弹到段首")
    assert local == len(paragraphs[index - 1])


def test_the_character_stream_is_the_joined_paragraphs_themselves():
    """偏移的单位钉死成 ``"\\n".join(段)`` 的下标——**不读那个常量**，读那串字本身。

    拿 ``PARAGRAPH_JOINER_LEN`` 去验 ``PARAGRAPH_JOINER_LEN`` 会永远绿：把段间那一格
    改成 0，字符流就与 ``"\\n".join`` 差一段，两侧的段首从此各指各处。
    """
    paragraphs = _display_paragraphs()
    stream = "\n".join(paragraphs)
    assert chapter_text_length(paragraphs) == len(stream)
    for index, offset in enumerate(paragraph_offsets(paragraphs)):
        assert stream[offset:offset + len(paragraphs[index])] == paragraphs[index], (
            f"第 {index} 段的段首偏移指错了那串字里的位置")


def test_both_ends_clamp_and_an_empty_chapter_stays_empty():
    paragraphs = _display_paragraphs()
    stream = "\n".join(paragraphs)
    total = len(stream)
    assert offset_of_paragraph(paragraphs, len(paragraphs)) == total
    assert offset_of_paragraph(paragraphs, len(paragraphs) + 99) == total
    assert offset_of_paragraph(paragraphs, -3) == 0
    assert paragraph_at_offset(paragraphs, -1) == (0, 0)
    # 超出章尾的偏移：段号停到最后一段，段内偏移停在该段末尾（不许造出第 len 段）
    assert paragraph_at_offset(paragraphs, total + 500) == (
        len(paragraphs) - 1, len(paragraphs[-1]))
    assert paragraph_at_offset(paragraphs, total) == (
        len(paragraphs) - 1, len(paragraphs[-1]))
    assert chapter_text_length([]) == 0
    assert offset_of_paragraph([], 4) == 0
    assert paragraph_at_offset([], 17) == (0, 0), "空章要回 0 段，不能造一段出来"


def test_the_whole_chapter_text_is_not_a_paragraph_list():
    """把整章正文当段列表传进来是最顺手的一种错：必须当场抛，不许逐字展开。"""
    with pytest.raises(TypeError):
        paragraph_offsets("第一段第二段")
    with pytest.raises(TypeError):
        paragraph_at_offset("第一段第二段", 3)
    with pytest.raises(TypeError):
        chapter_text_length(["第一段", None])
    with pytest.raises(TypeError):
        offset_of_paragraph(["第一段", 12], 1)


def test_paragraph_lines_stays_the_single_splitter():
    """``chapter_paragraphs`` 不许自己再切一遍：它必须只调 paragraph_lines。"""
    source = inspect.getsource(chapter_paragraphs)
    assert "paragraph_lines(" in source
    assert ".split(" not in source and "re.sub" not in source, (
        "这里又写了一份切段逻辑——显示、索引、坐标从此三把尺")


# --------------------------------------------------------------------------- #
# 三、会话接线：track 报出的 character_offset 必须由段号导出
# --------------------------------------------------------------------------- #
class _Chapter:
    def __init__(self, url: str, index: int) -> None:
        self.url, self.index, self.name = url, index, url


def _mount(session: ReadingSession, chapter: _Chapter, document_start: float,
           offsets, text_length: int = 1000, paragraph_start: int = 0) -> None:
    session.mount(chapter, document_start, text_length, paragraph_start,
                  tuple(offsets))


def test_track_stays_inside_the_anchored_paragraph():
    paragraphs = _display_paragraphs()
    offsets = paragraph_offsets(paragraphs)
    for paragraph in range(len(paragraphs)):
        session = ReadingSession()
        session.bind("http://127.0.0.1:1", "http://127.0.0.1:1/book",
                     [_Chapter("ch1", 0)])
        _mount(session, _Chapter("ch1", 0), 0.0, offsets)
        floor = offsets[paragraph]
        ceil = (offsets[paragraph + 1] if paragraph + 1 < len(offsets)
                else floor + len(paragraphs[paragraph]))
        for local in (0, (ceil - floor) // 2, max(0, ceil - floor - 1)):
            session.track(float(floor + local), float(offsets[-1] + 1), 16, paragraph)
            stored = session.position.character_offset
            assert floor <= stored < ceil, (
                f"第 {paragraph} 段段内第 {local} 字存成了 {stored}，越出 [{floor},{ceil})")
            assert paragraph_at_offset(paragraphs, stored)[0] == paragraph, (
                "存进去的偏移回位时不指回原来那一段")


def test_the_last_paragraph_has_no_intra_estimate():
    """最后一段后面没有别的段可减，所以那一格只认段首，不猜段内。

    段内偏移是按**整篇**像素比例估的（flet 那侧只有这个可用），所以除最后一段外
    拿不到"恰好段首"——除段首事实之外的都是估算，估算不该越过段尾，那一头由
    :func:`test_track_stays_inside_the_anchored_paragraph` 钉。
    """
    offsets = paragraph_offsets(_display_paragraphs())
    session = ReadingSession()
    session.bind("http://127.0.0.1:1", "http://127.0.0.1:1/book", [_Chapter("ch1", 0)])
    _mount(session, _Chapter("ch1", 0), 0.0, offsets)
    session.track(0.0, float(offsets[-1] + 1), 16, 0)
    assert session.position.character_offset == 0
    for pixels in (offsets[-1], offsets[-1] + 3, offsets[-1] + 900):
        session.track(float(pixels), float(offsets[-1] + 1), 16, len(offsets) - 1)
        assert session.position.character_offset == offsets[-1], (
            "最后一段的偏移不该随像素漂：那一段没有下界可减，漂了就是猜")


def test_without_offsets_the_old_estimate_is_still_reachable_and_labeled():
    """兜底口径要**看得见**：段号有、坐标没算过时退回旧的像素插值，而不是假称精确。"""
    session = ReadingSession()
    session.bind("s", "b", [_Chapter("ch1", 0)])
    _mount(session, _Chapter("ch1", 0), 0.0, ())
    session.track(500.0, 1000.0, 16, 2)
    assert session.position.character_offset == 500      # int(1000 × 0.5)
    assert session.position.chapter_paragraph == 2
    legacy = ReadingSession()
    legacy.bind("s", "b", [_Chapter("ch1", 0)])
    _mount(legacy, _Chapter("ch1", 0), 0.0, ())
    legacy.track(500.0, 1000.0, 16, None)
    assert legacy.position.chapter_paragraph is None


def test_appended_chapter_offsets_stay_chapter_local():
    """追加下一章：每一章的坐标都是**章内**的，段号与偏移都别串到上一章去。"""
    first = paragraph_offsets(_display_paragraphs())
    second = paragraph_lines("甲段\n乙段\n丙段")
    second_offsets = paragraph_offsets(second)
    session = ReadingSession()
    session.bind("s", "b", [_Chapter("ch1", 0), _Chapter("ch2", 1)])
    _mount(session, _Chapter("ch1", 0), 0.0, first)
    _mount(session, _Chapter("ch2", 1), 1000.0, second_offsets)
    position, visible = session.track(1000.0 + second_offsets[2], 2000.0, 16, 2)
    assert str(visible.url) == "ch2"
    assert position.chapter_paragraph == 2
    assert position.character_offset == second_offsets[2], (
        "追加章的偏移要按本章的坐标算，不是整篇累加")


def test_controller_delegates_the_display_transform_instead_of_doing_it_again():
    """口径收成一个函数之后，控制器不许留第二份渲染逻辑。"""
    source = inspect.getsource(ReaderController)
    assert "render_use_html_spans" not in source, (
        "控制器里又渲染了一次：两侧各渲染一遍就是这个门要防的那件事")
    assert "display_document(" in inspect.getsource(ReaderController._display_document)
    document = _raw_document()
    shown = display_document(document)
    assert shown.blocks == display_blocks(document)
    assert shown.image_style == "" and shown.title == "契约章"
    assert document.blocks[0].value != shown.blocks[0].value, "渲染没生效"


def test_the_controller_measures_offsets_off_the_same_blocks_it_renders():
    """控制器交给会话的那份坐标，必须是**可显示块**算出来的那一份。

    为什么不只看 ``chapter_paragraphs`` 绿：坐标是在 ``_content_plan`` 里现算的，
    那里顺手改成"从 plan 的段文本算"（那些段带缩进）也能一路绿到落库——差的就是
    每段两个字，而这一格正是本门要比对的那把尺。
    """
    controller = ReaderController.__new__(ReaderController)
    controller._view = _view(indent=True)
    document = _raw_document()
    plan, offsets = controller._content_plan(document)
    shown = display_document(document)
    assert offsets == paragraph_offsets(chapter_paragraphs(shown.blocks))
    assert offsets == paragraph_offsets(_plan_lines(_view(indent=False), shown))
    assert len(offsets) == len(_plan_lines(controller._view, shown))


def test_empty_blocks_and_rubbish_inputs_do_not_invent_paragraphs():
    assert chapter_paragraphs(None) == []
    assert chapter_paragraphs([]) == []
    assert chapter_paragraphs([SimpleNamespace(kind="text", value=None)]) == []
    assert chapter_paragraphs([SimpleNamespace(kind="image", value="x")]) == []
    assert chapter_paragraphs([SimpleNamespace(kind="", value="有字但没 kind")]) == []
