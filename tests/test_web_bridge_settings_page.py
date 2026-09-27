"""网络桥设置页（#33）：服务向导 + 界面渲染，全部离线。

分两层：

- **服务侧**：真起 aiohttp（回环临时端口，见 :data:`wbs.DEFAULT_HOST`），只验设置页
  依赖的四个方法 —— 开关写库并收敛监听态、端口只有一条生效路径（换端口即换监听）、
  换令牌要让旧令牌当场失效、绑定失败不留半个监听。
- **界面侧**：服务用替身，验"动作 → 一帧新状态 → 渲染"，尤其**失败也要把状态读回来**
  （开关停在"已打开"而端口其实没听，只报一行错是最坑用户的）。

不 import 真实 SourceService/规则引擎：网桥的端点委托已由
``test_tools_browser_webbridge_m5.py`` 覆盖，本文件只管设置页这条链。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import aiohttp
import flet as ft

from app.core.registry import (DEST_TOOLS_WEB_BRIDGE, GROUP_CONFIG, NAV_META,
                               ViewRegistry)
from app.services.tools import web_bridge_service as wbs
from app.services.tools.web_bridge_service import (ENABLED_KEY, ENDPOINTS,
                                                   PORT_KEY, TOKEN_KEY,
                                                   WebBridgeService)


class _MemSettings:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _svc(settings=None, **kwargs) -> WebBridgeService:
    kwargs.setdefault("host", wbs.DEFAULT_HOST)
    kwargs.setdefault("port", 0)
    return WebBridgeService(settings=settings, **kwargs)


class _RestartSpy(WebBridgeService):
    """记录 stop/start 调用顺序：端口只在 start_server 时读一次，改端口必须换监听。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.restarts: list[str] = []

    async def stop_server(self) -> None:
        self.restarts.append("stop")
        await super().stop_server()

    async def start_server(self) -> None:
        self.restarts.append("start")
        await super().start_server()


# ====================================================================== #
# 1. 服务向导
# ====================================================================== #
class BridgeWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_on_binds_and_off_releases_the_port(self):
        mem = _MemSettings()
        svc = _svc(mem, token="t")
        try:
            status = await svc.set_enabled(True)
            self.assertIs(mem.get(ENABLED_KEY), True)
            self.assertTrue(status["listening"])
            self.assertGreater(status["port"], 0)
            # 界面照抄这一串就能访问——拼错的地址比没有地址更坏
            self.assertEqual(status["url"], f"http://{svc._host}:{status['port']}")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{status['url']}/api/health",
                        headers={"Authorization": "Bearer t"}) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertTrue((await resp.json())["ok"])

            status = await svc.set_enabled(False)
            self.assertIs(mem.get(ENABLED_KEY), False)
            self.assertFalse(status["listening"])
            self.assertEqual(status["port"], 0)
            self.assertEqual(status["url"], "")
        finally:
            await svc.on_stop()

    async def test_status_reading_never_generates_a_token(self):
        """读状态是幂等只读：刷新一次就生成并落一个令牌，等于每次刷新都在改设置。"""
        mem = _MemSettings()
        svc = _svc(mem)
        try:
            status = await svc.bridge_status()
            self.assertEqual(status["token"], "")
            self.assertNotIn(TOKEN_KEY, mem.d)
            # 开关一开才生成口令并写回
            status = await svc.set_enabled(True)
            self.assertTrue(status["token"])
            self.assertEqual(mem.get(TOKEN_KEY), status["token"])
        finally:
            await svc.on_stop()

    async def test_changing_port_restarts_the_listener(self):
        mem = _MemSettings()
        mem.set(ENABLED_KEY, True)
        svc = _RestartSpy(settings=mem, host=wbs.DEFAULT_HOST, port=0, token="t")
        try:
            await svc.start_server()
            del svc.restarts[:]
            status = await svc.set_port(0)   # 0 = 交给系统重新分配
            self.assertEqual(svc.restarts, ["stop", "start"])
            self.assertTrue(status["listening"])
            self.assertGreater(status["port"], 0)
        finally:
            await svc.on_stop()

    async def test_junk_port_rejected_before_touching_settings(self):
        mem = _MemSettings()
        mem.set(PORT_KEY, 28300)
        svc = _svc(mem, token="t")
        for bad in ("自动", "0x1", 70000, -1):
            with self.assertRaises(ValueError) as ctx:
                await svc.set_port(bad)
            self.assertIn("端口", str(ctx.exception))
        self.assertEqual(mem.get(PORT_KEY), 28300)   # 校验不过就不该落库

    async def test_regenerated_token_replaces_the_one_in_use(self):
        """构造注入的口令优先级高于设置表：换新时必须让位，否则新口令写了库、
        校验还在读旧的（用户视角：换了令牌却"没生效"）。"""
        mem = _MemSettings()
        mem.set(TOKEN_KEY, "stale-from-settings")
        svc = _svc(mem, token="override-token")
        try:
            await svc.set_enabled(True)
            url = (await svc.bridge_status())["url"]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{url}/api/health",
                        headers={"Authorization": "Bearer override-token"}) as r:
                    self.assertEqual(r.status, 200)
                status = await svc.regenerate_token()
                fresh = status["token"]
                self.assertNotEqual(fresh, "override-token")
                self.assertEqual(mem.get(TOKEN_KEY), fresh)
                for stale in ("override-token", "stale-from-settings"):
                    async with session.get(
                            f"{url}/api/health",
                            headers={"Authorization": f"Bearer {stale}"}) as r:
                        self.assertEqual(r.status, 401, f"旧令牌 {stale} 仍可用")
                async with session.get(
                        f"{url}/api/health",
                        headers={"Authorization": f"Bearer {fresh}"}) as r:
                    self.assertEqual(r.status, 200)
        finally:
            await svc.on_stop()

    async def test_concurrent_starts_bind_only_once(self):
        """listening 要 runner+site 都在才算真；半途状态不能被第二次调用当成"没在听"。"""
        created: list = []
        real_runner = wbs.web.AppRunner

        class _Counting(real_runner):  # type: ignore[misc,valid-type]
            def __init__(self, app):
                super().__init__(app)
                created.append(self)

        svc = _svc(_MemSettings(), token="t")
        wbs.web.AppRunner = _Counting   # type: ignore[misc]
        try:
            await asyncio.gather(svc.start_server(), svc.start_server())
            self.assertTrue(svc.listening)
            self.assertEqual(len(created), 1, "第二次进入不该再绑一个 runner")
        finally:
            wbs.web.AppRunner = real_runner   # type: ignore[misc]
            await svc.on_stop()

    async def test_bind_failure_leaves_no_half_listener(self):
        real_start = wbs.web.TCPSite.start
        svc = _svc(_MemSettings(), token="t")

        async def _boom(self, *args, **kwargs):
            raise OSError(98, "address already in use")

        wbs.web.TCPSite.start = _boom    # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError) as ctx:
                await svc.set_enabled(True)
            self.assertIn("失败", str(ctx.exception))
            self.assertFalse(svc.listening)
            self.assertIsNone(svc._runner, "绑定失败要把刚 setup 的 runner 收掉")
            # 开关确实写下去了：界面上"开关开着但没监听"就是靠这帧状态说清真相
            status = await svc.bridge_status()
            self.assertTrue(status["enabled"])
            self.assertFalse(status["listening"])
        finally:
            wbs.web.TCPSite.start = real_start   # type: ignore[method-assign]
            await svc.on_stop()

    async def test_wizard_without_settings_says_why(self):
        """没有 SettingsService：开关/端口/令牌都无处持久化，只能明确报错。"""
        svc = _svc(None, token="t")
        try:
            for awaited in (svc.set_enabled(True), svc.set_port(1234)):
                with self.assertRaises(RuntimeError) as ctx:
                    await awaited
                self.assertIn("SettingsService", str(ctx.exception))
            with self.assertRaises(RuntimeError) as ctx:
                await svc.regenerate_token()
            self.assertIn("SettingsService", str(ctx.exception))
        finally:
            await svc.on_stop()


# ====================================================================== #
# 2. 界面替身
# ====================================================================== #
class _FakeBridge:
    """WebBridgeService 替身：动作改状态，状态只有一帧（和真服务一样返回 dict）。"""

    def __init__(self, status: dict | None = None, errors: dict | None = None):
        self.status = {
            "enabled": False, "listening": False, "host": "host", "port": 0,
            "url": "", "token": "", "endpoints": list(ENDPOINTS),
        }
        if status:
            self.status.update(status)
        self.errors = errors or {}
        self.calls: list = []

    async def bridge_status(self) -> dict:
        return dict(self.status)

    def _act(self, name, *args):
        self.calls.append((name, *args))

    def _fail(self, name):
        # 真服务是"先落库再收敛监听态"，所以失败时设置已经改了——替身也要这样，
        # 才能验出界面在失败后仍把"开关开着、端口没听"讲清楚。
        if name in self.errors:
            raise self.errors[name]

    async def set_enabled(self, enabled) -> dict:
        self._act("set_enabled", bool(enabled))
        self.status["enabled"] = bool(enabled)
        self._fail("set_enabled")
        return dict(self.status)

    async def set_port(self, port) -> dict:
        self._act("set_port", port)
        self._fail("set_port")
        return dict(self.status)

    async def regenerate_token(self) -> dict:
        self._act("regenerate_token")
        self.status["token"] = "fresh-token"
        self._fail("regenerate_token")
        return dict(self.status)

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]


class _FakeHub:
    def __init__(self, services: dict):
        self._services = services
        self.bus = None

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


class _FakeBus:
    def subscribe(self, event_type, handler):
        return lambda: None

    def publish(self, event):
        return []


def _attach(fake):
    """装配设置页；services 传 None 模拟"服务未注册"。"""
    from app.controllers.tools.web_bridge_controller import WebBridgeController
    from app.views.tools.web_bridge_view import WebBridgeView
    hub = _FakeHub({WebBridgeService: fake} if fake is not None else {})
    view = WebBridgeView()
    controller = WebBridgeController(hub)
    controller._bus = _FakeBus()
    controller.attach(view)
    return controller, view


async def _wait(predicate, timeout: float = 5.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时：{what}")


def _log(view) -> str:
    return str(view._log.value or "")


def _state(view) -> str:
    return str(view._state.value or "")


class WebBridgeUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_constructs_and_exposes_slots(self):
        from app.views.tools.web_bridge_view import WebBridgeView
        view = WebBridgeView()
        self.assertIsInstance(view, ft.Column)
        for slot in ("on_toggle_enabled", "on_save_port", "on_regenerate",
                     "on_refresh"):
            self.assertIsNone(getattr(view, slot), slot)

    async def test_attach_reads_one_frame_and_renders_it(self):
        fake = _FakeBridge({"enabled": True, "listening": True, "port": 28300,
                            "host": "host", "url": "http://host:28300",
                            "token": "abc"})
        _controller, view = _attach(fake)
        await _wait(lambda: "28300" in str(view._url_label.value or ""),
                    what="地址渲染")
        self.assertIs(view._switch.value, True)
        self.assertEqual(view._port.value, "28300")
        self.assertEqual(view._token.value, "abc")
        self.assertIn("正在监听", _state(view))
        # 端点清单来自服务侧 ENDPOINTS，界面不自己抄一份
        self.assertEqual(len(view._endpoints.controls), len(ENDPOINTS))
        # 刷新只读不写：attach 里那次 refresh 不该产生任何动作调用
        self.assertEqual(fake.calls, [])

    async def test_switch_drives_the_service_and_the_log(self):
        fake = _FakeBridge()
        _controller, view = _attach(fake)
        await _wait(lambda: "未启用" in _state(view), what="首帧状态")
        # 用户拨开关：控件值先变，on_change 才带着新值派发（和真点击同序）
        view._switch.value = True
        view._switch.on_change(SimpleNamespace(control=view._switch))
        await _wait(lambda: fake.calls == [("set_enabled", True)], what="开关派发")
        await _wait(lambda: "已启用" in _log(view), what="成功日志")
        self.assertIs(view._switch.value, True)

    async def test_port_and_token_actions_delegate_to_the_wizard(self):
        fake = _FakeBridge({"enabled": True, "port": 28300})
        controller, view = _attach(fake)
        await _wait(lambda: "28300" in str(view._port.value or ""), what="首帧")
        view._save_port_btn.on_click(None)
        await _wait(lambda: ("set_port", "28300") in fake.calls, what="保存端口")
        await _wait(lambda: "端口已保存" in _log(view), what="端口日志")
        view._regen_btn.on_click(None)
        await _wait(lambda: "regenerate_token" in fake.actions(), what="换令牌")
        await _wait(lambda: "已换新令牌" in _log(view), what="令牌日志")
        self.assertEqual(view._token.value, "fresh-token")
        self.assertNotIn("refresh", fake.actions())
        del controller

    async def test_failure_still_renders_the_truth(self):
        """服务报错时状态照读：开关停在"已打开"、端口没监听，两件事都要说出来。"""
        fake = _FakeBridge({"port": 28300, "host": "host",
                            "url": "http://host:28300"},
                           errors={"set_enabled": RuntimeError("监听被占用")})
        _controller, view = _attach(fake)
        await _wait(lambda: "未启用" in _state(view), what="首帧")
        view._switch.value = True
        view._switch.on_change(SimpleNamespace(control=view._switch))
        await _wait(lambda: "失败" in _log(view), what="失败日志")
        self.assertIn("监听被占用", _log(view))
        # 状态没被报错吞掉：开关回落到设置里的真值，并说明其实没监听
        self.assertIs(view._switch.value, True)
        self.assertIn("端口没有监听成功", _state(view))

    async def test_missing_service_reports_instead_of_freezing(self):
        _controller, view = _attach(None)
        await _wait(lambda: "未就绪" in _log(view), what="未就绪提示")
        self.assertIn("WebBridgeService", _log(view))

    async def test_detach_leaves_nothing_behind(self):
        controller, view = _attach(_FakeBridge())
        await _wait(lambda: view._state.value is not None, what="首帧")
        controller.detach()
        self.assertIsNone(controller._view)
        self.assertEqual(controller._subs, [])


# ====================================================================== #
# 3. 路由与入口
# ====================================================================== #
class WebBridgeWiringTests(unittest.TestCase):
    def test_nav_meta_marks_it_a_hidden_config_page(self):
        self.assertEqual(NAV_META[DEST_TOOLS_WEB_BRIDGE],
                         (GROUP_CONFIG, "网络桥", True))

    def test_registered_route_builds_the_view(self):
        from app.views.tools import register_destinations
        from app.views.tools.web_bridge_view import WebBridgeView
        registry = ViewRegistry()
        register_destinations(registry, _FakeHub({}))
        route = registry.try_get(DEST_TOOLS_WEB_BRIDGE)
        self.assertIsNotNone(route)
        self.assertTrue(route.hidden)
        self.assertIsInstance(route.factory(), WebBridgeView)

    def test_overview_has_a_card_for_it(self):
        from app.views.tools.overview_view import _TOOLS
        self.assertIn(DEST_TOOLS_WEB_BRIDGE, [card[0] for card in _TOOLS])

    def test_tools_view_package_does_not_import_services(self):
        """新视图仍守 §7.5：状态以 dict 注入，不碰 app.services.*。"""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "app" / "views" / "tools" / "web_bridge_view.py").read_text(
            encoding="utf-8-sig")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("app.services"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("app.services"))


if __name__ == "__main__":
    unittest.main()
