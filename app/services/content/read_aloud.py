"""ReadAloudService：TTS 语音朗读（M4 内容形态域，设计 §1.1 / §9.2）。

对应 Legado ``TTSReadAloud + HttpReadAloud + AudioPlay`` 三合一。

后端抽象（§9.2 ``TtsBackend``，三实现，可切换/可回退）：

- :class:`SystemTtsBackend`：pyttsx3 桥——Windows SAPI / macOS NSSpeech /
  Linux espeak。**M4 最小可用后端**；缺驱动/库时加载失败即 ``available=False``
  降级（无音频设备环境不可用属预期，契约 §5）。
- :class:`EdgeTtsBackend`：edge-tts 在线神经语音。系统 TTS 不可用时的自动回退
  后端（验收不绑定单一引擎，§8.4 风险清单）。
- :class:`HttpTtsBackend`：书源规则 HTTP-TTS，请求走 RuleEngineService 网络栈
  （会话复用/UA/超时一致）。M4 实现基础请求：按 profile.url 模板取音频流字节，
  经可注入 fetcher 拉取（默认接 RuleEngineService.fetch_media），音频流缓存
  经 BookCacheService；真实出声播放 M5 接 flet_audio。

服务层红线：本模块**零 flet import**；pyttsx3/edge-tts 仅在各自后端内懒加载。
播放状态与进度经 :class:`~app.core.events.ReadAloudStateEvent` 发布，
无通知栏——播放控制由 Flet 迷你播放条（视图层）承担（§1.1）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable

from app.core.events import ReadAloudStateEvent
from app.core.lifecycle import Service
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.engine.rule_engine_service import RuleEngineService

logger = logging.getLogger(__name__)

# HTTP-TTS 取音频流的 fetcher 签名：(url, headers) -> bytes（协程）。
Fetcher = Callable[[str, dict], Awaitable[bytes]]


class TtsBackend:
    """TTS 后端抽象基类（§9.2）。

    子类约定：

    - ``name``：后端类型标识（system/edge/http），与 tts_profiles.engine_type 对齐；
    - ``available``：后端是否可用（系统 TTS 缺驱动时为 False，服务据此回退）；
    - ``state``：当前播放态字符串（idle/speaking/paused/error），供迷你播放条镜像。
    """

    name = "base"

    def __init__(self) -> None:
        self.state = "idle"
        self.available = False
        self.last_error: str | None = None

    async def speak(self, text: str, opts: dict[str, Any] | None = None) -> None:
        """朗读一段文本；实现应在开始时置 state='speaking'，结束后置回 idle。"""
        raise NotImplementedError

    async def stop(self) -> None:
        """停止当前朗读（默认仅复位状态，子类覆盖真实停音）。"""
        self.state = "idle"

    def is_speaking(self) -> bool:
        return self.state == "speaking"


class SystemTtsBackend(TtsBackend):
    """系统 TTS 后端（pyttsx3 桥：Win SAPI / macOS NSSpeech / Linux espeak）。

    pyttsx3 在无音频设备 / 无语音驱动环境下 ``pyttsx3.init()`` 会抛错——
    此时不向上抛，而是把 ``available`` 置 False、``state`` 置 error，
    由 :class:`ReadAloudService` 自动回退到 EdgeTtsBackend（契约 §5）。
    """

    name = "system"

    def __init__(self) -> None:
        super().__init__()
        self._engine = None
        try:  # 懒加载：仅本后端 import pyttsx3
            import pyttsx3  # type: ignore
            self._engine = pyttsx3.init()
            self.available = True
            self.state = "idle"
        except Exception as exc:  # 缺驱动/无音频设备：降级而非崩溃
            self.available = False
            self.state = "error"
            self.last_error = f"系统 TTS 不可用：{type(exc).__name__}: {exc}"
            logger.warning("SystemTtsBackend 加载失败，将回退 edge：%s", self.last_error)

    async def speak(self, text: str, opts: dict[str, Any] | None = None) -> None:
        if not self.available or self._engine is None:
            raise RuntimeError(self.last_error or "系统 TTS 后端不可用")
        opts = opts or {}
        self.state = "speaking"
        try:
            # pyttsx3.runAndWait 会阻塞调用线程，挪到工作线程避免卡事件循环。
            await asyncio.to_thread(self._run_blocking, str(text), opts)
        finally:
            self.state = "idle"

    def _run_blocking(self, text: str, opts: dict[str, Any]) -> None:
        rate = opts.get("rate")
        if rate:
            self._engine.setProperty("rate", float(rate))
        voice = opts.get("voice")
        if voice:
            self._engine.setProperty("voice", str(voice))
        self._engine.say(text)
        self._engine.runAndWait()

    async def stop(self) -> None:
        if self._engine is not None:
            try:
                await asyncio.to_thread(self._engine.stop)
            except Exception as exc:  # pragma: no cover - 平台差异容错
                logger.debug("系统 TTS stop 异常：%s", exc)
        self.state = "idle"


class EdgeTtsBackend(TtsBackend):
    """edge-tts 在线神经语音后端（系统 TTS 不可用时的回退路径）。

    edge-tts 是纯 Python aiohttp 客户端，三端可用。M4 完成"合成并落盘音频流"
    的链路验证；真实出声播放接 flet_audio 属 M5（服务零 flet，不直连控件）。
    """

    name = "edge"
    #: 默认中文语音（可被 opts["voice"] 覆盖）。
    DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

    def __init__(self) -> None:
        super().__init__()
        try:  # 懒加载：仅本后端 import edge_tts
            import edge_tts  # noqa: F401, type: ignore
            self._edge_tts = edge_tts
            self.available = True
            self.state = "idle"
        except Exception as exc:  # pragma: no cover - 离线/未装库时降级
            self.available = False
            self.state = "error"
            self.last_error = f"edge-tts 不可用：{type(exc).__name__}: {exc}"

    async def speak(self, text: str, opts: dict[str, Any] | None = None) -> None:
        if not self.available:
            raise RuntimeError(self.last_error or "edge-tts 后端不可用")
        opts = opts or {}
        voice = str(opts.get("voice") or self.DEFAULT_VOICE)
        self.state = "speaking"
        try:
            communicate = self._edge_tts.Communicate(str(text), voice)
            out_path = opts.get("audio_path")
            if out_path:  # 指定落盘路径（缓存/试听）
                await communicate.save(str(out_path))
            else:  # 未落盘路径：仅拉取字节，验证链路可通
                async for _chunk in communicate.stream():
                    pass
        finally:
            self.state = "idle"


class HttpTtsBackend(TtsBackend):
    """书源规则 HTTP-TTS 后端（走 RuleEngineService 网络栈）。

    ``speak`` 按 ``opts["url"]`` 模板（含 ``{text}`` 占位）拼出音频请求地址，
    经可注入 ``fetcher(url, headers) -> bytes`` 拉取音频流。fetcher 默认由
    :class:`ReadAloudService` 接到 RuleEngineService 的 fetch_media（会话复用/
    Range/超时一致）；测试注入 stub fetcher，不发真实网络请求。
    """

    name = "http"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self._fetcher = fetcher
        self.available = True  # 网络后端自身"可用"；请求失败由 speak 上抛
        #: 最近一次请求记录（测试/调试断言用）。
        self.last_request: dict[str, Any] | None = None

    async def speak(self, text: str, opts: dict[str, Any] | None = None) -> None:
        opts = opts or {}
        from urllib.parse import quote
        url_template = str(opts.get("url") or "")
        headers = dict(opts.get("headers") or {})
        # {text} 占位替换：URL 编码正文，避免空格/中文破坏请求行。
        url = url_template.replace("{text}", quote(str(text))) if url_template else ""
        if not url:
            raise ValueError("HttpTtsBackend 需要 opts['url'] 模板")
        self.state = "speaking"
        try:
            audio = b""
            if self._fetcher is not None:
                audio = await self._fetcher(url, headers)
            self.last_request = {
                "url": url, "headers": headers, "bytes": len(audio)}
        finally:
            self.state = "idle"


def _release(backends: dict[str, Any]) -> None:
    """在专用线程上丢掉后端引用（COM 对象的 `__del__` 会在这里阻塞，不影响引擎循环）。"""
    backends.clear()


class ReadAloudService(Service):
    """朗读服务：后端装配/切换、播放控制、tts_profiles 配置 CRUD。"""

    name = "ReadAloudService"

    # B4 拓扑声明：on_start 硬 require 的协作者（Storage/Settings/RuleEngine）。
    # 构造期仍可注入替身独立启动（dependencies 仅影响 hub 拓扑序，不影响直连 on_start）。
    dependencies: tuple[type[Service], ...] = (
        StorageService, SettingsService, RuleEngineService)

    def __init__(self, rule_engine=None, storage=None, settings=None,
                 cache=None, backends: dict[str, TtsBackend] | None = None) -> None:
        super().__init__()
        self._rule_engine = rule_engine
        self._storage = storage
        self._settings = settings
        self._cache = cache
        # 注入后端字典仅供测试/自定义装配；None 时 on_start 建默认 system/edge/http 链。
        self._backends_injected = backends
        self._backends: dict[str, TtsBackend] = {}
        self._active: TtsBackend | None = None
        # 当前朗读上下文（事件载荷快照）。
        self._book_url = ""
        self._chapter = 0
        self._position = 0.0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._settings is None and self.hub is not None:
            from app.core.settings import SettingsService
            self._settings = self.hub.require(SettingsService)
        if self._rule_engine is None and self.hub is not None:
            from app.services.engine.rule_engine_service import RuleEngineService
            self._rule_engine = self.hub.require(RuleEngineService)
        if self._backends_injected is not None:
            self._backends = dict(self._backends_injected)
        else:
            self._backends = self._build_default_backends()
        # 默认激活 system，不可用则在 _resolve_active 内自动回退 edge。
        self._active = self._resolve_active("system")
        logger.info("ReadAloudService 已启动，可用后端：%s",
                    [b.name for b in self._backends.values() if b.available])

    async def on_stop(self) -> None:
        try:
            await self.stop_speaking()
        except Exception:  # pragma: no cover - 收尾容错
            logger.debug("ReadAloudService stop_speaking 异常")
        self._active = None
        # 后端对象的**释放**不能在这条循环上做：pyttsx3 在 Windows 上是 COM
        # (SAPI) 对象，最后一个引用被丢掉时 comtypes 的 `_events.__del__` 会在
        # **当前线程**同步等它自己那条 COM 线程——实测把 EngineHub 引擎循环整个冻住，
        # 退出时 stop_all 卡到 10s 超时，后面所有服务都停不掉。
        # 摘下来交给一次性守护线程去丢引用，退出不等它（进程随即回收）。
        backends, self._backends = self._backends, {}
        threading.Thread(target=_release, args=(backends,),
                         name="tts-teardown", daemon=True).start()

    def _build_default_backends(self) -> dict[str, TtsBackend]:
        """装配默认后端链：system（可能降级）→ edge（在线回退）→ http。"""
        backends: dict[str, TtsBackend] = {}
        try:
            backends["system"] = SystemTtsBackend()
        except Exception as exc:  # pragma: no cover - 构造期即容错
            logger.warning("SystemTtsBackend 构造失败：%s", exc)
        try:
            backends["edge"] = EdgeTtsBackend()
        except Exception as exc:  # pragma: no cover
            logger.warning("EdgeTtsBackend 构造失败：%s", exc)
        backends["http"] = HttpTtsBackend(fetcher=self._default_http_fetcher)
        return backends

    def _resolve_active(self, engine_type: str | None) -> TtsBackend | None:
        """按 engine_type 选后端；请求后端不可用时自动回退（edge→http→system）。"""
        requested = self._backends.get(engine_type or "")
        if requested is not None and requested.available:
            return requested
        # 回退链：验收不绑定单一引擎（§8.4）。
        for fallback in ("edge", "http", "system"):
            if fallback == engine_type:
                continue
            cand = self._backends.get(fallback)
            if cand is not None and cand.available:
                logger.info("TTS 后端 %s 不可用，回退到 %s", engine_type, fallback)
                return cand
        return None

    # ------------------------------------------------------------------ #
    # 播放控制
    # ------------------------------------------------------------------ #
    async def speak(self, book_url: str, text: str,
                    opts: dict[str, Any] | None = None) -> None:
        """朗读一段文本：选后端 → 发 speaking 事件 → 后端出声 → 发 idle 事件。"""
        opts = dict(opts or {})
        self._book_url = str(book_url or "")
        self._chapter = int(opts.get("chapter", 0) or 0)
        backend = self._resolve_active(opts.get("engine_type"))
        if backend is None:
            self._publish_state("error")
            raise RuntimeError("无可用 TTS 后端（系统/edge/http 均不可用）")
        self._active = backend
        self._publish_state("speaking")
        try:
            await backend.speak(text, opts)
        except Exception as exc:
            backend.state = "error"
            self._publish_state("error")
            raise
        self._publish_state("idle")

    async def stop_speaking(self) -> None:
        """停止当前朗读并复位事件态。

        名字带 `_speaking` 是硬要求：`stop()` 会盖掉 Service 基类的生命周期
        `stop()`，hub 逆序停止时轮到它会只停播放、不迁移状态，服务永远停不掉
        （见 tests/test_core_lifecycle.py 的护栏）。后端 `TtsBackend.stop()` 不受影响
        ——它不是 Service 子类。
        """
        if self._active is not None:
            try:
                await self._active.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("TTS stop 异常：%s", exc)
        self._publish_state("idle")

    def set_voice(self, profile: dict[str, Any] | None) -> TtsBackend | None:
        """按 tts_profile 切换后端（engine_type 决定走哪个后端）。

        ``profile`` 为 tts_profiles 行快照（name/url/headers/engine_type）；
        返回被选中的后端（不可用回退后可能与请求 engine_type 不同）。
        """
        profile = profile or {}
        engine_type = str(profile.get("engine_type") or "system")
        backend = self._resolve_active(engine_type)
        if backend is not None:
            self._active = backend
        return backend

    @property
    def active_backend(self) -> TtsBackend | None:
        return self._active

    # ------------------------------------------------------------------ #
    # tts_profiles 配置 CRUD（schema v4）
    # ------------------------------------------------------------------ #
    async def list_profiles(self) -> list[dict[str, Any]]:
        conn = self._storage.db if self._storage is not None else None
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id, name, url, headers, engine_type FROM tts_profiles"
        ).fetchall()
        result = []
        for r in rows:
            try:
                headers = json.loads(r[3] or "{}")
            except (ValueError, TypeError):
                headers = {}
            result.append({
                "id": r[0], "name": r[1], "url": r[2],
                "headers": headers, "engine_type": r[4]})
        return result

    async def add_profile(self, name: str, url: str = "",
                          headers: dict | None = None,
                          engine_type: str = "system") -> int:
        """登记一条 TTS 配置，返回新行 id。"""
        conn = self._storage.db if self._storage is not None else None
        if conn is None:
            raise RuntimeError("StorageService 未启动，无法登记 tts_profile")
        payload = json.dumps(headers or {}, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO tts_profiles(name, url, headers, engine_type)"
            " VALUES(?,?,?,?)",
            (str(name), str(url), payload, str(engine_type)))
        return int(cur.lastrowid)

    async def remove_profile(self, profile_id: int) -> bool:
        conn = self._storage.db if self._storage is not None else None
        if conn is None:
            return False
        cur = conn.execute("DELETE FROM tts_profiles WHERE id=?", (int(profile_id),))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    async def _default_http_fetcher(self, url: str, headers: dict) -> bytes:
        """默认 HTTP-TTS 取流：走 RuleEngineService 网络栈 fetch_media。

        fetch_media(value, source, base_url, extra_headers) 内部处理 ``url,{opt}``
        形式、Range 续传与 UA/cookie，M4 直接复用其整包拉取。无 RuleEngineService
        （纯单测未注入）时上抛，由调用方注入自己的 fetcher。
        """
        engine = self._rule_engine
        if engine is None:
            raise RuntimeError("无 RuleEngineService，HttpTts 无法取流")
        real = getattr(engine, "_default_engine", None)
        real = real or getattr(engine, "_engine", None)
        fetch = getattr(real, "fetch_media", None)
        if not callable(fetch):
            # RuleEngineService 池化壳：取一个池化引擎再调 fetch_media。
            getter = getattr(engine, "_get_engine", None)
            if callable(getter):
                real = await getter(None)
                fetch = getattr(real, "fetch_media", None)
        if not callable(fetch):
            raise RuntimeError("RuleEngineService 未暴露 fetch_media")
        return await fetch(url, None, base_url=url, extra_headers=headers)

    def _publish_state(self, state: str, position: float | None = None) -> None:
        if self.bus is None:
            return
        self.bus.publish(ReadAloudStateEvent(
            state=state,
            book_url=self._book_url,
            chapter=self._chapter,
            position=self._position if position is None else position))
