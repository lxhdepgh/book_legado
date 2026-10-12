"""ViewRegistry：声明式目的地路由表（§7.3）。

目的地 → (视图工厂, 所需服务集)。core 层不 import 任何 Flet 视图（红线 §4.6），
工厂由 main.py / 视图装配处在启动时绑定。

目的地分组（§7.3 分组式 NavigationRail）：

- **阅读组（M2 已注册）**：
  - :data:`DEST_FIND_BOOKS` —— ``"reading.find_books"`` 找书
  - :data:`DEST_BOOKSHELF` —— ``"reading.bookshelf"`` 书架
  - :data:`DEST_READER` —— ``"reading.reader"`` 阅读器
  工厂绑定入口：:func:`app.views.reading.register_destinations(registry, hub)`。
- **书源组（M3）**：``sources.list`` / ``sources.check`` / ``sources.repair`` /
  ``sources.editor`` —— 随 SourceService/CheckService/RuleRepairUi 落地注册。
- **RSS 组（M4）**：``rss.sources`` / ``rss.articles`` / ``rss.read``。
- **下载组（M4/M5）**：``downloads.center`` 下载中心、``downloads.cache`` 整书缓存。
- **工具组（M5）**：``tools.webdav`` / ``tools.export`` / ``tools.qr`` /
  ``tools.dict`` / ``tools.replace`` / ``tools.font`` / ``tools.bridge``。
- **设置组（M6）**：``settings.appearance`` / ``settings.dashboard``
  （``settings.dashboard`` 由 M6 第三线交付完整版 ServiceDashboard，本线先注册
  最小占位工厂，集成方替换——契约 §5）。

M6（本线）在 core 侧补齐**导航元数据**（分组 + 中文标题）：core 仍零 flet，
元数据只是字符串表（:data:`NAV_META`）。视图工厂仍由各域 ``register_destinations``
绑定；导航容器（``app/views/navigation.py``）装配后调
:meth:`ViewRegistry.attach_navigation_meta` 把分组/标题贴到路由上。

导航形状沿用旧单体（``old_code/app/app.py`` 的 3 个顶级入口）：**一个分组 = 侧栏
一项**，组内目的地作为该页顶部 Tab 合并展示，不再一项占一行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable


# --------------------------------------------------------------------------- #
# 阅读组目的地常量（M2 注册；其余分组目的地随各自里程碑补常量并注册）。
# 命名即约定：``<域>.<视图>``，视图层禁止散落 if/elif 装配。
# --------------------------------------------------------------------------- #
DEST_FIND_BOOKS = "reading.find_books"
DEST_BOOKSHELF = "reading.bookshelf"
DEST_READER = "reading.reader"

#: M2 阅读组已注册目的地（供导航层/测试一次性回查）。
READING_DESTINATIONS: tuple[str, ...] = (DEST_FIND_BOOKS, DEST_BOOKSHELF, DEST_READER)


# --------------------------------------------------------------------------- #
# 漫画专用阅读器目的地（M4 内容形态域，设计 §1.3；register 由
# views/reading/__init__.py 的 register_destinations 追加完成）。
# --------------------------------------------------------------------------- #
DEST_MANGA_READER = "reading.manga"       # image 类型书：纵向无限滚动漫画渲染通道

#: M4 漫画目的地（独立元组，不并入 M2 的 READING_DESTINATIONS——后者被 M2 测试
#: 按精确三元组回查；新增目的地另列，避免破坏既有断言）。
MANGA_DESTINATIONS: tuple[str, ...] = (DEST_MANGA_READER,)

# --------------------------------------------------------------------------- #
# RSS 目的地（RssService 早已交付，界面这次补上）。
# 作为阅读组的**第三个页签**（hidden=False）：订阅和找书/书架都是"读什么"，
# 分成三个侧栏入口只是把一页能看完的东西拆三页。
# --------------------------------------------------------------------------- #
DEST_RSS = "reading.rss"                   # 订阅源 / 文章 / 正文


# --------------------------------------------------------------------------- #
# 书源组目的地常量（M3 注册；register 由 views/source/__init__.py 完成）。
# --------------------------------------------------------------------------- #
DEST_SOURCE_LIST = "sources.list"        # 书源列表/分组/启用停用/导入导出/检测入口
DEST_SOURCE_IMPORT = "sources.import"    # 书源 JSON 文件导入 + 在线导入 URL
DEST_SOURCE_EDITOR = "sources.editor"    # 规则编辑器（选书源 + 按字段编辑 + 保存即校验）
DEST_SOURCE_CHECK = "sources.check"      # 书源检测（四段流水线结果渲染）
DEST_SOURCE_REPAIR = "sources.repair"    # 正文规则自愈报告确认写回

#: M3 书源组已注册目的地（供导航层/测试一次性回查）。
SOURCE_DESTINATIONS: tuple[str, ...] = (
    DEST_SOURCE_LIST, DEST_SOURCE_IMPORT, DEST_SOURCE_EDITOR,
    DEST_SOURCE_CHECK, DEST_SOURCE_REPAIR,
)


# --------------------------------------------------------------------------- #
# 工具组目的地常量（M5 注册；register 由 views/tools/__init__.py 完成）。
# 命名即约定 ``tools.<视图>``；新增目的地另列常量，不动上方阅读/书源组元组
# （后者被 M2/M3 测试按精确长度回查）。
# --------------------------------------------------------------------------- #
DEST_TOOLS_OVERVIEW = "tools.overview"      # 更多工具入口页（侧栏唯一工具组目的地）
DEST_TOOLS_BROWSER = "tools.browser"        # 通用浏览器入口（地址栏/前进后退/加载态）
DEST_TOOLS_DICT = "tools.dict"              # 划词取词弹窗（阅读域划词 → DictService）
DEST_TOOLS_REPLACE = "tools.replace"        # 替换规则管理（全局规则 CRUD / 启停）
DEST_TOOLS_FONT = "tools.font"              # 字体管理（系统枚举 / 用户字体增删 / 选中）
DEST_TOOLS_QR = "tools.qr"                  # 二维码（生成 / 识别 / 手动输入导入）
DEST_TOOLS_EXPORT = "tools.export"          # 书籍导出（epub / txt）
DEST_TOOLS_WEBDAV = "tools.webdav"         # 备份设置页（WebDAV 连接 / 备份 / 恢复）
DEST_TOOLS_UPDATE = "tools.update"          # 更新检查（检查 / 提示 / 下载到 downloads/）
#: 网络桥设置页（M5 §1.15 界面承接）。**不并入** :data:`TOOLS_DESTINATIONS`
#: （那条元组冻结 8 项，M5 测试按精确长度回查）。
DEST_TOOLS_WEB_BRIDGE = "tools.webbridge"   # 本地 HTTP/WebSocket 网桥开关与令牌
#: 听书语音档管理页（tts_profiles 的增删与默认档）。同样**不并入**冻结的
#: TOOLS_DESTINATIONS 元组。
DEST_TOOLS_TTS = "tools.tts"                # 朗读语音档（引擎类型 / HTTP-TTS 地址）

#: M5 工具组已注册目的地（供导航层/测试一次性回查；**冻结 8 项**——M5 测试
#: 按精确长度回查。工具入口页 tools.overview 另列 :data:`DEST_TOOLS_OVERVIEW`，
#: 不并入本元组，避免破坏既有断言；新增目的地另列常量，不动冻结元组。
TOOLS_DESTINATIONS: tuple[str, ...] = (
    DEST_TOOLS_BROWSER, DEST_TOOLS_REPLACE, DEST_TOOLS_FONT,
    DEST_TOOLS_QR, DEST_TOOLS_EXPORT, DEST_TOOLS_WEBDAV, DEST_TOOLS_UPDATE,
)


# --------------------------------------------------------------------------- #
# 设置组目的地常量（M6 注册；工厂由 app/views/navigation.py 的装配函数绑定）。
# --------------------------------------------------------------------------- #
DEST_SETTINGS_APPEARANCE = "settings.appearance"   # 外观（ThemeService 承接偏好，占位页）
DEST_SETTINGS_DASHBOARD = "settings.dashboard"    # 服务仪表盘（M6 第三线交付完整版）
#: 快捷键页（第七十三轮）：阅读器键位 + 老板键。此前 ``reader.shortcuts`` 只在
#: 开书时读一次，界面上没有任何地方能改（用户报"打开软件没找到按键设置的位置"）。
DEST_SETTINGS_SHORTCUTS = "settings.shortcuts"

#: 设置组已注册目的地（供导航层/测试一次性回查）。
SETTINGS_DESTINATIONS: tuple[str, ...] = (
    DEST_SETTINGS_APPEARANCE, DEST_SETTINGS_DASHBOARD, DEST_SETTINGS_SHORTCUTS,
)


# --------------------------------------------------------------------------- #
# 导航分组（§7.3；旧架构 app.py 的三个顶级入口，顺序与图标同序）。
# --------------------------------------------------------------------------- #
GROUP_READING = "阅读"
GROUP_SOURCE = "书源"
GROUP_CONFIG = "配置"

#: 旧别名：M5/M6 历史代码与测试 import GROUP_TOOLS / GROUP_SETTINGS；工具与
#: 设置全部收进「配置」组，别名指向同一字符串，import 不破、值语义一致。
GROUP_TOOLS = GROUP_CONFIG
GROUP_SETTINGS = GROUP_CONFIG

#: 分组展示顺序（NavigationRail 自上而下 / 底部导航从左到右）：书源 → 阅读 → 配置。
GROUP_ORDER: tuple[str, ...] = (GROUP_SOURCE, GROUP_READING, GROUP_CONFIG)


#: 导航元数据：目的地 → (分组, 标签, 是否整页)。
#: 这是全量目的地的权威清单。装配处调 :meth:`ViewRegistry.attach_navigation_meta`
#: 一次性贴到路由上；core 零 flet，这里只有字符串。顺序即导航展示顺序
#: （:meth:`ViewRegistry.grouped_destinations` 按 NAV_META 声明顺序产出，不再排序）。
#:
#: **一个分组 = 左侧一个顶级入口**（旧架构就 3 项：书源 / 阅读 / 配置）。
#: 分组内的非整页目的地不再各自占侧栏一行，而是作为那一页顶部的 Tab 共存
#: （``app/views/navigation.py::SectionView``），相关功能因此收在一起。
#:
#: 3 元组第 3 项 ``hidden`` 的新语义是「**整页目的地**」：注册可达但不在导航
#: 展示，也不进页签容器，切换到时独占内容区（阅读器、漫画、工具子页）。
#: 旧 2 元组值 ``(group, title)`` 仍兼容（hidden 取 route 注册时自带值，缺省 False）。
NAV_META: dict[str, tuple] = {
    # -- 书源组（M3）：五个目的地合成「书源」一页的五个页签 --
    DEST_SOURCE_LIST:    (GROUP_SOURCE, "管理",   False),
    DEST_SOURCE_IMPORT:  (GROUP_SOURCE, "导入",   False),
    DEST_SOURCE_CHECK:   (GROUP_SOURCE, "检测",   False),
    DEST_SOURCE_EDITOR:  (GROUP_SOURCE, "规则",   False),
    DEST_SOURCE_REPAIR:  (GROUP_SOURCE, "自愈",   False),
    # -- 阅读组（M2/M4）：找书 + 书架合成「阅读」页的两个页签；
    #    阅读器/漫画是整页目的地（开读时独占内容区，侧栏仍高亮「阅读」）--
    DEST_FIND_BOOKS:     (GROUP_READING, "找书",  False),
    DEST_BOOKSHELF:      (GROUP_READING, "书架",  False),
    DEST_RSS:            (GROUP_READING, "订阅",  False),
    DEST_READER:         (GROUP_READING, "阅读器", True),
    DEST_MANGA_READER:   (GROUP_READING, "漫画",   True),
    # -- 配置组（M5/M6）：工具入口/外观/服务合成「配置」页的三个页签；
    #    8 个工具子页仍是整页目的地（从「更多工具」卡片跳转）--
    DEST_TOOLS_OVERVIEW: (GROUP_CONFIG, "更多工具", False),
    DEST_SETTINGS_APPEARANCE: (GROUP_CONFIG, "外观", False),
    DEST_SETTINGS_SHORTCUTS: (GROUP_CONFIG, "快捷键", False),
    DEST_SETTINGS_DASHBOARD:  (GROUP_CONFIG, "服务", False),
    DEST_TOOLS_BROWSER:  (GROUP_CONFIG, "浏览器",   True),
    # [已移除] DEST_TOOLS_DICT 词典功能在任务 #45 中被删除
    # DEST_TOOLS_DICT:     (GROUP_CONFIG, "词典",     True),
    DEST_TOOLS_REPLACE:  (GROUP_CONFIG, "替换规则", True),
    DEST_TOOLS_FONT:     (GROUP_CONFIG, "字体",     True),
    DEST_TOOLS_QR:       (GROUP_CONFIG, "二维码",   True),
    DEST_TOOLS_EXPORT:   (GROUP_CONFIG, "导出",     True),
    DEST_TOOLS_WEBDAV:   (GROUP_CONFIG, "备份",    True),
    DEST_TOOLS_UPDATE:   (GROUP_CONFIG, "更新",     True),
    DEST_TOOLS_WEB_BRIDGE: (GROUP_CONFIG, "网络桥", True),
    DEST_TOOLS_TTS:      (GROUP_CONFIG, "朗读",     True),
}


@dataclass(frozen=True)
class ViewRoute:
    """一条目的地路由：视图工厂 + 该目的地依赖的服务名集合 + 导航元数据。"""

    factory: Callable[..., object]
    required_services: frozenset[str] = field(default_factory=frozenset)
    #: 导航条目标题（中文）；未 attach 元数据前为空串。
    title: str = ""
    #: 所属分组（:data:`GROUP_READING` 等）；未 attach 元数据前为空串。
    group: str = ""
    #: 整页目的地：不进侧栏也不进分组页签，切换到时独占内容区（阅读器、漫画、
    #: 工具子页经「更多工具」卡片跳转；§7.3 导航精简）。
    hidden: bool = False


class ViewRegistry:
    """目的地路由表：禁止视图层散落 if/elif 装配（§7.3）。"""

    def __init__(self) -> None:
        self._routes: dict[str, ViewRoute] = {}

    def register(self, destination: str, factory: Callable[..., object],
                 required_services: Iterable[str] = (), *,
                 title: str = "", group: str = "", hidden: bool = False) -> None:
        """注册/覆盖一条路由。title/group 可选：各域 register_destinations
        （M2/M3/M5）不传，由 :meth:`attach_navigation_meta` 统一补齐。
        ``hidden`` 表示目的地是整页视图：不在导航展示，也不进组内页签。"""
        self._routes[destination] = ViewRoute(
            factory=factory, required_services=frozenset(required_services),
            title=title, group=group, hidden=hidden)

    def get(self, destination: str) -> ViewRoute:
        """取路由；目的地未注册直接报错（导航层不可达目的地红线）。"""
        if destination not in self._routes:
            raise KeyError(f"未注册的目的地: {destination}")
        return self._routes[destination]

    def try_get(self, destination: str) -> ViewRoute | None:
        return self._routes.get(destination)

    def destinations(self) -> list[str]:
        return sorted(self._routes)

    def attach_navigation_meta(self, meta: dict | None = None) -> None:
        """把 (分组, 标题[, hidden]) 元数据贴到已注册路由上（M6 导航容器装配调用）。

        - ``meta`` 缺省用模块级 :data:`NAV_META`（全量目的地权威清单）；
        - 元数据指向**未注册**目的地直接报错——装配期漏注册即暴露，
          不允许导航条出现点不开的死项；
        - 值兼容 2 元组 ``(group, title)`` 与 3 元组 ``(group, title, hidden)``：
          2 元组时 hidden 沿用 route 注册时自带值（缺省 False），3 元组时以
          meta 为准（单一权威）；
        - 幂等：对已贴过元数据的路由重复调用结果一致。
        """
        meta = NAV_META if meta is None else meta
        for dest, entry in meta.items():
            route = self._routes.get(dest)
            if route is None:
                raise KeyError(f"导航元数据指向未注册目的地: {dest}")
            if len(entry) == 2:
                group, title = entry
                hidden = route.hidden
            else:
                group, title, hidden = entry
            self._routes[dest] = ViewRoute(
                factory=route.factory,
                required_services=route.required_services,
                title=title, group=group, hidden=bool(hidden))

    def grouped_destinations(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """按 :data:`GROUP_ORDER` 返回 ``[(分组, [(destination, 标签), ...])]``。

        这份表就是导航的结构定义：**每个分组是侧栏的一个顶级入口**，组内的条目
        是该入口页顶部的 Tab（``app/views/navigation.py::SectionView``）。整页
        目的地（``hidden=True``：阅读器/漫画/工具子页）不进表，由导航层单独挂载。
        组内顺序按 :data:`NAV_META` **声明顺序**（不再按 destination 字符串排序
        ——NAV_META 注释本就承诺"顺序即导航展示顺序"）。未贴元数据的目的地
        （group 为空）归入末尾"其他"，保证不丢项。
        """
        # 按 NAV_META 声明顺序构建可见桶（dict 保序）
        order = {dest: i for i, dest in enumerate(NAV_META)}
        buckets: dict[str, list[tuple[str, str]]] = {}
        for dest, route in self._routes.items():
            if route.hidden:
                continue
            group = route.group or "其他"
            buckets.setdefault(group, []).append((dest, route.title))
        for items in buckets.values():
            items.sort(key=lambda pair: order.get(pair[0], len(order)))
        ordered: list[tuple[str, list[tuple[str, str]]]] = []
        for group in GROUP_ORDER:
            if group in buckets:
                ordered.append((group, buckets.pop(group)))
        for group in sorted(buckets):  # 兜底：未分组目的地排末尾
            ordered.append((group, buckets[group]))
        return ordered


_REGISTRY: ViewRegistry | None = None


def default_registry() -> ViewRegistry:
    """进程级路由表（M1 仅含 dashboard；后续目的地随视图里程碑注册）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ViewRegistry()
    return _REGISTRY
