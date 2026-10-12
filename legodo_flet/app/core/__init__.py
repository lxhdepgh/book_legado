"""进程内多服务架构的基础设施层（设计文档 §4 / §9.1）。

本包是 Legado 桌面端 M1 里程碑的"进程内底座"，包含：

- :mod:`app.core.lifecycle` —— Service 基类 / ServiceState 状态机 / ServiceStatus 快照；
- :mod:`app.core.events`   —— AppEvent 基类与 §4.3 冻结事件表的子集；
- :mod:`app.core.bus`      —— EventBus：typed 事件、异步投递到 EngineHub 执行域；
- :mod:`app.core.engine_worker` —— EngineHub（守护线程事件循环）+ EngineProxy；
- :mod:`app.core.hub`      —— ServiceHub：注册 / 依赖拓扑启停 / 状态汇聚；
- :mod:`app.core.storage`  —— StorageService：SQLite 主库 legado.db + schema 迁移 + 目录分配；
- :mod:`app.core.settings` —— SettingsService：settings 表键值读写 + 变更事件；
- :mod:`app.core.registry` —— ViewRegistry：声明式目的地路由；
- :mod:`app.core.interfaces` —— 跨层回调接口（BrowserInteractionCallback）。

红线（§0.3 第 6/10/11 条）：本包内任何模块**不得** ``import flet``；
服务间通信只经 EventBus 或显式接口；禁止任何 Windows 专属 API。
"""
