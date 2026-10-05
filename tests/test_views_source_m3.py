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
8. import-lint AST 扫描：``app/views/source/`` 零 ``app.services.`` import；
9. 自愈页选择器流程（第六十七轮）：列表页带源进来 → 摊本地样本章 → 点「开始诊断」。
   下拉的 ``on_select`` 事件形状那条静态护栏挪去了
   ``test_controller_service_surface.py``（它扫的是整片视图面，不只书源域）。
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
                             RepairProgressEvent, RepairReportReadyEvent,
                             SourceChangedEvent)
from app.core.registry import SOURCE_DESTINATIONS, ViewRegistry
from app.services.rule_repair import RepairReport, RepairSample, RuleFix
from app.services.sources.online_import_service import OnlineImportService
from app.services.sources.rule_repair_service import (CandidateFace,
                                                      RuleRepairService)
from app.services.sources.source_check_service import SourceCheckService
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

    def test_list_controller_takes_the_write_back_event(self):
        """写回事件的接收端也要有：发出去没人接等于没发。

        服务面那条（``update_source_rule`` 落盘后发 ``updated``）钉的是"发"，
        这一条钉"收"——两条合起来才是"自愈写回之后列表页自己刷新"。
        """
        hub = _FakeHub()
        controller = BookSourceListController(hub)
        controller.attach(BookSourceListView())
        self.addCleanup(controller.detach)
        carriers = [h for etype, h in hub.bus.subscriptions
                    if etype is SourceChangedEvent]
        self.assertEqual(len(carriers), 1,
                         "列表页对 SourceChangedEvent 的订阅应当正好一条")
        # 挂在总线上的是界面编组壳 ``on_ui``（app/controllers/base.py:_marshal），
        # 真处理器在它的闭包里——只断"订阅了"照不出接的是别的路。
        wanted = controller._on_source_changed
        self.assertTrue(any(cell.cell_contents == wanted
                            for cell in (carriers[0].__closure__ or ())),
                        "订阅了却没接到 _on_source_changed 这条刷新路上")


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

    def test_repeat_result_replaces_in_place_without_losing_rows(self):
        """去重改用 source_id 索引以后，"覆盖第 2 行"不能变成"覆盖错行"或掉行。

        全库扫描是几百到上千行、每行都可能重测（快照重放也走这条），所以这条
        钉三件事：行数不涨、位置不动、内容确实是那一条被换掉。
        """
        view = CheckView()
        for source_id in ("https://a/", "https://b/", "https://c/"):
            view.render_result(source_id, "unknown", "首测")
        view.render_result("https://b/", "fully_usable", "重测")
        self.assertEqual([row.key for row in view._rows.controls],
                         ["https://a/", "https://b/", "https://c/"])
        self.assertEqual(view._rows.controls[1].subtitle.value, "完整可用 · 重测")
        self.assertEqual(view._rows.controls[0].subtitle.value, "待确认 · 首测")
        # 索引表必须与行表一致：漏一条就是下一次重测跑到错误的下标上
        self.assertEqual(len(view._row_at), len(view._rows.controls))

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
        # 空态不再是"等待自愈报告"：这一页现在自己就能起诊断，空态得把人指回选择器。
        self.assertIn("选一个书源", view._status.value)

    def test_closed_dropdowns_carry_a_readable_semantic_label(self):
        """读屏面：合着的下拉在语义树里没有字，选中值得由外层 Semantics 念出来。

        flet 0.86.5 的 ``Dropdown`` 根本没有 ``aria_label`` 这个字段（当场反射过
        字段表），而第六十七轮浏览器探针量到两条下拉是 ``role=button`` +
        ``aria-expanded``、``aria-label`` 与 ``textContent`` **全空**（值只画在
        canvas 上，选项文字只在展开那一刻在树里）。三条都要钉：没选中说"未选择"、
        程序改 value 跟得上、用户点选也跟得上——少一条就回到"念不出选的是谁"。
        """
        view = RuleRepairView()
        row = view.controls[1]
        self.assertIn(view._source_sem, row.controls,
                      "标签没挂上工具行：读屏还是念不到")
        self.assertIs(view._source_sem.content, view._source_dd)
        self.assertIs(view._chapter_sem.content, view._chapter_dd)

        view.set_source_options([(_PO5, "源A（正文规则坏了）"),
                                 ("https://b/", "源B")], current="")
        self.assertEqual(view._source_sem.label, "书源：未选择")
        view.set_source_options([("https://c/", "源C")])       # 单条候选会自动选中
        self.assertEqual(view._source_sem.label, "书源：源C")
        view.set_source_options([(_PO5, "源A（正文规则坏了）"),
                                 ("https://b/", "源B")], current="https://b/")
        self.assertEqual(view._source_sem.label, "书源：源B")
        _select(view._source_dd, _PO5)                         # 用户点选那一条
        self.assertEqual(view._source_sem.label, "书源：源A（正文规则坏了）")

        view.set_chapter_options([("0:0", "第1章 开局"), ("0:1", "第2章 转折")],
                                 current="0:1")
        self.assertEqual(view._chapter_sem.label, "正文章节：第2章 转折")

    def test_the_manual_box_reads_back_what_was_typed(self):
        """手填地址那只框：读屏既要念得出「要填什么」，也要念得出「填了什么」。

        第七十一轮探针量到的形状是裸 ``TextField`` 在语义树里 role/aria-label/
        textContent **三样全空**（连 label 文案都不进树），敲进去一整串地址之后
        树里仍然一个字没有；而 ``Semantics`` 的标签确实落进了树
        （group 节点带 ``aria-label``），并且套上之后键盘仍然打得进（8 拍
        ``on_change`` 全收到）。所以钉三条：标签包着这只框、打字那拍标签跟上、
        没填时明说没填。
        """
        view = RuleRepairView()
        self.assertIs(view._manual_sem.content, view._manual)
        self.assertIn(view._manual_sem, view.controls,
                      "挂上标签的是外壳，列里却是裸框——等于没套")

        view.set_manual_visible(True)
        _type(view._manual, f"{_PO5}/12345.html")
        self.assertIn("12345.html", view._manual_sem.label)

        _type(view._manual, "")
        self.assertEqual(view._manual_sem.label, "正文页地址：还没填")

    def test_hiding_the_manual_box_hides_the_reader_label_too(self):
        """藏框要连标签一起藏：只藏里面的话，读屏会念出一条根本不存在的出口。"""
        view = RuleRepairView()
        view.set_manual_visible(True)
        self.assertTrue(view._manual_sem.visible)
        view.set_manual_visible(False)
        self.assertFalse(view._manual.visible)
        self.assertFalse(view._manual_sem.visible, "外壳没跟着收，树里还挂着这只框")

    def test_reader_wrapper_and_its_field_always_share_visibility(self):
        """``Semantics`` 外壳与它包的那只控件必须**同进同退**。

        第七十二轮现场报错：这一页起手时手填框是藏着的、外壳还亮着，客户端建树当场
        提示 ``Semantics.content must be provided and visible``，整页起不来。反过来
        （外壳藏了、框亮着）是另一半：框在屏上看得见，读屏却念不出"这是填什么的"。
        两个方向都不能靠"点一遍"钉死，只能钉形状——任何时刻树上都不许有一对
        可见性不一致的壳/芯。
        """
        def offenders(view):
            bad = []
            stack = list(view.controls)
            while stack:
                node = stack.pop()
                for child in _children(node):
                    stack.append(child)
                inner = getattr(node, "content", None)
                if isinstance(node, ft.Semantics) and isinstance(inner, ft.Control):
                    if bool(getattr(node, "visible", True)) \
                            != bool(getattr(inner, "visible", True)):
                        bad.append((node, inner))
            return bad

        view = RuleRepairView()
        self.assertEqual([], offenders(view), "起手就有壳/芯可见性不一致的一对")
        for flag in (True, False, True, False):
            view.set_manual_visible(flag)
            self.assertEqual([], offenders(view), f"切到 visible={flag} 之后出现了")

    def test_the_proposed_rule_is_readable_not_only_copyable(self):
        """报告卡里的规则串不许再用 ``selectable=True``：可复制文本在语义树里是空节点。

        第七十一轮矩阵探针：普通 Text ✅ 在树里；``selectable=True`` 的 Text（中英
        都一样）❌ 空节点；外面套 ``Semantics`` ❌ 连标签一起消失；外层
        ``SelectionArea`` 包普通 Text ✅ 照样在树里。这两串是用户点「应用并重试」
        之前唯一能读到「要写回什么」的地方，看不见就等于这一页不能独自完成。
        """
        view = RuleRepairView()
        view.render_report({
            "report_id": "r", "source_name": "源A", "has_fix": True,
            "old_value": ".con@html", "new_value": ".con@html||.content@text",
            "samples": [{"chapter": "第2章", "chars": 1834, "snippet": "下雨了"}]})
        for value in (".con@html", ".con@html||.content@text"):
            node = _find_text(view._card, value)
            self.assertIsNotNone(node, f"{value!r} 不在卡里")
            self.assertFalse(node.selectable,
                             f"{value!r} 是可复制文本，语义树里读不到")
            path = _path_to_text(view._card, value)
            self.assertTrue(any(isinstance(c, ft.SelectionArea) for c in path),
                            f"{value!r} 没了 selectable 也就没了复制面")

    def test_the_no_fix_reason_is_readable_too(self):
        """没找到可写回规则那一面：原因那句原先也是 selectable 的。"""
        view = RuleRepairView()
        view.render_report({"source_name": "源A", "has_fix": False,
                            "reason": "这一页被拦截了，取不到正文。"})
        node = _find_text(view._card, "这一页被拦截了")
        self.assertIsNotNone(node)
        self.assertFalse(node.selectable, "原因念不出来，用户只知道「没改成」")

    def test_merged_source_option_updates_the_label(self):
        """列表页带源进来那条路是 ``add_source_option``（程序改 value，不走 on_select）。"""
        view = RuleRepairView()
        view.set_source_options([(_PO5, "源A")], current="")
        view.add_source_option("https://other/", "别源")
        self.assertEqual(view._source_sem.label, "书源：别源")


class _Settled:
    """``future.result()`` 的两面：有值返回值，有异常原样抛给回调。"""

    def __init__(self, value=None, error=None):
        self._value, self._error = value, error

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _DeferredFuture:
    """ui_call 的替身：截下协程等测试来 await，done 回调由 run() 手动触发。"""

    def __init__(self, coro, posted):
        self._coro = coro
        self._callbacks: list = []
        posted.append(self)

    def add_done_callback(self, fn):
        self._callbacks.append(fn)

    async def run(self):
        # 异常要照真 future 的样子交给 done 回调（真 ui_call 就是这么把失败露给
        # _after_* 的），只在最后再往外抛一次给测试断言。
        value, error = None, None
        try:
            value = await self._coro if self._coro is not None else None
        except BaseException as exc:  # noqa: BLE001 - 原样转交，不吞
            error = exc
        for fn in self._callbacks:
            fn(_Settled(value, error))
        if error is not None:
            raise error


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
        before = view._status.value
        await controller._on_progress(RepairProgressEvent(
            source_id="https://a/", stage="正在抓取样本页…"))
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id="https://a/"))
        self.assertEqual(view._status.value, before)
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


# --------------------------------------------------------------------------- #
# 自愈页的选择器：列表页带源进来 → 摊本地样本章 → 点「开始诊断」才出网
# --------------------------------------------------------------------------- #
_REPAIR_GROUPS = [{
    "book_url": "https://b/1", "book_name": "《测试书》",
    "chapters": [{"name": f"第{i}章", "url": f"https://b/1/{i}.html"}
                 for i in (1, 2, 3)],
}]
_PO5 = "https://www.po5.net"


def _select(dropdown, value):
    """模拟用户点下拉项：Flet 先改 ``control.value``，再发一条 ``Event``。

    事件对象上只有 name/data/control，**没有** ``value``——所以这里必须走真事件，
    否则视图 lambda 里那行读法（``e.control.value``）从来没被执行过，
    正是第六十七轮浏览器探针抓出来的那一族（替身 ``SimpleNamespace(value=…)``
    比真事件多一个字段，测试全绿而界面上点了没反应）。
    """
    dropdown.value = value
    dropdown.on_select(ft.Event(name="select", control=dropdown))


def _type(field, value):
    """模拟用户在手填框里敲字：Flet 先改 ``control.value``，再发一条 ``Event``。

    和 ``_select`` 同一条红线：程序改 value 不会触发 ``on_change``，读屏标签
    跟不跟得上只有走真事件才测得到。
    """
    field.value = value
    field.on_change(ft.Event(name="change", control=field))


def _children(control) -> list:
    out = list(getattr(control, "controls", None) or [])
    inner = getattr(control, "content", None)
    if isinstance(inner, ft.Control):
        out.append(inner)
    return out


def _path_to_text(control, needle, depth=0) -> list:
    """从 control 到那串文字所在节点的路径（找不到回空表）。

    要的是**路径**不是节点：钉"这串在 SelectionArea 底下"得看得到它的祖先。
    """
    if depth > 12:
        return []
    for child in _children(control):
        if isinstance(child, ft.Text) and needle in str(child.value or ""):
            return [control, child]
        deeper = _path_to_text(child, needle, depth + 1)
        if deeper:
            return [control] + deeper
    return []


def _find_text(control, needle) -> ft.Text | None:
    path = _path_to_text(control, needle)
    return path[-1] if path else None


class RepairPickerFlowTests(unittest.IsolatedAsyncioTestCase):
    """自愈页必须自己起得来诊断：从列表页跳进来不再是"等待自愈报告…"的空白页。"""

    def _make(self, *, groups=None, census=None, snapshot=None, report=None):
        rows = _REPAIR_GROUPS if groups is None else groups
        face = census if census is not None else CandidateFace(
            groups=list(rows), shelf_books=len(rows), cached_books=len(rows),
            shown_books=len(rows),
            rows=sum(len(group["chapters"]) for group in rows))
        repair = SimpleNamespace(
            get_report=lambda rid: report,
            apply_fix=AsyncMock(return_value=True),
            chapter_candidates=AsyncMock(return_value=face),
            repair=AsyncMock(return_value=report))
        source = SimpleNamespace(
            list_sources=AsyncMock(return_value=[
                SimpleNamespace(bookSourceUrl=_PO5, bookSourceName="源A")]),
            load_source=AsyncMock(return_value=SimpleNamespace(bookSourceUrl=_PO5)))
        check = SimpleNamespace(run_snapshot=AsyncMock(
            return_value=snapshot if snapshot is not None else {"results": []}))
        hub = _FakeHub({RuleRepairService: repair, SourceService: source,
                        SourceCheckService: check})
        view = RuleRepairView()
        controller = RuleRepairController(hub)
        # on_attach 自己就发一次拉书源的编排，所以 ui_call 必须**在 attach 之前**换掉
        # （同 test_source_rule_editor 的 _run_now 约定）。
        posted: list = []
        controller.ui_call = lambda coro: _DeferredFuture(coro, posted)
        controller.attach(view)
        self.addCleanup(controller.detach)
        return controller, view, repair, source, posted

    async def _drain(self, posted):
        """排干待跑编排：拉书源那趟会再排一次"摊章节"，所以要循环到没有为止。"""
        while posted:
            await posted.pop(0).run()

    def test_view_offers_two_pickers_and_a_start_button(self):
        view = RuleRepairView()
        self.assertIsInstance(view._source_dd, ft.Dropdown)
        self.assertIsInstance(view._chapter_dd, ft.Dropdown)
        self.assertFalse(view._manual.visible,
                         "粘贴框是零样本时的出口，不该一进来就占地方")
        self.assertEqual(set(view.get_selection()),
                         {"source_url", "chapter_key", "manual_url"})

    async def test_attach_wires_every_new_slot(self):
        """"按钮存在但没人接"正是这一页原来的病：四个新槽一个都不许留空。"""
        controller, view, repair, source, posted = self._make()
        for slot in ("on_start_diagnosis", "on_source_picked",
                     "on_chapter_picked", "on_reload_sources"):
            self.assertIsNotNone(getattr(view, slot), f"{slot} 没接：控件成了死键")
        await self._drain(posted)

    async def test_payload_from_the_list_page_lands_and_loads_chapters(self):
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        repair.chapter_candidates.reset_mock()
        controller.load_record({"source_url": "https://other/",
                                "source_name": "别源", "origin": "sources.list"})
        await self._drain(posted)
        self.assertEqual(view._source_dd.value, "https://other/",
                         "列表页带来的源没进下拉，用户还得自己找一遍")
        repair.chapter_candidates.assert_awaited_once_with("https://other/")
        self.assertEqual([o.key for o in view._chapter_dd.options],
                         ["0:0", "0:1", "0:2"])
        self.assertIn("真抓 1-3 页", view._hint.value)
        self.assertFalse(view._manual.visible,
                         "有得挑了粘贴框还挂着，两个入口会互相抢")

    async def test_the_hint_names_the_books_that_got_left_out(self):
        """摊不到的那几本要在界面上说出来：被预算切掉的东西不能是静默消失的。

        普查数字当场写死（在架 12 / 存过目录 9 / 摊了 1），不从服务模块读常量——
        读常量的写法在常量被改掉时会跟着改口（第七十轮变异核验抓到的那一族）。
        """
        census = CandidateFace(groups=list(_REPAIR_GROUPS), shelf_books=12,
                               cached_books=9, shown_books=1, rows=3)
        controller, view, repair, source, posted = self._make(census=census)
        await self._drain(posted)
        hint = view._hint.value
        self.assertIn("在架 12 本", hint, "提示行没报这个源一共有几本书")
        self.assertIn("读过 9 本", hint)
        self.assertIn("还有 8 本没摊到", hint,
                      "9 本只摊了 1 本，界面上却一句没提——那就是静默丢书")

    async def test_a_fully_covered_shelf_does_not_apologise(self):
        """反面：全都摊到了就不许再念"没摊到"，否则提示行永远在报一个不存在的洞。"""
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        self.assertIn("真抓 1-3 页", view._hint.value)
        self.assertNotIn("没摊到", view._hint.value)

    async def test_picking_the_source_by_hand_reloads_chapters(self):
        """用户自己点下拉那一条路：视图读的是 ``e.control.value``。

        上面那条走的是列表页带进来的 payload（程序改 value，不触发 on_select），
        所以这一条必须发**真事件**——探针抓出六个死下拉时，缺的正是这一条。
        """
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        repair.chapter_candidates.reset_mock()
        _select(view._source_dd, _PO5)
        await self._drain(posted)
        repair.chapter_candidates.assert_awaited_once_with(_PO5)
        self.assertEqual([o.key for o in view._chapter_dd.options],
                         ["0:0", "0:1", "0:2"],
                         "手点的源没换成它的章节，端上来的还是上一源的样本")

    async def test_start_diagnosis_passes_chapter_followers_and_book(self):
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        _select(view._chapter_dd, "0:1")
        self.assertIn("复核还会带上：第3章", view._hint.value)
        view._start_btn.on_click(None)
        self.assertEqual(len(posted), 1, "点了开始诊断却没把任务排出去")
        self.assertTrue(view._start_btn.disabled, "在途没被按死：连点两下就是两趟诊断")
        self.assertTrue(view._spinner.visible)
        self.assertFalse(view._card.visible)
        await self._drain(posted)
        repair.repair.assert_awaited_once()
        args, kwargs = repair.repair.await_args
        self.assertEqual(args[0].bookSourceUrl, _PO5)
        self.assertEqual(args[1].url, "https://b/1/2.html")
        self.assertEqual([c.url for c in kwargs["extra_chapters"]],
                         ["https://b/1/3.html"], "复核样本没带上紧跟的那一章")
        self.assertEqual(kwargs["book"].url, "https://b/1")
        self.assertFalse(view._start_btn.disabled, "跑完了按钮还按着，这一页就废了")
        self.assertFalse(view._spinner.visible)

    async def test_second_click_while_running_dispatches_nothing(self):
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        view._chapter_dd.value = "0:0"
        view._start_btn.on_click(None)
        view._start_btn.on_click(None)
        self.assertEqual(len(posted), 1,
                         "在途又点一次：同一源派了两趟诊断，两趟报告互相盖")
        await self._drain(posted)

    async def test_local_pseudo_source_is_refused_before_any_dispatch(self):
        """本地书在书架里**有**缓存目录，候选面却绝不能把它当正常源端出来。"""
        from app.services.content.local_book_service import LOCAL_BOOK_SOURCE_URL
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        repair.chapter_candidates.reset_mock()
        controller.load_record({"source_url": LOCAL_BOOK_SOURCE_URL,
                                "source_name": "本地书籍"})
        await self._drain(posted)
        self.assertIn("本地", view._status.value)
        repair.chapter_candidates.assert_not_awaited()
        self.assertEqual(view._chapter_dd.options, [],
                         "本地书的缓存目录被端上了候选面")
        view._manual.value = "file:///tmp/x.txt"
        view._start_btn.on_click(None)
        self.assertEqual(posted, [], "本地源还是派了诊断——那一趟只会去抓 file://")

    async def test_new_diagnosis_collapses_the_stale_report_card(self):
        """旧报告的卡必须先收起来：让它挂着等于叫用户确认一条不相干的规则。"""
        report = RepairReport(RuleFix(
            source_name="源A", source_url=_PO5, field="content",
            old_value="#nr1@html", new_value="#nr1@html||.txt@text",
            samples=(), page_url=_PO5, note=""))
        controller, view, repair, source, posted = self._make(report=report)
        await self._drain(posted)
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id=_PO5))
        self.assertTrue(view._card.visible)
        view._chapter_dd.value = "0:0"
        view._start_btn.on_click(None)
        self.assertFalse(view._card.visible)
        await self._drain(posted)

    async def test_zero_candidates_opens_the_manual_box_not_a_dead_button(self):
        controller, view, repair, source, posted = self._make(groups=[])
        await self._drain(posted)
        self.assertTrue(view._manual.visible,
                        "本地一章都没缓存过，粘贴框却没亮——用户只剩下一个"
                        "点了只会说『挑一章』的按钮")
        view._start_btn.on_click(None)
        self.assertEqual(posted, [], "没有样本章却照样派活")
        self.assertIn("挑一章", view._status.value)

        view._manual.value = "b/9.html"
        view._start_btn.on_click(None)
        self.assertEqual(posted, [], "相对地址派出去只会白抓一次")
        self.assertIn("http(s)://", view._status.value)

        view._manual.value = f"{_PO5}/9.html"
        view._start_btn.on_click(None)
        self.assertEqual(len(posted), 1, "手填了地址就该能起诊断")
        await self._drain(posted)
        self.assertEqual(repair.repair.await_args.args[1].url, f"{_PO5}/9.html")
        self.assertEqual(repair.repair.await_args.kwargs["extra_chapters"], [])

    async def test_check_samples_fill_a_source_never_read(self):
        """没读过、只检过的源：上一轮检测抽到的三章是唯一免联网的样本来源。"""
        controller, view, repair, source, posted = self._make(
            groups=[], snapshot={"results": [
                {"source_id": "https://别的/", "sample_chapter_urls": ["x"]},
                {"source_id": _PO5,
                 "sample_chapter_urls": [f"{_PO5}/7.html", f"{_PO5}/8.html"]}]})
        await self._drain(posted)
        self.assertEqual([o.key for o in view._chapter_dd.options], ["0:0", "0:1"],
                         "唯一那一源已被选中，章节却没跟着摊开")
        self.assertEqual(controller._candidate_groups[0]["book_name"],
                         "上次检测抽到的章节", "摊错了来源：那是检测样本，不是书架目录")
        self.assertFalse(view._manual.visible)
        view._chapter_dd.value = "0:1"
        view._start_btn.on_click(None)
        await self._drain(posted)
        self.assertEqual(repair.repair.await_args.args[1].url, f"{_PO5}/8.html")
        self.assertEqual(repair.repair.await_args.kwargs["extra_chapters"], [],
                         "检测样本是拼出来的三章，彼此没有同书后续章可复核")

    async def test_check_snapshot_is_matched_by_source_not_taken_at_random(self):
        """快照里排第一的可能是别的源：不看 source_id 就端上别源的章节。"""
        controller, view, repair, source, posted = self._make(
            groups=[], snapshot={"results": [
                {"source_id": _PO5, "sample_chapter_urls": [f"{_PO5}/7.html"]},
                {"source_id": "https://别的/",
                 "sample_chapter_urls": ["https://别的/9.html"]}]})
        await self._drain(posted)
        self.assertEqual([c["url"] for c in controller._candidate_groups[0]["chapters"]],
                         [f"{_PO5}/7.html"], "端上来的章节地址来自另一个源")
        self.assertFalse(view._manual.visible)

    async def test_report_from_another_source_does_not_steal_this_page(self):
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        view._chapter_dd.value = "0:0"
        view._start_btn.on_click(None)
        before = view._status.value
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="rx", source_id="https://别的源/"))
        self.assertEqual(view._status.value, before)
        self.assertFalse(view._card.visible)
        await self._drain(posted)
        await controller._on_report_ready(RepairReportReadyEvent(
            report_id="r1", source_id=_PO5))
        self.assertFalse(view._spinner.visible)

    async def test_inflight_candidate_load_after_detach_touches_no_widgets(self):
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        before = [o.key for o in view._chapter_dd.options]
        view._hint.value = "哨兵"
        controller.load_chapter_options(_PO5)
        controller.detach()          # 摊候选还没跑完人就切了页
        await self._drain(posted)
        self.assertEqual(view._hint.value, "哨兵")
        self.assertEqual([o.key for o in view._chapter_dd.options], before)

    async def test_diagnose_failure_leaves_the_page_startable_again(self):
        """诊断炸了不许把按钮永久按死：在途形状必须收掉，闸门的账也要抹平。"""
        controller, view, repair, source, posted = self._make()
        await self._drain(posted)
        source.load_source = AsyncMock(return_value=None)
        view._chapter_dd.value = "0:0"
        view._start_btn.on_click(None)
        with self.assertRaises(RuntimeError):
            await self._drain(posted)   # 源取不到 = 协程抛错，走的是 _after_repair 那面
        self.assertIn("没有书源", view._status.value)
        self.assertFalse(view._start_btn.disabled)
        self.assertFalse(view._spinner.visible)
        posted.clear()
        view._start_btn.on_click(None)
        self.assertEqual(len(posted), 1, "失败一次之后这一页再也起不动诊断")
        posted.pop(0)._coro.close()   # 这一趟不再跑：丢给 GC 只会喊 never awaited


class RepairNavigationTests(unittest.TestCase):
    """列表页那颗「自愈」：跳过去要带着勾上的那一源，别让自愈页再问一遍。"""

    def _make(self):
        view = BookSourceListView()
        controller = BookSourceListController(_FakeHub({}))
        # 这一页本轮只测导航，不测它的加载编排：协程就地关掉，别留 never-awaited
        controller.ui_call = lambda coro: (coro.close(), _Settled())[1]
        jumped: list = []
        controller.attach(view)
        controller.on_open_repair = lambda *payload: jumped.append(payload)
        return controller, view, jumped

    def test_unselected_jump_carries_no_payload(self):
        """没勾选就只跳页：塞一个空 source_url 过去，自愈页会把它当选中源显示。"""
        controller, view, jumped = self._make()
        view._repair_btn.on_click(None)
        self.assertEqual(jumped, [()])

    def test_selection_travels_as_a_two_key_payload(self):
        controller, view, jumped = self._make()
        view._repair_btn.on_click(None)
        controller._on_selection_changed("https://b/", True)
        controller._on_selection_changed("https://a/", True)
        view._repair_btn.on_click(None)
        self.assertEqual(jumped[-1], ({"source_url": "https://a/",
                                       "source_name": "https://a/"},),
                         "载荷里混进了章节或检测结果：自愈页只能靠猜键名取值")
        self.assertIn("一次只诊断一个源", view._status.value,
                      "多选只带走一条却没说带走的是哪条")


if __name__ == "__main__":
    unittest.main()
