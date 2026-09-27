"""M4 朗读服务测试（ReadAloudService，契约 §9 / 验收 1）。

覆盖：三后端抽象 / 系统后端加载失败降级路径 / HttpTts 基础请求 stub /
tts_profiles CRUD / ReadAloudStateEvent 发布 / 系统不可用自动回退 edge。

数据目录经环境变量重定向到短临时目录；StorageService 用真实实例，
后端协作用 stub，pyttsx3/edge-tts 不在测试里真实出声（无音频设备环境
属预期——服务降级+回退，契约 §5）。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
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


# --------------------------------------------------------------------------- #
# 1. 三后端均为 TtsBackend 子类，接口齐备
# --------------------------------------------------------------------------- #
def test_backends_are_tts_backend_subclasses():
    # 仅校验抽象关系与接口齐备；不真实实例化 SystemTtsBackend（pyttsx3.init 会
    # 拉起 SAPI，污染共享 engine 线程——契约 §5：测试用 stub 后端，真实降级路径
    # 由 test_system_backend_load_failure_degrades 单独覆盖）。
    for cls in (SystemTtsBackend, EdgeTtsBackend, HttpTtsBackend):
        assert issubclass(cls, TtsBackend), f"{cls.__name__} 未继承 TtsBackend"
        for method in ("speak", "stop", "is_speaking"):
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
