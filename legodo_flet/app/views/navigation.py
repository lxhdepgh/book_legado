"""M6 导航容器：三个顶级入口（旧架构布局）+ 组内页签 + 窄窗底部导航。

设计落点：

- §7.3：侧栏**一个分组一个顶级入口**（书源 / 阅读 / 配置，与旧单体
  ``old_code/app/app.py`` 的三项同序同图标），组内的目的地不再各占一行，而是
  合成该页顶部的 Tab（:class:`SectionView`）——旧架构把书源的列表/导入/检测、
  配置的外观/阅读/快捷键放在同一页的 TabBar 里，这里把那个布局搬回分层结构。
  切页签**不重建子视图也不退订控制器**，所以在途的搜索/扫描任务照常跑完，切回来
  就是最新结果；离开整个分组时才统一退订。
- 整页目的地（``NAV_META`` 第 3 项 ``hidden``：阅读器、漫画、8 个工具子页）
  不进页签，切换到时独占内容区，侧栏高亮它所属的分组。
- 切换目的地时按 ``ViewRegistry`` 工厂重建页签容器 / 整页视图；导航外壳不重建。
  阅读器 / 漫画的沉浸模式由各自视图内部处理（M2/M4 已交付），本容器不额外改造。
- 窄窗（:data:`NARROW_WIDTH_THRESHOLD` 以下）走底部 :class:`ft.NavigationBar`，
  同样只有三个顶级入口，切换判定纯函数 :func:`is_narrow_width`
  （按宽度阈值，便于无头测试注入 width 替身）。
- core 零 flet：本模块在视图层（可 import flet），是 main 装配与 ViewRegistry 之间
  的桥：负责把阅读/书源/工具三组 ``register_destinations`` + 设置组占位工厂 +
  :meth:`ViewRegistry.attach_navigation_meta` 一次性接齐。

设置组三个目的地：

- ``settings.appearance``：外观完整页（M7 补全，ThemeService 承接偏好）；
- ``settings.shortcuts``：快捷键页（第七十三轮，阅读键位 + 老板键）；
- ``settings.dashboard``：M6 第三线（Agent C）交付完整版 ServiceDashboard。
"""
from __future__ import annotations

import logging

import flet as ft

from app.core.hub import ServiceHub
from app.core.registry import (ViewRegistry,
                               DEST_BOOKSHELF, DEST_FIND_BOOKS, DEST_READER,
                               DEST_MANGA_READER, GROUP_READING,
                               DEST_SETTINGS_APPEARANCE, DEST_SETTINGS_DASHBOARD,
                               DEST_SETTINGS_SHORTCUTS)

logger = logging.getLogger(__name__)

# 窄窗判定阈值（dp）：宽度小于此值切底部导航（§7.3 自适应，无头测试可注入替身）。
NARROW_WIDTH_THRESHOLD = 720.0

#: 落地目的地兜底：注册表里没有阅读组、或书架探不到时用。首屏实际落点由
#: :meth:`NavigationHost._anchor` 决定（有书先书架，见该类）。
DEFAULT_DESTINATION = DEST_FIND_BOOKS

#: 顶级入口（分组）图标——照抄旧实现 ``old_code/app/app.py`` 的三个目的地。
_GROUP_ICONS: dict[str, str] = {
    "书源": ft.Icons.LIBRARY_BOOKS_OUTLINED,
    "阅读": ft.Icons.MENU_BOOK_OUTLINED,
    "配置": ft.Icons.SETTINGS_OUTLINED,
}

#: 通用图标兜底（分组未在 :data:`_GROUP_ICONS` 里时用它）。
_FALLBACK_ICON = ft.Icons.SETTINGS_OUTLINED

#: 目的地 → 页签图标（组内 Tab 只做外观，不驱动逻辑；未列出的用通用图标兜底）。
_ICONS: dict[str, str] = {
    DEST_FIND_BOOKS: ft.Icons.TRAVEL_EXPLORE,
    "reading.bookshelf": ft.Icons.AUTO_STORIES,
    "reading.reader": ft.Icons.BOOK,
    "reading.manga": ft.Icons.PHOTO_LIBRARY,
    "sources.list": ft.Icons.LIBRARY_BOOKS,
    "sources.import": ft.Icons.INPUT,
    "sources.editor": ft.Icons.CODE,
    "sources.check": ft.Icons.FACT_CHECK_OUTLINED,
    "sources.repair": ft.Icons.HEALING,
    "tools.overview": ft.Icons.BUILD_OUTLINED,
    # [已移除] 浏览器图标：不再需要内置浏览器页面
    # "tools.browser": ft.Icons.PUBLIC,
    "tools.dict": ft.Icons.LANGUAGE,
    "tools.replace": ft.Icons.FIND_REPLACE,
    "tools.font": ft.Icons.TEXT_FIELDS,
    "tools.qr": ft.Icons.QR_CODE_2,
    "tools.export": ft.Icons.DOWNLOAD_FOR_OFFLINE,
    "tools.webdav": ft.Icons.UPLOAD_FILE,
    "tools.update": ft.Icons.SYSTEM_UPDATE,
    DEST_SETTINGS_APPEARANCE: ft.Icons.PALETTE_OUTLINED,
    DEST_SETTINGS_SHORTCUTS: ft.Icons.KEYBOARD_OUTLINED,
    DEST_SETTINGS_DASHBOARD: ft.Icons.DASHBOARD_OUTLINED,
}


def is_narrow_width(width: float | None) -> bool:
    """窄窗判定：``width`` 为 None（无头/未挂载）时按宽窗处理（Rail 可离线构造）。"""
    return bool(width is not None and width < NARROW_WIDTH_THRESHOLD)


#: 全应用背景图与主题底色的合成比例（见 :meth:`NavigationHost.render`）。
#: 图是"垫在文字后面的一层"，不是壁纸展：超过这个数，深色主题下的浅色图块会把
#: 浅字顶掉；低于 0.1 就等于换了张看不出来的图。
_BACKGROUND_OPACITY = 0.25


# --------------------------------------------------------------------------- #
# 设置组工厂（M6 集成：appearance 占位 / dashboard 完整版）
# --------------------------------------------------------------------------- #
# 完整版 ServiceDashboard 工厂需要 page 引用（build_dashboard(page, hub) 签名），
# 由 build_app_shell 在装配时写入；无头测试 page=None 时 dashboard 仍可离线构造。
_APP_PAGE: ft.Page | None = None

#: 当前导航外壳（:func:`build_app_shell` 装配时写入）。装配根订阅到"背景图偏好变了"
#: 之后要把它推到那一层上：视图层拿不到服务（红线 §7.5），反过来装配根也不该重建外壳，
#: 只需要这一个句柄——和上面 ``_APP_PAGE`` 同一个晚绑定路子。
_APP_HOST: "NavigationHost | None" = None


def current_host():
    """当前导航外壳；还没装配起来时 None。"""
    return _APP_HOST


def _build_appearance_view(hub: ServiceHub):
    """外观设置完整页（M7 后补全：AppearanceView + AppearanceController 桥接）。

    Controller 是视图与 ThemeService 之间的唯一桥（§7.5 红线：views 零
    app.services import，controllers 允许）。
    """
    from app.views.settings.appearance_view import AppearanceView
    from app.controllers.settings.appearance_controller import AppearanceController
    view = AppearanceView()
    AppearanceController(hub).attach(view)
    return view


def _build_dashboard_full(hub: ServiceHub):
    """服务仪表盘：完整版（M6 仪表盘线交付，启停控制 + 事件流订阅）。

    视图自带 ``detach()`` 供导航切换离开时退订事件（NavigationHost.show 调用）；
    完整版工厂替换原 M1 最小版 build_dashboard 的目的地注册。
    """
    from app.views.service_dashboard import build_dashboard
    return build_dashboard(_APP_PAGE, hub)


def _build_shortcut_view(hub: ServiceHub):
    """快捷键页（第七十三轮）：阅读键位 + 老板键，按一下就能录。

    老板键的可用性要问 :class:`BossKeyService`（非 Windows 就明说"本机没有托盘
    能力"），所以工厂要把 hub 递下去；服务没注册时页照样起得来，只是那一段
    写着"不会生效"。
    """
    from app.controllers.settings.shortcut_controller import ShortcutController
    from app.views.settings.shortcut_view import ShortcutView
    view = ShortcutView()
    ShortcutController(hub).attach(view)
    return view


def _register_settings_destinations(registry: ViewRegistry, hub: ServiceHub) -> None:
    """设置组三个目的地（appearance 完整页 / 快捷键 / dashboard 完整版）。"""
    registry.register(
        DEST_SETTINGS_APPEARANCE,
        lambda: _build_appearance_view(hub),
        required_services=("ThemeService",),
    )
    registry.register(
        DEST_SETTINGS_SHORTCUTS,
        lambda: _build_shortcut_view(hub),
        required_services=("SettingsService",),
    )
    registry.register(
        DEST_SETTINGS_DASHBOARD,
        lambda: _build_dashboard_full(hub),
        required_services=(),
    )


def register_all_destinations(registry: ViewRegistry, hub: ServiceHub) -> ViewRegistry:
    """全量目的地装配入口（M6 导航全量切换出口）。

    顺序：阅读组(M2/M4) → 书源组(M3) → 工具组(M5) → 设置组(M6 占位)，
    最后 :meth:`ViewRegistry.attach_navigation_meta` 贴分组/标题元数据。
    幂等：重复调用以最后一次注册为准，attach 幂等。
    """
    from app.views.reading import register_destinations as _reg_reading
    from app.views.source import register_destinations as _reg_source
    from app.views.tools import register_destinations as _reg_tools

    _reg_reading(registry, hub)
    _reg_source(registry, hub)
    _reg_tools(registry, hub)
    _register_settings_destinations(registry, hub)
    registry.attach_navigation_meta()
    return registry


# --------------------------------------------------------------------------- #
# 分组页签容器 / 导航容器
# --------------------------------------------------------------------------- #
class SectionView(ft.Column):
    """一个分组 = 侧栏一个入口，组内目的地作为页顶 Tab 共存。

    照旧单体的布局（``old_code/app/views/book_source_view.py`` 的
    管理/导入/检测 页签、``settings_view.py`` 的外观/阅读/快捷键 页签）：相关功能
    收在同一页的页签里，而不是各占侧栏一行。

    与「切目的地」有两处关键差异：

    - 子视图**惰性装配**（首次切到该页签才走工厂），装配后不再重建；
    - 切页签**不退订控制器**：在途的搜索/扫描任务照常跑完，切回来就是最新结果
      （因此不需要 Legado 的 ``workingState`` 暂停/恢复，见对齐文档）。
      退订由 :meth:`NavigationHost._teardown` 在离开整个分组时统一执行。
    """

    def __init__(self, group: str, tabs: list[tuple[str, str]], build) -> None:
        super().__init__(expand=True, spacing=0)
        self.group = group
        self._dests = [dest for dest, _title in tabs]
        self._build = build
        self._views: dict[str, object] = {}
        #: 用户点页签时通知外壳同步 current（目的地已由外壳装配，视图不持导航）
        self.on_tab_selected = None       # (destination: str) -> None

        tab_bar = ft.TabBar(tabs=[
            ft.Tab(label=title, icon=_ICONS.get(dest, ft.Icons.CIRCLE))
            for dest, title in tabs])
        self._pages = [ft.Container(expand=True) for _ in self._dests]
        self._tabs = ft.Tabs(
            length=len(self._dests), selected_index=0, expand=True,
            on_change=self._on_change,
            content=ft.Column(spacing=0, expand=True, controls=[
                tab_bar, ft.TabBarView(expand=True, controls=self._pages),
            ]))
        self.controls = [self._tabs]

    # ------------------------------------------------------------------ #
    @property
    def active(self) -> str:
        """当前页签的目的地。"""
        return self._dests[self._tabs.selected_index]

    @property
    def current_view(self):
        return self._views.get(self.active)

    @property
    def controller(self):
        """当前页签视图的控制器（外壳回查跨视图动作）。"""
        return getattr(self.current_view, "controller", None)

    def views(self) -> list:
        """本组已装配的子视图，按页签顺序（供外壳离开分组时统一退订）。"""
        return [self._views[dest] for dest in self._dests if dest in self._views]

    def select(self, destination: str) -> None:
        """切到组内某页签并惰性装配其视图；不在本组则报错。"""
        index = self._dests.index(destination)
        if self._tabs.selected_index != index:
            self._tabs.selected_index = index
        self._mount(index)

    # ------------------------------------------------------------------ #
    def _on_change(self, e) -> None:
        index = int(e.data)
        self._mount(index)
        if self.on_tab_selected is not None:
            self.on_tab_selected(self._dests[index])

    def _mount(self, index: int) -> None:
        dest = self._dests[index]
        if dest in self._views:
            return
        view = self._build(dest)
        self._views[dest] = view
        self._pages[index].content = view


class NavigationHost:
    """导航外壳：侧栏/底栏三个顶级入口，内容区切页签组或整页视图。

    Parameters
    ----------
    page : flet.Page | None
        真实页面（运行期用于 update）；无头测试传 None。
    registry : ViewRegistry
        已 attach 导航元数据的注册表。
    width : float | None
        注入窗口宽度替身（无头测试用）；None 时运行期读 ``page.width``。
    initial_destination : str
        首屏目的地，缺省 :data:`DEFAULT_DESTINATION`。
    """

    def __init__(self, page, registry: ViewRegistry, *,
                 width: float | None = None,
                 initial_destination: str | None = None,
                 shelf_probe=None, background: str | None = None) -> None:
        """``shelf_probe``：书架上有没有书（阅读组落地判定，见 :meth:`_anchor`）。

        视图层不许 import 服务（红线 §7.5），所以这个判定由装配根
        （:func:`build_app_shell` / 测试）注入的可调用对象提供；None 表示探不到，
        此时阅读组按 NAV_META 顺序落「找书」。

        ``background``：全应用一层背景图的路径（空 = 没有）。外壳只认路径，
        谁改了偏好由装配根回调 :meth:`set_background`（同样不许 import 服务）。
        """
        self.page = page
        self.registry = registry
        self._width = width
        self._shelf_probe = shelf_probe
        #: 背景图路径（"" = 没有）；改它只经 :meth:`set_background`。
        self._background = str(background or "")
        self.current: str | None = None
        #: 最近一次开读派发的 future（无头测试可 await/回查；运行期 fire-and-forget）。
        self._pending_open = None

        # 分组 → 组内页签（NAV_META 声明顺序）；分组顺序即侧栏自上而下的顺序
        self._grouped = registry.grouped_destinations()
        self._groups: list[str] = [group for group, _items in self._grouped]
        self._tabs_of: dict[str, list[tuple[str, str]]] = {
            group: list(items) for group, items in self._grouped}
        #: 目的地 → 所属分组（含整页目的地，供侧栏高亮反查）
        self._dest_group: dict[str, str] = {
            dest: group for group, items in self._grouped for dest, _t in items}
        #: 每组最后停留的页签：再点该入口时回到上次看的位置（旧架构体验）
        self._last_tab: dict[str, str] = {}

        self.content = ft.Container(expand=True)  # 内容区：页签组/整页视图挂载点
        self._section: SectionView | None = None
        self._standalone = None                   # 整页目的地当前视图（阅读器/工具子页）

        entries = [ft.NavigationRailDestination(
            icon=_GROUP_ICONS.get(group, _FALLBACK_ICON), label=group)
            for group in self._groups]
        self._rail = ft.NavigationRail(
            destinations=entries, selected_index=0, extended=True,
            use_indicator=True, on_change=self._on_rail_change)
        self._bar = ft.NavigationBar(
            destinations=[ft.NavigationBarDestination(
                icon=_GROUP_ICONS.get(group, _FALLBACK_ICON), label=group)
                for group in self._groups],
            selected_index=0, on_change=self._on_bar_change)

        self.root = self.render()
        self.show(initial_destination or self._anchor(GROUP_READING))

    # -- 工具 ------------------------------------------------------------ #
    def _is_narrow(self) -> bool:
        width = self._width
        if width is None and self.page is not None:
            width = getattr(self.page, "width", None)
        return is_narrow_width(width)

    def render(self):
        """根控制：宽/窄窗布局，外面按需垫一层全应用背景图。"""
        layout = self._render_layout()
        if not self._background:
            return layout
        # 背景是**半透明地压在主题底色上**，不是"图 + 一层黑纱"：
        # DecorationImage 的 opacity 让它跟窗口自己的 scaffold 底色合成，所以浅色面
        # 仍是浅底深字、深色面仍是深底浅字，两套主题都不用认当前是哪一面。
        # 反过来若在上面盖一层固定颜色的压暗层，就得知道系统此刻在亮面还是暗面——
        # 装配根在 Python 侧问不到（theme_mode 可以是 SYSTEM）。
        # 合成比例见 :data:`_BACKGROUND_OPACITY`。
        return ft.Stack(expand=True, controls=[
            ft.Container(expand=True, image=ft.DecorationImage(
                src=self._background, fit=ft.BoxFit.COVER,
                opacity=_BACKGROUND_OPACITY)),
            layout,
        ])

    def _render_layout(self):
        """按当前宽/窄窗决定根布局（Rail 行 / Bar 列）。"""
        if self._is_narrow():
            return ft.Column([
                ft.Container(self.content, expand=True),
                self._bar,
            ], expand=True, spacing=0)
        return ft.Row([
            self._rail,
            ft.VerticalDivider(width=1, opacity=0.5),
            ft.Container(self.content, expand=True),
        ], expand=True, spacing=0)

    def rebuild(self):
        """宽/窄窗切换时调用：换根布局但保留当前内容视图（不重建目的地）。"""
        self.root = self.render()
        self._sync_selected()
        self._notify_band()
        return self.root

    def _notify_band(self, view=None) -> None:
        """把"此刻是宽窗还是窄窗"推给内容视图（只推实现了 ``on_band_change`` 的）。

        为什么要由外壳推、不让视图自己读 ``page.width``：需要换形状的页要的不是
        "某个时刻的宽度"，而是**宽度跨过档位**这一个时机——第七十八轮实测拖窗 30 拍
        Python 只收到 1–4 次 ``on_resize``（客户端自己合掉了），而外壳正是在这一拍
        换根；视图挂在同一拍上拿通知，形状就不会和外壳的档位错开。
        没实现钩子的视图一次调用都不会多花（getattr 兜空）。
        """
        target = view if view is not None else self.current_view()
        notify = getattr(target, "on_band_change", None)
        if notify is None:
            return
        try:
            notify(self._is_narrow())
        except Exception:      # 一个页的降级坏了不该拖垮外壳重建
            logger.warning("窄窗降级失败：%s", type(target).__name__, exc_info=True)

    # ------------------------------------------------------------------ #
    # 全应用背景图（装配根在偏好变更时调用）
    # ------------------------------------------------------------------ #
    @property
    def background(self) -> str:
        return self._background

    def set_background(self, path: str | None) -> None:
        """换/去掉那一层背景，并把新根挂回页面。

        路径没变时**什么都不做**：这条线会被每次偏好写入打到，重建整棵根树代价大，
        而且会把当前视图的在途渲染一起掀掉。
        """
        next_path = str(path or "")
        if next_path == self._background:
            return
        self._background = next_path
        self.root = self.render()
        self._sync_selected()
        self._repaint_root()

    def _repaint_root(self) -> None:
        """把 ``self.root`` 重新塞回页面（与 :func:`build_app_shell` 的缩放重建同一条路）。"""
        if self.page is None:
            return
        try:
            self.page.controls.clear()
            self.page.add(self.root)
            self.page.update()
        except Exception:  # 无头/已关窗：没有可刷新的窗口，不算失败
            pass

    # ------------------------------------------------------------------ #
    # 内容区当前生效的视图
    # ------------------------------------------------------------------ #
    def current_view(self):
        """内容区当前视图：页签组取当前页签，整页取整页视图。"""
        if self._section is not None:
            return self._section.current_view
        return self._standalone

    def current_controller(self):
        return getattr(self.current_view(), "controller", None)

    # -- 导航动作 -------------------------------------------------------- #
    def show(self, destination: str) -> None:
        """切换目的地：组内目的地切页签，整页目的地独占内容区。

        未注册目的地直接报错（不可达目的地红线）。只有离开整个分组/整页时才
        退订旧视图（:meth:`_teardown`，Controller / dashboard 事件退订）；同组内
        切页签不退订，在途任务继续跑。
        """
        route = self.registry.get(destination)  # KeyError：不可达目的地红线
        if route.hidden:
            if self.current == destination and self._standalone is not None:
                # 同一整页目的地重复 show：保留现视图。重建会把上一趟在途的
                # open_book 连视图一起摘掉（控制器 _view 变 None），慢源开书时
                # 再点一次就是白屏。
                return
            self._teardown()
            view = route.factory()
            self._wire_view(view)
            self._section = None
            self._standalone = view
            self.content.content = view
        else:
            if self._section is not None and self._section.group != route.group:
                self._teardown()
            if self._section is None:
                section = SectionView(route.group, self._tabs_of[route.group],
                                      self._build_child)
                section.on_tab_selected = self._on_tab_selected
                self._standalone = None
                self.content.content = section
                self._section = section
            self._section.select(destination)
        self.current = destination
        if not route.hidden:
            # 只记页签目的地：整页视图（阅读器）不该被记成"该组上次停留的页签"，
            # 否则从侧栏回到「阅读」会丢掉开读前看的那一签。
            self._last_tab[route.group] = destination
        self._sync_selected()
        self._notify_band()        # 新装配/切回的页先拿到档位，再谈它自己的形状
        if self.page is not None:
            self.page.update()

    def _build_child(self, destination: str):
        """页签子视图装配：工厂造视图 + 接跨视图动作与 FilePicker。"""
        view = self.registry.get(destination).factory()
        self._wire_view(view)
        return view

    def _on_tab_selected(self, destination: str) -> None:
        """用户点页签：只更新当前目的地与分组记忆，不重建任何东西。"""
        self.current = destination
        group = self._dest_group.get(destination)
        if group is not None:
            self._last_tab[group] = destination
        self._notify_band()        # 切到一个"上一档宽度下建好"的页：补推一次档位
        if self.page is not None:
            self.page.update()

    def _anchor(self, group: str) -> str:
        """分组入口的落地页签：上次停留的那个 > 分组落地规则 > 组内第一个。

        「阅读」组的规则是**架上有书就先看书架**：进阅读是要读书，不是要逛站，
        空架才退到找书。书架状态经注入的 :attr:`_shelf_probe` 问（视图层不许
        import 服务，红线 §7.5），探不到/报错都按组内第一个页签落地。
        """
        tabs = [dest for dest, _t in self._tabs_of.get(group, ())]
        last = self._last_tab.get(group)
        if last is not None and last in tabs:
            return last
        if DEST_FIND_BOOKS in tabs and DEST_BOOKSHELF in tabs:
            return DEST_BOOKSHELF if self._shelf_has_books() else DEST_FIND_BOOKS
        return tabs[0] if tabs else DEFAULT_DESTINATION

    def _shelf_has_books(self) -> bool:
        if self._shelf_probe is None:
            return False
        try:
            return bool(self._shelf_probe())
        except Exception:
            logger.warning("书架探测失败，阅读组落地退回找书", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # 视图装配/退场（原 show() 内联逻辑，页签子视图与整页视图共用）
    # ------------------------------------------------------------------ #
    def _wire_view(self, view) -> None:
        """新视图接入导航：跨页跳转回调槽、开读槽、FilePicker 服务挂载。"""
        # 工具子页跳转绑定：带 on_open_destination 回调槽的视图（更多工具入口页）
        # 由本容器注入导航动作（视图不 import services，也不持有导航）。
        if hasattr(view, "on_open_destination"):
            view.on_open_destination = self.show
        # 跨视图导航动作绑定：controller.nav_actions = {动作名: 目的地}，
        # 把对应回调槽绑定为导航切换（书源列表 → 导入/编辑器/检测等）。
        controller = getattr(view, "controller", None)
        nav_actions = getattr(controller, "nav_actions", None)
        if isinstance(nav_actions, dict):
            for attr, dest in nav_actions.items():
                if getattr(controller, attr, None) is None:
                    setattr(controller, attr,
                            lambda *payload, d=dest: self._open_nav_action(d, payload))
        # 跨视图开读接线：找书/书架控制器声明了 on_open_reader 槽但历史上无人赋值
        # （开读死链）。这里统一绑定为「切到 reader/manga 目的地 + 经 ui_call
        # 派发目标控制器 open_book」。两处调用签名不同（找书 (source, book)、
        # 书架 (record,)），_open_reader_book 内归一。
        if controller is not None and hasattr(controller, "on_open_reader") \
                and getattr(controller, "on_open_reader", None) is None:
            controller.on_open_reader = self._open_reader_book
        # FilePicker 挂载：视图暴露 file_picker/image_picker 的（导入页/二维码页），
        # append 到 page.services（Flet 0.86 官方 Service 通道，旧 app.py 同款）。
        # 绝不能塞 overlay——那会走普通控件通道，客户端报 "Unknown control"。
        services = self._services()
        if services is None:
            return
        for attr in ("file_picker", "image_picker"):
            picker = getattr(view, attr, None)
            if picker is not None and picker not in services:
                try:
                    services.append(picker)
                except Exception:
                    pass

    def _unwire_view(self, view) -> None:
        """视图退场：退订控制器事件 + 摘掉 FilePicker。"""
        # A1：切页退订——先调视图控制器的 detach（退订 BaseController._subs），
        # 再调视图自身暴露的 detach（service_dashboard 把 view.detach 接到 controller.detach，
        # 二者同一方法，按 (func, self) 去重避免重复触发 on_detach）。
        controller = getattr(view, "controller", None)
        seen: set[tuple] = set()
        for detach in (getattr(controller, "detach", None),
                       getattr(view, "detach", None)):
            if not callable(detach):
                continue
            key = (getattr(detach, "__func__", None),
                   getattr(detach, "__self__", None))
            if key in seen:
                continue
            seen.add(key)
            try:
                detach()
            except Exception:  # 退订幂等：个别失败不阻断切页
                pass
        # A4：FilePicker 只增不减修复——离开视图时把其 file_picker/image_picker
        # 从 page.services 摘除（存在才 remove），避免反复切页累积 Service 实例。
        services = self._services()
        if services is None:
            return
        for attr in ("file_picker", "image_picker"):
            picker = getattr(view, attr, None)
            if picker is not None:
                try:
                    services.remove(picker)
                except (ValueError, AttributeError):
                    pass

    def _teardown(self) -> None:
        """离开分组/整页：退订其下已装配的全部视图。"""
        views: list = []
        if self._standalone is not None:
            views.append(self._standalone)
        if self._section is not None:
            views += self._section.views()
        for view in views:
            self._unwire_view(view)
        self._section = None
        self._standalone = None

    def _services(self):
        if self.page is None:
            return None
        return getattr(self.page, "services", None)

    # ------------------------------------------------------------------ #
    # 跨视图开读接线：找书 (source, book) / 书架 (record,) 归一后切 reader/manga
    # ------------------------------------------------------------------ #
    @staticmethod
    def _book_kind(book) -> str:
        if isinstance(book, dict):
            return str(book.get("kind") or "")
        return str(getattr(book, "kind", "") or "")

    def _open_nav_action(self, dest: str, payload: tuple) -> None:
        """带载荷的跨页动作：先切目的地，再把载荷注入目标控制器。

        旧绑定把槽位做成 ``lambda d=dest: self.show(d)``，载荷字典被当成目的地
        传进 ``registry.get`` 直接崩（书源列表点「编辑」整页报错）；目标控制
        器按约定暴露 ``load_record``（或历史名 ``load_source``）接收载荷。
        """
        self.show(dest)
        if not payload:
            return
        controller = self.current_controller()
        if controller is None:
            return
        loader = getattr(controller, "load_record", None) \
            or getattr(controller, "load_source", None)
        if loader is None:
            return
        try:
            loader(*payload)
        except Exception as exc:
            logger.warning("目的地 %s 载荷注入失败：%s", dest, exc)

    def _open_reader_book(self, *args) -> None:
        """找书/书架 on_open_reader 槽统一实现：切目的地并派发 open_book。

        找书控制器调用 ``(source, book, shelve)``（试读 shelve=False）；书架控制
        器调用 ``(record,)``（已在架，不再入架）。此处归一，image 类型书切
        manga 目的地。open_book 经目标控制器 ui_call 派发，不阻塞 UI。
        """
        shelve = True
        if len(args) == 3:
            source, book, shelve = args
        elif len(args) == 2:
            source, book = args
        else:
            source, book = None, args[0]
            shelve = False  # 书架记录本就入架，重开不再触发入架写回
        dest = DEST_MANGA_READER if self._book_kind(book) == "image" else DEST_READER
        self.show(dest)
        controller = self.current_controller()
        if controller is not None and hasattr(controller, "open_book"):
            self._pending_open = controller.ui_call(
                controller.open_book(source, book, shelve=shelve))

    def _sync_selected(self) -> None:
        """侧栏/底栏高亮当前目的地**所属分组**（页签内的目的地不单独占位）。"""
        if self.current is None:
            return
        group = self._dest_group.get(self.current)
        if group is None:  # 整页目的地：按注册表上的分组高亮其归属
            group = self.registry.get(self.current).group
        if group in self._groups:
            index = self._groups.index(group)
            self._rail.selected_index = index
            self._bar.selected_index = index

    def _on_rail_change(self, e) -> None:
        self._on_nav_changed(e.control.selected_index)

    def _on_bar_change(self, e) -> None:
        self._on_nav_changed(e.control.selected_index)

    def _on_nav_changed(self, index: int) -> None:
        if not (0 <= index < len(self._groups)):
            return
        dest = self._anchor(self._groups[index])
        if dest != self.current:
            self.show(dest)

    # ------------------------------------------------------------------ #
    # 全局键盘（M6 漏接的 page.on_keyboard_event：旧 app.py 只在阅读器正文页
    # 分发；新架构分发到当前内容视图的 on_key 回调槽，其余视图无 on_key 即忽略）
    # ------------------------------------------------------------------ #
    def _on_keyboard_event(self, event) -> None:
        view = getattr(self.current_view(), "on_key", None)
        if callable(view):
            view(event)


# --------------------------------------------------------------------------- #
# main 入口装配
# --------------------------------------------------------------------------- #
def build_app_shell(page: ft.Page, hub: ServiceHub, shelf_probe=None,
                    key_filter=None, background: str | None = None) -> ft.Control:
    """M6 新入口：装配全量目的地 → 挂导航容器 → 首屏落地阅读组。

    ``shelf_probe``（可调用，返回「架上有书」）透传给 :class:`NavigationHost`：
    首屏与点「阅读」入口都按它决定落书架还是落找书。视图层不 import 服务，
    探测由装配根（main.py）提供；不传则落找书。

    ``key_filter``（可调用，收一个 ``KeyboardEvent``，返回 bool）是**全局按键的
    第一道闸**：老板键这类"命中就不许再往下分发"的动作走它。返回 True 表示这一拍
    已被吃掉。同样由装配根注入——视图层拿不到 :class:`BossKeyService`（红线 §7.5）。

    ``background``：首屏的**全应用背景图**路径（空 = 没有）。外壳只认路径；之后
    用户换图由装配根订阅偏好、回调 :meth:`NavigationHost.set_background`（同一红线）。

    旧单体（``old_code/app/app.py`` 的 3 个顶级入口）不再作为默认入口；其文件
    仍在（M7 删），导航只指向注册表里的新视图（契约 §4）。
    """
    import asyncio

    from app.core.engine_worker import inject_interaction_loop
    from app.core.registry import default_registry

    page.title = "Legado 书源管理工具"  # 对齐旧实现窗口标题（默认 "Flet"）
    global _APP_PAGE
    _APP_PAGE = page

    # F7：Flet 回调运行在 UI 的 asyncio loop 上——在此显式把 UI loop 绑为各
    # LegadoReadingEngine 的 interaction_loop，替代旧 EngineProxy._dispatch 的
    # 首次 await 自动绑定（灰色地带）。无头场景无 running loop 时 RuntimeError 兜底，
    # 注入跳过（引擎降级到当前 loop，语义不变）。
    try:
        ui_loop = asyncio.get_running_loop()
    except RuntimeError:
        ui_loop = None
    inject_interaction_loop(hub, ui_loop)

    # 外壳句柄登记给装配根：背景图偏好变了要有人能推到这一层（见 current_host）
    global _APP_HOST
    registry = register_all_destinations(default_registry(), hub)
    host = NavigationHost(page, registry, shelf_probe=shelf_probe,
                          background=background)
    _APP_HOST = host
    page.add(host.root)
    # 阅读器快捷键接线（M6 漏接，M7 后补回；分发到当前视图 on_key）。
    # key_filter 在最前面：老板键命中的那一拍不该再让正文页当成翻页。
    def _on_key(event):
        if key_filter is not None and key_filter(event):
            return
        host._on_keyboard_event(event)

    page.on_keyboard_event = _on_key

    # 窗口跨阈值缩放时：宽/窄窗切换重建外壳（内容视图保留，不重建）。
    #
    # 只在这一拍**跨过** :data:`NARROW_WIDTH_THRESHOLD` 时才重建，同档位内直接返回。
    # 第七十八轮实测：拖窗期间 Python 侧只收到 1–4 次 ``on_resize``（客户端合掉了），
    # 但**每一次**处理器都要 0.5–2.1 s——因为 `rebuild()+_repaint_root()` 是把整棵根树
    # 重新序列化后发给客户端（书源表 917 条时约 9200 个控件）。不跨阈值也照发的话，
    # 拖窗过程中会随机冻住界面循环半秒到两秒（心跳同期落后 max 545 ms）。
    # 形状没变的那一拍重发形状相同的树，除了冻页面什么都不换来。
    last_narrow = host._is_narrow()

    def _on_resize(e=None):
        nonlocal last_narrow
        now_narrow = host._is_narrow()
        if now_narrow == last_narrow:
            return
        last_narrow = now_narrow
        host.rebuild()
        host._repaint_root()

    page.on_resize = _on_resize
    return host.root
