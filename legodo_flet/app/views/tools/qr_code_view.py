"""QrCodeView：二维码生成 / 识别 / 手动输入导入（M5，§1.10 / 契约 §5 / §13）。

文本生成二维码、从图片识别二维码、手动输入 JSON 文本直接导入（无二维码也可用）。
**零 app.services.* import**：生成/识别经 Controller 调 QrCodeService。
"""
from __future__ import annotations

import flet as ft


class QrCodeView(ft.Column):
    """二维码：内容输入 + 生成 + 识别结果 + 手动导入。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_generate = None        # (text: str) -> None
        self.on_decode = None          # () -> None（Controller 弹图片选择）
        self.on_import_text = None     # (text: str) -> None

        # 图片选择走 page.services 通道（navigation 自动挂载）。
        self.image_picker = ft.FilePicker()

        self._title = ft.Text("二维码", size=20, weight=ft.FontWeight.BOLD)
        self._input = ft.TextField(
            label="要生成二维码的文本（书源/替换规则 JSON）", multiline=True,
            min_lines=3, max_lines=5)
        self._gen_btn = ft.FilledButton(
            "生成二维码", icon=ft.Icons.QR_CODE_2,
            on_click=lambda e: self._fire(self.on_generate, self._input.value or ""))
        self._decode_btn = ft.OutlinedButton(
            "从图片识别", icon=ft.Icons.IMAGE_SEARCH,
            on_click=lambda e: self._fire(self.on_decode))

        self._qr_host = ft.Container(
            height=220, alignment=ft.Alignment.CENTER,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=6)
        self._result = ft.Text("", size=12, color=ft.Colors.GREY_700, selectable=True)
        self._manual = ft.TextField(
            label="手动输入 JSON 直接导入（免二维码）", multiline=True,
            min_lines=3, max_lines=6)
        self._import_btn = ft.TextButton(
            "导入以上文本", icon=ft.Icons.INPUT,
            on_click=lambda e: self._fire(self.on_import_text, self._manual.value or ""))

        self.controls = [
            self._title,
            self._input,
            ft.Row([self._gen_btn, self._decode_btn], spacing=8),
            self._qr_host,
            self._result,
            ft.Divider(height=1),
            self._manual,
            ft.Row([self._import_btn], alignment=ft.MainAxisAlignment.END),
        ]

    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    def render_qr_image(self, image) -> None:
        """渲染生成的二维码图像（ft.Image 或占位文本；离线传入 None 清空）。"""
        self._qr_host.content = image if image is not None else ft.Text(
            "（二维码预览区）", color=ft.Colors.GREY_400)

    def set_result(self, text: str) -> None:
        self._result.value = text or ""
