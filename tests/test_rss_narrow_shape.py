"""第七十八轮护栏（下半）：订阅页的窄窗形状，以及"档位由外壳推"这条通道。

起因是用户那句"重新设计下 ui 看看"。第七十七轮在 344 那档量过并排三栏：flex 收到
3:4:5 之后每栏只剩 ~90–120px——第二栏被切掉半截、第三栏整个在窗外。所以窄窗的形状
不是"三栏再挤一点"，而是**一次只摊开一栏**，顶部一条「订阅源 › 文章 › 正文」级切换
跟着走。

两半各守一件事：

1. **通道**：视图不读 ``page.width``，档位由 :meth:`NavigationHost._notify_band` 推。
   推的时机是跨档重发、show、切页签三处；少一处就会出现"外壳已经换成底部导航栏、
   订阅页还摊着三栏"的错开形状。
2. **形状**：窄窗恰好一栏可见、宽窗恰好三栏可见（这一条是**计数**，所以两档互为对照
   ——只钉"窄窗只剩一栏"而不钉"宽窗还是三栏"，`visible` 全关也能绿）；到不了的级
   置灰；选中后自动跳到下一级；那一级的凭据没了就往前退。

换形状只动 ``visible``、不换父节点，所以还钉一条：两档之间来回切，三栏的容器对象
和行 ListView 的所属关系不许变（变了就是重新下发子树，刷新途中的渲染会白丢一截）。
"""
from __future__ import annotations

import unittest

import flet as ft

from app.core.registry import GROUP_READING, ViewRegistry
from app.views.navigation import NavigationHost
from app.views.reading.rss_view import RssView


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class _BandProbe(ft.Container):
    """实现了钩子的视图：把外壳每次推来的档位记下来。

    必须是 ``ft.Container`` 的子类——外壳拿它当真视图装配（进内容区、被 update），
    用裸 object 就绕过了"这条通道只在真控件上才走得通"这一层。
    """

    def __init__(self) -> None:
        super().__init__()
        self.bands: list[bool] = []

    def on_band_change(self, narrow: bool) -> None:
        self.bands.append(bool(narrow))


class _PlainView(ft.Container):
    """没实现钩子的视图：通道对它必须一次都不碰。"""


class _BoomView(ft.Container):
    def __init__(self) -> None:
        super().__init__()
        self.called = 0

    def on_band_change(self, narrow: bool) -> None:
        self.called += 1
        raise RuntimeError("这一页的降级坏了")


class _EventData:
    """TabBar 的 ``on_change`` 事件形状：客户端只给 ``e.data``（页签下标）。"""

    def __init__(self, data) -> None:
        self.data = data


class _Page:
    def __init__(self, width: float = 1200.0) -> None:
        self.width = width
        self.controls: list = []
        self.updates = 0
        self.adds = 0
        self.on_resize = None
        self.on_keyboard_event = None
        self.title = ""

    def add(self, *controls) -> None:
        self.adds += 1
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1

    def show_dialog(self, dialog) -> None:
        pass

    def pop_dialog(self) -> None:
        pass


class _Hub:
    class _Bus:
        def subscribe(self, *a, **k):
            return lambda: None

        def publish(self, *a, **k):
            return None

    def __init__(self) -> None:
        self.bus = self._Bus()

    def get(self, stype):
        raise KeyError(stype)

    def require(self, stype):
        raise KeyError(stype)


def _host(page: _Page, routes: dict[str, tuple]) -> NavigationHost:
    """按 ``page.width`` 判档位的壳（不注入 ``width``：注入了就永远读不到窗口变化）。

    ``routes`` 为 ``{目的地: (工厂, 标题)}``；首屏目的地取第一项，由构造函数自己 show。
    """
    registry = ViewRegistry()
    for dest, (factory, title) in routes.items():
        registry.register(dest, factory, title=title, group=GROUP_READING)
    first = next(iter(routes))
    return NavigationHost(page, registry, initial_destination=first)


def _visible_panes(view: RssView) -> list[int]:
    return [i for i, pane in enumerate(view._panes) if pane.visible]


# --------------------------------------------------------------------------- #
# 1. 档位通道：外壳推，视图接
# --------------------------------------------------------------------------- #
class BandPushChannelTests(unittest.TestCase):
    def setUp(self):
        import app.views.navigation as navigation
        self.navigation = navigation
        self._saved = navigation._APP_HOST
        self.page = _Page(width=1200.0)

    def tearDown(self):
        self.navigation._APP_HOST = self._saved

    def _attach(self, cls):
        host = _host(self.page, {"reading.rss": (cls, "订阅")})
        return host, host.current_view()

    def test_show_pushes_the_band(self):
        """首屏那一次 show（构造函数尾上）就得推档位，否则视图按默认宽窗摆。"""
        host, view = self._attach(_BandProbe)
        self.assertIs(view.__class__, _BandProbe, "取不到当前视图：装配没走通，这条空跑")
        self.assertEqual(view.bands, [False],
                         "show 之后视图还没拿到档位：窄窗首屏会摊开三栏")

    def test_crossing_the_threshold_repushes(self):
        host, view = self._attach(_BandProbe)
        self.page.width = 400.0
        host.rebuild()
        self.assertEqual(view.bands, [False, True], "跨档重发没推给视图")
        self.page.width = 1100.0
        host.rebuild()
        self.assertEqual(view.bands, [False, True, False])

    def test_tab_switch_repushes_to_a_view_built_in_the_other_band(self):
        """切页签不重建任何东西，但必须补推一次档位。

        形状：宽窗下装配 rss（拿到 False）→ 拖到窄窗（只有当前页拿到 True）→ 点页签
        切回 rss。rss 是上一档宽度下建好的页，不补推就会在窄窗里摊开三栏。
        """
        host = _host(self.page, {"reading.rss": (_BandProbe, "订阅"),
                                 "reading.find": (_BandProbe, "找书")})
        host.show("reading.find")
        section = host._section
        views = section._views
        self.assertEqual(sorted(views), ["reading.find", "reading.rss"],
                         "两页都没装配：下面的推送全落在空处")
        rss, find = views["reading.rss"], views["reading.find"]
        self.assertEqual(rss.bands, [False])

        self.page.width = 400.0
        host.rebuild()                       # 拖到窄窗：只推当前页
        self.assertEqual(find.bands, [False, True])

        self.assertTrue(callable(section.on_tab_selected),
                        "页签回调没挂到外壳：下面的'切回'是空按")
        # 真客户端在发 on_change 之前自己已经改过 selected_index，替身也得先改
        # （外壳的 current_view 就是按它取的）。
        section._tabs.selected_index = 0
        section._on_change(_EventData(data="0"))   # 真入口：用户点第一签
        self.assertEqual(rss.bands, [False, True],
                         "切回的页没被补推档位：它还会按建好那会儿的宽度摆")

    def test_view_without_hook_is_never_touched(self):
        host, view = self._attach(_PlainView)
        self.page.width = 400.0
        root = host.rebuild()                # 没实现钩子的页：一次调用都不许多花，也不许抛
        self.assertIsInstance(view, _PlainView)
        self.assertIsInstance(root, ft.Column)

    def test_a_broken_view_degradation_does_not_break_rebuild(self):
        host, view = self._attach(_BoomView)
        self.page.width = 400.0
        narrow = host.rebuild()              # 视图的降级抛了，根布局照样得换出来
        self.page.width = 1100.0
        wide = host.rebuild()
        self.assertGreaterEqual(view.called, 2, "钩子一次都没被调用：这条是空跑")
        self.assertIsInstance(narrow, ft.Column,
                              "视图抛错把窄窗根换成了宽窗形状")
        self.assertIsInstance(wide, ft.Row)


# --------------------------------------------------------------------------- #
# 2. 订阅页的两副形状
# --------------------------------------------------------------------------- #
class RssShapeTests(unittest.TestCase):
    def setUp(self):
        self.view = RssView()

    def _source(self, sid="s1"):
        return {"id": sid, "name": sid, "url": "http://127.0.0.1/%s.xml" % sid}

    def _article(self, aid="a1"):
        return {"id": aid, "title": "标题", "author": "作者", "url": "http://x"}

    def test_wide_shows_all_three_panes(self):
        """正对照（也是宽窗本来的形状）：宽窗三栏齐亮、切换条收起。
        没有这条，窄窗那条"只剩一栏"会把"全部关掉"也判成绿。"""
        self.view.on_band_change(False)
        self.assertEqual(_visible_panes(self.view), [0, 1, 2])
        self.assertFalse(self.view._switch.visible)
        self.assertTrue(all(d.visible for d in self.view._dividers))

    def test_narrow_shows_exactly_one_pane(self):
        self.view.on_band_change(True)
        self.assertEqual(_visible_panes(self.view), [0],
                         "窄窗摊开了 %d 栏：并排三栏每栏只剩 ~90px，读不了"
                         % len(_visible_panes(self.view)))
        self.assertTrue(self.view._switch.visible)
        self.assertFalse(any(d.visible for d in self.view._dividers))

    def test_the_visible_pane_follows_the_level(self):
        self.view.on_band_change(True)
        self.view.render_sources([self._source()], {}, "s1")
        self.view._pick_source("s1")
        self.assertEqual(_visible_panes(self.view), [1])
        self.view.show_article(self._article(), "正文")
        self.view._pick_article(self._article())
        self.assertEqual(_visible_panes(self.view), [2])

    def test_shape_switch_never_reparents_the_panes(self):
        """只动 visible：三栏容器对象、栏里的行 ListView 归属都不许变。"""
        contents = [self.view._source_pane, self.view._article_pane, self.view._detail]
        for narrow in (True, False, True, False):
            self.view.on_band_change(narrow)
        self.assertEqual([pane.content for pane in self.view._panes], contents,
                         "换形状把栏换了对象：那是重新下发整棵子树")
        self.assertIn(self.view._sources, contents[0].controls)
        self.assertIn(self.view._articles, contents[1].controls)

    def test_picking_in_wide_does_not_move_the_level(self):
        """宽窗三栏都在眼前，点选不该把谁藏起来，也不该改"当前那一级"。

        级别状态也要钉：宽窗下悄悄记成"正文"，一拖回窄窗就摊开一栏正文，
        而用户的视线还在源列表上。
        """
        self.view.on_band_change(False)
        self.view.render_sources([self._source()], {}, "s1")
        self.view._pick_source("s1")
        self.view.show_article(self._article(), "正文")
        self.view._pick_article(self._article())
        self.assertEqual(_visible_panes(self.view), [0, 1, 2])
        self.assertEqual(self.view._level, "sources",
                         "宽窗点选把级别推到了 %s" % self.view._level)

    # -- 级可达性 -------------------------------------------------------- #
    # 0.86 的按钮文本在 ``.content``（不是 ``.text``），也没有可用的 ``ft.Button``
    # 基类（那是个枚举），所以按具体类筛。
    _BUTTONS = (ft.TextButton, ft.FilledTonalButton)

    def _switch_buttons(self):
        return [c for c in self.view._switch.controls
                if isinstance(c, self._BUTTONS)]

    def _by_title(self):
        return {b.content: b for b in self._switch_buttons()}

    def test_unreachable_levels_are_disabled(self):
        self.view.on_band_change(True)
        by_title = self._by_title()
        self.assertEqual(sorted(by_title), ["文章", "正文", "订阅源"],
                         "级切换条上不是这三級")
        self.assertFalse(by_title["订阅源"].disabled)
        self.assertTrue(by_title["文章"].disabled, "还没选源就能跳文章")
        self.assertTrue(by_title["正文"].disabled)

    def test_selecting_a_source_makes_articles_reachable(self):
        self.view.on_band_change(True)
        self.view.render_sources([self._source()], {}, "s1")
        self.assertFalse(self._by_title()["文章"].disabled,
                         "正对照：选中源之后文章必须可达")

    def test_go_refuses_the_unreachable_level(self):
        self.view.on_band_change(True)
        self.view._go("detail")
        self.assertEqual(_visible_panes(self.view), [0])

    def test_clearing_detail_walks_back_from_the_dead_level(self):
        """停在"正文"时正文被清空（换源/删光）：那一栏已经空了，人得被送回有内容的那级。"""
        self.view.on_band_change(True)
        self.view.render_sources([self._source()], {}, "s1")
        self.view.show_article(self._article(), "正文")
        self.view._go("detail")
        self.assertEqual(self.view._level, "detail")
        self.view.clear_detail("选一篇文章看正文")
        self.assertNotEqual(self.view._level, "detail",
                            "正文级已经不可达，还停在原地＝摊开一栏空白")
        self.assertEqual(_visible_panes(self.view), [1])

    def test_removing_all_sources_walks_back_to_the_list(self):
        self.view.on_band_change(True)
        self.view.render_sources([self._source()], {}, "s1")
        self.view._go("articles")
        self.view.render_sources([], {}, "")     # 源被删光
        self.assertEqual(self.view._level, "sources")
        self.assertEqual(_visible_panes(self.view), [0])

    # -- 行形状（像素面量出来的，见 .probe/probe_rss_shape80.py） ---------- #
    def test_source_row_trailing_is_not_a_row(self):
        """源行的 trailing 一旦是 ``Row``（未读计数 + 按钮），标题就被挤成 0 宽。

        第七十八轮像素：1184 与 344 两档里四行源都只剩图标和「⋮」，源名整行不见
        （`.probe/out_rss80_wide1.png`）。文章那一栏的 trailing 是单个 IconButton，
        同档宽度下标题正常——所以钉住"不许是 Row"，未读计数改落副标题。
        """
        self.view.render_sources([self._source()], {"s1": {"total": 12, "unread": 7}},
                                 "s1")
        tile = self.view._sources.controls[0]
        self.assertNotIsInstance(tile.trailing, ft.Row,
                                 "trailing 是 Row 时标题被挤成 0 宽（源名在屏幕上消失）")
        self.assertIn("7 未读", tile.subtitle.value,
                      "未读计数没地方去了：挪进副标题就要看得见")


if __name__ == "__main__":
    unittest.main()
