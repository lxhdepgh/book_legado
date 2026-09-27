"""M5 工具域控制器（controllers/tools/，§7.4 / 契约 §13）。

每视图一个 Controller，继承 :class:`~app.controllers.base.BaseController`：
attach 时订阅 EventBus 事件并把视图回调接到业务方法，detach 统一退订。
协程动作经 :meth:`ui_call` 派发到 EngineHub；服务句柄经
``self.hub.get(XxxService)`` 取（服务实现由 M5 并行线交付，这里宽松 import，
未就位时控制器退化为"服务未就绪"提示而不崩）。
"""
