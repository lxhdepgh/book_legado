"""M3 书源域 SourceService 外壳服务级测试（契约 §10，≥12 条）。

覆盖：生命周期 / 书源 CRUD / 分组统计 / JSON 导入导出 / 书架 notShelf 语义 /
1.18 改封面（Pillow 归一 + books.cover_url 回写）/ 保存即审入口 / 事件发布。

不依赖 Flet、不发网络请求；StorageService 落在临时目录（短路径，规避 Windows
MAX_PATH），协作者全部注入。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import SourceChangedEvent
from app.core.storage import StorageService
from app.services.app_paths import AppPaths
from app.services.sources.source_service import SourceService


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env():
    """真实 StorageService（临时目录）+ 空 SourceService + 可观测事件总线。"""
    root = Path(tempfile.mkdtemp(prefix="m3src_"))
    paths = AppPaths(data=root / "data", cache=root / "cache")
    storage = StorageService(paths=paths)
    asyncio.run(storage.start())
    engine = engine_hub()
    bus = EventBus(engine=engine)
    # 捕获 publish 产生的 future，便于同步断言事件已投递。
    pending: list = []
    orig_publish = bus.publish

    def spy(ev):
        fs = orig_publish(ev)
        pending.extend(fs)
        return fs

    bus.publish = spy  # type: ignore[method-assign]
    svc = SourceService(resource_dir=root / "resource", storage=storage)
    svc.bus = bus
    asyncio.run(svc.start())
    try:
        yield SimpleNamespace(storage=storage, bus=bus, svc=svc,
                              root=root, pending=pending, engine=engine)
    finally:
        asyncio.run(svc.stop())
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _drain(pending) -> None:
    for f in list(pending):
        f.result(timeout=5)
    pending.clear()


def _source_dict(url="https://src-1.example.com", name="源一", group="默认"):
    """最小合法书源字典（BookSource.from_dict 可解析）。"""
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "bookSourceGroup": group,
        "enabled": True,
        "bookSourceComment": "m3",
    }


def _book(url="book-1", name="书"):
    return SimpleNamespace(url=url, name=name, author="作者", intro="",
                           cover_url="", variables={})


def _source(url="https://src-1.example.com"):
    return SimpleNamespace(bookSourceUrl=url, bookSourceName="源")


# --------------------------------------------------------------------------- #
# 1. 生命周期：空库启动不崩
# --------------------------------------------------------------------------- #
def test_on_start_empty_library(env):
    assert env.svc.status.state.value == "RUNNING"
    assert asyncio.run(env.svc.list_sources()) == []
    assert env.svc.shelved_books() == []


# --------------------------------------------------------------------------- #
# 2-5. CRUD：save / upsert / 缺 url 拒绝 / load_source
# --------------------------------------------------------------------------- #
def test_save_source_creates_and_persists(env):
    result = asyncio.run(env.svc.save_source(_source_dict(url="https://a.io")))
    assert result["saved"] is True
    sources = asyncio.run(env.svc.list_sources())
    assert len(sources) == 1
    # M6：持久落点由 bookSource.json 切主库 book_sources 表（旧 JSON 不再写）。
    rows = env.storage.db.execute(
        "SELECT json FROM book_sources WHERE id=?", ("https://a.io",)).fetchall()
    assert rows, "保存后 book_sources 主库表应可见"
    assert json.loads(rows[0][0])["bookSourceUrl"] == "https://a.io"


def test_save_source_upsert_by_url(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://a.io", name="第一版")))
    asyncio.run(env.svc.save_source(_source_dict(url="https://a.io", name="第二版")))
    sources = asyncio.run(env.svc.list_sources())
    assert len(sources) == 1
    assert sources[0].bookSourceName == "第二版"


def test_save_source_missing_url_rejected(env):
    result = asyncio.run(env.svc.save_source({"bookSourceName": "无url"}))
    assert result["saved"] is False
    assert result["errors"]


def test_load_source_by_url(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://b.io")))
    hit = asyncio.run(env.svc.load_source("https://b.io"))
    assert hit is not None and hit.bookSourceName == "源一"
    assert asyncio.run(env.svc.load_source("https://nope.io")) is None


# --------------------------------------------------------------------------- #
# 6-8. delete / set_enabled / set_group
# --------------------------------------------------------------------------- #
def test_delete_source(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://c.io")))
    assert asyncio.run(env.svc.delete_source("https://c.io")) is True
    assert asyncio.run(env.svc.list_sources()) == []
    # M6：删除后 book_sources 主库表中也没有（旧 bookSource.json 不再写）
    rows = env.storage.db.execute(
        "SELECT id FROM book_sources WHERE id=?", ("https://c.io",)).fetchall()
    assert rows == []


def test_set_enabled(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://d.io")))
    assert asyncio.run(env.svc.set_enabled("https://d.io", False)) is True
    hit = asyncio.run(env.svc.load_source("https://d.io"))
    assert hit.enabled is False
    assert asyncio.run(env.svc.set_enabled("https://missing.io", True)) is False


def test_set_group(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://e.io", group="旧组")))
    assert asyncio.run(env.svc.set_group("https://e.io", "新组")) is True
    hit = asyncio.run(env.svc.load_source("https://e.io"))
    assert hit.bookSourceGroup == "新组"
    assert "新组" in env.svc.get_groups()


# --------------------------------------------------------------------------- #
# 9-11. 导入导出 JSON
# --------------------------------------------------------------------------- #
def test_import_sources_json_dedup(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://dup.io")))
    text = json.dumps([
        _source_dict(url="https://dup.io"),       # 重复，跳过
        _source_dict(url="https://f1.io"),        # 新增
        _source_dict(url="https://f2.io"),         # 新增
    ], ensure_ascii=False)
    count, errors = asyncio.run(env.svc.import_sources_json(text))
    assert count == 2
    assert errors == []
    urls = {s.bookSourceUrl for s in asyncio.run(env.svc.list_sources())}
    assert {"https://dup.io", "https://f1.io", "https://f2.io"} <= urls


def test_import_sources_json_bad_json(env):
    count, errors = asyncio.run(env.svc.import_sources_json("{ not json"))
    assert count == 0
    assert errors


def test_import_sources_json_skips_entries_without_url(env):
    """缺 bookSourceUrl 的条目必须挡在门口。

    过去它会被 from_dict 兜成一条空 url 的书源落库：认不出、删不掉，
    而且所有空 url 互相撞成同一条（把 TXT 目录规则当书源选进来就是这样）。
    """
    text = json.dumps([
        {"bookSourceName": "没有地址的规则", "ruleText": "第?章"},
        _source_dict(url="https://ok.io"),
    ], ensure_ascii=False)
    count, errors = asyncio.run(env.svc.import_sources_json(text))
    assert count == 1
    assert any("bookSourceUrl" in e for e in errors), errors
    urls = [s.bookSourceUrl for s in asyncio.run(env.svc.list_sources())]
    assert urls == ["https://ok.io"]


def test_import_sources_json_replace_swaps_whole_table(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://old.io")))
    text = json.dumps([_source_dict(url="https://new.io")], ensure_ascii=False)
    count, errors = asyncio.run(env.svc.import_sources_json(text, "replace"))
    assert count == 1 and errors == []
    urls = {s.bookSourceUrl for s in asyncio.run(env.svc.list_sources())}
    assert urls == {"https://new.io"}, "替换模式必须丢掉旧列表"


def test_import_sources_json_replace_keeps_table_when_nothing_valid(env):
    """一个坏文件不能把整张书源表清空。"""
    asyncio.run(env.svc.save_source(_source_dict(url="https://keep.io")))
    count, _ = asyncio.run(env.svc.import_sources_json("[1, 2, 3]", "replace"))
    assert count == 0
    urls = {s.bookSourceUrl for s in asyncio.run(env.svc.list_sources())}
    assert urls == {"https://keep.io"}


def test_import_sources_json_rejects_unknown_mode(env):
    count, errors = asyncio.run(env.svc.import_sources_json("[]", "merge"))
    assert count == 0
    assert any("模式" in e for e in errors), errors


def test_import_sources_json_publishes_progress(env):
    """逐条落库阶段要发 ImportProgressEvent，界面才有真的进度条而不是空转。"""
    seen: list = []
    env.svc._publish = lambda ev: seen.append(ev) or []  # type: ignore[method-assign]
    text = json.dumps([_source_dict(url=f"https://p{i}.io") for i in range(5)],
                      ensure_ascii=False)
    count, _ = asyncio.run(env.svc.import_sources_json(text))
    assert count == 5
    progresses = [ev for ev in seen if type(ev).__name__ == "ImportProgressEvent"]
    assert progresses, "落库过程必须发进度"
    last = progresses[-1]
    assert (last.done, last.total) == (5, 5), "最后一条必须发到满，否则进度条停在半路"
    assert any(type(ev).__name__ == "SourceChangedEvent" for ev in seen)


def test_export_sources_json_roundtrip(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://g.io")))
    exported = asyncio.run(env.svc.export_sources_json())
    data = json.loads(exported)
    assert any(s["bookSourceUrl"] == "https://g.io" for s in data)


# --------------------------------------------------------------------------- #
# 12. 分组统计（继承旧实现）
# --------------------------------------------------------------------------- #
def test_groups_and_statistics(env):
    asyncio.run(env.svc.save_source(_source_dict(url="https://g1.io", group="A")))
    asyncio.run(env.svc.save_source(_source_dict(url="https://g2.io", group="B")))
    asyncio.run(env.svc.set_enabled("https://g2.io", False))
    stats = env.svc.get_statistics()
    assert stats["total"] == 2
    assert stats["enabled"] == 1
    assert stats["disabled"] == 1
    assert set(env.svc.get_groups()) >= {"A", "B"}


# --------------------------------------------------------------------------- #
# 13. 书架 notShelf 语义（对齐 找书与换源逻辑对齐.md §1.5）
# --------------------------------------------------------------------------- #
def test_notshelf_semantics(env):
    svc = env.svc
    src = _source("https://ns.io")
    # 试读（shelved=False）：记录存在但不进书架
    assert svc.add_to_bookshelf(src, _book("ns-book"), shelved=False) is True
    assert svc.is_shelved("ns-book") is False
    assert svc.shelved_books() == []
    # 转正：同记录保留进度
    assert svc.promote_to_bookshelf("https://ns.io", "ns-book") is True
    assert svc.is_shelved("ns-book") is True
    assert len(svc.shelved_books()) == 1
    # 启动清理：再写一条试读残留，discard 只删未入架的
    svc.add_to_bookshelf(src, _book("staged-book"), shelved=False)
    discarded = svc.discard_unshelved_books()
    assert ("https://ns.io", "staged-book") in discarded
    assert svc.is_shelved("ns-book") is True          # 已入架的保留
    assert svc.is_shelved("staged-book") is False     # 临时记录已删


# --------------------------------------------------------------------------- #
# 13.5 启动清理上次遗留的试读记录（Legado MainViewModel.init 的 deleteNotShelfBook）
# --------------------------------------------------------------------------- #
def test_on_start_discards_staged_leftovers_and_their_cache(env):
    from app.services.engine.rule_engine import ChapterItem
    from app.services.reading_cache import ReadingCache

    svc = env.svc
    svc.add_to_bookshelf(_source("https://stale.io"), _book("stale-book"),
                         shelved=False)
    svc.add_to_bookshelf(_source("https://keep.io"), _book("kept-book"),
                         shelved=True)
    cache = ReadingCache(svc.cache_dir)
    cache.save_catalog("https://stale.io", "stale-book",
                       [ChapterItem(name="一", url="ch1", index=0)])
    assert cache.load_catalog("https://stale.io", "stale-book")

    # 重启：未转正的试读不跨会话，已入架的和它的缓存都不受影响
    fresh = SourceService(resource_dir=env.root / "resource", storage=env.storage)
    asyncio.run(fresh.on_start())
    assert [str(b.get("url")) for b in fresh.bookshelf] == ["kept-book"]
    assert not cache.load_catalog("https://stale.io", "stale-book")


# --------------------------------------------------------------------------- #
# 14. 改封面：本地文件 Pillow 归一 + books.cover_url 回写
# --------------------------------------------------------------------------- #
def test_update_cover_local_normalizes(env):
    from PIL import Image

    svc = env.svc
    svc.add_to_bookshelf(_source("https://cov.io"), _book("cov-book"), shelved=True)
    env.storage.db.execute(
        "INSERT INTO books(book_url,source_url,name) VALUES(?,?,?)",
        ("cov-book", "https://cov.io", "书"))
    # 造一张 600x800 的本地图
    src_img = env.root / "in.png"
    Image.new("RGB", (600, 800), (120, 30, 30)).save(src_img)

    resolved = asyncio.run(svc.update_cover("cov-book", str(src_img)))
    assert resolved.endswith(".jpg")
    out_path = Path(resolved)
    assert out_path.exists()
    # 归一后最长边 <= 480
    with Image.open(out_path) as im:
        assert max(im.size) <= 480
    # books.cover_url 回写
    row = env.storage.db.execute(
        "SELECT cover_url FROM books WHERE book_url=?", ("cov-book",)).fetchone()
    assert row[0] == resolved
    # bookshelf 记录同步
    rec = next(r for r in svc.bookshelf if r["url"] == "cov-book")
    assert rec["cover_url"] == resolved


# --------------------------------------------------------------------------- #
# 15. 改封面：远端 URL 原样记录（M3 不在服务内联网）
# --------------------------------------------------------------------------- #
def test_update_cover_remote_url_passthrough(env):
    svc = env.svc
    svc.add_to_bookshelf(_source("https://cov2.io"), _book("cov2-book"), shelved=True)
    resolved = asyncio.run(
        svc.update_cover("cov2-book", "https://cdn.example.com/cover.jpg"))
    assert resolved == "https://cdn.example.com/cover.jpg"
    rec = next(r for r in svc.bookshelf if r["url"] == "cov2-book")
    assert rec["cover_url"] == "https://cdn.example.com/cover.jpg"


# --------------------------------------------------------------------------- #
# 16. 保存即审入口：注入审计替身被调用
# --------------------------------------------------------------------------- #
def test_save_source_calls_auditor(env):
    calls: list = []

    class FakeAuditor:
        def audit_source(self, source_json):
            calls.append(source_json)
            return {"missing_apis": []}

    svc = SourceService(resource_dir=env.root / "resource",
                        storage=env.storage, auditor=FakeAuditor())
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    result = asyncio.run(svc.save_source(_source_dict(url="https://aud.io")))
    assert result["saved"] is True
    assert len(calls) == 1
    assert calls[0]["bookSourceUrl"] == "https://aud.io"


# --------------------------------------------------------------------------- #
# 16b. 导入即审的落点：本层默认审，门面那条路传 audit=False 让它闭嘴
# --------------------------------------------------------------------------- #
def _auditor_recording():
    calls: list = []

    class FakeAuditor:
        def audit_source(self, source_json):
            calls.append(source_json.get("bookSourceUrl"))
            return {"missing_apis": []}

    return calls, FakeAuditor()


def _audit_svc(env, auditor):
    svc = SourceService(resource_dir=env.root / "resource",
                        storage=env.storage, auditor=auditor)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    return svc


def test_import_sources_json_audits_every_entry_by_default(env):
    """不经门面的入口（扫码导入、云端恢复）全靠这一条，所以默认必须是审。"""
    calls, auditor = _auditor_recording()
    svc = _audit_svc(env, auditor)
    text = json.dumps([_source_dict(url="https://i1/"), _source_dict(url="https://i2/")],
                      ensure_ascii=False)
    count, _ = asyncio.run(svc.import_sources_json(text))
    assert count == 2
    assert calls == ["https://i1/", "https://i2/"]


def test_import_sources_json_audit_false_skips_the_auditor(env):
    """``audit=False`` 是防双审的闸：门面已经为出 warning 审过一遍了。

    不传就会 ``audit_log`` 每源落两行，回溯时认不出哪行属于这次导入。
    """
    calls, auditor = _auditor_recording()
    svc = _audit_svc(env, auditor)
    text = json.dumps([_source_dict(url="https://i1/")], ensure_ascii=False)
    count, _ = asyncio.run(svc.import_sources_json(text, audit=False))
    assert count == 1
    assert calls == []
    assert [s.bookSourceUrl for s in svc.sources] == ["https://i1/"]


# --------------------------------------------------------------------------- #
# 17. SourceChangedEvent 发布
# --------------------------------------------------------------------------- #
def test_source_changed_event_published(env):
    fired: list = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(SourceChangedEvent, collector)
    asyncio.run(env.svc.save_source(_source_dict(url="https://ev.io")))
    asyncio.run(env.svc.delete_source("https://ev.io"))
    _drain(env.pending)
    saved = [e for e in fired if e.change_type == "saved"
             and e.source_id == "https://ev.io"]
    deleted = [e for e in fired if e.change_type == "deleted"
               and e.source_id == "https://ev.io"]
    assert saved and deleted
