"""QrCodeController：二维码生成/识别/手动导入编排（M5，§1.10 / 契约 §5）。

生成经 QrCodeService.generate 返回 PNG 字节 → 视图渲染 Image；识别经
decode_image；手动输入文本直接走 SourceService/ReplaceRuleService 导入（互操作 JSON）。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.qr_code_service import QrCodeService
except Exception:  # pragma: no cover - 并行线未落盘
    class QrCodeService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover
    class SourceService:  # type: ignore[no-redef]
        pass


class QrCodeController(BaseController):
    """二维码：生成 / 识别 / 手动文本导入。"""

    destination = "tools.qr"

    def on_attach(self) -> None:
        self._svc = self._safe_service(QrCodeService)
        self._view.on_generate = self.generate
        self._view.on_decode = self.decode
        self._view.on_import_text = self.import_text

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def generate(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._view.set_result("请输入要生成二维码的文本")
            return
        if self._svc is None:
            self._view.set_result("QrCodeService 未就绪")
            return
        future = self.ui_call(self._svc.generate(text))
        future.add_done_callback(self.on_done(self._render_image))

    def _render_image(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            data = future.result()
        except Exception as exc:
            self._view.set_result(f"生成失败：{exc}")
            return
        image = None
        if data:
            import flet as ft  # 控制器可碰 flet（边界只约束视图层）
            # Flet 0.86 的 Image 没有 src_base64：图片字节直接交给 src
            image = ft.Image(src=bytes(data), height=200, fit=ft.BoxFit.CONTAIN)
        self._view.render_qr_image(image)
        self._view.set_result("已生成二维码")

    def decode(self, _=None) -> None:
        """弹图片选择器 → 选中图片交 QrCodeService 解码（视图注释的约定）。"""
        if self._svc is None:
            self._view.set_result("QrCodeService 未就绪")
            return
        picker = getattr(self._view, "image_picker", None)
        if picker is None:
            self._view.set_result("图片选择器未挂载")
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=False,
                file_type=ft.FilePickerFileType.IMAGE,
                with_data=True)
        except Exception as exc:
            self._view.set_result(f"打开图片选择器失败：{exc}")
            return
        self.ui_call(self._decode_picked(files_awaitable))

    async def _decode_picked(self, files_awaitable) -> None:
        def say(text: str) -> None:
            # 等文件选择器回来的这段时间够用户切页了：一句话不值得崩
            if self._view is not None:
                self._view.set_result(text)

        try:
            files = await files_awaitable
        except Exception as exc:
            say(f"读取所选图片失败：{exc}")
            return
        if not files:
            say("未选择图片")
            return
        file = files[0]
        data = getattr(file, "bytes", None)
        path = str(getattr(file, "path", "") or "")
        if not data and not path:
            say("未取到图片内容")
            return
        try:
            text = await self._svc.decode_image(data if data else path)
            say(f"识别结果：{text or '（未识别到二维码）'}")
        except Exception as exc:
            say(f"识别失败：{exc}")

    def import_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._view.set_result("请输入 JSON 文本")
            return
        source_service = self._safe_service(SourceService)
        if source_service is None:
            self._view.set_result("SourceService 未就绪，无法导入")
            return
        self._view.set_result("正在导入书源…")
        future = self.ui_call(source_service.import_sources_json(text))
        future.add_done_callback(self.on_done(self._after_import))

    def _after_import(self, future) -> None:
        if self._view is None:
            return
        try:
            count, errors = future.result()
        except Exception as exc:
            self._view.set_result(f"导入失败：{exc}")
            return
        message = f"已导入 {count} 个书源"
        if errors:
            message += f"；{len(errors)} 条告警：" + " | ".join(
                str(err) for err in errors[:3])
        self._view.set_result(message)
