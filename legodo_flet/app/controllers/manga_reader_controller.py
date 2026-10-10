"""MangaReaderController：漫画阅读器的事件订阅与服务编排（§7.4 / §1.3）。

数据流（契约 §7）：``MangaReaderView → 本 Controller → ImageBookService``：

- 章节图片列表经 ``hub.get(ImageBookService).chapter_images(source, chapter)`` 解析；
- 单页图经 ``fetch_page_image`` 走引擎网络栈取回字节；
- 色彩滤镜（灰度/电子墨水）复用 M2 静态方法
  :meth:`ImageBookService.apply_color_filter`（Pillow 预渲染，跨平台），控制器把
  过滤后字节经 ``view.set_page_bytes`` 换上图源；
- 章节列表与进度位置经 ``hub.get(ReadingService)`` 复用阅读域进度模型
  （§3.9 books 表结构化列）。

懒加载分块在 Controller 层 0.5s 节流（§7.4：高频滚动合并，不在服务层节流）。
本文件允许 import ``app.services.reading.*``（仅作 ``hub.get()`` 的类型参数与编排）。
"""
from __future__ import annotations

import io
import logging

from app.controllers.base import BaseController
from app.core.events import ReadingSessionChangedEvent
from app.services.reading.image_book_service import ImageBookService
from app.services.reading.reading_service import ReadingService

logger = logging.getLogger(__name__)


class MangaReaderController(BaseController):
    """漫画阅读器：纵向滚动分块懒加载 / 色彩滤镜 / 缓存命中提示 / 章节与进度。"""

    destination = "reading.manga"

    def on_attach(self) -> None:
        self._images = self.service(ImageBookService)
        self._reading = self.service(ReadingService)

        # 会话状态
        self._chapters: list[dict] = []
        self._chapter_index = -1
        self._source = None
        self._book = None
        self._book_url = ""
        self._page_urls: list[str] = []
        # 渲染通道内字节缓存（同一 URL 跨分块 / 重切滤镜复用）
        self._page_cache: dict[str, bytes] = {}
        self._cache_hit = 0
        self._cache_miss = 0
        self._filter_mode = "none"

        # 视图回调 → 业务方法
        self._view.on_request_chunk = self.request_chunk
        self._view.on_change_filter = self.change_filter
        self._view.on_prev_chapter = self.prev_chapter
        self._view.on_next_chapter = self.next_chapter
        self._view.on_open_directory = lambda *a: self._view.show_directory(True)
        self._view.on_close_directory = lambda *a: self._view.show_directory(False)
        self._view.on_open_chapter = self.open_chapter_by_url
        self._view.on_save_scroll = self._throttled_save_position

        # 高频滚动：分块追加 / 进度落库均 0.5s 节流（§7.4）
        self._throttled_request_chunk = self.throttle(self._do_request_chunk, 0.5)
        self._save_position = self.throttle(self._persist_position, 0.5)

        # 事件订阅：阅读会话切换（换书/续读）后同步当前 book_url；detach 统一退订。
        self.subscribe(ReadingSessionChangedEvent, self._on_session_changed)

    # ------------------------------------------------------------------ #
    # 打开 / 章节
    # ------------------------------------------------------------------ #
    async def open_book(self, source, book, shelve: bool = True) -> None:
        """打开 image 书：ReadingService 开会话恢复章节进度，再渲染首章页图。"""
        from app.services.reading.reading_service import as_book_item
        record = as_book_item(book)
        session = await self._reading.open_book(source, book, shelve=shelve)
        if self._view is None:
            return    # 开书在途切了页：这一页已经没了，别再去碰控件
        self._source = getattr(self._reading, "current_source", None) or source
        self._book = record
        self._book_url = getattr(session, "book_url", "") or ""
        # 漫画章节解析缺省取用当前源（M2：chapter_images(source=None) 时回退）。
        self._images.current_source = self._source

        chapters = getattr(session, "chapters", []) or []
        self._chapters = [
            {"index": int(getattr(c, "index", i) or i),
             "name": getattr(c, "name", "") or "",
             "url": getattr(c, "url", "") or "",
             "is_volume": bool(getattr(c, "is_volume", False))}
            for i, c in enumerate(chapters)
        ]
        self._view.set_title(getattr(record, "name", "") or "漫画")
        self._view.render_chapters(self._chapters)

        pos = getattr(session, "position", None)
        start = int(getattr(pos, "chapter_index", -1) or -1) if pos else -1
        readable = [c for c in self._chapters if not c["is_volume"]]
        if start < 0 or start >= len(readable):
            start = 0
        await self.open_chapter(start)

    async def open_chapter(self, index: int) -> None:
        """加载某章图片 URL 列表并渲染首块；非原图滤镜立即预渲染已渲染页。"""
        readable = [c for c in self._chapters if not c["is_volume"]]
        if not readable:
            return
        index = max(0, min(index, len(readable) - 1))
        chapter = readable[index]
        raw = self._chapter_by_url(chapter["url"])

        urls = await self._images.chapter_images(
            self._book, raw, source=self._source)
        self._chapter_index = index
        self._page_urls = list(urls or [])
        self._page_cache.clear()
        self._cache_hit = 0
        self._cache_miss = 0
        if self._view is None:
            return    # 图链解析要联网，走开这一章的路上切页是常规操作

        self._view.set_pages(self._page_urls)
        self._view.set_title(chapter["name"])
        self._view.set_progress(f"{index + 1}/{len(readable)}")
        self._view.set_nav_enabled(
            prev=index > 0, next_=index < len(readable) - 1)
        self._view.set_cache_hint(0, 0)

        if self._filter_mode != "none":
            await self._rerender_filtered()
        await self._persist_position()

    def _chapter_by_url(self, url: str):
        session = getattr(self._reading, "current_session", None)
        for c in getattr(session, "chapters", []) or []:
            if getattr(c, "url", "") == url:
                return c
        return None

    def open_chapter_by_url(self, url: str, _=None) -> None:
        readable = [c for c in self._chapters if not c["is_volume"]]
        for i, c in enumerate(readable):
            if c["url"] == url:
                self.ui_call(self.open_chapter(i))
                return

    def prev_chapter(self, _=None) -> None:
        if self._chapter_index > 0:
            self.ui_call(self.open_chapter(self._chapter_index - 1))

    def next_chapter(self, _=None) -> None:
        readable = [c for c in self._chapters if not c["is_volume"]]
        if self._chapter_index < len(readable) - 1:
            self.ui_call(self.open_chapter(self._chapter_index + 1))

    # ------------------------------------------------------------------ #
    # 懒加载分块（Controller 节流）
    # ------------------------------------------------------------------ #
    def request_chunk(self, _=None) -> None:
        # _throttled_request_chunk 是 async wrapper（节流 async _do_request_chunk）：
        # 必须经 EngineHub 派发，直接调用只产生未 await 的协程（分块永不加载）。
        self.ui_call(self._throttled_request_chunk())

    async def _do_request_chunk(self) -> None:
        if not getattr(self._view, "has_more", False):
            return
        added = self._view.append_chunk()
        if added <= 0 or self._filter_mode == "none":
            return
        # 非原图滤镜：对新追加的页补 Pillow 预渲染并换图源。
        start = self._view.rendered_count - added
        for j in range(start, self._view.rendered_count):
            data = await self._load_page_bytes(self._page_urls[j])
            if self._view is None:
                return    # 逐页预渲染是长任务，中途切页就到此为止
            self._view.set_page_bytes(j, data)

    # ------------------------------------------------------------------ #
    # 色彩滤镜切换（Pillow 预渲染，跨平台）
    # ------------------------------------------------------------------ #
    def change_filter(self, mode: str, _=None) -> None:
        self._filter_mode = mode if mode in ("none", "gray", "ink") else "none"
        self._view.set_filter_mode(self._filter_mode)
        # 换滤镜后旧预渲染字节失效，清缓存重算。
        self._page_cache.clear()
        self._cache_hit = 0
        self._cache_miss = 0
        self._view.set_cache_hint(0, 0)
        if self._filter_mode != "none":
            self.ui_call(self._rerender_filtered())

    async def _rerender_filtered(self) -> None:
        if self._view is None:
            return
        count = self._view.rendered_count
        for i in range(count):
            url = self._page_urls[i] if i < len(self._page_urls) else ""
            if not url:
                continue
            data = await self._load_page_bytes(url)
            if self._view is None:
                return    # 整章重算要几十张图，中途切页不能再往回画
            self._view.set_page_bytes(i, data)

    async def _load_page_bytes(self, url: str) -> bytes:
        """取单页图字节并经 Pillow 滤镜预渲染；命中渲染通道内缓存则直接复用。"""
        cached = self._page_cache.get(url)
        if cached is not None:
            self._cache_hit += 1
        else:
            self._cache_miss += 1
            data = await self._images.fetch_page_image(
                self._source, self._book, url)
            data = self._prerender(data or b"")
            self._page_cache[url] = data
        # 命中提示只是状态栏一行字，页面随时可能已经走了
        if self._view is not None:
            self._view.set_cache_hint(
                self._cache_hit, self._cache_hit + self._cache_miss)
        return self._page_cache[url]

    def _prerender(self, data: bytes) -> bytes:
        """原图字节 → Pillow 打开 → apply_color_filter → PNG 字节（跨平台）。

        预渲染失败（图损坏 / Pillow 缺）时回退原图字节，不阻断阅读。
        """
        if not data or self._filter_mode == "none":
            return data
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            out = ImageBookService.apply_color_filter(img, self._filter_mode)
            buf = io.BytesIO()
            out.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:  # 预渲染降级：返回原图字节
            logger.warning("漫画页预渲染失败（回退原图）：%s", exc)
            return data

    # ------------------------------------------------------------------ #
    # 进度位置记忆（经 ReadingService 进度模型，0.5s 节流落库）
    # ------------------------------------------------------------------ #
    def _throttled_save_position(self, *_args) -> None:
        # async wrapper 经 EngineHub 派发，否则进度永不落库。
        self.ui_call(self._save_position())

    async def _persist_position(self) -> None:
        session = getattr(self._reading, "current_session", None)
        if session is None:
            return
        position = getattr(session, "position", None)
        if position is None:
            return
        await self._reading.save_progress(self._chapter_index, position)

    # ------------------------------------------------------------------ #
    async def _on_session_changed(self, event: ReadingSessionChangedEvent) -> None:
        """阅读会话切换（换书/续读）后同步当前 book_url。"""
        self._book_url = getattr(event, "book_url", "") or ""
