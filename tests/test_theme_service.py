import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from app.models.appearance_settings import AppearanceSettings
from app.services.theme_service import ThemeService
from app.views.appearance_view import AppearanceView


class ThemeServiceTests(unittest.TestCase):
    def test_round_trip_and_unknown_values_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ThemeService(directory)
            settings = AppearanceSettings(mode="dark", skin_id="emerald_green")
            service.save(settings)
            loaded = ThemeService(directory).load()
            self.assertEqual("dark", loaded.mode)
            self.assertEqual("emerald_green", loaded.skin_id)

            Path(directory, "appearance.json").write_text(
                json.dumps({"mode": "invalid", "skin_id": "missing"}),
                encoding="utf-8")
            loaded = ThemeService(directory).load()
            self.assertEqual("system", loaded.mode)
            self.assertEqual("classic_blue", loaded.skin_id)

    def test_invalid_json_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "appearance.json")
            path.write_text("not-json", encoding="utf-8")
            loaded = ThemeService(directory).load()
            self.assertEqual("classic_blue", loaded.skin_id)
            self.assertFalse(path.exists())
            self.assertEqual(1, len(list(Path(directory).glob("appearance.invalid-*.json"))))

    def test_eink_uses_light_mode_and_monochrome_theme(self):
        page = SimpleNamespace(theme=None, dark_theme=None, theme_mode=None)
        service = ThemeService("unused")
        service.apply(page, AppearanceSettings(mode="eink"))
        self.assertEqual(ft.ThemeMode.LIGHT, page.theme_mode)
        self.assertEqual("#000000", page.theme.color_scheme.primary)
        self.assertEqual("#FFFFFF", page.theme.color_scheme.surface)

    def test_cancel_preview_restores_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ThemeService(directory)
            service.current = AppearanceSettings(mode="dark", skin_id="ink_gray")
            previews = []
            view = AppearanceView(service, previews.append, lambda _message: None)
            view.pending.mode = "light"
            view.pending.skin_id = "warm_amber"
            view.cancel_preview()
            self.assertEqual("dark", view.pending.mode)
            self.assertEqual("ink_gray", view.pending.skin_id)
            self.assertEqual("dark", previews[-1].mode)


if __name__ == "__main__":
    unittest.main()
