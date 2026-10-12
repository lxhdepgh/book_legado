# 参考代码（Reference Code）

本目录保存 Legado 规则引擎的参考源码，**仅供查阅语义细节**。

## ⚠️ 重要说明

- **只读参考**：本目录所有文件仅供查阅，不参与构建、测试或打包
- **已清理 git 元数据**：移除了 `.git` 目录和相关文件，避免嵌套仓库冲突
- **固定快照**：代码固定在特定日期获取的版本，不随上游更新而变化
- **不在版本控制中**：已在项目根目录 `.gitignore` 中排除，需单独克隆

## 目录结构

### 1. lyc486 维护版（`legado-lyc486/`）
- **来源**：`https://gitee.com/lyc486/legado`
- **获取日期**：2026-10-12
- **说明**：Gitee 上的活跃维护分支，包含最新规则引擎改进
- **重点文件**：
  - `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeRule.kt` - 规则解析入口
  - `app/src/main/java/io/legado/app/model/analyzeRule/AnalyzeUrl.kt` - URL 参数解析与 JS 执行
  - `app/src/main/java/io/legado/app/model/webBook/` - 网络书源处理

## 使用原则

1. **语义对齐**：实现 Legado 规则引擎时，以此处源码为行为准绳
2. **双版本对照**：遇到行为分歧时，对比官方原版和 lyc486 分支的差异
3. **不修改源码**：如需实验性改动，请在项目主目录下创建独立分支或副本
4. **IDE 索引优化**：可在 IDE 中标记为"Excluded"，避免全文搜索干扰

## 如何更新参考代码

```bash
# 删除旧版本
rm -rf reference_code/legado-lyc486

# 重新克隆（浅克隆以节省空间）
cd reference_code
git clone --depth 1 https://gitee.com/lyc486/legado.git legado-lyc486

# 清理 git 元数据（避免嵌套仓库）
rm -rf legado-lyc486/.git legado-lyc486/.github
```
