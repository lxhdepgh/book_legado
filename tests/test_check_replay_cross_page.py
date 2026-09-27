"""书源检测跨页续跑（本轮修复）。

用户报的现场：开始检测后切到别的分组再切回来，进度条空了、结果行也没了，
像整轮检测被切页停掉。实际流水线一直在引擎循环上跑，断的是**渲染**：
``detach()`` 退了 ``CheckFinishedEvent`` 订阅，那期间发布的事件没人接。

修在两头：服务留一份本轮可回放快照（``run_snapshot``），控制器 attach 时回放
并接上收尾（``CheckController._restore``）。本文件两侧都盯：替身视图盯渲染，
真服务盯快照内容。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.core.events import CheckFinishedEvent
from app.models.book_source import BookSource
from app.controllers.source.check_controller import CheckController
from app.services.sources.source_check_service import SourceCheckService


class _FakeService:
    """够用的检测服务替身：只回快照、能等收尾，不跑真流水线。"""

    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.done = asyncio.Event()
        self.checks: list = []

    async def run_snapshot(self) -> dict:
        return self.snapshot

    async def await_run_finished(self) -> dict:
        await self.done.wait()
        return self.snapshot

    async def check_all(self, groups=None) -> dict:
        self.checks.append(groups)
        return {"total": 0, "usable": 0, "blocked": 0}

    def cancel(self) -> None:
        self.done.set()


class _FakeHub:
    def __init__(self, service):
        self._service = service
        self.bus = None

    def get(self, stype):
        return self._service


class _RecordingView:
    """够用的 CheckView 替身：只记渲染动作。"""

    def __init__(self):
        self.results: list = []
        self.progress: list = []
        self.running: list = []
        self.summaries: list = []

    def render_result(self, source_id, state_class, summary):
        self.results.append((source_id, state_class, summary))

    def set_progress(self, done, total):
        self.progress.append((done, total))

    def set_running(self, running, text=""):
        self.running.append(running)
        if text:
            self.summaries.append(text)

    def set_summary(self, text):
        self.summaries.append(text)


_RUNNING = {"running": True, "cancelled": False, "total": 4, "summary": None,
            "results": [{"source_id": "s1", "state_class": "fully_usable",
                         "summary": "甲：fully_usable"},
                        {"source_id": "s2", "state_class": "blocked",
                         "summary": "乙：blocked"}]}
_DONE = {"running": False, "cancelled": False, "total": 4,
         "summary": {"total": 4, "usable": 3, "blocked": 1}, "results": []}
_IDLE = {"running": False, "cancelled": False, "total": 0, "summary": None,
         "results": []}


async def _settle(rounds: int = 6) -> None:
    """放跑编排协程：回放要好几个 yield 才走到渲染。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _attach(service):
    """装配控制器，并把 ``ui_call`` 换成登记表（返回 controller/view/scheduled）。

    真环境里 attach 把回放松进**界面循环**；测试没有界面循环，它会退到真实
    EngineHub 守护线程上，替身那个 ``asyncio.Event`` 就跨循环了。所以这里只登记
    协程、由用例自己跑——顺带盯住「attach 到底有没有发起回放」这条接线。
    """
    view = _RecordingView()
    controller = CheckController(_FakeHub(service))   # type: ignore[arg-type]
    scheduled: list = []
    controller.ui_call = scheduled.append             # type: ignore[assignment]
    controller.attach(view)
    return controller, view, scheduled


def _replay(scheduled: list):
    """把 attach 登记下的回放跑起来（在跑的那轮会挂在等收尾上，故返回 task）。"""
    assert len(scheduled) == 1, f"attach 应发起一次回放，实际 {len(scheduled)} 次"
    return asyncio.create_task(scheduled[0])


class CheckReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_replays_in_flight_progress(self) -> None:
        """切回来就该有切走前的进度：逐源结果 + done/total + 仍在跑。"""
        controller, view, scheduled = _attach(_FakeService(_RUNNING))
        replay = _replay(scheduled)
        await _settle()
        self.assertEqual(view.results, [("s1", "fully_usable", "甲：fully_usable"),
                                        ("s2", "blocked", "乙：blocked")])
        self.assertEqual(view.progress[-1], (2, 4))
        self.assertTrue(view.running[-1], "服务在跑时开始按钮该保持禁用")
        replay.cancel()

    async def test_attach_replays_finished_round_summary(self) -> None:
        """切页期间跑完的：回来直接是收尾结论，不再挂「检测中」。"""
        controller, view, scheduled = _attach(_FakeService(_DONE))
        await scheduled[0]
        self.assertFalse(view.running[-1])
        self.assertIn("可用 3/4", view.summaries[-1])

    async def test_waits_for_the_run_it_did_not_start(self) -> None:
        """这一轮不是本页签启动的：等它收尾后仍要把结论画出来。"""
        service = _FakeService(dict(_RUNNING))
        controller, view, scheduled = _attach(service)
        replay = _replay(scheduled)
        await _settle(3)
        self.assertTrue(view.running[-1])
        service.snapshot = _DONE
        service.done.set()
        await replay
        self.assertFalse(view.running[-1])
        self.assertIn("可用 3/4", view.summaries[-1])

    async def test_start_while_running_does_not_dispatch_second_scan(self) -> None:
        """在跑时点「开始检测」不再派第二遍全量联网。"""
        service = _FakeService(_RUNNING)
        controller, view, scheduled = _attach(service)
        replay = _replay(scheduled)
        await _settle()
        controller.start()
        self.assertEqual(service.checks, [])
        self.assertIn("检测已在进行中", view.summaries)
        replay.cancel()

    async def test_live_events_use_snapshot_total(self) -> None:
        """在途事件推进度：分母取本轮快照的 total，不再从 0-1 比例反推。"""
        controller, view, scheduled = _attach(_FakeService(_RUNNING))
        replay = _replay(scheduled)
        await _settle()
        await controller._on_check_finished(CheckFinishedEvent(
            source_id="s3", state_class="unknown", summary="s3"))
        await controller._on_check_finished(CheckFinishedEvent(
            source_id="s4", state_class="unknown", summary="s4"))
        self.assertEqual(view.progress[-1], (4, 4))
        replay.cancel()

    async def test_never_checked_page_renders_nothing(self) -> None:
        """没检测过的空快照不该回放：初始的「尚未检测」不能被刷成结束态。"""
        controller, view, scheduled = _attach(_FakeService(_IDLE))
        await scheduled[0]
        self.assertEqual(view.results, [])
        self.assertEqual(view.progress, [])
        self.assertEqual(view.running, [], "空回放不该改动按钮运行态")

    async def test_replay_does_not_clear_a_run_just_started(self) -> None:
        """attach 的回放排在界面队列尾部，可能晚于用户点「开始」：
        那一刻快照还是**上一轮**的收尾，回放它会把刚开始的运行态刷回去。"""
        controller, view, scheduled = _attach(_FakeService(_DONE))
        controller.start()                    # 真实点击同步就落了运行态
        self.assertEqual(len(scheduled), 2, "回放 + 本轮检测各排一次")
        await scheduled[0]                    # 后松手的回放
        self.assertTrue(view.running[-1], "旧快照回放不该解禁开始按钮")
        self.assertEqual(view.results, [], "旧一轮的结果行不该盖到新一轮上")
        self.assertNotIn("可用 3/4", view.summaries)
        await scheduled[1]                    # 本派的那一轮（替身立即返回）


def _two_sources() -> SimpleNamespace:
    return SimpleNamespace(sources=[
        BookSource.from_dict({"bookSourceUrl": "https://a.example",
                              "bookSourceName": "甲"}),
        BookSource.from_dict({"bookSourceUrl": "https://b.example",
                              "bookSourceName": "乙"})])


def _result(i: int, state: str = "fully_usable") -> dict:
    return {"idx": i + 1, "raw_url": f"https://{chr(97 + i)}.example",
            "name": f"源{i}", "check_state": state, "usable": state != "blocked"}


class SourceCheckSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """快照本身：真服务 + 假 checker，盯记录 / 收尾 / 取消三条路径。"""

    def _service(self, checker) -> SourceCheckService:
        return SourceCheckService(checker=checker, source_service=_two_sources())

    async def test_snapshot_carries_results_total_and_summary(self) -> None:
        async def fake(sources, **kw):
            results = [_result(i) for i in range(len(sources))]
            for i, r in enumerate(results):
                kw["on_result"](r, i + 1, len(sources))
            return results

        svc = self._service(fake)
        await svc.check_all()
        snap = await svc.run_snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["total"], 2)
        self.assertEqual([e["source_id"] for e in snap["results"]],
                         ["https://a.example", "https://b.example"])
        self.assertEqual(snap["summary"]["usable"], 2)

    async def test_snapshot_advances_while_running(self) -> None:
        """跑到一半就能被回放出「几/总」，不用等收尾。"""
        release = asyncio.Event()

        async def slow(sources, **kw):
            for i in range(len(sources)):
                kw["on_result"](_result(i), i + 1, len(sources))
            await release.wait()
            return []

        svc = self._service(slow)
        run = asyncio.create_task(svc.check_all())
        for _ in range(20):
            snap = await svc.run_snapshot()
            if snap["results"]:
                break
            await asyncio.sleep(0)
        self.assertTrue(snap["running"])
        self.assertEqual((len(snap["results"]), snap["total"]), (2, 2))
        release.set()
        await run

    async def test_await_run_finished_returns_the_finished_round(self) -> None:
        """后来者等收尾：等到的那份快照必须是跑完的那份（含 summary）。"""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hanging(sources, **kw):
            entered.set()
            await release.wait()
            return [_result(i, "list_usable") for i in range(len(sources))]

        svc = self._service(hanging)
        run = asyncio.create_task(svc.check_all())
        await entered.wait()
        waiter = asyncio.create_task(svc.await_run_finished())
        await asyncio.sleep(0)
        release.set()
        await run
        snap = await waiter
        self.assertFalse(snap["running"])
        self.assertEqual(snap["summary"]["total"], 2)

    async def test_single_source_check_does_not_clobber_batch_log(self) -> None:
        """单源检测不覆盖批量回放（否则回到检测页会看到 1/1）。"""
        async def fake(sources, **kw):
            return [_result(i) for i in range(len(sources))]

        svc = self._service(fake)
        await svc.check_all()
        before = await svc.run_snapshot()
        await svc.check_source({"bookSourceUrl": "https://one.example"})
        after = await svc.run_snapshot()
        self.assertEqual(after["total"], before["total"])

    async def test_cancelled_run_is_marked(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hanging(sources, **kw):
            entered.set()
            await release.wait()
            return []

        svc = self._service(hanging)
        run = asyncio.create_task(svc.check_all())
        await entered.wait()
        svc.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        snap = await svc.run_snapshot()
        self.assertTrue(snap["cancelled"])
        self.assertFalse(snap["running"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
