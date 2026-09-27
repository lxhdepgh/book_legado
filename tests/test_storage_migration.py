"""M6 存储收敛迁移测试（集成契约 §2/§3，≥10 条断言）。

构造 4 类旧介质样本（bookSource.json / bookshelf.json / appearance.json /
search-books.sqlite3）→ 启动 StorageService（on_start 自动 migrate）→ 断言：

- 主库行数 / 投影字段 / json·reading·payload 全文保真；
- 迁移后旧文件均改名 .legacy-bak、原文件消失；
- 幂等：二次 migrate 不重复导入；
- SourceService 切主库后保存可见、旧 bookSource.json 不再写；
- SearchBookStore 经 conn 垫片直接读写主库 search_books（upsert/TTL）。

fixture 用系统短临时目录（tempfile.mkdtemp），规避 Windows MAX_PATH。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.storage import StorageService
from app.services.app_paths import AppPaths
from app.services.engine.rule_engine import BookItem
from app.services.search_model import SearchBookStore
from app.services.sources.source_service import SourceService


def _make_old_media(data: Path) -> None:
    """在 data 目录构造 4 类旧介质样本（含 1 条缺 URL 书源，触发字段缺失清单）。"""
    # 1) bookSource.json：2 条合法 + 1 条缺 bookSourceUrl（计入 missing）
    sources = [
        {"bookSourceName": "笔趣阁", "bookSourceUrl": "https://bqg.example.com",
         "bookSourceGroup": "阅读", "enabled": True, "customOrder": 3,
         "respondTime": 120000, "searchUrl": "/search?q={{key}}"},
        {"bookSourceName": "纵横", "bookSourceUrl": "https://zh.example.com",
         "bookSourceGroup": "", "enabled": False, "customOrder": 1},
        {"bookSourceName": "缺URL的源"},
    ]
    (data / "bookSource.json").write_text(
        json.dumps(sources, ensure_ascii=False), encoding="utf-8")

    # 2) bookshelf.json：2 条（一条含 reading 块 / variables / toc_url，一条试读）
    shelf = [
        {"bookUrl": "https://bqg.example.com/book/1",
         "url": "https://bqg.example.com/book/1",
         "source_url": "https://bqg.example.com", "name": "第一本书",
         "author": "张三", "kind": "玄幻", "cover_url": "https://cdn/1.jpg",
         "in_bookshelf": True,
         "reading": {"chapter_index": 5, "chapter_pos": 120, "scroll_ratio": 0.4},
         "variables": {"token": "abc"}, "toc_url": "/toc/1"},
        {"bookUrl": "https://zh.example.com/book/9",
         "url": "https://zh.example.com/book/9",
         "source_url": "https://zh.example.com", "name": "第二本",
         "author": "李四", "kind": "都市", "cover_url": "",
         "in_bookshelf": False, "reading": {}, "variables": {}},
    ]
    (data / "bookshelf.json").write_text(
        json.dumps(shelf, ensure_ascii=False), encoding="utf-8")

    # 3) appearance.json
    (data / "appearance.json").write_text(
        json.dumps({"skin_id": "ink_gray", "mode": "dark"}, ensure_ascii=False),
        encoding="utf-8")

    # 4) search-books.sqlite3（旧 Room 表结构）
    old = sqlite3.connect(str(data / "search-books.sqlite3"))
    old.execute(
        "CREATE TABLE search_books(book_url TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " author TEXT NOT NULL, origin TEXT NOT NULL, origin_order INTEGER NOT NULL,"
        " saved_at INTEGER NOT NULL, payload TEXT NOT NULL)")
    payload = json.dumps({"name": "旧书", "author": "王五", "kind": "科幻",
                          "origin": "https://old.example.com",
                          "origin_name": "旧源"}, ensure_ascii=False)
    old.execute(
        "INSERT INTO search_books VALUES(?,?,?,?,?,?,?)",
        ("https://old.example.com/b/1", "旧书", "王五", "https://old.example.com",
         0, int(time.time() * 1000), payload))
    old.commit()
    old.close()


@pytest.fixture()
def migrated_storage():
    """构造旧介质 → 启动 StorageService（on_start 自动 migrate）。"""
    root = Path(tempfile.mkdtemp(prefix="m6mig_"))
    paths = AppPaths(data=root / "data", cache=root / "cache")
    paths.data.mkdir(parents=True, exist_ok=True)
    _make_old_media(paths.data)
    storage = StorageService(paths=paths)
    asyncio.run(storage.start())
    try:
        yield storage, root, paths
    finally:
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------- #
# 1. 迁移总量 / 旧文件 .legacy-bak / appearance 并入 settings
# --------------------------------------------------------------------- #
def test_migrate_counts_and_backups(migrated_storage):
    storage, root, paths = migrated_storage
    conn = storage.db
    # book_sources：3 条输入 → 2 条合法入库（第 3 条缺 bookSourceUrl 进 missing）
    assert conn.execute("SELECT COUNT(*) FROM book_sources").fetchone()[0] == 2
    # bookshelf：2 条
    assert conn.execute("SELECT COUNT(*) FROM bookshelf").fetchone()[0] == 2
    # search_books：1 条
    assert conn.execute("SELECT COUNT(*) FROM search_books").fetchone()[0] == 1
    # appearance.snapshot 并入 settings
    row = conn.execute(
        "SELECT value FROM settings WHERE key='appearance.snapshot'").fetchone()
    assert json.loads(row[0])["skin_id"] == "ink_gray"
    # 4 个旧文件均改名 .legacy-bak，原文件不存在
    for name in ("bookSource.json", "bookshelf.json", "appearance.json",
                 "search-books.sqlite3"):
        assert not (paths.data / name).exists(), f"{name} 原文件应已改名"
        assert (paths.data / (name + ".legacy-bak")).exists(), \
            f"{name}.legacy-bak 应存在"


# --------------------------------------------------------------------- #
# 2. book_sources 投影列 + json 全文保真
# --------------------------------------------------------------------- #
def test_migrate_book_source_columns(migrated_storage):
    storage, _, _ = migrated_storage
    row = storage.db.execute(
        "SELECT id,name,`group`,enabled,custom_order,respond_time,json"
        " FROM book_sources WHERE id=?",
        ("https://bqg.example.com",)).fetchone()
    assert row[1] == "笔趣阁"
    assert row[2] == "阅读"          # group 投影
    assert row[3] == 1               # enabled
    assert row[4] == 3               # customOrder
    assert row[5] == 120000          # respondTime
    full = json.loads(row[6])        # json 全文保真
    assert full["searchUrl"] == "/search?q={{key}}"
    # 停用源 enabled=0
    dis = storage.db.execute(
        "SELECT enabled FROM book_sources WHERE id=?",
        ("https://zh.example.com",)).fetchone()
    assert dis[0] == 0


# --------------------------------------------------------------------- #
# 3. bookshelf：reading 块原样保留 / vars 列 / payload 保真
# --------------------------------------------------------------------- #
def test_migrate_bookshelf_reading_preserved(migrated_storage):
    storage, _, _ = migrated_storage
    row = storage.db.execute(
        "SELECT source_url,name,author,kind,cover_url,in_bookshelf,reading,vars"
        " FROM bookshelf WHERE book_url=?",
        ("https://bqg.example.com/book/1",)).fetchone()
    assert row[0] == "https://bqg.example.com"
    assert row[1] == "第一本书"
    assert row[3] == "玄幻"
    assert row[4] == "https://cdn/1.jpg"
    assert row[5] == 1
    reading = json.loads(row[6])     # reading 块原样保留
    assert reading["chapter_index"] == 5
    assert reading["chapter_pos"] == 120
    variables = json.loads(row[7])
    assert variables["token"] == "abc"
    # payload 列保留 toc_url 等额外字段
    payload = json.loads(storage.db.execute(
        "SELECT payload FROM bookshelf WHERE book_url=?",
        ("https://bqg.example.com/book/1",)).fetchone()[0])
    assert payload["toc_url"] == "/toc/1"


# --------------------------------------------------------------------- #
# 4. search_books：从旧 payload 投影 kind/origin_name
# --------------------------------------------------------------------- #
def test_migrate_search_books_projected(migrated_storage):
    storage, _, _ = migrated_storage
    row = storage.db.execute(
        "SELECT book_url,name,author,kind,origin,payload FROM search_books"
        " WHERE book_url=?", ("https://old.example.com/b/1",)).fetchone()
    assert row[1] == "旧书"
    assert row[2] == "王五"
    assert row[3] == "科幻"          # 从 payload 投影
    assert row[4] == "https://old.example.com"
    decoded = json.loads(row[5])
    assert decoded["origin_name"] == "旧源"


# --------------------------------------------------------------------- #
# 5. 幂等：二次 migrate 不重复导入
# --------------------------------------------------------------------- #
def test_migrate_idempotent(migrated_storage):
    storage, _, _ = migrated_storage
    report2 = storage.migrate()
    assert report2["already_migrated"] is True
    assert report2["book_sources"] == 0
    assert storage.db.execute(
        "SELECT COUNT(*) FROM book_sources").fetchone()[0] == 2
    assert storage.db.execute(
        "SELECT COUNT(*) FROM bookshelf").fetchone()[0] == 2


# --------------------------------------------------------------------- #
# 6. SourceService 切主库：启动读到迁移数据；保存后主库可见、旧 JSON 不再写
# --------------------------------------------------------------------- #
def test_source_service_switched_to_db(migrated_storage):
    storage, root, paths = migrated_storage
    svc = SourceService(resource_dir=root / "resource", storage=storage)
    asyncio.run(svc.on_start())
    try:
        sources = asyncio.run(svc.list_sources())
        assert {s.bookSourceUrl for s in sources} == {
            "https://bqg.example.com", "https://zh.example.com"}
        asyncio.run(svc.save_source({
            "bookSourceName": "新源", "bookSourceUrl": "https://new.example.com"}))
        row = storage.db.execute(
            "SELECT id FROM book_sources WHERE id=?",
            ("https://new.example.com",)).fetchone()
        assert row is not None
        # 旧 bookSource.json 不再写（迁移后已 .legacy-bak，运行期不重建）
        assert not (paths.data / "bookSource.json").exists()
    finally:
        asyncio.run(svc.on_stop())


# --------------------------------------------------------------------- #
# 7. SearchBookStore conn 垫片：直接读写主库 search_books（upsert/去重/TTL）
# --------------------------------------------------------------------- #
def test_search_book_store_shim_main_db(migrated_storage):
    storage, _, _ = migrated_storage
    store = SearchBookStore(conn=storage.db)
    items = [BookItem(name="垫片书", author="赵六",
                      url="https://shim/b/2", origin="https://shim.origin",
                      origin_order=1)]
    store.insert(items)
    got = store.get("https://shim/b/2")
    assert got is not None and got.name == "垫片书"
    # 落在主库 search_books 表
    row = storage.db.execute(
        "SELECT name,author,origin FROM search_books WHERE book_url=?",
        ("https://shim/b/2",)).fetchone()
    assert row[0] == "垫片书" and row[2] == "https://shim.origin"
    # upsert 去重：同 url 再插不翻倍
    store.insert(items)
    assert storage.db.execute(
        "SELECT COUNT(*) FROM search_books WHERE book_url=?",
        ("https://shim/b/2",)).fetchone()[0] == 1
    # TTL：clear_expired 按 saved_at 清理（远未来阈值全清）
    store.clear_expired(9_999_999_999_999)
    assert store.get("https://shim/b/2") is None


# --------------------------------------------------------------------- #
# 8. 书架往返走主库（add_to_bookshelf → bookshelf 表可见）
# --------------------------------------------------------------------- #
def test_bookshelf_roundtrip_via_db(migrated_storage):
    storage, root, _ = migrated_storage
    svc = SourceService(resource_dir=root / "resource", storage=storage)
    asyncio.run(svc.on_start())
    try:
        src = SimpleNamespace(bookSourceUrl="https://bqg.example.com",
                              bookSourceName="笔趣阁")
        book = SimpleNamespace(url="https://bqg.example.com/book/new",
                               name="新书", author="钱七", intro="", cover_url="",
                               toc_url="", kind="玄幻", last_chapter="",
                               word_count="", download_urls="", variables={})
        assert svc.add_to_bookshelf(src, book, shelved=True) is True
        row = storage.db.execute(
            "SELECT name,in_bookshelf FROM bookshelf WHERE book_url=?",
            ("https://bqg.example.com/book/new",)).fetchone()
        assert row is not None and row[0] == "新书" and row[1] == 1
    finally:
        asyncio.run(svc.on_stop())
