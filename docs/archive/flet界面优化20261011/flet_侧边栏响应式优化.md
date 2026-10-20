# Flet 侧边栏响应式优化方案

**日期**: 2026-10-11  
**状态**: 📋 待实施  
**参考**: Dart shell.dart + navigation.py 第 78 轮实测数据

---

## 🔍 问题诊断

### 1. 响应式触发机制差异

| 维度 | Dart (shell.dart) | Flet (navigation.py) | 影响 |
|------|-------------------|----------------------|------|
| **监听方式** | `LayoutBuilder` 每帧自动触发 | `on_resize` 事件（客户端合流） | 🔴 高 |
| **触发频率** | 连续（拖窗时每像素都触发） | 离散（78 轮实测：30 拍只收到 1-4 次） | 🔴 高 |
| **阈值判断** | `Breakpoint.of(constraints.maxWidth)` 实时计算 | `_is_narrow()` 缓存 `last_narrow` | 🟡 中 |
| **重建代价** | 轻量（只换 Rail/Bar 控件） | 重量（整棵根树序列化，920 行时 9200 控件） | 🔴 高 |

**实测数据**（78 轮 `.probe/probe_drag78c.py`）:
- 拖窗 30 拍，Python 侧只收到 **1-4 次** `on_resize`
- 每次 `rebuild()+_repaint_root()` 耗时 **0.5-2.1s**（书源表 917 条）
- 同档位内重复重建**什么都不换来，除了冻页面**

### 2. NavigationRail 配置缺失

```dart
// Dart: 标签始终可见
NavigationRail(
  labelType: NavigationRailLabelType.all,  // ✅ 所有标签展开
  extended: false,                          // 不需要这个
)
```

```python
# Flet: extended=True 在宽/窄档表现不一致
ft.NavigationRail(
    extended=True,  # ❌ 窄档时标签仍占位，浪费空间
)
```

### 3. 内容区自适应约束不足

```dart
// Dart: Expanded 确保填满剩余空间
Row(
  children: [
    NavigationRail(...),
    VerticalDivider(width: 1),
    Expanded(child: child),  // ✅ 强制填满
  ],
)
```

```python
# Flet: expand=True 在某些嵌套下会溢出
ft.Row([
    self._rail,
    ft.VerticalDivider(width=1, opacity=0.5),
    ft.Container(self.content, expand=True),  # ⚠️ 嵌套 TabBarView 时会塌
])
```

---

## 🎯 优化目标

### G1 判据（必须达成）

1. **响应式灵敏度**: 拖窗跨 720px 阈值时，切换延迟 < 100ms（当前 500-2100ms）
2. **同档位零重建**: 宽度在同档内变化时，完全不触发重建（当前已实现，需保持）
3. **Rail 标签一致性**: 宽档时所有分组标签可见，窄档时隐藏标签只显示图标
4. **内容区不溢出**: 任意窗口尺寸下，正文区不被裁切或留白

### G2 判据（可选增强）

1. **平滑过渡动画**: 切换宽/窄档时有淡入淡出效果（对齐 Material Design）
2. **记忆上次档位**: 重启应用后恢复上次的宽/窄档状态
3. **多显示器适配**: 外接显示器时自动识别 DPI 缩放

---

## 🛠️ 实施方案

### 方案 A：最小改动（推荐，1-2 小时）

#### A1. 修复 NavigationRail 标签配置

**位置**: `legodo_flet/app/views/navigation.py:327-329`

```python
# 修改前
self._rail = ft.NavigationRail(
    destinations=entries, selected_index=0, extended=True,
    use_indicator=True, on_change=self._on_rail_change)

# 修改后
self._rail = ft.NavigationRail(
    destinations=entries, selected_index=0,
    extended=not self._is_narrow(),  # 宽档展开标签，窄档只显示图标
    use_indicator=True, on_change=self._on_rail_change)
```

#### A2. 优化 rebuild 逻辑（避免同档重建）

**位置**: `legodo_flet/app/views/navigation.py:769-780`

当前代码已经实现了 `last_narrow` 缓存，但需要加强注释说明：

```python
def _on_resize(e=None):
    nonlocal last_narrow
    now_narrow = host._is_narrow()
    if now_narrow == last_narrow:
        return  # ✅ 同档不重建（已在 78 轮实测验证）
    last_narrow = now_narrow
    host.rebuild()
    host._repaint_root()
```

#### A3. 修复内容区溢出

**位置**: `legodo_flet/app/views/navigation.py:371-375`

```python
# 修改前
return ft.Row([
    self._rail,
    ft.VerticalDivider(width=1, opacity=0.5),
    ft.Container(self.content, expand=True),
], expand=True, spacing=0)

# 修改后
return ft.Row([
    self._rail,
    ft.VerticalDivider(width=1, opacity=0.5),
    ft.Container(self.content, expand=True, alignment=ft.Alignment.TOP_LEFT),
], expand=True, spacing=0, tight=True)  # tight=True 防止子项溢出
```

### 方案 B：架构级改进（中等工作量，半天）

#### B1. 引入 LayoutObserver 模拟 Dart 的 LayoutBuilder

Flet 没有原生的 `LayoutBuilder`，但可以通过定时器轮询 `page.width` 来弥补 `on_resize` 的漏报：

```python
class WidthObserver:
    """模拟 Dart 的 LayoutBuilder：每秒采样 page.width，跨阈值时通知外壳。"""
    
    def __init__(self, page, host: NavigationHost):
        self.page = page
        self.host = host
        self.last_band = host._is_narrow()
        self._timer = None
    
    def start(self):
        """启动轮询（1Hz 足够，不会像拖窗那样高频）。"""
        import asyncio
        async def _tick():
            while True:
                await asyncio.sleep(1.0)
                now_band = self.host._is_narrow()
                if now_band != self.last_band:
                    self.last_band = now_band
                    self.host.rebuild()
                    self.host._repaint_root()
        self._timer = asyncio.create_task(_tick())
    
    def stop(self):
        if self._timer:
            self._timer.cancel()
```

**装配位置**: `build_app_shell` 末尾

```python
observer = WidthObserver(page, host)
observer.start()
# 注意：无头测试不启动
```

**优点**: 弥补 `on_resize` 漏报，确保跨阈值必定切换  
**缺点**: 增加后台任务，1Hz 轮询有轻微开销

#### B2. 添加切换动画

```python
def rebuild(self):
    """宽/窄窗切换时调用：加淡入淡出动画。"""
    old_root = self.root
    self.root = self.render()
    self._sync_selected()
    self._notify_band()
    
    # 动画过渡
    if self.page is not None and hasattr(self.page, "animate"):
        old_root.opacity = 0.0
        self.root.opacity = 0.0
        self.page.update()
        # 简单淡入
        for alpha in [0.2, 0.4, 0.6, 0.8, 1.0]:
            old_root.opacity = 1.0 - alpha
            self.root.opacity = alpha
            self.page.update()
            time.sleep(0.05)
    
    return self.root
```

### 方案 C：彻底重构（不推荐，工作量大）

完全抛弃 `on_resize`，改用 Flet 的 `ft.ResponsiveRow` 或自定义 `LayoutBuilder` 封装。但这需要：

1. 重写整个导航容器为声明式布局
2. 放弃当前的 `NavigationHost` 命令式架构
3. 重新测试所有视图的兼容性

**结论**: 收益不足以覆盖成本，不建议采用。

---

## 📋 实施清单

### 阶段 1：紧急修复（方案 A，1-2 小时）

- [ ] **A1**: 修复 NavigationRail 标签配置（wide 展开 / narrow 收拢）
- [ ] **A2**: 加强 `last_narrow` 缓存注释，明确"同档不重建"契约
- [ ] **A3**: 修复内容区溢出（`tight=True` + `alignment`）
- [ ] **验收**: 拖窗测试，确认宽/窄档切换正常，同档不卡顿

### 阶段 2：增强体验（方案 B，半天，可选）

- [ ] **B1**: 实现 `WidthObserver` 轮询器（1Hz 采样）
- [ ] **B2**: 添加切换动画（淡入淡出 250ms）
- [ ] **验收**: 跨阈值必定切换，动画流畅不卡顿

### 阶段 3：长期规划（方案 C，暂不实施）

- [ ] 评估 Flet 新版本是否提供原生 `LayoutBuilder`
- [ ] 考虑迁移到 Flutter Web 直接渲染（绕过 Flet 桥接层）

---

## 🧪 验收标准

### 功能验收

| 测试场景 | 预期结果 | 实际结果 |
|---------|---------|---------|
| 拖窗跨 720px 阈值 | 切换延迟 < 100ms | ⏳ 待测 |
| 拖窗同档内变化 | 零重建（CPU 占用 < 5%） | ⏳ 待测 |
| 宽档（>720px） | Rail 显示图标+标签 | ⏳ 待测 |
| 窄档（<720px） | Bar 显示图标，无标签 | ⏳ 待测 |
| 内容区任意尺寸 | 正文不溢出、不留白 | ⏳ 待测 |

### 性能验收

| 指标 | 目标值 | 测量方法 |
|------|--------|---------|
| 跨阈值切换延迟 | < 100ms | 计时器 + 手动拖窗 |
| 同档拖窗 CPU | < 5% | Task Manager 监控 |
| 920 行渲染时间 | < 100ms | PaginatedDataTable 已有 |

---

## 📚 相关文档

- `docs/flet_ui优化完成报告.md`: 已完成的前端优化
- `app/views/navigation.py`: 当前导航实现（769-780 行同档优化注释）
- `legodo_dart/lib/app/shell.dart`: Dart 参考实现
- 记忆 `flet-listview-vs-scrolling-column-resize-cost`: ListView 虚拟化实测
- 记忆 `flet-0-86-narrow-window-sizing-contract`: 窄窗 sizing 契约

---

**总结**: Flet 侧边栏的主要问题是 `on_resize` 事件被客户端合流导致响应迟钝，但通过"同档不重建"策略已将负面影响降到最低。建议先实施方案 A（1-2 小时），若用户反馈仍有卡顿再考虑方案 B 的轮询补偿。
