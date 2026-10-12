"""BookExportService：epub / txt 离线导出（M5，契约 §4 / 设计 §1.12）。

- ``export_epub(book_url) -> Path`` / ``export_txt(book_url) -> Path``；
- **正文只来自 BookCacheService 已落盘的章节缓存**（``reader-cache``），导出全程
  不重新联网；未缓存的章节跳过并计入 ``skipped``；
- **章节顺序 / 标题** 取自缓存目录 catalog（与 ReadingService.load_chapters
  同源，离线命中即返回，不触发网络目录重拉）；
- 封面：仅当 bookshelf 记录的 ``cover_url`` 指向**本地已存在文件**时用 Pillow
  打开并内嵌（远端封面不联网抓取）；
- 产物落 :meth:`StorageService.download_dir()`，三端一致，不弹平台保存对话框；
- epub 用 ebooklib 组装（M4 已装），txt 自写 UTF-8。

红线：本模块零 flet import；ebooklib / Pillow 懒加载；跨平台。
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from pathlib import Path

from app.core.events import ExportProgressEvent
from app.core.lifecycle import Service
from app.core.storage import StorageService
from app.services.sources.source_service import SourceService

logger = logging.getLogger(__name__)

# 逐章拍的合并窗口：一本几百章的书逐章发会有几百条，界面吃不下。
# 收尾那一拍用 force 绕过它——末拍必须让人看到，否则进度条停在半路。
EXPORT_PUBLISH_INTERVAL = 0.2


class BookExportService(Service):
    """把一本已缓存的书导出为 epub / txt。"""

    name = "BookExportService"
    dependencies: tuple[type[Service], ...] = (StorageService, SourceService)

    def __init__(self, book_cache=None, reading_service=None, source_service=None,
                 storage=None, cache=None) -> None:
        super().__init__()
        # book_cache / reading_service 为契约声明依赖（测试可注入替身）；
        # 正文与目录实际经 ReadingCache 读盘（与二者同一 reader-cache 存储）。
        self._book_cache = book_cache
        self._reading = reading_service
        self._source = source_service
        self._storage = storage
        self._cache = cache  # ReadingCache，未注入时 on_start 由 storage 建

    async def on_start(self) -> None:
        hub = self.hub
        if self._storage is None and hub is not None:
            from app.core.storage import StorageService
            self._storage = hub.require(StorageService)
        if self._source is None and hub is not None:
            try:
                from app.services.sources.source_service import SourceService
                self._source = hub.require(SourceService)
            except Exception:
                self._source = None
        if self._cache is None and self._storage is not None:
            from app.services.reading_cache import ReadingCache
            # ReadingCache 在给定根下追加 reader-cache，与阅读链路同布局。
            self._cache = ReadingCache(self._storage.paths.cache)
        logger.info("BookExportService 已启动")

    # ------------------------------------------------------------------ #
    # 可选书清单（导出页的下拉框就吃这个）
    # ------------------------------------------------------------------ #
    async def list_exportable_books(self) -> list[dict]:
        """书架上**有已缓存章节**的书：``[{url,name,author,total,cached}, ...]``。

        导出全程不联网，所以只有真缓存到章节的书才可能导成功——把没缓存的列进
        选择框，用户点了只会得到"该书无已缓存章节"。只读目录与缓存计数，不读正文。
        """
        if self._source is None or self._cache is None:
            return []
        items: list[dict] = []
        for record in getattr(self._source, "bookshelf", []) or []:
            url = str(record.get("url") or record.get("bookUrl") or "")
            if not url:
                continue
            source_url = str(record.get("source_url") or record.get("origin") or "")
            try:
                # ReadingCache.cache_counts 的顺序是 (已缓存, 目录可读总数)。
                cached, total = self._cache.cache_counts(source_url, url)
            except Exception:
                logger.warning("读取缓存计数失败：%s", url, exc_info=True)
                continue
            if not cached:
                continue
            items.append({
                "url": url,
                "name": str(record.get("name") or url),
                "author": str(record.get("author") or ""),
                "total": int(total), "cached": int(cached)})
        return items

    # ------------------------------------------------------------------ #
    # 内部：定位书 + 读缓存
    # ------------------------------------------------------------------ #
    def _resolve_book(self, book_url: str) -> tuple[str, dict]:
        """由 book_url 从书架反查 (source_url, 书架记录)。离线、不联网。"""
        book_url = str(book_url or "")
        if self._source is None:
            raise RuntimeError("书源服务未就绪，无法定位书籍")
        for record in getattr(self._source, "bookshelf", []) or []:
            ru = str(record.get("url") or record.get("bookUrl") or "")
            if ru == book_url:
                source_url = str(record.get("source_url") or record.get("origin") or "")
                return source_url, record
        raise RuntimeError(f"书架中未找到该书（未缓存/未入架）: {book_url}")

    def _iter_cached_chapters(self, source_url: str, book_url: str):
        """按目录顺序产出 (ChapterItem, ChapterContent)；未缓存章节跳过。"""
        catalog = self._cache.load_catalog(source_url, book_url) or []
        for item in catalog:
            if getattr(item, "is_volume", False) or not getattr(item, "url", ""):
                continue  # 卷标题 / 空 url 不出正文
            content = self._cache.load_content(source_url, book_url, item.url)
            if content is None:
                continue  # 未缓存章节：导出不联网拉取
            yield item, content

    @staticmethod
    def _safe_name(name: str) -> str:
        """文件名清洗：去掉 Windows/macOS 非法字符。"""
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip()
        return cleaned or "未命名书籍"

    # ------------------------------------------------------------------ #
    # 进度上报
    # ------------------------------------------------------------------ #
    def _publish(self, event) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event)
        except Exception:  # 进度只是好看，不能因为它把导出打断
            logger.warning("导出进度发布失败: %s", getattr(event, "stage", ""),
                           exc_info=True)

    def _expected_chapters(self, source_url: str, book_url: str) -> int:
        """这一本预期要写多少章（进度条的分母）。

        用 ``cache_counts`` 的**已缓存**数，不是目录条数：未缓存的章压根不会被写，
        拿目录数当分母会让进度条永远走不满。读不到就报 0，界面走不定长条。
        """
        try:
            return int(self._cache.cache_counts(source_url, book_url)[0])
        except Exception:
            logger.debug("导出进度分母读取失败：%s", book_url, exc_info=True)
            return 0

    def _report(self, kind: str, stage: str, done: int, total: int) -> None:
        self._publish(ExportProgressEvent(kind=kind, stage=str(stage),
                                          done=int(done), total=int(total)))

    async def _paced(self, kind: str, source_url: str, book_url: str, total: int):
        """逐章产出，顺带按节拍发进度、把循环让出去一拍。

        ``EventBus.publish`` 只把处理器**排进**引擎循环（设计 §5.11），而导出通体是
        同步的磁盘/CPU 工作——不让出循环，攒下的进度处理器会全部堆到导出返回之后才跑，
        界面表现就是"进度条纹丝不动，然后一下子完成"。
        """
        done = 0
        # 从 0 起算 ⇒ 第一章那一拍必发：用户点完到看见反应之间不该隔一个合并窗口。
        state = {"published_at": 0.0}

        def report(force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - state["published_at"] < EXPORT_PUBLISH_INTERVAL:
                return
            state["published_at"] = now
            self._report(kind, f"正在写入第 {done} 章", done, total)

        for item, content in self._iter_cached_chapters(source_url, book_url):
            yield item, content
            done += 1
            report()
            await asyncio.sleep(0)

        if done:
            report(force=True)

    # ------------------------------------------------------------------ #
    # txt 导出
    # ------------------------------------------------------------------ #
    async def export_txt(self, book_url: str) -> Path:
        """导出为 UTF-8 TXT（章节标题 + 纯文本正文），返回产物路径。"""
        source_url, record = self._resolve_book(book_url)
        book_name = str(record.get("name") or "未命名书籍")
        out_path = self._export_dir() / f"{self._safe_name(book_name)}.txt"
        total = self._expected_chapters(source_url, book_url)

        lines: list[str] = [book_name]
        author = str(record.get("author") or "")
        if author:
            lines.append(f"作者：{author}")
        lines.append("")
        cached = 0
        async for item, content in self._paced("txt", source_url, book_url, total):
            title = item.name or content.title or f"第{cached + 1}章"
            lines.append(title)
            lines.append("")
            for block in content.blocks:
                if block.kind == "text" and isinstance(block.value, str):
                    lines.append(block.value)
            lines.append("")
            cached += 1
        if cached == 0:
            raise RuntimeError("该书无已缓存章节，无法导出（请先整书缓存）")
        self._report("txt", "正在写文件", cached, total)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("TXT 导出完成: %s（%d 章）", out_path, cached)
        return out_path

    # ------------------------------------------------------------------ #
    # epub 导出（ebooklib，懒加载）
    # ------------------------------------------------------------------ #
    async def export_epub(self, book_url: str) -> Path:
        """导出为 epub（ebooklib 组装，封面 Pillow 内嵌），返回产物路径。"""
        from ebooklib import epub  # 懒加载（M4 已装）
        from ebooklib.epub import EpubHtml, EpubImage, EpubNcx, EpubNav

        source_url, record = self._resolve_book(book_url)
        book_name = str(record.get("name") or "未命名书籍")
        author = str(record.get("author") or "")
        out_path = self._export_dir() / f"{self._safe_name(book_name)}.epub"
        total = self._expected_chapters(source_url, book_url)
        self._report("epub", "正在准备书籍信息", 0, total)
        total = self._expected_chapters(source_url, book_url)

        eb = epub.EpubBook()
        eb.set_identifier("legado-" + (source_url or "")[:8] + "-" + book_url[:24])
        eb.set_title(book_name)
        if author:
            eb.add_author(author)
        eb.set_language("zh")

        # 封面：仅本地文件用 Pillow 内嵌（远端 URL 不联网）。
        self._embed_cover(eb, record.get("cover_url"))

        toc = []
        spine = ["nav"]
        cached = 0
        async for item, content in self._paced("epub", source_url, book_url, total):
            title = item.name or content.title or f"第{cached + 1}章"
            chap = EpubHtml(
                uid=f"chap{cached}", file_name=f"chap_{cached:04d}.xhtml",
                lang="zh", title=title)
            body_parts = [f"<h1>{html.escape(title)}</h1>"]
            img_index = 0
            for block in content.blocks:
                if block.kind == "text" and isinstance(block.value, str):
                    for para in block.value.split("\n"):
                        para = para.strip()
                        if para:
                            body_parts.append(f"<p>{html.escape(para)}</p>")
                elif block.kind == "image" and isinstance(block.value, (bytes, bytearray)):
                    img_index += 1
                    ext, mime = _sniff_image(bytes(block.value))
                    img_name = f"images/c{cached}_{img_index}.{ext}"
                    eb.add_item(EpubImage(
                        uid=f"img{cached}_{img_index}", file_name=img_name,
                        media_type=mime, content=bytes(block.value)))
                    body_parts.append(f'<img src="{img_name}" alt="" />')
            chap.content = "".join(body_parts)
            eb.add_item(chap)
            toc.append(chap)
            spine.append(chap)
            cached += 1

        if cached == 0:
            raise RuntimeError("该书无已缓存章节，无法导出（请先整书缓存）")

        eb.toc = tuple(toc)
        eb.add_item(EpubNcx())
        eb.add_item(EpubNav())
        eb.spine = spine
        self._report("epub", "正在打包 epub", cached, total)
        # write_epub 是同步压缩，不让出循环这一拍就没人能把上面这句画到界面上。
        await asyncio.sleep(0)
        epub.write_epub(out_path, eb, {})
        logger.info("EPUB 导出完成: %s（%d 章）", out_path, cached)
        return out_path

    # ------------------------------------------------------------------ #
    def _export_dir(self) -> Path:
        if self._storage is not None:
            return self._storage.download_dir()
        # 无 StorageService 兜底：当前目录 downloads/（测试兜底）。
        path = Path("downloads")
        path.mkdir(exist_ok=True)
        return path

    def _embed_cover(self, eb, cover_url_or_path) -> None:
        """本地封面文件用 Pillow 归一后内嵌；远端/缺失则跳过（不联网）。"""
        path = Path(str(cover_url_or_path or ""))
        if not path.is_absolute() or not path.exists() or not path.is_file():
            return
        try:
            from PIL import Image  # 懒加载
            buf = __import__("io").BytesIO()
            with Image.open(path) as im:
                im.convert("RGB").save(buf, format="JPEG", quality=88)
            eb.set_cover("cover.jpg", buf.getvalue())
        except Exception:
            logger.warning("封面内嵌失败（忽略）: %s", path, exc_info=True)


def _sniff_image(data: bytes) -> tuple[str, str]:
    """按文件头嗅探图片格式，返回 (扩展名, mime)。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    return "jpg", "image/jpeg"  # 兜底按 jpeg 处理
