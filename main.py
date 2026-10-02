#!/usr/bin/env python3
"""Legado 桌面端 M6 入口（进程内多服务架构 + ViewRegistry 全量导航切换）。

启动方式：
    python main.py               # 正常启动：ServiceHub 装配 → 启动 → 分组导航外壳
    python main.py --smoke-test  # 冻结包动态依赖冒烟（沿用旧 main.py）
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（沿用旧 main.py 方式）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
    hub.register(RuleEngineService(
        webview_fetcher=webview, verification_fetcher=verification))
    hub.register(BookCacheService())
    hub.register(DownloadService())
    hub.register(BookmarkService())
    hub.register(ContentSearchService())
    hub.register(ImageBookService())
    hub.register(ReadingService())

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

    # M4 服务集合：内容形态域四服务（§8.2 M4）。新依赖（feedparser/ebooklib/
    # pymupdf/pyttsx3/edge-tts）均按服务懒加载，仅 requirements 记录。
    from app.services.content.local_book_service import LocalBookService
    from app.services.content.media_play import MediaPlayService
    from app.services.content.read_aloud import ReadAloudService
    from app.services.content.rss_service import RssService

    hub.register(RssService())
    hub.register(LocalBookService())
    hub.register(ReadAloudService())
    hub.register(MediaPlayService())

    # M5 服务集合：工具域十服务（§8.2 M5，§1.5/1.7/1.8/1.9/1.10/1.12/1.15/1.17/1.19 + §3.3/3.11）。
    # BrowserService 懒拉起 Chromium、WebBridgeService 默认关闭监听（SUSPENDED 语义）。
    from app.services.tools.book_export_service import BookExportService
    from app.services.tools.dict_service import DictService
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
    hub.register(DictService())
    hub.register(FontService())
    hub.register(QrCodeService())
    # 本机版本只有 app.APP_VERSION 一处：不传就用默认空串，更新页既显示"未知"、
    # 又拿不出比较基准（闸门任何远端版本都放行）。
    from app import APP_VERSION
    hub.register(UpdateService(current_version=APP_VERSION))
    hub.register(WebBridgeService())
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


def main():
    if "--smoke-test" in sys.argv:
        smoke_test()
        if getattr(sys, "frozen", False):
            os._exit(0)
        return

    import flet as ft

    # 视图在函数里才 import（不是模块顶层）：审计跑在子进程里，Windows 的 spawn
    # 会在每个子进程里把 `__main__`（也就是本文件）重新 import 一遍，顶层那行
    # flet + 视图树会让每个 worker 白背 ~126 ms 的导入和几十 MB 常驻。
    from app.views.navigation import build_app_shell

    # resource_dir：M3 起供浏览器/主题等资源查找使用，M1 不消费，保留通道。
    resource_dir = _ROOT / "resource"  # noqa: F841

    hub = _build_hub()
    asyncio.run(hub.start_all())
    try:
        def _on_page(page):
            _apply_saved_theme(page, hub)
            _wire_browser_page(hub, page)
            build_app_shell(page, hub, shelf_probe=lambda: _shelf_has_books(hub))

        ft.run(_on_page, view=ft.AppView.FLET_APP)
    finally:
        hub.shutdown()  # 主线程等 stop_all 收尾，随后关掉 EngineHub 守护线程


if __name__ == "__main__":
    # 冻结包里 spawn 出来的子进程会**重新执行本 exe**，没有这一句就是"exe 套 exe"
    # 无限繁殖（审计进程池一建就炸）。源码跑法下它是空操作。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
