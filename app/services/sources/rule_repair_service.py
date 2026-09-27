"""RuleRepairService：正文规则自愈闭环的服务外壳（M3，契约 §5 / 设计 §3.2/§3.4）。

外壳化 ``app/services/rule_repair.py``（M2 冻结副本，逻辑零改动）：

- 自愈闭环**原样保留**：真抓一章样本页（``_load_page``）→ ``_mine`` 按"汉字最多、
  链接占比低"定位正文并从紧容器向外层生成候选选择器 → ``ProbeEngine`` 把请求口焊死
  在同一份 HTML 上反复回放不出网 → ``_confirm_candidate`` 再抓 1-2 页同书章节复核
  → 产出 :class:`~app.services.rule_repair.RuleFix`/:class:`RepairReport`。
  本壳只调 :func:`rule_repair.diagnose_content_rule`，不改其内部；
- 对外入口：``async repair(...)``（自愈诊断，产出 RepairReport）、
  ``async apply_fix(report_id, fix_id)``（**用户确认后**才写回书源——保留红线，不自动改源）；
- 诊断的每个阶段发 :class:`~app.core.events.RepairProgressEvent`（冻结副本里
  ``diagnose_content_rule`` 的 ``on_stage`` 形参，本壳把它接成事件），完成发
  :class:`~app.core.events.RepairReportReadyEvent`，视图凭 report_id 取报告；
- 样本页加载时的拦截判定复用 block_pages（§3.4，已在 rule_repair._load_page 内）。

红线：本服务零 flet import；ProbeEngine 的 HTML 回放语义必须保留（注入的 engine
原样透传给 diagnose_content_rule，不在壳层改请求口）。
"""
from __future__ import annotations

import importlib
import logging
import uuid

from app.core.events import RepairProgressEvent, RepairReportReadyEvent
from app.core.lifecycle import Service
from app.services.rule_repair import (RepairReport, diagnose_content_rule)

logger = logging.getLogger(__name__)


class RuleRepairService(Service):
    """正文规则自愈：诊断出建议 → 用户确认后才写回。"""

    name = "RuleRepairService"
    #: 拓扑依赖：RuleEngineService/SourceCheckService 由并行线交付，运行期经
    #: on_start 懒解析或构造注入，此处不 import 未就绪类。
    dependencies: tuple[type, ...] = ()

    def __init__(self, source_service=None, auto_trigger: bool = False) -> None:
        super().__init__()
        self._source_override = source_service
        self._source_service = source_service
        # 自愈报告缓存：report_id -> RepairReport（视图凭 event 里的 id 取回）。
        self._reports: dict[str, RepairReport] = {}
        # CheckFinishedEvent 自动触发入口（契约 §5：可选，最小实现仅 UI 触发）。
        self._auto_trigger = bool(auto_trigger)
        self._unsubscribe = None

    async def on_start(self) -> None:
        self._source_service = self._resolve(
            "app.services.sources.source_service.SourceService",
            self._source_override)
        # F6：真实环境（hub 非空）下必需协作者 SourceService 缺失须可见——
        # apply_fix 无法写回；服务仍 RUNNING，仅写 last_error 告警，不 SUSPENDED。
        if self.hub is not None and self._source_service is None:
            logger.error(
                "RuleRepairService 协作者缺失：SourceService 未注册，"
                "apply_fix 无法写回书源")
            self.status.last_error = "SourceService 未注册，规则修复无法写回"
        # 可选：订阅 CheckFinishedEvent 自动触发（默认关——事件只带 source_id，
        # 不带失败章节，闭环所需的 chapter 仍由 UI 从检测结果里带进来，故最小实现
        # 仅 UI 主动调 repair()）。此处仅保留订阅钩子，不强行订阅。
        logger.info("RuleRepairService 已启动（auto_trigger=%s）",
                    self._auto_trigger)

    async def on_stop(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # pragma: no cover - 退订容错
                pass
            self._unsubscribe = None
        logger.info("RuleRepairService 已停止")

    # ------------------------------------------------------------------ #
    # 对外入口（契约 §5）
    # ------------------------------------------------------------------ #
    async def repair(self, source, chapter, *, extra_chapters=(), book=None,
                     engine=None, timeout: int = 15) -> RepairReport:
        """跑自愈闭环，产出 RepairReport（**不写回书源**）。

        参数对齐冻结的 :func:`diagnose_content_rule`：``source`` 为 BookSource，
        ``chapter`` 为失败章节 ChapterItem；``engine`` 可注入（测试用 ProbeEngine
        + 本地 HTML 回放），不传则由 rule_repair 自建。返回的 RepairReport 经
        report_id 缓存，并广播 :class:`RepairReportReadyEvent`。

        阶段进度经 :class:`RepairProgressEvent` 广播：一次诊断要真抓 1-3 页样本、
        再把几十条候选在本地回放，秒级到十几秒。诊断在引擎循环上跑，界面只拿得到
        事件——所以进度必须以事件出来（§5.11），服务里不碰任何控件。
        """
        source_id = str(getattr(source, "bookSourceUrl", "") or "")
        report = await diagnose_content_rule(
            source, chapter,
            extra_chapters=list(extra_chapters or ()),
            book=book, engine=engine, timeout=timeout,
            on_stage=self._stage_publisher(source_id))
        report_id = uuid.uuid4().hex
        self._reports[report_id] = report
        if self.bus is not None:
            self.bus.publish(RepairReportReadyEvent(
                report_id=report_id, source_id=source_id))
        return report

    def _stage_publisher(self, source_id: str):
        """diagnose_content_rule 的 ``on_stage`` 挂点 → 事件（无总线时返回 None）。"""
        if self.bus is None:
            return None

        def publish_stage(stage: str) -> None:
            self.bus.publish(RepairProgressEvent(
                source_id=source_id, stage=str(stage or "")))

        return publish_stage

    def get_report(self, report_id: str) -> RepairReport | None:
        """凭 report_id 取回缓存的报告（视图确认对话框用）。"""
        return self._reports.get(report_id)

    async def apply_fix(self, report_id: str, fix_id: str = "") -> bool:
        """用户确认后写回书源（红线：只在用户确认时调用，不自动改源）。

        写回经 SourceService.update_source_rule(source_url, field, new_value)——
        ``new_value`` 已是 rule_repair 产出的 ``old||new`` 兜底分支合并值，本壳不
        再拼合。写回失败（无报告/无 fix/无 SourceService）返回 False，不抛。
        """
        report = self._reports.get(report_id)
        if report is None or report.fix is None:
            logger.warning("apply_fix 无报告或无候选修复：%s", report_id)
            return False
        fix = report.fix
        if self._source_service is None or not hasattr(
                self._source_service, "update_source_rule"):
            logger.warning("apply_fix 无 SourceService，无法写回：%s", report_id)
            return False
        try:
            self._source_service.update_source_rule(
                fix.source_url, fix.field, fix.new_value)
        except Exception as exc:  # 写回失败由调用方（视图）提示，不抛穿服务
            logger.warning("apply_fix 写回失败：%s", exc)
            return False
        return True

    # ------------------------------------------------------------------ #
    def _resolve(self, dotted: str, override):
        """优先构造注入；否则经 hub.require 取原始实例；都没有返回 None。"""
        if override is not None:
            return override
        try:
            module_path, class_name = dotted.rsplit(".", 1)
            cls = getattr(importlib.import_module(module_path), class_name)
        except Exception:
            return None
        if self.hub is None:
            return None
        try:
            return self.hub.require(cls)
        except Exception:
            return None
