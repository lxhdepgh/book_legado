"""在途协程醒来发现页面已经走了：一类"切页即崩"的守卫（设计 §7.4 attach/detach 对称）。

控制器里每一条 `await` 都可能被用户切页打断：``detach()`` 把 ``_view`` 置 None，
而在途协程回来照旧 ``self._view.xxx`` —— 于是一次普通的"读一半去看书源页"就变成
AttributeError。第八轮补齐的这几处：阅读器偏好回填 / 书签面板 / 书内搜索、漫画阅读器
整条线（原本一处守卫都没有）、字体与二维码的文件选择器回执。

本文件的替身**在 await 回来之前**把视图摘掉，模拟真实时序而不是事后补状态；断言两件
事：协程**不抛**，且 detach 之后**一次控件都没碰**（守卫漏写就会红）。

全程离线：控件无窗口实例化，服务全替身，不联网（记忆 legado-live-scan-av-block）。
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from PIL import Image

from app.controllers.manga_reader_controller import MangaReaderController
from app.controllers.reader_controller import ReaderController
from app.controllers.tools.font_controller import FontController
from app.controllers.tools.qr_code_controller import QrCodeController
from app.services.reading.image_book_service import ImageBookService
from app.services.reading.reading_service import ReadingService
from app.views.reading.manga_reader_view import MangaReaderView
from app.views.reading.reader_view import ReaderView


class _FakeHub:
    """控制器只要 hub.get(类型)；bus=None 表示不发事件也不断言事件。"""

    def __init__(self, services=None):
        self.bus = None
        self._services = services or {}

    def get(self, stype):
        return self._services[stype]


class Value:
    """可 await 的固定返回值替身。"""

    def __init__(self, value):
        self._value = value

    async def __call__(self, *_args, **_kwargs):
        return self._value


class DetachThen:
    """第 ``before+1`` 次调用起先把视图摘掉，再回 ``after``（默认 None）。

    前 ``before`` 次正常回 ``value``：这样"在途切页"正好落在 await 之后的那一次
    控件写入上——守卫到底起没起作用，只有这么摆时序才测得出来。
    """

    def __init__(self, controller, value=None, *, before: int = 0, after=None):
        self._controller = controller
        self._value = value
        self._after = after
        self._before = before
        self.calls = 0

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls > self._before:
            self._controller.detach()
            return self._after
        return self._value


def _spy(owner: object, names: tuple[str, ...]) -> list[str]:
    """把若干控件方法换成记账桩，返回那份记录（谁被调用过就多出一条名字）。"""
    seen: list[str] = []

    def probe(name):
        def _fn(*_args, **_kwargs):
            seen.append(name)
        return _fn

    for name in names:
        setattr(owner, name, probe(name))
    return seen


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (160, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 阅读器：偏好回填 / 书签面板 / 书内搜索
# --------------------------------------------------------------------------- #
def _reader_view():
    """只建视图不 attach：attach 会把 _load_preferences 派发到循环上，与本文件无关。"""
    view = ReaderView()
    reading = SimpleNamespace(stop_prefetch=lambda: None,
                              get_reader_preference=Value(None),
                              save_reader_preference=Value(None))
    controller = ReaderController(_FakeHub({ReadingService: reading}))
    controller._view = view
    controller._reading = reading
    controller._bookmarks = SimpleNamespace(list_bookmarks=Value([]))
    controller._content_search = SimpleNamespace(
        search_ensured=Value({"hits": [], "written": 0, "indexedChapters": 0,
                              "ftsAvailable": True}))
    controller._tts = None
    controller._media = None
    controller._book_url = "http://127.0.0.1:1/book/1"
    return view, controller, reading


def test_layout_preferences_are_not_written_back_after_the_page_left():
    view, controller, reading = _reader_view()
    keys = tuple(view.PREFERENCE_KEYS)
    assert len(keys) >= 2, "偏好键不足两项，摆不出'读到一半才切页'的时序"
    applied = _spy(view, ("apply_preferences",))
    # 第一项正常回值（prefs 非空才会走到那次控件写入），第二项起页面已经走了
    reading.get_reader_preference = DetachThen(
        controller, keys[0], before=1, after=22)
    asyncio.run(controller._load_preferences())
    assert applied == [], "detach 之后还把排版写回了控件"
    assert controller._view is None, "替身没真把视图摘掉，本例等于没测"


def test_bookmark_panel_does_not_render_after_detach():
    view, controller, _ = _reader_view()
    drawn = _spy(view.bookmark_panel, ("render_bookmarks",))
    controller._bookmarks.list_bookmarks = DetachThen(controller, after=[
        SimpleNamespace(title="一处", chapter_index=0)])
    asyncio.run(controller._refresh_bookmarks())
    assert drawn == [], "detach 之后还往书签面板画条目"


def test_content_search_does_not_render_hits_after_detach():
    """全书扫一遍要几秒，中途切页是常规操作不是意外。"""
    view, controller, _ = _reader_view()
    drawn = _spy(view.content_search_bar, ("render_results",))
    controller._content_search.search_ensured = DetachThen(controller, after={
        "hits": [{"chapter_index": 0, "para_index": 3, "text": "命中"}],
        "written": 0, "indexedChapters": 1, "ftsAvailable": True})
    asyncio.run(controller._run_content_search("关键词"))
    assert drawn == [], "detach 之后还往搜索条画命中"


# --------------------------------------------------------------------------- #
# 漫画阅读器：这条线原本一处守卫都没有
# --------------------------------------------------------------------------- #
def _manga_rig():
    reading = SimpleNamespace(
        open_book=Value(SimpleNamespace(book_url="b", chapters=[], position=None)),
        current_source=None, current_session=SimpleNamespace(chapters=[]),
        save_progress=Value(None))
    images = SimpleNamespace(chapter_images=Value([]),
                             fetch_page_image=Value(_png()),
                             current_source=None)
    controller = MangaReaderController(
        _FakeHub({ReadingService: reading, ImageBookService: images}))
    view = MangaReaderView()
    controller.attach(view)
    return view, controller, reading, images


def test_manga_open_book_stops_before_it_touches_the_view():
    view, controller, reading, _ = _manga_rig()
    touched = _spy(view, ("set_title", "render_chapters"))
    reading.open_book = DetachThen(
        controller, after=SimpleNamespace(book_url="b", chapters=[], position=None))
    asyncio.run(controller.open_book(SimpleNamespace(), SimpleNamespace(name="漫画")))
    assert touched == [], "detach 之后还往漫画页写标题/目录"


def test_manga_open_chapter_keeps_state_but_skips_the_painting():
    """图链解析要联网：会话状态可以留（翻回来还在这一章），控件一下都不能碰。"""
    view, controller, _, images = _manga_rig()
    controller._chapters = [{"index": 0, "name": "第 1 话", "url": "c1",
                             "is_volume": False}]
    controller._book = SimpleNamespace(name="漫画")
    touched = _spy(view, ("set_pages", "set_title", "set_progress",
                          "set_nav_enabled", "set_cache_hint"))
    urls = ["http://127.0.0.1:1/1.png", "http://127.0.0.1:1/2.png"]
    images.chapter_images = DetachThen(controller, after=urls)
    asyncio.run(controller.open_chapter(0))
    assert touched == [], "detach 之后还往漫画页上图"
    assert controller._page_urls == urls, "守卫把该留的会话状态也一起丢了"


def test_manga_page_loop_stops_half_way_through_the_chapter():
    """分块补图/滤镜重算是逐页 await 的长任务：中途切页不能再往回画。"""
    view, controller, _, images = _manga_rig()
    urls = [f"http://127.0.0.1:1/{i}.png" for i in range(8)]
    controller._page_urls = urls
    controller._filter_mode = "gray"
    view.set_pages(urls)
    assert view.rendered_count > 0, "预置没建出已渲染页，本例测不到逐页补图那条循环"
    swapped = _spy(view, ("set_page_bytes", "set_cache_hint"))
    images.fetch_page_image = DetachThen(controller, after=_png())
    asyncio.run(controller._rerender_filtered())
    assert swapped == [], "detach 之后还把预渲染的字节换上去了"


# --------------------------------------------------------------------------- #
# 工具域：等用户选文件的路上切页
# --------------------------------------------------------------------------- #
def _tool_rig(cls, method_names, svc):
    """工具页只用了几个 set_* 方法，视图替身即可（真视图另有用例覆盖）。"""
    seen: list[str] = []
    view = SimpleNamespace(**{name: (lambda _msg, _n=name: seen.append(_n))
                              for name in method_names})
    controller = cls(_FakeHub())
    controller._svc = svc
    controller._view = view
    # 这两页 detach 留下的效果就是 _view=None（没有事件订阅要退）
    controller.detach = lambda: setattr(controller, "_view", None)
    return controller, seen


def test_font_add_picked_reports_nothing_once_the_page_is_gone():
    controller, seen = _tool_rig(FontController, ("set_status",), SimpleNamespace(
        add_font_file=Value(True)))
    picked = DetachThen(controller, after=[
        SimpleNamespace(path="http://127.0.0.1:1/a.ttf")])
    asyncio.run(controller._add_picked_fonts(picked()))
    assert seen == [], "detach 之后还往字体页写状态行"


def test_font_add_picked_says_why_when_the_picker_fails():
    """失败回执要看得见——只有切页才丢，其它情况不能一起吞掉。"""
    controller, seen = _tool_rig(FontController, ("set_status",), SimpleNamespace(
        add_font_file=Value(True)))

    async def boom():
        raise OSError("选择器被取消")

    controller.load_fonts = lambda: None
    asyncio.run(controller._add_picked_fonts(boom()))
    assert seen == ["set_status"], "读取所选文件失败的回执也被丢了"


def test_qr_decode_reports_nothing_once_the_page_is_gone():
    controller, seen = _tool_rig(QrCodeController, ("set_result",), SimpleNamespace(
        decode_image=Value("http://127.0.0.1:1")))
    picked = DetachThen(controller, after=[
        SimpleNamespace(path="http://127.0.0.1:1/x.png", bytes=b"")])
    asyncio.run(controller._decode_picked(picked()))
    assert seen == [], "detach 之后还往二维码页写结果"
