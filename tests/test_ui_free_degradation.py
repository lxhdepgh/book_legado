"""§8 R3：四个 UI 耦合服务的"无 UI 也能启动"降级路径。

设计文档给这一条的判据只有一句话——"G0 里先给这 4 个加'无 UI 也能启动'的降级路径并
测试，迁移时才有落点"。这一文件钉的就是那 4 个在**没有窗口、没有 flet** 的进程里各自
该怎么表现：

| 服务 | 无 UI 时的行为 | 钉在哪 |
| --- | --- | --- |
| BrowserService | 静默内核答得出来的当场答；答不了的推给远端 WebView 宿主；两头都没有就 ``WebviewUnavailable`` 当场失败 | ``UiFreeBrowserTests`` |
| MediaPlayService | 会话、位置落库、状态事件照旧，一个本机播放器都不叫 | ``UiFreeMediaPlayTests`` |
| BossKeyService | 够不着窗口就不藏，且不留下那颗点了没窗口可回的托盘墓碑 | ``UiFreeBossKeyTests`` |
| WebPageRenderer | 根本不被 import（它顶部 ``import flet``，只属于有界面的那条分支）；整条无头装配 RUNNING、``sys.modules`` 里没有 flet | ``HeadlessAssemblyTests`` |

规矩照旧：每个"没发生"的断言都配一根明知会发生的对照（记忆
``zero-counts-need-a-positive-control``）——推远端那条先证同一形状有宿主时真回得来，
"没有 flet"那条先证模块数 >100（否则只是什么都没加载），"不 spawn"那条先证扫描器在
真 ``import os`` 的文件上扫得出来。全程离线：假内核、假窗口、假 page，不起 Chromium、
不访问任何真站点。
"""
from __future__ import annotations

import ast
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.core.events import MediaPlayStateEvent
from app.core.lifecycle import ServiceState
from app.core.storage import StorageService
from app.services.app_paths import AppPaths
from app.services.content.media_play import (
    HeadlessVideoBackend, MediaPlayService)
from app.services.gateway.service import WebviewUnavailable
from app.services.tools.boss_key_service import BossKeyService
from app.services.tools.browser_service import BrowserService
from app.services.tools.win_tray import WinTray

ROOT = Path(__file__).resolve().parents[1]

ARTICLE = "<html><body><div id=txtcontent>" + "正文" * 40 + "</div></body></html>"
# 渲染完还是一堵墙：登录表单是 looks_like_gate 认的第二类（第一类是拦截页标记）。
LOGIN_GATE = '<html><body><form><input type="password" name="pwd"></form></body></html>'


class _Kernel:
    """假无头内核：只回备好的响应，并记自己被要了几次。"""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls = 0

    async def fetch(self, url: str, options: dict) -> dict:
        self.calls += 1
        return {"body": self.body, "headers": {"content-type": "text/html"},
                "status": 200, "url": url}

    async def aclose(self) -> None:      # on_stop 统一回收，替身得接得住
        pass


class _Bus:
    """记录型总线：这里要断言的是"服务发了哪些状态"，不是总线怎么投递。"""

    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> list:
        self.events.append(event)
        return []


class UiFreeBrowserTests(unittest.IsolatedAsyncioTestCase):
    """BrowserService：没有 page_provider 时 render_webview 这条路走得通、也炸得明白。"""

    def _service(self, body: str = ARTICLE, **kwargs) -> BrowserService:
        return BrowserService(headless=_Kernel(body),
                              chromium_probe=lambda: True, **kwargs)

    async def test_silent_kernel_answers_without_a_page(self) -> None:
        asked: list = []

        async def host(url, source, options):
            asked.append(url)
            return {"body": "宿主的答案"}

        svc = self._service(remote_webview=host)
        out = await svc.render_webview("https://loopback.test/ch/1", None, {})
        self.assertEqual(out["body"], ARTICLE)
        self.assertEqual(asked, [], "静默内核答得出来的书不该白跑一趟远端宿主")
        self.assertIsNone(svc._renderer,
                          "无头路径上构造了界面侧 renderer——那条链会 import flet")
        # 正对照：同一段代码、同一份替身，只把载荷换成"墙"，上面那条 asked 就非空
        # （见下一条），所以这里不是"根本没跑"。

    async def test_a_gate_is_pushed_to_the_remote_host(self) -> None:
        seen: list = []

        async def host(url, source, options):
            seen.append((url, source, options))
            return {"body": "<p>宿主渲染的正文</p>", "headers": {}, "status": 200,
                    "url": url}

        svc = self._service(LOGIN_GATE, remote_webview=host)
        out = await svc.render_webview("https://loopback.test/ch/2", "src",
                                       {"webView": True})
        self.assertEqual(out["body"], "<p>宿主渲染的正文</p>")
        self.assertEqual(seen, [("https://loopback.test/ch/2", "src",
                                 {"webView": True})],
                         "推给宿主的必须是 Legado 那三条参数（url, source, options）")

    async def test_no_host_and_no_kernel_fails_as_no_host(self) -> None:
        """两头都没人：要的是"当场知道没人答"，不是等满预算，也不是折成一句崩溃。"""
        svc = self._service(LOGIN_GATE)
        with self.assertRaises(WebviewUnavailable):
            await svc.render_webview("https://loopback.test/ch/3", None, {})

        # 内核压根不在场（playwright 没装）：同一条调用也不该去碰内核。
        kernel = _Kernel(LOGIN_GATE)
        absent = BrowserService(headless=kernel, chromium_probe=lambda: False)
        with self.assertRaises(WebviewUnavailable):
            await absent.render_webview("https://loopback.test/ch/4", None, {})
        self.assertEqual(kernel.calls, 0,
                         "探测说没内核还去 fetch——available_fetcher 那条老口径白降级了")

    async def test_captcha_needs_a_human_and_says_so(self) -> None:
        svc = self._service()
        with self.assertRaises(RuntimeError) as caught:
            await svc.verification_fetcher("image-bytes")
        self.assertIn("验证码", str(caught.exception))

        class _Dialog:
            async def verification_code(self, image, source=None) -> str:
                return "1a2b"

        with_ui = BrowserService(renderer_factory=lambda page, status: _Dialog())
        self.assertEqual(await with_ui.verification_fetcher("image-bytes"), "1a2b",
                         "有界面侧 renderer 时这条必须回得来（上面那条才不是白炸）")

    async def test_service_starts_and_stops_without_any_ui(self) -> None:
        svc = self._service()
        await svc.start()
        self.assertEqual(svc.status.state, ServiceState.RUNNING)
        await svc.stop()
        self.assertEqual(svc.status.state, ServiceState.DISPOSED)
        self.assertIsNone(svc.status.last_error)


class UiFreeMediaPlayTests(unittest.TestCase):
    """MediaPlayService：默认后端只记账。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="r3-media-"))
        self.storage = StorageService(AppPaths(data=self.root / "data",
                                               cache=self.root / "cache"))
        asyncio.run(self.storage.start())
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        asyncio.run(self.storage.stop())
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_backend_keeps_the_session_without_touching_a_player(self) -> None:
        bus = _Bus()
        svc = MediaPlayService(storage=self.storage)
        svc.bus = bus
        asyncio.run(svc.on_start())
        self.assertEqual(svc._backend.name, "none")
        self.assertIsInstance(svc._backend, HeadlessVideoBackend)

        chapters = [{"url": "https://loopback.test/a.mp4"},
                    {"url": "https://loopback.test/b.mp4"}]
        index, position = asyncio.run(svc.open_book("r3-video-book", chapters))
        self.assertEqual((index, position), (0, 0.0))
        asyncio.run(svc.play_chapter(1))
        asyncio.run(svc.seek(42.5))
        asyncio.run(svc.pause())
        asyncio.run(svc.stop_playback())

        row = self.storage.db.execute(
            "SELECT play_chapter_index, play_position FROM books WHERE book_url=?",
            ("r3-video-book",)).fetchone()
        self.assertIsNotNone(row, "无 UI 的会话也得把位置落库，否则「接着看」没依据")
        self.assertEqual((row[0], row[1]), (1, 42.5))
        self.assertEqual([e.state for e in bus.events],
                         ["idle", "playing", "playing", "paused", "idle"])
        self.assertTrue(all(isinstance(e, MediaPlayStateEvent) for e in bus.events),
                        "状态事件的载荷形状变了：客户端的播放条就接不上")

        # 正对照：换一根真会动的后端（视图层注入的那一类），同一条会话链路一模一样地跑，
        # 差别只在该后端记不记 open 的参数——证明上面不是"服务压根没被驱动"。
        # 另开一本书：共用一行 books 会把上面那次会话的位置覆盖掉。
        opened: list = []

        class _Spy(HeadlessVideoBackend):
            async def open(self, media_url, headers=None):
                opened.append(media_url)
                await super().open(media_url, headers)

        spy = MediaPlayService(storage=self.storage, backend=_Spy())
        asyncio.run(spy.on_start())
        asyncio.run(spy.open_book("r3-spy-book", chapters))
        asyncio.run(spy.play_chapter(0))
        self.assertEqual(opened, ["https://loopback.test/a.mp4"])

        again = MediaPlayService(storage=self.storage)
        asyncio.run(again.on_start())
        self.assertEqual(asyncio.run(again.open_book("r3-video-book", chapters)),
                         (1, 42.5))

    def test_the_module_imports_no_operating_system_handles(self) -> None:
        """「不开外部播放器」要能机械断言：整个模块连 os/subprocess/sys 都没 import。

        正对照：同一支扫描在真 import os 的 ``main.py`` 上扫得出来——扫不出东西的
        扫描器等于没仪器。
        """
        def imports_of(relative: str) -> list[str]:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"),
                             filename=relative)
            names: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.append(node.module or "")
            return names

        offenders = [name for name in imports_of("app/services/content/media_play.py")
                     if name in ("os", "subprocess", "sys")
                     or name.startswith(("os.", "subprocess.", "sys."))]
        self.assertEqual(offenders, [],
                         f"播放服务又能碰本机进程了：{offenders}")
        self.assertIn("os", imports_of("main.py"),
                      "扫描器连 main.py 的 import os 都认不出，上面那条等于没测")


class _NoWindow:
    """够不着的窗口：``visible`` 一写就抛（真机上是 page 已随窗口销毁）。"""

    def __setattr__(self, name, value) -> None:
        if name == "visible":
            raise RuntimeError("窗口已销毁")
        object.__setattr__(self, name, value)


class _FakeWindowPage:
    def __init__(self) -> None:
        self.visible: list[bool] = []
        self.broken = False

    @property
    def window(self):
        page = self
        if page.broken:
            return _NoWindow()

        class _W:
            def __setattr__(self, name, value):
                if name == "visible":
                    page.visible.append(bool(value))
                object.__setattr__(self, name, value)

            async def to_front(self) -> None:
                pass
        return _W()

    def update(self) -> None:
        pass


class _Tray(WinTray):
    """假托盘：只记挂上/摘下，不碰 pywin32（槽位仍走基类那套）。"""

    def __init__(self, tooltip: str = "", on_activate=None, **_kw) -> None:
        super().__init__(tooltip, on_activate)
        self.started = False
        self.stopped = False

    def start(self) -> bool:
        self.started = True
        self._ok = True
        return True

    def stop(self) -> None:
        self.stopped = True
        self._ok = False


class UiFreeBossKeyTests(unittest.TestCase):
    """BossKeyService：没有窗口就别说"藏好了"。"""

    def _service(self) -> tuple[BossKeyService, list]:
        trays: list[_Tray] = []
        service = BossKeyService(tray_factory=lambda tip, cb: (
            trays.append(_Tray(tip, cb)) or trays[-1]))
        return service, trays

    def test_hide_refuses_when_the_window_is_out_of_reach(self) -> None:
        svc, trays = self._service()
        self.assertFalse(svc.available)
        self.assertIn("窗口", svc.status_text)

        self.assertFalse(svc.hide(),
                         "没注入 page 却报「藏好了」——用户只会以为程序崩了直接强杀")
        self.assertFalse(svc.hidden,
                         "_hidden 一旦错置，handle_key 每拍直接 return，老板键失灵到重启")
        self.assertTrue(trays[0].stopped, "图标白挂了一颗没窗口可回的墓碑")
        self.assertIn("窗口", svc.error)

        # 正对照：窗口够得着时同一次 hide 必须真藏（否则上面那串 False 只是没跑起来）。
        page = _FakeWindowPage()
        svc.set_page_provider(lambda: page)
        self.assertTrue(svc.hide())
        self.assertTrue(svc.hidden)
        self.assertEqual(page.visible, [False])
        self.assertFalse(trays[-1].stopped, "藏成功了就不该把图标摘掉")

    def test_restore_clears_the_flag_even_when_the_window_is_gone(self) -> None:
        page = _FakeWindowPage()
        svc, trays = self._service()
        svc.set_page_provider(lambda: page)
        svc.hide()
        page.broken = True          # 藏起来之后窗口没了
        self.assertTrue(svc.restore())
        self.assertFalse(svc.hidden)
        self.assertTrue(trays[-1].stopped)


class HeadlessWiringTests(unittest.TestCase):
    """装配根那条线：无头入口把网关的**反向调用**交给注册在册的那份 BrowserService。

    只构造不启动（``start_all`` 会拉起真内核，本文件全程离线），同
    ``tests/test_browser_render_chain.py`` 的 ``AssemblyWiringTests`` 一个套路。
    这一条钉的是"落点真的接上了"：R1 那条竖切通了不等于产品里有人用它。
    """

    def test_serve_wires_the_gateway_reverse_call_into_the_registered_service(self) -> None:
        import main
        from app.services.tools.browser_service import BrowserService

        hub = main._build_hub()
        registered = hub.require(BrowserService)

        class _Gateway:
            async def request_web_content(self, url, source=None, options=None):
                return {}

        gateway = _Gateway()
        self.assertIsNone(registered._remote_webview,
                          "装配好的 BrowserService 本来不该带着远端宿主（桌面端注 page）")
        main._wire_headless_webview(hub, gateway)
        remote = registered._remote_webview
        self.assertIsNotNone(remote, "无头装配没把 WebView 口子接出去——"
                                     "R1 那条竖切在产品里就还是没人答")
        # 绑定的方法相等但不同一（bound method 每次取都新建），所以按"挂谁身上、
        # 是哪个函数"两段核，别用 assertIs 拿一个永假的条件当护栏。
        self.assertIs(remote.__self__, gateway, "接的不是这一只网关")
        self.assertIs(remote.__func__, _Gateway.request_web_content,
                      "接的不是 request_web_content（反向调用那只手）")

        # 正对照：接上之后，render_webview 真会走到这只手上（不是只把字段写了）。
        asked: list = []

        async def host(url, source=None, options=None):
            asked.append(url)
            return {"body": "宿主的答案", "headers": {}, "status": 200, "url": url}

        offline = BrowserService(headless=_Kernel(LOGIN_GATE),
                                 chromium_probe=lambda: True)
        offline.set_remote_webview(host)
        out = asyncio.run(offline.render_webview("https://loopback.test/wired", None, {}))
        self.assertEqual((out["body"], asked), ("宿主的答案",
                                                ["https://loopback.test/wired"]))

    def test_serve_walks_that_wiring_itself(self) -> None:
        """上一条是**手递** helper；这一条要 ``serve()`` 自己走去同一条线。

        "装配根忘了那一跳"（把 ``_wire_headless_webview(hub, gateway)`` 从 ``serve()``
        里删掉）在上一条里量不出来——那条用例自己调的 helper。这里从入口进：hub 换成
        事先建好的那一份，引擎 loop 换成一只 ``submit`` 必炸的替身，于是 ``serve()``
        在 ``start_all`` 之前就撞进它自己的失败分支并返回，除了"接线跑没跑过"，
        进程里什么都没真起来（不绑端口、不拉内核）。
        """
        import app.core.engine_worker as engine_worker
        import main
        from app.services.gateway.service import GatewayService
        from app.services.tools.browser_service import BrowserService

        hub = main._build_hub()
        registered = hub.require(BrowserService)
        self.addCleanup(setattr, main, "_build_hub", main._build_hub)
        self.addCleanup(setattr, engine_worker, "engine_hub", engine_worker.engine_hub)

        class _NoLoop:
            def submit(self, coro):
                coro.close()          # 没人 await 的协程别留 RuntimeWarning
                raise RuntimeError("护栏：不真起引擎 loop")

        # hub.shutdown() 会把 **进程级 EngineHub 单例**的守护 loop 停掉（shutdown 的
        # finally 里那句 self._engine.shutdown()），后面同进程的界面用例就全卡在
        # "等不到首帧"上——所以这条早退路径上的收尾只能记一笔、不能真收。
        # 这里没真起过任何服务，没什么需要收的。
        shut: list = []
        hub.shutdown = lambda: shut.append(1)

        main._build_hub = lambda: hub
        engine_worker.engine_hub = lambda: _NoLoop()
        rc = main.serve(["--port", "0"])

        # 非 0 是仪器接上的证据：这条路真的走到了"start_all 失败"那一段，
        # 而不是 serve() 在接线之前就返回了。
        self.assertEqual(rc, 1, "serve() 没走预期的早退分支，这条读数说明不了接线")
        self.assertEqual(shut, [1], "早退分支没叫 shutdown（那 loop 就被拆了）")
        remote = registered._remote_webview
        self.assertIsNotNone(remote, "serve() 没调用 _wire_headless_webview——"
                                     "无头进程里 WebView 规则照样没人答")
        self.assertIsInstance(remote.__self__, GatewayService, "接的不是这一只网关")


class HeadlessAssemblyTests(unittest.TestCase):
    """整条无头装配：UI 耦合服务照常 RUNNING，flet 一行都没被 import。

    跑在**子进程**里：本进程早就被别的测试把 flet 塞进 ``sys.modules`` 了，在这里查
    等于查仪器自己的历史。子进程只做装配 + 启动 + 收尾，不发请求（那是
    ``tests/test_gateway_inproc.py`` 那一族的事）。
    """

    SNIPPET = """
import asyncio, json, os, shutil, sys, tempfile
os.environ["LEGADO_BOOKSOURCE_LOG_LEVEL"] = "CRITICAL"
root = tempfile.mkdtemp(prefix="r3-assembly-")
os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = os.path.join(root, "data")
os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = os.path.join(root, "cache")
import main
from app.core.engine_worker import reset_ui_dispatch_counts, ui_dispatch_counts
hub = main._build_hub()
reset_ui_dispatch_counts()
try:
    asyncio.run(asyncio.wait_for(hub.start_all(), 120))
    report = {snap.service_id: snap.state.value for snap in hub.status_report()}
    print("R3REPORT " + json.dumps({
        "total": len(report),
        "four": {name: report.get(name, "MISSING") for name in (
            "MediaPlayService", "BossKeyService", "BrowserService")},
        "renderer_loaded": "app.services.web_page_renderer" in sys.modules,
        "not_running": {k: v for k, v in report.items() if v != "RUNNING"},
        "marshalled": ui_dispatch_counts()[0],
        "flet": "FLET" if "flet" in sys.modules else "NOFLET",
        "modules": len(sys.modules)}))
finally:
    hub.shutdown()
    shutil.rmtree(root, ignore_errors=True)
"""

    def test_the_headless_hub_starts_without_flet_and_without_a_ui_loop(self) -> None:
        result = subprocess.run([sys.executable, "-c", self.SNIPPET],
                                capture_output=True, text=True, timeout=600,
                                cwd=str(ROOT))
        lines = [line for line in (result.stdout or "").splitlines()
                 if line.startswith("R3REPORT ")]
        self.assertTrue(lines, f"子进程没交出读数：rc={result.returncode}\n"
                               f"{(result.stderr or '')[-3000:]}")
        report = json.loads(lines[-1][len("R3REPORT "):])
        self.assertEqual(report["four"], {"MediaPlayService": "RUNNING",
                                          "BossKeyService": "RUNNING",
                                          "BrowserService": "RUNNING"},
                         f"UI 耦合服务在无头装配里没起来：{report['four']}")
        self.assertFalse(report["renderer_loaded"],
                         "web_page_renderer 被 import 了——它顶部那条 import flet "
                         "会把整个无头进程拖回 flet")
        self.assertEqual(report["flet"], "NOFLET", "无头装配面上出现了 flet")
        self.assertGreater(report["modules"], 100,
                           f"只导了 {report['modules']} 个模块，仪器没接上，"
                           f"那条 NOFLET 不算证据")
        self.assertEqual(report["marshalled"], 0,
                         f"无头装配往界面循环投了 {report['marshalled']} 次")
        self.assertEqual(report["not_running"], {},
                         f"这些服务在无头装配里没 RUNNING：{report['not_running']}")


if __name__ == "__main__":
    unittest.main()
