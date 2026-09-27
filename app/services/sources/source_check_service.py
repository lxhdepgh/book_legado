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
    return f"{name}：{state}（{err}）" if err else f"{name}：{state}"


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
    async def check_source(self, source_json) -> dict:
        """检测单个书源（dict 或 BookSource），返回该源结果 dict。"""
        source = _normalize(source_json)
        results = await self._run_checks([source])
        return results[0] if results else {}

    async def check_all(self, groups=None) -> dict:
        """批量检测；``groups`` 给定时只检测这些分组。返回汇总 dict。

        已有批量在跑时**不再派第二遍全量联网**（两份流水线同时打站点对既浪费
        又容易被封），而是接到那一轮的收尾上。
        """
        if self._run is not None and self._run["running"]:
            logger.info("批量检测已在跑，check_all 接到当前这一轮的收尾")
            snap = await self.await_run_finished()
            return snap.get("summary") or {}
        sources = self._collect_sources(groups)
        run = self._begin_run(len(sources))
        try:
            results = await self._run_checks(sources, run)
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

    def _begin_run(self, total: int) -> dict:
        """开一轮可回放快照（必须在引擎循环上调，见 :meth:`_run_checks`）。"""
        run = {"total": total, "running": True, "cancelled": False,
               "summary": None, "results": []}
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
                    "summary": None, "results": []}
        return {"running": bool(run["running"]),
                "cancelled": bool(run["cancelled"]),
                "total": int(run["total"]),
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
                          run: dict | None = None) -> list[dict]:
        """跑一轮流水线；``run`` 给了就把逐源结果同步记进回放快照。

        快照必须在**引擎循环**上读写（协程方法经 EngineProxy 派发）：界面线程
        直接读会在流水线改到一半时拿到撕裂的进度。
        """
        if not sources:
            self.status.progress = None
            return []
        total = len(sources)
        self.status.progress = 0.0

        def on_result(result: dict, done: int, t: int) -> None:
            # 进度写 status.progress（§4.5 状态可见）
            self.status.progress = (done / t) if t else 1.0
            self._publish_finished(result, run)
            self._maybe_audit(result, sources)

        async def runner():
            return await self._checker(
                sources, concurrency=self._concurrency, timeout=self._timeout,
                on_result=on_result, auto_disable=self._auto_disable)

        self._task = asyncio.create_task(runner())
        try:
            results = await self._task
        except asyncio.CancelledError:
            if run is not None:
                run["cancelled"] = True
            raise
        finally:
            self._task = None
        self.status.progress = 1.0 if total else None
        return results

    def _publish_finished(self, result: dict, run: dict | None = None) -> None:
        entry = {"source_id": str(result.get("raw_url") or ""),
                 "state_class": str(result.get("check_state") or "unknown"),
                 "summary": _summary(result)}
        if run is not None:
            run["results"].append(entry)   # 切页期间没人接事件，靠这份回放
        if self.bus is None:
            return
        self.bus.publish(CheckFinishedEvent(**entry))

    def _maybe_audit(self, result: dict, sources: list[BookSource]) -> None:
        """触发点③：检测流水线每源复跑静态审计（best-effort，不阻断检测）。"""
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
        asyncio.create_task(self._audit_safe(data))

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
