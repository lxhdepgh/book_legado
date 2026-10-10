"""SourceSwitchDialog：换源候选 / 进度 / 确认（§7.2 模态覆盖层）。

自旧单体 ``ReadingView`` 的换源职责（``_show_source_switch`` / ``_run_source_switch`` /
``_render_source_switch_results`` / ``_confirm_source_switch``）拆出。对话框只渲染：
候选列表、扫描进度、停止/确认按钮；扫描编排与写回委托
:class:`~app.controllers.bookshelf_controller.BookshelfController`（经
``ReadingService.switch_source`` / ``apply_source_switch``）。

红线：本文件**不 import app.services.***；候选项以 dict 列表注入。
"""
from __future__ import annotations

import flet as ft


class SourceSwitchDialog(ft.AlertDialog):
    """换源对话框：左侧扫描状态/进度，右侧同名书候选，选定后确认写回。"""

    def __init__(self, record: dict | None = None) -> None:
        super().__init__(modal=True)
        self._record = record or {}

        # -- 回调槽（BookshelfController 注入） --
        self.on_start = None        # (record) -> None
        self.on_cancel = None       # () -> None
        self.on_confirm = None      # (record, source, book) -> None
        self.on_close = None        # () -> None

        self._status = ft.Text("等待开始", size=12, color=ft.Colors.GREY_600)
        self._progress = ft.ProgressBar(value=0, visible=False)
        self._candidates = ft.ListView(
            expand=True, spacing=2, height=320, auto_scroll=False)
        self._start_btn = ft.Button(
            "开始换源", icon=ft.Icons.PLAY_ARROW,
            on_click=lambda e: self._fire(self.on_start, self._record))
        self._cancel_btn = ft.TextButton(
            "停止", icon=ft.Icons.STOP, visible=False,
            on_click=lambda e: self._fire(self.on_cancel))

        self.title = ft.Text(
            f"换源 · {self._record.get('name', '')}",
            size=16, weight=ft.FontWeight.BOLD)
        self.content = ft.Column([
            self._status,
            self._progress,
            ft.Divider(height=1),
            self._candidates,
        ], tight=True, width=480, height=420)
        self.actions = [
            self._start_btn, self._cancel_btn,
            ft.TextButton("关闭", on_click=lambda e: self._fire(self.on_close)),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_running(self, running: bool, status: str = "") -> None:
        self._start_btn.visible = not running
        self._cancel_btn.visible = bool(running)
        self._progress.visible = bool(running)
        if status:
            self._status.value = status

    def set_progress(self, fraction: float) -> None:
        self._progress.value = max(0.0, min(1.0, float(fraction or 0.0)))

    def render_candidates(self, matches: list[dict]) -> None:
        """渲染换源候选；每项含 source_name / book / 相关度。

        命中行点击即确认写回（on_confirm）。
        """
        self._candidates.controls.clear()
        if not matches:
            self._candidates.controls.append(
                ft.Text("暂未找到同名书，换源扫描完成后这里会列出候选",
                        color=ft.Colors.GREY_500, size=12))
            return
        for match in matches:
            source = match.get("source")
            book = match.get("book")
            source_name = (match.get("source_name")
                            or getattr(source, "bookSourceName", "") or "")
            book_name = getattr(book, "name", "") if book is not None else ""
            self._candidates.controls.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.RADIO_BUTTON_UNCHECKED),
                title=ft.Text(book_name or match.get("name", "")),
                subtitle=ft.Text(source_name, size=11,
                                  color=ft.Colors.GREY_500),
                on_click=lambda e, s=source, b=book: self._fire(
                    self.on_confirm, self._record, s, b),
            ))

    def set_status(self, text: str) -> None:
        self._status.value = text or ""
