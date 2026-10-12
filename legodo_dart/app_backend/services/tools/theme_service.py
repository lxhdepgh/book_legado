"""ThemeService：内置主题皮肤的服务外壳（M5，§3.11 / 契约 §12）。

职责与边界：

- **外壳化** :mod:`app.services.theme_service`（M2 冻结副本）的能力：``BUILTIN_SKINS``
  五个命名皮肤（classic_blue / emerald_green / warm_amber / elegant_purple / ink_gray）
  与 ``EINK_PALETTE`` 墨水屏纯黑白配色原样复用自
  :mod:`app.themes.builtin_themes`（纯数据 dataclass，零 flet）；
- **外观持久化并入 SettingsService**：统一走 settings 键值（键
  :data:`THEME_SETTINGS_KEY`），旧 ``appearance.json`` 停止写入，其旧值由 M6 迁移；
- **服务不持控件**：:meth:`apply` 返回纯数据 :class:`ThemeData`，由视图侧把数据
  落成 ``ft.Theme``（实时预览随视图层）；本模块 **零 flet import**。

冻结旧服务 ``app/services/theme_service.py`` 仍服务 M2 冻结视图（app.py /
appearance_view.py），本外壳不改动它，二者并行存在直至 M6 收编。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.lifecycle import Service
from app.core.settings import SettingsService
from app.themes.builtin_themes import BUILTIN_SKINS, EINK_PALETTE, Palette

# --------------------------------------------------------------------------- #
# 外观快照在 SettingsService 中的统一键（替代旧 appearance.json 文件）。
# --------------------------------------------------------------------------- #
THEME_SETTINGS_KEY = "appearance.snapshot"

#: 合法皮肤名集合（五命名皮肤，冻结）。
VALID_SKINS = frozenset(BUILTIN_SKINS)

#: 默认皮肤 / 默认模式（与 M2 AppearanceSettings 默认值对齐）。
DEFAULT_SKIN = "classic_blue"
VALID_MODES = frozenset({"light", "dark", "system"})


@dataclass(frozen=True)
class ThemeData:
    """apply() 的返回值：视图侧据此构造 ft.Theme（服务不碰 flet）。

    ``light`` / ``dark`` 是颜色角色 → hex 的扁平字典；墨水屏模式下二者均为
    :data:`EINK_PALETTE` 且 ``mode`` 归一为 ``"light"``。
    """

    skin_id: str
    mode: str            # "light" | "dark" | "system"
    eink: bool
    light: dict[str, str]
    dark: dict[str, str]


def _palette_dict(palette: Palette) -> dict[str, str]:
    """:class:`Palette` → 扁平颜色角色字典（视图侧映射为 ColorScheme）。"""
    return {
        "primary": palette.primary,
        "on_primary": palette.on_primary,
        "primary_container": palette.primary_container,
        "on_primary_container": palette.on_primary_container,
        "surface": palette.surface,
        "surface_container": palette.surface_container,
        "on_surface": palette.on_surface,
        "on_surface_variant": palette.on_surface_variant,
        "outline": palette.outline,
    }


class ThemeService(Service):
    """内置主题皮肤外壳：提供外观偏好读写与纯数据主题求值。"""

    name = "ThemeService"

    # SettingsService：on_start 读回外观快照需其缓存已从主库加载（B4 拓扑声明，
    # 与 on_start 的软 require 容错并存：构造注入优先，缺失时回退默认皮肤）。
    dependencies: tuple[type[Service], ...] = (SettingsService,)

    def __init__(self, settings: Any = None, storage: Any = None,
                 data_dir: str | Path | None = None) -> None:
        super().__init__()
        self._settings = settings
        self._storage = storage
        # 保留 data_dir 仅作兼容占位：本外壳不再向该目录写 appearance.json。
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._current = self._normalize({})

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        """从 SettingsService 读回外观快照（未注入时从 hub 依赖面取）。"""
        if self._settings is None and self.hub is not None:
            try:  # pragma: no cover - 取决于并行 SettingsService 是否注册
                from app.core.settings import SettingsService
                self._settings = self.hub.require(SettingsService)
            except Exception:
                self._settings = None
        raw: Any = None
        if self._settings is not None:
            try:
                raw = self._settings.get(THEME_SETTINGS_KEY, None)
            except Exception:
                raw = None
        if isinstance(raw, dict):
            self._current = self._normalize(raw)

    # ------------------------------------------------------------------ #
    # 外观偏好读写（并入 SettingsService，不再写 appearance.json）
    # ------------------------------------------------------------------ #
    def get_preferences(self) -> dict[str, Any]:
        """当前外观偏好快照（skin_id / mode / eink）。"""
        return dict(self._current)

    def set_preferences(self, skin_id: str | None = None,
                        mode: str | None = None,
                        eink: bool | None = None) -> dict[str, Any]:
        """合并更新外观偏好并经 SettingsService 持久化（统一键值）。"""
        merged = dict(self._current)
        if skin_id is not None:
            merged["skin_id"] = self._norm_skin(skin_id)
        if mode is not None:
            merged["mode"] = self._norm_mode(mode)
        if eink is not None:
            merged["eink"] = bool(eink)
        self._current = self._normalize(merged)
        if self._settings is not None:
            try:
                self._settings.set(THEME_SETTINGS_KEY, self._current)
            except Exception:  # pragma: no cover - 存储未就绪时内存态仍生效
                pass
        return self.get_preferences()

    # ------------------------------------------------------------------ #
    # 主题求值（返回纯数据，不持控件）
    # ------------------------------------------------------------------ #
    def apply(self, skin_id: str | None = None,
              dark_mode: str | None = None,
              eink: bool | None = None) -> ThemeData:
        """按皮肤 / 明暗 / 墨水屏返回主题数据（视图侧应用为 ft.Theme）。

        入参缺省回退到 :meth:`get_preferences` 的当前偏好；传入即临时预览值
        （实时预览随视图层，不污染已持久化偏好）。
        """
        pref = dict(self._current)
        if skin_id is not None:
            pref["skin_id"] = self._norm_skin(skin_id)
        if dark_mode is not None:
            pref["mode"] = self._norm_mode(dark_mode)
        if eink is not None:
            pref["eink"] = bool(eink)

        skin = BUILTIN_SKINS.get(pref["skin_id"], BUILTIN_SKINS[DEFAULT_SKIN])
        light = _palette_dict(skin.light)
        dark = _palette_dict(skin.dark)
        mode = pref["mode"]
        if pref["eink"]:
            # 墨水屏：亮暗统一纯黑白高对比，模式归一 light
            light = dark = _palette_dict(EINK_PALETTE)
            mode = "light"
        return ThemeData(skin_id=skin.id, mode=mode, eink=bool(pref["eink"]),
                         light=light, dark=dark)

    # ------------------------------------------------------------------ #
    # 规范化（与 M2 AppearanceSettings 的合法集合对齐）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm_skin(value: Any) -> str:
        return str(value) if str(value) in VALID_SKINS else DEFAULT_SKIN

    @staticmethod
    def _norm_mode(value: Any) -> str:
        return str(value) if str(value) in VALID_MODES else "system"

    @classmethod
    def _normalize(cls, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "skin_id": cls._norm_skin(raw.get("skin_id", DEFAULT_SKIN)),
            "mode": cls._norm_mode(raw.get("mode", "system")),
            "eink": bool(raw.get("eink", False)),
        }


__all__ = [
    "ThemeService", "ThemeData", "THEME_SETTINGS_KEY",
    "VALID_SKINS", "VALID_MODES", "DEFAULT_SKIN",
    "BUILTIN_SKINS", "EINK_PALETTE",
]
