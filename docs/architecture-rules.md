# 架构约束（architecture-rules）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 分层与依赖方向（待补充）

- 预期：`views` → `services` → `models`，禁止反向依赖；待开发者确认。

## 2. 模块边界（待补充）

- `app/services/` 各服务职责边界（规则引擎、检测、缓存、阅读引擎等）
- `base_history/`、`other_history/` 为**只读参考资料**（Legado / jsoup / rhino 等参考源码），禁止修改、禁止被测试收集（`pytest.ini` 已排除）

## 3. 红线清单（待补充）

- 待补充：例如"不得引入新的 JS 引擎"、"规则解析必须与 Legado 原版行为对齐"等
