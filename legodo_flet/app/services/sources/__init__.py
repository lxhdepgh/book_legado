"""书源域服务包（M3，§4.2 书源域表）。

五个服务：SourceService（CRUD/导入导出/书架/封面）、SourceCheckService（检测）、
RuleRepairService（正文规则自愈）、CompatAuditService（JS/Java API 静态审计）、
OnlineImportService（多类型资源在线/本地导入分发器）。

红线（§4.6）：本包服务零 flet import；UI 经 views/source/ 与 controllers/source/。
"""
