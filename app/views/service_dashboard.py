"""ServiceDashboard：配置 → 「服务」页（§4.5 状态可见 / §7.4 生命周期）。

展示启动后注册的全部 31 行（实测：``ServiceHub`` 总控 1 + 基础设施 4 + 业务服务 26，
业务按域分成 规则引擎 1 / 阅读 6 / 书源 5 / 内容形态 4 / 工具 10）的实时状态：
名 / 所属域 / 状态 / 进度 / 最近错误 / 启动时间 + 每行挂起·恢复·停止。
数据面只经 hub 公开接口（``status_report`` / ``service_by_id`` / ``bus``），
不 import 任何服务实现类（红线 v2），也不持有服务实例列表。

两处踩过的坑（都表现为"整页灰底"）：

1. **目的地必须是可嵌入控件，不能是 ``ft.View``**。本视图早期返回
   ``ft.View(route="/dashboard")``，而 M6 之后目的地由 ``SectionView`` 塞进
   ``ft.Container`` 的页签里——``View`` 是页面级路由容器，嵌在控件树里 Flutter
   直接不渲染，看着就是一块灰砖。现在与其余目的地同构（``ft.Column``）。
2. **事件处理器必须在界面循环上改控件**。EventBus 把 async 处理器投递到 EngineHub
   守护 loop，早期版本在那里直接写控件并 ``page.update()``——跨线程且没人 flush，
   服务状态变了界面也不动。现在控制器走 :class:`BaseController`，
   ``subscribe`` 统一编组回界面循环并在收尾 flush。

进度不由服务发事件（``status.progress`` 是 RSS / 下载 / 检测就地写的），所以
挂载期间有一个 2s 心跳重读快照；快照签名没变就不重建表格，避免无谓整页下发。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from collections import deque
from typing import Any, Callable

import flet as ft

from app.core.engine_worker import engine_hub
from app.core.events import ServiceStateChangedEvent, SettingsChangedEvent
from app.core.hub import ServiceHub
from app.core.lifecycle import ServiceState, ServiceStatus
from app.controllers.base import BaseController

#: 事件流面板保留的最近事件条数（契约 §5：环形缓冲最近 50 条）。
_MAX_EVENTS = 50

#: 挂载期间重读快照的心跳间隔（秒）：进度只写 ``status.progress``，没有事件可订阅。
AUTO_REFRESH_SECONDS = 2.0

#: 状态 → 中文标签 + 颜色（只读提示，不驱动逻辑）。
_STATE_LABELS: dict[ServiceState, tuple[str, str]] = {
    ServiceState.REGISTERED: ("未启动", ft.Colors.GREY),
    ServiceState.INIT: ("初始化", ft.Colors.GREY),
    ServiceState.STARTING: ("启动中", ft.Colors.AMBER),
    ServiceState.RUNNING: ("运行中", ft.Colors.GREEN),
    ServiceState.SUSPENDED: ("已挂起", ft.Colors.ORANGE),
    ServiceState.STOPPING: ("停止中", ft.Colors.RED_300),
    ServiceState.DISPOSED: ("已停止", ft.Colors.RED),
}

#: 服务模块路径片段 → 所属域（仪表盘按域给人一眼看得懂的分组，不 import 服务类）。
_DOMAIN_LABELS: tuple[tuple[str, str], ...] = (
    ("app.core", "基础设施"),
    ("app.services.engine", "规则引擎"),
    ("app.services.sources", "书源域"),
    ("app.services.reading", "阅读域"),
    ("app.services.content", "内容形态"),
    ("app.services.tools", "工具域"),
    ("app.services", "旧层"),
)


def _state_label(state: Any) -> str:
    """状态取值 → 中文标签；不认识的值原样透出（渲染路径不抛异常）。"""
    if isinstance(state, str):
        try:
            state = ServiceState(state)
        except ValueError:
            return state
    return _STATE_LABELS.get(state, (str(state),))[0]


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _fmt_progress(p: float | None) -> str:
    if p is None:
        return "-"
    return f"{p * 100:.0f}%"


def _domain_of(svc: Any) -> str:
    """按实例的模块路径推所属域；取不到实例（hub 自身行）算「总控」。"""
    if svc is None:
        return "总控"
    module = type(svc).__module__ or ""
    for prefix, label in _DOMAIN_LABELS:
        if module.startswith(prefix):
            return label
    return "其他"


def _control_enabled(state: ServiceState, action: str) -> bool:
    """按状态机裁决单行按钮是否可用（只读快照语义，过渡态不响应）。

    - RUNNING：可挂起、可停止；
    - SUSPENDED：可恢复、可停止；
    - 其余（REGISTERED/INIT/STARTING/STOPPING/DISPOSED）：全部禁用。
    """
    if state == ServiceState.RUNNING:
        return action in ("suspend", "stop")
    if state == ServiceState.SUSPENDED:
        return action in ("resume", "stop")
    return False


class DashboardController(BaseController):
    """仪表盘控制器：快照渲染 + 启停派发 + 事件流（§4.5 / §7.4）。

    继承 :class:`BaseController` 拿到两件事：事件处理器编组回界面循环、
    收尾统一 ``flush``；``detach`` 时对称退订并停心跳。
    """

    destination = "settings.dashboard"

    def __init__(self, hub: ServiceHub, bus=None,
                 submit: Callable | None = None) -> None:
        super().__init__(hub, bus)
        # 协程派发器：默认走进程级 EngineHub；测试可注入跑掉/记录协程。
        self._submit = submit if submit is not None else self._default_submit
        #: 最近事件环形缓冲（EventBus 单处理器串行投递保证线程语义）。
        self.events: deque = deque(maxlen=_MAX_EVENTS)
        self._ticker: asyncio.Task | None = None
        self._signature: tuple | None = None

    # ------------------------------------------------------------------ #
    # 生命周期（§7.4 attach/detach 对称）
    # ------------------------------------------------------------------ #
    def on_attach(self) -> None:
        self._view.on_refresh = self.refresh
        self.subscribe(ServiceStateChangedEvent, self._on_state_changed)
        self.subscribe(SettingsChangedEvent, self._on_settings_changed)
        self._start_ticker()
        self.refresh()

    def on_detach(self) -> None:
        """停心跳（退订由基类统一做；重复 detach 安全）。"""
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()

    def _start_ticker(self) -> None:
        """没有运行中的界面循环（无头/构造期）时不起心跳，退化为事件驱动刷新。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ticker = loop.create_task(self._beat())

    async def _beat(self) -> None:
        while True:
            try:
                await asyncio.sleep(AUTO_REFRESH_SECONDS)
            except asyncio.CancelledError:
                raise
            if self._view is None:
                return
            self.refresh_if_changed()

    # ------------------------------------------------------------------ #
    # 快照渲染（只读，§4.5）
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """从 hub.status_report() 重读快照，重建服务表行（不触发任何服务动作）。"""
        if self._view is None:
            return
        statuses = list(self._hub.status_report())
        self._signature = self._signature_of(statuses)
        self._view.render_rows([self._row(st) for st in statuses],
                               self._summary_of(statuses))

    def refresh_if_changed(self) -> None:
        """心跳用：快照没任何变化就不整表重建。"""
        if self._view is None:
            return
        signature = self._signature_of(list(self._hub.status_report()))
        if signature != self._signature:
            self.refresh()

    @staticmethod
    def _signature_of(statuses) -> tuple:
        return tuple((st.service_id, st.state, st.progress, st.last_error)
                     for st in statuses)

    @staticmethod
    def _summary_of(statuses) -> str:
        """一行总览：总数 + 各态计数，降级（RUNNING 但有 last_error）单列。"""
        running = sum(1 for st in statuses if st.state == ServiceState.RUNNING)
        degraded = sum(1 for st in statuses
                       if st.state == ServiceState.RUNNING and st.last_error)
        suspended = sum(1 for st in statuses
                        if st.state == ServiceState.SUSPENDED)
        stopped = sum(1 for st in statuses
                      if st.state in (ServiceState.DISPOSED, ServiceState.STOPPING))
        parts = [f"共 {len(statuses)} 个服务", f"运行 {running}"]
        if suspended:
            parts.append(f"已挂起 {suspended}")
        if degraded:
            parts.append(f"降级 {degraded}")
        if stopped:
            parts.append(f"已停 {stopped}")
        return " · ".join(parts)

    def _row(self, st: ServiceStatus) -> ft.DataRow:
        label, color = _STATE_LABELS.get(st.state, (str(st.state), ft.Colors.GREY))
        # RUNNING 但带 last_error（必需协作者缺失）→ 状态文字转橙，
        # 只提示"运行中但降级"，不迁移 SUSPENDED 语义（state 仍 RUNNING）。
        if st.state == ServiceState.RUNNING and st.last_error:
            color = ft.Colors.ORANGE
        # 该行是否可控制：hub.service_by_id 能取到实例才可点（hub 自身行=None）。
        svc = self._hub.service_by_id(st.service_id)
        controllable = svc is not None
        buttons = []
        for action, icon, tip in (
                ("suspend", ft.Icons.PAUSE, "挂起（释放重资源）"),
                ("resume", ft.Icons.PLAY_ARROW, "恢复"),
                ("stop", ft.Icons.STOP, "停止")):
            buttons.append(ft.IconButton(
                icon=icon, tooltip=tip, icon_size=16,
                data=(action, st.service_id),
                on_click=self._on_control_click,
                disabled=not (controllable and _control_enabled(st.state, action))))
        progress = ft.Row(spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                          controls=[
                              ft.ProgressBar(width=60, height=8, bar_height=4,
                                             border_radius=2,
                                             visible=st.progress is not None,
                                             value=st.progress),
                              ft.Text(_fmt_progress(st.progress), size=12),
                          ])
        return ft.DataRow(cells=[
            ft.DataCell(ft.Text(st.service_id, selectable=True)),
            ft.DataCell(ft.Text(_domain_of(svc), size=12, color=ft.Colors.GREY_600)),
            ft.DataCell(ft.Text(label, color=color, weight=ft.FontWeight.W_600)),
            ft.DataCell(progress),
            ft.DataCell(ft.Text(st.last_error or "-", size=12,
                                color=ft.Colors.RED_300, selectable=True)),
            ft.DataCell(ft.Text(_fmt_time(st.started_at), size=12)),
            ft.DataCell(ft.Row(buttons, spacing=0)),
        ])

    # ------------------------------------------------------------------ #
    # 启停控制（经 hub 公开接口取实例，协程经 EngineHub 派发，不阻塞 UI）
    # ------------------------------------------------------------------ #
    def _on_control_click(self, e) -> None:
        action, service_id = e.control.data
        self.control(service_id, action)

    def control(self, service_id: str, action: str) -> None:
        """对单行服务发起 suspend/resume/stop；服务不存在或派发失败均静默兜底。"""
        svc = self._hub.service_by_id(service_id)
        if svc is None:
            return
        method = getattr(svc, action, None)
        if method is None:
            return
        try:
            self._submit(method())  # method() 产出协程对象，由派发器排进 EngineHub
        except Exception:  # 派发异常不冒泡到 Flet 事件回调
            pass

    @staticmethod
    def _default_submit(coro):
        """默认派发：run_coroutine_threadsafe 到 EngineHub 守护 loop（不阻塞调用方）。"""
        return engine_hub().submit(coro)

    # ------------------------------------------------------------------ #
    # 事件流（环形缓冲最近 50 条；handler 经基类编组回界面循环）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt_event(event: Any) -> str:
        ts = getattr(event, "timestamp", None)
        head = _fmt_time(ts) if ts else _fmt_time(None)
        name = type(event).__name__
        if isinstance(event, ServiceStateChangedEvent):
            tail = f"{event.service_id} → {_state_label(event.state)}"
            if event.error:
                tail += f"（{event.error}）"
        elif isinstance(event, SettingsChangedEvent):
            tail = f"{event.key}"
        else:
            tail = repr(event)
        return f"[{head}] {name}: {tail}"

    def _log_event(self, event: Any) -> None:
        self.events.append(event)
        if self._view is not None:
            # 新事件置顶；面板只渲染最近缓冲（与 deque 上限一致）
            self._view.render_events([self._fmt_event(ev) for ev in reversed(self.events)])

    async def _on_state_changed(self, event: ServiceStateChangedEvent) -> None:
        """服务状态迁移：记事件流，并重读快照刷新服务表（§4.5 实时展示）。"""
        self._log_event(event)
        self.refresh()

    async def _on_settings_changed(self, event: SettingsChangedEvent) -> None:
        self._log_event(event)


class DashboardView(ft.Column):
    """「配置 → 服务」页本体：总览行 + 服务表 + 事件流（可嵌入页签容器）。"""

    #: 表头（顺序与 :meth:`DashboardController._row` 的单元格一一对应）。
    COLUMNS = ("服务", "所属域", "状态", "进度", "最近错误", "启动时间", "操作")

    def __init__(self) -> None:
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO, spacing=0)

        #: 回调槽（Controller 在 attach 时填）：手动重读快照。
        self.on_refresh: Callable[[], None] | None = None

        self._summary = ft.Text("（正在读取服务状态…）", size=13,
                                color=ft.Colors.GREY_600, selectable=True)
        self.service_table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(name)) for name in self.COLUMNS],
            rows=[], column_spacing=22, heading_row_height=40,
            data_row_min_height=40, data_row_max_height=56)
        self.event_log = ft.Column(
            controls=[ft.Text("（暂无事件）", size=12, color=ft.Colors.GREY_600)],
            scroll=ft.ScrollMode.AUTO, height=200, spacing=2)

        self.controls = [
            ft.Text("服务仪表盘", size=26, weight=ft.FontWeight.BOLD),
            ft.Text("进程内全部服务的实时状态。挂起会释放该服务的重资源（如浏览器），"
                    "停止后本页不会自动重启，重启应用即恢复。",
                    size=13, color=ft.Colors.GREY_600),
            ft.Row([
                self._summary,
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="立即重读快照",
                              on_click=lambda e: self.on_refresh
                              and self.on_refresh()),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            # DataTable 宽表：套一层可横向滚动的 Row，窄窗下不至于把列裁掉看不见。
            ft.Row([self.service_table], scroll=ft.ScrollMode.AUTO),
            ft.Divider(height=1),
            ft.Text("事件流（最近 50 条）", size=14, weight=ft.FontWeight.W_600),
            self.event_log,
        ]

    # -- 渲染接口（Controller 调用） ------------------------------------ #
    def render_rows(self, rows: list, summary: str) -> None:
        self.service_table.rows = list(rows)
        self._summary.value = summary or ""
        self._push()

    def render_events(self, lines: list) -> None:
        self.event_log.controls = [
            ft.Text(line, size=12, selectable=True) for line in lines] or [
            ft.Text("（暂无事件）", size=12, color=ft.Colors.GREY_600)]
        self._push()

    def _push(self) -> None:
        """改完结尾下发；未挂载（无头测试）时静默跳过。

        ``Control.page`` 在未挂载时抛 ``RuntimeError``（不是返回 None，
        ``getattr(..., None)`` 兜不住），所以取值本身也要包起来。
        """
        try:
            page = self.page
        except Exception:
            return
        try:
            page.update()
        except Exception:
            pass


def build_dashboard(page: ft.Page | None, hub: ServiceHub) -> DashboardView:
    """组装仪表盘（M1 兼容签名 ``build_dashboard(page, hub)``）。

    ``page`` 形参保留只为兼容旧调用方；控件下发一律走视图自己的 ``page``，
    不持引用（导航容器每次切换目的地都会重建本工厂的产物）。
    返回的视图挂 ``detach()``——离开「配置」分组时由导航方调用以对称退订（§7.4）。
    """
    view = DashboardView()
    controller = DashboardController(hub)
    controller.attach(view)
    view.detach = controller.detach
    return view
