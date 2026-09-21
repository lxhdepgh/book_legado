# 系统设计文档（current-system-design）

> 状态：骨架（初始化生成于 2026-09-01），各节标记「待补充」处需开发者确认后补全。

## 1. 项目定位

Legado 书源连通性 / 搜索可用性检测与内置阅读器的桌面应用（Flet），参考实现来自 Legado Android 版规则体系。

## 2. 总体架构

```
main.py            # 应用入口
app/
├── app.py         # Flet 应用装配（待补充：页面路由组织方式）
├── ui_feedback.py # UI 反馈工具（待补充）
├── models/        # 数据模型：book_source / legacy_book_source / appearance_settings
├── services/      # 核心服务层（规则引擎、检测、缓存、阅读引擎等）
├── views/         # 页面视图：书源管理 / 检测 / 导入 / 阅读 / 设置等
└── themes/        # 内置主题
```

### 2.1 服务层关键模块（待补充职责边界描述）

- `rule_engine` / `legado_rule_parser` / `source_rule`：Legado 规则解析与执行
- `check_service`：书源连通性与搜索可用性检测
- `reading_engine` / `reading_cache` / `reading_progress`：阅读链路与进度持久化
- `book_cache_service`：整书离线缓存
- `legado_js` / `java_regex` / `icu_charset` 等：Legado 兼容层

## 3. 数据流（待补充）

- 书源导入 → 校验 → 检测 → 阅读 的完整链路图

## 4. 关键设计决策（待补充）

- 与 Legado 原版的对齐策略（参见 `base_history/`、`other_history/` 中的参考源码）

## 5. 持久化与缓存（待补充）

- 参见已有文档：`docs/数据与缓存目录.md`

## 6. 打包与分发

- Windows / Android 打包（入口脚本曾被删除，打包方式待补充）
