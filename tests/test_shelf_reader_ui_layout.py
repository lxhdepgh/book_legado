"""第二十八轮：书架页与阅读页的版面护栏（离线取证抓到的六处）。

这一轮的输入是**截图**，不是代码走查（`.probe/probe_shelf_reader.py`，本地假源 +
带真中文字体的 WEB_BROWSER 探针）。量出来的问题全是"测试全绿、界面却是坏的"那一类：

1. 书架工具栏六个控件在屏上排成**六行**（搜索框一行、两个下拉各一行、三个图标按钮
   竖排在右），工具区被撑到 ~330px 高，列表首行沉到 y≈371。根因：Flet 0.86 的
   ``ResponsiveRow`` 子项 ``col`` **默认 12**（= 独占一行），不写 col 就等于放弃栅格。
2. 书架状态行挂在 Column 末尾，永远贴着窗口底边（实测 y≈875），离列表末端几百像素。
3. 书架行副标题只有作者：进度从来不在 ``chapter_name`` 这个键上（在 ``reading``
   子对象 / 旧数据的 ``durChapterTitle``），控制器没投影，视图读的是个不存在的键。
4. 阅读器**没有沉浸模式的入口**：正文段是 selectable 的，点版心那一下被文本控件吃掉
   （实测既不切沉浸，还把整段亮到划词条上）。工具栏那颗按钮补上入口，右下角浮标补上
   出口——否则进了沉浸连退不出来（工具栏自己就是被藏掉的东西）。
5. 阅读器底部状态栏与顶部进度标签写的是同一句话（章名 + 序号 + 百分比），而整屏没有
   一个地方显示书名；朗读条的音色下拉 120px 把"内置语音"截成"内置语"。
6. 网格卡只印书名：列表行有作者，切到网格就只剩一排书名，卡片下半截是空白。

全程离线：控件无窗口实例化，服务是替身，不联网。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import flet as ft

from app.controllers.bookshelf_controller import BookshelfController
from app.core.registry import DEST_BOOKSHELF, DEST_FIND_BOOKS
from app.views import navigation as nav
from app.views.navigation import NavigationHost, build_app_shell
from app.views.reading.bookshelf_view import BookshelfView
from app.views.reading.reader_view import ReaderView


# --------------------------------------------------------------------------- #
# 书架：工具栏栅格 / 状态行落点
# --------------------------------------------------------------------------- #
class ShelfToolbarGridTests(unittest.TestCase):
    """工具栏必须一行放完，且六个控件都在这一行里。"""

    def setUp(self):
        self.view = BookshelfView()
        self.row = self.view._toolbar.content

    def test_no_child_left_on_the_default_full_width_column(self):
        """``col`` 默认 12 = 独占一行：漏写一个控件，工具栏就又开始竖排。"""
        offenders = [type(c).__name__ for c in self.row.controls if c.col == 12]
        self.assertEqual([], offenders, "工具栏子项没写 col，会被栅格顶成整行")

    def test_children_fill_exactly_one_row(self):
        self.assertEqual(12, sum(c.col for c in self.row.controls),
                         "子项列宽之和不等于 columns=12，栅格会多出一行或少一列")

    def test_every_toolbar_action_is_reachable_from_that_row(self):
        flat = []

        def walk(controls):
            for c in controls:
                flat.append(c)
                walk(getattr(c, "controls", None) or [])
                walk(getattr(getattr(c, "content", None), "controls", None) or [])

        walk(self.row.controls)
        for btn in (self.view._import_btn, self.view._layout_btn,
                    self.view._refresh_btn):
            self.assertIn(btn, flat, "按钮被挪出了工具栏那一行")
        for field in (self.view._query, self.view._group, self.view._sort):
            self.assertIn(field, flat, "搜索框/下拉被挪出了工具栏那一行")

    def test_dropdowns_are_widened_by_the_grid_not_by_a_font_measurement(self):
        """``Dropdown`` 默认**按内容量一次宽**，而这一量在字体晚到的面上拿的是回退
        字体：实测量成 120px 把"最近阅读"截成"最近阅…"，之后等 6s 不变、任何一次
        重排（改视口 / 切网格）才自己回到 148px。写死像素则在窄窗（720px 时 col=3
        只有 ~113px）溢出，所以宽度必须由栅格位给。
        """
        for field in (self.view._group, self.view._sort):
            self.assertEqual(float("inf"), field.width,
                             "下拉退回按内容量宽：首帧截字，之后要不要恢复全看有没有重排")


class ShelfStatusPlacementTests(unittest.TestCase):
    """状态行跟着工具栏走，空文本时整条收起（不占高度）。"""

    def setUp(self):
        self.view = BookshelfView()

    def test_status_sits_under_the_toolbar_not_at_the_window_bottom(self):
        controls = self.view.controls
        self.assertEqual(controls.index(self.view._toolbar) + 1,
                         controls.index(self.view._status_host),
                         "状态行没紧跟工具栏")
        self.assertIsNot(controls[-1], self.view._status_host,
                         "状态行还挂在列表下面：它会贴窗口底边，离内容几百像素")

    def test_empty_status_hides_the_host(self):
        self.view.set_status_text("书架共 3 本")
        self.assertTrue(self.view._status_host.visible)
        self.view.set_status_text("")
        self.assertFalse(self.view._status_host.visible,
                         "空状态仍然占一条高度")


# --------------------------------------------------------------------------- #
# 书架：行副标题的"读到哪儿"
# --------------------------------------------------------------------------- #
class ShelfRowProgressTests(unittest.TestCase):
    """进度在库里从来不是一个裸键，控制器必须归一化后再交给视图。"""

    def _subtitle(self, raw: dict) -> str:
        record = BookshelfController._to_render_record(raw)
        view = BookshelfView()
        view.render_books([record])
        return str(view._list.controls[0].subtitle.value or "")

    def test_new_shape_reading_subobject_shows_progress(self):
        self.assertEqual(
            "长声 · 读到 第3章 归途",
            self._subtitle({"name": "山海", "author": "长声",
                            "reading": {"chapter_name": "第3章 归途"}}))

    def test_legacy_dur_chapter_title_shows_progress(self):
        self.assertEqual(
            "佚名 · 读到 第7章 起风",
            self._subtitle({"name": "旧档", "author": "佚名",
                            "durChapterTitle": "第7章 起风"}))

    def test_unread_book_shows_no_progress_at_all(self):
        """反面那半：没读过的书不许编出一条"读到 "。"""
        subtitle = self._subtitle({"name": "新书", "author": "某作者"})
        self.assertEqual("某作者", subtitle)
        self.assertNotIn("读到", subtitle)

    def test_projection_does_not_write_back_into_the_source_record(self):
        raw = {"name": "山海", "author": "长声",
               "reading": {"chapter_name": "第3章 归途"}}
        BookshelfController._to_render_record(raw)
        self.assertNotIn("chapter_name", raw,
                         "投影写回了 SourceService 内存里的那份 dict，会被落进 json")


class ShelfCellMetaTests(unittest.TestCase):
    """网格卡以前只印书名：一切到网格，整排作者就没了（实测截图）。"""

    def _cell_texts(self, raw: dict) -> list[str]:
        record = BookshelfController._to_render_record(raw)
        view = BookshelfView()
        view.render_books([record])
        column = view._grid.controls[0].content
        return [str(t.value or "") for t in column.controls if isinstance(t, ft.Text)]

    def test_grid_cell_carries_author_next_to_the_name(self):
        self.assertEqual(["山海", "长声"],
                         self._cell_texts({"name": "山海", "author": "长声"}))

    def test_grid_cell_keeps_the_line_when_there_is_no_author(self):
        """没作者留空行、不省控件：卡片高度一致，架子才不会错落。"""
        self.assertEqual(["新书", ""], self._cell_texts({"name": "新书"}))


# --------------------------------------------------------------------------- #
# 阅读器：沉浸模式的入口与出口
# --------------------------------------------------------------------------- #
class ImmersiveEntryExitTests(unittest.TestCase):
    """入口在工具栏（点正文收不到），出口是右下角浮标（工具栏自己会被藏掉）。"""

    def setUp(self):
        self.view = ReaderView()
        self.toggled: list = []
        self.view.on_toggle_immersive = lambda: self.toggled.append(True)

    def _toolbar_controls(self) -> list:
        return list(self.view._toolbar.content.controls)

    def test_toolbar_has_an_immersive_button(self):
        self.assertIn(self.view._immersive_btn, self._toolbar_controls(),
                      "沉浸模式没有入口：正文 selectable，点版心那一下到不了视图")

    def test_clicking_it_fires_the_toggle(self):
        self.view._immersive_btn.on_click(None)
        self.assertEqual([True], self.toggled)

    def test_exit_float_only_shows_in_immersive(self):
        self.assertFalse(self.view._immersive_exit.visible)
        self.view.set_immersive(True)
        self.assertTrue(self.view._immersive_exit.visible,
                        "进了沉浸模式却没有出口：工具栏自己就是被藏掉的那条")
        self.view.set_immersive(False)
        self.assertFalse(self.view._immersive_exit.visible,
                         "退出沉浸后浮标还挂在正文右下角")

    def test_exit_float_click_toggles_back(self):
        self.view.set_immersive(True)
        self.view._immersive_exit.content.on_click(None)
        self.assertEqual([True], self.toggled)
        self.view.set_immersive(False)
        self.assertEqual(ft.Icons.FULLSCREEN, self.view._immersive_btn.icon)

    def test_directory_page_supersedes_the_exit_float(self):
        """目录页自己带「返回阅读」：浮标压上去只会挡住章名。"""
        self.view.set_immersive(True)
        self.view.show_directory(True)
        self.assertFalse(self.view._immersive_exit.visible)
        self.view.show_directory(False)
        self.assertTrue(self.view._immersive_exit.visible,
                        "关掉目录后浮标该回来：沉浸模式不能没有出口")


# --------------------------------------------------------------------------- #
# 阅读器：单击不算选词
# --------------------------------------------------------------------------- #
class TapIsNotAWordSelectionTests(unittest.TestCase):
    """在 selectable 文本上点一下，事件带的是 cause=TAP + **整段**文本。"""

    def setUp(self):
        self.view = ReaderView()
        self.paragraph = "这是第一段正文，故意写得长一些，好看清行距与版心宽度。"

    def test_tap_cause_does_not_light_the_bar(self):
        self.view._on_text_selection(SimpleNamespace(
            selected_text=self.paragraph, cause=ft.TextSelectionChangeCause.TAP))
        self.assertFalse(self.view.selection_bar.visible,
                         "单击被当成选词：划词条对着整段问要不要查词典")
        self.assertEqual("", self.view._selection_word)

    def test_drag_cause_still_lights_the_bar(self):
        """反面那半：只挡 TAP，不许顺手把划词功能弄坏。"""
        self.view._on_text_selection(SimpleNamespace(
            selected_text="归途", cause=ft.TextSelectionChangeCause.DRAG))
        self.assertTrue(self.view.selection_bar.visible)
        self.assertEqual("归途", self.view._selection_word)

    def test_double_tap_still_lights_the_bar(self):
        self.view._on_text_selection(SimpleNamespace(
            selected_text="山海",
            cause=ft.TextSelectionChangeCause.DOUBLE_TAP))
        self.assertTrue(self.view.selection_bar.visible)

    def test_event_without_a_cause_is_passed_through(self):
        """``cause`` 是可选字段：取不到时按原样放行，宁可多亮一条也不关掉划词。"""
        self.view._on_text_selection(SimpleNamespace(selected_text="归途"))
        self.assertTrue(self.view.selection_bar.visible)


# --------------------------------------------------------------------------- #
# 朗读条：音色下拉宽度
# --------------------------------------------------------------------------- #
class ReadAloudVoiceDropdownWidthTests(unittest.TestCase):
    def test_voice_option_label_fits(self):
        """实测 120px 时"内置语音"被截成"内置语"（下拉箭头与内边距各吃掉一截）。"""
        from app.views.reading.read_aloud_bar import ReadAloudBar
        bar = ReadAloudBar()
        self.assertGreaterEqual(bar._voice.width, 150)


# --------------------------------------------------------------------------- #
# 装配根：build_app_shell 必须把 shelf_probe 交给导航宿主
# --------------------------------------------------------------------------- #
class _ShellBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, event):
        return []


class _ShellHub:
    """只满足"按类型取服务"：取不到即 KeyError（与真 hub.get 一致）。"""

    def __init__(self):
        self.bus = _ShellBus()

    def get(self, stype):
        raise KeyError(stype)


class _ShellPage:
    def __init__(self):
        self.controls: list = []
        self.title = ""
        self.on_keyboard_event = None
        self.on_resize = None

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass


class AppShellWiringTests(unittest.TestCase):
    """生产入口的接线：以前 ``build_app_shell`` 把形参 ``shelf_probe`` 直接丢了。

    main.py 一直在传，两条落地用例又都**直接构造 NavigationHost**，所以"进阅读
    优先书架"在生产里从没生效，测试却全绿。这里改走真正的入口函数。
    """

    def setUp(self):
        self._saved_page = nav._APP_PAGE

    def tearDown(self):
        nav._APP_PAGE = self._saved_page

    def _host(self, probe):
        captured = {}
        real = NavigationHost

        class Recording(real):
            def __init__(self, page, registry, **kwargs):
                captured.update(kwargs)
                captured["host"] = self
                super().__init__(page, registry, **kwargs)

        nav.NavigationHost = Recording
        try:
            build_app_shell(_ShellPage(), _ShellHub(), shelf_probe=probe)
        finally:
            nav.NavigationHost = real
        return captured

    def test_probe_reaches_the_navigation_host(self):
        probe = lambda: True  # noqa: E731  身份要可断言：丢进黑洞就查不出来
        captured = self._host(probe)
        self.assertIs(probe, captured.get("shelf_probe"),
                      "装配根传进来的书架探测没交给导航宿主")

    def test_shelf_probe_lands_the_reading_group_on_bookshelf(self):
        self.assertEqual(DEST_BOOKSHELF, self._host(lambda: True)["host"].current)

    def test_empty_shelf_still_lands_on_find_books(self):
        """反面那半：探测说"没书"时不许也落书架。"""
        self.assertEqual(DEST_FIND_BOOKS, self._host(lambda: False)["host"].current)

    def test_without_probe_the_reading_group_lands_on_find_books(self):
        self.assertIsNone(self._host(None)["host"]._shelf_probe)
        self.assertEqual(DEST_FIND_BOOKS, self._host(None)["host"].current)


if __name__ == "__main__":
    unittest.main()
