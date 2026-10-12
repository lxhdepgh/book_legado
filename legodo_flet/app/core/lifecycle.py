"""服务生命周期：Service 基类、ServiceState 状态机、ServiceStatus 快照。

对应设计文档：

- §4.4 生命周期与启停：状态机
  ``REGISTERED → INIT → STARTING → RUNNING ⇄ SUSPENDED → STOPPING → DISPOSED``；
- §4.5 状态可见性：每个服务暴露 ``status: ServiceStatus``；
- §9.2 关键抽象表：Service / ServiceStatus / ServiceState 的成员约定。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 避免运行期循环导入，类型标注仅给 IDE/检查器看
    from app.core.bus import EventBus
    from app.core.hub import ServiceHub


class ServiceState(Enum):
    """服务状态机精确取值（§4.4，冻结词汇表，字符串值不得改动）。"""

    REGISTERED = "REGISTERED"
    INIT = "INIT"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    STOPPING = "STOPPING"
    DISPOSED = "DISPOSED"


@dataclass
class ServiceStatus:
    """单个服务的可观测快照（§4.5），仪表盘只读消费。

    说明：设计 §9.2 列出 ``state/last_error/started_at/updated_at/progress`` 五个字段；
    这里额外携带 ``service_id``——仪表盘按行展示"服务名"，快照自包含才能让
    ``ServiceHub.status_report()`` 的返回值脱离 Service 实例独立渲染。
    """

    service_id: str
    state: ServiceState
    last_error: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    progress: float | None = None  # 0-1 可选，无进度概念的服务留空


class Service:
    """所有进程内服务的基类（§9.2）。

    子类约定：

    - ``name``：服务名（冻结词汇表，§0.4），仪表盘展示用；
    - ``dependencies``：依赖的其他服务类（不是实例），ServiceHub 据此拓扑排序；
    - 重写 ``on_start/on_stop/on_suspend/on_resume`` 提供真实行为，
      基类负责状态机迁移与异常兜底，子类不要重写公共的 ``start/stop``。

    基类在每次状态迁移时经注入的 EventBus 发布
    :class:`~app.core.events.ServiceStateChangedEvent`（§4.5）。
    """

    name: str = "Service"
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self) -> None:
        self.status = ServiceStatus(
            service_id=self.name,
            state=ServiceState.REGISTERED,
            updated_at=time.time(),
        )
        # 由 ServiceHub.register() 注入（§9.2：bus/hub 注入）
        self.bus: EventBus | None = None
        self.hub: ServiceHub | None = None

    # ------------------------------------------------------------------ #
    # 子类扩展点（默认空实现，轻量服务注册即 RUNNING，见 §4.4 懒启动段）
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        """子类：启动资源（打开数据库连接、拉起循环等）。"""

    async def on_stop(self) -> None:
        """子类：释放资源、落盘收尾。"""

    async def on_suspend(self) -> None:
        """子类：释放重资源但保留快速恢复能力（如挂起浏览器内核会话）。"""

    async def on_resume(self) -> None:
        """子类：从 SUSPENDED 恢复。"""

    # ------------------------------------------------------------------ #
    # 公共生命周期（基类统管状态机，子类不要重写）
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """STARTING →（on_start）→ RUNNING；失败则记录错误并进入 SUSPENDED（§4.4）。"""
        self._transition(ServiceState.STARTING)
        try:
            await self.on_start()
        except Exception as exc:  # 启动失败不阻断其他服务（§4.4 异常策略）
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            self._transition(ServiceState.SUSPENDED)
            logging.getLogger(__name__).exception("服务 %s 启动失败", self.name)
            raise
        self.status.started_at = time.time()
        self._transition(ServiceState.RUNNING)

    async def stop(self) -> None:
        """STOPPING →（on_stop）→ DISPOSED；停止阶段异常被吞掉以保证逆序清理完成。"""
        if self.status.state in (ServiceState.STOPPING, ServiceState.DISPOSED):
            return
        self._transition(ServiceState.STOPPING)
        try:
            await self.on_stop()
        except Exception:
            logging.getLogger(__name__).exception("服务 %s 停止异常", self.name)
        self._transition(ServiceState.DISPOSED)

    async def suspend(self) -> None:
        # B5：与 start() 同款异常兜底——失败记 last_error 并保持状态一致性（停在
        # RUNNING，不迁移到 SUSPENDED），不抛穿以保证逆序挂起流程继续。
        try:
            await self.on_suspend()
        except Exception as exc:
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("服务 %s 挂起失败", self.name)
            return
        self._transition(ServiceState.SUSPENDED)

    async def resume(self) -> None:
        # B5：恢复失败记 last_error 并保持 SUSPENDED，不迁移到 RUNNING，不抛穿。
        try:
            await self.on_resume()
        except Exception as exc:
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("服务 %s 恢复失败", self.name)
            return
        self._transition(ServiceState.RUNNING)

    # ------------------------------------------------------------------ #
    def _transition(self, new_state: ServiceState) -> None:
        """更新快照并广播状态迁移事件（§4.5）。"""
        self.status.state = new_state
        self.status.updated_at = time.time()
        if self.bus is None:
            return
        # 延迟导入避免与 events 模块的循环依赖；events 本身不依赖本模块
        from app.core.events import ServiceStateChangedEvent

        self.bus.publish(
            ServiceStateChangedEvent(
                service_id=self.name,
                state=new_state.value,
                error=self.status.last_error,
            )
        )
