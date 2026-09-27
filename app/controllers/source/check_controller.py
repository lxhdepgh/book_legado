"""CheckController：书源检测视图的事件订阅与服务编排（§7.4）。

订阅 :class:`CheckFinishedEvent` 逐源渲染结果；开始/停止经 SourceCheckService
派发。检测流水线本身（四段九态/自动停用）在服务内，视图不关心。

**切页不退订就等于不丢进度**——这一条是本轮补上的。离开「书源」分组时
:meth:`BaseController.detach` 断了事件订阅，而流水线照常在引擎循环上跑：那期间
发出的 ``CheckFinishedEvent`` 没人接，回来看到的是空进度条，像整轮检测白跑。
现在 :meth:`_restore` 在 attach 时向服务要一份本轮快照回放（逐源结果 + 进度 +
运行态），还在跑就接着等收尾，所以检测结果与进度都能跨页接回。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.events import CheckFinishedEvent
from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.sources.source_check_service import SourceCheckService
except Exception:  # pragma: no cover
    class SourceCheckService:  # type: ignore[no-redef]
        pass


def _summary_line(snap: dict) -> str:
    """快照 → 一行收尾文案（空串表示还没结论，别把「尚未检测」盖掉）。"""
    if snap.get("cancelled"):
        return "检测已停止"
    summary = snap.get("summary")
    if isinstance(summary, dict):
        return (f"检测完成：可用 {summary.get('usable', 0)}/"
                f"{summary.get('total', 0)}，"
                f"阻断 {summary.get('blocked', 0)}")
    return ""


class CheckController(BaseController):
    """检测：启动/停止 + CheckFinishedEvent 逐源渲染 + 切页回来回放进度。"""

    destination = "sources.check"

    def on_attach(self) -> None:
        self._check = self._safe_service(SourceCheckService)
        self._seen: set[str] = set()
        self._total = 0
        self._running = False
        self._view.on_start = self.start
        self._view.on_stop = self.stop
        self.subscribe(CheckFinishedEvent, self._on_check_finished)
        self.ui_call(self._restore())

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 启动 / 停止
    # ------------------------------------------------------------------ #
    def start(self, _=None) -> None:
        if self._check is None:
            self._view.set_summary("SourceCheckService 未就绪（并行集成中）")
            return
        if self._running:  # 在跑就别再派第二遍全量联网
            self._view.set_summary("检测已在进行中")
            return
        self._seen.clear()
        self._total = 0
        self._running = True
        self._view.set_running(True, "检测已启动…")
        self.ui_call(self._run())

    async def _run(self) -> None:
        """跑完这一轮再按服务快照重画：收尾那一行不该只给启动它的视图看到。"""
        try:
            await self._check.check_all()
        except asyncio.CancelledError:
            logger.info("检测已按请求停止")
        except Exception as exc:
            if self._view is not None:
                self._view.set_running(False, f"检测中断：{exc}")
            self._running = False
            return
        self._running = False
        snap = await self._snapshot()
        if snap is not None and self._view is not None:
            self._render(snap)

    def stop(self, _=None) -> None:
        if self._check is None:
            return
        # 停止按钮 = 取消在跑任务（``SourceCheckService.cancel``），
        # 绝不能调 ``Service.stop()``——那是生命周期退役（DISPOSED），
        # 之后整轮会话检测服务都起不来。
        try:
            self._engine().submit(self._cancel_check())
        except Exception as exc:
            logger.warning("取消检测失败：%s", exc)
        self._view.set_running(False, "已请求停止")

    async def _cancel_check(self) -> None:
        """在引擎循环上执行同步 cancel（asyncio.Task.cancel 非线程安全）。"""
        self._check.cancel()

    # ------------------------------------------------------------------ #
    # 跨页回放
    # ------------------------------------------------------------------ #
    async def _restore(self) -> None:
        if self._check is None:
            return
        snap = await self._snapshot()
        if snap is None or self._view is None:
            return
        # attach 的回放排在界面队列尾部，可能晚于用户点「开始」：本视图已经派了
        # 一轮，就拿那一轮的实况，别拿旧快照（或空快照）把运行态盖回去。
        if self._running:
            return
        if (not snap.get("running") and not snap.get("results")
                and snap.get("summary") is None):
            return          # 从来没检测过：初始的「尚未检测」不该被空回放刷掉
        self._render(snap)
        if not snap.get("running"):
            return
        # 这一轮是别人（上一份视图）启动的：接到它的收尾上，跑完照样画出来
        self._running = True
        try:
            await self._check.await_run_finished()
        except Exception as exc:
            logger.warning("等待检测收尾失败：%s", exc)
            return
        self._running = False
        snap = await self._snapshot()
        if snap is not None and self._view is not None:
            self._render(snap)

    async def _snapshot(self) -> dict | None:
        """向服务要本轮快照；服务是替身/未就绪时返回 None（不炸视图）。"""
        try:
            return await self._check.run_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("读取检测进度失败：%s", exc)
            return None

    def _render(self, snap: dict) -> None:
        """快照 → 界面（幂等：``render_result`` 按 source_id 覆盖同名行）。"""
        results = list(snap.get("results") or [])
        self._total = int(snap.get("total") or 0)
        self._seen = {str(entry.get("source_id") or "") for entry in results}
        for entry in results:
            self._view.render_result(entry.get("source_id", ""),
                                      entry.get("state_class", "unknown"),
                                      entry.get("summary", ""))
        if self._total:
            self._view.set_progress(len(self._seen), self._total)
        if snap.get("running"):
            self._view.set_running(
                True, f"检测进行中：{len(self._seen)}/{self._total}")
        else:
            self._view.set_running(False, _summary_line(snap))

    # ------------------------------------------------------------------ #
    # 在途事件
    # ------------------------------------------------------------------ #
    async def _on_check_finished(self, event: CheckFinishedEvent) -> None:
        if self._view is None:  # 编组回界面循环的途中被 detach 了
            return
        self._view.render_result(event.source_id, event.state_class, event.summary)
        if event.source_id:
            self._seen.add(event.source_id)
        self._update_progress()

    def _update_progress(self) -> None:
        """总进度用本轮快照的 total（不再从 0-1 比例反推）。"""
        done = len(self._seen)
        if not done:
            return
        self._view.set_progress(done, self._total or done)
