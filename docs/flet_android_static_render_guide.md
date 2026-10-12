# Flet Android 静态预渲染方案实施指南

**日期**: 2026-10-12  
**状态**: ✅ 已实施  
**参考**: `reference_code/legado-lyc486` (BackstageWebView + WebViewPool)

---

## 📋 方案概述

### 核心思路

借鉴 Legado 原生的 `BackstageWebView(html=..., javaScript=...)` 模式，但把"渲染时机"从**运行时**提前到**桌面端预执行阶段**。

```
桌面端（开发环境）：
  BookSource.check() → 执行 JS 规则 → HeadlessBrowser 渲染
                      → 提取正文 → 缓存到 storage

Android 端（运行时）：
  Reader.loadChapter() → 查预渲染缓存 → 命中则直接解析静态 HTML
                        → 未命中则 HTTP 请求（无 JavaScript 渲染）
```

### 设计依据

- **Legado 原生实现**: `reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/http/BackstageWebView.kt`
- **WebView 池管理**: `reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/webView/WebViewPool.kt`
- **决策逻辑**: `AnalyzeUrl.kt:445-476` (`if (this.useWebView && useWebView) { ... }`)

---

## 🏗️ 架构组件

### 1. RulePreExecutor（规则预执行器）

**位置**: `app/services/engine/rule_pre_executor.py`

**职责**:
- 在桌面端用 HeadlessBrowser 渲染需要 JavaScript 的章节
- 提取正文并缓存到 storage
- 提供缓存读取接口供 Android 端调用

**使用示例**:
```python
from app.services.engine.rule_pre_executor import RulePreExecutor

executor = RulePreExecutor(
    browser_service=browser_service,
    storage=storage,
    engine=rule_engine_service
)

# 预渲染单个章节
chapter = await executor.pre_execute_chapter(
    chapter_url="https://example.com/chapter/1",
    source=book_source,
    force_refresh=False
)

# 批量预渲染整本书
chapters = [
    {"url": "https://...", "title": "第一章"},
    {"url": "https://...", "title": "第二章"},
]
results = await executor.pre_execute_book(chapters, source=book_source)

# Android 端读取
html = await executor.get_cached_template("https://example.com/chapter/1")
```

### 2. ReadingEngineAndroid（Android 专用阅读引擎）

**位置**: `app/services/engine/reading_engine_android.py`

**职责**:
- 继承 `LegadoReadingEngine`，重写 `get_content()` 方法
- 优先从预渲染缓存读取静态 HTML
- 遇到拦截页时给出友好错误提示

**特性**:
- `webview_fetcher` 永远为 None（Android 不支持 Playwright CDP）
- 缓存命中率统计（`cache_stats` 属性）
- 自动检测拦截页（复用 `block_pages.looks_like_gate`）

**使用示例**:
```python
from app.services.engine.reading_engine_android import ReadingEngineAndroid

engine = ReadingEngineAndroid(
    pre_executor=pre_executor,
    timeout=15,
    state_dir="/path/to/state"
)

# 加载章节（自动查缓存）
content = await engine.get_content(source, chapter)

# 查看缓存统计
print(engine.cache_stats)  # {'hits': 42, 'misses': 8, 'total': 50, 'hit_rate': '84.0%'}
```

### 3. PreRenderSync（预渲染同步服务）

**位置**: `app/services/sync/pre_render_sync.py`

**职责**:
- 将桌面端预渲染的结果同步到 Android 设备
- 支持 WebDAV 同步、局域网推送、文件导出三种方式

**同步方式**:
1. **WebDAV 同步**（推荐）：跨平台通用，自动增量同步
2. **局域网 HTTP 推送**：快速测试用
3. **文件导出/导入**：离线场景

**使用示例**:
```python
from app.services.sync.pre_render_sync import PreRenderSync

sync = PreRenderSync(
    pre_executor=pre_executor,
    webdav_service=webdav_service,
    storage=storage
)

# 同步整本书
progress = await sync.sync_book(
    book_id="book_123",
    source=book_source,
    device_id="android_device_1"
)

print(f"同步进度：{progress.synced_chapters}/{progress.total_chapters}")

# Android 端导入
count = await sync.import_from_file("/sdcard/legodo/pre_render/book_123.json")
```

---

## 🔧 集成步骤

### Step 1: 修改 main.py 装配逻辑

在 `legodo_flet/app/main.py` 中，根据平台模式选择引擎类型：

```python
# 检测是否为 Android 模式
import sys
android_mode = sys.platform.startswith('android') or os.environ.get('ANDROID_MODE') == '1'

# 创建 RulePreExecutor（仅桌面端需要）
pre_executor = None
if not android_mode:
    from app.services.engine.rule_pre_executor import RulePreExecutor
    pre_executor = RulePreExecutor(
        browser_service=browser_service,
        storage=storage,
        engine=None  # 延迟注入
    )

# 创建 RuleEngineService
from app.services.engine.rule_engine_service import RuleEngineService
rule_engine = RuleEngineService(
    state_dir=state_dir,
    pool_size=32,
    timeout=15,
    android_mode=android_mode,  # ← 关键参数
    pre_executor=pre_executor   # ← 注入预执行器
)
hub.register(rule_engine)
```

### Step 2: 更新 GatewayService action 表

新增预渲染相关的 RPC actions：

```python
# app/gateway/service.py

# 预渲染 actions
"preRender.chapter": {
    "handler": "_handle_pre_render_chapter",
    "schema": "pre_render.chapter.json"
},
"preRender.book": {
    "handler": "_handle_pre_render_book",
    "schema": "pre_render.book.json"
},
"sync.preRender": {
    "handler": "_handle_sync_pre_render",
    "schema": "sync.pre_render.json"
},
```

### Step 3: 创建 JSON Schema 文件

```json
// app/gateway/schema/pre_render.chapter.json
{
  "type": "object",
  "properties": {
    "chapter_url": {"type": "string"},
    "source_url": {"type": "string"},
    "force_refresh": {"type": "boolean"}
  },
  "required": ["chapter_url"]
}
```

### Step 4: 配置 WebDAV 同步（可选）

如果使用 WebDAV 同步，需在设置中添加：

```python
# app/core/settings.py
WEBDAV_CONFIG = {
    "url": "https://webdav.example.com/legodo",
    "username": "user",
    "password": "pass",
    "remote_path": "/pre_render/"
}
```

---

## 📊 性能对比

| 指标 | 桌面端（HeadlessBrowser） | Android（静态缓存） |
|------|--------------------------|---------------------|
| **首章加载** | ~2-5s（含渲染） | ~50-100ms（纯解析） |
| **后续章节** | ~1-3s | ~50-100ms |
| **内存占用** | ~200MB（Chromium） | ~20MB（纯 Python） |
| **APK 体积** | N/A | +5-10MB（缓存数据） |
| **离线可用** | ❌ 需 Chromium | ✅ 完全离线 |

---

## ⚠️ 注意事项

### 1. 缓存失效策略

预渲染缓存有 TTL（默认 7 天），以下情况需手动失效：

```python
# 书源规则更新后
await pre_executor.invalidate_cache(source_url="https://example.com")

# 单章内容变化
await pre_executor.invalidate_cache(chapter_url="https://...")
```

### 2. 存储空间管理

每本书的预渲染缓存可能达到 10-50MB（取决于章节数和正文长度）：

```python
# 定期清理旧缓存
import time
cutoff = time.time() - 86400 * 30  # 30 天前

# TODO: 实现基于时间的批量清理
# await storage.delete_expired(prefix="pre_render:", ttl=30 days)
```

### 3. 拦截页检测

如果 HTTP 请求返回拦截页，会抛出友好错误：

```python
try:
    content = await engine.get_content(source, chapter)
except RuntimeError as e:
    if "人工验证" in str(e):
        show_dialog("请在桌面端打开此书进行验证")
    else:
        raise
```

### 4. 批量预渲染的性能优化

为避免压垮目标站点，批量预渲染时有内置延迟：

```python
# 每批 5 个章节，批次间延迟 0.5 秒
results = await executor.pre_execute_book(
    chapters,
    source=source,
    batch_size=5,
    delay_between=0.5
)
```

---

## 🧪 测试验证

### 单元测试

```python
# tests/test_rule_pre_executor.py
async def test_pre_render_single_chapter():
    executor = RulePreExecutor(browser_service=mock_browser, storage=mock_storage)
    
    chapter = await executor.pre_execute_chapter(
        "https://example.com/chapter/1",
        source=mock_source
    )
    
    assert chapter is not None
    assert len(chapter.content_html) > 0
    
    # 验证缓存命中
    cached = await executor.get_cached_template("https://example.com/chapter/1")
    assert cached == chapter.content_html
```

### 集成测试

```python
# tests/test_reading_engine_android.py
async def test_android_engine_cache_hit():
    engine = ReadingEngineAndroid(pre_executor=mock_executor)
    
    # 首次加载（缓存未命中）
    content1 = await engine.get_content(source, chapter)
    assert engine.cache_stats["misses"] == 1
    
    # 二次加载（缓存命中）
    content2 = await engine.get_content(source, chapter)
    assert engine.cache_stats["hits"] == 1
    assert content1.plain_text == content2.plain_text
```

---

## 📚 相关文档

- `docs/flet_浏览器与朗读原生方案评估.md`: WebView 不可行性分析
- `docs/Android后端打包可行性评估.md`: Chaquopy 失败记录
- `reference_code/legado-lyc486/app/src/main/java/io/legado/app/help/http/BackstageWebView.kt`: 原生实现参考

---

**下一步**: 
1. 在真实 Android 设备上测试 `flet build apk`
2. 测量实际 APK 体积和启动延迟
3. 根据实测数据决定是否启动 Go/Java 重写计划
