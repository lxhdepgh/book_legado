"""RssController：订阅页的数据装配与动作编排（阅读组第三个页签）。

链路：视图槽位 → :class:`RssService`（源 CRUD / 刷新 / 文章已读收藏），
订阅 :class:`RssRefreshedEvent`（单源刷新完成，含 refresh_all 逐源）与
:class:`RssRefreshProgressEvent`（批量刷新进度）驱动局部重绘。

三条不那么显然的约束：

- **正文是 HTML**：库里存原文（刷新会覆盖回原文，见服务 `_upsert_articles`），
  显示前要去掉标签。去标签那把尺 :func:`plain_text` **在服务侧**（第九十六轮 G4-B
  从本文件搬进 ``rss_service``）：跨进程的 Dart 客户端读的是同一个函数，
  两边各剥一次迟早给出两种字数。视图不认 HTML，也不 import 服务。
- **已读要点开才算**：``mark_read`` 只在用户打开文章时调用，且随后同步本地那一份
  dict，否则中栏的"未读"字重要到下一次刷新才变。
- **失败要出声**：抓取失败只在服务 status 里，用户看不到；每条动作都以状态行回报，
  绝不让 ui_call 的 future 静默炸掉。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController
from app.core.events import RssRefreshedEvent, RssRefreshProgressEvent
from app.services.content.rss_service import RssService, plain_text

logger = logging.getLogger(__name__)


class RssController(BaseController):
    """订阅源：源列表 / 文章列表 / 正文，以及增删刷新与已读收藏。"""

    destination = "reading.rss"

    def on_attach(self) -> None:
        self._rss = self._safe_service(RssService)
        self._sources: list[dict] = []
        self._counts: dict[str, dict] = {}
        self._articles: list[dict] = []
        self._selected: str = ""
        self._unread_only: bool = False
        self._current: dict | None = None   # 正文栏正在显示的文章

        self._view.on_request_add = self.open_add_dialog
        self._view.on_add_source = self.add_source
        self._view.on_refresh_all = self.refresh_all
        self._view.on_select_source = self.select_source
        self._view.on_refresh_source = self.refresh_source
        self._view.on_remove_source = self.request_remove
        self._view.on_filter_unread = self.set_unread_only
        self._view.on_select_article = self.open_article
        self._view.on_toggle_star = self.toggle_star

        self.subscribe(RssRefreshedEvent, self._on_refreshed)
        self.subscribe(RssRefreshProgressEvent, self._on_progress)
        self._load_sources()

    def on_detach(self) -> None:
        # 订阅由基类统一退订；这里只放视图侧句柄
        self._current = None

    # ------------------------------------------------------------------ #
    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    def _status(self, text: str) -> None:
        if self._view is not None:
            self._view.set_status(text)

    def _page(self):
        if self._view is None:
            return None
        try:
            return self._view.page
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 加载与渲染
    # ------------------------------------------------------------------ #
    def _load_sources(self, keep: bool = True) -> None:
        self.ui_call(self._reload_sources(keep))

    async def _reload_sources(self, keep: bool) -> None:
        if self._view is None:
            return
        if self._rss is None:
            self._status("RssService 未就绪，订阅页无法读写")
            self._view.render_sources([], {}, "")
            return
        try:
            self._sources = list(await self._rss.list_sources())
            self._counts = dict(await self._rss.article_counts())
        except Exception as exc:
            logger.warning("订阅源列表加载失败：%s", exc)
            self._status(f"订阅源列表加载失败：{exc}")
            return
        if self._view is None:
            return
        # 选中项：保住原来源（keep），源被删了就退回首项
        ids = [str(s.get("id") or "") for s in self._sources]
        if not keep or self._selected not in ids:
            self._selected = ids[0] if ids else ""
        self._view.render_sources(self._sources, self._counts, self._selected)
        if self._selected:
            await self._reload_articles()
        else:
            self._articles = []
            self._view.render_articles([], None)
            self._view.clear_detail("还没有订阅源")

    async def _reload_articles(self) -> None:
        if self._rss is None or not self._selected:
            self._articles = []
        else:
            try:
                self._articles = list(await self._rss.list_articles(
                    self._selected, unread_only=self._unread_only))
            except Exception as exc:
                logger.warning("文章列表加载失败：%s", exc)
                self._status(f"文章列表加载失败：{exc}")
                return
        if self._view is None:
            return
        current_id = (self._current or {}).get("id")
        self._view.render_articles(self._articles, current_id)

    def _render_counts(self) -> None:
        """只重画左栏角标（文章数变了但顺序没变时用）。"""
        if self._view is not None:
            self._view.render_sources(self._sources, self._counts, self._selected)

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def open_add_dialog(self) -> None:
        if self._view is None:
            return
        page = self._page()
        dialog = self._view.build_add_dialog()
        if page is None:      # 离线/未挂载：没弹层可挂，直接说明
            self._status("页面未就绪，暂时弹不出添加框")
            return
        page.show_dialog(dialog)

    def add_source(self, name: str, url: str) -> None:
        self.ui_call(self._add_source(name, url))

    async def _add_source(self, name: str, url: str) -> None:
        if self._rss is None:
            self._status("RssService 未就绪")
            return
        try:
            source_id = await self._rss.add_source(name, url)
        except Exception as exc:
            self._status(f"添加失败：{exc}")
            return
        self._status(f"已添加「{name or source_id}」，正在抓取…")
        await self._reload_sources(True)
        await self._refresh_one(source_id, report=False)

    def select_source(self, source_id: str) -> None:
        if self._view is None:
            return
        self._selected = str(source_id or "")
        self._current = None
        self._render_counts()
        self._view.clear_detail("选一篇文章看正文")
        self.ui_call(self._reload_articles())

    def set_unread_only(self, only: bool) -> None:
        self._unread_only = bool(only)
        self.ui_call(self._reload_articles())

    def refresh_all(self) -> None:
        if self._rss is None:
            self._status("RssService 未就绪")
            return
        self._status("正在刷新全部订阅源…")
        self.ui_call(self._run_refresh_all())

    async def _run_refresh_all(self) -> None:
        try:
            new = await self._rss.refresh_all()
        except Exception as exc:
            self._status(f"全部刷新失败：{exc}")
            if self._view is not None:
                self._view.set_progress(0, 0)
            return
        self._status(f"刷新完成，新增 {new} 篇")
        await self._after_refresh_done()

    def refresh_source(self, source_id: str) -> None:
        self.ui_call(self._refresh_one(source_id))

    async def _refresh_one(self, source_id: str, report: bool = True) -> None:
        if self._rss is None or not source_id:
            return
        try:
            new = await self._rss.refresh_source(source_id)
        except Exception as exc:
            # 服务把失败原因记在 status.last_error 并上抛；界面只报这一句
            self._status(f"刷新失败：{exc}")
            return
        if report:
            name = self._name_of(source_id)
            self._status(f"「{name}」刷新完成，新增 {new} 篇")
        if str(source_id) == self._selected:
            await self._reload_sources(True)

    def request_remove(self, source: dict) -> None:
        """删除要先确认：连文章一起删，且不可撤销。"""
        if self._view is None:
            return
        page = self._page()
        dialog = self._view.build_confirm_remove(source, self._remove_source)
        if page is None:
            self._status("页面未就绪，暂时弹不出确认框")
            return
        page.show_dialog(dialog)

    def _remove_source(self, source: dict) -> None:
        self.ui_call(self._do_remove((source or {}).get("id", "")))

    async def _do_remove(self, source_id: str) -> None:
        if self._rss is None or not source_id:
            return
        try:
            removed = await self._rss.remove_source(source_id)
        except Exception as exc:
            self._status(f"删除失败：{exc}")
            return
        self._status(f"已删除「{self._name_of(source_id)}」"
                     if removed else "该订阅源已不在列表里")
        await self._reload_sources(True)

    def open_article(self, article: dict) -> None:
        article = article or {}
        self._current = article
        if self._view is not None:
            self._view.show_article(article, plain_text(article.get("content", "")))
            self._view.render_articles(self._articles, article.get("id"))
        if article.get("read") or self._rss is None:
            return
        article_id = article.get("id")

        async def _mark() -> None:
            try:
                await self._rss.mark_read(article_id)
            except Exception:   # 已读标记失败不值得打断阅读
                logger.warning("RSS 已读标记失败：%s", article_id)
                return
            article["read"] = True
            stat = self._counts.get(str(article.get("source_id") or "")) or {}
            if int(stat.get("unread") or 0) > 0:
                stat["unread"] = int(stat["unread"]) - 1
                self._counts[str(article.get("source_id") or "")] = stat
            if self._view is not None:
                # 未读字重立刻退掉；勾了"只看未读"时这条已经不在列表里，不动
                self._view.render_articles(self._articles, article.get("id"))
                self._render_counts()
        self.ui_call(_mark())

    def toggle_star(self, article: dict) -> None:
        article = article or {}
        want = not bool(article.get("starred"))
        if self._rss is None:
            self._status("RssService 未就绪")
            return
        article_id = article.get("id")

        async def _star() -> None:
            try:
                ok = await self._rss.star(article_id, want)
            except Exception as exc:
                self._status(f"收藏失败：{exc}")
                return
            if not ok:
                self._status("这条文章已经不在了（收藏未保存）")
                return
            article["starred"] = want
            if self._view is None:
                return
            self._view.render_articles(self._articles, article.get("id"))
            if (self._current or {}).get("id") == article_id:
                self._view.show_article(article, plain_text(
                    article.get("content", "")))
            self._status("已收藏" if want else "已取消收藏")
        self.ui_call(_star())

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    async def _on_progress(self, ev: RssRefreshProgressEvent) -> None:
        if self._view is None:
            return
        self._view.set_progress(ev.done, ev.total)
        if ev.total:
            self._status(f"正在刷新 {ev.done + 1}/{ev.total}："
                         f"{ev.current or '未知源'}")

    async def _on_refreshed(self, ev: RssRefreshedEvent) -> None:
        """逐源刷新完成：只刷当前选中源的那一栏，避免整页闪。"""
        if self._view is None or self._rss is None:
            return
        if str(ev.source_id) == self._selected:
            await self._reload_articles()
        try:
            self._counts = dict(await self._rss.article_counts())
        except Exception:
            return
        self._render_counts()

    async def _after_refresh_done(self) -> None:
        """整批刷完：角标与当前文章列表都要跟上（进度条交给 set_progress(0,0)）。"""
        if self._view is not None:
            self._view.set_progress(0, 0)
        if self._rss is None:
            return
        try:
            self._counts = dict(await self._rss.article_counts())
            await self._reload_articles()
        except Exception as exc:
            logger.debug("刷新后重绘失败：%s", exc)
            return
        self._render_counts()

    # ------------------------------------------------------------------ #
    def _name_of(self, source_id) -> str:
        for source in self._sources:
            if str(source.get("id") or "") == str(source_id or ""):
                return str(source.get("name") or source_id)
        return str(source_id or "")
