"""ReplaceRuleView：全局替换规则管理（M5，§1.7 / 契约 §6 / §13）。

规则列表 + 新增/编辑/删除 + 启停开关；正文叠加由 ReplaceRuleService 在阅读链路
完成，本视图只管规则 CRUD 的渲染与交互。**零 app.services.* import**。
"""
from __future__ import annotations

import flet as ft


class ReplaceRuleView(ft.Column):
    """替换规则：规则表格行 + 增删启停。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_add = None            # () -> None
        self.on_edit = None           # (rule: dict) -> None
        self.on_delete = None         # (rule_id: str) -> None
        self.on_toggle = None         # (rule_id: str, enabled: bool) -> None
        self.on_global_toggle = None   # (enabled: bool) -> None

        self._title = ft.Text("替换规则", size=20, weight=ft.FontWeight.BOLD)
        self._add_btn = ft.FilledButton(
            "新建规则", icon=ft.Icons.ADD,
            on_click=lambda e: self._fire(self.on_add))
        self._summary = ft.Text("尚未加载规则", size=12, color=ft.Colors.GREY_600)
        # 全局叠加总开关：关掉只是不再叠加正文，规则行本身不动。
        self._global = ft.Switch(
            label="全局替换生效", value=True,
            on_change=lambda e: self._fire(self.on_global_toggle, e.control.value))
        self._rows = ft.ListView(expand=True, spacing=4)

        self.controls = [
            self._title,
            # 三个都不定宽、都不 flex：窄窗里让它们自己换到第二拍，
            # 而不是把"共 N 条…"挤出屏幕（第七十五轮 360 档量到的截断）。
            ft.Row([self._add_btn, self._global, self._summary],
                   spacing=12, run_spacing=4, wrap=True),
            ft.Container(expand=True, content=self._rows),
        ]

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_global_enabled(self, enabled: bool) -> None:
        """回填开关状态（不触发 on_change，避免和服务回写打转）。"""
        self._global.value = bool(enabled)

    def render_rules(self, rules: list[dict]) -> None:
        """渲染规则行；rule 含 id/name/pattern/replacement/enabled。"""
        rules = list(rules or [])
        self._rows.controls = []
        for rule in rules:
            rule_id = str(rule.get("id", ""))
            enabled = bool(rule.get("enabled", True))
            self._rows.controls.append(ft.ListTile(
                key=rule_id or None,
                leading=ft.Switch(
                    value=enabled,
                    on_change=lambda e, rid=rule_id: self._fire(self.on_toggle, rid, e.control.value)),
                title=ft.Text(str(rule.get("name", "(未命名)")),
                              weight=ft.FontWeight.W_500),
                subtitle=ft.Text(
                    f"{rule.get('pattern', '')} → {rule.get('replacement', '')}",
                    size=12, color=ft.Colors.GREY_700, max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS),
                trailing=ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE, tooltip="删除",
                    on_click=lambda e, rid=rule_id: self._fire(self.on_delete, rid)),
                on_click=lambda e, r=rule: self._fire(self.on_edit, r),
            ))
        self._summary.value = f"共 {len(rules)} 条规则"

    def set_status(self, text: str) -> None:
        self._summary.value = text or ""
