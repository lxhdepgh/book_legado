"""WebBridgeView：本地网络桥设置页（M5 §1.15 的界面承接）。

服务 ``WebBridgeService`` 早就写好了（回环绑定 + 令牌 + 默认关闭），但**没有任何
界面能打开它**——用户唯一的入口是服务仪表盘上那个把整个服务停掉的按钮，既看不懂
地址也拿不到令牌。本页把三件事摆明：开关、监听地址与令牌、可调端点清单。

红线（§7.5）：本文件不 import 任何 ``app.services.*``；状态以 dict 注入。
"""
from __future__ import annotations

import flet as ft


class WebBridgeView(ft.Column):
    """网络桥：开关 / 端口 / 地址与令牌 / 端点清单 / 状态行。"""

    def __init__(self) -> None:
        super().__init__(scroll=ft.ScrollMode.AUTO, spacing=12, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_toggle_enabled = None    # (enabled: bool) -> None
        self.on_save_port = None         # (port: str) -> None
        self.on_regenerate = None        # () -> None
        self.on_refresh = None           # () -> None

        self._title = ft.Text("网络桥", size=20, weight=ft.FontWeight.BOLD)
        self._intro = ft.Text(
            "在本机开一个 HTTP/WebSocket 服务，用浏览器或脚本管理、检测、试跑书源。"
            "只监听本机回环地址（下方「监听地址」为准），机器之外连不上；"
            "所有端点都要带令牌。",
            size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        self._switch = ft.Switch(
            label="启用网络桥",
            on_change=lambda e: self._fire(self.on_toggle_enabled, e.control.value))
        self._port = ft.TextField(label="监听端口", width=160,
                                  hint_text="0 = 自动分配")
        self._save_port_btn = ft.OutlinedButton(
            "保存端口", icon=ft.Icons.SAVE,
            on_click=lambda e: self._fire(self.on_save_port, self._port.value))
        self._refresh_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="重新读取状态",
            on_click=lambda e: self._fire(self.on_refresh))

        self._state = ft.Text("状态未知", size=14, weight=ft.FontWeight.W_500)
        self._url_label = ft.Text("", selectable=True, size=14)
        self._token = ft.TextField(
            label="访问令牌", read_only=True, password=True,
            can_reveal_password=True,
            hint_text="启用后自动生成")
        self._regen_btn = ft.OutlinedButton(
            "重新生成令牌", icon=ft.Icons.REPLAY,
            on_click=lambda e: self._fire(self.on_regenerate))
        self._token_hint = ft.Text(
            "换新令牌后旧令牌立刻失效，正在用的脚本要跟着改。",
            size=11, color=ft.Colors.ON_SURFACE_VARIANT)

        self._endpoint_title = ft.Text("端点（都要带令牌）",
                                       size=14, weight=ft.FontWeight.W_500)
        self._endpoints = ft.Column(spacing=2)
        self._log = ft.Text("", size=12, color=ft.Colors.GREY_700, selectable=True)

        self.controls = [
            self._title, self._intro,
            ft.Row([self._switch, self._refresh_btn], spacing=12),
            ft.Divider(height=1),
            self._state,
            ft.Text("监听地址", size=12, color=ft.Colors.GREY_600),
            self._url_label,
            ft.Row([self._port, self._save_port_btn], spacing=8),
            self._token,
            ft.Row([self._regen_btn], spacing=8),
            self._token_hint,
            ft.Divider(height=16),
            self._endpoint_title,
            self._endpoints,
            self._log,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def set_status(self, status: dict) -> None:
        """一帧服务状态 ``{enabled, listening, url, port, token, endpoints}``。"""
        status = status or {}
        # 开关值回填不能触发 on_change（程序赋值不会），否则刷新一次就自己关回去。
        self._switch.value = bool(status.get("enabled"))
        self._port.value = str(status.get("port") or "")
        self._token.value = str(status.get("token") or "")
        self._url_label.value = str(status.get("url") or "")
        self._state.value = self._state_text(status)
        self._endpoints.controls = [
            ft.Row([
                ft.Text(str(route), size=12, weight=ft.FontWeight.W_500,
                        selectable=True),
                ft.Text(str(desc), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=10)
            for route, desc in (status.get("endpoints") or [])]

    @staticmethod
    def _state_text(status: dict) -> str:
        if status.get("listening"):
            return f"正在监听 {status.get('url') or ''}（仅本机）"
        if status.get("enabled"):
            # 开关开着却没监听：令牌写不回 / 端口被占，都是"看着开了其实没开"。
            return "开关已打开，但端口没有监听成功（看下方提示）"
        return "未启用（关掉时不占端口）"

    def set_result(self, ok: bool, detail: str) -> None:
        self._log.value = f"[{'成功' if ok else '失败'}] {detail or ''}"
