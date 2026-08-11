"""Expandable application settings hierarchy."""
from __future__ import annotations

import flet as ft

from app.views.appearance_view import AppearanceView


class SettingsView(ft.Column):
    """Top-level settings page; appearance is the first extensible section."""

    def __init__(self, theme_service, on_preview, on_saved):
        super().__init__(expand=True, spacing=0)
        self.appearance_view = AppearanceView(
            theme_service, on_preview=on_preview, on_saved=on_saved)
        tab_bar = ft.TabBar(tabs=[
            ft.Tab(label="外观", icon=ft.Icons.PALETTE_OUTLINED),
        ])
        tab_view = ft.TabBarView(
            expand=True, controls=[self.appearance_view])
        self.tabs = ft.Tabs(
            length=1, selected_index=0, expand=True,
            content=ft.Column(expand=True, spacing=0, controls=[tab_bar, tab_view]))
        self.controls = [
            ft.Container(
                padding=ft.Padding(24, 18, 24, 8),
                content=ft.Column(spacing=2, controls=[
                    ft.Text("配置", size=28, weight=ft.FontWeight.BOLD),
                    ft.Text("管理应用外观与后续扩展设置",
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ])),
            self.tabs,
        ]

    def sync_from_saved(self):
        self.appearance_view.sync_from_saved()

    def cancel_preview(self):
        self.appearance_view.cancel_preview()
