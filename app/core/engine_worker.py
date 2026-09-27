"""全服务统一执行域：守护线程事件循环 + EngineProxy（§3.6 升级版）。

旧语义（old_code/app/services/engine_worker.py）全部保留：

- daemon 线程跑 ``asyncio.new_event_loop().run_forever()``，loop 首次使用时懒启动；
- :meth:`EngineHub.submit` 经 ``run_coroutine_threadsafe`` 投递协程，任意线程可调；
- :class:`EngineProxy` 只把**协程方法**派到 hub loop 并 await future，
  普通属性读写（``current_book`` / ``js_variables`` 等）直达共享对象；
- 模块级单例 :func:`engine_hub` 与工厂 :func:`worker_engine` 沿用原名。

§3.6 升级点：

- 执行域从"仅 ReaderEngine 协程"扩为**所有业务服务协程的统一执行域**；
- :class:`EngineHub` 现在同时是一个 :class:`~app.core.lifecycle.Service`
  （name="EngineHub"），注册进 ServiceHub 后随拓扑启动预热 loop、随退出优雅停止；
- 分工：ServiceHub 管注册/依赖拓扑/状态汇聚，本类只管 loop 调度与 future 投递。

QuickJS 的 ``_QuickJsDispatcher``（单线程 + 前台/后台优先级队列）是独立原生
调度域，与本 loop 并存，边界不变。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading

from app.core.lifecycle import Service


class EngineHub(Service):
    """跑在 daemon 线程上的事件循环，全进程服务协程统一执行域。"""

    name = "EngineHub"
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self, name: str = "legado-engines") -> None:
        super().__init__()
        self._thread_name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------------ #
    # 旧有公开 API（语义不变）
    # ------------------------------------------------------------------ #
    def loop(self) -> asyncio.AbstractEventLoop:
        """hub 的 loop，首次使用时拉起守护线程。"""
        with self._lock:
            if self._thread is None and not self._stopped:
                self._thread = threading.Thread(
                    target=self._serve, name=self._thread_name, daemon=True)
                self._thread.start()
        self._ready.wait()
        return self._loop

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def submit(self, coroutine) -> concurrent.futures.Future:
        """把协程排进本 hub，返回 concurrent.futures.Future；任意线程可调。"""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop())

    # ------------------------------------------------------------------ #
    # §3.6 新增：Service 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        """启动即预热守护线程 loop，让"引擎就绪"状态对仪表盘可见。"""
        self.loop()

    async def on_stop(self) -> None:
        """逆序停止轮到 EngineHub 时**不能**在这里拆自己的 loop。

        `ServiceHub.shutdown()` 把 `stop_all()` 协程 submit 到本守护 loop 上跑，
        而 EngineHub 自己也在服务表里（main.py 注册），于是 on_stop 被调用时
        **当前线程就是那条 loop 线程**：这里 join 自己直接
        ``RuntimeError: cannot join current thread``（退出时那条刺眼的
        「服务 EngineHub 停止异常」 traceback），而 loop.stop() 更会让 stop_all
        自己永不收尾。loop 的实际关停交给外层调用方——见
        :meth:`ServiceHub.shutdown` 的 finally。
        """

    def shutdown(self) -> None:
        """优雅停止 loop，供进程退出时在**主线程**调用（§9.2）。

        桌面单进程退出时 loop 上可能还有在途任务；这里只保证 loop 收到
        stop 信号并短暂等待线程收尾，不做任务级取消（退出即进程回收，§4.4）。
        自线程守卫：在 loop 线程上调用时只发 stop 信号、不 join（join 自己必然
        RuntimeError），例如仪表盘对 EngineHub 那一行发停止。
        """
        with self._lock:
            self._stopped = True
            loop = self._loop
            thread = self._thread
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)


_HUB = EngineHub()


def engine_hub() -> EngineHub:
    """进程级唯一 hub（视图/服务经它拿执行域）。"""
    return _HUB


# --------------------------------------------------------------------------- #
# UI 循环登记：Flet 控件只允许在界面循环上改，且改完必须 flush 一次
# --------------------------------------------------------------------------- #
_UI_LOOP: asyncio.AbstractEventLoop | None = None


def set_ui_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """登记界面循环（由 ``build_app_shell`` 在 Flet 回调内调用一次）。"""
    global _UI_LOOP
    _UI_LOOP = loop


def ui_loop() -> asyncio.AbstractEventLoop | None:
    """当前登记的界面循环；无头/测试场景为 None。"""
    return _UI_LOOP


class EngineProxy:
    """跑在 EngineHub 上的服务对象的 UI 面句柄（§3.6 / §9.2）。

    协程方法被派发到 hub loop 并 await future；其余属性直达原对象，
    调用方读 ``svc.status``、写 ``svc.js_variables`` 与本地对象无异。
    """

    def __init__(self, hub: EngineHub, engine) -> None:
        object.__setattr__(self, "_hub", hub)
        object.__setattr__(self, "_engine", engine)

    def __getattr__(self, name):
        engine = self._engine
        value = getattr(engine, name)
        # 实例属性是外部直接挂上的替身（测试替身等），留在原地不派发到 hub。
        if name in getattr(engine, "__dict__", ()):
            return value
        return self._dispatch(value) if inspect.iscoroutinefunction(value) else value

    def __setattr__(self, name, value) -> None:
        setattr(self._engine, name, value)

    def _dispatch(self, method):
        async def call(*args, **kwargs):
            # F7：不再在这里自动绑定 engine.interaction_loop——首次 await 落在
            # 哪条 loop 上是不确定的（UI / hub / 后台都可能），绑错即把 webview/
            # 验证码回调派错线程。改由视图装配层 build_app_shell 经
            # :func:`inject_interaction_loop` 显式注入 UI loop；未注入时引擎
            # ``_interaction_target_loop()`` 仍降级到当前 running loop，语义不变。
            return await asyncio.wrap_future(
                self._hub.submit(method(*args, **kwargs)))
        return call


def worker_engine(engine, hub: EngineHub | None = None) -> EngineProxy:
    """把已构造好的引擎挂到 hub 上，返回 UI 面句柄（旧工厂函数，保留）。"""
    return EngineProxy(hub or engine_hub(), engine)


# --------------------------------------------------------------------------- #
# F7：UI loop 显式注入（替代 EngineProxy._dispatch 的自动绑定灰色地带）
# --------------------------------------------------------------------------- #
def inject_interaction_loop(hub, loop: asyncio.AbstractEventLoop | None) -> None:
    """把 UI loop 显式绑为各 LegadoReadingEngine 的 ``interaction_loop``。

    由视图装配层 ``build_app_shell`` 在 Flet UI loop 内调用。旧实现里
    ``EngineProxy._dispatch`` 在引擎**首次**被代理 await 时自动写
    ``engine.interaction_loop = asyncio.get_running_loop()``——但首次调用方
    可能是 UI 线程、也可能是 hub 后台协程，绑错即把 webview/验证码回调派到
    错误线程。改为装配期一次性显式注入。

    用 lazy import + ``hub.require`` 取服务实例，避免 core 模块加载期
    ``import app.services.*`` 破坏分层（本函数在调用时才解析）。

    只注入"有 UI 交互面"的引擎：
    - :class:`RuleEngineService` 池化的引擎（阅读器登录/验证码/webview 要碰界面）；
    - 它在装配后才懒建的新引擎，经 ``svc._interaction_loop`` 标记由
      :meth:`RuleEngineService._new_engine` 继承。
    跳过：``WholeBookCacheService`` / ``check_service`` 的 ``background=True``
    引擎（纯后台、无 UI 交互，不需要 UI loop）。
    """
    if loop is None:
        return
    set_ui_loop(loop)  # 控制器编排协程与事件订阅统一回到这条循环上跑
    try:
        from app.services.engine.rule_engine_service import RuleEngineService
    except Exception:
        return  # core 不能硬依赖 services；导入失败即视为无 UI 引擎

    require = getattr(hub, "require", None)
    if not callable(require):
        return  # 测试替身 hub（无 require 通道）跳过
    try:
        svc = require(RuleEngineService)
    except Exception:
        return  # 服务未注册（离线/无头装配）跳过

    # 标记服务：后续 _new_engine 创建的引擎继承此 UI loop。
    svc._interaction_loop = loop

    # 装配时池通常为空（引擎按源懒建），但仍兜底设置已存在的引擎。
    default = getattr(svc, "_default_engine", None)
    if default is not None and hasattr(default, "interaction_loop"):
        default.interaction_loop = loop
    pool = getattr(svc, "_pool", None)
    if pool:
        for engine in pool.values():
            if hasattr(engine, "interaction_loop"):
                engine.interaction_loop = loop
