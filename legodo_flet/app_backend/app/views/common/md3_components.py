"""MD3 component factories for consistent styling across views.

Provides ready-to-use Card, Button, TextField, and other components with
Material Design 3 styling tokens applied.
"""
from __future__ import annotations

import flet as ft

from .md3_styles import (
    RADIUS_CARD,
    RADIUS_BUTTON,
    RADIUS_INPUT,
    ELEVATION_1,
    SPACE_SM,
    SPACE_MD,
    BUTTON_HEIGHT,
    INPUT_HEIGHT,
)


def make_card(content, padding: tuple = (16, 12), **kwargs) -> ft.Container:
    """Create an MD3-styled card container.

    Parameters
    ----------
    content : ft.Control
        Card content
    padding : tuple
        (horizontal, vertical) padding in pixels
    **kwargs
        Additional Container properties (e.g., on_click for interactive cards)

    Returns
    -------
    ft.Container
        Styled card with elevation and rounded corners
    """
    return ft.Container(
        content=content,
        padding=ft.Padding(*padding),
        border_radius=RADIUS_CARD,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        shadow=[{"offset": (0, 1), "blur_radius": 3, "spread_radius": 0,
                 "color": "#00000014"}],
        **kwargs,
    )


def make_filled_button(text: str, icon=None, **kwargs) -> ft.FilledButton:
    """Create an MD3 filled button (primary action).

    Parameters
    ----------
    text : str
        Button label
    icon : ft.Icon | None
        Optional leading icon
    **kwargs
        Additional FilledButton properties

    Returns
    -------
    ft.FilledButton
        Pill-shaped filled button with MD3 height
    """
    btn = ft.FilledButton(
        content=ft.Text(text),
        icon=icon,
        height=BUTTON_HEIGHT,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=RADIUS_BUTTON),
            padding=ft.Padding(SPACE_MD, 0, SPACE_MD, 0),
        ),
        **kwargs,
    )
    return btn


def make_tonal_button(text: str, icon=None, **kwargs) -> ft.FilledTonalButton:
    """Create an MD3 filled tonal button (secondary action).

    Same pill shape as filled but uses secondary color scheme.
    """
    btn = ft.FilledTonalButton(
        content=ft.Text(text),
        icon=icon,
        height=BUTTON_HEIGHT,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=RADIUS_BUTTON),
            padding=ft.Padding(SPACE_MD, 0, SPACE_MD, 0),
        ),
        **kwargs,
    )
    return btn


def make_outlined_button(text: str, icon=None, **kwargs) -> ft.OutlinedButton:
    """Create an MD3 outlined button (tertiary action)."""
    btn = ft.OutlinedButton(
        content=ft.Text(text),
        icon=icon,
        height=BUTTON_HEIGHT,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=RADIUS_BUTTON),
            side=ft.BorderSide(width=1, color=ft.Colors.OUTLINE),
            padding=ft.Padding(SPACE_MD, 0, SPACE_MD, 0),
        ),
        **kwargs,
    )
    return btn


def make_text_field(**kwargs) -> ft.TextField:
    """Create an MD3-styled text field with rounded corners.

    Parameters
    ----------
    **kwargs
        Standard TextField parameters (hint_text, prefix_icon, etc.)

    Returns
    -------
    ft.TextField
        Rounded input field with MD3 spacing
    """
    return ft.TextField(
        height=INPUT_HEIGHT,
        border_radius=RADIUS_INPUT,
        content_padding=ft.Padding(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM),
        **kwargs,
    )


def make_chip(label: str, selected: bool = False, **kwargs) -> ft.Chip:
    """Create an MD3 filter chip.

    Parameters
    ----------
    label : str
        Chip text
    selected : bool
        Initial selection state
    **kwargs
        Additional Chip properties (on_select, etc.)

    Returns
    -------
    ft.Chip
        Filter chip with MD3 radius
    """
    return ft.Chip(
        label=ft.Text(label),
        selected=selected,
        shape=ft.RoundedRectangleBorder(radius=RADIUS_CHIP),
        **kwargs,
    )
