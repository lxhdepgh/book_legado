"""M4 本地书导入服务测试（LocalBookService，契约 §9 / 设计 §1.11）。

覆盖：txt 自写解析 / 编码探测 / 标题正则切章 / 归一化数据模型断言 /
epub 最小样例 / pdf 最小样例 / mobi、umd 与未知格式明确报错 /
books 表登记 + reader-cache 落盘 + ImportFinishedEvent 发布。
全程离线：epub/pdf 样例在测试内用 ebooklib/pymupdf 现场构造，不联网。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import ImportFinishedEvent
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.services.content.local_book_service import (
    LOCAL_BOOK_SOURCE_URL,
    LocalBookService,
    UnsupportedFormatError,
)
from app.services.engine.rule_engine import ChapterItem
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.reading_cache import ReadingCache


# --------------------------------------------------------------------------- #
# 夹具：真实 StorageService + SettingsService（短路径 tmp 数据根，规避 Windows MAX_PATH）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="m4local_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))
    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    settings = SettingsService(storage=storage, engine=engine)
    asyncio.run(settings.on_start())
    bus = EventBus(engine=engine)
    pending: list = []
    orig_publish = bus.publish

    def spy(ev):
        fs = orig_publish(ev)
        pending.extend(fs)
        return fs

    bus.publish = spy  # type: ignore[method-assign]
    try:
        yield SimpleNamespace(storage=storage, settings=settings, bus=bus,
                              pending=pending, root=root)
    finally:
        asyncio.run(settings.on_stop())
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _drain(pending) -> None:
    for f in list(pending):
        f.result(timeout=5)
    pending.clear()


def _make_service(env) -> LocalBookService:
    svc = LocalBookService(storage=env.storage)
    svc.bus = env.bus
    asyncio.run(svc.on_start())
    return svc


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# 1. txt 基本解析：单章文本落为文本块
# --------------------------------------------------------------------------- #
def test_txt_plain_parse(env):
    svc = _make_service(env)
    f = _write(env.root / "plain.txt",
               "这是第一段。\n这是第二段。\n".encode("utf-8"))
    result = asyncio.run(svc.import_local_file(str(f)))
    assert result.success
    assert result.chapter_count == 1
    ch = result.chapters[0]
    assert isinstance(ch, ChapterItem)
    assert "这是第一段" in result.__dict__["_contents"][0].plain_text


# --------------------------------------------------------------------------- #
# 2. 编码探测链：GBK 编码文本应被正确解码（不出现替换符）
# --------------------------------------------------------------------------- #
def test_txt_charset_detection_gbk(env):
    svc = _make_service(env)
    body = ("第一章 风起青萍\n"
            "夜色如墨，远山如黛。少年独自站在山崖之上，俯瞰着脚下连绵起伏的群山。"
            "他握紧了手中的长剑，指节因为用力而微微发白。山风猎猎，吹动他的衣袍。"
            "第二章 云开雾散\n"
            "晨光驱散了山间的薄雾，山谷中传来潺潺的流水声。少年收拾行囊，继续上路。"
            "这一路山高水长，他要去寻访传说中的剑仙，求得那长生不死的秘术。") * 3
    f = _write(env.root / "gbk.txt", body.encode("gbk"))
    result = asyncio.run(svc.import_local_file(str(f)))
    text = result.__dict__["_contents"][0].plain_text
    # 解码正确则无 Unicode 替换符，且关键中文在
    assert "�" not in text
    assert "风起青萍" in text or "夜色如墨" in text
    assert result.charset and result.charset.upper() in ("GBK", "GB18030")


# --------------------------------------------------------------------------- #
# 3. 标题正则切章：命中"第X章"行应切成多章，章名/索引正确
# --------------------------------------------------------------------------- #
def test_txt_chapter_split_by_title(env):
    svc = _make_service(env)
    body = ("第一章 起点\n"
            "少年睁开眼睛，发现自己躺在一间破旧的茅草屋里。\n"
            "第二章 试炼\n"
            "他走出茅屋，来到村口的老槐树下，那里已经聚集了不少人。\n"
            "第三章 远行\n"
            "辞别了乡亲，少年背上行囊，踏上了去往远方的道路。\n")
    f = _write(env.root / "split.txt", body.encode("utf-8"))
    result = asyncio.run(svc.import_local_file(str(f)))
    assert result.chapter_count == 3
    names = [c.name for c in result.chapters]
    assert any("起点" in n for n in names)
    assert any("试炼" in n for n in names)
    assert [c.index for c in result.chapters] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 4. 无标题 txt 兜底为单章
# --------------------------------------------------------------------------- #
def test_txt_no_title_single_chapter(env):
    svc = _make_service(env)
    body = "这是一段没有任何章节标题的散文。\n行云流水，不知所云。\n" * 5
    f = _write(env.root / "prose.txt", body.encode("utf-8"))
    result = asyncio.run(svc.import_local_file(str(f)))
    assert result.chapter_count == 1
    assert result.chapters[0].name  # 有兜底章名


# --------------------------------------------------------------------------- #
# 5. 归一化模型断言：字段集合与网络书源引擎产物对齐 + books 表 + reader-cache
# --------------------------------------------------------------------------- #
def test_normalized_model_books_table_and_cache(env):
    svc = _make_service(env)
    f = _write(env.root / "norm.txt",
               "第一章 归一化\n正文内容若干字。\n".encode("utf-8"))
    result = asyncio.run(svc.import_local_file(str(f)))

    # 5.1 book_url 以 file:// 标识；source_url 为本地伪源常量
    assert result.book_url.startswith("file://")
    assert result.source_url == LOCAL_BOOK_SOURCE_URL
    assert result.source.bookSourceUrl == LOCAL_BOOK_SOURCE_URL

    # 5.2 ChapterItem 字段集合与 rule_engine.ChapterItem 对齐
    for ch in result.chapters:
        assert isinstance(ch, ChapterItem)
        for field_name in ("name", "url", "index", "is_volume", "book_url"):
            assert hasattr(ch, field_name)

    # 5.3 正文是 ChapterContent，块为 ContentBlock（kind/value）
    doc = result.__dict__["_contents"][0]
    assert isinstance(doc, ChapterContent)
    assert doc.blocks and doc.blocks[0].kind == "text"
    assert isinstance(doc.blocks[0], ContentBlock)

    # 5.4 books 表已登记一行
    row = env.storage.db.execute(
        "SELECT name, kind, source_url FROM books WHERE book_url=?",
        (result.book_url,)).fetchone()
    assert row is not None
    assert row["kind"].startswith("local_")

    # 5.5 reader-cache 可被 ReadingCache 原样读回（下游零改动读取关键证据）
    cache = ReadingCache(env.storage.paths.cache)
    cached = cache.load_catalog(result.source_url, result.book_url)
    assert cached is not None and len(cached) == result.chapter_count
    loaded = cache.load_content(result.source_url, result.book_url,
                                result.chapters[0].url)
    assert loaded is not None and "归一化" in loaded.title or loaded.plain_text


# --------------------------------------------------------------------------- #
# 6. epub 最小样例现场构造并解析
# --------------------------------------------------------------------------- #
def test_epub_minimal_sample(env):
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("id-123")
    book.set_title("测试小书")
    book.add_author("张三")
    c1 = epub.EpubHtml(title="第一章 开端", file_name="chap1.xhtml", lang="zh")
    c1.content = ("<html><body><h1>开端</h1><p>少年步入江湖。</p>"
                  "<p>江湖路远，人心险恶。</p></body></html>").encode("utf-8")
    c2 = epub.EpubHtml(title="第二章 转折", file_name="chap2.xhtml", lang="zh")
    c2.content = ("<html><body><h1>转折</h1><p>他遇见了一位老者。</p></body></html>"
                  ).encode("utf-8")
    for c in (c1, c2):
        book.add_item(c)
    book.spine = [c1, c2]
    book.toc = (c1, c2)
    # reader 需要 ncx/nav 条目才能解析 spine（ebooklib 写读回的标准构造）。
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    f = env.root / "sample.epub"
    epub.write_epub(str(f), book)

    svc = _make_service(env)
    result = asyncio.run(svc.import_local_file(str(f)))
    assert result.success
    assert result.book.name == "测试小书"
    assert result.book.author == "张三"
    # spine 两章正文都应被文本化
    joined = "\n".join(d.plain_text for d in result.__dict__["_contents"])
    assert "少年步入江湖" in joined
    assert "他遇见了一位老者" in joined


# --------------------------------------------------------------------------- #
# 7. pdf 最小样例现场构造并解析
# --------------------------------------------------------------------------- #
def test_pdf_minimal_sample(env):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is page one PDF body content.")
    page2 = doc.new_page()
    page2.insert_text((72, 72), "This is page two PDF body content.")
    f = env.root / "doc.pdf"
    doc.save(str(f))
    doc.close()

    svc = _make_service(env)
    result = asyncio.run(svc.import_local_file(str(f)))
    assert result.success
    joined = "\n".join(d.plain_text for d in result.__dict__["_contents"])
    assert "PDF body content" in joined


# --------------------------------------------------------------------------- #
# 8. mobi 明确报不支持（不硬凑）
# --------------------------------------------------------------------------- #
def test_mobi_unsupported(env):
    svc = _make_service(env)
    f = _write(env.root / "book.mobi", b"# MOBI\x00garbage-bytes-not-real")
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(svc.import_local_file(str(f)))


# --------------------------------------------------------------------------- #
# 9. umd 明确报不支持
# --------------------------------------------------------------------------- #
def test_umd_unsupported(env):
    svc = _make_service(env)
    f = _write(env.root / "book.umd", b"UMD\x00fake")
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(svc.import_local_file(str(f)))


# --------------------------------------------------------------------------- #
# 10. 未知扩展名 / 文件不存在 明确报错
# --------------------------------------------------------------------------- #
def test_unknown_extension_and_missing_file(env):
    svc = _make_service(env)
    f = _write(env.root / "data.xyz", b"hello")
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(svc.import_local_file(str(f)))
    with pytest.raises(FileNotFoundError):
        asyncio.run(svc.import_local_file(str(env.root / "no-such.txt")))


# --------------------------------------------------------------------------- #
# 11. 导入完成发布 ImportFinishedEvent；失败时 count=0 且带错误
# --------------------------------------------------------------------------- #
def test_import_finished_event_published(env):
    svc = _make_service(env)
    fired: list[ImportFinishedEvent] = []

    async def collector(ev):
        fired.append(ev)

    env.bus.subscribe(ImportFinishedEvent, collector)

    f = _write(env.root / "ev.txt", "事件测试正文。\n".encode("utf-8"))
    asyncio.run(svc.import_local_file(str(f)))
    _drain(env.pending)
    evs = [e for e in fired if e.kind == "local_book"]
    assert evs and evs[-1].count == 1 and evs[-1].errors == []

    # 失败路径：count=0 且携带错误信息
    mobi_file = _write(env.root / "x.mobi", b"# MOBI\x00fake-not-supported")
    with pytest.raises(UnsupportedFormatError):
        asyncio.run(svc.import_local_file(str(mobi_file)))
    _drain(env.pending)
    fail = [e for e in fired if e.kind == "local_book" and e.count == 0]
    assert fail and fail[-1].errors
