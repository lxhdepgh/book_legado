"""LocalBookService：本地书导入与解析（M4 内容形态域，设计 §1.11 / 契约 §4）。

职责：把用户磁盘上的电子书文件（txt/epub/pdf/mobi/umd）导入并解析成**与网络书源
完全一致**的章节/正文数据模型，使下游 :class:`~app.services.reading.reading_service.
ReadingService` / 阅读视图**零改动**即可打开阅读。

归一化落点（硬指标，§1.11）：

1. ``book_url`` 用 ``file://`` 标识（``Path.resolve().as_uri()`` 规范形态）；
2. 经 :class:`~app.services.reading_cache.ReadingCache` 把目录写为 ``catalog.json``、
   每章正文写为 ``content/*.json``——与网络书源阅读走同一条缓存键
   ``(source_url, book_url[, chapter_url])``；
3. 在 ``StorageService.books`` 表登记一条本地书记录（书元数据 + 初始进度列）；
4. 导入完成发布 :class:`~app.core.events.ImportFinishedEvent`。

格式策略（契约 §4 / §10，懒加载、不硬凑）：

- ``txt``：自写解析，编码探测复用
  :func:`~app.services.engine.icu_charset.detect_charset` 链（ICU→charset-normalizer）；
  按可配标题正则切章，未命中标题则整篇一章。
- ``epub``：懒加载 ``ebooklib``，按 spine 顺序取正文条目，HTML 文本化并保留内嵌图片。
- ``pdf``：懒加载 ``pymupdf``（import pymupdf），逐页取文本，按页聚合为章节。
- ``mobi``：尝试 ``ebooklib``（其不支持 mobi），无法解析则抛
  :class:`UnsupportedFormatError` 明确报错——不做二进制硬猜文本。
- ``umd``：冷门格式，无可解析的纯 Python 库，登记为不支持并明确报错。

红线：本模块**零 flet import**；ebooklib/pymupdf 仅在对应解析函数内懒 import。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.events import ImportFinishedEvent
from app.core.lifecycle import Service
from app.core.storage import StorageService
from app.services.engine.rule_engine import BookItem, ChapterItem
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.reading_cache import ReadingCache

logger = logging.getLogger(__name__)

# 本地书在阅读域扮演的"伪书源"标识：网络书源的 bookSourceUrl 是其规则源 URL，
# 本地书没有规则源，统一用一个常量 source_url。ReadingCache 的目录/正文缓存键、
# books 表 source_url 列、以及返回给 ReadingService.open_book 的伪 source 对象
# 三处必须用同一常量，缓存才能被下游命中（§1.11 零改动复用的关键）。
LOCAL_BOOK_SOURCE_URL = "localsource://local_book"
LOCAL_BOOK_SOURCE_NAME = "本地书籍"

# 按扩展名分发解析器；mobi/umd 登记为不支持（见模块 docstring）。
#: 解析器真正吃得下的扩展名——导入入口的文件选择器必须用这个集合，
#: 否则用户会挑到一个必然报错的文件。
SUPPORTED_EXTS = {"txt", "epub", "pdf"}
_UNSUPPORTED_EXTS = {"mobi", "umd"}

# txt 章节标题默认正则：命中即作为新一章开头。覆盖常见中文卷/章/回/节与英文 Chapter。
# 可经 import_local_file(title_pattern=...) 覆盖（契约 §4：标题正则可配）。
_DEFAULT_TITLE_PATTERN = (
    r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千两]+\s*[章回节卷部篇]"
    r"[^\n]{0,40}|Chapter\s+\d+[^\n]{0,40})\s*$")


class UnsupportedFormatError(ValueError):
    """本地书格式暂不支持（mobi/umd 或未知扩展名）——明确报错，不硬凑解析。"""


class LocalBookCacheMissing(RuntimeError):
    """本地书的目录/正文只活在 reader-cache 里：缓存丢了就读不了。

    绝不能退回"让规则引擎去抓 file:// 这本书"——那是一条永远抓不到的联网请求，
    报错还会被说成网络问题。视图照这条提示用户重新导入文件。
    """


def is_local_source_url(url) -> bool:
    """这个源地址是不是本地书伪源（阅读域反查不到源时用它认领）。"""
    return str(url or "") == LOCAL_BOOK_SOURCE_URL


def make_local_source():
    """本地书的伪源对象（``bookSourceUrl`` / ``bookSourceName`` 两个属性）。

    从书架重开本地书时走 :class:`SourceService` 反查必然落空（它只登记了规则源），
    所以阅读域用这个合成对象顶上——缓存键、书架记录、入架写回都只认 ``bookSourceUrl``。
    """
    return SimpleNamespace(
        bookSourceUrl=LOCAL_BOOK_SOURCE_URL,
        bookSourceName=LOCAL_BOOK_SOURCE_NAME)


@dataclass
class ImportResult:
    """一次本地书导入的归一化产物（契约 §4 import_local_file 返回值）。

    ``source`` / ``book`` 可直接传给 ``ReadingService.open_book(source, book)``：
    目录与正文已在导入时落 reader-cache，下游 open_book 走缓存命中，无需联网/规则引擎。
    """

    path: str
    book_url: str
    source_url: str
    book: BookItem
    source: Any
    chapters: list[ChapterItem] = field(default_factory=list)
    chapter_count: int = 0
    charset: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.chapter_count > 0 and not self.errors


class LocalBookService(Service):
    """本地书导入与解析服务（M4，services/content/）。"""

    name = "LocalBookService"
    # 拓扑依赖：StorageService（books 表登记 + reader-cache 目录）。
    # 本地解析纯本地文件处理，不触网、不走规则引擎，故不声明 RuleEngine/BookCache。
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage=None, cache: ReadingCache | None = None,
                 title_pattern: str | None = None,
                 pdf_pages_per_chapter: int | None = None,
                 source_service=None) -> None:
        super().__init__()
        self._storage = storage
        self._cache = cache
        # 书架列表归 SourceService 管（books 表那行只是进度），导入要落架就问它取。
        self._source_service = source_service
        # txt 标题正则可配；pdf 每页/章聚合粒度可配（None 时按页数自适应）。
        self._title_pattern = title_pattern or _DEFAULT_TITLE_PATTERN
        self._pdf_pages_per_chapter = pdf_pages_per_chapter

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._cache is None and self._storage is not None:
            # ReadingCache 会在给定根下追加 "reader-cache"，与 ReadingService 一致。
            self._cache = ReadingCache(self._storage.paths.cache)
        if self._source_service is None and self.hub is not None:
            try:
                from app.services.sources.source_service import SourceService
                self._source_service = self.hub.require(SourceService)
            except Exception:
                logger.info("LocalBookService：SourceService 未就绪，导入不落书架列表")
        logger.info("LocalBookService 已启动")

    async def on_stop(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    async def import_local_file(self, path: str | Path, *,
                                title_pattern: str | None = None) -> ImportResult:
        """按扩展名分发解析本地文件，归一化落库并发布 ImportFinishedEvent。

        成功时返回 :class:`ImportResult`；格式不支持或解析失败时先发布事件再抛出
        :class:`UnsupportedFormatError` / 原始异常，供视图层提示用户。
        """
        src = Path(path)
        errors: list[str] = []
        try:
            result = await self._parse(src, title_pattern=title_pattern)
            self._register_books_table(result)
            self._write_cache(result)
            await self._shelve(result)
            self._publish(ImportFinishedEvent(
                kind="local_book", count=1, errors=[]))
            logger.info("本地书导入成功: %s -> %s（%d 章）",
                        src.name, result.book_url, result.chapter_count)
            return result
        except Exception as exc:  # 统一：事件先落地，再把异常抛回视图
            message = f"{type(exc).__name__}: {exc}"
            errors.append(message)
            self._publish(ImportFinishedEvent(
                kind="local_book", count=0, errors=errors))
            logger.warning("本地书导入失败: %s - %s", src, message)
            raise

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    async def _parse(self, src: Path, *,
                     title_pattern: str | None) -> ImportResult:
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"文件不存在或不是普通文件: {src}")
        ext = src.suffix.lower().lstrip(".")
        if ext in _UNSUPPORTED_EXTS:
            raise UnsupportedFormatError(
                f"暂不支持该格式（.{ext}）：本地无可用解析库，M4 不做二进制硬凑文本")
        if ext not in SUPPORTED_EXTS:
            raise UnsupportedFormatError(
                f"暂不支持的文件类型 .{ext}（支持 txt/epub/pdf）")

        book_url = src.resolve().as_uri()
        if ext == "txt":
            meta_chapters = self._parse_txt(
                src, title_pattern=title_pattern or self._title_pattern)
        elif ext == "epub":
            meta_chapters = self._parse_epub(src)
        else:  # pdf
            meta_chapters = self._parse_pdf(src)

        book_meta, raw_chapters = meta_chapters
        book = BookItem(
            name=book_meta.get("name") or src.stem,
            author=book_meta.get("author") or "",
            url=book_url,
            kind=f"local_{ext}",
            cover_url=book_meta.get("cover_url") or "",
            origin=LOCAL_BOOK_SOURCE_URL,
            origin_name=LOCAL_BOOK_SOURCE_NAME,
        )
        source = make_local_source()

        chapters: list[ChapterItem] = []
        contents: list[ChapterContent] = []
        for index, (title, document) in enumerate(raw_chapters):
            chapter_url = f"{book_url}#ch{index}"
            chapters.append(ChapterItem(
                name=title or f"第{index + 1}章", url=chapter_url,
                index=index, book_url=book_url))
            document.title = title or document.title
            contents.append(document)

        result = ImportResult(
            path=str(src), book_url=book_url,
            source_url=LOCAL_BOOK_SOURCE_URL,
            book=book, source=source, chapters=chapters,
            chapter_count=len(chapters),
            charset=book_meta.get("charset"))
        # 正文与章节一一对应挂到结果上，供 _write_cache 落盘（不进 dataclass 公开面）。
        result.__dict__["_contents"] = contents
        return result

    # ------------------------------------------------------------------ #
    # txt：自写解析 + icu_charset 编码探测链
    # ------------------------------------------------------------------ #
    def _parse_txt(self, src: Path, *,
                   title_pattern: str | None) -> tuple[dict, list[tuple[str, ChapterContent]]]:
        raw = src.read_bytes()
        from app.services.engine.icu_charset import detect_charset
        encoding = detect_charset(raw) or "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", errors="replace")
            encoding = "utf-8"

        compiled = re.compile(title_pattern or _DEFAULT_TITLE_PATTERN)
        # 以命中标题正则的行为界切章；未命中任何标题则整篇作为单章。
        boundaries: list[tuple[int, int]] = []  # (标题行索引, 标题文本)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if compiled.match(line.strip()):
                boundaries.append((i, line.strip()))

        chunks: list[tuple[str, list[str]]] = []
        if boundaries:
            for b, (line_idx, title) in enumerate(boundaries):
                end = boundaries[b + 1][0] if b + 1 < len(boundaries) else len(lines)
                body = [ln for ln in lines[line_idx + 1:end] if ln.strip()]
                chunks.append((title, body))
            # 开头到第一个标题之前的前言/简介并入第一章（若有正文）。
            preface = [ln for ln in lines[:boundaries[0][0]] if ln.strip()]
            if preface:
                first_title, first_body = chunks[0]
                chunks[0] = (first_title, preface + first_body)
        else:
            body = [ln.strip() for ln in lines if ln.strip()]
            chunks.append(("正文", body))

        out: list[tuple[str, ChapterContent]] = []
        for title, body in chunks:
            content = "\n".join(body).strip()
            if not content:
                continue
            out.append((title, ChapterContent(
                blocks=[ContentBlock("text", content)],
                title=title, raw_content=content)))
        if not out:  # 空文件兜底为一章空正文，避免下游目录为空
            out.append(("正文", ChapterContent(
                blocks=[ContentBlock("text", "")], title="正文", raw_content="")))
        return {"name": src.stem, "charset": encoding}, out

    # ------------------------------------------------------------------ #
    # epub：ebooklib（懒加载）
    # ------------------------------------------------------------------ #
    def _parse_epub(self, src: Path) -> tuple[dict, list[tuple[str, ChapterContent]]]:
        from ebooklib import ITEM_DOCUMENT, epub  # 懒加载：仅本格式 import
        book = epub.read_epub(str(src))

        def meta(name: str) -> str:
            values = book.get_metadata("DC", name)
            return values[0][0] if values else ""

        # spine 是阅读顺序：[(item_id, linear), ...]，按序取文档条目。
        spine_ids = [item[0] for item in book.spine if isinstance(item, (list, tuple))]
        id_map = {item.id: item for item in book.get_items()}

        # toc 可能给出 (节标题, item) 或嵌套结构，用于给章节命名；取不到则按序命名。
        toc_titles: dict[str, str] = {}
        def _walk_toc(entries):
            for entry in entries or []:
                if isinstance(entry, tuple) and len(entry) >= 2:
                    title, related = entry[0], entry[1]
                    if hasattr(related, "href"):
                        toc_titles[related.href] = str(title)
                    elif isinstance(related, list):
                        _walk_toc(related)
        _walk_toc(book.toc)

        out: list[tuple[str, ChapterContent]] = []
        for index, item_id in enumerate(spine_ids):
            item = id_map.get(item_id)
            if item is None or item.get_type() != ITEM_DOCUMENT:
                continue
            title = toc_titles.get(item.file_name, "") or f"第{index + 1}节"
            blocks = self._epub_html_to_blocks(item.get_content())
            text_value = "\n".join(
                b.value for b in blocks if b.kind == "text" and isinstance(b.value, str))
            if not text_value.strip() and not any(b.kind == "image" for b in blocks):
                continue  # 跳过空白目录页/封面占位
            out.append((title, ChapterContent(
                blocks=blocks, title=title, raw_content=text_value)))
        if not out:  # spine 为空时退化为按文档条目顺序兜底
            for index, item in enumerate(book.get_items_of_type(ITEM_DOCUMENT)):
                blocks = self._epub_html_to_blocks(item.get_content())
                out.append((item.get_name() or f"第{index + 1}节", ChapterContent(
                    blocks=blocks, title=item.get_name() or "",
                    raw_content="\n".join(
                        b.value for b in blocks if b.kind == "text"))))

        return {"name": meta("title") or src.stem,
                "author": meta("creator")}, out

    @staticmethod
    def _epub_html_to_blocks(html_bytes: bytes) -> list[ContentBlock]:
        """epub 正文 HTML → 有序文本块 + 内嵌图片块（字节值，离线可读）。"""
        from bs4 import BeautifulSoup
        blocks: list[ContentBlock] = []
        try:
            soup = BeautifulSoup(html_bytes, "html.parser")
        except Exception:
            return [ContentBlock("text", html_bytes.decode("utf-8", errors="replace"))]
        for img in soup.find_all("img"):
            src_attr = img.get("src") or img.get("xlink:href") or ""
            if src_attr:
                blocks.append(ContentBlock("image", src_attr))
        text = soup.get_text("\n").strip()
        # 折叠多余空行，保持与网络书源文本块一致的段落形态。
        text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        if text:
            blocks.insert(0, ContentBlock("text", text))
        return blocks

    # ------------------------------------------------------------------ #
    # pdf：pymupdf（懒加载）
    # ------------------------------------------------------------------ #
    def _parse_pdf(self, src: Path) -> tuple[dict, list[tuple[str, ChapterContent]]]:
        import pymupdf  # 懒加载：仅本格式 import
        doc = pymupdf.open(str(src))
        try:
            n_pages = doc.page_count
            per = self._pdf_pages_per_chapter
            if not per or per <= 0:
                # 自适应：目标约 30 章，每页一章时（<=30 页）保留逐页粒度。
                per = max(1, min(50, round(n_pages / 30) or 1))
            per = max(1, per)
            title = doc.metadata.get("title") if doc.metadata else ""
            author = doc.metadata.get("author") if doc.metadata else ""

            out: list[tuple[str, ChapterContent]] = []
            start = 0
            chunk = 1
            while start < n_pages:
                end = min(start + per, n_pages)
                parts: list[str] = []
                for pno in range(start, end):
                    parts.append(doc.load_page(pno).get_text("text") or "")
                text = "\n".join(parts).strip()
                if text:
                    label = f"第{chunk}部分（{start + 1}-{end}页）"
                    out.append((label, ChapterContent(
                        blocks=[ContentBlock("text", text)],
                        title=label, raw_content=text)))
                start = end
                chunk += 1
            if not out:
                out.append(("正文", ChapterContent(
                    blocks=[ContentBlock("text", "")], title="正文", raw_content="")))
            return {"name": title or src.stem, "author": author or ""}, out
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    # 归一化落盘：reader-cache + books 表
    # ------------------------------------------------------------------ #
    def _write_cache(self, result: ImportResult) -> None:
        if self._cache is None:  # 未启动（裸用）时临时构造，保证可测
            return
        contents: list[ChapterContent] = result.__dict__.get("_contents", [])
        self._cache.save_catalog(result.source_url, result.book_url, result.chapters)
        for chapter, document in zip(result.chapters, contents):
            self._cache.save_content(
                result.source_url, result.book_url, chapter.url, document)

    async def _shelve(self, result: ImportResult) -> None:
        """把导入的书落进**书架记录列表**。

        ``books`` 表那一行只是进度/元数据，书架 UI 读的是 SourceService 的
        bookshelf（Legado 的 Book 表）——只写前者的话，导完书架上根本看不见这本书。
        走 ``shelve_book`` 而不是 ``add_to_bookshelf``：前者会发 ``SourceChangedEvent``，
        书架页才是"导完立刻在架上"而不是"重开才看见"。
        SourceService 缺席（裸用/单测）时静默跳过：缓存与进度照写，
        阅读器打开时 ``open_book`` 仍会补一次入架。
        """
        service = self._source_service
        if service is None or not hasattr(service, "shelve_book"):
            return
        try:
            await service.shelve_book(result.source, result.book, shelved=True)
        except Exception:
            logger.exception("本地书入架失败（已忽略，阅读时仍会补登记）")

    def _register_books_table(self, result: ImportResult) -> None:
        """在 books 表登记本地书一行（元数据列 + 初始进度列）。"""
        conn = self._storage.db if self._storage is not None else None
        if conn is None:
            return
        now = time.time()
        conn.execute(
            "INSERT INTO books(book_url, source_url, name, author, kind,"
            " cover_url, chapter_index, scroll_offset, scroll_ratio,"
            " chapter_pos, font_size, in_bookshelf, shelf_group, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(book_url) DO UPDATE SET "
            " source_url=excluded.source_url, name=excluded.name,"
            " author=excluded.author, kind=excluded.kind,"
            " cover_url=excluded.cover_url, updated_at=excluded.updated_at",
            (result.book_url, result.source_url, result.book.name,
             result.book.author, result.book.kind, result.book.cover_url,
             0, 0.0, 0.0, 0, 16, 1, "local", now))

    def _publish(self, event) -> None:
        if self.bus is None:
            return
        self.bus.publish(event)
