"""Flet 视图层（§7.2）。

视图 = 轻壳布局 + Controller；M1 只有 ServiceDashboardView 一个只读视图。
视图经 ServiceHub.get() 取服务句柄，不直接 import 业务服务实现（§7.5 import-lint）。
"""
