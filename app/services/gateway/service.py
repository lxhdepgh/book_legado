"""GatewayService：无头 RPC 网关（设计 §5.1 / §4）。

把进程内的服务面暴露成 **一个动词 + action 表**（``POST /rpc``）、一条事件流
（``GET /ws``）、一个二进制出口（``GET /media/{key}``），绑 **127.0.0.1** 上的随机
端口、一次性令牌守门。形态照 ``app/services/tools/web_bridge_service.py``
（runner / 中间件 / 事件转 WS 都是它验证过的路子），差别有三条：

1. **一个动词**：那边是 8 个 REST 端点各写各的，这边只有 ``/rpc``，动作在
   :mod:`app.gateway.methods` 的表里显式登记；
2. **信封永远合法**：任何内部异常（含 ``BaseException``）都折成
   ``{v,id,ok,data,error,code}``——客户端解析器不许遇到第二种形状；
3. **它跑在 EngineHub 的守护循环上**（``main.py --serve`` 把 ``start_all`` 提交到那条
   loop），所以"网关 handler ↔ 服务协程"之间没有第二次跨循环编组。这是设计 §3
   说的"整层 glue 消失"里能在 Python 侧先兑现的那一半。

线程/循环红线（记忆 ``engine-hub-loop-split``、``tts-pyttsx3-com-same-thread``）：
本文件**只调服务**，绝不直接碰原生对象（QuickJS 句柄、SAPI COM、playwright 内核）。
要什么就去 action 表里登记一个动作，让拥有它的那条线程去执行。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from app import APP_VERSION
from app.core.events import AppEvent
from app.core.lifecycle import Service
from app.gateway import jsonschema_min as schema
from app.gateway.methods import BY_ACTION, Method, resolve_service_class
from app.gateway.protocol import (
    CODE_BAD_REQUEST, CODE_CANCELLED, CODE_ENGINE, CODE_SCHEMA, CODE_TIMEOUT,
    CODE_UNAVAILABLE, MAX_BODY_BYTES, PROTOCOL_VERSION, digest, err_envelope,
    ok_envelope,
)
from app.gateway.serialize import project, to_json

logger = logging.getLogger(__name__)

#: 红线：只绑回环。这是本机进程之间说话，不是局域网服务。
DEFAULT_HOST = "127.0.0.1"

#: 最后一个 WS 会话断开后多久自杀（设计 §4.6：Flutter 崩了不留无头 Python）。
IDLE_EXIT_SECONDS = 15.0

#: 进度类事件的下发合流窗口（秒）。沿用控制器 ``_coalesced_flush`` 的 0.5s 语义：
#: 事件一条不丢，只是攒成一帧发。
COALESCE_WINDOW = 0.5

#: 哪些事件走合流。判据是"这条只带计数，晚半秒看到的数也是对的"——结果类事件
#: （``CheckFinished`` / ``RssRefreshed`` / ``BackupFinished``）不在其列：
#: 一条就是一行图，合掉就是丢数据。
COALESCED_EVENTS: frozenset[str] = frozenset({
    "DownloadProgressEvent",
    "RssRefreshProgressEvent",
    "BackupProgressEvent",
    "ExportProgressEvent",
    "ImportProgressEvent",
    "RepairProgressEvent",
    "ServiceStateChangedEvent",
})

#: 断线重连要回放的状态事件（"最近一份"就够；成批历史各自的 snapshot 动作给）。
SNAPSHOT_EVENTS: frozenset[str] = frozenset({
    "ReadAloudStateEvent",
    "MediaPlayStateEvent",
})

#: ``/media`` 的 key 形状：封面落点就是 ``md5(book_url)[:16].jpg``
#: （见 ``source_service.py::_normalize_cover``）。**不新造 key 空间**，
#: 也不让路径进来——正则不匹配直接 404，``..`` 这种形状到不了文件名。
_MEDIA_KEY_RE = re.compile(r"^[0-9a-f]{16}\.jpg$")

#: 握手时回给客户端的能力位。加一条就得真有一条。
CAPABILITIES: tuple[str, ...] = ("rpc", "events", "media.covers", "job.actions",
                                 "webview.reverse")

#: 反向调用（§8 R1：可见 WebView 归 Flutter）的默认预算（秒）。
#: 与 ``headless_browser`` 的 20s 同值——那条是今天内核渲染的预算，换宿主不该更短。
WEBVIEW_TIMEOUT = 20.0


def event_types() -> list[type[AppEvent]]:
    """:class:`AppEvent` 的全部具体子类（事件名 = 类名，与 ``app/core/events.py`` 同源）。

    这里用反射而不是再抄一张表：动作面反射会把服务对象顶上协议面（所以那边用显式表），
    而事件面是 frozen dataclass、字段本就是纯 JSON——**它的定义处就是那张冻结表**。
    新增事件不会被漏掉，也不会被悄悄漏掉：护栏按名字比对整个清单。
    """
    seen: list[type[AppEvent]] = []
    stack: list[type[AppEvent]] = [AppEvent]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            seen.append(sub)
            stack.append(sub)
    return seen


def _log_task(task: asyncio.Task) -> None:
    """后台任务的异常必须有声音——静默的 job 就是"点了没反应"。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("网关后台任务失败", exc_info=exc)


class WebviewUnavailable(RuntimeError):
    """没有 WebView 宿主可接这次渲染（一个 /ws 会话都没连着，或会话中途断了）。

    单独一个类型，是为了让注入侧能按"内核不在场"那一条老口径降级：
    ``BrowserService.available_fetcher()`` 返回 None 时引擎走纯 HTTP，
    这里同理——不是"渲染失败"，是"根本没这个人"，该当场退回而不是折成 engine 错误。
    """


class GatewayService(Service):
    """无头 RPC 网关：环回监听 + 一次性令牌 + action 表分发。"""

    name = "GatewayService"
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self, host: str = DEFAULT_HOST, port: int = 0,
                 token: str | None = None,
                 idle_exit_seconds: float = IDLE_EXIT_SECONDS,
                 exit_event: threading.Event | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = int(port)
        self._token_value = token
        self._bound_port = 0
        self._idle_exit_seconds = float(idle_exit_seconds)
        #: 置位 = 请求进程收摊（``engine.shutdown``、空闲超时、主线程 Ctrl-C 共用一条）。
        self._exit = exit_event
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._starting = False
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._unsubs: list[Any] = []
        self._pending: list[dict] = []
        self._flush_handle: Any = None
        self._snapshots: dict[str, dict] = {}
        self._idle_timer: Any = None
        #: 见过几个客户端：连上过 /ws，或者**成功办完过一次动作**的 /rpc 调用方。
        #: 只出错的动作不算（坏 JSON、未知 action、schema 不符的探针不该给自己上一段
        #: 倒计时）；令牌不对的连处理器都进不来，天然也算不上。
        self._clients_seen = 0
        #: 在跑的 job 任务：不留引用会被 GC 悄悄回收（协程中途蒸发）。
        self._jobs: set[asyncio.Task] = set()
        #: §8 R1 反向调应在途表：``request_id`` → future（结果由 ``engine.webContentReady`` 落）。
        self._pending_webviews: dict[str, asyncio.Future] = {}
        self._webview_seq = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        await self.start_server()

    async def on_stop(self) -> None:
        await self.stop_server()

    # ------------------------------------------------------------------ #
    # 启停（幂等；半途状态不能被第二次调用当成"没开"，照 WebBridge 的 _starting 保险）
    # ------------------------------------------------------------------ #
    @property
    def listening(self) -> bool:
        return self._runner is not None and self._site is not None

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def token(self) -> str:
        """一次性令牌：每次启动新生成，**不落库、不读设置**。

        这条和 WebBridge 故意不同：那边是用户长期开启的网桥，口令要存下来复用；
        这边的进程是被客户端拉起来的子进程，口令只活这一次。
        """
        if self._token_value is None:
            self._token_value = secrets.token_hex(16)
        return self._token_value

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._bound_port}"

    def handshake_line(self) -> str:
        """交给启动方的那一行握手（父进程按 ``前缀 + JSON`` 解析）。

        端口只有绑定之后才知道（``port=0`` 由系统分配），所以这行**必须**在
        ``start_server`` 之后打——提前打就是报一个连不上的地址。
        """
        return "LEGODO_GATEWAY " + json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "build": APP_VERSION,
            "host": self._host,
            "port": self._bound_port,
            "token": self.token,
            "pid": os.getpid(),
        }, ensure_ascii=False)

    async def start_server(self) -> None:
        if self.listening or self._starting:
            return
        self._starting = True
        try:
            app = web.Application(middlewares=[self._auth_middleware],
                                  client_max_size=MAX_BODY_BYTES)
            app.add_routes([
                web.post("/rpc", self._h_rpc),
                web.get("/ws", self._h_ws),
                web.get("/media/{key}", self._h_media),
            ])
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self._host, self._port)
            try:
                await site.start()
            except OSError as exc:
                await runner.cleanup()
                raise RuntimeError(
                    f"网关监听 {self._host}:{self._port} 失败：{exc}") from exc
            self._runner, self._site = runner, site
            try:
                address = runner.addresses[0]
                self._bound_port = (address[1] if isinstance(address, tuple)
                                    else self._port)
            except Exception:
                self._bound_port = self._port
            self._subscribe_events()
            logger.info("GatewayService 监听 %s（回环 + 一次性令牌）", self.url)
        finally:
            self._starting = False

    async def stop_server(self) -> None:
        # 在途的 WebView 反向请求要先炸掉再拆台：runner 一 cleanup，那条 future
        # 就再也等不到回话（客户端的 RPC 进不来），留着只会让调用方挂到预算走完。
        self._fail_pending_webviews("网关已停")
        for ws in list(self._ws_clients):
            try:
                await ws.close(code=999, message=b"gateway stopped")
            except Exception:
                pass
        self._ws_clients.clear()
        for task in list(self._jobs):
            task.cancel()
        self._jobs.clear()
        self._cancel_idle_timer()
        self._cancel_flush()
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("GatewayService runner 清理异常")
        self._runner = None
        self._site = None
        self._bound_port = 0

    # ------------------------------------------------------------------ #
    # action 表里登记给网关自己的三个动作
    # ------------------------------------------------------------------ #
    async def hello(self) -> dict:
        """握手：版本/能力/schema 摘要。客户端比对不符就别起界面。"""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "build": APP_VERSION,
            "capabilities": list(CAPABILITIES),
            "schema_digest": self.schema_digest(),
            "actions": sorted(BY_ACTION),
            "pid": os.getpid(),
        }

    @staticmethod
    def schema_digest() -> str:
        """action 表 + 全部入参 schema 的摘要：改任何一个字段都会改这个串。

        ``fields``（出口投影）也在摘要里：它决定客户端能不能解出一行，
        白名单少一格就必须让握手不同意，而不是等客户端把空字段读成"这条源没名字"。
        """
        payload = {
            "actions": {
                action: {"params": list(method.params), "mode": method.mode,
                         "timeout": method.timeout,
                         "fields": list(method.fields) if method.fields else None}
                for action, method in BY_ACTION.items()
            },
            "schema": {action: schema.load(action) for action in sorted(BY_ACTION)},
        }
        return digest(payload)

    async def service_status(self) -> list[dict]:
        """全服务状态快照（``ServiceHub.status_report()`` 过一遍边界序列化）。"""
        if self.hub is None:
            return []
        return [to_json(item) for item in self.hub.status_report()]

    def request_shutdown(self, reason: str = "") -> dict:
        """置退出标志。**同步**：收摊不该排在任何在途业务后面才生效。"""
        self.request_exit(reason)
        return {"stopping": True, "reason": str(reason or "")}

    def request_exit(self, reason: str = "") -> None:
        logger.info("GatewayService 收到收摊请求：%s", reason or "（未说明）")
        if self._exit is None:
            logger.warning("网关没有退出通道（构造未注入 exit_event），请求被忽略")
            return
        self._exit.set()

    # ------------------------------------------------------------------ #
    # §8 R1 反向调用：可见 WebView 归客户端，引擎在它上面等一次渲染
    # ------------------------------------------------------------------ #
    async def request_web_content(self, url: str, source: Any = None,
                                  options: dict | None = None,
                                  timeout: float = WEBVIEW_TIMEOUT) -> dict:
        """把一次 WebView 渲染**推给客户端**并等它回传（引擎的 ``webview_fetcher`` 面）。

        为什么要有这一条：今天 ``<useweb>`` / ``url,{"webView":true}`` 那一族源（本仓库
        语料实测 56 条带 webView 选项、4 条带 ``ruleContent.webJs``）靠进程内的
        WebView/Chromium 取内容；进程一分为二之后**可见 WebView 只有 Flutter 那边有**，
        引擎这一侧只能"发出去、等回来"。设计 §8 R1 判的就是这个往返通不通。

        方向是反的（服务端→客户端），所以它不走 /rpc，走 /ws 的一条 ``request`` 帧；
        客户端渲染完再用 ``engine.webContentReady`` 把结果递回来。请求帧里带
        ``request_id``——它是这条等待的身份证，两个并发渲染不能共用一条 future。

        **没人连就别等**：没有会话时立刻抛 :class:`WebviewUnavailable`，因为这条线
        上永远不会有人回答；让引擎当场拿到异常退回纯 HTTP 路径，比挂满 20s 预算
        再超时便宜得多（``BrowserService.available_fetcher()`` 今天就是这个口径）。
        """
        if not self._ws_clients:
            raise WebviewUnavailable("没有已连接的 WebView 宿主（/ws 会话为 0）")
        self._webview_seq += 1
        request_id = f"w{self._webview_seq}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_webviews[request_id] = future
        frame = {
            "type": "request",
            "name": "webview.content",
            "request_id": request_id,
            "payload": {
                "url": str(url or ""),
                "source_url": str(getattr(source, "bookSourceUrl", "")
                                  or (source if isinstance(source, str) else "")
                                  or ""),
                "options": to_json(options or {}),
            },
        }
        try:
            await self._send(frame)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending_webviews.pop(request_id, None)

    async def web_content_ready(self, request_id: str, body: str = "",
                                headers: dict | None = None, status: int = 200,
                                url: str = "", cookie: str = "",
                                error: str = "", js_result: Any = None) -> dict:
        """客户端交回渲染结果：解开 :meth:`request_web_content` 那次等待。

        回晚了（引擎侧已经按预算超时走人）也算成功送达但**没人要了**——回
        ``{"delivered": false}`` 而不是抛错，因为这不是客户端的错，把 20s 之后
        才回来的渲染折成 ``engine`` 错误只会让人去查错地方。

        ``js_result`` 是 ``@webjs:`` 那条分支的完成值：请求帧里已经把脚本递过去
        （``options.javaScript``），宿主在页面里跑完要把它带回来——引擎侧
        ``LegadoJsRuntime._evaluate_webjs`` 读的就是这个键。没带就不塞这个键：
        空串会被读成"脚本没返回结果"，而"宿主压根没跑脚本"和"脚本返回了空"
        是两件事，别把前者写成后者。
        """
        future = self._pending_webviews.get(str(request_id or ""))
        if future is None or future.done():
            logger.info("WebView 渲染结果来晚了或没人等：%s", request_id)
            return {"delivered": False}
        if str(error or "").strip():
            future.set_exception(RuntimeError(f"客户端渲染失败：{error}"))
        else:
            result = {
                "body": str(body or ""),
                "headers": {str(k).lower(): str(v) for k, v in (headers or {}).items()},
                "status": int(status or 200),
                "url": str(url or ""),
                "cookie": str(cookie or ""),
            }
            if js_result is not None:
                result["jsResult"] = js_result
            future.set_result(result)
        return {"delivered": True}

    def _fail_pending_webviews(self, reason: str) -> None:
        """在途渲染必须有人把它炸掉：会话全断了还等 = 白等满预算。"""
        for future in list(self._pending_webviews.values()):
            if not future.done():
                future.set_exception(WebviewUnavailable(reason))
        self._pending_webviews.clear()

    # ------------------------------------------------------------------ #
    # POST /rpc —— 一个动词
    # ------------------------------------------------------------------ #
    async def _h_rpc(self, request: web.Request) -> web.Response:
        request_id: Any = None
        # 进来先掐掉倒计时、回完话再武装一次：HTTP 客户端没有"会话断开"这个事件
        # （known-issues 005），它的空闲只能读成"最后一次成功动作之后安静了 N 秒"。
        self._cancel_idle_timer()
        try:
            raw = await request.read()
            try:
                envelope = json.loads(bytes(raw).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self._reply(err_envelope(
                    request_id, CODE_BAD_REQUEST, f"请求体不是合法 JSON：{exc}"))
            if not isinstance(envelope, dict):
                return self._reply(err_envelope(
                    request_id, CODE_BAD_REQUEST, "信封必须是 JSON 对象"))
            request_id = envelope.get("id")
            reply = await self._dispatch(envelope)
            if reply.get("ok"):
                self._clients_seen += 1
            return self._reply(reply)
        except web.HTTPRequestEntityTooLarge:
            # client_max_size 先于 read() 返回体抛到这里：这一条也得是信封，
            # 不能是 aiohttp 那坨 HTML 错误页（客户端按 JSON 解析就是解析异常）。
            return self._reply(err_envelope(
                request_id, CODE_BAD_REQUEST,
                f"请求体超过上限 {MAX_BODY_BYTES} 字节"))
        except BaseException as exc:  # noqa: BLE001 - 信封不合法比功能失败更贵
            logger.exception("GatewayService /rpc 未捕获异常")
            return self._reply(err_envelope(
                request_id, CODE_ENGINE, f"{type(exc).__name__}: {exc}"))
        finally:
            self._arm_idle_timer()

    async def _dispatch(self, envelope: dict) -> dict:
        request_id = envelope.get("id")
        version = envelope.get("v")
        if version != PROTOCOL_VERSION:
            return err_envelope(request_id, CODE_BAD_REQUEST,
                                f"协议版本不符：期望 {PROTOCOL_VERSION}，"
                                f"收到 {version!r}")
        action = str(envelope.get("action") or "")
        method = BY_ACTION.get(action)
        if method is None:
            return err_envelope(request_id, CODE_BAD_REQUEST, f"未知 action：{action!r}")
        payload = envelope.get("payload")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            return err_envelope(request_id, CODE_BAD_REQUEST, "payload 必须是 JSON 对象")
        try:
            schema.validate(payload, schema.load(action))
        except (schema.SchemaError, ValueError) as exc:
            # ValueError 覆盖"schema 文件写了没实现的关键词"——那是装配错误，
            # 对客户端仍然只可能是 schema 一类，别让它冒成 engine。
            return err_envelope(request_id, CODE_SCHEMA, str(exc))
        except FileNotFoundError as exc:
            return err_envelope(request_id, CODE_SCHEMA, f"入参 schema 缺失：{exc}")

        try:
            target = self._target(method)
        except Exception as exc:
            return err_envelope(request_id, CODE_UNAVAILABLE,
                                f"{method.service}.{method.method} 不可用：{exc}")

        kwargs = {name: payload[name] for name in method.params if name in payload}
        try:
            if method.mode == "job":
                return ok_envelope(request_id, self._start_job(method, target, kwargs))
            result = target(**kwargs)
            if method.mode != "sync":
                result = await asyncio.wait_for(result, timeout=method.timeout)
        except asyncio.TimeoutError:
            return err_envelope(request_id, CODE_TIMEOUT,
                                f"{action} 超过服务端预算 {method.timeout:g}s")
        except asyncio.CancelledError:
            return err_envelope(request_id, CODE_CANCELLED, f"{action} 被取消")
        except Exception as exc:
            return err_envelope(request_id, CODE_ENGINE,
                                f"{type(exc).__name__}: {exc}")
        data = to_json(result)
        if method.fields is not None:
            data = project(data, method.fields)
        return ok_envelope(request_id, data)

    def _target(self, method: Method):
        """解析出可调用目标（服务没注册/方法不存在 → 抛，由调用方折成错误信封）。"""
        if self.hub is None:
            raise RuntimeError("GatewayService 未挂到 ServiceHub")
        cls = resolve_service_class(method.service)
        target = getattr(self.hub.get(cls), method.method)
        if not callable(target):
            raise TypeError(f"{method.service}.{method.method} 不可调用")
        return target

    def _start_job(self, method: Method, target, kwargs: dict) -> dict:
        """起后台任务并**立即**回受理凭据；进度与结论走事件流。"""
        task = asyncio.ensure_future(target(**kwargs))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        task.add_done_callback(_log_task)
        logger.info("网关起后台动作 %s", method.action)
        return {"started": True, "action": method.action}

    @staticmethod
    def _reply(envelope: dict) -> web.Response:
        text = json.dumps(envelope, ensure_ascii=False, default=str)
        return web.Response(text=text, content_type="application/json")

    # ------------------------------------------------------------------ #
    # 令牌中间件
    # ------------------------------------------------------------------ #
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        supplied = request.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[len("Bearer "):].strip()
        if not supplied:
            supplied = request.query.get("token", "")
        # 比字节而不是 str：``secrets.compare_digest`` 对**非 ASCII 字符串**直接抛
        # TypeError，令牌的来源是外部输入，非 ASCII 就是它的一种合法形状。抛穿
        # 中间件就是 500 + aiohttp 错误页，"令牌不对"这件事被写成"服务炸了"。
        if (not supplied
                or not secrets.compare_digest(supplied.encode("utf-8"),
                                              self.token.encode("utf-8"))):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------ #
    # GET /ws —— 事件流（不轮询）
    # ------------------------------------------------------------------ #
    async def _h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        self._clients_seen += 1
        self._cancel_idle_timer()
        try:
            await ws.send_json({
                "type": "ready",
                "protocol_version": PROTOCOL_VERSION,
                "build": APP_VERSION,
                "capabilities": list(CAPABILITIES),
                "schema_digest": self.schema_digest(),
            })
            # 重连补状态：先给"最近一份"，再发合流窗口里攒下的，最后才进直播。
            # 顺序反了会让旧状态盖掉新事件（记忆 ui-progress-must-travel-as-events）。
            if self._snapshots:
                await ws.send_json({"type": "snapshot", "events": [
                    {"name": name, "payload": payload}
                    for name, payload in sorted(self._snapshots.items())
                ]})
            await self._flush_pending()
            async for _ in ws:
                pass      # 这一条流只读；要发东西走 /rpc
        except Exception:
            logger.debug("GatewayService /ws 会话结束", exc_info=True)
        finally:
            self._ws_clients.discard(ws)
            if not self._ws_clients:
                self._fail_pending_webviews("最后一个 /ws 会话已断开")
                self._arm_idle_timer()
        return ws

    # ------------------------------------------------------------------ #
    # EventBus → WS
    # ------------------------------------------------------------------ #
    def _subscribe_events(self) -> None:
        if self.bus is None:
            return
        for cls in event_types():
            try:
                self._unsubs.append(self.bus.subscribe(cls, self._make_forwarder(cls)))
            except Exception:
                logger.exception("GatewayService 订阅 %s 失败", cls.__name__)

    def _make_forwarder(self, cls: type):
        name = cls.__name__

        async def _forward(event: AppEvent) -> None:
            frame = {"type": "event", "name": name, "payload": to_json(event)}
            if name in SNAPSHOT_EVENTS:
                self._snapshots[name] = frame["payload"]
            if name in COALESCED_EVENTS:
                self._pending.append(frame)
                self._schedule_flush()
                return
            # 非合流事件：先把攒下的进度打包发掉，客户端才按序看到。
            await self._flush_pending()
            await self._send(frame)
        return _forward

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None:
            return
        self._flush_handle = asyncio.get_running_loop().call_later(
            COALESCE_WINDOW, self._flush_soon)

    def _flush_soon(self) -> None:
        self._flush_handle = None
        asyncio.ensure_future(self._flush_pending())

    def _cancel_flush(self) -> None:
        if self._flush_handle is not None:
            try:
                self._flush_handle.cancel()
            except Exception:
                pass
        self._flush_handle = None
        self._pending.clear()

    async def _flush_pending(self) -> None:
        if not self._pending:
            return
        frames, self._pending = self._pending, []
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        await self._send({"type": "batch", "events": frames})

    async def _send(self, message: dict) -> None:
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)
        if dead and not self._ws_clients:
            self._arm_idle_timer()

    # ------------------------------------------------------------------ #
    # 空闲自杀：最后一个客户端安静下来 N 秒后收摊（§4.6）
    # WS 的"安静"是会话断开，HTTP 的"安静"是最后一次成功动作之后没人再来。
    # ------------------------------------------------------------------ #
    def _arm_idle_timer(self) -> None:
        """只在**有过客户端**之后武装：一次动作都没办成过的探针不该被倒计时打断。

        要一段永不打断的运行（手动 curl 慢慢试）用 ``--idle-seconds 0``，那才是
        关掉倒计时的开关；这里拦的是"从没跟人说过话"这一种情况。
        """
        if self._clients_seen == 0 or self._idle_timer is not None:
            return
        if self._idle_exit_seconds <= 0:
            return
        self._idle_timer = asyncio.get_running_loop().call_later(
            self._idle_exit_seconds, self._on_idle_expired)

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            try:
                self._idle_timer.cancel()
            except Exception:
                pass
        self._idle_timer = None

    def _on_idle_expired(self) -> None:
        self._idle_timer = None
        if self._ws_clients:
            return
        if self._jobs:
            # job 动作一发就回受理凭据，本体还在跑；这时候收摊等于把用户的活
            # 半程杀掉，而且没人记账（进度只从事件流走，客户端可能正在等）。
            # 改判成"N 秒后再看一眼"。代价要说清楚：任务本体没有服务端预算
            # （``mode="job"`` 不受 ``method.timeout`` 约束），所以一个挂死的任务
            # 会让这条推迟一直推迟下去——真要收摊走 ``engine.shutdown``。
            logger.info("网关空闲但仍有 %d 个在途动作，推迟 %.0fs 再看",
                        len(self._jobs), self._idle_exit_seconds)
            self._arm_idle_timer()
            return
        logger.info("网关空闲超过 %.0fs（最后一个客户端已安静），请求进程收摊",
                    self._idle_exit_seconds)
        self.request_exit("idle")

    # ------------------------------------------------------------------ #
    # GET /media/{key} —— 二进制不走 RPC（§4.4）
    # ------------------------------------------------------------------ #
    async def _h_media(self, request: web.Request) -> web.Response:
        # 拉封面也算"有人在用"：一屏封面拉得比窗口久时，只看 /rpc 会把活着的
        # 客户端切掉。这里只掐表/武装，不给 `_clients_seen` 加数——一次动作都没
        # 办成过的探针仍然豁免。
        self._cancel_idle_timer()
        try:
            return await self._media(request)
        finally:
            self._arm_idle_timer()

    async def _media(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if not _MEDIA_KEY_RE.match(key):
            return web.Response(status=404, text="unknown media key")
        root = self._covers_dir()
        if root is None:
            return web.Response(status=503, text="封面目录不可用")
        # key 已被正则钉死成 [0-9a-f]{16}.jpg：拼不出目录穿越。
        path = root / key
        if not path.is_file():
            return web.Response(status=404, text="not found")
        whole = path.stat().st_size
        body = path.read_bytes()
        headers = {"Accept-Ranges": "bytes"}
        status = 200
        spec = request.headers.get("Range", "")
        if spec.startswith("bytes="):
            span = _parse_range(spec[6:], whole)
            if span is None:
                # 越界回裸 416：Range 是 HTTP 语义，不归 RPC 信封管。
                return web.Response(status=416,
                                    headers={"Content-Range": f"bytes */{whole}"})
            start, end = span
            body = body[start:end + 1]
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{whole}"
        return web.Response(body=body, status=status,
                            content_type="image/jpeg", headers=headers)

    def _covers_dir(self) -> Path | None:
        """封面落点：走与 SourceService 同一条 ``StorageService.cache_dir`` 口径。"""
        if self.hub is None:
            return None
        try:
            from app.core.storage import StorageService

            return self.hub.require(StorageService).cache_dir("image-cache/covers")
        except Exception:
            logger.debug("网关取封面目录失败", exc_info=True)
            return None


def _parse_range(spec: str, size: int) -> tuple[int, int] | None:
    """``a-b`` / ``a-`` / ``-suffix`` → 闭区间两端；越界返回 None。"""
    text = spec.strip()
    if not text or size <= 0:
        return None
    try:
        if text.startswith("-"):
            suffix = int(text[1:])
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        head, _, tail = text.partition("-")
        start = int(head)
        end = int(tail) if tail else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)
