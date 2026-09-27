"""M7 红线 v2 守门：新结构 import-lint（AST 静态扫描，零运行时依赖）。

旧版 test_ui_safety.py 断言的是已删除的旧单体 UI（ManageView / CheckView /
ui_feedback 等），随 M7 删除冻结文件一并失效。按集成裁决重写为对新架构的红线检查：

红线 #6（设计 §5.3）：任何服务不得跑在 Flet 事件循环、不得持有 Flet 控件；
服务触 UI 仅经 BrowserService 的交互回调解耦。落地为两条可机械执行的 import-lint：

1. ``app/views/**`` 零 ``app.services.*`` 直接 import —— 视图是轻壳，经 Controller
   取服务句柄，不得直连业务服务实现（§7.5）。
2. ``app/services/**`` 中 ``import flet`` 只允许出现在白名单
   ``{web_page_renderer.py}`` —— 它是 BrowserService 唯一被授权的 UI 桥接点
   （in-app WebView / 投屏 / 验证码对话框），其余服务外壳一律不得引入 flet。

任何新增违规 import 都会让本测试失败。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SERVICES = _ROOT / "app" / "services"
_VIEWS = _ROOT / "app" / "views"

#: 被授权接触 Flet 的浏览器桥接层核心资产（设计 §3.3 / §0.3 #6 的唯一例外）。
_FLET_ALLOWED = {"web_page_renderer.py"}


def _py_files(package: Path):
    return sorted(p for p in package.rglob("*.py")
                  if "__pycache__" not in p.parts)


def _imported_modules(path: Path):
    """返回该文件里每条 import 语句解析出的全限定模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


class ViewServiceImportLintTests(unittest.TestCase):
    """红线：app/views/** 不得直接 import app.services.*（§7.5）。"""

    def test_views_do_not_import_services(self):
        offenders = []
        for py in _py_files(_VIEWS):
            for mod in _imported_modules(py):
                if mod == "app.services" or mod.startswith("app.services."):
                    offenders.append(f"{py.relative_to(_ROOT)}:{mod}")
        self.assertEqual(
            offenders, [],
            "视图层不得直接 import app.services.*（应经 Controller/ServiceHub）：\n"
            + "\n".join(offenders))


class ServiceFletImportLintTests(unittest.TestCase):
    """红线 #6：服务层默认零 flet，仅浏览器桥接白名单文件例外。"""

    def test_services_do_not_import_flet_except_bridge(self):
        offenders = []
        for py in _py_files(_SERVICES):
            for mod in _imported_modules(py):
                if mod == "flet" or mod.startswith("flet."):
                    if py.name not in _FLET_ALLOWED:
                        offenders.append(
                            f"{py.relative_to(_ROOT)}: import {mod}")
        self.assertEqual(
            offenders, [],
            "服务层（除 BrowserService 桥接白名单外）不得 import flet：\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
