"""M5 BrowserService / WebBridgeService 收编测试（契约 §15，离线 stub）。

不拉起真实 Chromium、不 import flet：
- BrowserService 的 headless/interactive/renderer 全部用注入替身；
- WebBridge 本地起 aiohttp server（127.0.0.1 临时端口）+ aiohttp client 请求，
  业务服务（SourceService/SourceCheckService/RuleEngineService）用 stub 替身。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.tools.browser_service import BrowserService
from app.services.tools.web_bridge_service import WebBridgeService


# ====================================================================== #
# 替身（stub）
# ====================================================================== #
class _FakeHeadless:
    """HeadlessBrowser 替身：记录调用，不碰 playwright。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def fetch(self, url, options=None) -> dict:
        self.calls.append((url, dict(options or {})))
        return {"body": f"<html>{url}</html>", "status": 200, "url": url}

    async def aclose(self) -> None:
        self.closed = True


class _FakeInteractive:
    """InteractiveBrowser 替身：记录 start/close。"""

    def __init__(self, on_frame=None):
        self.on_frame = on_frame
        self.started_url = None
        self.closed = False

    async def start(self, url, options=None) -> None:
        self.started_url = url

    async def close(self) -> None:
        self.closed = True


class _FakeRenderer:
    """WebPageRenderer 替身：记录 render/verification_code 调用。"""

    def __init__(self, page_provider=None, set_status=None):
        self.page_provider = page_provider
        self.render_calls: list[tuple] = []
        self.verification_calls: list = []

    async def render(self, url, source=None, options=None) -> dict:
        self.render_calls.append((url, source, options or {}))
        return {"body": "<html>gate-solved</html>", "status": 200, "url": url}

    async def verification_code(self, image, source=None) -> str:
        self.verification_calls.append((image, source))
        return "1234"


class _MemSettings:
    """SettingsService 内存替身。"""

    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _FakeSource:
    def to_dict(self):
        return {"bookSourceUrl": "http://demo", "bookSourceName": "demo"}


class _FakeSourceService:
    def __init__(self):
        self.saved: list = []

    async def list_sources(self):
        return [_FakeSource()]

    async def save_source(self, body):
        self.saved.append(body)
        return {"saved": True, "source_id": body.get("bookSourceUrl", "")}

    async def delete_source(self, url):
        return True


class _FakeCheckService:
    def __init__(self):
        self.last = None

    async def check_source(self, body):
        self.last = body
        return {"check_state": "fully_usable", "raw_url": body.get("bookSourceUrl", "")}

    async def check_all(self, groups=None):
        return {"total": 1, "usable": 1, "groups": groups}


class _FakeRuleEngine:
    def __init__(self):
        self.searched: list = []

    async def search(self, source, keyword, page=1):
        self.searched.append((keyword, page))
        return [{"name": f"book-{keyword}"}]


def _run(coro):
    return asyncio.run(coro)


# ====================================================================== #
# BrowserService：降级探测
# ====================================================================== #
def test_available_fetcher_none_when_kernel_missing():
    """playwright/Chromium 缺失（probe=False）：available_fetcher() 返回 None，不报错。"""
    svc = BrowserService(headless=_FakeHeadless(), chromium_probe=lambda: False)
    assert svc.available_fetcher() is None


def test_available_fetcher_fetcher_when_kernel_present():
    """内核就绪（probe=True）：返回 Legado 形状 fetcher，调用委托无头浏览器。"""
    fake = _FakeHeadless()
    svc = BrowserService(headless=fake, chromium_probe=lambda: True)
    fetcher = svc.available_fetcher()
    assert callable(fetcher)
    result = _run(fetcher("http://x", None, {"a": 1}))
    assert result["status"] == 200
    assert fake.calls and fake.calls[0][0] == "http://x"


# ====================================================================== #
# BrowserService：fetch 路由
# ====================================================================== #
def test_fetch_delegates_headless():
    fake = _FakeHeadless()
    svc = BrowserService(headless=fake, chromium_probe=lambda: True)
    out = _run(svc.fetch("http://a", {"webView": True}))
    assert out["url"] == "http://a"
    assert fake.calls[0][1].get("webView") is True


def test_fetch_merges_kwargs_into_options():
    fake = _FakeHeadless()
    svc = BrowserService(headless=fake, chromium_probe=lambda: True)
    _run(svc.fetch("http://b", {"a": 1}, b=2, c=None))
    opts = fake.calls[0][1]
    assert opts["a"] == 1 and opts["b"] == 2
    # None 值不污染 options
    assert "c" not in opts


# ====================================================================== #
# BrowserService：回调注册表（§4.6 唯一例外通道）
# ====================================================================== #
def test_callback_registry_register_and_clear():
    svc = BrowserService()
    sentinel = object()
    svc.register_interaction_callback(sentinel)
    assert svc.interaction_callback is sentinel
    svc.register_interaction_callback(None)
    assert svc.interaction_callback is None


# ====================================================================== #
# BrowserService：投屏会话池
# ====================================================================== #
def test_open_interactive_registers_session():
    created: list = []

    def factory(on_frame=None):
        h = _FakeInteractive(on_frame=on_frame)
        created.append(h)
        return h

    svc = BrowserService(interactive_factory=factory)
    handle = _run(svc.open_interactive("http://verify", on_frame=lambda b: None))
    assert handle.started_url == "http://verify"
    assert svc.session_count == 1
    # 会话句柄即返回给视图层的交互句柄
    assert handle is created[0]


def test_render_webview_delegates_renderer():
    renderer = _FakeRenderer()
    svc = BrowserService(
        renderer_factory=lambda page_provider, set_status: renderer,
        page_provider=lambda: object())
    out = _run(svc.render_webview("http://gate", source="src", options={"mode": "webJs"}))
    assert out["body"] == "<html>gate-solved</html>"
    assert renderer.render_calls[0][0] == "http://gate"
    assert renderer.render_calls[0][2].get("mode") == "webJs"


def test_verification_fetcher_delegates_renderer():
    renderer = _FakeRenderer()
    svc = BrowserService(
        renderer_factory=lambda page_provider, set_status: renderer,
        page_provider=lambda: object())
    code = _run(svc.verification_fetcher("image-bytes", "src"))
    assert code == "1234"
    assert renderer.verification_calls[0][1] == "src"


def test_on_stop_closes_sessions_and_headless():
    headless = _FakeHeadless()

    def factory(on_frame=None):
        return _FakeInteractive(on_frame=on_frame)

    svc = BrowserService(headless=headless, interactive_factory=factory)
    _run(svc.open_interactive("http://x"))
    assert svc.session_count == 1
    _run(svc.on_stop())
    assert headless.closed is True
    assert svc.session_count == 0


# ====================================================================== #
# WebBridgeService：开关默认关闭（不监听）
# ====================================================================== #
def test_webbridge_default_disabled_no_listener():
    mem = _MemSettings()  # webbridge.enabled 缺省 False
    svc = WebBridgeService(settings=mem, host="127.0.0.1", port=0, token="t")
    _run(svc.on_start())
    try:
        assert svc.listening is False
        assert svc.bound_port == 0
    finally:
        _run(svc.on_stop())


# ====================================================================== #
# WebBridgeService：开启后端点可访问 + 令牌校验（本地起 server + client 请求）
# ====================================================================== #
async def _start_enabled_bridge(token="secret"):
    """在当前事件循环内构造并启动网桥，返回 (svc, src, chk, eng)。"""
    mem = _MemSettings()
    mem.set("webbridge.enabled", True)
    src = _FakeSourceService()
    chk = _FakeCheckService()
    eng = _FakeRuleEngine()
    svc = WebBridgeService(settings=mem, source_service=src, source_check=chk,
                           rule_engine=eng, host="127.0.0.1", port=0, token=token)
    await svc.on_start()
    return svc, src, chk, eng


def test_webbridge_health_requires_and_accepts_token():
    import aiohttp

    async def scenario():
        svc, *_ = await _start_enabled_bridge()
        port = svc.bound_port
        assert port > 0
        try:
            async with aiohttp.ClientSession() as session:
                # 无令牌 → 401
                async with session.get(f"http://127.0.0.1:{port}/api/health") as r:
                    assert r.status == 401
                # 错误令牌 → 401
                async with session.get(
                        f"http://127.0.0.1:{port}/api/health",
                        headers={"Authorization": "Bearer wrong"}) as r:
                    assert r.status == 401
                # 正确令牌 → 200
                async with session.get(
                        f"http://127.0.0.1:{port}/api/health",
                        headers={"Authorization": "Bearer secret"}) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["ok"] is True
        finally:
            await svc.on_stop()

    _run(scenario())


def test_webbridge_delegates_source_service_stub():
    import aiohttp

    async def scenario():
        svc, src, _chk, _eng = await _start_enabled_bridge()
        port = svc.bound_port
        auth = {"Authorization": "Bearer secret"}
        try:
            async with aiohttp.ClientSession() as session:
                # GET 列表
                async with session.get(
                        f"http://127.0.0.1:{port}/api/sources",
                        headers=auth) as r:
                    assert r.status == 200
                    data = await r.json()
                    assert data["sources"][0]["bookSourceUrl"] == "http://demo"
                # POST 保存
                async with session.post(
                        f"http://127.0.0.1:{port}/api/sources",
                        json={"bookSourceUrl": "http://new"}, headers=auth) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["saved"] is True
            assert src.saved and src.saved[0]["bookSourceUrl"] == "http://new"
        finally:
            await svc.on_stop()

    _run(scenario())


def test_webbridge_check_and_debug_delegate():
    import aiohttp

    async def scenario():
        svc, _src, chk, eng = await _start_enabled_bridge()
        port = svc.bound_port
        auth = {"Authorization": "Bearer secret"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"http://127.0.0.1:{port}/api/sources/check",
                        json={"bookSourceUrl": "http://demo"}, headers=auth) as r:
                    assert r.status == 200
                    assert (await r.json())["check_state"] == "fully_usable"
                async with session.post(
                        f"http://127.0.0.1:{port}/api/sources/debug",
                        json={"source": "src", "action": "search",
                              "keyword": "小说"}, headers=auth) as r:
                    assert r.status == 200
                    data = await r.json()
                    assert data["result"][0]["name"] == "book-小说"
            assert eng.searched and eng.searched[0][0] == "小说"
        finally:
            await svc.on_stop()

    _run(scenario())


def test_webbridge_stop_closes_server():
    async def scenario():
        svc, *_ = await _start_enabled_bridge()
        assert svc.listening is True
        await svc.on_stop()
        assert svc.listening is False
        assert svc.bound_port == 0

    _run(scenario())
