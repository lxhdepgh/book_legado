"""每视图一个 Controller（设计 §7.4 / §9.2）。

Controller 是视图与服务之间的唯一编排层：

- 视图经 Controller 拿数据，Controller 经 ``ServiceHub.get()`` 按类型取服务；
- ``attach(view)/detach()`` 对称订阅/退订 EventBus 事件（防跨页泄漏）；
- 编排协程经 :meth:`BaseController.ui_call` 排进界面循环（引擎重活由 EngineProxy
  自行跳回 EngineHub 执行域），收尾统一 flush；
- 高频事件（下载进度 / 搜索流）的 0.5s 节流在 Controller 层
  :meth:`BaseController.throttle` 实现，不进服务层。

本包允许 import ``app.services.*`` 具体类型（用于 ``hub.get(XxxService)`` 的
类型参数）——import-lint 只守门 ``app/views/``，不守 ``app/controllers/``。
"""
from __future__ import annotations

from app.controllers.base import BaseController

__all__ = ["BaseController"]
