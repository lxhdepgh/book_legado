"""ExportController：书籍导出编排（M5，§1.12 / 契约 §4）。

导出 epub/txt 经 BookExportService（正文来自 BookCacheService 已缓存章节，不联网），
产物落 StorageService.downloads/。

选书这条链此前是断的：``pick_book`` 读的是 ``self._view._selected_book``，而全仓库
没有任何地方给那个属性赋值，所以永远回落到"请先选择已缓存书籍"。现在改为进页面就
向服务要一次「有已缓存章节的书」清单，填进视图下拉，选中才能导出。

进度走 :class:`ExportProgressEvent`（逐章一拍），结果行只有一支笔：进度不写结果行，
产物路径由 :meth:`_after` 独家落。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.core.events import ExportProgressEvent
except Exception:  # pragma: no cover - 并行线未落盘
    ExportProgressEvent = None  # type: ignore[assignment]

try:
    from app.services.tools.book_export_service import BookExportService
except Exception:  # pragma: no cover - 并行线未落盘
    class BookExportService:  # type: ignore[no-redef]
        pass


class ExportController(BaseController):
    """导出：选书 / epub / txt / 进度与产物路径回写。"""

    destination = "tools.export"

    def on_attach(self) -> None:
        self._svc = self._safe_service(BookExportService)
        self._book_url = ""
        self._books: list[dict] = []
        # 在途标记：进度事件是异步投递的，收尾之后迟到的那一拍不能再把条支起来
        # （否则界面表现是"结果都出来了，进度条还在转"）。
        self._running = False
        if ExportProgressEvent is not None:
            self.subscribe(ExportProgressEvent, self._on_progress)
        self._view.on_pick_book = self.refresh_books
        self._view.on_book_chosen = self.choose_book
        self._view.on_export_epub = lambda: self.export("epub")
        self._view.on_export_txt = lambda: self.export("txt")
        self.refresh_books()

    async def _on_progress(self, event) -> None:
        """逐章进度：只喂视图的进度行，不碰结果行（一条进度都没节流）。

        阶段事件一共就几十拍，节流会把末拍推到收尾之后——与备份/自愈同一族教训。
        """
        if self._view is None or not self._running:
            return
        self._view.set_progress(str(getattr(event, "stage", "") or ""),
                                int(getattr(event, "done", 0) or 0),
                                int(getattr(event, "total", 0) or 0))

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 选书
    # ------------------------------------------------------------------ #
    def refresh_books(self) -> None:
        """向服务要一次可导出清单（只列真缓存到章节的书）。"""
        if self._svc is None:
            self._view.set_result("BookExportService 未就绪")
            return
        self._view.set_result("正在读取已缓存书籍…")
        self.ui_call(self._load_books())

    async def _load_books(self) -> None:
        try:
            books = await self._svc.list_exportable_books()
        except Exception as exc:
            if self._view is not None:
                self._view.set_result(f"读取已缓存书籍失败：{exc}")
            return
        self._books = list(books or [])
        if self._view is None:            # 视图已 detach：在途结果丢弃
            return
        # 选中的那本还在清单里才保留，否则清空让用户重选
        keep = next((str(item["url"]) for item in self._books
                     if item.get("url") == self._book_url), "")
        self._book_url = keep
        self._view.set_books(self._books, keep)
        self._view.set_book(self._title_of(keep))
        self._view.set_result(
            "" if self._books else "没有可导出的书籍：先在下载页整书缓存")

    def choose_book(self, key) -> None:
        self._book_url = str(key or "")
        if self._view is not None:
            self._view.set_book(self._title_of(self._book_url))

    def _title_of(self, url: str) -> str:
        return next((str(item.get("name") or item.get("url"))
                     for item in self._books if item.get("url") == url), "")

    # ------------------------------------------------------------------ #
    def export(self, kind: str) -> None:
        if not self._book_url:
            self._view.set_result("请先选择已缓存书籍")
            return
        if self._svc is None:
            self._view.set_result("BookExportService 未就绪")
            return
        self._running = True
        self._view.set_busy(True, "导出中…")
        future = (self.ui_call(self._svc.export_epub(self._book_url))
                  if kind == "epub"
                  else self.ui_call(self._svc.export_txt(self._book_url)))
        future.add_done_callback(self.on_done(lambda f: self._after(f, kind)))

    def _after(self, future, kind: str) -> None:
        # 先落在途标记：这条回调跑完之前迟到的进度拍就不该再把进度条支起来。
        self._running = False
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            self._view.set_busy(False)
            self._view.clear_progress()
        finally:
            try:
                path = future.result()
            except Exception as exc:
                self._view.set_result(f"导出失败：{exc}")
                return
            self._view.set_result(f"已导出 {kind.upper()} → {path}")
