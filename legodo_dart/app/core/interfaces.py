"""跨层回调接口（§4.6 / §9.2）。

服务层零 Flet 依赖是硬约束；唯一例外通道是 BrowserService 碰 UI 的交互回调
（登录弹窗 / 验证码截图 / 投屏输入）。这里把旧 engine_worker 里
``interaction_loop`` 的灰色地带收编为显式 Protocol——视图层实现它，
BrowserService（M5）持有并调用，不向其他服务扩散。

M1 只声明、不使用。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BrowserInteractionCallback(Protocol):
    """浏览器交互回调：视图层实现，BrowserService 调用（§9.2）。

    三个回调都是异步的，由实现方决定切到哪条循环去碰界面控件
    （旧实现经 ``engine.interaction_loop = asyncio.get_running_loop()``
    把回调绑定到调用方所在循环，见 engine_worker.EngineProxy）。
    """

    async def on_login_required(self, url: str) -> str | None:
        """需要登录：返回用户提交的凭据（如 cookie/密码），取消返回 None。"""
        ...

    async def on_captcha_required(self, image_bytes: bytes) -> str | None:
        """需要识别验证码：传入验证码图片字节，返回用户输入，取消返回 None。"""
        ...

    async def on_projection_input(self, prompt: str) -> str | None:
        """投屏窗口需要输入文本（地址栏/对话框）：返回用户输入，取消返回 None。"""
        ...
