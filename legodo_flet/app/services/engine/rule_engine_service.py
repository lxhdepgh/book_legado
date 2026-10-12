# -*- coding: utf-8 -*-
"""RuleEngineService：核心资产引擎的外壳（M1 任务 2，§9.3）。

本服务是 ``app/services/engine/`` 这 9 个核心资产引擎文件的**薄外壳**：
不复制、不改动引擎的解析/请求/JS 行为，只负责——

1. 生命周期：与并行子代理交付的 ``app.core.lifecycle.Service`` 对齐——
   重写 ``on_start/on_stop``，由基类统一驱动状态机
   （REGISTERED→STARTING→RUNNING→STOPPING→DISPOSED）。core 尚未就位时，本模块自带
   一份等价的 on_start/on_stop 回落基类，保证离线单测可跑。
2. 统一入口：chapters / book_info / content / search / explore 转发到池里对应的
   LegadoReadingEngine 实例；put/get 统一转发到引擎变量袋
   （rule_engine._put_variable / _get_variable 语义）。
3. 网络栈配置：on_start() 时从 SettingsService 读取全局偏好（userAgent 等），
   经 ``app.services.engine._prefs`` 注入全局，供引擎发请求时读取。
4. 短命引擎实例池：按 ``bookSourceUrl`` LRU 缓存 LegadoReadingEngine 实例，
   上限可配置；淘汰时 aclose() 释放 aiohttp 会话与 JS runtime。

⚠️ 本文件是外壳，**不改变引擎行为**；所有规则语义仍在 engine/ 内。
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

import aiohttp

from app.services.engine.reading_engine import LegadoReadingEngine
from app.services.engine.rule_engine import dns_ip_resolver
from app.services.engine._prefs import set_override

logger = logging.getLogger(__name__)

# [M1] core.lifecycle 由并行子代理交付；宽松 import，未就位时回落本地状态机。
try:  # pragma: no cover - 取决于并行 core 是否先落地
    from app.core.lifecycle import Service, ServiceState  # type: ignore
except Exception:  # pragma: no cover - 离线/集成前的回落路径

    class _State:
        """与 app.core.lifecycle.ServiceState 对齐的最小状态枚举（回落用）。"""
        INIT = "INIT"
        STARTING = "STARTING"
        RUNNING = "RUNNING"
        STOPPING = "STOPPING"
        DISPOSED = "DISPOSED"

    ServiceState = _State  # type: ignore

    class Service:  # type: ignore
        """与 app.core.lifecycle.Service 对齐的最小基类（回落用）。

        同样采用 on_start/on_stop 扩展点，公共 start/stop 驱动状态迁移。
        """

        name = "Service"

        def __init__(self) -> None:
            self.status = type("S", (), {
                "state": ServiceState.INIT, "last_error": "",
            })()

        async def start(self) -> None:
            self.status.state = ServiceState.STARTING
            await self.on_start()
            self.status.state = ServiceState.RUNNING

        async def stop(self) -> None:
            self.status.state = ServiceState.STOPPING
            await self.on_stop()
            self.status.state = ServiceState.DISPOSED

        async def on_start(self) -> None:
            ...

        async def on_stop(self) -> None:
            ...


def _source_key(source) -> str:
    """书源身份键：优先 bookSourceUrl，其次 id(source)。"""
    url = str(getattr(source, "bookSourceUrl", "") or "").strip()
    return url or f"__anon_{id(source)}"


class RuleEngineService(Service):
    """LegadoReadingEngine 的生命周期外壳 + 短命实例池。"""

    #: 服务名（§0.4 冻结词汇表，仪表盘/事件按此路由）。
    name = "RuleEngineService"

    #: 默认池上限：与换源扫描的并发数对齐（Legado 的 threadCount 也是 32）。
    #: 池小于并发数时，扫描会把已建好的引擎连同它的会话一起淘汰掉；
    #: 引擎本身很便宜（实测构造 0.55ms），省下的只是对象，代价是冷连接。
    DEFAULT_POOL_SIZE = 32

    def __init__(self, state_dir=None, pool_size: int = DEFAULT_POOL_SIZE,
                 timeout: int = 15, connect_timeout: float | None = None,
                 webview_fetcher=None, verification_fetcher=None):
        super().__init__()
        self.state_dir = state_dir
        self.pool_size = max(1, int(pool_size))
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.webview_fetcher = webview_fetcher
        self.verification_fetcher = verification_fetcher
        # bookSourceUrl -> LegadoReadingEngine（OrderedDict 实现 LRU）。
        self._pool: OrderedDict[str, LegadoReadingEngine] = OrderedDict()
        self._pool_lock = asyncio.Lock()
        self._default_engine: LegadoReadingEngine | None = None
        # 每个事件循环共享一条 TCP 连接池（对标 OkHttp 单例 client 的
        # ConnectionPool）。aiohttp 的对象绑死在创建它的循环上，而本服务的入口
        # 可能来自引擎循环，也可能来自直接 require 本服务的后台服务，所以按循环
        # 分桶，绝不把一条 connector 交给两个循环用。
        self._shared_connectors: "OrderedDict[Any, Any]" = OrderedDict()

    # ------------------------------------------------------------------ #
    # 生命周期扩展点（基类统管状态机：STARTING→RUNNING / STOPPING→DISPOSED）
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        """初始化池，并把 SettingsService 的全局偏好注入引擎层。"""
        # [M1-2.13/2.20] 网络栈配置：从 SettingsService 读全局偏好，注入 _prefs。
        self._apply_global_prefs()
        logger.info("RuleEngineService 已启动（池上限=%d）", self.pool_size)

    async def on_stop(self) -> None:
        """释放池内所有引擎（close aiohttp 会话/JS runtime）并清池。"""
        async with self._pool_lock:
            for engine in list(self._pool.values()):
                try:
                    await engine.aclose()
                except Exception as exc:  # pragma: no cover - 释放容错
                    logger.warning("关闭引擎实例失败：%s", exc)
            self._pool.clear()
            if self._default_engine is not None:
                try:
                    await self._default_engine.aclose()
                except Exception:  # pragma: no cover
                    pass
                self._default_engine = None
            if self._shared_connectors:
                # 会话不拥有 connector（engine.aclose 只关会话），所以全池释放完
                # 之后由这里做唯一的关闭。绑在别的循环上的留给那个循环自己关。
                current = asyncio.get_running_loop()
                for loop in list(self._shared_connectors):
                    if loop is not current:
                        continue
                    try:
                        await self._shared_connectors.pop(loop).close()
                    except Exception:  # pragma: no cover
                        pass
        logger.info("RuleEngineService 已停止")

    def _apply_global_prefs(self) -> None:
        """把 SettingsService 的全局 UA/超时偏好注入引擎层（软读取，缺失则用默认）。"""
        try:
            from app.core import settings as _core_settings  # type: ignore
        except Exception:
            return
        getter = (getattr(_core_settings, "get_setting", None)
                  or getattr(_core_settings, "get", None))
        if not callable(getter):
            return
        for key in ("userAgent",):
            try:
                value = getter(key, None)
            except Exception:
                value = None
            if value is not None:
                set_override(key, value)

    # ------------------------------------------------------------------ #
    # 短命引擎实例池（按 bookSourceUrl LRU）
    # ------------------------------------------------------------------ #
    async def _get_engine(self, source=None) -> LegadoReadingEngine:
        """取书源对应的引擎实例；没有则新建，超池则淘汰最久未用（LRU）。

        池化理由：LegadoReadingEngine 持有该源的 Cookie/JS 状态与 put/get 变量袋，
        换源与检测要按源把这些状态留住。引擎本身构造并不贵（实测 0.55ms），真正
        不能丢的是会话状态；TCP 连接池已经上移到 :meth:`_shared_connector`，按循环
        共享一条，所以淘汰一个冷门源不再连带丢掉别人的热连接。
        """
        if source is None:
            if self._default_engine is None:
                self._default_engine = self._new_engine()
            return self._default_engine

        key = _source_key(source)
        async with self._pool_lock:
            engine = self._pool.get(key)
            if engine is not None:
                self._pool.move_to_end(key)  # LRU：标记为最近使用
                return engine
            engine = self._new_engine()
            self._pool[key] = engine
            self._pool.move_to_end(key)
            # 超池淘汰最久未用（队首）。
            while len(self._pool) > self.pool_size:
                _, stale = self._pool.popitem(last=False)
                asyncio.ensure_future(self._release(stale))
            return engine

    def _shared_connector(self):
        """当前循环专属的共享 TCP 连接池（没有运行中的循环就返回 None）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        connector = self._shared_connectors.get(loop)
        if connector is None or connector.closed:
            # [对齐 OkHttp ConnectionPool] 全局上限沿用 aiohttp 默认（与引擎
            # 单独建池时一致），每主机 5 条与 ReaderEngine._session_for 同值。
            # resolver 也必须同值：dnsIp 钉表挂在 resolver 上，共享池少了它，
            # 走这条路的书源就会悄悄按系统 DNS 解析（钉表形同失效）。
            connector = aiohttp.TCPConnector(
                ssl=False, limit_per_host=5, resolver=dns_ip_resolver())
            self._shared_connectors[loop] = connector
        return connector

    def _new_engine(self) -> LegadoReadingEngine:
        engine = LegadoReadingEngine(
            timeout=self.timeout,
            webview_fetcher=self.webview_fetcher,
            verification_fetcher=self.verification_fetcher,
            state_dir=self.state_dir,
            connect_timeout=self.connect_timeout,
            shared_connector=self._shared_connector(),
        )
        # F7：视图装配期经 inject_interaction_loop 写入 _interaction_loop；
        # 池化引擎在装配后才懒建，这里继承 UI loop，让 webview/验证码回调
        # 派到 UI 线程（未注入时 interaction_loop=None，引擎降级到当前 loop）。
        ui_loop = getattr(self, "_interaction_loop", None)
        if ui_loop is not None:
            engine.interaction_loop = ui_loop
        return engine

    async def _release(self, engine: LegadoReadingEngine) -> None:
        try:
            await engine.aclose()
        except Exception as exc:  # pragma: no cover
            logger.warning("淘汰引擎实例失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 统一转发入口（壳层不改引擎行为）
    # ------------------------------------------------------------------ #
    async def search(self, source, keyword: str, page: int = 1) -> list:
        engine = await self._get_engine(source)
        return await engine.search(source, keyword, page)

    async def explore(self, source, category, page: int = 1) -> list:
        engine = await self._get_engine(source)
        return await engine.explore(source, category, page)

    async def explore_categories(self, source) -> list:
        """解析书源 exploreUrl 得到分类清单（静态与 @js: 动态都支持）。"""
        from app.services.engine.rule_engine import resolve_explore_categories
        engine = await self._get_engine(source)
        runtime = getattr(engine, "js", None)
        return await resolve_explore_categories(source, runtime)

    async def content_document(self, source, chapter, next_chapter_url: str = "",
                               need_save: bool = True):
        """正文管线（带替换规则/图片内联的结构化文档，非纯文本）。"""
        engine = await self._get_engine(source)
        return await engine.content_document(
            source, chapter, next_chapter_url, need_save=need_save)

    async def fetch_media(self, value: str, source, base_url: str = "",
                          extra_headers=None) -> bytes:
        """经引擎网络栈取单个二进制资源（漫画页/附件），语义同引擎原生。"""
        engine = await self._get_engine(source)
        return await engine.fetch_media(
            value, source, base_url=base_url, extra_headers=extra_headers)

    async def book_info(self, source, book):
        engine = await self._get_engine(source)
        return await engine.book_info(source, book)

    async def chapters(self, source, book):
        engine = await self._get_engine(source)
        return await engine.chapters(source, book)

    async def content(self, source, chapter) -> str:
        engine = await self._get_engine(source)
        return await engine.content(source, chapter)

    def extract_values(self, rule: str, sample: str, kind: str = "") -> list[str]:
        """把一段示例正文当作**被求值的节点**，按一条规则取值（同步纯函数）。

        规则编辑器「试跑当前规则」要的就是这个：没有书源、不发请求。这里刻意不走
        :func:`rule_engine.select_nodes`——那条是"先按列表规则选容器、再逐容器取
        字段"的搜索管线，试跑现场只有一条字段规则和一段正文，空列表规则选不出任何
        容器，于是求值对象是空列表，**任何规则都只会回"没取到值"**。
        """
        from app.services.engine import rule_engine
        kind = str(kind or "").lower()
        if kind != "json":
            kind = ("json" if str(sample or "").lstrip()[:1] in {"{", "["}
                    else "html")
        root = (rule_engine.parse_json_smart(sample) if kind == "json"
                else rule_engine.parse_markup(sample, "html"))
        return list(rule_engine.extract_values(root, rule, kind) or [])

    # ------------------------------------------------------------------ #
    # 书源登录（Legado loginUi / loginUrl）
    # ------------------------------------------------------------------ #
    def login_available(self, source) -> bool:
        """是否有登录入口：判据与 Legado 菜单一致（loginUrl 非空）。同步纯函数。"""
        return LegadoReadingEngine.login_available(source)

    def login_target(self, source) -> str:
        """纯网页登录时要打开的地址（解析 URL 型 loginUrl，含相对与 ``{url:…}``）。

        空串表示"不是网页登录"——界面据此决定铺表单还是给地址，
        别拿一个空表单去问用户要什么。同步纯函数，不碰引擎池。
        """
        return LegadoReadingEngine.login_target(source)

    async def login_schema(self, source) -> list[dict]:
        """取该源 loginUi 字段配置（含已存登录值），交界面铺输入框。"""
        engine = await self._get_engine(source)
        return await engine.login_schema(source)

    async def run_login_action(self, source, action: str = "",
                               values: dict | None = None):
        """执行 loginUi 里某个**按钮**的 action（不提交整张表单、不落库字段）。

        Legado 的 loginUi 项可以是 ``type:"button"`` + ``action``（扫码 / 取 Cookie /
        过验证码那类）。少了这个转发，这类源在桌面上就是"点了没反应"。
        """
        engine = await self._get_engine(source)
        return await engine.execute_login_action(source, action, values)

    async def submit_login(self, source, values: dict):
        """落库登录字段并执行源的 login()：Cookie 留在该源的池化引擎上。"""
        engine = await self._get_engine(source)
        return await engine.submit_login(source, values)

    # ------------------------------------------------------------------ #
    # 变量作用域统一入口（转发引擎变量袋）
    # ------------------------------------------------------------------ #
    async def put(self, source, key: str, value: str, book=None,
                  chapter=None) -> str:
        """写变量：转发到对应引擎的变量袋（_put_variable 语义）。

        异步：LegadoReadingEngine 构造时绑定 aiohttp CookieJar 到当前事件循环，
        故建引擎必须在运行中的 loop 内（与其他转发入口一致）。
        """
        engine = await self._get_engine(source)
        return engine._put_variable(str(key), value, book, chapter)

    async def get(self, source, key: str, default: Any = "", book=None,
                  chapter=None) -> str:
        """读变量：转发到对应引擎的变量袋（_get_variable 语义）。"""
        engine = self._peek_engine(source)
        if engine is None:
            return default
        value = engine._get_variable(str(key), book, chapter)
        return value if value != "" else default

    def _peek_engine(self, source):
        """只看不建：池里已有该源引擎则返回，否则 None。"""
        if source is None:
            return self._default_engine
        return self._pool.get(_source_key(source))
