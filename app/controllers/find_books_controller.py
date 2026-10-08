"""FindBooksController：找书视图的事件订阅与服务编排（§7.4）。

数据链路（重构时曾被留成 M2 占位，现已全部接通）：

- 书源列表：attach 时经 ``SourceService.list_sources`` 拉取，订阅
  :class:`SourceChangedEvent` 增量刷新；
- 分类浏览：``ReadingService.explore_categories/explore_books``，触底翻页；
- 关键词搜索：``ReadingService.search_books``（SearchModel 的分页/generation/
  逐源增量在服务内），本控制器只认最新 ``search_id``，按 ``finished`` 收口；
- 开读：找书页点书是**试读**（Legado notShelf，不落书架），经
  ``on_open_reader(source, book, shelve=False)`` 交 NavigationHost 路由；
- 入架：详情对话框「加入书架」走 ``SourceService.shelve_book``（暂存记录转正的
  ``promote_book`` 由服务提供，界面暂无入口）；
- 书源登录：``RuleEngineService.login_schema/submit_login``（loginUi 动态字段）。

本文件允许 import app.services.* 类型（hub.get() 类型参数与纯函数）。
"""
from __future__ import annotations

import logging

import flet as ft

from app.core.events import SearchFinishedEvent, SourceChangedEvent
from app.controllers.base import BaseController
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.reading.reading_service import ReadingService, as_book_item
from app.services.sources.source_service import SourceService

logger = logging.getLogger(__name__)

_SCOPE_ALL = "__all__"
_SCOPE_UNGROUPED = "__ungrouped__"


class FindBooksController(BaseController):
    """找书：书源列表 / 分类浏览 / 关键词搜索 / 详情对话框。"""

    destination = "reading.find_books"

    def on_attach(self) -> None:
        self._reading = self._safe_service(ReadingService)
        self._source_service = self._safe_service(SourceService)
        self._rule_engine = self._safe_service(RuleEngineService)
        # -- 装配态 --
        self._sources: list = []
        self._scope = _SCOPE_ALL
        self._scope_name = "全部书源"
        self._search_id = 0
        self._search_keyword = ""
        self._search_running = False
        self._current_source = None
        self._login_visible = False
        self._categories: list[dict] = []
        self._category_index = 0
        self._browse_page = 0
        self._browse_loading = False
        #: 当前挂着的详情对话框：连点同一本书不该叠出第二层（关一层还剩一层）
        self._detail_dialog = None
        # 跨视图开读槽：NavigationHost.show 装配时绑定（视图间零互引）
        self.on_open_reader = None       # (source, book: dict, shelve: bool) -> None

        # 视图回调 → 业务方法
        self._view.on_open_search = lambda: self._view.set_discovery_page("search")
        self._view.on_refresh_sources = self.refresh_sources
        self._view.on_open_source = self.open_source
        self._view.on_open_login = self.open_login
        self._view.on_search_submit = self.search
        self._view.on_stop_search = self.stop_search
        self._view.on_open_scope = self.open_scope
        self._view.on_load_more = self.load_more
        self._view.on_category_changed = self._on_category_changed
        self._view.on_show_detail = self.show_detail
        self._view.on_read_book = self.read_book

        # 逐源增量事件高频，0.5s 合并（trailing 保证最后一次收口快照上屏）
        self.subscribe(SearchFinishedEvent,
                       self.throttle(self._on_search_finished, 0.5))
        self.subscribe(SourceChangedEvent, self._on_sources_changed)
        self._view.render_sources(self._sources)
        self._view.set_scope_label(self._scope_name)
        self.ui_call(self._load_sources())

    def on_detach(self) -> None:
        self._browse_loading = False

    # ------------------------------------------------------------------ #
    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 书源列表 / 分类浏览
    # ------------------------------------------------------------------ #
    async def _load_sources(self, report: bool = True) -> None:
        if self._view is None:  # attach 时排的任务可能在切页后才跑
            return
        if self._source_service is None:
            self._view.set_status("SourceService 未就绪")
            return
        try:
            self._sources = list(await self._source_service.list_sources())
        except Exception as exc:
            logger.warning("找书页加载书源失败：%s", exc)
            return
        self._view.render_sources(self._sources)
        if report:
            # 事件驱动的刷新不占状态行：否则"已加入书架"这类反馈会被盖掉
            self._view.set_status(f"共 {len(self._sources)} 个书源")

    async def _on_sources_changed(self, event: SourceChangedEvent) -> None:
        await self._load_sources(report=False)

    def refresh_sources(self, _=None) -> None:
        self.ui_call(self._load_sources())

    def open_source(self, source, _=None) -> None:
        """点开书源：切到浏览页并解析分类。"""
        self._current_source = source
        self._categories = []
        self._category_index = 0
        self._browse_page = 0
        name = getattr(source, "bookSourceName", "") or ""
        self._login_visible = bool(
            str(getattr(source, "loginUrl", "") or "").strip())
        self._view.set_browse_context(source, name,
                                      login_visible=self._login_visible)
        self._view.render_categories([])
        self._view.set_discovery_page("browse")
        self._view.set_status(f"正在解析 {name} 的分类…")
        self.ui_call(self._load_categories(source))

    async def _load_categories(self, source) -> None:
        if self._view is None:  # attach 时排的任务可能在切页后才跑
            return
        if self._reading is None:
            self._view.set_status("ReadingService 未就绪")
            return
        try:
            categories = await self._reading.explore_categories(source)
        except Exception as exc:
            if self._view is None:  # 在途时用户已切页
                return
            self._view.set_status(f"分类解析失败：{exc}")
            return
        if self._view is None or source is not self._current_source:
            return  # 用户已切走，丢弃过期结果
        self._categories = list(categories or [])
        self._view.render_categories([c.get("title", "") for c in self._categories])
        if not self._categories:
            self._view.set_status("该书源未配置分类浏览")
            return
        self._view.set_status(f"共 {len(self._categories)} 个分类")
        await self._open_category(0)

    def _on_category_changed(self, index: int) -> None:
        self.ui_call(self._open_category(int(index)))

    async def _open_category(self, index: int) -> None:
        if self._view is None:
            return
        source = self._current_source
        if source is None or not (0 <= index < len(self._categories)):
            return
        self._category_index = index
        self._view.set_browse_context(
            source, self._categories[index].get("title", ""),
            login_visible=self._login_visible)
        self._view.render_results([], flow="browse")
        try:
            books = await self._reading.explore_books(
                source, self._categories[index], page=1)
        except Exception as exc:
            if self._view is None:  # 在途时用户已切页
                return
            self._view.set_status(f"分类书单加载失败：{exc}")
            return
        if (self._view is None or source is not self._current_source
                or index != self._category_index):
            return
        self._browse_page = 1
        self._view.render_results(list(books or []), flow="browse")
        self._view.set_status(
            f"{self._categories[index].get('title', '')}：{len(books or [])} 本")

    def load_more(self, flow: str = "search") -> None:
        """触底翻页：浏览走 explore 下一页；搜索走同 search_id 再查一页。"""
        if flow == "browse":
            self.ui_call(self._load_more_browse())
        elif flow == "search" and self._search_running:
            self.ui_call(self._reading.search_books(
                self._search_keyword, self._sources,
                scope=self._scope, search_id=self._search_id))

    async def _load_more_browse(self) -> None:
        source = self._current_source
        if (source is None or self._browse_loading
                or not (0 <= self._category_index < len(self._categories))):
            return
        self._browse_loading = True
        try:
            books = await self._reading.explore_books(
                source, self._categories[self._category_index],
                page=self._browse_page + 1)
        except Exception as exc:
            if self._view is not None:
                self._view.set_status(f"下一页加载失败：{exc}")
            return
        finally:
            self._browse_loading = False
        if self._view is None or source is not self._current_source:
            return
        books = list(books or [])
        if not books:
            self._view.set_status("没有更多了")
            return
        self._browse_page += 1
        self._view.render_results(books, flow="browse", append=True)

    # ------------------------------------------------------------------ #
    # 关键词搜索
    # ------------------------------------------------------------------ #
    def search(self, keyword: str, _=None) -> None:
        keyword = str(keyword or "").strip()
        if not keyword:
            return
        if self._reading is None:
            self._view.set_status("ReadingService 未就绪")
            return
        pool = ReadingService.searchable_sources(self._sources, self._scope)
        if not pool:
            self._view.set_status("当前范围内没有启用且配置了搜索规则的书源")
            return
        self._search_id += 1
        self._search_keyword = keyword
        self._search_running = True
        self._view.set_search_running(True, f"正在搜索「{keyword}」（{len(pool)} 源）…")
        self.ui_call(self._reading.search_books(
            keyword, self._sources, scope=self._scope,
            search_id=self._search_id))

    def stop_search(self, _=None) -> None:
        self._search_running = False
        self._view.set_search_running(False, "已停止搜索")
        if self._reading is not None:
            self.ui_call(self._reading.stop_search())

    async def _on_search_finished(self, event: SearchFinishedEvent) -> None:
        # 只认最新一轮（generation 守护：旧 search_id 的迟到快照丢弃）。
        if int(event.search_id or 0) != self._search_id:
            return
        items = list(event.items or [])
        self._view.render_results(items, flow="search")
        if event.finished:
            self._search_running = False
            self._view.set_search_running(
                False, f"搜索完成：{len(items)} 条结果")
        else:
            self._view.set_status(f"已找到 {len(items)} 条结果，继续搜索中…")

    #: 两个哨兵的展示名；分组名的 label 就是组名本身。
    _SCOPE_LABELS = {_SCOPE_ALL: "全部书源", _SCOPE_UNGROUPED: "未分组"}

    def open_scope(self, _=None) -> None:
        """搜索范围选择：全部 / 未分组 / 各书源分组。

        选项表和"各范围内几条源"都取自 :meth:`ReadingService.scope_table`——
        同一份算法也喂协议面的 ``search.scopes``。这里再数一遍就是两个界面两套口径
        （改一条规则要记得改两处的那种）。
        """
        page = self._page()
        if page is None:
            return
        options = [(row["scope"],
                    self._SCOPE_LABELS.get(row["scope"], row["scope"]),
                    row["count"])
                   for row in ReadingService.scope_table(self._sources)]

        def pick(key: str, label: str) -> None:
            self._scope, self._scope_name = key, label
            self._view.set_scope_label(label)
            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True, title=ft.Text("搜索范围"),
            content=ft.Column([
                ft.ListTile(
                    dense=True, leading=ft.Icon(
                        ft.Icons.RADIO_BUTTON_CHECKED if key == self._scope
                        else ft.Icons.RADIO_BUTTON_UNCHECKED),
                    title=ft.Text(f"{label}（{count} 源）"),
                    on_click=lambda e, k=key, l=label: pick(k, l))
                for key, label, count in options
            ], tight=True, width=360))
        page.show_dialog(dialog)

    # ------------------------------------------------------------------ #
    # 详情 / 开读 / 入架
    # ------------------------------------------------------------------ #
    def show_detail(self, book: dict) -> None:
        """弹出详情对话框；简介按渲染契约剥标记后展示。"""
        page = self._page()
        if page is None:
            return
        if self._detail_dialog is not None and self._detail_dialog.open:
            return  # 双击搜索结果：第二层对话框叠在第一层上，关一层还剩一层
        from app.services.engine.rule_engine import display_intro
        book = dict(book or {})
        kind, text = display_intro(book.get("intro", ""))
        book["intro"] = text if kind == "text" else "(该书简介需网页/富文本渲染，开始阅读查看)"
        dialog = self._view.build_detail_dialog(
            book, on_read=self.read_book, on_shelve=self._shelve,
            on_close=lambda: page.pop_dialog())
        self._detail_dialog = dialog
        page.show_dialog(dialog)

    def _shelve(self, book: dict) -> None:
        """找书结果直接入书架（不经试读）。"""
        source = self._source_for(book)
        if source is None or self._source_service is None:
            self._view.set_status(f"未找到该书来源书源，无法入架：{book.get('name', '')}")
            return
        self.ui_call(self._do_shelve(source, book))

    async def _do_shelve(self, source, book: dict) -> None:
        # 找书结果是字典快照，书架按属性读取：进服务前必须还原成书条目
        ok = await self._source_service.shelve_book(
            source, as_book_item(book), shelved=True)
        page = self._page()
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        if self._view is None:  # 在途时用户已切页
            return
        self._view.set_status(
            f"已加入书架：{book.get('name', '')}" if ok
            else f"该书已在书架：{book.get('name', '')}")

    def read_book(self, book: dict) -> None:
        """找书页开读 = 试读（notShelf，不落书架）；要入架走详情框「加入书架」。"""
        page = self._page()
        if page is not None:  # 详情框是模态的：不关掉它会浮在阅读器上
            try:
                page.pop_dialog()
            except Exception:
                pass
        if getattr(self, "on_open_reader", None) is None:
            self._view.set_status(f"打开阅读：{book.get('name', '')}")
            return
        self.on_open_reader(self._source_for(book), dict(book or {}), False)

    def _source_for(self, book: dict):
        """按快照的 ``origin``（书源 URL）找回来源书源对象。"""
        origin = str((book or {}).get("origin") or "").strip()
        if not origin:
            return self._current_source
        for source in self._sources:
            if str(getattr(source, "bookSourceUrl", "") or "") == origin:
                return source
        return self._current_source

    def open_login(self, source, _=None) -> None:
        """书源登录：按源的 loginUi 铺输入框，提交走引擎 submit_login。"""
        source = source if source is not None else self._current_source
        if source is None:
            self._view.set_status("未选择书源，无法登录")
            return
        if self._rule_engine is None:
            self._view.set_status("RuleEngineService 未就绪（并行集成中）")
            return
        if not self._rule_engine.login_available(source):
            self._view.set_status(
                f"该书源未配置登录：{getattr(source, 'bookSourceName', '')}")
            return
        self.ui_call(self._show_login(source))

    async def _show_login(self, source) -> None:
        """读 loginUi 配置并按 Legado 的三种形态分流（对照旧 ``SourceLoginActivity``）。

        ① loginUi 有行 → 表单框：text/password 铺输入框**并回填已存值**，
        ``type:"button"`` 铺按钮、点它执行该行的 action（Legado 的扫码 / 过验证码就
        挂在这儿，以前这些 action 根本没人执行）；
        ② loginUi 空但 loginUrl 是网页 → **不铺空表单**（Legado 这里开 WebView），
        只报地址：桌面端没有"就地内嵌登录"，而且空表单点提交按引擎语义 = 清除登录信息；
        ③ 两者皆无（js 型 loginUrl）→ 直接把登录脚本跑一遍。
        """
        from app.views.reading.login_dialog import LoginDialog
        page = self._page()
        if page is None:
            return
        try:
            schema = await self._rule_engine.login_schema(source)
        except Exception as exc:
            logger.warning("读取书源登录配置失败：%s", exc)
            if self._view is not None:
                self._view.set_status(f"登录配置读取失败：{exc}")
            return
        rows = [self._login_row(item) for item in schema or []
                if isinstance(item, dict) and item.get("name")]
        if not rows:
            target = self._login_target(source)
            if not target:
                await self._login_by_script(source)
                return
            dialog = LoginDialog(source)
            dialog.show_web_login(target)
            dialog.on_clear = lambda: self._clear_login(dialog, source)
            dialog.on_close = lambda: page.pop_dialog()
            page.show_dialog(dialog)
            return
        dialog = LoginDialog(source)
        dialog.set_fields(rows)
        dialog.on_submit = lambda src, values: self._submit_login(dialog, src, values)
        dialog.on_action = lambda src, action, values: self._run_login_action(
            dialog, src, action, values)
        dialog.on_close = lambda: page.pop_dialog()
        page.show_dialog(dialog)

    @staticmethod
    def _login_row(item: dict) -> dict:
        """引擎的 loginUi 行 → 对话框的一行（键名不同源，这里翻一次）。"""
        name = str(item.get("name") or "")
        return {
            "key": name,
            "label": str(item.get("text") or name),
            "hint": str(item.get("hint") or ""),
            "password": str(item.get("type") or "").lower() == "password",
            "value": str(item.get("value") or ""),
            "action": str(item.get("action") or ""),
        }

    def _login_target(self, source) -> str:
        try:
            return str(self._rule_engine.login_target(source) or "")
        except Exception:
            logger.warning("解析书源登录地址失败", exc_info=True)
            return ""

    async def _login_by_script(self, source) -> None:
        """无表单、无网页地址：本源用登录脚本，跑一次就是"点登录"。"""
        try:
            await self._rule_engine.run_login_action(source)
        except Exception as exc:
            self._set_status(f"登录失败：{exc}")
            return
        self._set_status(f"已执行该源的登录脚本："
                         f"{getattr(source, 'bookSourceName', '')}")

    def _set_status(self, text: str) -> None:
        if self._view is not None:
            self._view.set_status(text)

    def _run_login_action(self, dialog, source, action: str,
                          values: dict) -> None:
        self.ui_call(self._do_login_action(dialog, source, action, values))

    async def _do_login_action(self, dialog, source, action: str,
                               values: dict) -> None:
        try:
            result = await self._rule_engine.run_login_action(source, action, values)
        except Exception as exc:
            dialog.set_message(f"该动作执行失败：{exc}", error=True)
            self.flush()
            return
        dialog.set_message("已执行" + (f"：{result}" if result not in (None, "") else ""))
        self.flush()

    def _clear_login(self, dialog, source) -> None:
        self.ui_call(self._do_clear_login(dialog, source))

    async def _do_clear_login(self, dialog, source) -> None:
        """显式的"清除已存登录信息"：引擎侧空 values 就是这个语义，别让它误触。"""
        try:
            await self._rule_engine.submit_login(source, {})
        except Exception as exc:
            dialog.set_message(f"清除失败：{exc}", error=True)
            self.flush()
            return
        page = self._page()
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        self._set_status(f"已清除登录信息：{getattr(source, 'bookSourceName', '')}")

    def _submit_login(self, dialog, source, values: dict) -> None:
        self.ui_call(self._do_login(dialog, source, values))

    async def _do_login(self, dialog, source, values: dict) -> None:
        try:
            await self._rule_engine.submit_login(source, values)
        except Exception as exc:
            dialog.set_message(f"登录失败：{exc}", error=True)
            self.flush()
            return
        page = self._page()
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        self._set_status(f"登录完成：{getattr(source, 'bookSourceName', '')}")

    # ------------------------------------------------------------------ #
    def _page(self):
        try:
            return self._view.page
        except Exception:
            return None
