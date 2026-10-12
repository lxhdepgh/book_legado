"""ReplaceRuleController：全局替换规则 CRUD 编排（M5，§1.7 / 契约 §6）。

规则列表渲染经 ReplaceRuleService；CRUD/启停动作派发后刷新；订阅
ReplaceRulesChangedEvent 在阅读链路重洗正文缓存时同步本视图。

新增/编辑走**编辑对话框**（表单在控制器里搭：视图不碰服务、控制器不碰布局树）。
此前「新建规则」直接存一条 ``pattern=""`` 的空规则，「编辑」把原 dict 原样再存一遍
——两个按钮点了都等于没做事，这就是用户看到的功能无效之一。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.replace_rule_service import ReplaceRuleService
except Exception:  # pragma: no cover - 并行线未落盘
    class ReplaceRuleService:  # type: ignore[no-redef]
        pass

try:
    from app.core.events import ReplaceRulesChangedEvent
except Exception:  # pragma: no cover - 事件随并行线落地
    ReplaceRulesChangedEvent = None  # type: ignore[assignment]

#: scope 取值 → 界面文案（服务侧只认这两个，见 replace_rule_service.save_rule）。
SCOPES = (("content", "替换正文"), ("title", "替换标题"))


class ReplaceRuleController(BaseController):
    """替换规则：载入列表 / 编辑对话框增删改 / 启停 / 全局开关 / 事件刷新。"""

    destination = "tools.replace"

    def on_attach(self) -> None:
        self._svc = self._safe_service(ReplaceRuleService)
        self._dialog = None
        self._fields: dict = {}
        self._view.on_add = lambda: self.show_editor()
        self._view.on_edit = self.show_editor
        self._view.on_delete = self.delete_rule
        self._view.on_toggle = self.toggle_rule
        self._view.on_global_toggle = self.set_global_enabled
        if ReplaceRulesChangedEvent is not None:
            self.subscribe(ReplaceRulesChangedEvent, self._on_rules_changed)
        self.load_rules()
        self.load_global_state()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # EventBus 要求 async 处理器：包装同步 load_rules（同步副作用在处理器内完成）。
    # ------------------------------------------------------------------ #
    async def _on_rules_changed(self, _event) -> None:
        self.load_rules()
        self.load_global_state()

    def load_rules(self) -> None:
        if self._svc is None:
            self._view.set_status("ReplaceRuleService 未就绪")
            return
        future = self.ui_call(self._svc.list_rules())
        future.add_done_callback(self.on_done(self._render))

    def _render(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            rules = future.result() or []
        except Exception as exc:
            self._view.set_status(f"载入失败：{exc}")
            return
        self._view.render_rules(list(rules))

    def load_global_state(self) -> None:
        """读全局叠加开关（服务没起就按"开"显示，与服务 _global_on 的缺省一致）。"""
        if self._svc is None:
            self._view.set_global_enabled(True)
            return
        future = self.ui_call(self._svc.global_enabled())
        future.add_done_callback(self.on_done(
            lambda f: self._view is not None and self._view.set_global_enabled(
                _result(f, True))))

    def set_global_enabled(self, value: bool) -> None:
        if self._svc is None:
            self._view.set_status("ReplaceRuleService 未就绪")
            return
        future = self.ui_call(self._svc.set_global_enabled(bool(value)))
        future.add_done_callback(self.on_done(lambda f: self._after(f, "更新")))

    # ------------------------------------------------------------------ #
    # 编辑对话框
    # ------------------------------------------------------------------ #
    def show_editor(self, rule: dict | None = None) -> None:
        """弹出规则编辑框；``rule`` 为 None 时是新建。"""
        page = self._page()
        if page is None:
            self._view.set_status("界面未就绪，无法打开编辑框")
            return
        rule = dict(rule or {})
        scope = str(rule.get("scope") or "content")
        self._fields = {
            "id": rule.get("id"),
            "name": ft.TextField(label="名称", value=str(rule.get("name") or ""),
                                 hint_text="例如：去掉广告行"),
            "pattern": ft.TextField(label="匹配（正则或字面文本）", value=str(rule.get("pattern") or ""),
                                    multiline=False),
            "replacement": ft.TextField(label="替换为", value=str(rule.get("replacement") or "")),
            "scope": ft.Dropdown(label="作用范围", value=scope, width=160, dense=True,
                                 options=[ft.dropdown.Option(key, text)
                                          for key, text in SCOPES]),
            "is_regex": ft.Switch(label="正则表达式", value=bool(rule.get("is_regex", True))),
            "enabled": ft.Switch(label="启用", value=bool(rule.get("enabled", True))),
        }
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑规则" if rule.get("id") else "新建规则"),
            content=ft.Container(
                width=420,
                content=ft.Column([
                    self._fields["name"], self._fields["pattern"],
                    self._fields["replacement"],
                    ft.Row([self._fields["scope"], self._fields["is_regex"],
                            self._fields["enabled"]], spacing=12),
                ], tight=True, spacing=8)),
            actions=[
                ft.TextButton("取消", on_click=lambda e: page.pop_dialog()),
                ft.Button("保存", on_click=self._save_from_dialog),
            ],
        )
        self._dialog = dialog
        page.show_dialog(dialog)

    def _save_from_dialog(self, _event=None) -> None:
        page = self._page()
        pattern = str(self._fields.get("pattern").value or "").strip() \
            if self._fields.get("pattern") is not None else ""
        if not pattern:
            # 空 pattern 存进去什么都替不掉，还会在正文链路上白跑一趟正则
            if self._view is not None:
                self._view.set_status("匹配内容不能为空")
            if page is not None:
                page.pop_dialog()
            return
        rule = {
            "id": self._fields.get("id"),
            "name": str(self._fields["name"].value or "").strip() or pattern[:20],
            "pattern": pattern,
            "replacement": str(self._fields["replacement"].value or ""),
            "scope": str(self._fields["scope"].value or "content"),
            "is_regex": bool(self._fields["is_regex"].value),
            "enabled": bool(self._fields["enabled"].value),
        }
        if page is not None:
            page.pop_dialog()
        self._fields = {}
        self.save_rule(rule)

    def _page(self):
        """Flet page：未挂载时 ``view.page`` 直接抛 RuntimeError（不是 AttributeError）。"""
        view = self._view
        if view is None:
            return None
        try:
            return view.page
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def save_rule(self, rule: dict) -> None:
        if self._svc is None:
            return
        future = self.ui_call(self._svc.save_rule(rule))
        future.add_done_callback(self.on_done(lambda f: self._after(f, "保存")))

    def delete_rule(self, rule_id: str) -> None:
        if self._svc is None or not rule_id:
            return
        future = self.ui_call(self._svc.delete_rule(rule_id))
        future.add_done_callback(self.on_done(lambda f: self._after(f, "删除")))

    def toggle_rule(self, rule_id: str, enabled: bool) -> None:
        if self._svc is None or not rule_id:
            return
        future = self.ui_call(self._svc.set_enabled(rule_id, enabled))
        future.add_done_callback(self.on_done(lambda f: self._after(f, "更新")))

    def _after(self, future, action: str) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            future.result()
        except Exception as exc:
            self._view.set_status(f"{action}失败：{exc}")
            return
        self.load_rules()


def _result(future, default):
    """取 future 值，失败/未就绪时回落到默认（开关这类显示态不该因一次异常空掉）。"""
    try:
        value = future.result()
    except Exception:
        return default
    return default if value is None else value
