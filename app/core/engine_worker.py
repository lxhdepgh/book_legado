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
import logging
import threading
import time
import weakref

from app.core.lifecycle import Service

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 循环异常归属：本进程同时有两条 asyncio 循环，报错必须先说清是谁家的
# --------------------------------------------------------------------------- #
#: 装过归属处理器的循环（弱引用：循环对象回收即 forget，id 复用不会误判成"已装"）
_ATTRIBUTED: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()

#: 族 1 的指纹：`proactor_events._call_connection_lost` 收尾段里那句裸 `sock.shutdown()` 抛出来
_SOCKET_TEARDOWN_MARK = "_call_connection_lost"
#: 族 2 的指纹：TLS 收尾握手期间对面又塞应用数据（`sslproto._do_shutdown` → `unwrap()`）
_SSL_TEARDOWN_MARK = "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
#: 同一族两条日志之间至少隔这么久：刷屏的量与检测轮长度成正比，信息量却只在"发生过、多少次"
_TEARDOWN_MIN_INTERVAL = 10.0


def _teardown_family(context: dict) -> str | None:
    """这条循环级记录是不是"连接收尾时的竞态"，是哪一族；认不出返回 ``None``。

    判据按**现场那两条 traceback 的原文**收得极窄（族 1 还要求异常本体是复位/中止），
    不是按"看着像噪声"来挑：认错的代价是把真故障折成一行计数，比多打几行严重得多。
    """
    message = str(context.get("message", ""))
    exc = context.get("exception")
    if (_SOCKET_TEARDOWN_MARK in message
            and isinstance(exc, (ConnectionResetError, ConnectionAbortedError))):
        return "socket 已复位仍被收尾"
    if isinstance(exc, OSError) and _SSL_TEARDOWN_MARK in str(exc):
        return "TLS 收尾期收到应用数据"
    return None


def _finish_socket_teardown(context: dict) -> bool:
    """补上 ``proactor_events.py:154-172`` 那句 ``finally`` 被异常跳过的清理。

    为什么要我们补：那里的顺序是 ``shutdown()`` → ``sock.close()`` → ``_sock = None`` →
    ``_called_connection_lost = True``，而对端复位过的 socket 在 ``shutdown()`` 就抛
    10054，后三行**一行都不执行**。实测后果（``.probe/loop_teardown_cleanup_probe.py``
    B 臂，把 40 条活传输层的 ``shutdown`` 换成抛 10054）：40/40 次命中后
    ``transport._sock.fileno()`` 仍 ≥ 0，进程句柄数 251 → 305，**显式 ``gc.collect()``
    之后还是 303**（对照臂与补关臂全程 245 不动）——一次竞态一个不归还的 socket 句柄。

    这里只做 CPython 自己已经决定要做的那三行（socket 本来就要被丢弃），不碰别的。
    返回是否真的补到了一条传输层（护栏测试要能断到）。
    """
    callback = getattr(context.get("handle"), "_callback", None)
    transport = getattr(callback, "__self__", None)
    if transport is None or getattr(transport, "_sock", None) is None:
        return False
    sock = transport._sock
    try:
        if sock.fileno() != -1:
            sock.close()
    except OSError:
        return False
    transport._sock = None                        # type: ignore[attr-defined]
    transport._called_connection_lost = True      # type: ignore[attr-defined]
    return True


def install_loop_attribution(loop: asyncio.AbstractEventLoop, tag: str) -> None:
    """给一条循环装"报错先自报家门"的异常处理器。

    为什么先只做身份（第 53 轮）：现场那串 ``Exception in callback
    _ProactorBasePipeTransport._call_connection_lost(None)``（10054）只写了回调名，
    没写是**哪条循环**家的 socket。本进程同时有两条 asyncio 循环（界面循环在 Flet 线程、
    服务协程在 ``legado-engines`` 线程），归到谁家决定下一步查 flet 的 websocket 还是查
    aiohttp 会话的归属。

    归属定了之后（第 65 轮现场：两族都落在 ``[engine]``，且**请求一条没失败**）就只把
    **认出的两族收尾竞态**降成一条带累计计数的短行——它们每次都是同一条码路径，重复的
    traceback 里没有新信息，反而把 ``logger.error`` + ``default_exception_handler`` 的
    双份打印放大成刷屏；族 1 顺手补上被跳过的 socket 清理（见
    :func:`_finish_socket_teardown`）。**其余一切照旧**：打完身份就原样交给
    ``default_exception_handler``，一条记录都不许少。

    重复调用安全（WeakSet 记账），两条循环各自只装一次。
    """
    if loop in _ATTRIBUTED:
        return
    _ATTRIBUTED.add(loop)
    #: 各族累计次数与上次落日志的时刻（随处理器闭包一起活，一条循环一份）
    teardown_stats: dict[str, list] = {}

    def handler(target: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        message = context.get("message", "")
        family = _teardown_family(context)
        if family is not None:
            stats = teardown_stats.setdefault(family, [0, 0.0])
            stats[0] += 1
            # socket 补关**不受降频影响**：每一次都得补，漏一次就漏一个不归还的句柄
            plugged = (_finish_socket_teardown(context)
                       if family.startswith("socket") else None)
            now = time.monotonic()
            if stats[1] and now - stats[1] < _TEARDOWN_MIN_INTERVAL:
                return                            # 只累计，不落日志
            logger.error("循环[%s]线程=%s loop=%#x 收尾竞态[%s] 累计第 %d 次"
                         "（已就地补关 socket=%s，不再逐条打 traceback）：%r",
                         tag, threading.current_thread().name, id(target),
                         family, stats[0], plugged, exc)
            stats[1] = now
            return
        if exc is not None:
            # 异常本体也写进消息一行：现场是翻控制台找线索，只挂 exc_info 的话
            # grep 一句 "10054" 什么都读不出来。
            logger.error("循环[%s]线程=%s loop=%#x 报告异常：%s：%r",
                         tag, threading.current_thread().name,
                         id(target), message, exc, exc_info=exc)
        else:
            logger.error("循环[%s]线程=%s loop=%#x 报告：%s",
                         tag, threading.current_thread().name,
                         id(target), message)
        target.default_exception_handler(context)

    loop.set_exception_handler(handler)



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
        install_loop_attribution(loop, "engine")
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
#
# 无头模式（``main.py --serve``）下这一整层是**恒等**的：没人登记界面循环，
# 于是 ``call_on_ui`` 就地执行、控制器的编排退回引擎守护循环。设计文档
# （docs/flutter前端与python服务架构设计.md §5.4）要的不是新行为，而是
# "这条路上一次跨循环投递都没发生过"能被机械断言——所以下面记两个计数器。
# --------------------------------------------------------------------------- #
_UI_LOOP: asyncio.AbstractEventLoop | None = None

#: 真的往界面循环投出去过几次（跨循环编组发生过）。
_UI_MARSHALLED = 0
#: 因为没有界面循环而就地执行过几次（无头路径上只有这一种）。
_UI_INLINE = 0


def set_ui_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """登记界面循环（由 ``build_app_shell`` 在 Flet 回调内调用一次）。"""
    global _UI_LOOP
    _UI_LOOP = loop
    if loop is not None:
        install_loop_attribution(loop, "ui")


def ui_loop() -> asyncio.AbstractEventLoop | None:
    """当前登记的界面循环；无头/测试场景为 None。"""
    return _UI_LOOP


def ui_dispatch_counts() -> tuple[int, int]:
    """``(投递到界面循环的次数, 就地执行的次数)``。

    无头装配的护栏按它核对：整条 serve 路径跑完，第一个数必须是 0。
    """
    return _UI_MARSHALLED, _UI_INLINE


def reset_ui_dispatch_counts() -> None:
    """归零两个计数（探针与护栏各自开一段账，免得互相看见）。"""
    global _UI_MARSHALLED, _UI_INLINE
    _UI_MARSHALLED = _UI_INLINE = 0


def call_on_ui(fn) -> None:
    """把一个"要碰控件"的动作编组到界面循环上执行（契约 §5.2）。

    后台线程或引擎守护 loop 上的回调直接改控件是不行的——控件只认界面循环。
    **没登记界面循环（无头测试、窗口已关）时就地执行**：那条线上本来就没有真窗口，
    就地跑才是测试能观察到的行为，把它静默丢掉会变成"测试全绿但没人执行"。
    """
    global _UI_MARSHALLED, _UI_INLINE
    loop = ui_loop()
    if loop is None or loop.is_closed():
        _UI_INLINE += 1
        try:
            fn()
        except Exception:
            logger.exception("后台回调就地执行失败")
        return

    async def call() -> None:
        try:
            fn()
        except Exception:
            logger.exception("界面循环上执行失败")

    _UI_MARSHALLED += 1
    asyncio.run_coroutine_threadsafe(call(), loop)


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
