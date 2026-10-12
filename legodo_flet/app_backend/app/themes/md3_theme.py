"""Material Design 3 ColorScheme definitions for light/dark modes.

Design intent:
- Match Dart version's MD3 color tokens (lib/app/shell.dart + theme layer)
- Use Flet's native ft.ColorScheme for both light and dark themes
- Dynamic color support via seed colors (Flet 0.86+ capability)
"""
from __future__ import annotations

import flet as ft


# --------------------------------------------------------------------------- #
# Seed colors — derived from Legado brand palette
# --------------------------------------------------------------------------- #
_PRIMARY_SEED = "#6750A4"       # M3 baseline purple
_SECONDARY_SEED = "#625B71"     # M3 muted secondary
_TERTIARY_SEED = "#7D5260"      # M3 accent tertiary


def _make_light_scheme() -> ft.ColorScheme:
    """Light mode ColorScheme following MD3 spec (Flet 0.86 compatible)."""
    return ft.ColorScheme(
        primary="#6750A4",
        on_primary="#FFFFFF",
        primary_container="#EADDFF",
        on_primary_container="#21005D",
        secondary="#625B71",
        on_secondary="#FFFFFF",
        secondary_container="#E8DEF8",
        on_secondary_container="#1D192B",
        tertiary="#7D5260",
        on_tertiary="#FFFFFF",
        tertiary_container="#FFD8E4",
        on_tertiary_container="#31111D",
        error="#B3261E",
        on_error="#FFFFFF",
        error_container="#F9DEDC",
        on_error_container="#410E0B",
        surface="#FEF7FF",
        on_surface="#1C1B1F",
        surface_container_low="#E7E0EC",
        on_surface_variant="#49454F",
        outline="#79747E",
        outline_variant="#CAC4D0",
        shadow="#000000",
        scrim="#000000",
        inverse_surface="#313033",
        on_inverse_surface="#F4EFF4",
        inverse_primary="#D0BCFF",
    )


def _make_dark_scheme() -> ft.ColorScheme:
    """Dark mode ColorScheme following MD3 spec (Flet 0.86 compatible)."""
    return ft.ColorScheme(
        primary="#D0BCFF",
        on_primary="#381E72",
        primary_container="#4F378B",
        on_primary_container="#EADDFF",
        secondary="#CCC2DC",
        on_secondary="#332D41",
        secondary_container="#4A4458",
        on_secondary_container="#E8DEF8",
        tertiary="#EFB8C8",
        on_tertiary="#492532",
        tertiary_container="#633B48",
        on_tertiary_container="#FFD8E4",
        error="#F2B8B5",
        on_error="#601410",
        error_container="#8C1D18",
        on_error_container="#F9DEDC",
        surface="#1C1B1F",
        on_surface="#E6E1E5",
        surface_container_low="#49454F",
        on_surface_variant="#CAC4D0",
        outline="#938F99",
        outline_variant="#49454F",
        shadow="#000000",
        scrim="#000000",
        inverse_surface="#E6E1E5",
        on_inverse_surface="#313033",
        inverse_primary="#6750A4",
    )


def build_light_theme() -> ft.Theme:
    """Complete light theme with MD3 ColorScheme + typography defaults."""
    scheme = _make_light_scheme()
    return ft.Theme(
        color_scheme=scheme,
        font_family="Segoe UI Variable, Segoe UI, Microsoft YaHei, sans-serif",
        visual_density=ft.VisualDensity.COMFORTABLE,
    )


def build_dark_theme() -> ft.Theme:
    """Complete dark theme with MD3 ColorScheme + typography defaults."""
    scheme = _make_dark_scheme()
    return ft.Theme(
        color_scheme=scheme,
        font_family="Segoe UI Variable, Segoe UI, Microsoft YaHei, sans-serif",
        visual_density=ft.VisualDensity.COMFORTABLE,
    )


def apply_to_page(page, theme_mode: str = "system") -> None:
    """Apply MD3 themes to a Flet page.

    Parameters
    ----------
    page : ft.Page
        Target page instance
    theme_mode : str
        "light", "dark", or "system" (auto-detect OS preference)
    """
    page.theme = build_light_theme()
    page.dark_theme = build_dark_theme()
    if theme_mode == "system":
        page.theme_mode = ft.ThemeMode.SYSTEM
    elif theme_mode == "dark":
        page.theme_mode = ft.ThemeMode.DARK
    else:
        page.theme_mode = ft.ThemeMode.LIGHT
