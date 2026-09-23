"""正文规则自愈：候选挖掘、回放验证、写回与两处入口。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import flet as ft

from app.models.book_source import BookSource
from app.services import rule_repair
from app.services.reading_engine import ContentEmptyError
from app.services.rule_engine import ChapterItem, UnsupportedRule
from app.services.rule_repair import (ProbeEngine, diagnose_content_rule,
                                      extract_candidates, merge_content_rule,
                                      rule_needs_script)
from app.services.source_service import SourceService
from app.views.check_view import CheckView
from app.views.reading_view import ReadingView
from app.views.rule_repair_ui import show_fix_dialog

SAMPLE_PAGE = "https://www.52shuku.net/bl/23_b/bkfks_2.html"
SECOND_PAGE = "https://www.52shuku.net/bl/23_b/bkfks_12.html"
THIRD_PAGE = "https://www.52shuku.net/bl/23_b/bkfks_101.html"
# 正文里有可见的 http 链接：站点模板常见的那种"本章未完"提示。
PROSE = [
    "萧青梧睁开眼的时候，长安城的晨鼓刚敲过第三通，坊门外的胡饼摊已经支起了炉子，"
    "热气在冷风里散成一片白雾，远处传来驼铃，一声一声压在青石板上，闷得很。",
    "他摸了摸胸口，那里藏着一枚从时空管理局带出来的空间钮，冰凉得像一块铁，"
    "却比这世上任何东西都可靠；钮里装着半亩良田、一间书库，还有他攒了三年的药。",
    "昨夜他还在图书馆里翻《旧唐书》，一页没读完便趴在桌上睡了过去；再醒过来时，"
    "身下是硬邦邦的木板床，头顶是熏得发黑的房梁，窗外有妇人高声叫卖胡饼。",
    "门外传来脚步声，一个穿青色短褐的少年探头进来，见他醒了便笑，"
    "说杜郎君你可算醒了，李学士在厅里等你半晌，再不去，那壶新酒可就要凉透了。",
    "离得近了才看清，少年腰间挂着一枚铜牌，上面刻着的名字他认得——"
    "那是史书里只出现过两次的人，每一次出现，都掀起了足以改写朝代的风波。",
]
TRAP = "本章未完，请访问 http://www.example.com/bl/23_b/bkfks_3.html 继续阅读下一章。"


def sample_page(*paragraphs: str, extra: str = "") -> str:
    """一页正文：带导航、侧栏和版权，中间是 WordPress 风格的正文容器。"""
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return (
        "<html><head><title>第2页</title></head><body>"
        '<div class="nav"><a href="/">首页</a><a href="/top/">排行榜</a></div>'
        '<div class="wrap"><div class="crumbs">位置：首页 &gt; 历史同人</div>'
        f'<div class="article-content">{body}</div>'
        f'<div class="tips"><a href="http://www.example.com">本站已启用新域名</a></div>'
        '<div class="sidebar"><a href="/bl/1_b/aaaa.html">推荐书一</a>'
        '<a href="/bl/2_b/bbbb.html">推荐书二</a></div></div>'
        f"{extra}</body></html>")


PAGE_ONE = sample_page(*PROSE, TRAP)
PAGE_TWO = sample_page(*PROSE)
PAGE_THREE = sample_page(*PROSE, TRAP)

SOURCE = {
    "bookSourceName": "示例书源",
    "bookSourceUrl": "https://www.po5.net",
    "bookSourceType": 0,
    "enabled": True,
    "ruleContent": {
        "content": "#nr1@html",
        "replaceRegex": "##目录.*尾页|Tips：.*|传送门：.*|Top|.*http.*",
    },
}


def chapter(name: str, url: str) -> ChapterItem:
    return ChapterItem(name, url)


class _Transport:
    """按 URL 发样本页的假传输：没登记过的地址一律给空页。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requests: list[str] = []

    async def __call__(self, request, source=None):
        url = str(request.get("url") or "")
        self.requests.append(url)
        page = self.pages.get(url)
        if page is None:
            return "", "text/html", url
        return page, "text/html", url


async def run_diagnosis(source, primary, extras=(), pages=None, engine=None):
    pages = pages if pages is not None else {primary.url: PAGE_ONE}
    own = engine is None
    engine = engine or ProbeEngine(timeout=5, background=True)
    engine._request_transport = _Transport(pages)
    try:
        report = await diagnose_content_rule(
            source, primary, extra_chapters=list(extras), engine=engine)
    finally:
        if own:
            await engine.aclose()
    return report, engine


class CandidateMiningTests(unittest.TestCase):
    def test_the_article_wins_over_navigation_and_sidebars(self):
        candidates = extract_candidates(PAGE_ONE)
        self.assertTrue(any("article-content" in item for item in candidates))
        self.assertFalse(any("sidebar" in item or "nav" in item
                             for item in candidates))
        self.assertTrue(candidates[0].startswith(".article-content"))

    def test_a_page_without_prose_offers_nothing(self):
        page = ('<html><body><div class="nav"><a href="/">首页</a></div>'
                "</body></html>")
        self.assertEqual(extract_candidates(page), [])
        self.assertEqual(rule_repair.region_text_length(page), 0)

    def test_the_region_is_long_enough_to_fix(self):
        self.assertGreaterEqual(rule_repair.region_text_length(PAGE_ONE),
                                rule_repair.CONTENT_FIX_MIN_CHARS)

    def test_merging_keeps_the_original_rule_first(self):
        merged = merge_content_rule("#nr1@html", ".article-content@p@text")
        self.assertEqual(merged, "#nr1@html||.article-content@p@text")
        self.assertEqual(
            merge_content_rule(merged, ".article-content@p@text"), merged)
        self.assertEqual(merge_content_rule("", ".a@text"), ".a@text")
        self.assertEqual(merge_content_rule("#nr1@html", ""), "#nr1@html")

    def test_script_rules_are_not_selector_fixable(self):
        self.assertTrue(rule_needs_script(BookSource.from_dict(
            {"ruleContent": {"content": "@js:java.get('x')"}})))
        self.assertTrue(rule_needs_script(BookSource.from_dict(
            {"ruleContent": {"content": ".a@text", "webJs": "wait(1)"}})))
        self.assertFalse(rule_needs_script(BookSource.from_dict(
            {"ruleContent": {"content": "#nr1@html||.a@p@text"}})))


class ProbeEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_serves_the_sample_page_and_never_goes_out(self):
        engine = ProbeEngine(timeout=5, background=True)
        try:
            engine.arm((PAGE_ONE, "text/html", SAMPLE_PAGE))
            # 同一路径换域名（站点 302 之后）也要认。
            self.assertEqual(
                (await engine._request_transport(
                    {"url": SAMPLE_PAGE.replace("52shuku", "po5")}))[0], PAGE_ONE)
            # 别的页不拿样本页顶替，免得把候选的得分灌水。
            self.assertEqual(
                (await engine._request_transport(
                    {"url": SECOND_PAGE}))[0], "")
            self.assertEqual(
                (await engine._request_transport(
                    {"url": "https://www.52shuku.net/other.html"}))[0], "")
            engine.arm(None)
            self.assertEqual(engine.fetches, 0)
        finally:
            await engine.aclose()


class DiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_broken_rule_gets_a_verified_fallback(self):
        source = BookSource.from_dict(SOURCE)
        extras = [chapter("第2页", SECOND_PAGE), chapter("第3页", THIRD_PAGE)]
        report, engine = await run_diagnosis(
            source, chapter("第1页", SAMPLE_PAGE), extras,
            {SAMPLE_PAGE: PAGE_ONE, SECOND_PAGE: PAGE_TWO, THIRD_PAGE: PAGE_THREE})
        try:
            self.assertIsNotNone(report.fix, report.reason)
            self.assertEqual(report.fix.new_value,
                             "#nr1@html||.article-content@p@text")
            self.assertEqual(report.fix.old_value, "#nr1@html")
            self.assertEqual([item.chapter for item in report.fix.samples],
                             ["第1页", "第2页", "第3页"])
            self.assertTrue(all(item.chars >= 200
                                for item in report.fix.samples))
            self.assertIn("萧青梧", report.fix.samples[0].snippet)
            # 复核只抓样本页：一页失败章节页 + 两页同书章节。
            self.assertEqual(engine.fetches, 3)
        finally:
            await engine.aclose()

    async def test_the_proposed_rule_really_parses_the_chapter(self):
        source = BookSource.from_dict(SOURCE)
        report, engine = await run_diagnosis(source, chapter("第1页", SAMPLE_PAGE))
        try:
            self.assertIsNotNone(report.fix, report.reason)
            fixed = BookSource.from_dict({
                **source.to_dict(),
                "ruleContent": {**source.ruleContent,
                                "content": report.fix.new_value}})
            document = await engine.content_document(
                fixed, chapter("第1页", SAMPLE_PAGE), "", need_save=False)
            text = "\n".join(str(block.value) for block in document.blocks)
            self.assertIn("萧青梧", text)
            self.assertIn("空间钮", text)
            self.assertNotIn("http", text)
            self.assertNotIn("<p>", text)
        finally:
            await engine.aclose()

    async def test_a_script_rule_is_left_alone(self):
        source = BookSource.from_dict({
            **SOURCE,
            "ruleContent": {"content": "@js:java.get('content')"}})
        report, engine = await run_diagnosis(source, chapter("第1页", SAMPLE_PAGE))
        try:
            self.assertIsNone(report.fix)
            self.assertIn("JS", report.reason)
            self.assertEqual(engine.fetches, 0)
        finally:
            await engine.aclose()

    async def test_a_page_without_prose_is_reported_as_such(self):
        source = BookSource.from_dict(SOURCE)
        thin = '<html><body><div class="nav"><a href="/">首页</a></div></body></html>'
        report, engine = await run_diagnosis(
            source, chapter("第1页", SAMPLE_PAGE), (), {SAMPLE_PAGE: thin})
        try:
            self.assertIsNone(report.fix)
            self.assertIn("样本页", report.reason)
        finally:
            await engine.aclose()

    async def test_a_selector_wiped_by_replace_regex_is_explained(self):
        source = BookSource.from_dict({
            **SOURCE,
            "ruleContent": {"content": "#nr1@html", "replaceRegex": ".*"}})
        report, engine = await run_diagnosis(source, chapter("第1页", SAMPLE_PAGE))
        try:
            self.assertIsNone(report.fix)
            self.assertIn("replaceRegex", report.reason)
            self.assertEqual(report.tried, report.tried)  # 试过候选才有这句
            self.assertGreater(report.tried, 0)
        finally:
            await engine.aclose()

    async def test_a_rule_that_works_here_gets_no_proposal(self):
        # 老规则在本地重放里能解析出正文：这次报错复现不出来，不该提议改动，
        # 否则改完旧分支照样赢在前面，用户看不到任何变化。
        source = BookSource.from_dict({
            **SOURCE,
            "ruleContent": {"content": ".article-content@p@text"}})
        report, engine = await run_diagnosis(source, chapter("第1页", SAMPLE_PAGE))
        try:
            self.assertIsNone(report.fix)
            self.assertEqual(report.reached, "replay")
            self.assertIn("没复现出这次报错", report.reason)
            self.assertEqual(engine.fetches, 1)
        finally:
            await engine.aclose()

    async def test_a_missing_content_rule_is_not_repairable(self):
        source = BookSource.from_dict({**SOURCE, "ruleContent": {}})
        report, engine = await run_diagnosis(source, chapter("第1页", SAMPLE_PAGE))
        try:
            self.assertIsNone(report.fix)
            self.assertIn("没有正文规则", report.reason)
            self.assertEqual(engine.fetches, 0)
        finally:
            await engine.aclose()


class WriteBackTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.service = SourceService(
            resource_dir=self.directory.name, data_dir=self.directory.name)
        self.service.sources = [
            BookSource.from_dict(SOURCE),
            BookSource.from_dict({**SOURCE, "bookSourceName": "另一个书源",
                                  "bookSourceUrl": "https://www.example.com"}),
        ]
        self.service.save()

    def tearDown(self):
        self.directory.cleanup()

    def test_a_rule_change_backs_up_the_file_and_touches_one_source(self):
        backup = self.service.update_source_rule(
            "https://www.po5.net", "content", "#nr1@html||.a@p@text")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.name, "bookSource.json.bak")
        data = json.loads(self.service.sources_path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["ruleContent"]["content"],
                         "#nr1@html||.a@p@text")
        self.assertEqual(data[0]["ruleContent"]["replaceRegex"],
                         SOURCE["ruleContent"]["replaceRegex"])
        self.assertEqual(data[1]["ruleContent"]["content"], "#nr1@html")
        reloaded = SourceService(resource_dir=self.directory.name,
                                 data_dir=self.directory.name)
        reloaded.load(self.service.sources_path)
        self.assertEqual(reloaded.sources[0].ruleContent["content"],
                         "#nr1@html||.a@p@text")

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(KeyError):
            self.service.update_source_rule(
                "https://missing.example", "content", ".a@text")


class FixDialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirming_the_dialog_writes_the_rule_back(self):
        applied = []
        service = SimpleNamespace(
            update_source_rule=lambda url, field, value: applied.append(
                (url, field, value)) or Path("bookSource.json.bak"))
        dialogs, snack_bars = [], []
        page = SimpleNamespace(show_dialog=dialogs.append,
                               pop_dialog=lambda: None,
                               session=object(),
                               show_snack_bar=snack_bars.append)
        fix = rule_repair.RuleFix(
            "示例书源", "https://www.po5.net", "content", "#nr1@html",
            "#nr1@html||.article-content@p@text",
            (rule_repair.RepairSample("第1页", SAMPLE_PAGE, 3144, "萧青梧…"),),
            SAMPLE_PAGE)
        show_fix_dialog(page, service, fix, on_applied=lambda: applied.append("重试"))
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        texts = [item.value for item in dialog.content.content.controls]
        self.assertIn("#nr1@html||.article-content@p@text", texts)
        self.assertIn("#nr1@html", texts)
        self.assertTrue(any("第1页（3144 字）" in item for item in texts))
        self.assertTrue(any("萧青梧…" in item for item in texts))
        dialog.actions[1].on_click(None)
        self.assertEqual(applied[0], ("https://www.po5.net", "content",
                                      "#nr1@html||.article-content@p@text"))
        self.assertEqual(applied[-1], "重试")


class _ReadingViewStub:
    """只借 ReadingView 的两个纯方法，不搭整个页面。"""


class ReaderEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        service = SimpleNamespace(
            data_dir=Path(self.directory.name), sources=[], bookshelf=[],
            category_checks={}, save=lambda: True)
        self.view = ReadingView(service)

    async def asyncTearDown(self):
        await self.view.engine.aclose()
        self.directory.cleanup()

    async def test_the_empty_body_error_offers_the_rule_fix(self):
        self.view.current_source = BookSource.from_dict(SOURCE)
        chapter_item = chapter("第1页", SAMPLE_PAGE)
        label, handler = self.view._repair_action(
            ContentEmptyError("chapter content is empty"), chapter_item)
        self.assertEqual(label, "查找正文规则")
        self.assertIsNotNone(handler)

    async def test_other_errors_and_script_sources_keep_the_plain_retry(self):
        chapter_item = chapter("第1页", SAMPLE_PAGE)
        self.assertEqual(
            self.view._repair_action(UnsupportedRule("no"), chapter_item),
            (None, None))
        self.view.current_source = None
        self.assertEqual(
            self.view._repair_action(
                ContentEmptyError("chapter content is empty"), chapter_item),
            (None, None))
        self.view.current_source = BookSource.from_dict(
            {**SOURCE, "ruleContent": {"content": "@js:java.get('x')"}})
        self.assertEqual(
            self.view._repair_action(
                ContentEmptyError("chapter content is empty"), chapter_item),
            (None, None))

    async def test_samples_come_from_the_same_book_and_skip_the_failing_one(self):
        self.view.chapters = [chapter(f"第{index}章", f"https://x/{index}")
                              for index in range(1, 10)]
        failing = self.view.chapters[0]
        samples = self.view._repair_samples(failing)
        self.assertEqual(len(samples), 2)
        self.assertNotIn(failing, samples)
        self.assertTrue(all(item.url for item in samples))


class CheckViewEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        service = SimpleNamespace(
            sources=[BookSource.from_dict(SOURCE)], category_checks={},
            data_dir=Path(self.directory.name), save=lambda: True)
        self.view = CheckView(service)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def _result(self, **overrides):
        result = {"name": "示例书源", "raw_url": "https://www.po5.net",
                  "status": "ok", "search_status": "ok",
                  "check_state": "content_unavailable",
                  "reading_stage": "content", "reading_status": "rule_error",
                  "reading_error": "正文解析结果为空",
                  "sample_chapter": "第1页",
                  "sample_chapter_urls": [SAMPLE_PAGE, SECOND_PAGE]}
        result.update(overrides)
        return result

    async def test_a_failed_content_stage_gets_a_repair_button(self):
        row = self.view._build_row(self._result())
        self.assertIsNotNone(row.trailing)
        self.assertIn("正文规则", row.trailing.tooltip)

    async def test_network_failures_and_script_rules_have_no_button(self):
        self.assertIsNone(
            self.view._build_row(self._result(reading_status="network_error")).trailing)
        self.assertIsNone(
            self.view._build_row(self._result(reading_stage="catalog")).trailing)
        self.assertIsNone(
            self.view._build_row(self._result(sample_chapter_urls=[])).trailing)
        self.view.service.sources = [BookSource.from_dict(
            {**SOURCE, "ruleContent": {"content": "@js:java.get('x')"}})]
        self.assertIsNone(self.view._build_row(self._result()).trailing)

    async def test_the_button_diagnoses_the_source_and_waits_for_confirmation(self):
        calls = []

        async def fake_diagnose(page, service, source, chapter_item, **kwargs):
            calls.append((source.bookSourceUrl, chapter_item.url,
                          [item.url for item in kwargs["extra_chapters"]]))
            return True

        scheduled = []

        def run_task(handler, *args):
            scheduled.append(handler(*args))

        page = SimpleNamespace(run_task=run_task,
                               update=lambda *args, **kwargs: None)
        with patch("app.views.check_view.diagnose_and_offer",
                   side_effect=fake_diagnose), \
                patch.object(CheckView, "page", new_callable=PropertyMock,
                             return_value=page):
            self.view._start_repair(self._result())
            for task in scheduled:
                await task
        self.assertEqual(calls,
                         [("https://www.po5.net", SAMPLE_PAGE, [SECOND_PAGE])])
        self.assertFalse(self.view._repairing)

    async def test_a_second_diagnosis_waits_for_the_first(self):
        notices = []
        self.view._repairing = True
        self.view._show_notice = notices.append
        scheduled = []
        page = SimpleNamespace(run_task=lambda handler, *args: scheduled.append(handler))
        with patch.object(CheckView, "page", new_callable=PropertyMock,
                          return_value=page):
            self.view._start_repair(self._result())
        self.assertEqual(scheduled, [])
        self.assertIn("还在诊断中", notices[0])

    async def test_a_running_check_defers_the_automatic_recheck(self):
        notices = []
        self.view._show_notice = notices.append
        self.view._running = True
        self.view._recheck_after_repair(BookSource.from_dict(SOURCE))
        self.assertIsNone(self.view._retry_targets)
        self.assertIn("等这轮检测跑完", notices[0])
