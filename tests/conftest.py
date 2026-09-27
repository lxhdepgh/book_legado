"""M7 里程碑测试收集策略。

M7 清理与回归（设计 §8.2 M7）：旧单体冻结文件已删除、新架构为唯一事实。
collect_ignore 全解除——历史上因依赖未迁移服务/视图而忽略的三个测试模块，
现已全部处置完毕并纳入全量回归：

- test_book_cache_service.py —— 改指新外壳 app.services.reading.book_cache_service
- test_reading_progress.py   —— 改指新阅读服务 app.services.reading.reading_service
- test_ui_safety.py          —— 重写为新结构红线 import-lint（见该文件 docstring）

此后不再有阶段性忽略清单；全量 pytest 即回归全集。
"""

collect_ignore: list[str] = []
