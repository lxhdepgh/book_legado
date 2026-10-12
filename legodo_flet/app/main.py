#!/usr/bin/env python3
"""Legado Flet 前端入口（双轨架构：可独立运行或连接外部后端）。

启动方式：
    python main.py                           # 独立模式：本地 ServiceHub + Flet UI
    python main.py --serve                   # 无头模式：仅 GatewayService（供远程调用）
    python main.py --connect URL --token T   # 环回模式：连接外部 Python 后端
    python main.py --smoke-test              # 冻结包动态依赖冒烟
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# 确保 legodo_flet 根目录在 sys.path 中（app/ 包含完整后端 + 前端）
# legodo_flet/app/main.py → legodo_flet 根 = ../ 
_FLET_ROOT = Path(__file__).resolve().parent.parent
if str(_FLET_ROOT) not in sys.path:
    sys.path.insert(0, str(_FLET_ROOT))

# 同时保留上级项目根（供跨子项目导入，如果需要）
_PROJECT_ROOT = _FLET_ROOT.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(1, str(_PROJECT_ROOT))

_ROOT = _FLET_ROOT

logger = logging.getLogger(__name__)


def smoke_test():
    """冻结包动态导入冒烟（适配新引擎路径，逻辑与旧 main.py 一致）。"""
    import io
    import json
    from types import SimpleNamespace

    from Crypto.Cipher import AES  # noqa: F401
    from PIL import Image
    from fontTools.ttLib import TTFont  # noqa: F401
    from opencc import OpenCC
    import elementpath  # noqa: F401
    import jsonpath_ng.ext  # noqa: F401
    import playwright  # noqa: F401
    import quickjs  # noqa: F401

    # 引擎核心资产新位置：app/services/engine/
    from app.services.engine.legado_js import JsContext, LegadoJsRuntime
    from app.services.engine.public_suffix import _DATA

    assert _DATA.is_file()

    source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://smoke"})
    runtime = LegadoJsRuntime()
    try:
        assert runtime.evaluate(
            "@js:java.md5Encode('ok').length", JsContext(source)) == 32
        assert OpenCC("t2s").convert("閱讀") == "阅读"
        image = Image.new("RGB", (2, 2), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        assert runtime._bitmap_info(runtime._bitmap_decode(
            json.dumps(list(output.getvalue())))) == "[2, 2]"
    finally:
        runtime.close()

    _audit_pool_selftest()


def _audit_pool_selftest():
    """审计进程池的现场自检：spawn 在这个包里到底走不走得通。

    静态审计跑在子进程里（``compat_audit_service.audit_off_loop``）。冻结包缺
    ``freeze_support``、或杀软拦 spawn 时，它会**静默**退回线程池——功能不坏，
    只是那 3.4× 的加速没了，而现场看不出区别。所以冒烟里显式量一次并断言走的
    是子进程；退回原因（``_STATE.reason``）一并打出来，别让人对着一个"通过"猜。
    """
    import time

    from app.services.sources import compat_audit_service as cas

    payload = {"bookSourceUrl": "https://smoke", "ruleSearch": {"bookList": "ul li"}}
    t0 = time.perf_counter()
    report = asyncio.run(cas.audit_off_loop(payload))
    built_ms = (time.perf_counter() - t0) * 1000
    state = cas._STATE  # noqa: SLF001  自检要读的就是这份记账
    hits, fallbacks, reason = state.subprocess_hits, state.thread_fallbacks, state.reason
    cas.shutdown_audit_pool()
    assert report.source_id == "https://smoke"
    print(f"审计进程池：建池 + 首条 {built_ms:.0f} ms | 子进程 {hits} 条"
          f" / 退回线程 {fallbacks} 条" + (f"（{reason}）" if reason else ""))
    assert hits == 1 and not fallbacks, (
        f"静态审计没能跑进子进程，退回线程池了：{reason or '未知原因'}")


def _engine_browser_fetchers(browser):
    """引擎要的两个 fetcher：内核在场才给（§1.19 浏览器回退）。

    绑的都是**同一个** BrowserService 实例——内置浏览器页和 webView 弹窗共用它的
    ``page_provider`` 与 cookie 罐，所以注册进 ServiceHub 的必须也是这一份
    （见本模块 M2/M5 段）。内核缺失（playwright 未装 / Chromium 未下载）时返回
    ``(None, None)``：引擎据此退回纯 HTTP 路径，不弹窗也不报错。
    """
    if browser.available_fetcher() is None:
        return None, None
    return browser.render_webview, browser.verification_fetcher


def _build_hub():
    """装配 M1 服务集合：4 个基础设施服务 + RuleEngineService（§8.2 M1）。

    注意：ServiceHub 自身不注册（它是总控制器），但 status_report 会带它一行，
    仪表盘因此展示 5 个基础设施 + RuleEngineService 共 6 行。
    """
    # 【关键修复】在任何服务初始化之前，先导入 action 表模块触发装饰器注册 BY_ACTION
    import app.gateway.methods  # noqa: F401  导入即注册
    import logging as _log
    _log.getLogger(__name__).info("BY_ACTION 已加载 %d 个 action", len(app.gateway.methods.BY_ACTION))
    
    from app.core.bus import EventBus
    from app.core.engine_worker import engine_hub
    from app.core.hub import ServiceHub
    from app.core.settings import SettingsService
    from app.core.storage import StorageService

    engine = engine_hub()          # EngineHub（§0.4 基础设施之一）
    bus = EventBus(engine=engine)  # EventBus
    storage = StorageService()     # StorageService
    settings = SettingsService(storage=storage, engine=engine)  # SettingsService

    hub = ServiceHub(bus=bus, engine=engine)
    hub.register(bus)
    hub.register(engine)
    hub.register(storage)
    hub.register(settings)

    # M2 服务集合：RuleEngineService（§8.2 M1）+ 阅读域六服务（§8.2 M2）。
    # 全部无参可构造，协作者在 on_start 经 hub.require 解析；
    # 启停顺序由各服务声明的 dependencies 拓扑决定（Kahn 拓扑序）。
    from app.services.engine.rule_engine_service import RuleEngineService
    from app.services.reading.book_cache_service import BookCacheService
    from app.services.reading.bookmark_service import BookmarkService
    from app.services.reading.content_search import ContentSearchService
    from app.services.reading.download_service import DownloadService
    from app.services.reading.image_book_service import ImageBookService
    from app.services.reading.reading_service import ReadingService
    from app.services.tools.browser_service import BrowserService

    # §1.19 内置浏览器接进引擎：webView / @webjs / 403 回退这些分支要的就是这两个
    # fetcher。内核不在场时一律留 None——引擎据此退回纯 HTTP 路径（见
    # :func:`_engine_browser_fetchers`）。
    browser = BrowserService()
    webview, verification = _engine_browser_fetchers(browser)
    
    # 静态预渲染：创建 RulePreExecutor 作为工具类（不注册为 Service）
    from app.services.engine.rule_pre_executor import RulePreExecutor
    pre_executor = RulePreExecutor(
        browser_service=browser,
        storage=None,  # 延迟注入，on_start 时从 hub 获取
        engine=None
    )
    
    hub.register(RuleEngineService(
        webview_fetcher=webview,
        verification_fetcher=verification,
        android_mode=False,      # 桌面端保持标准模式
        pre_executor=pre_executor  # 启用预渲染（合并渲染与提取）
    ))
    hub.register(BookCacheService())
    hub.register(DownloadService())
    hub.register(BookmarkService())
    hub.register(ContentSearchService())
    hub.register(ImageBookService())
    hub.register(ReadingService())  # 不再需要 pre_executor 参数

    # M3 服务集合：书源域五服务（§8.2 M3）。SourceService 外壳化后，
    # ReadingService 的 on_start 经 hub.require(SourceService) 解析（集成点）。
    from app.services.sources.compat_audit_service import CompatAuditService
    from app.services.sources.online_import_service import OnlineImportService
    from app.services.sources.rule_repair_service import RuleRepairService
    from app.services.sources.source_check_service import SourceCheckService
    from app.services.sources.source_service import SourceService

    hub.register(SourceService())
    hub.register(SourceCheckService())
    hub.register(RuleRepairService())
    hub.register(CompatAuditService())
    hub.register(OnlineImportService())

    # M4 服务集合：内容形态域三服务（§8.2 M4）。新依赖（feedparser/ebooklib/
    # pymupdf）均按服务懒加载，仅 requirements 记录。
    from app.services.content.local_book_service import LocalBookService
    from app.services.content.media_play import MediaPlayService
    from app.services.content.rss_service import RssService

    hub.register(RssService())
    hub.register(LocalBookService())
    hub.register(MediaPlayService())

    # M5 服务集合：工具域十服务（§8.2 M5，§1.5/1.7/1.8/1.9/1.10/1.12/1.15/1.17/1.19 + §3.3/3.11）。
    # BrowserService 懒拉起 Chromium、WebBridgeService 默认关闭监听（SUSPENDED 语义）。
    from app.services.tools.background_service import BackgroundService
    from app.services.tools.book_export_service import BookExportService
    # [已移除] from app.services.tools.boss_key_service import BossKeyService  # G4-E: Android 裁剪
    # [已移除] from app.services.tools.dict_service import DictService  # 任务#45: 删除词典服务
    from app.services.tools.font_service import FontService
    from app.services.tools.qr_code_service import QrCodeService
    from app.services.tools.replace_rule_service import ReplaceRuleService
    from app.services.tools.theme_service import ThemeService
    from app.services.tools.update_service import UpdateService
    from app.services.tools.web_dav_service import WebDavService
    from app.services.tools.web_bridge_service import WebBridgeService

    hub.register(WebDavService())
    hub.register(BookExportService())
    hub.register(ReplaceRuleService())
    # [已移除] hub.register(DictService())
    hub.register(FontService())
    hub.register(QrCodeService())
    # 本机版本只有 app.APP_VERSION 一处：不传就用默认空串，更新页既显示"未知"、
    # 又拿不出比较基准（闸门任何远端版本都放行）。
    from app import APP_VERSION
    hub.register(UpdateService(current_version=APP_VERSION))
    hub.register(WebBridgeService())
    # [已移除] 老板键（G4-E Android 裁剪）：BossKeyService 依赖的快捷键常量已删除
    # hub.register(BossKeyService())
    # 全应用一层背景图（第七十四轮）：只管文件与偏好，那一层画在导航外壳上。
    hub.register(BackgroundService())
    # 同一个实例：引擎的 webview/验证码 fetcher 绑的就是它（上面 M2 段），
    # 再 new 一个会让 page_provider 注到另一份上、弹窗那条链又断掉。
    hub.register(browser)
    hub.register(ThemeService())

    return hub


def _apply_saved_theme(page, hub):
    """启动即把**已保存**的外观应用到窗口。

    主题三件套（theme / dark_theme / theme_mode）此前只在外观页预览时设过：
    重启后皮肤全丢，不进外观页就永远是 Flet 默认灰主题。装配根在这里把服务
    数据交给视图层的 ``apply_page_theme``（视图不 import 服务，红线 §7.5）。
    """
    from app.services.tools.theme_service import ThemeService
    from app.views.settings.appearance_view import apply_page_theme

    try:
        service = hub.get(ThemeService)
    except Exception:
        return
    if service is None:
        return
    try:
        apply_page_theme(page, service.apply())
    except Exception:
        logger.exception("启动应用已保存外观失败")


def _shelf_has_books(hub):
    """书架上到底有没有书——导航「阅读」入口的落地判定（同步、只读缓存）。

    SourceService 启动时已把书架载进内存（``on_start → load()``），这里读那份
    缓存的 ``shelved_books()``（排除 notShelf 试读暂存）就够判定用，不为一个
    bool 再去跑一遍协程。导航外壳在视图层，不许 import 服务（红线 §7.5），
    所以判定在装配根做好按参数注入。
    """
    from app.services.sources.source_service import SourceService

    try:
        service = hub.get(SourceService)
    except Exception:
        return False
    if service is None:
        return False
    try:
        return bool(service.shelved_books())
    except Exception:
        logger.exception("书架探测失败，阅读入口按默认落地找书")
        return False


def _wire_browser_page(hub, page) -> None:
    """把 flet page 交给 BrowserService，网页验证/投屏才有地方弹窗。

    ``WebPageRenderer`` 顶部 import flet（服务层红线：零 flet import），所以
    BrowserService 只收一个"返回 page"的可调用；注入点因此在装配根。没注入时
    ``render_webview`` 直接抛 ``RuntimeError``（见 ``_ensure_renderer``），
    表现就是"webView 规则的书一打开就报错、内置浏览器点了没反应"。
    """
    from app.services.tools.browser_service import BrowserService

    try:
        service = hub.require(BrowserService)
    except Exception:
        return
    try:
        service.set_page_provider(lambda: page)
    except Exception:
        logger.exception("内置浏览器 page 注入失败")


def _wire_boss_key(hub, page):
    """[已移除] 老板键服务接线（G4-E Android 裁剪）。

    BossKeyService 依赖的快捷键常量已在主项目中删除，此处直接返回 None。
    """
    return None


def _background_path(hub) -> str:
    """当前该垫在哪一层的背景图路径（没有就空串）。"""
    from app.services.tools.background_service import BackgroundService

    try:
        service = hub.get(BackgroundService)
    except Exception:
        return ""
    if service is None:
        return ""
    try:
        return str(service.current or "")
    except Exception:
        logger.exception("读背景图偏好失败")
        return ""


def _wire_background(hub) -> str:
    """首屏该垫哪张图，并把"用户换了图"这条线接到导航外壳上。

    视图层拿不到 :class:`BackgroundService`（红线 §7.5），所以订阅装在装配根：
    换图的是设置页的控制器，画那一层的是导航外壳，两者互相不认识，中间这一跳
    只能由把两边都捏在手里的地方来搭。

    键订阅的处理器跑在 EngineHub 守护 loop 上，而 :meth:`set_background` 要改控件，
    所以经 :func:`~app.core.engine_worker.call_on_ui` 编组回界面循环（§5.2 同一条契约，
    老板键的托盘回调走的是同一个口子）。

    返回值是首屏路径：**外壳创建时**就要拿到它，晚一步就得整棵根树重建一次。
    """
    from app.core.engine_worker import call_on_ui
    from app.core.settings import SettingsService
    from app.services.tools.background_service import BACKGROUND_IMAGE_KEY
    from app.views.navigation import current_host

    initial = _background_path(hub)
    try:
        settings = hub.require(SettingsService)
    except Exception:
        logger.exception("SettingsService 没起来，换背景图要重开程序才生效")
        return initial
    if settings is None:
        return initial

    def apply() -> None:
        host = current_host()
        if host is None:          # 外壳还没装配（首屏那一下）：初值已经带上，不用补
            return
        host.set_background(_background_path(hub))

    async def on_change(_event) -> None:
        call_on_ui(apply)

    try:
        settings.subscribe([BACKGROUND_IMAGE_KEY], on_change)
    except Exception:
        logger.exception("背景图偏好订阅失败")
    return initial


def _install_sigint(stop_event) -> None:
    """Ctrl-C / 控制台关闭 → 置收摊标志（**不**直接 os._exit）。

    默认 SIGINT 处理器会在主线程抛 KeyboardInterrupt，而主线程正 blocked 在
    ``stop_event.wait()`` 上：抛穿的话 ``hub.shutdown()`` 那段收尾就没机会跑，
    表现是"Ctrl-C 之后 QuickJS/COM 的收尾全没做"。换成置标志走同一条正常出口。
    """
    import signal

    def _handler(signum, _frame):
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _handler)
        except (ValueError, OSError, RuntimeError):
            # 非主线程 / 平台不支持：少一个信号不影响主路径（engine.shutdown 还在）。
            logger.debug("忽略信号 %s 的接管失败", name, exc_info=True)


def _wire_headless_webview(hub, gateway) -> None:
    """把引擎的 WebView 渲染口子接到网关的反向调用上（设计 §8 R1/R3）。

    桌面端那一条是 :func:`_wire_browser_page`（把 flet page 交给 BrowserService，
    验证弹窗才有地方开）；无头进程没有窗口，"可见 WebView"只剩客户端那一边，所以
    这里交的是"发出去等回来"的那只手。两条线不共存：有窗口就弹窗，没窗口就推宿主。

    注的是**未启动也行**的同步 setter（``hub.require`` 按类定位，注册即可取），
    所以这一跳排在 ``start_all`` 之前之后都安全——排在前面是为了让"装配完整"这件事
    在握手行打出去之前就成立。
    """
    from app.services.tools.browser_service import BrowserService

    hub.require(BrowserService).set_remote_webview(gateway.request_web_content)


def serve(argv: list[str] | None = None) -> int:
    """``--serve`` 无头入口（设计 §5.5）：装配照旧，**不碰 flet**。

    与桌面入口的差别只有三处：

    - 多注册一个 :class:`GatewayService`（``_build_hub()`` 本身一行不动）；
    - 把 WebView 那条口子接到网关的反向调用上，而不是接到窗口上
      （:func:`_wire_headless_webview` 对 :func:`_wire_browser_page`，§8 R1/R3）；
    - 收尾不是 ``ft.run`` 返回，而是等一个"收摊信号"——客户端主动
      ``engine.shutdown``、最后一个 WS 会话断开后空闲超时、或 Ctrl-C。

    全部服务协程与网关本体都在 **EngineHub 守护 loop** 上（``start_all`` 是 submit
    上去的），所以这里没有第二条循环、也没有任何回调要编组回界面。
    """
    import threading

    from app.core.engine_worker import engine_hub
    from app.services.gateway.service import GatewayService

    # 无头进程没有控制台之外的任何东西：日志得有去处，否则"服务启动失败"这类
    # 只存在于 logger.exception 里的事实，父进程和人都看不见。
    logging.basicConfig(
        level=os.environ.get("LEGADO_BOOKSOURCE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr)

    argv = sys.argv[1:] if argv is None else list(argv)

    def _number(name: str, default: float) -> float:
        """读 ``--port 8765`` / ``--idle-seconds=5`` 两种写法；没给就用默认。"""
        value = None
        for index, item in enumerate(argv):
            if item == name and index + 1 < len(argv):
                value = argv[index + 1]
                break
            if item.startswith(f"{name}="):
                value = item.split("=", 1)[1]
                break
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            raise SystemExit(f"{name} 要一个数字，收到 {value!r}") from None

    idle = _number("--idle-seconds", 15.0)
    port = int(_number("--port", 0))

    stop_event = threading.Event()
    hub = _build_hub()
    gateway = GatewayService(port=port, idle_exit_seconds=idle, exit_event=stop_event)
    hub.register(gateway)
    
    # 【调试】确认 gateway.hub 被正确设置
    import logging as _serve_log
    _serve_log.getLogger(__name__).info("GatewayService.hub = %s", gateway.hub)
    
    _wire_headless_webview(hub, gateway)

    engine = engine_hub()
    try:
        # 装配与运行期同一条 loop：start_all 提交到守护 loop 上跑，网关的 socket
        # 因此绑在那条 loop 上（绑错 loop 是 aiohttp 最阴的一类故障：能连、不动）。
        engine.submit(hub.start_all()).result(timeout=180)
    except Exception:
        logger.exception("无头装配失败")
        hub.shutdown()
        return 1

    # 握手行必须在 start_all 之后打：端口只有真绑上才知道（port=0 由系统分配）。
    print(gateway.handshake_line(), flush=True)
    _install_sigint(stop_event)
    try:
        stop_event.wait()
    finally:
        hub.shutdown()
    return 0


def main():
    if "--smoke-test" in sys.argv:
        smoke_test()
        if getattr(sys, "frozen", False):
            os._exit(0)
        return

    if "--serve" in sys.argv:
        # 无头进程：这一条路上方一行都不许出现 flet（护栏 tests/test_gateway_serve.py）。
        sys.exit(serve())

    # ========== 双轨架构支持 ==========
    # 检查是否指定连接外部后端
    connect_url = None
    connect_token = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--connect" and i < len(sys.argv) - 1:
            connect_url = sys.argv[i + 1]
        elif arg == "--token" and i < len(sys.argv) - 1:
            connect_token = sys.argv[i + 1]

    import flet as ft

    # 视图在函数里才 import（不是模块顶层）：审计跑在子进程里，Windows 的 spawn
    # 会在每个子进程里把 `__main__`（也就是本文件）重新 import 一遍，顶层那行
    # flet + 视图树会让每个 worker 白背 ~126 ms 的导入和几十 MB 常驻。
    from app.views.navigation import build_app_shell

    # resource_dir：M3 起供浏览器/主题等资源查找使用，M1 不消费，保留通道。
    resource_dir = _ROOT / "resource"  # noqa: F841

    if connect_url:
        # 环回模式：不启动本地 ServiceHub，直接连外部后端
        logger.info("Connecting to external backend: %s", connect_url)
        hub = None  # 标记为远程模式
    else:
        # 独立模式：启动本地 ServiceHub
        hub = _build_hub()
        asyncio.run(hub.start_all())

    try:
        def _on_page(page):
            if hub is not None:
                _apply_saved_theme(page, hub)
                _wire_browser_page(hub, page)
                build_app_shell(page, hub, shelf_probe=lambda: _shelf_has_books(hub),
                                key_filter=_wire_boss_key(hub, page),
                                background=_wire_background(hub))
            else:
                # 远程模式：简化版导航（跳过服务依赖）
                from app.views.navigation import build_remote_app_shell
                build_remote_app_shell(page, backend_url=connect_url, token=connect_token)

        ft.run(_on_page, view=ft.AppView.FLET_APP)
    finally:
        if hub is not None:
            hub.shutdown()  # 主线程等 stop_all 收尾，随后关掉 EngineHub 守护线程


if __name__ == "__main__":
    # 冻结包里 spawn 出来的子进程会**重新执行本 exe**，没有这一句就是"exe 套 exe"
    # 无限繁殖（审计进程池一建就炸）。源码跑法下它是空操作。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
