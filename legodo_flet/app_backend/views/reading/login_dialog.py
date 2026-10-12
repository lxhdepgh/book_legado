"""LoginDialog：书源登录动作（§7.2 模态覆盖层）。

自旧单体 ``ReadingView`` 的 ``_show_source_login`` / ``_open_source_login`` /
``_run_login_action`` 拆出。对话框只渲染：由书源 loginUi 配置出来的动态输入框 /
按钮；具体登录动作（取 Cookie / 过验证码）由
:class:`~app.controllers.find_books_controller.FindBooksController` 经引擎执行。

Legado 的 loginUi 一项可以是输入框**也可以是按钮**（``type:"button"`` + ``action``），
所以这里两种控件都要铺得出来；纯网页登录（loginUrl 是 http 而 loginUi 为空）没有表单
可铺，走 :meth:`show_web_login`——那种形态下**没有"提交"可言**，提交空表单位同清登录。

红线：本文件**不 import app.services.***。
"""
from __future__ import annotations

import flet as ft


class LoginDialog(ft.AlertDialog):
    """书源登录：字段由 Controller 按书源 loginUi 配置动态铺出来。"""

    def __init__(self, source=None) -> None:
        super().__init__(modal=True)
        self._source = source

        # -- 回调槽 --
        self.on_submit = None     # (source, fields: dict) -> None
        self.on_action = None     # (source, action: str, fields: dict) -> None
        self.on_clear = None      # () -> None  显式清掉已存登录信息
        self.on_close = None      # () -> None

        self._fields_host = ft.Column(tight=True, spacing=8)
        self._message = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._submit_btn = ft.Button(
            "登录", icon=ft.Icons.LOGIN,
            on_click=lambda e: self._fire(self.on_submit, self._source,
                                          self._collect_fields()))

        source_name = getattr(source, "bookSourceName", "") if source else ""
        self.title = ft.Text(f"书源登录 · {source_name}", size=16,
                             weight=ft.FontWeight.BOLD)
        self.content = ft.Column([
            self._fields_host,
            self._message,
        ], tight=True, width=360)
        self.actions = [
            ft.TextButton("取消", on_click=lambda e: self._fire(self.on_close)),
            self._submit_btn,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _collect_fields(self) -> dict:
        return {key: (ctrl.value or "") for key, ctrl in self._field_inputs().items()}

    def _field_inputs(self) -> dict:
        out = {}
        for ctrl in self._fields_host.controls:
            key = getattr(ctrl, "data", None)
            if key:
                out[key] = ctrl
        return out

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller 调用）
    # ------------------------------------------------------------------ #
    def set_fields(self, fields: list[dict]) -> None:
        """按书源 loginUi 配置铺控件。

        每项：``{key, label, hint, password, value}`` 为输入框；带 ``action`` 的项是
        **按钮**（Legado 的 ``type:"button"``），点它只执行该动作、不提交表单。
        ``value`` 是引擎从源里读回的**已存登录值**，必须回填——不然每次打开都要重打。
        """
        self._fields_host.controls.clear()
        for spec in fields or []:
            action = str(spec.get("action") or "")
            label = spec.get("label") or spec.get("key", "")
            if action:
                self._fields_host.controls.append(ft.Button(
                    label, icon=ft.Icons.PLAY_ARROW,
                    on_click=lambda e, a=action: self._fire(
                        self.on_action, self._source, a, self._collect_fields())))
                continue
            self._fields_host.controls.append(ft.TextField(
                label=label,
                hint_text=spec.get("hint", ""),
                password=bool(spec.get("password", False)),
                value=str(spec.get("value", "") or ""),
                data=str(spec.get("key", "")),
                dense=True,
            ))

    def show_web_login(self, url: str) -> None:
        """纯网页登录形态：本源没有 loginUi 表单，只能在浏览器里登录。

        只**报地址**，不给"登录"按钮——空表单提交等价于清掉登录信息（见控制器）。
        要清得由用户点「清除已存登录信息」这个明确的按钮。
        """
        self.content.controls.insert(0, ft.Column(tight=True, spacing=6, controls=[
            ft.Text("该源在网页上登录，这里没有可填的表单。", size=13),
            ft.Text(f"登录地址：{url}", size=12, selectable=True),
            ft.Text("到「工具 → 内置浏览器」打开上面的地址完成登录，"
                    "登录态（Cookie）由该源自己的规则读取。", size=12),
        ]))
        self._submit_btn.visible = False
        self.actions = [
            ft.TextButton("关闭", on_click=lambda e: self._fire(self.on_close)),
            ft.Button("清除已存登录信息", icon=ft.Icons.DELETE_OUTLINE,
                      color=ft.Colors.RED,
                      on_click=lambda e: self._fire(self.on_clear)),
        ]

    def set_message(self, text: str, error: bool = False) -> None:
        self._message.value = text or ""
        self._message.color = ft.Colors.RED_400 if error else ft.Colors.GREY_600
