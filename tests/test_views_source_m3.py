"""M3 书源域 UI 拆分冒烟测试（§7.2/§7.4/§7.5，契约 §9/§10）。

不依赖真实 Flet 窗口与网络：ft 控件可无窗口实例化；Controller 用 stub hub / 服务
替身离线构造。覆盖：

1. 五视图离线构造；
2. 五 Controller attach/detach 订阅退订对称（§7.4 防泄漏）；
3. 代码编辑器高亮词表与分词（词表来自 legado_rule_parser 规则方言）；
4. 检测视图渲染 CheckFinishedEvent 结果行；
5. 导入视图渲染 ImportFinishedEvent 结果；
6. 自愈视图渲染报告卡；
7. 书源组五目的地注册进 ViewRegistry；
8. import-lint AST 扫描：``app/views/source/`` 零 ``app.services.`` import。
"""
from __future__ import annotations

import ast
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft

from app.core.events import (ImportFinishedEvent, ImportProgressEvent,
                             RepairProgressEvent, RepairReportReadyEvent)
from app.core.registry import SOURCE_DESTINATIONS, ViewRegistry
from app.services.rule_repair import RepairReport, RepairSample, RuleFix
from app.services.sources.online_import_service import OnlineImportService
from app.services.sources.rule_repair_service import RuleRepairService
from app.services.sources.source_service import SourceService

from app.views.source import (BookSourceListView, CheckView, CodeEditorView,
                              RuleRepairView, SourceImportView,
                              register_destinations)
from app.views.source.code_editor_view import (RULE_HIGHLIGHT_KEYWORDS,
                                               tokenize_rule)

from app.controllers.source.book_source_list_controller import BookSourceListController
from app.controllers.source.check_controller import CheckController
from app.controllers.source.code_editor_controller import CodeEditorController
from app.controllers.source.rule_repair_controller import RuleRepairController
from app.controllers.source.source_import_controller import SourceImportController


# --------------------------------------------------------------------------- #
# 离线替身：不启真实 EngineHub / ServiceHub
# --------------------------------------------------------------------------- #
class _FakeBus:
    """记录订阅的假 EventBus：subscribe 返回幂等退订句柄。"""

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
    """按类型返回替身服务句柄；未注册类型抛错，控制器 _safe_service 容错为 None。"""

    def __init__(self, services: dict | None = None):
        self.bus = _FakeBus()
        self._services = services or {}

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


class ViewConstructTests(unittest.TestCase):
    """五视图离线可构造。"""

    def test_five_views_construct_offline(self):
        self.assertIsInstance(BookSourceListView(), ft.Column)
        self.assertIsInstance(SourceImportView(), ft.Column)
        self.assertIsInstance(CodeEditorView(), ft.Column)
        self.assertIsInstance(CheckView(), ft.Column)
        self.assertIsInstance(RuleRepairView(), ft.Column)


class ControllerLifecycleTests(unittest.TestCase):
    """attach/detach 订阅退订对称（§7.4 防泄漏）。"""

    def _symmetric(self, controller_cls, view):
        hub = _FakeHub()
        controller = controller_cls(hub)
        controller.attach(view)
        n_after = len(hub.bus.subscriptions)
        self.assertGreater(n_after, 0, f"{controller_cls.__name__} 应订阅事件")
        controller.detach()
        self.assertEqual(len(hub.bus.subscriptions), 0,
                         f"{controller_cls.__name__} detach 后应全部退订")

    def test_list_controller_symmetric(self):
        self._symmetric(BookSourceListController, BookSourceListView())

    def test_editor_controller_symmetric(self):
        # 编辑器是动作驱动（保存按钮），本身不订阅事件；只校验 attach/detach 不抛错、
        # 退订对称（有订阅则全退，无订阅则为空）。
        hub = _FakeHub()
        controller = CodeEditorController(hub)
        controller.attach(CodeEditorView())
        controller.detach()
        self.assertEqual(len(hub.bus.subscriptions), 0)

    def test_check_controller_symmetric(self):
        self._symmetric(CheckController, CheckView())

    def test_repair_controller_symmetric(self):
        self._symmetric(RuleRepairController, RuleRepairView())

    def test_import_controller_symmetric(self):
        self._symmetric(SourceImportController, SourceImportView())


class CodeEditorHighlightTests(unittest.TestCase):
    """编辑器高亮词表与分词（词表来自 legado_rule_parser 规则方言）。"""

    def test_keyword_table_nonempty_and_frozen(self):
        self.assertIsInstance(RULE_HIGHLIGHT_KEYWORDS, frozenset)
        self.assertGreater(len(RULE_HIGHLIGHT_KEYWORDS), 10)
        # 方言核心关键字必须在词表内
        for kw in ("@text", "@href", "@html", "@json:", "@css:", "@xpath:"):
            self.assertIn(kw, RULE_HIGHLIGHT_KEYWORDS)

    def test_tokenizer_classifies_segments(self):
        segments = dict((text, kind) for text, kind in
                        tokenize_rule('.list@text || $.title <js>x()</js>'))
        kinds = set(segments.values())
        self.assertIn("op", kinds)        # @text / ||
        self.assertIn("jsonpath", kinds)  # $.title
        self.assertIn("jsblock", kinds)   # <js>x()</js>

    def test_render_highlight_produces_spans(self):
        view = CodeEditorView()
        view.render_highlight('.a@text || $.b')
        self.assertGreater(len(view._highlight.spans), 1)


class CheckViewRenderTests(unittest.TestCase):
    """检测视图渲染 CheckFinishedEvent 结果行。"""

    def test_render_result_row(self):
        view = CheckView()
        view.render_result("https://a/", "fully_usable", "连通 OK · 搜索 OK")
        self.assertEqual(len(view._rows.controls), 1)
        # 同源重测覆盖而非追加
        view.render_result("https://a/", "unusable", "搜索失败")
        self.assertEqual(len(view._rows.controls), 1)

    def test_state_labels_nine_states(self):
        from app.views.source.check_view import CHECK_STATE_LABELS
        self.assertIn("fully_usable", CHECK_STATE_LABELS)
        self.assertIn("blocked", CHECK_STATE_LABELS)


class ImportViewRenderTests(unittest.TestCase):
    """导入视图渲染 ImportFinishedEvent。"""

    def test_render_result_count_and_errors(self):
        view = SourceImportView()
        view.render_result("book_source", 3, ["《源A》missing_java_apis: ajax", "占位提示"])
        self.assertIn("成功 3 条", view._summary.value)
        self.assertEqual(len(view._details.controls), 2)

    def test_no_resources_no_cards(self):
        """刚进页面：两张卡都收起（用户说的"灰色的框体"就是空卡/无意义控件占位）。"""
        view = SourceImportView()
        self.assertFalse(view._preview_card.visible)
        self.assertFalse(view._result_card.visible)
        self.assertFalse(view._progress.visible)

    def test_no_resource_type_dropdown(self):
        """资源类型下拉已移除：7 个选项里只有 1 个真能导入，其余点了只回一句说明。"""
        view = SourceImportView()

        def walk(control):
            yield control
            for child in list(getattr(control, "controls", None) or []):
                yield from walk(child)
            content = getattr(control, "content", None)
            if isinstance(content, ft.Control):
                yield from walk(content)

        self.assertFalse(any(isinstance(c, ft.Dropdown) for c in walk(view)),
                         "导入页不该再有资源类型下拉")

    def test_preview_card_reports_counts_and_groups(self):
        view = SourceImportView()
        view.render_preview("D:/tmp/源.json", {
            "total": 4, "valid": 3, "broken": 1,
            "groups": [("网文", 2), ("未分组", 1)], "error": ""})
        self.assertTrue(view._preview_card.visible)
        self.assertIn("可导入 3 条", view._preview_counts.value)
        self.assertIn("跳过 1 条", view._preview_counts.value)
        self.assertIn("网文 2 条", view._preview_groups.value)
        self.assertEqual(view.import_mode, "append")

    def test_unreadable_file_never_shows_an_empty_preview_card(self):
        view = SourceImportView()
        view.render_preview("bad.json", {"total": 0, "valid": 0, "broken": 0,
                                         "groups": [], "error": "JSON 解析失败：x"})
        self.assertFalse(view._preview_card.visible)
        self.assertTrue(view._result_card.visible)
        self.assertIn("不能导入", view._stage.value)

    def test_importing_locks_actions_and_shows_progress(self):
        view = SourceImportView()
        view.set_importing(True, "正在解析并写入书源…")
        self.assertTrue(view._progress.visible)
        self.assertIsNone(view._progress.value, "条数未知时先走不定长进度")
        self.assertTrue(view._pick_btn.disabled)
        view.set_progress(12, 200)
        self.assertAlmostEqual(view._progress.value, 12 / 200)
        self.assertEqual(view._stage.value, "校验并落库 12/200")

    def test_finished_unlocks_and_collapses_idle_progress_card(self):
        view = SourceImportView()
        view.set_importing(True)
        view.render_result("book_source", 5, [])
        self.assertFalse(view._progress.visible)
        self.assertFalse(view._pick_btn.disabled)
        self.assertFalse(view._preview_card.visible, "确认过后预览卡要收起")
        # 只是读文件读完了（没有任何结果行）→ 卡整张收起，不留空灰框
        view.set_importing(False)
        self.assertTrue(view._result_card.visible)
        view._summary.value = ""
        view.set_importing(False)
        self.assertFalse(view._result_card.visible)


class ImportFlowTests(unittest.IsolatedAsyncioTestCase):
    """导入编排：选文件只出预览，确认才落库；替换先弹确认。"""

    _GOOD = json.dumps([
        {"bookSourceName": "甲", "bookSourceUrl": "https://a/",
         "bookSourceGroup": "网文"},
        {"bookSourceName": "乙", "bookSourceUrl": "https://b/"},
    ], ensure_ascii=False)

    def _make(self, page=None):
        importer = SimpleNamespace(
            import_from_file=AsyncMock(), import_from_url=AsyncMock())
        hub = _FakeHub({OnlineImportService: importer,
                        SourceService: SimpleNamespace(sources=[])})
        view = SourceImportView()
        controller = SourceImportController(hub)
        controller.attach(view)
        if page is not None:
            controller._page = lambda: page
        posted: list = []
        # ui_call 要真循环；这里只截下派发的协程，由测试循环逐个 await。
        controller.ui_call = lambda coro: posted.append(coro)
        self.addCleanup(controller.detach)
        return controller, view, importer, posted

    async def _drain(self, posted):
        for coro in posted:
            await coro
        posted.clear()

    async def test_preview_does_not_write(self):
        controller, view, importer, posted = self._make()
        controller._offer_preview("源.json", self._GOOD)
        self.assertTrue(view._preview_card.visible)
        self.assertEqual(importer.import_from_file.await_count, 0)

    async def test_confirm_append_writes_and_shows_progress(self):
        controller, view, importer, posted = self._make()
        controller._offer_preview("源.json", self._GOOD)
        controller.confirm_import("append")
        await self._drain(posted)
        importer.import_from_file.assert_awaited_once_with(
            self._GOOD, "book_source", "append")
        self.assertTrue(view._progress.visible, "确认后必须立刻有进度条")
        self.assertTrue(view._pick_btn.disabled, "在途时锁住发起入口")
        self.assertFalse(view._preview_card.visible, "确认后预览卡收起")
        # 服务完成事件把界面收口
        await controller._on_import_finished(
            ImportFinishedEvent(kind="book_source", count=2, errors=[]))
        self.assertIn("成功 2 条", view._summary.value)
        self.assertFalse(view._progress.visible)
        self.assertFalse(view._pick_btn.disabled)

    @staticmethod
    def _labels(dialog):
        """按钮文字：0.86 的 Button.content 直接收 str，不自动包成 Text。"""
        out = []
        for btn in dialog.actions:
            c = btn.content
            out.append(c if isinstance(c, str) else getattr(c, "value", ""))
        return out

    async def test_confirm_replace_asks_before_touching_the_table(self):
        shown: list = []
        page = SimpleNamespace(show_dialog=shown.append,
                               pop_dialog=lambda: None)
        controller, view, importer, posted = self._make(page=page)
        controller._offer_preview("源.json", self._GOOD)
        controller.confirm_import("replace")
        self.assertEqual(importer.import_from_file.await_count, 0)
        self.assertEqual(len(shown), 1, "替换必须先弹确认框")
        self.assertIn("替换全部现有书源", shown[0].title.value)
        # 只有点了「确认替换」才真动手
        labels = self._labels(shown[0])
        self.assertIn("取消", labels)
        confirm = [btn for btn, label in zip(shown[0].actions, labels)
                   if label == "确认替换"]
        self.assertEqual(len(confirm), 1, "确认按钮没找到")
        confirm[0].on_click(None)
        await self._drain(posted)
        importer.import_from_file.assert_awaited_once_with(
            self._GOOD, "book_source", "replace")

    async def test_replace_without_page_does_not_execute(self):
        """拿不到 page 就弹不出确认框——那就不做破坏性动作，而不是退回追加。"""
        controller, view, importer, posted = self._make()
        controller._offer_preview("源.json", self._GOOD)
        controller.confirm_import("replace")
        self.assertEqual(importer.import_from_file.await_count, 0)
        self.assertIn("替换", view._stage.value)

    async def test_progress_event_drives_the_bar(self):
        controller, view, importer, posted = self._make()
        view.set_importing(True, "正在解析并写入书源…")
        await controller._on_import_progress(ImportProgressEvent(
            kind="book_source", phase="校验并落库", done=7, total=10))
        self.assertAlmostEqual(view._progress.value, 0.7)
        self.assertEqual(view._stage.value, "校验并落库 7/10")

    async def test_totalless_progress_keeps_indeterminate(self):
        controller, view, importer, posted = self._make()
        view.set_importing(True, "正在解析并写入书源…")
        await controller._on_import_progress(ImportProgressEvent(
            kind="book_source", phase="拉取在线文件", done=0, total=0))
        self.assertTrue(view._progress.visible)
        self.assertIsNone(view._progress.value)
        self.assertEqual(view._stage.value, "拉取在线文件")

    async def test_detached_view_ignores_late_events(self):
        controller, view, importer, posted = self._make()
        controller.detach()
        await controller._on_import_progress(ImportProgressEvent(
            kind="book_source", phase="校验并落库", done=1, total=10))
        await controller._on_import_finished(
            ImportFinishedEvent(kind="book_source", count=1, errors=[]))
        self.assertFalse(view._progress.visible)


class RepairViewRenderTests(unittest.TestCase):
    """自愈视图渲染报告卡。"""

    @staticmethod
    def _texts(control) -> list[str]:
        """深度取控件树里所有 Text 的值（0.86 的容器有的用 controls、有的用 content）。"""
        stack, out = [control], []
        while stack:
            node = stack.pop()
            if isinstance(node, ft.Text):
                out.append(str(node.value or ""))
            stack.extend(getattr(node, "controls", None) or [])
            child = getattr(node, "content", None)
            if isinstance(child, (list, tuple)):
                stack.extend(child)
            elif child is not None:
                stack.append(child)
        return out

    @classmethod
    def _buttons(cls, control) -> list[str]:
        stack, out = [control], []
        while stack:
            node = stack.pop()
            if isinstance(node, (ft.TextButton, ft.FilledTonalButton,
                                 ft.FilledButton, ft.OutlinedButton)):
                c = node.content
                out.append(c if isinstance(c, str) else str(getattr(c, "value", "")))
            stack.extend(getattr(node, "controls", None) or [])
            child = getattr(node, "content", None)
            if isinstance(child, (list, tuple)):
                stack.extend(child)
            elif child is not None:
                stack.append(child)
        return out

    def test_render_report_card(self):
        view = RuleRepairView()
        view.render_report({
            "report_id": "r1", "source_id": "https://a/", "source_name": "源A",
            "old_value": "css.bad", "new_value": "css.good",
            "samples": [{"chapter": "ch1", "chars": 120, "snippet": "正文"}],
        })
        self.assertTrue(view._card.visible)

    def test_render_no_fix_report_shows_the_reason_only(self):
        """诊断失败必须显示成"没提议"，不能画成一条空规则等用户点应用。"""
        view = RuleRepairView()
        view.render_report({
            "report_id": "r2", "source_id": "https://a/", "source_name": "源A",
            "has_fix": False, "reason": "样本页里只有 12 字正文，可能是拦截页",
        })
        self.assertTrue(view._card.visible)
        self.assertFalse(any("应用" in label for label in self._buttons(view._card)),
                         "没有可写回的提议却给了应用按钮")
        self.assertIn("拦截页", " ".join(self._texts(view._card)))
        self.assertIn("书源未改动", view._status.value)

    def test_render_empty_report_returns_to_waiting(self):
        view = RuleRepairView()
        view.render_report({"report_id": "r1", "has_fix": True,
                            "old_value": "a", "new_value": "b"})
        self.assertTrue(view._card.visible)
        view.render_report(None)
        self.assertFalse(view._card.visible)
        self.assertEqual(view._status.value, "等待自愈报告…")


class _DeferredFuture:
    """ui_call 的替身：截下协程等测试来 await，done 回调由 run() 手动触发。"""

    def __init__(self, coro, posted):
        self._coro = coro
        self._callbacks: list = []
        posted.append(self)

    def add_done_callback(self, fn):
        self._callbacks.append(fn)

    async def run(self):
        value = await self._coro if self._coro is not None else None
        for fn in self._callbacks:
            fn(SimpleNamespace(result=lambda: value))


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    """自愈页链路：服务报告 → 控制器展平 → 报告卡 → 确认写回（§3.2 红线）。

    这里刻意用**真的** RepairReport/RuleFix 对象走一遍：视图契约是扁平 dict，
    服务给的是嵌套 dataclass，展平是控制器的活。历史上这段是拿手造 dict 测的，
    于是"报告卡一片空白、点应用说没有 report_id"整个界面面从没通过过。
    """

    def _make(self, report=None):
        repair = SimpleNamespace(
            get_report=lambda rid: report,
            apply_fix=AsyncMock(return_value=True))
        hub = _FakeHub({RuleRepairService: repair})
        view = RuleRepairView()
        controller = RuleRepairController(hub)
        controller.attach(view)
        self.addCleanup(controller.detach)
        posted: list = []
        controller.ui_call = lambda coro: _DeferredFuture(coro, posted)
        return controller, view, repair, posted

    @staticmethod
    def _fix() -> RuleFix:
        return RuleFix(
            source_name="源A", source_url="https://a/",
            field="content", old_value="#nr1@html",
            new_value="#nr1@html||.article-content@p@text",
            samples=(RepairSample(chapter="第1页", url="https://a/1",
                                  chars=1800, snippet="正文开头"),),
            page_url="https://a/1", note="兜底分支")

    async def _drain(self, posted):
        for handle in list(posted):
            await handle.run()
        posted.clear()

    async def test_ready_event_renders_the_real_report(self):
        controller, view, repair, posted = self._make(
            RepairReport(self._fix(), tried=7, pages=2))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id="https://a/"))
        self.assertTrue(view._card.visible)
        texts = " ".join(RepairViewRenderTests._texts(view._card))
        self.assertIn("#nr1@html", texts)
        self.assertIn(".article-content@p@text", texts)
        self.assertIn("源A", texts)
        self.assertIn("第1页", texts)

    async def test_confirmed_report_id_round_trips_back_to_apply_fix(self):
        """报告对象自己不带 report_id——它由事件带进来，写回时必须原样送回。"""
        controller, view, repair, posted = self._make(
            RepairReport(self._fix()))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id="https://a/"))
        buttons = [btn for label, btn in _walk_buttons(view._card)
                   if "应用" in label]
        self.assertEqual(len(buttons), 1, "报告卡上应当只有一个「应用并重试」")
        buttons[0].on_click(None)
        self.assertEqual(len(posted), 1, "点了确认却没把写回任务排出去")
        await self._drain(posted)
        repair.apply_fix.assert_awaited_once_with("r1")
        self.assertIn("已写回", view._status.value)

    async def test_failed_write_back_says_so(self):
        """写回失败是返回值 False，不是异常：界面必须说"未生效"。"""
        controller, view, repair, posted = self._make(
            RepairReport(self._fix()))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="stale", source_id="https://a/"))
        repair.apply_fix.return_value = False
        [btn for label, btn in _walk_buttons(view._card)
         if "应用" in label][0].on_click(None)
        await self._drain(posted)
        self.assertIn("未生效", view._status.value)

    async def test_no_fix_report_reaches_the_user_as_a_reason(self):
        controller, view, repair, posted = self._make(
            RepairReport(None, reason="正文规则里有 JS，选择器自愈不适用"))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r9", source_id="https://a/"))
        texts = " ".join(RepairViewRenderTests._texts(view._card))
        self.assertIn("选择器自愈不适用", texts)
        self.assertEqual(
            [label for label, _ in _walk_buttons(view._card)], [],
            "没有提议却给了按钮：用户点了只会写回一个空值")

    async def test_progress_event_writes_the_stage_line(self):
        controller, view, repair, posted = self._make()
        await controller._on_progress(RepairProgressEvent(
            source_id="https://a/", stage="正在回放 24 条候选规则…"))
        self.assertEqual(view._status.value, "正在回放 24 条候选规则…")

    async def test_late_events_after_detach_touch_no_widgets(self):
        """投递是异步的：detach 之后排进去的事件还是会跑到处理器里。"""
        controller, view, repair, posted = self._make(
            RepairReport(self._fix()))
        controller.detach()
        await controller._on_progress(RepairProgressEvent(
            source_id="https://a/", stage="正在抓取样本页…"))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id="https://a/"))
        self.assertEqual(view._status.value, "等待自愈报告…")
        self.assertFalse(view._card.visible)

    async def test_both_repair_events_are_subscribed_on_attach(self):
        """两条事件都必须在 attach 时订阅过——没订阅就是又一处只存在于服务面的功能。"""
        controller, view, repair, posted = self._make()
        subscribed = {etype for etype, _ in controller._bus.subscriptions}
        self.assertIn(RepairProgressEvent, subscribed)
        self.assertIn(RepairReportReadyEvent, subscribed)


def _walk_buttons(control):
    """(按钮文字, 按钮) 列表：0.86 的 Button.content 可能是裸 str。"""
    stack, out = [control], []
    while stack:
        node = stack.pop()
        if isinstance(node, (ft.TextButton, ft.FilledTonalButton,
                             ft.FilledButton, ft.OutlinedButton)):
            c = node.content
            label = c if isinstance(c, str) else str(getattr(c, "value", ""))
            out.append((label, node))
        stack.extend(getattr(node, "controls", None) or [])
        child = getattr(node, "content", None)
        if isinstance(child, (list, tuple)):
            stack.extend(child)
        elif child is not None:
            stack.append(child)
    return out


class RegistryWiringTests(unittest.TestCase):
    """书源组五目的地注册进 ViewRegistry。"""

    def test_register_source_destinations(self):
        self.assertEqual(len(SOURCE_DESTINATIONS), 5)
        registry = ViewRegistry()
        hub = _FakeHub()
        register_destinations(registry, hub)
        for dest in SOURCE_DESTINATIONS:
            route = registry.try_get(dest)
            self.assertIsNotNone(route, f"目的地未注册: {dest}")
            self.assertIsInstance(route.factory(), ft.Column)


class ImportLintTests(unittest.TestCase):
    """§7.5 import-lint 守门：views/source 零 app.services.* 命中（AST 扫描）。"""

    _ROOT = Path(__file__).resolve().parent.parent / "app" / "views" / "source"

    def test_no_services_import_in_source_views(self):
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
                    if mod and (mod == "app.services" or mod.startswith("app.services.")):
                        offenders.append(f"{py.name}:{node.lineno}: import {mod}")
        self.assertEqual(
            offenders, [],
            "views/source/ 的 import 语句不得引用 app.services.*（红线 §7.5）：\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
