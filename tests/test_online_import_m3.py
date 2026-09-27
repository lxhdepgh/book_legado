"""M3 OnlineImportService 测试（§1.16 / 契约 §7/§10，≥5 条）。

不真联网：HTTP 拉取用注入的 fetcher 替身（读本地字符串）；SourceService /
CompatAuditService 用 AsyncMock 替身。覆盖：

1. 书源文件导入：分发到 SourceService.import_sources_json 并计数；
2. 书源导入即审：CompatAuditService 缺口汇总为 warning，且不阻断导入；
3. 懒路由：未就绪 kind（rss/replace_rules…）报占位，count=0；
4. 未知 kind：报错且不调度任何服务；
5. URL 导入：经注入 fetcher 拉取后分发；
6. 完成发布 ImportFinishedEvent（kind/count/errors）；
7. JSON 非法时记 error 且不落库。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.events import ImportFinishedEvent
from app.services.sources.online_import_service import (
    OnlineImportService, preview_book_source_text)


class _RecordingBus:
    """记录 publish 的假总线。"""

    def __init__(self):
        self.published: list = []

    def publish(self, event):
        self.published.append(event)
        return []


def _make_service(source=None, audit=None, fetcher=None):
    svc = OnlineImportService(
        source_service=source, audit_service=audit, http_fetcher=fetcher)
    svc.bus = _RecordingBus()
    return svc


_GOOD = json.dumps([
    {"bookSourceName": "源甲", "bookSourceUrl": "https://a/",
     "bookSourceGroup": "默认"},
    {"bookSourceName": "源乙", "bookSourceUrl": "https://b/",
     "bookSourceGroup": "默认"},
], ensure_ascii=False)


class OnlineImportTests(unittest.IsolatedAsyncioTestCase):

    async def test_book_source_file_import_delegates_and_counts(self):
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))
        audit = SimpleNamespace(audit_source=AsyncMock(return_value=SimpleNamespace(
            missing_java_apis=[])))
        svc = _make_service(source=source, audit=audit)

        result = await svc.import_from_file(_GOOD, "book_source")

        source.import_sources_json.assert_awaited_once()
        self.assertEqual(result.count, 2)
        self.assertFalse(result.errors)
        # 发了 ImportFinishedEvent
        self.assertEqual(len(svc.bus.published), 1)
        event = svc.bus.published[0]
        self.assertIsInstance(event, ImportFinishedEvent)
        self.assertEqual(event.kind, "book_source")
        self.assertEqual(event.count, 2)

    async def test_import_audit_runs_once_per_source_not_twice(self):
        """导入即审只跑一遍：门面对外那条（要出 warning）或落库那条，二选一。

        重构后两条都在跑 → 几百条的源文件审两遍（CPU 白烧一倍），更糟的是
        ``audit_log`` 每个源落两行，回溯时分不清哪行是这次导入的。
        归属见 ``SourceService.import_sources_json`` 的 ``audit`` 参数。
        """
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))
        audit = SimpleNamespace(audit_source=AsyncMock(return_value=SimpleNamespace(
            missing_java_apis=[])))
        svc = _make_service(source=source, audit=audit)

        await svc.import_from_file(_GOOD, "book_source")

        self.assertEqual(audit.audit_source.await_count, 2)   # 每个源一次
        source.import_sources_json.assert_awaited_once_with(_GOOD, "append",
                                                            audit=False)

    async def test_import_audit_warning_nonblocking(self):
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))
        # 审计报出缺口 → 必须成为 warning，但导入照常完成
        audit = SimpleNamespace(audit_source=AsyncMock(return_value=SimpleNamespace(
            missing_java_apis=["ajax", "webView", "eval", "Hack"])))
        svc = _make_service(source=source, audit=audit)

        result = await svc.import_from_file(_GOOD, "book_source")

        self.assertEqual(result.count, 2)              # 不阻断
        self.assertTrue(any("missing_java_apis" in w for w in result.warnings),
                        f"审计缺口应汇总为 warning：{result.warnings}")

    async def test_pending_kind_placeholder(self):
        source = SimpleNamespace(import_sources_json=AsyncMock())
        svc = _make_service(source=source)

        result = await svc.import_from_file("anything", "rss")

        self.assertTrue(result.placeholder)
        self.assertEqual(result.count, 0)
        # 说明要给人看：不再往界面里漏 "待 M4 就绪" 这类里程碑黑话
        self.assertTrue(any("RSS" in w for w in result.warnings),
                        f"占位说明应可读：{result.warnings}")
        self.assertFalse(any("M4" in w or "M5" in w for w in result.warnings))
        source.import_sources_json.assert_not_awaited()  # 未就绪不落库
        # 占位提示仍经事件发出
        event = svc.bus.published[0]
        self.assertEqual(event.kind, "rss")

    async def test_unknown_kind_errors(self):
        source = SimpleNamespace(import_sources_json=AsyncMock())
        svc = _make_service(source=source)

        result = await svc.import_from_file("x", "nonsense")

        self.assertTrue(result.errors)
        source.import_sources_json.assert_not_awaited()

    async def test_url_import_via_injected_fetcher(self):
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))
        audit = SimpleNamespace(audit_source=AsyncMock(return_value=SimpleNamespace(
            missing_java_apis=[])))

        async def fake_fetch(url, headers):
            assert "example.com" in url
            return _GOOD

        svc = _make_service(source=source, audit=audit, fetcher=fake_fetch)
        result = await svc.import_from_url("https://example.com/source.json",
                                           "book_source")
        self.assertEqual(result.count, 2)
        source.import_sources_json.assert_awaited_once()

    async def test_broken_json_reports_error_without_storage(self):
        source = SimpleNamespace(import_sources_json=AsyncMock())
        svc = _make_service(source=source)

        result = await svc.import_from_file("{not json", "book_source")

        self.assertTrue(result.errors)
        self.assertEqual(result.count, 0)
        source.import_sources_json.assert_not_awaited()

    async def test_txt_rules_no_longer_misrouted_to_book_sources(self):
        """txt_rules 曾被当作"即时可用"塞进书源导入通道。

        TXT 目录规则的 JSON 里没有 bookSourceUrl，落库会生成一条空 url 的垃圾书源
        （认不出也删不掉，还会互相撞成同一条）。项目里没有它的存储落点，所以
        现在归入"暂无导入落点"，只发说明不落库。
        """
        source = SimpleNamespace(import_sources_json=AsyncMock())
        svc = _make_service(source=source)

        result = await svc.import_from_file(_GOOD, "txt_rules")

        self.assertTrue(result.placeholder)
        self.assertEqual(result.count, 0)
        source.import_sources_json.assert_not_awaited()

    async def test_import_from_url_carries_mode(self):
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))

        async def fake_fetch(url, headers):
            return _GOOD

        svc = _make_service(source=source, fetcher=fake_fetch)
        await svc.import_from_url("https://example.com/source.json",
                                 "book_source", mode="replace")
        source.import_sources_json.assert_awaited_once_with(_GOOD, "replace",
                                                            audit=True)


class ImportPreviewTests(unittest.TestCase):
    """``preview_book_source_text``：导入前的预览快照（旧单体有，重构后整块丢了）。"""

    def test_counts_groups_and_broken_entries(self):
        text = json.dumps([
            {"bookSourceName": "甲", "bookSourceUrl": "https://a/",
             "bookSourceGroup": "网文"},
            {"bookSourceName": "乙", "bookSourceUrl": "https://b/",
             "bookSourceGroup": "网文"},
            {"bookSourceName": "丙", "bookSourceUrl": "https://c/"},
            {"bookSourceName": "没有地址"},
        ], ensure_ascii=False)
        info = preview_book_source_text(text)
        self.assertEqual((info["total"], info["valid"], info["broken"]),
                         (4, 3, 1))
        self.assertEqual(info["groups"], [("网文", 2), ("未分组", 1)])
        self.assertEqual(info["error"], "")

    def test_unparsable_file_reports_error(self):
        info = preview_book_source_text("{ not json")
        self.assertTrue(info["error"])
        self.assertEqual(info["valid"], 0)

    def test_file_with_no_book_source_at_all_reports_error(self):
        """全是缺地址的条目（选错文件）：预览直接报错，不给一张"可导入 0 条"的卡。"""
        info = preview_book_source_text(json.dumps(
            [{"ruleName": "目录1", "url": "https://a/"},
             {"ruleName": "目录2", "url": "https://b/"}], ensure_ascii=False))
        self.assertEqual((info["total"], info["valid"]), (2, 0))
        self.assertIn("bookSourceUrl", info["error"])
        self.assertEqual(info["groups"], [])


class ImportProgressAndModeTests(unittest.TestCase):
    """进度事件与导入模式的接线。"""

    def test_url_fetch_publishes_stage_progress(self):
        """URL 导入的下载阶段也得有一拍进度，否则点完按钮像没反应。"""
        async def fake_fetch(url, headers):
            return _GOOD

        svc = _make_service(
            source=SimpleNamespace(
                import_sources_json=AsyncMock(return_value=(2, []))),
            fetcher=fake_fetch)
        asyncio.run(svc.import_from_url("https://example.com/s.json",
                                        "book_source"))
        stages = [ev for ev in svc.bus.published
                  if type(ev).__name__ == "ImportProgressEvent"]
        self.assertTrue(stages, svc.bus.published)
        self.assertEqual(stages[0].phase, "拉取在线文件")
        self.assertEqual(stages[0].total, 0)      # 拉取阶段给不出条数 → 不定长

    def test_local_file_import_passes_mode(self):
        source = SimpleNamespace(import_sources_json=AsyncMock(return_value=(2, [])))
        svc = _make_service(source=source)
        asyncio.run(svc.import_from_file(_GOOD, "book_source", "replace"))
        source.import_sources_json.assert_awaited_once_with(_GOOD, "replace",
                                                            audit=True)

    def test_pending_kind_never_reaches_storage(self):
        source = SimpleNamespace(import_sources_json=AsyncMock())
        svc = _make_service(source=source)
        result = asyncio.run(svc.import_from_file(_GOOD, "theme", "replace"))
        self.assertTrue(result.placeholder)
        source.import_sources_json.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
