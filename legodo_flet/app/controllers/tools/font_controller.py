"""FontController：字体管理编排（M5，§1.9 / 契约 §8）。

系统字体只读枚举 + 用户字体增删；当前选中字体落 SettingsService 键
``reader.font_family``——阅读器排版偏好读的就是这一个键。
"""
from __future__ import annotations

import logging

import flet as ft

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.tools.font_service import FontService
except Exception:  # pragma: no cover - 并行线未落盘
    class FontService:  # type: ignore[no-redef]
        pass

#: 当前选中字体的 settings 键：与阅读器排版偏好同键，选完正文即生效。
FONT_FAMILY_KEY = "reader.font_family"


class FontController(BaseController):
    """字体：扫描系统 / 列用户字体 / 添加移除 / 选中落库。"""

    destination = "tools.font"

    def on_attach(self) -> None:
        self._svc = self._safe_service(FontService)
        self._view.on_scan_system = self.scan_system
        self._view.on_add_font = self.add_font
        self._view.on_remove_font = self.remove_font
        self._view.on_select = self.select_font
        # 当前选中字体回填（否则进页永远不高亮）。
        getter = getattr(self._svc, "get_selected_family", None)
        self._current = str(getter() or "") if getter is not None else ""
        self._system: list = []
        self._user: list = []
        self.load_fonts()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def load_fonts(self) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        if self._svc is None:
            self._view.set_current("")
            self._view.render_fonts([], [], "")
            return
        self.ui_call(self._load_and_render())

    async def _load_and_render(self) -> None:
        """两条列表在**同一条协程**里先后 await，回来到齐才渲染。

        不能各自 `ui_call` 再在完成回调里 `future.result()` 等另一条：
        `add_done_callback` 跑在界面循环线程上，而另一条 future 的落值正需要这个
        循环——回调一堵，循环就永远腾不出手叫它醒（第七十五轮实测必现锁死，
        栈见 `docs/known-issues/004` 第 16 条）。
        """
        # 两条各兜各的异常：一次读取失败不该让整页空掉（沿用改前 `_safe` 的口径）。
        # 服务调用直接挂在 await 上，是为了过 tests/test_controller_service_surface.py
        # 那条判链——它只认 await / ui_call 这两种紧邻的落点。
        try:
            system = list(await self._svc.scan_system_fonts() or [])
        except Exception as exc:
            logger.warning("系统字体读取失败：%s", exc)
            system = []
        try:
            user = list(await self._svc.list_user_fonts() or [])
        except Exception as exc:
            logger.warning("用户字体读取失败：%s", exc)
            user = []
        self._render(system=system, user=user)

    def _render(self, system: list, user: list) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        self._system, self._user = list(system), list(user)
        self._view.set_current(self._current)
        self._view.render_fonts(self._system, self._user, self._current)

    def scan_system(self) -> None:
        self.load_fonts()

    def add_font(self, _=None) -> None:
        """弹文件选择器 → 选中字体文件交 FontService 落库（旧版是空操作）。"""
        if self._svc is None:
            self._view.set_status("FontService 未就绪")
            return
        picker = getattr(self._view, "file_picker", None)
        if picker is None:
            self._view.set_status("文件选择器未挂载")
            return
        try:
            files_awaitable = picker.pick_files(
                allow_multiple=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["ttf", "otf", "ttc"])
        except Exception as exc:
            self._view.set_status(f"打开文件选择器失败：{exc}")
            return
        self.ui_call(self._add_picked_fonts(files_awaitable))

    async def _add_picked_fonts(self, files_awaitable) -> None:
        try:
            files = await files_awaitable
        except Exception as exc:
            if self._view is not None:
                self._view.set_status(f"读取所选文件失败：{exc}")
            return
        added, failed = 0, []
        for file in files or []:
            path = str(getattr(file, "path", "") or "")
            if not path:
                failed.append("(无路径)")
                continue
            try:
                if await self._svc.add_font_file(path):
                    added += 1
                else:
                    failed.append(path)
            except Exception as exc:
                failed.append(f"{path}: {exc}")
        if self._view is None:
            return    # 逐个装字体要 await 好几轮，页随时可能已经切走
        self._view.set_status(
            f"已添加 {added} 个字体" + (f"；失败：{'、'.join(failed)}" if failed else ""))
        self.load_fonts()

    def remove_font(self, font_id: str) -> None:
        if self._svc is None or font_id in (None, ""):
            return
        future = self.ui_call(self._svc.remove_font(font_id))
        future.add_done_callback(self.on_done(
            lambda f: self._render(system=self._system, user=self._user)))

    def select_font(self, family: str) -> None:
        self._current = family or ""
        setter = getattr(self._svc, "set_selected_family", None)
        if setter is not None:
            try:
                setter(self._current)
            except Exception as exc:
                logger.warning("写入选中字体失败：%s", exc)
        else:
            # 服务无 set_selected_family 时兜底写 settings（同步键值）。
            settings = self._settings()
            if settings is not None:
                try:
                    settings.set(FONT_FAMILY_KEY, self._current)
                except Exception:  # pragma: no cover
                    pass
        self._view.set_current(self._current)
        self._view.render_fonts(self._system, self._user, self._current)

    def _settings(self):
        try:
            from app.core.settings import SettingsService
            return self.service(SettingsService)
        except Exception:
            return None
