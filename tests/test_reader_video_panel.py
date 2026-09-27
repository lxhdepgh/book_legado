"""阅读器视频面板的接线（设计 §1.4：把只有服务面的播放会话接出界面）。

``MediaPlayService`` 的播放列表 / 状态机 / 位置落库早就齐了（见
``test_media_play_m4.py``），缺的是界面这一侧：正文里的视频块由视图自建
``ftv.Video`` 播放，绕过服务，于是没有播放列表、没有"接着上次看"、切页停不下来。
本文件钉的是**这条链接上了没有**：

- 正文视频块 → 播放列表条目（跨章累积、按 url 去重）；
- 点「在本页播放」→ ``open_book`` + ``play_chapter``，恢复位置只认上次那一条；
- 面板的每个按钮都落到服务的某个动作（暂停/继续/重播/上下条/停止/列表选条）；
- 服务态经 ``MediaPlayStateEvent`` 镜像回面板，且**只认当前这本书**；
- 换书、切页、收起面板都会叫停播放器；
- 后端是视图层造的 ``FletVideoBackend``，控件操作编组回界面循环（红线）。

全程离线：真 ``ReaderView`` + 真 ``VideoPlayBar``（flet_video 控件只被构造、不被
调用，播放动作由假服务接手），服务全替身，不联网（记忆 legado-live-scan-av-block、
offline-loopback-probe）。真实播放器上屏那条留给浏览器探针与遗留清单。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft

from app.core.events import MediaPlayStateEvent
from app.controllers.reader_controller import ReaderController
from app.services.content.media_play import FletVideoBackend, MediaPlayService
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.views.reading.reader_view import ReaderView
from app.views.reading.video_play_bar import VideoPlayBar


class _FakeBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, event):
        return []


class _FakeHub:
    """控制器只要 hub.get(类型)；没登记的服务 _safe_service 认 None。"""

    def __init__(self, services=None):
        self.bus = _FakeBus()
        self._services = services or {}

    def get(self, stype):
        return self._services[stype]


class _FakeMedia:
    """播放会话替身：记账每个动作，不碰控件也不落库。"""

    def __init__(self, restored: tuple[int, float] = (0, 0.0)) -> None:
        self.calls: list = []
        self.restored = restored
        self.backend = None

    def set_backend(self, backend) -> None:
        self.backend = backend
        self.calls.append(("set_backend",))

    async def open_book(self, book_url, chapters):
        self.calls.append(("open_book", str(book_url),
                           [dict(item) for item in chapters]))
        return self.restored

    async def play_chapter(self, index, *, resume=False):
        self.calls.append(("play_chapter", int(index), bool(resume)))

    async def pause(self):
        self.calls.append(("pause",))

    async def resume_playback(self):
        self.calls.append(("resume_playback",))

    async def stop_playback(self):
        self.calls.append(("stop_playback",))

    async def mark_ended(self):
        self.calls.append(("mark_ended",))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _video_block(url: str, title: str = "", headers=None):
    return SimpleNamespace(kind="video", value=url, title=title,
                           headers=dict(headers or {}))


def _text_block(value: str):
    return SimpleNamespace(kind="text", value=value, title="", headers={})


def _document(*blocks):
    return SimpleNamespace(blocks=list(blocks), image_style="", title="")


def _rig(blocks=(), *, chapters=None, restored=(0, 0.0), with_media=True):
    """真视图 + 假服务：attach 之后正文已经挂出来一章。"""
    chapters = chapters if chapters is not None else [
        {"index": 0, "name": "第 1 章", "url": "https://x/1", "is_volume": False},
        {"index": 1, "name": "第 2 章", "url": "https://x/2", "is_volume": False},
    ]
    reading = SimpleNamespace(
        get_reader_preference=AsyncMock(return_value=None),
        save_reader_preference=AsyncMock(),
        load_content=AsyncMock(return_value=_document(*blocks)),
        stop_prefetch=lambda: None,
        prefetch_chapters=AsyncMock(),
        current_session=SimpleNamespace(chapters=[]),
    )
    services = {
        ReadingService: reading,
        BookmarkService: SimpleNamespace(
            list_bookmarks=AsyncMock(return_value=[])),
        ContentSearchService: SimpleNamespace(search=AsyncMock(return_value=[])),
    }
    media = _FakeMedia(restored=restored) if with_media else None
    if media is not None:
        services[MediaPlayService] = media
    view = ReaderView()
    controller = ReaderController(_FakeHub(services))
    controller.attach(view)
    controller._book_url = "http://127.0.0.1:1/book/1"
    controller._set_chapters(chapters)
    asyncio.run(controller._open_index(0))
    return view, controller, media


def _settled(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _emit(controller, state: str, chapter: int = 0, book_url=None) -> None:
    """把一条服务态事件喂给镜像（生产里由 EventBus 送，这里直调同一处理器）。"""
    asyncio.run(controller._on_media_play_state(MediaPlayStateEvent(
        state=state, book_url=controller._book_url if book_url is None else book_url,
        chapter=chapter)))


# --------------------------------------------------------------------------- #
# 1. 正文视频块 → 播放列表条目
# --------------------------------------------------------------------------- #
def test_video_blocks_become_playlist_entries():
    view, controller, _media = _rig([
        _text_block("正文一段"),
        _video_block("https://v/1.mp4", "第一集", {"Referer": "https://x"}),
    ])
    assert controller._video_items == [{
        "url": "https://v/1.mp4", "title": "第一集",
        "headers": {"Referer": "https://x"},
        "chapter_url": "https://x/1"}]
    # 正文里的条目是「在本页播放」，不是又一个播放器（一个播放面原则）。
    assert getattr(view.video_play, "player_control", None) is None


def test_same_url_across_chapters_is_not_listed_twice():
    """连读追加时同一集不该出现两遍（书源常把上一条视频的尾巴带进下一章）。"""
    _view, controller, _media = _rig([_video_block("https://v/1.mp4", "第一集")])
    controller._collect_videos(
        [("video", _video_block("https://v/1.mp4"), "")], {"url": "https://x/2"})
    controller._collect_videos(
        [("video", _video_block("https://v/2.mp4"), "")],
        {"url": "https://x/2", "name": "第 2 章"})
    assert [i["url"] for i in controller._video_items] == [
        "https://v/1.mp4", "https://v/2.mp4"]
    # 视频块没带小标题时用章名兜底：播放列表里一行"第 2 章"比一行"视频"认得出
    assert controller._video_items[1]["title"] == "第 2 章"


# --------------------------------------------------------------------------- #
# 2. 点「在本页播放」：面板亮起来，会话由服务持有
# --------------------------------------------------------------------------- #
def test_clicking_an_entry_opens_a_playback_session():
    view, controller, media = _rig([_video_block("https://v/1.mp4", "第一集")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    assert media.calls[:3] == [
        ("set_backend",),
        ("open_book", controller._book_url, [dict(controller._video_items[0])]),
        ("play_chapter", 0, False)]
    assert view.video_play.visible is True
    assert view.video_play.current == 0


def test_injected_backend_is_the_view_made_flet_one_on_the_ui_loop():
    """后端只能由视图层装（flet_video 类型在服务层造不出来），且控件操作要回界面循环。

    ``ui_marshal`` 漏了的话，播放动作就跑在 EngineHub 的 loop 上——表现为偶发
    "点了没反应"，测试里看不出，所以直接钉装配。
    """
    view, controller, media = _rig([_video_block("https://v/1.mp4")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: media.backend is not None)
    backend = media.backend
    assert isinstance(backend, FletVideoBackend)
    assert backend._control is view.video_play.player_control
    marshal = backend._ui_marshal
    assert marshal.__self__ is controller
    assert marshal.__func__.__name__ == "ui_await"
    # 两个工厂来自面板（VideoMedia / Duration 都是 flet 侧的类型）
    assert backend._media_factory == view.video_play.make_video_media
    assert backend._duration_factory == view.video_play.video_duration


def test_resume_only_for_the_entry_that_was_last_watch():
    """"接着上次看"只对上次那一条生效：点别的集必须从片头放。"""
    view, controller, media = _rig(
        [_video_block("https://v/1.mp4", "第一集"),
         _video_block("https://v/2.mp4", "第二集")], restored=(0, 300.0))
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: len(media.calls) >= 3)
    assert media.calls[2] == ("play_chapter", 0, True)

    media.calls.clear()
    view.on_play_video("https://v/2.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    play = [c for c in media.calls if c[0] == "play_chapter"][0]
    assert play == ("play_chapter", 1, False)


def test_without_the_service_the_panel_says_so():
    """没有 MediaPlayService（精简装配）：面板亮起来并把原因写在条上，不崩。"""
    view, controller, _media = _rig([_video_block("https://v/1.mp4")],
                                   with_media=False)
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: view.video_play.state == "error")
    assert "未就绪" in str(view.video_play._state_label.value)


def test_a_second_click_does_not_stack_two_sessions():
    """连点同一条：两次会话都跑，但播放面只有一个（面板不会被换成两份控件）。"""
    view, controller, media = _rig([_video_block("https://v/1.mp4")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    control = view.video_play.player_control
    media.calls.clear()
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    assert view.video_play.player_control is control


# --------------------------------------------------------------------------- #
# 3. 面板按钮 → 服务动作
# --------------------------------------------------------------------------- #
def test_toggle_routes_pause_resume_and_replay():
    view, controller, media = _rig([_video_block("https://v/1.mp4", "第一集")])
    bar = view.video_play
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())

    _emit(controller, "playing")
    media.calls.clear()
    bar.on_toggle()
    assert _settled(lambda: media.calls == [("pause",)])

    _emit(controller, "paused")
    media.calls.clear()
    bar.on_toggle()
    assert _settled(lambda: media.calls == [("resume_playback",)])

    # 放完/出错之后再按是重播当前条：从片头放，不接着上次的秒数
    _emit(controller, "ended")
    media.calls.clear()
    bar.on_toggle()
    assert _settled(lambda: "play_chapter" in media.names())
    assert [c for c in media.calls if c[0] == "play_chapter"][0] == (
        "play_chapter", 0, False)


def test_prev_next_walk_the_playlist_and_stop_leaves_the_panel_up():
    view, controller, media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集"),
        _video_block("https://v/3.mp4", "第三集")])
    bar = view.video_play
    view.on_play_video("https://v/2.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing", chapter=1)
    assert bar.current == 1

    bar.on_next()
    assert _settled(lambda: any(c[0] == "play_chapter" and c[1] == 2
                                for c in media.calls))
    # 真服务在 play_chapter 收尾会发这条事件（见 test_media_play_m4 的事件断言），
    # 替身不发：这里补上镜像，"上一个"才知道从哪一条往回走。
    _emit(controller, "playing", chapter=2)
    media.calls.clear()
    bar.on_prev()
    assert _settled(lambda: any(c[0] == "play_chapter" and c[1] == 1
                                for c in media.calls))
    # 第一条没有"上一个"：按了不该越界
    media.calls.clear()
    _emit(controller, "playing", chapter=0)
    bar.on_prev()
    assert media.calls == []

    bar.on_stop()
    assert _settled(lambda: "stop_playback" in media.names())
    assert bar.visible is True, "停止只是停，面板还在（还要按播放回来）"


def test_close_hides_the_panel_and_stops_the_player():
    """画面收走了还在放，比不关更糟。"""
    view, controller, media = _rig([_video_block("https://v/1.mp4")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing")
    view.video_play.on_close()
    assert _settled(lambda: "stop_playback" in media.names())
    assert view.video_play.visible is False


def test_stopping_an_idle_panel_is_not_a_stop_call():
    """没在放就没有"停"：空转的动作会让面板多闪一次状态。"""
    _view, controller, media = _rig([_video_block("https://v/1.mp4")])
    controller.stop_video()
    assert media.calls == []


# --------------------------------------------------------------------------- #
# 4. 播放列表弹层
# --------------------------------------------------------------------------- #
def _view_nodes(control):
    """深度遍历控件树：面板"多挂了一份播放器"这种问题只有翻整棵树才照得见。"""
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from _view_nodes(child)
    for name in ("content", "leading", "title", "subtitle", "trailing"):
        child = getattr(control, name, None)
        if isinstance(child, ft.Control):
            yield from _view_nodes(child)


class _FakePage:
    """只认 show_dialog/pop_dialog 的页面替身（弹层归控制器管）。"""

    def __init__(self):
        self.shown: list = []
        self.popped = 0

    def show_dialog(self, dialog):
        self.shown.append(dialog)

    def pop_dialog(self):
        self.popped += 1

    def update(self):
        pass


def _fake_page(controller) -> _FakePage:
    """控制器拿 page 只为了 show/pop 弹层；视图的 ``page`` 是只读属性，改挂控制器。"""
    page = _FakePage()
    controller._page = lambda: page
    return page


def test_playlist_dialog_lists_entries_and_picks_one():
    view, controller, media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集")])
    page = _fake_page(controller)
    # 播放列表入口在面板里：先起播（面板亮起来并回填列表），再点开弹层
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    controller.open_video_playlist()
    assert len(page.shown) == 1
    dialog = page.shown[0]
    titles = [n.value for n in _view_nodes(dialog) if isinstance(n, ft.Text)]
    assert "第一集" in titles and "第二集" in titles

    media.calls.clear()
    controller._pick_video(1)
    assert page.popped == 1, "选完条目弹层要收掉，不能压在播放器上"
    assert controller._video_dialog is None
    assert _settled(lambda: any(c[0] == "play_chapter" and c[1] == 1
                                for c in media.calls))


def test_playlist_dialog_height_follows_the_number_of_entries():
    """弹层高度按条数收：两集的书也钉 320 高的话，下面一大片空白。

    界面探针截图里这看着像弹层坏了（真实用户也一样），条数多了才封顶起滚动。
    """
    def _height(n: int) -> float:
        bar = VideoPlayBar()
        bar.set_playlist([{"url": f"https://v/{i}.mp4", "title": f"第 {i} 集"}
                          for i in range(n)], 0)
        return bar.build_playlist_dialog(lambda index: None,
                                         lambda: None).content.height
    assert _height(2) < 320, "两条也占满 320 高"
    assert _height(30) == 320, "条数多了要封顶（交给滚动）"


def test_closing_the_playlist_twice_pops_once():
    """没有弹层还 pop 一次，会把别人的对话框顶掉（同阅读设置那条）。"""
    view, controller, _media = _rig([_video_block("https://v/1.mp4")])
    page = _fake_page(controller)
    controller.open_video_playlist()
    controller.close_video_playlist()
    controller.close_video_playlist()
    assert page.popped == 1


def test_pick_outside_the_playlist_is_ignored():
    _view, controller, media = _rig([_video_block("https://v/1.mp4")])
    controller._pick_video(7)
    controller._pick_video(None)
    assert media.calls == []


# --------------------------------------------------------------------------- #
# 5. 服务态镜像
# --------------------------------------------------------------------------- #
def test_state_events_of_another_book_are_ignored():
    """换书时上一本的迟到 idle 不能让面板来回跳。"""
    view, controller, _media = _rig([_video_block("https://v/1.mp4")])
    view.video_play.visible = True
    _emit(controller, "playing")
    _emit(controller, "idle", chapter=0, book_url="http://other/book")
    assert view.video_play.state == "playing"


def test_state_event_relabels_the_title_and_availability():
    view, controller, _media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集")])
    view.video_play.visible = True
    _emit(controller, "playing", chapter=1)
    bar = view.video_play
    assert bar.current == 1
    assert bar._title.value == "第二集"
    assert bar._prev_btn.disabled is False and bar._next_btn.disabled is True


# --------------------------------------------------------------------------- #
# 5b. 播放器自己上报：放完了连播、放坏了就地停下
# --------------------------------------------------------------------------- #
def test_a_finished_entry_starts_the_next_one():
    """Legado 的连播语义：视频书一章一集，放完自动接下一条（不等人守着点）。"""
    view, controller, media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing", chapter=0)

    media.calls.clear()
    view.video_play.on_ended()
    assert _settled(lambda: any(c[0] == "play_chapter" and c[1] == 1
                                for c in media.calls)), "放完没有接下一条"
    assert media.calls[0] == ("mark_ended",), "没先把 ended 态交给服务就换片"


def test_the_last_finished_entry_does_not_wrap_around():
    """最后一条放完就停：绕回第一条会把人困在死循环里。

    两条断言分开等：`== [("mark_ended",)]` 这类"没有谁"的判据在 mark_ended 落账
    那一刻就成立，绕回的那发是**之后**才发生的，所以要等够一个窗口再收。
    """
    view, controller, media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集")])
    view.on_play_video("https://v/2.mp4")
    assert _settled(lambda: any(c[0] == "play_chapter" and c[1] == 1
                                for c in media.calls))
    _emit(controller, "playing", chapter=1)

    media.calls.clear()
    view.video_play.on_ended()
    assert _settled(lambda: ("mark_ended",) in media.calls)
    time.sleep(0.15)
    assert [c for c in media.calls if c[0] == "play_chapter"] == [], \
        f"最后一条放完还去点播条目：{media.calls}"


def test_a_late_completion_after_stop_is_ignored():
    """按了停止/收起面板之后客户端还能补一发 on_complete：不能把它点亮回来。"""
    view, controller, media = _rig([
        _video_block("https://v/1.mp4"), _video_block("https://v/2.mp4")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing", chapter=0)
    controller.stop_video()
    assert _settled(lambda: "stop_playback" in media.names())

    media.calls.clear()
    view.video_play.on_ended()
    time.sleep(0.15)
    assert media.calls == [], f"已经停了的会话被迟到的完成事件重启：{media.calls}"


def test_a_broken_entry_reports_error_and_does_not_advance():
    """书源里的视频常常整本都坏：报错只写在这一条上，不是一路连播错下去。"""
    view, controller, media = _rig([
        _video_block("https://v/1.mp4", "第一集"),
        _video_block("https://v/2.mp4", "第二集")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing", chapter=0)

    media.calls.clear()
    view.video_play.on_failed("403 Forbidden")
    assert view.video_play.state == "error"
    assert "403" in str(view.video_play._state_label.value)
    time.sleep(0.15)
    assert media.calls == [], "报错不该触发任何播放动作"


def test_player_events_reach_slots_bound_after_the_player_was_built():
    """槽是**事件到达时**取的：面板先建播放器、控制器后绑槽，照样要响。

    闭包里写 `callback=self.on_ended` 会把建播放器那一刻的 None 钉死，
    表现就是"放完了什么都不发生"，而且单测里换成手动调槽永远测不出。
    """
    fired: list = []
    bar = VideoPlayBar()
    assert bar.ensure_player() is True
    bar.on_ended = lambda: fired.append("ended")
    bar.on_failed = lambda detail: fired.append(f"failed:{detail}")
    bar.player_control.on_complete(None)
    bar.player_control.on_error(SimpleNamespace(data="boom"))
    assert fired == ["ended", "failed:boom"]


# --------------------------------------------------------------------------- #
# 6. 换书 / 切页 / 与朗读互斥
# --------------------------------------------------------------------------- #
def test_new_book_clears_the_previous_playlist():
    view, controller, media = _rig([_video_block("https://v/1.mp4", "第一集")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing")

    controller._set_chapters([
        {"index": 0, "name": "另一本", "url": "https://y/1", "is_volume": False}])
    assert controller._video_items == []
    assert view.video_play.visible is False
    assert _settled(lambda: "stop_playback" in media.names())


def test_detach_stops_playback_without_touching_widgets(caplog):
    """切页即随视图拆走：播放器要停，但**不能再碰已经拆掉的控件**。"""
    view, controller, media = _rig([_video_block("https://v/1.mp4", "第一集")])
    view.on_play_video("https://v/1.mp4")
    assert _settled(lambda: "play_chapter" in media.names())
    _emit(controller, "playing")

    touched: list[str] = []
    bar = view.video_play
    for name in ("set_state", "set_playlist", "build_playlist_dialog",
                 "ensure_player"):
        setattr(bar, name, lambda *a, _n=name, **k: touched.append(_n))
    with caplog.at_level(logging.ERROR, logger="app.controllers.base"):
        controller.detach()
        assert _settled(lambda: "stop_playback" in media.names())
    assert touched == [], f"detach 之后还碰了控件：{touched}"
    assert caplog.records == [], "detach 后的编排任务抛了异常"


def test_playing_a_video_interrupts_reading_aloud():
    """听书和看视频不能同时出声。"""
    view, controller, media = _rig([_video_block("https://v/1.mp4")])
    scheduled: list[str] = []

    def fake_ui_call(coro):
        # 把协程关掉再记账：本条只验"起播前先停了朗读"，不让播放真的跑起来
        scheduled.append(getattr(getattr(coro, "cr_code", None), "co_name", ""))
        coro.close()
        return None

    controller._tts = SimpleNamespace(stop_speaking=AsyncMock(),
                                      active_backend=None)
    controller._tts_playing = True
    controller.ui_call = fake_ui_call
    view.on_play_video("https://v/1.mp4")
    assert "_stop_backend" in scheduled, "起播没有叫停朗读"
    assert controller._tts_playing is False
    assert media.calls == []
