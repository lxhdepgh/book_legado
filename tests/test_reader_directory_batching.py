"""目录分批渲染 + 目录内过滤：网文源几千章时目录页不该把界面冻住。

重构时目录退化成"一次为每一章建一个 ``ListTile``"（旧实装有 ``DIRECTORY_BATCH_SIZE``
+ 滚动追加 + 关键字过滤），几千章的书点开目录就是一次可感知的卡死；搜索框
``_directory_query`` 更是渲染出来了却没有任何回调（点了打字都没反应）。本文件钉住
补回来的那几条：

1. 首批只建 ``DIRECTORY_BATCH_SIZE`` 行，状态行说清"已加载多少"；
2. 滚到快到底才补下一批（离底还远的滚动不许白建控件）；
3. 关键字过滤只影响看得见的那份，清空即原样回来；
4. 正/倒序切换**就地生效**（以前只翻标志位，等于点了没反应）；
5. 卷标题仍按分组标题渲染，点击回调拿到的是它自己那一章；
6. 打开目录**定位到在读的那一章**（分批恢复后一开仍停在列表顶上，读到第 2000
   章的人得自己滚过去），并把那一行标出来。定位要一批一批下发：一口气补到第
   2000 行正是本文件第 1 条要避免的那次卡死。

全程离线：真 Flet 控件无窗口实例化，不联网（记忆 flet-0-86-api-surface）。
"""
from __future__ import annotations

import asyncio
import gc
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft
import pytest

from app.views.reading.reader_view import ReaderView


def _chapters(total: int) -> list[dict]:
    out = [{"index": 0, "name": "序章", "url": "c0", "is_volume": False}]
    for i in range(1, total + 1):
        out.append({"index": i, "name": f"第{i}章 内容", "url": f"c{i}",
                    "is_volume": False})
    return out


def _tiles(view: ReaderView) -> list:
    return [c for c in view._directory_list.controls
            if type(c).__name__ == "ListTile"]


def _tile_titles(view: ReaderView) -> list[str]:
    return [str(t.title.value) for t in _tiles(view)]


def _scroll(view: ReaderView, pixels: float, extent: float) -> None:
    view._on_directory_scroll(SimpleNamespace(
        pixels=pixels, max_scroll_extent=extent))


def _type(view: ReaderView, keyword: str) -> None:
    """在目录搜索框里打字：走控件的 on_change，不是直接调私有方法。"""
    view._directory_query.value = keyword
    view._directory_query.on_change(SimpleNamespace(control=view._directory_query))


def _status(view: ReaderView) -> str:
    return str(view._directory_status.value or "")


# --------------------------------------------------------------------------- #
# 分批
# --------------------------------------------------------------------------- #
def test_a_thousand_chapters_render_one_batch_at_a_time():
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    assert len(view._directory_list.controls) == view.DIRECTORY_BATCH_SIZE, \
        "首批建了整份目录：几千章的书点开目录就是那次卡死"
    assert "已加载" in _status(view) and "1001" in _status(view), _status(view)


def test_scrolling_near_the_bottom_appends_the_next_batch():
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    extent = 60000.0
    _scroll(view, extent - 40, extent)
    assert len(view._directory_list.controls) == view.DIRECTORY_BATCH_SIZE * 2
    # 顺序不能乱：第二批接在第一批后面，而不是把已看过的重挂一遍
    titles = _tile_titles(view)
    assert titles[0] == "序章" and titles[1] == "第1章 内容"
    assert titles[view.DIRECTORY_BATCH_SIZE] == "第50章 内容", titles[48:52]


def test_scrolling_far_from_the_bottom_does_not_build_anything():
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    before = len(view._directory_list.controls)
    _scroll(view, 3000.0, 60000.0)
    assert len(view._directory_list.controls) == before, "离底还远就白建了一批控件"


def test_reading_to_the_end_of_the_list_renders_every_chapter():
    view = ReaderView()
    view.render_chapters(_chapters(120))
    extent = 60000.0
    for _ in range(10):
        _scroll(view, extent - 10, extent)
    assert len(view._directory_list.controls) == 121, "追加到末尾漏掉了章节"
    assert _status(view) == "共 121 章", _status(view)
    _scroll(view, extent - 10, extent)
    assert len(view._directory_list.controls) == 121, "全挂完了还在往列表里加"


def test_volume_titles_stay_group_headers():
    chapters = _chapters(3)
    chapters.insert(1, {"index": 1, "name": "第一卷", "url": "", "is_volume": True})
    view = ReaderView()
    view.render_chapters(chapters)
    kinds = [type(c).__name__ for c in view._directory_list.controls]
    assert kinds.count("ListTile") == 4 and kinds.count("Container") == 1, kinds
    # 卷标题钉了行高才能按行号算落点，但它不能变成可点的一行（点了没章可开）
    header = view._directory_list.controls[kinds.index("Container")]
    assert header.content.value == "第一卷", header


def test_clicking_a_tile_opens_its_own_chapter():
    """分批后闭包要各自绑到自己那一章：绑错了就是"点第 5 章开第 1 章"。"""
    opened: list[dict] = []
    view = ReaderView()
    view.on_open_chapter = opened.append
    view.render_chapters(_chapters(60))
    _scroll(view, 60000.0 - 10, 60000.0)    # 补第二批
    tiles = _tiles(view)
    assert len(tiles) == 61, "第二批没补齐剩下的章节"
    tiles[view.DIRECTORY_BATCH_SIZE].on_click(None)
    assert opened == [{"index": 50, "name": "第50章 内容", "url": "c50",
                       "is_volume": False}], opened


# --------------------------------------------------------------------------- #
# 过滤 / 排序
# --------------------------------------------------------------------------- #
def test_directory_search_box_filters_the_visible_list():
    view = ReaderView()
    view.render_chapters(_chapters(300))
    _type(view, "第7章")
    assert _tile_titles(view) == ["第7章 内容"], _tile_titles(view)
    assert "共 1 章" in _status(view), _status(view)


def test_clearing_the_search_box_brings_the_whole_directory_back():
    view = ReaderView()
    view.render_chapters(_chapters(300))
    _type(view, "第7章")
    _type(view, "")
    assert len(view._directory_list.controls) == view.DIRECTORY_BATCH_SIZE
    assert "已加载" in _status(view) and "301" in _status(view), _status(view)


def test_a_keyword_that_matches_nothing_says_so():
    view = ReaderView()
    view.render_chapters(_chapters(10))
    _type(view, "不存在的章节名")
    assert _tiles(view) == []
    assert _status(view) == "没有匹配的章节", _status(view)


def test_reversing_the_order_takes_effect_at_once():
    """旧代码只翻标志位：不点"倒序"看不出来，等于这颗按钮是坏的。"""
    view = ReaderView()
    view.render_chapters(_chapters(300))
    view.toggle_directory_order()
    assert _tile_titles(view)[0] == "第300章 内容", _tile_titles(view)[:2]
    view.toggle_directory_order()
    assert _tile_titles(view)[0] == "序章"


def test_reverse_and_filter_combine():
    view = ReaderView()
    view.render_chapters(_chapters(20))
    _type(view, "章")
    view.toggle_directory_order()
    assert _tile_titles(view)[0] == "第20章 内容", _tile_titles(view)[:2]


# --------------------------------------------------------------------------- #
# 打开目录定位到当前章
# --------------------------------------------------------------------------- #
class _LocatePage:
    """记账用的假 page：跑协程 + 记每次下发。

    ``updates`` 是"分批下发"的证据：一口气建到第 400 行时它只有 1。
    """

    def __init__(self) -> None:
        self.tasks: list = []
        self.updates = 0

    def run_task(self, handler, *args, **kwargs):
        self.tasks.append(asyncio.ensure_future(handler(*args, **kwargs)))

    def update(self, control=None) -> None:
        self.updates += 1


@pytest.fixture
def scrolls(monkeypatch):
    """把 ``ListView.scroll_to`` 换成记账替身：真方法要会话，离线没有。"""
    calls: list[dict] = []

    async def fake(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(ft.ListView, "scroll_to", fake, raising=True)
    return calls


def _mounted(view: ReaderView, monkeypatch) -> _LocatePage:
    page = _LocatePage()
    monkeypatch.setattr(ReaderView, "page",
                        property(lambda self: page), raising=False)
    return page


def _locate(view: ReaderView, page: _LocatePage, url: str) -> None:
    async def main():
        view.focus_chapter(url)
        await asyncio.gather(*page.tasks)

    asyncio.run(main())


def _marked(view: ReaderView) -> list[str]:
    """被标成"在读"的那些行的章名（selected 是唯一锚点，别按颜色猜）。"""
    return [str(t.title.value) for t in _tiles(view) if t.selected]


def test_opening_the_directory_lands_on_the_chapter_being_read(monkeypatch, scrolls):
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    page = _mounted(view, monkeypatch)
    _locate(view, page, "c400")
    position = view._directory_position("c400")
    assert position == 400, position
    assert view._directory_rendered > position, "目标行还没建出来就滚不过去"
    assert view._directory_rendered <= position + view.DIRECTORY_BATCH_SIZE + 1, \
        "补批次补过了头：多挂的那几十行没人看"
    assert scrolls == [{"offset": position * view._directory_pitch,
                        "duration": 0}], scrolls
    assert page.updates >= 8, f"一批一次下发没做到（updates={page.updates}）"


def test_a_chapter_that_is_already_rendered_only_scrolls(monkeypatch, scrolls):
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    page = _mounted(view, monkeypatch)
    before = len(view._directory_list.controls)
    _locate(view, page, "c10")
    assert len(view._directory_list.controls) == before, "首批内还白补了一批"
    assert scrolls == [{"offset": 10 * view._directory_pitch, "duration": 0}], scrolls


def test_the_row_being_read_is_marked_and_the_old_mark_moves():
    view = ReaderView()
    view.render_chapters(_chapters(60))
    view.focus_chapter("c3")
    assert _marked(view) == ["第3章 内容"], _marked(view)
    view.focus_chapter("c5")
    assert _marked(view) == ["第5章 内容"], "旧标记没摘：目录里同时亮着两章"
    view.focus_chapter("")
    assert _marked(view) == []


def test_a_chapter_filtered_out_of_the_directory_leaves_the_scroll_alone(
        monkeypatch, scrolls):
    """搜索框里留着关键字时当前章常常不在可见表里：那时什么都不该动。"""
    view = ReaderView()
    view.render_chapters(_chapters(300))
    _type(view, "第7章")
    page = _mounted(view, monkeypatch)
    _locate(view, page, "c200")
    assert scrolls == [], scrolls
    assert view._directory_rendered == 1
    assert _marked(view) == []


def test_an_unmounted_view_does_not_build_the_whole_directory():
    """没上页面时协程就地关掉：既不许建几千行，也不许留 never-awaited 警告。"""
    view = ReaderView()
    view.render_chapters(_chapters(1000))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        view.focus_chapter("c400")
        gc.collect()
    assert view._directory_rendered == view.DIRECTORY_BATCH_SIZE, \
        "定位协程在没有页面时把整份目录建完了"
    assert not [w for w in caught if "never awaited" in str(w.message)], \
        [str(w.message) for w in caught]
    assert _marked(view) == [], "目标行还没建出来，标记不该落在别的行上"


# --------------------------------------------------------------------------- #
# 控制器侧：两个开目录的入口都得带上"我在读哪一章"
# --------------------------------------------------------------------------- #
def _controller_rig(total: int = 60, index: int = 3):
    from app.controllers.reader_controller import ReaderController
    from app.services.reading.bookmark_service import BookmarkService
    from app.services.reading.content_search import ContentSearchService
    from app.services.reading.reading_service import ReadingService

    reading = SimpleNamespace(
        load_content=AsyncMock(return_value=SimpleNamespace(blocks=[])),
        save_progress=AsyncMock(),
        get_reader_preference=AsyncMock(side_effect=lambda key, default=None: default),
        save_reader_preference=AsyncMock(),
        prefetch_chapters=AsyncMock(),
        stop_prefetch=lambda: None,
        current_session=SimpleNamespace(chapters=[], viewports={}, position=None))
    hub = SimpleNamespace(bus=None, get=lambda stype: {
        ReadingService: reading,
        BookmarkService: SimpleNamespace(
            list_bookmarks=AsyncMock(return_value=[])),
        ContentSearchService: SimpleNamespace(
            rebuild_index=AsyncMock(return_value=0)),
    }[stype])
    view = ReaderView()
    controller = ReaderController(hub)
    controller.attach(view)
    controller._set_chapters(_chapters(total))
    controller._chapter_index = index
    return SimpleNamespace(view=view, controller=controller, reading=reading)


def test_opening_the_directory_shows_it_and_marks_the_current_chapter():
    rig = _controller_rig()
    try:
        rig.controller.open_directory()
        assert rig.view._detail_page.visible is True
        assert rig.view._directory_current_url == "c3", \
            "控制器没把在读的那一章交给目录"
    finally:
        rig.controller.detach()


def test_toolbar_button_and_shortcut_both_locate_the_current_chapter():
    """工具栏与 f 快捷键是两个入口：漏接一个，另一个也只是"翻出列表"而已。"""
    rig = _controller_rig(total=200, index=120)
    try:
        button = next(btn for btn in rig.view._toolbar.content.controls
                      if getattr(btn, "tooltip", "") == "目录")
        button.on_click(None)
        assert rig.view._detail_page.visible is True
        assert rig.view._directory_current_url == "c120"
        # 换一章再从快捷键进来：还是死绑 show_directory(True) 的话标记停在旧章
        rig.controller._chapter_index = 9
        rig.view.show_directory(False)
        rig.controller._on_key(SimpleNamespace(key="F"))
        assert rig.view._detail_page.visible is True
        assert rig.view._directory_current_url == "c9", \
            "快捷键那条路没接上定位（第 120 章的标记还挂着）"
    finally:
        rig.controller.detach()
