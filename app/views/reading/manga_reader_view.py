"""MangaReaderView：漫画（image 类型书）专用阅读器（M4 内容形态域，设计 §1.3）。

纵向无限滚动漫画渲染通道：

- **可滚动容器**：:class:`ft.Column` 自带 ``scroll``，三端一致（纯 Python + Flet，
  无平台图像 API，§1.3 跨平台策略）；
- **图片懒加载分块**：一次只渲染前 :attr:`CHUNK_SIZE` 页，滚动接近底部经
  ``on_request_chunk`` 回调请 Controller 追加下一块（大图不一次性全量进树）；
- **色彩滤镜切换**：原图 / 灰度 / 电子墨水三态；像素级预渲染由
  :class:`~app.services.reading.image_book_service.ImageBookService` 经 Pillow 完成
  （本视图只负责把过滤后的字节 ``ft.Image(src=bytes)`` 换上去，**不做像素计算**）；
- **图片缓存命中提示**：状态栏实时显示 ``缓存命中 hit/total``；
- **章节切换 / 进度位置记忆**：上一章/下一章 + 进度标签；滚动经 ``on_save_scroll``
  回调由 Controller 落 ReadingService 进度模型（§3.9 books 表结构化列）。

红线（§7.5 import-lint）：本文件**不 import 任何 app.services.***，也不 import
PIL——图片 URL/字节、滤镜开关、章节/进度全部由
:class:`~app.controllers.manga_reader_controller.MangaReaderController` 注入。
"""
from __future__ import annotations

import flet as ft


class MangaReaderView(ft.Column):
    """漫画阅读器：顶部滤镜/章节工具栏 + 纵向无限滚动图片流 + 状态栏。"""

    #: 懒加载分块大小：一次渲染多少页图（滚动接近底部再追加下一块）。
    CHUNK_SIZE = 6
    #: 支持的色彩滤镜模式（与 ImageBookService.apply_color_filter 的 mode 对齐）。
    FILTER_MODES = ("none", "gray", "ink")
    _FILTER_LABELS = {"none": "原图", "gray": "灰度", "ink": "电子墨水"}

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller 在 attach 时接线）--
        self.on_request_chunk = None        # () -> None（滚动到底部，请求下一块）
        self.on_change_filter = None        # (mode: str) -> None
        self.on_prev_chapter = None         # () -> None
        self.on_next_chapter = None         # () -> None
        self.on_open_directory = None       # () -> None
        self.on_close_directory = None      # () -> None
        self.on_open_chapter = None         # (chapter: dict) -> None
        self.on_save_scroll = None          # () -> None（Controller 内 0.5s 节流落进度）

        # -- 渲染状态（视图侧只持有，不做业务判定）--
        self._all_urls: list[str] = []       # 本章全部页图 URL（待渲染队列）
        self._page_images: list[ft.Image] = []  # 已渲染页图控件（与 url 按序对齐）
        self._rendered = 0                   # 已渲染页数
        self._filter_mode = "none"

        # ------------------------------------------------------------------ #
        # 顶部工具栏：目录 / 滤镜切换 / 章节导航
        # ------------------------------------------------------------------ #
        self._title = ft.Text(
            "漫画", size=16, weight=ft.FontWeight.BOLD, expand=True)
        self._progress_label = ft.Text(
            "未加载", size=12, text_align=ft.TextAlign.CENTER)
        self._filter_label = ft.Text("滤镜：原图", size=12)

        self._btn_none = ft.TextButton(
            "原图", on_click=lambda e: self._fire(self.on_change_filter, "none"))
        self._btn_gray = ft.TextButton(
            "灰度", on_click=lambda e: self._fire(self.on_change_filter, "gray"))
        self._btn_ink = ft.TextButton(
            "电子墨水", on_click=lambda e: self._fire(self.on_change_filter, "ink"))

        self._prev_btn = ft.IconButton(
            ft.Icons.CHEVRON_LEFT, tooltip="上一章", disabled=True,
            on_click=lambda e: self._fire(self.on_prev_chapter))
        self._next_btn = ft.IconButton(
            ft.Icons.CHEVRON_RIGHT, tooltip="下一章", disabled=True,
            on_click=lambda e: self._fire(self.on_next_chapter))

        self._toolbar = ft.Container(
            padding=ft.Padding(6, 2, 6, 2),
            content=ft.Row([
                ft.IconButton(ft.Icons.MENU_BOOK, tooltip="目录",
                              on_click=lambda e: self._fire(self.on_open_directory)),
                self._prev_btn,
                self._title,
                self._next_btn,
                self._btn_none, self._btn_gray, self._btn_ink,
            ], spacing=2))

        # ------------------------------------------------------------------ #
        # 纵向无限滚动图片流（Flet 可滚动容器，三端一致）
        # ------------------------------------------------------------------ #
        self._page_host = ft.Column(
            spacing=8, expand=True,
            scroll=ft.ScrollMode.AUTO, scroll_interval=200,
            on_scroll=self._on_scroll,
            controls=[
                ft.Row([ft.Text("暂无图片", color=ft.Colors.GREY_500)],
                       alignment=ft.MainAxisAlignment.CENTER),
            ])

        # ------------------------------------------------------------------ #
        # 状态栏：进度 + 缓存命中提示
        # ------------------------------------------------------------------ #
        self._cache_label = ft.Text(
            "缓存命中 0/0", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        self._status_bar = ft.Container(
            padding=ft.Padding(12, 2, 12, 4),
            content=ft.Row([self._progress_label, self._filter_label,
                            self._cache_label],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN))

        # ------------------------------------------------------------------ #
        # 目录覆盖层（章列表，点击交 Controller 换章）
        # ------------------------------------------------------------------ #
        self._directory_status = ft.Text("尚未打开书籍", size=12, expand=True)
        self._directory_list = ft.ListView(expand=True, spacing=2)
        self._directory_page = ft.Container(
            visible=False, bgcolor=ft.Colors.SURFACE, expand=True,
            content=ft.Column(spacing=0, expand=True, controls=[
                ft.Container(
                    padding=ft.Padding(4, 2, 8, 2),
                    content=ft.Row([
                        ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回阅读",
                                      on_click=lambda e: self._fire(
                                          self.on_close_directory)),
                        self._directory_status,
                    ])),
                ft.Divider(height=1),
                self._directory_list,
            ]))

        self._body = ft.Column(
            spacing=0, expand=True,
            controls=[self._toolbar, self._page_host, self._status_bar])
        self.controls = [ft.Stack(expand=True,
                                  controls=[self._body, self._directory_page])]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 目录（Controller 调用）
    # ------------------------------------------------------------------ #
    def render_chapters(self, chapters: list[dict]) -> None:
        """渲染章列表；chapter 含 index/name/url/is_volume（卷标题只作分组）。"""
        self._directory_list.controls.clear()
        items = list(chapters or [])
        for chapter in items:
            if chapter.get("is_volume"):
                self._directory_list.controls.append(
                    ft.Text(chapter.get("name", ""), size=13, italic=True,
                            weight=ft.FontWeight.W_600))
                continue
            self._directory_list.controls.append(ft.ListTile(
                dense=True,
                title=ft.Text(chapter.get("name", ""), size=13),
                on_click=lambda e, c=chapter: self._fire(
                    self.on_open_chapter, c),
            ))
        readable = [c for c in items if not c.get("is_volume")]
        self._directory_status.value = f"共 {len(readable)} 章"

    def show_directory(self, show: bool) -> None:
        self._directory_page.visible = bool(show)

    # ------------------------------------------------------------------ #
    # 图片流渲染（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_pages(self, urls: list[str]) -> None:
        """灌入本章全部页图 URL，先渲染第一块（其余随滚动懒加载）。"""
        self._all_urls = list(urls or [])
        self._page_images.clear()
        self._page_host.controls.clear()
        self._rendered = 0
        self._render_chunk()
        if not self._all_urls:
            self._page_host.controls.append(
                ft.Row([ft.Text("本章暂无图片", color=ft.Colors.GREY_500)],
                       alignment=ft.MainAxisAlignment.CENTER))

    def _render_chunk(self) -> int:
        """渲染下一块页图，返回本次新增页数。"""
        end = min(self._rendered + self.CHUNK_SIZE, len(self._all_urls))
        added = 0
        while self._rendered < end:
            url = self._all_urls[self._rendered]
            image = ft.Image(
                src=url, fit=ft.BoxFit.CONTAIN, gapless_playback=True,
                error_content=ft.Text(f"图片加载失败：{url}", size=11,
                                       color=ft.Colors.GREY_500))
            self._page_images.append(image)
            self._page_host.controls.append(
                ft.Row([image], alignment=ft.MainAxisAlignment.CENTER))
            self._rendered += 1
            added += 1
        return added

    def append_chunk(self) -> int:
        """Controller（节流后）调用：追加渲染下一块页图。"""
        return self._render_chunk()

    @property
    def has_more(self) -> bool:
        """是否还有未渲染页（Controller 决定要不要继续追加）。"""
        return self._rendered < len(self._all_urls)

    @property
    def rendered_count(self) -> int:
        return self._rendered

    @property
    def total_count(self) -> int:
        return len(self._all_urls)

    def set_page_bytes(self, index: int, data: bytes) -> None:
        """把已渲染第 ``index`` 页的图源换成过滤后的字节（Pillow 预渲染结果）。"""
        if 0 <= index < len(self._page_images):
            self._page_images[index].src = data

    # ------------------------------------------------------------------ #
    # 滤镜 / 缓存提示 / 章节导航（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_filter_mode(self, mode: str) -> None:
        """记录滤镜模式并刷新标签（按钮视觉高亮 M5  polish，M4 以标签为准）。"""
        if mode not in self.FILTER_MODES:
            mode = "none"
        self._filter_mode = mode
        self._filter_label.value = f"滤镜：{self._FILTER_LABELS[mode]}"

    @property
    def filter_mode(self) -> str:
        return self._filter_mode

    def set_cache_hint(self, hit: int, total: int) -> None:
        """更新图片缓存命中提示（渲染通道内字节缓存命中率）。"""
        self._cache_label.value = f"缓存命中 {int(hit)}/{int(total)}"

    def set_title(self, text: str) -> None:
        self._title.value = text or "漫画"

    def set_progress(self, text: str) -> None:
        self._progress_label.value = text or ""

    def set_nav_enabled(self, prev: bool, next_: bool) -> None:
        self._prev_btn.disabled = not prev
        self._next_btn.disabled = not next_

    # ------------------------------------------------------------------ #
    # 滚动：接近底部请 Controller 追加下一块；顺带上报滚动位置（节流落进度）
    # ------------------------------------------------------------------ #
    def _on_scroll(self, event) -> None:
        # OnScrollEvent 属性按存在性防御读取（离线/无窗口时也不炸）。
        pixels = float(getattr(event, "pixels", 0.0) or 0.0)
        extent = float(getattr(event, "max_scroll_extent", 0.0) or 0.0)
        self._fire(self.on_save_scroll)
        # 距底部不足 400px 且仍有未渲染页 → 请求下一块。
        if self.has_more and extent > 0 and pixels >= extent - 400:
            self._fire(self.on_request_chunk)
