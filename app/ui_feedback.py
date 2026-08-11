"""Small Flet-version compatibility helpers for transient UI feedback."""
from __future__ import annotations

import flet as ft


def show_snack_bar(page: ft.Page, snack_bar: ft.SnackBar) -> None:
    """Display a SnackBar using Flet 0.86's dialog lifecycle API."""
    page.show_dialog(snack_bar)
