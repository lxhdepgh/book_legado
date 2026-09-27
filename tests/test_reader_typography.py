"""阅读器排版与阅读设置（旧 ``ReadingView`` 的显示层逻辑回归）。

豆包那轮分层重构把正文渲染简化成"一个 block 一个 Text"：不切段、不认小标题、
没有行距/段距/版心/背景，阅读设置整块没了入口（快捷键 ``settings`` 甚至成了
死绑定）。这些都不是崩溃，而是"读起来不对劲"——只能靠断言钉住。

红线：视图不 import 服务（§7.5），所以本文件直接用真 :class:`ReaderView` 验排版，
控制器那侧只验"偏好搬运 + 弹层开关"的编排，不发真实网络请求。
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import flet as ft

from app.controllers.reader_controller import ReaderController
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService, _READER_PREFIX
from app.views.reading.reader_view import (
    DEFAULT_PREFERENCES, ReaderView, _HEADING_BLOCK, _TEXT_BLOCK)


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


def _reading_stub(preferences=None):
    values = dict(preferences or {})
    return SimpleNamespace(
        open_book=AsyncMock(return_value=SimpleNamespace(
            book_url="/b", chapters=[], position=None)),
        load_content=AsyncMock(return_value=SimpleNamespace(blocks=[], image_style="")),
        load_chapters=AsyncMock(return_value=[]),
        save_progress=AsyncMock(),
        get_reader_preference=AsyncMock(
            side_effect=lambda key, default=None: values.get(key, default)),
        save_reader_preference=AsyncMock(),
        prefetch_chapters=AsyncMock(),
        stop_prefetch=lambda: None,
        current_session=SimpleNamespace(chapters=[], viewports={}, position=None),
        staged_record=lambda: None,
    )


def _rig(preferences=None):
    reading = _reading_stub(preferences)
    view = ReaderView()
    controller = ReaderController(_FakeHub({
        ReadingService: reading,
        BookmarkService: SimpleNamespace(
            list_bookmarks=AsyncMock(return_value=[]),
            add_bookmark=AsyncMock(return_value=1),
            remove_bookmark=AsyncMock(return_value=True)),
        ContentSearchService: SimpleNamespace(
            search=AsyncMock(return_value=[]),
            rebuild_index=AsyncMock(return_value=0)),
    }))
    controller.attach(view)
    return view, controller, reading


def _settled(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _document(*blocks):
    return SimpleNamespace(blocks=list(blocks), image_style="", title="")


def _text(value):
    return SimpleNamespace(kind="text", value=value, title="", headers={})


def _descendants(control):
    """深度遍历一棵控件树（0.86 的子控件挂在 controls/content/leading/… 上）。

    "页面上有没有第二个播放面"这种断言只能整棵树翻，不能只看直接子控件——
    Card→Container→Row 里那份 flet_video 控件隔两层就照不见了。
    """
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from _descendants(child)
    for name in ("content", "leading", "title", "subtitle", "trailing"):
        child = getattr(control, name, None)
        if isinstance(child, ft.Control):
            yield from _descendants(child)


class ParagraphSplitTests(unittest.TestCase):
    """``novel_paragraphs``：切段只影响显示，不改数据。"""

    def test_blank_lines_are_dropped_and_runs_of_spaces_collapse(self):
        paragraphs = ReaderView.novel_paragraphs("第一段\n\n   \n第二段")
        self.assertEqual(paragraphs, [("第一段", False), ("第二段", False)])

    def test_a_source_that_sends_one_giant_block_still_becomes_paragraphs(self):
        value = "\n".join(f"第{i}段内容" for i in range(1, 301))
        self.assertEqual(len(ReaderView.novel_paragraphs(value)), 300)

    def test_no_break_paragraph_stays_one_paragraph(self):
        """引擎已经把段落切成 "\n　　…" 形态了：切分只会按行分开，不会拆句子。"""
        self.assertEqual(
            ReaderView.novel_paragraphs("　　甲段\n　　乙段", indent=True),
            [("　　甲段", False), ("　　乙段", False)])

    def test_chinese_heading_lines_are_recognised(self):
        for line in ("第一百二十章 风起", "序章", "前言：关于本书", "楔子",
                     "第二百零三章：转折", "番外 后记"):
            self.assertTrue(ReaderView.novel_paragraphs(line)[0][1],
                            f"未识别为小标题：{line}")

    def test_a_long_or_mid_sentence_line_is_not_a_heading(self):
        for line in ("第一百二十章之前他从未想过这些", "他说：第一百二十章 风起",
                     "第二百零三章转折", "卷首语"):
            self.assertFalse(ReaderView.novel_paragraphs(line)[0][1],
                             f"误判为小标题：{line}")

    def test_indent_adds_one_pair_and_never_doubles_it(self):
        once = ReaderView.novel_paragraphs("正文一行", indent=True)[0][0]
        twice = ReaderView.novel_paragraphs(once, indent=True)[0][0]
        self.assertEqual(once, "　　正文一行")
        self.assertEqual(twice, once, "重复切段把缩进叠成了两层的四层")

    def test_bullet_and_already_indented_lines_keep_their_own_marker(self):
        for line in ("• 清单一项", "· 要点", "- 破折号项", "　　已有缩进"):
            self.assertEqual(ReaderView.novel_paragraphs(line, indent=True)[0][0],
                             line)

    def test_nbsp_and_cr_crlf_are_normalised(self):
        self.assertEqual(ReaderView.novel_paragraphs("甲\xa0\xa0乙\r\n丙"),
                         [("甲 乙", False), ("丙", False)])


class ContentPlanTests(unittest.TestCase):
    """``content_plan`` → ``render_plan``：纯计算与建控件分家（前者上线程）。"""

    def setUp(self):
        self.view = ReaderView()

    def test_text_and_media_blocks_keep_their_kind(self):
        plan = self.view.content_plan(_document(
            _text("甲\n乙"),
            SimpleNamespace(kind="image", value="https://x/1.png", title="",
                            headers={}),
            SimpleNamespace(kind="audio", value="https://x/1.mp3", title="片头",
                            headers={}),
            SimpleNamespace(kind="video", value="https://x/1.mp4", title="花絮",
                            headers={"Referer": "https://x"})))
        self.assertEqual([kind for kind, _, _ in plan],
                         ["text", "image", "audio", "video"])
        self.assertEqual(plan[0][1], [("　　甲", False), ("　　乙", False)])
        self.assertEqual(plan[3][1].headers, {"Referer": "https://x"})

    def test_empty_and_unknown_blocks_are_dropped(self):
        plan = self.view.content_plan(_document(
            _text("   "), SimpleNamespace(kind="script", value="alert(1)",
                                          title="", headers={})))
        self.assertEqual(plan, [])

    def test_plan_builds_paragraph_controls_with_role_markers(self):
        self.view.render_plan(self.view.content_plan(_document(
            _text("第三百章 起\n正文一段"))))
        controls = self.view._content_host.controls
        self.assertEqual([getattr(c, "data", None) for c in controls],
                         [_HEADING_BLOCK, _TEXT_BLOCK])
        heading, body = controls
        self.assertEqual(heading.weight, ft.FontWeight.W_600)
        self.assertIsNone(body.weight)
        self.assertEqual(heading.size, self.view._font_size + 2)
        self.assertEqual(body.size, self.view._font_size)
        self.assertEqual(body.style.height, DEFAULT_PREFERENCES["line_height"])

    def test_media_blocks_render_without_touching_the_network(self):
        """图片/音频/视频只搭控件：src 是地址，测不到也不会去下载。"""
        self.view.render_plan(self.view.content_plan(SimpleNamespace(
            blocks=[SimpleNamespace(kind="image", value="https://x/1.png",
                                    title="", headers={})],
            image_style="FULL")))
        image = self.view._content_host.controls[0]
        self.assertIsInstance(image, ft.Image)
        self.assertEqual(image.fit, ft.BoxFit.FIT_WIDTH)

    def test_audio_and_video_controls_are_constructed(self):
        """音频仍是内嵌播放器；视频改成「在本页播放」条目，正文里不多一个播放器。

        一个播放面由控制器经 ``MediaPlayService`` 驱动（§1.4）：正文里内嵌一份
        ``ftv.Video`` 等于绕过服务——没有播放列表、没有"接着上次看"、切页也停不
        下来。这条断言的是"页面上没有第二个播放面"，光看点击有没有反应照不出来。
        """
        played: list[str] = []
        self.view.on_play_video = played.append
        self.view.render_plan(self.view.content_plan(_document(
            SimpleNamespace(kind="audio", value="https://x/1.mp3", title="片头",
                            headers={}),
            SimpleNamespace(kind="video", value="https://x/1.mp4", title="花絮",
                            headers={"Referer": "https://x"}))))
        card, entry = self.view._content_host.controls
        self.assertIsInstance(card, ft.Card)
        self.assertIsInstance(entry, ft.Card)
        nodes = list(_descendants(entry))
        self.assertTrue(any(isinstance(n, ft.Text) and n.value == "花絮" for n in nodes))
        self.assertTrue(any(isinstance(n, ft.Text) and "https://x/1.mp4" in str(n.value)
                            for n in nodes), "条目没把地址露出来，用户不知道要放什么")
        button = next(n for n in nodes if isinstance(n, ft.TextButton)
                      and str(getattr(n.content, "value", n.content)) == "在本页播放")
        button.on_click(None)
        self.assertEqual(played, ["https://x/1.mp4"])
        # 整页只有一份播放面：视图不许自己 import flet_video 建控件。
        self.assertEqual([n for n in _descendants(self.view)
                          if type(n).__module__.startswith("flet_video")], [])

    def test_an_empty_chapter_says_so_instead_of_showing_a_blank_page(self):
        self.view.render_plan([])
        self.assertEqual(self.view._content_host.controls[0].value, "本章暂无正文")


class PreferenceTests(unittest.TestCase):
    """``apply_preferences``：夹区间 + 已挂正文就地重排。"""

    def setUp(self):
        self.view = ReaderView()
        self.view.render_content(_document(_text("正文一行\n另一段")))

    def _bodies(self):
        return [c for c in self.view._content_host.controls
                if getattr(c, "data", None) == _TEXT_BLOCK]

    def test_defaults_match_the_old_reader(self):
        self.assertEqual(self.view.preferences["line_height"], 1.7)
        self.assertTrue(self.view.preferences["indent"])
        self.assertEqual(self.view._reader_document.width, 760.0)
        self.assertEqual(self.view._content_host.spacing, 12.0)

    def test_out_of_range_values_are_clamped_not_applied(self):
        merged = self.view.apply_preferences(
            {"font_size": 900, "line_height": 0, "max_width": 12.5})
        self.assertEqual(merged["font_size"], 28)
        self.assertEqual(merged["line_height"], 1.3)
        self.assertEqual(merged["max_width"], 760.0, "非三档宽度应退回默认版心")
        self.assertEqual(self.view._font_size_label.value, "28")

    def test_unreadable_values_fall_back_to_defaults(self):
        merged = self.view.apply_preferences(
            {"font_size": "大", "line_height": None, "background": "霓虹",
             "text_align": "中间"})
        self.assertEqual(merged["font_size"], 16)
        self.assertEqual(merged["line_height"], 1.7)
        self.assertEqual(merged["background"], "follow")
        self.assertEqual(merged["text_align"], "start")

    def test_relayout_rewrites_what_is_already_on_screen(self):
        self.view.apply_preferences({"line_height": 2.0, "text_align": "justify"})
        body = self._bodies()[0]
        self.assertEqual(body.style.height, 2.0)
        self.assertEqual(body.text_align, ft.TextAlign.JUSTIFY)

    def test_night_background_recolorises_the_document(self):
        self.view.apply_preferences({"background": "night"})
        self.assertEqual(self.view._reader_document.bgcolor, "#151515")
        self.assertEqual(self._bodies()[0].color, "#D8D8D8")
        self.view.apply_preferences({"background": "follow"})
        self.assertIsNone(self.view._reader_document.bgcolor)
        self.assertIsNone(self._bodies()[0].color)

    def test_toggling_indent_off_and_on_does_not_stack_markers(self):
        first = self._bodies()[0].value
        self.view.apply_preferences({"indent": False})
        self.assertEqual(self._bodies()[0].value, first.lstrip("　"))
        self.view.apply_preferences({"indent": True})
        self.assertEqual(self._bodies()[0].value, first, "缩进来回切会越长越厚")

    def test_status_bar_visibility_respects_immersive_too(self):
        self.view.set_immersive(True)
        self.assertFalse(self.view._status_bar.visible)
        self.view.apply_preferences({"show_status": True})
        self.assertFalse(self.view._status_bar.visible,
                         "沉浸模式下改设置不该把状态栏抖出来")
        self.view.set_immersive(False)
        self.assertTrue(self.view._status_bar.visible)

    def test_appended_chapter_survives_a_preference_change(self):
        """排版重排只碰正文段：追加进来的章名和分隔线不能被抹掉。"""
        self.view.render_content(_document(_text("第一章正文")))
        self.view.render_content(_document(_text("第二章正文")),
                                 append=True, chapter_name="第二章")
        before = len(self.view._content_host.controls)
        self.view.apply_preferences({"font_size": 20, "line_height": 1.4})
        self.assertEqual(len(self.view._content_host.controls), before)
        names = [c.value for c in self.view._content_host.controls
                 if getattr(c, "data", None) is None
                 and getattr(c, "value", None)]
        self.assertIn("第二章", names, "改个行距就把接上的章节标题抹了")

    def test_font_size_entry_stays_in_range(self):
        self.view.set_font_size(1)
        self.assertEqual(self.view._font_size, 13)
        self.view.set_font_size(99)
        self.assertEqual(self.view._font_size, 28)


class SettingsDialogTests(unittest.TestCase):
    """设置弹层：视图只搭控件，改动经回调交给控制器。"""

    def setUp(self):
        self.view = ReaderView()
        self.changes: list[tuple[str, object]] = []
        self.closed = 0
        self.dialog = self.view.build_settings_dialog(
            lambda key, value: self.changes.append((key, value)),
            lambda: self.__setattr__("closed", self.closed + 1))

    def _walk(self, control):
        yield control
        for child in getattr(control, "controls", None) or []:
            yield from self._walk(child)
        content = getattr(control, "content", None)
        if content is not None and not isinstance(content, (list, tuple)):
            yield from self._walk(content)

    def test_dialog_carries_every_preference_control(self):
        controls = list(self._walk(self.dialog))
        sliders = [c for c in controls if isinstance(c, ft.Slider)]
        self.assertEqual([(c.min, c.max) for c in sliders],
                         [(13.0, 28.0), (1.3, 2.2), (4.0, 24.0)])
        dropdowns = [c for c in controls if isinstance(c, ft.Dropdown)]
        self.assertEqual({c.label for c in dropdowns},
                         {"正文宽度", "文本对齐", "阅读背景"})
        switches = [c for c in controls if isinstance(c, ft.Switch)]
        self.assertEqual({c.label for c in switches},
                         {"段首缩进", "显示底部阅读状态", "划词可选（选中正文查词典）"})

    def test_every_preference_key_has_a_control_that_can_change_it(self):
        """新增偏好键必须带开关：漏一条就是"这个设置没做"，测试全绿也照不出来。"""
        changes: list[tuple[str, object]] = []
        dialog = self.view.build_settings_dialog(
            lambda key, value: changes.append((key, value)), lambda: None)
        for control in self._walk(dialog):
            if isinstance(control, ft.Slider) and control.on_change:
                control.on_change(SimpleNamespace(
                    control=SimpleNamespace(value=control.max)))
            elif isinstance(control, ft.Dropdown) and control.on_select:
                control.on_select(SimpleNamespace(
                    control=SimpleNamespace(value=control.value)))
            elif isinstance(control, ft.Switch) and control.on_change:
                control.on_change(SimpleNamespace(
                    control=SimpleNamespace(value=not control.value)))
        reported = {key for key, _ in changes}
        # shortcuts / font_family 不在这里改（前者有专页，后者在「配置 → 字体」）
        self.assertEqual(reported, set(self.view.PREFERENCE_KEYS)
                         - {"font_family"})

    def test_controls_show_what_is_currently_applied(self):
        self.view.apply_preferences({"line_height": 2.0, "background": "night",
                                     "text_align": "justify"})
        dialog = self.view.build_settings_dialog(lambda k, v: None, lambda: None)
        controls = list(self._walk(dialog))
        self.assertEqual([c.value for c in controls if isinstance(c, ft.Slider)],
                         [16.0, 2.0, 12.0])
        self.assertEqual({c.value for c in controls if isinstance(c, ft.Dropdown)},
                         {"760", "justify", "night"})

    def test_moving_a_slider_reports_key_and_value_to_the_callback(self):
        for control in self._walk(self.dialog):
            if isinstance(control, ft.Slider) and control.on_change:
                control.value = 20.0
                control.on_change(SimpleNamespace(control=control))
        self.assertIn(("font_size", 20.0), self.changes)

    def test_switches_and_dropdowns_use_the_0_86_callback_names(self):
        for control in self._walk(self.dialog):
            if isinstance(control, ft.Switch) and control.on_change:
                control.on_change(SimpleNamespace(control=SimpleNamespace(
                    value=not control.value)))
            elif isinstance(control, ft.Dropdown) and control.on_select:
                control.on_select(SimpleNamespace(control=SimpleNamespace(
                    value=control.value)))
        keys = {key for key, _ in self.changes}
        self.assertTrue({"indent", "show_status", "max_width", "text_align",
                         "background"} <= keys, f"少了能改的项：{keys}")

    def test_close_button_goes_through_the_callback(self):
        for control in self._walk(self.dialog):
            if isinstance(control, ft.IconButton) and control.tooltip == "关闭":
                control.on_click(None)
        self.assertEqual(self.closed, 1)


class PreferenceOrchestrationTests(unittest.TestCase):
    """控制器那侧：偏好读回来、改出去，弹层由 page 开关。"""

    def test_attached_view_adopts_stored_preferences(self):
        view, controller, _ = _rig({"line_height": 2.1, "background": "eye",
                                    "font_size": 21})
        try:
            self.assertTrue(_settled(lambda: view._font_size == 21),
                            "存过的字号没在读偏好时生效")
            self.assertEqual(view.preferences["line_height"], 2.1)
            self.assertEqual(view._reader_document.bgcolor, "#DCE8D5")
        finally:
            controller.detach()

    def test_missing_stored_values_leave_the_defaults_alone(self):
        view, controller, reading = _rig({})
        try:
            _settled(lambda: False, timeout=0.3)
            self.assertEqual(view.preferences, DEFAULT_PREFERENCES)
            self.assertEqual(view._font_size, 16)
        finally:
            controller.detach()

    def test_changing_font_size_persists_the_clamped_value(self):
        view, controller, reading = _rig()
        try:
            view.apply_preferences({"font_size": 27})
            controller.change_font_size(5)
            _settled(lambda: reading.save_reader_preference.await_count > 0)
            self.assertEqual(view._font_size, 28)
            reading.save_reader_preference.assert_awaited_with("font_size", 28)
        finally:
            controller.detach()

    def test_preference_change_is_applied_then_saved(self):
        view, controller, reading = _rig()
        try:
            controller.set_preference("text_align", "justify")
            self.assertEqual(view._preferences["text_align"], "justify")
            _settled(lambda: reading.save_reader_preference.await_count > 0)
            reading.save_reader_preference.assert_awaited_with(
                "text_align", "justify")
        finally:
            controller.detach()

    def test_settings_dialog_is_shown_and_popped_through_the_page(self):
        view, controller, _ = _rig()
        page = SimpleNamespace(show_dialog=MagicMock(), pop_dialog=MagicMock())
        controller._page = lambda: page
        try:
            controller.open_settings()
            self.assertEqual(page.show_dialog.call_count, 1)
            dialog = page.show_dialog.call_args[0][0]
            self.assertIsInstance(dialog, ft.AlertDialog)
            controller.close_settings()
            self.assertEqual(page.pop_dialog.call_count, 1)
            self.assertIsNone(controller._settings_dialog)
            controller.close_settings()
            self.assertEqual(page.pop_dialog.call_count, 1,
                             "没有弹层还 pop 一次，会把别人的对话框顶掉")
        finally:
            controller.detach()

    def test_toolbar_button_and_comma_shortcut_both_open_settings(self):
        """两个入口都得通：工具栏是新用户唯一的线索，逗号是老用户的肌肉记忆。"""
        view, controller, _ = _rig()
        page = SimpleNamespace(show_dialog=MagicMock(), pop_dialog=MagicMock())
        controller._page = lambda: page
        try:
            button = next(btn for btn in view._toolbar.content.controls
                          if getattr(btn, "tooltip", "") == "阅读设置")
            self.assertEqual(view.on_open_settings, controller.open_settings)
            button.on_click(None)
            self.assertEqual(page.show_dialog.call_count, 1)
            controller._on_key(SimpleNamespace(key="Comma"))
            self.assertEqual(page.show_dialog.call_count, 2,
                             "逗号快捷键没接到阅读设置（DEFAULT_SHORTCUTS 里它是死的）")
            controller._on_key(SimpleNamespace(key="Escape"))
            self.assertEqual(page.pop_dialog.call_count, 1)
        finally:
            controller.detach()

    def test_without_a_page_the_dialog_is_not_built_at_all(self):
        """无头/测试环境拿不到 page：不建弹层，免得留一个永远关不掉的浮层。"""
        view, controller, _ = _rig()
        try:
            self.assertIsNone(controller._page())
            controller.open_settings()
            self.assertIsNone(controller._settings_dialog)
        finally:
            controller.detach()


class _StubSettings:
    """对任何键都答"存过"：这样白名单里列了什么，导出结果里就出现什么。"""

    def get(self, key, default=None):
        return f"stored:{key}"


class PreferenceBackupWhitelistTests(unittest.TestCase):
    """每条阅读偏好都得进 WebDAV 备份的 config.json。

    备份那侧的键清单是**手写**的（SettingsService 没有全量迭代接口），漏一条就是
    "换机恢复之后设置悄悄回到默认值"。这条真错过一次：白名单当时列的是
    ``reader.font.family`` 这种没人写过的键，读者的设置一条都备不出去。
    """

    def _exported(self):
        from app.services.tools.web_dav_service import WebDavService
        return WebDavService(settings=_StubSettings())._export_settings()

    def test_every_reader_preference_survives_a_backup(self):
        exported = self._exported()
        missing = {_READER_PREFIX + name for name in DEFAULT_PREFERENCES} \
            - set(exported)
        self.assertEqual(missing, set(), f"这些偏好键没进备份白名单：{missing}")

    def test_whitelist_does_not_carry_keys_nobody_writes(self):
        """反向钉：白名单多列一条没人写的 reader.* 键，上一条看不出来。"""
        # shortcuts / tts_rate 由控制器写（不在视图偏好表里），其余必须一一对应。
        written_by_controller = {"shortcuts", "tts_rate"}
        exported = self._exported()
        reader_keys = {k for k in exported if k.startswith(_READER_PREFIX)}
        self.assertEqual(reader_keys,
                         {_READER_PREFIX + name
                          for name in set(DEFAULT_PREFERENCES) | written_by_controller})


if __name__ == "__main__":
    unittest.main()
