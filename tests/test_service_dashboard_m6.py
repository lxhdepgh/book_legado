"""「配置 → 服务」页测试（集成契约 §5 / 设计 §4.5 / §7.4）。

不依赖真实 Flet 窗口与 EngineHub：ft 控件可无窗口实例化；hub/bus/submit 全部
用 stub 注入。覆盖：

1. 工厂离线构造：**返回可嵌入的 ft.Column**（曾经是 ft.View，塞进页签容器的
   Container 里 Flutter 不渲染 → 整页灰底），并挂 detach 钩子；
2. 快照渲染：服务名 / 所属域 / 中文状态标签 / 进度条与百分比 / 最近错误 / 行数；
3. 总览行：总数与各态计数，RUNNING 带 error 记为"降级"；
4. 控制按钮：suspend/resume/stop 经 submit 派发，restart 派**总控的一次编排协程**；
   hub 自身行（service_by_id=None）四按钮全禁且不派发；DISPOSED 行只放开重启；
5. 事件流：订阅/退订对称、环形缓冲 50 上限、状态事件入流并刷新快照；
6. 心跳：快照签名未变不重表，变了才重读；无运行循环时不起心跳；
7. 渲染路径对未知状态取值不抛异常。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import flet as ft

from app.core.events import ServiceStateChangedEvent, SettingsChangedEvent
from app.core.lifecycle import ServiceState, ServiceStatus
from app.views.service_dashboard import (DashboardController, DashboardView,
                                         _MAX_EVENTS, _domain_of,
                                         build_dashboard)


# --------------------------------------------------------------------------- #
# 离线替身
# --------------------------------------------------------------------------- #
class _FakeBus:
    """记录订阅的假 EventBus：subscribe 返回幂等退订句柄。"""

    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

        def unsubscribe():
            if (event_type, handler) in self.subscriptions:
                self.subscriptions.remove((event_type, handler))
        return unsubscribe


class _StubService:
    """记录 suspend/resume/stop 调用的服务替身（async 方法，与基类签名一致）。"""

    def __init__(self, name: str):
        self.name = name
        self.actions: list[str] = []

    async def suspend(self):
        self.actions.append("suspend")

    async def resume(self):
        self.actions.append("resume")

    async def stop(self):
        self.actions.append("stop")


async def _ready(value):
    """替身用的"已就绪"协程：真 restart_service 是 async def，派发方拿到的是协程对象。"""
    return value


class _FakeHub:
    """stub ServiceHub：status_report 返回注入快照并计数，service_by_id 按表查。"""

    def __init__(self, statuses: list[ServiceStatus], services: dict):
        self.bus = _FakeBus()
        self._statuses = statuses
        self._services = services  # service_id -> _StubService
        self.report_calls = 0
        #: 重启编排的记账（真实实现是 ServiceHub.restart_service）
        self.restart_calls: list[str] = []

    def status_report(self) -> list[ServiceStatus]:
        self.report_calls += 1
        return list(self._statuses)

    def service_by_id(self, service_id: str):
        return self._services.get(service_id)

    def restartable(self, service_id: str) -> bool:
        return self.service_by_id(service_id) is not None

    def restart_service(self, service_id: str):
        """真实现是 ``async def``；替身把记账放在**同步段**、返回一条待跑的协程。

        这样"已经派出去了但还没跑完"的在途形状也测得到（派发方只拿到协程对象）。
        """
        self.restart_calls.append(service_id)
        return _ready([service_id] if self.restartable(service_id) else [])


class _StubPage:
    """记录 update() 调用的 Page 替身。"""

    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


def _run(coro):
    """测试用派发器：同步跑掉协程（不碰 EngineHub 守护线程）。"""
    asyncio.run(coro)


def _make_hub() -> _FakeHub:
    """四行快照：hub 自身（不可控）+ RUNNING + 降级 RUNNING + SUSPENDED。"""
    statuses = [
        ServiceStatus(service_id="ServiceHub", state=ServiceState.RUNNING,
                      started_at=1_700_000_000.0, updated_at=1_700_000_000.0),
        ServiceStatus(service_id="BookService", state=ServiceState.RUNNING,
                      progress=0.5, started_at=1_700_000_001.0),
        ServiceStatus(service_id="CheckService", state=ServiceState.RUNNING,
                      last_error="必需协作者未就绪", started_at=1_700_000_002.0),
        ServiceStatus(service_id="BrowserService", state=ServiceState.SUSPENDED,
                      last_error="boom", started_at=1_700_000_003.0),
    ]
    services = {
        "BookService": _StubService("BookService"),
        "CheckService": _StubService("CheckService"),
        "BrowserService": _StubService("BrowserService"),
        # ServiceHub 自身不注册进 _services → service_by_id 返回 None
    }
    return _FakeHub(statuses, services)


def _view_stub(hub, submit=_run):
    """真视图 + 注入 hub 的控制器（离线构造，未挂载到 page）。"""
    view = DashboardView()
    controller = DashboardController(hub, submit=submit)
    return view, controller


def _cells(row):
    return [cell.content for cell in row.cells]


def _progress_of(row):
    """进度格是 Row(ProgressBar + Text)。"""
    return _cells(row)[3]


# --------------------------------------------------------------------------- #
# 1. 工厂与形状
# --------------------------------------------------------------------------- #
class FactoryShapeTests(unittest.TestCase):

    def test_returns_embeddable_column_not_view(self):
        """目的地必须是可嵌入控件：ft.View 嵌在 Container 里根本不渲染（灰底根因）。"""
        hub = _make_hub()
        view = build_dashboard(_StubPage(), hub)
        self.assertIsInstance(view, ft.Column)
        self.assertNotIsInstance(view, ft.View)
        self.assertTrue(view.expand, "页签容器里要撑满内容区")
        self.assertTrue(callable(getattr(view, "detach", None)),
                        "必须挂可调用 detach()（§7.4 离开分组时退订）")

    def test_offline_build_does_not_crash_on_unmounted_push(self):
        """未挂载时 Control.page 抛 RuntimeError，渲染路径必须自己兜住。"""
        view = build_dashboard(_StubPage(), _make_hub())
        self.assertTrue(view.service_table.rows)

    def test_factory_end_to_end_detach_unsubscribes(self):
        hub = _make_hub()
        view = build_dashboard(_StubPage(), hub)
        self.assertGreaterEqual(len(hub.bus.subscriptions), 2,
                                "构建即应订阅 ServiceStateChangedEvent 等事件")
        view.detach()
        self.assertEqual(len(hub.bus.subscriptions), 0)

    def test_domain_labels_from_module_path(self):
        """所属域按实例模块路径推（视图不 import 服务实现类）。"""
        for module, label in (
                ("app.core.bus", "基础设施"),
                ("app.services.sources.source_service", "书源域"),
                ("app.services.reading.reading_service", "阅读域"),
                ("app.services.tools.font_service", "工具域"),
                ("somewhere.else", "其他")):
            fake = type("Fake", (), {})
            fake.__module__ = module
            self.assertEqual(_domain_of(fake()), label, module)
        self.assertEqual(_domain_of(None), "总控")


# --------------------------------------------------------------------------- #
# 2. 快照渲染
# --------------------------------------------------------------------------- #
class SnapshotRenderTests(unittest.TestCase):

    def setUp(self):
        self.hub = _make_hub()
        self.view = build_dashboard(_StubPage(), self.hub)
        self.rows = self.view.service_table.rows

    def test_header_covers_state_columns(self):
        headers = [c.label.value for c in
                   self.view.service_table.columns]
        self.assertEqual(headers,
                         ["服务", "所属域", "状态", "进度", "最近错误", "启动时间", "操作"])

    def test_rows_names_and_domain(self):
        self.assertEqual(len(self.rows), 4)
        self.assertEqual([_cells(r)[0].value for r in self.rows],
                         ["ServiceHub", "BookService", "CheckService", "BrowserService"])
        # hub 自身行取不到实例 → 总控；测试替身不在 app.services.* 下 → 其他
        self.assertEqual(_cells(self.rows[0])[1].value, "总控")
        self.assertEqual(_cells(self.rows[1])[1].value, "其他")

    def test_state_labels_are_chinese(self):
        """状态给用户看中文，不裸露 RUNNING/SUSPENDED 内部取值。"""
        labels = [_cells(r)[2].value for r in self.rows]
        self.assertEqual(labels, ["运行中", "运行中", "运行中", "已挂起"])
        self.assertNotIn("RUNNING", labels)

    def test_progress_cell_shows_bar_and_percent(self):
        cell = _progress_of(self.rows[1])
        bar = next(c for c in cell.controls if isinstance(c, ft.ProgressBar))
        text = next(c for c in cell.controls if isinstance(c, ft.Text))
        self.assertAlmostEqual(bar.value, 0.5)
        self.assertTrue(bar.visible)
        self.assertEqual(text.value, "50%")
        # 没有进度概念的服务：条收起、百分比是 "-"
        none_cell = _progress_of(self.rows[0])
        none_bar = next(c for c in none_cell.controls if isinstance(c, ft.ProgressBar))
        self.assertFalse(none_bar.visible)
        self.assertEqual(next(c for c in none_cell.controls
                              if isinstance(c, ft.Text)).value, "-")

    def test_error_and_started_time_rendered(self):
        self.assertEqual(_cells(self.rows[3])[4].value, "boom")
        self.assertEqual(_cells(self.rows[0])[4].value, "-")
        started = _cells(self.rows[1])[5].value
        self.assertRegex(started, r"^\d{2}:\d{2}:\d{2}$", "启动时间渲染成时:分:秒")

    def test_summary_line_counts_degraded(self):
        """总览：4 个服务 · 运行 3 · 已挂起 1 · 降级 1（RUNNING 但带 error）。"""
        summary = _cells_of_summary(self.view)
        self.assertIn("共 4 个服务", summary)
        self.assertIn("运行 3", summary)
        self.assertIn("已挂起 1", summary)
        self.assertIn("降级 1", summary)

    def test_unknown_state_value_does_not_raise(self):
        """状态取值不认识时原样透出，渲染路径不能抛。"""
        from app.views.service_dashboard import _state_label
        self.assertEqual(_state_label("WHATEVER"), "WHATEVER")
        self.assertEqual(_state_label(ServiceState.RUNNING), "运行中")


def _cells_of_summary(view) -> str:
    """总览行文本（不依赖它在 controls 里的下标）。"""
    for control in view.controls:
        text = getattr(control, "value", None)
        if isinstance(text, str) and "个服务" in text:
            return text
        for child in list(getattr(control, "controls", None) or []):
            value = getattr(child, "value", None)
            if isinstance(value, str) and "个服务" in value:
                return value
    return ""


# --------------------------------------------------------------------------- #
# 3. 启停控制
# --------------------------------------------------------------------------- #
class ControlDispatchTests(unittest.TestCase):

    def test_suspend_resume_stop_dispatched_to_stub(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        ctrl.control("BookService", "suspend")
        ctrl.control("BookService", "resume")
        ctrl.control("BookService", "stop")
        self.assertEqual(hub._services["BookService"].actions,
                         ["suspend", "resume", "stop"])

    def test_control_unknown_service_is_noop(self):
        """hub 自身行（service_by_id=None）点击控制不产生任何派发。"""
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        ctrl.control("ServiceHub", "suspend")
        self.assertEqual(hub._services["BookService"].actions, [])
        self.assertEqual(len(ctrl.events), 0)

    def test_control_click_reads_button_data(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)

        class _Btn:
            control = SimpleNamespace(data=("stop", "BrowserService"))

        ctrl._on_control_click(_Btn())
        self.assertEqual(hub._services["BrowserService"].actions, ["stop"])

    def test_buttons_disabled_by_state(self):
        """状态机裁决：hub 自身行全禁；RUNNING 挂起/停止/重启可点、恢复禁；SUSPENDED 反向。"""
        hub = _make_hub()
        view = build_dashboard(_StubPage(), hub)
        rows = view.service_table.rows
        hub_buttons = _cells(rows[0])[6].controls
        self.assertTrue(all(b.disabled for b in hub_buttons),
                        "ServiceHub 自身行无对应实例，启停按钮应全部禁用")
        book_buttons = _cells(rows[1])[6].controls
        self.assertEqual([b.disabled for b in book_buttons],
                         [False, True, False, False],
                         "RUNNING：挂起可点、恢复禁、停止可点、重启可点")
        browser_buttons = _cells(rows[3])[6].controls
        self.assertEqual([b.disabled for b in browser_buttons],
                         [True, False, False, False],
                         "SUSPENDED：挂起禁、恢复可点、停止可点、重启可点")

    def test_disposed_row_only_restart_is_clickable(self):
        """DISPOSED（已退役）那一行只有「重启」可点——这就是就地拉回来的唯一出口。"""
        statuses = [ServiceStatus(service_id="DeadService",
                                  state=ServiceState.DISPOSED)]
        hub = _FakeHub(statuses, {"DeadService": _StubService("DeadService")})
        view = build_dashboard(_StubPage(), hub)
        buttons = _cells(view.service_table.rows[0])[6].controls
        self.assertEqual([b.disabled for b in buttons], [True, True, True, False],
                         "退役行应只放开重启")

    def test_restart_dispatches_one_hub_orchestration(self):
        """重启派的是**总控的一次编排协程**，不是服务自己的方法，也不能拆成两次派发。

        分成 stop()+start() 两次 submit 之间，引擎循环可以插进别的协程，那就出现
        "依赖已经停了、消费者还照原样跑"的中间态；下游连带也归总控算，视图不知道拓扑。
        """
        hub = _make_hub()
        submits: list = []

        def record_submit(coro):
            submits.append(coro)
            return None          # 与 _default_submit 同形：真实实现返 future

        view, ctrl = _view_stub(hub, submit=record_submit)
        ctrl.attach(view)
        ctrl.control("BookService", "restart")

        self.assertEqual(len(submits), 1, f"重启必须只派发一次：{len(submits)}")
        for coro in submits:
            coro.close()         # 只记录不跑：协程对象别在测试里留未消费警告
        self.assertEqual(hub.restart_calls, ["BookService"])
        self.assertEqual(hub._services["BookService"].actions, [],
                         "重启不该走服务自己的方法面")

    def test_restart_is_not_dispatched_for_uncontrollable_row(self):
        """总控自身行（service_by_id=None）不产生任何重启派发——重启 EngineHub 会把
        正在执行这次重启的守护 loop 自己拆掉。"""
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        ctrl.control("ServiceHub", "restart")
        self.assertEqual(hub.restart_calls, [])

    def test_restart_in_flight_blocks_second_click(self):
        """一轮重启没跑完之前，同一行不能再点第二次（别把一串服务停两遍）。"""
        hub = _make_hub()
        held: list = []

        class _PendingFuture:
            def add_done_callback(self, fn):
                held.append(fn)

        def submit(coro):
            coro.close()          # 只派不跑：真实实现是 run_coroutine_threadsafe
            return _PendingFuture()

        view, ctrl = _view_stub(hub, submit=submit)
        ctrl.attach(view)
        ctrl.control("BookService", "restart")
        self.assertEqual(hub.restart_calls, ["BookService"])
        ctrl.control("BookService", "restart")
        self.assertEqual(hub.restart_calls, ["BookService"], "在途中又被派了一遍")
        self.assertEqual(len(held), 1, "收尾必须挂 done 回调，不然在途标记永远不清")
        # 收尾回调只做两件事：清标记 + 把重画编回界面循环（后者走 ui_call，
        # 无头环境里没有界面循环，这里直接验清标记那一步）。
        ctrl._restart_done("BookService")
        self.assertNotIn("BookService", ctrl._restarting)
        ctrl.control("BookService", "restart")
        self.assertEqual(hub.restart_calls, ["BookService", "BookService"],
                         "收尾之后应允许再来一轮")

    def test_refresh_button_slot_bound(self):
        """手动刷新按钮的槽位由控制器填（视图不持服务）。"""
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        self.assertIs(getattr(view.on_refresh, "__self__", None), ctrl)
        before = hub.report_calls
        view.on_refresh()
        self.assertGreater(hub.report_calls, before)


# --------------------------------------------------------------------------- #
# 4. 事件流与生命周期
# --------------------------------------------------------------------------- #
class EventStreamTests(unittest.TestCase):

    def test_subscribe_detach_symmetric(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        before = len(hub.bus.subscriptions)
        ctrl.attach(view)
        self.assertEqual(len(hub.bus.subscriptions) - before, 2)
        kinds = {et for et, _ in hub.bus.subscriptions}
        self.assertIn(ServiceStateChangedEvent, kinds)
        self.assertIn(SettingsChangedEvent, kinds)
        ctrl.detach()
        self.assertEqual(len(hub.bus.subscriptions), before,
                         "detach 必须对称退订（§7.4 防泄漏）")

    def test_ring_buffer_capped_at_max_events(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        for i in range(60):
            _run(ctrl._on_state_changed(
                ServiceStateChangedEvent(service_id=f"S{i:02d}", state="RUNNING")))
        self.assertEqual(len(ctrl.events), _MAX_EVENTS)
        self.assertEqual(_MAX_EVENTS, 50)
        rendered = [t.value for t in view.event_log.controls]
        self.assertTrue(rendered[0].startswith("["), "新事件置顶")
        self.assertIn("S59", rendered[0])
        self.assertFalse(any("S00" in line for line in rendered),
                         "最旧事件应被环形缓冲挤出")

    def test_state_event_logs_chinese_and_refreshes_table(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        _run(ctrl._on_state_changed(
            ServiceStateChangedEvent(service_id="BookService", state="SUSPENDED")))
        self.assertEqual(len(ctrl.events), 1)
        rendered = view.event_log.controls[0].value
        self.assertIn("BookService", rendered)
        self.assertIn("已挂起", rendered, "事件流也别裸露内部枚举名")
        # 状态事件顺带重读快照
        self.assertGreater(hub.report_calls, 1)

    def test_settings_event_logged(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        _run(ctrl._on_settings_changed(SettingsChangedEvent(key="theme.skin")))
        self.assertIn("theme.skin", view.event_log.controls[0].value)

    def test_detach_is_idempotent(self):
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        ctrl.detach()
        ctrl.detach()
        self.assertEqual(len(hub.bus.subscriptions), 0)

    def test_heartbeat_skips_unchanged_snapshot_and_picks_up_changes(self):
        """心跳：服务没变就不重表（进度没事件可订阅，全靠这条心跳）。"""
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        renders: list = []
        original = view.render_rows
        view.render_rows = lambda rows, summary: (renders.append(summary),
                                                  original(rows, summary))
        ctrl.attach(view)
        baseline = len(renders)
        ctrl.refresh_if_changed()
        ctrl.refresh_if_changed()
        self.assertEqual(len(renders), baseline, "签名没变不该重表刷屏")
        # BrowserService 进度从 None → 0.4：签名变了，必须重表
        hub._statuses[3].progress = 0.4
        ctrl.refresh_if_changed()
        self.assertEqual(len(renders), baseline + 1)
        self.assertEqual(_progress_of(view.service_table.rows[3]).controls[1].value,
                         "40%")

    def test_ticker_not_started_without_running_loop(self):
        """同步测试环境没有运行中的循环：不该凭空起心跳。"""
        hub = _make_hub()
        view, ctrl = _view_stub(hub)
        ctrl.attach(view)
        self.assertIsNone(ctrl._ticker)
        ctrl.detach()

    def test_ticker_starts_and_is_cancelled_on_detach(self):
        """在循环上 attach 会起心跳，detach 必须取消（否则切页后还在刷）。"""
        hub = _make_hub()

        async def scenario():
            view, ctrl = _view_stub(hub)
            ctrl.attach(view)
            ticker = ctrl._ticker
            started = ticker is not None and not ticker.done()
            ctrl.detach()
            await asyncio.sleep(0)
            return started, ticker, ctrl._ticker

        started, ticker, after_detach = asyncio.run(scenario())
        self.assertTrue(started, "界面循环上应当起心跳")
        self.assertIsNone(after_detach, "detach 后不再持有心跳任务")
        self.assertTrue(ticker.cancelled(), "detach 必须取消心跳任务")


if __name__ == "__main__":
    unittest.main()
