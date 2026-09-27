"""书源域控制器包（M3，§7.4）：每视图一个 Controller。

控制器订阅 EventBus 事件、经 ``hub.get()`` 按类型取服务、把数据快照渲染进视图；
视图本身零业务。本包允许 import app.services.sources.* 类型（仅作 hub.get() 的类型参数，
与 controllers/find_books_controller.py 同例）。

并行集成说明：SourceService / SourceCheckService / RuleRepairService /
CompatAuditService 可能与本包并行开发、尚未落盘——各控制器对它们的类引用用
try/except 懒解析，缺失时退化为占位类型，测试用替身覆盖；兄弟服务落盘后
自动切换为真类，控制器代码零改动。
"""
