"""ServiceHub：进程内服务的总控制器（§4.4 / §4.5 / §9.2）。

职责边界（§3.6）：

- ServiceHub 只管**注册 / 依赖拓扑解析 / 按序启停 / status_report 状态汇聚**；
- 协程跑在哪条 loop 上、future 怎么投递，全归 EngineHub。

消费方经 :meth:`get` 拿到的是 EngineProxy 包装的实例——协程方法一律派发到
EngineHub 执行域（§4.3 请求-响应规则），同步属性/方法直达原对象。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from app.core.bus import EventBus
from app.core.engine_worker import EngineHub, EngineProxy, engine_hub
from app.core.lifecycle import Service, ServiceState, ServiceStatus

logger = logging.getLogger(__name__)


class ServiceHub:
    """主进程唯一总控制器：注册服务、拓扑启停、汇聚状态。"""

    name = "ServiceHub"

    def __init__(self, bus: EventBus | None = None, engine: EngineHub | None = None) -> None:
        self._engine: EngineHub = engine if engine is not None else engine_hub()
        self.bus: EventBus = bus if bus is not None else EventBus(self._engine)
        self._services: dict[type[Service], Service] = {}
        self._proxies: dict[type[Service], EngineProxy] = {}
        # ServiceHub 自身不注册，但它也是 §0.4 五个基础设施之一，
        # 仪表盘需展示其状态——用一条独立快照行承载。
        self._own = ServiceStatus(
            service_id=self.name,
            state=ServiceState.INIT,
            updated_at=time.time(),
        )

    # ------------------------------------------------------------------ #
    # 注册 / 定位
    # ------------------------------------------------------------------ #
    def register(self, service: Service) -> Service:
        stype = type(service)
        if stype in self._services:
            raise ValueError(f"服务类重复注册: {stype.__name__}")
        service.bus = self.bus
        service.hub = self
        self._services[stype] = service
        service.status.state = ServiceState.INIT
        service.status.updated_at = time.time()
        return service

    def require(self, stype: type[Service]) -> Service:
        """取**原始**服务实例（服务间装配用，不派发）。"""
        return self._services[stype]

    def get(self, stype: type[Service]):
        """取 EngineProxy 包装的服务句柄：协程方法经 EngineHub 派发（§4.3）。"""
        if stype not in self._proxies:
            self._proxies[stype] = EngineProxy(self._engine, self._services[stype])
        return self._proxies[stype]

    def service_by_id(self, service_id: str) -> Service | None:
        """按服务 id 取**原始**服务实例（M6 仪表盘单行启停控制用，§4.5/契约 §5）。

        ``status_report()`` 汇聚的是纯快照（只有 service_id 字符串），而
        :meth:`get`/:meth:`require` 按服务类定位——仪表盘按行渲染后要对某行
        发 suspend/resume/stop，只能经此公开通道反查实例，视图不持有实例列表、
        不碰私有 ``_services``。返回 None 表示该 id 不是已注册服务（含
        ServiceHub 自身那一行——总控制器不接受单行启停）。
        """
        for svc in self._services.values():
            if svc.name == service_id:
                return svc
        return None

    # ------------------------------------------------------------------ #
    # 生命周期编排
    # ------------------------------------------------------------------ #
    def init(self) -> None:
        """同步外壳：注册完成后、start_all 前的确定性准备（M1 无额外动作，预留钩子）。"""

    async def start_all(self) -> None:
        """按依赖拓扑序（先依赖后消费者）启动全部已注册服务。"""
        order = self._topo_order()
        self._own.state = ServiceState.STARTING
        self._own.updated_at = time.time()
        for stype in order:
            svc = self._services[stype]
            try:
                await svc.start()
            except Exception:
                # 单个服务失败不阻断其他服务（§4.4 异常策略）
                logger.exception("服务 %s 启动失败，继续其余服务", svc.name)
        self._own.state = ServiceState.RUNNING
        self._own.updated_at = time.time()

    async def stop_all(self) -> None:
        """按拓扑逆序停止全部已注册服务（先消费者后依赖）。"""
        order = reversed(self._topo_order())
        for stype in order:
            svc = self._services[stype]
            try:
                await svc.stop()
            except Exception:
                logger.exception("服务 %s 停止异常", svc.name)
        self._own.state = ServiceState.DISPOSED
        self._own.updated_at = time.time()

    def shutdown(self) -> None:
        """同步退出外壳：在 EngineHub 守护 loop 上调度 stop_all，供 ft.run 返回后调用。

        旧实现 ``asyncio.run(self.stop_all())`` 新建临时 loop 去关 aiohttp/
        QuickJS——这些对象是在 EngineHub loop 上创建的，跨 loop 关闭会告警。
        改为把 ``stop_all()`` 协程 submit 到 EngineHub 守护 loop，调用线程
        阻塞等 future 完成（保持 main.py finally 的同步语义，退出不悬挂）；
        超时兜底 10s，异常记 logger.exception 不抛穿。

        loop 的收尾放在 **这里**（stop_all 跑完之后、调用方线程上）：
        EngineHub 自己也是注册服务，逆序停止轮到它时它正跑在那条 loop 上，
        绝不能在 ``on_stop`` 里 join 自己（见 :meth:`EngineHub.on_stop`）。
        """
        if self._own.state is ServiceState.DISPOSED:
            return  # 已收尾过：loop 也停了，再 submit 只会得到"loop 已停"异常
        try:
            future = self._engine.submit(self.stop_all())
            future.result(timeout=10.0)
        except Exception:
            logger.exception("ServiceHub.shutdown 收尾异常")
        finally:
            try:
                self._engine.shutdown()
            except Exception:
                logger.exception("EngineHub loop 收尾异常")

    def _dependent_closure(self, stype: type[Service]) -> set[type[Service]]:
        """目标服务 + 它的**全部下游消费者**（传递闭包）。

        重启为什么必须连带下游：`require()` 给的永远是同一个实例（:meth:`require`），
        总控也不会在 stop 之后重建它。所以只把被重启的那个服务停到 DISPOSED、
        消费者却一动不动，等于让一堆服务继续拿着一条已经退役的依赖跑——
        现象是"重启了存储，书架照样读写"，直到下一次进程退出。
        """
        edges: dict[type[Service], list[type[Service]]] = defaultdict(list)
        for consumer, svc in self._services.items():
            for dep in svc.dependencies:
                if dep in self._services:
                    edges[dep].append(consumer)
        seen: set[type[Service]] = {stype}
        stack = [stype]
        while stack:
            for nxt in edges[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def _upstream_retired(self, stype: type[Service]) -> set[type[Service]]:
        """目标服务**已经退役（DISPOSED）的上游依赖**（传递闭包）。

        只收 DISPOSED，不收 SUSPENDED：挂起是人有意按下去的（释放浏览器这类重资源），
        重启一个消费者不该擅自把它挂起的依赖也拉起来。DISPOSED 不一样——被重启的服务
        在 `on_start` 里就要用依赖（`require()` 拿到的永远是同一个死实例），
        不把它一起复活的重启等于当场把那一行做成"运行中但降级"。
        """
        found: set[type[Service]] = set()
        stack = [stype]
        while stack:
            for dep in self._services[stack.pop()].dependencies:
                svc = self._services.get(dep)
                if svc is None or dep in found:
                    continue
                if svc.status.state is ServiceState.DISPOSED:
                    found.add(dep)
                    stack.append(dep)
        return found

    def restartable(self, service_id: str) -> bool:
        """这一行能不能单行重启：查不到实例的行（总控自身）与 EngineHub 都不行。

        EngineHub 的守护 loop 正是**执行这次重启的那个协程所在的 loop**——停它等于
        把自己脚下那块拆掉，收尾顺序只有进程退出时的 :meth:`shutdown` 那条路能保证。
        """
        svc = self.service_by_id(service_id)
        return svc is not None and svc is not self._engine

    async def restart_service(self, service_id: str) -> list[str]:
        """重启单个服务：连带它的**下游消费者**与**已退役的上游依赖**。

        返回本轮真正处理过的服务名（按依赖序），空列表表示这一行不可重启
        （:meth:`restartable`）。

        顺序不能反：停的时候要先把消费者拆干净再动依赖（否则消费者在依赖已经
        关掉的连接上继续跑），起的时候必须先起依赖。这与 :meth:`start_all` /
        :meth:`stop_all` 用的是同一张拓扑序，只是取其中的连通子图。

        单个服务失败不阻断其余（§4.4 异常策略照搬 start_all）：失败的那一行会经
        状态迁移事件在仪表盘上显示成 SUSPENDED + last_error，由人决定要不要
        再点一次。
        """
        if not self.restartable(service_id):
            return []
        target = self.service_by_id(service_id)
        affected = (self._dependent_closure(type(target))
                    | self._upstream_retired(type(target)))
        order = [t for t in self._topo_order() if t in affected]
        for stype in reversed(order):
            svc = self._services[stype]
            try:
                await svc.stop()
            except Exception:
                logger.exception("重启时停止服务 %s 异常，继续下游", svc.name)
        for stype in order:
            svc = self._services[stype]
            try:
                await svc.start()
            except Exception:
                logger.exception("重启时启动服务 %s 异常，继续其余", svc.name)
        return [self._services[t].name for t in order]

    # ------------------------------------------------------------------ #
    # 状态可见性
    # ------------------------------------------------------------------ #
    def status_report(self) -> list[ServiceStatus]:
        """汇聚全量服务快照（含 ServiceHub 自身这一行），§4.5。"""
        report: list[ServiceStatus] = [self._own]
        report.extend(svc.status for svc in self._services.values())
        return report

    # ------------------------------------------------------------------ #
    def _topo_order(self) -> list[type[Service]]:
        """Kahn 拓扑排序：依赖先启动；同层按注册序保证确定性。"""
        indeg: dict[type[Service], int] = {t: 0 for t in self._services}
        edges: dict[type[Service], list[type[Service]]] = defaultdict(list)
        for t, svc in self._services.items():
            for dep in svc.dependencies:
                if dep not in self._services:
                    raise ValueError(
                        f"服务 {svc.name} 依赖未注册: {dep.__name__}")
                indeg[t] += 1
                edges[dep].append(t)
        reg_order = list(self._services)
        ready = deque(t for t in reg_order if indeg[t] == 0)
        order: list[type[Service]] = []
        while ready:
            t = ready.popleft()
            order.append(t)
            for nxt in edges[t]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
        if len(order) != len(self._services):
            raise RuntimeError("服务依赖图存在环，无法拓扑排序")
        return order
