# 参考代码（Reference Code）

本目录保存规则引擎与解析器兼容工作的只读对照源码，供实现时查阅语义细节。

## 目录结构

### Legado 主项目（三个版本对照）

#### 1. 原始基准版（`legado-3.23.110211/`）
- **版本**：`3.23.110211`
- **提交**：`56c3301dc441c23aaa75bac973943f7469c202be`
- **来源**：`https://github.com/gedoor/legado` → `https://gitee.com/crwth/legado.git`
- **获取日期**：`2026-08-10`
- **说明**：官方 GitHub 仓库已清除历史主体源码，使用保留原发布标签的历史镜像

#### 2. MD3 Material Design 3 改版（`legado-md3-new/`）
- **来源**：`https://github.com/HapeLee/legado-with-MD3`
- **获取日期**：`2026-10-12`
- **说明**：应用 Material Design 3 设计语言的 Legado 分支，UI 层重构参考
- **状态**：完整克隆（151,180 个对象，82.88 MB），包含完整规则引擎代码
- **注意**：旧的空目录 `legado-md3/` 因系统锁定未删除，请使用 `legado-md3-new/`

#### 3. lyc486 维护版（`legado-lyc486/`）
- **来源**：`https://gitee.com/lyc486/legado`
- **获取日期**：`2026-10-12`
- **说明**：Gitee 上的活跃维护分支，包含最新规则引擎改进

**重点对照入口**（所有版本通用）：
- `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeRule.kt` - 规则解析入口
- `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeUrl.kt` - URL 参数解析与 JS 执行
- `app/src/main/java/io/legado/app/model/webBook/` - 网络书源处理
- `app/src/main/java/io/legado/app/data/entities/rule/` - 规则实体定义
- `app/src/main/java/io/legado/app/data/entities/BookSource.kt` - 书源实体

### Jsoup HTML 解析器
- **版本**：`1.16.2`
- **目录**：`jsoup-1.16.2/`（源码 JAR + 编译 JAR）
- **用途**：CSS 选择器、DOM 操作、HTML 清理

### JsoupXpath XPath 扩展
- **版本**：`2.5.3`
- **目录**：`JsoupXpath-2.5.3/`
- **用途**：在 Jsoup DOM 上支持 XPath 查询

### 依赖库
- **目录**：`dependencies/`
- **内容**：`okhttp-4.11.0.jar`、`jsoup-1.16.2.jar` 等

### rarfile Python 库
- **目录**：`rarfile/`
- **用途**：RAR 压缩包解压支持（漫画/电子书）

## 使用原则

1. **只读参考**：本目录所有文件仅供查阅，不参与构建或测试
2. **语义对齐**：实现 Legado 规则引擎时，以此处源码为行为准绳
3. **多版本对照**：遇到行为分歧时，对比三个版本的实现差异
4. **不随分支更新**：固定于特定提交，避免上游变更影响兼容性验证

