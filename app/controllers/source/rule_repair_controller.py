"""RuleRepairController：自愈的选择器接线、事件订阅与确认写回编排（§3.2 / 契约 §5）。

订阅 :class:`RepairProgressEvent`（阶段进度）与 :class:`RepairReportReadyEvent`
（报告就绪）渲染；**只有用户点确认**才经 RuleRepairService.apply_fix 以
old||new 兜底分支写回书源（红线：不自动改源）。

这一页过去只会"等报告"，报告却只有阅读器的失败补救条起得来——列表页那颗「自愈」
按钮跳进来看到的是一行永远不变的"等待自愈报告…"。现在进页自己拉书源列表、按源
摊平本地已有样本（书架的已缓存目录 + 上一轮检测抽到的三章），点「开始诊断」才出网。

三条必须记住的形状：
- 服务给出的是 :class:`rule_repair.RepairReport`（提议在 ``report.fix`` 里，
  失败时只有 ``report.reason``），视图契约是扁平 dict——展平是这里的活。
  曾经按顶层属性取值，结果报告卡一片空白、且"报告缺少 report_id"永远写不回。
- 事件投递是异步的（§5.11）：处理器可能在 ``detach()`` 之后才被调到，
  **碰控件前一律再查一次 ``self._view``**。
- 进度与报告是**全局广播**：别的页（阅读器）也能起诊断，所以本页发起过一轮时
  按 ``_expect_source_id`` 过滤，没发起过一律按到来即画——否则阅读器那一趟的报告
  就没地方渲染了。
"""
from __future__ import annotations

import logging

from app.core.events import RepairProgressEvent, RepairReportReadyEvent
from app.controllers.base import BaseController
from app.services.content.local_book_service import is_local_source_url
from app.services.engine.rule_engine import BookItem, ChapterItem

logger = logging.getLogger(__name__)

try:
    from app.services.sources.rule_repair_service import RuleRepairService
except Exception:  # pragma: no cover
    class RuleRepairService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover - 仅在兄弟服务未落盘时触发
    class SourceService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.source_check_service import SourceCheckService
except Exception:  # pragma: no cover
    class SourceCheckService:  # type: ignore[no-redef]
        pass

#: 列表页跳过来时载荷里只该有的三个键（多一个都把检测内部结构外溢出去了）。
_PAYLOAD_KEYS = ("source_url", "source_name", "origin")
#: 样本章文案里带书名的阈值：多本书时不带书名就分不清"第一章"是哪个第一章。
_NAME_BOOK_WHEN_MULTIPLE = 2
#: 复核样本跟着阅读器那条链的口径：失败章后面 1-2 章（同书同规则最能验真伪）。
EXTRA_CHAPTERS = 2


class RuleRepairController(BaseController):
    """自愈：选源选章 → 起诊断 → 阶段进度 → 报告渲染 → 确认后写回。"""

    destination = "sources.repair"

    def on_attach(self) -> None:
        self._repair = self._safe_service(RuleRepairService)
        self._source = self._safe_service(SourceService)
        self._check = self._safe_service(SourceCheckService)
        # 本页发起的那趟诊断的状态。_expect_source_id 只在"点过开始"之后才有值，
        # 空串表示"这页没起过诊断"，此时来的报告一律照画（阅读器那一趟）。
        self._diagnosing = False
        self._expect_source_id = ""
        self._candidate_groups: list[dict] = []

        self._view.on_confirm_fix = self.confirm_fix
        self._view.on_dismiss = self.dismiss_fix
        self._view.on_start_diagnosis = self.start_diagnosis
        self._view.on_source_picked = self.load_chapter_options
        self._view.on_chapter_picked = self._on_chapter_picked
        self._view.on_reload_sources = self.refresh_sources
        self.subscribe(RepairProgressEvent, self._on_progress)
        self.subscribe(RepairReportReadyEvent, self._on_report_ready)
        # 进页就拉一次书源列表（读的是主库，零网络）。真去出网的只有
        # 「开始诊断」那一下——列表页点进来的人未必想诊断。
        self.refresh_sources()

    def on_detach(self) -> None:
        # 页都下了，"本页在途"就不再成立：回来时不许被上一趟的闸门挡住按钮。
        self._diagnosing = False
        self._expect_source_id = ""

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 事件：阶段进度与报告就绪
    # ------------------------------------------------------------------ #
    def _is_mine(self, source_id: str) -> bool:
        """这趟诊断是不是本页点出来的那一趟。"""
        return not (self._expect_source_id
                    and str(source_id or "") != self._expect_source_id)

    async def _on_progress(self, event: RepairProgressEvent) -> None:
        if self._view is None or not self._is_mine(event.source_id):
            return
        self._view.set_status(event.stage or "")

    async def _on_report_ready(self, event: RepairReportReadyEvent) -> None:
        if self._view is None:
            # 诊断还在路上用户切了页：报告照样缓存，只是这趟没地方渲染
            return
        if not self._is_mine(event.source_id):
            logger.info("自愈页忽略别处的报告：本页在诊断 %s，来的是 %s",
                        self._expect_source_id, event.source_id)
            return
        report = self._load_report(event.report_id, event.source_id)
        self._diagnosing = False
        self._expect_source_id = ""
        self._view.set_diagnosing(False)
        self._view.render_report(report)

    def _load_report(self, report_id: str, source_id: str) -> dict:
        """凭 report_id 取回报告；服务无 get_report 时退化为最小占位 dict。"""
        getter = getattr(self._repair, "get_report", None) if self._repair else None
        if getter is None:
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": "RuleRepairService 未就绪"}
        try:
            return self._to_dict(getter(report_id), report_id, source_id)
        except Exception as exc:
            logger.warning("取回自愈报告失败 %s：%s", report_id, exc)
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": f"取回报告失败：{exc}"}

    @staticmethod
    def _to_dict(report, report_id: str = "", source_id: str = "") -> dict:
        """RepairReport / RuleFix → 视图契约的扁平 dict。

        ``report_id`` 由事件带来（报告对象自己没有它），写回时要原样带回去。
        ``has_fix`` 区分"给出了提议"和"给出了为什么给不出来"——两者都必须让
        用户看清，否则失败会显示成一条空规则等着被"应用"。
        """
        if isinstance(report, dict):
            payload = dict(report)
            payload.setdefault("report_id", report_id)
            payload.setdefault("source_id", source_id)
            return payload
        if report is None:
            return {"report_id": report_id, "source_id": source_id,
                    "has_fix": False, "reason": "没有这份报告（可能已过期）"}
        fix = getattr(report, "fix", None)
        samples = tuple(getattr(fix, "samples", ()) or ())
        return {
            "report_id": report_id,
            "source_id": (getattr(fix, "source_url", "") or source_id),
            "source_name": getattr(fix, "source_name", "") or source_id,
            "field": getattr(fix, "field", ""),
            "old_value": getattr(fix, "old_value", ""),
            "new_value": getattr(fix, "new_value", ""),
            "samples": [
                {"chapter": getattr(s, "chapter", ""),
                 "url": getattr(s, "url", ""),
                 "chars": int(getattr(s, "chars", 0) or 0),
                 "snippet": getattr(s, "snippet", "")}
                for s in samples],
            "note": getattr(fix, "note", ""),
            "has_fix": fix is not None,
            "reason": getattr(report, "reason", ""),
            "tried": int(getattr(report, "tried", 0) or 0),
            "pages": int(getattr(report, "pages", 0) or 0),
        }

    # ------------------------------------------------------------------ #
    # 书源下拉
    # ------------------------------------------------------------------ #
    def refresh_sources(self, _=None) -> None:
        """拉一遍书源列表填下拉；没有 SourceService（精简装配）就直说。"""
        if self._view is None:
            return
        if self._source is None:
            self._view.set_status("SourceService 未就绪，列不出书源")
            return
        future = self.ui_call(self._load_sources())
        future.add_done_callback(self.on_done(self._log_only))

    async def _load_sources(self) -> None:
        records = await self._source.list_sources() or []
        options = []
        for record in records:
            url = self._field_of(record, "bookSourceUrl")
            name = self._field_of(record, "bookSourceName") or url
            if url:
                options.append((url, name))
        if self._view is None:            # 拉列表的工夫里切了页
            return
        selected = self._view.get_selection()["source_url"]
        self._view.set_source_options(options, current=selected)
        if not options:
            self._view.set_status("还没有书源，先到「导入」页导入一份")
            return
        self._view.set_status(f"共 {len(options)} 个书源，选一个来诊断正文规则")
        # 只有一个源时下拉已经写着它了（set_source_options 会替用户选上），
        # 章节就得跟着落——程序改 value 不触发 on_select，不补这一下首屏就是
        # 一排"正文章节（空）"，看着像加载坏了。
        shown = self._view.get_selection()["source_url"]
        if shown and shown != selected:
            self.load_chapter_options(shown)

    def load_record(self, record) -> None:
        """列表页「自愈」带过来的载荷：把那一源塞进下拉并去摊它的章节。

        载荷只有三键（``_PAYLOAD_KEYS``）——章节不许由列表页传：它手上也没有，
        而且检测内部结构一旦外溢，这里就得跟着猜键名。
        """
        if self._view is None:
            return
        payload = dict(record or {})
        url = str(payload.get("source_url") or "")
        if not url:
            return
        self._view.add_source_option(url, str(payload.get("source_name") or url))
        self.load_chapter_options(url)

    # ------------------------------------------------------------------ #
    # 章节下拉：本地已有样本（书架缓存目录 + 上一轮检测抽到的章）
    # ------------------------------------------------------------------ #
    def load_chapter_options(self, source_url: str = "") -> None:
        url = str(source_url or "").strip()
        if not url or self._view is None:
            return
        if is_local_source_url(url):
            # 本地书没有规则源，候选面却**有**它的缓存目录（书架里的伪源
            # book_url 是本地路径），端出来只会让人起一趟抓 file:// 的空诊断。
            self._candidate_groups = []
            self._view.set_chapter_options([])
            self._view.set_manual_visible(False)
            self._view.set_chapter_hint("")
            self._view.set_status("本地书没有规则源，正文规则自愈不适用")
            return
        future = self.ui_call(self._load_candidates(url))
        future.add_done_callback(self.on_done(self._log_only))

    async def _load_candidates(self, url: str) -> None:
        groups: list[dict] = []
        getter = getattr(self._repair, "chapter_candidates", None) \
            if self._repair is not None else None
        if getter is not None:
            groups = list(await getter(url) or [])
        sample_group = await self._check_sample_group(url)
        if sample_group is not None:
            groups.append(sample_group)
        if self._view is None:            # 摊候选的工夫里切了页
            return
        self._candidate_groups = [g for g in groups if g.get("chapters")]
        options = [(f"{gi}:{ci}", self._chapter_label(group, chapter))
                   for gi, group in enumerate(self._candidate_groups)
                   for ci, chapter in enumerate(group["chapters"])]
        self._view.set_chapter_options(options)
        # 零样本必须让粘贴框接手：这时候"挑不出一章"不是错误，是本地确实没样本。
        self._view.set_manual_visible(not options)
        self._view.set_chapter_hint(self._candidate_hint(url, options))

    async def _check_sample_group(self, url: str) -> dict | None:
        """上一轮检测给这一源抽到的样本章（检过、但没读过的源只有这一路）。"""
        snapshot = getattr(self._check, "run_snapshot", None)
        if snapshot is None:
            return None
        try:
            run = await snapshot() or {}
        except Exception as exc:            # 没跑过检测是常态，不许因此报脏
            logger.info("自愈候选取检测快照失败：%s", exc)
            return None
        for entry in reversed(list(run.get("results") or [])):
            if str(entry.get("source_id") or "") != url:
                continue
            urls = [str(u) for u in (entry.get("sample_chapter_urls") or []) if u]
            if not urls:
                return None
            return {"book_url": "", "book_name": "上次检测抽到的章节",
                    "chapters": [{"name": f"样本 {i + 1}", "url": u}
                                 for i, u in enumerate(urls)]}
        return None

    def _chapter_label(self, group: dict, chapter: dict) -> str:
        name = str(chapter.get("name") or "") or str(chapter.get("url") or "")
        book = str(group.get("book_name") or "")
        if not book:
            return name
        if len(self._candidate_groups) >= _NAME_BOOK_WHEN_MULTIPLE:
            return f"{book} · {name}"
        return name

    def _candidate_hint(self, url: str, options: list[tuple[str, str]]) -> str:
        if options:
            cached = [g for g in self._candidate_groups if g.get("book_url")]
            shelf = sum(len(g["chapters"]) for g in cached)
            # 覆盖几本书要写出来：候选面是按本数摊的，不写就看不出"还有书没摊出来"
            tail = (f"（其中 {shelf} 章来自 {len(cached)} 本书的已缓存目录）"
                    if shelf else "")
            return (f"本地有 {len(options)} 章可做样本{tail}"
                    + "；诊断会真抓 1-3 页，只有你点确认才写回书源")
        return ("这个源本地没有可读目录，也没有检测样本：粘贴一个正文页地址，"
                "或先读一章 / 跑一轮检测")

    def _on_chapter_picked(self, chapter_key: str = "") -> None:
        """选了哪一章，顺带说清这一趟还会带上谁做复核。"""
        if self._view is None:
            return
        extras = self._extras_for(chapter_key)
        if not extras:
            self._view.set_chapter_hint("只用这一页做样本（后面没有可复核的章）")
            return
        self._view.set_chapter_hint(
            "复核还会带上：" + "、".join(item.name for item in extras))

    # ------------------------------------------------------------------ #
    # 发起诊断（只有点「开始诊断」才出网）
    # ------------------------------------------------------------------ #
    def start_diagnosis(self, _=None) -> None:
        if self._view is None:
            return
        if self._repair is None:
            self._view.set_status("RuleRepairService 未就绪，无法诊断")
            return
        if self._diagnosing:
            self._view.set_status("诊断已在进行中，等这一轮出报告")
            return
        selection = self._view.get_selection()
        url = selection["source_url"]
        if not url:
            self._view.set_status("先选一个书源")
            return
        if is_local_source_url(url):
            self._view.set_status("本地书没有规则源，正文规则自愈不适用")
            return
        chapter = self._chapter_of(selection)
        if chapter is None:
            self._view.set_status(
                "没有可诊断的章节：挑一章，或在上面粘贴一个 http(s):// 开头的正文页地址")
            return
        key = "" if str(selection.get("manual_url") or "").strip() \
            else selection["chapter_key"]
        # 旧报告必须先收起：让上一源的候选还挂在卡上，等于叫用户去确认一条不相干的规则。
        self._diagnosing = True
        self._expect_source_id = url
        self._view.render_report(None)
        self._view.set_diagnosing(True, f"正在准备诊断《{url}》…")
        future = self.ui_call(self._diagnose(
            url, chapter, self._extras_for(key), self._book_of(key)))
        future.add_done_callback(self.on_done(self._after_repair))

    def _chapter_of(self, selection: dict) -> ChapterItem | None:
        """选中的那一章（粘贴框优先——它是零样本时的唯一出口）。"""
        manual = str(selection.get("manual_url") or "").strip()
        if manual:
            # 相对地址在这一源上必然抓不到，白跑一趟真请求还显得像规则坏了。
            if not manual.startswith(("http://", "https://")):
                return None
            return ChapterItem(name="手填样本页", url=manual)
        group_index, chapter_index = self._split_key(selection.get("chapter_key"))
        if group_index is None:
            return None
        try:
            chapter = self._candidate_groups[group_index]["chapters"][chapter_index]
        except (IndexError, KeyError, TypeError):
            return None
        return ChapterItem(name=chapter.get("name") or "", url=chapter.get("url") or "")

    def _extras_for(self, chapter_key: str) -> list[ChapterItem]:
        """同组（同书）里紧跟其后的 1-2 章，给诊断做复核样本。"""
        group_index, chapter_index = self._split_key(chapter_key)
        if group_index is None:
            return []
        try:
            chapters = self._candidate_groups[group_index]["chapters"]
        except (IndexError, KeyError, TypeError):
            return []
        return [ChapterItem(name=item.get("name") or "", url=item.get("url") or "")
                for item in chapters[chapter_index + 1:chapter_index + 1 + EXTRA_CHAPTERS]]

    @staticmethod
    def _split_key(key) -> tuple[int | None, int | None]:
        parts = str(key or "").split(":")
        if len(parts) != 2:
            return None, None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None, None

    async def _diagnose(self, url: str, chapter: ChapterItem,
                        extras: list[ChapterItem], book: BookItem | None):
        """取回 BookSource 再派诊断：源都取不到就不必出网。

        ``extras``/``book`` 在这里只是把 :meth:`start_diagnosis` 已经算好的东西
        带过来——协程跑起来时选择器上已经可能换了，届时再反查就是拿新选择诊断旧章。
        """
        if self._source is None:
            raise RuntimeError("SourceService 未就绪，取不到书源")
        source = await self._source.load_source(url)
        if source is None:
            raise RuntimeError(f"没有书源 {url}")
        return await self._repair.repair(source, chapter,
                                         extra_chapters=extras, book=book)

    def _book_of(self, chapter_key: str) -> BookItem | None:
        """选中章所属那本书的壳：诊断只拿它做 JS 上下文与 read_config，凑不出来就 None。"""
        group_index, _chapter_index = self._split_key(chapter_key)
        if group_index is None:
            return None
        try:
            group = self._candidate_groups[group_index]
        except (IndexError, KeyError, TypeError):
            return None
        if not group.get("book_url"):
            return None
        return BookItem(name=group.get("book_name") or "", url=group["book_url"])

    def _after_repair(self, future) -> None:
        # 两条复位都做（报告事件那一路也复位一次）：迟到一拍不许又把进度条支起来。
        self._diagnosing = False
        if self._view is None:
            return
        try:
            future.result()
        except Exception as exc:
            self._expect_source_id = ""
            self._view.set_diagnosing(False, f"诊断失败：{str(exc)[:120]}")
            return
        self._view.set_diagnosing(False)

    # ------------------------------------------------------------------ #
    # 确认写回 / 忽略
    # ------------------------------------------------------------------ #
    def confirm_fix(self, report: dict) -> None:
        report_id = str((report or {}).get("report_id", "") or "")
        if self._repair is None:
            self._view.set_status("RuleRepairService 未就绪，未写回")
            return
        if not report_id:
            self._view.set_status("报告缺少 report_id，无法写回")
            return
        # apply_fix 的 fix_id 可省（服务内按报告默认修复项）；写回结果以
        # 返回值为准——失败是 False，不是异常。
        self._view.set_status("正在写回书源并复测…")
        future = self.ui_call(self._repair.apply_fix(report_id))
        future.add_done_callback(self.on_done(self._after_apply))

    def _after_apply(self, future) -> None:
        try:
            ok = bool(future.result())
        except Exception as exc:
            self._view.set_status(f"写回失败：{exc}")
            return
        self._view.set_status(
            "已写回书源并复测，列表页会自己刷新" if ok
            else "写回未生效（报告过期或书源已删除）")

    def dismiss_fix(self, report: dict) -> None:
        self._expect_source_id = ""
        self._view.render_report(None)
        self._view.set_status("已忽略该自愈候选（书源未改动）")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _field_of(record, key: str) -> str:
        if isinstance(record, dict):
            return str(record.get(key) or "")
        return str(getattr(record, key, "") or "")

    @staticmethod
    def _log_only(future) -> None:
        """回调只负责把异常露出来（``on_done`` 已补 flush）。"""
        future.result()
