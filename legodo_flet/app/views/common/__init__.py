"""通用 UI 组件库（对齐 Dart 的 core/widgets/）。

本包提供跨屏幕复用的展示组件，禁止 import app.services.*（红线 §7.5）。
"""
from .failure_banner import EmptyState, FailureBanner, LoadingSpinner, StatusChip
from .state_bar import StateBar
from .md3_components import (
    make_card,
    make_filled_button,
    make_tonal_button,
    make_outlined_button,
    make_text_field,
    make_chip,
)
from .md3_styles import (
    RADIUS_CARD,
    RADIUS_BUTTON,
    RADIUS_INPUT,
    ELEVATION_1,
    SPACE_SM,
    SPACE_MD,
)

__all__ = [
    "FailureBanner",
    "LoadingSpinner",
    "EmptyState",
    "StatusChip",
    "StateBar",
    # MD3 components
    "make_card",
    "make_filled_button",
    "make_tonal_button",
    "make_outlined_button",
    "make_text_field",
    "make_chip",
    # MD3 constants
    "RADIUS_CARD",
    "RADIUS_BUTTON",
    "RADIUS_INPUT",
    "ELEVATION_1",
    "SPACE_SM",
    "SPACE_MD",
]
