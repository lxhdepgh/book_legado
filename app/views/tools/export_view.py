"""ExportView：书籍导出（M5，§1.12 / 契约 §4 / §13）。

从已缓存章节导出 epub / txt，产物落 StorageService.downloads/。**零
app.services.* import**：导出动作经 Controller 调 BookExportService。
"""
from __future__ import annotations

import flet as ft


class ExportView(ft.Column):
    """导出：可选书籍 + epub/txt 导出按钮 + 进度与产物路径。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_pick_book = None       # () -> None  刷新可导出清单
        self.on_book_chosen = None     # (key) -> None
        self.on_export_epub = None     # () -> None
        self.on_export_txt = None      # () -> None

        self._title = ft.Text("导出书籍", size=20, weight=ft.FontWeight.BOLD)
        self._book = ft.Text("未选择书籍", size=13, color=ft.Colors.GREY_700)
        # 下拉选书，不是"点一下弹个什么都不知道"的按钮：清单来自
        # BookExportService.list_exportable_books()（只列真缓存到章节的书）。
        self._chooser = ft.Dropdown(
            label="已缓存书籍", width=320, dense=True,
            options=[], on_select=lambda e: self._fire(self.on_book_chosen,
                                                       getattr(e, "value", None)))
        self._pick_btn = ft.OutlinedButton(
            "刷新", icon=ft.Icons.REFRESH,
            on_click=lambda e: self._fire(self.on_pick_book))
        self._epub_btn = ft.FilledButton(
            "导出 EPUB", icon=ft.Icons.PICTURE_AS_PDF, disabled=True,
            on_click=lambda e: self._fire(self.on_export_epub))
        self._txt_btn = ft.FilledButton(
            "导出 TXT", icon=ft.Icons.TEXT_SNIPPET, disabled=True,
            on_click=lambda e: self._fire(self.on_export_txt))
        self._progress = ft.ProgressBar(visible=False, bar_height=6,
                                        border_radius=3)
        self._stage = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._log = ft.Text("", size=12, color=ft.Colors.GREY_700, selectable=True)

        self.controls = [
            self._title,
            ft.Row([self._chooser, self._pick_btn, self._book], spacing=8),
            ft.Row([self._epub_btn, self._txt_btn], spacing=8),
            self._progress,
            self._stage,
            self._log,
        ]

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def set_books(self, books, selected: str = "") -> None:
        """填入可导出清单（``[{url,name,cached,total}]``）并回显选中项。"""
        self._chooser.options = [
            ft.dropdown.Option(
                str(item.get("url") or ""),
                f"{item.get('name') or item.get('url')} "
                f"（已缓存 {item.get('cached', 0)}/{item.get('total', 0)} 章）")
            for item in books or []]
        self._chooser.value = selected if selected and any(
            o.key == selected for o in self._chooser.options) else None
        self._chooser.disabled = not self._chooser.options
        if not books:
            self.set_book("")

    def set_book(self, title: str) -> None:
        self._book.value = title or "未选择书籍"
        can = bool(title) and title != "未选择书籍"
        self._epub_btn.disabled = not can
        self._txt_btn.disabled = not can

    def set_busy(self, busy: bool, text: str = "") -> None:
        self._progress.visible = bool(busy)
        # 导出结束后要回到"选中没有书就不能点"，而不是停在 busy 留下的禁用态。
        chosen = bool(self._book.value) and self._book.value != "未选择书籍"
        self._epub_btn.disabled = bool(busy) or not chosen
        self._txt_btn.disabled = bool(busy) or not chosen
        if text:
            self._log.value = text

    def set_progress(self, stage: str, done: int = 0, total: int = 0) -> None:
        """逐章进度：有分母走定长条，没分母走不定长条（一句话都别省）。

        进度只由控制器从 ``ExportProgressEvent`` 喂进来（设计 §5.11），
        视图不主动问服务。
        """
        self._stage.value = str(stage or "")
        self._progress.visible = True
        try:
            ratio = float(done) / float(total) if total else None
        except (TypeError, ValueError):
            ratio = None
        self._progress.value = (max(0.0, min(1.0, ratio))
                                if ratio is not None else None)

    def clear_progress(self) -> None:
        self._progress.visible = False
        self._progress.value = None
        self._stage.value = ""

    def set_result(self, text: str) -> None:
        self._log.value = text or ""
