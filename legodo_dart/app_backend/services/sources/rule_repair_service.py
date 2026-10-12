"""RuleRepairService：正文规则自愈闭环的服务外壳（M3，契约 §5 / 设计 §3.2/§3.4）。

外壳化 ``app/services/rule_repair.py``（M2 冻结副本，逻辑零改动）：

- 自愈闭环**原样保留**：真抓一章样本页（``_load_page``）→ ``_mine`` 按"汉字最多、
  链接占比低"定位正文并从紧容器向外层生成候选选择器 → ``ProbeEngine`` 把请求口焊死
  在同一份 HTML 上反复回放不出网 → ``_confirm_candidate`` 再抓 1-2 页同书章节复核
  → 产出 :class:`~app.services.rule_repair.RuleFix`/:class:`RepairReport`。
  本壳只调 :func:`rule_repair.diagnose_content_rule`，不改其内部；
- 对外入口：``async repair(...)``（自愈诊断，产出 RepairReport）、
  ``async apply_fix(report_id, fix_id)``（**用户确认后**才写回书源——保留红线，不自动改源）、
  ``async chapter_candidates(source_url)``（自愈页的"给哪一章做样本"候选面，
  读的是 reader-cache 里已经躺在磁盘上的目录，**不出网**）；
- 诊断的每个阶段发 :class:`~app.core.events.RepairProgressEvent`（冻结副本里
  ``diagnose_content_rule`` 的 ``on_stage`` 形参，本壳把它接成事件），完成发
  :class:`~app.core.events.RepairReportReadyEvent`，视图凭 report_id 取报告；
- 样本页加载时的拦截判定复用 block_pages（§3.4，已在 rule_repair._load_page 内）。

红线：本服务零 flet import；ProbeEngine 的 HTML 回放语义必须保留（注入的 engine
原样透传给 diagnose_content_rule，不在壳层改请求口）。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.core.events import RepairProgressEvent, RepairReportReadyEvent
from app.core.lifecycle import Service
from app.services.content.local_book_service import is_local_source_url
from app.services.reading_cache import ReadingCache
from app.services.rule_repair import (RepairReport, diagnose_content_rule)

logger = logging.getLogger(__name__)

#: 自愈候选面的三个上限。**成本口径不同，所以不能合成一个数**：
#: * ``MAX_CANDIDATE_ROWS``——下拉整页下发约 0.024 ms/行（单价出处见
#:   `docs/dev-logs/2026-09-30.md`），只管**总行数**；
#: * ``MAX_CANDIDATE_SCAN_MS``——翻目录的钱。实测单本 5000 章 25.7 ms、1000 章
#:   4.4 ms、300 章 1.3 ms、100 章 0.4 ms（`.probe/out_catalog_concurrency.txt`），
#:   **价差 60 倍**，所以这一档必须按**时间**给而不是按本数：按本数钉死就等于给小书
#:   收 100 倍过路费（40 本 × 100 章一共 17 ms，本来全摊得下）；
#: * ``MAX_CANDIDATE_CHAPTERS_PER_BOOK``——一本书独占预算时也不把整本书端上下拉。
#:
#: 为什么并发不能替时间预算省钱（第六十七/七十轮那版留的疑问）：实测 8 本并发只省
#: 4.5%，16/24/40 本**负收益**——一次读的三段是 read_text 2.7 ms + json.loads 8.7 ms
#: + 构造 5000 个 ChapterItem 7.5 ms，后两段全在 GIL 里，线程并发买不回来。
#:
#: 历史：旧写法只有"3 本 × 100 章"，两个成本混成一个数，还让第 4 本之后的书在下拉里
#: 静默消失；第七十轮拆成"本数 + 行数"，本数那一档今天换成时间。现在每本摊到的是
#: ``行预算 // 实际本数``，摊不到的那几本由 :class:`CandidateFace` 报给界面。
MAX_CANDIDATE_ROWS = 300
MAX_CANDIDATE_SCAN_MS = 200
MAX_CANDIDATE_CHAPTERS_PER_BOOK = 100


@dataclass(frozen=True)
class CandidateFace:
    """候选面：摊出来的章 + 一份**普查**（普查是用来承认没摊到谁的）。

    ``cached_books`` 的口径是"目录文件在盘上"（一次 stat，实测 8 本 0.11 ms），
    不是"读得出来"——版本不符的老目录极少数情况下会算进 ``cached_books`` 却进不了
    ``groups``，那种书本来也做不了样本，宁可让提示行多说一句"没摊到"。
    """

    groups: list[dict] = field(default_factory=list)
    shelf_books: int = 0        # 这个源在架几本书（书架记录，免费）
    cached_books: int = 0       # 其中本地存过目录的几本（stat 级）
    shown_books: int = 0        # 真摊进下拉的几本
    rows: int = 0               # 摊出来的总行数

    @property
    def skipped_books(self) -> int:
        return max(0, self.cached_books - self.shown_books)


def _sample(chapters: list[dict], allowance: int) -> list[dict]:
    """等间隔抽到 allowance 章以内，保持原序。

    步长必须**向上取整**：``len // allowance`` 那种向下取整的步长会超额
    （80 章 / 37 → 步长 2 → 端出 40 章），实测 8 本 × 5000 章那样摊下来是 312 行，
    行预算就白设了（`.probe/out_candidate_cost.txt` 的 B 臂）。
    """
    if len(chapters) <= allowance:
        return chapters
    stride = max(1, -(-len(chapters) // allowance))
    return chapters[::stride]


class RuleRepairService(Service):
    """正文规则自愈：诊断出建议 → 用户确认后才写回。"""

    name = "RuleRepairService"
    #: 拓扑依赖：RuleEngineService/SourceCheckService 由并行线交付，运行期经
    #: on_start 懒解析或构造注入，此处不 import 未就绪类。
    dependencies: tuple[type, ...] = ()

    def __init__(self, source_service=None, auto_trigger: bool = False,
                 cache=None) -> None:
        super().__init__()
        self._source_override = source_service
        self._source_service = source_service
        #: reader-cache 的读端（章节候选用）；on_start 里按 StorageService 补齐。
        self._cache = cache
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
        # 章节候选要读 reader-cache 的目录（自愈页的选择器数据源）。读不到缓存目录
        # 只是候选面为空，诊断本身不受影响，所以这里不升级成错误。
        if self._cache is None and self.hub is not None:
            try:
                from app.core.storage import StorageService
                storage = self.hub.require(StorageService)
                self._cache = ReadingCache(storage.paths.cache)
            except Exception as exc:  # pragma: no cover - 精简装配没有 StorageService
                logger.info("自愈章节候选降级为空：读不到缓存目录（%s）", exc)
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

        ``chapter`` 允许是合成对象（只有 ``name``/``url``）：诊断只读这两个字段，
        ``book`` 缺省只是少一份 JS 上下文，``extra_chapters`` 缺省只是复核退化成
        "只有样本页那一页"——所以用户手填一个正文页地址照样能起一轮真诊断。

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

    # ------------------------------------------------------------------ #
    # 章节候选：自愈页"给哪一章做样本"的数据源
    # ------------------------------------------------------------------ #
    async def chapter_candidates(self, source_url: str, *,
                                 max_scan_ms: int = MAX_CANDIDATE_SCAN_MS,
                                 per_book: int = MAX_CANDIDATE_CHAPTERS_PER_BOOK,
                                 max_rows: int = MAX_CANDIDATE_ROWS,
                                 clock=time.perf_counter) -> CandidateFace:
        """这个源在架书的**已缓存目录**，按书分组给出可诊断的章节（不出网）。

        自愈要的样本章只有一个地方能免联网拿到：reader-cache 里读过的那份目录。
        这口径也对得上现场——会撞见正文规则坏的，正是把这本书读过的人。没读过
        的源返回空表，那不是错误而是"本地没有样本"，界面上由粘贴框接手。

        两个预算各钉一种成本（实测见模块常量的注）：**行预算** ``max_rows`` 管下发，
        **时间预算** ``max_scan_ms`` 管翻目录。翻目录这一档按时间不按本数，因为
        单本价格随章数差 60 倍（100 章 0.4 ms ↔ 5000 章 25.7 ms）：小书多的书架
        能整架摊完，大书多的到点就停。停下来不是静默——``CandidateFace`` 把
        "在架几本 / 存过目录几本 / 摊了几本 / 几本没摊"一并带回，界面上必须说。

        时间预算的检查在**每本开读之前**，所以最坏情况超出"一本"（一本五千章约
        26 ms），不会超出"一整架"。
        """
        source_url = str(source_url or "")
        if (not source_url or self._source_service is None
                or self._cache is None or is_local_source_url(source_url)):
            return CandidateFace()
        try:
            records = list(await self._source_service.list_bookshelf() or [])
        except Exception as exc:
            logger.warning("自愈候选取书架失败（当作没有样本）：%s", exc)
            return CandidateFace()
        # 书架记录的源/书地址有 Legado 的两套别名（同 SourceService.bookshelf_key）；
        # 重复记录去重，顺序按书架原序（最近在读在前）。
        wanted: list[tuple[str, str]] = []
        seen: set[str] = set()
        for record in records:
            origin = str((record or {}).get("source_url")
                         or (record or {}).get("origin") or "")
            book_url = str((record or {}).get("url")
                           or (record or {}).get("bookUrl") or "")
            if origin != source_url or not book_url or book_url in seen:
                continue
            seen.add(book_url)
            wanted.append((book_url, str((record or {}).get("name") or "")))
        if not wanted:
            return CandidateFace()
        # 一次 stat 普查（实测 8 本 0.11 ms）先把没存过目录的书滤掉：它们那一次
        # 读本来就是开文件失败，但失败也要付一次线程跳转。
        cached = await asyncio.to_thread(self._with_catalogs, source_url, wanted)
        budget = max(0.0, float(max_scan_ms)) / 1000.0
        started = clock()
        collected: list[tuple[str, str, list[dict]]] = []
        for book_url, book_name in cached:
            if collected and (clock() - started) > budget:
                break
            chapters = await self._cached_chapters(source_url, book_url)
            if chapters:
                collected.append((book_url, book_name, chapters))
        if not collected:
            return CandidateFace(shelf_books=len(wanted), cached_books=len(cached))
        allowance = max(1, min(int(per_book), int(max_rows) // len(collected)))
        groups = [{"book_url": book_url, "book_name": book_name,
                   "chapters": _sample(chapters, allowance)}
                  for book_url, book_name, chapters in collected]
        return CandidateFace(groups=groups, shelf_books=len(wanted),
                             cached_books=len(cached), shown_books=len(groups),
                             rows=sum(len(g["chapters"]) for g in groups))

    def _with_catalogs(self, source_url: str,
                       wanted: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """只留下盘上确实存着目录的那些本（一次 stat 一本，不读内容）。"""
        return [(book_url, book_name) for book_url, book_name in wanted
                if self._cache.has_catalog(source_url, book_url)]

    async def _cached_chapters(self, source_url: str,
                               book_url: str) -> list[dict]:
        """读一本书的缓存目录，只留"有地址、不是卷标题"的可诊断章。"""
        try:
            catalog = await asyncio.to_thread(
                self._cache.load_catalog, source_url, book_url)
        except Exception as exc:
            logger.warning("自愈候选读目录跳过 %s：%s", book_url, exc)
            return []
        out = []
        for item in catalog or []:
            url = str(getattr(item, "url", "") or "")
            if not url or getattr(item, "is_volume", False):
                continue
            out.append({"name": str(getattr(item, "name", "") or ""), "url": url})
        return out

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
