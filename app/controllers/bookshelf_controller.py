"""BookshelfController：书架视图的事件订阅与服务编排（§7.4）。

数据链路（M2 遗留的占位装配已换成真实服务）：

- 记录来自 :class:`SourceService.list_bookshelf`（notShelf 试读记录天然过滤），
  订阅 :class:`SourceChangedEvent` 自动刷新（入架/移除/换源后都会发）；
- 移除经 ``unshelve_book`` 持久化，绝不做只在内存里滤一行的假删除；
- 换源对话框：本地种子 ``ReadingService.switch_seeds`` 秒开，联网扫描走
  ``switch_source``（候选按同站优先排序、排除当前源自身），确认写回经
  ``apply_source_switch``（旧 SourceService 组合，保留用户进度）；
- 本地书：工具栏「导入本地书籍」→ ``LocalBookService.import_local_file``，
  解析产物直接落书架（服务侧入架），导入结果只报"几本成功/几个为什么失败"；
- 换封面：长按管理对话框「更换封面」→ FilePicker 单选图片 → ``update_cover``，
  成败以**读回书架记录**为准（服务只保证写 books 表，book_url 对不上时那一行
  根本不会变，这时候报"已更新"就是假话）。

本文件允许 import app.services.* 类型（hub.get() 类型参数与纯函数）与 flet
（文件选择器的枚举常量，视图层红线不适用于控制器）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import flet as ft

from app.core.events import (
    DownloadProgressEvent,
    ProgressSavedEvent,
    SourceChangedEvent,
)
from app.controllers.base import BaseController
from app.services.content.local_book_service import (
    SUPPORTED_EXTS,
    LocalBookService,
    is_local_source_url,
)
from app.services.reading.reading_service import (
    ReadingService,
    as_book_item,
)
from app.services.reading.download_service import DownloadService
from app.services.sources.source_service import SourceService

if TYPE_CHECKING:  # 仅类型注解用；运行时在方法内延迟导入，避免与 views 包循环导入。
    from app.views.reading.source_switch_dialog import SourceSwitchDialog

logger = logging.getLogger(__name__)


class BookshelfController(BaseController):
    """书架：记录渲染 / 续读卡 / 整书缓存任务 / 换源编排 / 移除确认。"""

    destination = "reading.bookshelf"

    def on_attach(self) -> None:
        self._reading = self._safe_service(ReadingService)
        self._download = self._safe_service(DownloadService)
        self._source_service = self._safe_service(SourceService)
        self._local = self._safe_service(LocalBookService)
        self._records: list[dict] = []
        self._sources: list = []
        self._switch_dialog: SourceSwitchDialog | None = None
        self._switch_task = None
        # 跨视图开读槽：NavigationHost.show 装配时绑定（视图间零互引）
        self.on_open_reader = None       # (record: dict) -> None

        self._view.on_open_book = self.open_book
        self._view.on_refresh = self.refresh
        self._view.on_filter = self._apply_filter
        self._view.on_toggle_layout = self.toggle_layout
        self._view.on_request_switch = self.open_source_switch
        self._view.on_toggle_cache = self.toggle_cache
        self._view.on_cancel_cache = self.cancel_cache
        self._view.on_request_remove = self.request_remove
        self._view.on_request_cover = self.request_cover
        self._view.on_import_local = self.import_local

        # 进度落库 → 最近阅读卡；下载进度 → 0.5s 节流后刷新缓存状态
        self.subscribe(ProgressSavedEvent, self._on_progress_saved)
        self.subscribe(DownloadProgressEvent,
                       self.throttle(self._on_download_progress, 0.5))
        self.subscribe(SourceChangedEvent, self._on_source_changed)
        self.ui_call(self._load())

    def on_detach(self) -> None:
        self._switch_dialog = None
        self._switch_task = None

    # ------------------------------------------------------------------ #
    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 数据装配 / 渲染
    # ------------------------------------------------------------------ #
    async def _load(self, report: bool = True) -> None:
        if self._view is None:  # attach 时排的任务可能在切页后才跑
            return
        if self._source_service is None:
            self._view.set_status_text("SourceService 未就绪（并行集成中）")
            return
        try:
            self._records = [self._to_render_record(r)
                             for r in await self._source_service.list_bookshelf()]
            self._sources = list(await self._source_service.list_sources())
        except Exception as exc:
            logger.warning("书架加载失败：%s", exc)
            return
        if self._view is None:  # 在途时用户已切页
            return
        self._view.render_books(self._records)
        # 在途缓存任务从服务快照补渲染：切页/重启回来时不靠"刚好收到事件"
        self._render_tasks()
        if report:
            # 事件驱动的刷新不占状态行：否则"已换源到 X 站"这类反馈会被盖掉
            self._view.set_status_text(f"书架共 {len(self._records)} 本")

    @staticmethod
    def _to_render_record(raw: dict) -> dict:
        """副本 + ``local`` 标记：视图据此收起换源/整书缓存这两个对本地书无意义的入口。

        必须复制——``list_bookshelf`` 返回的是 SourceService 内存里的那批 dict，
        直接写键会被 save_bookshelf 落进 bookshelf.json。
        """
        record = dict(raw or {})
        record["local"] = is_local_source_url(
            record.get("source_url") or record.get("origin"))
        return record

    async def _on_source_changed(self, event: SourceChangedEvent) -> None:
        await self._load(report=False)

    def refresh(self, _=None) -> None:
        self.ui_call(self._load())

    def _apply_filter(self, query: str = "", sort: str = "read",
                      group: str = "__all__") -> None:
        query = (query or "").strip().lower()
        records = list(self._records)
        if query:
            records = [
                r for r in records
                if query in str(r.get("name", "")).lower()
                or query in str(r.get("author", "")).lower()
            ]
        self._view.render_books(records)

    def toggle_layout(self, _=None) -> None:
        self._view.set_layout("grid" if getattr(self._view, "_layout", "list") == "list"
                              else "list")

    # ------------------------------------------------------------------ #
    # 本地书导入（导完即上架：LocalBookService 内已入架，这里只补刷新与反馈）
    # ------------------------------------------------------------------ #
    def import_local(self, _=None) -> None:
        if self._local is None:
            self._view.set_status_text("LocalBookService 未就绪")
            return
        picker = getattr(self._view, "file_picker", None)
        if picker is None:
            self._view.set_status_text("文件选择器未挂载")
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=sorted(SUPPORTED_EXTS))
        except Exception as exc:
            self._view.set_status_text(f"打开文件选择器失败：{exc}")
            return
        self.ui_call(self._import_picked(files_awaitable))

    async def _import_picked(self, files_awaitable) -> None:
        try:
            files = await files_awaitable
        except Exception as exc:
            self._status(f"读取所选文件失败：{exc}")
            return
        paths = [str(getattr(f, "path", "") or "") for f in files or []]
        paths = [p for p in paths if p]
        if not paths:
            self._status("已取消导入（未选择文件）")
            return
        self._status(f"正在解析 {len(paths)} 个文件…")
        imported, failed = 0, []
        for path in paths:
            try:
                await self._local.import_local_file(path)
                imported += 1
            except Exception as exc:
                failed.append(f"{Path(path).name}：{exc}")
        # 入架已在服务内完成；这里读回列表，让新书记录与状态行同真。
        await self._load(report=False)
        text = f"已导入 {imported} 本本地书"
        if failed:
            text += f"；失败 {len(failed)} 个：{'、'.join(failed)}"
        self._status(text)

    def _status(self, text: str) -> None:
        """状态行只在视图还在架上的时候写（在途切页是常态，不是异常）。"""
        if self._view is not None:
            self._view.set_status_text(text)

    # ------------------------------------------------------------------ #
    # 开读 / 移除
    # ------------------------------------------------------------------ #
    def open_book(self, record: dict, _=None) -> None:
        if getattr(self, "on_open_reader", None) is not None:
            self.on_open_reader(dict(record or {}))

    def request_remove(self, record: dict, _=None) -> None:
        page = self._page()
        if page is None:
            return
        dialog = self._view.confirm_remove(
            record, on_confirm=lambda r: self._do_remove(r))
        page.show_dialog(dialog)

    def _do_remove(self, record: dict) -> None:
        source_url = str((record or {}).get("source_url")
                         or (record or {}).get("origin") or "")
        book_url = self._book_url_of(record)
        self.ui_call(self._remove_and_refresh(source_url, book_url))

    async def _remove_and_refresh(self, source_url: str, book_url: str) -> None:
        ok = False
        try:
            ok = bool(await self._source_service.unshelve_book(
                source_url, book_url))
        except Exception as exc:
            logger.warning("移出书架失败：%s", exc)
        page = self._page()
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        await self._load()
        if self._view is None:  # 在途时用户已切页
            return
        self._view.set_status_text(
            "已移出书架" if ok else "未找到该书架记录（可能已被移除）")

    # ------------------------------------------------------------------ #
    # 换封面（本地图片 → SourceService.update_cover：Pillow 归一落 image-cache/covers/）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _book_url_of(record: dict) -> str:
        """书架记录里的书地址：``url`` 是渲染记录用的键，``bookUrl`` 兼容原始数据。"""
        return str((record or {}).get("url")
                   or (record or {}).get("bookUrl") or "")

    def request_cover(self, record: dict, _=None) -> None:
        book_url = self._book_url_of(record)
        if not book_url:
            self._view.set_status_text("这条书架记录没有书地址，换不了封面")
            return
        if self._source_service is None:
            self._view.set_status_text("SourceService 未就绪")
            return
        picker = getattr(self._view, "file_picker", None)
        if picker is None:
            self._view.set_status_text("文件选择器未挂载")
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=False, file_type=ft.FilePickerFileType.IMAGE)
        except Exception as exc:
            self._view.set_status_text(f"打开图片选择器失败：{exc}")
            return
        self.ui_call(self._apply_cover(
            files_awaitable, book_url, str((record or {}).get("name") or "")))

    async def _apply_cover(self, files_awaitable, book_url: str, name: str) -> None:
        try:
            files = await files_awaitable
        except Exception as exc:
            self._status(f"读取所选图片失败：{exc}")
            return
        # 空结果 = 用户取消：什么都不写，也不发 SourceChangedEvent。
        path = str(getattr((files or [None])[0], "path", "") or "")
        if not path:
            self._status("已取消换封面（未选择图片）")
            return
        try:
            resolved = await self._source_service.update_cover(book_url, path)
        except Exception as exc:
            logger.warning("换封面失败：%s", exc)
            self._status(f"《{name}》换封面失败：{exc}")
            return
        # 自己读回一次再报成败：update_cover 只保证写 books 表，书架记录要靠
        # book_url 对上才落得下去。对不上时界面那一行不会变，这时候说
        # "封面已更新"就是假话。事件驱动的那次刷新只是双保险。
        await self._load(report=False)
        applied = any(
            str(r.get("cover_url") or "") == str(resolved or "")
            for r in self._records if self._book_url_of(r) == book_url)
        self._status(f"《{name}》封面已更新" if applied else
                     f"《{name}》的这张记录没对上书地址，封面没落到书架上")

    # ------------------------------------------------------------------ #
    # 整书缓存（任务统一经 DownloadService 注册/取消）
    # ------------------------------------------------------------------ #
    def toggle_cache(self, record: dict, _=None) -> None:
        """整书缓存 / 取消缓存：任务经 DownloadService.enqueue('whole_cache')。"""
        try:
            self._download.enqueue(
                "whole_cache", record.get("url", ""),
                payload={"source": self._source_for(record), "book": record})
            self._view.set_status_text(f"已加入整书缓存队列：{record.get('name', '')}")
        except Exception as exc:  # 服务未就绪时降级提示
            logger.warning("整书缓存入队失败：%s", exc)
            self._view.set_status_text("整书缓存服务暂未就绪")
            return
        self._render_tasks()

    def cancel_cache(self, task_id: str) -> None:
        try:
            ok = bool(self._download.cancel(str(task_id or "")))
        except Exception as exc:
            logger.warning("取消整书缓存失败：%s", exc)
            self._view.set_status_text("取消失败：整书缓存服务暂未就绪")
            return
        self._view.set_status_text(
            "已取消整书缓存" if ok else "该任务已结束，无需取消")
        self._render_tasks()

    def _source_for(self, record: dict):
        origin = str((record or {}).get("source_url")
                     or (record or {}).get("origin") or "")
        for source in self._sources:
            if str(getattr(source, "bookSourceUrl", "") or "") == origin:
                return source
        return None

    # 在途任务：DownloadService 的快照 → 视图行（切页回来也能续看）
    _LIVE_STATUSES = ("queued", "running")

    def _cache_rows(self) -> list[dict]:
        if self._download is None:
            return []
        names = {str(r.get("url") or ""): str(r.get("name") or "")
                 for r in self._records}
        try:
            tasks = self._download.list_tasks()
        except Exception as exc:
            logger.debug("读取整书缓存任务失败：%s", exc)
            return []
        return [
            {"task_id": t.task_id,
             "name": names.get(str(t.book_url or "")) or str(t.book_url or ""),
             "done": t.done, "total": t.total, "progress": t.progress}
            for t in tasks
            if t.kind == "whole_cache" and t.status in self._LIVE_STATUSES
        ]

    def _render_tasks(self) -> None:
        if self._view is None:
            return
        self._view.set_cache_tasks(self._cache_rows())

    async def _on_download_progress(self, event: DownloadProgressEvent) -> None:
        if self._view is None:  # 节流窗口内用户已切页
            return
        self._view.set_status_text(
            f"整书缓存进度 {int(event.progress * 100)}%")
        self._render_tasks()

    # ------------------------------------------------------------------ #
    # 换源对话框（编排并入本 Controller，不单开文件）
    # ------------------------------------------------------------------ #
    def open_source_switch(self, record: dict, _=None) -> None:
        # 延迟导入：控制器顶层不反向 import 视图（循环导入防护，见模块 docstring）。
        from app.views.reading.source_switch_dialog import SourceSwitchDialog
        page = self._page()
        if page is None:
            return
        dialog = SourceSwitchDialog(record)
        dialog.on_start = self._start_switch
        dialog.on_cancel = lambda: self._stop_switch(dialog)
        dialog.on_confirm = self._confirm_switch
        dialog.on_close = lambda: page.pop_dialog()
        dialog.render_candidates([])
        self._switch_dialog = dialog
        page.show_dialog(dialog)
        self.ui_call(self._seed_switch(record, dialog))

    async def _seed_switch(self, record: dict, dialog) -> None:
        """本地种子先行：之前搜索落库的同名书，不联网、秒开。"""
        try:
            seeds = await self._reading.switch_seeds(record)
        except Exception as exc:
            logger.debug("换源种子获取失败：%s", exc)
            seeds = []
        dialog.render_candidates([self._to_candidate(s) for s in seeds or []])
        dialog.set_status(
            f"本地已有 {len(seeds or [])} 个结果；点「开始换源」扫描全部书源"
            if seeds else "点「开始换源」扫描全部书源")

    def _start_switch(self, record: dict) -> None:
        dialog = self._switch_dialog
        if dialog is None:
            return
        dialog.set_running(True, "正在扫描同名书源…")
        dialog.set_progress(0)
        # switch_source 扫描候选（seed/扫描/排序在服务内）；候选结果回渲染。
        self._switch_task = self.ui_call(self._run_switch(record, dialog))

    async def _run_switch(self, record: dict, dialog: SourceSwitchDialog) -> None:
        def on_progress(completed: int, total: int, failures: int) -> None:
            if self._switch_dialog is not dialog:
                return  # 对话框已关，不再刷旧句柄
            dialog.set_progress(completed / total if total else 0)
            dialog.set_running(True, f"正在扫描 {completed}/{total} 个书源"
                               + (f"，{failures} 个失败" if failures else ""))

        try:
            matches = await self._reading.switch_source(
                record, self._switch_candidates(record),
                keyword=record.get("name", ""), on_progress=on_progress)
            candidates = [self._to_candidate(m) for m in matches or []]
            dialog.render_candidates(candidates)
            dialog.set_running(False, f"扫描完成，{len(candidates)} 个候选")
        except Exception as exc:
            dialog.set_running(False, f"换源失败：{exc}")

    def _switch_candidates(self, record: dict) -> list:
        """扫描顺序：同站优先、已失效靠后；排除当前源自身。"""
        own = str((record or {}).get("source_url")
                  or (record or {}).get("origin") or "")
        token = _site_token(own)
        pool = []
        for source in self._sources:
            url = str(getattr(source, "bookSourceUrl", "") or "")
            if url == own:
                continue
            if not getattr(source, "enabled", False):
                continue
            if not str(getattr(source, "searchUrl", "") or "").strip():
                continue
            pool.append(source)
        pool.sort(key=lambda s: (
            0 if token and token == _site_token(
                str(getattr(s, "bookSourceUrl", "") or "")) else 1,
            0 if getattr(s, "enabled", False) else 1,
            str(getattr(s, "bookSourceUrl", "") or "")))
        return pool

    def _to_candidate(self, match: dict) -> dict:
        """服务候选 ``{source_url,source_name,book:快照}`` → 对话框渲染形态。"""
        match = match or {}
        book = as_book_item(match.get("book") or {})
        return {
            "source": self._source_by_url(match.get("source_url", "")),
            "book": book,
            "source_name": match.get("source_name", "") or "",
        }

    def _source_by_url(self, url: str):
        url = str(url or "")
        for source in self._sources:
            if str(getattr(source, "bookSourceUrl", "") or "") == url:
                return source
        return None

    def _stop_switch(self, dialog: SourceSwitchDialog) -> None:
        task = self._switch_task
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
            self._switch_task = None
        dialog.set_running(False, "已停止换源扫描")

    def _confirm_switch(self, record: dict, source, book) -> None:
        if source is None:
            self._view.set_status_text("该候选的来源书源已不在列表中，无法换源")
            return
        self.ui_call(self._do_switch(record, source, book))

    async def _do_switch(self, record: dict, source, book) -> None:
        ok = False
        try:
            ok = bool(await self._reading.apply_source_switch(
                record, source, book))
        except Exception as exc:
            logger.warning("换源写回失败：%s", exc)
        page = self._page()
        if page is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
        await self._load()
        if self._view is None:  # 在途时用户已切页
            return
        name = getattr(book, "name", "") or ""
        self._view.set_status_text(
            f"已切换到：{name}" if ok else "换源失败：记录已被移除或与其他书冲突")

    # ------------------------------------------------------------------ #
    async def _on_progress_saved(self, event: ProgressSavedEvent) -> None:
        # detach() 的退订挡不住已经 publish 出去的在途事件（EventBus 把处理器
        # submit 到引擎循环），切页瞬间这条协程仍可能跑起来——那时 _view 已是 None。
        if self._view is None:
            return
        name = ""
        for record in self._records:
            if str(record.get("url") or record.get("bookUrl") or "") == event.book_url:
                name = str(record.get("name", "") or "")
                break
        self._view.set_continue_reading(
            {"name": name, "url": event.book_url})

    def _page(self):
        try:
            return self._view.page
        except Exception:
            return None


def _site_token(url: str) -> str:
    """书源 URL 的站点名（旧换源排序里的 host 令牌近似：去 www 的主机首段）。"""
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] if host else ""
