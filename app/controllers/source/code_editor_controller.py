"""CodeEditorController：规则编辑器的保存即校验编排（§1.6 / 契约 §9）。

保存路径：视图取文本 → 解析 JSON → SourceService.save_source（校验+落库）→
CompatAuditService.audit_source 审计提示渲染进视图审计区（§3.7 触发点②）。
试跑调试（M5）：run_debug 经 RuleEngineService.extract_values 对选中规则字段
做单字段求值，结果回显到视图调试区。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover
    class SourceService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.compat_audit_service import CompatAuditService
except Exception:  # pragma: no cover
    class CompatAuditService:  # type: ignore[no-redef]
        pass

try:
    from app.services.engine.rule_engine_service import RuleEngineService
except Exception:  # pragma: no cover
    class RuleEngineService:  # type: ignore[no-redef]
        pass


class CodeEditorController(BaseController):
    """规则编辑器：载入源 / 保存即校验 / 审计提示。"""

    destination = "sources.editor"

    def on_attach(self) -> None:
        self._source = self._safe_service(SourceService)
        self._audit = self._safe_service(CompatAuditService)
        self._rule_engine = self._safe_service(RuleEngineService)
        self._current = None  # 当前编辑的书源 dict
        # 试跑调试默认求值字段（Legado 书源搜索结果书名规则）与可选示例 HTML。
        self._debug_field = "bookNameRule"
        self._sample_html = None

        self._view.on_save = self.save
        self._view.on_run_debug = self.run_debug
        self._view.on_changed = self.mark_dirty

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def mark_dirty(self, text: str = "", _=None) -> None:
        """编辑即提示未保存：避免改完直接切页丢改动。"""
        if self._view is not None:
            self._view.set_status("有未保存修改")

    # ------------------------------------------------------------------ #
    def load_source(self, record: dict) -> None:
        """载入待编辑书源：把整条 JSON 喂进编辑器（规则字段即 JSON 文本）。"""
        self._current = record
        self._view.set_title(f"编辑规则：{record.get('bookSourceName', '')}")
        self._view.set_text(json.dumps(record, ensure_ascii=False, indent=2))
        self._view.set_status("编辑后点「保存并校验」")

    # ------------------------------------------------------------------ #
    def save(self, text: str) -> None:
        """保存即校验：解析 → 落库 → 审计提示。"""
        try:
            source = json.loads(text)
        except json.JSONDecodeError as exc:
            self._view.set_audit_report([f"JSON 语法错误：{exc}"])
            return
        if not isinstance(source, dict):
            self._view.set_audit_report(["书源规则应为 JSON 对象"])
            return
        if self._source is None:
            self._view.set_status("SourceService 未就绪，未保存")
            return
        future = self.ui_call(self._source.save_source(source))
        future.add_done_callback(self.on_done(lambda f: self._after_save(source, f)))

    def _after_save(self, source: dict, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            future.result()
        except Exception as exc:
            self._view.set_audit_report([f"保存失败：{exc}"])
            return
        self._view.set_status("已保存，正在审计…")
        if self._audit is None:
            self._view.set_audit_report([])
            return
        audit_future = self.ui_call(self._audit.audit_source(source))
        audit_future.add_done_callback(self.on_done(self._render_audit))

    def _render_audit(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            report = future.result()
        except Exception as exc:
            self._view.set_audit_report([f"审计失败：{exc}"])
            return
        lines = []
        for field_name in ("missing_java_apis", "missing_object_apis",
                           "unknown_fields", "unsupported_pseudos",
                           "js_syntax_errors", "regex_errors"):
            bucket = getattr(report, field_name, None)
            if bucket:
                items = bucket if isinstance(bucket, (list, tuple)) else [bucket]
                lines.append(f"{field_name}: " + "、".join(str(x) for x in items[:5]))
        self._view.set_audit_report(lines)

    # ------------------------------------------------------------------ #
    # 试跑调试（M5 补全）：RuleEngineService.extract_values 单字段求值。
    # ------------------------------------------------------------------ #
    def run_debug(self, text: str) -> None:
        """单字段规则试跑：解析书源 JSON → 取目标字段规则 → 经引擎求值回显。"""
        try:
            source = json.loads(text)
        except json.JSONDecodeError as exc:
            self._view.set_debug_result([f"JSON 语法错误：{exc}"])
            return
        if not isinstance(source, dict):
            self._view.set_debug_result(["书源规则应为 JSON 对象"])
            return
        field = getattr(self, "_debug_field", "bookNameRule")
        rule = self._pick_rule(source, field)
        if not rule:
            self._view.set_debug_result([f"未找到字段规则 {field}"
                                         "（可设 controller._debug_field）"])
            return
        if self._rule_engine is None:
            self._view.set_debug_result([
                "RuleEngineService 未就绪（规则已解析）", f"规则：{rule}"])
            return
        sample = getattr(self, "_sample_html", None)
        future = self.ui_call(self._eval_field(rule, sample))
        future.add_done_callback(self.on_done(lambda f: self._render_debug(f, rule)))

    @staticmethod
    def _pick_rule(source: dict, field: str) -> str:
        """从书源 dict 取目标字段规则：先顶层，再 Legado 常见嵌套容器。"""
        direct = source.get(field)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for container in ("ruleSearch", "ruleExplore", "ruleBookInfo",
                          "ruleContent", "ruleFind"):
            sub = source.get(container)
            if isinstance(sub, dict):
                value = sub.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    async def _eval_field(self, rule: str, sample_html: str | None) -> list[str]:
        """经 RuleEngineService 句柄做单字段求值；回落直接调引擎模块纯函数。"""
        method = (getattr(self._rule_engine, "extract_values", None)
                  or getattr(self._rule_engine, "eval_field", None))
        if method is not None:
            result = method(rule, sample_html or "", "html")
            if asyncio.iscoroutine(result):
                result = await result
            return list(result or [])
        if not sample_html:
            return []
        from app.services.engine import rule_engine
        nodes, _ = rule_engine.select_nodes(sample_html, "html", "")
        return rule_engine.extract_values(nodes, rule, "html")

    def _render_debug(self, future, rule: str) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        lines = [f"规则：{rule}"]
        try:
            values = future.result() or []
        except Exception as exc:
            lines.append(f"求值失败：{exc}")
            self._view.set_debug_result(lines)
            return
        if not values:
            lines.append("（示例为空：提供示例 HTML 后输出匹配值）")
        else:
            lines.extend(f"→ {v}" for v in values[:10])
        self._view.set_debug_result(lines)
