"""AppearanceController：外观设置页编排（M7 后补全，§3.11 主题皮肤）。

偏好读写经 ThemeService（get_preferences / set_preferences），实时预览经
``apply()`` 取纯数据 ThemeData 交给视图应用为 ft.Theme（服务不持控件）。

预览/保存语义：控件每变更更新 ``self._pending``（内存暂存），预览按 pending
求值即时应用；保存时才 ``set_preferences(**pending)`` 落 SettingsService。
恢复默认只改 pending 并预览，不直接落库。

视图零 import 服务：本控制器是唯一桥（红线 §7.5，controllers 允许
import 服务类型作 ``hub.get()`` 参数）。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.theme_service import ThemeService
except Exception:  # pragma: no cover - 服务未落盘时退化
    class ThemeService:  # type: ignore[no-redef]
        pass

#: 恢复默认时的目标偏好（与 ThemeService 默认值对齐）。
_DEFAULTS: dict[str, object] = {
    "skin_id": "classic_blue", "mode": "system", "eink": False,
}


class AppearanceController(BaseController):
    """外观：载入偏好 / 模式皮肤墨水屏切换 / 实时预览 / 保存 / 恢复默认。"""

    destination = "settings.appearance"

    def on_attach(self) -> None:
        self._theme = self._safe_service(ThemeService)
        self._view.on_change_mode = self.change_mode
        self._view.on_change_skin = self.change_skin
        self._view.on_change_eink = self.change_eink
        self._view.on_save = self.save
        self._view.on_reset = self.reset
        self.load()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def load(self, _=None) -> None:
        """载入当前偏好 → pending → 回填视图并应用一次预览（首屏即所见）。"""
        pref = self._preferences()
        self._pending: dict[str, object] = {
            "skin_id": pref.get("skin_id", "classic_blue"),
            "mode": pref.get("mode", "system"),
            "eink": bool(pref.get("eink", False)),
        }
        self._reflect_pending()
        self.preview()

    def _preferences(self) -> dict:
        if self._theme is None:
            return {}
        try:
            return self._theme.get_preferences() or {}
        except Exception as exc:
            logger.warning("读外观偏好失败：%s", exc)
            return {}

    # ------------------------------------------------------------------ #
    def change_mode(self, mode: str) -> None:
        self._pending["mode"] = mode
        self._reflect_pending()
        self.preview()

    def change_skin(self, skin_id: str) -> None:
        self._pending["skin_id"] = skin_id
        self._reflect_pending()
        self.preview()

    def change_eink(self, eink: bool) -> None:
        self._pending["eink"] = bool(eink)
        self._reflect_pending()
        self.preview()

    def _reflect_pending(self) -> None:
        """pending → 视图控件回填（皮肤名描述等跟随）。"""
        self._view.set_preferences(
            str(self._pending["skin_id"]),
            str(self._pending["mode"]),
            bool(self._pending["eink"]))

    def preview(self) -> None:
        """按 pending 求值主题数据并交给视图应用（实时预览，不持久化）。"""
        if self._theme is None:
            self._view.set_status("ThemeService 未就绪", error=True)
            return
        try:
            data = self._theme.apply(
                skin_id=str(self._pending["skin_id"]),
                dark_mode=str(self._pending["mode"]),
                eink=bool(self._pending["eink"]))
            self._view.apply_preview(data)
        except Exception as exc:
            # 只记日志等于"点了没反应"：预览失败必须写在状态行上。
            logger.warning("主题预览失败：%s", exc)
            self._view.set_status(f"主题应用失败：{exc}", error=True)

    # ------------------------------------------------------------------ #
    def save(self, _=None) -> None:
        """把 pending 持久化到 SettingsService。"""
        if self._theme is None:
            self._view.set_status("ThemeService 未就绪", error=True)
            return
        try:
            self._theme.set_preferences(
                skin_id=str(self._pending["skin_id"]),
                mode=str(self._pending["mode"]),
                eink=bool(self._pending["eink"]))
            self._view.set_status("外观设置已保存")
        except Exception as exc:
            self._view.set_status(f"保存失败：{exc}", error=True)

    def reset(self, _=None) -> None:
        """恢复默认：pending 重置 + 预览（保存后生效）。"""
        self._pending = dict(_DEFAULTS)
        self._reflect_pending()
        self.preview()
        self._view.set_status("已恢复默认外观（保存后生效）")
