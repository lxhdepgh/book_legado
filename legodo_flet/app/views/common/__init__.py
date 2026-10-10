"""通用 UI 组件库（对齐 Dart 的 core/widgets/）。

本包提供跨屏幕复用的展示组件，禁止 import app.services.*（红线 §7.5）。
"""
from .failure_banner import EmptyState, FailureBanner, LoadingSpinner, StatusChip

__all__ = ["FailureBanner", "LoadingSpinner", "EmptyState", "StatusChip"]
