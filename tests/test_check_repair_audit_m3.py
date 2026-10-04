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
                       auto_disable=False, depth="full"):
    """离线检测替身：不发网络，按序号造结果并逐条回调 on_result。

    ``depth`` 是 check_sources 的真实形参（分级检测），替身照收——服务那侧
    是否把深度转下去，由 test_check_depth_tiering 钉住。
    """
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

    async def test_event_carries_the_planned_denominator(self):
        """发布方把 done/total 挂上事件：视图自己数完成的条数只会得出 12/12。

        流水线那边本来就有这两个数（``on_result(result, done, total)``），钉的是
        "每一个事件都带着**计划规模**当分母"，而不是"最后一条等于总数"。
        """
        bus = RecordingBus()
        srcs = [BookSource.from_dict({**REPAIR_SOURCE,
                                      "bookSourceUrl": f"https://s{i}.example.com"})
                for i in range(5)]
        svc = SourceCheckService(checker=fake_checker)
        svc.bus = bus
        await svc._run_checks(srcs)
        events = [e for e in bus.events if isinstance(e, CheckFinishedEvent)]
        assert len(events) == 5
        assert [e.total for e in events] == [5] * 5, "分母该是计划规模，每条都带"
        assert [e.done for e in events] == [1, 2, 3, 4, 5]

    async def test_single_source_event_says_the_plan_size(self):
        """单源检测：total=1、done=1（分母不是 0，界面不用猜）。"""
        bus = RecordingBus()
        svc = SourceCheckService(checker=fake_checker)
        svc.bus = bus
        await svc.check_source(REPAIR_SOURCE)
        ev = bus.events[0]
        assert (ev.done, ev.total) == (1, 1)

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
        # 收尾（_drain_audits）就在这一轮里等完，不再留没人等的后台尾巴
        assert recorded == ["https://www.po5.net"]


# --------------------------------------------------------------------------- #
# 1b. 触发点③的扇出上限（越跑越慢的第二笔账：整罐 cookie 之外就是这里）
# --------------------------------------------------------------------------- #
def _fanout_sources(n: int) -> list[BookSource]:
    return [BookSource.from_dict({**REPAIR_SOURCE,
                                  "bookSourceUrl": f"https://s{i}.example.com"})
            for i in range(n)]


async def _fanout_checker(sources, concurrency=8, timeout=12, on_result=None,
                          auto_disable=False, depth="full"):
    """逐源回调的检测替身：每源之间让出一次，好把审计的并发形状照出来。"""
    results = []
    for i, source in enumerate(sources, 1):
        payload = {"idx": i, "name": source.bookSourceName,
                   "raw_url": source.bookSourceUrl, "check_state": "fully_usable",
                   "usable": True, "status": "ok"}
        results.append(payload)
        if on_result:
            on_result(payload, i, len(sources))
        await asyncio.sleep(0)      # 把刚 create_task 的审计放上线
    return results


class _CountingCompat:
    """记账替身（子类约束见记忆 `guard-tests-assert-absence`：这里必须是同形状协程）。

    `audit_source` 里 await 一段假 CPU——真实审计是纯 Python 遍历（`_audit_one`
    中位 3.2 ms、p95 189 ms，`.probe/mp_cpu_step_magnitude.py`）；扇出有没有被限住，
    就看同时有几条卡在这个 await 上。
    """

    def __init__(self, hold: float = 0.02) -> None:
        self.running = 0
        self.peak = 0
        self.calls: list[str] = []
        self.hold = hold

    async def audit_source(self, data):
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.hold)
            self.calls.append(str(data.get("bookSourceUrl")))
        finally:
            self.running -= 1


class TestAuditFanOutBound(unittest.IsolatedAsyncioTestCase):
    N = 24

    async def _service(self, compat):
        return SourceCheckService(checker=_fanout_checker, compat_audit=compat)

    async def test_fan_out_never_exceeds_the_declared_cap(self):
        """护栏钉"没发生"：并发从未越过本轮该用的那一档上限。"""
        from app.services.sources import source_check_service as shell
        compat = _CountingCompat()
        svc = await self._service(compat)
        bound = shell._audit_fanout_bound()   # noqa: SLF001
        await svc._run_checks(_fanout_sources(self.N))
        self.assertLessEqual(compat.peak, bound,
                             f"审计并发冲到 {compat.peak}，本轮上限是 {bound}")
        self.assertEqual(len(compat.calls), self.N, "有审计被限丢了")

    async def test_the_cap_actually_lets_that_many_through(self):
        """反证：上限不是"永远只跑一条"——照出的并发必须真的等于上限。

        上一栏只写 `peak <= 上限` 的话，把上限改成 1、或让闸门永远只放一条，都能蒙过去。
        """
        from app.services.sources import source_check_service as shell
        compat = _CountingCompat()
        svc = await self._service(compat)
        bound = shell._audit_fanout_bound()   # noqa: SLF001
        await svc._run_checks(_fanout_sources(self.N))
        self.assertEqual(compat.peak, bound)

    async def test_the_bound_follows_where_the_audit_actually_runs(self):
        """两档口径：审计在子进程里跑就喂满池，退回线程池就必须限死。

        退回那一档不是摆设——池被打穿（子进程原生 abort）超过
        `AUDIT_POOL_MAX_REBUILDS` 次之后，审计就回到默认线程池里跑，而检测自己要的
        `to_thread` 用的是同一个池；那时还按 8 扇出就是把重 CPU 审计放回去挤它。
        """
        from app.services.sources import compat_audit_service as cas
        from app.services.sources import source_check_service as shell
        saved = cas._STATE.disabled           # noqa: SLF001
        try:
            cas._STATE.disabled = False       # noqa: SLF001
            self.assertEqual(shell._audit_fanout_bound(),  # noqa: SLF001
                             shell.AUDIT_POOL_FANOUT)
            self.assertGreater(shell.AUDIT_POOL_FANOUT, shell.AUDIT_CONCURRENCY,
                               "两档写成一样，这个分支就白读了")
            cas._STATE.disabled = True        # noqa: SLF001
            self.assertEqual(shell._audit_fanout_bound(),  # noqa: SLF001
                             shell.AUDIT_CONCURRENCY)
        finally:
            cas._STATE.disabled = saved       # noqa: SLF001

    async def test_the_thread_tier_bound_is_the_one_actually_gating(self):
        """池关掉那一轮，闸门必须真的降到线程那一档（不是只改了那个查询函数）。"""
        from app.services.sources import compat_audit_service as cas
        from app.services.sources import source_check_service as shell
        compat = _CountingCompat()
        svc = await self._service(compat)
        saved = cas._STATE.disabled           # noqa: SLF001
        try:
            cas._STATE.disabled = True        # noqa: SLF001
            await svc._run_checks(_fanout_sources(self.N))
        finally:
            cas._STATE.disabled = saved       # noqa: SLF001
        self.assertEqual(compat.peak, shell.AUDIT_CONCURRENCY)
        self.assertEqual(len(compat.calls), self.N, "降档把审计限丢了")

    async def test_no_audit_is_still_running_when_the_run_returns(self):
        """收尾：`_run_checks` 返回时不该还有在跑的审计（占的是下一条检测的线程池）。"""
        compat = _CountingCompat(hold=0.03)
        svc = await self._service(compat)
        await svc._run_checks(_fanout_sources(self.N))
        self.assertEqual(compat.running, 0)
        self.assertEqual(len(svc._audit_tasks), 0,
                         "任务引用没被回收（done 回调没接上？）")

    async def test_each_run_builds_its_own_gate(self):
        """信号量必须每轮新建：它第一次 await 就认死当前循环，跨轮/跨循环复用会抛
        "bound to a different event loop"（服务重启、单测每条各起一条循环都会换循环）。
        """
        compat = _CountingCompat(hold=0.001)
        svc = await self._service(compat)
        await svc._run_checks(_fanout_sources(4))
        first = svc._audit_sem
        self.assertIsNotNone(first, "这一轮根本没建闸门")
        await svc._run_checks(_fanout_sources(4))
        self.assertIsNot(svc._audit_sem, first, "第二轮还在拿第一轮那把信号量")
        self.assertEqual(len(compat.calls), 8, "换闸门后审计断了一半")

    async def test_cancelling_the_run_also_stops_the_audit_tail(self):
        """停检测就得把排出去的审计一起收掉，不留"服务停了后台还在写 audit_log"。"""
        hold = asyncio.Event()

        async def stopping_checker(sources, concurrency=8, timeout=12,
                                   on_result=None, auto_disable=False,
                                   depth="full"):
            for i, source in enumerate(sources, 1):
                payload = {"idx": i, "name": source.bookSourceName,
                           "raw_url": source.bookSourceUrl,
                           "check_state": "fully_usable", "usable": True, "status": "ok"}
                if on_result:
                    on_result(payload, i, len(sources))
                await hold.wait()          # 第一条结果出来后整条流水线钉住
            return []

        compat = _CountingCompat(hold=0.02)
        svc = SourceCheckService(checker=stopping_checker, compat_audit=compat)
        task = asyncio.create_task(svc._run_checks(_fanout_sources(6)))
        await asyncio.sleep(0.005)         # 让审计任务排上去（并发正好等于上限）
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)         # 被取消的审计要跑完自己的 finally
        self.assertEqual(compat.running, 0, "取消后还有审计在跑")
        self.assertEqual(compat.calls, [], "取消后还有审计落到底（写进了 audit_log）")
        self.assertTrue(all(t.done() for t in svc._audit_tasks))


# --------------------------------------------------------------------------- #
# 1.5 大轮收尾把审计进程池关掉还内存（还早/还晚都算退化）
# --------------------------------------------------------------------------- #
class TestAuditPoolReap(unittest.IsolatedAsyncioTestCase):
    """关池这笔交换只对**大轮**做：早了会砍掉还在跑的审计，晚了白背 270 MB。

    护栏钉的是"没发生"（记忆 `guard-tests-assert-absence`）：小轮没发生关池、
    大轮的尾巴没被关池砍掉、计数没跨轮累加。计数用替身记账，不碰真池状态
    （`_STATE` 是模块级的，跨用例跨文件会互相污染）。
    """

    def setUp(self) -> None:
        from app.services.sources import compat_audit_service as cas
        from app.services.sources import source_check_service as shell
        self.shell = shell
        self.calls: list[dict] = []
        self._orig = cas.shutdown_audit_pool
        #: 关池那一刻的"还在跑的审计条数"由用例注入；-1 = 探针没接上
        self.running_probe = lambda: -1

        def recorder() -> None:
            # 快照"关池时审计跑完没有"——把关池搬到 drain 之前就会在这里露出来
            self.calls.append({"running": self.running_probe()})

        cas.shutdown_audit_pool = recorder                   # noqa: SLF001

    def tearDown(self) -> None:
        from app.services.sources import compat_audit_service as cas
        cas.shutdown_audit_pool = self._orig                 # noqa: SLF001

    async def _run(self, n: int, hold: float = 0.002):
        compat = _CountingCompat(hold=hold)
        svc = SourceCheckService(checker=_fanout_checker, compat_audit=compat)
        # (正在跑的审计, 还挂着引用的审计任务) —— 探针必须是**这两样都归零**才算收完
        self.running_probe = lambda: (compat.running, len(svc._audit_tasks))
        await svc._run_checks(_fanout_sources(n))
        return svc, compat

    async def test_a_big_round_closes_the_pool_after_the_audit_tail(self):
        """到阈值就关：而且必须等审计尾巴收完再关，不能把在跑的审计一起带走。

        hold 取 0.03 s、条数取阈值，就是让审计在收尾那一刻**还没跑完**——把关池
        搬到 `_drain_audits()` 之前，这里会照见 (在跑 > 0, 引用 > 0) 和少掉的条数。
        """
        n = self.shell.AUDIT_POOL_REAP_AFTER
        svc, compat = await self._run(n, hold=0.03)
        self.assertEqual(len(self.calls), 1,
                         f"审计 {n} 条这一轮没关池（花了 {n} 条的内存却没还回去）")
        self.assertEqual(self.calls[0]["running"], (0, 0),
                         f"关池时审计还没收完：{self.calls[0]['running']}")
        self.assertEqual(len(compat.calls), n, "审计被关池砍丢了几条")
        self.assertEqual(svc._audit_submitted, n)

    async def test_a_small_round_leaves_the_pool_warm(self):
        """单源/小批量不关：那 177~184 ms 的建池钱会算进下一次检测的收尾等待。"""
        for n in (1, 8, self.shell.AUDIT_POOL_REAP_AFTER - 1):
            self.calls.clear()
            await self._run(n)
            self.assertEqual(self.calls, [],
                             f"{n} 条的小轮把池关了，下一轮要赔一次建池")

    async def test_the_count_is_per_round_not_cumulative(self):
        """阈值读的是**本轮**排出去多少条：跨轮累加会让第二轮小轮也触发关池。"""
        n = self.shell.AUDIT_POOL_REAP_AFTER // 2
        svc, _ = await self._run(n)
        self.assertEqual(self.calls, [])
        await svc._run_checks(_fanout_sources(n))
        self.assertEqual(self.calls, [],
                         "计数没每轮复位，两轮小批量凑成了大轮（白赔一次建池）")
        self.assertEqual(svc._audit_submitted, n)


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
    """带一张 audit_log 表的内存库替身（离线落库测试）。

    两条面都要有：``.db`` 裸连接（list_logs 读用）、``execute``（写路径，
    StorageService 的持锁助手）。生产里裸 ``db.execute`` 落在 autocommit 连接上
    会并进别人的 transaction 一起回滚，见 TestCompatAuditPersistence。
    """

    def __init__(self):
        import sqlite3
        import threading
        # isolation_level=None 与生产同：autocommit，显式包事务。裸连接上的
        # 写会并进别人正开着的那笔事务——这条替身的语义必须照抄，否则护栏
        # 测的就不是生产的那个形状。
        self.db = sqlite3.connect(":memory:", check_same_thread=False,
                                 isolation_level=None)
        self._db_lock = threading.RLock()
        self.db.execute(
            "CREATE TABLE audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " source_id TEXT, missing_apis TEXT, ts TEXT)")
        #: 每次 execute() 落在哪条线程上（钉「写不在事件循环」）
        self.write_threads: list[int] = []

    def execute(self, sql, params=()):
        """StorageService.execute 的形状：持 _db_lock 再执行。"""
        import threading
        self.write_threads.append(threading.get_ident())
        with self._db_lock:
            return self.db.execute(sql, params)

    def transaction(self):
        """StorageService.transaction 的形状：握锁跨 BEGIN/COMMIT/ROLLBACK。"""
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            with self._db_lock:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    yield self.db
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
                self.db.execute("COMMIT")
        return _tx()

    def rows(self) -> list:
        return self.db.execute(
            "SELECT source_id, missing_apis, ts FROM audit_log").fetchall()


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


class TestCompatAuditWriteShape(unittest.IsolatedAsyncioTestCase):
    """audit_log 那条 INSERT 的**落库形状**护栏（§任务 #75，实测出来的两条）。

    生产里这里写的是 ``self._storage.db.execute(...)``——裸连接、不持
    ``StorageService._db_lock``。本机实测（本地临时库、离线，
    ``.probe/audit_log_write_shape.py``）这套形状有两件坏事：

    1. **丢行且不报错**。主库以 ``isolation_level=None`` 打开，别的线程正握着
       ``transaction()``（BEGIN IMMEDIATE 到 COMMIT 全程持锁）时，裸写不抢锁、
       不报错，直接**并进那笔未提交的事务**；对方一 ROLLBACK，审计行跟着没了，
       而调用方看到的是成功。同一格实测：回滚后表里剩 **0 行**，走持锁助手则剩
       1 行（写被挡在事务外面等，量到最长等锁 304 ms）。
    2. **并发裸写互相踩**。8 线程 × 240 条、3 遍同一张表：每遍至少抛一次
       ``InterfaceError: bad parameter or other API misuse``，三遍共活下
       **610/720 行**（丢 110）；持锁写 720/720。

    第 2 件的等锁代价（单条最大 91.5 ms）也解释了为什么写不能在事件循环线程上
    ——那正是触发点②编辑保存与③检测流水线共用的界面循环。
    """

    @staticmethod
    async def _wait(predicate, timeout: float = 5.0) -> bool:
        """在循环里等谓词——必须 await 着等，死等会把被等的那个任务饿死。"""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.005)
        return False

    async def test_audit_row_survives_another_threads_rollback(self):
        """别人的事务回滚，不能把审计行一起带走（钉"没丢"，不是钉"没报错"）。"""
        import threading

        storage = _FakeStorage()
        svc = CompatAuditService(storage=storage)
        started, release = threading.Event(), threading.Event()
        aborts = []

        def bulk_writer():
            # 一次整表镜像的形状：持锁跨 BEGIN..COMMIT，中途作废
            try:
                with storage.transaction():
                    started.set()
                    if not release.wait(5.0):
                        raise AssertionError("测试没放行")
                    raise RuntimeError("这批写作废")
            except RuntimeError as exc:
                aborts.append(exc)

        t = threading.Thread(target=bulk_writer, daemon=True)
        t.start()
        assert started.wait(5.0), "并发事务没开起来"

        task = asyncio.ensure_future(svc.audit_source(AUDIT_SRC))
        # 等到写确实**试过了**（替身在抢锁之前记账）——不然就是空转放行：
        # 事务可能先 COMMIT，丢行的形状就根本没被压到。
        assert await self._wait(lambda: len(storage.write_threads) == 1), \
            "审计写没经过 StorageService.execute（持锁助手），闸门空转"
        release.set()
        await task
        t.join(5.0)

        assert len(aborts) == 1, "并发事务没走到作废分支，这格没测到回滚"
        rows = storage.rows()
        assert [r[0] for r in rows] == ["https://audit.example"], \
            f"审计行被别人的事务回滚带走了（剩下 {rows}）"

    async def test_audit_write_does_not_run_on_the_event_loop_thread(self):
        """落库必须离开事件循环：等锁最长实测 91.5 ms，留在循环上就是界面卡死。"""
        import threading

        storage = _FakeStorage()
        svc = CompatAuditService(storage=storage)
        loop_thread = threading.get_ident()   # 协程体跑在循环线程上
        await svc.audit_source(AUDIT_SRC)

        # 反空转：写确实发生了（"不在循环上"不能靠"一次都没写"来成立）
        assert storage.write_threads, "审计写没落库，断言成了空转"
        assert loop_thread not in storage.write_threads, \
            "audit_log 的写落在事件循环线程上，等锁时界面会整块冻住"
        assert len(storage.rows()) == 1



# --------------------------------------------------------------------------- #
# 2b. RuleRepairService.chapter_candidates：自愈页选择器的数据源（全程不出网）
# --------------------------------------------------------------------------- #
class _ShelfSourceService:
    """只回答书架的 SourceService 替身。"""

    def __init__(self, records):
        self._records = records
        self.calls = 0

    async def list_bookshelf(self, include_staged: bool = False):
        self.calls += 1
        return list(self._records)


def _shelf(source_url: str, book_url: str, name: str = "《测试书》") -> dict:
    return {"source_url": source_url, "url": book_url, "name": name}


class _Clock:
    """假钟：每问一次走 ``step_ms`` 毫秒（候选面的时间预算只能这样钉）。

    用真 ``time.perf_counter`` 的话，这条用例红不红取决于机器当时多忙——
    那测的是调度器，不是预算。
    """

    def __init__(self, step_ms: float):
        self._now = 0.0
        self._step = step_ms / 1000.0

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


class ChapterCandidateTests(unittest.IsolatedAsyncioTestCase):
    """候选面只能读磁盘上已有的目录：一多出网就成了"点开列表页就抓站"。"""

    def setUp(self):
        import tempfile
        from app.services.reading_cache import ReadingCache
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cache = ReadingCache(self._dir.name)
        self.source_url = "https://www.po5.net"

    def _svc(self, records):
        return RuleRepairService(
            source_service=_ShelfSourceService(records), cache=self.cache)

    def _catalog(self, book_url, count=3, source_url=None, start=1):
        self.cache.save_catalog(source_url or self.source_url, book_url, [
            ChapterItem(name=f"第{i}章", url=f"{book_url}/{i}.html", index=i - 1)
            for i in range(start, start + count)])

    async def test_only_the_requested_source_and_only_readable_chapters(self):
        self._catalog("https://b/1", 3)
        self._catalog("https://b/other-source", 2, source_url="https://elsewhere/")
        # 卷标题没有地址、也没有地址的章：递给诊断只会白抓一次
        self.cache.save_catalog(self.source_url, "https://b/vol", [
            ChapterItem(name="第一卷", url="", is_volume=True),
            ChapterItem(name="空地址", url=""),
            ChapterItem(name="第二章", url="https://b/vol/2.html")])
        svc = self._svc([
            _shelf(self.source_url, "https://b/1"),
            _shelf("https://elsewhere/", "https://b/other-source"),
            _shelf(self.source_url, "https://b/vol"),
        ])
        groups = (await svc.chapter_candidates(self.source_url)).groups
        self.assertEqual([g["book_url"] for g in groups],
                         ["https://b/1", "https://b/vol"], "串了源或整组丢了")
        first = groups[0]["chapters"]
        self.assertEqual([c["url"] for c in first],
                         ["https://b/1/1.html", "https://b/1/2.html",
                          "https://b/1/3.html"])
        self.assertEqual([c["url"] for c in groups[1]["chapters"]],
                         ["https://b/vol/2.html"], "卷标题/空地址被端上了候选面")

    async def test_a_foreign_shelf_record_does_not_borrow_this_key(self):
        """源隔离要钉的是"这一组候选属于这一源"：地址撞车的别源书不算。

        缓存按 (源, 书) 分键，所以只要漏了源这一维，别源的书架记录就会借着
        同一本书的目录缓存端上来，诊断拿到的却是手上这个源的规则。
        """
        self._catalog("https://b/1", 2, source_url=self.source_url)
        svc = self._svc([_shelf("https://elsewhere/", "https://b/1")])
        face = await svc.chapter_candidates(self.source_url)
        self.assertEqual(face.groups, [],
                         "串了源：那是别源书架上的书，只是书地址相同")
        self.assertEqual((face.shelf_books, face.cached_books), (0, 0),
                         "普查把别源的书数进了这个源的账")

    async def test_no_cache_and_no_service_degrade_to_empty(self):
        """没读过 / 没装配 = 空表，不是异常：界面上由粘贴框接手。"""
        self._svc([])
        empty = self._svc([_shelf(self.source_url, "https://b/missing")])
        self.assertEqual((await empty.chapter_candidates(self.source_url)).groups,
                         [])
        self.assertEqual((await RuleRepairService().chapter_candidates(
            self.source_url)).groups, [], "没 SourceService 也不该炸")
        self.assertEqual((await self._svc([]).chapter_candidates("")).groups, [])

    async def test_local_pseudo_source_is_refused(self):
        """本地书有缓存目录却没有规则源：端出来只会起一趟抓 file:// 的空诊断。"""
        from app.services.content.local_book_service import LOCAL_BOOK_SOURCE_URL
        self._catalog("/tmp/某本书.txt", 2, source_url=LOCAL_BOOK_SOURCE_URL)
        svc = self._svc([_shelf(LOCAL_BOOK_SOURCE_URL, "/tmp/某本书.txt")])
        calls = svc._source_service
        self.assertEqual((await svc.chapter_candidates(
            LOCAL_BOOK_SOURCE_URL)).groups, [])
        self.assertEqual(calls.calls, 0, "连书架都不该去读")

    async def test_big_catalog_is_sampled_not_dumped(self):
        """整书几千章必须抽样下发：整页 diff 约 0.024 ms/行，五千条本身就是一次卡顿。"""
        self._catalog("https://b/big", 5000)
        svc = self._svc([_shelf(self.source_url, "https://b/big")])
        chapters = (await svc.chapter_candidates(
            self.source_url, per_book=100)).groups[0]["chapters"]
        self.assertLessEqual(len(chapters), 100)
        indexes = [int(url.rsplit("/", 1)[1].split(".")[0])
                   for url in (c["url"] for c in chapters)]
        self.assertEqual(indexes, sorted(indexes), "抽样打乱了目录顺序")
        # 抽样不许退化成"取前 100 章"：正文规则坏了的常常正是后面新出的章。
        self.assertGreater(indexes[-1], 5000 // 2, f"抽到的最深只到第 {indexes[-1]} 章")

    async def test_duplicate_shelf_records_are_merged(self):
        """同一本书两条书架记录（Legado 的 url/bookUrl 别名）：候选面只摊一份。"""
        for i in range(1, 5):
            self._catalog(f"https://b/{i}", 2)
        svc = self._svc([_shelf(self.source_url, f"https://b/{i}")
                         for i in range(1, 5)]
                        + [_shelf(self.source_url, "https://b/2")])
        face = await svc.chapter_candidates(self.source_url)
        self.assertEqual([g["book_url"] for g in face.groups],
                         [f"https://b/{i}" for i in range(1, 5)],
                         "重复的书架记录被摊了两份")
        self.assertEqual(face.shelf_books, 4, "普查把重复记录数成了两本书")

    async def test_five_books_each_get_a_face(self):
        """第 4、5 本不许静默消失。

        旧 shape 硬编"最近 3 本"：一个把同一源读了 5 本的人，在下拉里永远挑不到
        第 4 本之后的章，界面上也没有一行字说过这件事。**这条不许从常量取数**——
        上一版那么写，把上限改回 3 之后测试跟着改口，变异照样绿（第七十轮核验抓到
        的就是这一族）。
        """
        for i in range(1, 6):
            self._catalog(f"https://b/{i}", 80)
        svc = self._svc([_shelf(self.source_url, f"https://b/{i}")
                         for i in range(1, 6)])
        groups = (await svc.chapter_candidates(self.source_url)).groups
        self.assertEqual([g["book_url"] for g in groups],
                         [f"https://b/{i}" for i in range(1, 6)],
                         "只摊了前几本：后面的书在下拉里没了")

    async def test_row_budget_bounds_the_total_not_each_book(self):
        """行预算管的是**总行数**：8 本各摊满 100 章就是 640 行，那是一次卡顿。

        预算数字当场写死（300），不从服务模块读常量——读常量的写法在
        ``MAX_CANDIDATE_ROWS`` 被人改掉时会一起改口，等于没钉。
        """
        for i in range(1, 9):
            self._catalog(f"https://b/{i}", 80)
        svc = self._svc([_shelf(self.source_url, f"https://b/{i}")
                         for i in range(1, 9)])
        face = await svc.chapter_candidates(self.source_url, per_book=100,
                                            max_rows=300)
        groups = face.groups
        self.assertEqual(len(groups), 8, "本数没摊全")
        self.assertLessEqual(face.rows, 300, f"摊出 {face.rows} 行，超了行预算")
        self.assertEqual(face.rows, sum(len(g["chapters"]) for g in groups),
                         "普查报的行数和真摊出来的不是一回事")
        self.assertTrue(all(g["chapters"] for g in groups),
                        "名额全花在第一本上了，后面的书还是没摊到")
        # 每本抽到的章不许退化成"取前 N 章"：坏的常常正是后面新出的章
        for group in groups:
            numbers = [int(c["url"].rsplit("/", 1)[1].split(".")[0])
                       for c in group["chapters"]]
            self.assertEqual(numbers, sorted(numbers), "抽样打乱了目录顺序")
            self.assertGreater(numbers[-1], 40,
                               f"抽到的最深只到第 {numbers[-1]} 章")

    async def test_uncached_books_do_not_eat_the_scan_slots(self):
        """没缓存过的书不占预算：stat 普查（实测 0.014 ms/本）先把它们挡在翻目录之前。

        前面排的要**比默认预算摊得动的本数多**，否则"先切前 N 本"那种写法照样能翻到
        缓存那本，变异就绿了（第七十轮 N9 抓到的正是这一条没钉住）。
        """
        self._catalog("https://b/cached", 4)
        svc = self._svc([_shelf(self.source_url, f"https://b/miss{i}")
                         for i in range(10)]
                        + [_shelf(self.source_url, "https://b/cached")])
        face = await svc.chapter_candidates(self.source_url)
        self.assertEqual([g["book_url"] for g in face.groups], ["https://b/cached"],
                         "前面的空书把预算吃光了：这一源的样本明明就在后面那本")
        self.assertEqual((face.shelf_books, face.cached_books, face.skipped_books),
                         (11, 1, 0), "普查把没存过目录的书算成了欠账")

    async def test_small_books_all_fit_under_a_time_budget(self):
        """40 本 × 100 章一共十几毫秒：按**本数**钉上限会把它们一律砍到 8 本。

        实测单本 100 章 0.43 ms、5000 章 25.7 ms（`.probe/out_catalog_concurrency.txt`），
        差 60 倍，所以这一档预算只能按时间给。钟是注入的假钟（每问一次走 1 ms），
        不然这条会在机器负载高时红——那测的是调度器，不是预算。
        """
        for i in range(40):
            self._catalog(f"https://b/{i}", 100)
        svc = self._svc([_shelf(self.source_url, f"https://b/{i}")
                         for i in range(40)])
        face = await svc.chapter_candidates(self.source_url, max_scan_ms=200,
                                            clock=_Clock(1.0))
        self.assertEqual(face.shown_books, 40, "小书被本数上限砍了")
        self.assertEqual(face.skipped_books, 0)
        self.assertLessEqual(face.rows, 300, "摊全了本数就不管行数了")

    async def test_the_time_budget_stops_and_reports_who_is_left_out(self):
        """预算到点就停，而且**停下来的人要报出来**：静默截断就是第七十轮的病。

        假钟每问一次走 100 ms、预算 200 ms、五本都有目录 → 第 4 本开读前超时。
        数字全写死，不从模块常量取。
        """
        for i in range(1, 6):
            self._catalog(f"https://b/{i}", 20)
        svc = self._svc([_shelf(self.source_url, f"https://b/{i}")
                         for i in range(1, 6)])
        face = await svc.chapter_candidates(self.source_url, max_scan_ms=200,
                                            clock=_Clock(100.0))
        self.assertEqual([g["book_url"] for g in face.groups],
                         [f"https://b/{i}" for i in (1, 2, 3)],
                         "预算没生效，或者停的位置不对")
        self.assertEqual((face.shelf_books, face.cached_books, face.shown_books,
                          face.skipped_books), (5, 5, 3, 2),
                         "普查没承认'存过目录但没摊到'那一截")

    async def test_the_first_book_is_never_dropped_by_a_zero_budget(self):
        """预算给 0 也只停"第二本起"：一本都没摊出来的候选面等于把用户推回手填框。"""
        self._catalog("https://b/1", 3)
        self._catalog("https://b/2", 3)
        svc = self._svc([_shelf(self.source_url, "https://b/1"),
                         _shelf(self.source_url, "https://b/2")])
        face = await svc.chapter_candidates(self.source_url, max_scan_ms=0,
                                            clock=_Clock(100.0))
        self.assertEqual([g["book_url"] for g in face.groups], ["https://b/1"])
        self.assertEqual(face.skipped_books, 1)


class CheckSnapshotCarriesSampleChapters(unittest.TestCase):
    """检测快照必须把抽到的样本章留下来——那是"检过但没读过"的源唯一的章节来源。"""

    def _svc(self):
        svc = SourceCheckService()
        svc.bus = RecordingBus()
        return svc

    def test_snapshot_entry_has_them_and_the_event_still_builds(self):
        svc = self._svc()
        run = {"results": []}
        svc._publish_finished(
            {"raw_url": "https://a/", "check_state": "fully_usable",
             "sample_chapter_urls": ("https://a/1.html", None, "https://a/3.html")},
            run=run, done=1, total=9)
        self.assertEqual(run["results"][0]["sample_chapter_urls"],
                         ["https://a/1.html", "https://a/3.html"])
        # 事件那一面不许跟着加键：CheckFinishedEvent 只有五个字段，多一个就是 TypeError
        event = svc.bus.events[0]
        self.assertIsInstance(event, CheckFinishedEvent)
        self.assertFalse(hasattr(event, "sample_chapter_urls"))

    def test_source_without_samples_gets_an_empty_list(self):
        svc = self._svc()
        run = {"results": []}
        svc._publish_finished({"raw_url": "https://a/", "check_state": "blocked"},
                              run=run, done=1, total=1)
        self.assertEqual(run["results"][0]["sample_chapter_urls"], [])
