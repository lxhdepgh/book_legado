"""AppearanceView：外观设置页（M7 后补全，替代导航里的占位页）。

模式单选（跟随系统/浅色/深色）+ 墨水屏开关 + 五个命名皮肤 + 全应用背景图 + 实时预览。
本视图只做 Flet 布局与渲染：偏好读写、主题求值全部经
:class:`~app.controllers.settings.appearance_controller.AppearanceController`
（视图零 import app.services，红线 §7.5）。

实时预览：Controller 把 :class:`~app.services.tools.theme_service.ThemeData`
（纯颜色数据）交给本视图，由视图侧构造 ``ft.Theme`` 应用到整页。

**主题必须三件套一起下发**（``apply_page_theme``）：``page.theme`` 只在**亮面**
主题模式下生效，``page.dark_theme`` 管暗面，``page.theme_mode`` 决定用哪一面
（``SYSTEM`` 时跟操作系统）。只设 ``page.theme`` 而把 ``theme_mode`` 留在默认的
``SYSTEM``，在深色桌面上等于什么都没改——表现就是"换皮肤/切深色毫无反应，界面
一片默认灰"。本模块零 import app.services（红线 §7.5）。
"""
from __future__ import annotations

from pathlib import Path

import flet as ft

from app.themes.builtin_themes import BUILTIN_SKINS

#: 外观模式 → Flet 主题模式（"system" 及未知值走跟随系统）。
_THEME_MODES = {
    "light": ft.ThemeMode.LIGHT,
    "dark": ft.ThemeMode.DARK,
}


def build_theme(palette: dict, eink: bool = False) -> ft.Theme:
    """颜色角色字典 → ``ft.Theme``（服务给数据，视图负责落成 Flet 控件属性）。"""
    scheme = ft.ColorScheme(
        primary=palette.get("primary"),
        on_primary=palette.get("on_primary"),
        primary_container=palette.get("primary_container"),
        on_primary_container=palette.get("on_primary_container"),
        surface=palette.get("surface"),
        surface_container=palette.get("surface_container"),
        on_surface=palette.get("on_surface"),
        on_surface_variant=palette.get("on_surface_variant"),
        outline=palette.get("outline"),
        error="#000000" if eink else "#BA1A1A",
        on_error="#FFFFFF",
    )
    return ft.Theme(
        color_scheme=scheme, use_material3=True,
        # 这三项决定"整页底色/卡片/分隔线"。少了 scaffold_bgcolor 时窗口底色由
        # Flet 默认主题给，换皮肤只改到按钮，看不出"皮肤换了"。
        scaffold_bgcolor=palette.get("surface"),
        card_bgcolor=palette.get("surface_container"),
        divider_color=palette.get("outline"),
        visual_density=ft.VisualDensity.COMPACT if eink else None,
    )


def apply_page_theme(page, data) -> None:
    """把 :class:`ThemeData` 应用到页面三件套（``page`` 为 None 时什么都不做）。

    墨水屏不分亮暗：两面都是同一套纯黑白，且强制 ``LIGHT``（否则暗面桌面上
    深色底 + 纯黑字会糊成一片）。
    """
    if page is None or data is None:
        return
    if getattr(data, "eink", False):
        theme = build_theme(data.light, eink=True)
        page.theme = theme
        page.dark_theme = theme
        page.theme_mode = ft.ThemeMode.LIGHT
    else:
        page.theme = build_theme(data.light)
        page.dark_theme = build_theme(data.dark)
        page.theme_mode = _THEME_MODES.get(getattr(data, "mode", "system"),
                                           ft.ThemeMode.SYSTEM)
    page.update()


class AppearanceView(ft.Column):
    """外观：模式 / 墨水屏 / 皮肤 / 背景图 / 预览 / 保存。"""

    def __init__(self) -> None:
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO, spacing=0)

        # -- 回调槽（Controller 绑定） --
        self.on_change_mode = None    # (mode: str) -> None
        self.on_change_skin = None    # (skin_id: str) -> None
        self.on_change_eink = None    # (eink: bool) -> None
        self.on_save = None           # () -> None
        self.on_reset = None          # () -> None
        self.on_pick_background = None    # () -> None（选一张图铺到整个应用后面）
        self.on_clear_background = None   # () -> None

        # 图片选择器：视图只持有控件，"选完之后拷去哪儿"是控制器与服务的事。
        # 名字必须是 image_picker——导航外壳装配视图时按这个名字自动 append 到
        # page.services（Flet 0.86 的 Service 通道），离开本页再摘掉。
        self.image_picker = ft.FilePicker()

        self._mode = ft.RadioGroup(
            value="system",
            on_change=lambda e: self._fire(
                self.on_change_mode, e.control.value),
            content=ft.Row(wrap=True, controls=[
                ft.Radio(value="system", label="跟随系统"),
                ft.Radio(value="light", label="浅色"),
                ft.Radio(value="dark", label="深色"),
            ]))
        self._eink = ft.Switch(
            label="墨水屏（纯黑白高对比）",
            on_change=lambda e: self._fire(
                self.on_change_eink, bool(e.control.value)))
        self._skin = ft.Dropdown(
            value="classic_blue", expand=True,
            on_select=lambda e: self._fire(
                self.on_change_skin, e.control.value),
            options=[ft.DropdownOption(key=item.id, text=item.name)
                     for item in BUILTIN_SKINS.values()])
        self._description = ft.Text("", size=12,
                                    color=ft.Colors.ON_SURFACE_VARIANT)
        # 背景图那一行：缩略 + 文件名 + 两颗按钮。**没有"透明度/背景模式"控件**——
        # 图与主题底色的合成比例定死在外壳里（navigation._BACKGROUND_OPACITY），
        # 因为一放开就得知道此刻系统是亮面还是暗面，而 theme_mode 可以是 SYSTEM。
        self._background_thumb = ft.Image(
            src="", width=132, height=76, fit=ft.BoxFit.COVER,
            border_radius=6, visible=False,
            error_content=ft.Container(
                width=132, height=76, border_radius=6,
                content=ft.Text("图读不出来", size=11,
                                 color=ft.Colors.ON_SURFACE_VARIANT)))
        self._background_label = ft.Text("当前没有背景图", size=12,
                                         color=ft.Colors.ON_SURFACE_VARIANT)
        self._clear_background = ft.TextButton(
            "去掉背景", visible=False,
            on_click=lambda e: self._fire(self.on_clear_background))
        self._status = ft.Text("", size=12, color=ft.Colors.GREEN_400)
        # 实时预览：一小组**真实控件**，配色全部来自 page.theme（换了主题这里就
        # 跟着变，等于把整页缩到一个卡片里看）。以前是三块手写 bgcolor 的色板，
        # 而且用 ft.Container(expand=True) 塞进 wrap Row —— wrap 与 expand 互斥，
        # 整行渲染成一片灰（项目已知 Flet 陷阱）。
        self._preview = ft.Container(
            padding=16, border_radius=12,
            content=ft.Column([
                ft.Text("主题预览", size=18, weight=ft.FontWeight.BOLD),
                ft.Text("正文、按钮、输入框与分隔线随选择即时变化。",
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row(wrap=True, controls=[
                    ft.FilledButton("主要操作", icon=ft.Icons.CHECK),
                    ft.OutlinedButton("次要操作"),
                    ft.Chip(label=ft.Text("状态标签")),
                ]),
                ft.TextField(label="输入框预览", value="搜索书名或作者"),
                ft.Divider(),
            ], spacing=12))

        self.controls = [
            ft.Container(padding=24, content=ft.Column([
                ft.Text("外观设置", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("应用皮肤与阅读主题相互独立；主题应用到整个应用窗口。"),
                ft.Divider(),
                ft.Text("外观模式", size=18, weight=ft.FontWeight.BOLD),
                self._mode,
                self._eink,
                ft.Text("应用皮肤", size=18, weight=ft.FontWeight.BOLD),
                self._skin,
                self._description,
                ft.Divider(),
                ft.Text("应用背景图", size=18, weight=ft.FontWeight.BOLD),
                ft.Text("选一张图垫在整个应用窗口后面，只淡到认得出来、不压正文；"
                        "选中的图会存进应用数据目录，原图挪走也不影响。"),
                ft.Row(spacing=12, controls=[
                    self._background_thumb,
                    ft.Column([
                        self._background_label,
                        ft.Row([
                            ft.OutlinedButton("选择图片", icon=ft.Icons.IMAGE,
                                              on_click=lambda e: self._fire(
                                                  self.on_pick_background)),
                            self._clear_background,
                        ], spacing=8),
                    ]),
                ]),
                ft.Divider(),
                self._preview,
                self._status,
                ft.Row(alignment=ft.MainAxisAlignment.END, controls=[
                    ft.TextButton("恢复默认", on_click=lambda e: self._fire(
                        self.on_reset)),
                    ft.FilledButton("保存并应用", icon=ft.Icons.SAVE,
                                    on_click=lambda e: self._fire(self.on_save)),
                ]),
            ])),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（AppearanceController 调用）
    # ------------------------------------------------------------------ #
    def set_preferences(self, skin_id: str, mode: str, eink: bool) -> None:
        """把当前偏好回填到控件（含恢复默认/重新载入）。"""
        if mode in {"system", "light", "dark"}:
            self._mode.value = mode
        self._eink.value = bool(eink)
        # 开关必须始终可点：曾把 disabled=bool(eink) 写在这里，墨水屏一开就
        # 再也关不掉（只能连其它自定义一起"恢复默认"）。
        self._eink.disabled = False
        if skin_id in BUILTIN_SKINS:
            self._skin.value = skin_id
        self._refresh_description(skin_id)

    def _refresh_description(self, skin_id: str) -> None:
        skin = BUILTIN_SKINS.get(skin_id)
        if skin is not None:
            self._description.value = skin.description

    def apply_preview(self, data) -> None:
        """把 ThemeData 应用到整页（预览即所见，不持久化）。

        ``data`` 鸭子类型：``mode / eink / light / dark``，light/dark 为颜色角色
        → hex 字典。视图未挂载（无头测试）时 ``self.page`` 抛 RuntimeError，
        没有可着色的窗口，直接返回。
        """
        try:
            page = self.page
        except Exception:
            return
        apply_page_theme(page, data)

    def set_status(self, text: str, error: bool = False) -> None:
        self._status.value = str(text or "")
        self._status.color = ft.Colors.RED_300 if error else ft.Colors.GREEN_400

    def set_background(self, path: str) -> None:
        """回填当前背景图：缩略图 + 文件名；空串表示"没有"。

        只认文件名不认整条路径：用户要确认的是"换成了我选的那张"，一长串
        ``C:\\Users\\...`` 既占地方又读不出信息。
        """
        shown = str(path or "")
        if shown:
            self._background_thumb.src = shown
            self._background_label.value = f"当前背景：{Path(shown).name}"
        else:
            self._background_label.value = "当前没有背景图"
        self._background_thumb.visible = bool(shown)
        self._clear_background.visible = bool(shown)
