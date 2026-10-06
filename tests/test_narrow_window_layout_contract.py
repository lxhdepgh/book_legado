"""第七十七轮：窄窗（320 档）里"会被切掉"的三种形状，静态钉住不许再长回来。

这一条不是风格检查，三条判据**每一根都是桌面客户端当场量出来的**（探针与产物都写在
`docs/dev-logs/2026-10-06.md` 第七十七轮里）：

- **R1（第七十六轮）**：`Row(wrap=True)` 里挂了**显式传过 `expand`** 的子项——
  `expand=False` 也算——整条 Row 在 release 客户端画成一块灰。
- **R2（第七十七轮）**：`Row(wrap=True)` 挂在 ResponsiveRow 的**格子 content** 里。
  同一位置两臂实测：格子里套 wrap 行，音色+语速排完就换行、右边空着 400px；把那条
  wrap 换成不换行 Row，三件立刻排回一行（`out_cell77g_bar_A/B_1200.png`）。而格子
  本身量到 665px——**该给的宽给了**，是 wrap 行在格子里自己塌。所以降级只能走 col，
  不能"格子里再套一条会换行的行"。
- **R3（本轮收的 36 处定宽）**：**不换行** Row 里若干定宽子项合计超出窄窗预算。
  这一行不会重排，超出的部分是真的看不见（朗读条 344 那一档"连读下一章"整只被挤出
  屏幕就是它）。**单个**定宽子项不算违例：客户端会把容器挤到父框宽、内容跟着重排
  （侧栏抽屉 320 挤成 284，头部那颗 `✕` 跟着右移同样的像素 ——
  `out_sc77m_clamped/unclamped.png` 两臂差 4px 的位移）。

预算 `NARROW_BUDGET` 也来自实测：窗口 320 → 客户端面积 304 → 去掉页边距 20 = 284。

红线：全程离线，控件不挂窗口实例化；只读视图自己的公共形状，不碰服务层。
"""
from __future__ import annotations

import unittest

import flet as ft

from app.core.registry import ViewRegistry
from app.views.navigation import register_all_destinations

#: 窄窗里一行能用的宽度（320 窗口 → 客户端 304 → 去掉页边距 20）。
NARROW_BUDGET = 284
#: 不定宽子项的占位估计：一颗图标按钮实测 40px。
_GUESS = 40

_CHILD_SLOTS = ("controls", "content", "title", "subtitle", "leading", "trailing",
                "top", "bottom", "actions", "app_bar", "tab_bar", "tabs",
                "destinations", "rows", "columns", "src", "hint_content",
                "items", "end")

#: 已知要留的例外：`(判据, 目的地) -> 为什么可以留`。空表是本轮的读数；
#: 加一行必须写清理由，且**这条真在窄窗里拍过像素**才算数。
ALLOWED: dict[tuple[str, str], str] = {}


# --------------------------------------------------------------------------- #
# 扫描器
# --------------------------------------------------------------------------- #
def _children(control) -> list:
    out = []
    for slot in _CHILD_SLOTS:
        value = getattr(control, slot, None)
        if value is None:
            continue
        if isinstance(value, ft.Control):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out += [item for item in value if isinstance(item, ft.Control)]
    return out


def _slots(control):
    for slot in _CHILD_SLOTS:
        value = getattr(control, slot, None)
        if isinstance(value, ft.Control):
            yield slot, value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, ft.Control):
                    yield slot, item


def _fixed_width(child):
    width = getattr(child, "width", None)
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return None
    return width


def _label(control) -> str:
    text = getattr(control, "text", None) or getattr(control, "label", None) or ""
    return f"{type(control).__name__}({str(text)[:12]})"


def scan_control(root) -> list[tuple[str, str]]:
    """返回这根控件树里所有违例 `(判据, 说明)`；递归覆盖子树。"""
    hits: list[tuple[str, str]] = []

    def walk(control, in_cell, seen):
        if id(control) in seen:
            return
        seen.add(id(control))
        kids = _children(control)
        if isinstance(control, ft.Row):
            wrapped = bool(getattr(control, "wrap", False))
            if wrapped:
                for child in kids:
                    if getattr(child, "expand", None) is not None:
                        hits.append(("R1", f"wrap 行里 {_label(child)} 传了 expand"))
                if in_cell:
                    hits.append(("R2", "wrap 行挂在 ResponsiveRow 的格子 content 里"))
            elif not isinstance(control, ft.ResponsiveRow):
                widths = [(_fixed_width(c) if _fixed_width(c) is not None else _GUESS)
                          for c in kids]
                fixed = [_label(c) for c in kids if _fixed_width(c) is not None]
                total = (sum(widths)
                         + max(0, len(kids) - 1) * int(getattr(control, "spacing", 0) or 0))
                if len(fixed) >= 2 and total > NARROW_BUDGET:
                    hits.append(("R3", f"{len(kids)} 件合计≈{total}px，"
                                       f"其中定宽 {'、'.join(fixed)}"))
        for slot, child in _slots(control):
            # 格子 = ResponsiveRow 的直接子项（带 col 的那一位）。往里**任何一层**都算：
            # 塌的成因是"松约束下 wrap 只报固有宽"，松约束会顺着子树往下传，
            # 不是只有格子那一格 content 才吃到。
            cell = (isinstance(control, ft.ResponsiveRow)
                    and getattr(child, "col", None) is not None)
            walk(child, cell or in_cell, seen)

    walk(root, False, set())
    return hits


def _all_destinations() -> dict:
    class _NullBus:
        def subscribe(self, *a, **k):
            return lambda: None

        def publish(self, *a, **k):
            return None

    class _Hub:
        """按类型当场无参造真服务（未 start）；视图只读形状，不驱动行为。"""

        def __init__(self) -> None:
            self.bus = _NullBus()
            self._cache: dict = {}

        def _resolve(self, stype):
            if not isinstance(stype, type):
                raise KeyError(stype)
            if stype not in self._cache:
                self._cache[stype] = stype()
            return self._cache[stype]

        get = _resolve
        require = _resolve

        def status_report(self):
            return []

    registry = ViewRegistry()
    register_all_destinations(registry, _Hub())
    return {dest: registry.get(dest).factory() for dest in registry.destinations()}


class NarrowRowContractTests(unittest.TestCase):
    """三条判据各自"看得见越界"——否则全绿的读数不算数。"""

    def test_r1_sees_expand_inside_a_wrap_row(self):
        bad = ft.Row([ft.Text("标题"), ft.Text("尾部", expand=True)], wrap=True)
        self.assertIn("R1", {rule for rule, _ in scan_control(bad)})

    def test_r1_also_fires_on_explicit_expand_false(self):
        # 客户端读法：`expand=False` 与"没传"不一样，整行照样画灰（第七十六轮）。
        bad = ft.Row([ft.Text("标题"), ft.Text("尾部", expand=False)], wrap=True)
        self.assertIn("R1", {rule for rule, _ in scan_control(bad)})

    def test_r2_sees_wrap_row_nested_in_a_cell(self):
        bad = ft.ResponsiveRow([
            ft.Container(col={"xs": 12, "md": 7},
                         content=ft.Row([ft.Text("甲"), ft.Text("乙")], wrap=True)),
        ])
        self.assertIn("R2", {rule for rule, _ in scan_control(bad)})

    def test_r2_fires_even_when_the_wrap_row_is_deeper_in_the_cell(self):
        bad = ft.ResponsiveRow([
            ft.Container(col={"xs": 12}, content=ft.Column([
                ft.Row([ft.Text("甲"), ft.Text("乙")], wrap=True)])),
        ])
        self.assertIn("R2", {rule for rule, _ in scan_control(bad)})

    def test_r3_sees_two_fixed_width_children_that_cannot_reflow(self):
        bad = ft.Row([ft.Container(width=200), ft.Container(width=200)])
        self.assertIn("R3", {rule for rule, _ in scan_control(bad)})

    def test_single_fixed_width_child_is_not_a_violation(self):
        # 客户端会挤到父框宽、内容跟着重排（侧栏抽屉那条两臂量的），不算切掉。
        fine = ft.Row([ft.Container(width=400), ft.Text("可缩")])
        self.assertEqual([], scan_control(fine))

    def test_fixed_widths_that_fit_the_narrow_budget_are_not_flagged(self):
        fine = ft.Row([ft.Container(width=140), ft.Container(width=140)], spacing=4)
        self.assertEqual([], scan_control(fine))


class WholeAppContractTests(unittest.TestCase):
    """24 个目的地全部走完：窄窗三判据**一处都不许有**。"""

    @classmethod
    def setUpClass(cls):
        cls.views = _all_destinations()

    def test_every_destination_constructs(self):
        # 扫描器只能证明它扫到的东西；建不起来 = 那一页根本没进读数，必须先钉住。
        self.assertGreaterEqual(len(self.views), 24,
                                f"目的地少于 24 个，扫描覆盖面变小了：{sorted(self.views)}")

    def test_no_narrow_window_violations(self):
        found = {}
        for dest, view in self.views.items():
            hits = scan_control(view)
            if hits:
                found[dest] = hits
        unexpected = {dest: hits for dest, hits in found.items()
                      if not all((rule, dest) in ALLOWED for rule, _ in hits)}
        self.assertEqual({}, unexpected,
                         "窄窗会被切掉的形状又长回来了：\n" + "\n".join(
                             f"  [{d}] {r} {m}" for d, hs in sorted(unexpected.items())
                             for r, m in hs))

    def test_allowlist_entries_are_all_still_needed(self):
        # 豁免表不许烂在里面：改好了就得删掉，否则下一轮看不见那条真的坏了。
        for (rule, dest), reason in ALLOWED.items():
            self.assertTrue(reason.strip(), f"{rule}/{dest} 的豁免理由没写")
            hits = [h for h in scan_control(self.views[dest]) if h[0] == rule]
            self.assertTrue(hits, f"{rule}/{dest} 已经不再违例，请把豁免项删掉")


class MutatedRealShapesTests(unittest.TestCase):
    """正对照打在**真控件**上：把本轮改掉前的形状原地装回去，扫描器必须报。

    判据若只对探针自搭的桩有效，就等于什么都没钉住（记忆
    `probe-stand-in-must-not-be-the-measured-workload`）。
    """

    def test_read_aloud_bar_old_two_cell_wrap_shape_is_caught(self):
        from app.views.reading.read_aloud_bar import ReadAloudBar

        bar = ReadAloudBar()
        cells = bar.content.controls
        # 第七十七轮改之前的形状：第二格里套一条 wrap 行
        cells[1].col = {"xs": 12, "md": 7}
        cells[1].content = ft.Row([bar._voice, bar._rate, bar._follow], wrap=True)
        self.assertIn("R2", {rule for rule, _ in scan_control(bar)})

    def test_rss_panes_old_fixed_widths_are_caught(self):
        from app.views.reading.rss_view import RssView

        view = RssView()
        # 改之前的三栏宽度（`git diff` 里那一拍）：260 / 340 / 剩下一位 expand。
        # 三块**真栏**装回一条不换行的 Row：扫描器要能认出这一行在窄窗里排不下。
        old = ft.Row([ft.Container(width=260, content=view._source_pane),
                      ft.VerticalDivider(width=1),
                      ft.Container(width=340, content=view._article_pane),
                      ft.VerticalDivider(width=1),
                      ft.Container(expand=True, content=view._detail)])
        self.assertIn("R3", {rule for rule, _ in scan_control(old)})


if __name__ == "__main__":
    unittest.main()
