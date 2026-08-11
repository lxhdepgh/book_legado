"""Load, validate, persist and apply Flet appearance settings."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import flet as ft

from app.models.appearance_settings import AppearanceSettings
from app.themes.builtin_themes import BUILTIN_SKINS, EINK_PALETTE, Palette


class ThemeService:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "appearance.json"
        self.current = AppearanceSettings()

    def load(self) -> AppearanceSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.current = AppearanceSettings.from_dict(raw, set(BUILTIN_SKINS))
        except FileNotFoundError:
            self.current = AppearanceSettings()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._backup_invalid_file()
            self.current = AppearanceSettings()
        return copy.deepcopy(self.current)

    def save(self, settings: AppearanceSettings) -> None:
        validated = AppearanceSettings.from_dict(
            settings.to_dict(), set(BUILTIN_SKINS))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(validated.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        temp.replace(self.path)
        self.current = copy.deepcopy(validated)

    def apply(self, page: ft.Page, settings: AppearanceSettings | None = None) -> None:
        settings = settings or self.current
        skin = BUILTIN_SKINS.get(settings.skin_id, BUILTIN_SKINS["classic_blue"])
        if settings.mode == "eink":
            theme = self._make_theme(EINK_PALETTE, eink=True)
            page.theme = theme
            page.dark_theme = theme
            page.theme_mode = ft.ThemeMode.LIGHT
            return
        page.theme = self._make_theme(skin.light)
        page.dark_theme = self._make_theme(skin.dark)
        page.theme_mode = {
            "light": ft.ThemeMode.LIGHT,
            "dark": ft.ThemeMode.DARK,
        }.get(settings.mode, ft.ThemeMode.SYSTEM)

    @staticmethod
    def clone(settings: AppearanceSettings) -> AppearanceSettings:
        return copy.deepcopy(settings)

    @staticmethod
    def _make_theme(palette: Palette, eink: bool = False) -> ft.Theme:
        scheme = ft.ColorScheme(
            primary=palette.primary, on_primary=palette.on_primary,
            primary_container=palette.primary_container,
            on_primary_container=palette.on_primary_container,
            surface=palette.surface, surface_container=palette.surface_container,
            on_surface=palette.on_surface,
            on_surface_variant=palette.on_surface_variant,
            outline=palette.outline,
            error="#BA1A1A" if not eink else "#000000",
            on_error="#FFFFFF",
        )
        return ft.Theme(
            color_scheme=scheme, use_material3=True,
            scaffold_bgcolor=palette.surface,
            card_bgcolor=palette.surface_container,
            divider_color=palette.outline,
            visual_density=ft.VisualDensity.COMPACT if eink else None,
        )

    def _backup_invalid_file(self) -> None:
        if not self.path.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"appearance.invalid-{stamp}.json")
        try:
            self.path.replace(backup)
        except OSError:
            pass
