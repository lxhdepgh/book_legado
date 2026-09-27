"""M4 视频书播放服务测试（MediaPlayService，契约 §9 / 验收 1）。

覆盖：后端抽象 / 播放列表（章节列表即播放列表）/ 进度落库（stub 后端）/
MediaPlayStateEvent 发布 / 视频扩展名集合复用 / FletVideoBackend 委托控件。

服务本身不持有 flet 控件——测试用 stub VideoBackend，不拉起 flet_video。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import MediaPlayStateEvent
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.content.media_play import (
    FletVideoBackend,
    MediaPlayService,
    SystemPlayerBackend,
    VIDEO_EXTENSIONS,
    VideoBackend,
    is_video_url,
)


# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(monkeypatch):
    """真实 StorageService + SettingsService + EventBus（短路径 tmp 数据根）。"""
    root = Path(tempfile.mkdtemp(prefix="m4media_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))
    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    settings = SettingsService(storage=storage, engine=engine)
    asyncio.run(settings.on_start())
    bus = EventBus(engine=engine)
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


class _StubVideo(VideoBackend):
    """测试替身播放后端：记录 open/play/pause/seek，不拉起真实播放器。"""

    def __init__(self):
        super().__init__()
        self.opened_url: str | None = None
        self.opened_headers: dict | None = None
        self.play_count = 0
        self.seeked_to: float | None = None

    async def open(self, media_url, headers=None):
        self.opened_url = media_url
        self.opened_headers = dict(headers or {})
        self.state = "playing"

    async def play(self):
        self.play_count += 1
        self.state = "playing"

    async def pause(self):
        self.state = "paused"

    async def seek(self, position):
        self.seeked_to = float(position)


# --------------------------------------------------------------------------- #
# 1. 视频扩展名集合复用 reading_engine；is_video_url 判定
# --------------------------------------------------------------------------- #
def test_video_extensions_reused_and_detected():
    # 复用 reading_engine 的集合（mp4/m3u8/...），不改该文件。
    assert "mp4" in VIDEO_EXTENSIONS and "m3u8" in VIDEO_EXTENSIONS
    assert is_video_url("https://cdn.example.com/a/ep1.mp4") is True
    assert is_video_url("https://cdn.example.com/live.m3u8") is True
    assert is_video_url("https://cdn.example.com/a.txt") is False
    # 带 query/hash 不影响扩展名判定。
    assert is_video_url("https://cdn.example.com/a.mp4?token=xxx") is True


# --------------------------------------------------------------------------- #
# 2. 两后端均为 VideoBackend 子类，接口齐备
# --------------------------------------------------------------------------- #
def test_video_backends_are_subclasses():
    for cls in (FletVideoBackend, SystemPlayerBackend):
        assert issubclass(cls, VideoBackend)
        inst = cls()
        for method in ("open", "play", "pause", "seek", "stop",
                       "current_position"):
            assert hasattr(inst, method), f"{cls.__name__} 缺 {method}"


# --------------------------------------------------------------------------- #
# 3. 播放列表：open_book 把章节列表作为播放列表，恢复上次位置
# --------------------------------------------------------------------------- #
def test_open_book_playlist_and_restore(env):
    backend = _StubVideo()
    svc = MediaPlayService(storage=env.storage, backend=backend)
    asyncio.run(svc.on_start())

    chapters = [{"url": "https://v/1.mp4", "name": "ep1"},
                {"url": "https://v/2.mp4", "name": "ep2"}]
    index, position = asyncio.run(svc.open_book("video-book-1", chapters))
    assert index == 0 and position == 0.0

    asyncio.run(svc.play_chapter(1))
    assert backend.opened_url == "https://v/2.mp4"

    # 重开同一本书：恢复到第 1 章 / 上次位置。
    index2, position2 = asyncio.run(svc.open_book("video-book-1", chapters))
    assert index2 == 1


# --------------------------------------------------------------------------- #
# 4. 播放位置落 books 表（play_chapter_index + play_position），**不动阅读进度**
# --------------------------------------------------------------------------- #
def test_play_progress_persisted_to_books(env):
    backend = _StubVideo()
    svc = MediaPlayService(storage=env.storage, backend=backend)
    asyncio.run(svc.on_start())

    chapters = [{"url": "https://v/1.mp4"}, {"url": "https://v/2.mp4"}]
    asyncio.run(svc.open_book("video-book-p", chapters))
    asyncio.run(svc.play_chapter(1))
    asyncio.run(svc.seek(42.5))

    row = env.storage.db.execute(
        "SELECT play_chapter_index, play_position FROM books WHERE book_url=?",
        ("video-book-p",)).fetchone()
    assert row is not None
    assert row[0] == 1
    assert abs(row[1] - 42.5) < 1e-6


def test_playing_a_video_does_not_move_reading_progress(env):
    """正文里嵌视频的小说：放第 50 章的视频不能把"上次读到第 3 章"改掉。

    v4 曾把播放索引复用成 ``books.chapter_index``（那时只放视频书），界面接出
    播放面板后这条设想会变成进度篡改 —— 这条护栏钉的就是"没发生"。
    """
    conn = env.storage.db
    conn.execute(
        "INSERT INTO books(book_url, name, chapter_index, play_position)"
        " VALUES(?,?,?,?)", ("novel-with-video", "有视频的小说", 3, 0.0))
    svc = MediaPlayService(storage=env.storage, backend=_StubVideo())
    asyncio.run(svc.on_start())
    asyncio.run(svc.open_book("novel-with-video",
                              [{"url": f"https://v/{n}.mp4"} for n in (1, 2, 50)]))
    asyncio.run(svc.play_chapter(2))
    asyncio.run(svc.seek(120.0))
    asyncio.run(svc.pause())

    row = conn.execute(
        "SELECT chapter_index, play_chapter_index, play_position, name"
        " FROM books WHERE book_url=?", ("novel-with-video",)).fetchone()
    assert row[0] == 3, "播放改写了阅读进度列"
    assert row[1] == 2 and abs(row[2] - 120.0) < 1e-6
    assert row[3] == "有视频的小说", "upsert 把已有行的其它列抹了"

    # "接着上次看"读的也得是播放列：换一次会话重开，恢复的是第 3 条 / 120 秒，
    # 不是阅读进度那章（第 4 章＝chapter_index 3）。
    again = MediaPlayService(storage=env.storage, backend=_StubVideo())
    asyncio.run(again.on_start())
    index, position = asyncio.run(again.open_book(
        "novel-with-video", [{"url": "https://v/1.mp4"}] * 3))
    assert (index, position) == (2, 120.0)


# --------------------------------------------------------------------------- #
# 5. MediaPlayStateEvent 发布（playing / paused）
# --------------------------------------------------------------------------- #
def test_media_play_state_event_published(env):
    fired: list[MediaPlayStateEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(MediaPlayStateEvent, collector)

    svc = MediaPlayService(storage=env.storage, backend=_StubVideo())
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    asyncio.run(svc.open_book("vb", [{"url": "u.mp4"}]))
    asyncio.run(svc.play_chapter(0))
    asyncio.run(svc.pause())
    _drain(env.pending)

    mine = [e for e in fired if e.book_url == "vb"]
    states = [e.state for e in mine]
    assert "playing" in states
    assert "paused" in states
    playing = [e for e in mine if e.state == "playing"][0]
    assert playing.chapter == 0


# --------------------------------------------------------------------------- #
# 6. 播放列表越界抛 IndexError（状态机闭合）
# --------------------------------------------------------------------------- #
def test_mark_ended_publishes_ended_and_clears_the_position(env):
    """放完了：状态归 ended，**位置清 0** 而不是记到片尾。

    记到片尾的话，下次「接着上次看」一进去就立刻到底，看起来像播放器坏了。
    """
    conn = env.storage.db
    fired: list[MediaPlayStateEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(MediaPlayStateEvent, collector)
    backend = _StubVideo()
    svc = MediaPlayService(storage=env.storage, backend=backend)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    asyncio.run(svc.open_book("vb-end", [{"url": "a.mp4"}, {"url": "b.mp4"}]))
    asyncio.run(svc.play_chapter(1))
    asyncio.run(svc.seek(900.0))
    asyncio.run(svc.mark_ended())
    _drain(env.pending)

    row = conn.execute(
        "SELECT play_chapter_index, play_position FROM books WHERE book_url=?",
        ("vb-end",)).fetchone()
    assert row[0] == 1, "停在哪一条还得留着（下次从这条的片头看）"
    assert row[1] == 0.0, "片尾位置被记下来了，下次进去立刻又到底"
    assert [e.state for e in fired if e.book_url == "vb-end"][-1] == "ended"

    # 重开这本书：恢复的是"第 2 条的片头"，不是"第 2 条的第 900 秒"
    again = MediaPlayService(storage=env.storage, backend=_StubVideo())
    asyncio.run(again.on_start())
    index, position = asyncio.run(again.open_book(
        "vb-end", [{"url": "a.mp4"}, {"url": "b.mp4"}]))
    assert (index, position) == (1, 0.0)


def test_play_chapter_out_of_range_raises(env):
    svc = MediaPlayService(storage=env.storage, backend=_StubVideo())
    asyncio.run(svc.on_start())
    asyncio.run(svc.open_book("vb", [{"url": "u.mp4"}]))
    with pytest.raises(IndexError):
        asyncio.run(svc.play_chapter(5))


# --------------------------------------------------------------------------- #
# 7. FletVideoBackend 委托注入的控件（鸭子类型，服务层不 import flet）
# --------------------------------------------------------------------------- #
def test_flet_video_backend_delegates_to_control():
    class _FakeControl:
        def __init__(self):
            self.playlist: list = []
            self.calls: list = []
            self.position = 42.0

        def play(self):
            self.calls.append("play")

        def pause(self):
            self.calls.append("pause")

        def stop(self):
            self.calls.append("stop")

        def seek(self, position):
            self.calls.append(("seek", position))

        async def get_current_position(self):
            return self.position

    ctrl = _FakeControl()
    backend = FletVideoBackend(
        control=ctrl,
        media_factory=lambda url, headers: {"resource": url, "headers": headers},
        duration_factory=lambda seconds: ("duration", int(seconds)))
    assert backend.available is True

    # 换片只能改 playlist：0.86 的 Video 没有 source 字段，写上去不会下发。
    asyncio.run(backend.open("https://v/x.mp4", {"Referer": "https://b/"}))
    assert ctrl.playlist == [{"resource": "https://v/x.mp4",
                              "headers": {"Referer": "https://b/"}}]
    assert not hasattr(ctrl, "source")
    asyncio.run(backend.play())
    asyncio.run(backend.pause())
    asyncio.run(backend.stop())
    assert ["play", "pause", "stop"] == ctrl.calls
    asyncio.run(backend.seek(10.0))
    assert ("seek", ("duration", 10)) in ctrl.calls
    assert asyncio.run(backend.current_position()) == 42.0


def test_flet_video_backend_refuses_to_guess_units_or_types():
    """没注入工厂时：不换片、不跳转，而不是把字符串/裸秒硬塞给播放器。

    这两条早先都是"看着能跑"的实错——``control.source = url`` 静默无效（换章
    永远停在第一条），``seek(10.0)`` 单位猜错就是"接着上次看"跳到片头几毫秒。
    """
    class _FakeControl:
        def __init__(self):
            self.playlist: list = []

        def seek(self, position):
            raise AssertionError("没注入 duration_factory 不该调 seek")

    ctrl = _FakeControl()
    backend = FletVideoBackend(control=ctrl)
    with pytest.raises(RuntimeError):
        asyncio.run(backend.open("https://v/x.mp4"))
    assert ctrl.playlist == []
    asyncio.run(backend.seek(10.0))          # 静默跳过
    assert asyncio.run(backend.current_position()) == 0.0


def test_flet_video_backend_marshals_every_control_touch():
    """每一步控件操作都要过 ``ui_marshal``：服务方法跑在 EngineHub 的 loop 上，
    而 flet 控件只认界面循环（漏一步就是"偶发点了没反应"级别的跨线程 bug）。

    护栏钉的是记账：marshal 没被调用、或控件被直接碰到，都在这里变红。
    """
    class _FakeControl:
        def __init__(self):
            self.playlist: list = []
            self.calls: list = []

        def play(self):
            self.calls.append("play")

        def pause(self):
            self.calls.append("pause")

        def stop(self):
            self.calls.append("stop")

        def seek(self, position):
            self.calls.append(("seek", position))

        async def get_current_position(self):
            self.calls.append("get_current_position")
            return 7.5

    ctrl = _FakeControl()
    marshalled: list[str] = []

    async def marshal(coro):
        marshalled.append("ran")
        return await coro

    backend = FletVideoBackend(
        control=ctrl, ui_marshal=marshal,
        media_factory=lambda url, headers: url,
        duration_factory=lambda seconds: int(seconds))
    asyncio.run(backend.open("https://v/x.mp4"))
    asyncio.run(backend.play())
    asyncio.run(backend.pause())
    asyncio.run(backend.seek(3.0))
    asyncio.run(backend.stop())
    assert asyncio.run(backend.current_position()) == 7.5
    # 6 步：open(换片) / play / pause / seek / stop / current_position
    assert len(marshalled) == 6, "有控件操作没经界面循环"
    assert ctrl.playlist == ["https://v/x.mp4"]
    assert ("seek", 3) in ctrl.calls

    # 没注入 marshal 时照样能跑（无头测试、以及本来就在界面循环上的调用方）。
    plain = FletVideoBackend(control=_FakeControl(),
                             media_factory=lambda url, headers: url)
    asyncio.run(plain.open("https://v/y.mp4"))
    assert plain.media_url == "https://v/y.mp4"
