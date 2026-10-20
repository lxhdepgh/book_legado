# Python 后端 Android 打包可行性评估

**日期**: 2026-10-10
**目标**: 评估将当前 Python 后端服务打包为 Android 可用服务的可行性

## 一、当前架构概览

### 1.1 技术栈
- **语言**: Python 3.12
- **Web 框架**: aiohttp（异步 HTTP 服务器）
- **UI 层**: Flet（桌面端，已解耦）+ Dart/Flutter（移动端前端）
- **通信协议**: HTTP/RPC + WebSocket 事件流
- **打包方式**: PyInstaller（Windows/Linux/macOS）

### 1.2 核心服务清单（31 个）
| 类别 | 服务数量 | 关键依赖 |
|------|---------|---------|
| 书源管理 | 5 | aiohttp, bs4, regex |
| 内容解析 | 4 | quickjs, elementpath, jsonpath_ng |
| 阅读相关 | 4 | pymupdf, ebooklib, lxml |
| RSS/漫画 | 2 | feedparser, Pillow |
| 工具域 | 8 | qrcode, opencv-python-headless, pyttsx3, edge-tts |
| 网关/基础设施 | 8 | aiohttp, aiohttp_socks |

## 二、Android 打包方案对比

### 2.1 方案 A：Chaquopy（推荐 ⭐⭐⭐⭐⭐）

**原理**: Gradle 插件，在 Android APK 中嵌入 CPython 解释器 + Python 包

**优势**:
- ✅ 原生 Android 集成，无需 root
- ✅ 支持大部分纯 Python 包（aiohttp, bs4, regex, quickjs 等）
- ✅ 可调用 Java/Kotlin API（访问文件系统、网络权限等）
- ✅ 已有成功案例（Kivy, BeeWare 项目）
- ✅ 与 Flutter 可通过 MethodChannel 通信

**劣势**:
-  APK 体积增加 ~30-50MB（CPython + 依赖）
- ❌ 部分 C 扩展需交叉编译（opencv-python-headless, pymupdf）
- ❌ 不支持 Playwright（需要桌面 Chromium）

**适用性**: ⭐⭐⭐⭐ 高度适合本项目

---

### 2.2 方案 B：BeeWare / Briefcase

**原理**: 将 Python 应用打包为独立 Android APK

**优势**:
- ✅ 完整 Python 运行时
- ✅ 跨平台工具链统一

**劣势**:
- ❌ 主要针对"纯 Python 应用"，不适合作为后台服务
- ❌ 与 Flutter 集成复杂（两个独立进程）
- ❌ 社区活跃度低于 Chaquopy

**适用性**: ⭐⭐ 不推荐（架构不匹配）

---

### 2.3 方案 C：Termux + 本地服务器

**原理**: 用户在 Termux 中安装 Python + 依赖，手动启动 `--serve`

**优势**:
- ✅ 零开发成本，直接复用现有代码
- ✅ 完整 Python 环境

**劣势**:
- ❌ 用户体验差（需安装 Termux、手动配置）
- ❌ 无法上架 Google Play
-  权限管理困难

**适用性**:  仅适合开发调试

---

### 2.4 方案 D：重写为 Kotlin/Java 服务

**原理**: 用 Android 原生语言重写后端逻辑

**优势**:
- ✅ 性能最优，APK 体积小
- ✅ 完全原生集成

**劣势**:
- ❌ 工作量巨大（31 个服务全部重写）
- ❌ 规则引擎（quickjs, elementpath）无等价替代
- ❌ 维护两套代码库

**适用性**: ⭐ 不现实（成本过高）

---

## 三、依赖兼容性分析

### 3.1 ✅ 可直接使用的包（纯 Python 或有 Android wheels）

| 包名 | 版本 | 用途 | Android 兼容 |
|------|------|------|-------------|
| aiohttp | >=3.9 | HTTP 服务器 | ✅ 纯 Python |
| beautifulsoup4 | >=4.12 | HTML 解析 | ✅ 纯 Python |
| regex | >=2024 | 正则表达式 | ✅ 有 Android wheels |
| quickjs | >=1.19 | JS 规则引擎 | ✅ 有 Android wheels |
| elementpath | >=5.0 | XPath 解析 | ✅ 纯 Python |
| jsonpath-ng | >=1.7 | JSONPath | ✅ 纯 Python |
| charset-normalizer | >=3.0 | 编码检测 | ✅ 纯 Python |
| html5lib | >=1.1 | HTML5 解析 | ✅ 纯 Python |
| opencc-python-reimplemented | >=0.1.7 | 繁简转换 | ✅ 纯 Python |
| fonttools | >=4.55 | 字体处理 | ✅ 纯 Python |
| pillow | >=10.4 | 图像处理 | ✅ 有 Android wheels |
| pycryptodome | >=3.20 | 加密解密 | ✅ 有 Android wheels |
| py7zr | >=0.22 | 7z 解压 | ✅ 纯 Python |
| rarfile | >=4.2 | RAR 解压 | ✅ 纯 Python |
| EbookLib | >=0.18 | EPUB 解析 | ✅ 纯 Python |
| feedparser | >=6.0 | RSS 解析 | ✅ 纯 Python |
| edge-tts | >=6.1 | 在线 TTS | ✅ 纯 Python |
| qrcode | >=7.4 | 二维码生成 | ✅ 纯 Python |
| aiohttp-socks | >=0.10 | SOCKS 代理 | ✅ 纯 Python |

### 3.2 ️ 需特殊处理的包

| 包名 | 问题 | 解决方案 |
|------|------|---------|
| **pymupdf** | C 扩展，需交叉编译 | 使用预编译 Android wheels 或降级为纯 Python PDF 解析 |
| **opencv-python-headless** | C 扩展，体积大 (~30MB) | 交叉编译或使用 OpenCV Android SDK |
| **pyttsx3** | 依赖系统 TTS（Win SAPI/macOS NSSpeech） | Android 改用 `android.speech.tts.TextToSpeech`（通过 Chaquopy Java 桥） |

### 3.3 ❌ 不可用的包（需移除或替换）

| 包名 | 原因 | 替代方案 |
|------|------|---------|
| **playwright** | 需要桌面 Chromium 浏览器 | Android 使用 WebView 或移除浏览器功能 |
| **flet** | 桌面 UI 框架 | 已解耦，Android 不需要 |
| **pywin32** | Windows 专用 | Android 不需要（老板键托盘功能移除） |

---

## 四、功能裁剪建议

### 4.1 必须保留的核心功能
- ✅ 书源管理（导入/导出/检测/修复）
- ✅ 书籍搜索与换源
- ✅ 阅读器（章节加载/书签/进度）
- ✅ RSS 订阅
- ✅ 设置同步（WebDAV）
- ✅ 替换规则
- ✅ 字体管理
- ✅ 二维码生成/识别

### 4.2 可降级或移除的功能
- ⚠️ **浏览器投屏**: 移除（Playwright 不可用），改用 Android WebView
- ⚠️ **本地 TTS**: 移除 pyttsx3，改用 Android 系统 TTS 或 edge-tts
- ❌ **老板键托盘**: 移除（Android 无系统托盘概念）
- ❌ **漫画媒体**: 视情况保留（需解决 opencv 依赖）

---

## 五、推荐实施路径

### Phase 1: 原型验证（2-3 周）
1. 创建 Chaquopy + Flutter 混合项目
2. 移植最小服务子集（GatewayService + SourceService）
3. 验证 HTTP/RPC 通信链路
4. 测试文件读写权限

### Phase 2: 核心功能迁移（4-6 周）
1. 移植所有纯 Python 服务
2. 处理 pymupdf/opencv 交叉编译
3. 实现 Android TTS 桥接
4. Flutter 前端适配（移除浏览器/TTS 占位页）

### Phase 3: 优化与测试（2-3 周）
1. APK 体积优化（ProGuard/R8）
2. 性能基准测试
3. 真机兼容性测试
4. 准备 Google Play 上架材料

---

## 六、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| C 扩展编译失败 | 高 | 提前验证 pymupdf/opencv Android wheels 可用性 |
| APK 体积过大 | 中 | 使用 App Bundle + 按需加载 |
| 内存占用过高 | 中 | 限制并发数，优化 aiohttp 连接池 |
| 权限拒绝 | 低 | 动态申请存储/网络权限，优雅降级 |
| Play Store 政策 | 低 | 确保不包含违规内容，明确声明用途 |

---

## 七、结论

**可行性**: ✅ **高度可行**

**推荐方案**: Chaquopy + Flutter MethodChannel

**预计工作量**: 8-12 周（含测试）

**关键决策点**:
1. 是否保留浏览器功能？（建议移除，改用 WebView）
2. 是否需要离线 TTS？（建议仅保留 edge-tts）
3. 漫画模块是否必需？（影响 opencv 依赖处理）

**下一步行动**:
1. 创建 Chaquopy 原型项目验证技术可行性
2. 测试 pymupdf/opencv Android wheels 可用性
3. 确定功能裁剪范围
4. 制定详细实施计划
