"""M2 阅读域 UI 拆分冒烟测试（§7.2/§7.4/§7.5）。

不依赖真实 Flet 窗口与网络：ft 控件可无窗口实例化；Controller 用 stub hub / 服务
替身离线构造。覆盖：

1. 三视图（找书/书架/阅读器）+ 对话框/面板可离线构造；
2. Controller attach/detach 订阅退订对称（防跨页泄漏，§7.4）；
3. 换源对话框候选渲染（空态 + 命中行）；
4. 书签面板空态；
5. 书内搜索条触发 ContentSearchService 查询路径（book_url + keyword）；
6. 退出阅读收尾：试读暂存询问「放入书架？」，放入转正 / 不放入丢弃，普通书直接回书架；
7. import-lint：``app/views/reading/`` 全部 .py 零 ``app.services.`` 前缀命中。

旧行为基线由 tests/test_reader_ux.py（128 用例）守门，本文件不复刻其用例，
只做新结构冒烟。
"""
from __future__ import annotations

import asyncio
import inspect
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft

from app.core.events import DownloadProgressEvent
from app.core.registry import NAV_META, READING_DESTINATIONS, ViewRegistry
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.download_service import DownloadService, TaskInfo
from app.services.reading.reading_service import ReadingService, ReadingSession
from app.services.content.local_book_service import LOCAL_BOOK_SOURCE_URL
from app.services.sources.rule_repair_service import RuleRepairService
from app.services.sources.source_service import SourceService

from app.views.reading import (BookmarkPanel, BookshelfView, ContentSearchBar,
                               FindBooksView, LoginDialog, ReaderView,
                               SourceSwitchDialog, register_destinations)
from app.controllers.bookshelf_controller import BookshelfController
from app.controllers.find_books_controller import FindBooksController
from app.controllers.reader_controller import ReaderController


# --------------------------------------------------------------------------- #
# 离线替身：不启真实 EngineHub / ServiceHub
# --------------------------------------------------------------------------- #
class _FakeBus:
    """记录订阅的假 EventBus：subscribe 返回可幂等退订句柄。"""

    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

        def unsubscribe():
            if (event_type, handler) in self.subscriptions:
                self.subscriptions.remove((event_type, handler))

        return unsubscribe

    def published(self, event):
        return []


class _FakeHub:
    """按类型返回替身服务句柄的假 ServiceHub。"""

    def __init__(self, services: dict):
        self.bus = _FakeBus()
        self._services = services

    def get(self, stype):
        return self._services[stype]


def _stub_services():
    """构造阅读域服务替身（async 方法用 AsyncMock 记录调用）。"""
    reading = SimpleNamespace(
        open_book=AsyncMock(return_value=SimpleNamespace(
            chapters=[], position=None)),
        load_content=AsyncMock(return_value=SimpleNamespace(blocks=[])),
        load_chapters=AsyncMock(return_value=[]),
        save_progress=AsyncMock(),
        get_reader_preference=AsyncMock(return_value=None),
        save_reader_preference=AsyncMock(),
        search_books=AsyncMock(return_value=[]),
        stop_search=AsyncMock(),
        switch_source=AsyncMock(return_value=[]),
        apply_source_switch=AsyncMock(return_value=True),
        close_book=AsyncMock(),
        staged_record=lambda: None,
        promote_staged_book=AsyncMock(return_value=True),
        discard_staged_book=AsyncMock(return_value=True),
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
        rebuild_index=AsyncMock(return_value=0),
    )
    download = SimpleNamespace(
        enqueue=lambda *a, **k: "task-1",
        cancel=lambda *a: True,
    )
    return {ReadingService: reading, BookmarkService: bookmarks,
            ContentSearchService: content_search, DownloadService: download}


def _walk(control):
    """深度遍历控件树（0.86 的容器有的用 controls、有的用 content）。"""
    stack = [control]
    while stack:
        node = stack.pop()
        if not isinstance(node, ft.BaseControl):
            continue
        yield node
        stack.extend(getattr(node, "controls", None) or [])
        child = getattr(node, "content", None)
        if isinstance(child, (list, tuple)):
            stack.extend(child)
        elif child is not None:
            stack.append(child)


def _text_values(control) -> list[str]:
    return [str(node.value or "") for node in _walk(control)
            if isinstance(node, ft.Text)]


def _cancel_button(control):
    """找到这一行里的「取消」按钮（0.86 的 TextButton 文本在 .content，可能是裸字符串）。"""
    for node in _walk(control):
        if isinstance(node, ft.TextButton):
            label = node.content if isinstance(node.content, str) else \
                getattr(node.content, "value", "")
            if str(label or "") == "取消":
                return node
    return None


# --------------------------------------------------------------------------- #
class ReadingViewsSmokeTests(unittest.TestCase):
    """三视图 + 对话框/面板离线可构造。"""

    def test_three_views_construct_offline(self):
        self.assertIsInstance(FindBooksView(), ft.Column)
        self.assertIsInstance(BookshelfView(), ft.Column)
        self.assertIsInstance(ReaderView(), ft.Column)

    def test_dialogs_and_panels_construct_offline(self):
        dlg = SourceSwitchDialog({"name": "三体"})
        self.assertIsInstance(dlg, ft.AlertDialog)
        login = LoginDialog(SimpleNamespace(bookSourceName="书源A"))
        self.assertIsInstance(login, ft.AlertDialog)
        self.assertIsInstance(BookmarkPanel(), ft.Column)
        self.assertIsInstance(ContentSearchBar(), ft.Column)


class ControllerLifecycleTests(unittest.TestCase):
    """attach/detach 订阅退订对称（§7.4 防泄漏）。"""

    def _attach_detach(self, controller_cls, view):
        hub = _FakeHub(_stub_services())
        controller = controller_cls(hub)
        controller.attach(view)
        n_after_attach = len(hub.bus.subscriptions)
        self.assertGreater(n_after_attach, 0, f"{controller_cls.__name__} 应订阅事件")
        controller.detach()
        self.assertEqual(
            len(hub.bus.subscriptions), 0,
            f"{controller_cls.__name__} detach 后应全部退订")

    def test_find_books_controller_subscribe_unsubscribe_symmetric(self):
        self._attach_detach(FindBooksController, FindBooksView())

    def test_bookshelf_controller_subscribe_unsubscribe_symmetric(self):
        self._attach_detach(BookshelfController, BookshelfView())

    def test_reader_controller_subscribe_unsubscribe_symmetric(self):
        self._attach_detach(ReaderController, ReaderView())


class FindBooksShelveSnapshotTests(unittest.TestCase):
    """找书页「加入书架」：递给书架服务的必须是书条目，不是字典快照。

    字典快照进 ``SourceService.add_to_bookshelf`` 会 ``book.url`` 直接
    AttributeError，而它发生在控制器后台任务里——界面上就是点了没反应。
    """

    def test_shelve_passes_book_object_not_dict(self):
        source = SimpleNamespace(bookSourceUrl="https://s", bookSourceName="源",
                                 enabled=True, searchUrl="", bookSourceGroup="")
        source_service = SimpleNamespace(shelve_book=AsyncMock(return_value=True))
        hub = _FakeHub({**_stub_services(), SourceService: source_service})
        controller = FindBooksController(hub)
        controller.attach(FindBooksView())
        try:
            controller._sources = [source]
            controller._shelve({"name": "测试书", "author": "甲",
                                "url": "/book/1", "origin": "https://s"})
            deadline = time.time() + 10
            while (source_service.shelve_book.await_count == 0
                   and time.time() < deadline):
                time.sleep(0.05)
            self.assertEqual(source_service.shelve_book.await_count, 1,
                             "入架编排没有跑到服务")
            book = source_service.shelve_book.await_args.args[1]
            self.assertEqual(getattr(book, "url", ""), "/book/1")
            self.assertEqual(getattr(book, "name", ""), "测试书")
        finally:
            controller.detach()


class DetailDialogStackTests(unittest.TestCase):
    """详情对话框只留一层：连点同一本书不该叠出第二层。

    两层一模一样的对话框，点「开始阅读」只 pop 掉最上面那层——阅读器开好了，
    屏上还盖着一层"关一层还剩一层"的详情框（界面探针实测）。
    """

    def _controller(self, page):
        controller = FindBooksController(_FakeHub(_stub_services()))
        controller.attach(FindBooksView())
        controller._page = lambda: page
        return controller

    def test_second_click_on_the_same_book_is_ignored(self):
        page = _FakeDialogPage()
        controller = self._controller(page)
        book = {"name": "秘境测试", "url": "/book/1"}
        try:
            controller.show_detail(book)
            controller.show_detail(book)
            self.assertEqual(len(page.shown), 1, "详情对话框叠了两层")
        finally:
            controller.detach()

    def test_detail_dialog_reopens_after_close(self):
        page = _FakeDialogPage()
        controller = self._controller(page)
        book = {"name": "秘境测试", "url": "/book/1"}
        try:
            controller.show_detail(book)
            page.pop_dialog()
            controller.show_detail(book)
            self.assertEqual(len(page.shown), 1, "关掉之后就该能再开")
        finally:
            controller.detach()


class SourceSwitchDialogTests(unittest.TestCase):
    """换源对话框候选渲染。"""

    def test_empty_candidate_state(self):
        dlg = SourceSwitchDialog({"name": "三体"})
        dlg.render_candidates([])
        texts = [getattr(c, "value", "") for c in dlg._candidates.controls]
        self.assertTrue(any("暂未找到" in t for t in texts))
        # 初始即开始按钮可见、停止隐藏
        self.assertTrue(dlg._start_btn.visible)
        self.assertFalse(dlg._cancel_btn.visible)

    def test_candidates_render_rows(self):
        dlg = SourceSwitchDialog({"name": "三体"})
        book = SimpleNamespace(name="三体(完整版)")
        dlg.render_candidates([
            {"source": SimpleNamespace(bookSourceName="源甲"), "book": book},
            {"source": SimpleNamespace(bookSourceName="源乙"), "book": book},
        ])
        self.assertEqual(len(dlg._candidates.controls), 2)
        self.assertIsInstance(dlg._candidates.controls[0], ft.ListTile)

    def test_running_toggles_buttons(self):
        dlg = SourceSwitchDialog({"name": "x"})
        dlg.set_running(True, "扫描中")
        self.assertFalse(dlg._start_btn.visible)
        self.assertTrue(dlg._cancel_btn.visible)


class SourceSwitchProgressTests(unittest.IsolatedAsyncioTestCase):
    """换源扫描的进度反馈要真的接到对话框进度条上。

    扫描在服务内跑几十秒，进度条若不刷新，用户看到的就是"点了没反应"。
    """

    async def test_scan_progress_reaches_dialog(self):
        reading = _stub_services()[ReadingService]
        seen: dict = {}

        async def switch_source(item, candidates, keyword="", on_progress=None):
            seen["on_progress"] = on_progress
            if on_progress is not None:
                on_progress(1, 2, 1)      # 扫完 1/2，1 个源失败
            return []

        reading.switch_source = switch_source
        reading.searchable_sources = staticmethod(
            lambda sources, scope=None: list(sources))
        source = SimpleNamespace(bookSourceUrl="https://a", bookSourceName="甲",
                                 enabled=True, searchUrl="/q", bookSourceGroup="")
        services = {ReadingService: reading,
                    SourceService: SimpleNamespace(
                        list_bookshelf=AsyncMock(return_value=[]),
                        list_sources=AsyncMock(return_value=[source]))}
        controller = BookshelfController(_FakeHub(services))
        controller.attach(BookshelfView())
        record = {"name": "三体", "url": "/book/1", "source_url": "https://a"}
        dialog = SourceSwitchDialog(record)
        statuses: list[str] = []
        original_set_running = dialog.set_running

        def spy_set_running(running: bool, status: str = "") -> None:
            statuses.append(status)
            original_set_running(running, status)

        dialog.set_running = spy_set_running
        try:
            controller._sources = [source]
            controller._switch_dialog = dialog
            await controller._run_switch(record, dialog)
            self.assertIn("on_progress", seen, "控制器没把进度回调传给服务")
            self.assertIsNotNone(seen["on_progress"])
            self.assertAlmostEqual(dialog._progress.value, 0.5, places=2)
            self.assertTrue(any("1/2" in s and "失败" in s for s in statuses),
                            f"扫描中状态没报进度/失败数：{statuses}")
            self.assertTrue(any("扫描完成" in s for s in statuses),
                            f"扫描结束没收口：{statuses}")
        finally:
            controller._switch_dialog = None
            controller.detach()


class _FakeDialogPage:
    """只记录对话框收发的假 page（控制器询问用 page.show_dialog/pop_dialog）。"""

    def __init__(self):
        self.shown: list = []
        self.pop_count = 0

    def show_dialog(self, dialog) -> None:
        dialog.open = True
        self.shown.append(dialog)

    def pop_dialog(self) -> None:
        self.pop_count += 1
        # 真实现只把 open 置 False，这里同构：关掉的从 shown 里摘掉
        self.shown = [d for d in self.shown if getattr(d, "open", False)]


class ReaderExitStagedTests(unittest.TestCase):
    """退出阅读的收尾询问（Legado「返回时提示放入书架」）。

    试读的书记录在、但书架不认；退出时不问一声就直接切页，用户以为书进了架，
    下次启动残留又被清掉——书就"凭空消失"了。
    """

    staged = {"name": "秘境测试", "url": "/book/1.html",
              "source_url": "http://127.0.0.1:1"}

    def _build(self, staged_record):
        services = _stub_services()
        reading = services[ReadingService]
        reading.staged_record = lambda: staged_record
        controller = ReaderController(_FakeHub(services))
        view = ReaderView()
        controller.attach(view)
        page = _FakeDialogPage()
        # 无头视图没有 page 容器：把询问落到一个只记账的假 page 上
        controller._page = lambda: page
        left: list[str] = []
        controller.on_back_to_find_books = lambda: left.append("find_books")
        controller.on_back_to_bookshelf = lambda: left.append("bookshelf")
        return controller, view, reading, page, left

    @staticmethod
    def _exit_button(view):
        return next(btn for btn in view._toolbar.content.controls
                    if getattr(btn, "tooltip", "") == "退出阅读")

    @staticmethod
    def _wait_until(predicate, deadline: float = 10.0) -> bool:
        end = time.time() + deadline
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_toolbar_has_exit_action(self):
        view = ReaderView()
        self.assertTrue(any(getattr(btn, "tooltip", "") == "退出阅读"
                            for btn in view._toolbar.content.controls),
                        "阅读器工具栏没有退出入口：试读询问永远触发不了")

    def test_nav_action_slots_are_declared(self):
        """nav_actions 的键必须是控制器上真实存在的槽，值必须是注册可达的目的地。

        名字打错时 NavigationHost 只会静默不绑（getattr 拿到方法而非 None），
        落点就变成了点了没反应。目的地按 NAV_META 全量校验而不是只认本组——
        「章节读不出来 → 去自愈页」正当是跨组的（reading.reader → sources.repair）。
        """
        services = _stub_services()
        controller = ReaderController(_FakeHub(services))
        view = ReaderView()
        controller.attach(view)
        try:
            for slot, dest in controller.nav_actions.items():
                self.assertIn(dest, NAV_META,
                              f"{slot} 指向了导航表之外的目的地")
                self.assertIsNone(getattr(controller, slot),
                                  f"{slot} 应在 on_attach 里初始化为 None 槽位")
        finally:
            controller.detach()

    def test_shelved_book_leaves_without_asking(self):
        controller, view, reading, page, left = self._build(None)
        try:
            self._exit_button(view).on_click(None)
            self.assertTrue(self._wait_until(lambda: bool(left)))
            self.assertEqual(page.shown, [], "在架的书不该被问是否入架")
            self.assertEqual(left, ["bookshelf"])
            self.assertEqual(reading.close_book.await_count, 1)
            self.assertIs(reading.close_book.await_args.kwargs["save_progress"], True)
            self.assertEqual(reading.promote_staged_book.await_count, 0)
        finally:
            controller.detach()

    def test_trial_book_promoted_on_confirm(self):
        controller, view, reading, page, left = self._build(self.staged)
        calls: list[str] = []
        promote = reading.promote_staged_book

        async def record_promote():
            calls.append("promote")
            return await promote()

        close = reading.close_book

        async def record_close(save_progress=True):
            calls.append(f"close(save_progress={save_progress})")
            return await close(save_progress=save_progress)

        reading.promote_staged_book = record_promote
        reading.close_book = record_close
        try:
            self._exit_button(view).on_click(None)
            self.assertTrue(self._wait_until(lambda: bool(page.shown)),
                            "退出试读没弹出入架询问")
            dialog = page.shown[0]
            self.assertEqual(dialog.title.value, "放入书架")
            self.assertIn("《秘境测试》", dialog.content.controls[0].value)
            next(btn for btn in dialog.actions
                 if getattr(btn, "content", "") == "放入书架").on_click(None)
            self.assertTrue(self._wait_until(lambda: bool(left)))
            # 转正必须排在关会话前：close_book 一清当前书源/书，暂存记录就找不回了
            self.assertEqual(calls, ["promote", "close(save_progress=True)"],
                             f"收尾顺序不对：{calls}")
            self.assertEqual(page.pop_count, 1)
            # 试读落回找书页（书可能没入架，落回书架等于扑空）
            self.assertEqual(left, ["find_books"])
        finally:
            controller.detach()

    def test_trial_book_discarded_on_refuse(self):
        controller, view, reading, page, left = self._build(self.staged)
        try:
            self._exit_button(view).on_click(None)
            self.assertTrue(self._wait_until(lambda: bool(page.shown)))
            refuse = next(btn for btn in page.shown[0].actions
                          if getattr(btn, "content", "") == "不放入")
            refuse.on_click(None)
            self.assertTrue(self._wait_until(lambda: bool(left)))
            self.assertEqual(reading.discard_staged_book.await_count, 1)
            self.assertEqual(reading.promote_staged_book.await_count, 0)
            # 不放入：不能再存进度，否则刚删的 books 行又被 upsert 回来
            self.assertIs(reading.close_book.await_args.kwargs["save_progress"], False)
            self.assertEqual(left, ["find_books"])
        finally:
            controller.detach()


class ReaderOpenFailureTests(unittest.TestCase):
    """开书失败必须在屏幕上说话：调用方是 ``ui_call`` 投递的，没人 await 那个 future。

    漏接就等于"点了书没反应"的白屏——本地书缓存丢失（``LocalBookCacheMissing``）
    正是这条路径上最先撞到的失败。
    """

    def _build(self, error: Exception):
        services = _stub_services()
        reading = services[ReadingService]
        reading.open_book = AsyncMock(side_effect=error)
        controller = ReaderController(_FakeHub(services))
        view = ReaderView()
        controller.attach(view)
        return controller, view, reading

    def _wait(self, predicate, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_cache_missing_is_shown_and_retry_is_allowed(self):
        from app.services.content.local_book_service import LocalBookCacheMissing
        controller, view, _reading = self._build(
            LocalBookCacheMissing("本地书「上山」的目录缓存已丢失，请在书架重新导入该文件"))
        try:
            controller.ui_call(controller.open_book(
                None, {"name": "上山", "url": "file:///c/上山.txt"}))
            self.assertTrue(self._wait(
                lambda: str(view._progress_label.value or "").startswith("打开失败")),
                f"开书失败没有反馈：{view._progress_label.value!r}")
            self.assertIn("重新导入", str(view._progress_label.value))
            # 防连点的键要放回去，否则用户重新导入后再点就没反应了
            # （反馈和复位在同一段 except 里，看到反馈即已复位）
            self.assertEqual(controller._opening, "")
        finally:
            controller.detach()

    def test_network_failure_message_reaches_the_view(self):
        controller, view, _reading = self._build(RuntimeError("书源连不通"))
        try:
            controller.ui_call(controller.open_book(
                None, {"name": "网书", "url": "http://s/book/1"}))
            self.assertTrue(self._wait(
                lambda: "书源连不通" in str(view._progress_label.value or "")),
                f"失败原因没有报出来：{view._progress_label.value!r}")
        finally:
            controller.detach()


class BookmarkPanelTests(unittest.TestCase):
    """书签面板空态（契约 §4 测试锚点）。"""
    def test_empty_state(self):
        panel = BookmarkPanel()
        panel.render_bookmarks([])
        joined = " ".join(
            getattr(c, "value", "") for c in panel._list.controls)
        self.assertIn("暂无书签", joined)

    def test_render_rows(self):
        panel = BookmarkPanel()
        panel.render_bookmarks([
            {"bookmark_id": 1, "chapter_index": 2, "offset": 0.5,
             "summary": "第三章开头"},
        ])
        self.assertEqual(len(panel._list.controls), 1)
        self.assertIsInstance(panel._list.controls[0], ft.ListTile)


class ReaderSidePanelTests(unittest.TestCase):
    """左侧抽屉：书签与书内搜索合成一个面板（工具栏三个开面板动作相邻）。"""

    @classmethod
    def _descendants(cls, control):
        yield control
        for child in (getattr(control, "controls", None)
                      or ([control.content] if getattr(control, "content", None) else [])):
            if child is None:
                continue
            yield from cls._descendants(child)

    def test_both_panels_live_in_the_same_drawer(self):
        """两个面板都在 _side_host 子树里：开搜索不再把书签列表顶掉。"""
        view = ReaderView()
        inside = list(self._descendants(view._side_host))
        self.assertIn(view.bookmark_panel, inside)
        self.assertIn(view.content_search_bar, inside)

    def test_show_side_panel_selects_tab_without_closing(self):
        view = ReaderView()
        view.show_side_panel("bookmark")
        self.assertTrue(view._side_host.visible)
        self.assertEqual(view._side_tabs.selected_index, 0)
        view.show_side_panel("search")
        self.assertTrue(view._side_host.visible, "换签不该把抽屉关掉")
        self.assertEqual(view._side_tabs.selected_index, 1)
        view.show_side_panel("")
        self.assertFalse(view._side_host.visible)

    def test_panel_openers_are_adjacent_in_toolbar(self):
        """目录 / 书签 / 书内搜索 是同一类动作，工具栏上放在一起。"""
        view = ReaderView()
        tooltips = [getattr(btn, "tooltip", "")
                    for btn in view._toolbar.content.controls]
        slots = [tooltips.index(name) for name in ("目录", "书签", "书内搜索")]
        self.assertEqual(sorted(slots), slots, f"开面板入口被打散：{tooltips}")

    def test_directory_overlay_is_opaque(self):
        """目录页盖在正文之上：覆盖层必须不透明，否则正文从列表背后透出来。

        用户报的正是这个——点目录后「章节列表直接叠在正文上」，两层同时可见。
        """
        view = ReaderView()
        self.assertEqual(view._detail_page.bgcolor, ft.Colors.SURFACE,
                         "目录覆盖层透明，会和正文叠在一起")
        # 同一个 Stack 里的左抽屉同理（此前就设了底色，一起钉住）
        self.assertEqual(view._side_host.bgcolor, ft.Colors.SURFACE)

    def test_directory_show_toggles_only_visibility(self):
        view = ReaderView()
        view.show_directory(True)
        self.assertTrue(view._detail_page.visible)
        self.assertTrue(view._reader_page.visible,
                        "正文页不参与目录显隐：目录页只是叠在它上面的覆盖层")
        view.show_directory(False)
        self.assertFalse(view._detail_page.visible)
        self.assertTrue(view._reader_page.visible)


class ReaderChapterSelectTests(unittest.TestCase):
    """点目录里的章节：目录页收起、正文换成该章（用户报的"列表叠在正文上"）。"""

    CHAPTERS = [
        {"index": 0, "name": "第一章", "url": "/c1", "is_volume": False},
        {"index": 1, "name": "第二章", "url": "/c2", "is_volume": False},
    ]

    def setUp(self):
        services = _stub_services()
        services[ReadingService].load_content = AsyncMock(
            return_value=SimpleNamespace(
                blocks=[SimpleNamespace(kind="text", value="第二章正文")]))
        self.hub = _FakeHub(services)
        self.view = ReaderView()
        self.controller = ReaderController(self.hub)
        self.controller.attach(self.view)
        # 目录只能经控制器落位：_set_chapters 会一并重置派生的续读状态
        self.controller._set_chapters([dict(c) for c in self.CHAPTERS])
        self.view.render_chapters(self.controller._chapters)

    def tearDown(self):
        self.controller.detach()

    def _settled(self, predicate, timeout: float = 5.0) -> bool:
        """等 ui_call 派到引擎循环的那一跳跑完（无头场景没有界面循环）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _tiles(self):
        return [c for c in self.view._directory_list.controls
                if isinstance(c, ft.ListTile)]

    def _open_directory_button(self):
        for btn in self.view._toolbar.content.controls:
            if getattr(btn, "tooltip", "") == "目录":
                return btn
        raise AssertionError("工具栏没有「目录」入口")

    def test_selecting_chapter_collapses_directory_and_swaps_text(self):
        self._open_directory_button().on_click(None)
        self.assertTrue(self.view._detail_page.visible, "点「目录」应盖出目录页")

        self._tiles()[1].on_click(None)   # 真实 ListTile 的 on_click
        self.assertFalse(self.view._detail_page.visible,
                         "选完章节目录仍盖在正文上")
        self.assertTrue(self._settled(lambda: self.view._title.value == "第二章"),
                        f"正文标题没换成所选章节：{self.view._title.value!r}")
        first = self.view._content_host.controls[0]
        # 段首缩进（"　　"）由排版测试单独盯，这里只关心换没换章
        self.assertEqual(getattr(first, "value", "").lstrip("　"), "第二章正文")
        self.assertEqual(self.view._progress_label.value, "2/2 · 第二章")

    def test_back_button_collapses_without_touching_content(self):
        """「返回阅读」只收起目录页，正文保持原章。"""
        self._open_directory_button().on_click(None)
        back = self.view._detail_page.content.controls[0].content.controls[0]
        self.assertEqual(getattr(back, "tooltip", ""), "返回阅读")
        back.on_click(None)
        self.assertFalse(self.view._detail_page.visible)
        self.assertEqual(self.view._title.value, "正文")


class ReaderAutoContinueTests(unittest.TestCase):
    """读到章尾自动接下一章：旧阅读器的连续滚动，重构时整段丢了。

    用户报的就是这个缺口——一章读完必须回目录点下一章。这里的断言不止"接上了"，
    还钉住三条容易写错的语义：追加不得清空已读章节、同一章不得接第二遍、
    滚动定位要跟着换到追加进来的那一章（进度落库靠它）。
    """

    CHAPTERS = [
        {"index": 0, "name": "第一章", "url": "/c1", "is_volume": False},
        {"index": 1, "name": "第二章", "url": "/c2", "is_volume": False},
        {"index": 2, "name": "第三章", "url": "/c3", "is_volume": False},
    ]

    def setUp(self):
        services = _stub_services()
        reading = services[ReadingService]
        raw = [SimpleNamespace(index=c["index"], name=c["name"], url=c["url"])
               for c in self.CHAPTERS]
        session = ReadingSession()
        session.bind("http://src", "http://src/book", raw, None)
        reading.current_session = session

        async def load_content(chapter, append=False, restore_offset=0.0):
            return SimpleNamespace(blocks=[SimpleNamespace(
                kind="text", value=f"{chapter.name}正文")])

        reading.load_content = load_content
        self.session = session
        self.reading = reading
        self.view = ReaderView()
        self.controller = ReaderController(_FakeHub(services))
        self.controller.attach(self.view)
        self.controller._set_chapters([dict(c) for c in self.CHAPTERS])
        self.controller.ui_call(self.controller._open_index(0))
        # 挂载那一阵 `_restoring_scroll` / `_reading_loading` 都还立着，
        # `_on_reader_scroll` 会整段早退：只等 `_chapter_index` 的话，紧跟着的那一发
        # 滚动会被当成噪音丢掉（真用户还在继续滚，下一条事件就落进来了；用例只发一发，
        # 于是全量套件偶发 "接不上下一章"）。等首屏真收工再滚。
        self.assertTrue(self._settled(
            lambda: self.controller._chapter_index == 0
            and not self.controller._reading_loading
            and not self.controller._restoring_scroll),
            "首章没有挂载完成（或一直卡在加载中标记）")

    def tearDown(self):
        self.controller.detach()

    def _settled(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _scroll(self, pixels, maximum):
        """模拟一次真实滚动事件（视图那条 on_scroll 也接到同一个处理器上）。

        发之前等加载收工：追加那一阵 `_reading_loading` / `_restoring_scroll` 还立着，
        `_on_reader_scroll` 在 `reader_controller.py:827` 与 `:845` 两处整段早退，
        这一发就被当成挂载噪音丢了。真用户的手指是连续的、收工后必然还有一发落在处理里；
        用例只发**一发**，丢了就永远等不到（单跑绿、全量负载下偶发红）。
        要测"加载中被丢掉"本身，用 `_scroll_now`。
        """
        self.assertTrue(self._settled(self._idle), "滚动前没等到加载收工")
        self._scroll_now(pixels, maximum)

    def _scroll_now(self, pixels, maximum):
        self.controller._on_reader_scroll(SimpleNamespace(
            pixels=pixels, max_scroll_extent=maximum))

    def _idle(self):
        return (not self.controller._reading_loading
                and not self.controller._restoring_scroll)

    def _texts(self):
        """屏幕上挂出来的文本（剥掉段首缩进：这里要验的是"哪几章在屏上"）。"""
        return [str(getattr(c, "value", "") or "").lstrip("　")
                for c in self.view._content_host.controls
                if getattr(c, "value", "")]

    def test_scroll_handler_is_wired_to_the_view(self):
        # 绑法方法按 == 比较（assertIs 会拿两次临时绑定的对象身份，必然不等）
        self.assertEqual(self.view.on_save_scroll,
                         self.controller._on_reader_scroll,
                         "正文滚动没接到续读逻辑：自动加载下一章永远不会触发")

    def test_bottom_of_chapter_appends_the_next_one(self):
        self._scroll(900, 1000)          # 距底 100px < 180 → 追加
        self.assertTrue(self._settled(lambda: "/c2" in self.session.viewports),
                        "滚到章尾没有自动接下一章")
        self.assertEqual(self._texts(), ["第一章正文", "第二章", "第二章正文"],
                         "追加的章节没有接在文末，或把已读章节顶掉了")
        # 本章起点 = 触发追加那次滚动事件里的文档总高
        self.assertAlmostEqual(self.session.viewports["/c2"].document_start,
                               1000.0)

    def test_a_chapter_is_never_appended_twice(self):
        """同一章只能接一遍：这条性质上有**两道**防御，删任意一道另一道都兜得住。

        变异核验实测（`.probe/mutate_scroll_harness.py`）：单独去掉追加后的
        `_auto_scroll_armed = False`（`reader_controller.py:856`）**照样绿**，
        单独放行加载闸门（:845）也**照样绿**——第二道 `_continuous_loaded` 成员判定
        （:854）把活儿接了过去。所以这条用例钉的是"这条链整体没做出第二遍"，
        不是名字里某一道闸；要证明单道闸存在得把两道一起拆（组合变异已核验会红）。
        """
        self._scroll(900, 1000)
        self.assertTrue(self._settled(lambda: "/c2" in self.session.viewports))
        # 等第二章挂载收工再连发：不然这几发全被加载闸门丢掉，用例是"vacuously 绿"，
        # 真正该挡的那道逻辑一次都没被压到。
        self.assertTrue(self._settled(self._idle))
        for _ in range(3):               # 同一位置反复触发（惯性滚动的常态）
            self._scroll_now(900, 1000)
        time.sleep(0.2)
        self.assertEqual(self._texts(), ["第一章正文", "第二章", "第二章正文"],
                         "布防/去布防与已挂集合两道防御一起失效")

    def test_a_bottom_scroll_during_a_load_is_dropped_and_a_later_one_works(self):
        """挂载途中的"到底"不作数，但闸门开了之后同一发必须接得上。

        那一刻的文档几何是布局中间态而不是"用户读完了"（与 :815 那条"偏移 0 的挂载
        噪音"同源），所以 `_on_reader_scroll` 在 :845 整段早退。**代价写在这儿**：
        被挡掉的这一发没人补判，用户要再滚一下才接得上——真实手指是连续的所以看不出来。
        反面那半不能省：只断"没接上"的话，把续读整个弄坏也能让这条绿。
        """
        self.controller._reading_loading = True
        self._scroll_now(900, 1000)
        self.assertFalse(
            self._settled(lambda: "/c2" in self.session.viewports, timeout=0.5),
            "加载中间态的几何被当成读完了，凭空接进第二章")
        self.controller._reading_loading = False
        self._scroll(900, 1000)
        self.assertTrue(self._settled(lambda: "/c2" in self.session.viewports),
                        "闸门放行后还是不接章：上面那个「没接上」就是假过的")

    def test_tracking_follows_the_appended_chapter(self):
        self._scroll(900, 1000)
        self.assertTrue(self._settled(lambda: "/c2" in self.session.viewports))
        self._scroll(1100, 2000)         # 滚进第二章：距底 900 → 只重新布防
        self.assertTrue(self._settled(
            lambda: self.controller._chapter_index == 1))
        self.assertEqual(self.view._title.value, "第二章")
        self.assertEqual(self.view._status_bar.content.value, "第二章 · 2/3 · 66%")
        self._scroll(1900, 2000)         # 第二章读到头 → 接第三章
        self.assertTrue(self._settled(lambda: "/c3" in self.session.viewports))
        self.assertEqual(self._texts()[-1], "第三章正文")

    def test_position_persistence_skips_the_mount_noise(self):
        """换整篇文档时那次"偏移 0"的瞬时事件不是用户进度，不能落库。"""
        self.controller._restoring_scroll = True
        # 这条测的就是"挂载窗口里那一发必须被丢掉"，所以直接发、不等收工。
        self._scroll_now(0, 0)
        self.assertFalse(self._settled(
            lambda: self.reading.save_progress.await_count > 0, timeout=0.5),
            "挂载噪音被当成阅读进度写盘，下次打开会回到章首")

    def test_font_size_relayout_keeps_appended_chapters(self):
        self._scroll(900, 1000)
        self.assertTrue(self._settled(lambda: "/c2" in self.session.viewports))
        self.controller.change_font_size(4)
        self.assertEqual(self.view._font_size, 20)
        self.assertEqual([getattr(c, "size", None) for c in
                          self.view._content_host.controls
                          if getattr(c, "data", None) == "reader-text"],
                         [20, 20], "字号重排漏掉了追加进来的章节，或被换回单章")
        self.controller.change_font_size(99)
        self.assertEqual(self.view._font_size, 28, "字号没有夹在 13-28 区间")
        self.controller.change_font_size(-99)
        self.assertEqual(self.view._font_size, 13)


class ContentSearchBarPathTests(unittest.IsolatedAsyncioTestCase):
    """书内搜索条触发 ContentSearchService 查询路径。"""

    async def test_search_bar_calls_content_search_service(self):
        services = _stub_services()
        hub = _FakeHub(services)
        view = ReaderView()
        controller = ReaderController(hub)
        controller.attach(view)
        # 会话上下文：book_url
        controller._book_url = "https://x/book"
        # 搜索条 on_search 已被接到 search_in_book（视图回调接线正确）
        self.assertEqual(view.content_search_bar.on_search, controller.search_in_book)
        # ui_call 会派到真 engine loop；直接跑内部协程断言服务调用路径。
        await controller._run_content_search("命运")
        services[ContentSearchService].search.assert_awaited_once_with(
            "https://x/book", "命运")
        controller.detach()


class RegistryWiringTests(unittest.TestCase):
    """阅读组三目的地注册进 ViewRegistry。"""

    def test_register_reading_destinations(self):
        registry = ViewRegistry()
        self.assertEqual(READING_DESTINATIONS,
                         ("reading.find_books", "reading.bookshelf",
                          "reading.reader"))
        # 用 stub hub 跑工厂（attach 不联网）
        hub = _FakeHub(_stub_services())
        register_destinations(registry, hub)
        for dest in READING_DESTINATIONS:
            route = registry.try_get(dest)
            self.assertIsNotNone(route, f"目的地未注册: {dest}")
            view = route.factory()
            self.assertIsNotNone(view)


class ImportLintTests(unittest.TestCase):
    """§7.5 import-lint 守门：views/reading 零 app.services. 命中。"""

    _ROOT = Path(__file__).resolve().parent.parent / "app" / "views" / "reading"

    def test_no_services_import_in_reading_views(self):
        import ast
        offenders = []
        for py in sorted(self._ROOT.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for mod in names:
                    if mod and (mod == "app.services"
                                 or mod.startswith("app.services.")):
                        offenders.append(f"{py.name}:{node.lineno}: import {mod}")
        self.assertEqual(
            offenders, [],
            "views/reading/ 的 import 语句不得引用 app.services.*（红线 §7.5）：\n"
            + "\n".join(offenders))


# --------------------------------------------------------------------------- #
class BookshelfCacheTaskRowsTests(unittest.IsolatedAsyncioTestCase):
    """书架在途整书缓存：任务行来自服务快照，每条各自可取消（第十四轮的护栏）。

    钉的是重构后丢失的三件事，缺一件就回到"点了没反应 / 切页回来什么都不剩"：

    1. attach 时从 ``DownloadService.list_tasks()`` 补渲染（不靠"刚好收到事件"）；
    2. 「取消」按钮带的是**这一行**的 task_id，取消后重画；
    3. detach 之后的在途进度事件不再碰控件。
    """

    async def _settled(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return predicate()

    def _rows(self, view):
        return list(getattr(view, "_task_host").controls)

    @staticmethod
    def _task(task_id, book_url, status="queued", kind="whole_cache",
              done=0, total=0, progress=0.0):
        return TaskInfo(task_id=task_id, kind=kind, book_url=book_url,
                        status=status, done=done, total=total, progress=progress)

    def _build(self, tasks, snapshot=True):
        """真视图 + 真控制器 + 记账式 DownloadService 替身（完全离线）。"""
        services = _stub_services()
        services[SourceService] = SimpleNamespace(
            list_bookshelf=AsyncMock(return_value=[
                {"name": "三体", "url": "https://s/book-1"},
                {"name": "流浪地球", "url": "https://s/book-2"}]),
            list_sources=AsyncMock(return_value=[]))
        state = {"tasks": list(tasks), "cancelled": []}
        download = services[DownloadService]
        if snapshot:
            download.list_tasks = lambda: list(state["tasks"])
            download.cancel = self._canceller(state)
        hub = _FakeHub(services)
        return BookshelfView(), BookshelfController(hub), state, hub

    @staticmethod
    def _canceller(state):
        def cancel(task_id):
            state["cancelled"].append(task_id)
            for info in state["tasks"]:
                if info.task_id == task_id and info.status in ("queued", "running"):
                    info.status = "cancelled"
                    return True
            return False
        return cancel

    # ------------------------------------------------------------------ #
    async def test_attach_renders_live_tasks_from_the_service_snapshot(self):
        view, controller, _state, _hub = self._build([
            self._task("t1", "https://s/book-1", "running", done=3, total=10,
                       progress=0.3),
            self._task("t2", "https://s/book-2"),
            self._task("t3", "https://s/book-1", "done"),          # 已结束：不占行
            self._task("t4", "https://s/book-2", "running", kind="download"),
        ])
        controller.attach(view)
        try:
            self.assertTrue(
                await self._settled(lambda: len(self._rows(view)) == 2),
                "attach 后应从服务快照渲染出 2 行在途任务（只算 whole_cache 且未结束）")
            names = _text_values(view._task_host)
            self.assertIn("三体", names)
            self.assertIn("流浪地球", names)
            self.assertNotIn(
                "https://s/book-1", names, "任务行要显示书名，不是把 book_url 摊给用户")
            self.assertTrue(view._task_host.visible)
        finally:
            controller.detach()

    async def test_cancel_button_cancels_its_own_row_only(self):
        view, controller, state, _hub = self._build([
            self._task("t1", "https://s/book-1", "running"),
            self._task("t2", "https://s/book-2", "queued"),
        ])
        controller.attach(view)
        try:
            self.assertTrue(await self._settled(lambda: len(self._rows(view)) == 2),
                            "前置：两行在途任务先渲染出来")
            button = _cancel_button(self._rows(view)[0])
            self.assertIsNotNone(button, "每条任务行自带一个「取消」按钮")
            button.on_click(None)
            self.assertEqual(state["cancelled"], ["t1"],
                             "「取消」要带这一行的 task_id，不能整批取消或取消错本")
            self.assertTrue(await self._settled(lambda: len(self._rows(view)) == 1),
                            "取消后要重画：该行消失，另一行留着")
            self.assertEqual(state["tasks"][1].status, "queued",
                             "取消一行不该动到别的目标")
            self.assertEqual(view._status.value, "已取消整书缓存")
        finally:
            controller.detach()

    async def test_absent_download_service_renders_nothing_and_does_not_crash(self):
        """服务缺席（精简启动/替身装配）：整块收起，且**不许带走后面的加载**。

        这条要钉的是"快照读不到 ≠ 书架加载失败"：编排协程跑在引擎循环上，
        异常只会被 future 吞掉（界面表现为"少了一块"而不是报错），所以断言
        不能只看没画行，还得看 `_render_tasks` 之后的状态行确实写了。
        """
        view, controller, _state, _hub = self._build(
            [self._task("t1", "https://s/book-1", "running")], snapshot=False)
        controller.attach(view)
        try:
            self.assertTrue(
                await self._settled(lambda: "书架共" in (view._status.value or "")),
                "整书缓存快照读不到时，书架加载不该被它带崩（状态行是这段的落点）")
            self.assertEqual(self._rows(view), [],
                             "读不到任务快照时必须一行都不画（画半行的界面更可疑）")
            self.assertFalse(view._task_host.visible,
                             "没有任务就别留一块灰框占位")
        finally:
            controller.detach()

    async def test_progress_event_after_detach_touches_no_widget(self):
        """退订挡不住在途事件（EventBus 异步派发）：处理器自己要认 ``_view is None``。"""
        view, controller, _state, hub = self._build([
            self._task("t1", "https://s/book-1", "running")])
        rendered: list = []
        view.set_cache_tasks = lambda tasks: rendered.append(list(tasks))
        controller.attach(view)
        handlers = [handler for event_type, handler in hub.bus.subscriptions
                    if event_type is DownloadProgressEvent]
        self.assertEqual(len(handlers), 1, "控制器应订阅 DownloadProgressEvent")
        self.assertTrue(await self._settled(lambda: rendered),
                        "前置：attach 的快照渲染确实走了 set_cache_tasks")
        rendered.clear()
        controller.detach()
        view._status.value = "（切页前的状态行）"
        await handlers[0](DownloadProgressEvent("t1", "whole_cache", 0.9))
        self.assertEqual(rendered, [],
                         "detach 后收到的在途进度事件不该再碰控件（切页瞬间的红线）")
        self.assertEqual(view._status.value, "（切页前的状态行）",
                         "同理不该再写状态行")


class ReaderRepairOfferTests(unittest.IsolatedAsyncioTestCase):
    """章节读不出来 → 自愈入口（§3.2）：RuleRepairService 曾经在整个 app/ 里零调用。

    钉住四件事：失败必须亮出可点的补救条；点它要把**真的**失败章节对象递给服务并
    跳到自愈页（进度与报告都在那一页显示）；本地书和没有自愈服务时不许亮；
    下一章读成功要把上一条失败收起（不然正文和"读不出来"同时在屏上）。
    """

    CHAPTERS = [
        {"index": 0, "name": "第一章", "url": "/c1", "is_volume": False},
        {"index": 1, "name": "卷二", "url": "", "is_volume": True},
        {"index": 2, "name": "第二章", "url": "/c2", "is_volume": False},
        {"index": 3, "name": "第三章", "url": "/c3", "is_volume": False},
    ]

    async def _settled(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return predicate()

    def _build(self, source_url="http://src/", broken="/c1"):
        services = _stub_services()
        reading = services[ReadingService]
        raw = [SimpleNamespace(index=c["index"], name=c["name"], url=c["url"],
                               is_volume=c["is_volume"]) for c in self.CHAPTERS]
        session = ReadingSession()
        session.bind(source_url, "http://src/book", raw, None)
        reading.current_session = session
        reading.current_source = SimpleNamespace(bookSourceUrl=source_url)
        reading.current_book = SimpleNamespace(name="书名", url="http://src/book")

        async def load_content(chapter, append=False, restore_offset=0.0):
            if chapter.url == broken:
                raise RuntimeError("规则 #nr1@html 没有解析出正文")
            return SimpleNamespace(blocks=[SimpleNamespace(
                kind="text", value=f"{chapter.name}正文")])

        reading.load_content = load_content
        repairs: list = []
        services[RuleRepairService] = SimpleNamespace(
            repair=self._recorder(repairs))
        hub = _FakeHub(services)
        view = ReaderView()
        controller = ReaderController(hub)
        controller.attach(view)
        controller._set_chapters([dict(c) for c in self.CHAPTERS])
        jumped: list = []
        controller.on_open_repair = lambda: jumped.append("sources.repair")
        self.addCleanup(controller.detach)
        return view, controller, reading, repairs, jumped

    @staticmethod
    def _recorder(repairs):
        async def repair(source, chapter, *, extra_chapters=(), book=None,
                         **_ignored):
            repairs.append({"source": source, "chapter": chapter,
                            "extra": list(extra_chapters), "book": book})
            return SimpleNamespace(fix=None, reason="替身报告")
        return repair

    async def _fail_first_chapter(self, view, controller):
        chapter = controller._readable[0]
        await controller._run_chapter(
            chapter, controller._chapter_by_url(chapter["url"]), append=False)

    def _repair_button(self, view):
        for node in _walk(view):
            if isinstance(node, ft.TextButton):
                label = node.content if isinstance(node.content, str) else \
                    getattr(node.content, "value", "")
                if "自动修复" in str(label or ""):
                    return node
        return None

    async def test_failure_lights_the_offer_and_keeps_the_chapter(self):
        view, controller, reading, repairs, jumped = self._build()
        await self._fail_first_chapter(view, controller)
        self.assertTrue(view.repair_offer.visible,
                        "章节加载失败只留一行字，用户就没有出路了")
        self.assertIn("#nr1@html", " ".join(_text_values(view.repair_offer)))
        self.assertIs(controller._failed_chapter,
                      controller._chapter_by_url("/c1"),
                      "诊断要用的就是失败的那一章，不是当前章")

    async def test_click_offers_diagnoses_then_jumps_to_the_repair_page(self):
        view, controller, reading, repairs, jumped = self._build()
        await self._fail_first_chapter(view, controller)
        button = self._repair_button(view)
        self.assertIsNotNone(button, "补救条上没有「自动修复该书源」按钮")
        button.on_click(None)
        self.assertTrue(await self._settled(lambda: bool(repairs)),
                        "点了自动修复却没起诊断")
        self.assertEqual([c.url for c in repairs[0]["extra"]], ["/c2", "/c3"],
                         "复核样本要跳过卷标题，取后面两章")
        self.assertEqual(repairs[0]["book"], reading.current_book)
        self.assertEqual(jumped, ["sources.repair"],
                         "诊断在后台跑，界面必须跳到能看到进度的那一页")
        self.assertFalse(view.repair_offer.visible, "跳走时旧失败条该收掉")

    async def test_local_book_does_not_offer_rule_repair(self):
        """本地书没有规则源：亮一个只会跳到空白页的按钮比不亮更糟。"""
        view, controller, reading, repairs, jumped = self._build(
            source_url=LOCAL_BOOK_SOURCE_URL)
        await self._fail_first_chapter(view, controller)
        self.assertFalse(view.repair_offer.visible)
        self.assertIn("加载失败", view._progress_label.value)

    async def test_missing_repair_service_does_not_offer(self):
        view, controller, reading, repairs, jumped = self._build()
        controller._repair = None
        await self._fail_first_chapter(view, controller)
        self.assertFalse(view.repair_offer.visible)

    async def test_next_successful_chapter_hides_the_stale_failure(self):
        view, controller, reading, repairs, jumped = self._build()
        await self._fail_first_chapter(view, controller)
        self.assertTrue(view.repair_offer.visible)
        second = controller._readable[1]
        await controller._run_chapter(
            second, controller._chapter_by_url(second["url"]), append=False)
        self.assertFalse(view.repair_offer.visible,
                         "正文已经读出来了，屏幕上不该还挂着「这一章读不出来」")
        self.assertIsNone(controller._failed_chapter)


if __name__ == "__main__":
    unittest.main()
