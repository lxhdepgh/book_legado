"""reader.* 协议面的服务侧护栏（设计 §9 门 G3、§4.2 出口投影、§4.4 二进制不过 RPC）。

这一族动作是跨进程那侧读一本书的唯一路径，所以护栏钉的是**协议面自己**：

- 身份按 url 显式传，会话不是那一本书时必须**拒答**而不是回空目录/空正文
  （回空看起来像书坏了，查的人会把时间花在规则上——记忆里
  ``refactor-regression-orphan-methods`` 那一族最难查的形状就是"接线断在隐式状态上"）；
- 出参必须是 JSON 原生值（``find_leak`` 扫叶子），正文要已经是**可显示形态**
  （``<usehtml>`` 在服务侧渲染掉，两侧才数得出同一个字数），媒体块按 §4.4 交付；
- 位置那一族按位置契约那把尺收口（``app.core.text_layout``），夹与不夹都要有读数；
- 目录行的键与 ``ChapterItem`` 的字段**双向**对齐：反射上协议面不行，
  模型加了字段而协议面漏了也不行。

全程离线：引擎与书源都是替身，不发真实网络请求（记忆 legado-live-scan-av-block）。
每条守卫都过了变异核验，脚本与读数见 ``.probe/g3b_mutation_check.py``。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import ProgressSavedEvent
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.gateway.methods import BY_ACTION
from app.gateway.serialize import find_leak, project, to_json
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem
from app.services.reading.reading_service import (
    ReadingService,
    ReaderSessionError,
    _block_row,
    _chapter_row,
)
from app.services.reading_cache import ReadingCache

SOURCE_URL = "http://127.0.0.1:1/g3"
BOOK_URL = f"{SOURCE_URL}/book/1"
CHAPTER_URLS = [f"{SOURCE_URL}/ch/{i}.html" for i in (1, 2, 3)]

#: 原文形态的一章：带 <usehtml> 标记（渲染会改字数），中间挂一张插图。
#: 段长**故意不均匀**：段长一样的语料里"像素插值"与"段号导出"会算出几乎同一个数。
RAW_BLOCKS = [
    ContentBlock("text",
                 "<usehtml><b>甲段重点句</b></usehtml>\n\n"
                 + "乙段" + "乙" * 40 + "\n\n丙段" + "丙" * 7),
    ContentBlock("image", f"{SOURCE_URL}/pic.jpg"),
    ContentBlock("text", "丁段" + "丁" * 17 + "\n\n戊段" + "戊" * 3),
]


def _catalog() -> list[ChapterItem]:
    return [ChapterItem(name=f"第{index + 1}章", url=url, index=index)
            for index, url in enumerate(CHAPTER_URLS)]


def _book_dict(shelved_url: str = BOOK_URL) -> dict:
    return {"name": "G3 样本书", "author": "作者戊", "url": shelved_url,
            "origin": SOURCE_URL, "coverUrl": "", "kind": "text"}


class _Engine:
    """引擎替身：目录是造的，正文永远返回同一份原文块，并记录被请求了哪几章。"""

    def __init__(self, catalog):
        self.catalog = list(catalog)
        self.calls: list[str] = []

    async def chapters(self, source, book):
        return list(self.catalog)

    async def content_document(self, source, chapter, next_url=""):
        self.calls.append(str(chapter.url))
        return ChapterContent(title=chapter.name, blocks=list(RAW_BLOCKS))


class _Sources:
    """书源/书架替身：只实现协议面那族用到的那几个方法。"""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.loaded: list[str] = []

    async def load_source(self, url):
        self.loaded.append(str(url or ""))
        return SimpleNamespace(bookSourceUrl=str(url or ""), bookSourceName="G3 源")

    def add_to_bookshelf(self, source, book, shelved=True):
        self.records[book.url] = {
            "source_url": source.bookSourceUrl, "url": book.url,
            "name": book.name, "kind": "text", "shelved": bool(shelved)}

    def find_bookshelf_item(self, item):
        return self.records.get(str((item or {}).get("url") or ""))

    def is_shelved(self, record):
        return bool((record or {}).get("shelved"))

    async def promote_book(self, source_url, book_url):
        record = self.records.get(book_url)
        if record is None or record["shelved"]:
            return False
        record["shelved"] = True
        return True

    async def unshelve_book(self, source_url, book_url):
        return self.records.pop(book_url, None) is not None


class _Rig:
    def __init__(self, reading, sources, engine, storage, settings, bus,
                 root, events):
        self.reading = reading
        self.sources = sources
        self.engine = engine
        self.storage = storage
        self.settings = settings
        self.bus = bus
        self.root = root
        self.events = events

    async def close(self) -> None:
        self.reading.stop_prefetch()
        await self.reading.on_stop()
        await self.settings.on_stop()
        await self.storage.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def drain(self) -> None:
        for future in list(self.events["pending"]):
            future.result(timeout=5)
        self.events["pending"].clear()


async def _rig(*, open_the_book: bool = True) -> _Rig:
    """真 StorageService/ReadingCache + 替身引擎与书源，并按协议面开好会话。"""
    root = Path(tempfile.mkdtemp(prefix="g3rpc_"))
    os.environ["LEGADO_BOOKSOURCE_DATA_DIR"] = str(root / "data")
    os.environ["LEGADO_BOOKSOURCE_CACHE_DIR"] = str(root / "cache")
    hub_engine = engine_hub()
    storage = StorageService()
    await storage.start()
    settings = SettingsService(storage=storage, engine=hub_engine)
    await settings.on_start()
    engine = _Engine(_catalog())
    sources = _Sources()
    reading = ReadingService(rule_engine=engine, storage=storage, settings=settings,
                             source_service=sources, cache=ReadingCache(storage.paths.cache))
    bus = EventBus(engine=hub_engine)
    orig_publish = bus.publish
    events: dict = {"saved": [], "pending": []}

    def spy(event):
        if isinstance(event, ProgressSavedEvent):
            events["saved"].append(event)
        futures = orig_publish(event)
        events["pending"].extend(futures)
        return futures
    bus.publish = spy
    reading.bus = bus
    rig = _Rig(reading, sources, engine, storage, settings, bus, root, events)
    if open_the_book:
        await reading.open_by_url(SOURCE_URL, _book_dict(), shelve=True, resume=False)
    return rig


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 一、会话归属：错的书必须拒答，不能回空
# --------------------------------------------------------------------------- #

def test_reader_actions_all_target_json_only_facades() -> None:
    """reader.* 每一条都解析得到可调用目标，且 params 全是它接受的关键字参数。

    这一条钉的是"表里写了却没接上"那一族：动作名对了、方法名漂了，网关会在
    运行期才报 unavailable，而契约测试本来能当场抓住。
    """
    from app.gateway.methods import resolve_service_class

    reader_actions = [m for m in BY_ACTION.values() if m.action.startswith("reader.")]
    assert len(reader_actions) >= 10, f"阅读面动作太少：{sorted(m.action for m in reader_actions)}"
    for method in reader_actions:
        func = getattr(resolve_service_class(method.service), method.method)
        accepted = set(func.__code__.co_varnames[1:func.__code__.co_argcount])
        missing = [name for name in method.params if name not in accepted]
        assert not missing, f"{method.action}：{method.method} 不吃 {missing}"


def test_toc_and_content_refuse_a_book_the_session_is_not() -> None:
    """会话不是那一本书时，reader.toc / reader.content / reader.position_save 都拒答。

    反证两半：① 不许回空列表（空目录看起来像书坏了）；② 不许顺手把别的书的章当这本的
    去请求——engine.calls 必须一根都不涨。
    """
    async def main():
        rig = await _rig()
        try:
            other = f"{SOURCE_URL}/book/999"
            for coro in (rig.reading.chapter_rows(SOURCE_URL, other),
                         rig.reading.chapter_document(SOURCE_URL, other, CHAPTER_URLS[0]),
                         rig.reading.save_position(
                             SOURCE_URL, other, {"chapter_index": 0})):
                with pytest.raises(ReaderSessionError):
                    await coro
            assert rig.engine.calls == [], f"拒答的路上还是去取了正文：{rig.engine.calls}"
        finally:
            await rig.close()

    _run(main())


def test_toc_still_works_for_the_open_book() -> None:
    """正对照：同一批调用换成会话那本书就必须成功，否则上一条是"任何调用都红"。"""
    async def main():
        rig = await _rig()
        try:
            rows = await rig.reading.chapter_rows(SOURCE_URL, BOOK_URL)
            assert len(rows) == 3, rows
        finally:
            await rig.close()

    _run(main())


# --------------------------------------------------------------------------- #
# 二、正文出参：已经是可显示形态，媒体按 §4.4 交付
# --------------------------------------------------------------------------- #

def test_chapter_document_leaves_no_usehtml_markup_behind() -> None:
    """出参的文本块里不许还有 ``<usehtml>`` 或标签：字数会被渲染改掉，两侧必须同一串。"""
    async def main():
        rig = await _rig()
        try:
            document = await rig.reading.chapter_document(
                SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
            joined = "".join(str(block.get("value") or "")
                             for block in document["blocks"]
                             if block.get("kind") == "text")
            assert "<usehtml>" not in joined and "</usehtml>" not in joined, joined[:120]
            assert "<b>" not in joined, f"标签没被清洗掉：{joined[:120]}"
            assert "甲段重点句" in joined, f"渲染把内容也吃了：{joined[:120]}"
        finally:
            await rig.close()

    _run(main())


def test_chapter_document_counts_paragraphs_over_text_blocks_only() -> None:
    """段数与总字数都不许把插图算进去；两者互相能对上（不读被测常量）。"""
    async def main():
        rig = await _rig()
        try:
            document = await rig.reading.chapter_document(
                SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
            texts = [str(block.get("value") or "") for block in document["blocks"]
                     if block.get("kind") == "text"]
            kinds = [block.get("kind") for block in document["blocks"]]
            assert "image" in kinds, f"插图块没了：{kinds}"
            # 章内字符流就是 "\n".join(自然段)：长度用整串实算，不引常量。
            from app.core.text_layout import paragraph_lines
            paragraphs = [line for value in texts for line in paragraph_lines(value)]
            stream = "\n".join(paragraphs)
            assert document["paragraphCount"] == len(paragraphs), document
            assert document["totalChars"] == len(stream), (
                f"totalChars={document['totalChars']} vs 实算 {len(stream)}")
            assert len(paragraphs) >= 5, f"段数少得不像话：{len(paragraphs)}"
        finally:
            await rig.close()

    _run(main())


def test_media_block_travels_as_url_not_bytes() -> None:
    """§4.4：媒体块给地址、binary=false；真二进制才该 value=null + binary=true。"""
    url_row = _block_row(ContentBlock("image", f"{SOURCE_URL}/pic.jpg"))
    assert url_row["binary"] is False and url_row["value"] == f"{SOURCE_URL}/pic.jpg"
    assert url_row["url"] == f"{SOURCE_URL}/pic.jpg", "地址两格都不给，客户端就没图可渲染"
    bytes_row = _block_row(ContentBlock("image", b"\xff\xd8\xff\xe0"))
    assert bytes_row["binary"] is True and bytes_row["value"] is None, bytes_row
    assert bytes_row["url"] == "", f"没有地址时不该编一个出来：{bytes_row}"
    assert _block_row(ContentBlock("text", "一段正文"))["url"] == ""
    # 真二进制不许在 JSON 里变成一个"看起来像值"的字符串：过一遍编码再解回来。
    assert json.loads(json.dumps(to_json(bytes_row)))["value"] is None
    # 按**未降级**的那一份判：to_json 见对象就展开（它的设计），过了它再扫等于
    # 在测 to_json 的降级能力，门面漏一个 ContentBlock 也照样"干净"。
    assert find_leak([url_row, bytes_row]) is None


def test_protected_image_keeps_its_address_through_the_cache() -> None:
    """受保护图被引擎换成字节之后，地址还得能过一遍缓存回到协议面。

    ``reader.content`` 永远不给字节（§4.4），客户端渲染全靠 ``url`` + ``headers``
    那一格——地址在 ``value`` 被覆盖时丢了，界面就是一张永远空白的图，而且没人报错
    （记忆里 ``guard-tests-assert-absence`` 的反向：这一条钉的是"还在"）。
    走真 ``ReadingCache`` 的落盘/读回：地址若只活在内存里那一趟，缓存一过就没了。
    """
    from app.services.engine.reading_engine import display_blocks
    from app.services.reading_cache import ReadingCache

    root = Path(tempfile.mkdtemp(prefix="g3cache_"))
    try:
        cache = ReadingCache(root / "cache")
        picture = f"{SOURCE_URL}/pic.jpg"
        cache.save_content(SOURCE_URL, BOOK_URL, CHAPTER_URLS[0], ChapterContent(
            title="第一章",
            blocks=[ContentBlock("text", "甲段。\n\n乙段。"),
                    ContentBlock("image", b"\xff\xd8\xff\xe0",
                                 {"Authorization": "Bearer x"}, "插图", picture)]))
        loaded = cache.load_content(SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
        assert loaded is not None, "缓存读不回整章：这一发连正文都没有"
        rows = [_block_row(block) for block in display_blocks(loaded)]
        media = rows[1]
        assert media["binary"] is True and media["value"] is None, media
        assert media["url"] == picture, f"地址在落盘那一趟丢了：{media}"
        assert media["headers"] == {"Authorization": "Bearer x"}, media
        assert rows[0]["url"] == "", "文本块不该带地址"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 三、目录行：键与 ChapterItem 双向对齐，投影裁得掉重的
# --------------------------------------------------------------------------- #

def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def test_chapter_row_keys_match_chapter_item_fields_both_ways() -> None:
    """``_chapter_row`` 的键集合必须正好是 ChapterItem 字段的驼峰形：双向都不许多。

    少一个键 = 模型加了字段而协议面默默丢列（读数会是"这一列恒为 null"）；
    多一个键 = 反射把内部字段顶上了协议面（设计 §2 数的那笔账）。
    """
    row = _chapter_row(_catalog()[0])
    declared = {_camel(item.name) for item in fields(ChapterItem)}
    assert set(row) == declared, (
        f"只在行里：{sorted(set(row) - declared)}；只在模型里：{sorted(declared - set(row))}")


def test_reader_toc_whitelist_names_keys_the_row_actually_has() -> None:
    """``reader.toc`` 的 fields 白名单里每个名字都必须在行里存在。

    白名单写错一个名字，``project`` 会安静地不补那一格（serialize.project 的口径），
    客户端只会看到"这一列一直是空的"——这是"计数为 0 的读数"那一族。
    """
    method = BY_ACTION["reader.toc"]
    assert method.fields, "reader.toc 没声明出口投影：整行变量会跟着目录一起过网"
    row_keys = set(_chapter_row(_catalog()[0]))
    unknown = [name for name in method.fields if name not in row_keys]
    assert unknown == [], f"白名单点名了行里没有的键：{unknown}"


def test_reader_toc_projection_drops_the_heavy_fields() -> None:
    """投影后目录行只剩白名单那几格；未投影的那一份必须真的带着重的（正对照）。"""
    rows = [_chapter_row(chapter) for chapter in _catalog()]
    heavy = {"variables", "start", "end", "startFragmentId", "endFragmentId",
             "baseUrl", "bookUrl", "resourceUrl"}
    assert set(rows[0]) & heavy, f"行里本来就没有重字段，这条守卫是聋的：{rows[0]}"
    projected = project(to_json(rows), BY_ACTION["reader.toc"].fields)
    for row in projected:
        assert set(row) <= set(BY_ACTION["reader.toc"].fields), row
        assert not set(row) & heavy, row
    assert (len(json.dumps(projected, ensure_ascii=False).encode("utf-8"))
            < len(json.dumps(rows, ensure_ascii=False).encode("utf-8")))


# --------------------------------------------------------------------------- #
# 四、位置：按 index 认章、按字符流夹偏移，且两向都有读数
# --------------------------------------------------------------------------- #

def test_save_position_reanchors_the_chapter_by_index() -> None:
    """客户端报了矛盾的 (index, url) 时，落库以目录里那一条为准。"""
    async def main():
        rig = await _rig()
        try:
            saved = await rig.reading.save_position(
                SOURCE_URL, BOOK_URL,
                {"chapter_index": 2, "chapter_url": f"{SOURCE_URL}/lies.html",
                 "chapter_pos": 3})
            assert saved["chapter_url"] == CHAPTER_URLS[2], saved
        finally:
            await rig.close()

    _run(main())


def test_save_position_clamps_only_what_is_out_of_range() -> None:
    """范围内原样落、越界夹回，两个方向都要有读数（只测夹回会绿在"永远夹"上）。"""
    async def main():
        rig = await _rig()
        try:
            document = await rig.reading.chapter_document(
                SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
            total = int(document["totalChars"])
            paragraphs = int(document["paragraphCount"])
            inside = await rig.reading.save_position(
                SOURCE_URL, BOOK_URL,
                {"chapter_index": 0, "chapter_pos": 12, "chapter_paragraph": 1,
                 "scroll_ratio": 0.4})
            assert inside["chapter_pos"] == 12, inside
            assert inside["chapter_paragraph"] == 1, inside
            over = await rig.reading.save_position(
                SOURCE_URL, BOOK_URL,
                {"chapter_index": 0, "chapter_pos": total + 9999,
                 "chapter_paragraph": paragraphs + 40, "scroll_ratio": 3.5})
            assert over["chapter_pos"] == total, over
            assert over["chapter_paragraph"] == paragraphs - 1, over
            assert over["scroll_ratio"] == 1.0, over
            # 正对照：夹过的那一发与没夹的那一发必须不是同一个数，否则"夹回"没跑到。
            assert over["chapter_pos"] != inside["chapter_pos"]
        finally:
            await rig.close()

    _run(main())


def test_read_position_distinguishes_no_record_from_zero() -> None:
    """没记录回 None（不是"位置为 0"）；落过一篇之后必须读回那一篇。"""
    async def main():
        rig = await _rig()
        try:
            assert await rig.reading.read_position(SOURCE_URL, BOOK_URL) is None
            await rig.reading.save_position(
                SOURCE_URL, BOOK_URL, {"chapter_index": 1, "chapter_pos": 77})
            rig.drain()
            restored = await rig.reading.read_position(SOURCE_URL, BOOK_URL)
            assert restored is not None and restored["chapter_pos"] == 77, restored
            # books 表不存 chapter_url：这条存档的续读靠 index，样本与客户端都不能以为 url 可靠。
            assert restored["chapter_url"] == "", restored
        finally:
            await rig.close()

    _run(main())


def test_position_save_then_close_keeps_the_saved_position() -> None:
    """RPC 落过一篇位置再关会话，读回来必须还是那一篇（关的时候不许盖回默认值）。

    ``reader.close(save_progress=true)`` 落的是**会话上**那一份位置，而跨进程客户端
    的位置只在 ``reader.position_save`` 那一发里出现过——门面要是不把它写回会话，
    退出那一发就把刚存的落点换成 chapter 0/偏移 0。这条是 G3 收样本的活臂先撞到的
    （``.probe/g3_golden_run.log`` 里 position_get 读回一整串 0）。
    """
    async def main():
        rig = await _rig()
        try:
            saved = await rig.reading.save_position(
                SOURCE_URL, BOOK_URL,
                {"chapter_index": 0, "chapter_pos": 137, "chapter_paragraph": 2})
            closed = await rig.reading.close_by_url(SOURCE_URL, BOOK_URL,
                                                    save_progress=True)
            rig.drain()
            assert closed["closed"] is True, closed
            restored = await rig.reading.read_position(SOURCE_URL, BOOK_URL)
            assert restored is not None and int(restored["chapter_pos"]) == 137, restored
            assert int(restored["chapter_index"]) == int(saved["chapter_index"]), restored
        finally:
            await rig.close()

    _run(main())


def test_close_with_the_wrong_book_saves_nothing() -> None:
    """关错书：closed=false，且一根进度事件都不发（断言的是"没发生"）。"""
    async def main():
        rig = await _rig()
        try:
            await rig.reading.save_position(
                SOURCE_URL, BOOK_URL, {"chapter_index": 0, "chapter_pos": 5})
            rig.drain()
            rig.events["saved"].clear()
            result = await rig.reading.close_by_url(
                SOURCE_URL, f"{SOURCE_URL}/别的书", save_progress=True)
            assert result["closed"] is False, result
            assert rig.events["saved"] == [], "关错书却落了进度：会盖掉别人那一行"
            assert rig.reading._current_book is not None, "会话被关掉了"
        finally:
            await rig.close()

    _run(main())


def test_close_for_the_right_book_saves_and_reports_staged() -> None:
    """关对的那一发：进度落库（断言"发生了"），且 staged 给的是记录本体不是布尔。"""
    async def main():
        rig = await _rig()
        try:
            result = await rig.reading.close_by_url(SOURCE_URL, BOOK_URL,
                                                    save_progress=True)
            rig.drain()
            assert result["closed"] is True, result
            assert len(rig.events["saved"]) == 1, rig.events["saved"]
            # 这本书是 in-shelf（open 时 shelve=True），暂存记录该是 None 而不是 False。
            assert result["staged"] is None, result
            assert isinstance(rig.sources.records.get(BOOK_URL), dict)
        finally:
            await rig.close()

    _run(main())


def test_staged_round_trip_reports_the_record_then_nothing() -> None:
    """试读那条线：staged 回的是那条书架记录（dict），转正/放弃各回一次真读数。"""
    async def main():
        rig = await _rig(open_the_book=False)
        try:
            await rig.reading.open_by_url(SOURCE_URL, _book_dict(), shelve=False,
                                          resume=False)
            staged = rig.reading.staged_record()
            assert isinstance(staged, dict) and staged.get("shelved") is False, staged
            assert await rig.reading.promote_staged_book() is True
            assert rig.reading.staged_record() is None
            await rig.reading.open_by_url(SOURCE_URL, _book_dict(), shelve=False,
                                          resume=False)
            assert await rig.reading.discard_staged_book() is True
            assert rig.sources.find_bookshelf_item({"url": BOOK_URL}) is None
        finally:
            await rig.close()

    _run(main())


def test_close_reports_the_staged_record_in_its_own_shape() -> None:
    """退出试读那一发的 ``staged`` 必须是**那条记录本身**，不是它的空壳。

    这一族里唯一没被真进程语料跑到的分支：四发 ``reader.close`` 抓的都是**在架**的书，
    ``staged`` 全是 null（而 null 在那一发是合法回话）。被它罩住的是一个真缺陷——
    ``_book_item_dict`` 对 dict 走 ``getattr`` 兜底，而 ``getattr({...}, "name", "")``
    恒为空串，回出来的是一份**键齐全、值全空**的壳。所以这里的靶子必须是
    "名字是那个真名字"：``is not None`` 那一断言在缺陷版本上照样绿。

    与 ``reader.staged`` 那一发比的是**键集相等**：同一条记录不许有两种形状，
    否则客户端得写两套 ``fromJson``（记忆里 `legado-search-changesource-alignment`
    那条"两边同一套键"的规矩，这是它的第三个读数口）。
    """
    async def main():
        rig = await _rig(open_the_book=False)
        try:
            await rig.reading.open_by_url(SOURCE_URL, _book_dict(), shelve=False,
                                          resume=False)
            before = rig.reading.staged_record()
            assert isinstance(before, dict) and before.get("name") == "G3 样本书", before
            result = await rig.reading.close_by_url(SOURCE_URL, BOOK_URL,
                                                    save_progress=True)
            assert result["closed"] is True, result
            staged = result["staged"]
            assert isinstance(staged, dict), f"待询问的那一发回的不是记录：{staged!r}"
            assert staged.get("name") == "G3 样本书", (
                f"空壳那一支又回来了：{staged!r}")
            assert set(staged) == set(before), (
                f"close 的 staged 与 reader.staged 键集不同：{set(staged) ^ set(before)}")
            assert staged == before
        finally:
            await rig.close()

    _run(main())


# --------------------------------------------------------------------------- #
# 五、载荷形状：整族出参都不许漏模型对象；open 那一发不带目录
# --------------------------------------------------------------------------- #
def test_open_snapshot_carries_no_chapter_rows() -> None:
    """reader.open 的返回里不许有目录：把几千章挂在"点开一本书"上是 G2 那笔账的重演。"""
    async def main():
        rig = await _rig(open_the_book=False)
        try:
            snapshot = await rig.reading.open_by_url(SOURCE_URL, _book_dict(),
                                                     shelve=True, resume=False)
            assert not ({"chapters", "toc", "chapterRows"} & set(snapshot)), sorted(snapshot)
            assert snapshot["chapterCount"] == len(rig.reading._session.chapters)
            assert find_leak(snapshot) is None
        finally:
            await rig.close()

    _run(main())


def test_every_reader_facade_return_is_json_native() -> None:
    """这一族每个门面的返回**本身就是** JSON 原生值（设计 §5.3 的出口序列化）。

    判的是门面交出的那一份，不是网关降级之后的那一份：``to_json`` 见 dataclass 就
    按字段展开、见对象就 ``vars()``，扫它等于测降级器而不是测门面——门面漏一个
    ``ContentBlock`` 也照样"干净"（护栏必须能红，见 ``guard-tests-assert-absence``）。    """
    collected: list = []

    async def main():
        rig = await _rig()
        try:
            collected.append(await rig.reading.chapter_rows(SOURCE_URL, BOOK_URL))
            collected.append(await rig.reading.chapter_document(
                SOURCE_URL, BOOK_URL, CHAPTER_URLS[0]))
            collected.append(await rig.reading.save_position(
                SOURCE_URL, BOOK_URL, {"chapter_index": 0, "chapter_pos": 4}))
            collected.append(await rig.reading.read_position(SOURCE_URL, BOOK_URL))
            collected.append(rig.reading.staged_record())
            collected.append(await rig.reading.close_by_url(SOURCE_URL, BOOK_URL,
                                                            save_progress=False))
        finally:
            await rig.close()

    _run(main())
    assert len(collected) == 6, collected
    for index, value in enumerate(collected):
        assert find_leak(value) is None, f"第 {index} 份出参漏了模型对象"


# --------------------------------------------------------------------------- #
# 六、书内搜索：索引与查在同一步，0 命中分得清原因
# --------------------------------------------------------------------------- #

def test_search_ensured_distinguishes_no_index_from_no_word() -> None:
    """空命中必须带着"索引覆盖了几章"回来，否则客户端分不清"没缓存"与"没这个词"。"""
    from app.services.reading.content_search import ContentSearchService

    async def main():
        rig = await _rig()
        try:
            await rig.reading.chapter_document(SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
            search = ContentSearchService(storage=rig.storage,
                                          cache=ReadingCache(rig.storage.paths.cache))
            await search.on_start()
            absent = await search.search_ensured(BOOK_URL, "根本不存在的词组", SOURCE_URL)
            assert absent["ftsAvailable"] is True, absent
            assert absent["indexedChapters"] >= 1, absent
            assert absent["hits"] == [], absent
            present = await search.search_ensured(BOOK_URL, "甲段重点", SOURCE_URL)
            assert present["hits"], present
            assert present["hits"][0]["para_index"] == 0, present
        finally:
            await search.on_stop()
            await rig.close()

    _run(main())


def test_index_offsets_use_the_position_ruler() -> None:
    """FTS 行里的段首偏移必须等于位置契约算出来的那一份（两把尺会跳错段）。"""
    from app.core.text_layout import chapter_paragraphs, paragraph_offsets
    from app.services.engine.reading_engine import display_blocks
    from app.services.reading.content_search import ContentSearchService

    async def main():
        rig = await _rig()
        try:
            await rig.reading.chapter_document(SOURCE_URL, BOOK_URL, CHAPTER_URLS[0])
            search = ContentSearchService(storage=rig.storage,
                                          cache=ReadingCache(rig.storage.paths.cache))
            await search.on_start()
            assert await search.ensure_index(BOOK_URL, SOURCE_URL) >= 5
            # 只看第一章：正文门面带起一轮预取，后面几章也在索引里，
            # 而它们的段首偏移各自从 0 数起（章内坐标，不是全书坐标）。
            rows = rig.storage.db.execute(
                "SELECT para_index, offset FROM content_search"
                " WHERE book_url=? AND chapter_index=0 ORDER BY para_index",
                (BOOK_URL,)).fetchall()
            paragraphs = chapter_paragraphs(display_blocks(
                ChapterContent(blocks=list(RAW_BLOCKS))))
            expected = paragraph_offsets(paragraphs)
            assert len(rows) == len(expected), (rows, expected)
            assert [int(r[0]) for r in rows] == list(range(len(expected))), rows
            assert [float(r[1]) for r in rows] == [float(v) for v in expected], rows
            # 反证：不含段间换行格的那把旧尺不许也能对上。
            assert [float(r[1]) for r in rows] != [
                float(sum(len(text) for text in paragraphs[:i]))
                for i in range(len(expected))]
        finally:
            await search.on_stop()
            await rig.close()

    _run(main())


def test_controller_does_not_sequence_index_then_search_itself() -> None:
    """flet 那侧不许自己排"先建索引再查"：两步的顺序由 search_ensured 一处负责。

    两侧各排一遍就是两条会漂的路径——协议面忘了第一步时，书内搜索永远回 0 条，
    而这正是 ``refactor-regression-orphan-methods`` 那一族的形状（服务在、方法在、
    接线断在调用顺序上）。
    """
    from pathlib import Path as _Path

    source = (_Path(__file__).resolve().parents[1]
              / "app" / "controllers" / "reader_controller.py").read_text(
                  encoding="utf-8")
    assert "ensure_index(" not in source, "控制器又自己排建索引那一步了"
    assert "search_ensured(" in source
