# Flet 布局优化对比报告

**日期**: 2026-10-11  
**状态**: ✅ 阶段 1 完成（StateBar + FailureBanner 增强）  
**参考**: legodo_dart vs legodo_flet 全面对比分析

---

## 🔍 问题诊断方法

通过 Agent 全面扫描两个项目的 UI 实现，对比以下 6 个维度：

1. **书架屏幕** - AppBar、ListTile、网格/列表切换、空状态
2. **书源列表** - DataTable、分页器、批量操作栏、搜索框
3. **阅读器** - 目录 Drawer、正文排版、搜索面板、朗读控制
4. **设置页面** - 表单布局、开关控件、下拉选择器
5. **工具页面** - 卡片布局、按钮排列、图标使用
6. **通用组件** - FailureBanner、LoadingSpinner、EmptyState、对话框

---

## 📊 发现的差异点（按优先级排序）

### 🔴 高优先级（必须修复）

| # | 优化项 | 文件路径 | Dart 实现 | Flet 当前状态 | 工作量 |
|---|--------|----------|-----------|--------------|--------|
| 1 | **StateBar 通用组件** | `app/views/common/` | ✅ state_bar.dart | ❌ 不存在 | ✅ 已完成 |
| 2 | **FailureBanner 响应式** | `failure_banner.py` | Stream 订阅 | 命令式调用 | ✅ 已增强 |
| 3 | **书架 AppBar 架构** | `bookshelf_view.py` | ft.AppBar | v1: Column<br>v2: AppBar | ✅ v2 已完成 |
| 4 | **书源列表虚拟化** | `book_source_list_view.py` | ListView.builder | v1: ListView<br>v2: PaginatedDataTable | ✅ v2 已完成 |
| 5 | **阅读器排版引擎** | `reader_view_v2.py` | DocumentLine 模型 | v1: 完整<br>v2: 简化过度 | ⚠️ 保持 v1 |
| 6 | **目录呈现方式** | `reader_view.py` | BottomSheet | v1: 整页覆盖<br>v2: NavigationDrawer | ⚠️ 评估中 |

### 🟡 中优先级（应该修复）

| # | 优化项 | 文件路径 | 建议方案 | 工作量 |
|---|--------|----------|---------|--------|
| 7 | **书架空状态 Stack 层叠** | `bookshelf_view_v2.py` | 用 Stack 覆盖而非替换列表 | 1h |
| 8 | **书源筛选栏 Wrap** | `book_source_list_view_v2.py` | Row 改为 wrap=True | 1h |
| 9 | **字体页 AppBar** | `font_view.py` | Column 改为 Scaffold + AppBar | 2h |
| 10 | **字体页加载指示器** | `font_view.py` | 集成 LoadingSpinner | 1h |
| 11 | **工具卡片主题色** | `overview_view.py` | GREY_200 → SURFACE_CONTAINER_HIGHEST | 0.5h |

### 🟢 低优先级（可以优化）

| # | 优化项 | 文件路径 | 建议方案 | 工作量 |
|---|--------|----------|---------|--------|
| 12 | **IconButton key 属性** | 所有视图 | 为所有可交互元素添加 key | 2h |
| 13 | **朗读 FAB 位置** | `reader_view_v2.py` | FAB 改回工具栏按钮 | 1h |
| 14 | **外观设置组件化** | `appearance_view.py` | 提取子组件减少嵌套 | 2h |

---

## ✅ 已完成工作

### 1. StateBar 通用组件

**位置**: `legodo_flet/app/views/common/state_bar.py` (130 行)

**核心特性**:
```python
class StateBar(ft.Container):
    """底部状态行：动作消息 > 失败 > 加载中 > 数据文案（四态优先级）。"""
    
    def __init__(
        self,
        *,
        failed_text: str = "加载失败",
        loading_text: str = "正在加载…",
        data_text: str | None = None,  # 可以是字符串或 callable
        is_error: bool = False,
        visible: bool = True,
    ) -> None:
```

**对齐 Dart 的关键点**:
- ✅ 四态优先级：message > failed > loading > data
- ✅ 错误状态红字显示（`is_error=True`）
- ✅ 支持动态 data_text（callable）
- ✅ 关键约束：**加载失败不许报成"共 0 条"**

**使用示例**:
```python
# 书架屏幕
self._status = StateBar(
    failed_text="书架加载失败",
    loading_text="正在加载书架…",
    data_text=lambda: f"书架共 {len(items)} 本",
)

# Controller 中
def on_refresh_success(items):
    status.show_data()

def on_refresh_error(msg):
    status.show_error(msg)
```

### 2. FailureBanner 响应式增强

**位置**: `legodo_flet/app/views/common/failure_banner.py`

**新增功能**:
```python
class FailureBanner(ft.Row):
    def __init__(
        self,
        visible: bool = False,
        on_error: Callable[[], str | None] | None = None,  # 新增
    ) -> None:
        self._on_error = on_error
    
    def check_and_show(self) -> None:
        """检查错误源并自动显示（订阅式模式）。"""
        if self._on_error is not None:
            message = self._on_error()
            if message:
                self.show(message)
```

**两种模式**:
1. **命令式**（保持兼容）: `banner.show("错误消息")`
2. **订阅式**（新增）: 传入 `on_error` 回调，自动读取错误状态

**与 Dart 的差异**:
- Dart: `Stream<EngineFailure>` 响应式订阅
- Flet: `Callable[[], str | None]` 回调轮询（折中方案）

**原因**: Flet 架构中没有统一的失败事件流，Controller 直接管理错误状态。

---

## 🎯 下一步建议

### 短期（本周内）

1. **集成 StateBar 到关键页面**
   - 书架屏幕 (`bookshelf_view_v2.py`)
   - 书源列表 (`book_source_list_view_v2.py`)
   - 字体管理页 (`font_view.py`)

2. **字体页 AppBar 改造**
   ```python
   # 当前：简单 Column
   self.controls = [self._title, ft.Row([...]), self._current, ...]
   
   # 目标：Scaffold + AppBar
   return ft.Page(
       appbar=ft.AppBar(title=ft.Text("字体"), actions=[scan_btn, add_btn]),
       body=ft.Column([...]),
   )
   ```

3. **为 IconButton 添加 key**
   ```python
   # 当前
   ft.IconButton(ft.Icons.REFRESH, tooltip="刷新")
   
   # 目标
   ft.IconButton(ft.Icons.REFRESH, key="sources.refresh", tooltip="刷新")
   ```

### 中期（下个月）

1. **阅读器排版引擎评估**
   - v1 的完整排版能力是否必要？
   - v2 的简化版能否满足需求？
   - 是否需要引入 Dart 的 `DocumentLine` 模型？

2. **目录呈现方式决策**
   - 桌面端：保留 v1 的整页覆盖（全屏阅读时不遮挡正文）
   - 移动端：改用 BottomSheet（符合移动习惯）
   - v2 的 NavigationDrawer 方案不完整，需补充或放弃

3. **TabbedGroup 组件**
   - Dart 的惰性装配 + Offstage 保活机制
   - Flet 是否需要类似组件？
   - 或在现有 Tabs 基础上添加惰性加载逻辑

### 长期（3-6 个月）

1. **考虑迁移到 Flutter Web**
   - 绕过 Flet 桥接层的性能瓶颈
   - 直接使用 Dart 的所有 Material Design 组件
   - 但需要重写所有 Python 业务逻辑

2. **评估 Flet 新版本**
   - 是否提供原生 LayoutBuilder？
   - 是否改进 on_resize 事件合流问题？
   - 是否有更好的虚拟化渲染支持？

---

## 📈 优化成果统计

### 代码产出

| 文件 | 行数 | 说明 |
|------|------|------|
| `state_bar.py` | 130 | StateBar 通用组件 |
| `failure_banner.py` | +25 | 响应式增强（总计 164 行） |
| `__init__.py` | +2 | 导出 StateBar |
| **合计** | **157 行** | |

### 文档产出

| 文件 | 行数 | 说明 |
|------|------|------|
| `flet_侧边栏响应式优化.md` | 299 | 侧边栏优化方案 |
| `flet_布局优化对比报告.md` | 本文件 | 全面对比分析 |
| **合计** | **~500 行** | |

### 预估节省工时

- **StateBar 组件**: 避免 4 个屏幕各自实现，节省 ~8h
- **FailureBanner 增强**: 统一错误处理模式，节省 ~4h
- **侧边栏优化**: 解决拖窗卡顿问题，节省调试时间 ~6h
- **总计**: ~18h（约 2.5 个工作日）

---

## 📚 相关文档

- `docs/flet_ui优化完成报告.md`: 前期 UI 优化成果
- `docs/flet_侧边栏响应式优化.md`: 侧边栏响应式详细方案
- `docs/flet_ui适配指南.md`: 服务层 API 适配指南
- `legodo_dart/lib/core/widgets/`: Dart 通用组件参考
- 记忆 `flet-0-86-narrow-window-sizing-contract`: 窄窗 sizing 契约

---

**总结**: 本次优化补齐了 Flet 缺失的两个关键通用组件（StateBar + FailureBanner 增强），并对齐了 Dart 的错误处理模式。后续应优先集成到关键页面，并评估阅读器排版引擎的完整性需求。
