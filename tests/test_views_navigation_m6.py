"""M6 导航全量切换测试（契约 §4；设计 §7.3）。

不依赖真实 Flet 窗口与网络：ft 控件可无窗口实例化；Controller 用 stub hub /
服务替身离线构造。覆盖：

1. 全量目的地注册完整性（NAV_META 每个键都已注册）；
2. 顶级入口 = 分组（书源/阅读/配置，旧架构三项布局）与组内页签成员；
3. 每个目的地工厂可离线构造视图；
4. 宽窗 NavigationRail / 窄窗底部 NavigationBar 分支（注入 width 替身）；
5. 不可达目的地报错（registry.get / host.show 未注册目的地）；
6. 注册表幂等（重复 register_all_destinations / attach_navigation_meta）；
7. 页签容器语义：子视图惰性装配、切签不退订、离开分组才退订、入口回到上次页签；
8. show() 切换内容区（页签组 / 整页目的地）；
9. 设置组占位工厂（外观 / ServiceDashboard）可构造。

既有 M2/M3/M5 注册断言由各自测试文件守门，本文件不复刻。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft

from app.core.hub import ServiceHub
from app.core.lifecycle import ServiceState, ServiceStatus
from app.core.registry import (DEST_BOOKSHELF, DEST_FIND_BOOKS, DEST_MANGA_READER,
                               DEST_READER, DEST_RSS,
                               DEST_SETTINGS_APPEARANCE,
                               DEST_SETTINGS_DASHBOARD, DEST_SETTINGS_SHORTCUTS,
                               DEST_SOURCE_CHECK,
                               DEST_SOURCE_EDITOR, DEST_SOURCE_IMPORT,
                               DEST_SOURCE_LIST, DEST_SOURCE_REPAIR,
                               DEST_TOOLS_EXPORT,
                               DEST_TOOLS_OVERVIEW, GROUP_CONFIG, GROUP_ORDER,
                               GROUP_READING, GROUP_SETTINGS, GROUP_SOURCE,
                               NAV_META, SETTINGS_DESTINATIONS, ViewRegistry)

from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.download_service import DownloadService
from app.services.reading.image_book_service import ImageBookService
from app.services.reading.reading_service import ReadingService

from app.views.navigation import (DEFAULT_DESTINATION, NavigationHost, SectionView,
                                   is_narrow_width, register_all_destinations)
from app.views.reading.bookshelf_view import BookshelfView
from app.views.reading.find_books_view import FindBooksView
from app.views.source.code_editor_view import CodeEditorView


# --------------------------------------------------------------------------- #
# 离线替身：不启真实 EngineHub / ServiceHub
# --------------------------------------------------------------------------- #
class _FakeBus:
    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

        def unsubscribe():
            if (event_type, handler) in self.subscriptions:
                self.subscriptions.remove((event_type, handler))
        return unsubscribe

    def publish(self, event):
        return []


class _FakeHub:
    """按类型返回替身服务；未注册类型自然抛 KeyError（与真 hub.get 一致）。"""

    def __init__(self, services: dict | None = None):
        self.bus = _FakeBus()
        self._services = services or {}

    def get(self, stype):
        return self._services[stype]

    def status_report(self):
        return [
            ServiceStatus(service_id="ReadingService", state=ServiceState.RUNNING),
        ]

    def service_by_id(self, service_id: str):
        """按 id 返回服务实例（M6 仪表盘完整版依赖；hub 自身行返回 None）。"""
        for stype, inst in self._services.items():
            if getattr(stype, "name", stype.__name__) == service_id or \
                    getattr(inst, "name", None) == service_id:
                return inst
        return None

    def restartable(self, service_id: str) -> bool:
        """与真 ServiceHub 同表面：查得到实例的行才可重启。

        仪表盘画每一行都要问一次（重启按钮的可用性），替身缺这个符号就会
        AttributeError——替身必须跟着真实表面走，不然测出来的红是探针自造的。
        """
        return self.service_by_id(service_id) is not None

    async def restart_service(self, service_id: str) -> list[str]:
        return [service_id] if self.restartable(service_id) else []


def _stub_hub() -> _FakeHub:
    """阅读域服务替身（async 方法用 AsyncMock 记录调用，attach 不联网）。"""
    reading = SimpleNamespace(
        open_book=AsyncMock(return_value=SimpleNamespace(chapters=[], position=None)),
        load_content=AsyncMock(return_value=SimpleNamespace(blocks=[])),
        load_chapters=AsyncMock(return_value=[]),
        save_progress=AsyncMock(),
        get_reader_preference=AsyncMock(return_value=None),
        save_reader_preference=AsyncMock(),
        search_books=AsyncMock(return_value=[]),
        stop_search=AsyncMock(),
        switch_source=AsyncMock(return_value=[]),
        apply_source_switch=AsyncMock(return_value=True),
        prefetch_chapters=AsyncMock(),
        stop_prefetch=lambda: None,
        current_session=SimpleNamespace(chapters=[], position=None),
    )
    bookmarks = SimpleNamespace(
        list_bookmarks=AsyncMock(return_value=[]),
        add_bookmark=AsyncMock(return_value=1),
        remove_bookmark=AsyncMock(return_value=True),
    )
    content_search = SimpleNamespace(
        search=AsyncMock(return_value=[]),
        ensure_index=AsyncMock(return_value=0),
    )
    download = SimpleNamespace(
        enqueue=lambda *a, **k: "task-1",
        cancel=lambda *a: True,
    )
    image_book = SimpleNamespace(
        chapter_images=AsyncMock(return_value=[]),
    )
    return _FakeHub({
        ReadingService: reading, BookmarkService: bookmarks,
        ContentSearchService: content_search, DownloadService: download,
        ImageBookService: image_book,
    })


def _populated_registry() -> tuple[ViewRegistry, _FakeHub]:
    hub = _stub_hub()
    registry = ViewRegistry()
    register_all_destinations(registry, hub)
    return registry, hub


# --------------------------------------------------------------------------- #
class NavigationMetadataTests(unittest.TestCase):
    """全量目的地注册完整性 / 分组归属 / 分组顺序。"""

    def test_all_nav_meta_destinations_registered(self):
        registry, _hub = _populated_registry()
        missing = [d for d in NAV_META if registry.try_get(d) is None]
        self.assertEqual(missing, [], f"NAV_META 目的地未注册: {missing}")

    def test_group_assignment_and_order(self):
        registry, _hub = _populated_registry()
        grouped = registry.grouped_destinations()
        groups = [g for g, _items in grouped]
        # 三组（旧实现：书源 → 阅读 → 配置）且按 GROUP_ORDER 顺序
        self.assertEqual(groups, list(GROUP_ORDER))
        self.assertEqual(groups, [GROUP_SOURCE, GROUP_READING, GROUP_CONFIG])
        # 逐目的地分组归属正确
        for group, items in grouped:
            for dest, _title in items:
                self.assertEqual(NAV_META[dest][0], group,
                                 f"{dest} 应属于 {group}")
        members = {group: [d for d, _t in items] for group, items in grouped}
        # 书源组五个目的地合成「书源」一页的页签（旧 BookSourceView 同款）
        self.assertEqual(members[GROUP_SOURCE], [
            DEST_SOURCE_LIST, DEST_SOURCE_IMPORT, DEST_SOURCE_CHECK,
            DEST_SOURCE_EDITOR, DEST_SOURCE_REPAIR])
        # 阅读组：找书 + 书架 + 订阅三个页签（阅读器/漫画是整页目的地）
        self.assertEqual(members[GROUP_READING], [
            DEST_FIND_BOOKS, DEST_BOOKSHELF, DEST_RSS])
        # 配置组：更多工具 + 外观 + 快捷键 + 服务（8 个工具子页整页跳转，不进页签）。
        # 快捷键页是第七十三轮新加的：阅读器那七个动作以前只存在库里、界面没入口，
        # 用户报的就是这句"打开软件没找到按键设置的位置"。
        self.assertEqual(members[GROUP_CONFIG], [
            DEST_TOOLS_OVERVIEW, DEST_SETTINGS_APPEARANCE,
            DEST_SETTINGS_SHORTCUTS, DEST_SETTINGS_DASHBOARD])

    def test_every_factory_constructs_view(self):
        registry, _hub = _populated_registry()
        for dest in NAV_META:
            route = registry.get(dest)
            view = route.factory()
            self.assertIsNotNone(view, f"{dest} 工厂返回空")
            self.assertNotEqual(route.title, "", f"{dest} 缺标题")
            self.assertNotEqual(route.group, "", f"{dest} 缺分组")


class NarrowWidthTests(unittest.TestCase):
    """窄窗判定纯函数 + 宽/窄窗分支构造（注入 width 替身）。"""

    def test_is_narrow_width_threshold(self):
        self.assertFalse(is_narrow_width(None))     # 无头按宽窗
        self.assertFalse(is_narrow_width(1200.0))
        self.assertTrue(is_narrow_width(400.0))
        self.assertTrue(is_narrow_width(719.0))
        self.assertFalse(is_narrow_width(720.0))

    def test_wide_window_uses_navigation_rail(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0)
        self.assertIsInstance(host.root, ft.Row)
        self.assertTrue(any(isinstance(c, ft.NavigationRail) for c in host.root.controls))
        self.assertFalse(any(isinstance(c, ft.NavigationBar) for c in host.root.controls))

    def test_narrow_window_uses_navigation_bar(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=400.0)
        self.assertIsInstance(host.root, ft.Column)
        self.assertTrue(any(isinstance(c, ft.NavigationBar) for c in host.root.controls))
        self.assertFalse(any(isinstance(c, ft.NavigationRail) for c in host.root.controls))


class RailEntryTests(unittest.TestCase):
    """侧栏只有三个顶级入口（旧架构布局）+ show 切换内容区。"""

    def _rail(self, host):
        return next(c for c in host.root.controls if isinstance(c, ft.NavigationRail))

    def test_rail_has_one_entry_per_group(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0)
        rail = self._rail(host)
        self.assertEqual([d.label for d in rail.destinations], list(GROUP_ORDER))
        # 旧布局：入口就是分组本身，不再插 disabled 分组头占行
        self.assertFalse(any(d.disabled for d in rail.destinations))

    def test_rail_highlights_group_of_current_destination(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                               initial_destination=DEST_FIND_BOOKS)
        for dest in (DEST_BOOKSHELF, DEST_READER, DEST_MANGA_READER):
            host.show(dest)
            self.assertEqual(host._rail.selected_index,
                             list(GROUP_ORDER).index(GROUP_READING), f"{dest} 高亮错")
        host.show(DEST_SOURCE_EDITOR)
        self.assertEqual(host._rail.selected_index,
                         list(GROUP_ORDER).index(GROUP_SOURCE))
        host.show(DEST_TOOLS_EXPORT)
        self.assertEqual(host._rail.selected_index,
                         list(GROUP_ORDER).index(GROUP_CONFIG))

    def test_show_swaps_content(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                               initial_destination=DEST_FIND_BOOKS)
        self.assertEqual(host.current, DEST_FIND_BOOKS)
        host.show(DEST_SOURCE_LIST)
        self.assertEqual(host.current, DEST_SOURCE_LIST)
        self.assertIsNotNone(host.current_view())
        self.assertIn(host._rail.selected_index, range(len(host._groups)))


class SectionTabTests(unittest.TestCase):
    """页签容器语义（把相关功能合在一页）：惰性装配、切签不退订、整组退订。"""

    def _host(self, registry):
        return NavigationHost(page=None, registry=registry, width=1200.0,
                              initial_destination=DEST_FIND_BOOKS)

    def test_tabs_are_built_lazily(self):
        registry, _hub = _populated_registry()
        host = self._host(registry)
        self.assertIsInstance(host.content.content, SectionView)
        self.assertEqual([type(v).__name__ for v in host._section.views()],
                         ["FindBooksView"])
        host.show(DEST_BOOKSHELF)
        self.assertEqual([type(v).__name__ for v in host._section.views()],
                         ["FindBooksView", "BookshelfView"])
        self.assertIsInstance(host.current_view(), BookshelfView)

    def test_switching_tab_does_not_detach(self):
        """切签不退订：在途的搜索/扫描继续跑，切回来还是原状态。"""
        registry, _hub = _populated_registry()
        host = self._host(registry)
        first = host.current_controller()
        host.show(DEST_BOOKSHELF)
        host.show(DEST_FIND_BOOKS)
        self.assertIs(host.current_controller(), first, "切回来不该是新的控制器实例")
        self.assertIsNotNone(first._view, "切签不该 detach 掉找书控制器")

    def test_leaving_group_detaches_every_built_tab(self):
        registry, _hub = _populated_registry()
        host = self._host(registry)
        host.show(DEST_BOOKSHELF)
        controllers = [host._section.views()[0].controller,
                       host._section.views()[1].controller]
        host.show(DEST_SOURCE_LIST)
        self.assertTrue(all(c._view is None for c in controllers),
                        "离开分组必须退订组内已装配的全部页签")

    def test_rail_entry_returns_to_last_tab_of_group(self):
        registry, _hub = _populated_registry()
        host = self._host(registry)
        host.show(DEST_BOOKSHELF)
        host.show(DEST_SOURCE_LIST)
        host._on_nav_changed(list(GROUP_ORDER).index(GROUP_READING))
        self.assertEqual(host.current, DEST_BOOKSHELF)
        # 开读不打扰页签记忆：从阅读器回「阅读」还是回到书架
        host.show(DEST_READER)
        host._on_nav_changed(list(GROUP_ORDER).index(GROUP_READING))
        self.assertEqual(host.current, DEST_BOOKSHELF)

    def test_nav_action_jumps_to_tab_in_same_group_with_payload(self):
        """书源列表点「编辑」→ 同组切到「规则」页签并注入载荷，不重建视图。"""
        registry, _hub = _populated_registry()
        host = self._host(registry)
        host.show(DEST_SOURCE_LIST)
        list_controller = host.current_controller()
        host.show(DEST_SOURCE_EDITOR)
        editor = host.current_controller()
        loaded: list = []
        editor.load_record = loaded.append
        host.show(DEST_SOURCE_LIST)
        list_controller.on_open_editor({"url": "http://source"})
        self.assertEqual(host.current, DEST_SOURCE_EDITOR)
        self.assertIs(host.current_controller(), editor)
        self.assertEqual(loaded, [{"url": "http://source"}])


class WholePageRouteTests(unittest.TestCase):
    """整页目的地（hidden）：注册可达但不进侧栏也不进页签（route.hidden 与元数据一致）。"""

    def test_hidden_routes_flagged(self):
        registry, _hub = _populated_registry()
        for dest, entry in NAV_META.items():
            hidden = entry[2] if len(entry) == 3 else False
            route = registry.get(dest)
            self.assertEqual(route.hidden, hidden,
                             f"{dest} hidden 不一致: meta={hidden} route={route.hidden}")

    def test_tab_count_is_twelve_and_whole_page_is_twelve(self):
        registry, _hub = _populated_registry()
        tabs = sum(len(items) for _g, items in registry.grouped_destinations())
        # 页签：书源 5（管理/导入/检测/规则/自愈）+ 阅读 3（找书/书架/订阅）
        #       + 配置 4（更多工具/外观/快捷键/服务）
        # 整页：阅读器、漫画、8 个工具子页 + 网络桥设置页 + 朗读语音档页
        #       （tools.webbridge / tools.tts 后补，都不在 TOOLS_DESTINATIONS
        #        冻结元组里，所以这里必须跟着涨）
        whole_page = sum(1 for entry in NAV_META.values() if entry[2])
        self.assertEqual(tabs, 12)
        self.assertEqual(whole_page, 12)
        self.assertEqual(tabs + whole_page, len(NAV_META))

    def test_whole_page_destination_replaces_content_area(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                              initial_destination=DEST_FIND_BOOKS)
        host.show(DEST_READER)
        self.assertIsNone(host._section, "整页目的地不该留在页签组里")
        self.assertIs(host.current_view(), host._standalone)


class OpenReaderWiringTests(unittest.TestCase):
    """跨视图开读接线：找书/书架 on_open_reader → 切 reader/manga + 派发 open_book。"""

    def test_find_books_open_reader_switches_to_reader(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                               initial_destination=DEST_FIND_BOOKS)
        fb_controller = host.content.content.controller
        self.assertTrue(callable(fb_controller.on_open_reader))
        book = {"name": "测试书", "url": "https://x/y", "kind": "text"}
        fb_controller.on_open_reader(None, book)
        self.assertEqual(host.current, DEST_READER)
        reader_controller = host.content.content.controller
        if host._pending_open is not None:
            host._pending_open.result(timeout=5.0)
        self.assertTrue(
            reader_controller._reading.open_book.await_count >= 1,
            "reader controller 的 open_book 未被派发")

    def test_repeated_open_reader_keeps_the_same_reader_view(self):
        """连点两下开读：第二次 show 不许重建阅读器。

        重建会把第一趟在途的 open_book 连视图一起摘掉（控制器 ``_view`` 变
        None），界面停在详情页——慢源下就是"点了开始阅读没反应"。
        """
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                              initial_destination=DEST_FIND_BOOKS)
        fb_controller = host.content.content.controller
        book = {"name": "测试书", "url": "https://x/y", "kind": "text"}
        fb_controller.on_open_reader(None, book)
        view = host._standalone
        fb_controller.on_open_reader(None, book)
        self.assertIs(host._standalone, view, "第二次 show 把阅读器重建了")
        self.assertIs(view.controller._view, view, "阅读器视图已被摘掉，开书没地方挂")
        host._pending_open.result(timeout=5)

    def test_image_book_switches_to_manga(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                               initial_destination=DEST_FIND_BOOKS)
        fb_controller = host.content.content.controller
        book = {"name": "漫画书", "url": "https://x/m", "kind": "image"}
        fb_controller.on_open_reader(None, book)
        self.assertEqual(host.current, DEST_MANGA_READER)

    def test_bookshelf_open_reader_normalizes_record(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0,
                               initial_destination=DEST_BOOKSHELF)
        bs_controller = host.content.content.controller
        self.assertTrue(callable(bs_controller.on_open_reader))
        record = {"name": "续读书", "url": "https://z/q", "kind": "text"}
        bs_controller.on_open_reader(record)
        self.assertEqual(host.current, DEST_READER)
        if host._pending_open is not None:
            host._pending_open.result(timeout=5.0)


class UnreachableDestinationTests(unittest.TestCase):
    """不可达目的地报错（导航层红线）。"""

    def test_get_unknown_destination_raises(self):
        registry, _hub = _populated_registry()
        with self.assertRaises(KeyError):
            registry.get("no.such.destination")

    def test_host_show_unknown_raises(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0)
        with self.assertRaises(KeyError):
            host.show("no.such.destination")


class IdempotencyTests(unittest.TestCase):
    """注册表幂等：重复装配不报错、结果一致。"""

    def test_register_all_twice_idempotent(self):
        hub = _stub_hub()
        registry = ViewRegistry()
        register_all_destinations(registry, hub)
        first = set(registry.destinations())
        register_all_destinations(registry, hub)
        second = set(registry.destinations())
        self.assertEqual(first, second)
        # attach 再贴一次元数据不报错、标题/分组不丢
        registry.attach_navigation_meta()
        for dest in NAV_META:
            route = registry.get(dest)
            self.assertTrue(route.title and route.group)

    def test_attach_meta_on_unregistered_destination_raises(self):
        registry = ViewRegistry()
        registry.register("x.y", lambda: ft.Text("x"))
        with self.assertRaises(KeyError):
            # NAV_META 里全是已交付目的地，这里塞一个未注册的进去
            registry.attach_navigation_meta({"ghost.dest": (GROUP_READING, "幽灵")})


class SettingsPlaceholderTests(unittest.TestCase):
    """设置组占位工厂可构造（外观 / ServiceDashboard 占位）。"""

    def test_settings_placeholders_construct(self):
        registry, _hub = _populated_registry()
        for dest in (DEST_SETTINGS_APPEARANCE, DEST_SETTINGS_DASHBOARD):
            view = registry.get(dest).factory()
            # appearance 占位返回 ft.Column；dashboard 完整版返回页面级 ft.View——
            # 均为 Flet 控件，统一按 Control 断言（集成方替换完整版后放宽）
            self.assertIsInstance(view, ft.Control)

    def test_default_destination_is_find_books(self):
        self.assertEqual(DEFAULT_DESTINATION, DEST_FIND_BOOKS)



class ShelfFirstLandingTests(unittest.TestCase):
    """进「阅读」优先书架（用户诉求：进阅读是要读书，空架才去逛找书）。"""

    def _host(self, has_books: bool, boom: bool = False) -> NavigationHost:
        registry, _hub = _populated_registry()

        def probe() -> bool:
            if boom:
                raise RuntimeError("库读不动")
            return has_books

        return NavigationHost(page=None, registry=registry, width=1200.0,
                              shelf_probe=probe)

    def test_rail_entry_lands_on_bookshelf_when_not_empty(self):
        host = self._host(has_books=True)
        self.assertEqual(host.current, DEST_BOOKSHELF)
        host.show(DEST_SOURCE_LIST)
        host._on_nav_changed(host._groups.index(GROUP_READING))
        self.assertEqual(host.current, DEST_BOOKSHELF)

    def test_rail_entry_lands_on_find_books_when_shelf_empty(self):
        host = self._host(has_books=False)
        self.assertEqual(host.current, DEST_FIND_BOOKS)
        host.show(DEST_SOURCE_LIST)
        host._on_nav_changed(host._groups.index(GROUP_READING))
        self.assertEqual(host.current, DEST_FIND_BOOKS)

    def test_probe_failure_falls_back_to_find_books(self):
        """探测抛错不该把导航带崩：按 NAV_META 顺序落找书。"""
        host = self._host(has_books=True, boom=True)
        self.assertEqual(host.current, DEST_FIND_BOOKS)

    def test_last_tab_beats_the_shelf_rule(self):
        """记住的页签优先：用户看过找书，再点入口就该回找书。"""
        host = self._host(has_books=True)
        host.show(DEST_FIND_BOOKS)
        host.show(DEST_SOURCE_LIST)
        host._on_nav_changed(host._groups.index(GROUP_READING))
        self.assertEqual(host.current, DEST_FIND_BOOKS)

    def test_without_probe_first_tab_wins(self):
        registry, _hub = _populated_registry()
        host = NavigationHost(page=None, registry=registry, width=1200.0)
        self.assertEqual(host.current, DEST_FIND_BOOKS)


if __name__ == "__main__":
    unittest.main()
