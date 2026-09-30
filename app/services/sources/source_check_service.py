"""SourceCheckService：四段检测流水线的服务外壳（M3，契约 §4 / 设计 §3.1/§3.4）。

外壳化 ``app/services/check_service.py``（M2 冻结副本，逻辑零改动）：

- 四段流水线（连通→搜索→分类→阅读链路）、九态分类、自动停用、单源预算**全部保留**，
  本壳只调 :func:`check_service.check_sources`，不改其内部；
- 对外入口：``async check_source(source_json)``（单源）、``async check_all(groups=None)``
  （批量）、``cancel()``（取消在跑的检测；服务生命周期停止走基类 async stop）；
- 检测完成发 :class:`~app.core.events.CheckFinishedEvent`，检测进度写
  ``status.progress``（0-1）；
- **本轮批量检测留一份可回放快照**（:meth:`run_snapshot` /
  :meth:`await_run_finished`）：切页时控制器退订只是断了渲染，流水线照常在引擎
  循环上跑完，事件没人接就永久丢了。快照 + 等待收尾让回到检测页能接回进度。
- ``block_pages`` 拦截识别作为共享依赖（九态 ``blocked`` 结论由其喂入，§3.4）；
- 触发点③：每源检测出结果后复跑 CompatAuditService.audit_source（best-effort）。

红线：本服务零 flet import；核心资产 check_service.py 既有行为原样复用。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Callable

from app.core.events import CheckFinishedEvent
from app.core.lifecycle import Service
from app.models.book_source import BookSource
from app.services import check_service
from app.services.block_pages import block_reason  # noqa: F401  共享依赖归属声明（§3.4）

logger = logging.getLogger(__name__)


def _resolve_class(dotted: str) -> type | None:
    """按 dotted path 懒解析类；目标模块/类未就绪（并行未交付）时返回 None。"""
    try:
        module_path, class_name = dotted.rsplit(".", 1)
        return getattr(importlib.import_module(module_path), class_name)
    except Exception:  # 并行线未落盘/符号缺失：降级，不阻断本服务导入
        return None


def _normalize(source) -> BookSource:
    """把 dict / BookSource 统一成 BookSource（check_sources 按属性访问）。"""
    if isinstance(source, BookSource):
        return source
    if isinstance(source, dict):
        return BookSource.from_dict(source)
    raise TypeError(f"check_source 需要 dict 或 BookSource，收到 {type(source)!r}")


def _summary(result: dict) -> str:
    """从单源结果拼一句人类可读摘要（CheckFinishedEvent.summary）。"""
    state = str(result.get("check_state") or "unknown")
    name = result.get("name") or result.get("raw_url") or "(未命名)"
    err = (result.get("reading_error") or result.get("error")
           or result.get("search_error") or "").strip()
    line = f"{name}：{state}（{err}）" if err else f"{name}：{state}"
    # "没试正文"和"试了失败"在九态里都落进列表那一级，界面上必须点出来是哪种，
    # 否则快速检测的结论看起来像源本身有毛病。
    if result.get("reading_check_skipped") == check_service.CHECK_DEPTH_LIST:
        line += "（本轮快速检测，未验正文）"
    return line


#: 触发点③（每源检测完复跑一次静态审计）的并发上限。审计单条只有 ~12.9 ms 的 CPU，
#: 但它是**每条两笔 `asyncio.to_thread`**甩进默认线程池（本机 cpu=20 → 24 条），而
#: 检测自己要的 `to_thread`（`_unsupported_java_packages`、`_parse_off_loop`）用的是
#: **同一个池**。不设上限的实测形状（240 条真库快照、每源 20 ms 假 IO、三臂交替，
#: `.probe/check_slowdown_audit_fanout.py`）：
#:   峰值同时在跑的审计 206 条 vs 限死后 2 条；整轮墙钟（含收尾）不跑审计 8167 ms /
#:   无上限 16186 ms / 上限 2 13596 ms；检测自己那条 to_thread 的 max 从 13257.7 ms
#:   回到 8528.2 ms（不跑审计是 8029.0 ms）——限并发买的是"检测自己的线程步回到没审计
#:   那一档"，代价是收尾那一截有界（`AUDIT_DRAIN_BUDGET`）。取 2 与 JS 检测段
#:   （`JS_CHECK_CONCURRENCY`）同量级。
AUDIT_CONCURRENCY = 2
#: 收尾等多久。审计是 best-effort：超预算就把没跑完的取消，不拖检测的结束。
AUDIT_DRAIN_BUDGET = 5.0


class SourceCheckService(Service):
    """书源检测：单源 / 批量 / 停止，发布九态完成事件。"""

    name = "SourceCheckService"
    #: 拓扑依赖（契约 §1）：SourceService/RuleEngineService 由并行子代理交付，
    #: 这里不 import 以免耦合未就绪类；运行期协作者一律经 on_start 懒解析或构造注入。
    dependencies: tuple[type, ...] = ()

    def __init__(self, source_service=None, settings=None, compat_audit=None,
                 timeout: int = 12,
                 concurrency: int = check_service.CHECK_CONCURRENCY,
                 auto_disable: bool = False,
                 checker: Callable | None = None) -> None:
        super().__init__()
        self._source_override = source_service
        self._settings_override = settings
        self._compat_override = compat_audit
        self._source_service = source_service
        self._settings = settings
        self._compat = compat_audit
        self._timeout = int(timeout)
        self._concurrency = int(concurrency)
        self._auto_disable = bool(auto_disable)
        # 底层检测函数可注入（测试替身）；默认走 M2 冻结的 check_sources。
        self._checker = checker or check_service.check_sources
        self._task: asyncio.Task | None = None
        #: 本轮排出去的审计任务：留引用（不留会被 GC 悄悄回收）、收尾等它们
        self._audit_tasks: set[asyncio.Task] = set()
        self._audit_sem: asyncio.Semaphore | None = None
        #: 本轮批量检测的回放快照（见 :meth:`run_snapshot`）
        self._run: dict | None = None
        self._run_done: asyncio.Event | None = None

    # ------------------------------------------------------------------ #
    # 生命周期：经 hub 补齐未注入的基础设施，并读检测偏好
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        self._settings = self._resolve(
            "app.core.settings.SettingsService", self._settings_override)
        self._source_service = self._resolve(
            "app.services.sources.source_service.SourceService",
            self._source_override)
        self._compat = self._resolve(
            "app.services.sources.compat_audit_service.CompatAuditService",
            self._compat_override)
        # F6：真实环境（hub 非空）下必需协作者 SourceService 缺失须可见——
        # check_all 无源可检；服务仍 RUNNING，仅写 last_error 告警，不 SUSPENDED。
        # CompatAudit 仍 best-effort，缺失不标记。
        if self.hub is not None and self._source_service is None:
            logger.error(
                "SourceCheckService 协作者缺失：SourceService 未注册，check_all 无源可检")
            self.status.last_error = "SourceService 未注册，检测链路无源可检"
        # 软读取检测偏好（缺失则用构造默认）
        if self._settings is not None:
            self._timeout = int(self._settings.get("check.timeout", self._timeout)
                                or self._timeout)
            self._concurrency = int(self._settings.get(
                "check.concurrency", self._concurrency) or self._concurrency)
            self._auto_disable = bool(self._settings.get(
                "check.auto_disable", self._auto_disable))
        logger.info("SourceCheckService 已启动（timeout=%s concurrency=%s）",
                    self._timeout, self._concurrency)

    async def on_stop(self) -> None:
        self.cancel()
        logger.info("SourceCheckService 已停止")

    # ------------------------------------------------------------------ #
    # 对外入口（契约 §4）
    # ------------------------------------------------------------------ #
    async def check_source(self, source_json,
                           depth: str = check_service.CHECK_DEPTH_FULL) -> dict:
        """检测单个书源（dict 或 BookSource），返回该源结果 dict。"""
        source = _normalize(source_json)
        results = await self._run_checks([source], depth=depth)
        return results[0] if results else {}

    async def check_all(self, groups=None,
                        depth: str = check_service.CHECK_DEPTH_FULL) -> dict:
        """批量检测；``groups`` 给定时只检测这些分组。返回汇总 dict。

        ``depth`` 是分级检测的深度：``full`` 一路验到正文，``list`` 验到列表链路就
        收（阅读链是唯一会翻倍的段，实测占两段规则耗时 74.7%，见
        :data:`check_service.CHECK_DEPTH_LIST`）。

        已有批量在跑时**不再派第二遍全量联网**（两份流水线同时打站点对既浪费
        又容易被封），而是接到那一轮的收尾上。
        """
        if self._run is not None and self._run["running"]:
            logger.info("批量检测已在跑，check_all 接到当前这一轮的收尾")
            snap = await self.await_run_finished()
            return snap.get("summary") or {}
        sources = self._collect_sources(groups)
        run = self._begin_run(len(sources), depth)
        try:
            results = await self._run_checks(sources, run, depth)
            run["summary"] = {
                "total": len(results),
                "finished": len(results),
                "usable": sum(1 for r in results if r.get("usable")),
                "blocked": sum(1 for r in results
                               if r.get("check_state") == "blocked"),
                "results": results,
            }
        finally:
            self._end_run(run)   # 标记写完才放行等待收尾的视图
        return run["summary"]

    def _begin_run(self, total: int, depth: str = check_service.CHECK_DEPTH_FULL) -> dict:
        """开一轮可回放快照（必须在引擎循环上调，见 :meth:`_run_checks`）。"""
        run = {"total": total, "running": True, "cancelled": False,
               "summary": None, "results": [], "depth": depth}
        self._run = run
        self._run_done = asyncio.Event()
        return run

    def _end_run(self, run: dict) -> None:
        run["running"] = False
        event, self._run_done = self._run_done, None
        if event is not None:
            event.set()

    def cancel(self) -> None:
        """取消在跑的检测（同步入口，视图停止按钮直接调；不撤服务生命周期）。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------ #
    # 回放快照：切页退订期间丢掉的事件由这里补回来
    # ------------------------------------------------------------------ #
    async def run_snapshot(self) -> dict:
        """当前/最近一轮批量检测的回放快照（``results`` 按检测完成顺序）。

        ``await`` 而非同步读：协程方法经 EngineProxy 跳回引擎循环，读到的
        ``run`` dict 不会被在跑的流水线改到一半（界面线程直读会拿到撕裂的进度）。
        """
        run = self._run
        if run is None:
            return {"running": False, "cancelled": False, "total": 0,
                    "depth": check_service.CHECK_DEPTH_FULL,
                    "summary": None, "results": []}
        return {"running": bool(run["running"]),
                "cancelled": bool(run["cancelled"]),
                "total": int(run["total"]),
                "depth": run.get("depth", check_service.CHECK_DEPTH_FULL),
                "summary": run["summary"],
                "results": list(run["results"])}

    async def await_run_finished(self) -> dict:
        """等当前在跑的批量检测收尾，返回那一轮的快照（没有在跑就立即返回）。"""
        event = self._run_done
        if event is not None:
            await event.wait()
        return await self.run_snapshot()

    # ------------------------------------------------------------------ #
    # 内部：批量执行 + 进度/事件/审计触发点
    # ------------------------------------------------------------------ #
    def _collect_sources(self, groups=None) -> list[BookSource]:
        if self._source_service is not None and hasattr(self._source_service, "sources"):
            sources = list(self._source_service.sources)
        else:
            sources = []
        if groups:
            wanted = set(groups)
            sources = [s for s in sources
                       if (getattr(s, "bookSourceGroup", "") or "") in wanted]
        return sources

    async def _run_checks(self, sources: list[BookSource],
                          run: dict | None = None,
                          depth: str = check_service.CHECK_DEPTH_FULL) -> list[dict]:
        """跑一轮流水线；``run`` 给了就把逐源结果同步记进回放快照。

        快照必须在**引擎循环**上读写（协程方法经 EngineProxy 派发）：界面线程
        直接读会在流水线改到一半时拿到撕裂的进度。
        """
        if not sources:
            self.status.progress = None
            return []
        total = len(sources)
        self.status.progress = 0.0
        # 每轮换一把新信号量：Semaphore 第一次 await 就认死当前循环，跨循环复用会抛
        # "bound to a different event loop"（服务重启、单测每条各起一条循环都会换）。
        self._audit_sem = asyncio.Semaphore(AUDIT_CONCURRENCY)

        def on_result(result: dict, done: int, t: int) -> None:
            # 进度写 status.progress（§4.5 状态可见）
            self.status.progress = (done / t) if t else 1.0
            self._publish_finished(result, run, done, t)
            self._maybe_audit(result, sources)

        async def runner():
            return await self._checker(
                sources, concurrency=self._concurrency, timeout=self._timeout,
                on_result=on_result, auto_disable=self._auto_disable,
                depth=depth)

        self._task = asyncio.create_task(runner())
        try:
            results = await self._task
        except asyncio.CancelledError:
            if run is not None:
                run["cancelled"] = True
            self._cancel_audits()
            raise
        finally:
            self._task = None
        # 审计的尾巴要在进度到 100% 之后立刻收掉：留着不等人，占的是下一条检测、
        # 阅读页都要用的默认线程池（见 AUDIT_CONCURRENCY 上方的实测）。
        await self._drain_audits()
        self.status.progress = 1.0 if total else None
        return results

    def _cancel_audits(self) -> None:
        """把这一轮还没跑完的审计全部取消（检测被停/被取消时不留后台尾巴）。"""
        for task in list(self._audit_tasks):
            if not task.done():
                task.cancel()

    def _publish_finished(self, result: dict, run: dict | None = None,
                          done: int = 0, total: int = 0) -> None:
        entry = {"source_id": str(result.get("raw_url") or ""),
                 "state_class": str(result.get("check_state") or "unknown"),
                 "summary": _summary(result)}
        if run is not None:
            run["results"].append(entry)   # 切页期间没人接事件，靠这份回放
        if self.bus is None:
            return
        # 分母跟着事件走：订阅方自己数完成的条数，只会得出 12/12（症状见事件文档）
        self.bus.publish(CheckFinishedEvent(**entry, done=int(done), total=int(total)))

    def _maybe_audit(self, result: dict, sources: list[BookSource]) -> None:
        """触发点③：检测流水线每源复跑静态审计（best-effort，不阻断检测）。

        限并发（:data:`AUDIT_CONCURRENCY`）并留任务引用：审计本身只有十几毫秒 CPU，
        但它往默认线程池一次甩两笔 `to_thread`，不设上限时检测自己那步要排在
        200 多条审计后面（实测见 :data:`AUDIT_CONCURRENCY` 上方注释）。
        任务引用必须留在 `self._audit_tasks` 里——`create_task` 的返回值不被持有
        时，事件循环只握弱引用，跑到一半的任务可能被 GC 掉。
        """
        if self._compat is None or not hasattr(self._compat, "audit_source"):
            return
        idx = result.get("idx")
        source = (sources[idx - 1]
                   if isinstance(idx, int) and 1 <= idx <= len(sources) else None)
        if source is None:
            return
        try:
            data = source.to_dict()
        except Exception:
            return
        task = asyncio.create_task(self._audit_gated(data))
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    async def _drain_audits(self) -> None:
        """等本轮排出去的审计收尾，超预算就取消剩下的（别拖检测结束）。"""
        pending = {t for t in self._audit_tasks if not t.done()}
        if not pending:
            return
        _done, still = await asyncio.wait(pending, timeout=AUDIT_DRAIN_BUDGET)
        for task in still:
            task.cancel()
        if still:
            logger.info("检测收尾：%s 条审计没跑完（预算 %s s），已取消",
                        len(still), AUDIT_DRAIN_BUDGET)
            await asyncio.gather(*still, return_exceptions=True)

    async def _audit_gated(self, data: dict) -> None:
        async with self._audit_sem:
            await self._audit_safe(data)

    async def _audit_safe(self, data: dict) -> None:
        try:
            await self._compat.audit_source(data)
        except Exception:  # 审计失败绝不影响检测结论
            logger.warning("检测流水线复跑审计失败，已忽略", exc_info=True)

    # ------------------------------------------------------------------ #
    def _resolve(self, dotted: str, override):
        """优先构造注入；否则经 hub.require 取原始实例；都没有返回 None。"""
        if override is not None:
            return override
        cls = _resolve_class(dotted)
        if cls is None or self.hub is None:
            return None
        try:
            return self.hub.require(cls)
        except Exception:
            return None
