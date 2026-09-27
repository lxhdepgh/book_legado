"""Controller 基类：attach/detach、事件订阅退订、UI 编排派发、节流（§7.4 / §9.2）。

职责边界：

- Controller 生命周期与视图绑定：``attach(view)`` 时经 :meth:`subscribe` 订阅
  EventBus 事件并把视图回调接到业务方法；``detach()`` 统一退订，防止跨页泄漏；
- 编排协程一律经 :meth:`ui_call` 排进**界面循环**，完成后再 :meth:`flush` 一次；
- 服务重活仍留在 EngineHub 执行域：``self.service(X)`` 返回 EngineProxy，其协程
  方法在界面循环上被 await 时会自行跳回引擎循环。

两条 Flet 0.86 硬约束（本次重构的回归根因，务必遵守）：

1. **控件只能在界面循环上改**。EventBus 把处理器投递到 EngineHub 循环执行
   （``core/bus.py``），控制器若在处理器里直接写 ``view.xxx`` 就跑错了线程——
   所以 :meth:`subscribe` 统一把处理器编组回界面循环。
2. **改完必须显式 flush**。``session.after_event`` 的自动 update 只在**控件事件
   回调**返回后触发；事件总线/后台编排里改的控件永远等不到那次自动 update，
   不 flush 就是"数据到了、界面不动"。所以 :meth:`flush` 挂在订阅包装器和
   :meth:`ui_call` 的收尾上，不依赖调用方自觉。
3. **登记的界面循环可能已经关了**。Flet 关窗口时会先把自己的 loop close 掉，
   而服务表还在往总线上发 ``ServiceStateChangedEvent``——那一刻往死循环上派发
   就是 ``RuntimeError: Event loop is closed`` 刷屏。:meth:`subscribe` 认这个
   状态并把事件丢弃（控件早就不在了，处理了也没处渲染），:meth:`ui_call` 则
   退回引擎循环，至少协程能跑完、不会拿不到 await。

高频事件（下载进度 / 搜索结果流）用 :meth:`throttle` 做 0.5s 合并：窗口内先放行
第一次、只保留最后一次参数在窗口末尾补一发（trailing），否则进度条会停在半路。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable

from app.core.engine_worker import EngineHub, engine_hub, ui_loop
from app.core.events import AppEvent
from app.core.hub import ServiceHub

logger = logging.getLogger(__name__)


def _log_future(future) -> None:
    """后台 future 的异常必须有声音——吞掉的异常等于功能没反应。"""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("控制器编排任务失败", exc_info=exc)


class BaseController:
    """视图控制器基类（子类实现 on_attach/on_detach 订阅具体事件）。"""

    #: 所属目的地名（§9.2：与 ViewRegistry 路由键一致），子类覆盖。
    destination: str = ""

    def __init__(self, hub: ServiceHub, bus=None) -> None:
        self._hub = hub
        # bus 可注入（测试替身）；缺省从 hub 取。
        self._bus = bus if bus is not None else getattr(hub, "bus", None)
        self._view: Any = None
        self._subs: list[Callable[[], None]] = []
        self._throttle_state: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 生命周期（§7.4：attach/detach 对称）
    # ------------------------------------------------------------------ #
    def attach(self, view) -> "BaseController":
        """绑定视图并订阅事件；返回 self 便于链式。

        ``view.controller`` 反向引用供导航容器装配时绑定跨视图动作
        （:attr:`nav_actions`：动作名 → 目的地，NavigationHost.show 处理）。
        """
        self._view = view
        try:
            view.controller = self
        except Exception:  # 控件属性被占用等极端情况不阻断 attach
            pass
        self.on_attach()
        return self

    def detach(self) -> None:
        """退订全部事件句柄并解绑视图（重复 detach 安全）。"""
        self.on_detach()
        for unsubscribe in list(self._subs):
            try:
                unsubscribe()
            except Exception:  # 退订幂等，个别失败不阻断其余
                pass
        self._subs.clear()
        self._view = None

    # 子类扩展点
    def on_attach(self) -> None:
        """子类：在这里 subscribe(...) 并把视图回调接到业务方法。"""

    def on_detach(self) -> None:
        """子类：释放视图侧临时句柄（对话框、在途任务等）。"""

    # ------------------------------------------------------------------ #
    # 事件订阅（统一登记 + 编组回界面循环 + 收尾 flush）
    # ------------------------------------------------------------------ #
    def subscribe(self, event_type: type[AppEvent], handler) -> Callable[[], None]:
        """订阅某类事件，句柄登记后随 detach 自动退订。

        处理器由 EventBus 在 EngineHub 循环上调用；这里包一层把它编组回界面
        循环再执行，执行完 flush 视图，保证"事件到了界面也动了"。
        """
        if self._bus is None:
            return lambda: None
        unsubscribe = self._bus.subscribe(
            event_type, self._marshal(handler))
        self._subs.append(unsubscribe)
        return unsubscribe

    def _marshal(self, handler) -> Callable:
        """把总线处理器包装成"在界面循环上执行 + 收尾 flush"的协程。"""

        async def on_ui(event) -> None:
            loop = ui_loop()
            if loop is not None and loop.is_closed():
                # 进程正在收尾：Flet 关掉自己的循环后，服务表还会发
                # ServiceStateChangedEvent。这时派处理器只会得到
                # "Event loop is closed"（控件也早就不在了）——静默丢弃。
                logger.debug("界面循环已关闭，丢弃事件 %s",
                             type(event).__name__)
                return
            try:
                if loop is None or _is_current(loop):
                    await handler(event)
                else:
                    await asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(handler(event), loop))
            finally:
                self.flush()

        return on_ui

    # ------------------------------------------------------------------ #
    # 界面编排派发（§9.2）
    # ------------------------------------------------------------------ #
    def ui_call(self, coro):
        """把编排协程排进界面循环，返回 future（UI 线程不阻塞）。

        协程里可以改控件、可以 await ``self.service(X).some_async(...)``——后者由
        EngineProxy 自行跳回 EngineHub 执行域，重活不会压在界面循环上。
        协程结束后统一 flush；异常经 :func:`_log_future` 落日志，不再静默。
        无头/测试场景没有界面循环，退回引擎守护循环（此时 flush 是空操作）。
        界面循环**已关闭**（进程正在收尾）同样退回：往死循环上派发会当场抛
        ``Event loop is closed``，而协程对象连一次 await 都没拿到。
        """
        loop = ui_loop()
        if loop is None or loop.is_closed():
            future = self._engine().submit(self._guard(coro))
        else:
            future = asyncio.run_coroutine_threadsafe(
                self._guard(coro), loop)
        future.add_done_callback(_log_future)
        return future

    async def _guard(self, coro):
        try:
            return await coro
        finally:
            self.flush()

    async def ui_await(self, coroutine):
        """把协程挪到界面循环上执行，并在**调用方所在的 loop** 上等它的结果。

        和 :meth:`ui_call` 的分工：那条是 fire-and-forget（结果没人要，收尾 flush
        一次就够），这条给"要返回值"的场合——服务实现的协程跑在 EngineHub 的
        loop 上（消费方拿到的是 EngineProxy），而那里面有一步要碰控件。flet 的
        控件只认界面循环，同一约定见 ``reading_engine._call_ui``。

        等待用 ``wrap_future`` 而不是 ``future.result()``：后者会把引擎线程整个
        堵死，而界面循环此刻正等着这次引擎调用返回——两边互等就是死锁。
        界面循环已关（进程收尾）或本就是当前 loop 时直接就地 await。
        """
        loop = ui_loop()
        if loop is None or loop.is_closed() or _is_current(loop):
            return await coroutine
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coroutine, loop))

    def on_done(self, fn: Callable) -> Callable:
        """包装 future 完成回调：执行后补一次 flush。

        ``ui_call`` 的 flush 发生在 future 落值之时，而 ``add_done_callback``
        的渲染在其**之后**才跑——不补这一下，回调里改的控件永远等不到下发
        （用户感受：点一次没反应，切个页才看到上一次的结果）。
        """

        def wrapper(future) -> None:
            try:
                fn(future)
            except Exception:
                logger.exception("控制器完成回调失败")
            finally:
                self.flush()

        return wrapper

    def _engine(self) -> EngineHub:
        return engine_hub()

    # ------------------------------------------------------------------ #
    # 渲染落图（改完控件必须 flush 一次，见模块 docstring 约束 2）
    # ------------------------------------------------------------------ #
    def flush(self) -> None:
        """把本轮改动下发到客户端；视图未挂载时静默跳过。

        走整页 update 而不是 ``view.update()``：对话框 / 侧面板是 page 级覆盖层，
        只补视图子树会漏掉它们（换源扫描进度就是这样停在半路）。
        """
        view = self._view
        if view is None:
            return
        try:
            page = getattr(view, "page", None)
            if page is None:
                return
            page.update()
        except Exception as exc:  # 切页瞬间视图已脱离页面：不是错误，记 debug
            logger.debug("视图 %s flush 跳过：%s", type(view).__name__, exc)

    # ------------------------------------------------------------------ #
    # 节流（§7.4：0.5s 合并 + 窗口末尾补发最后一次）
    # ------------------------------------------------------------------ #
    def throttle(self, fn: Callable, interval: float = 0.5) -> Callable:
        """leading + trailing 合并：窗口内首次立即执行，其余只留最后一次参数。

        纯 leading 会把"最后一次"进度事件吞掉——界面永远停在半路。fn 为 async
        时返回 async wrapper，否则返回同步 wrapper。
        """
        state = self._throttle_state.setdefault(
            id(fn), {"last": 0.0, "pending": None, "timer": None})
        if inspect.iscoroutinefunction(fn):

            async def wrapper(*args, **kwargs):
                now = time.monotonic()
                if now - state["last"] >= interval:
                    state["last"] = now
                    state["pending"] = None
                    return await fn(*args, **kwargs)
                state["pending"] = (args, kwargs)
                _schedule_trailing(state, fn, interval, now)
                return None

        else:

            def wrapper(*args, **kwargs):
                now = time.monotonic()
                if now - state["last"] >= interval:
                    state["last"] = now
                    state["pending"] = None
                    return fn(*args, **kwargs)
                state["pending"] = (args, kwargs)
                _schedule_trailing(state, fn, interval, now)
                return None

        return wrapper

    # ------------------------------------------------------------------ #
    # 服务定位（子类统一入口）
    # ------------------------------------------------------------------ #
    def service(self, stype):
        """经 ServiceHub.get() 取 EngineProxy 包装的服务句柄。"""
        return self._hub.get(stype)


def _is_current(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def _schedule_trailing(state, fn, interval: float, now: float) -> None:
    """窗口内已合并过事件：挂一个延时器，到点补发最后一次（只挂一个）。"""
    if state["timer"] is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        state["pending"] = None  # 没有循环可挂延时器：放弃补发，不留悬挂任务
        return

    def fire():
        state["timer"] = None
        pending = state["pending"]
        if pending is None:
            return
        args, kwargs = pending
        state["pending"] = None
        state["last"] = time.monotonic()
        if inspect.iscoroutinefunction(fn):
            task = loop.create_task(fn(*args, **kwargs))
            task.add_done_callback(_log_future)
        else:
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("节流补发同步回调失败")

    state["timer"] = loop.call_later(
        max(0.0, interval - (now - state["last"])), fire)
