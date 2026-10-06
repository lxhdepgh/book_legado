"""RSS 订阅页（#34）：服务角标/进度事件 + 界面链路，全部离线。

RssService 早就齐了，缺的是"有人调用它"：本文件把两条断链各钉一遍。

- 服务侧用**真实** :class:`StorageService`（tmp 数据根）+ 注入的本地 fetcher，
  和 ``test_rss_m4.py`` 同一套路：不联网，feedparser 真解析本地 XML；
- 界面侧用内存替身服务，跑真视图/真控制器：动作 → 服务 → 重绘（跨线程派发，
  一律轮询等待，不用 sleep 猜时间）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import (RssRefreshedEvent, RssRefreshProgressEvent)
from app.core.registry import (DEST_RSS, GROUP_READING, NAV_META, ViewRegistry)
from app.core.storage import StorageService
from app.services.content.rss_service import RssService

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>测试频道</title>
  <item><title>文章一</title><link>https://example.com/a/1</link>
    <description>&lt;p&gt;第一段&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</description>
  </item>
  <item><title>文章二</title><link>https://example.com/a/2</link>
    <description>正文二</description>
  </item>
</channel></rss>
"""


# ====================================================================== #
# 1. 服务侧：角标数据 + 批量刷新进度事件
# ====================================================================== #
class _Env:
    """真实存储 + 真事件总线 + 注入 fetcher 的 RssService（离线）。"""

    _ENV_KEYS = ("LEGADO_BOOKSOURCE_DATA_DIR", "LEGADO_BOOKSOURCE_CACHE_DIR")

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rss_ui_"))
        # 数据根必须落进 tmp：不能改完不还，否则后面的测试读到本用例的库。
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = str(self.root / "data")
        os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = str(self.root / "cache")
        self.feed_file = self.root / "feed.xml"
        self.feed_file.write_text(SAMPLE_FEED, encoding="utf-8")
        self.progress: list[RssRefreshProgressEvent] = []
        self.refreshed: list[RssRefreshedEvent] = []

    async def __aenter__(self):
        self.storage = StorageService()
        await self.storage.start()
        self.bus = EventBus(engine=engine_hub())
        await self.bus.start()
        self._unsubs = [
            self.bus.subscribe(RssRefreshProgressEvent, self._collect_progress),
            self.bus.subscribe(RssRefreshedEvent, self._collect_refreshed),
        ]

        async def fetcher(url: str) -> str:
            return self.feed_file.read_text(encoding="utf-8")

        self.svc = RssService(storage=self.storage, fetcher=fetcher)
        self.svc.bus = self.bus
        return self

    async def __aexit__(self, *exc):
        for unsubscribe in self._unsubs:
            unsubscribe()
        await self.bus.stop()
        await self.storage.stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)

    async def _collect_progress(self, ev) -> None:
        self.progress.append(ev)

    async def _collect_refreshed(self, ev) -> None:
        self.refreshed.append(ev)


async def _wait(predicate, timeout: float = 8.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时：{what}")


class RssServiceSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_article_counts_gives_badges(self):
        async with _Env() as env:
            sid = await env.svc.add_source("测试源", "https://example.com/feed")
            self.assertEqual(await env.svc.article_counts(), {})   # 没文章 = 没行
            await env.svc.refresh_source(sid)
            counts = await env.svc.article_counts()
            self.assertEqual(counts[sid]["total"], 2)
            self.assertEqual(counts[sid]["unread"], 2)
            article = (await env.svc.list_articles(sid))[0]
            await env.svc.mark_read(article["id"])
            counts = await env.svc.article_counts()
            self.assertEqual(counts[sid]["unread"], 1)

    async def test_refresh_all_publishes_progress_in_order(self):
        async with _Env() as env:
            first = await env.svc.add_source("甲", "https://example.com/feed/a")
            second = await env.svc.add_source("乙", "https://example.com/feed/b")
            new = await env.svc.refresh_all()
            # 投递是异步的（EventBus 把处理器 submit 到引擎循环）：等的是"末拍进度
            # 事件真的到了"，不是"刷新事件到了几条"——后者先满足，断言就会跑在前面。
            await _wait(lambda: any(ev.done == 2 for ev in env.progress),
                        what="末拍进度事件")
            await _wait(lambda: len(env.refreshed) >= 2, what="逐源刷新事件")
            self.assertEqual(new, 4)
            done = [ev.done for ev in env.progress]
            self.assertEqual(done, sorted(done), "进度只能往前走")
            self.assertEqual(env.progress[0].total, 2)
            self.assertEqual(done[0], 0)
            self.assertEqual(env.progress[-1].done, 2)
            # 每条进度都带着"正在刷哪个源"，界面才有话可说
            self.assertIn(env.progress[1].current, ("甲", "乙"))
            self.assertTrue(all(ev.total == 2 for ev in env.progress))
            del first


# ====================================================================== #
# 2. 界面替身
# ====================================================================== #
class _RssStub:
    """RssService 替身：内存两张表，方法签名与返回形状对齐真服务。"""

    def __init__(self, sources=None, articles=None, fail: dict | None = None):
        self.sources = sources if sources is not None else [
            {"id": "feed-a", "name": "甲源", "url": "https://a/feed",
             "enabled": True, "group": "", "custom_order": 0, "type": 0,
             "last_update_time": 0.0},
            {"id": "feed-b", "name": "乙源", "url": "https://b/feed",
             "enabled": True, "group": "", "custom_order": 0, "type": 0,
             "last_update_time": 0.0},
        ]
        self.articles = articles if articles is not None else {
            "feed-a": [
                {"id": 1, "source_id": "feed-a", "title": "文章一",
                 "url": "https://a/1", "author": "张三",
                 "pub_date": "2025-01-01", "content": "<p>第一段</p>",
                 "image": "", "read": False, "starred": False,
                 "updated_at": 0.0},
                {"id": 2, "source_id": "feed-a", "title": "文章二",
                 "url": "https://a/2", "author": "", "pub_date": "2025-01-02",
                 "content": "", "image": "", "read": True, "starred": True,
                 "updated_at": 0.0},
            ],
            "feed-b": [],
        }
        self.fail = fail or {}
        self.calls: list = []
        self.counts = {"feed-a": {"total": 2, "unread": 1}}

    # ---- 读 ---- #
    async def list_sources(self):
        self.calls.append(("list_sources",))
        return list(self.sources)

    async def article_counts(self):
        self.calls.append(("article_counts",))
        return {k: dict(v) for k, v in self.counts.items()}

    async def list_articles(self, source_id, unread_only=False):
        self.calls.append(("list_articles", str(source_id), bool(unread_only)))
        rows = list(self.articles.get(str(source_id) or "", []))
        return [a for a in rows if not a["read"]] if unread_only else rows

    # ---- 写 ---- #
    async def add_source(self, name, url, source_group=""):
        self.calls.append(("add_source", name, url))
        if "add_source" in self.fail:
            raise self.fail["add_source"]
        sid = str(url)
        self.sources.append({"id": sid, "name": name or url, "url": url,
                             "enabled": True, "group": source_group,
                             "custom_order": 0, "type": 0,
                             "last_update_time": 0.0})
        self.articles.setdefault(sid, [])
        return sid

    async def remove_source(self, source_id):
        self.calls.append(("remove_source", str(source_id)))
        before = len(self.sources)
        self.sources = [s for s in self.sources if s["id"] != str(source_id)]
        self.articles.pop(str(source_id), None)
        self.counts.pop(str(source_id), None)
        return len(self.sources) < before

    async def refresh_source(self, source_id):
        self.calls.append(("refresh_source", str(source_id)))
        if "refresh_source" in self.fail:
            raise self.fail["refresh_source"]
        return 1

    async def refresh_all(self):
        self.calls.append(("refresh_all",))
        if "refresh_all" in self.fail:
            raise self.fail["refresh_all"]
        await asyncio.sleep(0.05)
        return 3

    async def mark_read(self, article_id):
        self.calls.append(("mark_read", int(article_id)))
        for rows in self.articles.values():
            for row in rows:
                if row["id"] == int(article_id):
                    row["read"] = True
                    return True
        return False

    async def star(self, article_id, starred):
        self.calls.append(("star", int(article_id), bool(starred)))
        for rows in self.articles.values():
            for row in rows:
                if row["id"] == int(article_id):
                    row["starred"] = bool(starred)
                    return True
        return False


class _FakeHub:
    def __init__(self, services: dict, bus=None):
        self._services = services
        self.bus = bus

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


def _attach(stub):
    from app.controllers.rss_controller import RssController
    from app.views.reading.rss_view import RssView
    hub = _FakeHub({RssService: stub} if stub is not None else {})
    view = RssView()
    controller = RssController(hub)
    controller.attach(view)
    return controller, view


def _rows(list_view) -> list:
    return list(list_view.controls)


def _text_of(control) -> str:
    """控件文本：ft.Text 的 repr 不含 value（``Text(40 - 0x…)``），只能读 .value。"""
    return str(getattr(control, "value", "") or "")


class RssPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_loads_sources_and_articles_of_first(self):
        stub = _RssStub()
        _controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="源列表")
        await _wait(lambda: len(_rows(view._articles)) == 2, what="文章列表")
        # 角标来自 article_counts（feed-a 未读 1）；未读写在副标题里，
        # 不再放 trailing 的 Row（那会把源名挤没，见第七十八轮像素）
        self.assertIn("1 未读", view._sources.controls[0].subtitle.value)
        self.assertIn("2 篇", view._sources.controls[0].subtitle.value)
        self.assertIn("甲源", view._sources.controls[0].title.value)

    async def test_empty_store_explains_what_to_do(self):
        stub = _RssStub(sources=[], articles={})
        _controller, view = _attach(stub)
        await _wait(lambda: view._sources.controls and _text_of(
            view._sources.controls[0]).startswith("还没有订阅源"), what="空源提示")
        self.assertIn("还没有订阅源", _text_of(view._sources.controls[0]))
        self.assertEqual(view._detail_title.value, "还没有订阅源")

    async def test_selecting_a_source_loads_its_articles(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="源列表")
        controller.select_source("feed-b")
        await _wait(
            lambda: ("list_articles", "feed-b", False) in stub.calls,
            what="切源后拉文章")
        await _wait(lambda: "还没有文章" in str(
            view._articles.controls[0].value), what="空文章提示")

    async def test_opening_an_article_marks_it_read_once(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="文章列表")
        article = controller._articles[0]
        controller.open_article(article)
        self.assertEqual(view._detail_title.value, "文章一")
        self.assertEqual(view._detail_body.value, "第一段")   # HTML 已去掉
        await _wait(lambda: ("mark_read", 1) in stub.calls, what="已读落库")
        await _wait(lambda: controller._counts["feed-a"]["unread"] == 0,
                    what="角标跟着减")
        # 再点一次不该重复写
        controller.open_article(article)
        await asyncio.sleep(0.1)
        self.assertEqual(
            [c for c in stub.calls if c[0] == "mark_read"], [("mark_read", 1)])

    async def test_star_toggle_updates_row_and_detail(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="文章列表")
        article = controller._articles[0]
        controller.open_article(article)          # 正文栏打开，跟着一起变
        before = view._articles.controls[0].trailing.icon
        controller.toggle_star(article)
        await _wait(lambda: ("star", 1, True) in stub.calls, what="收藏落库")
        # 列表行与正文栏的星标都要跟着变（不是自己等于自己的空话）
        await _wait(lambda: (view._articles.controls[0].trailing.icon
                             != before
                             and view._detail_star.icon
                             == view._articles.controls[0].trailing.icon),
                    what="星标重绘")
        self.assertEqual(view._articles.controls[0].trailing.icon, ft.Icons.STAR)
        self.assertTrue(article["starred"])
        controller.toggle_star(article)
        await _wait(lambda: ("star", 1, False) in stub.calls, what="取消收藏")
        await _wait(lambda: view._articles.controls[0].trailing.icon
                    == ft.Icons.STAR_BORDER, what="星标熄灭")
        self.assertFalse(article["starred"])

    async def test_unread_filter_is_passed_down(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="文章列表")
        view._unfiltered.value = True
        view._unfiltered.on_change(SimpleNamespace(control=view._unfiltered))
        await _wait(lambda: ("list_articles", "feed-a", True) in stub.calls,
                    what="只看未读下传")
        await _wait(lambda: len(_rows(view._articles)) == 1, what="只剩未读")

    async def test_add_source_then_fetches_it(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="源列表")
        controller.add_source("丙源", "https://c/feed")
        await _wait(lambda: ("add_source", "丙源", "https://c/feed") in stub.calls,
                    what="添加")
        await _wait(lambda: ("refresh_source", "https://c/feed") in stub.calls,
                    what="添加后立刻抓一次")
        await _wait(lambda: "已添加「丙源」" in str(view._status.value),
                    what="状态回报")
        await _wait(lambda: len(_rows(view._sources)) == 3, what="新源进列表")

    async def test_add_dialog_needs_a_page_and_says_so(self):
        """离线/未挂载：show_dialog 无从谈起，界面得说清而不是没反应。"""
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="首帧")
        controller.open_add_dialog()
        self.assertIn("页面未就绪", str(view._status.value))
        self.assertFalse([c for c in stub.calls if c[0] == "add_source"])

    async def test_removal_goes_through_confirm_then_reselects(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="首帧")
        # 没有 page：确认框弹不出来，也就绝不能顺手删掉
        controller.request_remove(stub.sources[0])
        self.assertIn("页面未就绪", str(view._status.value))
        self.assertFalse([c for c in stub.calls if c[0] == "remove_source"])
        # 用户在确认框点「删除」之后才会走到这里
        controller._remove_source(stub.sources[0])
        await _wait(lambda: ("remove_source", "feed-a") in stub.calls,
                    what="删除下传")
        await _wait(lambda: controller._selected == "feed-b", what="选中回落到下一个源")
        self.assertIn("已删除「甲源」", str(view._status.value))

    async def test_refresh_failure_is_spoken_not_swallowed(self):
        stub = _RssStub(fail={"refresh_source": RuntimeError("DNS 解析失败")})
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="首帧")
        controller.refresh_source("feed-a")
        await _wait(lambda: "刷新失败" in str(view._status.value), what="失败状态")
        self.assertIn("DNS 解析失败", str(view._status.value))

    async def test_refresh_all_shows_progress_and_final_tally(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="首帧")
        controller.refresh_all()
        # 进度事件由服务发；界面只需按事件帧渲染
        await controller._on_progress(
            RssRefreshProgressEvent(done=1, total=3, current="甲源"))
        self.assertTrue(view._progress.visible)
        self.assertAlmostEqual(view._progress.value, 1 / 3)
        self.assertIn("正在刷新 2/3：甲源", str(view._status.value))
        await _wait(lambda: "刷新完成，新增 3 篇" in str(view._status.value),
                    what="收尾状态")
        self.assertFalse(view._progress.visible)

    async def test_refresh_all_failure_hides_the_bar(self):
        stub = _RssStub(fail={"refresh_all": RuntimeError("网络不通")})
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._sources)) == 2, what="首帧")
        controller.refresh_all()
        await _wait(lambda: "全部刷新失败" in str(view._status.value),
                    what="失败状态")
        self.assertFalse(view._progress.visible)

    async def test_refreshed_event_repaints_the_open_source(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="首帧")
        stub.articles["feed-a"].append(
            {"id": 3, "source_id": "feed-a", "title": "文章三", "url": "https://a/3",
             "author": "", "pub_date": "2025-01-03", "content": "正文三",
             "image": "", "read": False, "starred": False, "updated_at": 0.0})
        stub.counts["feed-a"] = {"total": 3, "unread": 2}
        before = len(stub.calls)
        await controller._on_refreshed(
            RssRefreshedEvent(source_id="feed-a", new_count=1))
        self.assertTrue(any(c[0] == "list_articles" for c in stub.calls[before:]),
                        "刷新事件要重新拉列表")
        await _wait(lambda: len(_rows(view._articles)) == 3, what="第三条出现")
        self.assertIn("2 未读", view._sources.controls[0].subtitle.value)

    async def test_other_sources_refresh_does_not_touch_open_list(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="首帧")
        before = len([c for c in stub.calls if c[0] == "list_articles"])
        await controller._on_refreshed(
            RssRefreshedEvent(source_id="feed-b", new_count=5))
        self.assertEqual(
            len([c for c in stub.calls if c[0] == "list_articles"]), before,
            "别的源刷新完不该重拉当前源")

    async def test_missing_service_reports_not_ready(self):
        controller, view = _attach(None)
        await _wait(lambda: "未就绪" in str(view._status.value), what="未就绪提示")
        self.assertIn("RssService", str(view._status.value))
        controller.refresh_all()
        self.assertIn("RssService 未就绪", str(view._status.value))

    async def test_detach_then_event_does_not_touch_a_dead_view(self):
        stub = _RssStub()
        controller, view = _attach(stub)
        await _wait(lambda: len(_rows(view._articles)) == 2, what="首帧")
        controller.detach()
        await controller._on_refreshed(
            RssRefreshedEvent(source_id="feed-a", new_count=1))
        await controller._on_progress(RssRefreshProgressEvent(done=1, total=2))
        self.assertIsNone(controller._view)


# ====================================================================== #
# 3. 路由归属
# ====================================================================== #
class RssWiringTests(unittest.TestCase):
    def test_rss_is_a_reading_tab_not_a_hidden_page(self):
        self.assertEqual(NAV_META[DEST_RSS], (GROUP_READING, "订阅", False))

    def test_registered_as_reachable_tab(self):
        from app.views.reading import register_destinations
        registry = ViewRegistry()
        register_destinations(registry, _FakeHub({}))
        route = registry.get(DEST_RSS)
        self.assertFalse(route.hidden, "订阅是页签，不是整页目的地")
        # 分组来自 NAV_META 元数据（导航装配时 attach）；这里单域注册，只贴本条。
        registry.attach_navigation_meta({DEST_RSS: NAV_META[DEST_RSS]})
        self.assertEqual(registry.get(DEST_RSS).group, GROUP_READING)

    def test_view_stays_clean_of_services(self):
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "views"
               / "reading" / "rss_view.py").read_text(encoding="utf-8-sig")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("app.services"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("app.services"))


if __name__ == "__main__":
    unittest.main()
