"""M4 RssService 测试（契约 §9，≥8 条，离线不联网）。

覆盖：源 CRUD / 本地 feed 文件 refresh（feedparser 真实解析 RSS 2.0 XML）/
文章列表 / 已读 / 收藏 / RssRefreshedEvent 发布 / 刷新去重与状态保留 /
刷新进度写 status.progress。

网络不联网：feed 原文由注入的 ``fetcher`` 从本地 XML 文件读取（写入 tmp 目录），
不走 RuleEngineService 网络栈；feedparser 对该本地 XML 做真实解析。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import RssRefreshedEvent
from app.core.storage import StorageService
from app.services.content.rss_service import RssService


# 本地 RSS 2.0 样例（2 条 item），写入临时文件后由 fetcher 读回——
# feedparser 对该真实 XML 做解析（不联网）。
SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>测试频道</title>
    <link>https://example.com/</link>
    <description>本地样例</description>
    <item>
      <title>文章一</title>
      <link>https://example.com/a/1</link>
      <author>张三</author>
      <pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate>
      <description>正文一摘要</description>
    </item>
    <item>
      <title>文章二</title>
      <link>https://example.com/a/2</link>
      <pubDate>Thu, 02 Jan 2025 00:00:00 GMT</pubDate>
      <description>正文二摘要</description>
    </item>
  </channel>
</rss>
"""

# 第二次刷新多带一条新 item，用于验证"新增计数 + 去重不覆盖已读/收藏"。
SAMPLE_FEED_V2 = SAMPLE_FEED.replace(
    "</channel>",
    """<item>
      <title>文章三</title>
      <link>https://example.com/a/3</link>
      <pubDate>Fri, 03 Jan 2025 00:00:00 GMT</pubDate>
      <description>正文三摘要</description>
    </item>
  </channel>""")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真实 StorageService（短路径 tmp 数据根）+ EventBus + RssService（注入 fetcher）。"""
    root = Path(tempfile.mkdtemp(prefix="m4rss_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))

    feed_file = root / "feed.xml"
    feed_file.write_text(SAMPLE_FEED, encoding="utf-8")

    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    bus = EventBus(engine=engine)

    captured: list[RssRefreshedEvent] = []

    async def _on_refresh(ev: RssRefreshedEvent) -> None:
        captured.append(ev)

    bus.subscribe(RssRefreshedEvent, _on_refresh)

    async def fetcher(url: str) -> str:
        # 本地 feed 文件读取：忽略 url，直接返回磁盘上的样例 XML。
        return feed_file.read_text(encoding="utf-8")

    svc = RssService(storage=storage, fetcher=fetcher)
    svc.bus = bus  # 服务间只经 EventBus 发事件

    try:
        yield type("E", (), {
            "storage": storage, "bus": bus, "svc": svc,
            "captured": captured, "feed_file": feed_file, "root": root,
        })
    finally:
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _drain(bus) -> None:
    # EventBus.publish 把处理器投到 engine loop；这里跑一轮让其执行完。
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        time.sleep(0.02)


# --------------------------------------------------------------------- #
# 1. 源 CRUD
# --------------------------------------------------------------------- #
def test_source_crud(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    assert sid == "https://example.com/feed"  # id 即规范化 url

    sources = asyncio.run(svc.list_sources())
    assert len(sources) == 1
    assert sources[0]["name"] == "测试源"
    assert sources[0]["url"] == "https://example.com/feed"
    assert sources[0]["enabled"] is True

    # 同 url 重复 add 不产生重复行（ON CONFLICT 更新 name）
    asyncio.run(svc.add_source("改名源", "https://example.com/feed"))
    sources = asyncio.run(svc.list_sources())
    assert len(sources) == 1
    assert sources[0]["name"] == "改名源"

    ok = asyncio.run(svc.remove_source(sid))
    assert ok is True
    assert asyncio.run(svc.list_sources()) == []


# --------------------------------------------------------------------- #
# 2. 本地 feed refresh（feedparser 真实解析）
# --------------------------------------------------------------------- #
def test_refresh_parses_local_feed(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    new_count = asyncio.run(svc.refresh_source(sid))
    assert new_count == 2  # 样例里 2 条 item

    articles = asyncio.run(svc.list_articles(sid))
    assert len(articles) == 2
    titles = {a["title"] for a in articles}
    assert titles == {"文章一", "文章二"}
    # feedparser 解析出的字段落库
    a1 = next(a for a in articles if a["url"] == "https://example.com/a/1")
    assert a1["author"] == "张三"
    assert "正文一摘要" in a1["content"]


# --------------------------------------------------------------------- #
# 3. 刷新去重 + 已读/收藏状态保留
# --------------------------------------------------------------------- #
def test_refresh_dedup_and_preserve_state(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    articles = asyncio.run(svc.list_articles(sid))
    a1 = next(a for a in articles if a["url"] == "https://example.com/a/1")

    # 标记第一条已读 + 收藏
    asyncio.run(svc.mark_read(a1["id"]))
    asyncio.run(svc.star(a1["id"], True))

    # 换成 v2 样例（多一条新文章）再刷新
    env.feed_file.write_text(SAMPLE_FEED_V2, encoding="utf-8")
    new_count = asyncio.run(svc.refresh_source(sid))
    assert new_count == 1  # 只有"文章三"是新增

    articles = asyncio.run(svc.list_articles(sid))
    assert len(articles) == 3
    # 已读/收藏状态不被刷新覆盖
    a1 = next(a for a in articles if a["url"] == "https://example.com/a/1")
    assert a1["read"] is True
    assert a1["starred"] is True


# --------------------------------------------------------------------- #
# 4. 文章列表 + unread_only 过滤
# --------------------------------------------------------------------- #
def test_list_articles_unread_filter(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    articles = asyncio.run(svc.list_articles(sid))
    assert len(articles) == 2

    # 全部未读 -> unread_only 仍是 2
    assert len(asyncio.run(svc.list_articles(sid, unread_only=True))) == 2
    # 读掉第一条后，unread_only 剩 1
    asyncio.run(svc.mark_read(articles[0]["id"]))
    assert len(asyncio.run(svc.list_articles(sid, unread_only=True))) == 1


# --------------------------------------------------------------------- #
# 5. 标记已读
# --------------------------------------------------------------------- #
def test_mark_read(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    articles = asyncio.run(svc.list_articles(sid))
    target = articles[0]
    assert target["read"] is False

    ok = asyncio.run(svc.mark_read(target["id"]))
    assert ok is True
    articles = asyncio.run(svc.list_articles(sid))
    assert next(a for a in articles if a["id"] == target["id"])["read"] is True


# --------------------------------------------------------------------- #
# 6. 收藏
# --------------------------------------------------------------------- #
def test_star(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    articles = asyncio.run(svc.list_articles(sid))
    target = articles[0]

    asyncio.run(svc.star(target["id"], True))
    out = asyncio.run(svc.list_articles(sid))
    assert next(a for a in out if a["id"] == target["id"])["starred"] is True

    asyncio.run(svc.star(target["id"], False))
    out = asyncio.run(svc.list_articles(sid))
    assert next(a for a in out if a["id"] == target["id"])["starred"] is False


# --------------------------------------------------------------------- #
# 7. RssRefreshedEvent 发布
# --------------------------------------------------------------------- #
def test_rss_refreshed_event(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    _drain(env.bus)

    events = [e for e in env.captured if isinstance(e, RssRefreshedEvent)]
    assert len(events) == 1
    assert events[0].source_id == sid
    assert events[0].new_count == 2


# --------------------------------------------------------------------- #
# 8. 刷新进度写 status.progress
# --------------------------------------------------------------------- #
def test_refresh_progress(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    assert svc.status.progress is None  # 刷新前无进度
    asyncio.run(svc.refresh_source(sid))
    assert svc.status.progress == 1.0  # 刷新完成进度归零到 1


# --------------------------------------------------------------------- #
# 9. remove_source 级联删文章 + refresh_all
# --------------------------------------------------------------------- #
def test_remove_source_cascade_and_refresh_all(env):
    svc = env.svc
    sid = asyncio.run(svc.add_source("测试源", "https://example.com/feed"))
    asyncio.run(svc.refresh_source(sid))
    assert len(asyncio.run(svc.list_articles(sid))) == 2

    total = asyncio.run(svc.refresh_all())
    assert total == 0  # 已全部存在，重复刷新无新增

    asyncio.run(svc.remove_source(sid))
    # 级联：源删后文章也没了
    assert asyncio.run(svc.list_articles(sid)) == []


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
