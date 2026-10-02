"""护栏：静态审计的进程池（第六十三轮）。

要钉住的全是"没发生"的事（记忆 `guard-tests-assert-absence`）：

1. 审计**真的**离开了本进程——不是悄悄退回线程池还报成功；
2. 池建不起来时退回线程池，且**不再反复试**（每试一次都要赔建池那笔钱）；
3. 子进程被打穿时**不重跑那条毒源**（换进新池只会再带走一个 worker；退回线程池跑
   等于把原生 abort 请回主进程），而是返回一份 `not_audited` 报告；
4. 打穿太多次就永久退回，不留一个"每条源崩一次"的循环；
5. 停机把池关掉，不留孤儿 worker；关了之后还能按需重建；
6. `not_audited` 会走到用户眼前（导入面 warning / 编辑器审计栏），不是只写进对象。

替身一律是**真 Executor 的子类**（`concurrent.futures.ThreadPoolExecutor`）：
`asyncio.run_in_executor` 走的就是 `submit` + `wrap_future`，自造一个只有 submit 的
对象量到的是探针自己的形状。只有最后一栏真 spawn，为的是证明生产那条路
（Windows spawn + pickle `_audit_one` + 子进程 import）在本机走得通。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import inspect
import unittest
from concurrent.futures.process import BrokenProcessPool

from app.services.sources import compat_audit_service as cas
from app.services.sources.compat_audit_service import AuditReport

#: 一条带缺口的源：JS 语法错 + 未知字段，好让「审出来了」和「没审」能分开断言。
POISON = {"bookSourceUrl": "https://poison.example",
          "ruleSearch": {"bookList": "ul li", "notAField": "x",
                         "name": "@js:this is not js(("}}
CLEAN = {"bookSourceUrl": "https://clean.example",
         "ruleSearch": {"bookList": "ul li", "name": "a@text"}}


class _FakePool(concurrent.futures.ThreadPoolExecutor):
    """记账版真 Executor：只数 submit / shutdown，其余行为与线程池一致。"""

    def __init__(self, fail_submit: Exception | None = None) -> None:
        super().__init__(max_workers=2)
        self.submits = 0
        self.shutdowns: list[dict] = []
        self._fail_submit = fail_submit

    def submit(self, fn, *args, **kwargs):
        self.submits += 1
        if self._fail_submit is not None:
            future = concurrent.futures.Future()
            future.set_exception(self._fail_submit)
            return future
        return super().submit(fn, *args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


class PoolStateCase(unittest.IsolatedAsyncioTestCase):
    """每条用例前后都把模块级池状态复位（它会跨用例、跨文件污染）。"""

    def setUp(self) -> None:
        st = cas._STATE                                    # noqa: SLF001
        self._saved = (st.pool, st.disabled, st.rebuilds, st.reason,
                       st.subprocess_hits, st.thread_fallbacks, cas._new_pool)  # noqa: SLF001
        cas.shutdown_audit_pool()
        st.disabled, st.rebuilds, st.reason = False, 0, ""
        st.subprocess_hits = st.thread_fallbacks = 0
        self.pools: list[_FakePool] = []
        cas._new_pool = self._factory()                    # noqa: SLF001

    def tearDown(self) -> None:
        cas.shutdown_audit_pool()
        st = cas._STATE                                    # noqa: SLF001
        (st.pool, st.disabled, st.rebuilds, st.reason, st.subprocess_hits,
         st.thread_fallbacks, cas._new_pool) = self._saved  # noqa: SLF001

    def _factory(self, fail_submit: Exception | None = None):
        """建池出口的替身：**第一次 audit 时**才建，好数"建了几只池"。"""
        def factory():
            pool = _FakePool(fail_submit=fail_submit)
            self.pools.append(pool)
            return pool
        return factory

    def use_broken_pools(self) -> None:
        cas._new_pool = self._factory(                      # noqa: SLF001
            fail_submit=BrokenProcessPool("子进程被原生 abort 带走"))

    @property
    def state(self):
        return cas._STATE                                  # noqa: SLF001


class TestSubprocessPathIsReallyTaken(PoolStateCase):
    async def test_the_report_comes_back_through_the_pool(self):
        """审计进了子进程（记账），报告内容与线程路径一字不差，池常驻复用。"""
        report = await cas.audit_off_loop(dict(POISON))
        self.assertEqual(self.state.subprocess_hits, 1,
                         "记账说没走子进程——那就是悄悄退回线程池了")
        self.assertEqual(self.state.thread_fallbacks, 0)
        self.assertEqual(len(self.pools), 1, "一条源建了一只池（该常驻复用）")
        self.assertIn("notAField", report.unknown_fields)
        self.assertTrue(report.js_syntax_errors)
        self.assertEqual(report.source_id, POISON["bookSourceUrl"])

        await cas.audit_off_loop(dict(CLEAN))
        self.assertEqual(len(self.pools), 1, "第二条又建了一只池")
        self.assertEqual(self.pools[0].submits, 2)
        self.assertEqual(self.state.subprocess_hits, 2)

    async def test_service_entry_point_uses_the_same_path(self):
        """对外入口 `audit_source` 走的也是子进程那条（不是自己另开一条 to_thread）。"""
        svc = cas.CompatAuditService(storage=None)
        report = await svc.audit_source(dict(CLEAN))
        self.assertEqual(self.state.subprocess_hits, 1)
        self.assertEqual(self.state.thread_fallbacks, 0)
        self.assertTrue(report.clean)


class TestPoolCreationFailure(PoolStateCase):
    async def test_unbuildable_pool_falls_back_once_and_never_retries(self):
        """建不出池（没权限 / 杀软拦 spawn）：退回线程池，且不再一条条重试建池。"""
        attempts: list[int] = []

        def factory():
            attempts.append(1)
            raise PermissionError("拒绝访问")
        cas._new_pool = factory                            # noqa: SLF001

        report = await cas.audit_off_loop(dict(POISON))
        self.assertIn("notAField", report.unknown_fields, "退回线程池后审计也没跑")
        self.assertEqual(self.state.thread_fallbacks, 1)
        self.assertTrue(self.state.disabled, "建失败后没永久退回，下一条又要赔一次")
        self.assertIn("PermissionError", self.state.reason)

        await cas.audit_off_loop(dict(CLEAN))
        await cas.audit_off_loop(dict(CLEAN))
        self.assertEqual(len(attempts), 1, "已经判死了还在反复建池")
        self.assertEqual(self.state.thread_fallbacks, 3)
        self.assertFalse(cas.audit_pool_in_use(),
                         "扇出上限读的就是这个，判死了必须报 False")

    async def test_one_bad_submission_falls_back_for_that_one_only(self):
        """单条提交失败（不可 pickle / 池正好在关）是**这条**的毛病：退回跑这一条，
        池照旧留给别的源——不能一条坏源就把整只池判死。"""
        cas._new_pool = self._factory(fail_submit=TypeError(   # noqa: SLF001
            "cannot pickle 'dict' object"))
        report = await cas.audit_off_loop(dict(POISON))
        self.assertIsInstance(report, AuditReport)
        self.assertIn("notAField", report.unknown_fields, "退回线程池后审计也没跑")
        self.assertEqual(self.state.thread_fallbacks, 1)
        self.assertFalse(self.state.disabled, "一条坏源把整只池判死了")
        self.assertTrue(cas.audit_pool_in_use())
        self.assertEqual(self.state.rebuilds, 0, "非打穿类失败被记成打穿")

        cas._new_pool = self._factory()                    # noqa: SLF001
        cas.shutdown_audit_pool()      # 把那只坏池关掉，别留给 atexit 收
        await cas.audit_off_loop(dict(CLEAN))
        self.assertEqual(self.state.subprocess_hits, 1, "坏源之后池就不工作了")


class TestBrokenPool(PoolStateCase):
    async def test_poison_source_is_never_rerun(self):
        """子进程被打穿：这条**不重跑**（不重进新池、也不退回线程池跑）。

        重跑的两种形状都比不跑更坏：换进新池会再带走一个 worker，退回线程池等于把
        原生 abort 请回主进程——那正是这层隔离要挡的东西（known-issues 004）。
        """
        self.use_broken_pools()
        report = await cas.audit_off_loop(dict(POISON))
        self.assertNotEqual(report.not_audited, "",
                            "打穿了却返回一份空清单报告，界面上就成了「这条源干净」")
        self.assertFalse(report.clean)
        self.assertEqual(self.state.thread_fallbacks, 0,
                         "毒源被退回线程池跑了（原生 abort 会带走主进程）")
        self.assertEqual(self.state.subprocess_hits, 0)
        self.assertIsNone(self.state.pool, "被打穿的池没被摘掉")
        self.assertEqual(self.state.rebuilds, 1)
        self.assertFalse(self.state.disabled, "第一次打穿就永久判死，太急")
        self.assertEqual(self.pools[0].shutdowns,
                         [{"wait": False, "cancel_futures": True}],
                         "摘池没关掉它，孤儿子进程留着")

    async def test_next_source_rebuilds_and_succeeds(self):
        """打穿只废掉那一条：下一条自动建新池照常审。"""
        self.use_broken_pools()
        await cas.audit_off_loop(dict(POISON))
        cas._new_pool = self._factory()                    # noqa: SLF001
        report = await cas.audit_off_loop(dict(CLEAN))
        self.assertEqual(report.not_audited, "")
        self.assertTrue(report.clean)
        self.assertEqual(self.state.subprocess_hits, 1)
        self.assertEqual(self.state.rebuilds, 1, "重建被记成又一次打穿")

    async def test_repeated_breakage_disables_the_pool(self):
        """连着打穿超过上限就永久退回线程池：不留一个「每条源崩一次」的循环。"""
        self.use_broken_pools()
        for _ in range(cas.AUDIT_POOL_MAX_REBUILDS + 1):
            await cas.audit_off_loop(dict(POISON))
        self.assertEqual(self.state.rebuilds, cas.AUDIT_POOL_MAX_REBUILDS + 1)
        self.assertTrue(self.state.disabled,
                        f"打穿 {cas.AUDIT_POOL_MAX_REBUILDS + 1} 次还没判死")

        pools_before = len(self.pools)
        report = await cas.audit_off_loop(dict(CLEAN))
        self.assertTrue(report.clean, "判死之后连审计都不跑了")
        self.assertEqual(self.state.thread_fallbacks, 1)
        self.assertEqual(len(self.pools), pools_before, "判死了还在建池")


class TestShutdown(PoolStateCase):
    async def test_stop_closes_the_pool_without_waiting(self):
        """`on_stop` 关池：`wait=False`（停机跑在引擎循环上，不能等 worker 收尾）。"""
        await cas.audit_off_loop(dict(CLEAN))
        await cas.CompatAuditService(storage=None).on_stop()
        self.assertIsNone(self.state.pool, "停机后池还挂着")
        self.assertEqual(self.pools[0].shutdowns,
                         [{"wait": False, "cancel_futures": True}])

    async def test_shutdown_is_idempotent_and_rebuilds_on_demand(self):
        """关两次不炸；关了之后下一条审计会重建（服务重启、大轮收尾就是这个形状）。"""
        await cas.audit_off_loop(dict(CLEAN))
        cas.shutdown_audit_pool()
        cas.shutdown_audit_pool()
        self.assertEqual(len(self.pools[0].shutdowns), 1, "第二次关关到了别人身上")
        self.assertTrue(cas.audit_pool_in_use(),
                        "收尾关池把执行位置降成了线程档——下一轮的扇出上限会跟着掉")
        await cas.audit_off_loop(dict(CLEAN))
        self.assertEqual(len(self.pools), 2, "关过之后不再建池，审计就断了")


class TestNotAuditedReachesTheUser(unittest.IsolatedAsyncioTestCase):
    """`not_audited` 必须走到用户眼前，否则「没审」在界面上就是「干净」。"""

    async def test_import_surface_shows_it_as_a_warning(self):
        from app.services.sources import online_import_service as ois

        class _CrashedAudit:
            async def audit_source(self, source_json):
                return AuditReport(source_id="https://x", not_audited="子进程崩了")

        svc = ois.OnlineImportService(audit_service=_CrashedAudit())
        warnings = await svc._audit_sources(                     # noqa: SLF001
            [{"bookSourceUrl": "https://x", "bookSourceName": "崩源"}])
        self.assertTrue(any("崩源" in w and "子进程崩了" in w for w in warnings),
                        f"导入面没把「没审完」报出来：{warnings}")

    def test_warning_table_covers_every_gap_field(self):
        """字段表逐字段对齐（漏一个就是「那种缺口在导入面永远不出声」）。"""
        from app.services.sources import online_import_service as ois
        real = {f.name for f in dataclasses.fields(AuditReport)}
        self.assertIn("not_audited", real)
        gaps = real - {"source_id", "suggestions"}
        self.assertFalse(gaps - set(ois._AUDIT_WARNING_FIELDS),  # noqa: SLF001
                         f"这些缺口字段不会变成导入 warning：{sorted(gaps)}")

    def test_editor_surface_lists_it(self):
        """编辑器那面的渲染字段表也要有它（同一份报告，两个出口）。"""
        from app.controllers.source import code_editor_controller as cec
        body = inspect.getsource(cec.CodeEditorController._render_audit)
        self.assertIn("not_audited", body)

    def test_audit_log_row_tells_it_apart_from_clean(self):
        """落库那面同样不能把「没审」写成「干净」：payload 里要带得出来。"""
        import json

        rows: list[tuple] = []

        class _RecordingStorage:
            db = None

            def execute(self, sql, params=()):
                rows.append((sql, params))

        svc = cas.CompatAuditService(storage=_RecordingStorage())
        svc._persist(AuditReport(source_id="https://x",        # noqa: SLF001
                                 not_audited="子进程崩了"))
        payload = json.loads(rows[0][1][1])
        self.assertEqual(payload["not_audited"], "子进程崩了",
                         "audit_log 里那行和「审完了没缺口」长得一样，回溯分不出来")


class TestRealSpawnWorks(unittest.IsolatedAsyncioTestCase):
    """真 spawn 一次：证明生产那条路走得通（假池只能验编排）。

    验不了的两件正是 Windows spawn 上最容易哑的：子进程里 import 得到这个模块、
    报告 pickle 得回来。跑完必须把池关掉，别给测试进程留孤儿 worker。
    """

    async def test_one_audit_in_a_real_child_process(self):
        st = cas._STATE                                    # noqa: SLF001
        saved = (st.pool, st.disabled, st.subprocess_hits, st.thread_fallbacks)
        cas.shutdown_audit_pool()
        st.disabled, st.subprocess_hits, st.thread_fallbacks = False, 0, 0
        try:
            report = await asyncio.wait_for(cas.audit_off_loop(dict(POISON)), 60)
            self.assertEqual(st.subprocess_hits, 1,
                             f"真池没用上，退回了线程池：{st.reason}")
            self.assertEqual(report.not_audited, "")
            self.assertIn("notAField", report.unknown_fields)
            self.assertTrue(report.js_syntax_errors)
        finally:
            cas.shutdown_audit_pool()
            (st.pool, st.disabled, st.subprocess_hits,
             st.thread_fallbacks) = saved


if __name__ == "__main__":
    unittest.main()
