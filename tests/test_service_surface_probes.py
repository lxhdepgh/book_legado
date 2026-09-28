"""护栏：控制器探一个**真服务类面上没有**的成员，这一整类形状。

出处（09-27 第二十六轮）：规则编辑器「试跑当前规则」在生产里从没取出过值——控制器
`getattr(self._rule_engine, "extract_values", …)` 探一个 `RuleEngineService` 根本没有的
方法，探不到就回落到 `select_nodes(sample, kind, "")`，而空列表规则一个容器都选不出，
于是任何规则都只回"没取到值"。24 条单测全绿，因为替身自己定义了那个 phantom 方法。

本测试不看替身，只看 `app/services/**` 里的真类：把 `app/controllers/**` 中所有
"经 `self.service(X)` / `self._safe_service(X)` 拿到的句柄" 上的成员访问（含
`getattr`/`hasattr` 的字面量第二参）逐条对真类面核一遍。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS = PROJECT_ROOT / "app" / "controllers"

#: 服务获取器：控制器只会用这几个入口拿服务句柄
SERVICE_GETTERS = ("service", "_safe_service", "require")


def _handle_types(tree: ast.Module) -> dict[str, str]:
    """``self._X = self.service(XService)`` ⇒ ``{"_X": "XService"}``。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        rhs = node.value
        if not (isinstance(target, ast.Attribute)
                and isinstance(rhs, ast.Call)
                and isinstance(rhs.func, ast.Attribute)
                and rhs.func.attr in SERVICE_GETTERS):
            continue
        args = list(rhs.args) + [kw.value for kw in rhs.keywords]
        svc = [a.id for a in args
               if isinstance(a, ast.Name) and a.id.endswith("Service")]
        if svc:
            out[target.attr] = svc[0]
    return out


def _imported_modules(tree: ast.Module) -> dict[str, str]:
    """{类名: 模块点路径}——控制器一律 `from 模块 import 类`。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
    return out


def _members(tree: ast.Module) -> list[tuple[str, str, int, str]]:
    """[(句柄, 成员名, 行号, 形态)]：句柄属于 self，成员是字面量可定的那两种写法。"""
    out: list[tuple[str, str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            base = node.value
            if (isinstance(base, ast.Attribute)
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "self"):
                out.append((base.attr, node.attr, node.lineno, "取属性"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "hasattr")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            base = node.args[0]
            if (isinstance(base, ast.Attribute)
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "self"):
                out.append((base.attr, node.args[1].value, node.lineno,
                            f"{node.func.id} 探测"))
    return out


def _exists(klass, attr: str) -> bool:
    """类面有（方法/property/类属性/继承来的）或实例属性在源码里被赋过值。"""
    if hasattr(klass, attr):
        return True
    try:
        return f"self.{attr}" in inspect.getsource(klass)
    except (OSError, TypeError):     # 取不到源码（冻结打包）时不误报
        return True


def scan_service_probes() -> tuple[list[str], int]:
    """返回 (违规清单, 实际核验过的成员引用条数)。"""
    problems: list[str] = []
    checked = 0
    for path in sorted(CONTROLLERS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        handles = _handle_types(tree)
        if not handles:
            continue
        modules = _imported_modules(tree)
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for handle, attr, lineno, shape in _members(tree):
            cls = handles.get(handle)
            if cls is None:
                continue          # self._view / self._pending / self.service 等
            module = modules.get(cls)
            if module is None:
                problems.append(f"{rel}:{lineno}: 句柄 {handle} 的类型 {cls} "
                                "没有 import 来源，核不了")
                continue
            klass = getattr(importlib.import_module(module), cls, None)
            if klass is None:
                problems.append(f"{rel}:{lineno}: {module}.{cls} 导入不到，核不了")
                continue
            checked += 1
            if not _exists(klass, attr):
                problems.append(f"{rel}:{lineno}: {shape} self.{handle}.{attr} "
                                f"—— 真类 {cls} 面上没有这个成员")
    return problems, checked


class ServiceSurfaceProbeTests(unittest.TestCase):
    def test_no_controller_touches_a_member_the_real_service_lacks(self) -> None:
        problems, checked = scan_service_probes()
        self.assertEqual(problems, [], "控制器探测/访问了真服务面上不存在的成员")

    def test_the_scanner_is_actually_looking_at_something(self) -> None:
        """护栏的护栏：扫描器解析不到东西时上一条会空绿。"""
        problems, checked = scan_service_probes()
        self.assertEqual(problems, [])
        # 本轮实测 150 条；数量级掉了说明句柄识别或形态识别退化了。
        self.assertGreater(checked, 120, f"只核到 {checked} 条成员引用，扫描器该修了")


class _Bus:
    def subscribe(self, event_type, handler):
        return lambda: None

    def publish(self, event):
        return []


class _Hub:
    def __init__(self, services):
        self.bus = _Bus()
        self._services = services

    def get(self, stype):
        return self._services[stype]


class UpdateVersionEndToEndTests(unittest.TestCase):
    """更新页那条实错的正面护栏：**不装替身**走真服务 + 真视图。

    光有上一条静态扫描不够——静态只保证"成员存在"，不保证"装配时传了值"。
    ``main.py`` 注册 ``UpdateService()`` 不传版本时，属性照样在、界面照样空白。
    """

    def test_the_real_service_feeds_the_real_view(self) -> None:
        from app import APP_VERSION
        from app.controllers.tools.update_controller import UpdateController
        from app.services.tools.update_service import UpdateService
        from app.views.tools.update_view import UpdateView

        view = UpdateView()
        controller = UpdateController(
            _Hub({UpdateService: UpdateService(current_version=APP_VERSION)}))
        controller.attach(view)
        shown = str(view._current.value or "")
        self.assertIn(APP_VERSION, shown, f"界面没装上本机版本：{shown!r}")
        self.assertNotIn("未知", shown)
        controller.detach()

    def test_the_app_assembly_registers_that_version(self) -> None:
        """`main._build_hub()` 注册的那一份必须带版本号（不是构造默认空串）。"""
        import os
        import tempfile
        from unittest import mock

        from app import APP_VERSION
        from app.services.tools.update_service import UpdateService

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {
                    "LEGADO_BOOKSOURCE_DATA_DIR": tmp,
                    "LEGADO_BOOKSOURCE_CACHE_DIR": tmp}):
            import main
            hub = main._build_hub()
        svc = hub.get(UpdateService)
        self.assertEqual(svc.current_version, APP_VERSION,
                         "装配时没把本机版本传给 UpdateService，更新页会是空白")


if __name__ == "__main__":
    unittest.main()
