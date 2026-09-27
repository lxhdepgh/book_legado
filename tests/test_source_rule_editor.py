"""「规则」页签（书源规则编辑器）的重做护栏（设计 §1.6，第二十三轮）。

这一页原来有三处"看着像坏了"：

1. 从导航直接进是**空编辑器**——唯一的入口是书源列表点「编辑」把记录跳过来，
   页签自己不会列书源；
2. 输入框下面挂一份**同样内容的只读着色副本**（"语法高亮预览"），一整块高度用来
   重复上面已经写着的东西；
3. 「试跑调试」求值的字段硬编码 ``bookNameRule``、示例正文永远为空 → 点必然回
   "（示例为空…）"，等于一个不响的按钮。

本轮把**编辑单位**改成"选书源 → 选一条规则字段"：编辑器只装这一条规则，保存时
由控制器把它合并回原书源 dict 再落库（其它键一字不动），试跑用视图里粘进来的
示例正文（**本页不联网**，记忆 legado-live-scan-av-block）。

全程离线：假服务记账每个动作（"没发生"也要断言，见记忆
guard-tests-assert-absence），不启真 EngineHub（把 ``ui_call`` 换成就地跑）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import unittest
from types import SimpleNamespace

import flet as ft

from app.controllers.source.code_editor_controller import CodeEditorController
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.sources.compat_audit_service import CompatAuditService
from app.services.sources.source_service import SourceService
from app.views.source.code_editor_view import (WHOLE_SOURCE, CodeEditorView,
                                               tokenize_rule)


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


class _FakeSource:
    """书源服务替身：记账保存了什么，返回什么。"""

    def __init__(self, records=None):
        self.records = list(records or [])
        self.saved: list[dict] = []

    async def list_sources(self):
        return list(self.records)

    async def load_source(self, url):
        return next((r for r in self.records
                     if r.get("bookSourceUrl") == url), None)

    async def save_source(self, source_json):
        self.saved.append(json.loads(json.dumps(source_json)))   # 深拷贝记账
        return {"saved": True, "source_id": source_json.get("bookSourceUrl", ""),
                "errors": [], "audit": None}


class _FakeEngine:
    def __init__(self, values=("命中甲", "命中乙")):
        self.calls: list[tuple[str, str, str]] = []
        self.values = list(values)

    async def extract_values(self, rule, sample, kind):
        self.calls.append((rule, sample, kind))
        return list(self.values)


class _FakeAudit:
    def __init__(self, report=None):
        self.report = report if report is not None else SimpleNamespace(
            missing_java_apis=["AndroidApi.webkit"], unknown_fields=[],
            missing_object_apis=[], unsupported_pseudos=[],
            js_syntax_errors=[], regex_errors=[])

    async def audit_source(self, source):
        return self.report


class _FakeHub:
    def __init__(self, services=None):
        self.bus = _FakeBus()
        self._services = services or {}

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


def _run_now(ctrl) -> None:
    """把 ``ui_call`` 换成就地跑：测试不等 EngineHub，回调顺序可断言。

    必须在 **attach 之前**换：``on_attach`` 自己就发一次拉书源的编排。
    """
    def run(coro):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - 交给 done 回暴露
            fut.set_exception(exc)
        return fut
    ctrl.ui_call = run


def _rig(records=None, *, engine=None, with_source=True):
    """装好的一页：真视图 + 替身服务，attach 完（书源列表已经拉好）。"""
    services = {CompatAuditService: _FakeAudit()}
    if with_source:
        services[SourceService] = _FakeSource(records)
    if engine is not None:
        services[RuleEngineService] = engine
    view = CodeEditorView()
    controller = CodeEditorController(_FakeHub(services))
    _run_now(controller)
    controller.attach(view)
    return view, controller


def _record(url="https://src1/", name="测试书源"):
    return {"bookSourceUrl": url, "bookSourceName": name,
            "bookSourceGroup": "默认",
            "ruleSearch": {"name": ".title@text", "bookList": ".item",
                           "bookUrl": ".a@href"},
            "ruleContent": {"content": "#content@html"}}


# --------------------------------------------------------------------------- #
# 1. 进页面就能选源，不再依赖上一页跳转
# --------------------------------------------------------------------------- #
class SourcesListedOnOpenTests(unittest.TestCase):
    def test_opening_the_tab_lists_the_imported_sources(self):
        view, controller = _rig([_record("https://a/", "甲源"),
                                 _record("https://b/", "乙源")])
        keys = [option.key for option in view._source_dd.options]
        self.assertEqual(keys, ["https://a/", "https://b/"])
        # 只有一本时也预选它：下拉不该空着让人以为没加载
        self.assertEqual(view._source_dd.value, "https://a/")

    def test_without_the_service_the_page_says_so(self):
        view, controller = _rig(with_source=False)
        self.assertEqual(view._source_dd.options, [])
        self.assertIn("未就绪", view._status.value)

    def test_no_sources_yet_points_at_the_import_page(self):
        view, _controller = _rig([])
        self.assertIn("导入", view._status.value)

    def test_picking_a_source_loads_that_record(self):
        view, controller = _rig([_record("https://a/", "甲源")])
        # 走控件的 on_select：验证下拉确实把值递给了控制器（不是只绑了个方法名）
        view._source_dd.on_select(SimpleNamespace(value="https://a/"))
        self.assertIn("甲源", view._title.value)
        self.assertIn("ruleSearch", view.get_text())
        self.assertEqual(controller._current["bookSourceUrl"], "https://a/")


# --------------------------------------------------------------------------- #
# 2. 编辑单位：整份 JSON ↔ 单条规则
# --------------------------------------------------------------------------- #
class FieldScopedEditingTests(unittest.TestCase):
    def setUp(self):
        self.record = _record()
        self.view, self.controller = _rig([self.record])
        self.view._source_dd.on_select(SimpleNamespace(value="https://src1/"))

    def _field_keys(self):
        return [option.key for option in self.view._field_dd.options]

    def test_the_dropdown_offers_only_the_rules_this_source_has(self):
        keys = self._field_keys()
        self.assertEqual(keys[0], WHOLE_SOURCE, "第一项仍是整份 JSON（原能力不缩）")
        self.assertIn("ruleSearch.name", keys)
        self.assertIn("ruleContent.content", keys)
        self.assertNotIn("ruleSearch.wordCount",
                         keys, "这份书源里没有的字段不该列出来让人点开空白")

    def test_labels_are_human_readable(self):
        by_key = {option.key: option.text for option in self.view._field_dd.options}
        self.assertEqual(by_key["ruleSearch.name"], "搜索 · 书名")
        self.assertEqual(by_key["ruleContent.content"], "正文 · 正文")

    def test_choosing_a_field_loads_only_that_rule(self):
        self.view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        self.assertEqual(self.view.get_text(), ".title@text")
        self.assertIn("ruleSearch.name", self.view._status.value)

    def test_switching_back_to_whole_shows_the_stashed_edit(self):
        self.view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        self.view._editor.value = ".h2@text"
        self.view._on_text_changed()
        self.view._field_dd.on_select(SimpleNamespace(value=WHOLE_SOURCE))
        text = self.view.get_text()
        self.assertIn(".h2@text", text, "换回整份 JSON 时刚才那条编辑不能消失")
        self.assertIn("bookSourceGroup", text, "合并只动那一个键")

    def test_saving_one_field_merges_and_keeps_everything_else(self):
        self.view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        self.view._editor.value = ".h3@text"
        self.view._on_text_changed()
        fake = self.controller._source
        self.controller.save(self.view.get_text())
        self.assertEqual(len(fake.saved), 1)
        saved = fake.saved[0]
        self.assertEqual(saved["ruleSearch"]["name"], ".h3@text")
        self.assertEqual(saved["ruleSearch"]["bookList"], ".item",
                         "同组其它规则被顺手改掉了")
        self.assertEqual(saved["bookSourceGroup"], "默认")
        self.assertEqual(saved["bookSourceUrl"], "https://src1/")

    def test_saving_a_field_without_a_loaded_source_is_refused(self):
        """没选源就没有"其它键"可保留：凭空气造一条只有这个键的书源会覆盖库里那条。"""
        view, controller = _rig([_record()])
        controller._current = None
        controller._field = "ruleSearch.name"
        fake = controller._source
        controller.save(".anything@text")
        self.assertEqual(fake.saved, [], "还是落库了")
        self.assertIn("选一个书源", view._audit.value)


# --------------------------------------------------------------------------- #
# 3. 试跑调试：规则取自当前编辑目标，样本必须粘进来
# --------------------------------------------------------------------------- #
class DebugRunTests(unittest.TestCase):
    def test_empty_sample_never_reaches_the_engine(self):
        """没样本就别调引擎：以前那条"（示例为空…）"的回显等于坏按钮。"""
        engine = _FakeEngine()
        view, controller = _rig([_record()], engine=engine)
        view._source_dd.on_select(SimpleNamespace(value="https://src1/"))
        view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        controller.run_debug(view.get_text())
        self.assertEqual(engine.calls, [], "空样本还是去求值了")
        self.assertIn("示例正文", view._debug.value)
        self.assertTrue(view._debug_host.visible)

    def test_debug_runs_the_rule_being_edited_not_a_hardcoded_field(self):
        engine = _FakeEngine()
        view, controller = _rig([_record()], engine=engine)
        view._source_dd.on_select(SimpleNamespace(value="https://src1/"))
        view._field_dd.on_select(SimpleNamespace(value="ruleContent.content"))
        view.set_sample("<div id='content'>正文一段</div>")
        controller.run_debug(view.get_text())
        self.assertEqual(engine.calls, [("#content@html",
                                        "<div id='content'>正文一段</div>", "html")])
        self.assertIn("命中甲", view._debug.value)

    def test_jsonpath_rules_are_evaluated_as_json(self):
        engine = _FakeEngine()
        view, controller = _rig([_record()], engine=engine)
        controller._field = "ruleSearch.name"
        view.set_sample('{"data":[{"title":"甲"}]}')
        controller.run_debug("$.data[*].title")
        self.assertEqual(engine.calls[0][0], "$.data[*].title")
        self.assertEqual(engine.calls[0][2], "json")

    def test_whole_json_mode_still_debugs_and_says_which_field(self):
        engine = _FakeEngine()
        view, controller = _rig([_record()], engine=engine)
        view._source_dd.on_select(SimpleNamespace(value="https://src1/"))
        view.set_sample("<div class='item'>甲</div>")
        controller.run_debug(view.get_text())
        self.assertIn("bookNameRule", view._debug.value,
                      "整份 JSON 模式要说清试跑的是哪条字段")
        self.assertEqual(engine.calls, [])

    def test_without_the_engine_the_rule_still_shows_as_parsed(self):
        view, controller = _rig([_record()])
        controller._field = "ruleSearch.name"
        view.set_sample("<p>x</p>")
        controller.run_debug(".title@text")
        self.assertIn("未就绪", view._debug.value)


# --------------------------------------------------------------------------- #
# 3b. 试跑落在**真服务的表面**上：替身有方法、真服务没有，绿灯就是骗人的
# --------------------------------------------------------------------------- #
class RealEngineSurfaceTests(unittest.TestCase):
    """第二十六轮浏览器面实测的实错：单测全绿，按钮一次都没取出过值。

    ``RuleEngineService`` 从来没有 ``extract_values``，控制器那句 ``getattr`` 探测
    永远落空，于是回落到 ``select_nodes(sample, kind, "")``——空列表规则在一段正文里
    一个容器都选不出来，求值对象是空列表，任何规则都只会回"没取到值"。而替身自己
    定义了这个方法，所以那条回落分支从来没被任何用例走过。
    """

    def test_the_service_actually_offers_single_field_evaluation(self):
        hits = RuleEngineService().extract_values(
            ".title@text", "<div class='title'>远航</div>", "html")
        self.assertEqual(hits, ["远航"],
                         "服务面上取不到值，「试跑当前规则」就是坏的")

    def test_the_service_reads_json_samples_as_json(self):
        hits = RuleEngineService().extract_values(
            "$.data[*].title", '{"data":[{"title":"远航"},{"title":"归途"}]}', "json")
        self.assertEqual(hits, ["远航", "归途"])

    def test_the_button_end_to_end_runs_on_the_real_service(self):
        """不装替身：真服务接进 rig，粘样本点试跑，屏幕上得是真取到的值。"""
        view, controller = _rig([_record()], engine=RuleEngineService())
        view._source_dd.on_select(SimpleNamespace(value="https://src1/"))
        view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        view.set_sample("<div class='item'><span class='title'>远航</span></div>")
        controller.run_debug(view.get_text())
        self.assertIn("远航", view._debug.value)
        self.assertNotIn("没取到值", view._debug.value)


# --------------------------------------------------------------------------- #
# 4. 关键字识别区不再重复正文
# --------------------------------------------------------------------------- #
class HighlightPanelTests(unittest.TestCase):
    def _span_texts(self, view):
        return [span.text for span in view._highlight.spans]

    def test_the_panel_lists_tokens_not_the_text_again(self):
        view = CodeEditorView()
        view.render_highlight(".item || $.title <js>a()</js>")
        texts = self._span_texts(view)
        self.assertIn("||", texts)
        self.assertIn("$.title", texts)
        self.assertIn("<js>a()</js>", texts)
        self.assertNotIn(".item || $.title <js>a()</js>", texts,
                         "整行正文被照抄 = 又做了一份副本")
        self.assertTrue(view._analysis_host.visible)
        self.assertIn("识别到 3 处", view._analysis_summary.value)

    def test_json_keys_are_not_reported_as_keywords(self):
        """整份 JSON 模式：键名都是引号串，全列出来等于把文本再抄一遍。"""
        view = CodeEditorView()
        view.render_highlight(json.dumps(
            {"bookSourceUrl": "https://x/", "bookSourceName": "甲",
             "bookSourceGroup": "默认"}, ensure_ascii=False))
        self.assertFalse(view._analysis_host.visible,
                         "整份 JSON 里只有键名引号串时不该亮起来")

    def test_a_rule_inside_json_is_still_recognised(self):
        view = CodeEditorView()
        view.render_highlight('{"ruleSearch": {"name": ".title@text"}}')
        self.assertIn("@text", self._span_texts(view))

    def test_tokens_are_deduped_and_capped(self):
        view = CodeEditorView()
        view.render_highlight(" ".join([".a@text"] * 60))
        self.assertIn("识别到 60 处", view._analysis_summary.value)
        self.assertLessEqual(len(self._span_texts(view)),
                             CodeEditorView._MAX_TOKENS * 2)

    def test_tokenizer_still_classifies_segments(self):
        kinds = {kind for _text, kind in
                 tokenize_rule('.list@text || $.title <js>x()</js>')}
        self.assertTrue({"op", "jsonpath", "jsblock"} <= kinds)


# --------------------------------------------------------------------------- #
# 5. 装配细节：不能凭空造选中值、不能碰已拆掉的视图
# --------------------------------------------------------------------------- #
class WiringTests(unittest.TestCase):
    def test_dropdown_never_selects_a_source_it_was_not_given(self):
        """value 填一个不在 options 里的键，flet 渲染成空白 = 看着像加载坏了。"""
        view, _controller = _rig([_record("https://a/", "甲源")])
        view.set_source_options([("https://a/", "甲源")], current="https://zzz/")
        self.assertIsNone(view._source_dd.value)
        view.set_source_options([("https://a/", "甲源")], current="https://a/")
        self.assertEqual(view._source_dd.value, "https://a/")

    def test_the_loaded_record_is_merged_into_the_list_not_replacing_it(self):
        """从书源列表跳转过来只带一条记录：不能把已经列好的其它源抹掉。"""
        view, controller = _rig([_record("https://a/", "甲源"),
                                 _record("https://b/", "乙源")])
        controller.load_source(_record("https://c/", "丙源"))
        keys = [option.key for option in view._source_dd.options]
        self.assertEqual(keys, ["https://a/", "https://b/", "https://c/"])
        self.assertEqual(view._source_dd.value, "https://c/")

    def test_reloading_a_source_already_in_the_list_does_not_duplicate_it(self):
        """判重：同一个源反复载入（保存后从列表又跳回来）下拉不能长成 a、a、a。"""
        view, controller = _rig([_record("https://a/", "甲源")])
        controller.load_source(_record("https://a/", "甲源"))
        controller.load_source(_record("https://a/", "甲源"))
        self.assertEqual([o.key for o in view._source_dd.options], ["https://a/"])

    def test_in_flight_callbacks_after_detach_do_not_touch_widgets(self):
        """切页时正在拉书源/正在审计：协程要跑完，但不能再碰已经拆掉的控件。"""
        view, controller = _rig([_record()])
        touched: list[str] = []
        for name in ("set_source_options", "add_source_option", "set_title",
                     "set_text", "set_status", "set_field_options",
                     "set_audit_report", "set_debug_result"):
            setattr(view, name,
                    lambda *a, _n=name, **k: touched.append(_n))
        controller.detach()
        controller._view = None
        asyncio.run(controller._load_sources())
        controller._render_audit(_done(SimpleNamespace(
            missing_java_apis=[], unknown_fields=[], missing_object_apis=[],
            unsupported_pseudos=[], js_syntax_errors=[], regex_errors=[])))
        controller._render_debug(_done(["命中甲"]), ".title@text")
        self.assertEqual(touched, [], f"detach 之后还碰了控件：{touched}")


def _done(report):
    """造一个已落值的 future：直接喂渲染回调，不等 EngineHub。"""
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result(report)
    return fut

    def test_refresh_button_and_slots_are_all_bound(self):
        view, controller = _rig([])
        for slot in ("on_save", "on_run_debug", "on_changed", "on_source_picked",
                     "on_field_picked", "on_reload_sources"):
            self.assertIsNotNone(getattr(view, slot), f"{slot} 没绑")
        self.assertIsInstance(view._source_dd, ft.Dropdown)
        self.assertIsInstance(view._field_dd, ft.Dropdown)

    def test_editing_marks_the_status_with_what_is_being_edited(self):
        view, controller = _rig([_record()])
        controller.load_source(_record())
        view._field_dd.on_select(SimpleNamespace(value="ruleSearch.name"))
        controller.mark_dirty(".x@text")
        self.assertIn("ruleSearch.name", view._status.value)

    def test_detach_is_symmetric(self):
        view, controller = _rig([_record()])
        hub_bus = controller._bus
        controller.detach()
        self.assertEqual(hub_bus.subscriptions, [])
        self.assertIsNone(controller._view)


if __name__ == "__main__":
    unittest.main()
