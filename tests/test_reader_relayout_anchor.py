"""#87 护栏：就地重排（改字号/行距/版心/缩进）之后，屏幕上还是**原来那一段**。

排版偏好改的是每段多高，而阅读位置按**裸像素**存：像素不动，段落就跟着尺子跑了。
真客户端取证（``.probe/relayout_anchor_attr.py``，300 段对话体，本地 flet web 面 +
Playwright 回读）：16 号挂在第 240 段（``pixels=21714.6``，版心 29238px），就地换 20
号后版心涨到 42994px，重排后客户端**没有一次新的滚动回读**——像素原样不动，而那个
像素在新装尺下是第 191 段（**退 49 段**）。补上回位之后同一篇落回第 240 段
（``pixels=31669.2``，差 0 段）。改前那条是把挂单钉死成 None 的单点变异现跑的。

所以这一轮钉的是"换算的时机与口径"，四条：

1. **反函数与正函数同一把尺**（``_paragraph_units``）：像素→段号→像素必须回得来。
2. **换算只在旧装尺还挂着的时候做**：新字号一写进偏好，反函数量到的就是还没生效
   的尺子；尺寸还没报上来时宁可不换算，也不许拿猜出来的宽度造一个像模像样的错值。
3. **回位要等客户端报来新的版心高**：重排后立刻按旧高度乘新段高，是个看着合理、
   实则偏移的数。高没变（重排没挪动任何人）就不滚；文档在等的这几百毫秒里被追加
   过（自动续读）也作废。
4. **不重排的偏好不许挂单**：背景、状态栏、划词开关只改颜色与可见性。

全程本地内存 + 合成章节，不联网、不读 ``resource/``、不开浏览器；
版心高与段顶真值取自 #86/#87 两轮现场读数（``.probe/offset_shape_attr.py``、
``.probe/out_shape_d20.txt``）。
"""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.views.reading.reader_view import ReaderView

TOTAL = 300
ANCHOR = 240
#: 对话密集区里的一个测点，专门用来验"换算的时机"：16 号下这一段段顶 2335.6px，
#: 换算晚一步（新段高乘还没生效的旧版心高）会答出第 70 段，像素不滚则答第 45 段。
#: 第 240 段是这套文档的两装尺重合点——算晚了照样恰好答对，时机缺陷在那里验不出。
MIXED_ANCHOR = 60
#: 同一篇 300 段对话体，客户端报来的版心渲染高（字号 16 / 20 各一套装尺）。
HOST_16 = (728.0, 38607.0)
HOST_20 = (728.0, 57614.0)
#: 字号 20 那套装尺下，浏览器现场量到的前缀段顶（真值，不是模型自造的）。
PREFIX_TOP_20 = {120: 9758.0, 240: 41730.0}


def paragraphs_of(total: int = TOTAL) -> list[str]:
    """取证六的对话形状：前 100 段每段一二十字，之后整墙叙述（同一随机种子）。

    任意前缀的"像素/字"都不是常数——换字号时两套装尺的差别也正断在这里。
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


class _ScrollSpy:
    """记账用的假滚动列：只收 ``scroll_to`` 的下发参数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def scroll_to(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _FakePage:
    """按 Flet 的规矩记账的假 page：只认协程函数，协程留着由 ``drain`` 点着。"""

    def __init__(self) -> None:
        self.pending: list = []

    def run_task(self, handler, *args, **kwargs) -> None:
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError("handler must be a coroutine function")
        self.pending.append(handler(*args, **kwargs))

    async def drain(self) -> None:
        while self.pending:
            await self.pending.pop(0)


@pytest.fixture
def wired(monkeypatch):
    """挂好 300 段对话体、报上字号 16 的版心高，并把滚动下发接上记账替身。"""
    view = ReaderView()
    paras = paragraphs_of()
    view.render_plan(view.content_plan(ChapterContent(
        title="测试章", blocks=[ContentBlock("text", para) for para in paras])),
        append=False, chapter_name="")
    assert view.paragraph_count() == TOTAL, "挂载段数与文档对不上，测的不是这篇"

    page = _FakePage()
    spy = _ScrollSpy()
    view._reader_scroll = spy                                  # type: ignore[assignment]
    monkeypatch.setattr(ReaderView, "page",
                        property(lambda self: page), raising=False)
    # 客户端布局完自己报尺寸（#86 取证四），生产里就是这个入口
    view._on_content_size(SimpleNamespace(width=HOST_16[0], height=HOST_16[1]))
    return view, page, spy


def _drain(page: _FakePage) -> None:
    asyncio.run(page.drain())


def _saved_position(view: ReaderView, page: _FakePage, spy: _ScrollSpy,
                    paragraph: int) -> float:
    """把正文滚到第 k 段并把客户端回读写进生产（模拟真 on_scroll 链）。"""
    assert view.scroll_to_paragraph(paragraph), "段级定位没发起（量不到尺寸）"
    _drain(page)
    pixels = spy.calls[-1]["offset"]
    view._on_document_scroll(SimpleNamespace(pixels=pixels))
    return float(pixels)


# --------------------------------------------------------------------------- #
# 一、像素 ⇄ 段号：一把尺子的正反两面
# --------------------------------------------------------------------------- #
def test_every_paragraph_round_trips_through_the_pixel(wired):
    """每一段：段顶像素反查回来还是这一段（300 段全数，不是抽查）。"""
    view, _page, _spy = wired
    for index in range(view.paragraph_count()):
        pixel = view.paragraph_pixel(index)
        assert view.paragraph_at_pixel(pixel) == index, (
            f"第 {index} 段顶 {pixel:.1f}px 反查回 {view.paragraph_at_pixel(pixel)}")


def test_the_inverse_is_not_a_proportional_map(wired):
    """反空转：段高差得很远，"像素 × 总段数 / 版心高"这种线性反比会答错。

    换算要是退化成比例，上面那条 round-trip 照样全绿（正函数也变比例），
    所以这里单独钉"形状确实不均匀"，并钉线性算法在同一个测点上就是错的。
    """
    view, _page, _spy = wired
    units = view._paragraph_units()
    assert min(units) * 4 < max(units), (
        f"段高最窄 {min(units):.0f}px 与最宽 {max(units):.0f}px 差得不够，"
        "这篇文档验不出非线性的尺子")
    height = view._content_size[1]
    for index in (ANCHOR, 90, 200):
        pixel = view.paragraph_pixel(index)
        naive = min(int(pixel * len(units) / height), len(units) - 1)
        assert view.paragraph_at_pixel(pixel) == index
        assert naive != index, f"第 {index} 段：线性反比恰好答对，测点没选中不均匀处"


def test_inverse_refuses_without_a_measured_ruler(wired):
    """量不到渲染尺寸时两个方向都给 None，不拿猜出来的宽度算错值。"""
    view, _page, _spy = wired
    assert view.paragraph_at_pixel(1234.0) is not None
    view._content_size = (0.0, 0.0)
    assert view.paragraph_at_pixel(1234.0) is None
    assert view.paragraph_pixel(ANCHOR) is None
    view._content_size = (HOST_16[0], 0.0)
    assert view.paragraph_at_pixel(1234.0) is None


def test_pixel_outside_the_document_clamps_instead_of_indexing_off(wired):
    view, _page, _spy = wired
    height = view._content_size[1]
    assert view.paragraph_at_pixel(-800.0) == 0
    assert view.paragraph_at_pixel(height * 3) == view.paragraph_count() - 1
    assert view.paragraph_at_pixel(height) == view.paragraph_count() - 1


# --------------------------------------------------------------------------- #
# 二、像素是从滚动事件里来的，且不许把落库链条挤掉
# --------------------------------------------------------------------------- #
def test_document_scroll_goes_through_the_recording_handler():
    """滚动事件先经生产自己的记账 handler，不是裸转给 Controller 的 lambda。"""
    view = ReaderView()
    assert view._reader_scroll.on_scroll == view._on_document_scroll


def test_scroll_event_records_the_pixel_and_still_saves_progress(wired):
    """``on_scroll`` 换了 handler，但 Controller 那条落库回调必须照样收到事件。

    两半都要钉：只记账不落库 → 续读进度再也不更新；只落库不记账 → 重排时无据可查。
    """
    view, _page, _spy = wired
    seen: list = []
    view.on_save_scroll = lambda event: seen.append(event)

    event = SimpleNamespace(pixels=4321.5, max_scroll_extent=9000.0)
    view._on_document_scroll(event)

    assert view._last_scroll_pixels == 4321.5
    assert seen == [event], "落库回调没收到原事件，续读进度要断"

    view._on_document_scroll(SimpleNamespace(pixels=None))
    assert view._last_scroll_pixels == 4321.5, "坏事件不该把记下的像素冲成 0"
    assert len(seen) == 2


def test_restore_scroll_records_the_pixel_before_the_echo(wired):
    """续读定位当场就把要去的像素记下来：等不到回读，第一次重排也得有依据。"""
    view, page, spy = wired
    view.restore_scroll(7777.0)
    _drain(page)
    assert view._last_scroll_pixels == 7777.0
    assert spy.calls[-1]["offset"] == 7777.0


# --------------------------------------------------------------------------- #
# 三、重排的兑现：新尺子报上来，才滚回那一段
# --------------------------------------------------------------------------- #
def test_font_relayout_anchors_to_a_paragraph_and_waits(wired):
    """改字号：当场换算成段号挂单，**不许**立刻按旧高度滚一次。

    测点选在对话密集区（第 60 段）——换算晚一步在那里答第 70 段，当场见分晓；
    第 240 段是两套装尺的重合点，算晚了也恰好答对，时机错在那儿看不出来。
    """
    view, page, spy = wired
    saved = _saved_position(view, page, spy, MIXED_ANCHOR)
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})

    assert view._relocate_pending is not None
    assert view._relocate_pending[0] == MIXED_ANCHOR, (
        f"重排前 {saved:.1f}px 换算成的段号不是 {MIXED_ANCHOR}——"
        "那是拿旧像素按还没生效的新尺子算出来的混搭值")
    assert len(spy.calls) == calls, "新版心高还没报上来就滚，滚的是旧高度乘新段高"


def test_new_rendered_height_puts_the_same_paragraph_back(wired):
    """客户端报来字号 20 的版心高后：只滚一次，且落点确实是第 240 段段顶。

    落点还须对上浏览器现场量到的段顶（41730px，`.probe/out_shape_d20.txt`），容差
    是**渲染后**的这一段高（257px）——这一条把换算钉在真机上，而不是钉在模型自洽上。
    """
    view, page, spy = wired
    old_pixel = _saved_position(view, page, spy, ANCHOR)
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})
    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)

    assert len(spy.calls) == calls + 1, "回位应当恰好滚一次"
    landed = spy.calls[-1]["offset"]
    assert view.paragraph_at_pixel(landed) == ANCHOR, (
        f"重排后落在 {landed:.1f}px，反查第 {view.paragraph_at_pixel(landed)} 段")
    tops = view._paragraph_tops()
    step = tops[ANCHOR] - tops[ANCHOR - 1]
    truth = PREFIX_TOP_20[ANCHOR]
    assert abs(landed - truth) <= step, (
        f"落点 {landed:.0f}px 与浏览器实测段顶 {truth:.0f}px 差过这一段的高 "
        f"({step:.0f}px)，回位到的不是那一段")

    # 反空转：不补偿（= 改前）就是让像素停在重排前那个数，本测点上差 52 段。
    assert abs(view.paragraph_at_pixel(old_pixel) - ANCHOR) > 10, (
        "旧像素在新尺子下几乎还是那一段，这篇文档换字号换得太轻，测不出补偿")


def test_relayout_that_keeps_the_height_does_not_scroll(wired):
    """重排完高度一模一样 = 那段本来就没动，多滚一次反倒把位置写歪。"""
    view, page, spy = wired
    _saved_position(view, page, spy, ANCHOR)
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})
    view._on_content_size(SimpleNamespace(width=HOST_16[0], height=HOST_16[1]))
    _drain(page)
    assert len(spy.calls) == calls, "版心高没变还滚一次"

    # 挂单留着：真涨高的那次仍然要兑现。
    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)
    assert len(spy.calls) == calls + 1
    assert view.paragraph_at_pixel(spy.calls[-1]["offset"]) == ANCHOR


def test_non_reflow_preference_leaves_the_viewport_alone(wired):
    """背景/状态栏/划词开关不重排：不挂单、不滚（逐条查，别让后一条抹掉前一条）。"""
    view, page, spy = wired
    _saved_position(view, page, spy, ANCHOR)
    calls = len(spy.calls)

    for key, value in (("background", "night"), ("show_status", False),
                       ("text_selection", False)):
        view.apply_preferences({key: value})
        assert view._relocate_pending is None, f"{key} 不改版面，不该挂回位的单"

    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)
    assert len(spy.calls) == calls


def test_relayout_without_a_measured_size_forges_no_anchor(wired):
    """还没量到版心高就改字号（刚开书）：换算给 None，静默不补偿、不抛。"""
    view, page, spy = wired
    view._content_size = (0.0, 0.0)
    view._last_scroll_pixels = 12345.0
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})
    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)

    assert view._relocate_pending is None
    assert len(spy.calls) == calls


def test_pending_anchor_dropped_when_a_chapter_lands(wired):
    """等尺寸这几百毫秒里自动续读接了下一章：文档变了，回位作废。"""
    view, page, spy = wired
    _saved_position(view, page, spy, ANCHOR)
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})
    view.render_plan(view.content_plan(ChapterContent(
        title="下一章", blocks=[ContentBlock("text", "追加的一整段" * 40)])),
        append=True, chapter_name="第1章")
    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)

    assert view._relocate_pending is None
    assert len(spy.calls) == calls, "用户刚读到新章尾，却被滚回原来那一段"


def test_second_relayout_before_the_echo_keeps_the_paragraph_index(wired):
    """连改两次字号、中间客户端还没报：接着用第一次的段号，不混搭两套装尺。"""
    view, page, spy = wired
    _saved_position(view, page, spy, MIXED_ANCHOR)
    calls = len(spy.calls)

    view.apply_preferences({"font_size": 20})
    view.apply_preferences({"font_size": 24})
    assert view._relocate_pending[0] == MIXED_ANCHOR, (
        f"第二次重排把锚换成了 {view._relocate_pending[0]}——那是拿 16 号像素"
        "按 20 号尺子算出来的混搭值")

    view._on_content_size(SimpleNamespace(width=HOST_20[0], height=HOST_20[1]))
    _drain(page)
    assert len(spy.calls) == calls + 1
    assert view.paragraph_at_pixel(spy.calls[-1]["offset"]) == MIXED_ANCHOR


def test_new_document_clears_the_pixel_memory(wired):
    """整篇换章：上一张文档的像素与挂单一起作废，否则下一次重排按旧书回位。"""
    view, page, spy = wired
    _saved_position(view, page, spy, ANCHOR)
    view.apply_preferences({"font_size": 20})
    assert view._relocate_pending is not None

    view.render_plan(view.content_plan(ChapterContent(
        title="另一本", blocks=[ContentBlock("text", "重起一章" * 30)])),
        append=False, chapter_name="")

    assert view._last_scroll_pixels == 0.0
    assert view._relocate_pending is None
