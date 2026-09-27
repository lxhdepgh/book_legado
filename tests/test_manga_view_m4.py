"""M4 漫画渲染通道（ImageBookService 接阅读域）测试（设计 §1.3 / 契约 §7）。

不依赖真实 Flet 窗口与网络：ft 控件可无窗口实例化，图片 URL 用 stub 注入；
Controller 用 stub hub / 服务替身离线构造。覆盖：

1. :class:`MangaReaderView` 离线构造 + stub 图片 URL 注入；
2. 懒加载分块：set_pages 只渲染首块，append_chunk 逐块追加，has_more 收口；
3. 色彩滤镜函数（Pillow 灰度 / 电子墨水，**真实像素断言**）；
4. Controller attach/detach 订阅退订对称（§7.4 防跨页泄漏）；
5. Controller open_chapter 路径经 ImageBookService.chapter_images 取数并渲页；
6. 非原图滤镜：fetch_page_image 取字节 → Pillow 预渲染 → view 换上字节图源，
   并刷新缓存命中提示；
7. import-lint：``app/views/reading/`` 全部 .py 零 ``app.services.`` 前缀命中（§7.5）。
"""
from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft
from PIL import Image

from app.core.registry import DEST_MANGA_READER, MANGA_DESTINATIONS
from app.services.reading.image_book_service import ImageBookService
from app.services.reading.reading_service import ReadingPosition, ReadingService

from app.views.reading.manga_reader_view import MangaReaderView
from app.controllers.manga_reader_controller import MangaReaderController


# --------------------------------------------------------------------------- #
# 离线替身：不启真实 EngineHub / ServiceHub
# --------------------------------------------------------------------------- #
class _FakeBus:
    """记录订阅的假 EventBus：subscribe 返回可幂等退订句柄。"""

    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

        def unsubscribe():
            if (event_type, handler) in self.subscriptions:
                self.subscriptions.remove((event_type, handler))

        return unsubscribe


class _FakeHub:
    """按类型返回替身服务句柄的假 ServiceHub。"""

    def __init__(self, services: dict):
        self.bus = _FakeBus()
        self._services = services

    def get(self, stype):
        return self._services[stype]


def _png_bytes(rgb=(180, 40, 40), size=(8, 8)) -> bytes:
    """造一张真实 PNG 字节（供 fetch_page_image 替身返回，喂给 Pillow 预渲染）。"""
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


def _stub_services():
    """构造漫画线服务替身（async 方法用 AsyncMock 记录调用）。"""
    reading = SimpleNamespace(
        open_book=AsyncMock(return_value=SimpleNamespace(
            chapters=[], position=None)),
        current_session=SimpleNamespace(chapters=[], position=None),
        save_progress=AsyncMock(),
    )
    images = SimpleNamespace(
        chapter_images=AsyncMock(return_value=[]),
        fetch_page_image=AsyncMock(return_value=_png_bytes()),
        current_source=None,
    )
    return {ReadingService: reading, ImageBookService: images}


# --------------------------------------------------------------------------- #
class MangaViewConstructTests(unittest.TestCase):
    """1. 视图离线构造 + stub URL 注入。"""

    def test_view_construct_offline(self):
        view = MangaReaderView()
        self.assertIsInstance(view, ft.Column)
        # stub 图片 URL 注入（不联网）
        view.set_pages(["https://cdn.example.com/p%d.png" % i for i in range(3)])
        self.assertEqual(view.total_count, 3)
        # 渲染控件是 ft.Image，src 为注入的 stub URL
        self.assertIsInstance(view._page_images[0], ft.Image)
        self.assertEqual(view._page_images[0].src, "https://cdn.example.com/p0.png")


class ChunkLazyLoadTests(unittest.TestCase):
    """2. 懒加载分块：首块 + 追加 + has_more 收口。"""

    def test_first_chunk_then_append(self):
        view = MangaReaderView()
        urls = ["https://x/%d.png" % i for i in range(14)]
        view.set_pages(urls)
        # 14 页：首块只渲染 CHUNK_SIZE=6，不一次性全量进树
        self.assertEqual(view.rendered_count, view.CHUNK_SIZE)
        self.assertEqual(view.rendered_count, 6)
        self.assertTrue(view.has_more)

        added = view.append_chunk()
        self.assertEqual(added, 6)
        self.assertEqual(view.rendered_count, 12)
        self.assertTrue(view.has_more)

        added = view.append_chunk()
        self.assertEqual(added, 2)
        self.assertEqual(view.rendered_count, 14)
        self.assertFalse(view.has_more)
        # 已无更多时再追加不再新增
        self.assertEqual(view.append_chunk(), 0)

    def test_set_page_bytes_swaps_source(self):
        view = MangaReaderView()
        view.set_pages(["https://x/a.png"])
        self.assertEqual(view._page_images[0].src, "https://x/a.png")
        view.set_page_bytes(0, b"\x89PNG\r\n\x1a\nFAKE")
        self.assertEqual(view._page_images[0].src, b"\x89PNG\r\n\x1a\nFAKE")


class ColorFilterPixelTests(unittest.TestCase):
    """3. 滤镜函数：Pillow 灰度 / 电子墨水，真实像素断言。"""

    def test_gray_equal_channels(self):
        # 纯红 (255,0,0) → 灰度后三通道相等（亮度≈0.299*255≈76）
        img = Image.new("RGB", (2, 2), (255, 0, 0))
        gray = ImageBookService.apply_color_filter(img, "gray").convert("RGB")
        r, g, b = gray.getpixel((0, 0))
        self.assertEqual(r, g)
        self.assertEqual(g, b)
        self.assertTrue(70 <= r <= 82, f"灰度亮度越界: {r}")

    def test_ink_is_pure_bw(self):
        # 上半暗红（<128→黑）、下半纯白（≥128→白），电子墨水二值化后只含 0/255
        img = Image.new("RGB", (2, 2))
        img.putpixel((0, 0), (255, 0, 0))    # L≈76 → 黑
        img.putpixel((1, 0), (255, 255, 255))  # L=255 → 白
        img.putpixel((0, 1), (0, 0, 0))      # L=0 → 黑
        img.putpixel((1, 1), (200, 200, 200))  # L≈200 → 白
        ink = ImageBookService.apply_color_filter(img, "ink").convert("RGB")
        for x in range(2):
            for y in range(2):
                p = ink.getpixel((x, y))
                self.assertIn(p, [(0, 0, 0), (255, 255, 255)],
                              f"电子墨水应纯黑白，实际 {p}")

    def test_none_and_unknown_mode_return_original(self):
        img = Image.new("RGB", (2, 2), (1, 2, 3))
        self.assertIs(ImageBookService.apply_color_filter(img, ""), img)
        self.assertIs(ImageBookService.apply_color_filter(img, "weird"), img)


class ControllerLifecycleTests(unittest.TestCase):
    """4. attach/detach 订阅退订对称（§7.4 防跨页泄漏）。"""

    def test_attach_detach_subscribe_unsubscribe_symmetric(self):
        hub = _FakeHub(_stub_services())
        view = MangaReaderView()
        controller = MangaReaderController(hub)
        controller.attach(view)
        self.assertGreater(len(hub.bus.subscriptions), 0, "漫画 Controller 应订阅事件")
        controller.detach()
        self.assertEqual(
            len(hub.bus.subscriptions), 0,
            "detach 后应全部退订")


class ControllerOpenChapterTests(unittest.IsolatedAsyncioTestCase):
    """5. open_chapter 路径经 ImageBookService.chapter_images 取数并渲页。"""

    async def test_open_book_loads_chapter_images(self):
        chapters = [
            SimpleNamespace(index=0, name="第1话", url="c0", is_volume=False),
            SimpleNamespace(index=1, name="第2话", url="c1", is_volume=False),
        ]
        position = ReadingPosition(source_url="s", book_url="b", chapter_index=0)
        session = SimpleNamespace(chapters=chapters, position=position)
        services = _stub_services()
        services[ReadingService].open_book.return_value = session
        services[ReadingService].current_session = session
        services[ImageBookService].chapter_images.return_value = [
            "https://x/p%d.png" % i for i in range(10)]

        hub = _FakeHub(services)
        view = MangaReaderView()
        controller = MangaReaderController(hub).attach(view)

        await controller.open_book(
            SimpleNamespace(bookSourceUrl="s"), SimpleNamespace(url="b", name="漫画"))

        services[ImageBookService].chapter_images.assert_awaited()
        # 10 页只渲首块 6 页
        self.assertEqual(view.rendered_count, 6)
        self.assertEqual(view.total_count, 10)
        self.assertEqual(view._title.value, "第1话")
        # 进度落库经 ReadingService.save_progress（章节级记忆）
        services[ReadingService].save_progress.assert_awaited()
        controller.detach()


class ControllerFilterRerenderTests(unittest.IsolatedAsyncioTestCase):
    """6. 非原图滤镜：取字节 → Pillow 预渲染 → 换图源 + 缓存命中提示。"""

    async def test_gray_filter_prerenders_pages(self):
        services = _stub_services()
        hub = _FakeHub(services)
        view = MangaReaderView()
        controller = MangaReaderController(hub).attach(view)

        urls = ["https://x/p%d.png" % i for i in range(10)]
        view.set_pages(urls)  # 首块 6 页
        controller._page_urls = urls
        controller._filter_mode = "gray"

        await controller._rerender_filtered()

        # 已渲染 6 页都应经 fetch_page_image 取字节
        self.assertEqual(
            services[ImageBookService].fetch_page_image.await_count, 6)
        # 图源被换成 Pillow 预渲染后的 PNG 字节
        src0 = view._page_images[0].src
        self.assertIsInstance(src0, bytes)
        self.assertEqual(src0[:8], b"\x89PNG\r\n\x1a\n")
        # 首次全部未命中缓存 → miss 6
        self.assertIn("0/6", view._cache_label.value)

        # 再渲一遍：命中渲染通道内字节缓存
        await controller._rerender_filtered()
        self.assertIn("6/12", view._cache_label.value)
        controller.detach()


class RegistryWiringTests(unittest.TestCase):
    """目的地常量与工厂可离线构建。"""

    def test_manga_destination_constants(self):
        self.assertEqual(DEST_MANGA_READER, "reading.manga")
        self.assertEqual(MANGA_DESTINATIONS, ("reading.manga",))

    def test_register_manga_destination_builds_view(self):
        from app.core.registry import ViewRegistry
        from app.views.reading import register_destinations
        registry = ViewRegistry()
        hub = _FakeHub(_stub_services())
        register_destinations(registry, hub)
        route = registry.try_get(DEST_MANGA_READER)
        self.assertIsNotNone(route)
        view = route.factory()
        self.assertIsInstance(view, MangaReaderView)


class ImportLintTests(unittest.TestCase):
    """7. §7.5 import-lint 守门：views/reading 零 app.services. 命中。"""

    _ROOT = Path(__file__).resolve().parent.parent / "app" / "views" / "reading"

    def test_no_services_import_in_reading_views(self):
        offenders = []
        for py in sorted(self._ROOT.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for mod in names:
                    if mod and (mod == "app.services"
                                 or mod.startswith("app.services.")):
                        offenders.append(f"{py.name}:{node.lineno}: import {mod}")
        self.assertEqual(
            offenders, [],
            "views/reading/ 的 import 语句不得引用 app.services.*（红线 §7.5）：\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
