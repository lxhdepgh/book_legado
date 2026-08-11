"""Expandable application settings hierarchy."""
from __future__ import annotations

import flet as ft

from app.views.appearance_view import AppearanceView


class SettingsView(ft.Column):
    """Top-level settings page with extensible appearance/reader sections."""

    def __init__(self, theme_service, on_preview, on_saved, reader_view=None):
        super().__init__(expand=True, spacing=0)
        self.appearance_view = AppearanceView(
            theme_service, on_preview=on_preview, on_saved=on_saved)
        self.reader_layout_view = (
            ReaderLayoutSettingsView(reader_view) if reader_view else None)
        tabs = [ft.Tab(label="外观", icon=ft.Icons.PALETTE_OUTLINED)]
        views = [self.appearance_view]
        if self.reader_layout_view:
            tabs.append(ft.Tab(label="阅读", icon=ft.Icons.TEXT_FIELDS))
            views.append(self.reader_layout_view)
        self.shortcut_help_view = ShortcutHelpView(reader_view)
        tabs.append(ft.Tab(label="快捷键", icon=ft.Icons.KEYBOARD_OUTLINED))
        views.append(self.shortcut_help_view)
        tab_bar = ft.TabBar(tabs=tabs)
        tab_view = ft.TabBarView(expand=True, controls=views)
        self.tabs = ft.Tabs(
            length=len(tabs), selected_index=0, expand=True,
            content=ft.Column(
                expand=True, spacing=0, controls=[tab_bar, tab_view]))
        self.controls = [
            ft.Container(
                padding=ft.Padding(24, 18, 24, 8),
                content=ft.Column(spacing=2, controls=[
                    ft.Text("配置", size=28, weight=ft.FontWeight.BOLD),
                    ft.Text("管理应用外观、阅读排版与后续扩展设置",
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ])),
            self.tabs,
        ]

    def sync_from_saved(self):
        self.appearance_view.sync_from_saved()

    def cancel_preview(self):
        self.appearance_view.cancel_preview()


class ReaderLayoutSettingsView(ft.ListView):
    """Persistent reader typography settings exposed from configuration."""

    def __init__(self, reader_view):
        super().__init__(expand=True, padding=24, spacing=14)
        self.reader_view = reader_view
        preferences = reader_view._reader_preferences

        def slider(title, key, minimum, maximum, divisions):
            return ft.Column([
                ft.Text(title, weight=ft.FontWeight.BOLD),
                ft.Slider(
                    value=float(preferences[key]), min=minimum, max=maximum,
                    divisions=divisions, label="{value}",
                    on_change=lambda e, name=key: reader_view._set_reader_preference(
                        name, float(e.control.value))),
            ], spacing=2)

        self.controls = [
            ft.Text("阅读排版", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("这些设置会即时应用到当前书籍，并在下次启动时恢复。",
                    color=ft.Colors.ON_SURFACE_VARIANT),
            slider("字号", "font_size", 13, 28, 15),
            slider("行距", "line_height", 1.3, 2.2, 9),
            slider("段距", "paragraph_spacing", 4, 24, 10),
            ft.Dropdown(
                label="正文宽度", value=str(int(preferences["max_width"])),
                options=[ft.dropdown.Option("620", "窄"),
                         ft.dropdown.Option("700", "标准"),
                         ft.dropdown.Option("760", "宽")],
                on_select=lambda e: reader_view._set_reader_preference(
                    "max_width", float(e.control.value))),
            ft.Dropdown(
                label="文本对齐", value=preferences["text_align"],
                options=[ft.dropdown.Option("start", "左对齐"),
                         ft.dropdown.Option("justify", "两端对齐")],
                on_select=lambda e: reader_view._set_reader_preference(
                    "text_align", e.control.value)),
            ft.Dropdown(
                label="阅读背景", value=preferences["background"],
                options=[ft.dropdown.Option("follow", "跟随主题"),
                         ft.dropdown.Option("paper", "纸张"),
                         ft.dropdown.Option("eye", "护眼"),
                         ft.dropdown.Option("night", "夜间"),
                         ft.dropdown.Option("eink", "墨水屏")],
                on_select=lambda e: reader_view._set_reader_preference(
                    "background", e.control.value)),
            ft.Switch(
                label="段首缩进", value=bool(preferences["indent"]),
                on_change=lambda e: reader_view._set_reader_preference(
                    "indent", bool(e.control.value))),
            ft.Switch(
                label="显示底部阅读状态", value=bool(preferences["show_status"]),
                on_change=lambda e: reader_view._set_reader_preference(
                    "show_status", bool(e.control.value))),
        ]


class ShortcutHelpView(ft.ListView):
    """Editable keyboard bindings and reader pointer reference."""

    def __init__(self, reader_view=None):
        super().__init__(expand=True, padding=24, spacing=10)
        self.reader_view = reader_view
        self.shortcut_fields = {}

        def shortcut(keys, action):
            return ft.ListTile(
                dense=True,
                leading=ft.Container(
                    width=112, padding=ft.Padding(8, 4, 8, 4),
                    border_radius=6, bgcolor=ft.Colors.SURFACE_CONTAINER,
                    content=ft.Text(keys, text_align=ft.TextAlign.CENTER,
                                    weight=ft.FontWeight.W_600)),
                title=ft.Text(action))

        def binding_field(action, label):
            bindings = [
                ("left", "←"), ("right", "→"),
                ("up", "↑"), ("down", "↓"),
                ("pageup", "PageUp"), ("pagedown", "PageDown"),
                ("space", "Space"), ("escape", "Esc"),
                ("f", "F"), ("comma", ","),
                ("home", "Home"), ("end", "End"),
                ("j", "J"), ("k", "K"),
            ]
            field = ft.Dropdown(
                label=label,
                value=reader_view._reader_preferences["shortcuts"][action],
                options=[ft.dropdown.Option(value, text)
                         for value, text in bindings],
                on_select=lambda e, name=action: self._change_binding(
                    name, e.control.value),
            )
            self.shortcut_fields[action] = field
            return field

        editable = []
        if reader_view is not None:
            editable = [
                ft.Text("自定义按键", size=16, weight=ft.FontWeight.BOLD),
                ft.Text("选择重复按键时，两个功能会自动交换按键。",
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.ResponsiveRow([
                    binding_field("previous", "上一章"),
                    binding_field("next", "下一章"),
                    binding_field("page_up", "向上翻页"),
                    binding_field("page_down", "向下翻页"),
                    binding_field("directory", "打开目录"),
                    binding_field("settings", "打开配置"),
                    binding_field("close", "关闭 / 返回"),
                ], columns=12, spacing=8, run_spacing=8),
                ft.Divider(),
            ]
            for field in self.shortcut_fields.values():
                field.col = {"xs": 12, "sm": 6, "md": 4}

        self.controls = [
            ft.Text("键盘与阅读操作", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("快捷键仅在正文阅读页生效；章节搜索输入框获得焦点后，普通文字输入不会触发阅读操作。",
                    color=ft.Colors.ON_SURFACE_VARIANT),
            *editable,
            ft.Text("键盘", size=16, weight=ft.FontWeight.BOLD),
            shortcut("← / →", "上一章 / 下一章"),
            shortcut("PageUp", "向上滚动一屏"),
            shortcut("PageDown", "向下滚动一屏"),
            shortcut("Space", "向下滚动一屏"),
            shortcut("Esc", "关闭目录或设置；没有面板时返回书籍详情"),
            shortcut("F", "打开目录并聚焦章节搜索"),
            shortcut(",", "打开应用配置"),
            ft.Divider(),
            ft.Text("鼠标", size=16, weight=ft.FontWeight.BOLD),
            shortcut("滚轮", "正常上下滚动正文"),
            shortcut("单击正文", "显示或隐藏阅读工具栏"),
            ft.Divider(),
            ft.Text("触摸屏", size=16, weight=ft.FontWeight.BOLD),
            shortcut("点击中央", "显示或隐藏阅读工具栏"),
            shortcut("点击左 / 右", "上一章 / 下一章"),
            shortcut("上下滑动", "滚动正文"),
        ]

    def _change_binding(self, action, binding):
        if self.reader_view is None:
            return
        self.reader_view._set_reader_shortcut(action, binding)
        shortcuts = self.reader_view._reader_preferences["shortcuts"]
        for name, field in self.shortcut_fields.items():
            field.value = shortcuts[name]
        self.update()
