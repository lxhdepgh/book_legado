"""RuleRepairController：自愈报告的事件订阅与确认写回编排（§3.2 / 契约 §5）。

订阅 :class:`RepairProgressEvent`（阶段进度）与 :class:`RepairReportReadyEvent`
（报告就绪）渲染；**只有用户点确认**才经 RuleRepairService.apply_fix 以
old||new 兜底分支写回书源（红线：不自动改源）。

两条必须记住的形状：
- 服务给出的是 :class:`rule_repair.RepairReport`（提议在 ``report.fix`` 里，
  失败时只有 ``report.reason``），视图契约是扁平 dict——展平是这里的活。
  曾经按顶层属性取值，结果报告卡一片空白、且"报告缺少 report_id"永远写不回。
- 事件投递是异步的（§5.11）：处理器可能在 ``detach()`` 之后才被调到，
  **碰控件前一律再查一次 ``self._view``**。
"""
from __future__ import annotations

import logging

from app.core.events import RepairProgressEvent, RepairReportReadyEvent
from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.sources.rule_repair_service import RuleRepairService
except Exception:  # pragma: no cover
    class RuleRepairService:  # type: ignore[no-redef]
        pass


class RuleRepairController(BaseController):
    """自愈：阶段进度 + 报告渲染 + 确认后写回。"""

    destination = "sources.repair"

    def on_attach(self) -> None:
        self._repair = self._safe_service(RuleRepairService)
        self._view.on_confirm_fix = self.confirm_fix
        self._view.on_dismiss = self.dismiss_fix
        self.subscribe(RepairProgressEvent, self._on_progress)
        self.subscribe(RepairReportReadyEvent, self._on_report_ready)

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    async def _on_progress(self, event: RepairProgressEvent) -> None:
        if self._view is None:
            return
        self._view.set_status(event.stage or "")

    async def _on_report_ready(self, event: RepairReportReadyEvent) -> None:
        if self._view is None:
            # 诊断还在路上用户切了页：报告照样缓存，只是这趟没地方渲染
            return
        report = self._load_report(event.report_id, event.source_id)
        self._view.render_report(report)

    def _load_report(self, report_id: str, source_id: str) -> dict:
        """凭 report_id 取回报告；服务无 get_report 时退化为最小占位 dict。"""
        getter = getattr(self._repair, "get_report", None) if self._repair else None
        if getter is None:
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": "RuleRepairService 未就绪"}
        try:
            return self._to_dict(getter(report_id), report_id, source_id)
        except Exception as exc:
            logger.warning("取回自愈报告失败 %s：%s", report_id, exc)
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": f"取回报告失败：{exc}"}

    @staticmethod
    def _to_dict(report, report_id: str = "", source_id: str = "") -> dict:
        """RepairReport / RuleFix → 视图契约的扁平 dict。

        ``report_id`` 由事件带来（报告对象自己没有它），写回时要原样带回去。
        ``has_fix`` 区分"给出了提议"和"给出了为什么给不出来"——两者都必须让
        用户看清，否则失败会显示成一条空规则等着被"应用"。
        """
        if isinstance(report, dict):
            payload = dict(report)
            payload.setdefault("report_id", report_id)
            payload.setdefault("source_id", source_id)
            return payload
        if report is None:
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": "没有这份报告（可能已过期）"}
        fix = getattr(report, "fix", None)
        samples = tuple(getattr(fix, "samples", ()) or ())
        return {
            "report_id": report_id,
            "source_id": (getattr(fix, "source_url", "") or source_id),
            "source_name": getattr(fix, "source_name", "") or source_id,
            "field": getattr(fix, "field", ""),
            "old_value": getattr(fix, "old_value", ""),
            "new_value": getattr(fix, "new_value", ""),
            "samples": [
                {"chapter": getattr(s, "chapter", ""),
                 "url": getattr(s, "url", ""),
                 "chars": int(getattr(s, "chars", 0) or 0),
                 "snippet": getattr(s, "snippet", "")}
                for s in samples],
            "note": getattr(fix, "note", ""),
            "has_fix": fix is not None,
            "reason": getattr(report, "reason", ""),
            "tried": int(getattr(report, "tried", 0) or 0),
            "pages": int(getattr(report, "pages", 0) or 0),
        }

    # ------------------------------------------------------------------ #
    def confirm_fix(self, report: dict) -> None:
        report_id = str((report or {}).get("report_id", "") or "")
        if self._repair is None:
            self._view.set_status("RuleRepairService 未就绪，未写回")
            return
        if not report_id:
            self._view.set_status("报告缺少 report_id，无法写回")
            return
        # apply_fix 的 fix_id 可省（服务内按报告默认修复项）；写回结果以
        # 返回值为准——失败是 False，不是异常。
        self._view.set_status("正在写回书源并复测…")
        future = self.ui_call(self._repair.apply_fix(report_id))
        future.add_done_callback(self.on_done(self._after_apply))

    def _after_apply(self, future) -> None:
        try:
            ok = bool(future.result())
        except Exception as exc:
            self._view.set_status(f"写回失败：{exc}")
            return
        self._view.set_status(
            "已写回书源并复测" if ok else "写回未生效（报告过期或书源已删除）")

    def dismiss_fix(self, report: dict) -> None:
        self._view.render_report(None)
        self._view.set_status("已忽略该自愈候选（书源未改动）")
