"""WebBridgeService：本地 Web 服务（远程书源管理 / 调试）（M5，契约 §10 / 设计 §1.15）。

用 aiohttp.web 起 HTTP + WebSocket，把书源管理/检测/调试能力以薄协议层暴露到本机：

- **安全面（红线 §5 / §1.15）**：默认**仅绑定 127.0.0.1**（回环，免局域网防火墙弹窗，
  三端无平台分支）；**开关默认关闭**（SettingsService 键 ``webbridge.enabled``，默认
  False）；启用后**所有端点经令牌校验**（``Authorization: Bearer <token>`` 或
  ``?token=``）。on_start 时开关关闭则**不监听**（懒启动），服务处于 SUSPENDED 语义。
- **薄协议层**：书源 CRUD/检测/调试全部委托 SourceService/SourceCheckService/
  RuleEngineService，本服务不重复实现业务；
- **EventBus → WebSocket**：把调试事件（SourceChanged/CheckFinished 等）推给已连接的
  浏览器端，供远程调试面板实时观察。

红线 §5 说明：``webbridge.token`` 存 SettingsService 是契约 §10 显式要求——它是用户
**主动开启网桥后**自行生成/配置的本机共享口令，用于回环端口的二次防护，而非第三方
凭据泄漏；默认关闭时不生成、不写库。

硬约束：本模块零 flet import；aiohttp 懒加载（on_start 启用时才 import）；跨平台。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from aiohttp import web

from app.core.events import CheckFinishedEvent, SourceChangedEvent
from app.core.lifecycle import Service
# F5：模块级 import 用于 dependencies 拓扑声明（core 不反向依赖 services，无环）。
from app.core.settings import SettingsService

logger = logging.getLogger(__name__)

#: 网桥三项偏好键（界面上的开关/端口/令牌都写在这里；键名沿用契约 §10）。
#: 常量导出给控制器与测试用——字符串各写一遍迟早会飘。
ENABLED_KEY = "webbridge.enabled"
PORT_KEY = "webbridge.port"
TOKEN_KEY = "webbridge.token"

#: 红线 §5：只绑回环。局域网暴露要用户改代码承担，不是界面上的一个勾。
DEFAULT_HOST = "127.0.0.1"

#: 对外端点清单（设置页只读展示）。改路由必须同步改这里——界面在替用户念地址，
#: 念错一条就等于骗他去调一个 404 的接口。
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET /api/health", "服务与协作者就绪状态"),
    ("GET /api/sources", "书源列表"),
    ("POST /api/sources", "保存书源"),
    ("DELETE /api/sources/{url}", "删除书源"),
    ("POST /api/sources/check", "检测单个书源"),
    ("POST /api/sources/check_all", "检测全部书源"),
    ("POST /api/sources/debug", "规则试跑（search / bookInfo / chapters / content）"),
    ("GET /ws", "事件流（WebSocket）"),
)


class WebBridgeService(Service):
    """本地书源管理/调试网桥：默认关闭、回环绑定、令牌校验、业务全委托。"""

    name = "WebBridgeService"
    # F5：声明依赖 SettingsService —— 拓扑保证其先于本服务 on_start（缓存已从主库
    # 加载），on_start 读到的 webbridge.enabled 即上次持久化值；再叠加 _apply_settings
    # 启动时重读，形成"拓扑先就绪 + 启动时重读"双保险。其余业务协作者仍懒解析。
    dependencies: tuple[type[Service], ...] = (SettingsService,)

    def __init__(self, settings=None, source_service=None, source_check=None,
                 rule_engine=None, host: str = DEFAULT_HOST, port: int = 0,
                 token: str | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._source_service = source_service
        self._source_check = source_check
        self._rule_engine = rule_engine
        self._host = host
        self._port = int(port)
        self._token_override = token
        # ── aiohttp 运行期对象 ──
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._app: web.Application | None = None
        self._bound_port: int = 0
        self._starting = False
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._unsubs: list[Any] = []

    # ------------------------------------------------------------------ #
    # 生命周期（开关默认关闭：on_start 不监听 = SUSPENDED 语义）
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        # 依赖懒解析：构造未注入时经 hub.require 取已注册实例（真实环境下
        # SettingsService 已在拓扑中先于本服务启动，此处仅兜底无注入场景）。
        if self._settings is None and self.hub is not None:
            try:
                self._settings = self.hub.require(SettingsService)
            except Exception:
                self._settings = None
        self._resolve_collaborators()
        # 订阅开关变更，运行期热启停网桥（SettingsChangedEvent 由 EventBus 派发）。
        self._subscribe_settings()
        # F5：拓扑先就绪后，启动时再按持久化值幂等拉起/停止网桥。
        await self._apply_settings()

    async def on_stop(self) -> None:
        await self.stop_server()
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        logger.info("WebBridgeService 已停止")

    def _resolve_collaborators(self) -> None:
        """经 hub.require 补齐未注入的业务服务（缺失则对应端点降级 503）。"""
        if self.hub is None:
            return

        def _try(cls_name: str):
            try:
                import importlib
                module_path, class_name = cls_name.rsplit(".", 1)
                cls = getattr(importlib.import_module(module_path), class_name)
                return self.hub.require(cls)
            except Exception:
                return None

        if self._source_service is None:
            self._source_service = _try(
                "app.services.sources.source_service.SourceService")
        if self._source_check is None:
            self._source_check = _try(
                "app.services.sources.source_check_service.SourceCheckService")
        if self._rule_engine is None:
            self._rule_engine = _try(
                "app.services.engine.rule_engine_service.RuleEngineService")

    def _subscribe_settings(self) -> None:
        """订阅开关/端口变更，运行期热启停网桥；总线不可用时静默跳过。"""
        if self.bus is None:
            return
        from app.core.events import SettingsChangedEvent

        async def _on_changed(ev: SettingsChangedEvent) -> None:
            if ev.key == ENABLED_KEY:
                # 开关只有一条启动路径：界面按钮和别处写设置都收敛到同一状态机。
                try:
                    await self._apply_settings()
                except Exception:
                    logger.exception("WebBridge 开关热切换异常")
            elif ev.key == PORT_KEY and self.listening:
                # 端口只在 start_server 时读一次：正在监听就得换端口重来。
                try:
                    await self.stop_server()
                    await self.start_server()
                except Exception:
                    logger.exception("WebBridge 换端口重启失败")

        try:
            self._unsubs.append(self.bus.subscribe(SettingsChangedEvent, _on_changed))
        except Exception:
            pass

    async def _apply_settings(self) -> None:
        """F5：按持久化 ``webbridge.enabled`` 幂等拉起/停止网桥。

        拓扑已保证 SettingsService 先于本服务 on_start（其缓存已从主库加载），
        此处再读一次即用户上次保存的值：上次开启 → 重启后自动 ``start_server``；
        与当前监听态一致时幂等返回，不重复绑定/不重复停。
        """
        want = self._enabled()
        if want and not self.listening:
            await self.start_server()
        elif not want and self.listening:
            await self.stop_server()
        elif not want:
            logger.info("WebBridgeService 开关关闭（webbridge.enabled 默认 False），"
                        "不监听，待用户显式开启")
        logger.info("WebBridgeService 已启动（listening=%s）", self.listening)

    # ------------------------------------------------------------------ #
    # 开关 / 令牌 / 端口（懒读 SettingsService，缺失用默认）
    # ------------------------------------------------------------------ #
    def _enabled(self) -> bool:
        if self._settings is None:
            return False
        return bool(self._settings.get(ENABLED_KEY, False))

    def _peek_token(self) -> str:
        """只读当前令牌，**绝不生成**——状态页每刷新一次都要问一遍。"""
        if self._token_override:
            return self._token_override
        if self._settings is None:
            return ""
        return str(self._settings.get(TOKEN_KEY, "") or "")

    def _resolve_token(self) -> str:
        token = self._peek_token()
        if token or self._settings is None:
            return token
        # 首次启用且未配置令牌：生成随机口令并写回 Settings（红线 §5：用户显式开启
        # 网桥后才生成；回环二次防护用，不属第三方凭据）。
        token = secrets.token_hex(16)
        try:
            self._settings.set(TOKEN_KEY, token)
        except Exception:
            logger.exception("WebBridge 令牌写回失败")
        return str(token)

    def _resolve_port(self) -> int:
        port = self._port
        if port == 0 and self._settings is not None:
            port = int(self._settings.get(PORT_KEY, 0) or 0)
        return port

    # ------------------------------------------------------------------ #
    # 界面向导（设置页唯一的写入口：改完返回一帧新状态）
    # ------------------------------------------------------------------ #
    async def bridge_status(self) -> dict:
        """一帧可渲染状态 ``{enabled, listening, host, port, url, token, endpoints}``。

        ``port`` 取**实际绑定端口**：port=0 时由系统分配，界面上报 "0" 等于没报，
        用户拿它拼不出能访问的地址。
        """
        port = int(self._bound_port or self._resolve_port() or 0)
        return {
            "enabled": self._enabled(),
            "listening": self.listening,
            "host": self._host,
            "port": port,
            "url": f"http://{self._host}:{port}" if port else "",
            "token": self._peek_token(),
            "endpoints": list(ENDPOINTS),
        }

    async def set_enabled(self, enabled: bool) -> dict:
        """开/关网桥：写开关后按目标态收敛（和 SettingsChangedEvent 同一条路径）。"""
        if self._settings is None:
            raise RuntimeError("SettingsService 未就绪，网桥开关无法持久化")
        self._settings.set(ENABLED_KEY, bool(enabled))
        await self._apply_settings()
        return await self.bridge_status()

    async def set_port(self, port) -> dict:
        """改监听端口（0 = 交给系统分配）；正在监听就换端口重来。"""
        if self._settings is None:
            raise RuntimeError("SettingsService 未就绪，端口无法持久化")
        try:
            value = int(str(port or "0").strip())
        except (TypeError, ValueError):
            raise ValueError(f"端口不是数字：{port!r}") from None
        if not 0 <= value <= 65535:
            raise ValueError(f"端口要在 0–65535 之间（0 表示自动分配）：{value}")
        self._settings.set(PORT_KEY, value)
        if self.listening:
            await self.stop_server()
            await self.start_server()
        return await self.bridge_status()

    async def regenerate_token(self) -> dict:
        """换新口令。中间件每请求现读口令，所以旧口令当场失效，不必重启。"""
        if self._settings is None:
            raise RuntimeError("SettingsService 未就绪，令牌无法持久化")
        # 构造注入的口令优先级高于设置表，重新生成必须让它让位，
        # 否则新口令写了库、校验与状态页却还在读旧的。
        self._token_override = None
        self._settings.set(TOKEN_KEY, secrets.token_hex(16))
        return await self.bridge_status()

    # ------------------------------------------------------------------ #
    # server 启停
    # ------------------------------------------------------------------ #
    @property
    def listening(self) -> bool:
        """是否正在监听（SUSPENDED/RUNNING 可观测面）。"""
        return self._runner is not None and self._site is not None

    @property
    def bound_port(self) -> int:
        return self._bound_port

    async def start_server(self) -> None:
        """搭建 aiohttp AppRunner 并绑定回环端口（幂等）。

        ``_starting`` 不是多余的保险：开关写设置会发 SettingsChangedEvent，处理器和
        界面按钮都可能落到同一趟 loop 上，而 ``listening`` 要 runner 与 site 都在
        才算真——中间 await 时它还是 False，第二次进来就会再绑一个 runner（端口占用
        + 前一个永不回收）。
        """
        if self.listening or self._starting:
            return
        self._starting = True
        try:
            token = self._resolve_token()
            if not token:
                logger.warning("WebBridge 无令牌，拒绝监听（安全面）")
                return
            app = web.Application(middlewares=[self._auth_middleware])
            self._register_routes(app)
            runner = web.AppRunner(app)
            await runner.setup()
            port = self._resolve_port()
            try:
                site = web.TCPSite(runner, self._host, port)
                await site.start()
            except OSError as exc:
                # 端口被人占了：先把刚 setup 的 runner 收掉，别留半个监听。
                await runner.cleanup()
                raise RuntimeError(f"监听 {self._host}:{port} 失败：{exc}") from exc
            # 全部就位才对外可见（同上：半途状态不能被第二次调用当成"没在监听"）。
            self._app, self._runner, self._site = app, runner, site
            # 读出实际绑定端口（port=0 时由 OS 分配）。
            try:
                sock = runner.addresses[0]
                self._bound_port = sock[1] if isinstance(sock, tuple) else port
            except Exception:
                self._bound_port = port
            self._subscribe_bus_to_ws()
            logger.info("WebBridge 监听 http://%s:%d（回环+令牌）",
                        self._host, self._bound_port)
        finally:
            self._starting = False

    async def stop_server(self) -> None:
        # 先关 WebSocket 客户端，再停 runner。
        for ws in list(self._ws_clients):
            try:
                await ws.close(code=999, message=b"bridge stopped")
            except Exception:
                pass
        self._ws_clients.clear()
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("WebBridge runner 清理异常")
        self._runner = None
        self._site = None
        self._app = None
        self._bound_port = 0

    # ------------------------------------------------------------------ #
    # 路由注册（薄协议层，业务全委托）
    # ------------------------------------------------------------------ #
    def _register_routes(self, app: web.Application) -> None:
        app.add_routes([
            web.get("/api/health", self._h_health),
            web.get("/api/sources", self._h_list_sources),
            web.post("/api/sources", self._h_save_source),
            web.delete("/api/sources/{url:.*}", self._h_delete_source),
            web.post("/api/sources/check", self._h_check_source),
            web.post("/api/sources/check_all", self._h_check_all),
            web.post("/api/sources/debug", self._h_debug),
            web.get("/ws", self._h_ws),
        ])

    # ── 令牌校验中间件（红线：默认关闭+回环+令牌）──
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        token = self._resolve_token()
        supplied = request.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[len("Bearer "):].strip()
        if not supplied:
            supplied = request.query.get("token", "")
        if not supplied or supplied != token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    # ------------------------------------------------------------------ #
    # 端点处理（全部委托 SourceService/SourceCheckService/RuleEngineService）
    # ------------------------------------------------------------------ #
    async def _h_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True, "service": self.name,
            "has_source_service": self._source_service is not None,
            "has_check_service": self._source_check is not None,
            "has_rule_engine": self._rule_engine is not None,
        })

    async def _h_list_sources(self, request: web.Request) -> web.Response:
        svc = self._source_service
        if svc is None:
            return self._unavailable("SourceService 未注册")
        sources = await svc.list_sources()
        payload = []
        for s in sources:
            to_dict = getattr(s, "to_dict", None)
            payload.append(to_dict() if callable(to_dict) else dict(s))
        return web.json_response({"sources": payload})

    async def _h_save_source(self, request: web.Request) -> web.Response:
        svc = self._source_service
        if svc is None:
            return self._unavailable("SourceService 未注册")
        body = await request.json()
        result = await svc.save_source(body)
        return web.json_response(result)

    async def _h_delete_source(self, request: web.Request) -> web.Response:
        svc = self._source_service
        if svc is None:
            return self._unavailable("SourceService 未注册")
        from urllib.parse import unquote
        url = unquote(request.match_info["url"])
        removed = await svc.delete_source(url)
        return web.json_response({"deleted": bool(removed)})

    async def _h_check_source(self, request: web.Request) -> web.Response:
        svc = self._source_check
        if svc is None:
            return self._unavailable("SourceCheckService 未注册")
        body = await request.json()
        result = await svc.check_source(body)
        return web.json_response(result)

    async def _h_check_all(self, request: web.Request) -> web.Response:
        svc = self._source_check
        if svc is None:
            return self._unavailable("SourceCheckService 未注册")
        body = await request.json() if request.can_read_body else {}
        result = await svc.check_all(body.get("groups"))
        return web.json_response(result)

    async def _h_debug(self, request: web.Request) -> web.Response:
        """书源试跑调试：action=search/bookInfo/chapters/content 委托 RuleEngineService。"""
        engine = self._rule_engine
        if engine is None:
            return self._unavailable("RuleEngineService 未注册")
        body = await request.json()
        source = body.get("source")
        action = str(body.get("action") or "search")
        try:
            if action == "search":
                out = await engine.search(
                    source, str(body.get("keyword", "")), int(body.get("page", 1)))
            elif action == "bookInfo":
                out = await engine.book_info(source, body.get("book"))
            elif action == "chapters":
                out = await engine.chapters(source, body.get("book"))
            elif action == "content":
                out = await engine.content(source, body.get("chapter"))
            else:
                return web.json_response({"error": f"未知 action: {action}"},
                                        status=400)
        except Exception as exc:
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                    status=500)
        # 结果可能含非 JSON 原生类型，做一次容错序列化。
        return web.json_response({"action": action, "result": _safe_json(out)})

    async def _h_ws(self, request: web.Request) -> web.WebSocketResponse:
        """调试日志 WebSocket：EventBus 事件推送至此。"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            await ws.send_json({"type": "ready", "service": self.name})
            async for _ in ws:
                pass  # 客户端消息暂不处理，仅服务端推送
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ------------------------------------------------------------------ #
    # EventBus → WebSocket 广播
    # ------------------------------------------------------------------ #
    def _subscribe_bus_to_ws(self) -> None:
        if self.bus is None:
            return
        for event_type, kind in ((SourceChangedEvent, "source_changed"),
                                 (CheckFinishedEvent, "check_finished")):
            try:
                self._unsubs.append(
                    self.bus.subscribe(event_type, self._make_broadcaster(kind)))
            except Exception:
                pass

    def _make_broadcaster(self, kind: str):
        async def _broadcast(ev: Any) -> None:
            await self.broadcast({"type": kind, "data": _event_to_dict(ev)})
        return _broadcast

    async def broadcast(self, message: dict) -> None:
        """向全部已连接调试 WebSocket 推送一条消息。"""
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _unavailable(message: str) -> web.Response:
        return web.json_response({"error": message}, status=503)


# ---------------------------------------------------------------------- #
# 序列化辅助（事件/调试结果可能含 dataclass / 非原生类型）
# ---------------------------------------------------------------------- #
def _event_to_dict(ev: Any) -> dict:
    to_dict = getattr(ev, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    out: dict = {}
    for field in vars(ev):
        out[field] = getattr(ev, field, None)
    return out


def _safe_json(value: Any) -> Any:
    """容错把调试结果转成 JSON 原生类型（失败退化为字符串）。"""
    import json

    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)
