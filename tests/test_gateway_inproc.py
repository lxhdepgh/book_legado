"""网关运行期护栏（设计 §5.1 / §5.3 / §5.4）：真起一个 GatewayService，真发 HTTP/WS。

装配是**最小**的：EngineHub + EventBus + StorageService(临时目录) + SettingsService +
SourceService + SourceCheckService(打桩 checker) + GatewayService。检测用打桩 checker
（记忆里 ``offline-loopback-probe`` 那条老规矩：绝不打真书源），但它照样逐源发
``CheckFinishedEvent``，事件面拿到的是真实形状。

循环归属照 ``main.py --serve``：``start_all`` 提交到 EngineHub 那条守护循环，网关的
socket 因此绑在那条循环上；测试客户端（aiohttp）也在**同一条循环**上跑——aiohttp 的
Server 与 Client 分处两条循环时，"能连、不动"是最难查的一类故障。

静态那一半（action 表 ↔ schema ↔ 真方法、信封、边界序列化）在
``tests/test_gateway_contract.py``。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from aiohttp import ClientSession, WSMsgType

from app.core.bus import EventBus
from app.core.engine_worker import (
    EngineHub, call_on_ui, reset_ui_dispatch_counts, set_ui_loop, ui_dispatch_counts)
from app.core.events import (
    CheckFinishedEvent, DownloadProgressEvent, ReadAloudStateEvent)
from app.core.hub import ServiceHub
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.gateway import jsonschema_min as schema
from app.gateway.methods import BY_ACTION
from app.gateway.serialize import find_leak
from app.services.app_paths import AppPaths
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.gateway.service import (
    WEBVIEW_TIMEOUT, GatewayService, WebviewUnavailable)
from app.services.sources.source_check_service import SourceCheckService
from app.services.sources.source_service import SourceService

ENVELOPE_KEYS = ["code", "data", "error", "id", "ok", "v"]
BASE = "http://127.0.0.1"


def _source_dict(name: str, url: str) -> dict:
    return {"bookSourceName": name, "bookSourceUrl": url, "bookSourceGroup": "护栏组",
            "enabled": True, "searchUrl": "/s?q={{key}}",
            "ruleSearch": {"bookList": "ul li", "name": "a@text"}}


async def _stub_checker(sources, *, concurrency, timeout, on_result, auto_disable,
                        depth=None):
    """离线打桩：逐源定论并回调，事件面因此拿到与真流水线同形的载荷。"""
    results = []
    for index, source in enumerate(sources, start=1):
        result = {"raw_url": source.bookSourceUrl, "check_state": "search_only",
                  "book_count": 1, "respond_time": 1}
        results.append(result)
        on_result(result, index, len(sources))
    return results


#: 挂 0.5s 的 list_sources 替身：timeout 那条护栏要的"预算 0.05s、活要 0.5s"
#: 就得有个真慢的方法，否则 timeout 分支永远量不到。
async def _slow_list() -> list:
    await asyncio.sleep(0.5)
    return []


class _StubEngine:
    """SourceService 只要有一个引擎句柄就够（本文件不跑规则引擎）。"""


class _StubAuditor:
    """CompatAuditService 替身：本文件不审兼容性，但要它别把 last_error 涂上降级。"""

    audited: list[str] = []

    async def audit_source(self, data: dict) -> None:
        _StubAuditor.audited.append(str(data.get("bookSourceUrl") or ""))


class Harness:
    """一次独立的无头装配 + 同循环驱动器。"""

    def __init__(self, *, sources: int = 2) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="gw-inproc-"))
        data = self.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "bookSource.json").write_text(json.dumps(
            [_source_dict(f"护栏源{i}", f"https://i{i}.example.com")
             for i in range(sources)], ensure_ascii=False), encoding="utf-8")
        self.exit_event = threading.Event()
        self.engine = EngineHub("gw-inproc")
        self.bus = EventBus(engine=self.engine)
        self.storage = StorageService(AppPaths(data=data, cache=self.root / "cache"))
        self.settings = SettingsService(storage=self.storage, engine=self.engine)
        # RuleEngineService 必须是**真类**注册（拓扑声明了这条依赖，且 require 按类定位），
        # 但 SourceService 拿到的引擎句柄仍打桩：本文件一次都不跑规则引擎。
        self.rule_engine = RuleEngineService()
        self.source_service = SourceService(
            storage=self.storage, settings=self.settings, engine=_StubEngine(),
            auditor=_StubAuditor())
        self.check_service = SourceCheckService(
            source_service=self.source_service, settings=self.settings,
            compat_audit=None, checker=_stub_checker)
        self.gateway = GatewayService(idle_exit_seconds=0.0,
                                      exit_event=self.exit_event)
        self.hub = ServiceHub(bus=self.bus, engine=self.engine)
        for service in (self.engine, self.bus, self.storage, self.settings,
                        self.rule_engine, self.source_service, self.check_service,
                        self.gateway):
            self.hub.register(service)

    def start(self) -> None:
        reset_ui_dispatch_counts()
        self.engine.submit(self.hub.start_all()).result(timeout=60)

    def stop(self) -> None:
        self.hub.shutdown()

    def drive(self, factory):
        """在引擎循环上跑一段客户端代码（与服务端同循环）。

        收的是**协程工厂**不是协程对象：ClientSession 必须在引擎循环里创建，
        在测试线程里 new 出来再递进去就是"绑到另一条循环"那一类故障。
        """
        async def _wrapped():
            async with ClientSession() as session:
                return await factory(session, self.base(), self.gateway.token)
        return self.engine.submit(_wrapped()).result(timeout=90)

    def base(self) -> str:
        return f"{BASE}:{self.gateway.bound_port}"

    def on_loop(self, action):
        """在引擎循环上直接跑一段同步/异步改动（不走 HTTP），用于装配现场。"""
        return self.engine.submit(action()).result(timeout=60)

    def publish(self, event) -> None:
        """从测试线程把事件丢进总线（handler 仍落在引擎循环上）。"""
        self.bus.publish(event)


async def _rpc(session, base, token, body, *, with_token=True):
    headers = {"Authorization": f"Bearer {token}"} if with_token else {}
    async with session.post(f"{base}/rpc", json=body, headers=headers) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except json.JSONDecodeError:
            return resp.status, {"_not_json": text}


def _envelope(action: str, payload=None, **overrides) -> dict:
    body = {"v": 1, "id": f"t-{action}", "action": action, "seq": 1,
            "payload": payload if payload is not None else {}}
    body.update(overrides)
    return body


class GatewayRpcTests(unittest.TestCase):
    """``POST /rpc``：合法路径、四类错误路径，以及"客户端解析器只认一种形状"。"""

    def setUp(self) -> None:
        self.harness = Harness()
        self.harness.start()
        self.addCleanup(self.harness.stop)

    def test_list_actions_return_non_empty_json_native_payloads(self) -> None:
        async def _drive(session, base, token):
            status, env = await _rpc(session, base, token, _envelope("source.list"))
            self.assertEqual((status, env["ok"], sorted(env)), (200, True, ENVELOPE_KEYS))
            names = [item["bookSourceName"] for item in env["data"]]
            self.assertEqual(names, ["护栏源0", "护栏源1"],
                             "正对照：动作必须真读到种进去的两条源")
            self.assertIsNone(find_leak(env["data"]))

            status, env = await _rpc(session, base, token, _envelope("engine.status"))
            self.assertTrue(env["ok"])
            self.assertEqual(len(env["data"]), 9, "状态行 = 8 个注册服务 + ServiceHub 自己")
            self.assertIsNone(find_leak(env["data"]))
        self.harness.drive(_drive)

    def test_find_leak_is_the_instrument_that_works(self) -> None:
        """上一条里的"反证"单独钉一次：漏检才是真的危险。"""
        self.assertIsNotNone(find_leak({"a": [self.harness.gateway]}))

    def test_unauthorized_request_is_refused_before_dispatch(self) -> None:
        called = []
        original = self.harness.source_service.list_sources

        async def spy():
            called.append(1)
            return await original()

        self.harness.source_service.list_sources = spy
        try:
            async def _drive(session, base, token):
                status, body = await _rpc(session, base, token,
                                          _envelope("source.list"), with_token=False)
                self.assertEqual(status, 401)
                self.assertEqual(body, {"error": "unauthorized"},
                                 "401 走 HTTP 语义，不套 RPC 信封")
                status, body = await _rpc(session, base, "错令牌",
                                          _envelope("source.list"))
                self.assertEqual(status, 401,
                                 "非 ASCII 令牌也得是 401：compare_digest 对 str 上的"
                                 "非 ASCII 是直接抛，抛穿中间件就成了 500 错误页")
                status, body = await _rpc(session, base, "Bearer 错令牌",
                                          _envelope("source.list"))
                self.assertEqual(status, 401)
                # 三条 401 都没落到服务上——这条断言必须在正对照之前，
                # 否则"合法请求确实会调用"会把 401 的账一起算进来。
                self.assertEqual(called, [], "没令牌的请求不许打到服务上")
                # 正对照：仪器接着线（合法令牌确实会走到那个方法）
                status, env = await _rpc(session, base, token, _envelope("source.list"))
                self.assertEqual((status, env["ok"], len(env["data"])), (200, True, 2))
                self.assertEqual(called, [1])
            self.harness.drive(_drive)
        finally:
            del self.harness.source_service.list_sources

    def test_every_error_kind_is_a_legal_envelope(self) -> None:
        cases = [
            ("bad_request", _envelope("no.such.action"), None),
            ("bad_request", _envelope("source.list", v=99), None),
            ("bad_request", _envelope("source.list", payload=[1]), None),
            ("schema", _envelope("source.list", {"unexpected": 1}), None),
        ]

        async def _drive(session, base, token):
            for expect_code, body, _ in cases:
                status, env = await _rpc(session, base, token, body)
                self.assertEqual(status, 200, "业务失败也是 200，差别在 ok/code")
                self.assertEqual(sorted(env), ENVELOPE_KEYS)
                self.assertFalse(env["ok"])
                self.assertEqual(env["code"], expect_code)
                self.assertIsInstance(env["error"], str)
                self.assertIsNone(env["data"])
        self.harness.drive(_drive)

    def test_timeout_engine_and_unavailable_codes(self) -> None:
        """三个错误类别各自都得真到、且仍然回合法信封。

        用**替换现有条目**而不是造新 action：造出来的新名字没有 schema 文件，
        分发在 schema 那一步就返回 ``schema`` 错误，根本走不到目标解析。
        """
        backup = dict(BY_ACTION)
        shelf = BY_ACTION["bookshelf.list"]
        source = BY_ACTION["source.list"]
        self.harness.source_service.list_sources = _slow_list
        try:
            BY_ACTION["bookshelf.list"] = dataclasses.replace(
                shelf, service="app.services.reading.bookmark_service.BookmarkService")
            BY_ACTION["source.list"] = dataclasses.replace(
                source, method="no_such_method")

            async def _drive(session, base, token):
                _, env = await _rpc(session, base, token, _envelope("bookshelf.list"))
                self.assertEqual((env["ok"], env["code"]), (False, "unavailable"),
                                 "协作者没注册 → unavailable，且错误里说清是哪个方法")
                self.assertIn("BookmarkService", env["error"])
                _, env = await _rpc(session, base, token, _envelope("source.list"))
                self.assertEqual((env["ok"], env["code"]), (False, "unavailable"),
                                 "表指向过期方法名 → unavailable（孤儿方法那一族的协议面护栏）")
        finally:
            BY_ACTION.clear()
            BY_ACTION.update(backup)

        self.harness.source_service.list_sources = _slow_list
        try:
            BY_ACTION["source.list"] = dataclasses.replace(source, timeout=0.05)

            async def _drive2(session, base, token):
                _, env = await _rpc(session, base, token, _envelope("source.list"))
                self.assertEqual((env["ok"], env["code"]), (False, "timeout"),
                                 "0.05s 预算下 0.5s 的方法必须折成 timeout")
                self.assertIn("0.05", env["error"])
            self.harness.drive(_drive2)
        finally:
            BY_ACTION["source.list"] = source
            del self.harness.source_service.list_sources

    def test_business_exception_becomes_engine_envelope(self) -> None:
        original = self.harness.source_service.list_sources

        async def boom():
            raise RuntimeError("服务里面炸了")

        self.harness.source_service.list_sources = boom
        try:
            async def _drive(session, base, token):
                _, env = await _rpc(session, base, token, _envelope("source.list"))
                self.assertEqual((env["ok"], env["code"]), (False, "engine"))
                self.assertIn("RuntimeError", env["error"])
            self.harness.drive(_drive)
        finally:
            del self.harness.source_service.list_sources

    def test_job_action_replies_before_the_work_finishes(self) -> None:
        """``mode="job"`` 的语义是"回话排在干活之前"，用时长把这条钉死。

        打桩 checker 睡 1.0s：RPC 若在 0.5s 内回来、而快照仍报 running，就证明
        这次调用没有把任务的时长背在自己身上（进度只从事件流走）。
        """
        began = []

        async def slow_checker(sources, *, concurrency, timeout, on_result,
                               auto_disable, depth=None):
            began.append(len(sources))
            await asyncio.sleep(1.0)
            for index, source in enumerate(sources, start=1):
                on_result({"raw_url": source.bookSourceUrl,
                           "check_state": "search_only"}, index, len(sources))
            return []

        self.harness.check_service._checker = slow_checker
        frames = []

        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                frames.append(json.loads((await ws.receive()).data))
                loop = asyncio.get_running_loop()
                started_at = loop.time()
                _, env = await _rpc(session, base, token,
                                    _envelope("check.start", {"groups": ["护栏组"]}))
                elapsed = loop.time() - started_at
                self.assertTrue(env["ok"], env)
                self.assertEqual(env["data"], {"started": True, "action": "check.start"})
                self.assertLess(elapsed, 0.5,
                                "job 动作在 1.0s 的活儿干完之前就该回话；"
                                f"实测 {elapsed:.2f}s = 它把任务时长背上了自己")
                _, snap = await _rpc(session, base, token, _envelope("check.snapshot"))
                self.assertTrue(snap["ok"])
                self.assertTrue(snap["data"]["running"], "任务还在跑时快照必须报 running")
                self.assertEqual(began, [2], "正对照：检测确实被派下去了，不是回了个空受理")
                # 结论事件在任务收尾后到（这一秒就是"进度走事件流、不占 RPC"的价钱）
                while True:
                    msg = await ws.receive(timeout=10)
                    if msg.type is not WSMsgType.TEXT:
                        break
                    frames.append(json.loads(msg.data))
                    if any(f.get("name") == "CheckFinishedEvent" for f in frames):
                        break
                return frames
        frames = self.harness.drive(_drive)
        names = [f["name"] for f in frames if f.get("type") == "event"]
        self.assertIn("CheckFinishedEvent", names)
        self.assertEqual(frames[0]["type"], "ready")

    def test_bookshelf_list_honours_the_include_staged_kwarg(self) -> None:
        """参数跨过协议面还得是那个参数：payload 的键名就是方法形参名。

        书架是 M6 落库的（``list_bookshelf`` 每次先 ``load_bookshelf()`` 重读表），
        所以种子必须**写进库**，改内存列表会被下一次读覆盖掉。
        """
        async def _seed() -> None:
            svc = self.harness.source_service
            svc.bookshelf = [
                {"bookUrl": "https://a/1", "name": "在架", "in_bookshelf": True},
                {"bookUrl": "https://a/2", "name": "暂存", "in_bookshelf": False},
            ]
            svc.save_bookshelf()
        self.harness.on_loop(_seed)

        async def _drive(session, base, token):
            _, default = await _rpc(session, base, token, _envelope("bookshelf.list"))
            _, staged = await _rpc(
                session, base, token,
                _envelope("bookshelf.list", {"include_staged": True}))
            self.assertEqual([item["name"] for item in default["data"]], ["在架"])
            self.assertEqual([item["name"] for item in staged["data"]], ["在架", "暂存"])
        self.harness.drive(_drive)

    def test_engine_shutdown_is_sync_and_sets_the_exit_flag(self) -> None:
        async def _drive(session, base, token):
            _, env = await _rpc(session, base, token,
                                _envelope("engine.shutdown", {"reason": "护栏"}))
            self.assertEqual((env["ok"], env["data"]), (True, {"stopping": True,
                                                               "reason": "护栏"}))
        self.harness.drive(_drive)
        self.assertTrue(self.harness.exit_event.is_set())

    def test_hello_reports_the_frozen_handshake(self) -> None:
        async def _drive(session, base, token):
            _, env = await _rpc(session, base, token, _envelope("engine.hello"))
            data = env["data"]
            self.assertEqual(data["protocol_version"], 1)
            self.assertEqual(sorted(data["actions"]), sorted(BY_ACTION))
            self.assertEqual(sorted(data["capabilities"]),
                             ["events", "job.actions", "media.covers", "rpc",
                              "webview.reverse"])
            self.assertEqual(data["schema_digest"], GatewayService.schema_digest())
            # 摘要必须随任一处字段变化而变（否则它拦不住版本错配）
            schema.load.cache_clear()
            try:
                with unittest.mock.patch.dict(
                        BY_ACTION, {"engine.hello": dataclasses.replace(
                            BY_ACTION["engine.hello"], timeout=99.0)}):
                    self.assertNotEqual(GatewayService.schema_digest(),
                                        data["schema_digest"])
            finally:
                schema.load.cache_clear()
        self.harness.drive(_drive)


class GatewayEventStreamTests(unittest.TestCase):
    """``GET /ws``：合流、顺序、重连补状态（设计 §5.1 那三条）。"""

    def setUp(self) -> None:
        self.harness = Harness()
        self.harness.start()
        self.addCleanup(self.harness.stop)

    def _frames(self, coro):
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                return await coro(session, base, token, ws)
        return self.harness.drive(_drive)

    def test_ready_frame_carries_the_same_digest_as_hello(self) -> None:
        async def _coro(session, base, token, ws):
            ready = json.loads((await ws.receive()).data)
            self.assertEqual(ready["type"], "ready")
            self.assertEqual(ready["schema_digest"], GatewayService.schema_digest())
        self._frames(_coro)

    def test_progress_events_coalesce_and_keep_order(self) -> None:
        async def _coro(session, base, token, ws):
            frames = [json.loads((await ws.receive()).data)]    # ready
            for index in range(5):
                self.harness.publish(DownloadProgressEvent(
                    task_id="t", kind="epub", progress=index / 5))
            self.harness.publish(CheckFinishedEvent(
                source_id="https://i0.example.com", state_class="search_only",
                summary="离线打桩", done=1, total=2))
            while True:
                msg = await ws.receive(timeout=10)
                if msg.type is not WSMsgType.TEXT:
                    break
                frames.append(json.loads(msg.data))
                if any(f.get("name") == "CheckFinishedEvent" for f in frames):
                    break
            return frames
        frames = self._frames(_coro)
        # 启动期那批 ServiceStateChangedEvent 也在同一个合流通道里，所以按内容定位
        # 自己那一帧，而不是假设"连上之后的第一帧就是它"。
        batches = [i for i, f in enumerate(frames) if f["type"] == "batch"
                   and any(item["name"] == "DownloadProgressEvent"
                           for item in f["events"])]
        tail = next(i for i, f in enumerate(frames)
                    if f.get("name") == "CheckFinishedEvent")
        self.assertEqual(len(batches), 1, "五条进度应当合成一帧，不是各发一帧")
        batch = frames[batches[0]]
        progress = [item for item in batch["events"]
                    if item["name"] == "DownloadProgressEvent"]
        self.assertEqual([item["payload"]["progress"] for item in progress],
                         [0.0, 0.2, 0.4, 0.6, 0.8], "合流只攒不改：顺序与数值都得在")
        self.assertLess(batches[0], tail,
                        "结果类事件不能被自己前面的进度盖到后头")
        self.assertEqual(frames[tail]["type"], "event",
                         "结果类事件不合流：它就是一帧 event")

    def test_reconnect_replays_the_latest_state_snapshot(self) -> None:
        async def _coro(session, base, token, ws):
            await ws.receive()                      # ready
            self.harness.publish(ReadAloudStateEvent(state="speaking",
                                                     book_url="https://a/1"))
            await asyncio.sleep(0.05)
            self.harness.publish(ReadAloudStateEvent(state="paused", book_url="https://a/1",
                                                     chapter=3))
            await asyncio.sleep(0.05)
            await ws.close()
            async with session.ws_connect(f"{base}/ws?token={token}") as again:
                frames = [json.loads((await again.receive()).data)]
                while frames[-1]["type"] != "ready":
                    frames.append(json.loads((await again.receive()).data))
                snap = json.loads((await again.receive(timeout=5)).data)
            return frames, snap
        frames, snap = self._frames(_coro)
        self.assertEqual(snap["type"], "snapshot")
        self.assertEqual(len(snap["events"]), 1, "只回放最近一份，不留历史")
        self.assertEqual(snap["events"][0]["name"], "ReadAloudStateEvent")
        self.assertEqual(snap["events"][0]["payload"]["state"], "paused")
        self.assertEqual(snap["events"][0]["payload"]["chapter"], 3)

    def test_idle_session_loss_asks_the_process_to_exit(self) -> None:
        self.harness.gateway._idle_exit_seconds = 0.2
        async def _coro(session, base, token, ws):
            await ws.receive()
            await ws.close()
            await asyncio.sleep(0.6)
        self._frames(_coro)
        self.assertTrue(self.harness.exit_event.is_set(),
                        "最后一个会话断开后必须自杀（§4.6：不留无头进程）")

    def test_no_session_never_arms_the_idle_timer(self) -> None:
        """纯 curl 试跑不该被一段倒计时打断：没连过 WS 就不武装。"""
        async def _coro(session, base, token):
            _, env = await _rpc(session, base, token, _envelope("engine.hello"))
            return env
        self.harness.gateway._idle_exit_seconds = 0.3
        self.harness.drive(_coro)
        time.sleep(0.6)
        self.assertFalse(self.harness.exit_event.is_set())


class HeadlessLoopTests(unittest.TestCase):
    """§5.4：无头路径上"一次跨循环编组都没发生过"必须能被机械断言。"""

    def test_headless_assembly_never_marshals_to_a_ui_loop(self) -> None:
        harness = Harness()
        harness.start()
        self.addCleanup(harness.stop)

        async def _drive(session, base, token):
            for action in ("engine.hello", "engine.status", "bookshelf.list",
                           "source.list", "check.snapshot"):
                _, env = await _rpc(session, base, token, _envelope(action))
                self.assertTrue(env["ok"], env)
            _, env = await _rpc(session, base, token,
                                _envelope("check.start", {"groups": ["护栏组"]}))
            self.assertTrue(env["ok"])
            await asyncio.sleep(0.2)
        harness.drive(_drive)
        marshalled, inline = ui_dispatch_counts()
        self.assertEqual(marshalled, 0,
                         "无头装配里出现了一次往界面循环的投递（§5.4 红线）")
        self.assertEqual(inline, 0,
                         "serve 路径不该有人调 call_on_ui；非零说明有控制器被牵进来了")

    def test_the_counter_actually_records_a_marshal(self) -> None:
        """正对照：登记一条"别的循环"，投递计数就得涨，否则上一条的 0 说明不了任何事。"""
        other = asyncio.new_event_loop()
        thread_ready = threading.Event()

        def _run():
            asyncio.set_event_loop(other)
            other.call_soon(thread_ready.set)
            other.run_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread_ready.wait(2.0)
        set_ui_loop(other)
        try:
            reset_ui_dispatch_counts()
            call_on_ui(lambda: None)
            marshalled, inline = ui_dispatch_counts()
            # 让那条循环把已排队的回调跑掉再停：不然这条协程永远不被 await，
            # GC 时炸出一条 RuntimeWarning，而案发现场会被记到别的测试身上。
            drained = threading.Event()
            other.call_soon_threadsafe(drained.set)
            self.assertTrue(drained.wait(2.0), "对照循环没把投递的回调吃掉")
        finally:
            set_ui_loop(None)
            other.call_soon_threadsafe(other.stop)
            thread.join(timeout=5)
        self.assertEqual((marshalled, inline), (1, 0))


class WebviewReverseCallTests(unittest.TestCase):
    """§8 R1：服务端→客户端的反向渲染（可见 WebView 归 Flutter 之后引擎怎么取正文）。

    这一族断言的是**形状与生死**：请求帧发得出去、答复解得开、宿主不在场时当场就知道、
    中途断线不会把预算等满。真实渲染的贵不贵是探针的事（``.probe/r1_webview_slice.py``），
    护栏这边一次都不起浏览器。
    """

    def setUp(self) -> None:
        self.harness = Harness()
        self.harness.start()
        self.addCleanup(self.harness.stop)

    async def _next_request(self, ws) -> dict:
        """取下一条 request 帧。

        ``/ws`` 上排在它前面的可能是服务启动时的合批（``ServiceStateChangedEvent``
        就在 :data:`COALESCED_EVENTS` 里，与它共用一条通道），所以只能按**类型**认，
        不能按帧序认。
        """
        while True:
            frame = json.loads((await ws.receive(timeout=10)).data)
            if frame.get("type") == "request":
                return frame
            self.assertIn(frame.get("type"), ("batch", "snapshot"),
                          f"request 之前只该有合批/回放，收到 {frame}")

    async def _answer(self, session, base, token, frame, **overrides) -> dict:
        payload = {"request_id": frame["request_id"], "body": "<p>渲染出来了</p>",
                   "headers": {"Content-Type": "text/html"}, "status": 200,
                   "url": frame["payload"]["url"]}
        payload.update(overrides)
        _, env = await _rpc(session, base, token,
                            _envelope("engine.webContentReady", payload))
        return env

    def test_request_frame_round_trips_into_the_fetcher_result(self) -> None:
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                json.loads((await ws.receive()).data)          # ready
                pending = self.harness.engine.submit(
                    self.harness.gateway.request_web_content(
                        "https://loopback.test/chapter/1", None,
                        {"webView": True, "headerMap": {"User-Agent": "护栏"}}))
                frame = await self._next_request(ws)
                self.assertEqual(frame["name"], "webview.content")
                self.assertEqual(frame["payload"]["url"], "https://loopback.test/chapter/1")
                self.assertEqual(frame["payload"]["options"]["webView"], True)
                self.assertIsNone(find_leak(frame["payload"]),
                                  "反向帧也是协议面，不许把服务实例塞进 options")
                env = await self._answer(session, base, token, frame)
                self.assertTrue(env["ok"], env)
                self.assertEqual(env["data"], {"delivered": True})
                result = await asyncio.wrap_future(pending)
                self.assertEqual(result["body"], "<p>渲染出来了</p>")
                self.assertEqual(result["headers"], {"content-type": "text/html"})
                self.assertEqual(result["status"], 200)
            self.assertEqual(self.harness.gateway._pending_webviews, {},
                             "答完就必须从在途表里摘掉；留着就是每本书漏一条 future")
        self.harness.drive(_drive)

    def test_the_loop_stays_served_while_the_engine_waits(self) -> None:
        """R1 真正要证的不是能不能渲染，是**等渲染的那半秒里循环还活着**。

        网关 handler 和引擎协程在同一条 EngineHub 循环上：await 一挂就把循环钉住的话，
        客户端那条回话 RPC 就永远进不来——两边都在等对方，读数却是"什么都不发生"。
        """
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                json.loads((await ws.receive()).data)          # ready
                pending = self.harness.engine.submit(
                    self.harness.gateway.request_web_content("https://loopback.test/c",
                                                             None, {}))
                frame = await self._next_request(ws)
                await asyncio.sleep(0.05)      # 引擎侧此刻正挂在那条 future 上
                status, env = await _rpc(session, base, token, _envelope("engine.status"))
                self.assertEqual((status, env["ok"]), (200, True),
                                 "引擎在等 WebView 时，别的 RPC 照样得能回来")
                await _rpc(session, base, token, _envelope("engine.hello"))
                env = await self._answer(session, base, token, frame)
                self.assertTrue(env["ok"], env)
                self.assertEqual((await asyncio.wrap_future(pending))["body"],
                                 "<p>渲染出来了</p>")
        self.harness.drive(_drive)

    def test_no_host_fails_at_once_instead_of_burning_the_budget(self) -> None:
        """一个会话都没连着 = 这条线上永远不会有人回答，当场就该炸给调用方。"""

        async def _call():
            with self.assertRaises(WebviewUnavailable) as caught:
                await self.harness.gateway.request_web_content(
                    "https://loopback.test/c", None, {}, timeout=30.0)
            return str(caught.exception)

        started = time.monotonic()
        message = self.harness.on_loop(_call)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0,
                        f"没宿主却等了 {elapsed:.1f}s——30s 预算白烧，用户看到的是卡死")
        self.assertIn("WebView", message)

        # 正对照：同一条调用在有会话时是**会等**的（不然上面那条快可能只是它压根没跑）
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                json.loads((await ws.receive()).data)
                pending = self.harness.engine.submit(
                    self.harness.gateway.request_web_content("https://loopback.test/c",
                                                             None, {}, timeout=30.0))
                frame = await self._next_request(ws)
                await asyncio.sleep(0.2)
                self.assertFalse(pending.done(), "有宿主时这条等待必须真的挂在那儿")
                await self._answer(session, base, token, frame)
                self.assertEqual((await asyncio.wrap_future(pending))["body"],
                                 "<p>渲染出来了</p>")
        self.harness.drive(_drive)

    def test_host_dying_mid_flight_releases_the_waiter(self) -> None:
        async def _drive(session, base, token):
            ws = await session.ws_connect(f"{base}/ws?token={token}")
            json.loads((await ws.receive()).data)
            pending = self.harness.engine.submit(
                self.harness.gateway.request_web_content("https://loopback.test/c",
                                                         None, {}, timeout=30.0))
            await self._next_request(ws)                    # request 帧
            await ws.close()                            # Flutter 崩了
            started = time.monotonic()
            with self.assertRaises(WebviewUnavailable):
                await asyncio.wrap_future(pending)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0,
                            f"宿主断了还等 {elapsed:.1f}s：30s 预算等满就是"
                            "整条阅读链路停在同一页上")
        self.harness.drive(_drive)

    def test_late_answer_is_reported_not_raised(self) -> None:
        """引擎已经按预算走人，客户端才把渲染送回——这条得是 delivered:false，不是炸。"""
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                json.loads((await ws.receive()).data)
                pending = self.harness.engine.submit(
                    self.harness.gateway.request_web_content("https://loopback.test/c",
                                                            None, {}, timeout=0.2))
                frame = await self._next_request(ws)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wrap_future(pending)
                _, env = await _rpc(session, base, token, _envelope(
                    "engine.webContentReady",
                    {"request_id": frame["request_id"], "body": "迟到的正文"}))
                self.assertEqual((env["ok"], env["data"]), (True, {"delivered": False}))
        self.harness.drive(_drive)

    def test_error_answer_reaches_the_caller_as_an_error(self) -> None:
        """客户端"渲染失败"也得回话：引擎据此退回非浏览器路径，别按成功走空正文。"""
        async def _drive(session, base, token):
            async with session.ws_connect(f"{base}/ws?token={token}") as ws:
                json.loads((await ws.receive()).data)
                pending = self.harness.engine.submit(
                    self.harness.gateway.request_web_content("https://loopback.test/c",
                                                            None, {}, timeout=10.0))
                frame = await self._next_request(ws)
                env = await self._answer(session, base, token, frame,
                                         body="", error="域名解析失败")
                self.assertEqual(env["data"], {"delivered": True})
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wrap_future(pending)
                self.assertIn("域名解析失败", str(caught.exception))
        self.harness.drive(_drive)

    def test_shutdown_releases_every_waiter(self) -> None:
        """停服时若在等渲染，等的人必须被放掉——否则 hub.shutdown 排在它后面不走。"""
        async def _drive(session, base, token):
            ws = await session.ws_connect(f"{base}/ws?token={token}")
            json.loads((await ws.receive()).data)
            pending = self.harness.engine.submit(
                self.harness.gateway.request_web_content("https://loopback.test/c",
                                                        None, {}, timeout=30.0))
            await self._next_request(ws)
            self.assertEqual(len(self.harness.gateway._pending_webviews), 1)
            await self.harness.gateway.stop_server()
            with self.assertRaises(WebviewUnavailable):
                await asyncio.wrap_future(pending)
        self.harness.drive(_drive)


class MediaEndpointTests(unittest.TestCase):
    """``GET /media/{key}``：二进制不走 RPC，key 被正则钉死，Range 是真语义。"""

    KEY = "0123456789abcdef.jpg"

    def setUp(self) -> None:
        self.harness = Harness()
        self.harness.start()
        self.addCleanup(self.harness.stop)
        covers = self.harness.storage.cache_dir("image-cache/covers")
        self.body = bytes(range(256)) * 4
        (covers / self.KEY).write_bytes(self.body)

    def _get(self, key: str, headers=None):
        async def _drive(session, base, token):
            url = f"{base}/media/{key}?token={token}"
            async with session.get(url, headers=headers or {}) as resp:
                return resp.status, await resp.read(), dict(resp.headers)
        return self.harness.drive(_drive)

    def test_full_read_and_ranges(self) -> None:
        status, body, headers = self._get(self.KEY)
        self.assertEqual((status, body), (200, self.body))
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        status, body, headers = self._get(self.KEY, {"Range": "bytes=10-19"})
        self.assertEqual((status, body), (206, self.body[10:20]))
        self.assertEqual(headers["Content-Range"], f"bytes 10-19/{len(self.body)}")
        status, body, _ = self._get(self.KEY, {"Range": "bytes=-8"})
        self.assertEqual((status, body), (206, self.body[-8:]))
        status, _, headers = self._get(self.KEY, {"Range": "bytes=999999-1000000"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(self.body)}")

    def test_keys_that_are_not_the_pinned_shape_are_404(self) -> None:
        for key in ("..%2f..%2fdata%2flegado.db", "zzzzzzzzzzzzzzzz.jpg",
                    "0123456789abcdef.png", "0123456789abcde.jpg"):
            status, _, _ = self._get(key)
            self.assertEqual(status, 404, f"{key} 不该被当成文件名")

    def test_missing_file_is_404_not_a_crash(self) -> None:
        status, _, _ = self._get("ffffffffffffffff.jpg")
        self.assertEqual(status, 404)


class ServeImportTests(unittest.TestCase):
    """§5.5：serve 路径与协议层零 flet。

    三条互补：静态（协议层不 import 服务层、也不 import flet）、装配序（``--serve``
    分支必须排在 ``import flet`` 之前）、运行期（新进程里把网关会用到的模块导一遍，
    ``sys.modules`` 里就是没有 flet）。运行期那条比 AST 强：AST 只看写得出来的 import，
    看不到函数里懒加载进来的那条链。
    """

    def test_protocol_layer_is_flet_free_and_service_free(self) -> None:
        import ast

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for py in sorted((root / "app" / "gateway").rglob("*.py")):
            if py.suffix != ".py" or "__pycache__" in py.parts:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name == "flet" or name.startswith("flet."):
                        offenders.append(f"{py.name}: import {name}")
                    if name == "app.services" or name.startswith("app.services."):
                        offenders.append(f"{py.name}: 协议层直连服务实现 {name}")
        self.assertEqual(offenders, [],
                         "app/gateway/** 必须既零 flet 又零 app.services：\n"
                         + "\n".join(offenders))

    def test_serve_branch_runs_before_flet_is_imported(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8")
        serve_at = source.index('"--serve"')
        flet_at = source.index("import flet as ft")
        self.assertLess(serve_at, flet_at,
                        "--serve 分支一旦被挪到 import flet 之后，无头进程就背上了 flet")

    def test_gateway_import_surface_does_not_load_flet(self) -> None:
        code = (
            "import sys\n"
            "import app.services.gateway.service\n"
            "import app.gateway.methods as m\n"
            "for method in m.METHODS:\n"
            "    m.resolve_service_class(method.service)\n"
            "print('FLET' if 'flet' in sys.modules else 'NOFLET', len(sys.modules))\n"
        )
        import subprocess
        import sys as _sys

        result = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=180,
                                cwd=str(Path(__file__).resolve().parents[1]))
        tail = (result.stdout or "").strip().splitlines()
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertTrue(tail, result.stderr[-2000:])
        marker, count = tail[-1].split()
        self.assertEqual(marker, "NOFLET", "网关导入面上出现了 flet")
        self.assertGreater(int(count), 100,
                           f"只导了 {count} 个模块，仪器没接上（这条 NOFLET 不算证据）")


if __name__ == "__main__":
    unittest.main()
