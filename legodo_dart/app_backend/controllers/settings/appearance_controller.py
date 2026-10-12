"""AppearanceController：外观设置页编排（M7 后补全，§3.11 主题皮肤）。

偏好读写经 ThemeService（get_preferences / set_preferences），实时预览经
``apply()`` 取纯数据 ThemeData 交给视图应用为 ft.Theme（服务不持控件）。

预览/保存语义：控件每变更更新 ``self._pending``（内存暂存），预览按 pending
求值即时应用；保存时才 ``set_preferences(**pending)`` 落 SettingsService。
恢复默认只改 pending 并预览，不直接落库。

**背景图不走这套 pending/保存**：换图是"选完就该看到"的一件事，落偏好即生效
（:class:`~app.services.tools.background_service.BackgroundService` 写键 →
装配根的订阅把新图推到导航外壳那一层）。所以"恢复默认"也不动背景——把用户
刚选的图连带清掉是最容易被当成丢数据的意外。

视图零 import 服务：本控制器是唯一桥（红线 §7.5，controllers 允许
import 服务类型作 ``hub.get()`` 参数）。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.theme_service import ThemeService
except Exception:  # pragma: no cover - 服务未落盘时退化
    class ThemeService:  # type: ignore[no-redef]
        pass

try:
    from app.services.tools.background_service import BackgroundService
except Exception:  # pragma: no cover - 服务未落盘时退化
    class BackgroundService:  # type: ignore[no-redef]
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
        self._background = self._safe_service(BackgroundService)
        self._view.on_change_mode = self.change_mode
        self._view.on_change_skin = self.change_skin
        self._view.on_change_eink = self.change_eink
        self._view.on_save = self.save
        self._view.on_reset = self.reset
        self._view.on_pick_background = self.pick_background
        self._view.on_clear_background = self.clear_background
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
        self._view.set_background(self._current_background())
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
        """恢复默认：pending 重置 + 预览（保存后生效）。背景图不动（见模块文档）。"""
        self._pending = dict(_DEFAULTS)
        self._reflect_pending()
        self.preview()
        self._view.set_status("已恢复默认外观（保存后生效；背景图不受影响）")

    # ------------------------------------------------------------------ #
    # 背景图：选完即生效，不进 pending/保存那条线
    # ------------------------------------------------------------------ #
    def _current_background(self) -> str:
        if self._background is None:
            return ""
        try:
            return str(self._background.current or "")
        except Exception as exc:
            logger.warning("读背景图偏好失败：%s", exc)
            return ""

    def pick_background(self, _=None) -> None:
        """弹图片选择器；选中的图交给服务拷进数据目录并立刻铺上去。"""
        if self._background is None:
            self._view.set_status("BackgroundService 未就绪，换不了背景图", error=True)
            return
        picker = getattr(self._view, "image_picker", None)
        if picker is None:
            # 选择器没挂到窗口上（page.services 没接到）时 pick_files 是 None future：
            # 点了就是"转圈转到天荒地老"，必须当场把话说明白。
            self._view.set_status("文件选择器没挂上窗口，换不了背景图", error=True)
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=False, file_type=ft.FilePickerFileType.IMAGE)
        except Exception as exc:
            self._view.set_status(f"打开图片选择器失败：{exc}", error=True)
            return
        self.ui_call(self._install_background(files_awaitable))

    async def _install_background(self, files_awaitable) -> None:
        try:
            picked = await files_awaitable
        except Exception as exc:
            self._view.set_status(f"读取所选图片失败：{exc}", error=True)
            return
        # 空结果 = 用户取消：什么都不写。
        path = str(getattr((picked or [None])[0], "path", "") or "")
        if not path:
            self._view.set_status("已取消（没选图片）")
            return
        try:
            installed = await self._background.install(path)
        except Exception as exc:
            logger.warning("背景图没换上：%s", exc)
            self._view.set_status(f"背景图没换上：{exc}", error=True)
            return
        # 自己读回一次再报成败：install 落了键，但真正铺上去的是装配根那一跳订阅，
        # 服务没起来/键没写进去时说"已换上"就是假话。
        now = self._current_background()
        self._view.set_background(now)
        if now != str(installed):
            self._view.set_status(f"图拷好了但偏好没落上（期望 {installed}，读到 {now}）",
                                  error=True)
            return
        self._view.set_status("背景图已换上（整窗口立刻生效，不用点保存）")

    def clear_background(self, _=None) -> None:
        """去掉背景图：同样立刻生效。"""
        if self._background is None:
            self._view.set_status("BackgroundService 未就绪，去不掉背景图", error=True)
            return
        self.ui_call(self._do_clear_background())

    async def _do_clear_background(self) -> None:
        try:
            await self._background.clear()
        except Exception as exc:
            logger.warning("去掉背景图失败：%s", exc)
            self._view.set_status(f"去掉背景图失败：{exc}", error=True)
            return
        now = self._current_background()
        self._view.set_background(now)
        if now:
            self._view.set_status(f"偏好里还留着图（读到 {now}），没清干净", error=True)
            return
        self._view.set_status("已去掉背景图（立刻生效）")
