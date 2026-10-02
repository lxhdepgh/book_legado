"""分级检测的护栏：浅遍/深遍两遍怎么分、"哪一段没试"记在哪、界面不再管档位。

取证见 `.probe/tiered_two_pass_cost.py`（全程离线：读 resource/ 快照 + 127.0.0.1 假源），
本文件里的数都是那支探针当场量出来的：
* 阅读链是唯一会翻倍的段——36 条同形状假源上浅遍 12.1 ms/源、完整档 23.7 ms/源（2.0×），
  每源 HTTP 从 2.00 次涨到 4.92 次，多出来的 2.92 次全是正文段（开书/取目录/取正文）；
* 主站连通**不能**当硬门——真库 917 条里 **35 条**（3.8%）searchUrl 与 bookSourceUrl
  不同家、**169 条**（18.4%）静态判不出搜索主机、**13 条** bookSourceUrl 连 http(s)
  都不是；`.probe/check_tier_depth.py` (B) 里"主站拒连 + 搜索正文全通"的源现判
  fully_usable，加硬门直接把它打死。

所以分级的是**深度**（要不要验正文），不是**门**（主站不通就不往下）；而且这一档
**不由用户选**：`check_all` 自己先扫浅遍，再按 :mod:`tiered_check` 的三条判据决定
谁补验正文（第六十四轮）。第六十二轮那颗下拉已经撤掉。
按 `guard-tests-assert-absence`：快速深度的护栏钉的是"阅读链一次都没被派发"，
分档的护栏钉的是"这些源根本没进第二遍"，界面面的护栏钉的是"工具行里没有档位可点"。
记账替身记录的是**调用**而不是返回值。
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import flet as ft

from app.models.book_source import BookSource
from app.services import check_service
from app.services.sources import tiered_check
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


#: 与 check_service 里同一组"可用"结论（替身要照着它算 usable，别自造一套）。
USABLE_STATES = frozenset({"fully_usable", "search_only", "explore_only",
                           "list_usable"})


class _RecordingChecker:
    """服务外壳那侧的流水线替身：按**遍**记账（用了哪一档、跑了哪几条源）。

    自动分档会把一轮批量拆成最多两遍调用，所以记的不只是 depth，还得记每一遍拿到
    的源列表——"第二遍只跑了该跑的那些"这条判据只能从这儿读出来。
    """

    DEFAULT_STATE = {check_service.CHECK_DEPTH_LIST: "search_only",
                     check_service.CHECK_DEPTH_FULL: "fully_usable"}

    def __init__(self, states=None, no_books=(), valid_tags=(), watch=None):
        self.calls: list[tuple[str, list[str]]] = []
        #: ``{raw_url: {depth: check_state}}``——不给某条源配状态就走 DEFAULT_STATE
        self.states = states or {}
        #: 浅遍"链路通、这个词没命中"的那些源（手里一本书都没有，见 no_books 的用法）
        self.no_books = set(no_books)
        #: 只有发现页标签里握着书的源（搜索空手，靠标签给阅读链候选）
        self.valid_tags = set(valid_tags)
        #: 每一遍结束时读一下外部状态（审计计数只能在**遍结束时**取，见 watch）
        self.watch = watch
        self.watched: list = []

    @property
    def depths(self) -> list[str]:
        return [depth for depth, _urls in self.calls]

    def urls_for(self, depth: str) -> list[list[str]]:
        return [urls for item, urls in self.calls if item == depth]

    async def __call__(self, sources, concurrency=8, timeout=12, on_result=None,
                       auto_disable=False, depth="full"):
        self.calls.append((depth, [str(s.bookSourceUrl) for s in sources]))
        results = []
        for index, source in enumerate(sources, 1):
            state = self.states.get(source.bookSourceUrl, {}).get(depth)
            if state is None:
                state = self.DEFAULT_STATE.get(depth, "fully_usable")
            results.append({
                "idx": index, "name": source.bookSourceName,
                "raw_url": source.bookSourceUrl, "check_state": state,
                "usable": state in USABLE_STATES,
                # 深遍的候选只来自"搜索命中的书 + 有效标签里的书"，判据③要读这两个数；
                # 替身不发的话，测试里每条"列表可用"都自带书，正是探针自证。
                "book_count": (0 if source.bookSourceUrl in self.no_books else 1),
                "tag_valid": (1 if source.bookSourceUrl in self.valid_tags else 0),
                "reading_check_skipped": (
                    depth if depth == check_service.CHECK_DEPTH_LIST else None)})
        for index, row in enumerate(results, 1):
            if on_result:
                on_result(row, index, len(results))
        if self.watch is not None:
            self.watched.append(self.watch())
        return results


def _src(url: str, **overrides) -> BookSource:
    values = {"bookSourceUrl": url, "bookSourceName": url,
              "searchUrl": "/search?key={{key}}", "enabled": True}
    values.update(overrides)
    return BookSource.from_dict(values)


def _library(*sources: BookSource) -> SimpleNamespace:
    return SimpleNamespace(sources=list(sources))


class _Bus:
    """事件记账替身：收 CheckFinishedEvent 的 done/total 与 summary。"""

    def __init__(self):
        self.events: list = []

    def publish(self, event):
        self.events.append(event)
        return []


def _service(checker, *sources, compat=None) -> SourceCheckService:
    svc = SourceCheckService(checker=checker, source_service=_library(*sources),
                             compat_audit=compat)
    svc.bus = _Bus()
    return svc


class _RecordingCompat:
    """静态审计替身：记**被调用了几次**（审计是重 CPU 活，次数就是钱）。"""

    def __init__(self):
        self.audited: list[str] = []

    async def audit_source(self, data: dict) -> None:
        self.audited.append(str(data.get("bookSourceUrl") or ""))


class TieringCriteriaTests(unittest.TestCase):
    """三条判据本身（纯函数）。真库快照上的分布实测见 `.probe/tiered_two_pass_cost.py` (A)。"""

    @staticmethod
    def _shallow(state: str, **extra) -> dict:
        """浅遍的结论形状：`reading_check_skipped="list"` + 手里有 1 本搜索命中的书。"""
        return {"check_state": state, "reading_check_skipped": "list",
                "book_count": 1, "tag_valid": 0, **extra}

    def test_a_relative_search_url_is_the_same_site(self):
        self.assertEqual("", tiered_check.static_deep_reason(
            _src("https://m.kadokado.com.tw", searchUrl="/api/search?q={{key}}")))

    def test_a_subdomain_only_difference_is_the_same_site(self):
        """`m.jjwxc.net → www.jjwxc.net` 只是同家的不同入口，为它赔一整条阅读链不值。"""
        self.assertEqual("", tiered_check.static_deep_reason(
            _src("https://www.jjwxc.net", searchUrl="https://m.jjwxc.net/search")))

    def test_a_different_registrable_domain_is_cross_host(self):
        self.assertEqual(tiered_check.REASON_CROSS_HOST,
                         tiered_check.static_deep_reason(_src(
                             "https://www.jjwxc.net",
                             searchUrl="https://biqugse.com/search?key={{key}}")))

    def test_a_multi_label_public_suffix_does_not_trick_the_comparison(self):
        """裸切"最后两段"会把 `kadokado.com.tw` 当成别人家——这里必须认出来是同家。"""
        self.assertEqual("", tiered_check.static_deep_reason(_src(
            "https://kadokado.com.tw", searchUrl="https://www.kadokado.com.tw/s")))

    def test_a_scheme_less_search_url_is_still_compared_by_host(self):
        self.assertEqual(tiered_check.REASON_CROSS_HOST, tiered_check.static_deep_reason(
            _src("https://a.com", searchUrl="other.net/search?key={{key}}")))

    def test_an_ip_literal_without_a_scheme_is_a_host_not_a_path(self):
        self.assertEqual(tiered_check.REASON_CROSS_HOST,
                         tiered_check.static_deep_reason(
                             _src("https://a.com", searchUrl="1.2.3.4/s?key={{key}}")))

    def test_a_bare_relative_path_is_not_mistaken_for_a_host(self):
        """`search.php?key={{key}}` 这种不带前导斜杠的相对路径是**同家**（真库有一批）。

        这条护栏是从快照里长出来的：按"head 里有个点就当主机"判时，凭空造出 76 条
        假跨站（占当时跨站桶的 68%），76 条源没跑过浅遍就直接赔上一条完整阅读链。
        """
        for search in ("search.php?keyWord={{key}}", "search.html?searchkey={{key}}",
                       "index.php?s=/web/index/search", "books.asp?kw={{key}}"):
            with self.subTest(search=search):
                self.assertEqual("", tiered_check.static_deep_reason(
                    _src("https://a.com", searchUrl=search)))

    def test_a_relative_path_with_a_request_option_block_is_still_relative(self):
        """`s.php,{"charset":"gbk"}` 逗号之后是 Legado 的请求选项，不是主机名。"""
        self.assertEqual("", tiered_check.static_deep_reason(
            _src("https://a.com", searchUrl='s.php,{\n"method":"POST",\n"body":"k={{key}}"\n}')))

    def test_a_search_host_built_by_a_template_cannot_be_decided_statically(self):
        """`{{cookie.removeCookie(source.getKey())}}/search/book`：主机那段是算出来的。"""
        self.assertEqual(tiered_check.REASON_UNDECIDABLE_HOST,
                         tiered_check.static_deep_reason(_src(
                             "https://a.com",
                             searchUrl="{{cookie.removeCookie(source.getKey())}}"
                                     "/search/book")))

    def test_a_dynamically_generated_search_url_cannot_be_decided_statically(self):
        self.assertEqual(tiered_check.REASON_UNDECIDABLE_HOST,
                         tiered_check.static_deep_reason(
                             _src("https://a.example", searchUrl="@js:\nresult=x")))

    def test_a_non_http_home_cannot_be_decided_statically(self):
        self.assertEqual(tiered_check.REASON_UNDECIDABLE_HOST,
                         tiered_check.static_deep_reason(
                             _src("ftp://a.example", searchUrl="/s?key={{key}}")))

    def test_a_source_without_a_search_url_is_not_judged_on_hosts(self):
        """没有搜索地址：浅遍自己会给结论（发现链路那一头照样测得到），不按主机判。"""
        self.assertEqual("", tiered_check.static_deep_reason(
            _src("https://a.example", searchUrl="")))

    def test_shallow_list_level_verdicts_ask_for_the_prose_pass(self):
        for state in ("search_only", "explore_only", "list_usable"):
            with self.subTest(state=state):
                self.assertEqual(tiered_check.REASON_LIST_USABLE,
                                 tiered_check.shallow_deep_reason(
                                     self._shallow(state)))

    def test_an_explore_only_source_queues_on_its_tag_books_alone(self):
        """搜索空手、发现页标签里有书：阅读链的候选来自标签，照样该补验正文。"""
        self.assertEqual(tiered_check.REASON_LIST_USABLE,
                         tiered_check.shallow_deep_reason(
                             self._shallow("explore_only", book_count=0,
                                           tag_valid=1)))

    def test_a_list_that_handed_over_no_book_buys_no_deep_pass(self):
        """`no_results` 在九态里算"搜索链路证明通了"，可手里一本书都没有：深遍没东西可试。

        这条护栏是从实测里长出来的（`.probe/tiered_two_pass_cost.py` (D) 的 `nogate`
        臂）：把候选门打成恒真，36 条假源里那 18 条"搜索通、没命中"的也排队，深遍占比
        从 50.0% 冲到 100.0%，墙钟多 17.0%、多付 36 次请求而正文段一次没多花，
        两遍相对一遍全完整档反而贵 65.1%。
        """
        self.assertEqual("", tiered_check.shallow_deep_reason(
            self._shallow("search_only", book_count=0)))
        self.assertEqual("", tiered_check.shallow_deep_reason(
            self._shallow("list_usable", book_count=0, tag_valid=0)))

    def test_a_missing_book_count_is_not_guessed_into_the_queue(self):
        """计数缺失/坏值按"手里没书"处理：宁可不花深检的钱，也不猜一个候选出来。"""
        for value in (None, "", "abc"):
            with self.subTest(value=value):
                self.assertEqual("", tiered_check.shallow_deep_reason(
                    self._shallow("search_only", book_count=value)))

    def test_a_shallow_pass_that_got_no_verdict_also_queues(self):
        self.assertEqual(tiered_check.REASON_UNKNOWN_SHALLOW,
                         tiered_check.shallow_deep_reason(
                             self._shallow("unknown", book_count=0)))

    def test_a_shallow_dead_end_does_not_queue(self):
        """死了的、被拦的、已经到底的：重跑深遍买不到新信息。"""
        for state in ("unusable", "blocked", "content_unavailable", "fully_usable"):
            with self.subTest(state=state):
                self.assertEqual("", tiered_check.shallow_deep_reason(
                    self._shallow(state)))

    def test_a_deep_result_never_requeues_itself(self):
        """验过正文的结论（skipped=None）不能再把自己排进深遍——那是自我循环。"""
        self.assertEqual("", tiered_check.shallow_deep_reason(
            {"check_state": "search_only", "reading_check_skipped": None,
             "book_count": 1}))

    def test_a_probe_blocked_source_is_not_the_same_as_an_unverified_one(self):
        """本机拦截/没有可点的书：正文段是"因为别的原因没试"，不该按浅遍结论排队。

        第二臂的 state 故意用 `list_usable`：完整档也会给出这个结论，只按状态判的话
        它每轮都把自己排回深遍。
        """
        self.assertEqual("", tiered_check.shallow_deep_reason(
            {"check_state": "blocked", "reading_check_skipped": "probe_blocked",
             "book_count": 1}))
        self.assertEqual("", tiered_check.shallow_deep_reason(
            {"check_state": "list_usable", "reading_check_skipped": "no_candidates",
             "book_count": 0}))


class _Clock:
    """可推进的假钟：队列的 TTL/冷却只能靠它测，睡 7 天的测试不算测试。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeepQueueTests(unittest.TestCase):
    """待深检队列（服务侧）的账：谁在队里、试了几次、什么时候不再排它。"""

    def _queue(self):
        clock = _Clock()
        return tiered_check.DeepQueue(clock=clock), clock

    def test_added_urls_are_selected_in_source_list_order(self):
        queue, _clock = self._queue()
        queue.add("https://b.example", tiered_check.REASON_CROSS_HOST)
        queue.add("https://a.example", tiered_check.REASON_LIST_USABLE)
        picked = queue.select([_src("https://a.example"), _src("https://b.example"),
                               _src("https://c.example")])
        self.assertEqual(["https://a.example", "https://b.example"],
                         [s.bookSourceUrl for s in picked])

    def test_a_queued_url_outside_this_round_stays_in_the_queue(self):
        """只检某个分组时，队列里别的分组不该被打掉或丢掉。"""
        queue, _clock = self._queue()
        queue.add("https://g2.example", tiered_check.REASON_LIST_USABLE)
        self.assertEqual([], queue.select([_src("https://g1.example")]))
        self.assertEqual({"https://g2.example"}, set(queue.pending()))

    def test_an_unknown_reason_or_an_empty_url_is_refused(self):
        queue, _clock = self._queue()
        queue.add("https://a.example", "just_because")
        queue.add("", tiered_check.REASON_LIST_USABLE)
        self.assertEqual([], queue.snapshot())

    def test_a_settled_entry_is_dropped(self):
        queue, _clock = self._queue()
        queue.add("https://a.example", tiered_check.REASON_UNKNOWN_SHALLOW)
        queue.drop("https://a.example")
        self.assertEqual(0, len(queue))

    def test_the_same_reason_never_washes_the_attempt_count_clean(self):
        """同一轮浅遍重判出同一个原因时不能把次数洗掉，否则配额形同虚设。"""
        queue, _clock = self._queue()
        url = "https://a.example"
        queue.add(url, tiered_check.REASON_UNKNOWN_SHALLOW)
        queue.note_attempt(url)
        queue.add(url, tiered_check.REASON_UNKNOWN_SHALLOW)
        self.assertEqual(1, queue.snapshot()[0]["attempts"])

    def test_a_changed_reason_is_new_information_and_resets_the_budget(self):
        queue, _clock = self._queue()
        url = "https://a.example"
        queue.add(url, tiered_check.REASON_UNKNOWN_SHALLOW)
        queue.note_attempt(url)
        queue.note_attempt(url)
        queue.add(url, tiered_check.REASON_CROSS_HOST)
        row = queue.snapshot()[0]
        self.assertEqual(0, row["attempts"])
        self.assertEqual(tiered_check.REASON_CROSS_HOST, row["reason"])

    def test_an_exhausted_entry_is_not_pending_but_survives_the_cooldown(self):
        queue, clock = self._queue()
        url = "https://a.example"
        queue.add(url, tiered_check.REASON_UNKNOWN_SHALLOW)
        for _ in range(tiered_check.DEEP_MAX_ATTEMPTS):
            queue.note_attempt(url)
        self.assertEqual({}, queue.pending())
        self.assertEqual(1, len(queue))          # 还在，是"别再排它"的凭据
        clock.advance(tiered_check.DEEP_RETRY_COOLDOWN_SECONDS + 1)
        self.assertEqual([], queue.snapshot())    # 歇够就删，下一轮重新给机会

    def test_a_stale_entry_is_pruned_by_the_ttl(self):
        queue, clock = self._queue()
        queue.add("https://a.example", tiered_check.REASON_LIST_USABLE)
        clock.advance(tiered_check.DEEP_ENTRY_TTL_SECONDS + 1)
        self.assertEqual(["https://a.example"], queue.prune())

    def test_the_snapshot_is_grouped_by_reason_then_url(self):
        queue, _clock = self._queue()
        queue.add("https://z.example", tiered_check.REASON_CROSS_HOST)
        queue.add("https://a.example", tiered_check.REASON_LIST_USABLE)
        self.assertEqual([tiered_check.REASON_LIST_USABLE,
                          tiered_check.REASON_CROSS_HOST],
                         [row["reason"] for row in queue.snapshot()])


class ServiceTieringTests(unittest.IsolatedAsyncioTestCase):
    """自动分档：谁跑浅遍、谁补验正文、两遍怎么并成一份结论。"""

    async def test_the_shallow_pass_runs_first_and_the_deep_pass_follows(self):
        checker = _RecordingChecker()
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"))
        await svc.check_all()
        self.assertEqual([check_service.CHECK_DEPTH_LIST,
                          check_service.CHECK_DEPTH_FULL], checker.depths)
        # 两条源的浅遍结论都是"列表可用、正文没验"——正是"贵的那段还没花"的那批
        self.assertEqual([["https://a.example", "https://b.example"]],
                         checker.urls_for(check_service.CHECK_DEPTH_FULL))

    async def test_a_source_the_shallow_pass_ruled_out_buys_no_deep_pass(self):
        """浅遍已经明确否证的（不可用/被拦）一次正文都不试：重跑买不到新信息。"""
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unusable"},
            "https://b.example": {check_service.CHECK_DEPTH_LIST: "blocked"}})
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"))
        await svc.check_all()
        self.assertEqual([check_service.CHECK_DEPTH_LIST], checker.depths)

    async def test_a_relative_search_url_is_not_mistaken_for_a_different_site(self):
        """`/d/{key}` 这种相对搜索地址和主站天生同一台机器（真库 917 条里有 386 条）。

        把它算成"跨站"就等于把这 386 条全拖进深检，第二遍成了全库重跑。这条护栏
        的反面是：一个把字符串不等当跨站的实现会让它出现在深遍里。
        """
        checker = _RecordingChecker(states={
            "https://x.example": {check_service.CHECK_DEPTH_LIST: "blocked"}})
        svc = _service(checker, _src("https://x.example", searchUrl="/d/{{key}}"))
        await svc.check_all()
        self.assertEqual([check_service.CHECK_DEPTH_LIST], checker.depths)

    async def test_a_cross_host_search_url_skips_the_shallow_pass_entirely(self):
        """主站通不通和搜索通不通问的是两台机器：浅遍那段结论对它没有推力。"""
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unusable"},
            "https://b.example": {check_service.CHECK_DEPTH_FULL: "fully_usable"}})
        svc = _service(checker, _src("https://a.example"),
                       _src("https://b.example",
                            searchUrl="https://other.example/s?q={{key}}"))
        await svc.check_all()
        self.assertEqual([["https://a.example"]],
                         checker.urls_for(check_service.CHECK_DEPTH_LIST))
        self.assertEqual([["https://b.example"]],
                         checker.urls_for(check_service.CHECK_DEPTH_FULL))

    async def test_a_js_generated_search_url_is_decided_by_the_deep_pass(self):
        """`@js:` 的搜索地址静态看不出会打到哪，浅遍无从判定 → 直接按完整档跑。"""
        checker = _RecordingChecker()
        svc = _service(checker, _src("https://c.example", searchUrl="@js:\nresult=x"))
        await svc.check_all()
        self.assertEqual([check_service.CHECK_DEPTH_FULL], checker.depths)

    async def test_a_shallow_verdict_with_no_book_in_hand_buys_no_deep_pass(self):
        """替身交出"搜索链路通、一本没命中"：这条不进深检队列，第二遍不替它花钱。

        没有这一条护栏，`search_only` 一律排队会让深遍占比冲到 100%
        （`.probe/tiered_two_pass_cost.py` (D) 的 `nogate` 臂：+65.1% 墙钟），
        自动分档反而比一遍全完整档贵。
        """
        checker = _RecordingChecker(no_books={"https://a.example"})
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"))
        summary = await svc.check_all()
        self.assertEqual([["https://b.example"]],
                         checker.urls_for(check_service.CHECK_DEPTH_FULL))
        self.assertEqual(1, summary["tiering"]["deep"], summary["tiering"])
        self.assertEqual({"list_usable"}, set(summary["tiering"]["by_reason"]))
        # 没进队列不等于结论变了：它仍然按浅遍的结果出那一行
        self.assertEqual(2, summary["total"])

    async def test_the_deep_verdict_replaces_the_shallow_one_for_that_source(self):
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_FULL: "fully_usable"}})
        svc = _service(checker, _src("https://a.example"))
        summary = await svc.check_all()
        self.assertEqual(1, summary["total"], "同一条源不能算成两行")
        self.assertEqual(1, summary["usable"])
        self.assertEqual("fully_usable", summary["results"][0]["check_state"])
        self.assertEqual("full", summary["results"][0]["tier"])

    async def test_two_passes_still_yield_one_row_per_source_when_urls_repeat(self):
        """库里两条源填了同一个地址：按 url 配对覆盖会吞掉一条，结果数对不上计划数。"""
        checker = _RecordingChecker()
        svc = _service(checker, _src("https://d.example"), _src("https://d.example"))
        summary = await svc.check_all()
        self.assertEqual(2, summary["total"])
        self.assertEqual([1, 2], [row["idx"] for row in summary["results"]])

    async def test_the_bar_counts_settled_sources_not_dispatches(self):
        """分母是本轮计划源数，分子是**已定论**的源数：被收进深检队列的那条浅遍里不推进度。

        这条是界面侧算法逼出来的：控制器按 source_id 去重数进度，浅遍就把两条都"跑过"
        了，进度会提前到 100%，深遍再长也不动——所以分子只能数定论。
        """
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unusable"}})
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"))
        await svc.check_all()
        seen = [(event.done, event.total) for event in svc.bus.events]
        self.assertEqual([(1, 2), (1, 2), (2, 2)], seen)
        self.assertEqual(1.0, svc.status.progress)

    async def test_only_the_row_that_was_never_prose_checked_says_so(self):
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unusable"}})
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"))
        await svc.check_all()
        lines = [event.summary for event in svc.bus.events]
        self.assertIn("未验正文", lines[0], lines)
        self.assertNotIn("未验正文", lines[2], lines)

    async def test_an_unreachable_source_stops_buying_deep_passes_after_three(self):
        """浅遍深遍都没结论（超时那一类）：试满三次就歇着，不许每轮都赔一条深检。"""
        checker = _RecordingChecker(states={
            "https://e.example": {check_service.CHECK_DEPTH_LIST: "unknown",
                                  check_service.CHECK_DEPTH_FULL: "unknown"}})
        svc = _service(checker, _src("https://e.example"))
        for _round in range(4):
            checker.calls.clear()
            await svc.check_all()
            depths = checker.depths
            # 第 1 轮浅遍+深遍；第 2、3 轮队列直接把它送去深遍（浅遍不重跑）；
            # 第 4 轮预算花完 → 只剩浅遍重判，深遍一次都不派。
            if _round < 1:
                self.assertEqual(["list", "full"], depths)
            elif _round < 3:
                self.assertEqual(["full"], depths)
            else:
                self.assertEqual(["list"], depths, "试满配额还每轮重跑深遍 = 计数没生效")
        rows = svc._deep_queue.snapshot()
        self.assertEqual(3, rows[0]["attempts"], rows)
        self.assertTrue(rows[0]["exhausted"], rows)

    async def test_a_deep_verdict_takes_the_source_out_of_the_queue(self):
        """深遍给出明确结论（哪怕是"正文不可用"）就把这条摘掉，不留悬挂。"""
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_FULL: "content_unavailable"}})
        svc = _service(checker, _src("https://a.example"))
        await svc.check_all()
        self.assertEqual([], svc._deep_queue.snapshot())
        self.assertEqual(0, len(svc._deep_queue.pending()))

    async def test_the_queue_is_visible_in_the_replay_snapshot(self):
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unknown",
                                  check_service.CHECK_DEPTH_FULL: "unknown"}})
        svc = _service(checker, _src("https://a.example"))
        await svc.check_all()
        snap = await svc.run_snapshot()
        self.assertEqual(1, snap["tiering"]["plan_total"])
        self.assertEqual({"unknown_shallow"}, set(snap["tiering"]["by_reason"]))
        self.assertEqual([tiered_check.REASON_UNKNOWN_SHALLOW],
                         [row["reason"] for row in snap["deep_queue"]])
        self.assertEqual("auto", snap["depth"])

    async def test_check_all_takes_no_depth_anymore(self):
        """分级不是给用户选的档位：入口上没有 depth 这个形参。"""
        import inspect
        params = inspect.signature(SourceCheckService.check_all).parameters
        self.assertNotIn("depth", params,
                        "批量检测再接受一个用户选的档位 = 回到第六十二轮那个下拉")

    async def test_a_source_queued_for_the_deep_pass_is_audited_only_by_that_pass(self):
        """静态审计只跟着"会被留下的那一行"走：浅遍排进深检队列的那条不在浅遍审。

        审计读的是同一份规则、却是马上被深遍结论顶掉的那一行（`_merge_by_url`），
        审它等于每个深检源多付一次重 CPU 活。`watched` 是每一遍**结束时**的审计提交数：
        浅遍只该提交 a（b 还要被深遍重审），深遍再提交 b。把闸门撤掉就是 [2, 3]。
        """
        compat = _RecordingCompat()
        holder: dict = {}
        checker = _RecordingChecker(states={
            "https://a.example": {check_service.CHECK_DEPTH_LIST: "unusable"}},
            watch=lambda: holder["svc"]._audit_submitted)
        svc = _service(checker, _src("https://a.example"), _src("https://b.example"),
                       compat=compat)
        holder["svc"] = svc
        summary = await svc.check_all()
        self.assertEqual([1, 2], checker.watched, checker.watched)
        self.assertEqual(["https://a.example", "https://b.example"],
                         sorted(compat.audited), compat.audited)
        self.assertEqual(3, sum(len(urls) for _depth, urls in checker.calls),
                         "浅遍 2 行 + 深遍 1 行 = 3 行结果，审计只该有 2 次")
        self.assertEqual("unusable", summary["results"][0]["check_state"])

    async def test_single_source_check_still_asks_for_the_depth_it_was_given(self):
        """单源检测（换源、Web 桥那条路）档位仍由调用方定，不受批量分档影响。"""
        checker = _RecordingChecker()
        svc = _service(checker)
        await svc.check_source({"bookSourceUrl": "https://a.example",
                                "bookSourceName": "甲"}, depth="list")
        self.assertEqual(["list"], checker.depths)


class _FakeHub:
    def __init__(self, service):
        self._service = service
        self.bus = None

    def get(self, stype):
        return self._service


class _FakeCheckService:
    """SourceCheckService 替身：记 check_all 收到的参数，回放给定的快照。"""

    def __init__(self, snapshot: dict | None = None):
        self.checks: list = []
        self.snapshot = snapshot or {"running": False, "cancelled": False,
                                     "total": 0, "summary": None, "results": []}

    async def check_all(self, groups=None) -> dict:
        self.checks.append((groups,))
        return {"total": 0, "usable": 0, "blocked": 0}

    async def run_snapshot(self) -> dict:
        return self.snapshot

    async def await_run_finished(self) -> dict:
        return self.snapshot

    def cancel(self) -> None:
        pass


class _RecordingCheckView:
    """CheckView 替身：记界面收到的文案与运行态。"""

    def __init__(self):
        self.summaries: list = []
        self.running: list = []

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


def _snap(**overrides) -> dict:
    values = {"running": False, "cancelled": False, "total": 40,
              "depth": "auto", "deep_queue": [],
              "tiering": {"plan_total": 40, "shallow": 30, "deep": 10,
                          "carried": 0, "by_reason": {"list_usable": 10}},
              "summary": {"total": 40, "usable": 12, "blocked": 1},
              "results": []}
    values.update(overrides)
    return values


class ControllerTieringTests(unittest.IsolatedAsyncioTestCase):
    """界面面：点开始不再传档位，收尾那一行报的是这一轮实际怎么分的。"""

    def _attach(self, service=None):
        from app.controllers.source.check_controller import CheckController
        service = service or _FakeCheckService()
        view = _RecordingCheckView()
        controller = CheckController(_FakeHub(service))   # type: ignore[arg-type]
        scheduled: list = []
        controller.ui_call = scheduled.append             # type: ignore[assignment]
        controller.attach(view)
        return controller, view, service, scheduled

    async def test_start_dispatches_the_batch_without_a_depth(self) -> None:
        controller, _view, service, scheduled = self._attach()
        await scheduled[0]                    # attach 的回放
        controller.start()
        await scheduled[-1]
        self.assertEqual([(None,)], service.checks)

    async def test_the_finished_line_reports_the_split_this_round_made(self):
        """收尾不能说"完整/快速"了——两遍都跑过，要说各跑了多少、还欠多少。"""
        controller, view, _service, scheduled = self._attach(
            _FakeCheckService(_snap(deep_queue=[{"raw_url": "https://x", "reason": "unknown_shallow",
                                                 "attempts": 1, "exhausted": False}])))
        await scheduled[0]
        self.assertIn("快检 30 源", view.summaries[-1])
        self.assertIn("补验正文 10 源", view.summaries[-1])
        self.assertIn("待深检 1 源", view.summaries[-1])

    async def test_a_cancelled_round_still_says_it_was_stopped(self):
        controller, view, _service, scheduled = self._attach(
            _FakeCheckService(_snap(cancelled=True)))
        await scheduled[0]
        self.assertEqual("检测已停止", view.summaries[-1])

    async def test_the_replay_does_not_claim_a_single_tier_for_a_two_pass_round(self):
        """一轮里两遍都跑了，收尾那行不该再说"本轮快速检测，未验正文"。"""
        controller, view, _service, scheduled = self._attach(
            _FakeCheckService(_snap()))
        await scheduled[0]
        self.assertNotIn("快速检测，未验正文", view.summaries[-1])
        self.assertIn("检测完成", view.summaries[-1])


class ViewTieringTests(unittest.TestCase):
    """视图面：档位不再由人点（第六十二轮那颗下拉已撤）。"""

    @staticmethod
    def _toolbar(view):
        return view.controls[1].controls

    @staticmethod
    def _walk(control):
        yield control
        for child in getattr(control, "controls", None) or []:
            yield from ViewTieringTests._walk(child)

    def test_the_view_offers_no_depth_choice(self) -> None:
        from app.views.source.check_view import CheckView
        self.assertEqual([], [c for c in self._walk(CheckView())
                              if isinstance(c, ft.Dropdown)],
                         "分档是检测自己判的，界面上不该再有可选的档位")

    def test_start_click_calls_the_callback_with_no_arguments(self) -> None:
        """`on_start` 的签名里没有档位：多传一个参数就该当场 TypeError。"""
        from app.views.source.check_view import CheckView
        seen: list = []
        view = CheckView()
        view.on_start = lambda: seen.append("started")      # type: ignore[assignment]
        start = next(c for c in self._toolbar(view)
                     if isinstance(c, ft.FilledButton))
        start.on_click(None)
        self.assertEqual(["started"], seen)

    def test_running_toggles_the_two_buttons(self) -> None:
        from app.views.source.check_view import CheckView
        view = CheckView()
        self.assertFalse(view._start_btn.disabled)
        self.assertTrue(view._stop_btn.disabled)
        view.set_running(True)
        self.assertTrue(view._start_btn.disabled)
        self.assertFalse(view._stop_btn.disabled)
        view.set_running(False)
        self.assertFalse(view._start_btn.disabled)


if __name__ == "__main__":
    unittest.main()
