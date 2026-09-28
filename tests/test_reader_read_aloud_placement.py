"""第二十九轮：朗读入口搬进工具栏、朗读条搬到阅读区上方的版面护栏。

用户原话是**版面**要求："朗读功能放到区域上方和书内搜索、设置放在一起。" 之前
这条迷你播放条挂在 ``_reader_page`` 的**倒数第二个**位置（状态栏之上），离它的
同伴（目录/书签/书内搜索/阅读设置）隔着一整个正文区。所以钉三件事：

1. **落点**：朗读条紧跟在工具栏下面（不再沉底），工具栏里有一颗「朗读」入口，
   且它和「书内搜索」「阅读设置」在同一处（中间不许插别的动作）；
2. **收起不吞停止**：入口负责收起/展开那一条（否则顶部永久占一条高度），但
   **正在念的时候不允许收起来**——桌面模式没有通知栏，收起就等于把唯一的停止键
   一起藏掉，声音停在后台没人按得下来；
3. **可用性是控制器的事**：没有朗读服务（替身装配/精简启动）或没开章时，入口
   整颗禁用、面板不占位置。这一条以前由 ``bar.visible = ...`` 直接写控件，现在
   必须经 :meth:`ReaderView.set_read_aloud_available`——可见性归视图，"能不能念"
   归控制器。

红线（§7.5）：视图不 import 服务；这里也只经公共接口，不伸手进服务层。
全程离线：控件无窗口实例化，服务全是替身，不联网（记忆 legado-live-scan-av-block）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import flet as ft

from app.controllers.reader_controller import ReaderController
from app.services.reading.reading_service import ReadingService
from app.services.content.read_aloud import ReadAloudService
from app.views.reading.reader_view import ReaderView


def _tooltips(view) -> list[str]:
    return [str(getattr(btn, "tooltip", "") or "")
            for btn in view._toolbar.content.controls]


def _button(view, tooltip: str):
    for btn in view._toolbar.content.controls:
        if getattr(btn, "tooltip", "") == tooltip:
            return btn
    raise AssertionError(f"工具栏里没有 {tooltip}：{ _tooltips(view)}")


class ReadAloudPlacementTests(unittest.TestCase):
    """朗读条跟着工具栏走；入口和书内搜索/阅读设置挨在一起。"""

    def setUp(self):
        self.view = ReaderView()

    def test_bar_sits_directly_under_the_toolbar(self):
        controls = self.view._reader_page.controls
        self.assertEqual(controls.index(self.view._toolbar) + 1,
                         controls.index(self.view.read_aloud),
                         "朗读条没紧跟工具栏：它又沉回正文下面去了")
        self.assertIsNot(controls[-1], self.view.read_aloud,
                         "朗读条还挂在末尾那一格")

    def test_opener_lives_between_the_search_and_settings_buttons(self):
        tooltips = _tooltips(self.view)
        # 图标名会随收起态翻（GRAPHIC_EQ / _OUTLINED），所以按"这一组"而不是死字符串认
        opener = self.view._read_aloud_btn
        self.assertIn(opener, self.view._toolbar.content.controls,
                      "朗读没有工具栏入口")
        position = tooltips.index(opener.tooltip)
        search = tooltips.index("书内搜索")
        settings = next(i for i, name in enumerate(tooltips) if name == "阅读设置")
        self.assertTrue(search < position < settings,
                        f"朗读入口没和书内搜索/设置放在一起：{tooltips}")

    def test_toolbar_row_has_no_unwrapped_overflow(self):
        """工具栏是一颗 Row（不换行）：控件再往上添，就得同时报出"这一行有多长"。

        钉的是"没人为新增的那颗键留位置"——1280 宽时版心左边有 266px 导航栏，
        工具栏实际可用约 1010px，12 颗 40px 图标 + 进度标签已经吃掉 ~560px。
        """
        row = self.view._toolbar.content
        self.assertIsInstance(row, ft.Row)
        self.assertFalse(getattr(row, "wrap", False),
                         "工具栏换成 wrap 之后高度会随窗口变化，第二十八轮的栅格教训")


class ReadAloudFoldTests(unittest.TestCase):
    """入口收起/展开那一条；正在念的时候不许收。"""

    def setUp(self):
        self.view = ReaderView()
        self.view.set_read_aloud_available(True)

    def test_it_starts_expanded(self):
        """默认展开：朗读是"点一下就能听起来"的功能，缩起来就没人知道它在。"""
        self.assertTrue(self.view.read_aloud.visible)
        self.view.toggle_read_aloud_panel()
        self.assertFalse(self.view.read_aloud.visible, "工具栏那颗按下去收起不了朗读条")
        self.view.toggle_read_aloud_panel()
        self.assertTrue(self.view.read_aloud.visible, "收起之后再按该展开")

    def test_opener_is_the_click_path_not_just_a_decoration(self):
        """点击走的是控件自己的 on_click：接线断了的形状是"看着能按"。"""
        self.view._read_aloud_btn.on_click(None)
        self.assertFalse(self.view.read_aloud.visible)

    def test_folding_is_refused_while_speaking(self):
        """桌面模式没有通知栏：念着的时候收起 = 停止键一起没了。"""
        self.view.read_aloud.set_state("speaking", chapter="第一章")
        self.view.sync_read_aloud()
        self.view.toggle_read_aloud_panel()
        self.assertTrue(self.view.read_aloud.visible,
                        "念着还能收起，用户就只剩「离开阅读器」一条路能停")
        self.assertTrue(self.view._read_aloud_btn.disabled,
                        "既然按不动，就得让它看起来按不动")
        self.assertIn("停止", self.view._read_aloud_btn.tooltip,
                      "禁用而不说原因 = 用户以为是坏了")

    def test_it_can_be_folded_again_after_the_voice_stops(self):
        """反面那半：不许把"收起"永久禁掉。"""
        self.view.read_aloud.set_state("speaking", chapter="第一章")
        self.view.sync_read_aloud()
        self.view.read_aloud.set_state("idle")
        self.view.sync_read_aloud()
        self.assertFalse(self.view._read_aloud_btn.disabled)
        self.view.toggle_read_aloud_panel()
        self.assertFalse(self.view.read_aloud.visible)

    def test_icon_reads_the_fold(self):
        """亮着的那一面就是当前读数（与沉浸那颗同一规则）。"""
        opened = self.view._read_aloud_btn.icon
        self.view.toggle_read_aloud_panel()
        self.assertNotEqual(opened, self.view._read_aloud_btn.icon,
                            "收起与展开画的是同一个图标：读不出当前状态")


class ReadAloudAvailabilityTests(unittest.TestCase):
    """控制器只报"这会儿能不能念"；可见性与禁用是视图的事。"""

    def _controller(self, view, *, with_tts: bool):
        services = {ReadingService: SimpleNamespace(
            stop_prefetch=lambda: None,
            get_reader_preference=lambda *a: _none_coro(),
            save_reader_preference=lambda *a: _none_coro(),
            reading_state=lambda record: {})}
        if with_tts:
            services[ReadAloudService] = SimpleNamespace(
                list_profiles=lambda: _none_coro([]),
                set_voice=lambda profile: None)
        hub = SimpleNamespace(bus=None, get=lambda stype: services[stype])
        controller = ReaderController(hub)
        controller._view = view
        controller._reading = services[ReadingService]
        controller._tts = services.get(ReadAloudService)
        controller._book_label = "书名"
        return controller

    def test_no_tts_service_disables_the_opener_and_hides_the_bar(self):
        view = ReaderView()
        controller = self._controller(view, with_tts=False)
        controller._readable = [{"name": "第一章", "url": "u1"}]
        controller._chapter_index = 0
        controller._update_chapter_navigation()
        self.assertFalse(view.read_aloud.visible, "没有朗读服务还亮着朗读条")
        self.assertTrue(view._read_aloud_btn.disabled,
                        "按不动的入口亮着，用户会以为点了没反应")

    def test_a_chapter_on_a_tts_backend_enables_it(self):
        """反面那半：不许把可用性恒关（那正是"服务在、界面接不上"的形状）。"""
        view = ReaderView()
        controller = self._controller(view, with_tts=True)
        controller._readable = [{"name": "第一章", "url": "u1"}]
        controller._chapter_index = 0
        controller._update_chapter_navigation()
        self.assertTrue(view.read_aloud.visible)
        self.assertFalse(view._read_aloud_btn.disabled)
        self.assertEqual("收起朗读", view._read_aloud_btn.tooltip)

    def test_closing_the_book_takes_the_entry_away(self):
        """没章可念 = 那条面板和那颗入口一起下台：留着一个能按的朗读键是假线索。"""
        view = ReaderView()
        controller = self._controller(view, with_tts=True)
        controller._readable = [{"name": "第一章", "url": "u1"}]
        controller._chapter_index = 0
        controller._update_chapter_navigation()
        self.assertTrue(view.read_aloud.visible)
        controller._chapter_index = -1
        controller._update_chapter_navigation()
        self.assertFalse(view.read_aloud.visible, "退出到没有章的状态，朗读条还挂着")
        self.assertTrue(view._read_aloud_btn.disabled,
                        "没章可念时入口还能按，按下去只会得到一句「先打开一章再朗读」")
        # 再开一章要能回来（不许一次关闭就把可用性焊死）
        controller._chapter_index = 0
        controller._update_chapter_navigation()
        self.assertTrue(view.read_aloud.visible)
        self.assertFalse(view._read_aloud_btn.disabled)


    def test_the_state_reaches_the_opener_through_the_controller(self):
        """控制器 → 视图这条路也要通：只测视图自己同步，接线断了照样绿。"""
        view = ReaderView()
        controller = self._controller(view, with_tts=True)
        controller._readable = [{"name": "第一章", "url": "u1"}]
        controller._chapter_index = 0
        controller._update_chapter_navigation()
        controller._show_read_aloud("speaking", chapter="第一章")
        self.assertTrue(view.read_aloud.visible)
        self.assertTrue(view._read_aloud_btn.disabled,
                        "控制器把态写进播放条却没回流到入口：念着时那颗还能按收起")
        view.toggle_read_aloud_panel()
        self.assertTrue(view.read_aloud.visible, "回流没生效时，收起会把停止键一起藏掉")


async def _none_coro(value=None):
    return value


if __name__ == "__main__":
    unittest.main()
