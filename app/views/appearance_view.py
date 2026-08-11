"""User-facing appearance preferences and live preview."""
from __future__ import annotations

import flet as ft

from app.models.appearance_settings import AppearanceSettings
from app.services.theme_service import ThemeService
from app.themes.builtin_themes import BUILTIN_SKINS


class AppearanceView(ft.Column):
    def __init__(self, theme_service: ThemeService, on_preview, on_saved):
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO, spacing=0)
        self.theme_service = theme_service
        self.on_preview = on_preview
        self.on_saved = on_saved
        self.pending = ThemeService.clone(theme_service.current)

        self._mode = ft.RadioGroup(
            value=self.pending.mode, on_change=self._change_mode,
            content=ft.Row(wrap=True, controls=[
                ft.Radio(value="system", label="跟随系统"),
                ft.Radio(value="light", label="浅色"),
                ft.Radio(value="dark", label="深色"),
                ft.Radio(value="eink", label="墨水屏"),
            ]))
        self._skin = ft.Dropdown(
            value=self.pending.skin_id, expand=True, on_select=self._change_skin,
            options=[ft.DropdownOption(key=item.id, text=item.name)
                     for item in BUILTIN_SKINS.values()])
        self._description = ft.Text()
        self._preview = ft.Container(border_radius=16, padding=20, content=ft.Column([
            ft.Text("主题预览", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("正文、卡片和操作控件会随选择即时变化。"),
            ft.Row(wrap=True, controls=[
                ft.Button("主要操作", icon=ft.Icons.CHECK),
                ft.OutlinedButton("次要操作"),
                ft.Chip(label=ft.Text("状态标签")),
            ]),
            ft.TextField(label="输入框预览", value="搜索书名或作者"),
        ]))
        self.controls = [
            ft.Container(padding=24, content=ft.Column([
                ft.Text("外观设置", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("应用皮肤与阅读主题相互独立；本阶段先开放应用皮肤。"),
                ft.Divider(),
                ft.Text("外观模式", size=18, weight=ft.FontWeight.BOLD),
                self._mode,
                ft.Text("应用皮肤", size=18, weight=ft.FontWeight.BOLD),
                ft.Row([self._skin]), self._description, self._preview,
                ft.Row(alignment=ft.MainAxisAlignment.END, controls=[
                    ft.TextButton("恢复默认", on_click=self._reset),
                    ft.OutlinedButton("取消预览", on_click=self._cancel),
                    ft.Button("保存并应用", icon=ft.Icons.SAVE, on_click=self._save),
                ]),
            ])),
        ]
        self._refresh_description()

    def sync_from_saved(self):
        self.pending = ThemeService.clone(self.theme_service.current)
        self._mode.value = self.pending.mode
        self._skin.value = self.pending.skin_id
        self._refresh_description()

    def cancel_preview(self):
        self.sync_from_saved()
        self.on_preview(self.theme_service.current)

    def _change_mode(self, event):
        mode = event.control.value
        if mode == "eink" and self.pending.mode != "eink":
            self.pending.previous_mode = self.pending.mode
        self.pending.mode = mode
        self._skin.disabled = mode == "eink"
        self.on_preview(self.pending)
        self.update()

    def _change_skin(self, event):
        self.pending.skin_id = event.control.value
        self._refresh_description()
        self.on_preview(self.pending)
        self.update()

    def _refresh_description(self):
        skin = BUILTIN_SKINS[self.pending.skin_id]
        self._description.value = skin.description
        self._skin.disabled = self.pending.mode == "eink"

    def _save(self, _):
        self.theme_service.save(self.pending)
        self.on_saved("外观设置已保存")

    def _cancel(self, _):
        self.cancel_preview()
        self.on_saved("已恢复保存前的外观")
        self.update()

    def _reset(self, _):
        self.pending = AppearanceSettings()
        self._mode.value = self.pending.mode
        self._skin.value = self.pending.skin_id
        self._refresh_description()
        self.on_preview(self.pending)
        self.update()
