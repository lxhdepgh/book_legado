"""ShortcutController：快捷键页编排（读两处偏好、录键、冲突提示、保存）。

**存量在哪**：阅读那七个动作存在 ``reader.shortcuts``（一个字典，走
:meth:`ReadingService.save_reader_preference`），老板键存在 ``bosskey.enabled`` /
``bosskey.shortcut`` 两条标量（走 :class:`BossKeyService`）。界面上是一张表，
落库是两处——所以保存要**分别写**，且读回来时按各自默认值补齐
（:func:`app.core.shortcuts.merge_reader_shortcuts`）。

录键：视图把 ``on_key`` 回调槽交给本控制器，导航外壳在按键那一拍分发进来
（只有本页签是当前视图时收得到）。控制器只认"正在录的那个动作"，其余按键
一律不看——用户在页面上翻别的控件不该被当成录键。

冲突：判定在 :func:`app.core.shortcuts.conflicts`，这里只负责把结果落到行上。
撞键**不拦保存**（Legado 也不拦），但分发顺序是确定的（表内第一个），
所以提示语得把"按下去只有排前面的会动"说清楚，别让人以为两个都会跑。
"""
from __future__ import annotations

import logging

from app.controllers.base import BaseController
from app.core.shortcuts import (BOSS_ACTION, DEFAULT_BOSS_KEY, READER_ACTIONS,
                                chord_of, conflicts,
                                default_reader_shortcuts,
                                merge_reader_shortcuts)

logger = logging.getLogger(__name__)

try:
    from app.services.reading.reading_service import ReadingService
except Exception:  # pragma: no cover - 服务未落盘时退化
    class ReadingService:  # type: ignore[no-redef]
        pass

try:
    from app.services.tools.boss_key_service import BossKeyService
except Exception:  # pragma: no cover
    class BossKeyService:  # type: ignore[no-redef]
        pass

#: 阅读动作 id（保存时按这份切字典，老板键那条不能混进去）。
_READER_IDS = tuple(action.id for action in READER_ACTIONS)


class ShortcutController(BaseController):
    """快捷键：载入 → 暂存 pending → 录键/清除 → 冲突提示 → 保存/恢复默认。"""

    destination = "settings.shortcuts"

    def on_attach(self) -> None:
        self._reading = self._safe_service(ReadingService)
        self._boss = self._safe_service(BossKeyService)
        self._bindings: dict[str, str] = {
            **default_reader_shortcuts(), BOSS_ACTION.id: DEFAULT_BOSS_KEY}
        self._boss_enabled = True
        self._recording = ""
        view = self._view
        view.on_key = self.handle_key
        view.on_start_record = self.start_record
        view.on_cancel_record = self.cancel_record
        view.on_clear = self.clear_binding
        view.on_change_boss_enabled = self.set_boss_enabled
        view.on_save = self.save
        view.on_reset = self.reset
        self.load()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def load(self, _=None) -> None:
        """首屏：老板键同步读，阅读键异步读（读不到就按默认值画）。"""
        self._bindings[BOSS_ACTION.id] = DEFAULT_BOSS_KEY
        self._boss_enabled = True
        if self._boss is not None:
            try:
                self._bindings[BOSS_ACTION.id] = self._boss.shortcut
                self._boss_enabled = bool(self._boss.enabled)
            except Exception as exc:
                logger.warning("读老板键偏好失败：%s", exc)
        self._reflect()
        if self._reading is None:
            self._view.set_status("ReadingService 未就绪，阅读键改不了", error=True)
            return
        self.ui_call(self._load_reader())

    async def _load_reader(self) -> None:
        try:
            saved = await self._reading.get_reader_preference("shortcuts", None)
        except Exception as exc:
            logger.warning("读阅读快捷键偏好失败：%s", exc)
            if self._view is not None:
                self._view.set_status(f"读阅读键位失败：{exc}", error=True)
            return
        if self._view is None:          # await 期间切走了页签
            return
        self._bindings.update(merge_reader_shortcuts(saved))
        self._reflect()

    # ------------------------------------------------------------------ #
    # 录键
    # ------------------------------------------------------------------ #
    def start_record(self, action_id: str) -> None:
        if action_id not in self._bindings:
            return
        self._recording = "" if self._recording == action_id else action_id
        self._reflect()

    def cancel_record(self, _=None) -> None:
        self._recording = ""
        self._reflect()

    def handle_key(self, event) -> None:
        """录制中的那一拍：把按键落成绑定；空串（纯修饰键）不收，继续等。"""
        if not self._recording:
            return
        chord = chord_of(event)
        if not chord:
            return                      # 只按住 Ctrl/Shift：还不算一个键位
        action_id = self._recording
        self._recording = ""
        self._bindings[action_id] = chord
        self._reflect()
        peers = conflicts(self._scoped(self._bindings)).get(action_id) or []
        if peers:
            labels = {a.id: a.label for a in (*READER_ACTIONS, BOSS_ACTION)}
            self._view.set_status(
                f"「{labels[action_id]}」与"
                + "、".join(f"「{labels[p]}」" for p in peers)
                + "撞了同一个键位", error=True)

    def clear_binding(self, action_id: str) -> None:
        if action_id in self._bindings:
            self._bindings[action_id] = ""
            self._reflect()

    def set_boss_enabled(self, enabled: bool) -> None:
        self._boss_enabled = bool(enabled)
        self._reflect()

    # ------------------------------------------------------------------ #
    def _scoped(self, bindings: dict[str, str]) -> dict[str, str]:
        """本页的键位表（老板键也当一条，好让它与阅读键的互相撞键被画出来）。

        两条分发链本来是分开的（老板键命中就不再往下发），但它若与某个阅读键
        同串，那个阅读键在正文页永远不响——这算撞，必须显示。
        """
        scoped = {k: v for k, v in bindings.items() if k in _READER_IDS}
        scoped[BOSS_ACTION.id] = str(bindings.get(BOSS_ACTION.id) or "")
        return scoped

    # ------------------------------------------------------------------ #
    def _reflect(self) -> None:
        view = self._view
        if view is None:
            return
        view.set_bindings(self._bindings,
                          conflicts(self._scoped(self._bindings)),
                          recording=self._recording)
        view.set_recording(self._recording)
        view.set_boss_enabled(self._boss_enabled)
        available = True
        status = ""
        if self._boss is None:
            available = False
            status = "BossKeyService 未注册，老板键不会生效"
        else:
            try:
                available = bool(self._boss.available)
                status = str(self._boss.status_text)
            except Exception as exc:
                available = False
                status = f"读老板键状态失败：{exc}"
        view.set_boss_status(status, unavailable=not available)

    # ------------------------------------------------------------------ #
    def save(self, _=None) -> None:
        """两处分别落库；任何一处失败都写在状态行上，不只记日志。"""
        self.ui_call(self._save())

    async def _save(self) -> None:
        errors: list[str] = []
        reader_part = {k: self._bindings.get(k, "") for k in _READER_IDS}
        if self._reading is None:
            errors.append("ReadingService 未就绪")
        else:
            try:
                await self._reading.save_reader_preference("shortcuts", reader_part)
            except Exception as exc:
                logger.warning("保存阅读快捷键失败：%s", exc)
                errors.append(f"阅读键：{exc}")
        if self._boss is None:
            errors.append("BossKeyService 未就绪")
        else:
            try:
                self._boss.set_preferences(
                    enabled=self._boss_enabled,
                    shortcut=self._bindings.get(BOSS_ACTION.id, ""))
                if self._boss.error:
                    errors.append(self._boss.error)
            except Exception as exc:
                logger.warning("保存老板键偏好失败：%s", exc)
                errors.append(f"老板键：{exc}")
        if self._view is None:
            return
        if errors:
            self._view.set_status("保存不完整：" + "；".join(errors), error=True)
        else:
            self._view.set_status("快捷键已保存并应用")

    def reset(self, _=None) -> None:
        """恢复默认：只改 pending（与外观页同一套语义，保存后才落库）。"""
        self._bindings = {**default_reader_shortcuts(),
                          BOSS_ACTION.id: DEFAULT_BOSS_KEY}
        self._boss_enabled = True
        self._recording = ""
        self._reflect()
        self._view.set_status("已恢复默认键位（保存后生效）")
