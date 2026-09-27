"""SourceImportController：导入视图的事件订阅与编排（§1.16 / 契约 §7）。

订阅 :class:`ImportProgressEvent`（阶段进度）与 :class:`ImportFinishedEvent`（结果）。
本地文件经 FilePicker 读出文本后**先渲染预览卡**（总条数 / 分组 / 追加还是替换），
用户点「确认导入」才交 OnlineImportService.import_from_file；在线 URL 没有预览一步
（文本还没到手），直接按追加导入；图片（二维码）经 QrCodeService.decode_image 解码后
按内容分发 URL / JSON。分发/审计/落库全在服务内，控制器只做 UI 编排。

改控件一律在界面循环上：事件处理器由 :meth:`BaseController.subscribe` 编组回来，
服务侧的进度计数走 ``ImportProgressEvent``（不传回调进来），编排协程收尾由
``ui_call`` 统一 flush——旧版进度条"改了没人下发"就是因为缺这一段。
"""
from __future__ import annotations

import logging

import flet as ft

from app.core.events import ImportFinishedEvent, ImportProgressEvent
from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.sources.online_import_service import (
        OnlineImportService, preview_book_source_text)
except Exception:  # pragma: no cover
    class OnlineImportService:  # type: ignore[no-redef]
        pass

    def preview_book_source_text(text):  # type: ignore[no-redef]
        return {"total": 0, "valid": 0, "broken": 0, "groups": [],
                "error": f"导入服务未就绪：{text[:0]}"}

try:
    from app.services.tools.qr_code_service import QrCodeService
except Exception:  # pragma: no cover
    class QrCodeService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover
    class SourceService:  # type: ignore[no-redef]
        pass

#: 本项目的导入通道目前只有书源 JSON 有落点（其余类型在服务侧登记了"暂无落点"）。
IMPORT_KIND = "book_source"


class SourceImportController(BaseController):
    """导入：本地文件（先预览后确认）+ 在线 URL + 图片（二维码）识别。"""

    destination = "sources.import"

    def on_attach(self) -> None:
        self._importer = self._safe_service(OnlineImportService)
        self._qr = self._safe_service(QrCodeService)
        self._source = self._safe_service(SourceService)
        #: 预览卡待确认的内容：``(展示名, 文本)``；确认/取消后清空。
        self._pending = None
        self._view.on_pick_file = self.pick_file
        self._view.on_pick_image = self.pick_image
        self._view.on_import_url = self.import_url
        self._view.on_confirm_import = self.confirm_import
        self._view.on_cancel_preview = self.cancel_preview
        self.subscribe(ImportProgressEvent, self._on_import_progress)
        self.subscribe(ImportFinishedEvent, self._on_import_finished)

    def on_detach(self) -> None:
        self._pending = None

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    def _page(self):
        """取视图所在 page；未挂到页面上时 ``Control.page`` 抛 RuntimeError，
        而 ``getattr(view, "page", None)`` 只吞 AttributeError——必须自己兜住。"""
        view = self._view
        if view is None:
            return None
        try:
            return view.page
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 本地文件：选 → 读 → 预览（不落库）
    # ------------------------------------------------------------------ #
    def pick_file(self, _=None) -> None:
        """弹出文件选择器；选中后读出文本并渲染预览卡。"""
        view = self._view
        picker = getattr(view, "file_picker", None) if view else None
        if picker is None:
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
                with_data=True)  # 桌面部分平台 path 为 None，bytes 兜底
        except Exception as exc:  # 离线未挂载等场景
            self._fail(f"打开文件选择器失败：{exc}")
            return
        self._read_and_preview(files_awaitable)

    def _read_and_preview(self, files_awaitable) -> None:
        async def _run():
            files = await files_awaitable
            if self._view is None or not files:
                return
            file = files[0]
            name = getattr(file, "path", None) or getattr(file, "name", None) or "所选文件"
            self._view.set_importing(True, "正在读取文件…")
            data = getattr(file, "bytes", None)
            if data:
                text = data.decode("utf-8", errors="replace")
            else:
                path = getattr(file, "path", None)
                if not path:
                    self._view.set_importing(False)
                    return
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        text = handle.read()
                except Exception as exc:
                    self._fail(f"读取文件失败：{exc}")
                    return
            if self._view is None:
                return
            self._offer_preview(name, text)
        self.ui_call(_run())

    def _offer_preview(self, name: str, text: str) -> None:
        """渲染预览卡；读不出东西就直接给一行反馈，不留一张空卡。"""
        self._pending = None
        info = preview_book_source_text(text)
        self._view.set_importing(False)
        self._view.render_preview(name, info)
        if not info.get("error"):
            self._pending = (name, text)

    def cancel_preview(self, _=None) -> None:
        self._pending = None
        if self._view is not None:
            self._view.clear_preview()

    # ------------------------------------------------------------------ #
    # 确认导入（预览卡上的动作）
    # ------------------------------------------------------------------ #
    def confirm_import(self, mode: str = "append") -> None:
        pending = self._pending
        if pending is None or self._view is None:
            return
        name, text = pending
        if mode == "replace":
            # 覆盖整张书源表不可撤销：先弹确认，确认后才真动手。
            page = self._page()
            if page is None:
                self._fail("界面尚未就绪，本次替换没有执行")
                return
            page.show_dialog(self._replace_dialog(name, text))
            return
        self._start_import(text, mode)

    def _replace_dialog(self, name: str, text: str) -> ft.AlertDialog:
        info = preview_book_source_text(text)
        return ft.AlertDialog(
            modal=True,
            title=ft.Text("替换全部现有书源？"),
            content=ft.Column(tight=True, spacing=8, controls=[
                ft.Text(f"{name} 里的 {info.get('valid', 0)} 条书源将完全替换现有"
                        f" {self._existing_count()} 条。"),
                ft.Text("此操作不可撤销。", color=ft.Colors.RED_400, size=12),
            ]),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self._close_dialog()),
                ft.Button("确认替换", icon=ft.Icons.SWAP_HORIZ,
                          color=ft.Colors.RED,
                          on_click=lambda e: self._confirm_replace()),
            ])

    def _existing_count(self) -> int:
        """替换确认框里的"现有多少条"（读服务侧列表，取不到就当 0，不拦确认）。"""
        try:
            return len(getattr(self._source, "sources", None) or [])
        except Exception:
            return 0

    def _close_dialog(self) -> None:
        page = self._page()
        try:
            if page is not None:
                page.pop_dialog()
        except Exception:
            pass

    def _confirm_replace(self) -> None:
        self._close_dialog()
        pending = self._pending
        if pending is None or self._view is None:
            return
        self._start_import(pending[1], "replace")

    # ------------------------------------------------------------------ #
    # 在线 URL / 二维码
    # ------------------------------------------------------------------ #
    def import_url(self, url: str, kind: str = IMPORT_KIND) -> None:
        url = (url or "").strip()
        if not url:
            return
        if self._view is None:
            return
        if self._importer is None:
            self._fail("导入服务未就绪，本次没有落库")
            return
        self._view.set_importing(True, f"正在下载 {url[:60]}…")
        self.ui_call(self._importer.import_from_url(url, IMPORT_KIND))

    def pick_image(self, _=None) -> None:
        """弹出图片选择器；选中后经 QrCodeService 解码二维码并按内容导入。"""
        view = self._view
        picker = getattr(view, "image_picker", None) if view else None
        if picker is None:
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["png", "jpg", "jpeg", "webp", "bmp"],
                with_data=True)
        except Exception as exc:
            self._fail(f"打开图片选择器失败：{exc}")
            return

        async def _run():
            files = await files_awaitable
            if self._view is None or not files:
                return
            file = files[0]
            data = getattr(file, "bytes", None)
            target = data or getattr(file, "path", None)
            if not target:
                return
            self._view.set_importing(True, "正在识别图片中的二维码…")
            if self._qr is None:
                self._fail("二维码识别服务未就绪")
                return
            try:
                text = await self._qr.decode_image(target)
            except Exception as exc:
                self._fail(f"二维码识别失败：{exc}")
                return
            if self._view is None:
                return
            content = str(text or "").strip()
            if not content:
                self._fail("未在图片中识别到二维码内容")
                return
            self._import_decoded(content)

        self.ui_call(_run())

    def _import_decoded(self, text: str) -> None:
        """按解码内容分发：URL → 在线导入；JSON → 走预览卡（与选文件同一条路）。"""
        lowered = text.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            self._view.set_importing(False)
            self.import_url(text)
        elif text.startswith("{") or text.startswith("["):
            self._view.set_importing(False)
            self._offer_preview("二维码里的 JSON 内容", text)
        else:
            self._fail(f"二维码内容不是书源 URL/JSON：{text[:80]}")

    # ------------------------------------------------------------------ #
    # 落库与事件渲染
    # ------------------------------------------------------------------ #
    def _start_import(self, text: str, mode: str) -> None:
        if self._importer is None:
            self._fail("导入服务未就绪，本次没有落库")
            return
        self._pending = None
        stage = "正在解析并写入书源…" if mode == "append" else "正在替换书源列表…"
        self._view.set_importing(True, stage)
        self.ui_call(self._importer.import_from_file(text, IMPORT_KIND, mode))

    def _fail(self, message: str) -> None:
        """发起动作前的失败：一行状态卡即可，别伪装成"导入完成 0 条"。"""
        if self._view is None:
            return
        self._view.show_stage(message, finished=True)

    async def _on_import_progress(self, event: ImportProgressEvent) -> None:
        view = self._view
        if view is None:
            return
        if event.total:
            view.set_progress(event.done, event.total)
        elif event.phase:
            view.set_importing(True, event.phase)

    async def _on_import_finished(self, event: ImportFinishedEvent) -> None:
        if self._view is None:
            return
        self._view.render_result(event.kind, event.count, list(event.errors or []))
