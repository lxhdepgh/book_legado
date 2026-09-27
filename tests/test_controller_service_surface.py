"""控制器→服务 调用面静态护栏。

重构后成片出现的"点了没反应 / 结果永远是空"，根因都在控制器写了服务根本不存在的
方法名、参数对不上，或把 ``async def`` 服务方法当同步函数用（拿到协程对象当布尔判，
永远为真）。这些错误在单测里只有恰好跑到那一行才炸，这里用 AST 把 app/controllers
下所有调用点一次性对齐到真实服务类签名上。

服务类按控制器文件自己的 ``from app.services... import X`` 解析，不按类名全局搜索
（``SourceService`` 在新旧两版里重名，按名搜索会拿错实现）。

顺带扫两样同类的"点了没反应"：视图里 ``self.on_xxx = None`` 的回调槽必须在别处被
真实函数接上；``ft.X`` 控件名 / ``ft.Icons.X`` 枚举成员 / 控件构造参数必须真存在
（按别的 Flet 版本写出来的调用会在渲染函数里抛 TypeError，开书就看不到正文）。

只用标准库静态解析 + flet 签名反射，不启动界面、不联网。
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import sys
from pathlib import Path

import flet  # 静态护栏要对齐当前 flet 真实面（不启动界面）

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTROLLER_DIR = ROOT / "app" / "controllers"
VIEW_DIR = ROOT / "app" / "views"
#: 协程方法允许的非 await 落点（交给事件循环/派发器执行）
COROUTINE_SINKS = {"ui_call", "submit", "create_task", "ensure_future",
                   "gather", "run_coroutine_threadsafe"}


def _parse(path: Path) -> ast.AST | None:
    try:
        # utf-8-sig：部分视图文件带 BOM，按 utf-8 读会把 \ufeff 当正文报语法错
        return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _controller_files():
    for path in sorted(CONTROLLER_DIR.rglob("*.py")):
        tree = _parse(path)
        yield path, tree, _imports(tree) if tree is not None else {}


def _root_is_self(node: ast.AST) -> bool:
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name) and node.id == "self"


def _dead_slots() -> list[str]:
    """视图声明的 on_* 回调槽必须在别处接线，且不能接成空操作。

    ``self.on_xxx = None`` 在视图里是槽位声明；按钮点击只 `_fire(self.on_xxx)`，
    槽位没人接（或接了 `lambda: None`）就是界面上按下去没反应的那一类 bug。
    """
    declared: dict[str, str] = {}   # slot -> "where"（首个视图声明处）
    decl_lines: set[str] = set()    # 声明语句本身（视图/控制器的槽位占位）
    for path in sorted(VIEW_DIR.rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant)
                    and node.value.value is None):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr.startswith("on_")
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    declared.setdefault(target.attr, f"{rel}:{node.lineno}")
                    decl_lines.add(f"{rel}:{node.lineno}")

    bound: set[str] = set()
    dead_impl: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            where = f"{rel}:{node.lineno}"
            if where in decl_lines:
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Attribute)
                        and target.attr in declared):
                    continue
                value = node.value
                noop = (isinstance(value, ast.Constant) and value.value is None) \
                    or (isinstance(value, ast.Lambda)
                        and isinstance(value.body, ast.Constant)
                        and value.body.value is None)
                if noop:
                    # 控制器自己的 `self.on_x = None` 占位由外壳注入，不算断线
                    if _root_is_self(target) and isinstance(
                            value, ast.Constant):
                        continue
                    dead_impl.append(f"{where} {target.attr} 接成了空操作")
                    continue
                bound.add(target.attr)
    missing = [f"{declared[name]} {name} 从未接线（点击无反应）"
               for name in declared if name not in bound]
    return missing + dead_impl


def _imports(tree: ast.AST) -> dict[str, str]:
    """本文件的 ``from m import X`` → {X: "m.X"}（含 as 别名）。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = ("." * node.level + node.module) if node.level else node.module
        for alias in node.names:
            out[alias.asname or alias.name] = f"{module}.{alias.name}"
    return out


def _resolve(dotted: str):
    module_path, _, attr = dotted.rpartition(".")
    try:
        return getattr(importlib.import_module(module_path), attr)
    except Exception:
        return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _service_attrs(class_node: ast.ClassDef) -> dict[str, str]:
    """收集 ``self._x = self.service(XService)`` → {"_x": "XService"}。"""
    bound: dict[str, str] = {}
    for node in ast.walk(class_node):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func, args = node.value.func, node.value.args
        if not (isinstance(func, ast.Attribute)
                and func.attr in ("service", "_safe_service") and args
                and isinstance(args[0], ast.Name)):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                bound[target.attr] = args[0].id
    return bound


def _unwrap(func):
    while isinstance(func, (staticmethod, classmethod)):
        func = func.__func__
    if isinstance(func, property):
        func = func.fget
    return func


def _bindable(method, call: ast.Call) -> tuple[bool, str]:
    """按调用点的实参形状试绑定真实签名；返回 (可绑定, 说明)。"""
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):  # builtins / C 实现：不判定
        return True, ""
    parameters = list(sig.parameters.values())
    has_self = bool(parameters) and parameters[0].name in ("self", "cls")
    declared = parameters[1:] if has_self else parameters
    star = next((p for p in declared if p.kind is p.VAR_POSITIONAL), None)
    kwargs_star = next((p for p in declared if p.kind is p.VAR_KEYWORD), None)
    positional = [p for p in declared
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    names = {p.name for p in declared if p.kind is not p.POSITIONAL_ONLY}

    if any(isinstance(a, ast.Starred) for a in call.args):
        return True, ""          # 动态参数：放弃判定而不是误报
    if any(k.arg is None for k in call.keywords):
        return True, ""          # **kwargs 展开
    if len(call.args) > len(positional) and star is None:
        return False, f"传入 {len(call.args)} 个位置参数，签名只接受 {len(positional)} 个"
    for keyword in call.keywords:
        if keyword.arg not in names and kwargs_star is None:
            return False, f"关键字参数 {keyword.arg!r} 不存在"
    return True, ""


def _coroutine_call_awaited(call: ast.Call, parents: dict) -> bool:
    """协程调用是否落在 await / 派发器参数上（两者都算正确用法）。"""
    parent = parents.get(call)
    if isinstance(parent, ast.Await):
        return True
    if isinstance(parent, ast.Call):
        func = parent.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name in COROUTINE_SINKS:
            return True
    return False


def _violations() -> list[str]:
    out: list[str] = []
    for path, tree, imports in _controller_files():
        if tree is None:
            out.append(f"{path} 语法错误")
            continue
        parents = _parents(tree)
        for class_node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            bound = _service_attrs(class_node)
            if not bound:
                continue
            for node in ast.walk(class_node):
                if not isinstance(node, ast.Call) or not isinstance(
                        node.func, ast.Attribute):
                    continue
                owner = node.func.value
                if not (isinstance(owner, ast.Attribute)
                        and isinstance(owner.value, ast.Name)
                        and owner.value.id == "self"):
                    continue
                service_name = bound.get(owner.attr)
                if service_name is None:
                    continue
                dotted = imports.get(service_name)
                service_cls = _resolve(dotted) if dotted else None
                if not inspect.isclass(service_cls):
                    continue  # 服务尚未落盘：并行集成期允许
                method = getattr(service_cls, node.func.attr, None)
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                if method is None:
                    out.append(f"{where} {service_name}.{node.func.attr} 不存在")
                    continue
                method = _unwrap(method)
                if not callable(method):
                    continue
                ok, why = _bindable(method, node)
                if not ok:
                    out.append(f"{where} {service_name}.{node.func.attr} 参数不匹配：{why}")
                    continue
                if (asyncio.iscoroutinefunction(method)
                        and not _coroutine_call_awaited(node, parents)):
                    out.append(f"{where} {service_name}.{node.func.attr} 是协程，"
                               "必须 await 或交给 ui_call 派发")
    return out


def _base_members() -> set[str]:
    """BaseController 的方法名（控制器实例属性撞上其中之一就是遮蔽）。"""
    from app.controllers.base import BaseController

    names = {name for name in dir(BaseController)
             if not name.startswith("__")
             and callable(getattr(BaseController, name, None))}
    assert {"_engine", "ui_call", "service", "flush", "subscribe"} <= names, \
        "基类成员清单变了，护栏需同步"
    return names


def _shadowed_base_members() -> list[str]:
    problems: list[str] = []
    members = _base_members()
    for path, tree, _imports in _controller_files():
        if tree is None:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for class_node in (n for n in ast.walk(tree)
                           if isinstance(n, ast.ClassDef)):
            for node in ast.walk(class_node):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and target.attr in members):
                        problems.append(
                            f"{rel}:{node.lineno} self.{target.attr} 遮蔽了 "
                            f"BaseController.{target.attr}")
    return problems


def test_controller_service_surface():
    """每个 self._svc.method(...) 调用点都对得上真实服务签名。"""
    problems = _violations()
    assert not problems, "控制器调用面断链：\n" + "\n".join(problems)


def test_controller_does_not_shadow_base_members():
    """控制器实例属性不许与 BaseController 成员重名。

    ``self._engine = self._safe_service(RuleEngineService)`` 会把基类的
    ``_engine()``（无头/无 UI loop 时 ui_call 的回落入口）吃掉：此后该控制器
    每一次 ui_call 都抛 TypeError，表现就是"点了没反应"。
    """
    problems = _shadowed_base_members()
    assert not problems, "基类成员被遮蔽：\n" + "\n".join(problems)


def test_view_callback_slots_wired():
    """视图回调槽全部接线：没接或接成空操作 = 界面点了没反应。"""
    problems = _dead_slots()
    assert not problems, "回调槽断线：\n" + "\n".join(problems)


def test_service_bindings_resolved():
    """护栏自身的前提：调用点确实解析到了服务类（否则整条检查静默空转）。"""
    resolved = 0
    for path, tree, imports in _controller_files():
        if tree is None:
            continue
        for class_node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for name in _service_attrs(class_node).values():
                dotted = imports.get(name)
                if dotted and inspect.isclass(_resolve(dotted)):
                    resolved += 1
    assert resolved >= 20, f"仅解析到 {resolved} 处服务绑定，护栏失效"


# --------------------------------------------------------------------------- #
# flet 面：控件名 / 枚举成员 / 构造参数必须真存在
#
# 按别的 Flet 版本写出来的调用（ft.ChoiceChip、ft.ImageFit、ft.Text(no_selection=)）
# 全是 TypeError/AttributeError 级崩溃，而且崩在渲染函数里——用户看到的就是
# "点了书源没反应""打开书没有正文"。这类错误只有跑到那一行才炸，单测覆盖不全，
# 所以在这里按 AST 一次性对齐当前 flet 的真实签名。
# --------------------------------------------------------------------------- #
def _flet_surface_problems() -> list[str]:
    problems: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "ft":
                if not hasattr(flet, node.attr):
                    problems.append(f"{rel}:{node.lineno} flet 没有 ft.{node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "ft":
                cls = getattr(flet, node.func.attr, None)
                if isinstance(cls, type) and issubclass(cls, flet.Control):
                    _check_ft_call(problems, rel, node, cls)
    return problems


def _check_ft_call(problems: list[str], rel: str, node: ast.Call, cls) -> None:
    try:
        params = inspect.signature(cls.__init__).parameters
    except (ValueError, TypeError):
        return
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return
    for kw in node.keywords:
        if kw.arg and kw.arg not in params:
            problems.append(
                f"{rel}:{node.lineno} ft.{cls.__name__} 没有参数 {kw.arg}")


def test_flet_surface_names_and_kwargs():
    """所有 ft.X / ft.X(...) 调用都对得上当前 flet 的真实面。"""
    problems = _flet_surface_problems()
    assert not problems, "flet 面断链：\n" + "\n".join(problems)


def test_flet_enum_members_exist():
    """``ft.Icons.RADIOS`` 这类不存在的枚举成员会在构造控件时抛 AttributeError。"""
    import enum

    problems: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
                continue
            base = node.value
            if not (isinstance(base.value, ast.Name) and base.value.id == "ft"):
                continue
            holder = getattr(flet, base.attr, None)
            if isinstance(holder, type) and issubclass(holder, enum.Enum) \
                    and not hasattr(holder, node.attr):
                problems.append(
                    f"{rel}:{node.lineno} ft.{base.attr} 没有成员 {node.attr}")
    assert not problems, "flet 枚举成员不存在：\n" + "\n".join(problems)


if __name__ == "__main__":  # 手工跑出全部违规清单
    for line in _violations():
        print(line)
