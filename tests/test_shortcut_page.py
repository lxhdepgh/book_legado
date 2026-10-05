"""第七十三轮：快捷键页 + 老板键（键位表、分发、藏窗/回窗、装配接线）。

要钉住的四件事：

1. **旧存量不许失效**：``reader.shortcuts`` 里存的是 ``left``/``pagedown`` 这类
   裸键，页面新录的是组合串；共用的规范化表一旦在两处各写一份，表现就是
   "页面上显示对了、按下去什么都不动"（本条用真分发跑一遍，不查字符串）。
2. **图标先上、窗口后藏**：藏了窗口而托盘没挂上＝程序"消失"，用户只会强杀，
   阅读进度全丢。所以顺序是断言的重点，不是注释。
3. **``to_front`` 是协程，而且发之前要过"还不还回窗口"那道关**：0.86 上裸调用只会得到
   "coroutine was never awaited"，窗口一动没动；真发出去又**晚于**其后那条 ``visible=False``
   落地——连按两下时窗口会赖在桌面上（本文件最后一条用 0 秒间隔复现它）。
4. **装配根的那条链**：``key_filter`` 命中时不许再往当前视图分发（老板键按下去
   既藏窗又翻了页＝灾难）；``page.on_keyboard_event`` 必须真的接到组合链。

替身纪律：``_FakeTray``/``_FakeBoss`` 都是**被测类的子类**（真方法都在，
拼错属性名会炸而不是被假字段盖住）；``_FakePage`` 只给 ``window``/``update``
两个成员，多一个都不给。
"""
from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import flet as ft

from app.controllers.reader_controller import ReaderController
from app.controllers.settings.shortcut_controller import ShortcutController
from app.core import shortcuts as sc
from app.core.registry import (DEST_SETTINGS_SHORTCUTS, GROUP_CONFIG, NAV_META,
                               ViewRegistry)
from app.services.reading.bookmark_service import BookmarkService
from app.services.reading.content_search import ContentSearchService
from app.services.reading.reading_service import ReadingService
from app.services.tools.boss_key_service import _FRONT_DELAY, BossKeyService
from app.services.tools.win_tray import WinTray
from app.views.reading.reader_view import ReaderView
from app.views.settings.shortcut_view import ShortcutView


def _settled(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _key(chord: str, **mods) -> SimpleNamespace:
    """一条 flet KeyboardEvent 的形状：``key`` + 四个修饰键布尔。"""
    return SimpleNamespace(key=chord, ctrl=bool(mods.get("ctrl")),
                           alt=bool(mods.get("alt")),
                           shift=bool(mods.get("shift")),
                           meta=bool(mods.get("meta")))


# --------------------------------------------------------------------------- #
# 1. 键表（纯函数）
# --------------------------------------------------------------------------- #
class ShortcutModelTests(unittest.TestCase):
    def test_defaults_cover_every_action(self):
        self.assertEqual(sorted(a.id for a in sc.READER_ACTIONS),
                         sorted(sc.DEFAULT_READER_SHORTCUTS))

    def test_legacy_bare_keys_still_match(self):
        """库里存过的是裸键：规范化必须原样落回同一个动作（不迁移存量）。"""
        for action_id, binding in sc.DEFAULT_READER_SHORTCUTS.items():
            self.assertEqual(
                action_id, sc.action_for(sc.parse_binding(binding),
                                         sc.DEFAULT_READER_SHORTCUTS),
                f"存量 {binding!r} 认不出自己的动作了")

    def test_underscored_names_are_one_token(self):
        """``page_down`` 是**一个**键名，按下划线拆段会把它落成 "down"。"""
        self.assertEqual("pagedown", sc.parse_binding("page_down"))
        self.assertEqual("pagedown", sc.parse_binding("Page Down"))
        self.assertEqual("pagedown", sc.parse_binding("pagedown"))

    def test_modifiers_carry_into_the_chord(self):
        self.assertEqual("ctrl+alt+p", sc.chord_of(_key("P", ctrl=True, alt=True)))
        self.assertEqual("ctrl+shift+left",
                         sc.chord_of(_key("Arrow Left", ctrl=True, shift=True)))

    def test_bare_modifier_press_is_not_a_binding(self):
        """单独按住 Ctrl 那一拍不成套：录键时得继续等，分发时谁都不许动。"""
        self.assertEqual("", sc.chord_of(_key("Control Left")))
        self.assertEqual("", sc.action_for("", sc.DEFAULT_READER_SHORTCUTS))

    def test_modifier_order_is_canonical(self):
        self.assertEqual(sc.parse_binding("alt+ctrl+h"), sc.parse_binding("h+ctrl+alt"))
        self.assertEqual("ctrl+alt+h", sc.parse_binding("alt+h+ctrl"))

    def test_display_and_parse_round_trip(self):
        """念出来的写法必须能读回同一个绑定：中文那几条（"逗号(,)"）与 "Win" 都在这条上钉。"""
        for binding in (*sc.DEFAULT_READER_SHORTCUTS.values(),
                        "ctrl+alt+pageup", sc.DEFAULT_BOSS_KEY,
                        "space", "up", "shift+del", "meta+p"):
            shown = sc.format_binding(binding)
            self.assertEqual(binding, sc.parse_binding(shown),
                             f"「{shown}」写回库里就变了：{binding!r}")

    def test_cleared_binding_never_fires_nor_conflicts(self):
        """清空的行不参与分发也不报撞键：允许用户把不想用的动作全部关掉。"""
        bindings = {"next": "", "previous": ""}
        self.assertEqual("", sc.action_for("left", bindings))    # 空绑定不算命中
        self.assertEqual("", sc.action_for("", bindings))        # 空串输入也不命中
        self.assertEqual({}, sc.conflicts(bindings))

    def test_conflicts_reports_both_sides(self):
        got = sc.conflicts({"next": "right", "previous": "right", "close": "escape"})
        self.assertEqual({"next": ["previous"], "previous": ["next"]}, got)

    def test_unknown_saved_keys_are_dropped(self):
        merged = sc.merge_reader_shortcuts(
            {"next": "j", "typo_action": "k", "page_up": "page_down"})
        self.assertEqual("j", merged["next"])
        self.assertEqual("pagedown", merged["page_up"])
        self.assertNotIn("typo_action", merged)
        self.assertEqual("escape", merged["close"])       # 没存过的补默认


# --------------------------------------------------------------------------- #
# 2. 阅读器的分发（真视图 + 真控制器，只是不联网）
# --------------------------------------------------------------------------- #
class _RecordingReaderView(ReaderView):
    """只记"翻页/关面板"这三下，排版那套一概不碰。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []

    def scroll_by(self, delta) -> None:
        self.events.append(("scroll", delta))

    def show_directory(self, flag) -> None:
        self.events.append(("directory", flag))

    def show_side_panel(self, name) -> None:
        self.events.append(("panel", name))


class _FakeBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, event):
        return []


class _FakeHub:
    def __init__(self, services):
        self.bus = _FakeBus()
        self._services = services

    def get(self, stype):
        return self._services[stype]


class _RecordingReaderController(ReaderController):
    def __init__(self, hub) -> None:
        super().__init__(hub)
        self.fired: list[str] = []

    def open_directory(self):
        self.fired.append("directory")

    def prev_chapter(self):
        self.fired.append("previous")

    def next_chapter(self):
        self.fired.append("next")

    def open_settings(self):
        self.fired.append("settings")

    def close_settings(self):
        self.fired.append("close")


class ReaderDispatchTests(unittest.TestCase):
    """每条默认键位都得真的落到动作上——页面显示对了但按下去不动，就是这张表裂了。"""

    def _rig(self):
        reading = SimpleNamespace(
            get_reader_preference=AsyncMock(return_value=None),
            save_reader_preference=AsyncMock(),
            stop_prefetch=lambda: None)
        view = _RecordingReaderView()
        # 阅读器控制器的 on_attach 对这三样是**硬取**（没有就 KeyError），
        # 可选形态才走 _safe_service——替身表得按这条边界给齐。
        controller = _RecordingReaderController(_FakeHub({
            ReadingService: reading,
            BookmarkService: SimpleNamespace(
                list_bookmarks=AsyncMock(return_value=[]),
                add_bookmark=AsyncMock(return_value=1),
                remove_bookmark=AsyncMock(return_value=True)),
            ContentSearchService: SimpleNamespace(
                search=AsyncMock(return_value=[]),
                ensure_index=AsyncMock(return_value=0)),
        }))
        controller.attach(view)
        controller._shortcuts = sc.default_reader_shortcuts()
        return view, controller

    def test_every_default_key_fires_its_action(self):
        seen: dict[str, list] = {}
        for action_id, binding in sc.DEFAULT_READER_SHORTCUTS.items():
            view, controller = self._rig()
            try:
                controller._on_key(_key(binding))
                seen[action_id] = [*controller.fired, *view.events]
            finally:
                controller.detach()
        for action_id, events in seen.items():
            self.assertTrue(events, f"默认键位 {action_id} 按下去什么都没发生")

    def test_chord_binding_fires(self):
        view, controller = self._rig()
        try:
            controller._shortcuts["next"] = "ctrl+alt+p"
            controller._on_key(_key("p", ctrl=True, alt=True))
            self.assertEqual(["next"], controller.fired)
            controller._on_key(_key("p"))          # 没按修饰键：不算命中
            self.assertEqual(["next"], controller.fired)
        finally:
            controller.detach()

    def test_boss_key_does_not_move_the_reader(self):
        """老板键命中的那一拍在装配根就被吃掉了；万一漏进阅读器，也不许翻章。"""
        view, controller = self._rig()
        try:
            controller._on_key(_key("h", ctrl=True, alt=True))
            self.assertEqual([], controller.fired)
            self.assertEqual([], view.events)
            self.assertNotIn(sc.DEFAULT_BOSS_KEY,
                             controller._shortcuts.values())
        finally:
            controller.detach()

    def test_unbound_action_fires_nothing(self):
        view, controller = self._rig()
        try:
            controller._shortcuts["next"] = ""
            controller._on_key(_key("right"))
            self.assertEqual([], controller.fired)
            self.assertEqual(["previous"], _once(controller, "left"))
        finally:
            controller.detach()


def _once(controller, binding: str) -> list[str]:
    controller.fired.clear()
    controller._on_key(_key(binding))
    return list(controller.fired)


# --------------------------------------------------------------------------- #
# 3. 快捷键页（视图 + 控制器）
# --------------------------------------------------------------------------- #
class _FakeBoss(BossKeyService):
    """老板键服务的替身：只把偏好与可用性改成可控，方法面还是真服务那一套。"""

    def __init__(self, *, enabled=True, shortcut="ctrl+alt+h", available=True,
                 status="当前绑定：Ctrl + Alt + H"):
        super().__init__()
        self._enabled = enabled
        self._shortcut = shortcut
        self._available = available
        self._status = status
        self.saved: list[tuple] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def shortcut(self) -> str:
        return self._shortcut

    @property
    def available(self) -> bool:
        return self._available

    @property
    def status_text(self) -> str:
        return self._status

    def set_preferences(self, *, enabled=None, shortcut=None) -> None:
        self.saved.append((enabled, shortcut))
        if shortcut is not None:
            self._shortcut = sc.parse_binding(shortcut)


def _page_rig(boss=None, preferences=None):
    reading = SimpleNamespace(
        get_reader_preference=AsyncMock(
            side_effect=lambda key, default=None: (preferences or {}).get(key, default)),
        save_reader_preference=AsyncMock())
    view = ShortcutView()
    hub_services = {ReadingService: reading}
    if boss is not None:
        hub_services[BossKeyService] = boss
    controller = ShortcutController(_FakeHub(hub_services))
    # 编排协程在本文件里**同步跑完**：真 ui_call 会把它排到另一条循环上，那么
    # "载入阅读键位"就可能在用例改完 pending、甚至点完保存之后才落回来——
    # 断言变成赛跑，赢的是默认值。同步了才测得出"保存写的是哪两处"。
    controller.ui_call = lambda coro: asyncio.run(controller._guard(coro))
    controller.attach(view)
    return view, controller, reading


class ShortcutPageTests(unittest.TestCase):
    def test_page_has_one_row_per_action(self):
        view = ShortcutView()
        self.assertEqual(
            sorted([a.id for a in sc.READER_ACTIONS] + [sc.BOSS_ACTION.id]),
            sorted(view._rows))

    def test_record_then_key_press_binds(self):
        boss = _FakeBoss()
        view, controller, _ = _page_rig(boss)
        controller.start_record("next")
        self.assertIn("正在录", view._recording_hint.value)
        controller.handle_key(_key("j"))
        self.assertEqual("j", controller._bindings["next"])
        self.assertEqual("J", view._rows["next"]["binding"].value)
        self.assertEqual("", controller._recording)

    def test_bare_modifier_keeps_recording(self):
        view, controller, _ = _page_rig(_FakeBoss())
        controller.start_record("next")
        controller.handle_key(_key("Control Left"))
        self.assertEqual("next", controller._recording)
        self.assertEqual("right", controller._bindings["next"])   # 原绑定没被吃掉

    def test_recording_toggles_off_on_the_same_row(self):
        view, controller, _ = _page_rig(_FakeBoss())
        controller.start_record("next")
        controller.start_record("next")
        self.assertEqual("", controller._recording)

    def test_esc_is_recordable(self):
        """取消靠按钮不吞 Esc：阅读器默认就是把"退出阅读"绑在 Esc 上。"""
        view, controller, _ = _page_rig(_FakeBoss())
        controller.start_record("next")
        controller.handle_key(_key("Escape"))
        self.assertEqual("escape", controller._bindings["next"])

    def test_conflict_shows_on_both_rows(self):
        view, controller, _ = _page_rig(_FakeBoss())
        controller.start_record("next")
        controller.handle_key(_key("left"))          # 与"上一章"撞
        self.assertIn("上一章", view._rows["next"]["conflict"].value)
        self.assertIn("下一章", view._rows["previous"]["conflict"].value)
        self.assertIn("撞了同一个键位", view._status.value)

    def test_boss_key_clashing_a_reader_key_is_shown(self):
        view, controller, _ = _page_rig(_FakeBoss(shortcut="f"))
        self.assertIn("章节目录", view._rows["boss_key"]["conflict"].value)
        self.assertIn("老板键", view._rows["directory"]["conflict"].value)

    def test_save_writes_two_stores_separately(self):
        boss = _FakeBoss()
        view, controller, reading = _page_rig(boss)
        controller._bindings["next"] = "j"
        controller._bindings["boss_key"] = "ctrl+alt+j"
        controller._boss_enabled = False
        controller.save()
        self.assertTrue(_settled(
            lambda: bool(reading.save_reader_preference.await_args)
            and bool(boss.saved)))
        key, payload = reading.save_reader_preference.await_args.args
        self.assertEqual("shortcuts", key)
        self.assertNotIn("boss_key", payload, "老板键混进阅读字典了")
        self.assertEqual(sorted(sc.DEFAULT_READER_SHORTCUTS), sorted(payload))
        self.assertEqual("j", payload["next"])
        self.assertEqual((False, "ctrl+alt+j"), boss.saved[-1])

    def test_reset_does_not_touch_the_store(self):
        boss = _FakeBoss()
        view, controller, reading = _page_rig(boss)
        controller.clear_binding("next")
        self.assertEqual("", controller._bindings["next"])
        self.assertEqual("未设置", view._rows["next"]["binding"].value)
        controller.reset()
        self.assertEqual("right", controller._bindings["next"])
        self.assertEqual(0, reading.save_reader_preference.await_count)
        self.assertEqual([], boss.saved)
        self.assertIn("保存后生效", view._status.value)

    def test_unusable_boss_key_says_so_on_the_page(self):
        view, controller, _ = _page_rig(_FakeBoss(available=False,
                                                  status="本机没有托盘能力"))
        self.assertIn("没有托盘能力", view._boss_hint.value)
        self.assertEqual(ft.Colors.RED_300, view._boss_hint.color)
        self.assertFalse(view._rows["boss_key"]["clear"].disabled)

    def test_missing_services_are_reported_not_swallowed(self):
        view, controller, _reading = _page_rig()      # 没有 BossKeyService
        self.assertIn("BossKeyService", view._boss_hint.value)
        controller.start_record("next")
        controller.handle_key(_key("k"))
        self.assertEqual("k", controller._bindings["next"])   # 阅读键那半边照常用

    def test_switch_stays_clickable_after_being_turned_off(self):
        view, controller, _ = _page_rig(_FakeBoss())
        controller.set_boss_enabled(False)
        view.set_boss_enabled(False)
        self.assertFalse(view._boss_switch.disabled)


# --------------------------------------------------------------------------- #
# 4. 老板键服务：顺序、降级、回调编组
# --------------------------------------------------------------------------- #
class _FakeTray(WinTray):
    """托盘替身：把 start/stop 记进一条共享 sink（顺序才是这条功能的重点）。

    子类而不是鸭子对象：真类的 ``ok/error/_activate`` 都在，控制器拼错属性名
    会当场炸，不会被假字段盖住。
    """

    def __init__(self, tooltip="t", on_activate=None, *, ok=True, error="",
                 sink: list[str] | None = None):
        super().__init__(tooltip, on_activate or (lambda: None))
        self.sink: list[str] = sink if sink is not None else []
        self._ok = ok
        self._error = error

    def start(self) -> bool:
        self.sink.append("tray_start")
        return self._ok

    def stop(self) -> None:
        self.sink.append("tray_stop")

    def activate(self) -> None:
        """模拟通知区那一下点击（真实现里由 Win32 回调线程调 :meth:`_activate`）。"""
        self._activate()


def _bound(tray: _FakeTray, callback) -> _FakeTray:
    """把服务传给工厂的回调接到替身的 ``_on_activate``（真类自己的字段）上。

    不接这一线，"点图标回来"的用例只会调用一个构造时塞进去的空函数，服务那边
    注册的回调有没有人接、接的是谁，全都照不出来。
    """
    tray._on_activate = callback
    return tray


class _FakeWindow:
    """``visible`` 的写入也记进同一条 sink：藏窗与挂图标谁先谁后要能读出来。"""

    def __init__(self, sink: list[str] | None = None):
        self._sink = sink if sink is not None else []
        self._visible = True
        self.brought_front = False

    @property
    def visible(self) -> bool:
        return self._visible

    @visible.setter
    def visible(self, value) -> None:
        self._visible = bool(value)
        self._sink.append("visible=True" if value else "visible=False")

    async def to_front(self):
        self.brought_front = True
        # 真客户端的 toFront 会把窗口重新 ShowWindow(SW_SHOW)——桌面探针量到它**晚于**
        # 其后那条 visible=False 落地。替身要是不亮回来，"晚到的 to_front 把窗口叫回来"
        # 这一族就在这里测不到。
        self._visible = True


class _FakePage:
    """只给被测代码真正会碰的两样：``window`` 与 ``update()``。"""

    def __init__(self, sink: list[str] | None = None):
        self.sink: list[str] = sink if sink is not None else []
        self.window = _FakeWindow(self.sink)
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class BossKeyServiceTests(unittest.TestCase):
    def _service(self, page, tray, *, enabled=True, shortcut=sc.DEFAULT_BOSS_KEY):
        service = BossKeyService(
            # 工厂签名里传进来的那条回调才是"点图标回来"的真线：接到替身的
            # _on_activate 上（真类的字段，不是新造的假字段），托盘那一下才测得到。
            tray_factory=lambda tip, cb: _bound(tray, cb))
        service.set_page_provider(lambda: page)
        service._settings = SimpleNamespace(
            get=lambda key, default=None: {
                sc.BOSS_KEY_ENABLED: enabled,
                sc.BOSS_KEY_SHORTCUT: shortcut,
            }.get(key, default),
            set=lambda key, value: None)
        return service

    def test_unmatched_chord_does_nothing(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            self.assertFalse(service.handle_key(_key("f")))
        self.assertEqual([], page.sink)
        self.assertTrue(page.window.visible)

    def test_icon_goes_up_before_the_window_goes_down(self):
        """顺序就是这条功能的命：图标先上、窗口后藏（写序的那次改动由变异核验钉）。"""
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            self.assertTrue(service.handle_key(_key("h", ctrl=True, alt=True)))
        self.assertEqual(["tray_start", "visible=False"], page.sink)
        self.assertFalse(page.window.visible)
        self.assertTrue(service.hidden)

    def test_no_icon_means_no_hide(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink, ok=False,
                                                error="没有通知区"))
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            self.assertFalse(service.handle_key(_key("h", ctrl=True, alt=True)))
        self.assertTrue(page.window.visible, "图标没挂上还是把窗口藏了＝回不来")
        self.assertFalse(service.hidden)
        self.assertIn("托盘图标挂不上", service.error)
        self.assertEqual(["tray_start"], page.sink)

    def test_disabled_never_hides(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink), enabled=False)
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            self.assertFalse(service.handle_key(_key("h", ctrl=True, alt=True)))
        self.assertEqual([], page.sink)

    def test_non_windows_is_unavailable_and_says_why(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))
        with patch.object(WinTray, "available", staticmethod(lambda: False)):
            self.assertFalse(service.available)
            self.assertIn("没有托盘能力", service.status_text)
            self.assertFalse(service.handle_key(_key("h", ctrl=True, alt=True)))
        self.assertTrue(page.window.visible)
        self.assertEqual([], page.sink)

    def test_restore_shows_window_before_dropping_the_icon(self):
        """回窗与摘图标的顺序反过来就是"图标先没、窗口还没回来"——看着像程序死了。"""
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            service.handle_key(_key("h", ctrl=True, alt=True))
        page.sink.clear()
        service.restore()
        self.assertTrue(page.window.visible)
        self.assertEqual(["visible=True", "tray_stop"], page.sink)
        self.assertFalse(service.hidden)

    def test_tray_callback_comes_back_and_restores(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            service.handle_key(_key("h", ctrl=True, alt=True))
        self.assertTrue(page.window.visible is False)
        page.sink.clear()
        tray = service._tray
        tray.activate()                     # 托盘线程上那一下"点击"
        # 回调要编组回界面循环，本机上没有那条循环就就地跑：两种都算回来，只等结果。
        self.assertTrue(_settled(lambda: page.window.visible))
        self.assertFalse(service.hidden)
        self.assertEqual(["visible=True", "tray_stop"], page.sink)

    def test_to_front_is_awaited_not_called_bare(self):
        """0.86 的 ``window.to_front()`` 是协程：裸调用只会得到 never-awaited 警告。"""
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))

        async def scenario():
            with patch.object(WinTray, "available", staticmethod(lambda: True)):
                service.handle_key(_key("h", ctrl=True, alt=True))
            service.restore()
            await asyncio.sleep(_FRONT_DELAY + 0.05)   # 抢前台是等过那道关才发的
            return page.window.brought_front

        self.assertTrue(asyncio.run(scenario()))

    def test_a_late_to_front_does_not_undo_a_hide(self):
        """连按两下（回窗后立刻再藏）：那道抢前台的 toFront **根本不许发**。

        桌面探针 0 秒间隔那一臂复现的形状是"0.05 藏、0.09 起一路亮"，而 ``hidden``
        仍是 True——用户看到的就是"按了老板键，窗口闪一下又赖着不走"，且此后每一拍
        :meth:`BossKeyService.handle_key` 都因为标志说藏着而直接 return。
        第一版修法是"to_front 落地后补一次藏"，实测**没修掉**：那条协程在消息发出去时
        就完成了，补的藏排在真 ShowWindow 之前（改后重跑 0.23 起一路亮）。所以钉的是
        发之前把关：到点时标志还说藏着，就不许再碰 toFront。
        """
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink))

        async def scenario():
            with patch.object(WinTray, "available", staticmethod(lambda: True)):
                service.handle_key(_key("h", ctrl=True, alt=True))
                service.restore()                                     # 排了那道抢前台，还没到点
                service.handle_key(_key("h", ctrl=True, alt=True))    # 用户立刻再按一次
                await asyncio.sleep(_FRONT_DELAY + 0.05)
            return service.hidden, page.window.visible, page.window.brought_front

        hidden, visible, front = asyncio.run(scenario())
        self.assertTrue(hidden)
        self.assertFalse(front, "藏窗期间还把窗口往前提＝那条 toFront 没把关")
        self.assertFalse(visible, "晚到的 toFront 把刚藏起来的窗口又叫亮了")

    def test_custom_shortcut_is_honoured(self):
        page = _FakePage()
        service = self._service(page, _FakeTray(sink=page.sink),
                                shortcut="Ctrl+Alt+B")
        with patch.object(WinTray, "available", staticmethod(lambda: True)):
            self.assertFalse(service.handle_key(_key("h", ctrl=True, alt=True)))
            self.assertTrue(service.handle_key(_key("b", ctrl=True, alt=True)))
        self.assertTrue(service.hidden)
        self.assertEqual(["tray_start", "visible=False"], page.sink)


# --------------------------------------------------------------------------- #
# 5. 目的地与装配根
# --------------------------------------------------------------------------- #
class ShortcutDestinationTests(unittest.TestCase):
    def test_it_is_a_tab_of_the_config_group(self):
        self.assertEqual((GROUP_CONFIG, "快捷键", False), NAV_META[DEST_SETTINGS_SHORTCUTS])

    def test_the_config_group_lists_it_between_appearance_and_services(self):
        from app.core.registry import (DEST_SETTINGS_APPEARANCE,
                                       DEST_SETTINGS_DASHBOARD,
                                       DEST_TOOLS_OVERVIEW)
        registry = ViewRegistry()
        # 全量目的地都挂号：attach_navigation_meta 对"meta 里有、注册表里没有"
        # 是当场报错的，只挂这四条会先撞在别的目的地上面。
        for dest in NAV_META:
            registry.register(dest, lambda: None)
        registry.attach_navigation_meta()
        grouped = dict(registry.grouped_destinations())
        self.assertEqual([DEST_TOOLS_OVERVIEW, DEST_SETTINGS_APPEARANCE,
                          DEST_SETTINGS_SHORTCUTS, DEST_SETTINGS_DASHBOARD],
                         [dest for dest, _t in grouped[GROUP_CONFIG]])


class _ShellBus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, event):
        return []


class _ShellHub:
    def __init__(self):
        self.bus = _ShellBus()

    def get(self, stype):
        raise KeyError(stype)


class _ShellPage:
    def __init__(self):
        self.controls: list = []
        self.title = ""
        self.on_keyboard_event = None
        self.on_resize = None

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass


class AppShellKeyFilterTests(unittest.TestCase):
    """``key_filter`` 必须接在视图分发**之前**，而且要真的挂上 on_keyboard_event。"""

    def setUp(self):
        import app.views.navigation as nav
        self._saved_page = nav._APP_PAGE

    def tearDown(self):
        import app.views.navigation as nav
        nav._APP_PAGE = self._saved_page

    def _build(self, key_filter):
        import app.views.navigation as nav
        from app.views.navigation import build_app_shell
        page = _ShellPage()
        captured = {}
        real = nav.NavigationHost

        class Recording(real):
            def __init__(self, host_page, registry, **kwargs):
                captured.update(kwargs)
                captured["host"] = self
                super().__init__(host_page, registry, **kwargs)

        nav.NavigationHost = Recording
        try:
            build_app_shell(page, _ShellHub(), key_filter=key_filter)
        finally:
            nav.NavigationHost = real
        return page, captured["host"]

    def test_filter_sees_the_event_itself(self):
        """闸门拿到的就是那条原始按键事件（外壳不许在半路改写它）。

        这里不测"闸门有没有被传进 NavigationHost"——它压根不是宿主的参数，接线
        在 :func:`build_app_shell` 的那个闭包里，所以能用得上的证据只有行为那两条
        （见下面 consumed / unconsumed）。
        """
        seen: list = []
        page, _host = self._build(
            lambda event: seen.append(event) or False)
        event = _key("f")
        page.on_keyboard_event(event)
        self.assertEqual([event], seen)

    def test_consumed_key_never_reaches_the_view(self):
        """命中老板键那一拍被吃掉：既藏窗又翻页＝灾难。

        这里钉的是**闸门与分发之间那一段**（把 ``_on_keyboard_event`` 换成记录器），
        而不是某个真视图收不收得到——真视图有没有 ``on_key`` 由
        ``tests/test_views_navigation_m6.py`` 那侧钉；混在一起只会让这条在视图
        起不来时悄悄变成"什么都没发生，所以通过"。
        """
        fired: list = []
        page, host = self._build(lambda event: True)
        host._on_keyboard_event = lambda event: fired.append(event)
        page.on_keyboard_event(_key("h", ctrl=True, alt=True))
        self.assertEqual([], fired)

    def test_unconsumed_key_still_reaches_dispatch(self):
        """反面那半：闸门说"不吃"时不许顺手把阅读快捷键一起关掉（对照臂，非跳过）。"""
        fired: list = []
        page, host = self._build(lambda event: False)
        host._on_keyboard_event = lambda event: fired.append(event)
        page.on_keyboard_event(_key("f"))
        self.assertEqual(1, len(fired))

    def test_without_a_filter_the_view_still_gets_keys(self):
        """没接老板键（main 之外入口/非 Windows）时按键链照旧一路到分发。"""
        fired: list = []
        page, host = self._build(None)
        host._on_keyboard_event = lambda event: fired.append(event)
        page.on_keyboard_event(_key("f"))
        self.assertEqual(1, len(fired))


if __name__ == "__main__":
    unittest.main()
