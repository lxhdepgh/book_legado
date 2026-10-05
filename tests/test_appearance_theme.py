"""外观主题下发护栏（§3.11 / §5.11）。

钉住三件真实踩过的事：

1. **主题必须三件套一起下发**——``page.theme`` 只在亮面生效，缺了
   ``page.dark_theme`` + ``page.theme_mode`` 时，深色桌面上换皮肤毫无反应
   （表现："主题切换没用、界面一片默认灰"）；
2. **预览区不许再把 ``expand`` 子控件塞进 ``wrap=True`` 的 Row**——那对组合在
   Flet 0.86 里渲染成灰色区域（项目已知陷阱，用户点名的"灰色区域"）；
3. **启动即应用已保存的外观**（``main._apply_saved_theme``）——不点进外观页也应
   是用户上次保存的皮肤，而不是 Flet 默认主题。
"""
from __future__ import annotations

import unittest
from unittest import mock

import flet as ft

from app.controllers.settings.appearance_controller import AppearanceController
from app.services.tools.theme_service import (BUILTIN_SKINS, ThemeService,
                                              ThemeData)
from app.views.settings.appearance_view import (AppearanceView,
                                                apply_page_theme, build_theme)


class _FakePage:
    """只记账的页面替身（``page.update()`` 真发协议，无头时不可用）。"""

    def __init__(self):
        self.theme = None
        self.dark_theme = None
        self.theme_mode = None
        self.updates = 0

    def update(self):
        self.updates += 1


def _data(mode: str, eink: bool = False, skin: str = "classic_blue") -> ThemeData:
    return ThemeService().apply(skin_id=skin, dark_mode=mode, eink=eink)


# --------------------------------------------------------------------------- #
# 1. 三件套下发
# --------------------------------------------------------------------------- #
class PageThemeTests(unittest.TestCase):

    def test_light_mode_sets_both_themes_and_mode(self):
        page = _FakePage()
        apply_page_theme(page, _data("light"))
        self.assertIs(page.theme_mode, ft.ThemeMode.LIGHT)
        self.assertIsNotNone(page.theme)
        self.assertIsNotNone(page.dark_theme)
        self.assertEqual(page.updates, 1)

    def test_dark_mode_ships_a_dark_scheme_and_dark_theme(self):
        """只设 page.theme 时暗面桌面用的是 dark_theme（默认灰）——这条正是回归点。"""
        page = _FakePage()
        apply_page_theme(page, _data("dark", skin="ink_gray"))
        self.assertIs(page.theme_mode, ft.ThemeMode.DARK)
        dark = page.dark_theme.color_scheme
        self.assertEqual(dark.surface, BUILTIN_SKINS["ink_gray"].dark.surface)
        self.assertNotEqual(page.theme.scaffold_bgcolor,
                            page.dark_theme.scaffold_bgcolor)

    def test_system_mode_keeps_both_variants(self):
        page = _FakePage()
        apply_page_theme(page, _data("system"))
        self.assertIs(page.theme_mode, ft.ThemeMode.SYSTEM)
        self.assertIsNotNone(page.theme)
        self.assertIsNotNone(page.dark_theme)

    def test_eink_uses_one_black_white_theme_and_forces_light(self):
        page = _FakePage()
        apply_page_theme(page, _data("dark", eink=True))
        self.assertIs(page.theme, page.dark_theme)
        self.assertIs(page.theme_mode, ft.ThemeMode.LIGHT)
        self.assertEqual(page.theme.scaffold_bgcolor, "#FFFFFF")
        self.assertIs(page.theme.visual_density, ft.VisualDensity.COMPACT)

    def test_every_skin_builds_a_theme_for_both_variants(self):
        """五个皮肤 × 亮暗两面都要能真构造出 ft.Theme（Flet 表面错只在构造瞬间抛）。"""
        for skin_id in BUILTIN_SKINS:
            data = _data("system", skin=skin_id)
            light, dark = build_theme(data.light), build_theme(data.dark)
            self.assertIsInstance(light, ft.Theme)
            self.assertIsInstance(dark, ft.Theme)
            self.assertTrue(light.use_material3)
            for theme, palette in ((light, data.light), (dark, data.dark)):
                self.assertEqual(theme.scaffold_bgcolor, palette["surface"])
                self.assertEqual(theme.card_bgcolor, palette["surface_container"])
                self.assertEqual(theme.divider_color, palette["outline"])
                self.assertEqual(theme.color_scheme.surface_container,
                                 palette["surface_container"])

    def test_none_page_or_none_data_is_a_no_op(self):
        page = _FakePage()
        apply_page_theme(None, _data("light"))
        apply_page_theme(page, None)
        self.assertIsNone(page.theme)
        self.assertEqual(page.updates, 0)


# --------------------------------------------------------------------------- #
# 2. 预览区形状：不许 wrap + expand，且预览必须是真控件
# --------------------------------------------------------------------------- #
def _walk(control):
    yield control
    for attr in ("content", "controls", "tabs"):
        child = getattr(control, attr, None)
        if isinstance(child, ft.Control):
            yield from _walk(child)
        elif isinstance(child, (list, tuple)):
            for item in child:
                if isinstance(item, ft.Control):
                    yield from _walk(item)
    for attr in ("label", "title", "subtitle", "trailing", "leading", "icon"):
        child = getattr(control, attr, None)
        if isinstance(child, ft.Control):
            yield from _walk(child)


class PreviewLayoutTests(unittest.TestCase):

    def setUp(self):
        self.view = AppearanceView()

    def test_no_expanded_child_inside_a_wrapping_row(self):
        for node in _walk(self.view):
            if isinstance(node, ft.Row) and getattr(node, "wrap", False):
                for child in node.controls or []:
                    self.assertFalse(getattr(child, "expand", None),
                                     "wrap=True 的 Row 里放 expand 子控件会渲染成灰色区域")

    def test_preview_shows_real_controls_not_hand_painted_slabs(self):
        kinds = {type(node).__name__ for node in _walk(self.view._preview)}
        self.assertIn("FilledButton", kinds)
        self.assertIn("TextField", kinds)

    def test_apply_preview_pushes_theme_onto_the_page(self):
        page = _FakePage()
        with mock.patch.object(AppearanceView, "page",
                               new=property(lambda self: page)):
            self.view.apply_preview(_data("dark", skin="emerald_green"))
        self.assertIs(page.theme_mode, ft.ThemeMode.DARK)
        self.assertEqual(page.dark_theme.scaffold_bgcolor,
                         BUILTIN_SKINS["emerald_green"].dark.surface)

    def test_unmounted_view_skips_theme_without_touching_any_page(self):
        # ``Control.page`` 未挂载时抛 RuntimeError：取不到页面就没有可着色的窗口。
        self.view.apply_preview(_data("light"))


# --------------------------------------------------------------------------- #
# 3. 控制器：pending → 预览，失败要写在状态行上
# --------------------------------------------------------------------------- #
class _RecordingView:
    """只记调用的外观视图替身（控制器侧测试不建 Flet 控件）。"""

    def __init__(self):
        self.previews: list[ThemeData] = []
        self.statuses: list[tuple[str, bool]] = []
        self.applied: list[tuple[str, str, bool]] = []
        self.backgrounds: list[str] = []
        self.controller = None
        self.page = None

    def set_preferences(self, skin_id, mode, eink):
        self.applied.append((skin_id, mode, eink))

    def apply_preview(self, data):
        self.previews.append(data)

    def set_status(self, text, error=False):
        self.statuses.append((str(text), bool(error)))

    def set_background(self, path):
        self.backgrounds.append(str(path))


class _Hub:
    def __init__(self, service):
        self._service = service
        self.bus = None

    def get(self, stype):
        # 按类型给：外观控制器现在还要 BackgroundService，替身不分类型就返回同一个
        # 对象，"那个服务没注册"这条线会被测成"齐了"（未就绪的降级永远走不到）。
        svc = self._service
        return svc if isinstance(svc, stype) else None


class AppearanceControllerTests(unittest.TestCase):

    def setUp(self):
        self.theme = ThemeService()
        self.view = _RecordingView()
        self.controller = AppearanceController(_Hub(self.theme))
        self.controller.attach(self.view)

    def test_attach_loads_saved_preferences_and_previews_once(self):
        self.theme.set_preferences(skin_id="warm_amber", mode="dark")
        self.controller.load()
        self.assertEqual(self.view.applied[-1], ("warm_amber", "dark", False))
        self.assertEqual(self.view.previews[-1].skin_id, "warm_amber")
        self.assertEqual(self.view.previews[-1].mode, "dark")

    def test_mode_change_previews_without_persisting(self):
        before = self.theme.get_preferences()
        self.controller.change_mode("dark")
        self.assertEqual(self.view.previews[-1].mode, "dark")
        self.assertEqual(self.theme.get_preferences(), before,
                         "预览不得写库：保存才落盘")

    def test_save_persists_pending_and_status_says_so(self):
        self.controller.change_skin("elegant_purple")
        self.controller.change_eink(True)
        self.controller.save()
        self.assertEqual(self.theme.get_preferences(),
                         {"skin_id": "elegant_purple", "mode": "system",
                          "eink": True})
        self.assertIn("已保存", self.view.statuses[-1][0])

    def test_reset_returns_to_defaults_and_previews(self):
        self.controller.change_skin("ink_gray")
        self.controller.reset()
        self.assertEqual(self.view.previews[-1].skin_id, "classic_blue")
        self.assertEqual(self.theme.get_preferences()["skin_id"], "classic_blue",
                         "恢复默认只改预览，不得抢先落盘")

    def test_preview_failure_is_reported_on_the_status_line(self):
        """只记日志等于"点了没反应"——预览炸了必须在界面上说清楚。"""
        boom = _RecordingView()
        boom.apply_preview = mock.Mock(side_effect=RuntimeError("控件树没了"))
        AppearanceController(_Hub(self.theme)).attach(boom)
        text, is_error = boom.statuses[-1]
        self.assertTrue(is_error)
        self.assertIn("控件树没了", text)

    def test_missing_service_reports_ready_state_instead_of_crashing(self):
        class _NoHub(_Hub):
            def get(self, stype):
                raise KeyError("ThemeService 未注册")

        view = _RecordingView()
        AppearanceController(_NoHub(None)).attach(view)   # 不得抛
        text, is_error = view.statuses[-1]
        self.assertTrue(is_error)
        self.assertIn("未就绪", text)


# --------------------------------------------------------------------------- #
# 4. 启动引导：已保存的外观在首屏就生效
# --------------------------------------------------------------------------- #
class StartupThemeTests(unittest.TestCase):

    def test_saved_theme_applied_before_the_shell_is_built(self):
        import main

        theme = ThemeService()
        theme.set_preferences(skin_id="ink_gray", mode="dark")
        page = _FakePage()
        main._apply_saved_theme(page, _Hub(theme))
        self.assertIs(page.theme_mode, ft.ThemeMode.DARK)
        self.assertEqual(page.dark_theme.scaffold_bgcolor,
                         BUILTIN_SKINS["ink_gray"].dark.surface)

    def test_bootstrap_survives_an_unregistered_service(self):
        import main

        class _BadHub:
            bus = None

            def get(self, stype):
                raise KeyError(stype.__name__)

        page = _FakePage()
        main._apply_saved_theme(page, _BadHub())
        self.assertIsNone(page.theme)


if __name__ == "__main__":
    unittest.main()
