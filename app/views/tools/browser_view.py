"""BrowserView：通用浏览器入口（M5，§1.19 / 契约 §11 / §13）。

地址栏 + 前进/后退/重载 + 加载态；经 BrowserService 会话打开页面。浏览器交互回调
（登录 / 验证码 / 投屏输入）由本视图实现 :class:`~app.core.interfaces.BrowserInteractionCallback`
协议注入——这是 §7.5 唯一例外通道，视图因此可以定义这些异步回调，但仍 **零
app.services.* import**：具体浏览器动作经 Controller 调 BrowserService。

**页面渲染区（``_page_host``）不是摆设**：内置浏览器走 CDP 投屏（Windows/Linux 没有
应用内 WebView 控件），会话帧由 Controller 编组回界面循环后喂 :meth:`render_frame`，
点击/滚动/拖拽/输入再由这里的槽位转成坐标交给 Controller。视图自己不发请求、不认服务。
"""
from __future__ import annotations

import base64

import flet as ft

#: 投屏画面尺寸：与 services/interactive_browser.VIEWPORT 同一约定，
#: 视图按它摆控件，Controller 再用内核回报的实际尺寸换算坐标。
CAST_WIDTH = 400
CAST_HEIGHT = 480

# BrowserInteractionCallback 为 runtime_checkable Protocol，视图按结构化方式实现其
# 三个异步方法即可注入（不运行期继承，避免与 ft.Column 元类冲突）。


class BrowserView(ft.Column):
    """通用浏览器：地址栏 / 前进后退 / 加载态 + 投屏画面与手势。"""

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_navigate = None       # (url: str) -> None
        self.on_back = None           # () -> None
        self.on_forward = None        # () -> None
        self.on_reload = None         # () -> None
        self.on_close = None          # () -> None  关闭会话页
        self.on_cast_tap = None       # (x, y, button) -> None
        self.on_cast_scroll = None    # (x, y, delta_x, delta_y) -> None
        self.on_cast_pan = None       # (kind, x, y) -> None，kind: press/move_to/release
        self.on_cast_type = None      # (text: str) -> None

        self._title = ft.Text("内置浏览器", size=20, weight=ft.FontWeight.BOLD)
        self._address = ft.TextField(
            hint_text="输入网址后回车访问", expand=True,
            on_submit=lambda e: self._fire(self.on_navigate, self._address.value or ""))
        self._back_btn = ft.IconButton(
            icon=ft.Icons.ARROW_BACK, tooltip="后退",
            on_click=lambda e: self._fire(self.on_back))
        self._forward_btn = ft.IconButton(
            icon=ft.Icons.ARROW_FORWARD, tooltip="前进",
            on_click=lambda e: self._fire(self.on_forward))
        self._reload_btn = ft.IconButton(
            icon=ft.Icons.REFRESH, tooltip="重载",
            on_click=lambda e: self._fire(self.on_reload))
        self._close_btn = ft.IconButton(
            icon=ft.Icons.CLOSE, tooltip="关闭页面", visible=False,
            on_click=lambda e: self._fire(self.on_close))
        self._go_btn = ft.FilledTonalButton(
            "访问", icon=ft.Icons.OPEN_IN_BROWSER,
            on_click=lambda e: self._fire(self.on_navigate, self._address.value or ""))

        self._loading = ft.ProgressBar(visible=False)
        self._status = ft.Text("就绪", size=12, color=ft.Colors.GREY_600)
        # 页面渲染区（投屏帧；空闲时放一行说明文字）
        self._page_host = ft.Container(expand=True, border=ft.Border.all(
            1, ft.Colors.OUTLINE_VARIANT), border_radius=6, padding=4,
            alignment=ft.Alignment.CENTER,
            content=self._idle_text("输入网址后回车，页面会渲染在这块区域里"))

        # -- 投屏画面（会话打开后才挂进 _page_host） --
        self._frame = ft.Image(src="", width=CAST_WIDTH, height=CAST_HEIGHT,
                               fit=ft.BoxFit.FILL)
        self._typed = ft.TextField(label="输入到网页的文字", width=250, dense=True,
                                   on_submit=lambda e: self._send_typed())
        self._cast = ft.GestureDetector(
            content=self._frame,
            on_tap_down=lambda e: self._tap(e, "left"),
            on_secondary_tap_down=lambda e: self._tap(e, "right"),
            on_scroll=lambda e: self._scroll(e),
            on_pan_start=lambda e: self._pan("press", e),
            on_pan_update=lambda e: self._pan("move_to", e),
            on_pan_end=lambda e: self._pan("release", e),
            on_pan_cancel=lambda e: self._pan("release", e))
        self._cast_size = (CAST_WIDTH, CAST_HEIGHT)

        self.controls = [
            self._title,
            ft.Row([self._back_btn, self._forward_btn, self._reload_btn,
                    self._close_btn, self._address, self._go_btn], spacing=6),
            self._loading,
            self._page_host,
            self._status,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    @staticmethod
    def _idle_text(text: str) -> ft.Control:
        return ft.Text(text, size=13, color=ft.Colors.ON_SURFACE_VARIANT)

    # ------------------------------------------------------------------ #
    # 渲染接口（Controller / BrowserService 驱动）
    # ------------------------------------------------------------------ #
    def set_address(self, url: str) -> None:
        self._address.value = url or ""

    def set_loading(self, loading: bool, text: str = "") -> None:
        self._loading.visible = bool(loading)
        if text:
            self._status.value = text

    def set_status(self, text: str) -> None:
        self._status.value = text or "就绪"

    def set_nav_enabled(self, can_back: bool, can_forward: bool) -> None:
        self._back_btn.disabled = not can_back
        self._forward_btn.disabled = not can_forward

    # ------------------------------------------------------------------ #
    # 投屏画面
    # ------------------------------------------------------------------ #
    def begin_cast(self) -> None:
        """会话已开：把画面与输入行摆进渲染区，显示「关闭页面」。"""
        self._page_host.content = ft.Column([
            self._cast,
            ft.Text("点击网页输入框后，在这里输入并回车；拖动可操作滑块",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Row([self._typed,
                    ft.Button("输入", on_click=lambda e: self._send_typed())],
                   alignment=ft.MainAxisAlignment.CENTER),
        ], tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        self._close_btn.visible = True

    def render_frame(self, data: bytes) -> None:
        """一帧 JPEG（会话帧由 Controller 编组回界面循环后喂进来）。"""
        if not data:
            return
        self._frame.src = ("data:image/jpeg;base64,"
                           + base64.b64encode(data).decode("ascii"))

    def set_cast_size(self, width: int, height: int) -> None:
        """内核回报的投屏尺寸：坐标换算按它，画面仍按控件尺寸显示。"""
        if width and height:
            self._cast_size = (int(width), int(height))

    def end_cast(self, message: str = "") -> None:
        """会话关闭：画面撤下，渲染区回到说明文字。"""
        self._frame.src = ""
        self._page_host.content = self._idle_text(message or "尚未打开页面")
        self._close_btn.visible = False

    # ------------------------------------------------------------------ #
    # 手势 → 槽位（把控件坐标换算到投屏画面）
    # ------------------------------------------------------------------ #
    def _point(self, event) -> tuple[float, float] | None:
        position = getattr(event, "local_position", None)
        if position is None:
            return None
        cast_w, cast_h = self._cast_size
        scale_x = cast_w / float(CAST_WIDTH)
        scale_y = cast_h / float(CAST_HEIGHT)
        return (position.x * scale_x, position.y * scale_y)

    def _tap(self, event, button: str) -> None:
        point = self._point(event)
        if point is not None:
            self._fire(self.on_cast_tap, point[0], point[1], button)

    def _scroll(self, event) -> None:
        delta = getattr(event, "scroll_delta", None)
        point = self._point(event)
        if delta is not None and point is not None:
            self._fire(self.on_cast_scroll, point[0], point[1], delta.x, delta.y)

    def _pan(self, kind: str, event) -> None:
        point = None if kind == "release" else self._point(event)
        self._fire(self.on_cast_pan, kind,
                   point[0] if point else 0, point[1] if point else 0)

    def _send_typed(self) -> None:
        text = str(self._typed.value or "")
        if not text:
            return
        self._typed.value = ""
        self._fire(self.on_cast_type, text)

    # ------------------------------------------------------------------ #
    # BrowserInteractionCallback 协议实现（视图实现、BrowserService 回调）。
    # 这三个异步回调把登录/验证码/投屏输入接回界面；取消返回 None。
    # ------------------------------------------------------------------ #
    async def on_login_required(self, url: str) -> str | None:
        self.set_status(f"需要登录：{url}")
        return None  # 真实界面由 Controller 弹出登录对话框后回填

    async def on_captcha_required(self, image_bytes: bytes) -> str | None:
        self.set_status(f"需要识别验证码（{len(image_bytes)} 字节）")
        return None

    async def on_projection_input(self, prompt: str) -> str | None:
        self.set_status(f"投屏输入：{prompt}")
        return None
