"""ImageBookService：image 类型书服务能力（M2，契约 §8 / 设计 §1.3）。

M2 只交付服务能力，视图在 M4 接：

- :meth:`chapter_images`：复用 reading_engine 章节管线，从章节内容里解析图片 URL 列表；
- :meth:`fetch_page_image`：走引擎网络栈取单页图（M4 再叠加 BookCacheService 图片缓存）；
- :meth:`apply_color_filter`：Pillow 灰度 / 电子墨水预渲染，跨平台，不依赖平台图像 API。
"""
from __future__ import annotations

import logging

from app.core.lifecycle import Service
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.reading.book_cache_service import BookCacheService

logger = logging.getLogger(__name__)


class ImageBookService(Service):
    """漫画/图片书的取图与色彩预处理能力。"""

    name = "ImageBookService"
    dependencies: tuple[type[Service], ...] = (RuleEngineService, BookCacheService)

    def __init__(self, engine=None, book_cache=None) -> None:
        super().__init__()
        self._engine = engine
        self._book_cache = book_cache
        # M4 阅读视图打开 image 书后回写当前源（chapter_images 缺省取用）。
        self.current_source = None

    async def on_start(self) -> None:
        if self._engine is None and self.hub is not None:
            from app.services.engine.rule_engine_service import RuleEngineService
            self._engine = self.hub.require(RuleEngineService)
        if self._book_cache is None and self.hub is not None:
            from app.services.reading.book_cache_service import BookCacheService
            self._book_cache = self.hub.require(BookCacheService)
        logger.info("ImageBookService 已启动")

    # ------------------------------------------------------------------ #
    async def chapter_images(self, book, chapter, source=None) -> list[str]:
        """从章节管线解析出图片 URL 列表（跳过已内联的 data: 图）。"""
        src = source if source is not None else self.current_source
        document = await self._engine.content_document(src, chapter, "")
        urls: list[str] = []
        for block in getattr(document, "blocks", []):
            if block.kind != "image" or not isinstance(block.value, str):
                continue
            value = block.value.strip()
            if value and not value.startswith("data:"):
                urls.append(value)
        return urls

    async def fetch_page_image(self, source, book, url: str,
                               headers: dict | None = None) -> bytes:
        """经引擎网络栈取单页图，返回字节（失败返回空 bytes）。"""
        payload = await self._engine.fetch_media(
            str(url), source, extra_headers=dict(headers or {}))
        if payload is None:
            return b""
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return bytes(payload)

    # ------------------------------------------------------------------ #
    @staticmethod
    def apply_color_filter(image, mode: str = ""):
        """Pillow 跨平台预渲染：gray 灰度 / ink 电子墨水二值化。

        ``image`` 为 PIL.Image.Image；未知 mode 原样返回。
        """
        if not mode:
            return image
        try:
            from PIL import Image  # noqa: F401  (确认运行环境可用)
        except Exception:  # pragma: no cover - Pillow 缺失时原样返回
            return image
        if mode == "gray":
            return image.convert("L").convert("RGB")
        if mode == "ink":
            gray = image.convert("L")
            return gray.point(lambda p: 0 if p < 128 else 255, mode="1").convert("RGB")
        return image
