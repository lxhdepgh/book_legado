# 参考代码（Reference Code）

本目录保存 Legado 规则引擎的参考源码，供实现时查阅语义细节。

## 目录结构

### 1. lyc486 维护版（`legado-lyc486/`）
- **来源**：`https://gitee.com/lyc486/legado`
- **获取日期**：2026-10-12
- **说明**：Gitee 上的活跃维护分支，包含最新规则引擎改进
- **重点文件**：
  - `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeRule.kt` - 规则解析入口
  - `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeUrl.kt` - URL 参数解析与 JS 执行
  - `app/src/main/java/io/legado/app/model/webBook/` - 网络书源处理

### 2. MD3 Material Design 3 改版（`legado-md3/`）
- **来源**：`https://github.com/HapeLee/legado-with-MD3`
- **获取日期**：2026-10-12
- **说明**：应用 Material Design 3 设计语言的 Legado 分支，UI 层重构参考
- **状态**：完整克隆（151,180 个对象），包含完整规则引擎代码和 MD3 UI 实现

## 使用原则

1. **只读参考**：本目录所有文件仅供查阅，不参与构建或测试
2. **语义对齐**：实现 Legado 规则引擎时，以此处源码为行为准绳
3. **双版本对照**：遇到行为分歧时，对比两个版本的实现差异
4. **不随分支更新**：固定于特定提交，避免上游变更影响兼容性验证
