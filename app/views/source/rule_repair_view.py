"""RuleRepairView：正文规则自愈报告确认写回（M3，契约 §9）。

自旧冻结视图 ``rule_repair_ui.py`` 的"提议弹窗、确认后写回"职责拆出。新视图是
事件驱动轻壳：RuleRepairService 完成自愈发 :class:`RepairReportReadyEvent`，
Controller 取回报告后调本视图渲染候选修复；**只有用户点确认**才经 Controller
写回书源（红线 §3.2：不自动改源）。

红线（§7.5）：本文件零 app.services.* import；报告以 dict 快照注入。
报告 dict 结构：report_id / source_id / source_name / field / old_value /
new_value / samples / note；``has_fix`` 为假时另有 ``reason``（此时只渲染原因，
不出现"应用"按钮）。
"""
from __future__ import annotations

import flet as ft


class RuleRepairView(ft.Column):
    """自愈：报告卡（原规则 vs 新规则 + 抽样复核）+ 确认写回 / 忽略。"""

    def __init__(self) -> None:
        super().__init__(spacing=10, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_confirm_fix = None     # (report: dict) -> None
        self.on_dismiss = None         # (report: dict) -> None

        self._title = ft.Text("正文规则自愈", size=24, weight=ft.FontWeight.BOLD)
        self._status = ft.Text("等待自愈报告…", size=12, color=ft.Colors.GREY_600)

        # 报告卡（空态占位；render_report 填充内容）
        self._card = ft.Container(visible=False)
        self._body = ft.Column(spacing=6)

        self.controls = [
            self._title,
            self._status,
            ft.Container(expand=True, content=self._card),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def render_report(self, report: dict) -> None:
        """渲染一条自愈报告；空 dict/None 回到等待态。

        ``has_fix`` 为假时**只说原因、不给按钮**：诊断大多数时候是以"没找到可写回
        的规则"结束的（页面被拦截、原规则本地重放又成功、候选都没过复核…），
        把这种结果画成一条空规则等用户点"应用"，等于让界面说谎。
        """
        if not report:
            self._card.visible = False
            self.set_status("等待自愈报告…")
            return
        source_name = report.get("source_name") or report.get("source_id", "")
        samples = report.get("samples") or []
        if not report.get("has_fix", True):
            self._card.content = ft.Card(
                content=ft.Container(
                    padding=16,
                    content=ft.Column([
                        ft.Text(f"书源：{source_name}", weight=ft.FontWeight.W_500),
                        ft.Text(report.get("reason") or "没有找到可写回的规则。",
                                size=13, selectable=True),
                        ft.Text("书源未做任何改动。", size=12,
                                color=ft.Colors.GREY_600),
                    ], tight=True, spacing=8)))
            self._card.visible = True
            self.set_status("这次自愈没有给出可确认的提议（书源未改动）")
            return
        sample_text = "、".join(
            f"{s.get('chapter', '')}（{s.get('chars', 0)} 字）"
            for s in samples if isinstance(s, dict)) or "仅 1 页样本"

        rows = [
            ft.Text(f"书源：{source_name}", weight=ft.FontWeight.W_500),
            ft.Text(f"抽样复核：{sample_text}", size=12, color=ft.Colors.GREY_700),
        ]
        if report.get("note"):
            rows.append(ft.Text(report["note"], size=12, color=ft.Colors.GREY_600))
        rows += [
            ft.Text("原规则", size=12, weight=ft.FontWeight.BOLD),
            ft.Text(report.get("old_value", ""), size=12, selectable=True),
            ft.Text("新规则", size=12, weight=ft.FontWeight.BOLD),
            ft.Text(report.get("new_value", ""), size=12, selectable=True,
                     color=ft.Colors.BLUE_700),
        ]
        preview = (samples[0].get("snippet", "") if samples
                   and isinstance(samples[0], dict) else "")
        if preview:
            rows.append(ft.Text(f"正文预览：{preview}", size=12,
                                 color=ft.Colors.GREY_500))

        report_slot = report
        self._card.content = ft.Card(
            content=ft.Container(
                padding=16,
                content=ft.Column([
                    *rows,
                    ft.Row([
                        ft.TextButton("忽略", on_click=lambda e: self._fire(
                            self.on_dismiss, report_slot)),
                        ft.FilledTonalButton(
                            "应用并重试", icon=ft.Icons.BUILD_OUTLINED,
                            on_click=lambda e: self._fire(
                                self.on_confirm_fix, report_slot)),
                    ], alignment=ft.MainAxisAlignment.END),
                ], tight=True, spacing=8)))
        self._card.visible = True
        self.set_status(f"已收到《{source_name}》的自愈候选，请确认是否写回")
