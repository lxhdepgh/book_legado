"""M4 朗读服务测试（ReadAloudService，契约 §9 / 验收 1）。

覆盖：三后端抽象 / 系统后端加载失败降级路径 / **系统后端线程亲和**（pyttsx3
是 COM/SAPI，跨线程调 say+runAndWait 会静默死锁）/ stop 打断长语音 / close 在
专属线程上释放 / HttpTts 基础请求 stub / tts_profiles CRUD /
ReadAloudStateEvent 发布 / 系统不可用自动回退 edge。

数据目录经环境变量重定向到短临时目录；StorageService 用真实实例，
后端协作用 stub，pyttsx3/edge-tts 不在测试里真实出声（无音频设备环境
属预期——服务降级+回退，契约 §5）。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import ReadAloudStateEvent
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.content.read_aloud import (
    EdgeTtsBackend,
    HttpTtsBackend,
    ReadAloudService,
    SystemTtsBackend,
    TtsBackend,
)


# --------------------------------------------------------------------------- #
# 测试夹具与替身
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(monkeypatch):
    """真实 StorageService + SettingsService + EventBus（短路径 tmp 数据根）。"""
    root = Path(tempfile.mkdtemp(prefix="m4read_"))
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


class _StubTts(TtsBackend):
    """测试替身后端：记录朗读调用，不出声。"""

    def __init__(self, name="stub", available=True):
        super().__init__()
        self.name = name
        self.available = available
        self.spoken: list[str] = []

    async def speak(self, text, opts=None):
        self.spoken.append(str(text))
        self.state = "idle"


class _DegradedSystem(TtsBackend):
    """模拟"系统 TTS 加载失败"的降级后端（available=False / state=error）。"""

    name = "system"

    def __init__(self):
        super().__init__()
        self.available = False
        self.state = "error"
        self.last_error = "no audio device"


class _RecordingEngine:
    """pyttsx3 ``Engine`` 的替身：照真实外部循环语义工作，并记录**每次调用落在
    哪条线程**（:class:`SystemTtsBackend` 的线程亲和护栏靠它取证）。

    真实语义见 ``pyttsx3/engine.py``：``startLoop(useDriverLoop=False)`` 之后
    要由调用方反复 ``iterate()``，驱动在其中某一轮回 ``finished-utterance``；
    没进循环就 ``iterate()`` 抛 RuntimeError；``runAndWait`` 走的是驱动自带
    循环（外部插不进停音），所以后端不该用它。
    """

    def __init__(self, finish_after: int | None = 3) -> None:
        #: [(线程 ident, 线程名, 方法名)]，按调用顺序。
        self.calls: list[tuple[int, str, str]] = []
        self._cbs: dict[str, list] = {}
        self._in_loop = False
        self._driver_loop = True
        self._rounds = 0
        #: None = 永不自己回读完事件（模拟长语音，只能被 stop 打断）。
        self.finish_after = finish_after
        self.stop_count = 0

    def _mark(self, method: str) -> None:
        self.calls.append(
            (threading.get_ident(), threading.current_thread().name, method))

    @property
    def methods(self) -> list[str]:
        return [c[2] for c in self.calls]

    @property
    def idents(self) -> set[int]:
        return {c[0] for c in self.calls}

    def setProperty(self, name, value) -> None:
        self._mark(f"setProperty:{name}")

    def say(self, text, name=None) -> None:
        self._mark("say")

    def connect(self, topic, cb) -> dict:
        self._mark("connect")
        self._cbs.setdefault(topic, []).append(cb)
        return {"topic": topic, "cb": cb}

    def disconnect(self, token) -> None:
        self._mark("disconnect")
        self._cbs.setdefault(token["topic"], []).remove(token["cb"])

    def startLoop(self, useDriverLoop=True) -> None:
        self._mark("startLoop")
        if self._in_loop:
            raise RuntimeError("run loop already started")
        self._in_loop = True
        self._driver_loop = useDriverLoop
        self._rounds = 0

    def iterate(self) -> None:
        self._mark("iterate")
        if not self._in_loop:
            raise RuntimeError("run loop not started")
        if self._driver_loop:
            raise RuntimeError("iterate not valid in driver run loop")
        self._rounds += 1
        if self.finish_after is not None and self._rounds >= self.finish_after:
            for cb in list(self._cbs.get("finished-utterance", [])):
                cb(name=None, completed=True)

    def endLoop(self) -> None:
        self._mark("endLoop")
        if not self._in_loop:
            raise RuntimeError("run loop not started")
        self._in_loop = False

    def runAndWait(self) -> None:
        # 真实实现会起驱动自带循环、阻塞到读完；外部无法插进停音。
        self._mark("runAndWait")
        for cb in list(self._cbs.get("finished-utterance", [])):
            cb(name=None, completed=True)

    def stop(self) -> None:
        self._mark("stop")
        self.stop_count += 1


@pytest.fixture()
def fake_engine(monkeypatch):
    """把 ``pyttsx3.init`` 换成返回 :class:`_RecordingEngine`（不碰真实 SAPI）。

    用法：``backend, engine = fake_engine(finish_after=None)``。init 本身也在
    后端的专属线程上被调用，与真实 COM 建对象的线程一致。
    """
    import pyttsx3

    holder: list[_RecordingEngine] = []

    def _init(*args, **kwargs):
        engine = holder[0]
        engine._mark("init")
        return engine

    monkeypatch.setattr(pyttsx3, "init", _init)

    def _make(finish_after: int | None = 3):
        holder.clear()
        holder.append(_RecordingEngine(finish_after=finish_after))
        return SystemTtsBackend(), holder[0]

    return _make


# --------------------------------------------------------------------------- #
# 1. 三后端均为 TtsBackend 子类，接口齐备
# --------------------------------------------------------------------------- #
def test_backends_are_tts_backend_subclasses():
    # 仅校验抽象关系与接口齐备；不真实实例化 SystemTtsBackend（pyttsx3.init 会
    # 拉起 SAPI，污染共享 engine 线程——契约 §5：测试用 stub 后端，真实降级路径
    # 由 test_system_backend_load_failure_degrades 单独覆盖）。
    for cls in (SystemTtsBackend, EdgeTtsBackend, HttpTtsBackend):
        assert issubclass(cls, TtsBackend), f"{cls.__name__} 未继承 TtsBackend"
        for method in ("speak", "stop", "is_speaking", "close"):
            assert callable(getattr(cls, method)), f"{cls.__name__} 缺 {method}"
    # HttpTtsBackend 可无副作用实例化（不碰 pyttsx3/edge-tts）。
    http = HttpTtsBackend(fetcher=None)
    assert hasattr(http, "state")


# --------------------------------------------------------------------------- #
# 2. 系统后端加载失败 → 降级（available=False / state=error），不抛崩
# --------------------------------------------------------------------------- #
def test_system_backend_load_failure_degrades(monkeypatch):
    import pyttsx3

    def _boom():
        raise RuntimeError("no audio device / no driver")

    # 模拟无音频设备环境：pyttsx3.init() 抛错。
    monkeypatch.setattr(pyttsx3, "init", _boom)
    backend = SystemTtsBackend()
    assert backend.available is False
    assert backend.state == "error"
    assert backend.last_error
    # speak 在降级态上抛 RuntimeError（服务据此回退）。
    with pytest.raises(RuntimeError):
        asyncio.run(backend.speak("不会真的朗读"))
    # 降级不留专属线程（否则每次装配失败都漏一条）。
    backend.close()
    backend._owner.join(timeout=5)
    assert backend._owner.is_alive() is False


# --------------------------------------------------------------------------- #
# 2.1 线程亲和：pyttsx3 的每一次调用（含 init）都只落在专属线程上
#     跨线程调 say + runAndWait 是静默死锁——SAPI COM 对象绑创建线程。
# --------------------------------------------------------------------------- #
def test_system_backend_calls_stay_on_one_owner_thread(fake_engine):
    backend, engine = fake_engine()
    assert backend.available is True
    owner = backend._owner.ident
    asyncio.run(backend.speak("朗读这段", {"rate": 180, "voice": "z"}))

    assert engine.idents == {owner}, \
        f"pyttsx3 调用跨了线程：{engine.calls}"
    assert {c[1] for c in engine.calls} == {SystemTtsBackend.OWNER_THREAD_NAME}
    # 调用方（测试线程）一次都没被借去碰引擎。
    assert threading.get_ident() not in engine.idents
    # 出声走外部循环，不走 runAndWait：驱动自带循环里插不进停音。
    assert "runAndWait" not in engine.methods
    for required in ("init", "setProperty:rate", "say",
                     "startLoop", "iterate", "endLoop"):
        assert required in engine.methods, f"缺 {required}：{engine.methods}"
    assert backend.state == "idle"
    backend.close()
    backend._owner.join(timeout=5)


def test_system_backend_stop_preempts_long_utterance(fake_engine):
    # finish_after=None：驱动永不回读完事件，模拟一段长语音。
    backend, engine = fake_engine(finish_after=None)
    owner = backend._owner.ident

    async def _run():
        speaking = asyncio.ensure_future(backend.speak("很长的第一段", {}))
        await asyncio.sleep(0.3)
        await backend.stop()
        # 停音后 speak 必须返回；返回不了就是原来那种死锁。
        await asyncio.wait_for(speaking, timeout=5.0)

    started = time.monotonic()
    asyncio.run(_run())
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"stop 没能打断朗读（耗时 {elapsed:.1f}s）"
    assert engine.stop_count >= 1
    assert engine.methods.index("stop") > engine.methods.index("say")
    assert engine.idents == {owner}
    assert backend.state == "idle"


def test_system_backend_close_releases_on_owner_thread(fake_engine):
    backend, engine = fake_engine()
    owner = backend._owner.ident
    asyncio.run(backend.speak("第一段", {}))
    engine.calls.clear()

    started = time.monotonic()
    backend.close()
    # close 只把释放排进专属线程、不 join：调用方不能等 COM 收尾。
    assert time.monotonic() - started < 0.5
    backend._owner.join(timeout=5)
    assert backend._owner.is_alive() is False
    assert backend.available is False
    assert backend._engine is None
    assert "disconnect" in engine.methods
    assert engine.idents == {owner}

    # 关闭后 speak 立即上抛，不排队给已经收摊的线程。
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        asyncio.run(backend.speak("关闭后不再朗读", {}))
    assert time.monotonic() - started < 1.0


def test_service_on_stop_closes_backend_on_owner_thread(env, fake_engine):
    # 接线护栏：释放动作真被服务调用到，且仍落在专属线程上（不在引擎循环上）。
    backend, engine = fake_engine()
    owner = backend._owner.ident
    svc = ReadAloudService(storage=env.storage, backends={"system": backend})
    asyncio.run(svc.on_start())
    asyncio.run(svc.speak("book-x", "要收尾的语音", {}))
    engine.calls.clear()

    asyncio.run(svc.on_stop())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "disconnect" not in engine.methods:
        time.sleep(0.05)
    assert "disconnect" in engine.methods, f"on_stop 没关掉后端：{engine.calls}"
    assert engine.idents == {owner}


# --------------------------------------------------------------------------- #
# 3. HttpTts 基础请求 stub：URL 模板替换 + headers 透传
# --------------------------------------------------------------------------- #
def test_http_tts_basic_request_stub():
    calls: list[tuple[str, dict]] = []

    async def fetcher(url, headers):
        calls.append((url, headers))
        return b"FAKE_MP3_BYTES"

    backend = HttpTtsBackend(fetcher=fetcher)
    opts = {
        "url": "https://tts.example.com/speak?text={text}",
        "headers": {"X-Token": "abc", "Accept": "audio/mpeg"},
    }
    asyncio.run(backend.speak("你好世界", opts))
    expected_url = "https://tts.example.com/speak?text=" + quote("你好世界")
    assert calls == [(expected_url, {"X-Token": "abc", "Accept": "audio/mpeg"})]
    assert backend.last_request is not None
    assert backend.last_request["bytes"] == len(b"FAKE_MP3_BYTES")
    assert backend.state == "idle"


# --------------------------------------------------------------------------- #
# 4. tts_profiles 表 CRUD（schema v4）
# --------------------------------------------------------------------------- #
def test_tts_profiles_crud(env):
    # 注入空后端表，避免 on_start 装配真实 SystemTtsBackend（pyttsx3 拉 SAPI）。
    svc = ReadAloudService(storage=env.storage, backends={})
    asyncio.run(svc.on_start())

    pid = asyncio.run(svc.add_profile(
        "我的语音", "https://tts/{text}", {"X-Token": "abc"}, "http"))
    assert pid > 0

    profiles = asyncio.run(svc.list_profiles())
    assert len(profiles) == 1
    p = profiles[0]
    assert p["name"] == "我的语音"
    assert p["engine_type"] == "http"
    assert p["headers"] == {"X-Token": "abc"}

    assert asyncio.run(svc.remove_profile(pid)) is True
    assert asyncio.run(svc.list_profiles()) == []


# --------------------------------------------------------------------------- #
# 5. ReadAloudStateEvent 发布（speaking / idle）
# --------------------------------------------------------------------------- #
def test_read_aloud_state_event_published(env):
    fired: list[ReadAloudStateEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(ReadAloudStateEvent, collector)

    stub = _StubTts(name="system", available=True)
    svc = ReadAloudService(storage=env.storage, backends={"system": stub})
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    asyncio.run(svc.speak("book-1", "朗读这段", {"chapter": 2}))
    _drain(env.pending)

    mine = [e for e in fired if e.book_url == "book-1"]
    states = [e.state for e in mine]
    assert "speaking" in states
    assert "idle" in states
    speaking_ev = [e for e in mine if e.state == "speaking"][0]
    assert speaking_ev.chapter == 2


# --------------------------------------------------------------------------- #
# 6. 系统 TTS 不可用 → 自动回退 edge（验收不绑定单一引擎）
# --------------------------------------------------------------------------- #
def test_service_falls_back_to_edge_when_system_unavailable(env):
    edge_stub = _StubTts(name="edge", available=True)
    svc = ReadAloudService(
        storage=env.storage,
        backends={"system": _DegradedSystem(), "edge": edge_stub,
                  "http": HttpTtsBackend(fetcher=None)})
    svc.bus = env.bus
    asyncio.run(svc.on_start())

    # 默认激活的不是不可用的 system，而是回退到 edge。
    assert svc.active_backend is edge_stub

    # speak 也走 edge 后端。
    asyncio.run(svc.speak("book-fb", "回退朗读", {}))
    assert edge_stub.spoken == ["回退朗读"]


# --------------------------------------------------------------------------- #
# 7. set_voice 按 profile.engine_type 选后端
# --------------------------------------------------------------------------- #
def test_set_voice_selects_backend(env):
    http_stub = _StubTts(name="http", available=True)
    svc = ReadAloudService(
        storage=env.storage,
        backends={"system": _DegradedSystem(), "edge": _StubTts("edge"),
                  "http": http_stub})
    asyncio.run(svc.on_start())

    chosen = svc.set_voice({"engine_type": "http", "url": "https://t/{text}",
                            "headers": {}})
    assert chosen is http_stub
    assert svc.active_backend is http_stub
