"""书源登录链路测试（对照 Legado ``SourceLoginActivity`` / ``SourceLoginDialog``）。

Legado 的登录有三种形态，桌面端必须一样分：
① ``loginUi`` 有行 → 表单（text/password 铺输入框**并回填已存登录值**，
   ``type:"button"`` 铺按钮、点它只执行该行 action）；
② ``loginUi`` 空但 loginUrl 是网页 → 开 WebView 登录（这里报地址 + 显式清除）；
③ 无表单也无地址 → 直接跑登录脚本。

钉住的历史断链：按钮行被当输入框渲染（action 永远没人执行）、已存值不回填、
②那种源弹一张空表单——空表单点「登录」按引擎语义是**清除**登录信息。

全程离线：服务面用替身，不起引擎、不联网。
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import flet as ft

from app.controllers.find_books_controller import FindBooksController
from app.services.engine.rule_engine_service import RuleEngineService
from app.services.reading.reading_service import ReadingService
from app.services.sources.source_service import SourceService
from app.views.reading.find_books_view import FindBooksView
from app.views.reading.login_dialog import LoginDialog


class _Bus:
    def __init__(self):
        self.subscriptions: list = []

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))
        return lambda: (event_type, handler) in self.subscriptions and \
            self.subscriptions.remove((event_type, handler))


class _Hub:
    def __init__(self, services: dict):
        self.bus = _Bus()
        self._services = services

    def get(self, stype):
        return self._services.get(stype)


class _FakePage:
    """只记账的假 page：对话框链路只认 show_dialog / pop_dialog。"""

    def __init__(self):
        self.shown: list = []
        self.pop_count = 0

    def show_dialog(self, dialog) -> None:
        dialog.open = True
        self.shown.append(dialog)

    def pop_dialog(self) -> None:
        self.pop_count += 1
        self.shown = [d for d in self.shown if getattr(d, "open", False)]


def _rule_engine(schema=None, target="") -> SimpleNamespace:
    return SimpleNamespace(
        login_available=lambda source: True,
        login_schema=AsyncMock(return_value=schema or []),
        login_target=lambda source: target,
        submit_login=AsyncMock(return_value="ok"),
        run_login_action=AsyncMock(return_value="done"),
    )


def _rig(schema=None, target="", source=None) -> tuple:
    services = {
        RuleEngineService: _rule_engine(schema, target),
        ReadingService: SimpleNamespace(
            search_books=AsyncMock(return_value=[]), stop_search=AsyncMock(),
            staged_record=lambda: None),
        SourceService: SimpleNamespace(
            list_sources=AsyncMock(return_value=[]),
            get_sources=AsyncMock(return_value=[])),
    }
    controller = FindBooksController(_Hub(services))
    view = FindBooksView()
    controller.attach(view)
    page = _FakePage()
    controller._page = lambda: page
    source = source or SimpleNamespace(
        bookSourceUrl="https://src", bookSourceName="书源A",
        loginUrl=target or "function login(){}", loginUi="[]")
    return controller, view, services[RuleEngineService], page, source


def _wait_until(predicate, deadline: float = 10.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _labels(dialog) -> list[str]:
    return [str(getattr(a, "content", "")) for a in dialog.actions]


# --------------------------------------------------------------------------- #
# 视图层：LoginDialog 铺得对
# --------------------------------------------------------------------------- #
class LoginDialogRenderingTests(unittest.TestCase):
    def test_button_rows_render_as_buttons_not_text_fields(self):
        dialog = LoginDialog(SimpleNamespace(bookSourceName="书源A"))
        dialog.set_fields([
            {"key": "Account", "label": "账号", "hint": "", "password": False,
             "value": "", "action": ""},
            {"key": "Sign in", "label": "登 录", "hint": "", "password": False,
             "value": "", "action": "Login()"},
        ])
        kinds = [type(c).__name__ for c in dialog._fields_host.controls]
        self.assertEqual(kinds, ["TextField", "Button"],
                         f"loginUi 的按钮行被铺成了 {kinds}")

    def test_stored_login_values_are_prefilled(self):
        dialog = LoginDialog(SimpleNamespace(bookSourceName="书源A"))
        dialog.set_fields([{"key": "Account", "label": "账号", "value": "alice",
                            "hint": "", "password": False, "action": ""}])
        field = dialog._fields_host.controls[0]
        self.assertEqual(field.value, "alice", "已存登录值没回填：每次都要重打")

    def test_clicking_a_button_carries_the_current_field_values(self):
        seen: list = []
        dialog = LoginDialog(SimpleNamespace(bookSourceName="书源A"))
        dialog.on_action = lambda src, action, values: seen.append((action, values))
        dialog.set_fields([
            {"key": "Account", "label": "账号", "value": "", "hint": "",
             "password": False, "action": ""},
            {"key": "Code", "label": "验证码", "value": "", "hint": "",
             "password": False, "action": "check()"},
        ])
        dialog._fields_host.controls[0].value = "alice"
        button = dialog._fields_host.controls[1]
        self.assertIsInstance(button, ft.Button)
        button.on_click(None)
        self.assertEqual(seen, [("check()", {"Account": "alice"})])

    def test_web_login_panel_offers_no_submit(self):
        """纯网页登录：没有可填的表单，就不该有"登录"按钮（点了等于清登录信息）。"""
        dialog = LoginDialog(SimpleNamespace(bookSourceName="书源A"))
        dialog.show_web_login("https://src/login")
        self.assertFalse(dialog._submit_btn.visible)
        self.assertIn("清除已存登录信息", _labels(dialog))
        texts = []
        for col in dialog.content.controls:
            for c in getattr(col, "controls", []) or []:
                texts.append(str(getattr(c, "value", "")))
        self.assertIn("https://src/login", " ".join(texts))


# --------------------------------------------------------------------------- #
# 控制器层：三形态分流
# --------------------------------------------------------------------------- #
class LoginDispatchTests(unittest.TestCase):

    def _open(self, controller, source):
        controller.open_login(source)

    def test_form_mode_button_runs_that_action_and_never_submits(self) -> None:
        controller, view, engine, page, source = _rig(schema=[
            {"name": "Account", "type": "text", "value": "alice"},
            {"name": "Sign in", "type": "button", "action": "Login()"},
        ])
        try:
            self._open(controller, source)
            self.assertTrue(_wait_until(lambda: bool(page.shown)), "登录框没弹出来")
            dialog = page.shown[0]
            self.assertEqual(dialog._fields_host.controls[0].value, "alice",
                             "控制器把已存登录值弄丢了：每次都要重打")
            button = dialog._fields_host.controls[1]
            button.on_click(None)
            self.assertTrue(_wait_until(
                lambda: engine.run_login_action.await_count == 1))
            args = engine.run_login_action.await_args
            self.assertEqual(args.args[1], "Login()")
            self.assertEqual(args.args[2], {"Account": "alice"})
            self.assertEqual(engine.submit_login.await_count, 0,
                             "点按钮不该提交整张表单")
            self.assertIn("已执行", str(dialog._message.value))
        finally:
            controller.detach()

    def test_url_only_source_does_not_get_an_empty_form(self) -> None:
        controller, view, engine, page, source = _rig(
            schema=[], target="https://src/login")
        try:
            self._open(controller, source)
            self.assertTrue(_wait_until(lambda: bool(page.shown)))
            dialog = page.shown[0]
            self.assertEqual(
                [c for c in dialog._fields_host.controls
                 if isinstance(c, ft.TextField)], [],
                "纯网页登录源铺了一张空表单")
            self.assertEqual(engine.submit_login.await_count, 0,
                             "打开登录面板不该动已存登录信息")
            clear = next(a for a in dialog.actions
                         if str(getattr(a, "content", "")) == "清除已存登录信息")
            clear.on_click(None)
            self.assertTrue(_wait_until(
                lambda: engine.submit_login.await_count == 1))
            self.assertEqual(engine.submit_login.await_args.args[1], {},
                             "清除走的就是引擎的空 values 语义")
        finally:
            controller.detach()

    def test_script_only_source_runs_the_login_script(self) -> None:
        controller, view, engine, page, source = _rig(schema=[], target="")
        status: list[str] = []
        view.set_status = lambda text, *a: status.append(str(text))
        try:
            self._open(controller, source)
            self.assertTrue(_wait_until(
                lambda: engine.run_login_action.await_count == 1))
            self.assertEqual(engine.run_login_action.await_args.args, (source,),
                             "无 action = 执行源自己的登录脚本")
            self.assertEqual(page.shown, [], "这种形态不该弹框")
            self.assertTrue(_wait_until(
                lambda: any("登录脚本" in s for s in status)), status)
        finally:
            controller.detach()

    def test_failing_action_speaks_in_the_dialog(self) -> None:
        """动作炸了要在框里说：模态框背后没人看得见状态行。"""
        controller, view, engine, page, source = _rig(schema=[
            {"name": "Go", "type": "button", "action": "boom()"}])
        engine.run_login_action = AsyncMock(side_effect=RuntimeError("js 报错"))
        try:
            self._open(controller, source)
            self.assertTrue(_wait_until(lambda: bool(page.shown)))
            dialog = page.shown[0]
            dialog._fields_host.controls[0].on_click(None)
            self.assertTrue(_wait_until(
                lambda: "js 报错" in str(dialog._message.value)))
            self.assertEqual(page.pop_count, 0, "失败不该把框关掉")
        finally:
            controller.detach()


# --------------------------------------------------------------------------- #
# 服务层：转发面存在且是纯函数
# --------------------------------------------------------------------------- #
class RuleEngineLoginForwardTests(unittest.TestCase):

    def test_login_target_is_sync_and_reads_the_source(self):
        svc = RuleEngineService.__new__(RuleEngineService)   # 不建引擎池
        got = RuleEngineService.login_target(
            svc, SimpleNamespace(bookSourceUrl="https://src", loginUrl="/login"))
        self.assertEqual(got, "https://src/login")

    def test_run_login_action_forwards_to_the_engine(self):
        calls: list = []

        class _Engine:
            async def execute_login_action(self, source, action="", values=None):
                calls.append((action, values))
                return "ok"

        svc = RuleEngineService.__new__(RuleEngineService)

        async def _get_engine(source):
            return _Engine()

        svc._get_engine = _get_engine
        import asyncio
        result = asyncio.run(svc.run_login_action(
            SimpleNamespace(bookSourceUrl="https://src"), "Login()", {"a": "1"}))
        self.assertEqual(result, "ok")
        self.assertEqual(calls, [("Login()", {"a": "1"})])


if __name__ == "__main__":
    unittest.main()
