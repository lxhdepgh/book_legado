# Flet UI 优化计划（基于 Dart 实现对照）

**日期**: 2026-10-11  
**目标**: 将 Flet UI 对齐到 Dart 版本的现代化设计模式和交互体验

---

## 1. 核心差异分析

### 1.1 书架屏幕对比

| 维度 | Dart 版本 | Flet 版本 | 优化优先级 |
|------|----------|----------|-----------|
| **布局结构** | Scaffold + AppBar + Column | Column + 自定义工具栏 | 🔴 高 |
| **列表渲染** | ListView.separated + ListTile | ListView + 手动构建行 | 🟡 中 |
| **刷新机制** | RefreshIndicator（下拉刷新） | 工具栏按钮刷新 | 🟢 低 |
| **错误处理** | FailureBanner（流式错误） | 无统一错误展示 | 🔴 高 |
| **状态管理** | Riverpod AsyncValue（加载/成功/失败） | 直接更新 controls | 🟡 中 |
| **换源入口** | trailing IconButton | 行管理菜单第一项 | 🟢 低 |
| **封面加载** | _Cover widget（占位符+缓存） | ft.Image（无占位） | 🟡 中 |

### 1.2 阅读器屏幕对比

| 维度 | Dart 版本 | Flet 版本 | 优化优先级 |
|------|----------|----------|-----------|
| **正文渲染** | SelectableText + custom spans | ft.Markdown / ft.Text | 🟡 中 |
| **进度保存** | 自动保存 + 手动书签 | 手动书签为主 | 🟢 低 |
| **目录面板** | Drawer + ListView | SideSheet + ListView | 🟢 低 |
| **搜索栏** | BottomSheet + TextField | 内嵌搜索条 | 🟢 低 |
| **朗读控制** | Floating Action Button | 底部固定条 | 🟢 低 |

### 1.3 书源管理屏幕对比

| 维度 | Dart 版本 | Flet 版本 | 优化优先级 |
|------|----------|----------|-----------|
| **数据表格** | DataTable + PaginatedDataTable | ListView + 手动表格 | 🔴 高 |
| **批量操作** | Checkbox + SelectionArea | 复选框列表 | 🟡 中 |
| **检测进度** | LinearProgressIndicator | Text 显示百分比 | 🟡 中 |
| **导入导出** | FilePicker + SnackBar | FilePicker + AlertDialog | 🟢 低 |

---

## 2. 优化方案

### 2.1 书架屏幕重构（优先级 🔴 高）

#### 当前问题
1. 缺少统一的错误展示机制（FailureBanner）
2. 工具栏过于复杂（6 个控件挤在一行）
3. 没有下拉刷新手势支持
4. 状态行位置不当（贴在窗口底部而非列表下方）

#### 优化方案

**A. 引入 FailureBanner 组件**
```python
class FailureBanner(ft.Row):
    """统一错误横幅（对齐 Dart 的 failure_banner.dart）。"""
    
    def __init__(self, message: str = "", on_dismiss=None):
        super().__init__()
        self.visible = bool(message)
        self.controls = [
            ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.RED_700),
            ft.Text(message, color=ft.Colors.RED_700, expand=True),
            ft.IconButton(ft.Icons.CLOSE, on_click=on_dismiss),
        ]
```

**B. 简化为 AppBar 模式**
```python
# 旧：复杂的 ResponsiveRow 工具栏
# 新：使用 ft.AppBar（对齐 Dart 的 Scaffold.appBar）
appbar = ft.AppBar(
    title=ft.Text("书架"),
    actions=[
        ft.IconButton(ft.Icons.UPLOAD_FILE, on_click=_import_local),
        ft.IconButton(ft.Icons.GRID_VIEW, on_click=_toggle_layout),
        ft.IconButton(ft.Icons.REFRESH, on_click=_refresh),
    ],
)
```

**C. 添加下拉刷新支持**
```python
# Flet 0.86.x 不支持原生 RefreshIndicator，用 GestureDetector 模拟
refresh_detector = ft.GestureDetector(
    on_vertical_drag_end=lambda e: _check_pull_to_refresh(e),
    content=ft.ListView(...)
)
```

**D. 调整状态行位置**
```python
# 旧：挂在 Column 末尾
# 新：紧跟在列表后面，作为最后一个子项
body_controls = [
    appbar,
    failure_banner,
    ft.Expanded(content=list_view),
    status_row,  # 紧贴列表底部
]
```

### 2.2 书源列表屏幕重构（优先级 🔴 高）

#### 当前问题
1. 手动构建表格（效率低、不可排序）
2. 缺少分页支持（920 行一次性加载）
3. 批量操作界面不直观

#### 优化方案

**A. 使用 DataTable 替代手动表格**
```python
# 旧：ListView + 手动 Row/Cell
# 新：ft.DataTable（支持排序、列宽自适应）
data_table = ft.DataTable(
    columns=[
        ft.DataColumn(ft.Text("名称"), on_sort=lambda col, asc: _sort_by_name(asc)),
        ft.DataColumn(ft.Text("URL"), on_sort=lambda col, asc: _sort_by_url(asc)),
        ft.DataColumn(ft.Text("状态")),
        ft.DataColumn(ft.Text("操作")),
    ],
    rows=[
        ft.DataRow(cells=[
            ft.DataCell(ft.Text(source["name"])),
            ft.DataCell(ft.Text(source["url"], selectable=True)),
            ft.DataCell(_status_chip(source)),
            ft.DataCell(_action_buttons(source)),
        ])
        for source in sources
    ],
)
```

**B. 添加分页支持**
```python
# 使用 PaginatedDataTable（内置分页器）
paginated_table = ft.PaginatedDataTable(
    header=ft.Text(f"书源共 {len(sources)} 个"),
    columns=[...],
    source=BookSourceDataSource(sources),  # 自定义 DataTableSource
    rows_per_page=50,
    available_rows_per_page=[25, 50, 100],
)
```

### 2.3 阅读器屏幕优化（优先级 🟡 中）

#### 当前问题
1. 正文渲染不支持富文本选择
2. 目录面板占用空间过大
3. 搜索功能不够显眼

#### 优化方案

**A. 改进正文渲染**
```python
# 旧：ft.Markdown（简单 Markdown）
# 新：ft.Text with spans（支持混合样式）
content_text = ft.Text(
    spans=[
        ft.TextSpan("这是普通文本", ft.TextStyle(size=16)),
        ft.TextSpan("加粗部分", ft.TextStyle(weight=ft.FontWeight.BOLD)),
        ft.TextSpan("链接", ft.TextStyle(color=ft.Colors.BLUE, 
                  decoration=ft.TextDecoration.UNDERLINE)),
    ],
)
```

**B. 目录改为可折叠 Drawer**
```python
# 旧：始终显示的 SideSheet
# 新：可滑出的 Drawer（节省空间）
page.drawer = ft.NavigationDrawer(
    controls=[
        ft.ListTile(title=ft.Text("目录")),
        ft.Divider(),
        ft.ListView([...chapters...]),
    ],
)
```

---

## 3. 实施计划

### 阶段 1: 书架屏幕重构（预计 2 小时）
- [ ] 创建 FailureBanner 组件
- [ ] 改用 AppBar 替代自定义工具栏
- [ ] 添加下拉刷新手势检测
- [ ] 调整状态行位置
- [ ] 测试与 Dart 版本的视觉一致性

### 阶段 2: 书源列表重构（预计 3 小时）
- [ ] 实现 DataTable 替代手动表格
- [ ] 添加 PaginatedDataTable 分页支持
- [ ] 改进批量操作界面（Checkbox + SelectionArea）
- [ ] 添加列排序功能
- [ ] 性能测试（920 行数据加载时间）

### 阶段 3: 阅读器优化（预计 2 小时）
- [ ] 改进正文渲染（TextSpan 混合样式）
- [ ] 目录改为可折叠 Drawer
- [ ] 搜索栏改为 BottomSheet
- [ ] 朗读控制改为 Floating Action Button

### 阶段 4: 全局组件库（预计 1 小时）
- [ ] 创建通用 FailureBanner
- [ ] 创建通用 LoadingSpinner
- [ ] 创建通用 EmptyState
- [ ] 创建通用 StatusChip

---

## 4. 技术约束

### 4.1 Flet 0.86.x API 限制
- ❌ 无原生 RefreshIndicator（需手势模拟）
- ❌ DataTable 不支持虚拟滚动（大数据量需分页）
- ✅ 支持 GestureDetector（可实现自定义手势）
- ✅ 支持 NavigationDrawer（可替代 SideSheet）

### 4.2 性能考量
- ListView 超过 500 行需考虑虚拟化（Flet 无内置支持）
- Image 加载需添加占位符和缓存策略
- DataTable 每页不超过 100 行以保证流畅度

### 4.3 兼容性要求
- 保持与当前服务层 API 兼容（不修改控制器接口）
- 保持事件订阅模式不变（继续使用 controller.subscribe）
- 保持路由注册不变（DEST_* 常量不动）

---

## 5. 验收标准

### 5.1 视觉一致性
- [ ] 书架屏幕与 Dart 版本布局误差 < 5%（像素级比对）
- [ ] 书源列表表格对齐方式一致
- [ ] 阅读器正文字号/行距一致

### 5.2 交互一致性
- [ ] 下拉刷新手势响应时间 < 200ms
- [ ] 表格排序点击反馈 < 100ms
- [ ] 目录展开/收起动画流畅（60fps）

### 5.3 性能指标
- [ ] 书架加载 100 本书 < 500ms
- [ ] 书源列表加载 920 行 < 2s（分页后）
- [ ] 阅读器打开章节 < 300ms

---

**下一步**: 从阶段 1 开始，先优化书架屏幕。
