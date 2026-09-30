"""分级检测（depth=full/list）的护栏：钉住"哪一段没试"和"没试不等于试了失败"。

取证见 `.probe/check_tier_depth.py`（全程离线：读 resource/ 快照 + 127.0.0.1 假源）：
* 阅读链是唯一会翻倍的段——16 条健康源上占两段规则耗时 **74.7%**，每源 5 次 HTTP
  里 3 次是它发的；
* 主站连通**不能**当硬门——真库 917 条里 **61 条**（6.7%）searchUrl 与 bookSourceUrl
  不同主机、**13 条** bookSourceUrl 连 http(s) 都不是；同探针里"主站拒连 + 搜索正文
  全通"的源现判 fully_usable，加硬门直接把它打死。

所以这一轮加的是**深度**（要不要验正文），不是**门**（主站不通就不往下）。
按 `guard-tests-assert-absence`：快速深度的护栏钉的是"阅读链一次都没被派发"，
记账替身记录的是**调用**而不是返回值。
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import flet as ft

from app.models.book_source import BookSource
from app.services import check_service
from app.services.sources.source_check_service import SourceCheckService
from app.core.events import CheckFinishedEvent


def _source(**overrides):
    values = {
        "bookSourceName": "分级源", "bookSourceUrl": "https://example.com",
        "searchUrl": "/search?key={{key}}", "ruleSearch": {}, "ruleExplore": {},
        "ruleBookInfo": {}, "ruleToc": {}, "ruleContent": {}, "header": "",
        "enabled": True, "enabledExplore": False, "exploreUrl": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _book(index: int = 0) -> SimpleNamespace:
    return SimpleNamespace(name=f"书{index}", url=f"https://example.com/book/{index}")


def _good_document() -> SimpleNamespace:
    return SimpleNamespace(blocks=[SimpleNamespace(kind="text", value="正文")],
                           raw_content="正文")


async def _noop_aclose():
    pass


class _ChainSpy:
    """阅读链记账替身：记录**被派发了几次**，快速深度下必须是 0。"""

    def __init__(self):
        self.calls = 0

    async def __call__(self, source, engine, candidates, timeout, budget=None):
        self.calls += 1
        chapter = SimpleNamespace(name="章", url="https://example.com/ch",
                                  is_volume=False)
        return {"reading_status": "ok", "reading_stage": "complete",
                "reading_error": None, "chapter_count": 1,
                "sample_book": source.bookSourceName, "sample_chapter": "章",
                "sample_chapter_urls": [chapter.url], "reading_tried": 1,
                "reading_check_skipped": None}


class PipelineDepthTests(unittest.IsolatedAsyncioTestCase):
    """check_sources 的深度分支：流水线面证据，不碰界面。"""

    def _harness(self, chain, search_status="ok", book_count=1, probe=None):
        """把三段替身装好；``chain=None`` 表示阅读链不打桩（跑真函数）。"""

        async def fake_search(source, engine, timeout):
            books = [_book(i) for i in range(book_count)] \
                if search_status == "ok" else []
            return ({"search_status": search_status, "book_count": book_count,
                     "search_keyword": "我的", "search_method": "rule-engine",
                     "search_error": None}, books)

        async def fake_probe(*args, **kwargs):
            return dict(probe or {"status": "ok", "status_code": 200,
                                  "response_time_ms": 1.0, "error": None})

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)
                self.aclose = _noop_aclose
                self.book_info = None

        patches = [
            patch.object(check_service, "_engine_search_check", fake_search),
            patch.object(check_service, "_probe_source", fake_probe),
            patch.object(check_service, "LegadoReadingEngine", FakeEngine),
        ]
        if chain is not None:
            patches.append(patch.object(check_service,
                                        "_reading_chain_check", chain))
        for item in patches:
            item.start()
        return lambda: [item.stop() for item in reversed(patches)]

    async def test_list_depth_dispatches_the_reading_chain_zero_times(self):
        chain = _ChainSpy()
        undo = self._harness(chain)
        try:
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1, depth="list")
        finally:
            undo()
        self.assertEqual(0, chain.calls, "快速深度一条阅读链请求都不该派")
        self.assertEqual("list", results[0]["reading_check_skipped"])
        self.assertEqual("skip", results[0]["reading_status"])
        self.assertEqual(0, results[0]["reading_tried"])

    async def test_full_depth_still_runs_the_chain(self):
        """对照臂：默认深度照常跑。没有它，"永远跳过"也能过上一条。"""
        chain = _ChainSpy()
        undo = self._harness(chain)
        try:
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1)
        finally:
            undo()
        self.assertEqual(1, chain.calls)
        self.assertIsNone(results[0]["reading_check_skipped"])
        self.assertEqual("fully_usable", results[0]["check_state"])

    async def test_list_depth_stops_at_the_list_verdict_not_a_failure(self):
        """搜索通、正文没验：结论停在列表那一级，不是 content_unavailable。"""
        chain = _ChainSpy()
        undo = self._harness(chain)
        try:
            results = await check_service.check_sources(
                [_source()], concurrency=1, timeout=1, depth="list")
        finally:
            undo()
        result = results[0]
        self.assertEqual("search_only", result["check_state"])
        self.assertTrue(result["usable"])
        self.assertNotIn("content", str(result["check_state"]))

    async def test_a_blocked_probe_is_a_different_kind_of_not_attempted(self):
        """两种"没试"必须能分开：被主站拦 vs 本轮只验列表。"""
        chain = _ChainSpy()
        undo = self._harness(
            chain, probe={"status": "blocked", "status_code": 200,
                          "response_time_ms": 1.0,
                          "error": "本机安全软件已拦截该站点（火绒）"})
        try:
            blocked = (await check_service.check_sources(
                [_source()], concurrency=1, timeout=1))[0]
        finally:
            undo()
        self.assertEqual("probe_blocked", blocked["reading_check_skipped"])
        self.assertEqual("blocked", blocked["check_state"])
        self.assertEqual(0, chain.calls)

    async def test_zero_candidates_are_recorded_as_not_attempted(self):
        """搜索没给出可点的书 ≠ 正文链路坏了：reason 落在 no_candidates。

        这一条**不**打桩阅读链：打了桩就走不到"没候选"那个分支，测的是替身。
        """
        undo = self._harness(None, search_status="no_results", book_count=0)
        try:
            result = (await check_service.check_sources(
                [_source()], concurrency=1, timeout=1))[0]
        finally:
            undo()
        self.assertEqual("no_candidates", result["reading_check_skipped"])
        self.assertEqual("skip", result["reading_status"])

    async def test_a_failed_chain_is_not_recorded_as_skipped(self):
        """试了、坏了：skipped 仍是 None——"没试"和"试了失败"不能糊成同一个结论。"""
        async def book_info(source, book):
            raise RuntimeError("正文规则解析失败")

        engine = SimpleNamespace(book_info=book_info)
        result = await check_service._reading_chain_check(
            _source(), engine,
            [SimpleNamespace(name="书", url="https://example.com/book")],
            timeout=1)
        self.assertIsNone(result["reading_check_skipped"])
        self.assertEqual("rule_error", result["reading_status"])
        self.assertEqual(1, result["reading_tried"])

    async def test_an_unknown_depth_is_refused_not_silently_downgraded(self):
        """打错一个字母就悄悄跳过正文，等于整轮结论都变松——必须当场抛。"""
        with self.assertRaises(ValueError):
            await check_service.check_sources([_source()], concurrency=1,
                                              timeout=1, depth="lst")

    async def test_list_depth_auto_disable_reason_does_not_claim_the_chain_failed(self):
        """快速深度没验正文，停用理由不能写"阅读链路不可用"。"""
        async def fake_search(source, engine, timeout):
            return ({"search_status": "rule_error", "book_count": None,
                     "search_keyword": "我的", "search_method": "rule-engine",
                     "search_error": "规则解析失败"}, [])

        async def fake_probe(*args, **kwargs):
            return {"status": "ok", "status_code": 200,
                    "response_time_ms": 1.0, "error": None}

        class FakeEngine:
            def __init__(self, **kwargs):
                self.js = SimpleNamespace(close=lambda: None)
                self.aclose = _noop_aclose

        src = _source(enabled=True)
        with patch.object(check_service, "_engine_search_check", fake_search), \
                patch.object(check_service, "_probe_source", fake_probe), \
                patch.object(check_service, "_reading_chain_check", _ChainSpy()), \
                patch.object(check_service, "LegadoReadingEngine", FakeEngine):
            result = (await check_service.check_sources(
                [src], concurrency=1, timeout=1, depth="list",
                auto_disable=True))[0]
        self.assertTrue(result["auto_disabled"], "两条列表段都明确坏了，该停用")
        self.assertNotIn("阅读", result["disable_reason"])
        self.assertEqual("unusable", result["check_state"])


class _RecordingChecker:
    """服务外壳那侧的替身：记下转下来的 depth。"""

    def __init__(self, results=None):
        self.depths = []
        self.results = results

    async def __call__(self, sources, concurrency=8, timeout=12, on_result=None,
                       auto_disable=False, depth="full"):
        self.depths.append(depth)
        results = self.results if self.results is not None else [
            {"idx": i, "name": s.bookSourceName, "raw_url": s.bookSourceUrl,
             "check_state": "search_only", "usable": True, "status": "ok",
             "reading_check_skipped": depth if depth == "list" else None}
            for i, s in enumerate(sources, 1)]
        for i, r in enumerate(results, 1):
            if on_result:
                on_result(r, i, len(results))
        return results


def _two_sources():
    return SimpleNamespace(sources=[
        BookSource.from_dict({"bookSourceUrl": "https://a.example", "bookSourceName": "甲"}),
        BookSource.from_dict({"bookSourceUrl": "https://b.example", "bookSourceName": "乙"})])


class ServiceDepthForwardingTests(unittest.IsolatedAsyncioTestCase):
    """服务外壳：深度要真的转下去，快照要带回界面用的是哪一档。"""

    async def test_check_all_forwards_the_depth_to_the_pipeline(self):
        checker = _RecordingChecker()
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        await svc.check_all(depth="list")
        self.assertEqual(["list"], checker.depths)

    async def test_default_stays_full_when_the_caller_says_nothing(self):
        checker = _RecordingChecker()
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        await svc.check_all()
        self.assertEqual(["full"], checker.depths)

    async def test_the_run_snapshot_carries_the_depth_of_that_round(self):
        checker = _RecordingChecker()
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        await svc.check_all(depth="list")
        snap = await svc.run_snapshot()
        self.assertEqual("list", snap["depth"])

    async def test_single_source_check_forwards_the_depth_too(self):
        checker = _RecordingChecker(results=[
            {"idx": 1, "name": "甲", "raw_url": "https://a.example",
             "check_state": "search_only", "usable": True,
             "reading_check_skipped": "list"}])
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        await svc.check_source({"bookSourceUrl": "https://a.example",
                                "bookSourceName": "甲"}, depth="list")
        self.assertEqual(["list"], checker.depths)

    async def test_the_summary_says_the_prose_was_not_checked(self):
        """结论行不能只说"仅搜索可用"——用户会把"本轮没验"读成"源有毛病"。"""
        bus_events = []

        class Bus:
            def publish(self, event):
                bus_events.append(event)
                return []

        checker = _RecordingChecker()
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        svc.bus = Bus()
        await svc.check_all(depth="list")
        self.assertTrue(all("未验正文" in ev.summary for ev in bus_events),
                        [ev.summary for ev in bus_events])

    async def test_a_full_round_summary_does_not_claim_it_was_skipped(self):
        bus_events = []

        class Bus:
            def publish(self, event):
                bus_events.append(event)
                return []

        checker = _RecordingChecker(results=[
            {"idx": 1, "name": "甲", "raw_url": "https://a.example",
             "check_state": "fully_usable", "usable": True,
             "reading_status": "ok"},
            {"idx": 2, "name": "乙", "raw_url": "https://b.example",
             "check_state": "fully_usable", "usable": True,
             "reading_status": "ok"}])
        svc = SourceCheckService(checker=checker, source_service=_two_sources())
        svc.bus = Bus()
        await svc.check_all()
        self.assertEqual(2, len(bus_events))
        self.assertTrue(all("未验正文" not in ev.summary for ev in bus_events),
                        [ev.summary for ev in bus_events])


class _FakeHub:
    def __init__(self, service):
        self._service = service
        self.bus = None

    def get(self, stype):
        return self._service


class _FakeCheckService:
    """SourceCheckService 替身：只记 check_all 收到哪一档深度。"""

    def __init__(self, snapshot: dict | None = None):
        self.checks: list = []
        self.snapshot = snapshot or {"running": False, "cancelled": False,
                                     "total": 0, "summary": None, "results": []}

    async def check_all(self, groups=None, depth="full") -> dict:
        self.checks.append((groups, depth))
        return {"total": 0, "usable": 0, "blocked": 0}

    async def run_snapshot(self) -> dict:
        return self.snapshot

    async def await_run_finished(self) -> dict:
        return self.snapshot

    def cancel(self) -> None:
        pass


class _RecordingCheckView:
    """CheckView 替身：记界面收到的深度（回放要把下拉回到那一档）。"""

    def __init__(self):
        self.depth = "full"
        self.depths_shown: list = []
        self.summaries: list = []
        self.running: list = []

    def set_depth(self, depth):
        self.depths_shown.append(depth)
        self.depth = depth

    def set_running(self, running, text=""):
        self.running.append(running)
        if text:
            self.summaries.append(text)

    def set_summary(self, text):
        self.summaries.append(text)

    def set_progress(self, done, total):
        pass

    def render_result(self, source_id, state_class, summary):
        pass


class ControllerDepthTests(unittest.IsolatedAsyncioTestCase):
    """界面面：下拉那一档 → check_all 的 depth → 回放的 set_depth。"""

    def _attach(self, service=None):
        from app.controllers.source.check_controller import CheckController
        service = service or _FakeCheckService()
        view = _RecordingCheckView()
        controller = CheckController(_FakeHub(service))   # type: ignore[arg-type]
        scheduled: list = []
        controller.ui_call = scheduled.append             # type: ignore[assignment]
        controller.attach(view)
        return controller, view, service, scheduled

    async def test_the_selected_depth_reaches_the_service(self) -> None:
        controller, _view, service, scheduled = self._attach()
        await scheduled[0]                    # attach 的回放
        controller.start("list")
        await scheduled[-1]
        self.assertEqual([(None, "list")], service.checks)

    async def test_start_without_a_depth_stays_full(self) -> None:
        """默认必须是完整检测：看不见下拉的调用方不该悄悄少验一段。"""
        controller, _view, service, scheduled = self._attach()
        await scheduled[0]
        controller.start()
        await scheduled[-1]
        self.assertEqual([(None, "full")], service.checks)

    async def test_a_round_started_elsewhere_restores_the_picker(self) -> None:
        """别人启动的那轮是快速：回来下拉得停在"快速"，别显示成完整。"""
        service = _FakeCheckService({
            "running": False, "cancelled": False, "total": 2, "depth": "list",
            "summary": {"total": 2, "usable": 2, "blocked": 0}, "results": []})
        controller, view, _svc, scheduled = self._attach(service)
        await scheduled[0]
        self.assertEqual(["list"], view.depths_shown)
        self.assertIn("快速检测，未验正文", view.summaries[-1])


class ViewDepthControlTests(unittest.TestCase):
    """视图面：下拉的两个档位都得能落到 on_start 的参数上。"""

    @staticmethod
    def _toolbar(view):
        return view.controls[1].controls

    def _fire_start(self, view, chosen: str) -> list:
        seen: list = []
        view.on_start = lambda depth=None: seen.append(depth)
        view.depth = chosen
        # 按钮文本在 .content 不在 .value（Flet 0.86），按类型认更稳。
        start = next(c for c in self._toolbar(view)
                     if isinstance(c, ft.FilledButton))
        start.on_click(None)
        return seen

    def test_the_picked_depth_is_handed_to_on_start(self) -> None:
        from app.views.source.check_view import CheckView
        self.assertEqual(["list"], self._fire_start(CheckView(), "list"))
        self.assertEqual(["full"], self._fire_start(CheckView(), "full"))

    def test_the_picker_offers_exactly_the_two_pipeline_depths(self) -> None:
        """下拉里的 key 必须就是 check_service 认的那两个值（打错=整轮少验一段）。"""
        from app.views.source.check_view import CheckView
        picker = next(c for c in self._toolbar(CheckView())
                      if isinstance(c, ft.Dropdown))
        self.assertEqual({check_service.CHECK_DEPTH_FULL,
                          check_service.CHECK_DEPTH_LIST},
                         {option.key for option in picker.options})

    def test_running_disables_the_picker(self) -> None:
        """跑起来改深度不会立刻生效，禁用比让人以为能改更诚实。"""
        from app.views.source.check_view import CheckView
        view = CheckView()
        picker = next(c for c in self._toolbar(view) if isinstance(c, ft.Dropdown))
        self.assertFalse(picker.disabled)
        view.set_running(True)
        self.assertTrue(picker.disabled)
        view.set_running(False)
        self.assertFalse(picker.disabled)


if __name__ == "__main__":
    unittest.main()
