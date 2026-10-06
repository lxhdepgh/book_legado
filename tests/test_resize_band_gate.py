"""第七十八轮护栏：拖窗那两条实测出来的成本。

起因是用户那句"现在应用无法顺畅拖动"。取证（`.probe/probe_drag78*.py`，真窗口、
同步 `SetWindowPos` 每一拍等窗口线程处理完才返回）量到两笔钱，本文件一条守一笔：

1. **整拖期间每一拍都重发整棵根树**。真数据（917 条书源、停在书源列表页）下
   处理器单次 0.5–2.1 s，而拖窗 30 拍里 Python 只收到 1–4 次 ``on_resize``
   （客户端自己合掉了）——重发换来的是界面循环同期落后 max 545 ms。
   守法：同档位内连续 resize **不许下发**；跨阈值必须下发（不跨不管，跨了不管
   就是把底部导航栏留在宽窗上）。
2. **长列表装在 `Column(scroll=AUTO)` 里**。同样 917 行、同一行形状：
   ListView 11–12 ms／Column 237 ms（空页底价 11 ms；917 条裸 Text 装 Column
   也只 13 ms，所以贵的是"行数 × 每行控件数"，客户端只对 ListView 按可见区建行）。
   守法：逐页钉住"承载记录行的那个容器必须是 ListView"。

仪器规矩（都在这份文件里兑现）：
- 钉的是"没发生"，不是"失败了"（第 1 组断言 `page.updates` 不涨，而不是不抛异常）；
- 计数为 0 的读数要一根明知非 0 的对照（同文件里必须有一条"跨阈值确实下发了"）；
- 处理器替身必须真的挂在 ``page.on_resize`` 上并被调用（先断言 callable，
  免得"没接线"这条退化成"什么都不做"的绿）。
"""
from __future__ import annotations

import unittest

import flet as ft

from app.core.registry import GROUP_READING, ViewRegistry
from app.views.navigation import NavigationHost, build_app_shell, current_host
from app.views.reading.bookshelf_view import BookshelfView
from app.views.reading.find_books_view import FindBooksView
from app.views.reading.reader_view import ReaderView
from app.views.reading.rss_view import RssView
from app.views.source.book_source_list_view import BookSourceListView
from app.views.source.check_view import CheckView


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class _Page:
    """够外壳用的最小窗口面：width 可注入，update/add 计数用来钉"有没有下发"。"""

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

    def show_dialog(self, dialog) -> None:      # 外壳不直接用，视图弹窗才要
        pass

    def pop_dialog(self) -> None:
        pass


class _Hub:
    """装配整盘用的最小服务表：`get/require` 一律报"没注册"（首屏就是这个形状）。"""

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


def _registry() -> ViewRegistry:
    registry = ViewRegistry()
    registry.register("reading.find", lambda: ft.Container(),
                      title="找书", group=GROUP_READING)
    return registry


# --------------------------------------------------------------------------- #
# 1. resize 只在跨档位那一拍重发
# --------------------------------------------------------------------------- #
class ResizeBandGateTests(unittest.TestCase):
    #: 与 navigation.NARROW_WIDTH_THRESHOLD 同值，但**不 import 那个常量**：
    #: 护栏不许读被测代码自己的常量来定阈值（改了阈值连同这条一起绿掉）。
    THRESHOLD = 720.0

    def setUp(self):
        self.page = _Page(width=1200.0)
        import app.views.navigation as navigation
        self.navigation = navigation
        self._saved = navigation._APP_HOST
        build_app_shell(self.page, _Hub())
        self.host = current_host()
        self.assertIsNotNone(self.host)

    def tearDown(self):
        self.navigation._APP_HOST = self._saved

    def _resize(self, width: float) -> None:
        self.page.width = width
        self.assertTrue(callable(self.page.on_resize),
                        "外壳没挂 on_resize：下面所有'没下发'都是没接线的假绿")
        self.page.on_resize(None)

    def test_same_band_resizes_never_repaint(self):
        """宽档内一路拖：一次都不许重发整棵根树。"""
        root, updates, adds = self.host.root, self.page.updates, self.page.adds
        for width in (1150.0, 1040.0, 960.0, 880.0, 800.0, 721.0):
            self._resize(width)
        self.assertEqual(self.page.updates, updates,
                         "同档位内 resize 下发了控件：整拖期间界面循环要背 0.5 s/次")
        self.assertEqual(self.page.adds, adds, "同档位内 resize 又 add 了一次根")
        self.assertIs(self.host.root, root, "同档位内根被换掉了")

    def test_narrow_band_resizes_never_repaint(self):
        root = self.host.root
        updates = self.page.updates
        self._resize(400.0)                      # 先跨进窄档
        self.assertIsNot(self.host.root, root)
        root, updates = self.host.root, self.page.updates
        for width in (399.0, 360.0, 330.0):
            self._resize(width)
        self.assertEqual(self.page.updates, updates, "窄档内拖窗一样不许重发")
        self.assertIs(self.host.root, root)

    # -- 正对照：闸门不能把"该重建"也一起关掉 ---------------------------- #
    def test_crossing_wide_to_narrow_rebuilds_and_repaints(self):
        adds_before, updates_before = self.page.adds, self.page.updates
        self._resize(400.0)
        self.assertGreater(self.page.updates, updates_before,
                           "跨阈值没下发：正对照失败，上面的'不下发'全是假绿")
        self.assertGreater(self.page.adds, adds_before, "跨阈值没把新根挂回页面")
        self.assertIsInstance(self.host.root.controls[-1], ft.NavigationBar,
                              "宽→窄要换成底部导航栏")

    def test_crossing_narrow_to_wide_rebuilds_again(self):
        self._resize(400.0)
        adds_before, updates_before = self.page.adds, self.page.updates
        self._resize(1100.0)
        self.assertGreater(self.page.updates, updates_before, "窄→宽没重发")
        self.assertIsInstance(self.host.root.controls[0], ft.NavigationRail,
                              "窄→宽要换回侧栏")

    def test_oscillating_around_the_threshold_repaints_each_cross(self):
        """阈值上来回 721/719：每一次跨界都得重发（守住"闸门=按档位，不是按次数"）。"""
        crosses = 0
        for width in (719.0, 721.0, 719.0, 721.0):
            before = self.page.updates
            self._resize(width)
            crosses += 1 if self.page.updates > before else 0
        self.assertEqual(crosses, 4, "来回跨界只重发了 %d 次" % crosses)

    def test_host_without_injected_width_falls_back_to_page_width(self):
        """`_is_narrow` 读的是 page.width：注入替身优先，没注入才读窗口。"""
        host = NavigationHost(self.page, _registry(), width=400.0,
                              initial_destination="reading.find")
        self.assertTrue(host._is_narrow(), "注入了 400 还说宽窗")
        host._width = None
        self.assertFalse(host._is_narrow(), "回读 page.width=1200 应判宽窗")


# --------------------------------------------------------------------------- #
# 2. 承载记录行的容器必须是虚拟化的 ListView
# --------------------------------------------------------------------------- #
class LongListContainerTests(unittest.TestCase):
    #: (页面, 那个装记录行的容器属性)。加新页时把它的行容器加进这张表。
    ROW_HOSTS = [
        ("书源列表", BookSourceListView, "_cards"),
        ("检测结果", CheckView, "_rows"),
        ("书架", BookshelfView, "_list"),
        ("找书·书源", FindBooksView, "_source_list"),
        ("找书·结果", FindBooksView, "_search_list"),
        ("订阅·源", RssView, "_sources"),
        ("订阅·文章", RssView, "_articles"),
        ("阅读器·目录", ReaderView, "_directory_list"),
    ]

    def test_record_row_hosts_are_virtualized(self):
        for label, view_cls, attr in self.ROW_HOSTS:
            view = view_cls()
            host = getattr(view, attr, None)
            self.assertIsNotNone(host, f"{label} 的行容器 {attr} 不见了")
            self.assertIsInstance(
                host, ft.ListView,
                f"{label} 的行容器是 {type(host).__name__}：客户端只对 ListView 按可见区"
                f"建行，917 行真实形状装进滚动 Column 时每拍改尺寸要 237 ms（拖不动窗口）")

    def test_source_list_has_no_dead_batch_constant(self):
        """`BATCH_SIZE` 是重构后留下的孤儿（控制器每次都渲染全表）。
        留着它等于给下一个人"这里有懒加载"的假线索——ListView 虚拟化之后更不需要。"""
        self.assertFalse(hasattr(BookSourceListView, "BATCH_SIZE"),
                         "行容器已虚拟化，别再留一个没人用的分批常量")


if __name__ == "__main__":
    unittest.main()
