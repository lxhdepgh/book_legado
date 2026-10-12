# 参考代码（Reference Code）

本目录保存规则引擎与解析器兼容工作的只读对照源码，供实现时查阅语义细节。

## 目录结构

### Legado 主项目
- **版本**：`3.23.110211`
- **提交**：`56c3301dc441c23aaa75bac973943f7469c202be`
- **目录**：`legado-3.23.110211/`
- **来源**：`https://github.com/gedoor/legado` → `https://gitee.com/crwth/legado.git`
- **获取日期**：`2026-08-10`

> 官方 GitHub 仓库已清除历史主体源码，使用保留原发布标签的历史镜像。后续行为对齐以该固定提交为准。

**重点对照入口**：
- `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeRule.kt` - 规则解析入口
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
3. **不随分支更新**：固定于特定提交，避免上游变更影响兼容性验证

