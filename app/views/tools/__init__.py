"""M5 工具域 UI 拆分：视图按域分包 + 每视图一个 Controller（§7.2 / §7.4 / §7.5）。

本包（``app/views/tools/``）只承载 Flet 布局与渲染，**禁止 import 任何
``app.services.*`` 具体模块**（import-lint 守门，§7.5，测试 AST 扫描）。
视图数据一律经所属 Controller 从 ``ServiceHub.get()`` 取服务后注入；
唯一例外通道是 BrowserView 实现 BrowserInteractionCallback（core 协议，不违反依赖方向）。

工具组十个目的地（注册进 ViewRegistry）：

- :class:`BrowserView`        通用浏览器：地址栏 / 前进后退 / 加载态；
- :class:`DictPopup`          划词取词弹窗；
- :class:`ReplaceRuleView`    全局替换规则管理；
- :class:`FontView`           字体管理（系统枚举 / 用户字体 / 选中）；
- :class:`QrCodeView`         二维码（生成 / 识别 / 手动输入）；
- :class:`ExportView`         书籍导出（epub / txt）；
- :class:`WebDavView`         备份设置页（WebDAV 连接 / 备份 / 恢复）；
- :class:`UpdateView`         更新检查（检查 / 提示 / 下载）；
- :class:`WebBridgeView`      网络桥设置页（开关 / 端口 / 令牌 / 端点清单）。
- :class:`TtsProfileView`     听书语音档（tts_profiles 增删 + 默认档）。
"""
from __future__ import annotations

from app.core.hub import ServiceHub
from app.core.registry import (DEST_TOOLS_BROWSER, DEST_TOOLS_DICT,
                               DEST_TOOLS_EXPORT, DEST_TOOLS_FONT,
                               DEST_TOOLS_OVERVIEW, DEST_TOOLS_QR,
                               DEST_TOOLS_REPLACE, DEST_TOOLS_TTS,
                               DEST_TOOLS_UPDATE,
                               DEST_TOOLS_WEBDAV, DEST_TOOLS_WEB_BRIDGE,
                               TOOLS_DESTINATIONS, ViewRegistry)

from app.views.tools.browser_view import BrowserView
from app.views.tools.dict_popup import DictPopup
from app.views.tools.replace_rule_view import ReplaceRuleView
from app.views.tools.font_view import FontView
from app.views.tools.qr_code_view import QrCodeView
from app.views.tools.export_view import ExportView
from app.views.tools.webdav_view import WebDavView
from app.views.tools.update_view import UpdateView
from app.views.tools.web_bridge_view import WebBridgeView
from app.views.tools.tts_view import TtsProfileView

from app.controllers.tools.browser_controller import BrowserController
from app.controllers.tools.dict_popup_controller import DictPopupController
from app.controllers.tools.replace_rule_controller import ReplaceRuleController
from app.controllers.tools.font_controller import FontController
from app.controllers.tools.qr_code_controller import QrCodeController
from app.controllers.tools.export_controller import ExportController
from app.controllers.tools.webdav_controller import WebDavController
from app.controllers.tools.update_controller import UpdateController
from app.controllers.tools.web_bridge_controller import WebBridgeController
from app.controllers.tools.tts_controller import TtsProfileController

__all__ = [
    "BrowserView", "DictPopup", "ReplaceRuleView", "FontView", "QrCodeView",
    "ExportView", "WebDavView", "UpdateView", "WebBridgeView", "TtsProfileView",
    "BrowserController", "DictPopupController", "ReplaceRuleController",
    "FontController", "QrCodeController", "ExportController",
    "WebDavController", "UpdateController", "WebBridgeController",
    "TtsProfileController",
    "register_destinations", "TOOLS_DESTINATIONS",
]


def register_destinations(registry: ViewRegistry, hub: ServiceHub) -> ViewRegistry:
    """把工具组目的地绑进路由表（§7.3：工厂由装配处绑定）。

    工厂闭包在实例化视图的同时 new 对应 Controller 并 attach(view)，返回挂载好的视图。
    所需服务集按 §9.2 声明，供导航层做就绪性检查。

    导航精简（M7 后）：侧栏只展示「更多工具」（DEST_TOOLS_OVERVIEW）入口页，
    八个工具子页注册为**隐藏目的地**（``hidden=True``，注册可达但不在导航栏
    显示），由 overview 页内卡片经 ``on_open_destination`` 跳转。
    """
    # 更多工具入口页（工具组唯一展示目的地）
    registry.register(
        DEST_TOOLS_OVERVIEW,
        lambda: _build_overview(),
        required_services=())
    # 八个工具子页：隐藏目的地（可达，不展示）
    registry.register(
        DEST_TOOLS_BROWSER, lambda: _build(hub, BrowserView, BrowserController),
        required_services=("BrowserService",), hidden=True)
    # [已移除] 词典功能在任务 #45 中被删除
    # registry.register(
    #     DEST_TOOLS_DICT, lambda: _build(hub, DictPopup, DictPopupController),
    #     required_services=("DictService",), hidden=True)
    registry.register(
        DEST_TOOLS_REPLACE,
        lambda: _build(hub, ReplaceRuleView, ReplaceRuleController),
        required_services=("ReplaceRuleService",), hidden=True)
    registry.register(
        DEST_TOOLS_FONT, lambda: _build(hub, FontView, FontController),
        required_services=("FontService",), hidden=True)
    registry.register(
        DEST_TOOLS_QR, lambda: _build(hub, QrCodeView, QrCodeController),
        required_services=("QrCodeService",), hidden=True)
    registry.register(
        DEST_TOOLS_EXPORT,
        lambda: _build(hub, ExportView, ExportController),
        required_services=("BookExportService",), hidden=True)
    registry.register(
        DEST_TOOLS_WEBDAV, lambda: _build(hub, WebDavView, WebDavController),
        required_services=("WebDavService",), hidden=True)
    registry.register(
        DEST_TOOLS_UPDATE, lambda: _build(hub, UpdateView, UpdateController),
        required_services=("UpdateService",), hidden=True)
    # 网络桥设置页：同样是隐藏目的地，但它不在 TOOLS_DESTINATIONS 冻结元组里
    # （那条元组被 M5 测试按长度 8 回查），只多挂一条路由。
    registry.register(
        DEST_TOOLS_WEB_BRIDGE,
        lambda: _build(hub, WebBridgeView, WebBridgeController),
        required_services=("WebBridgeService",), hidden=True)
    # 听书语音档页：tts_profiles 的 CRUD 面（朗读条的音色下拉吃这张表）
    registry.register(
        DEST_TOOLS_TTS,
        lambda: _build(hub, TtsProfileView, TtsProfileController),
        required_services=("ReadAloudService",), hidden=True)
    return registry


def _build_overview():
    from app.views.tools.overview_view import ToolsOverviewView
    return ToolsOverviewView()


def _build(hub: ServiceHub, view_cls, controller_cls):
    view = view_cls()
    controller_cls(hub).attach(view)
    return view
