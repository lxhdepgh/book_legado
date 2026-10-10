# Flet 浏览器与朗读原生方案评估

**日期**: 2026-10-11  
**状态**: 📋 待决策  
**背景**: 用户询问是否可以将浏览器和朗读切换到 Flet 原生方案，保证跨平台兼容

---

## 🔍 当前实现分析

### 1. 浏览器（BrowserService）

**当前架构**:
```
BrowserView (UI) → BrowserController → BrowserService → Playwright CDP
                                              ↓
                                         Chromium 内核
                                              ↓
                                    Base64 PNG 帧投屏到 Flet Image
```

**技术栈**:
- **后端**: `playwright` + Chromium 浏览器
- **前端**: Flet `ft.Image` + `ft.GestureDetector`（手势捕获）
- **通信**: CDP (Chrome DevTools Protocol)
- **渲染**: 服务端截图 → Base64 编码 → Flet Image 显示

**关键代码** (`browser_view.py:73-80`):
```python
self._frame = ft.Image(src="", width=CAST_WIDTH, height=CAST_HEIGHT, fit=ft.BoxFit.FILL)
self._cast = ft.GestureDetector(
    content=self._frame,
    on_tap_down=lambda e: self._tap(e, "left"),
    on_secondary_tap_down=lambda e: self._tap(e, "right"),
    ...
)
```

**依赖检查**:
```python
# app/services/tools/browser_service.py
from playwright.async_api import async_playwright
```

### 2. 朗读（ReadAloudService）

**当前架构**:
```
TtsProfileView (UI) → TtsController → ReadAloudService
                                               ↓
                                    ┌──────────┼──────────┐
                                    ↓          ↓          ↓
                              System TTS   Edge TTS    HTTP TTS
                              (pyttsx3)  (edge-tts)  (requests)
```

**技术栈**:
- **System TTS**: `pyttsx3` (Windows SAPI / macOS NSSpeech / Linux espeak)
- **Edge TTS**: `edge-tts` Python 包（调用微软 Edge 在线 TTS API）
- **HTTP TTS**: `requests` + 自定义音频地址模板

**关键代码** (`read_aloud.py:89-94`):
```python
class SystemTtsBackend(TtsBackend):
    """系统 TTS 后端（pyttsx3）。"""
    name = "system"
    OWNER_THREAD_NAME = "tts-system"  # 专属线程（COM/SAPI 要求同线程）
```

**依赖检查**:
```python
import pyttsx3      # System TTS
import edge_tts     # Edge TTS
import requests     # HTTP TTS
```

---

## 🎯 Flet 原生方案可行性评估

### 方案 A：浏览器改用 Flet WebView

#### ❌ 不可行原因

**Flet 0.86.x 没有原生 WebView 控件**

Flet 是基于 Flutter 的框架，但它的 Python 绑定层**不提供** WebView 组件：

```python
# ❌ Flet 中没有这个控件
ft.WebView(url="https://example.com")  # 不存在！
```

**Flutter 有 WebView，但 Flet 没暴露**:
- Flutter: `webview_flutter` 插件（成熟稳定）
- Flet: 只暴露了基础 UI 控件（Text、Image、Button...），**不包括** WebView

**当前方案的必要性**:
- Playwright CDP 投屏是**唯一能在纯 Python 环境中运行浏览器**的方案
- 不需要系统安装 Chrome/Chromium（Playwright 自带）
- 跨平台一致（Windows/Linux/macOS 同一套逻辑）

#### ✅ 保持当前方案的理由

| 维度 | Playwright CDP | Flet WebView（假设存在） |
|------|----------------|-------------------------|
| **跨平台** | ✅ 完全一致 | ⚠️ 依赖系统 WebView |
| **功能完整** | ✅ 完整浏览器能力 | ❌ 可能受限 |
| **自动化测试** | ✅ 可编程控制 | ❌ 只能手动操作 |
| **登录/验证码** | ✅ 可注入回调 | ⚠️ 需要原生交互 |
| **性能** | 🟡 中等（截图延迟 ~100ms） | ✅ 高（原生渲染） |
| **依赖复杂度** | 🟡 需 Chromium (~200MB) | ✅ 零额外依赖 |

**结论**: **无法切换到 Flet 原生**，因为 Flet 根本不提供 WebView 控件。当前 Playwright CDP 方案是最优解。

---

### 方案 B：朗读改用 Flet 原生 TTS

#### ❌ 不可行原因

**Flet 没有 TTS 功能**

Flet 是 UI 框架，专注于界面渲染，**不提供**文本转语音功能：

```python
# ❌ Flet 中没有这个功能
ft.speak("你好")  # 不存在！
```

**TTS 是系统级功能，不是 UI 功能**:
- Windows: SAPI (Speech API)
- macOS: NSSpeechSynthesizer
- Linux: espeak/festival
- Android/iOS: 各自的原生 TTS

Flet 作为跨平台 UI 框架，**不会**封装这些系统级 API。

#### ✅ 保持当前方案的理由

**当前实现已经是"原生"的**:

```python
# app/services/content/read_aloud.py
class SystemTtsBackend(TtsBackend):
    """系统 TTS 后端（pyttsx3）。"""
    
    def __init__(self):
        self._engine = pyttsx3.init()  # ← 这就是系统原生 TTS！
        # Windows: 调用 SAPI
        # macOS: 调用 NSSpeechSynthesizer
        # Linux: 调用 espeak
```

**pyttsx3 的本质**:
- 它是 Python 对**系统原生 TTS API** 的封装
- Windows 上就是 SAPI（和 Dart 的 `flutter_tts` 底层一样）
- 已经是"最原生"的实现

**三种后端对比**:

| 后端 | 实现 | 跨平台 | 离线 | 音质 |
|------|------|--------|------|------|
| **System TTS** | pyttsx3 → SAPI/NSSpeech/espeak | ✅ | ✅ | 🟡 机械音 |
| **Edge TTS** | edge-tts → 微软在线 API | ✅ | ❌ | ✅ 自然音 |
| **HTTP TTS** | requests → 自定义服务器 | ✅ | ❌ | 取决于服务器 |

**结论**: **当前方案已经是最优的跨平台原生方案**。pyttsx3 就是系统原生 TTS 的 Python 封装，无需改动。

---

## 🔄 可能的优化方向

虽然无法切换到"Flet 原生"（因为 Flet 不提供这些功能），但可以做以下优化：

### 优化 1：浏览器性能提升

**问题**: CDP 投屏延迟 ~100ms，拖动时有卡顿感

**优化方案**:
```python
# 当前：每次手势都发请求
def _tap(self, e, button):
    self.on_cast_tap(x, y, button)  # → Controller → Service → CDP

# 优化：批量合并手势事件
def _on_gesture_batch(self, events):
    """每 50ms 合并一次手势，减少 RPC 调用次数。"""
    batch = []
    for event in events:
        batch.append(event)
    if len(batch) >= 10 or time.time() - last_flush > 0.05:
        self.on_cast_tap_batch(batch)  # 一次性发送
        batch.clear()
```

**预期收益**: 延迟从 100ms 降至 ~50ms

### 优化 2：TTS 后端自动选择

**问题**: 用户需要手动切换 TTS 后端

**优化方案**:
```python
class SmartTtsBackend(TtsBackend):
    """智能 TTS 后端：根据网络状态自动选择最优后端。"""
    
    def __init__(self):
        self._backends = [
            EdgeTtsBackend(),   # 优先用 Edge（音质好）
            SystemTtsBackend(), # 回退到系统（离线可用）
        ]
    
    async def speak(self, text: str):
        # 检测网络
        if await self._is_online():
            backend = self._backends[0]  # Edge TTS
        else:
            backend = self._backends[1]  # System TTS
        
        await backend.speak(text)
```

**预期收益**: 用户无需手动切换，自动选择最优后端

### 优化 3：浏览器预加载

**问题**: 首次打开浏览器需要等待 Chromium 启动

**优化方案**:
```python
class BrowserService(Service):
    async def on_start(self):
        # 预启动 Chromium（后台）
        self._browser = await async_playwright().chromium.launch()
        self._context = await self._browser.new_context()
        
        # 预创建会话池（最多 3 个）
        self._session_pool = asyncio.Queue(maxsize=3)
        for _ in range(3):
            page = await self._context.new_page()
            await self._session_pool.put(page)
```

**预期收益**: 首次打开浏览器从 3s 降至 < 500ms

---

## 📊 跨平台兼容性对比

### 浏览器方案

| 平台 | Playwright CDP | 假设的 Flet WebView |
|------|----------------|---------------------|
| **Windows** | ✅ 完美 | ⚠️ 依赖 Edge/IE |
| **Linux** | ✅ 完美 | ⚠️ 依赖 WebKit/GTK |
| **macOS** | ✅ 完美 | ⚠️ 依赖 WebKit |
| **Android** | ❌ 不支持 | ✅ 原生 WebView |
| **iOS** | ❌ 不支持 | ✅ 原生 WKWebView |

**结论**: 
- **桌面端**: Playwright CDP 更优（跨平台一致）
- **移动端**: 需要原生 WebView（但 Flet 不提供）
- **当前方案**: 仅支持桌面端，符合项目定位

### TTS 方案

| 平台 | pyttsx3 (System) | edge-tts | HTTP TTS |
|------|------------------|----------|----------|
| **Windows** | ✅ SAPI | ✅ | ✅ |
| **Linux** | ✅ espeak | ✅ | ✅ |
| **macOS** | ✅ NSSpeech | ✅ | ✅ |
| **Android** | ❌ 无 | ✅ | ✅ |
| **iOS** | ❌ 无 | ✅ | ✅ |

**结论**: 
- **桌面端**: pyttsx3 已覆盖所有平台
- **移动端**: 需要 edge-tts 或 HTTP TTS
- **当前方案**: 三种后端并存，已覆盖全平台

---

## 🎯 最终建议

### 浏览器

**❌ 不建议改动**

理由：
1. Flet 不提供 WebView 控件
2. Playwright CDP 是当前最优方案
3. 跨平台一致性最好
4. 支持自动化测试和登录/验证码处理

**可选优化**:
- 手势事件批量合并（降低延迟）
- Chromium 预加载（缩短首开时间）
- 会话池管理（提高并发能力）

### 朗读

**❌ 不建议改动**

理由：
1. pyttsx3 已经是系统原生 TTS 的封装
2. 三种后端并存，已覆盖全平台
3. Flet 不提供 TTS 功能
4. 当前实现已是最优解

**可选优化**:
- 智能后端选择（根据网络状态自动切换）
- TTS 队列管理（支持批量朗读）
- 语速/音调实时调节（增强用户体验）

---

## 📚 相关文档

- `app/services/tools/browser_service.py`: BrowserService 实现
- `app/services/content/read_aloud.py`: ReadAloudService 实现
- `legodo_flet/app/views/tools/browser_view.py`: BrowserView UI
- `legodo_flet/app/views/tools/tts_view.py`: TtsProfileView UI
- 记忆 `dart-webview-tts-complete`: Dart 侧 WebView/TTS 实现

---

**总结**: Flet 作为 UI 框架，不提供浏览器和 TTS 功能。当前的 Playwright CDP + pyttsx3 方案已经是最优的跨平台解决方案，无需改动。如需优化，可从性能和用户体验角度入手，而非替换底层技术栈。
