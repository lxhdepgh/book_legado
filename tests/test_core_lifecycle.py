"""M1 基础设施核心测试（设计 §4 / §6 / §9.2）。

覆盖：
- ServiceHub 注册 / 依赖拓扑启停 / status_report 汇聚；
- EventBus 订阅 / 发布 / 退订 / 异常隔离；
- SettingsService 同步读写 / 事件 / 重启后持久化；
- StorageService schema 版本与 cookies 表读写（tmp 数据根，不污染真实目录）。

数据目录经 LEGADO_BOOKSOURCE_DATA_DIR / LEGADO_BOOKSOURCE_CACHE_DIR 环境变量
重定向（app_paths 支持），所有测试都在 tmp_path 内完成。
"""
from __future__ import annotations

import asyncio
import ast
import logging
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

import app as app_pkg

from app.core.bus import EventBus
from app.core.engine_worker import (EngineHub, engine_hub, install_loop_attribution,
                                    set_ui_loop, ui_loop)
import app.core.engine_worker as engine_worker
from app.core.events import AppEvent, ServiceStateChangedEvent, SettingsChangedEvent
from app.core.hub import ServiceHub
from app.core.lifecycle import Service, ServiceState
from app.core.settings import SettingsService
from app.core.storage import SCHEMA_VERSION, StorageService
from app.controllers.base import BaseController


def _drain(futures) -> None:
    """收干 EventBus 投递产生的 future，让异步断言变同步。"""
    for f in futures:
        f.result(timeout=5)


# --------------------------------------------------------------------- #
# 1. ServiceHub 注册 / 拓扑启停 / 状态汇聚
# --------------------------------------------------------------------- #
class _TopA(Service):
    name = "TopA"
    dependencies = ()


class _TopB(Service):
    name = "TopB"
    dependencies = (_TopA,)


def test_hub_topo_start_stop_order():
    started: list[str] = []
    stopped: list[str] = []

    a, b = _TopA(), _TopB()

    async def a_start():
        started.append("A")

    async def b_start():
        started.append("B")

    async def a_stop():
        stopped.append("A")

    async def b_stop():
        stopped.append("B")

    a.on_start = a_start  # type: ignore[method-assign]
    b.on_start = b_start  # type: ignore[method-assign]
    a.on_stop = a_stop  # type: ignore[method-assign]
    b.on_stop = b_stop  # type: ignore[method-assign]

    engine = engine_hub()
    bus = EventBus(engine=engine)
    hub = ServiceHub(bus=bus, engine=engine)
    # 故意先注册消费者 B 再注册依赖 A：拓扑序仍须 A 先于 B
    hub.register(b)
    hub.register(a)

    asyncio.run(hub.start_all())

    assert started == ["A", "B"], f"拓扑启动序错误: {started}"
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopA"] is ServiceState.RUNNING
    assert states["TopB"] is ServiceState.RUNNING
    # 仪表盘行 = ServiceHub 自身 + 2 个注册服务
    assert len(hub.status_report()) == 3

    asyncio.run(hub.stop_all())
    assert stopped == ["B", "A"], f"逆序停止错误: {stopped}"
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopA"] is ServiceState.DISPOSED
    assert states["TopB"] is ServiceState.DISPOSED


# --------------------------------------------------------------------- #
# 1b. 单服务重启（仪表盘「重启」按钮的编排面）
# --------------------------------------------------------------------- #
class _TopC(Service):
    name = "TopC"
    dependencies = (_TopB,)


class _Side(Service):
    name = "Side"
    dependencies = ()


def _spy(svc: Service, calls: list[str]) -> Service:
    """把 on_start/on_stop 换成记账（真实服务重启要碰数据库/浏览器，测试不碰）。"""

    async def _start():
        calls.append(f"start:{svc.name}")

    async def _stop():
        calls.append(f"stop:{svc.name}")

    svc.on_start = _start  # type: ignore[method-assign]
    svc.on_stop = _stop    # type: ignore[method-assign]
    return svc


def _restart_hub(services: list[Service]) -> ServiceHub:
    """按**乱序**注册（消费者先注册），保证结论只能来自拓扑排序而不是注册序。"""
    engine = engine_hub()
    hub = ServiceHub(bus=EventBus(engine=engine), engine=engine)
    for svc in reversed(services):
        hub.register(svc)
    return hub


def test_restart_covers_downstream_in_topo_order():
    """重启 A 必须连带下游 B、C：停逆序（C→B→A），起正序（A→B→C）。

    `require()` 给的永远是同一个实例、总控也不重建，所以只把 A 停到 DISPOSED、
    B/C 照原样跑 = 一群服务拿着已经退役的依赖继续干活。
    """
    calls: list[str] = []
    a, b, c, side = (_spy(_TopA(), calls), _spy(_TopB(), calls),
                    _spy(_TopC(), calls), _spy(_Side(), calls))
    hub = _restart_hub([a, b, c, side])
    calls.clear()

    names = asyncio.run(hub.restart_service("TopA"))

    assert names == ["TopA", "TopB", "TopC"], f"重启范围/顺序错: {names}"
    assert calls == ["stop:TopC", "stop:TopB", "stop:TopA",
                     "start:TopA", "start:TopB", "start:TopC"], calls
    assert "stop:Side" not in calls and "start:Side" not in calls, \
        "与重启目标无关的服务被惊动了"
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopC"] is ServiceState.RUNNING


def test_restart_revives_retired_upstream_but_not_suspended_one():
    """依赖已经退役就一起复活；人**有意挂起**的依赖不许擅自拉起。"""
    calls: list[str] = []
    a, b = _spy(_TopA(), calls), _spy(_TopB(), calls)
    hub = _restart_hub([a, b])

    asyncio.run(hub.stop_all())         # 两行都 DISPOSED
    calls.clear()
    names = asyncio.run(hub.restart_service("TopB"))
    assert names == ["TopA", "TopB"], f"退役的上游没被一起复活: {names}"
    # 已经 DISPOSED 的服务，stop() 是空转（基类守卫直接 return），所以这里
    # 只有 start 记账——别把它当成"没停"。
    assert calls == ["start:TopA", "start:TopB"], calls
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopA"] is ServiceState.RUNNING

    calls.clear()
    asyncio.run(a.suspend())            # 人按下去的挂起
    calls.clear()
    names = asyncio.run(hub.restart_service("TopB"))
    assert names == ["TopB"], f"SUSPENDED 的上游被擅自拉起了: {names}"
    assert calls == ["stop:TopB", "start:TopB"], calls
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopA"] is ServiceState.SUSPENDED


def test_restart_of_uncontrollable_id_dispatches_nothing():
    """护栏钉"没发生"：不可重启的行不许动任何服务。

    两类：总控自身那一行（查不到实例），以及 **EngineHub**——它查得到实例，
    但执行这次重启的协程正跑在它那条守护 loop 上，停它等于把自己脚下那块拆掉。
    旧实现只防了前者，绕开 UI 直接调编排入口就能把后者按下去。
    """
    calls: list[str] = []
    a = _spy(_TopA(), calls)
    engine = EngineHub(name="legado-restart-guard-test")
    hub = ServiceHub(bus=EventBus(engine=engine), engine=engine)
    hub.register(engine)
    hub.register(a)
    calls.clear()

    assert hub.restartable("ServiceHub") is False
    assert hub.restartable("EngineHub") is False
    assert hub.restartable("TopA") is True
    assert asyncio.run(hub.restart_service("ServiceHub")) == []
    assert asyncio.run(hub.restart_service("EngineHub")) == []
    assert asyncio.run(hub.restart_service("并不存在")) == []
    assert calls == [], f"不可控行了却动了服务: {calls}"
    assert engine.status.state is ServiceState.INIT, "EngineHub 被按停了"
    engine.shutdown()


def test_restart_keeps_going_when_one_service_fails():
    """单个服务起不来不阻断整串（与 start_all 同一异常策略 §4.4）。"""
    calls: list[str] = []
    a, b = _spy(_TopA(), calls), _spy(_TopB(), calls)

    async def broken():
        raise RuntimeError("资源没起来")

    a.on_start = broken  # type: ignore[method-assign]
    hub = _restart_hub([a, b])

    names = asyncio.run(hub.restart_service("TopA"))
    assert names == ["TopA", "TopB"]
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states["TopA"] is ServiceState.SUSPENDED, "启动失败应停在 SUSPENDED"
    assert states["TopB"] is ServiceState.RUNNING, f"上游失败拖死了下游: {states}"
    assert a.status.last_error and "资源没起来" in a.status.last_error


# --------------------------------------------------------------------- #
# 2. EventBus 订阅 / 发布 / 退订 / 异常隔离
# --------------------------------------------------------------------- #
def test_eventbus_pub_sub_unsubscribe():
    engine = engine_hub()
    bus = EventBus(engine=engine)
    received: list[ServiceStateChangedEvent] = []

    async def handler(ev):
        received.append(ev)

    unsubscribe = bus.subscribe(ServiceStateChangedEvent, handler)
    ev = ServiceStateChangedEvent(service_id="X", state="RUNNING")
    _drain(bus.publish(ev))
    assert received == [ev]

    # 退订后不再投递；重复退订安全
    unsubscribe()
    unsubscribe()
    _drain(bus.publish(ServiceStateChangedEvent(service_id="Y", state="RUNNING")))
    assert received == [ev], "退订后仍收到事件"


def test_eventbus_handler_exception_isolation():
    engine = engine_hub()
    bus = EventBus(engine=engine)
    ok_received: list[AppEvent] = []

    async def boom(ev):
        raise RuntimeError("boom")

    async def ok(ev):
        ok_received.append(ev)

    bus.subscribe(ServiceStateChangedEvent, boom)
    bus.subscribe(ServiceStateChangedEvent, ok)
    ev = ServiceStateChangedEvent(service_id="Z", state="STARTING")
    _drain(bus.publish(ev))  # boom 抛错不得阻断 ok
    assert ok_received == [ev]


# --------------------------------------------------------------------- #
# 3. SettingsService get/set/事件/持久化
# --------------------------------------------------------------------- #
@pytest.fixture()
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(tmp_path / "cache"))
    storage = StorageService()
    asyncio.run(storage.start())
    try:
        yield storage
    finally:
        asyncio.run(storage.stop())


def test_settings_get_set_and_event(tmp_storage):
    engine = engine_hub()
    bus = EventBus(engine=engine)
    settings = SettingsService(storage=tmp_storage, engine=engine)
    settings.bus = bus

    # 收干 set() 内部 publish 的 future，让事件断言同步化
    pending: list = []
    orig_publish = bus.publish

    def spy_publish(ev):
        futures = orig_publish(ev)
        pending.extend(futures)
        return futures

    bus.publish = spy_publish  # type: ignore[method-assign]

    fired: list[SettingsChangedEvent] = []

    async def on_changed(ev):
        fired.append(ev)

    bus.subscribe(SettingsChangedEvent, on_changed)

    asyncio.run(settings.on_start())
    try:
        assert settings.get("nonexistent", "def") == "def"
        settings.set("reader.font_size", 18)
        settings.set("reader.font_size", 20)
        assert settings.get("reader.font_size") == 20
    finally:
        asyncio.run(settings.on_stop())

    _drain(pending)
    values = [e.value for e in fired if e.key == "reader.font_size"]
    assert values == [18, 20], f"变更事件序列错误: {values}"


def test_settings_persists_across_restart(tmp_storage):
    engine = engine_hub()
    first = SettingsService(storage=tmp_storage, engine=engine)
    asyncio.run(first.on_start())
    first.set("theme.name", "ink_gray")
    asyncio.run(first.on_stop())

    # 新实例复用同一主库，启动时预载缓存
    second = SettingsService(storage=tmp_storage, engine=engine)
    asyncio.run(second.on_start())
    try:
        assert second.get("theme.name") == "ink_gray"
    finally:
        asyncio.run(second.on_stop())


# --------------------------------------------------------------------- #
# 4. StorageService schema 版本 + cookies 表
# --------------------------------------------------------------------- #
def test_storage_schema_version_and_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(tmp_path / "cache"))
    storage = StorageService()
    asyncio.run(storage.start())
    try:
        conn = storage.db
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # v8（#88 续读位置）：books 补 chapter_paragraph（章内段序号，可空），
        # v7 补的是 play_chapter_index（播放进度与阅读进度分列）；user_version 升到 8。
        assert version == SCHEMA_VERSION == 8
        assert "chapter_paragraph" in {
            row[1] for row in conn.execute("PRAGMA table_info(books)")}

        # cookies 表读写
        conn.execute(
            "INSERT INTO cookies(key, jar, updated_at) VALUES(?, ?, ?)",
            ("example.com", "{}", 123.0))
        row = conn.execute(
            "SELECT key, jar, updated_at FROM cookies WHERE key=?",
            ("example.com",)).fetchone()
        assert row[0] == "example.com" and row[1] == "{}" and row[2] == 123.0

        # 目录分配
        assert storage.cache_dir("reader-cache").is_dir()
        assert storage.download_dir().is_dir()
    finally:
        asyncio.run(storage.stop())


# --------------------------------------------------------------------- #
# 6. EngineHub 退出收尾：stop_all 就跑在守护 loop 上，绝不能自 join
# --------------------------------------------------------------------- #
def _engine_thread(name: str):
    return next((t for t in threading.enumerate() if t.name == name), None)


def test_service_hub_shutdown_does_not_self_join(caplog):
    """真实退出链路（main.py finally）：主线程等 stop_all 收尾，之后关掉守护线程。

    回归的是关闭时那条 traceback：ServiceHub.shutdown 把 stop_all 派到 EngineHub
    自己的 loop 上跑，而 EngineHub 也在服务表里（基础设施之一），逆序轮到它时
    on_stop 若去 join 当前线程就是 RuntimeError: cannot join current thread。
    """
    name = "legado-exit-test"
    eng = EngineHub(name=name)
    hub = ServiceHub(bus=EventBus(engine=eng), engine=eng)
    hub.register(eng)
    hub.register(_TopA())
    asyncio.run(hub.start_all())
    assert _engine_thread(name) is not None, "start_all 应预热守护线程"

    with caplog.at_level(logging.ERROR):
        hub.shutdown()

    assert "cannot join current thread" not in caplog.text
    assert "停止异常" not in caplog.text, f"退出链路不该记任何停止异常：{caplog.text}"
    states = {s.service_id: s.state for s in hub.status_report()}
    assert states == {"ServiceHub": ServiceState.DISPOSED,
                      "EngineHub": ServiceState.DISPOSED,
                      "TopA": ServiceState.DISPOSED}
    thread = _engine_thread(name)
    assert thread is None or not thread.is_alive(), "shutdown 后守护线程应收尾"


def test_engine_hub_shutdown_on_own_thread_does_not_join():
    """在 loop 线程上请求关停（仪表盘对 EngineHub 那一行发停止）：只发信号不 join。"""
    name = "legado-self-shutdown-test"
    eng = EngineHub(name=name)
    asyncio.run(eng.start())
    thread = _engine_thread(name)
    assert thread is not None

    async def stopper():
        eng.shutdown()  # 当前线程正是 loop 线程：旧实现在这里抛 RuntimeError

    eng.submit(stopper()).result(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive(), "loop 收到 stop 信号后线程应退出"


# --------------------------------------------------------------------- #
# 7. 静态护栏：Service 子类不许覆盖生命周期方法
# --------------------------------------------------------------------- #
_LIFECYCLE_METHODS = {"start", "stop", "suspend", "resume"}


def test_no_service_subclass_shadows_lifecycle_methods():
    """回归：`ReadAloudService.stop()` 与 `MediaPlayService.stop()/resume()` 原本是
    "停止朗读 / 停止播放 / 继续播放"，把基类的生命周期方法整块盖掉了——hub 逆序停止
    轮到它们时只停播放、不迁移状态，退出后服务表上留着两行 RUNNING，仪表盘的
    「恢复」也拨不动状态机。播放面动作请叫 stop_speaking() / stop_playback() /
    resume_playback()（`TtsBackend.stop()` 之类非 Service 子类不受限）。
    """
    app_root = pathlib.Path(app_pkg.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name == "Service":
                continue   # 基类自身就是这些方法的定义处
            bases = {ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
            if "Service" not in bases:
                continue
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name in _LIFECYCLE_METHODS):
                    offenders.append(f"{path.name}:{item.lineno} "
                                     f"{node.name}.{item.name}")
    assert not offenders, f"Service 子类覆盖了生命周期方法：{offenders}"


# --------------------------------------------------------------------- #
# 8. 退出收尾期：登记的界面循环已经关了，不许再往上面派发
# --------------------------------------------------------------------- #
class _StubBus:
    """只记录订阅的假总线：subscribe 返回幂等退订句柄。"""

    def __init__(self):
        self.handlers: dict = {}

    def subscribe(self, event_type, handler):
        self.handlers[event_type] = handler
        return lambda: self.handlers.pop(event_type, None)


class _StubView:
    page = None


class _ShutdownProbe(BaseController):
    """订阅一个事件、处理器只记账的探针控制器（视图用没有 page 的哑对象）。"""

    def __init__(self, bus):
        super().__init__(hub=SimpleNamespace(bus=bus), bus=bus)
        self.seen = 0

    async def _handler(self, event) -> None:
        self.seen += 1

    def on_attach(self) -> None:
        self.subscribe(ServiceStateChangedEvent, self._handler)


def _dispatch(bus, event) -> None:
    """在一条独立循环上跑总线处理器（真/bus 把处理器投到 EngineHub 循环）。"""

    async def go():
        await bus.handlers[type(event)](event)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()


def test_controller_drops_events_after_the_ui_loop_is_closed():
    """回归：Flet 关窗后服务表还在发 ServiceStateChangedEvent。

    旧实现无条件 ``run_coroutine_threadsafe(handler, loop)``，而 loop 已被
    Flet close → 总线上冒 ``RuntimeError: Event loop is closed``（退出时满屏
    traceback）。控件这时早就不在了，事件既不该崩也不该硬执行——丢掉即可。
    """
    closed = asyncio.new_event_loop()
    closed.close()
    saved = ui_loop()
    set_ui_loop(closed)
    bus = _StubBus()
    controller = _ShutdownProbe(bus)
    try:
        controller.attach(_StubView())
        _dispatch(bus, ServiceStateChangedEvent(service_id="x", state="disposed"))
        assert controller.seen == 0, "界面循环已关，处理器不该被执行"
        # ui_call 走另一条路：退回引擎循环，协程必须真的跑完（不能只崩在派发上）
        ran: list[int] = []

        async def work():
            ran.append(1)

        controller.ui_call(work()).result(timeout=10)
        assert ran == [1], "界面循环已关时 ui_call 应退回引擎循环，而不是抛错"
    finally:
        set_ui_loop(saved)
        controller.detach()


class _CountingPage:
    """只数 `page.update()` 次数的假页（合并下发看的就是这一个数）。"""

    def __init__(self):
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class _CountingView:
    def __init__(self):
        self.page = _CountingPage()


class _FlushProbe(BaseController):
    """订阅一条事件、处理器只记账的探针；flush_interval 由用例决定。"""

    def __init__(self, bus, flush_interval=None):
        super().__init__(hub=SimpleNamespace(bus=bus), bus=bus)
        self._flush_interval = flush_interval
        self.rendered: list[str] = []

    def on_attach(self) -> None:
        self.subscribe(ServiceStateChangedEvent, self._handler,
                       flush_interval=self._flush_interval)

    async def _handler(self, event) -> None:
        self.rendered.append(event.service_id)


class _LiveUiLoop:
    """把一条真循环跑在旁路线程上并登记成界面循环（收尾还原）。"""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever,
                                       name="ui-loop-stub", daemon=True)
        self.thread.start()
        self.saved = ui_loop()
        set_ui_loop(self.loop)
        return self.loop

    def __exit__(self, *exc_info):
        set_ui_loop(self.saved)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        return False


def test_flush_interval_delivers_every_event_but_updates_once():
    """`flush_interval` 合并的是**下发**，不是事件：20 条结果一条都不许丢。

    检测面每源一条 ``CheckFinishedEvent``（``app/core/events.py:146``）。当时评估过
    直接 :meth:`BaseController.throttle` 那个处理器，那是错的——合并处理器就等于把
    中间那些源的结果行整条抹掉。整页 diff 实测随行数线性变贵（≈0.024 ms/行，
    900 行一轮 = 900 次下发、占用界面循环 10.6 s，见
    ``docs/known-issues/004_long_scan_page_frozen_and_quickjs_abort.md`` 的取证表），
    要压的是下发次数，不是渲染次数。
    """
    with _LiveUiLoop() as loop:
        bus = _StubBus()
        controller = _FlushProbe(bus, flush_interval=0.2)
        view = _CountingView()
        controller.attach(view)
        handler = bus.handlers[ServiceStateChangedEvent]
        try:
            for index in range(20):
                fut = asyncio.run_coroutine_threadsafe(
                    handler(ServiceStateChangedEvent(service_id=f"s{index}",
                                                     state="running")), loop)
                fut.result(timeout=5)
            assert len(controller.rendered) == 20, \
                f"合并下发把结果行也一起合掉了：{len(controller.rendered)}/20"
            # leading：窗口第一次立刻下发；此后 19 条只改控件树，不再各下发一次
            assert view.page.updates == 1, \
                f"0.2s 窗口内应当只下发一次，实际 {view.page.updates} 次"
            time.sleep(0.45)            # 窗口末尾的补发
            assert view.page.updates == 2, \
                f"trailing 补发没跑，最后几条改动停在客户端之外：{view.page.updates}"
        finally:
            controller.detach()


def test_flush_interval_none_keeps_updating_per_event():
    """对照：不传 `flush_interval` 就是原行为——每条事件都当场下发一次。

    没有这条对照，上一条用例可能只是"flush 整个没跑"，那才是界面永远不动的 bug。
    """
    with _LiveUiLoop() as loop:
        bus = _StubBus()
        controller = _FlushProbe(bus, flush_interval=None)
        view = _CountingView()
        controller.attach(view)
        handler = bus.handlers[ServiceStateChangedEvent]
        try:
            for index in range(5):
                asyncio.run_coroutine_threadsafe(
                    handler(ServiceStateChangedEvent(service_id=f"s{index}",
                                                     state="running")),
                    loop).result(timeout=5)
            assert [view.page.updates] == [5], \
                f"默认路径该逐条下发，实际 {view.page.updates} 次"
        finally:
            controller.detach()


def test_detach_cancels_the_pending_coalesced_flush():
    """切页时窗口里挂着的补发必须撤掉，不然它往下一份视图上刷一次没主的 update。"""
    with _LiveUiLoop() as loop:
        bus = _StubBus()
        controller = _FlushProbe(bus, flush_interval=0.2)
        view = _CountingView()
        controller.attach(view)
        handler = bus.handlers[ServiceStateChangedEvent]
        asyncio.run_coroutine_threadsafe(
            handler(ServiceStateChangedEvent(service_id="s0", state="running")),
            loop).result(timeout=5)          # leading，立刻下发
        asyncio.run_coroutine_threadsafe(
            handler(ServiceStateChangedEvent(service_id="s1", state="running")),
            loop).result(timeout=5)          # 落在窗口里，挂上补发
        assert view.page.updates == 1
        controller.detach()
        time.sleep(0.45)
        assert view.page.updates == 1, \
            f"detach 之后还补发了一次下发：{view.page.updates}"


# --------------------------------------------------------------------- #
# 循环异常归属（docs/known-issues/004 待修清单第 3 条：10054 归谁家）
# --------------------------------------------------------------------- #
def _probe_exception(target: asyncio.AbstractEventLoop, context: dict) -> str:
    """在这条循环上走一次异常处理器，返回它打出来的那行日志。"""
    return (_probe_lines(target, [context]) or [""])[0]


def _probe_lines(target: asyncio.AbstractEventLoop, contexts: list[dict]) -> list[str]:
    """连走多次，返回每次打出来的行（可能为空 = 那条被降频了）。"""
    records: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Catch()
    log = logging.getLogger("app.core.engine_worker")
    log.addHandler(handler)
    try:
        for context in contexts:
            before = len(records)
            target.call_exception_handler(context)
            if len(records) == before:
                records.append("")            # 占位：第 n 次事件没落日志
    finally:
        log.removeHandler(handler)
    return records


def test_attribution_names_the_loop_and_keeps_the_original_record():
    """认不出的循环异常照旧两遍待遇：日志里有 tag/线程名，且原样转给默认处理器。

    现场那串 ``Exception in callback _ProactorBasePipeTransport
    ._call_connection_lost(...)`` 只写了回调名，读不出是**谁家**的 socket——本进程
    同时有界面循环与 ``legado-engines`` 两条，归错家下一步就查错子系统。
    第六十五轮只把**认出的两族收尾竞态**降成一行，其余一律"打完身份再原样交出去"，
    这一条钉的就是那条边界（拿一个同样带 10054、但回调名不是收尾段的异常来试，
    免得测试本身把白名单当成"所有 10054 都滤"）。
    """
    loop = asyncio.new_event_loop()
    delegated: list[dict] = []
    try:
        install_loop_attribution(loop, "engine")
        # 实例属性顶掉内置默认处理器：只盯"有没有转出去"，不真往 stderr 打
        loop.default_exception_handler = delegated.append
        line = _probe_exception(loop, {
            "message": "Exception in callback _SSLProtocol._on_shutdown()",
            "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")})
    finally:
        loop.close()
    assert "循环[engine]" in line, f"归属日志连是谁家的循环都没写：{line!r}"
    assert "线程=" in line and "loop=0x" in line, f"归属日志缺线程名/loop id：{line!r}"
    assert "10054" in line, f"归属日志里读不出原异常：{line!r}"
    assert delegated, f"认不出的异常被吞了（现场再也看不到回调异常）：{line!r}"


# --------------------------------------------------------------------------- #
# 第六十五轮：认出的两族收尾竞态——一行带计数、不再刷 traceback、socket 就地补关
# --------------------------------------------------------------------------- #
class _FakeSock:
    """只装 `fileno/close` 的最小 socket：要能断到"补关真的发生了"。"""

    def __init__(self, fileno=4321):
        self._fileno = fileno
        self.close_calls = 0

    def fileno(self):
        return self._fileno

    def close(self):
        self.close_calls += 1
        self._fileno = -1


class _FakeTransport:
    def __init__(self, sock):
        self._sock = sock
        self._called_connection_lost = False


def _lost_context(sock: _FakeSock) -> dict:
    """现场族 1 的 context 形状：`Handle._callback` 是绑定方法，`__self__` 就是传输层。"""
    transport = _FakeTransport(sock)

    def _call_connection_lost(exc=None):     # 只为造出绑定方法的 `__self__`
        pass

    class _Bound:
        __self__ = transport

    bound = _Bound()
    bound.__name__ = "_call_connection_lost"
    return {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
        "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接"),
        "handle": type("H", (), {"_callback": bound})(),
        "_transport": transport,
    }


def test_socket_teardown_race_is_one_counted_line_and_the_socket_gets_closed():
    """认出的族 1：一行带累计计数、**不**再转给默认处理器，并补上被跳过的 `sock.close()`。

    为什么补：`proactor_events.py:154-172` 的 `finally` 顺序是 `shutdown()` → `sock.close()`
    → `_sock = None` → `_called_connection_lost = True`，复位过的 socket 在 `shutdown()`
    就抛，后三行一行都不执行。实测（`.probe/loop_teardown_cleanup_probe.py` B 臂）：40 次
    这种事件后 40/40 个 socket 的 `fileno()` 仍 ≥ 0，句柄 253 → 307，`gc.collect()` 之后
    还是 306 —— 一次竞态一个不归还的句柄。
    """
    loop = asyncio.new_event_loop()
    delegated: list[dict] = []
    sock = _FakeSock()
    try:
        install_loop_attribution(loop, "engine")
        loop.default_exception_handler = delegated.append
        context = _lost_context(sock)
        line = _probe_exception(loop, context)
        transport = context["_transport"]
    finally:
        loop.close()
    assert "收尾竞态" in line and "累计第 1 次" in line, f"没折成一条带计数的短行：{line!r}"
    assert "循环[engine]" in line and "10054" in line, f"短行丢了归属或异常本体：{line!r}"
    assert not delegated, "这一族还是被转给了默认处理器——traceback 刷屏没被止住"
    assert sock.close_calls == 1, f"CPython 跳过的 sock.close() 没补上：{sock.__dict__}"
    assert transport._sock is None, "补关后还留着 socket 引用（`__del__` 会再报 ResourceWarning）"
    assert transport._called_connection_lost is True, "闸门没置位：同一传输层会被二次收尾"


def test_every_teardown_event_gets_the_socket_back_even_while_logging_is_collapsed():
    """降频窗口里不落日志的那些次，**句柄照样要一次不落地补回来**。

    护栏钉的是"没发生"（记忆 `guard-tests-assert-absence`）：5 次事件只许出 1 行日志，
    但 5 个 socket 全部关闭。把补关挪进"要落日志"那个分支的改法会被这条抓到。
    """
    loop = asyncio.new_event_loop()
    delegated: list[dict] = []
    socks = [_FakeSock(fileno=4400 + i) for i in range(5)]
    try:
        install_loop_attribution(loop, "engine")
        loop.default_exception_handler = delegated.append
        lines = _probe_lines(loop, [_lost_context(sock) for sock in socks])
    finally:
        loop.close()
    logged = [line for line in lines if line]
    assert len(logged) == 1, f"同族 5 次落了 {len(logged)} 行，刷屏没止住：{logged}"
    assert "累计第 1 次" in logged[0], f"落的那行不是首次：{logged[0]!r}"
    assert all(s.close_calls == 1 for s in socks), \
        f"降频窗口里的次数漏补关：{[s.close_calls for s in socks]}"
    assert not delegated, "被降频的那几次还是转给了默认处理器"


def test_ssl_close_notify_race_is_counted_but_touches_no_socket():
    """现场第二族（TLS 收尾期收到应用数据）：同样折成一条计数，但它没有 socket 可补。"""
    import ssl as _ssl

    loop = asyncio.new_event_loop()
    delegated: list[dict] = []
    try:
        install_loop_attribution(loop, "engine")
        loop.default_exception_handler = delegated.append
        line = _probe_exception(loop, {
            "message": "Future exception was never retrieved",
            "exception": _ssl.SSLError(1, "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] "
                                          "application data after close notify (_ssl.c:2706)")})
    finally:
        loop.close()
    assert "收尾竞态[TLS 收尾期收到应用数据]" in line, f"第二族没被认出：{line!r}"
    assert "APPLICATION_DATA_AFTER_CLOSE_NOTIFY" in line, f"短行读不出是哪族：{line!r}"
    assert not delegated, "第二族还是被转给了默认处理器——那一大段 traceback 没被止住"


def test_recognition_needs_both_halves_of_the_fingerprint():
    """认族必须**两半都对**才算：只带回调名（异常是 ValueError）或只带 10054（回调名不认）
    都不许降级——按一半的形状滤，滤掉的可能是真故障。"""
    loop = asyncio.new_event_loop()
    delegated: list[dict] = []
    try:
        install_loop_attribution(loop, "engine")
        loop.default_exception_handler = delegated.append
        lines = _probe_lines(loop, [
            {"message": "Exception in callback _ProactorBasePipeTransport"
                        "._call_connection_lost(None)",
             "exception": ValueError("协议解析炸了")},
            {"message": "Exception in callback SomeOtherThing.run()",
             "exception": ConnectionResetError(10054, "复位")},
        ])
    finally:
        loop.close()
    assert all("收尾竞态" not in line for line in lines), f"半指纹被认成了收尾竞态：{lines}"
    assert len(delegated) == 2, f"两半都不齐的异常被滤掉了：{len(delegated)}/2 转出去"



def test_attribution_reports_a_context_without_an_exception_object():
    """现场 6 行里有几行末尾是 `None`（context 不带 exception）——那种也要落一条。"""
    loop = asyncio.new_event_loop()
    try:
        install_loop_attribution(loop, "ui")
        loop.default_exception_handler = lambda context: None
        line = _probe_exception(loop, {"message": "Exception in callback x: None"})
    finally:
        loop.close()
    assert "循环[ui]" in line, f"没有 exception 对象的 context 被跳过：{line!r}"


def test_attribution_is_installed_once_per_loop():
    """重复登记不许装两层：那会把每条噪声打成两遍，现场更难读。"""
    loop = asyncio.new_event_loop()
    try:
        install_loop_attribution(loop, "ui")
        first = loop.get_exception_handler()
        install_loop_attribution(loop, "ui")
        assert loop.get_exception_handler() is first, "同一循环被装了两层归属处理器"
    finally:
        loop.close()


def test_both_production_loops_carry_their_own_identity():
    """接线核验：引擎循环与界面循环**各自**报得出自己是谁（线程名要真是它自己的）。

    只测 :func:`install_loop_attribution` 本身不够——归属只在真循环上、真线程里
    打出来才有意义，接错线（比如界面循环没装）照样全绿（记忆
    `refactor-regression-orphan-methods`）。
    """
    hub = EngineHub(name="attribution-probe-engine")
    ui = asyncio.new_event_loop()
    saved_ui = ui_loop()
    try:
        hub.loop()                       # 首次使用即拉起守护线程
        set_ui_loop(ui)
        # 引擎侧：必须在那条循环自己的线程上打，才验得出"线程名=循环所属线程"
        async def probe_on_engine() -> str:
            return _probe_exception(asyncio.get_running_loop(),
                                    {"message": "engine 侧探针"})

        engine_line = hub.submit(probe_on_engine()).result(timeout=10)
        ui_line = _probe_exception(ui, {"message": "ui 侧探针"})
        assert "循环[engine]" in engine_line and "attribution-probe-engine" in engine_line, \
            f"引擎循环没装归属或线程名不是它自己的：{engine_line!r}"
        assert "循环[ui]" in ui_line and "attribution-probe-engine" not in ui_line, \
            f"界面循环没装归属：{ui_line!r}"
        assert "MainThread" in ui_line, f"界面侧线程名读不出是谁的线程：{ui_line!r}"
    finally:
        set_ui_loop(saved_ui)
        hub.shutdown()
        ui.close()


def test_controller_still_marshals_to_a_live_ui_loop():
    """对照：循环活着时照旧编组过去执行（别把守卫写成"逢事件就丢"）。"""
    live = asyncio.new_event_loop()
    thread = threading.Thread(target=live.run_forever, name="ui-loop-stub",
                              daemon=True)
    thread.start()
    saved = ui_loop()
    set_ui_loop(live)
    bus = _StubBus()
    controller = _ShutdownProbe(bus)
    try:
        controller.attach(_StubView())
        _dispatch(bus, ServiceStateChangedEvent(service_id="x", state="running"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and controller.seen == 0:
            time.sleep(0.02)
        assert controller.seen == 1, "活着的界面循环上，事件应被编组执行"
    finally:
        set_ui_loop(saved)
        controller.detach()
        live.call_soon_threadsafe(live.stop)
        thread.join(timeout=5)
        live.close()
