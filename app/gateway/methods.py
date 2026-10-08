"""action 表：协议面上**唯一**一张"谁能被外部调用"的声明（设计 §4.2）。

为什么是显式表而不是反射扫服务：反射会把 ``list_sources() -> list[BookSource]``
这种服务面对象直接顶上协议面（设计 §2 数的就是这一条），也会把
`_` 前缀的内部方法、以及"参数是 flet 控件"的方法一并开放出去。表在这里，
等于"每次加动作都要有人显式签一次字"。

三条硬规矩（都有护栏测试钉着，见 ``tests/test_gateway_contract.py``）：

1. ``service`` + ``method`` 必须解析到**真的可调用属性**——防记忆里
   ``refactor-regression-orphan-methods`` 那一族（重构后方法还在、没人调用）；
2. ``params`` 必须与该 action 的入参 schema 的 ``properties`` **逐字相等**——
   字段在两侧各写一遍迟早会飘（真果鉴 112 个手写 ``fromJson`` 就是这笔债）；
3. ``service`` 写**类的全名字符串**而不是 import 进来的类对象：协议层反过来依赖
   ``app.services.*`` 会把分层拧个方向，运行期由网关解析一次并缓存。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Method:
    """一个 action 的全部对外事实。"""

    action: str
    #: 服务类全名（``模块.类名``）。
    service: str
    #: 该类上的方法名（协程或同步皆可，由 :attr:`mode` 说明怎么调）。
    method: str
    #: 传给方法的 payload 键（按名字取、按顺序传）。
    params: tuple[str, ...]
    #: 服务端预算（秒）；``mode="job"`` 时这条不适用于任务本体，只适用于"起任务"。
    timeout: float
    #: ``call`` = 等结果回 data；``job`` = 起后台立即回受理凭据，进度走事件流；
    #: ``sync`` = 同步方法就地调用（停止/取消这类，本来就不该排队等 loop）。
    mode: str = "call"
    summary: str = ""


def _m(action: str, service: str, method: str, params: tuple[str, ...],
       timeout: float, mode: str = "call", summary: str = "") -> Method:
    return Method(action=action, service=service, method=method, params=params,
                  timeout=timeout, mode=mode, summary=summary)


#: 网关自己实现的动作（握手/状态/停服）——它们的"服务类"就是 GatewayService。
_SELF = "app.services.gateway.service.GatewayService"
_SOURCES = "app.services.sources.source_service.SourceService"
_CHECK = "app.services.sources.source_check_service.SourceCheckService"
_READING = "app.services.reading.reading_service.ReadingService"
_IMPORT = "app.services.sources.online_import_service.OnlineImportService"
_AUDIT = "app.services.sources.compat_audit_service.CompatAuditService"
_SETTINGS = "app.core.settings.SettingsService"
_THEME = "app.services.tools.theme_service.ThemeService"

METHODS: tuple[Method, ...] = (
    _m("engine.hello", _SELF, "hello", (), 2.0,
       summary="协议版本/能力位/schema 摘要握手；版本不符客户端就该拒启动"),
    _m("engine.status", _SELF, "service_status", (), 2.0,
       summary="全部服务的状态快照（迁移期用来对照 flet 仪表盘）"),
    _m("engine.shutdown", _SELF, "request_shutdown", ("reason",), 5.0, mode="sync",
       summary="客户端主动收摊：置退出标志，主线程随即走 hub.shutdown()"),
    _m("bookshelf.list", _SOURCES, "list_bookshelf", ("include_staged",), 20.0,
       summary="书架记录（默认排除 notShelf 试读暂存）"),
    _m("bookshelf.add", _SOURCES, "shelve_by_url",
       ("source_url", "book", "shelved"), 20.0,
       summary="把搜索结果那一行放进书架；书以字典快照交付，模型对象在服务侧还原"),
    _m("source.list", _SOURCES, "list_sources", (), 30.0,
       summary="全部书源；返回的是 BookSource 对象，出口序列化在边界上折成 dict"),
    _m("source.get", _SOURCES, "load_source", ("url",), 10.0,
       summary="单条书源全文（编辑器打开一条要读回它的全部规则字段）"),
    _m("source.save", _SOURCES, "save_source", ("source_json",), 20.0,
       summary="校验 + upsert + 落盘 + 发事件；回 {saved, source_id, errors, audit}"),
    _m("source.delete", _SOURCES, "delete_source", ("url",), 15.0,
       summary="删一条书源（连同它的规则）；界面必须先二次确认"),
    _m("source.enable", _SOURCES, "set_enabled", ("url", "enabled"), 15.0,
       summary="启用/停用一条书源"),
    _m("source.group", _SOURCES, "set_group", ("url", "group"), 15.0,
       summary="改一条书源的分组（逗号分隔的组名）"),
    _m("source.batch_enable", _SOURCES, "batch_set_enabled", ("urls", "enabled"),
       60.0, summary="批量启停；回改动的条数，不是「是否成功」"),
    _m("source.batch_delete", _SOURCES, "batch_delete", ("urls",), 60.0,
       summary="批量删除；回删掉的条数"),
    _m("source.export", _SOURCES, "export_sources_json", (), 60.0,
       summary="全部书源的 JSON 文本（导入导出口径与 flet 那份一字不差）"),
    _m("source.audit", _AUDIT, "audit_source", ("source_json",), 30.0,
       summary="兼容性审计报告：编辑器保存前那一道缺口清单"),
    _m("import.text", _IMPORT, "import_from_file", ("text", "kind", "mode"), 120.0,
       summary="本地文本导入（kind=book_source 才真落库，其余登记说明不动数据）"),
    _m("import.url", _IMPORT, "import_from_url", ("url", "kind", "mode"), 120.0,
       summary="按 URL 拉取再导入；拉不到记一条 error 而不是抛穿"),
    _m("browse.categories", _READING, "browse_categories", ("source_url",), 30.0,
       summary="该书源的分类清单（静态与 @js: 动态都在引擎里解析）"),
    _m("browse.books", _READING, "browse_books", ("source_url", "category", "page"),
       30.0, summary="分类下一页书单；category 用 browse.categories 回的那一条原样传回"),
    _m("search.scopes", _READING, "search_scopes", (), 5.0,
       summary="搜索范围表（全部/未分组/各分组）与各自的可搜源数；口径见 searchable_sources"),
    _m("search.start", _READING, "search_in_scope",
       ("keyword", "scope", "search_id"), 30.0, mode="job",
       summary="多源找书：结果逐批走 SearchFinishedEvent，同一 search_id 再调即翻页"),
    _m("search.stop", _READING, "stop_search", (), 5.0,
       summary="停止当前搜索：取消在飞任务，晚到的结果不再回界面"),
    _m("check.start", _CHECK, "check_all", ("groups",), 30.0, mode="job",
       summary="起一轮批量检测；进度与结论走事件流，不占这条 RPC"),
    _m("check.snapshot", _CHECK, "run_snapshot", (), 5.0,
       summary="当前/最近一轮检测的回放快照（断线重连补进度用）"),
    _m("check.cancel", _CHECK, "cancel", (), 5.0, mode="sync",
       summary="取消在跑的批量检测"),
    _m("check.source", _CHECK, "check_source", ("source_json", "depth"), 60.0,
       summary="只检这一条源（编辑器问一句「这条源能不能用」）；会真的发网络请求"),
    _m("settings.read", _SETTINGS, "read_keys", ("keys",), 2.0, mode="sync",
       summary="按名单读设置；没设过的键不出现在返回里（与「设成 null」是两件事）"),
    _m("settings.write", _SETTINGS, "set", ("key", "value"), 2.0, mode="sync",
       summary="写一项设置：内存 + 落库 + 广播 SettingsChangedEvent"),
    _m("theme.preferences", _THEME, "get_preferences", (), 2.0, mode="sync",
       summary="外观偏好快照（skin_id / mode / eink）"),
    _m("theme.save", _THEME, "set_preferences", ("skin_id", "mode", "eink"), 5.0,
       mode="sync", summary="合并更新外观偏好并持久化；None 的那一格保持原值"),
    _m("engine.webContentReady", _SELF, "web_content_ready",
       ("request_id", "body", "headers", "status", "url", "cookie", "error",
        "js_result"), 5.0,
       summary="WebView 渲染结果交回：解开一次服务端→客户端的 webview.content 反向请求"),
)

#: 按 action 名索引（重名当场炸：两条同名的动作到了客户端就是随机路由）。
BY_ACTION: dict[str, Method] = {}
for _method in METHODS:
    if _method.action in BY_ACTION:
        raise ValueError(f"action 重复注册：{_method.action}")
    BY_ACTION[_method.action] = _method
del _method


def resolve_service_class(dotted: str) -> type:
    """``模块.类名`` → 类对象（解析失败抛 ImportError/AttributeError，由网关折成错误信封）。"""
    module_path, class_name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), class_name)
