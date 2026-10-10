# Dart/Flutter 实现 WebView + TTS 可行性评估

**日期**: 2026-10-10
**目标**: 评估在 Flutter/Dart 前端直接实现 WebView 渲染和 TTS 朗读的可行性，替代 Python 后端对应功能

## 一、当前架构现状

### 1.1 Python 后端现有实现

| 功能 | 服务 | 技术栈 | 状态 |
|------|------|--------|------|
| **浏览器投屏** | BrowserService | Playwright + CDP + JPEG 帧 | ✅ 桌面端可用 |
| **本地 TTS** | ReadAloudService | pyttsx3 (Win SAPI/macOS NSSpeech) | ✅ 桌面端可用 |
| **在线 TTS** | ReadAloudService | edge-tts (aiohttp) | ✅ 跨平台可用 |

### 1.2 Flutter 前端占位页

| 页面 | 路由 | 当前状态 |
|------|------|---------|
| 浏览器 | `/tools/browser` | ⚠️ 占位（无 WebSocket 客户端） |
| TTS | `/tools/tts` | ⚠️ 占位（无语音列表加载） |

---

## 二、方案 A：Flutter WebView 插件（推荐 ⭐⭐⭐⭐⭐）

### 2.1 技术选型

| 插件 | 版本 | 引擎 | Android/iOS 支持 |
|------|------|------|-----------------|
| **webview_flutter** | ^4.8 | Android WebView / iOS WKWebView | ✅ 官方维护 |
| flutter_inappwebview | ^6.0 | 同上 + 更多 API | ✅ 社区活跃 |
| desktop_webview_window | ^0.2 | 桌面原生 WebView | ✅ Windows/macOS/Linux |

### 2.2 核心能力对比

| 能力 | webview_flutter | flutter_inappwebview | Python Playwright |
|------|----------------|---------------------|------------------|
| 加载 URL | ✅ | ✅ | ✅ |
| HTML 字符串注入 | ✅ | ✅ | ✅ |
| JS 执行 | ✅ evaluateJavascript | ✅ | ✅ page.evaluate |
| DOM 查询 | ❌ 需手动桥接 | ✅ addJavaScriptHandler | ✅ page.query |
| Cookie 管理 | ✅ CookieManager | ✅ | ✅ context.cookies |
| 截图 | ❌ | ✅ takeScreenshot | ✅ page.screenshot |
| 网络拦截 | ❌ | ✅ shouldInterceptRequest | ✅ route.intercept |
| 文件上传 | ✅ | ✅ | ✅ |
| 控制台日志 | ✅ onConsoleMessage | ✅ | ✅ page.on("console") |

### 2.3 优势

- ✅ **零后端依赖**：不需要 Python 进程、不需要 Playwright
- ✅ **原生性能**：Android WebView / iOS WKWebView 是系统组件
- ✅ **APK 体积小**：不打包 Chromium（~100MB）
- ✅ **权限简单**：只需 INTERNET 权限
- ✅ **触摸交互**：原生支持缩放、滚动、长按菜单

### 2.4 劣势

- ❌ **DOM 操作受限**：不能像 Playwright 那样直接 querySelector
-  **调试困难**：WebView 内 JS 错误不易捕获
- ❌ **Android 碎片化**：不同厂商 WebView 内核版本差异大
- ❌ **iOS 限制**：WKWebView 不允许访问文件系统路径

### 2.5 适用场景

✅ **适合**：
- 书源规则调试预览（显示解析后的 HTML）
- 登录验证页面（需要用户输入验证码）
- 简单的网页内容展示

❌ **不适合**：
- 复杂的自动化操作（批量点击、表单提交）
- 需要精确 DOM 操作的规则测试
- 反爬虫对抗（WebView UA 固定，难以伪装）

---

## 三、方案 B：Flutter TTS 插件

### 3.1 技术选型

| 插件 | 版本 | 引擎 | 离线/在线 |
|------|------|------|----------|
| **flutter_tts** | ^4.0 | Android TextToSpeech / iOS AVSpeechSynthesizer | 离线 |
| **tts** | ^3.0 | 同上 | 离线 |
| edge_tts_dart | - | Microsoft Edge TTS API | 在线 |

### 3.2 核心能力对比

| 能力 | flutter_tts | tts | edge_tts_dart | Python pyttsx3 | Python edge-tts |
|------|------------|-----|---------------|---------------|----------------|
| 文本转语音 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 语速调节 | ✅ setSpeechRate | ✅ | ✅ | ✅ | ✅ |
| 音调调节 | ✅ setPitch | ✅ | ✅ | ✅ | ✅ |
| 音量调节 | ✅ setVolume | ✅ | ✅ | ✅ | ✅ |
| 语音列表 | ✅ getVoices | ✅ | ✅ | ✅ | ✅ |
| 暂停/恢复 | ✅ | ✅ |  | ✅ | ❌ |
| 队列播放 | ✅ | ✅ | ❌ | ✅ | ✅ |
| SSML 支持 | ❌ | ❌ | ✅ | ❌ | ✅ |
| 多语言 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 离线可用 | ✅ | ✅ | ❌ | ✅ | ❌ |

### 3.3 优势

- ✅ **flutter_tts 完全离线**：使用系统 TTS 引擎，无需网络
- ✅ **edge_tts_dart 音质更好**：Microsoft 神经语音，接近真人
- ✅ **纯 Dart 实现**：不需要 Python 后端、不需要 COM/SAPI 桥接
- ✅ **跨平台一致**：Android/iOS/Desktop 同一套 API

### 3.4 劣势

- ❌ **flutter_tts 音质一般**：系统默认语音机械感强
- ❌ **edge_tts_dart 需联网**：无法离线使用
- ❌ **Android 语音包缺失**：部分设备未安装中文 TTS 引擎
- ❌ **SSML 支持有限**：flutter_tts 不支持停顿、重音等标记

### 3.5 推荐组合

**最佳实践**：
```dart
// 优先使用系统 TTS（离线），失败时降级到 edge-tts（在线）
class ReadAloudService {
  final FlutterTts _systemTts = FlutterTts();
  final EdgeTtsClient? _onlineTts;

  Future<void> speak(String text) async {
    try {
      // 尝试系统 TTS
      await _systemTts.speak(text);
    } catch (_) {
      // 降级到在线 TTS
      await _onlineTts?.speak(text);
    }
  }
}
```

---

## 四、实施建议

### 4.1 Phase 1：WebView 基础集成（1-2 周）

1. 添加 `webview_flutter: ^4.8` 依赖
2. 实现 `/tools/browser` 页面的 WebView 容器
3. 支持 URL 加载和 HTML 字符串注入
4. 添加基本的 JS 执行能力（用于书源规则测试）

**验收标准**：
- ✅ 能打开任意 URL
- ✅ 能显示书源返回的 HTML 内容
- ✅ 能执行简单的 JS 表达式

### 4.2 Phase 2：TTS 基础集成（1 周）

1. 添加 `flutter_tts: ^4.0` 依赖
2. 实现 `/tools/tts` 页面的语音选择器
3. 支持语速/音调调节
4. 添加播放/暂停/停止控制

**验收标准**：
- ✅ 能列出系统可用语音
- ✅ 能朗读中文文本
- ✅ 能调节语速和音调

### 4.3 Phase 3：高级功能（2-3 周）

1. WebView 添加 Cookie 管理
2. WebView 添加截图功能（用于漫画预览）
3. TTS 添加 edge-tts 在线降级
4. TTS 添加队列播放和书签跳转

**验收标准**：
- ✅ WebView 能保持登录状态（Cookie 持久化）
- ✅ 能截取 WebView 当前画面
- ✅ 离线 TTS 不可用时自动切换在线
- ✅ 长文本能分段朗读并记住进度

---

## 五、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Android WebView 内核过旧 | 中 | 使用 flutter_inappwebview 自带 XWalkView |
| 系统 TTS 语音包缺失 | 高 | 提供 edge-tts 在线降级，引导用户安装语音包 |
| WebView 内存泄漏 | 中 | 及时 dispose，限制并发 WebView 数量 |
| edge-tts API 限流 | 低 | 缓存语音结果，添加重试机制 |
| iOS WKWebView 文件访问限制 | 低 | 使用 data URI 或临时文件方案 |

---

## 六、结论

**可行性**: ✅ **高度可行**

**推荐方案**：
- **WebView**: `webview_flutter`（基础需求）或 `flutter_inappwebview`（需要 DOM 操作）
- **TTS**: `flutter_tts`（离线）+ `edge_tts_dart`（在线降级）

**预计工作量**：4-6 周

**关键决策点**：
1. 是否需要复杂 DOM 操作？（决定用 webview_flutter 还是 flutter_inappwebview）
2. 是否必须支持离线 TTS？（决定是否需要 flutter_tts）
3. 是否需要漫画截图功能？（决定是否需要 flutter_inappwebview）

**下一步行动**：
1. 创建 Flutter WebView 原型验证基本加载能力
2. 测试 flutter_tts 在目标 Android 设备上的语音包可用性
3. 确定最终插件选型
4. 制定详细实施计划
