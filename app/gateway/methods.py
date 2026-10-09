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
    #: 出口字段白名单（只用于**行列表**载荷）：None = 原样出，元组 = 每行只留这些键。
    #:
    #: 为什么要在这一格声明而不是改服务的返回：``SourceService.list_sources()`` 返回
    #: 的是 ``list[BookSource]``，出口按 ``to_dict()`` 折平就是 30 个键，920 行那一发的
    #: data 是 6,969,039 B（响应体同量，投影前 G2-F 那一趟线上读数），其中
    #: ``exploreUrl`` 40.1 % + ``jsLib`` 23.5 % + 五组 ``rule*`` 24.0 % 是规则原文
    #: （逐键字节账见 ``.probe/g2f_payload_anatomy.py``——它自己那份副本 6,896,985 B，
    #: 绝对值随库里有没有备注/登录页漂，占比取这一支、绝对值取线上那一趟）。
    #: 列表那一屏一根都不显示（flet 那侧同样只读
    #: ``book_source_list_view.py:208-211`` 的四格 + 搜索框的备注）。
    #: 跨进程时这笔字节钱全由客户端付——投影前实测：Dart 解那 6.97 MB 是
    #: 转码 13.4 ms + `jsonDecode` 64.4 ms，而 Python 侧整发（含解）才 101.2 ms，
    #: 总账 1.7 倍；投影后同一发 351,554 B，总账回到 1.39 倍。
    #:
    #: 裁剪是**单向**的：规则原文仍然在库里，编辑器按 ``source.get`` 取回全文，
    #: 保存那条线（``source.save`` 吃整条 ``source_json``）绝不能拿列表行当底本，
    #: 否则 ``exploreUrl``/``jsLib`` 会被空值盖掉。护栏见
    #: ``tests/test_gateway_contract.py`` 的投影那组。
    fields: tuple[str, ...] | None = None


def _m(action: str, service: str, method: str, params: tuple[str, ...],
       timeout: float, mode: str = "call", summary: str = "",
       fields: tuple[str, ...] | None = None) -> Method:
    return Method(action=action, service=service, method=method, params=params,
                  timeout=timeout, mode=mode, summary=summary, fields=fields)


#: 网关自己实现的动作（握手/状态/停服）——它们的"服务类"就是 GatewayService。
_SELF = "app.services.gateway.service.GatewayService"
_SOURCES = "app.services.sources.source_service.SourceService"
_CHECK = "app.services.sources.source_check_service.SourceCheckService"
_READING = "app.services.reading.reading_service.ReadingService"
_BOOKMARK = "app.services.reading.bookmark_service.BookmarkService"
_CONTENT_SEARCH = "app.services.reading.content_search.ContentSearchService"
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
       summary="书源列表行：只带列表那一屏读的字段，规则原文按 source.get 取",
       fields=("bookSourceUrl", "bookSourceName", "bookSourceGroup",
               "bookSourceComment", "enabled", "loginUrl")),
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
    _m("reader.open", _READING, "open_by_url",
       ("source_url", "book", "shelve", "resume"), 60.0,
       summary="开阅读会话：按 url 取回书源、载目录、恢复进度、按需入书架；"
               "目录不在这一发里（几千章另走 reader.toc）"),
    _m("reader.close", _READING, "close_by_url",
       ("source_url", "book_url", "save_progress"), 20.0,
       summary="结束会话并按需落最后一篇进度；url 对不上当前会话就什么都不关"),
    _m("reader.toc", _READING, "chapter_rows",
       ("source_url", "book_url", "refresh"), 60.0,
       summary="目录行：只带目录那一屏要的字段，正文与逐源变量不过协议面",
       fields=("name", "url", "index", "isVolume", "isVip", "isPay", "tag",
               "wordCount")),
    _m("reader.content", _READING, "chapter_document",
       ("source_url", "book_url", "chapter_url"), 60.0,
       summary="一章正文（已是可显示形态，``<usehtml>`` 在服务侧渲染掉）；"
               "顺带起一轮预取，二进制块只报 binary 不给值"),
    _m("reader.position_get", _READING, "read_position",
       ("source_url", "book_url"), 10.0,
       summary="续读位置：回的是 books 表那一份存档的形状，没有记录就是 null"),
    _m("reader.position_save", _READING, "save_position",
       ("source_url", "book_url", "position_json"), 10.0,
       summary="落一篇阅读位置：章按 index 认、偏移与段号按本章字符流收口，"
               "回落库后的形状（客户端据此知道自己存进去的是什么）"),
    _m("reader.staged", _READING, "staged_record", (), 5.0, mode="sync",
       summary="当前书若是 notShelf 试读暂存给出那条书架记录，在架的书回 null"),
    _m("reader.staged_promote", _READING, "promote_staged_book", (), 10.0,
       summary="试读转正：暂存记录原地变书架记录，进度/变量/目录缓存不动"),
    _m("reader.staged_discard", _READING, "discard_staged_book", (), 10.0,
       summary="试读放弃：删掉那条暂存记录"),
    _m("reader.bookmarks", _BOOKMARK, "list_bookmarks", ("book_url",), 10.0,
       summary="按书列书签（创建时间升序）"),
    _m("reader.bookmark_add", _BOOKMARK, "add_bookmark",
       ("book_url", "chapter_index", "offset", "summary"), 10.0,
       summary="加一条书签，回它的 id"),
    _m("reader.bookmark_remove", _BOOKMARK, "remove_bookmark",
       ("book_url", "bookmark_id"), 10.0,
       summary="删一条书签；回是否真删到（可能已被另一侧删过）"),
    _m("reader.search_content", _CONTENT_SEARCH, "search_ensured",
       ("book_url", "keyword", "source_url"), 120.0,
       summary="书内全文检索：先按需补索引再查，并回索引覆盖几章——"
               "0 命中要分得清『没缓存』与『真没有』"),
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
