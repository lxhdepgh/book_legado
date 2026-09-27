"""DictPopupController：词典页的取词 + 词典规则管理编排（M5，§1.8 / 契约 §7）。

本控制器服务「更多工具 → 词典」页（手输查词 + 规则 CRUD），走 ``DictService.lookup(word)``
复用 RuleEngineService 抓释义页；结果回写页面，服务未就位时给出错误提示。
阅读器的划词取词不归这里：那条链在 ``ReaderController.lookup_word``，用同一套
``DictService``，弹窗由 ``ReaderView`` 自己搭。

词典规则的 CRUD 以前**没有任何入口**（服务方法建好了没人调用），而 ``lookup`` 只认
"第一条启用规则"——于是本页永远显示「（无释义）」。现在本页下半区就是规则清单，
新建/编辑走对话框。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.dict_service import DictService
except Exception:  # pragma: no cover - 并行线未落盘
    class DictService:  # type: ignore[no-redef]
        pass


class DictPopupController(BaseController):
    """取词弹窗：查询派发 + 结果回写 + 词典规则增删改。"""

    destination = "tools.dict"

    def on_attach(self) -> None:
        self._dict = self._safe_service(DictService)
        self._view.on_lookup = self.lookup
        # "关闭"只清结果：detach 会把控制器与视图解绑、页面变僵尸（切页
        # 再进才能恢复），关本页的正确方式是导航切走。
        self._view.on_close = lambda: self._view.reset()
        self._view.on_add_rule = lambda: self.show_rule_editor()
        self._view.on_edit_rule = self.show_rule_editor
        self._view.on_delete_rule = self.delete_rule
        self._rules: list[dict] = []
        self._fields: dict = {}
        self.load_rules()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def lookup(self, word: str) -> None:
        word = (word or "").strip()
        if not word:
            return
        if self._dict is None:
            self._view.set_error("DictService 未就绪")
            return
        if not any(rule.get("enabled", True) for rule in self._rules):
            # 服务在"没有启用规则"时返回空串，视图只会显示没头没尾的「（无释义）」
            self._view.set_error("还没有启用中的词典规则：在本页下方新建一条")
            return
        future = self.ui_call(self._dict.lookup(word))
        future.add_done_callback(self.on_done(lambda f: self._after_lookup(word, f)))

    def _after_lookup(self, word: str, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            result = future.result()
        except Exception as exc:
            logger.warning("取词失败 %s: %s", word, exc)
            self._view.set_error(f"查询失败：{exc}")
            return
        if isinstance(result, dict):
            self._view.set_definition(result.get("definition", ""),
                                      result.get("phonetic", ""))
        else:
            self._view.set_definition(str(result or ""))

    # ------------------------------------------------------------------ #
    # 词典规则 CRUD
    # ------------------------------------------------------------------ #
    def load_rules(self) -> None:
        if self._dict is None:
            self._view.set_rules_status("DictService 未就绪")
            return
        future = self.ui_call(self._dict.list_rules())
        future.add_done_callback(self.on_done(self._render_rules))

    def _render_rules(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            self._rules = list(future.result() or [])
        except Exception as exc:
            self._view.set_rules_status(f"词典规则载入失败：{exc}")
            return
        self._view.render_rules(self._rules)
        self._view.set_rules_status(
            f"共 {len(self._rules)} 条规则" if self._rules
            else "还没有词典规则：新建一条才能查词")

    def show_rule_editor(self, rule: dict | None = None) -> None:
        """规则编辑对话框；``rule`` 为 None 时是新建。"""
        page = self._page()
        if page is None:
            self._view.set_rules_status("界面未就绪，无法打开编辑框")
            return
        rule = dict(rule or {})
        rules = rule.get("rules") or {}
        if not isinstance(rules, dict):
            rules = {}
        self._fields = {
            "id": rule.get("id"),
            "name": ft.TextField(label="词典名称", value=str(rule.get("name") or ""),
                                 hint_text="例如：有道词典"),
            "url": ft.TextField(label="释义页地址", value=str(rule.get("url") or ""),
                                hint_text="https://dict.example.com/{{key}}"),
            "rule_content": ft.TextField(
                label="正文规则（可留空）", value=str(rules.get("ruleContent") or ""),
                hint_text="legado 规则，如 @css:div#Meaning"),
            "enabled": ft.Switch(label="启用", value=bool(rule.get("enabled", True))),
        }
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑词典规则" if rule.get("id") else "新建词典规则"),
            content=ft.Container(
                width=460,
                content=ft.Column([
                    self._fields["name"], self._fields["url"],
                    self._fields["rule_content"], self._fields["enabled"],
                ], tight=True, spacing=8)),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.Button("保存", on_click=self._save_from_dialog),
            ],
        )
        page.show_dialog(dialog)

    def _save_from_dialog(self, _event=None) -> None:
        page = self._page()
        url = str(self._fields["url"].value or "").strip()
        if not url:
            # 服务侧同样会拒（ValueError），但对话框已经关了，用户只看到"没存上"
            self._view.set_rules_status("释义页地址不能为空")
            if page is not None:
                page.pop_dialog()
            return
        rule = {
            "id": self._fields.get("id"),
            "name": str(self._fields["name"].value or "").strip() or url[:20],
            "url": url,
            "rules": {"ruleContent": str(self._fields["rule_content"].value or "").strip()},
            "enabled": bool(self._fields["enabled"].value),
        }
        if page is not None:
            page.pop_dialog()
        self._fields = {}
        future = self.ui_call(self._dict.save_rule(rule))
        future.add_done_callback(self.on_done(self._after_save))

    def _after_save(self, future) -> None:
        if self._view is None:
            return
        try:
            future.result()
        except Exception as exc:
            self._view.set_rules_status(f"保存失败：{exc}")
            return
        self.load_rules()

    def delete_rule(self, rule_id) -> None:
        if self._dict is None or rule_id in (None, ""):
            return
        future = self.ui_call(self._dict.delete_rule(rule_id))
        future.add_done_callback(self.on_done(lambda f: self.load_rules()))

    def _page(self):
        """Flet page：视图未挂到页上时 ``view.page`` 抛 RuntimeError。"""
        view = self._view
        if view is None:
            return None
        try:
            return view.page
        except Exception:
            return None
