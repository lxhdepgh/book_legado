# -*- coding: utf-8 -*-
"""QrCodeService：二维码生成 + 图片识别（M5，契约 §5）。

- **生成** ``generate(text) -> bytes``：``qrcode`` 库构造矩阵，Pillow 渲染 PNG，
  返回 PNG 字节流（视图侧直接存文件或展示）。
- **识别** ``decode_image(path_or_bytes) -> str | None``：opencv-python-headless 的
  ``QRCodeDetector`` 识别图片文件或剪贴板截图字节。**桌面无摄像头，不做摄像头
  扫码**；手动输入通道由视图层输入框直接调 SourceService/ReplaceRuleService 导入。
- **内容互操作**：书源/替换规则 JSON 文本 ↔ SourceService/ReplaceRuleService
  （本服务只负责编解码文本，不解析业务 JSON）。

硬约束：零 flet import；qrcode / opencv 懒加载；跨平台（headless 免 GUI 依赖）。
"""
from __future__ import annotations

import io
import logging

from app.core.lifecycle import Service

logger = logging.getLogger(__name__)


class QrCodeService(Service):
    """二维码生成（qrcode+Pillow）与图片识别（opencv QRCodeDetector）。"""

    name = "QrCodeService"
    # 不依赖 StorageService（生成产物由调用方/下载服务落盘；识别只读入参）。
    dependencies: tuple[type[Service], ...] = ()

    async def on_start(self) -> None:
        logger.info("QrCodeService 已启动")

    # ------------------------------------------------------------------ #
    # 生成（契约 §5：generate(text) -> bytes，PNG）
    # ------------------------------------------------------------------ #
    async def generate(self, text: str) -> bytes:
        """把文本编码为二维码 PNG 字节流。"""
        import qrcode  # 懒加载

        text = str(text or "")
        if not text:
            raise ValueError("二维码内容不能为空")
        qr = qrcode.QRCode(
            box_size=10, border=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ------------------------------------------------------------------ #
    # 识别（契约 §5：decode_image(path_or_bytes) -> str | None）
    # ------------------------------------------------------------------ #
    async def decode_image(self, path_or_bytes) -> str | None:
        """识别图片中的二维码内容；无二维码/解码失败返回 None。

        入参为图片文件路径（str/Path）或图片字节（PNG/JPG 等）。
        仅支持图片文件/剪贴板截图——桌面无摄像头，不提供扫码摄像头流。
        """
        import cv2  # 懒加载
        import numpy as np  # 懒加载（opencv 传递依赖）

        if isinstance(path_or_bytes, (bytes, bytearray)):
            arr = np.frombuffer(bytes(path_or_bytes), dtype=np.uint8)
            image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            image = cv2.imread(str(path_or_bytes), cv2.IMREAD_COLOR)
        if image is None:
            logger.warning("QrCodeService.decode_image：无法解码图片 %r",
                           path_or_bytes if isinstance(path_or_bytes, str)
                           else "<bytes>")
            return None
        detector = cv2.QRCodeDetector()
        try:
            data, _points, _straight = detector.detectAndDecode(image)
            # opencv 5.x QRCodeDetector 在中等分辨率(约370px)上偶发检测不到定位符；
            # 放大 2x（最近邻，不糊模块）后重试即可命中。
            if not (data or "").strip():
                up = cv2.resize(image, None, fx=2, fy=2,
                                interpolation=cv2.INTER_NEAREST)
                data, _points, _straight = detector.detectAndDecode(up)
        except Exception as exc:  # opencv 内部异常不崩识别流程
            logger.warning("QrCodeService.decode_image 识别异常: %s", exc)
            return None
        data = (data or "").strip()
        return data or None
