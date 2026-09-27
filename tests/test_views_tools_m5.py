"""M5 工具域视图/控制器/ThemeService 外壳化冒烟测试（§3.11/§7.2-7.5/契约 §12/§13）。

不依赖真实 Flet 窗口、网络或并行线未落盘的服务实现：ft 控件可无窗口实例化；
Controller 用 stub hub / 服务替身离线构造；工具服务未注册时控制器退化为"未就绪"。
覆盖：

1. 八视图离线构造；
2. 工具组八目的地注册进 ViewRegistry；
3. 八个 Controller attach/detach 订阅退订对称（§7.4 防泄漏）；
4. ThemeService.apply 默认/墨水屏数据求值；
5. ThemeService 外观持久化并入 SettingsService（appearance.json 不再写断言）；
6. 书源编辑器试跑调试：坏 JSON 同步报错 / 单字段经 RuleEngineService 替身求值；
7. import-lint AST 扫描：``app/views/tools/`` 零 ``app.services.*`` import。
"""
from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

import flet as ft

from app.core.registry import TOOLS_DESTINATIONS, ViewRegistry

from app.views.tools import (BrowserView, DictPopup, ExportView, FontView,
                             QrCodeView, ReplaceRuleView, UpdateView,
                             WebDavView, register_destinations)
from app.controllers.tools.browser_controller import BrowserController
from app.controllers.tools.dict_popup_controller import DictPopupController
from app.controllers.tools.replace_rule_controller import ReplaceRuleController
from app.controllers.tools.font_controller import FontController
from app.controllers.tools.qr_code_controller import QrCodeController
from app.controllers.tools.export_controller import ExportController
from app.controllers.tools.webdav_controller import WebDavController
from app.controllers.tools.update_controller import UpdateController

from app.services.tools.theme_service import (THEME_SETTINGS_KEY, ThemeService,
                                              ThemeData)


# --------------------------------------------------------------------------- #
# 离线替身
# --------------------------------------------------------------------------- #
class _FakeBus:
    """记录订阅的假 EventBus：subscribe 返回幂等退订句柄。"""

    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

        def unsubscribe():
            if (event_type, handler) in self.subscriptions:
                self.subscriptions.remove((event_type, handler))
        return unsubscribe

    def publish(self, event):
        return []


class _FakeHub:
    """按类型返回替身服务句柄；未注册类型抛错，控制器 _safe_service 容错为 None。"""

    def __init__(self, services: dict | None = None):
        self.bus = _FakeBus()
        self._services = services or {}

    def get(self, stype):
        if stype in self._services:
            return self._services[stype]
        raise KeyError(stype)


class _FakeSettings:
    """记录 set() 调用的 SettingsService 替身（内存键值）。"""

    def __init__(self):
        self.store: dict = {}
        self.calls: list = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.calls.append((key, value))
        self.store[key] = value


_VIEW_CLASSES = (BrowserView, DictPopup, ReplaceRuleView, FontView, QrCodeView,
                 ExportView, WebDavView, UpdateView)
_CONTROLLER_VIEWS = (
    (BrowserController, BrowserView),
    (DictPopupController, DictPopup),
    (ReplaceRuleController, ReplaceRuleView),
    (FontController, FontView),
    (QrCodeController, QrCodeView),
    (ExportController, ExportView),
    (WebDavController, WebDavView),
    (UpdateController, UpdateView),
)


class ViewConstructTests(unittest.TestCase):
    """八视图离线可构造。"""

    def test_eight_views_construct_offline(self):
        self.assertEqual(len(_VIEW_CLASSES), 8)
        for cls in _VIEW_CLASSES:
            self.assertIsInstance(cls(), ft.Column, f"{cls.__name__} 应是 ft.Column")


class RegistryWiringTests(unittest.TestCase):
    """工具组八目的地注册进 ViewRegistry，工厂可离线装配视图。"""

    def test_register_tools_destinations(self):
        self.assertEqual(len(TOOLS_DESTINATIONS), 8)
        registry = ViewRegistry()
        hub = _FakeHub()
        register_destinations(registry, hub)
        for dest in TOOLS_DESTINATIONS:
            route = registry.try_get(dest)
            self.assertIsNotNone(route, f"目的地未注册: {dest}")
            self.assertIsInstance(route.factory(), ft.Column)


class ControllerLifecycleTests(unittest.TestCase):
    """attach/detach 订阅退订对称（§7.4 防泄漏）。"""

    def _symmetric(self, controller_cls, view_cls):
        hub = _FakeHub()
        controller = controller_cls(hub)
        controller.attach(view_cls())
        n_after = len(hub.bus.subscriptions)
        controller.detach()
        self.assertEqual(len(hub.bus.subscriptions), 0,
                         f"{controller_cls.__name__} detach 后应全部退订（"
                         f"attach 时订阅 {n_after} 条）")

    def test_browser_symmetric(self):
        self._symmetric(BrowserController, BrowserView)

    def test_dict_symmetric(self):
        self._symmetric(DictPopupController, DictPopup)

    def test_replace_symmetric(self):
        self._symmetric(ReplaceRuleController, ReplaceRuleView)

    def test_font_symmetric(self):
        self._symmetric(FontController, FontView)

    def test_qr_symmetric(self):
        self._symmetric(QrCodeController, QrCodeView)

    def test_export_symmetric(self):
        self._symmetric(ExportController, ExportView)

    def test_webdav_symmetric(self):
        self._symmetric(WebDavController, WebDavView)

    def test_update_symmetric(self):
        self._symmetric(UpdateController, UpdateView)


class ThemeServiceTests(unittest.TestCase):
    """ThemeService 外壳化：纯数据 apply + 持久化并入 SettingsService。"""

    def test_apply_default_and_eink(self):
        svc = ThemeService()
        data = svc.apply()
        self.assertIsInstance(data, ThemeData)
        self.assertEqual(data.skin_id, "classic_blue")
        self.assertFalse(data.eink)
        # 墨水屏：亮暗统一黑白、模式归一 light
        eink = svc.apply(skin_id="ink_gray", eink=True)
        self.assertTrue(eink.eink)
        self.assertEqual(eink.mode, "light")
        self.assertEqual(eink.light["surface"], "#FFFFFF")
        self.assertEqual(eink.light["primary"], "#000000")
        # 非法皮肤回退默认
        fallback = svc.apply(skin_id="no_such_skin")
        self.assertEqual(fallback.skin_id, "classic_blue")

    def test_persists_to_settings_not_appearance_json(self):
        settings = _FakeSettings()
        with tempfile.TemporaryDirectory() as tmp:
            svc = ThemeService(settings=settings, data_dir=tmp)
            svc.set_preferences(skin_id="emerald_green", mode="dark", eink=True)
            # 统一键值写入 SettingsService
            self.assertTrue(any(k == THEME_SETTINGS_KEY for k, _ in settings.calls),
                            "外观偏好必须写入 SettingsService 统一键")
            written = dict(settings.store[THEME_SETTINGS_KEY])
            self.assertEqual(written["skin_id"], "emerald_green")
            self.assertEqual(written["mode"], "dark")
            self.assertTrue(written["eink"])
            # appearance.json 停止写入
            self.assertFalse((Path(tmp) / "appearance.json").exists(),
                             "外壳化后不得再写 appearance.json")


class CodeEditorDebugTests(unittest.TestCase):
    """书源编辑器试跑调试：单字段求值经 RuleEngineService 替身。"""

    def _sync_engine_call(self, ctrl):
        def run(coro):
            fut: concurrent.futures.Future = concurrent.futures.Future()
            try:
                fut.set_result(asyncio.run(coro))
            except BaseException as exc:  # noqa: BLE001 - 回传给渲染回调
                fut.set_exception(exc)
            return fut
        ctrl.ui_call = run

    def test_run_debug_bad_json_sync_error(self):
        from app.views.source.code_editor_view import CodeEditorView
        from app.controllers.source.code_editor_controller import CodeEditorController

        view = CodeEditorView()
        ctrl = CodeEditorController(_FakeHub())
        ctrl.attach(view)
        ctrl.run_debug("{不是合法 JSON")
        self.assertTrue(view._debug_host.visible)
        self.assertIn("JSON", view._debug.value)

    def test_run_debug_single_field_via_stub_engine(self):
        from app.views.source.code_editor_view import CodeEditorView
        from app.controllers.source.code_editor_controller import CodeEditorController

        class _StubEngine:
            async def extract_values(self, rule, html, kind):
                return ["《测试书》" + rule]

        view = CodeEditorView()
        ctrl = CodeEditorController(_FakeHub())
        ctrl.attach(view)
        ctrl._rule_engine = _StubEngine()
        self._sync_engine_call(ctrl)

        source = {"ruleSearch": {"bookNameRule": "div.title@text"}}
        # 第二十三轮起试跑要先有示例正文（本页不联网抓样本），没样本就不调引擎。
        view.set_sample('<div class="title">甲</div>')
        ctrl.run_debug(json.dumps(source, ensure_ascii=False))
        self.assertTrue(view._debug_host.visible)
        self.assertIn("div.title@text", view._debug.value)
        self.assertIn("《测试书》", view._debug.value)


class ImportLintTests(unittest.TestCase):
    """§7.5 import-lint 守门：views/tools 零 app.services.* 命中（AST 扫描）。"""

    _ROOT = Path(__file__).resolve().parent.parent / "app" / "views" / "tools"

    def test_no_services_import_in_tools_views(self):
        offenders = []
        for py in sorted(self._ROOT.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for mod in names:
                    if mod and (mod == "app.services" or mod.startswith("app.services.")):
                        offenders.append(f"{py.name}:{node.lineno}: import {mod}")
        self.assertEqual(
            offenders, [],
            "views/tools/ 的 import 语句不得引用 app.services.*（红线 §7.5）：\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
