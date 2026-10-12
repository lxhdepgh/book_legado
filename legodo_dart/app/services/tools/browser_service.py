"""BrowserService：浏览器层统一会话服务（M5，契约 §11 / 设计 §1.19 / §3.3）。

把三个既有浏览器模块收编为**统一会话管理 + 路由**服务（三模块逻辑零改动）：

- :mod:`app.services.headless_browser`（HeadlessBrowser 无人值守渲染）——本服务持有其
  进程级单例 ``BROWSER`` 的生命周期（on_stop 统一回收），profile 目录
  ``chromium-profile/`` 登录态由其自身管理，本服务只做启停与降级探测；
- :mod:`app.services.interactive_browser`（CDP 投屏交互窗口）——``open_interactive()``
  按需创建一个有头 Chromium 会话并返回交互句柄（tap/scroll/type_text/capture/close）；
- :mod:`app.services.web_page_renderer`（webView/验证码请求接入）——``render_webview()``
  懒加载构造（其顶部 import flet，故仅在视图层就绪、确需可见验证时才导入），
  ``ftw.WebView`` 平台判定分支原样保留，本服务不动。

对外契约面（§11）：

- ``async fetch(url, options=None, **opts)``：无头渲染，委托 HeadlessBrowser；
- ``async open_interactive(url, callbacks=None, on_frame=None, options=None)``：
  CDP 投屏，返回交互句柄；
- **应用内会话页面**（内置浏览器页用，见 :meth:`open_page`）：``open_page`` 开一个
  投屏会话并**只返回会话号**——内核句柄活在引擎循环上，视图层拿着它调协程就跑错
  线程，故后续 ``navigate_page`` / ``cast_gesture`` / ``cast_viewport`` /
  ``close_page`` 一律按会话号派发；``on_frame`` 回调也在引擎循环上被调用，视图层
  要自己编组回界面循环（见 controllers/tools/browser_controller）。
- ``async render_webview(url, source=None, options=None)``：webView/验证码请求，
  委托 WebPageRenderer（静默无头失败后才弹窗，逻辑在该模块内）；**没有界面时**走
  :meth:`_render_without_ui`（静默内核 → 推给远端 WebView 宿主），见 §8 R3；
- ``available_fetcher()``：降级探测——playwright/Chromium 内核缺失时返回 None，
  调用方据此退回非浏览器路径，**不报错**；
- 回调注册表：视图层实现 :class:`~app.core.interfaces.BrowserInteractionCallback`
  并经 ``register_interaction_callback()`` 注入；服务不持有任何 Flet 控件。

硬约束：本模块**零 flet import**；playwright 仅在三个被收编模块内部懒加载，未装时
``available_fetcher()`` 返回 None 优雅降级；跨平台纯 Python。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.lifecycle import Service
from app.services.legado_url import fix_url

logger = logging.getLogger(__name__)

# 交互回调注入面：视图层实现 BrowserInteractionCallback（core/interfaces.py）。
InteractionCallback = Any  # 运行期 duck-type，避免对 core 的硬 import 风险

# webview_fetcher 注入面：async (url, source, options) -> dict，
# 与 reading_engine.py 既有调用约定一致（§1.19 浏览器回退）。
WebviewFetcher = Callable[..., Awaitable[dict]]


class BrowserService(Service):
    """浏览器会话的统一生命周期持有者与路由入口。"""

    name = "BrowserService"
    # 依赖方向例外（§5.2）：本服务被 reading/RuleEngineService 依赖，自身只经
    # on_start/构造注入取协作者，不反向 import 阅读域，故拓扑依赖留空。
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self, storage=None, settings=None, engine=None,
                 headless: Any = None,
                 chromium_probe: Callable[[], bool] | None = None,
                 interactive_factory: Callable[..., Any] | None = None,
                 renderer_factory: Callable[..., Any] | None = None,
                 page_provider: Callable[[], Any] | None = None,
                 remote_webview: WebviewFetcher | None = None,
                 profile_root: Path | None = None) -> None:
        super().__init__()
        self._storage = storage
        self._settings = settings
        self._engine = engine
        # ── 可注入面（测试替身）：默认 None 时懒加载真实模块 ──
        self._headless_override = headless
        self._headless: Any = headless  # 真实单例首次用到时再解析
        self._probe = chromium_probe
        self._interactive_factory = interactive_factory
        self._renderer_factory = renderer_factory
        self._renderer: Any = None
        # 视图层注入：flet page 提供者（返回控件树里的 page），服务不 import flet。
        self._page_provider = page_provider
        # 无头装配（``main.py --serve``）注入：把一次渲染推给远端 WebView 宿主并等回话
        # （网关 :meth:`GatewayService.request_web_content`，形状同 WebviewFetcher）。
        self._remote_webview = remote_webview
        self._profile_root = profile_root
        # ── 回调注册表（BrowserInteractionCallback，视图层实现注入）──
        self._interaction_cb: InteractionCallback | None = None
        # ── 有头投屏会话登记（会话池/生命周期）──
        self._sessions: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._session_seq = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        # 浏览器内核按需懒启动：启动期不拉起 Chromium（首 fetch 才起线程/进程），
        # 仅做依赖补齐，保证无内核环境也能正常 RUNNING（available_fetcher 探测降级）。
        if self._storage is None and self.hub is not None:
            try:
                from app.core.storage import StorageService
                self._storage = self.hub.require(StorageService)
            except Exception:
                self._storage = None
        logger.info("BrowserService 已启动（会话按需懒建）")

    async def on_stop(self) -> None:
        # 统一回收：有头投屏会话逐个关闭，再关共享无头单例。
        await self._close_all_sessions()
        headless = self._resolve_headless()
        if headless is not None:
            try:
                close = getattr(headless, "aclose", None)
                if close is not None:
                    await close()
            except Exception:
                logger.exception("BrowserService 关闭无头浏览器异常")
        logger.info("BrowserService 已停止")

    # ------------------------------------------------------------------ #
    # 回调注册表（§4.6 唯一例外通道：BrowserInteractionCallback）
    # ------------------------------------------------------------------ #
    def register_interaction_callback(self, callback: InteractionCallback | None
                                      ) -> None:
        """注入视图层实现的 BrowserInteractionCallback（登录/验证码/投屏输入）。"""
        self._interaction_cb = callback

    @property
    def interaction_callback(self) -> InteractionCallback | None:
        return self._interaction_cb

    def set_page_provider(self, provider: Callable[[], Any] | None) -> None:
        """注入 flet page 提供者（视图层调用；服务侧不触碰 flet 类型）。"""
        self._page_provider = provider

    def set_remote_webview(self, fetcher: WebviewFetcher | None) -> None:
        """注入"把这次渲染推给远端 WebView 宿主"的可调用（无头装配调用）。

        这就是设计 §8 R1 那条竖切在产品里的落点：进程一分为二之后**可见 WebView 只在
        客户端那边**，引擎这一侧的 ``webview_fetcher`` 只能发出去等回来。装配根把
        :meth:`GatewayService.request_web_content` 递进来，形状与 ``WebviewFetcher``
        一致（``url, source, options``）。
        """
        self._remote_webview = fetcher

    # ------------------------------------------------------------------ #
    # 内核降级探测（§1.19：available_fetcher() 返回 None 则退回非浏览器路径）
    # ------------------------------------------------------------------ #
    def available_fetcher(self) -> WebviewFetcher | None:
        """返回 Legado 形状的 webview_fetcher（url, source, options）；内核不可用返回 None。

        不抛错：playwright 未安装 / Chromium 未下载时一律按"无浏览器"处理，
        让检测与阅读链路退回纯 HTTP 路径。
        """
        if not self._kernel_available():
            return None
        return self._webview_fetcher

    def _kernel_available(self) -> bool:
        if self._probe is not None:
            try:
                return bool(self._probe())
            except Exception:
                return False
        # 默认探测：懒查 chromium 是否已下载（不 import playwright、不拉起进程）。
        try:
            from app.services.headless_browser import chromium_installed
        except Exception:
            return False
        try:
            return bool(chromium_installed())
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 无头渲染（委托 headless_browser，逻辑零改动）
    # ------------------------------------------------------------------ #
    async def fetch(self, url: str, options: dict | None = None, **opts: Any
                    ) -> dict:
        """无头渲染一个 URL，返回浏览器响应映射（委托 HeadlessBrowser.fetch）。

        ``options`` 与 ``opts`` 合并后透传（webView/webJs/sourceRegex/headerMap 等）。
        内核不可用时由被委托模块抛 BrowserUnavailable；调用方应先看 available_fetcher()。
        """
        headless = self._require_headless()
        merged: dict = dict(options or {})
        merged.update({k: v for k, v in opts.items() if v is not None})
        return await headless.fetch(str(url or ""), merged)

    async def _webview_fetcher(self, url: str, source: Any = None,
                               options: dict | None = None) -> dict:
        """Legado 形状的 fetcher：与 reading_engine.webview_fetcher 约定一致。"""
        del source  # 与旧 headless_fetch 一致：source 仅作标题展示，渲染参数在 options
        return await self.fetch(url, options)

    # ------------------------------------------------------------------ #
    # CDP 投屏交互（委托 interactive_browser，逻辑零改动）
    # ------------------------------------------------------------------ #
    async def open_interactive(self, url: str,
                               callbacks: InteractionCallback | None = None,
                               on_frame: Callable[[bytes], None] | None = None,
                               options: dict | None = None) -> Any:
        """打开有头 Chromium 投屏会话，返回交互句柄（InteractiveBrowser）。

        ``on_frame`` 接收 JPEG 帧字节（视图层负责刷 Flet Image）；``callbacks``
        覆盖本次会话的 BrowserInteractionCallback（投屏输入经它回灌）。
        会话登记在服务会话池中，on_stop 统一回收。
        """
        if callbacks is not None:
            self._interaction_cb = callbacks
        handle = await self._new_session(on_frame=on_frame)
        try:
            await handle.start(fix_url(str(url or "")), options or {})
        except Exception:
            await self._close_session_handle(handle)
            raise
        sid = self._register(handle)
        logger.info("BrowserService 投屏会话已开: %s -> %s", sid, url)
        return handle

    # ------------------------------------------------------------------ #
    # 应用内会话页（内置浏览器页面：跨循环只传会话号，句柄留在服务池）
    # ------------------------------------------------------------------ #
    #: 视图可转发的投屏手势（白名单，服务侧不暴露内核对象）
    GESTURES: tuple[str, ...] = (
        "tap", "scroll", "press", "move_to", "release", "type_text")

    async def open_page(self, url: str, on_frame: Callable[[bytes], None] | None = None,
                        options: dict | None = None) -> str:
        """开一个投屏会话页并返回会话号；``on_frame`` 在**引擎循环**上被回调。

        视图层拿不到内核句柄（它活在引擎循环上），后续跳转/手势/关闭都按会话号
        走这里的协程方法——协程经 EngineProxy 跳回引擎循环执行。
        """
        handle = await self._new_session(on_frame=on_frame)
        try:
            await handle.start(fix_url(str(url or "")), dict(options or {}))
        except Exception:
            # 地址写错/站点拒连时内核已经起来了：不关掉就留一个僵尸 Chromium 窗口
            await self._close_session_handle(handle)
            raise
        sid = self._register(handle)
        logger.info("BrowserService 会话页已开: %s", sid)
        return sid

    async def navigate_page(self, sid: str, url: str) -> None:
        """同一会话跳到新地址（前进/后退/重载不重开会话，省一次内核启动）。"""
        await self._session(sid).goto(fix_url(str(url or "")))

    async def close_page(self, sid: str) -> None:
        """按会话号关闭并注销（视图 detach 时调用，别把有头内核留成孤儿）。"""
        with self._lock:
            handle = self._sessions.pop(sid, None)
        if handle is not None:
            await self._close_session_handle(handle)

    async def cast_gesture(self, sid: str, action: str, *args: Any) -> None:
        """把一次界面手势转给会话页（见 :attr:`GESTURES`）。"""
        if action not in self.GESTURES:
            raise ValueError(f"未知投屏手势：{action}")
        await getattr(self._session(sid), action)(*args)

    async def cast_viewport(self, sid: str) -> tuple[int, int]:
        """会话页在投屏里的尺寸，视图据此把点击坐标换算到网页。"""
        return self._session(sid).cast_viewport()

    async def _new_session(self, on_frame=None) -> Any:
        factory = self._interactive_factory
        if factory is None:
            # 懒加载：仅在真正需要投屏时才 import interactive_browser（连带 playwright）。
            from app.services.interactive_browser import InteractiveBrowser
            factory = lambda **kw: InteractiveBrowser(**kw)  # noqa: E731
        return factory(on_frame=on_frame)

    def _register(self, handle: Any) -> str:
        with self._lock:
            self._session_seq += 1
            sid = f"interactive-{self._session_seq}"
            self._sessions[sid] = handle
        return sid

    def _session(self, sid: str) -> Any:
        with self._lock:
            handle = self._sessions.get(sid)
        if handle is None:
            raise RuntimeError("内置浏览器会话已关闭，请重新访问")
        return handle

    @staticmethod
    async def _close_session_handle(handle: Any) -> None:
        close = getattr(handle, "close", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------ #
    # webView / 验证码请求接入（委托 web_page_renderer，逻辑零改动）
    # ------------------------------------------------------------------ #
    async def render_webview(self, url: str, source: Any = None,
                             options: dict | None = None) -> dict:
        """渲染 webView/验证码请求：先静默无头，命中拦截页再弹窗（逻辑在 renderer 内）。

        ``ftw.WebView`` 分支（移动/macOS）与 CDP 投屏分支均在 web_page_renderer 内按
        平台判定，本服务不动；本方法只负责以视图层注入的 page_provider 构造 renderer。
        没有 page_provider（无头装配）时走 :meth:`_render_without_ui`——本服务的
        启动与这条路径都**不碰 flet**（§8 R3）。
        """
        renderer = self._ensure_renderer()
        if renderer is None:
            return await self._render_without_ui(url, source, options or {})
        return await renderer.render(str(url or ""), source, options or {})

    async def _render_without_ui(self, url: str, source: Any,
                                 options: dict) -> dict:
        """没有界面时的 webView 渲染：静默内核 → 实在要人，才推给远端宿主。

        两步的先后与 :meth:`WebPageRenderer.render` 一致（那里也是先 ``_render_silently``
        再弹窗），所以"无头能答的都答了"这一条不需要新判据，用的是同一句
        :func:`~app.services.block_pages.looks_like_gate`。远端宿主答不了（没人连
        ``/ws``）时抛 :class:`WebviewUnavailable`，与 :meth:`available_fetcher`
        返回 None 同一条老口径：**不是渲染失败，是根本没这个人**，引擎据此退回纯 HTTP。
        """
        silent = await self._silent_render(url, options)
        if silent is not None:
            return silent
        if self._remote_webview is None:
            from app.services.gateway.service import WebviewUnavailable
            raise WebviewUnavailable(
                "没有 WebView 宿主可接这次渲染（无头装配既无界面、也未接网关反向调用）")
        return await self._remote_webview(url, source, options)

    async def _silent_render(self, url: str, options: dict) -> dict | None:
        """内置无头内核渲染；内核不在场、渲染失败、或回来的还是一堵墙 → None。"""
        if not self._kernel_available():
            return None
        try:
            rendered = await self.fetch(url, options)
        except Exception:
            logger.debug("静默浏览器渲染失败，交给 WebView 宿主：%s", url, exc_info=True)
            return None
        from app.services.block_pages import looks_like_gate
        if looks_like_gate(str(rendered.get("body") or "")):
            return None
        return rendered

    async def verification_fetcher(self, image: Any, source: Any = None) -> str:
        """验证码取页入口（verification_fetcher 形状：(image, source) -> str）。"""
        renderer = self._ensure_renderer()
        if renderer is None:
            # 验证码要的是**一个人**：无头进程没有输入面，也没有"自动答对"这条路。
            # 明说比静默返回空串好——空串会被 java.getVerificationCode() 当成答案，
            # 规则继续跑、登录永远失败，现场看不出来。
            raise RuntimeError("验证码需要人工输入：当前进程没有界面，"
                               "请在有窗口的端打开这本书")
        return await renderer.verification_code(image, source)

    # ------------------------------------------------------------------ #
    # 内部装配
    # ------------------------------------------------------------------ #
    def _resolve_headless(self) -> Any:
        """取共享无头浏览器单例；注入了替身则直接返回（测试不碰真实内核）。"""
        if self._headless is not None:
            return self._headless
        if self._headless_override is not None:
            self._headless = self._headless_override
            return self._headless
        try:
            from app.services.headless_browser import BROWSER
        except Exception:
            return None
        self._headless = BROWSER
        return BROWSER

    def _require_headless(self) -> Any:
        headless = self._resolve_headless()
        if headless is None:
            # 未注入且 headless_browser 模块不可用：抛与旧路径一致的语义。
            from app.services.headless_browser import BrowserUnavailable
            raise BrowserUnavailable("内置无头浏览器不可用（playwright/Chromium 缺失）")
        return headless

    def _ensure_renderer(self) -> Any:
        """懒构造界面侧 renderer；**没有界面时返回 None**（降级见 :meth:`_render_without_ui`）。

        ``web_page_renderer`` 顶部 import flet，所以只在真要有窗口的时候才碰它——
        这条判据顺带就是 §8 R3 要的"无 UI 也能启动"：无头装配一行 flet 都不 import。
        """
        if self._renderer is not None:
            return self._renderer
        if self._renderer_factory is not None:
            self._renderer = self._renderer_factory(self._page_provider, None)
            return self._renderer
        if self._page_provider is None:
            return None
        # 懒加载 web_page_renderer（连带 flet / ftw 平台分支，平台判定分支不动）。
        from app.services.web_page_renderer import WebPageRenderer
        self._renderer = WebPageRenderer(self._page_provider)
        return self._renderer

    async def _close_all_sessions(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for handle in sessions:
            try:
                await self._close_session_handle(handle)
            except Exception:
                logger.exception("BrowserService 关闭投屏会话异常")

    # ------------------------------------------------------------------ #
    @property
    def session_count(self) -> int:
        """当前存活的有头投屏会话数（会话池可观测）。"""
        return len(self._sessions)
