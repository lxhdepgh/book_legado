"""SourceCheckService：四段检测流水线的服务外壳（M3，契约 §4 / 设计 §3.1/§3.4）。

外壳化 ``app/services/check_service.py``（M2 冻结副本，逻辑零改动）：

- 四段流水线（连通→搜索→分类→阅读链路）、九态分类、自动停用、单源预算**全部保留**，
  本壳只调 :func:`check_service.check_sources`，不改其内部；
- 对外入口：``async check_source(source_json)``（单源）、``async check_all(groups=None)``
  （批量，**自动两遍**：浅遍只验列表链路，进深检队列的源再补验正文，判据与队列在
  :mod:`app.services.sources.tiered_check`）、``cancel()``（取消在跑的检测；服务
  生命周期停止走基类 async stop）；
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
from collections import Counter
from typing import Callable

from app.core.events import CheckFinishedEvent
from app.core.lifecycle import Service
from app.models.book_source import BookSource
from app.services import check_service
from app.services.block_pages import block_reason  # noqa: F401  共享依赖归属声明（§3.4）
from app.services.sources import compat_audit_service, tiered_check

logger = logging.getLogger(__name__)

#: 批量检测那一轮用的高度：不是 `full`/`list` 之一，而是"两遍都跑，谁进第二遍由判据定"。
#: 快照里写这个值，界面据此别再显示"本轮快速检测"那种单档措辞。
DEPTH_AUTO = "auto"


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
    # 否则浅遍的结论看起来像源本身有毛病。
    if (result.get("tier") == check_service.CHECK_DEPTH_LIST
            or result.get("reading_check_skipped") == check_service.CHECK_DEPTH_LIST):
        line += "（本轮快速检测，未验正文）"
    return line


#: 触发点③（每源检测完复跑一次静态审计）的并发上限，**按审计跑在哪儿分两档**。
#:
#: 退回线程池那一档必须限死：审计是**每条两笔 `asyncio.to_thread`**甩进默认线程池
#: （本机 cpu=20 → 24 条），而检测自己要的 `to_thread`（`_unsupported_java_packages`、
#: `_parse_off_loop`）用的是**同一个池**。不设上限的形状（240 条真库快照、三臂交替，
#: `.probe/check_slowdown_audit_fanout.py`）：峰值同时在跑的审计 **206** 条，限死后 2 条。
#: 取 2 与 JS 检测段（`JS_CHECK_CONCURRENCY`）同量级。
AUDIT_CONCURRENCY = 2
#: 走子进程那一档（现形状）：提交只是往池的队列里塞一条，不占本机线程，上限的作用
#: 变成"别把 900 条一次性全塞进队列"，所以放宽到够喂满 worker 即可
#: （= 2 × `AUDIT_POOL_WORKERS`；worker 数与其实测见 compat_audit_service）。
AUDIT_POOL_FANOUT = 8
#: 收尾等多久。审计是 best-effort：超预算就把没跑完的取消，不拖检测的结束。
AUDIT_DRAIN_BUDGET = 5.0
#: 一轮排出去多少条审计之后，跑完就把进程池关掉（下一轮再按需重建）。
#:
#: 为什么要关：worker 的常驻内存是**跟着做过的条数涨**的——刚建好 4 个共 63 MB
#: （≈16 MB/个），审完 917 条真库快照涨到 269 MB（≈67 MB/个），审完 2161 条
#: 326 MB（≈80 MB/个），之后基本不再涨也不还给 OS（`.probe/mp_pool_memory.py`）。
#: 桌面端一轮整库检测完就白背这 270 MB，而同一台机器上还有内置浏览器要吃内存。
#: 关池实测把子进程 RSS 归零，代价是下次重建 177~184 ms——所以只对**大轮**这么干：
#: 单源/小批量检测本来就快，为它赔一次建池不划算（那 180 ms 会算进收尾等待里）。
#:
#: 顺带一个好处：`shutdown(cancel_futures=True)` 会把收尾时被取消、却还排在池队列里
#: 的那些条目真丢掉（线程路径上取消不掉，线程还会把 `_audit_one` 跑完）。
AUDIT_POOL_REAP_AFTER = 64


def _audit_fanout_bound() -> int:
    """本轮该用哪一档扇出上限——**每轮读一次**，不是写死常量。

    池可能在跑的过程中被打穿（子进程原生 abort）而永久退回线程池，那种情况下
    下一轮必须自动降回 :data:`AUDIT_CONCURRENCY`，否则就是 8 条重 CPU 审计
    挤在默认线程池里，正好是上面那个上限要挡的形状。
    """
    return (AUDIT_POOL_FANOUT if compat_audit_service.audit_pool_in_use()
            else AUDIT_CONCURRENCY)


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
        #: 本轮排出去多少条审计（任务跑完就从 `_audit_tasks` 里摘掉，所以要单独数）：
        #: 收尾时按这个数决定要不要把进程池关掉还内存（`AUDIT_POOL_REAP_AFTER`）。
        self._audit_submitted = 0
        #: 本轮批量检测的回放快照（见 :meth:`run_snapshot`）
        self._run: dict | None = None
        self._run_done: asyncio.Event | None = None
        #: 待深检队列（服务侧，见 :mod:`tiered_check`）：跨轮存"谁该补验正文"。
        #: 不落库、不进用户分组——它是检测自己的账，不是用户看得见的一个组。
        self._deep_queue = tiered_check.DeepQueue()

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

    async def check_all(self, groups=None) -> dict:
        """批量检测：**浅遍扫全书，补验正文排在后面几轮**。

        分级不是给用户选的档位，而是这一轮自己决定"谁值得花钱验正文"。浅遍
        （`CHECK_DEPTH_LIST`，实测 12.1 ms/源、每源 2.00 次 HTTP，正文段 0 次）跑完整
        个范围，其中列表链路证明可用、浅遍没给出结论、或地址形状使浅遍无从判定的源
        进深检队列，由**后续轮次**按限额补验正文（`CHECK_DEPTH_FULL`，23.7 ms/源、
        4.92 次 HTTP，多出来的 2.92 次全在正文段）。判据与队列在
        :mod:`app.services.sources.tiered_check`。

        为什么不赶在同一轮里补验：第六十四轮 (C) 的扫档实测那样**每一档都是净亏**
        （深遍占比最低那档 −16.1%，最亏 −47.8%），两臂的正文段请求数逐档相等，
        赔的是同一批源浅遍、深遍各付一遍。挪出这一轮之后一条源一轮只被派发一次，
        首轮只付浅遍单价（2.0× 便宜），贵的那一半摊到后续轮次里排队吃。

        进度与 `CheckFinishedEvent.done/total` 的分母仍是**本轮计划源数**：浅遍 +
        补验那一片加起来正好走满 0→1。本轮浅遍新排进队列的条数记在
        `tiering.deferred`，服务侧快照的 `deep_queue` 一直看得见还欠着几条。

        已有批量在跑时**不再派第二遍全量联网**（两份流水线同时打站点对既浪费
        又容易被封），而是接到那一轮的收尾上。
        """
        if self._run is not None and self._run["running"]:
            logger.info("批量检测已在跑，check_all 接到当前这一轮的收尾")
            snap = await self.await_run_finished()
            return snap.get("summary") or {}
        sources = self._collect_sources(groups)
        run = self._begin_run(len(sources), depth=DEPTH_AUTO)
        try:
            if not sources:
                self.status.progress = None
                results, tiering = [], self._empty_tiering()
            else:
                results, tiering = await self._run_tiered(sources, run)
                # 两遍的派发数加起来正好是计划源数，这里只把收尾那一格钉到 1.0。
                self.status.progress = 1.0
            run["tiering"] = tiering
            run["summary"] = {
                "total": len(results),
                "finished": len(results),
                "usable": sum(1 for r in results if r.get("usable")),
                "blocked": sum(1 for r in results
                               if r.get("check_state") == "blocked"),
                "results": results,
                "tiering": tiering,
            }
        finally:
            self._end_run(run)   # 标记写完才放行等待收尾的视图
        return run["summary"]

    @staticmethod
    def _empty_tiering() -> dict:
        return {"plan_total": 0, "shallow": 0, "deep": 0, "carried": 0,
                "slice_limit": tiered_check.DEEP_PER_ROUND_MIN,
                "deferred": 0, "queued": 0, "by_reason": {}}

    async def _run_tiered(self, sources: list[BookSource],
                          run: dict) -> tuple[list[dict], dict]:
        """浅遍扫全书 + 按限额补验队列里欠着的那一片，返回结果与本轮的分级账目。

        第六十四轮 (C) 实测"浅遍跑完紧接着在同一轮里补验"每一档都是净亏
        （深遍占比 5.6% 就已经 −16.1%，最亏 −47.8%）：那一批源浅遍、深遍各付一次钱，
        而正文段请求数与"一遍全完整档"完全相等。所以补验的条数由
        :func:`tiered_check.per_round_deep_limit` 封顶、从队列里按"欠得久的先"切片，
        本轮浅遍新排进来的排在下一轮——**一条源在一轮里只被派发一次**，浅遍的那一行
        和补验的那一行不会出现在同一轮里，贵的阅读链摊到后面几轮慢慢付。

        两遍挑的是互不相交的源，所以每一行派发都是这条源这一轮的定论（进度、审计都
        按它走），也无需合并：同一条源一轮只出一行。
        """
        url_of = lambda s: str(getattr(s, "bookSourceUrl", "") or "")   # noqa: E731
        in_scope = {url_of(s) for s in sources}
        self._deep_queue.prune()
        carried = {url: reason for url, reason in self._deep_queue.pending().items()
                   if url in in_scope}

        # 本轮的欠账 = 上一轮带着的 + 静态判据（地址形状），静态更具体所以覆写它。
        static_reasons: dict[str, str] = {}
        for source in sources:
            reason = tiered_check.static_deep_reason(source)
            if reason:
                static_reasons[url_of(source)] = reason
        reasons: dict[str, str] = dict(carried)
        reasons.update(static_reasons)
        for url, reason in reasons.items():
            self._deep_queue.add(url, reason)

        limit = tiered_check.per_round_deep_limit(len(sources))
        deep_sources = self._deep_queue.select(sources, limit=limit)
        deep_urls = {url_of(s) for s in deep_sources}
        shallow_sources = [s for s in sources if url_of(s) not in deep_urls]
        tiering = {"plan_total": len(sources),
                   "shallow": len(shallow_sources), "deep": len(deep_sources),
                   "slice_limit": limit,
                   "carried": sum(1 for s in deep_sources
                                  if url_of(s) in carried),
                   "by_reason": {}}

        results: list[dict] = []
        if shallow_sources:
            results = await self._run_checks(
                shallow_sources, run, check_service.CHECK_DEPTH_LIST,
                total_plan=len(sources), reap=False)

        deferred = 0
        for result in results:
            url = str(result.get("raw_url") or "")
            reason = tiered_check.shallow_deep_reason(result)
            if not reason:
                # 浅遍给了明确结论（不可用 / 被拦 / 正文段那一层已经跑过）：旧债跟着
                # 注销——深遍对它是重付一遍已经否证过的路。静态判据不注销，那条债要的
                # 是"验正文"，列表段的结论回答不了它。
                if url in carried and url not in static_reasons:
                    self._deep_queue.drop(url)
                continue
            if url in static_reasons:
                continue
            if url not in reasons:
                deferred += 1
            reasons[url] = reason
            self._deep_queue.add(url, reason)

        if deep_sources:
            deep_results = await self._run_checks(
                deep_sources, run, check_service.CHECK_DEPTH_FULL,
                total_plan=len(sources), settled_before=len(results),
                fresh_round=False, reap=False)
            results = results + deep_results
            self._settle_queue(deep_results)
        # 两遍合起来才算一次关池决定：浅遍单独不到 64 条不该让深遍那 600 条白背着
        # 子进程内存过夜（计数闸门与单价见 AUDIT_POOL_REAP_AFTER）。
        self._reap_audit_pool()
        tiering["deferred"] = deferred
        # 按原因归组的是**整本欠账**（含这一轮没吃到的那一片），不是切片：界面要说清
        # "队列里欠着的那些各是为了什么"，只数切片会把绝大多数写成空。
        tiering["by_reason"] = dict(Counter(reasons.values()))
        tiering["queued"] = len(self._deep_queue.pending())
        deep_set = {url_of(s) for s in deep_sources}
        for result in results:
            result["tier"] = ("full" if str(result.get("raw_url") or "")
                              in deep_set else "list")
        order = {url_of(s): index for index, s in enumerate(sources)}
        results.sort(key=lambda r: order.get(str(r.get("raw_url") or ""),
                                             len(order)))
        for index, result in enumerate(results, 1):
            result["idx"] = index
        return results, tiering

    def _settle_queue(self, deep_results: list[dict]) -> None:
        """深遍跑过的条目从队列里摘掉；只有一时判不出结论的（`unknown`）留下重试。

        留也要有次数上限：一条 `rate_limited`/超时的源不能每轮都占一条深检预算，
        试满 :data:`tiered_check.DEEP_MAX_ATTEMPTS` 次之后它不再被排进深遍，歇
        :data:`tiered_check.DEEP_RETRY_COOLDOWN_SECONDS` 再由浅遍重新判（计数与冷却
        都在队列里，见 :class:`tiered_check.DeepQueue`）。
        """
        for result in deep_results:
            url = str(result.get("raw_url") or "")
            if str(result.get("check_state") or "") == "unknown":
                self._deep_queue.note_attempt(url)
                continue
            self._deep_queue.drop(url)

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

        除了本轮结果，还带两份自动分档的账：``tiering``（这一轮浅遍/深遍各跑了多少、
        按什么判据收的）和 ``deep_queue``（**下一轮**还欠着深检的那些）。队列是服务
        侧状态，切页、甚至换一轮都还在，界面读它只是为了如实说"还有 N 条待深检"。
        """
        run = self._run
        if run is None:
            return {"running": False, "cancelled": False, "total": 0,
                    "depth": DEPTH_AUTO,
                    "tiering": self._empty_tiering(), "deep_queue": [],
                    "summary": None, "results": []}
        return {"running": bool(run["running"]),
                "cancelled": bool(run["cancelled"]),
                "total": int(run["total"]),
                "depth": run.get("depth", DEPTH_AUTO),
                "tiering": run.get("tiering") or self._empty_tiering(),
                "deep_queue": self._deep_queue.snapshot(),
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
                          depth: str = check_service.CHECK_DEPTH_FULL,
                          *, total_plan: int | None = None,
                          settled_before: int = 0,
                          fresh_round: bool = True,
                          reap: bool = True) -> list[dict]:
        """跑一遍流水线；``run`` 给了就把逐源结果同步记进回放快照。

        快照必须在**引擎循环**上读写（协程方法经 EngineProxy 派发）：界面线程
        直接读会在流水线改到一半时拿到撕裂的进度。

        自动分档把批量检测拆成浅遍+补验两遍调用（补验只吃开跑前欠着的那一片，见
        :meth:`_run_tiered`），一条源在一轮里只被派发一次，所以**每一行派发都是它的
        定论**，进度按派发数推进。剩下三个口子是为跨两遍准备的：

        - 分母是**本轮计划源数**（``total_plan``），不是这一遍的条数。
        - ``settled_before`` 是上一遍已经定论的条数，第二遍从它接着数，进度条单调。
        - ``fresh_round=False`` 表示这是同一轮的后续一遍：审计信号量与扇出计数都
          不重置（重置会把浅遍排出去的审计条数抹掉，关池那道闸门就数错账）。
        - ``reap=False`` 让关池的决定留到整轮结束再做一次。
        """
        plan_total = int(total_plan) if total_plan else len(sources)
        if not sources:
            if fresh_round:
                self.status.progress = None
            return []
        settled = int(settled_before)
        if fresh_round:
            self.status.progress = 0.0
            # 每轮换一把新信号量：Semaphore 第一次 await 就认死当前循环，跨循环复用会抛
            # "bound to a different event loop"（服务重启、单测每条各起一条循环都会换）。
            self._audit_sem = asyncio.Semaphore(_audit_fanout_bound())
            self._audit_submitted = 0

        def on_result(result: dict, done: int, t: int) -> None:
            # 进度写 status.progress（§4.5 状态可见）
            nonlocal settled
            settled += 1
            self.status.progress = (settled / plan_total) if plan_total else 1.0
            self._publish_finished(result, run, settled, plan_total)
            # 每一行派发都是这条源**这一轮**的结论（补验在后面的轮次，不改本轮这一行），
            # 所以审计跟着每一条留下的结果走。
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
        # 审计的尾巴要在进度到 100% 之后立刻收掉：留着不等人，占的是子进程池的队列
        # （退回线程池时占的就是下一条检测、阅读页都要用的默认线程池）。
        await self._drain_audits()
        if reap:
            self._reap_audit_pool()
        self.status.progress = min(1.0, settled / plan_total) if plan_total else None
        return results

    def _reap_audit_pool(self) -> None:
        """大轮跑完就把审计进程池关掉，把 worker 涨出来的内存还给系统。

        见 :data:`AUDIT_POOL_REAP_AFTER`：常驻内存跟着做过的条数涨（917 条 269 MB），
        关池能归零、重建要 177~184 ms，所以只在大轮做这笔交换。小轮不关，
        免得单源检测每次都为下一轮赔一次建池。

        关池不改变"审计走不走子进程"这件事（`shutdown_audit_pool` 不动
        `_STATE.disabled`），所以下一轮的扇出上限仍是进程池那一档；池会在下一条
        审计来时按原样重建。
        """
        if self._audit_submitted < AUDIT_POOL_REAP_AFTER:
            return
        logger.info("本轮审计 %s 条（≥%s），关掉审计进程池还内存",
                    self._audit_submitted, AUDIT_POOL_REAP_AFTER)
        compat_audit_service.shutdown_audit_pool()

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
            # 快照比事件多带一样：抽到的样本章地址（check_service 检正文时目录就
            # 在手上，顺手记了三章）。自愈页要靠它给"检过但没读过"的源挑失败章——
            # 那种源在 reader-cache 里没有目录，这一份是唯一来源。
            # 事件那边不许加键：CheckFinishedEvent 只有五个字段，**entry 多一个键
            # 就是 TypeError。
            run["results"].append({
                **entry,
                "sample_chapter_urls": [str(url) for url in
                                        (result.get("sample_chapter_urls") or [])
                                        if url]})
        if self.bus is None:
            return
        # 分母跟着事件走：订阅方自己数完成的条数，只会得出 12/12（症状见事件文档）
        self.bus.publish(CheckFinishedEvent(**entry, done=int(done), total=int(total)))

    def _maybe_audit(self, result: dict, sources: list[BookSource]) -> None:
        """触发点③：检测流水线每源复跑静态审计（best-effort，不阻断检测）。

        限并发（:func:`_audit_fanout_bound`）并留任务引用：审计是重 CPU 活
        （`_audit_one` 中位 3.2 ms、p95 189 ms，240 条真库快照合计 7.8 s），
        不设上限时一轮就是 200 多条同时往外甩（实测见 :data:`AUDIT_CONCURRENCY`
        上方注释）。任务引用必须留在 `self._audit_tasks` 里——`create_task` 的返回值
        不被持有时，事件循环只握弱引用，跑到一半的任务可能被 GC 掉。
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
        self._audit_submitted += 1
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
