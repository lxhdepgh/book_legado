"""朗读播放条的离线护栏：动作 → 服务 → 重绘，一条链不能断。

``ReadAloudService`` 的 speak/stop/状态事件在 M4 就写完了，但界面一处都没接（设计
§1.1 的"迷你播放条"只存在于文档里）。本文件钉住新接上的那一段，覆盖只有真视图 +
真控制器 + 真 ``ReadingService`` 才暴露得出来的五件事：

1. 播放条随章节亮灭（没有朗读服务时整条不出现）；
2. 点播放 → 服务收到**当前章的正文**（段首缩进不许进朗读稿）；
3. 连读先把下一章挂出来、再念新章的稿子，而不是把第一章念两遍；
4. 每一次停都有人被告知：再按一次、手动翻章、切页离开，三条路都得叫停后端；
5. 语速档位真的进了 ``opts``（并落库、重开阅读器还在），念的时候标签上写着**是谁在
   念**——因为 edge / HTTP 后端根本不吃语速，不显示后端名就是让用户对着空气调档。

全程离线：TTS 后端是替身（无音频设备环境属预期，契约 §5），引擎协作用桩，
不发真实网络请求（见记忆 legado-live-scan-av-block）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.engine_worker import engine_hub
from app.core.events import ReadAloudStateEvent
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.content.read_aloud import ReadAloudService
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

CHAPTERS = [
    ChapterItem("第一章 出海", f"{BOOK_URL}/c1", index=0, is_volume=False),
    ChapterItem("卷二", "", index=1, is_volume=True),
    ChapterItem("第二章 靠岸", f"{BOOK_URL}/c2", index=2, is_volume=False),
]
FIRST, VOLUME, SECOND = CHAPTERS
FIRST_TEXT = "第一章 出海的正文\n海上的第二段"
SECOND_TEXT = "第二章 靠岸的正文\n岸上的第二段"

_ENV_KEYS = ("LEGADO_BOOKSOURCE_DATA_DIR", "LEGADO_BOOKSOURCE_CACHE_DIR")


class _StubEngine:
    """引擎替身：正文带段首缩进，朗读稿必须把它去掉（不然每段先喘一口）。"""

    async def chapters(self, source, book):
        return list(CHAPTERS)

    async def content_document(self, source, chapter, next_url=""):
        body = FIRST_TEXT if chapter.url == FIRST.url else SECOND_TEXT
        head, tail = body.splitlines()
        return ChapterContent(
            title=chapter.name,
            blocks=[ContentBlock("text", f"　　{head}\n　　{tail}")])


class _StubTts:
    """朗读服务替身：记账每次朗读，可选"堵住不出声"与"当场失败"两种脾气。"""

    def __init__(self, *, raises: Exception | None = None, hold: bool = False,
                 backend: str = "system",
                 profiles: list[dict] | None = None) -> None:
        self.spoken: list[tuple[str, str, dict]] = []
        self.stop_calls = 0
        self._raises = raises
        self._gate = threading.Event() if hold else None
        # 真服务在 speak 里才会把 _active 换成实际后端；替身固定报一个名，
        # 让"播放条显示谁在念"这一条测得动（回退链本身由服务侧用例覆盖）。
        self.active_backend = SimpleNamespace(name=backend)
        # tts_profiles：界面对这张表的全部认知都从 list_profiles 来
        self.profiles = [dict(p) for p in (profiles or [])]
        self.voices_applied: list[dict] = []

    async def speak(self, book_url, text, opts=None) -> None:
        if self._raises is not None:
            raise self._raises
        self.spoken.append((str(book_url), str(text), dict(opts or {})))
        if self._gate is not None:      # 真 TTS 会念很久：用门闩冻在这一章
            await asyncio.to_thread(self._gate.wait)

    async def stop_speaking(self) -> None:
        self.stop_calls += 1
        if self._gate is not None:      # 停音 = 把正在念的这一章放开
            self._gate.set()

    async def list_profiles(self) -> list[dict]:
        return [dict(p) for p in self.profiles]

    def set_voice(self, profile) -> SimpleNamespace:
        self.voices_applied.append(dict(profile or {}))
        return self.active_backend


class _FakeHub:
    """控制器只要 hub.get(类型)；bus=None 表示不发事件（事件面另有用例）。"""

    def __init__(self, services):
        self.bus = None
        self._services = services

    def get(self, stype):
        return self._services[stype]


# --------------------------------------------------------------------------- #
# 装配 / 收尾
# --------------------------------------------------------------------------- #
def _attach(tts=None, with_service: bool = True):
    """真 ReadingService + 真 ReaderView/ReaderController，短路径 tmp 数据根。"""
    root = Path(tempfile.mkdtemp(prefix="reader_tts_"))
    env = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = str(root / "data")
    os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = str(root / "cache")
    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    settings = SettingsService(storage=storage, engine=engine)
    asyncio.run(settings.on_start())
    reading = ReadingService(
        rule_engine=_StubEngine(), storage=storage, settings=settings,
        source_service=None, cache=ReadingCache(storage.paths.cache))
    services = {ReadingService: reading, BookmarkService: SimpleNamespace(),
                ContentSearchService: SimpleNamespace()}
    if with_service:
        tts = tts if tts is not None else _StubTts()
        services[ReadAloudService] = tts
    view = ReaderView()
    controller = ReaderController(_FakeHub(services))
    controller.attach(view)
    return SimpleNamespace(view=view, controller=controller, reading=reading,
                           services=services, storage=storage, settings=settings,
                           root=root, env=env, tts=tts)


def _detach(rig) -> None:
    """收尾顺序照旧：先摘控制器（停预取/停朗读），再关服务、还 env、删 tmp。"""
    rig.controller.detach()
    asyncio.run(rig.reading.on_stop())
    asyncio.run(rig.settings.on_stop())
    asyncio.run(rig.storage.stop())
    for key, value in rig.env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(rig.root, ignore_errors=True)


def _rig(**kwargs):
    """单用例夹具工厂：attach → yield → detach，数据根不外泄给别的用例。"""
    handle = _attach(**kwargs)
    try:
        yield handle
    finally:
        _detach(handle)


@pytest.fixture()
def rig():
    yield from _rig()


@pytest.fixture()
def held_rig():
    """朗读"堵住不放"的装配：用来测"正在念的时候按停/翻章/切页"。"""
    yield from _rig(tts=_StubTts(hold=True))


@pytest.fixture()
def broken_rig():
    yield from _rig(tts=_StubTts(raises=RuntimeError("无可用 TTS 后端")))


@pytest.fixture()
def bare_rig():
    """hub 里没有 ReadAloudService：阅读器要照常工作。"""
    yield from _rig(with_service=False)


#: 一条 HTTP-TTS 语音档：engine_type/url/headers 三样都得原样进 speak 的 opts，
#: 少带一样服务侧就只会拿默认地址念，界面上看着"选了却没换"。
HTTP_PROFILE = {
    "id": 7, "name": "书源 TTS",
    "url": "http://127.0.0.1:1/tts?text={text}",
    "headers": {"X-Key": "offline"}, "engine_type": "http",
}


@pytest.fixture()
def voiced_rig():
    """表里有一条语音档的装配：音色下拉才有东西可选。"""
    yield from _rig(tts=_StubTts(profiles=[HTTP_PROFILE]))


# --------------------------------------------------------------------------- #
# 驱动辅助
# --------------------------------------------------------------------------- #
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
    assert _settled(lambda: rig.controller._chapter_index == 0), "开书没挂出第一章"


def _press(rig) -> None:
    """点播放条主按钮：走视图登记的回调槽，不是直接调控制器方法。"""
    rig.view.read_aloud.on_toggle()


def _follow(rig, value: bool) -> None:
    box = rig.view.read_aloud._follow
    box.value = value
    box.on_change(SimpleNamespace(control=box))


def _pick_rate(rig, rate: str) -> None:
    """在下拉里选语速档位：走 Dropdown 的 on_select，不直接改控件的值。"""
    box = rig.view.read_aloud._rate
    box.value = rate
    box.on_select(SimpleNamespace(control=box))


def _voice_options(rig) -> list[str]:
    return [str(o.key) for o in rig.view.read_aloud._voice.options]


def _pick_voice(rig, key: str) -> None:
    """在下拉里选音色：同 _pick_rate，走控件的 on_select。"""
    box = rig.view.read_aloud._voice
    box.value = key
    box.on_select(SimpleNamespace(control=box))


def _reattach(rig):
    """同一套服务上再装一次界面：只有这么测才看得见"重开阅读器时偏好回填没有"。"""
    view = ReaderView()
    controller = ReaderController(_FakeHub(rig.services))
    controller.attach(view)
    return SimpleNamespace(view=view, controller=controller)


def _label(rig) -> str:
    return str(getattr(rig.view.read_aloud._label, "value", "") or "")


# --------------------------------------------------------------------------- #
# 亮灭 / 播放 / 连读
# --------------------------------------------------------------------------- #
def test_bar_shows_up_only_with_a_chapter_open(rig):
    assert rig.view.read_aloud.visible is False, "没开书就亮出播放条，按什么都是空的"
    _open(rig)
    assert rig.view.read_aloud.visible is True
    assert rig.view.read_aloud.state == "idle"


def test_play_button_reads_the_open_chapter(rig):
    _open(rig)
    _follow(rig, False)
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing), "朗读没能自己收工"
    assert len(rig.tts.spoken) == 1, f"只关了连读却念了 {len(rig.tts.spoken)} 章"
    book_url, text, opts = rig.tts.spoken[0]
    assert book_url == BOOK_URL and opts["chapter"] == 0
    assert text == FIRST_TEXT, f"朗读稿被缩进或切段弄坏了：{text!r}"
    assert opts["rate"] == 185, f"默认语速没传给后端：{opts}"
    assert rig.view.read_aloud.state == "idle" and "本章读完" in _label(rig)


def test_continuous_reading_advances_to_the_chapter_it_mounted(rig):
    """连读必须念"挂出来的下一章"：正文没换就再念一遍，等于原地打转。"""
    _open(rig)
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing), "连读没在最后一章收口"
    assert [opts["chapter"] for _, _, opts in rig.tts.spoken] == [0, 1]
    assert [text for _, text, _ in rig.tts.spoken] == [FIRST_TEXT, SECOND_TEXT]
    assert rig.controller._chapter_index == 1, "念到第二章了，屏幕还停在第一章"
    assert "最后一章" in _label(rig)


def test_volume_titles_are_never_read(rig):
    """卷标题不是正文：既不进可读书章表，也就不会被打断朗读去念"卷二"。"""
    _open(rig)
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing)
    assert VOLUME.url not in [c["url"] for c in rig.controller._readable]
    assert [text for _, text, _ in rig.tts.spoken] == [FIRST_TEXT, SECOND_TEXT]


# --------------------------------------------------------------------------- #
# 停：再按一次 / 手动翻章 / 切页离开
# --------------------------------------------------------------------------- #
def test_pressing_again_stops_the_backend(held_rig):
    _open(held_rig)
    _press(held_rig)
    assert _settled(lambda: len(held_rig.tts.spoken) == 1), "按了播放没念"
    assert held_rig.view.read_aloud.state == "speaking"
    _press(held_rig)
    assert _settled(lambda: held_rig.tts.stop_calls == 1), "再按一次没有叫停后端"
    assert _settled(lambda: not held_rig.controller._tts_playing)
    time.sleep(0.3)
    assert len(held_rig.tts.spoken) == 1, "停止之后还接着念下一章"
    assert "已停止" in _label(held_rig)


def test_turning_the_page_by_hand_stops_the_voice(held_rig):
    """手动翻章时还念着上一章是干扰：下一章要出声，得由用户再按一次。"""
    _open(held_rig)
    _press(held_rig)
    assert _settled(lambda: len(held_rig.tts.spoken) == 1)
    held_rig.controller.next_chapter()
    assert _settled(lambda: held_rig.tts.stop_calls == 1), "手动翻章没把朗读停掉"
    assert _settled(lambda: not held_rig.controller._tts_playing)


def test_leaving_the_reader_stops_the_audio(held_rig):
    """阅读器是整页视图，切页即拆：声音继续放着却没地方按停，比安静更糟。"""
    _open(held_rig)
    _press(held_rig)
    assert _settled(lambda: len(held_rig.tts.spoken) == 1)
    held_rig.controller.detach()
    assert _settled(lambda: held_rig.tts.stop_calls == 1), "离开阅读器还在念"
    assert held_rig.controller._tts_playing is False


# --------------------------------------------------------------------------- #
# 出声失败要说话 / 过期事件不许把界面搅回去
# --------------------------------------------------------------------------- #
def test_backend_failure_is_said_out_loud(broken_rig):
    _open(broken_rig)
    _press(broken_rig)
    assert _settled(lambda: broken_rig.view.read_aloud.state == "error")
    assert "无可用 TTS 后端" in _label(broken_rig), _label(broken_rig)
    assert broken_rig.controller._tts_playing is False


def test_stale_state_events_do_not_flicker_the_bar(rig):
    """上一章的 idle 可能后到：按书 + 章号过滤，否则播放条在两种态之间来回跳。"""
    _open(rig)
    _follow(rig, False)      # 停在第一章：第 2 章的事件对本例才是"过期"的
    _press(rig)
    assert _settled(lambda: rig.view.read_aloud.state == "idle")
    for event in (ReadAloudStateEvent(state="speaking", book_url="http://other/book"),
                  ReadAloudStateEvent(state="speaking", book_url=BOOK_URL, chapter=1)):
        asyncio.run(rig.controller._on_read_aloud_state(event))
        assert rig.view.read_aloud.state != "speaking", \
            f"过期事件（{event.book_url} 第 {event.chapter} 章）被照单渲染了"
    asyncio.run(rig.controller._on_read_aloud_state(
        ReadAloudStateEvent(state="speaking", book_url=BOOK_URL, chapter=0)))
    assert rig.view.read_aloud.state == "speaking"
    assert "第一章" in _label(rig)


# --------------------------------------------------------------------------- #
# 语速档位与"谁在念"：调了要生效、生效要看得见、关了要还记得
# --------------------------------------------------------------------------- #
def test_choosing_a_rate_reaches_the_backend_and_is_remembered(rig):
    """语速只有系统 TTS 后端吃：档位必须换成绝对值进 opts，并落到 reader.tts_rate。"""
    _open(rig)
    _follow(rig, False)
    _pick_rate(rig, "fast")
    assert _settled(lambda: rig.settings.get("reader.tts_rate") == "fast"), \
        "选了语速没落库，下次开阅读器又回到默认"
    _press(rig)
    assert _settled(lambda: rig.tts.spoken), "按了播放没念"
    assert _settled(lambda: not rig.controller._tts_playing)
    assert rig.tts.spoken[0][2]["rate"] == 240, f"档位没换成绝对语速：{rig.tts.spoken[0][2]}"


def test_rate_choice_survives_reopening_the_reader(rig):
    """偏好回填走的是 attach 里同一条读取路径：换一副界面才测得出"重新打开"。"""
    _pick_rate(rig, "slow")
    assert _settled(lambda: rig.settings.get("reader.tts_rate") == "slow")
    second = _reattach(rig)
    try:
        # attach 把偏好读取派发到了循环上（headless 时是另一条线程），要等它落地
        assert _settled(lambda: second.view.read_aloud.rate == "slow"), \
            "重开阅读器语速没回填"
        assert second.view.read_aloud.rate_value == 140
    finally:
        second.controller.detach()


def test_the_bar_says_which_backend_is_reading(held_rig):
    """edge / HTTP 后端不吃语速：念的时候标签上必须写着是谁在念。"""
    _open(held_rig)
    _press(held_rig)
    assert _settled(lambda: held_rig.view.read_aloud.state == "speaking")
    assert "系统 TTS" in _label(held_rig), _label(held_rig)
    _press(held_rig)
    assert _settled(lambda: held_rig.tts.stop_calls == 1), "再按一次没停"
    # 标签由循环上的收尾协程刷新，只能等它自己变（_tts_playing 是同步标志位，太早）
    assert _settled(lambda: "系统 TTS" not in _label(held_rig)), \
        f"停下来了还挂着后端名：{_label(held_rig)}"


def test_a_fallback_backend_shows_its_own_name():
    """服务会自己回退（system→edge→http），界面只能问它当前是谁：回退要看得见。"""
    handle = _attach(tts=_StubTts(backend="edge"))
    try:
        _open(handle)
        handle.controller._show_read_aloud("speaking", chapter="第一章 出海")
        assert "edge 语音" in _label(handle), _label(handle)
    finally:
        _detach(handle)


# --------------------------------------------------------------------------- #
# 音色（tts_profiles）：下拉的选项、选中之后的 opts、以及"选不到"的三种情况
# --------------------------------------------------------------------------- #
def test_the_voice_options_come_from_the_profile_table(voiced_rig):
    """下拉不能写死：选项来自 list_profiles，第一项才是"内置语音"。"""
    assert _voice_options(voiced_rig) == ["builtin", str(HTTP_PROFILE["id"])], \
        "语音档没进下拉，读到的还是内置嗓音"
    labels = [str(o.text) for o in voiced_rig.view.read_aloud._voice.options]
    assert labels == ["内置语音", "书源 TTS"]
    assert voiced_rig.view.read_aloud.voice == "builtin"


def test_choosing_a_voice_sends_its_engine_url_and_headers(voiced_rig):
    """语音档的三样接法必须一起进 opts：漏一项等于选了个不存在的引擎。"""
    rig = voiced_rig
    _open(rig)
    _follow(rig, False)
    _pick_voice(rig, str(HTTP_PROFILE["id"]))
    assert _settled(lambda: rig.settings.get("reader.tts_profile") == "7"), \
        "音色没落库，重开阅读器就回到内置语音"
    assert rig.tts.voices_applied == [HTTP_PROFILE], "没叫服务切后端，标签还挂在旧引擎上"
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing)
    opts = rig.tts.spoken[0][2]
    assert opts["engine_type"] == "http" and opts["url"] == HTTP_PROFILE["url"]
    assert opts["headers"] == {"X-Key": "offline"}, opts


def test_the_builtin_voice_carries_no_engine_at_all(rig):
    """选"内置语音"= 不指定引擎：opts 里不能留下上一次那档的 url/headers。"""
    _open(rig)
    _follow(rig, False)
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing)
    opts = rig.tts.spoken[0][2]
    assert "engine_type" not in opts and "url" not in opts, opts
    assert rig.settings.get("reader.tts_profile") in (None, "")


def test_voice_choice_survives_reopening_the_reader(voiced_rig):
    """偏好回填与语速同一条路径：重开阅读器时下拉要停在原来那一档。"""
    _pick_voice(voiced_rig, str(HTTP_PROFILE["id"]))
    assert _settled(lambda: voiced_rig.settings.get("reader.tts_profile") == "7")
    second = _reattach(voiced_rig)
    try:
        assert _settled(lambda: second.view.read_aloud.voice == "7"), \
            "重开阅读器音色没回填"
        assert second.controller._tts_profile == HTTP_PROFILE
    finally:
        second.controller.detach()


def test_a_deleted_profile_falls_back_without_lying(voiced_rig):
    """偏好指着一条已删掉的档：下拉退回内置语音，而不是显示一个不存在的值。"""
    _pick_voice(voiced_rig, str(HTTP_PROFILE["id"]))
    assert _settled(lambda: voiced_rig.settings.get("reader.tts_profile") == "7")
    voiced_rig.tts.profiles = []          # 在管理页把这条删了
    second = _reattach(voiced_rig)
    try:
        assert _settled(lambda: second.view.read_aloud.voice == "builtin"), \
            "语音档没了还停在已删的那一档"
        assert second.controller._tts_profile is None
    finally:
        second.controller.detach()


def test_switching_voice_mid_reading_says_when_it_takes_effect():
    """正在念的那一句改不了口：不说"从下一章生效"，用户只会以为按了没反应。"""
    held_rig = _attach(tts=_StubTts(hold=True, profiles=[HTTP_PROFILE]))
    try:
        _open(held_rig)
        _press(held_rig)
        assert _settled(lambda: len(held_rig.tts.spoken) == 1), "按了播放没念"
        assert _settled(lambda: held_rig.view.read_aloud.state == "speaking")
        _pick_voice(held_rig, str(HTTP_PROFILE["id"]))
        assert "下一章" in _label(held_rig), _label(held_rig)
        assert "书源 TTS" in _label(held_rig), _label(held_rig)
        assert held_rig.view.read_aloud.state == "speaking", "换个音色把正在念的掐了"
        _press(held_rig)
        assert _settled(lambda: held_rig.tts.stop_calls == 1), "收尾没叫停后端"
    finally:
        _detach(held_rig)


def test_an_empty_profile_table_is_not_an_error(rig):
    """表是空的（还没登记过）也要正常出界面：只有"内置语音"一项，不许抛。"""
    assert _voice_options(rig) == ["builtin"]
    _open(rig)
    _follow(rig, False)
    _press(rig)
    assert _settled(lambda: not rig.controller._tts_playing)
    assert len(rig.tts.spoken) == 1


def test_a_failed_profile_read_leaves_the_reader_usable():
    """读表失败只是没音色可选：不能把整页偏好（字号/快捷键）一起带走。"""
    class _NoProfiles(_StubTts):
        async def list_profiles(self):
            raise RuntimeError("表打不开")

    handle = _attach(tts=_NoProfiles())
    try:
        assert _settled(lambda: _voice_options(handle) == ["builtin"])
        _open(handle)
        assert handle.controller._chapter_index == 0, "读表失败把开书也带崩了"
    finally:
        _detach(handle)


def test_reader_works_without_a_read_aloud_service(bare_rig):
    """替身装配里没有 ReadAloudService：阅读器不能因此挂掉，播放条整条不出现。"""
    assert bare_rig.controller._tts is None
    _open(bare_rig)
    assert bare_rig.view.read_aloud.visible is False, "没有朗读服务还亮着播放条"
    bare_rig.controller.toggle_read_aloud()
    bare_rig.controller.stop_read_aloud()
    assert bare_rig.tts is None

