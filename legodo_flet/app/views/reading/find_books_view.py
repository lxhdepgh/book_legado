"""FindBooksView：找书页（书源列表 / 分类浏览 / 搜索页 / 结果流 / 详情对话框）。

自旧单体 ``ReadingView`` 的 find / discovery / search 职责拆出（§7.2）。
本视图只做 Flet 布局与渲染：书源行、分类 Tab、结果流、详情对话框；搜索/换源/
登录的编排全部在 :class:`~app.controllers.find_books_controller.FindBooksController`。

红线（§7.5）：本文件**不 import 任何 app.services.***。书/源数据以纯 dict /
鸭子类型对象经 render 方法注入，回调槽由 Controller 在 attach 时接业务方法。
"""
from __future__ import annotations

import flet as ft


class FindBooksView(ft.Column):
    """找书页：三块（书源列表 / 分类浏览 / 搜索）叠放，底部状态常驻。"""

    def __init__(self) -> None:
        super().__init__(spacing=0, expand=True)

        # -- 回调槽（Controller.attach 时填充；视图只调用，不含业务逻辑） --
        self.on_open_search = None        # () -> None
        self.on_refresh_sources = None    # () -> None
        self.on_open_source = None        # (source) -> None
        self.on_open_login = None         # (source) -> None
        self.on_search_submit = None      # (keyword: str) -> None
        self.on_stop_search = None        # () -> None
        self.on_open_scope = None         # () -> None
        self.on_load_more = None          # (flow: str) -> None
        self.on_category_changed = None   # (index: int) -> None
        self.on_read_book = None          # (book: dict) -> None
        self.on_show_detail = None        # (book: dict) -> None
        self.on_report_issue = None       # (book: dict) -> None  # 新增：标记问题

        # ------------------------------------------------------------------ #
        # 书源列表页
        # ------------------------------------------------------------------ #
        self._search_box = ft.Container(
            padding=ft.Padding(12, 9, 12, 9), border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=lambda e: self._fire(self.on_open_search),
            content=ft.Row([
                ft.Icon(ft.Icons.SEARCH, size=18,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("搜索书名或作者", expand=True,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=8))
        self._source_list = ft.ListView(expand=True, spacing=2)
        self._sources_page = ft.Column(expand=True, spacing=0, controls=[
            ft.Container(padding=12, content=ft.Column([
                ft.Text("找书", size=28, weight=ft.FontWeight.BOLD),
                self._search_box,
                ft.Row([
                    ft.Text("书源", size=13, weight=ft.FontWeight.W_500),
                    ft.IconButton(ft.Icons.REFRESH, tooltip="刷新书源列表",
                                  on_click=lambda e: self._fire(self.on_refresh_sources)),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ], spacing=8)),
            ft.Divider(height=1),
            self._source_list,
        ])

        # ------------------------------------------------------------------ #
        # 分类浏览页
        # ------------------------------------------------------------------ #
        self._browse_title = ft.Text("", size=16, weight=ft.FontWeight.W_500,
                                     expand=True, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS)
        self._login_btn = ft.TextButton(
            "登录书源", icon=ft.Icons.LOGIN, visible=False,
            on_click=lambda e: self._fire(self.on_open_login, self._current_source))
        self._category_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)
        self._browse_tabs_host = ft.Container(content=self._category_row)
        self._browse_list = ft.ListView(
            expand=True, spacing=4, on_scroll=self._on_browse_scroll,
            scroll_interval=250)
        self._browse_page = ft.Column(expand=True, visible=False, spacing=0, controls=[
            ft.Container(padding=ft.Padding(4, 2, 6, 2), content=ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书源列表",
                              on_click=lambda e: self.set_discovery_page("sources")),
                self._browse_title, self._login_btn,
            ], spacing=4)),
            self._browse_tabs_host,
            ft.Divider(height=1),
            self._browse_list,
        ])

        # ------------------------------------------------------------------ #
        # 关键词搜索页
        # ------------------------------------------------------------------ #
        self._keyword = ft.TextField(
            label="书名或关键词", expand=True,
            on_submit=lambda e: self._fire(self.on_search_submit,
                                           self._keyword.value))
        self._search_btn = ft.Button("搜索", icon=ft.Icons.SEARCH,
                                     on_click=lambda e: self._fire(
                                         self.on_search_submit, self._keyword.value))
        self._search_stop = ft.TextButton(
            "停止", icon=ft.Icons.STOP, visible=False,
            on_click=lambda e: self._fire(self.on_stop_search))
        self._search_progress = ft.ProgressBar(value=0, visible=False)
        self._search_scope_label = ft.Text(
            "全部书源", size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._search_scope_button = ft.Container(
            padding=ft.Padding(12, 7, 12, 7), border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=lambda e: self._fire(self.on_open_scope),
            content=ft.Row([
                ft.Icon(ft.Icons.TUNE, size=16, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("搜索范围", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([self._search_scope_label], expand=True),
                ft.Icon(ft.Icons.EXPAND_MORE, size=16,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self._search_list = ft.ListView(
            expand=True, spacing=4, on_scroll=self._on_search_scroll,
            scroll_interval=250)
        self._search_page = ft.Column(expand=True, visible=False, spacing=0, controls=[
            ft.Container(padding=ft.Padding(4, 2, 8, 2), content=ft.Row([
                ft.IconButton(ft.Icons.ARROW_BACK, tooltip="返回书源列表",
                              on_click=lambda e: self.set_discovery_page("sources")),
                self._keyword, self._search_btn, self._search_stop,
            ], spacing=4)),
            ft.Container(padding=ft.Padding(12, 0, 12, 6),
                         content=self._search_scope_button),
            self._search_progress,
            self._search_list,
        ])

        self._discover = ft.Stack(
            expand=True,
            controls=[self._sources_page, self._browse_page, self._search_page])
        self._status = ft.Text("选择书源浏览分类，或直接搜索书名",
                               color=ft.Colors.GREY_600, size=12)
        self.controls = [
            self._discover,
            ft.Container(padding=ft.Padding(12, 2, 12, 6), content=self._status),
        ]
        self._current_source = None
        self.set_discovery_page("sources")

    # ------------------------------------------------------------------ #
    # 内部小工具（纯渲染，无业务）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _on_browse_scroll(self, event):
        self._fire(self.on_load_more, "browse")

    def _on_search_scroll(self, event):
        self._fire(self.on_load_more, "search")

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_discovery_page(self, name: str) -> None:
        """切换三块子页：sources / browse / search。"""
        self._sources_page.visible = name == "sources"
        self._browse_page.visible = name == "browse"
        self._search_page.visible = name == "search"

    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def render_sources(self, sources) -> None:
        """渲染书源列表行（source 为鸭子类型对象）。"""
        self._source_list.controls.clear()
        for source in sources or []:
            name = getattr(source, "bookSourceName", "") or "(未命名)"
            self._source_list.controls.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.LANGUAGE),
                title=ft.Text(name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(getattr(source, "bookSourceUrl", "") or "",
                                 size=11, color=ft.Colors.GREY_500,
                                 max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                on_click=lambda e, s=source: self._fire(self.on_open_source, s),
            ))

    def render_categories(self, titles: list[str]) -> None:
        """渲染分类选择行（标题列表，点选即回调 on_category_changed）。"""
        self._category_row.controls = [
            ft.Chip(
                label=t or f"分类{i}", selected=(i == 0),
                on_click=lambda e, idx=i: self._fire(self.on_category_changed, idx))
            for i, t in enumerate(titles or [])
        ]
        self._browse_title.value = titles[0] if titles else ""

    def set_browse_context(self, source, title: str, login_visible: bool) -> None:
        self._current_source = source
        self._browse_title.value = title or ""
        self._login_btn.visible = bool(login_visible)

    def render_results(self, books: list[dict], flow: str = "search",
                        append: bool = False) -> None:
        """渲染结果流；append=False 时清空后重绘（对齐旧 _ResultFlow）。"""
        host = self._search_list if flow == "search" else self._browse_list
        if not append:
            host.controls.clear()
        for book in books or []:
            host.controls.append(self._book_row(book))
        if not host.controls:
            host.controls.append(ft.Text("暂无结果", color=ft.Colors.GREY_500))

    def _book_row(self, book: dict) -> ft.Control:
        """单行结果：书名 / 作者 / 来源 + trailing 操作按钮（试读 + 标记问题）。"""
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.BOOK_OUTLINED, size=20),
            title=ft.Text(book.get("name", ""), max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS, expand=True),
            subtitle=ft.Text(
                " · ".join(p for p in (book.get("author", ""),
                                       book.get("origin_name", "")) if p),
                size=11, color=ft.Colors.GREY_500, max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS, expand=True),
            trailing=ft.Row([
                # 试读按钮
                ft.IconButton(
                    ft.Icons.READ_MORE, tooltip="试读", icon_size=18,
                    on_click=lambda e, b=book: self._fire(self.on_read_book, b)),
                # 标记问题按钮
                ft.IconButton(
                    ft.Icons.FLAG_OUTLINED, tooltip="书源有问题", icon_size=18,
                    on_click=lambda e, b=book: self._fire(self.on_report_issue, b)),
            ], spacing=2, tight=True),
            on_click=lambda e, b=book: self._fire(self.on_show_detail, b),
            on_long_press=lambda e, b=book: self._fire(self.on_read_book, b),
        )

    def set_search_running(self, running: bool, status: str = "") -> None:
        self._search_stop.visible = bool(running)
        self._search_progress.visible = bool(running)
        if status:
            self.set_status(status)

    def set_search_progress(self, fraction: float) -> None:
        self._search_progress.value = max(0.0, min(1.0, float(fraction or 0.0)))

    def set_scope_label(self, text: str) -> None:
        self._search_scope_label.value = text or "全部书源"

    # 详情对话框（模态，由 Controller 在 page 上 show_dialog） ---------------- #
    def build_detail_dialog(self, book: dict, on_read=None, on_shelve=None,
                            on_close=None) -> ft.AlertDialog:
        """构造书籍详情对话框；动作为回调槽，Controller 传入业务方法。"""
        actions = [ft.TextButton("关闭", on_click=lambda e: self._fire(on_close))]
        if on_shelve is not None:
            actions.append(ft.TextButton(
                "加入书架", icon=ft.Icons.BOOKMARK_ADD,
                on_click=lambda e, b=book: self._fire(on_shelve, b)))
        if on_read is not None:
            actions.append(ft.Button(
                "开始阅读", icon=ft.Icons.PLAY_ARROW,
                on_click=lambda e, b=book: self._fire(on_read, b)))
        return ft.AlertDialog(
            modal=True,
            title=ft.Text(book.get("name", "书籍详情"), size=18,
                          weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Text(book.get("author", ""), color=ft.Colors.GREY_700),
                ft.Text(book.get("origin_name", ""), size=12,
                        color=ft.Colors.GREY_500),
                ft.Text(book.get("intro", "") or "暂无简介",
                        max_lines=8, selectable=True),
            ], tight=True, width=420),
            actions=actions,
        )

    # 问题反馈对话框（模态，由 Controller 在 page 上 show_dialog） ---------------- #
    def build_issue_dialog(self, book: dict, on_try_repair=None,
                           on_remove_source=None, on_cancel=None) -> ft.AlertDialog:
        """构造书源问题反馈对话框。
        
        Args:
            book: 书籍信息字典（含 origin_url / origin_name）
            on_try_repair: 尝试自愈回调 (book: dict) -> None
            on_remove_source: 移除书源回调 (book: dict) -> None
            on_cancel: 取消回调 () -> None
        """
        source_name = book.get("origin_name", "未知书源")
        book_name = book.get("name", "未知书籍")
        
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("报告书源问题", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Text(f"书源：{source_name}", size=14, weight=ft.FontWeight.W_500),
                ft.Text(f"书籍：{book_name}", size=13, color=ft.Colors.GREY_700),
                ft.Container(height=12),
                ft.Text("请选择处理方式：", size=13, color=ft.Colors.GREY_600),
                ft.Container(height=8),
                ft.Text("• 尝试自愈：自动分析并修复该书源的正文规则", size=12),
                ft.Text("• 移除此源：禁用该搜索书源", size=12),
            ], tight=True, width=420),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._fire(on_cancel)),
                ft.TextButton(
                    "移除此源", icon=ft.Icons.DELETE_OUTLINE,
                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                    on_click=lambda e, b=book: self._fire(on_remove_source, b)),
                ft.Button(
                    "尝试自愈", icon=ft.Icons.AUTO_FIX_HIGH,
                    on_click=lambda e, b=book: self._fire(on_try_repair, b)),
            ],
        )

    # 进度提示对话框（自愈过程中显示） ---------------------------------------- #
    def build_progress_dialog(self, message: str) -> ft.AlertDialog:
        """构造进度提示对话框（不可关闭，等任务完成）。"""
        return ft.AlertDialog(
            modal=True,
            content=ft.Column([
                ft.ProgressRing(width=32, height=32),
                ft.Container(height=12),
                ft.Text(message, text_align=ft.TextAlign.CENTER),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )

    # 自愈结果对话框 ---------------------------------------------------------- #
    def build_repair_result_dialog(self, report: dict, on_apply=None,
                                    on_remove=None, on_cancel=None) -> ft.AlertDialog:
        """构造自愈结果对话框。
        
        Args:
            report: 自愈报告字典（含 has_fix / old_value / new_value / samples / reason）
            on_apply: 应用修复回调 (report: dict) -> None
            on_remove: 移除书源回调 (report: dict) -> None
            on_cancel: 取消回调 () -> None
        """
        has_fix = report.get("has_fix", False)
        
        if not has_fix:
            # 自愈失败：只显示原因 + 移除选项
            reason = report.get("reason", "无法生成修复方案")
            return ft.AlertDialog(
                modal=True,
                title=ft.Text("自愈失败", size=18, weight=ft.FontWeight.BOLD),
                content=ft.Column([
                    ft.Icon(ft.Icons.ERROR_OUTLINE, size=48, color=ft.Colors.ERROR),
                    ft.Container(height=12),
                    ft.Text(reason, max_lines=6, selectable=True),
                    ft.Container(height=8),
                    ft.Text("建议移除此书源或手动调整规则", size=12,
                            color=ft.Colors.GREY_600),
                ], tight=True, width=420,
                   horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                actions=[
                    ft.TextButton("取消", on_click=lambda e: self._fire(on_cancel)),
                    ft.TextButton(
                        "移除此源", icon=ft.Icons.DELETE_OUTLINE,
                        style=ft.ButtonStyle(color=ft.Colors.ERROR),
                        on_click=lambda e, r=report: self._fire(on_remove, r)),
                ],
            )
        
        # 自愈成功：显示原规则 vs 新规则 + 抽样复核
        old_value = report.get("old_value", "")
        new_value = report.get("new_value", "")
        samples = report.get("samples", [])
        sample_text = ""
        if samples:
            parts = [f"{s.get('chapter', '')} ({s.get('chars', 0)}字)" for s in samples[:3]]
            sample_text = "抽样复核：" + "、".join(parts)
        
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("自愈成功", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=48, color=ft.Colors.GREEN),
                ft.Container(height=12),
                ft.Text(sample_text, size=12, color=ft.Colors.GREY_700),
                ft.Container(height=8),
                ft.Text("原规则", size=11, color=ft.Colors.GREY_500),
                ft.Text(old_value or "(空)", size=11, max_lines=3, selectable=True),
                ft.Container(height=8),
                ft.Text("新规则（已优化）", size=11, color=ft.Colors.BLUE),
                ft.Text(new_value or "(空)", size=11, max_lines=3, selectable=True),
            ], tight=True, width=420),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._fire(on_cancel)),
                ft.TextButton(
                    "移除此源", icon=ft.Icons.DELETE_OUTLINE,
                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                    on_click=lambda e, r=report: self._fire(on_remove, r)),
                ft.Button(
                    "应用并重试", icon=ft.Icons.PLAY_ARROW,
                    on_click=lambda e, r=report: self._fire(on_apply, r)),
            ],
        )
