"""M3 书源域检测/修复/审计三服务外壳测试（契约 §10，≥10 条）。

覆盖：
- SourceCheckService：生命周期 / 单源与批量入口 / blocked 结论喂入事件 /
  进度写 status.progress / cancel 取消 / 检测流水线复跑审计（触发点③）；
- RuleRepairService：离线自愈闭环（注入 ProbeEngine + 本地 HTML 回放）/
  apply_fix 确认才写回 / RepairReportReadyEvent；
- CompatAuditService：单源审计缺口 / 三个触发点 / audit_log 落库。

不依赖 Flet / 真实网络：网络一律用注入 checker 或本地 HTML 回放替身。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import pytest

from app.core.events import (CheckFinishedEvent, RepairProgressEvent,
                             RepairReportReadyEvent)
from app.core.lifecycle import ServiceState
from app.models.book_source import BookSource
from app.services.engine.rule_engine import ChapterItem
from app.services.rule_repair import ProbeEngine
from app.services.sources.compat_audit_service import CompatAuditService
from app.services.sources.rule_repair_service import RuleRepairService
from app.services.sources.source_check_service import SourceCheckService


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class RecordingBus:
    """只收事件的假总线：publish 立即记录、不派发。"""

    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event):
        self.events.append(event)
        return []


async def fake_checker(sources, concurrency=8, timeout=12, on_result=None,
                       auto_disable=False):
    """离线检测替身：不发网络，按序号造结果并逐条回调 on_result。"""
    results = []
    for i, s in enumerate(sources, 1):
        state = "blocked" if i == 1 else "fully_usable"
        r = {"idx": i, "name": getattr(s, "bookSourceName", ""),
             "raw_url": getattr(s, "bookSourceUrl", ""),
             "check_state": state, "usable": state == "fully_usable",
             "status": "ok"}
        results.append(r)
        if on_result:
            on_result(r, i, len(sources))
    return results


# 自愈样本页（与 tests/test_rule_repair.py 同形，离线本地 HTML）
PROSE = [
    "萧青梧睁开眼的时候，长安城的晨鼓刚敲过第三通，坊门外的胡饼摊已经支起了炉子，"
    "热气在冷风里散成一片白雾，远处传来驼铃，一声一声压在青石板上。",
    "他摸了摸胸口，那里藏着一枚从时空管理局带出来的空间钮，冰凉得像一块铁，"
    "却比这世上任何东西都可靠。",
    "昨夜他还在图书馆里翻《旧唐书》，一页没读完便趴在桌上睡了过去；再醒过来时，"
    "身下是硬邦邦的木板床，头顶是熏得发黑的房梁。",
    "门外传来脚步声，一个穿青色短褐的少年探头进来，见他醒了便笑，"
    "说杜郎君你可算醒了，李学士在厅里等你半晌。",
    "离得近了才看清，少年腰间挂着一枚铜牌，上面刻着的名字他认得。",
]
TRAP = "本章未完，请访问 http://www.example.com/bl/23_b/3.html 继续阅读。"
SAMPLE = "https://www.52shuku.net/bl/23_b/p1.html"
SECOND = "https://www.52shuku.net/bl/23_b/p2.html"
THIRD = "https://www.52shuku.net/bl/23_b/p3.html"


def _page(*paragraphs):
    body = "".join(f"<p>{t}</p>" for t in paragraphs)
    return (
        "<html><head><title>页</title></head><body>"
        '<div class="nav"><a href="/">首页</a></div>'
        '<div class="wrap"><div class="crumbs">位置</div>'
        f'<div class="article-content">{body}</div>'
        '<div class="sidebar"><a href="/a.html">推荐</a></div></div></html>')


PAGE_ONE, PAGE_TWO, PAGE_THREE = _page(*PROSE, TRAP), _page(*PROSE), _page(*PROSE, TRAP)

REPAIR_SOURCE = {
    "bookSourceName": "示例书源",
    "bookSourceUrl": "https://www.po5.net",
    "ruleContent": {
        "content": "#nr1@html",
        "replaceRegex": "##目录.*尾页|Tips：.*|传送门：.*|Top|.*http.*",
    },
}


def _chapter(name: str, url: str) -> ChapterItem:
    return ChapterItem(name, url)


class _Transport:
    """按 URL 发本地样本页的假传输（不出网）。"""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requests: list[str] = []

    async def __call__(self, request, source=None):
        url = str(request.get("url") or "")
        self.requests.append(url)
        return self.pages.get(url, ""), "text/html", url


def _make_probe() -> ProbeEngine:
    engine = ProbeEngine(timeout=5, background=True)
    engine._request_transport = _Transport(
        {SAMPLE: PAGE_ONE, SECOND: PAGE_TWO, THIRD: PAGE_THREE})
    return engine


# --------------------------------------------------------------------------- #
# 1. SourceCheckService 外壳生命周期
# --------------------------------------------------------------------------- #
class TestCheckServiceLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_moves_state(self):
        svc = SourceCheckService(checker=fake_checker)
        await svc.start()
        assert svc.status.state is ServiceState.RUNNING
        await svc.stop()
        assert svc.status.state is ServiceState.DISPOSED

    async def test_single_check_returns_result_and_publishes_event(self):
        bus = RecordingBus()
        svc = SourceCheckService(checker=fake_checker)
        svc.bus = bus
        result = await svc.check_source(REPAIR_SOURCE)
        assert result["raw_url"] == "https://www.po5.net"
        assert result["check_state"] == "blocked"  # idx=1 → blocked 结论
        assert len(bus.events) == 1
        ev = bus.events[0]
        assert isinstance(ev, CheckFinishedEvent)
        assert ev.source_id == "https://www.po5.net"
        assert ev.state_class == "blocked"

    async def test_batch_check_progress_and_summary(self):
        bus = RecordingBus()
        srcs = [BookSource.from_dict(REPAIR_SOURCE),
                BookSource.from_dict({**REPAIR_SOURCE,
                                      "bookSourceUrl": "https://two.example"})]
        svc = SourceCheckService(checker=fake_checker)
        svc.bus = bus
        summary = await svc._run_checks(srcs)
        # 进度收尾落在 1.0
        assert svc.status.progress == 1.0
        assert len(bus.events) == 2
        assert summary[0]["check_state"] == "blocked"
        assert summary[1]["check_state"] == "fully_usable"

    async def test_blocked_verdict_flows_into_event(self):
        bus = RecordingBus()
        svc = SourceCheckService(checker=fake_checker)
        svc.bus = bus
        await svc.check_source(REPAIR_SOURCE)
        ev = bus.events[0]
        # 九态 blocked 结论由 block_pages 喂入，原样透传到事件 state_class
        assert ev.state_class == "blocked"
        assert "blocked" in ev.summary

    async def test_cancel_interrupts_running_check(self):
        release = asyncio.Event()
        entered = asyncio.Event()

        async def hanging_checker(sources, **kw):
            entered.set()
            await release.wait()
            return []

        # 给一个带 sources 的桩，check_all 才会真正进入 hanging_checker
        stub = SimpleNamespace(sources=[BookSource.from_dict(REPAIR_SOURCE)])
        svc = SourceCheckService(checker=hanging_checker, source_service=stub)
        task = asyncio.create_task(svc.check_all())
        await entered.wait()
        svc.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_detection_reruns_audit_per_source(self):
        recorded: list[str] = []

        class FakeCompat:
            async def audit_source(self, data):
                recorded.append(data.get("bookSourceUrl"))

        svc = SourceCheckService(checker=fake_checker,
                                 compat_audit=FakeCompat())
        svc.bus = RecordingBus()
        await svc.check_source(REPAIR_SOURCE)
        # 复跑审计是 fire-and-forget 任务，让它跑一个调度节拍
        await asyncio.sleep(0)
        assert recorded == ["https://www.po5.net"]


# --------------------------------------------------------------------------- #
# 2. RuleRepairService 自愈闭环（离线本地 HTML 回放）
# --------------------------------------------------------------------------- #
class TestRuleRepairService(unittest.IsolatedAsyncioTestCase):
    async def test_offline_closed_loop_produces_fix(self):
        svc = RuleRepairService()
        engine = _make_probe()
        source = BookSource.from_dict(REPAIR_SOURCE)
        try:
            report = await svc.repair(
                source, _chapter("第1页", SAMPLE),
                extra_chapters=[_chapter("第2页", SECOND),
                                _chapter("第3页", THIRD)],
                engine=engine)
        finally:
            await engine.aclose()
        assert report.fix is not None, report.reason
        # old||new 兜底分支合并值（红线：不改原规则在前）
        assert report.fix.new_value == "#nr1@html||.article-content@p@text"
        assert report.fix.old_value == "#nr1@html"
        # 回放不出网：真抓 3 页（1 失败页 + 2 复核页），候选回放不发新请求
        assert engine.fetches == 3

    async def test_repair_publishes_stage_progress_then_ready_event(self):
        bus = RecordingBus()
        svc = RuleRepairService()
        svc.bus = bus
        engine = _make_probe()
        try:
            report = await svc.repair(
                BookSource.from_dict(REPAIR_SOURCE),
                _chapter("第1页", SAMPLE), engine=engine)
        finally:
            await engine.aclose()
        ready = [ev for ev in bus.events if isinstance(ev, RepairReportReadyEvent)]
        progress = [ev for ev in bus.events if isinstance(ev, RepairProgressEvent)]
        # 长任务必须有进度事件（§5.11）：诊断要真抓样本页 + 本地回放几十条候选，
        # 只有一条收尾事件的话，界面从点下去到报告就绪之间没有任何东西可显示。
        assert len(progress) >= 2, bus.events
        assert all(ev.stage for ev in progress)
        assert all(ev.source_id == "https://www.po5.net" for ev in progress)
        assert len(ready) == 1
        ev = ready[0]
        assert ev.source_id == "https://www.po5.net"
        # 凭 report_id 能取回报告
        assert svc.get_report(ev.report_id) is report

    async def test_repair_without_bus_still_returns_report(self):
        """没接总线（无头/替身装配）时诊断照样跑完：进度是加分项，不是前置条件。"""
        svc = RuleRepairService()
        engine = _make_probe()
        try:
            report = await svc.repair(
                BookSource.from_dict(REPAIR_SOURCE),
                _chapter("第1页", SAMPLE), engine=engine)
        finally:
            await engine.aclose()
        assert report.fix is not None, report.reason

    async def test_apply_fix_only_writes_after_confirm(self):
        applied: dict = {}

        class FakeSourceService:
            def update_source_rule(self, url, field, value):
                applied["url"], applied["field"], applied["value"] = url, field, value

        bus = RecordingBus()
        svc = RuleRepairService(source_service=FakeSourceService())
        svc.bus = bus
        engine = _make_probe()
        try:
            report = await svc.repair(
                BookSource.from_dict(REPAIR_SOURCE),
                _chapter("第1页", SAMPLE), engine=engine)
        finally:
            await engine.aclose()
        # 诊断阶段绝不写回（红线）
        assert applied == {}
        report_id = [ev for ev in bus.events
                     if isinstance(ev, RepairReportReadyEvent)][0].report_id
        # 用户确认后才写回
        ok = await svc.apply_fix(report_id, "")
        assert ok is True
        assert applied["url"] == "https://www.po5.net"
        assert applied["field"] == "content"
        assert applied["value"] == "#nr1@html||.article-content@p@text"

    async def test_apply_fix_unknown_report_is_refused(self):
        svc = RuleRepairService(source_service=object())
        assert await svc.apply_fix("no-such-id", "") is False


# --------------------------------------------------------------------------- #
# 3. CompatAuditService 单源审计 + audit_log 落库
# --------------------------------------------------------------------------- #
class _FakeStorage:
    """带一张 audit_log 表的内存库替身（离线落库测试）。"""

    def __init__(self):
        import sqlite3
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " source_id TEXT, missing_apis TEXT, ts TEXT)")


AUDIT_SRC = {
    "bookSourceName": "审计书源",
    "bookSourceUrl": "https://audit.example",
    "ruleContent": {"content": ".article-content@p@text",
                    "bogusField": "x"},
}


class TestCompatAuditService(unittest.IsolatedAsyncioTestCase):
    async def test_audit_source_reports_unknown_field(self):
        svc = CompatAuditService(storage=_FakeStorage())
        report = await svc.audit_source(AUDIT_SRC)
        assert report.source_id == "https://audit.example"
        assert "bogusField" in report.unknown_fields
        # 干净源其余清单为空
        assert report.missing_java_apis == []
        assert report.js_syntax_errors == []

    async def test_audit_source_accepts_booksource_object(self):
        # BookSource.to_dict() 会滤掉未知规则字段，这里只验证对象入口可跑通
        svc = CompatAuditService(storage=_FakeStorage())
        report = await svc.audit_source(BookSource.from_dict(AUDIT_SRC))
        assert report.source_id == "https://audit.example"
        assert isinstance(report.unknown_fields, list)

    async def test_audit_log_persists_row(self):
        storage = _FakeStorage()
        svc = CompatAuditService(storage=storage)
        await svc.audit_source(AUDIT_SRC)
        rows = storage.db.execute(
            "SELECT source_id, missing_apis, ts FROM audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "https://audit.example"
        assert rows[0][2]  # ts 非空
        # list_logs 能读回
        logs = svc.list_logs("https://audit.example")
        assert len(logs) == 1
        assert "bogusField" in logs[0]["missing_apis"]["unknown_fields"]

    async def test_audit_clean_source_has_no_log_gaps(self):
        svc = CompatAuditService(storage=_FakeStorage())
        clean = {"bookSourceName": "干净源", "bookSourceUrl": "https://clean.example",
                 "ruleContent": {"content": ".main@p@text"}}
        report = await svc.audit_source(clean)
        assert report.clean is True
