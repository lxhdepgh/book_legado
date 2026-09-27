"""内置浏览器能渲染网页（本轮修复）。

用户报「内置浏览器无法渲染网页」。查下来内核是好的（playwright 1.63 + chromium
已下载，投屏会话实测出帧），断的是**接线**：``BrowserController.navigate`` 只调
``BrowserService.fetch``（无头取 DOM 字符串）就把返回值丢掉，视图的渲染区
``_page_host`` 从没被写过；``open_interactive`` / ``render_webview`` /
``set_page_provider`` 在 app/** 里零调用者。顺带 RuleEngineService 在 main.py 里
被无参构造，webView / @webjs / 403 回退分支全程拿不到 fetcher。

本文件盯三处：控制器把会话开起来并把帧/手势接回视图、服务按会话号管投屏会话
（内核句柄绝不跨循环交给视图层）、装配根把 fetcher 与 page 注进服务。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import flet as ft

from app.controllers.tools.browser_controller import BrowserController
from app.services.tools.browser_service import BrowserService
from app.views.tools.browser_view import BrowserView


class _FakeService:
    """BrowserService 会话面的替身：只记调用，不碰内核。"""

    def __init__(self, fail: Exception | None = None):
        self.calls: list = []
        self.frame_sinks: list = []
        self.fail = fail
        self.sid = "s1"

    async def open_page(self, url, on_frame=None, options=None):
        self.calls.append(("open", url))
        self.frame_sinks.append(on_frame)
        if self.fail is not None:
            raise self.fail
        return self.sid

    async def navigate_page(self, sid, url):
        self.calls.append(("goto", sid, url))
        if self.fail is not None:
            raise self.fail

    async def cast_viewport(self, sid):
        self.calls.append(("viewport", sid))
        return (412, 500)

    async def cast_gesture(self, sid, action, *args):
        self.calls.append(("gesture", action) + tuple(args))

    async def close_page(self, sid):
        self.calls.append(("close", sid))

    def fetch(self, *a, **kw):            # 断链回归哨兵：不该再被调用
        self.calls.append(("fetch",))
        raise AssertionError("navigate 不该走无头 fetch（返回值没人渲染）")


class _RecordingView:
    """BrowserView 替身：记渲染动作。"""

    def __init__(self):
        self.events: list = []
        self.frames: list = []

    def _rec(self, kind, *args):
        self.events.append((kind,) + args)

    def set_address(self, url):
        self._rec("address", url)

    def set_loading(self, loading, text=""):
        self._rec("loading", loading, text)

    def set_status(self, text):
        self._rec("status", text)

    def set_nav_enabled(self, can_back, can_forward):
        self._rec("nav", can_back, can_forward)

    def begin_cast(self):
        self._rec("begin_cast")

    def end_cast(self, message=""):
        self._rec("end_cast", message)

    def render_frame(self, data):
        self.frames.append(data)

    def set_cast_size(self, width, height):
        self._rec("cast_size", width, height)


def _attach(service):
    """装配控制器；``ui_call`` 换成登记表，由用例自己在本循环上跑。

    真环境里编排协程排在**界面循环**上；测试没有界面循环，``ui_call`` 会退到真实
    EngineHub 守护线程，替身里的会话状态就跨循环了。顺带盯住"每次动作到底派了几个
    任务"。
    """
    view = _RecordingView()
    controller = BrowserController(SimpleNamespace(bus=None))   # type: ignore[arg-type]
    # 走真的 _safe_service 通道：把服务定位换成替身（attach 里会取一次）
    controller.service = lambda stype: service
    scheduled: list = []
    controller.ui_call = scheduled.append                     # type: ignore[assignment]
    controller.flush = lambda: None
    controller.attach(view)
    return controller, view, scheduled


async def drain(scheduled: list, limit: int = 30) -> None:
    """把排出来的编排协程按序跑完（协程里还可能再排，故循环取）。"""
    rounds = 0
    while scheduled and rounds < limit:
        await scheduled.pop(0)
        rounds += 1


class BrowserControllerTests(unittest.TestCase):
    def test_attach_wires_every_slot(self) -> None:
        """九个回调槽全接上：少一个就是"按钮点了没反应"。"""
        controller, view, scheduled = _attach(_FakeService())
        del view, scheduled
        for slot in ("on_navigate", "on_back", "on_forward", "on_reload",
                     "on_close", "on_cast_tap", "on_cast_scroll", "on_cast_pan",
                     "on_cast_type"):
            self.assertIsNotNone(getattr(controller._view, slot), f"{slot} 没接")


class BrowserRenderChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_opens_cast_session_and_paints(self) -> None:
        controller, view, scheduled = _attach(_FakeService())
        controller.navigate("example.com")       # 裸域名也要补协议
        await drain(scheduled)
        service = controller._browser
        self.assertEqual(service.calls[0], ("open", "https://example.com"))
        self.assertIn(("viewport", "s1"), service.calls)
        self.assertIn(("begin_cast",), view.events)
        self.assertIn(("cast_size", 412, 500), view.events)
        self.assertIn(("address", "https://example.com"), view.events)
        self.assertIn(("status", "加载完成"), view.events)

    async def test_second_navigate_reuses_the_session(self) -> None:
        """前进后退/再次访问不该重开一次内核。"""
        controller, _view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        controller.navigate("http://b")
        await drain(scheduled)
        calls = controller._browser.calls
        self.assertEqual([c[0] for c in calls].count("open"), 1)
        self.assertIn(("goto", "s1", "http://b"), calls)

    async def test_back_and_forward_drive_the_live_page(self) -> None:
        controller, _view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        controller.navigate("http://b")
        await drain(scheduled)
        controller.back()
        await drain(scheduled)
        self.assertEqual(controller._browser.calls[-1], ("goto", "s1", "http://a"))
        controller.forward()
        await drain(scheduled)
        self.assertEqual(controller._browser.calls[-1], ("goto", "s1", "http://b"))

    async def test_frames_are_marshalled_and_coalesced(self) -> None:
        """帧在引擎循环上回调：必须编组回界面循环，且只画最新一帧。"""
        controller, view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        scheduled.clear()
        controller._browser.frame_sinks[0](b"f1")
        controller._browser.frame_sinks[0](b"f2")     # 上一发还没画：合并
        self.assertEqual(len(scheduled), 1, "连续两帧只该排一次绘制")
        await drain(scheduled)
        self.assertEqual(view.frames, [b"f2"])

    async def test_gestures_run_fifo_in_one_pump(self) -> None:
        """滑块拖动靠 press→move→release 的次序：手势必须串行按序发出。"""
        controller, _view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        scheduled.clear()
        controller._view.on_cast_pan("press", 1, 2)
        controller._view.on_cast_pan("move_to", 3, 4)
        controller._view.on_cast_pan("release", 0, 0)
        controller._view.on_cast_tap(5, 6, "left")
        self.assertEqual(len(scheduled), 1, "一波手势只派一个泵")
        await drain(scheduled)
        self.assertEqual(controller._browser.calls[-4:], [
            ("gesture", "press", 1, 2), ("gesture", "move_to", 3, 4),
            ("gesture", "release", 0, 0), ("gesture", "tap", 5, 6, "left")])

    async def test_detach_closes_the_session(self) -> None:
        """切页必须关掉有头会话，否则内核窗口没人回收。"""
        controller, _view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        controller.detach()
        self.assertTrue(scheduled, "detach 该排一次关闭")
        await drain(scheduled)
        self.assertIn(("close", "s1"), controller._browser.calls)
        self.assertIsNone(controller._sid)

    async def test_close_button_clears_the_surface(self) -> None:
        controller, view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        controller.close_page()
        await drain(scheduled)
        self.assertIn(("end_cast", "页面已关闭"), view.events)
        self.assertIn(("close", "s1"), controller._browser.calls)

    async def test_kernel_missing_says_how_to_install_it(self) -> None:
        """内核不在场：报错要说人话，并把渲染区换成可执行的提示。"""
        boom = RuntimeError("Chromium 内核不可用，请执行 python -m playwright install chromium")
        controller, view, scheduled = _attach(_FakeService(fail=boom))
        controller.navigate("http://a")
        await drain(scheduled)
        statuses = [e[1] for e in view.events if e[0] == "status"]
        self.assertTrue(any("加载失败" in s for s in statuses), str(statuses))
        casts = [e for e in view.events if e[0] == "end_cast"]
        self.assertTrue(casts and "playwright" in casts[0][1], str(view.events))

    async def test_no_view_means_no_frame_painting(self) -> None:
        controller, _view, scheduled = _attach(_FakeService())
        controller.navigate("http://a")
        await drain(scheduled)
        controller.detach()
        await drain(scheduled)
        scheduled.clear()
        controller._browser.frame_sinks[0](b"late")   # 迟到帧：视图没了就不画
        self.assertEqual(scheduled, [])


class BrowserServiceSessionTests(unittest.IsolatedAsyncioTestCase):
    """服务侧会话池：跨循环只传会话号，失败不留僵尸内核。"""

    class _Handle:
        def __init__(self, on_frame=None):
            self.on_frame = on_frame
            self.calls: list = []
            self.closed = 0

        async def start(self, url, options):
            self.calls.append(("start", url, options))

        async def goto(self, url):
            self.calls.append(("goto", url))

        async def tap(self, x, y, button="left"):
            self.calls.append(("tap", x, y, button))

        async def type_text(self, text):
            self.calls.append(("type_text", text))

        def cast_viewport(self):
            return (400, 480)

        async def close(self):
            self.closed += 1

    def _service(self, handle=None, boom=None):
        handles: list = []

        def factory(on_frame=None):
            target = boom() if boom is not None else (handle or self._Handle())
            handles.append(target)
            return target

        svc = BrowserService(interactive_factory=factory)
        svc.hub = None
        return svc, handles

    async def test_open_page_returns_sid_and_pools_handle(self) -> None:
        svc, handles = self._service()
        sid = await svc.open_page("example.com/a", on_frame=lambda d: None)
        self.assertEqual(svc.session_count, 1)
        self.assertEqual(handles[0].calls[0][1], "https://example.com/a")
        await svc.navigate_page(sid, "b")
        self.assertEqual(handles[0].calls[1], ("goto", "https://b"))

    async def test_failed_start_closes_handle(self) -> None:
        """地址写错时内核已经起来了：不关掉就是没人管的 Chromium 窗口。"""
        boom = self._Handle
        handle = None

        class Broken(boom):
            async def start(self, url, options):
                raise RuntimeError("net down")

        svc, handles = self._service(handle=Broken(), )
        with self.assertRaises(RuntimeError):
            await svc.open_page("http://a")
        self.assertEqual(svc.session_count, 0)
        self.assertEqual(handles[0].closed, 1)

    async def test_gesture_whitelist_and_unknown_session(self) -> None:
        svc, handles = self._service()
        sid = await svc.open_page("http://a")
        await svc.cast_gesture(sid, "tap", 1, 2, "left")
        self.assertEqual(handles[0].calls[-1], ("tap", 1, 2, "left"))
        with self.assertRaises(ValueError):
            await svc.cast_gesture(sid, "rm_rf", 0)
        with self.assertRaises(RuntimeError):
            await svc.cast_gesture("no-such-session", "tap", 0, 0)
        self.assertEqual(await svc.cast_viewport(sid), (400, 480))

    async def test_close_page_twice_is_safe(self) -> None:
        svc, handles = self._service()
        sid = await svc.open_page("http://a")
        await svc.close_page(sid)
        await svc.close_page(sid)
        self.assertEqual(handles[0].closed, 1)
        self.assertEqual(svc.session_count, 0)


class BrowserViewSurfaceTests(unittest.TestCase):
    """视图面：投屏控件真在树上，坐标换算按内核回报的尺寸走。"""

    def test_cast_surface_mounts_on_begin_cast(self) -> None:
        view = BrowserView()
        self.assertIsInstance(view._page_host.content, ft.Text)
        view.begin_cast()
        content = view._page_host.content
        self.assertIsInstance(content, ft.Column)
        self.assertIs(content.controls[0], view._cast)
        self.assertTrue(view._close_btn.visible)

    def test_frame_renders_as_data_uri(self) -> None:
        view = BrowserView()
        view.begin_cast()
        view.render_frame(b"\xff\xd8jpeg")
        self.assertTrue(str(view._frame.src).startswith("data:image/jpeg;base64,"))

    def test_tap_scales_to_cast_viewport(self) -> None:
        view = BrowserView()
        seen: list = []
        view.on_cast_tap = lambda *a: seen.append(a)
        view.set_cast_size(800, 960)          # 内核比控件大一倍
        event = SimpleNamespace(local_position=SimpleNamespace(x=100, y=50))
        view._tap(event, "left")
        self.assertEqual(seen, [(200.0, 100.0, "left")])


class AssemblyWiringTests(unittest.TestCase):
    """装配根：引擎要拿到**注册在册的那一份** BrowserService。

    ``RuleEngineService`` 此前在 main.py 里被无参构造，webView / @webjs / 403 回退
    分支全程没有 fetcher；page_provider 也曾没人注入（``_ensure_renderer`` 一调就
    抛）。这里盯两条：fetcher 绑定的是同一个实例，以及内核缺失时降级为 (None, None)。
    """

    class _Stub:
        def __init__(self, available):
            self._available = available

        def available_fetcher(self):
            return (lambda url, source=None, options=None: None) if self._available else None

        async def render_webview(self, url, source=None, options=None):
            return {}

        async def verification_fetcher(self, image, source=None):
            return ""

    def test_fetchers_bind_the_same_service_instance(self) -> None:
        import main

        stub = self._Stub(available=True)
        webview, verification = main._engine_browser_fetchers(stub)
        self.assertIs(webview.__self__, stub)
        self.assertIs(verification.__self__, stub)

    def test_missing_kernel_degrades_to_no_fetchers(self) -> None:
        """没内核就别弹窗：引擎收到 None 才走纯 HTTP 路径。"""
        import main

        self.assertEqual(main._engine_browser_fetchers(self._Stub(available=False)),
                         (None, None))

    def test_hub_wires_browser_into_the_engine(self) -> None:
        """整盘装配：注册表里的 BrowserService 就是引擎 fetcher 的那一份。

        只构造不启动（服务协程会拉起真内核），收尾也不 shutdown——EngineHub 是
        进程级单例，拆掉它会影响同进程后面的用例。
        """
        import main

        from app.services.engine.rule_engine_service import RuleEngineService
        from app.services.tools.browser_service import BrowserService

        hub = main._build_hub()
        browser = hub.require(BrowserService)
        engine_service = hub.require(RuleEngineService)
        if browser.available_fetcher() is None:          # 本机无内核：只验降级
            self.assertIsNone(engine_service.webview_fetcher)
            self.assertIsNone(engine_service.verification_fetcher)
            return
        self.assertIs(engine_service.webview_fetcher.__self__, browser)
        self.assertIs(engine_service.verification_fetcher.__self__, browser)

    def test_page_provider_reaches_the_registered_service(self) -> None:
        """page 注入走的是服务表里那一份，不是临时 new 的对象。"""
        import main

        from app.services.tools.browser_service import BrowserService

        class _Hub:
            def __init__(self):
                self.service = BrowserService(chromium_probe=lambda: False)

            def require(self, stype):
                return self.service if stype is BrowserService else None

        hub = _Hub()
        page = object()
        main._wire_browser_page(hub, page)
        self.assertIs(hub.service._page_provider(), page)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
