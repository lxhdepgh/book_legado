"""M3 书源域 UI 拆分：视图按域分包 + 每视图一个 Controller（设计 §7.2 / §7.4 / §7.5）。

本包（``app/views/source/``）只承载 Flet 布局与渲染，**禁止 import 任何
``app.services.*`` 具体模块**（import-lint 守门，§7.5）。视图所需数据一律经
所属 Controller 从 ``ServiceHub.get()`` 取服务后注入；跨视图协作经 ViewRegistry
路由与 EventBus 事件，互不直接 import。

书源组五个目的地（注册进 ViewRegistry）：

- :class:`BookSourceListView` 书源列表：分组 / 启用停用 / 导入导出 / 检测入口；
- :class:`SourceImportView`    导入：本地 JSON 文件 + 在线 URL 多类型；
- :class:`CodeEditorView`     规则编辑器：自绘输入框 + 分词高亮层 + 保存即校验；
- :class:`CheckView`          检测：CheckFinishedEvent 逐源渲染；
- :class:`RuleRepairView`     自愈：RepairReportReadyEvent 报告确认写回。
"""
from __future__ import annotations

from app.core.hub import ServiceHub
from app.core.registry import (DEST_SOURCE_CHECK, DEST_SOURCE_EDITOR,
                               DEST_SOURCE_IMPORT, DEST_SOURCE_LIST,
                               DEST_SOURCE_REPAIR, SOURCE_DESTINATIONS,
                               ViewRegistry)

from app.views.source.book_source_list_view import BookSourceListView
from app.views.source.code_editor_view import CodeEditorView
from app.views.source.check_view import CheckView
from app.views.source.rule_repair_view import RuleRepairView
from app.views.source.import_view import SourceImportView

from app.controllers.source.book_source_list_controller import BookSourceListController
from app.controllers.source.code_editor_controller import CodeEditorController
from app.controllers.source.check_controller import CheckController
from app.controllers.source.rule_repair_controller import RuleRepairController
from app.controllers.source.source_import_controller import SourceImportController

__all__ = [
    "BookSourceListView", "SourceImportView", "CodeEditorView",
    "CheckView", "RuleRepairView",
    "BookSourceListController", "SourceImportController", "CodeEditorController",
    "CheckController", "RuleRepairController",
    "register_destinations", "SOURCE_DESTINATIONS",
]


def register_destinations(registry: ViewRegistry, hub: ServiceHub) -> ViewRegistry:
    """把书源组五个目的地绑进路由表（§7.3：工厂由装配处绑定）。

    工厂闭包在实例化视图的同时 new 对应 Controller 并 attach(view)，返回挂载好的视图。
    所需服务集按 §9.2 声明，供导航层做就绪性检查。
    """
    registry.register(
        DEST_SOURCE_LIST, lambda: _build(hub, BookSourceListView, BookSourceListController),
        required_services=("SourceService",))
    registry.register(
        DEST_SOURCE_IMPORT, lambda: _build(hub, SourceImportView, SourceImportController),
        required_services=("OnlineImportService",))
    registry.register(
        DEST_SOURCE_EDITOR, lambda: _build(hub, CodeEditorView, CodeEditorController),
        required_services=("SourceService", "CompatAuditService"))
    registry.register(
        DEST_SOURCE_CHECK, lambda: _build(hub, CheckView, CheckController),
        required_services=("SourceCheckService",))
    registry.register(
        DEST_SOURCE_REPAIR, lambda: _build(hub, RuleRepairView, RuleRepairController),
        required_services=("RuleRepairService",))
    return registry


def _build(hub: ServiceHub, view_cls, controller_cls):
    view = view_cls()
    controller_cls(hub).attach(view)
    return view
