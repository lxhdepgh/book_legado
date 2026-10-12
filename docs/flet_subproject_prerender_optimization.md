# Flet 子项目预渲染优化方案

**日期**: 2026-10-12  
**状态**: 📋 待实施  
**目标**: 在 Windows/Linux/macOS 桌面端利用预渲染提升阅读体验

---

## 🎯 优化目标

虽然静态预渲染方案最初是为 Android 设计的，但它同样可以优化桌面端的用户体验：

### 当前痛点（桌面端）

1. **首章加载慢**：需要启动 Chromium + 渲染 JavaScript（~2-5s）
2. **切换书源卡顿**：换源时需要重新渲染所有章节（每章 ~1-3s）
3. **重复渲染浪费**：同一章节每次打开都要重新渲染

### 优化后收益

| 场景 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| **首次打开书** | 每章 2-5s | 首批 5 章预渲染，后续秒开 | 首屏 < 1s |
| **切换书源** | 全量重渲染 | 增量更新（仅新章节渲染） | 快 3-5× |
| **重复阅读** | 每次都渲染 | 缓存命中（~50ms） | 快 40-100× |
| **离线阅读** | ❌ 需 Chromium | ✅ 完全离线 | 可用性↑ |

---

## 🏗️ 架构设计

### 方案 A：后台预渲染队列（推荐）

**核心思路**：用户加入书架时，自动在后台预渲染该书的所有章节。

```
用户操作                    后台任务
─────────                   ────────
加入书架 →                 创建预渲染任务
                            ↓
                        优先级队列（最多 3 本书并发）
                            ↓
                        批量预渲染（每批 5 章）
                            ↓
                        缓存到 storage
                            ↓
                        更新进度条（UI 反馈）
```

**优势**：
- ✅ 用户无感知（后台异步执行）
- ✅ 智能优先级（最近阅读的书优先）
- ✅ 可中断/恢复（支持断点续传）

**劣势**：
- ⚠️ 占用磁盘空间（每本书 10-50MB）
- ⚠️ 首次加入书架时有延迟（但可后台进行）

### 方案 B：按需预渲染 + 智能缓存

**核心思路**：用户阅读到某章时，预渲染后续 N 章（类似视频预加载）。

```
用户阅读第 10 章 →         预渲染第 11-15 章
                            ↓
                        缓存最近阅读的 3 本书
                            ↓
                        LRU 淘汰旧缓存（保留 7 天）
```

**优势**：
- ✅ 节省存储空间（只缓存近期阅读的章节）
- ✅ 无缝阅读体验（下一章已就绪）

**劣势**：
- ⚠️ 首次打开新书仍需等待渲染
- ⚠️ 跳跃阅读时命中率低

### 方案 C：混合模式（最佳）

**结合 A + B**：
- 加入书架时预渲染前 20 章（快速进入阅读状态）
- 阅读时动态预渲染后续章节（无缝体验）
- 缓存策略：最近 3 本书全量缓存 + 其他书前 20 章

---

## 🔧 实施步骤

### Step 1: 启用预渲染功能（main.py）

修改 `legodo_flet/app/main.py` 第 154 行：

```python
# 当前代码
hub.register(RuleEngineService(
    webview_fetcher=webview, verification_fetcher=verification))

# 优化后代码
from app.services.engine.rule_pre_executor import RulePreExecutor

# 创建预执行器
pre_executor = RulePreExecutor(
    browser_service=browser,
    storage=storage,  # 从 hub 获取
    engine=None  # 延迟注入
)

hub.register(RuleEngineService(
    webview_fetcher=webview,
    verification_fetcher=verification,
    android_mode=False,      # 桌面端保持标准模式
    pre_executor=pre_executor  # ← 启用预渲染
))
```

### Step 2: 添加书架预渲染触发器

在 `app/controllers/bookshelf_controller.py` 中：

```python
class BookshelfController:
    async def add_to_shelf(self, book):
        """加入书架时触发预渲染。"""
        # 1. 保存书籍信息
        await self._storage.save_book(book)
        
        # 2. 获取目录
        chapters = await self._engine.get_toc(book.source, book)
        
        # 3. 提交后台预渲染任务（仅前 20 章）
        first_20 = chapters[:20]
        asyncio.create_task(
            self._pre_render_background(book, first_20)
        )
    
    async def _pre_render_background(self, book, chapters):
        """后台预渲染任务。"""
        from app.services.engine.rule_pre_executor import RulePreExecutor
        
        # 从 hub 获取预执行器
        pre_executor = self.hub.require(RulePreExecutor)
        
        # 批量预渲染（低优先级，避免影响前台操作）
        await pre_executor.pre_execute_book(
            chapters=[{"url": ch.url, "title": ch.title} for ch in chapters],
            source=book.source,
            batch_size=5,
            delay_between=0.3  # 降低频率，避免压垮站点
        )
        
        # 通知 UI 更新进度
        self._notify_progress(book.id, len(chapters))
```

### Step 3: 阅读器集成智能预加载

在 `app/views/reading/reader_view.py` 中：

```python
class ReaderView:
    def __init__(self, ...):
        self._preload_queue = asyncio.Queue(maxsize=10)
        self._start_preload_worker()
    
    def _start_preload_worker(self):
        """启动预加载工作线程。"""
        asyncio.create_task(self._preload_next_chapters())
    
    async def _preload_next_chapters(self):
        """预加载后续 5 章。"""
        while True:
            current_chapter = await self._current_chapter_event.wait()
            
            # 获取后续章节
            next_chapters = self._get_next_chapters(current_chapter, count=5)
            
            # 检查哪些未缓存
            pre_executor = self.hub.require(RulePreExecutor)
            uncached = []
            for ch in next_chapters:
                cached = await pre_executor.get_cached_template(ch.url)
                if not cached:
                    uncached.append(ch)
            
            # 预渲染未缓存的章节
            if uncached:
                await pre_executor.pre_execute_book(
                    uncached,
                    source=self.current_source,
                    batch_size=3,
                    delay_between=0.2
                )
```

### Step 4: 添加预渲染管理界面

在 `app/views/tools/pre_render_manager.py` 中创建管理界面：

```python
class PreRenderManagerView(ft.UserControl):
    """预渲染管理界面。"""
    
    def build(self):
        return ft.Column([
            ft.Text("预渲染管理", size=20, weight=ft.FontWeight.BOLD),
            
            # 统计信息
            ft.Row([
                ft.Text(f"缓存书籍: {self._cached_books}"),
                ft.Text(f"缓存章节: {self._cached_chapters}"),
                ft.Text(f"缓存大小: {self._cache_size_mb:.1f} MB"),
            ]),
            
            # 操作按钮
            ft.Row([
                ft.ElevatedButton("清理过期缓存", on_click=self._cleanup_expired),
                ft.ElevatedButton("导出缓存", on_click=self._export_cache),
                ft.ElevatedButton("导入缓存", on_click=self._import_cache),
            ]),
            
            # 书籍列表
            ft.ListView([
                self._build_book_row(book)
                for book in self._cached_books_list
            ])
        ])
```

### Step 5: 配置缓存策略

在 `app/core/settings.py` 中添加：

```python
# 预渲染缓存配置
PRE_RENDER_CONFIG = {
    "max_cached_books": 10,          # 最多缓存 10 本书
    "chapters_per_book": 20,         # 每本书预渲染前 20 章
    "cache_ttl_days": 7,             # 缓存有效期 7 天
    "max_cache_size_mb": 500,        # 最大缓存空间 500MB
    "batch_size": 5,                 # 批量预渲染每批 5 章
    "delay_between_batches": 0.3,    # 批次间延迟 0.3s
}
```

---

## 📊 性能基准测试

### 测试场景

| 场景 | 测试方法 | 预期结果 |
|------|---------|---------|
| **首次打开书** | 加入一本 100 章的书，测量前 20 章预渲染时间 | < 30s（后台） |
| **阅读流畅度** | 连续翻页 20 次，测量每章加载时间 | < 100ms/章 |
| **缓存命中率** | 阅读 3 本书后，重复打开任意章节 | > 90% |
| **磁盘占用** | 缓存 10 本书（每本 100 章） | < 500MB |

### 实测数据模板

```markdown
## 实测结果（2026-10-XX）

- 测试设备：Windows 11, i7-12700K, 16GB RAM
- 测试书籍：《XXX》（100 章，平均每章 5KB）

### 预渲染性能
- 前 20 章预渲染时间：**XX.Xs**（目标 < 30s）
- 平均每章渲染时间：**X.Xs**（目标 < 2s）
- 缓存命中率：**XX%**（目标 > 90%）

### 阅读体验
- 首章加载时间：**XXms**（目标 < 100ms）
- 连续翻页平均延迟：**XXms**（目标 < 50ms）
- 磁盘占用：**XXXMB**（目标 < 500MB）
```

---

## ⚠️ 注意事项

### 1. 资源占用控制

预渲染会占用 CPU 和网络带宽，需限制并发：

```python
# 最多同时预渲染 3 本书
SEMAPHORE = asyncio.Semaphore(3)

async def pre_render_with_limit(book, chapters):
    async with SEMAPHORE:
        await pre_executor.pre_execute_book(chapters, source=book.source)
```

### 2. 缓存失效策略

书源规则更新后需清理旧缓存：

```python
async def on_source_updated(source_url):
    """书源更新时清理缓存。"""
    await pre_executor.invalidate_cache(source_url=source_url)
```

### 3. 用户隐私保护

预渲染缓存可能包含敏感内容，需提供清理选项：

```python
async def clear_all_cache():
    """一键清理所有预渲染缓存。"""
    await storage.delete_prefix("pre_render:")
```

### 4. 网络友好性

避免频繁请求同一站点，增加随机延迟：

```python
import random

delay = 0.3 + random.uniform(-0.1, 0.1)  # 0.2-0.4s 随机延迟
await asyncio.sleep(delay)
```

---

## 🎨 UI 增强建议

### 1. 预渲染进度条

在书架视图中显示预渲染进度：

```python
ft.LinearProgressIndicator(
    value=progress.synced_chapters / progress.total_chapters,
    color=ft.Colors.GREEN,
)
```

### 2. 缓存状态指示器

在阅读视图中显示当前章节的缓存状态：

```python
if is_cached:
    ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN, size=16)
else:
    ft.Icon(ft.Icons.CLOUD_DOWNLOAD, color=ft.Colors.GREY, size=16)
```

### 3. 预渲染设置面板

在设置页添加预渲染配置项：

```python
ft.Switch(
    label="启用后台预渲染",
    value=settings.get("pre_render.enabled", True),
    on_change=lambda e: settings.set("pre_render.enabled", e.data),
)

ft.Slider(
    label="每本书预渲染章节数",
    min=10, max=100, divisions=9,
    value=settings.get("pre_render.chapters_per_book", 20),
)
```

---

## 📚 相关文档

- `docs/flet_android_static_render_guide.md`: 静态预渲染方案实施指南
- `reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/http/BackstageWebView.kt`: 原生实现参考
- `app/services/engine/rule_pre_executor.py`: 规则预执行器实现
- `app/services/engine/reading_engine_android.py`: Android 专用阅读引擎

---

**下一步**: 
1. 修改 `main.py` 启用预渲染功能
2. 在书架控制器中添加后台预渲染触发器
3. 编写性能基准测试脚本
4. 根据实测数据调整缓存策略
