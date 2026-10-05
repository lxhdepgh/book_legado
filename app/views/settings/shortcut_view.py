"""ShortcutView：「配置 → 快捷键」页（按一下就能录）。

形状照旧单体 ``settings_view.py`` 的快捷键页签：一张动作表，每行
"动作名 + 说明 + 当前键位 + 录键/清除"，而不是让用户去猜键名怎么写。
本视图只做布局与渲染：读偏好、写偏好、冲突判定全在
:class:`~app.controllers.settings.shortcut_controller.ShortcutController`
（视图零 import 服务，红线 §7.5）。

录键走 :attr:`on_key` 回调槽——导航外壳把 ``page.on_keyboard_event`` 分发到
当前视图的 ``on_key``（``app/views/navigation.py::_NavigationHost``），所以这个页
在本页签被选中时天然收得到按键，不需要自己抢那条全局钩子。

**Esc 这里不当"取消录制"**：阅读器默认把 Esc 绑在"退出阅读"上，录制中把 Esc
吃掉就再也录不回它了（取消另有按钮）。
"""
from __future__ import annotations

import flet as ft

from app.core.shortcuts import (BOSS_ACTION, READER_ACTIONS, format_binding)

#: 每行按 action id 存一份控件，重画时只改文字不重建（重建会把焦点/滚动丢掉）。
_ROW_WIDTH = 120.0


class ShortcutView(ft.Column):
    """快捷键：阅读动作一段 + 全局动作（老板键）一段。"""

    def __init__(self) -> None:
        super().__init__(expand=True, scroll=ft.ScrollMode.AUTO, spacing=0)

        # -- 回调槽（Controller 绑定） --
        self.on_key = None                 # (event) -> None
        self.on_start_record = None        # (action_id: str) -> None
        self.on_cancel_record = None       # () -> None
        self.on_clear = None               # (action_id: str) -> None
        self.on_change_boss_enabled = None  # (bool) -> None
        self.on_save = None                 # () -> None
        self.on_reset = None               # () -> None

        self._status = ft.Text("", size=12, color=ft.Colors.GREEN_400)
        self._recording_hint = ft.Text("", size=12, color=ft.Colors.AMBER_400)
        self._boss_hint = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self._boss_switch = ft.Switch(
            label="启用老板键", value=True,
            on_change=lambda e: self._fire(
                self.on_change_boss_enabled, bool(e.control.value)))
        self._rows: dict[str, dict[str, ft.Control]] = {}

        reader_table = ft.Column(
            [self._row(action) for action in READER_ACTIONS], spacing=4)
        boss_table = ft.Column(
            [self._row(BOSS_ACTION), self._boss_hint], spacing=4)

        self.controls = [
            ft.Container(padding=24, content=ft.Column([
                ft.Text("快捷键", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("点「录键」后按下想要的组合键就能录；改完点右下角"
                        "「保存并应用」才生效。"),
                self._recording_hint,
                ft.Divider(),
                ft.Text("阅读", size=18, weight=ft.FontWeight.BOLD),
                reader_table,
                ft.Divider(),
                ft.Text("全局", size=18, weight=ft.FontWeight.BOLD),
                self._boss_switch,
                boss_table,
                ft.Divider(),
                self._status,
                ft.Row(alignment=ft.MainAxisAlignment.END, controls=[
                    ft.TextButton("恢复默认", on_click=lambda e: self._fire(
                        self.on_reset)),
                    ft.FilledButton("保存并应用", icon=ft.Icons.SAVE,
                                    on_click=lambda e: self._fire(self.on_save)),
                ]),
            ], spacing=10)),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _row(self, action) -> ft.Container:
        """一行：动作名 / 键位 / 冲突提示 / 录键 + 清除。"""
        name = ft.Text(action.label, width=_ROW_WIDTH, size=14,
                       weight=ft.FontWeight.W_500)
        binding = ft.Text("未设置", width=160, size=14,
                          color=ft.Colors.ON_SURFACE_VARIANT,
                          selectable=True)
        conflict = ft.Text("", size=11, color=ft.Colors.RED_300)
        record = ft.OutlinedButton(
            "录键", icon=ft.Icons.MIC,
            on_click=lambda e, aid=action.id: self._fire(
                self.on_start_record, aid))
        clear = ft.IconButton(
            ft.Icons.CLEAR, tooltip=f"清空「{action.label}」的键位",
            on_click=lambda e, aid=action.id: self._fire(self.on_clear, aid))
        self._rows[action.id] = {
            "binding": binding, "conflict": conflict, "record": record,
            "clear": clear, "label": action.label}
        return ft.Container(
            padding=ft.Padding(0, 2, 0, 2),
            content=ft.Column([
                ft.Row([name, binding, record, clear], spacing=8, wrap=True),
                conflict,
            ], spacing=2))

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_bindings(self, bindings: dict[str, str],
                     conflicted: dict[str, list[str]],
                     recording: str = "") -> None:
        """回填每行的键位文字与冲突说明；``recording`` 那一行改成"请按键…"。"""
        labels = {a.id: a.label for a in (*READER_ACTIONS, BOSS_ACTION)}
        for action_id, controls in self._rows.items():
            binding = str(bindings.get(action_id, ""))
            controls["binding"].value = format_binding(binding)
            peers = conflicted.get(action_id) or []
            controls["conflict"].value = (
                "与" + "、".join(f"「{labels.get(p, p)}」" for p in peers)
                + "用了同一个键位，按下去只有排前面的会动" if peers else "")
            recording_here = bool(recording) and recording == action_id
            controls["record"].text = "请按键…" if recording_here else "录键"
            controls["record"].icon = (
                ft.Icons.CANCEL if recording_here else ft.Icons.MIC)
            controls["clear"].disabled = not binding

    def set_recording(self, action_id: str) -> None:
        """录制中的那一句提示（顶部一行，跟着按键那拍由控制器刷下来）。"""
        if not action_id:
            self._recording_hint.value = ""
            return
        labels = {a.id: a.label for a in (*READER_ACTIONS, BOSS_ACTION)}
        self._recording_hint.value = (
            f"正在录「{labels.get(action_id, action_id)}」："
            "现在按下的组合键就是它的新键位（取消请按右边的按钮）")

    def set_boss_enabled(self, enabled: bool) -> None:
        self._boss_switch.value = bool(enabled)
        # 开关任何时候都得能点：把 disabled 写在这里，一旦关掉就再也开不回来。
        self._boss_switch.disabled = False

    def set_boss_status(self, text: str, unavailable: bool = False) -> None:
        """可用性一句话（非 Windows / 缺 pywin32 时这里写清"为什么按了没反应"）。"""
        self._boss_hint.value = str(text or "")
        self._boss_hint.color = (ft.Colors.RED_300 if unavailable
                                 else ft.Colors.ON_SURFACE_VARIANT)

    def set_status(self, text: str, error: bool = False) -> None:
        self._status.value = str(text or "")
        self._status.color = ft.Colors.RED_300 if error else ft.Colors.GREEN_400
