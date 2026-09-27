"""划词查词典：阅读器选中正文 → 「查词典」条 → DictService.lookup → 释义弹窗。

词典这条链以前只有**一个**入口（更多工具 → 词典，手输单词），阅读器里划词根本
没有取词动作：Legado 的"读到哪里词不认识就点哪里"在桌面端是断的（验收 1.8 的
半项）。补的是最后这一段——正文 selectable、选中的词亮成一条、点一下才查。

两条必须钉住的性质：

1. **选中过程一次网络都不发**。一次拖拽会发几十条 selection_change，每条都去
   抓释义页等于对着词典站点做压测；请求只能由「查词典」那一下点击发起。
2. **迟到结果不许串窗**。A 词慢、B 词快时，A 的释义不能贴到 B 的弹窗上；
   弹窗关掉/切页之后在途结果一律丢弃。

全程离线：控件无窗口实例化，词典服务是替身（记忆 legado-live-scan-av-block），
不联网、不碰真实释义页。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

import flet as ft

from app.controllers.reader_controller import ReaderController
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.services.tools.dict_service import DictService
from app.views.reading.reader_view import ReaderView, _TEXT_BLOCK


class _FakeBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, event):
        return []


class _FakeHub:
    def __init__(self, services):
        self.bus = _FakeBus()
        self._services = services

    def get(self, stype):
        return self._services[stype]


class _NoDict:
    """占位：表示这台替身**不注册** DictService（可选服务缺位的真实形态）。"""


class FakeDictService:
    """词典服务替身：按词给返回值/延时/异常，并记录每一次被查询的词。"""

    def __init__(self, result="", *, delay: float = 0.0, error=None,
                 results: dict | None = None, delays: dict | None = None):
        self.result = result
        self.results = dict(results or {})
        self.delay = delay
        self.delays = dict(delays or {})
        self.error = error
        self.calls: list[str] = []

    async def lookup(self, word):
        self.calls.append(word)
        wait = self.delays.get(word, self.delay)
        if wait:
            await asyncio.sleep(wait)
        if self.error is not None:
            raise self.error
        return self.results.get(word, self.result)


class FakePage:
    """假 page：只记账对话框收发（控制器 show/pop 走的就是这三个方法）。"""

    def __init__(self):
        self.shown: list = []
        self.pop_count = 0

    def show_dialog(self, dialog) -> None:
        dialog.open = True
        self.shown.append(dialog)

    def pop_dialog(self) -> None:
        self.pop_count += 1
        if self.shown:
            self.shown.pop().open = False

    def update(self) -> None:
        pass


async def _noop(*_args, **_kwargs):
    return None


class _PreferenceSource:
    def __init__(self, values):
        self._values = dict(values)

    async def __call__(self, key, default=None):
        return self._values.get(key, default)


class _returns:
    """可 await 的固定返回值替身（``service.method(...)`` 直接协程化）。"""

    def __init__(self, value):
        self._value = value

    async def __call__(self, *_args, **_kwargs):
        return self._value


def _rig(dictionary=_NoDict, *, preferences=None):
    """装一台阅读器（视图 + 控制器 + 假 page + 假词典服务），全程离线。"""
    reading = SimpleNamespace(
        get_reader_preference=_PreferenceSource(preferences or {}),
        save_reader_preference=_noop,
        stop_prefetch=lambda: None,
        current_session=None,
        staged_record=lambda: None,
    )
    services = {
        ReadingService: reading,
        BookmarkService: SimpleNamespace(
            list_bookmarks=_returns([]), add_bookmark=_returns(1),
            remove_bookmark=_returns(True)),
        ContentSearchService: SimpleNamespace(
            search=_returns([]), rebuild_index=_returns(0)),
    }
    if dictionary is not _NoDict:
        services[DictService] = dictionary
    view = ReaderView()
    controller = ReaderController(_FakeHub(services))
    page = FakePage()
    controller._page = lambda: page
    controller.attach(view)
    return view, controller, page


def _document(*blocks):
    return SimpleNamespace(blocks=list(blocks), image_style="", title="")


def _text(value):
    return SimpleNamespace(kind="text", value=value, title="", headers={})


def _mount(view, *paragraphs) -> list[ft.Text]:
    view.render_plan(view.content_plan(_document(
        _text("\n".join(paragraphs)))))
    return [c for c in view._content_host.controls
            if getattr(c, "data", None) == _TEXT_BLOCK]


def _select(view, word) -> ft.Text:
    """模拟一次选中事件（真实现给 TextSelectionChangeEvent，鸭子读取即可）。"""
    for control in view._content_host.controls:
        if getattr(control, "data", None) == _TEXT_BLOCK:
            control.on_selection_change(SimpleNamespace(selected_text=word))
            return control
    raise AssertionError("没有正文段可点，挂载失败")


def _bar_button(view) -> ft.Control:
    buttons = [c for c in view.selection_bar.content.controls
               if isinstance(c, ft.TextButton)]
    assert len(buttons) == 1, f"划词条上应当只有一个「查词典」按钮：{buttons}"
    return buttons[0]


def _settled(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _definition_body(view) -> str:
    return str(getattr(view, "_definition_body").value or "")


def _spy(owner: object, names: tuple[str, ...]) -> list[str]:
    """把若干视图方法换成记账桩，返回那份记录（谁被调用过就多出一条名字）。"""
    seen: list[str] = []

    def probe(name):
        def _fn(*_args, **_kwargs):
            seen.append(name)
        return _fn

    for name in names:
        setattr(owner, name, probe(name))
    return seen


class SelectionIsViewOnlyTests(unittest.TestCase):
    """选中只亮条：条本身一条网络都不发，查询是点击那一刻的事。"""

    def setUp(self):
        self.dictionary = FakeDictService("释义：风起，形容事态开端。")
        self.view, self.controller, self.page = _rig(self.dictionary)

    def test_mounted_paragraphs_are_selectable_and_report_selection(self):
        paragraphs = _mount(self.view, "第一段的正文内容。", "第二段也是正文。")
        self.assertTrue(paragraphs, "正文没挂出来")
        for control in paragraphs:
            self.assertTrue(control.selectable)
            self.assertIsNotNone(control.on_selection_change)

    def test_turning_the_preference_off_takes_selection_back_off_the_page(self):
        paragraphs = _mount(self.view, "第一段", "第二段")
        self.view.set_selection("第一段")
        self.assertTrue(self.view.selection_bar.visible)
        self.view.apply_preferences({"text_selection": False})
        self.assertEqual([c.selectable for c in paragraphs], [False, False])
        self.assertFalse(self.view.selection_bar.visible,
                         "开关关掉了还留着一条选中，是个假线索")

    def test_a_selection_shows_the_word_and_asks_nobody_yet(self):
        _mount(self.view, "他终于明白风起的意思。")
        _select(self.view, "  风起  ")
        self.assertTrue(self.view.selection_bar.visible)
        self.assertEqual(self.view._selection_word, "风起")
        self.assertIn("风起", self.view._selection_label.value)
        self.assertEqual(self.dictionary.calls, [],
                         "选中就查词：拖一次拽会打出几十次释义页请求")

    def test_dragging_through_many_prefixes_only_repaints_the_bar(self):
        _mount(self.view, "一段比较长的正文用来拖动")
        for index in range(1, 13):
            _select(self.view, "一段比较长的正文用来拖动"[:index])
        self.assertEqual(self.dictionary.calls, [])
        self.assertEqual(self.view._selection_word, "一段比较长的正文用来拖动")

    def test_clearing_the_selection_hides_the_bar(self):
        _mount(self.view, "第一段")
        _select(self.view, "第一段")
        _select(self.view, "   ")
        self.assertFalse(self.view.selection_bar.visible)
        self.assertEqual(self.view._selection_word, "")

    def test_reselecting_the_same_word_does_not_repaint_the_bar(self):
        # 拖拽时同一次选中的末尾几像素会连着发好几条同词事件：去重留在这里钉，
        # 不然谁都会顺手把它当"没用的早退"删掉。
        _mount(self.view, "风起于青萍之末")
        repaints: list[str] = []
        original = self.view.set_selection
        self.view.set_selection = lambda word: (
            repaints.append(word), original(word))[1]
        _select(self.view, "风起")
        _select(self.view, "风起")
        _select(self.view, "青萍")
        self.assertEqual(repaints, ["风起", "青萍"])
        self.assertEqual(self.dictionary.calls, [])

    def test_a_selection_with_stray_whitespace_asks_for_the_clean_word(self):
        """钉的是**行为**：选区里带的空白不该跟着上屏、也不该跟着进查询。

        strip 一共三道（事件、划词条、控制器），删掉任意一道另两道都会顶上——
        所以这条不针对某一行代码，它盯的就是"用户看到的词是干净的那个词"。
        """
        _mount(self.view, "风起于青萍之末")
        _select(self.view, "\n 风起 \t")
        self.assertEqual(self.view._selection_word, "风起")
        self.assertEqual(self.view._selection_label.value.strip(), "风起")
        _bar_button(self.view).on_click(None)
        self.assertTrue(_settled(lambda: self.dictionary.calls))
        self.assertEqual(self.dictionary.calls, ["风起"])

    def test_the_button_is_what_asks_for_the_word(self):
        _mount(self.view, "风起于青萍之末")
        _select(self.view, "青萍")
        _bar_button(self.view).on_click(None)
        self.assertTrue(_settled(lambda: self.dictionary.calls))
        self.assertEqual(self.dictionary.calls, ["青萍"])

    def test_replacing_the_chapter_drops_the_stale_selection(self):
        _mount(self.view, "上一章的词")
        _select(self.view, "上一章")
        self.view.render_plan(self.view.content_plan(_document(_text("新的一章"))))
        self.assertFalse(self.view.selection_bar.visible)

    def test_appending_the_next_chapter_keeps_the_selection_on_screen(self):
        _mount(self.view, "这一章的词")
        _select(self.view, "这一章")
        self.view.render_plan(
            self.view.content_plan(_document(_text("下一章的内容"))),
            append=True, chapter_name="第二章")
        self.assertTrue(self.view.selection_bar.visible)


class LookupChainTests(unittest.TestCase):
    """控制器那一侧：点击 → 服务 → 回填弹窗，含各种失败形态的明确说法。"""

    def test_clicking_the_bar_opens_a_pending_dialog_then_the_definition(self):
        dictionary = FakeDictService("释义正文在这里。", delay=0.05)
        view, controller, page = _rig(dictionary)
        _mount(view, "风起")
        _select(view, "风起")
        _bar_button(view).on_click(None)
        self.assertTrue(page.shown, "点了查词典连个反应都没有")
        self.assertEqual(_definition_body(view), "查询中…")
        self.assertTrue(_settled(lambda: "释义正文" in _definition_body(view)))
        self.assertEqual(dictionary.calls, ["风起"])

    def test_html_markup_never_reaches_the_reader_as_markup(self):
        view, controller, page = _rig(FakeDictService(
            "<div class='result'><b>形容词</b>：开始兴起</div>"))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "开始兴起" in _definition_body(view)))
        self.assertNotIn("<", _definition_body(view))

    def test_usehtml_payload_is_unwrapped_before_display(self):
        view, controller, page = _rig(
            FakeDictService("<usehtml><p>带标记的释义</p></usehtml>"))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "带标记的释义" in _definition_body(view)))
        self.assertNotIn("usehtml", _definition_body(view))

    def test_markdown_payload_keeps_its_text_and_drops_the_wrapper(self):
        """<md> 释义不渲染也读得下去：剥壳留下原文，比把整段丢掉强。"""
        view, controller, page = _rig(FakeDictService("<md>**形容词**：兴起</md>"))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "兴起" in _definition_body(view)))
        self.assertNotIn("<md>", _definition_body(view))

    def test_a_whitespace_only_answer_counts_as_no_definition(self):
        view, controller, page = _rig(FakeDictService("   \n  "))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "没查到" in _definition_body(view)))

    def test_an_empty_result_says_why_instead_of_showing_a_blank_box(self):
        view, controller, page = _rig(FakeDictService(""))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "没查到" in _definition_body(view)))
        self.assertIn("词典", _definition_body(view),
                      "空结果必须指路：词典规则在「更多工具 → 词典」里维护")

    def test_a_service_failure_lands_in_the_dialog(self):
        view, controller, page = _rig(
            FakeDictService(error=RuntimeError("释义页超时")))
        controller.lookup_word("风起")
        self.assertTrue(_settled(
            lambda: "查询失败" in _definition_body(view)))
        self.assertIn("释义页超时", _definition_body(view))

    def test_missing_dict_service_says_so_instead_of_staying_silent(self):
        view, controller, page = _rig()
        controller.lookup_word("风起")
        self.assertTrue(page.shown)
        self.assertIn("词典服务未就绪", _definition_body(view))

    def test_an_empty_word_never_reaches_the_service(self):
        dictionary = FakeDictService("释义")
        view, controller, page = _rig(dictionary)
        controller.lookup_word("   ")
        controller.lookup_word("")
        self.assertEqual(dictionary.calls, [])
        self.assertEqual(page.shown, [])

    def test_double_clicking_the_same_word_asks_once(self):
        dictionary = FakeDictService("释义", delay=0.05)
        view, controller, page = _rig(dictionary)
        _mount(view, "风起")
        _select(view, "风起")
        button = _bar_button(view)
        button.on_click(None)
        button.on_click(None)
        self.assertTrue(_settled(lambda: "释义" in _definition_body(view)))
        self.assertEqual(dictionary.calls, ["风起"])

    def test_closing_the_dialog_pops_it(self):
        view, controller, page = _rig(FakeDictService("释义"))
        controller.lookup_word("风起")
        self.assertTrue(_settled(lambda: "释义" in _definition_body(view)))
        controller.close_definition()
        self.assertEqual(page.pop_count, 1)
        self.assertEqual(page.shown, [])

    def test_a_late_result_for_an_older_word_does_not_hijack_the_new_dialog(self):
        # 甲慢乙快：乙先回填，甲后到——甲的结果必须丢掉，不然弹窗说的是没选的词。
        dictionary = FakeDictService(results={"甲": "甲的释义", "乙": "乙的释义"},
                                     delays={"甲": 0.4, "乙": 0.0})
        view, controller, page = _rig(dictionary)
        controller.lookup_word("甲")
        controller.lookup_word("乙")
        self.assertTrue(_settled(lambda: "乙的释义" in _definition_body(view)))
        time.sleep(0.5)
        self.assertEqual(dictionary.calls, ["甲", "乙"])
        self.assertIn("乙的释义", _definition_body(view))
        self.assertNotIn("甲的释义", _definition_body(view))

    def test_closing_the_dialog_abandons_the_in_flight_query(self):
        dictionary = FakeDictService("释义", delay=0.3)
        view, controller, page = _rig(dictionary)
        controller.lookup_word("风起")
        touched = _spy(view, ("update_definition",))
        controller.close_definition()
        time.sleep(0.5)
        self.assertEqual(touched, [], "弹窗已经关了，迟到的释义还在往里写")

    def test_a_result_that_arrives_after_the_page_left_touches_nothing(self):
        dictionary = FakeDictService("释义", delay=0.3)
        view, controller, page = _rig(dictionary)
        touched = _spy(view, ("update_definition", "set_selection"))
        with self.assertNoLogs("app.controllers.base", level="ERROR"):
            controller.lookup_word("风起")
            controller.detach()
            time.sleep(0.6)
        self.assertEqual(touched, [], "detach 之后还在改控件")
        self.assertEqual(dictionary.calls, ["风起"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
