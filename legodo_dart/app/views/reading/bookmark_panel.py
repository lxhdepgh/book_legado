"""BookmarkPanel：书签列表 / 跳转（§7.2 侧栏面板，接 BookmarkService）。

自旧单体 ``ReadingView`` 中未独立成型的书签能力（M2 新增，§1.13）落成独立面板。
面板只渲染书签行与空态；CRUD 由 :class:`~app.controllers.reader_controller.ReaderController`
经 ``BookmarkService.add/remove/list`` 完成，并订阅 ``BookmarkChangedEvent`` 自刷新。

红线：本文件**不 import app.services.***；书签以 dict 列表注入。
"""
from __future__ import annotations

import flet as ft


class BookmarkPanel(ft.Column):
    """书签面板：标题栏 + 列表 + 空态；跳转/删除回调由 Controller 注入。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽 --
        self.on_jump = None      # (bookmark: dict) -> None
        self.on_remove = None    # (bookmark_id: int) -> None
        self.on_add = None        # () -> None
        self.on_close = None      # () -> None

        self._list = ft.ListView(expand=True, spacing=2, auto_scroll=False)
        self.controls = [
            ft.Container(
                padding=ft.Padding(12, 8, 12, 8),
                content=ft.Row([
                    ft.Icon(ft.Icons.BOOKMARK_OUTLINED),
                    ft.Text("书签", size=16, weight=ft.FontWeight.W_500, expand=True),
                    ft.IconButton(ft.Icons.ADD, tooltip="添加当前位置书签",
                                  on_click=lambda e: self._fire(self.on_add)),
                    ft.IconButton(ft.Icons.CLOSE, tooltip="关闭",
                                  on_click=lambda e: self._fire(self.on_close)),
                ])),
            ft.Divider(height=1),
            self._list,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def render_bookmarks(self, bookmarks: list[dict]) -> None:
        """渲染书签；空列表时显示空态（契约 §4 测试锚点）。"""
        self._list.controls.clear()
        if not bookmarks:
            self._list.controls.append(ft.Text("暂无书签", size=12,
                                              color=ft.Colors.GREY_500))
            return
        for mark in bookmarks:
            self._list.controls.append(ft.ListTile(
                dense=True,
                leading=ft.Icon(ft.Icons.BOOKMARK, size=18),
                title=ft.Text(mark.get("summary", "") or
                              f"第 {mark.get('chapter_index', 0) + 1} 章",
                              size=13, max_lines=2,
                              overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(
                    f"章节 {mark.get('chapter_index', 0) + 1} · "
                    f"{int(float(mark.get('offset', 0)))}%",
                    size=11, color=ft.Colors.GREY_500),
                on_click=lambda e, m=mark: self._fire(self.on_jump, m),
                on_long_press=lambda e, m=mark: self._fire(
                    self.on_remove, m.get("bookmark_id", 0)),
            ))
