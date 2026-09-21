# 本地测试指南（local-testing-guide）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 单元测试

```powershell
conda activate legodo_booksource_check
python -m pytest -q                 # 全量
python -m pytest tests/test_legado_compat.py -q   # 单文件
```

- `pytest.ini` 已配置 `norecursedirs = base_history`，参考源码目录不参与收集。

## 2. 测试目录说明（待补充各文件覆盖范围）

- `tests/`：单元 / 集成测试（书源缓存、兼容性、阅读器体验、进度持久化、主题、UI 安全等）
- `test_book_sources_async.py`、`test_reading_chains_async.py`：根目录的异步书源 / 阅读链路测试
- `tools/`：Java Oracle 对比工具（jsoup / rhino / legado），用于与原版行为对齐

## 3. 书源业务测试

- 见根目录 `书源测试方法说明.md`（两层测试：连通性 + 搜索可用性）。

## 4. 改动后验证清单

- 语法：`python -m compileall -q app main.py`
- 测试：`python -m pytest -q`
- lint：暂不配置
