"""DictPopup：词典页（M5，§1.8 / 契约 §7 / §13）。

本页是「更多工具 → 词典」这个独立目的地：手输查词 + 词典规则 CRUD。阅读器里的
划词取词**不走本页**——正文选中由 ``ReaderView.selection_bar`` 起，释义弹窗由
``ReaderView.build_definition_dialog`` 搭、``ReaderController`` 填，两条路共用同一个
``DictService.lookup``。本视图是轻量弹窗壳：展示待查词、释义正文与加载/错误态。
**零 app.services.* import**：查询动作经 Controller 派发，结果以文本注入
:meth:`set_definition`。
"""
from __future__ import annotations

import flet as ft


class DictPopup(ft.Column):
    """取词弹窗：词条 + 释义 + 关闭。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, tight=True)

        # -- 回调槽（Controller 填充） --
        self.on_lookup = None        # (word: str) -> None
        self.on_close = None         # () -> None
        self.on_add_rule = None      # () -> None
        self.on_edit_rule = None     # (rule: dict) -> None
        self.on_delete_rule = None   # (rule_id) -> None

        self._word = ft.Text("", size=18, weight=ft.FontWeight.BOLD)
        self._phonetic = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._definition = ft.Text("", size=14, selectable=True)
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_500)
        self._close_btn = ft.TextButton(
            "关闭", icon=ft.Icons.CLOSE,
            on_click=lambda e: self._fire(self.on_close))
        # 本页也是独立目的地：没有输入框就无从查起（划词入口之外必须可手输）。
        # expand 而不是定宽：那一行不换行，非 flex 子项先按无限宽排好，
        # 360 档下"查询"按钮整只被挤出屏幕（第七十六轮像素量到）。
        self._query = ft.TextField(
            label="查词", hint_text="输入单词或短语", expand=True,
            on_submit=lambda e: self._lookup_clicked())
        self._lookup_btn = ft.Button(
            "查询", icon=ft.Icons.SEARCH,
            on_click=lambda e: self._lookup_clicked())

        # 词典规则管理：lookup() 只认"第一条启用规则"，而这条 CRUD 此前没有任何
        # 入口——所以本页永远显示「（无释义）」。规则就是"目标为释义页的特殊书源"。
        self._add_rule_btn = ft.OutlinedButton(
            "新建词典规则", icon=ft.Icons.ADD,
            on_click=lambda e: self._fire(self.on_add_rule))
        self._rules_hint = ft.Text(
            "词典规则 = 一条释义页地址（{{key}} 是要查的词）+ 抽取正文的规则",
            size=12, color=ft.Colors.GREY_600)
        self._rule_rows = ft.ListView(spacing=4, height=180)

        self.controls = [
            ft.Row([self._query, self._lookup_btn], spacing=8),
            ft.Row([self._word, self._phonetic]),
            self._definition,
            self._status,
            ft.Row([self._close_btn], alignment=ft.MainAxisAlignment.END),
            ft.Divider(height=24),
            ft.Row([self._add_rule_btn, self._rules_hint], spacing=8,
                   run_spacing=4, wrap=True),
            ft.Container(self._rule_rows, expand=True),
        ]

    def _lookup_clicked(self) -> None:
        word = self._query.value or ""
        self._word.value = word
        self._fire(self.on_lookup, word)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def show(self, word: str) -> None:
        """打开弹窗并发起查询（Controller 接 on_lookup）。"""
        self._word.value = word or ""
        self._definition.value = ""
        self._status.value = "查询中…"
        self._fire(self.on_lookup, word or "")

    def set_definition(self, text: str, phonetic: str = "") -> None:
        self._definition.value = text or "（无释义）"
        self._phonetic.value = phonetic or ""
        self._status.value = ""

    def set_error(self, text: str) -> None:
        self._definition.value = ""
        self._status.value = text or "查询失败"

    def reset(self) -> None:
        """清空查询结果（"关闭"是清屏，不是把控制器 detach 掉）。"""
        self._word.value = ""
        self._phonetic.value = ""
        self._definition.value = ""
        self._status.value = ""

    # ------------------------------------------------------------------ #
    def render_rules(self, rules: list[dict]) -> None:
        """词典规则行：名字 + 释义页地址，点行编辑、尾部删除。"""
        rows = []
        for rule in rules or []:
            rid = rule.get("id")
            name = str(rule.get("name") or "(未命名)")
            url = str(rule.get("url") or "")
            if not rule.get("enabled", True):
                name = f"{name}（已停用）"
            rows.append(ft.ListTile(
                key=str(rid),
                title=ft.Text(name, weight=ft.FontWeight.W_500),
                subtitle=ft.Text(url, size=12, color=ft.Colors.GREY_700,
                                 max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                trailing=ft.IconButton(
                    ft.Icons.DELETE_OUTLINE, tooltip="删除",
                    on_click=lambda e, i=rid: self._fire(self.on_delete_rule, i)),
                on_click=lambda e, r=rule: self._fire(self.on_edit_rule, r)))
        self._rule_rows.controls = rows

    def set_rules_status(self, text: str) -> None:
        self._rules_hint.value = text or ""
