# Legodo Dart 前端（Flutter 主项目）

基于 Flutter 的跨平台应用，WebView 和 TTS 使用 Dart 原生实现，核心规则引擎为 Go 编译的动态库（`legodo_engine.dll`），经 `dart:ffi` 在**同一进程内**直调（无服务进程、无网络端口）。

## 项目定位

- **适用场景**：Windows/Linux/macOS 桌面端、Android 移动端
- **架构特点**：单进程 FFI 架构（Flutter exe + Go 引擎动态库）
- **技术栈**：Flutter + webview_flutter + flutter_tts + Go c-shared 引擎

## 目录结构

```
legodo_dart/
├── lib/                    # Dart 源码
│   ├── main.dart          # 入口文件
│   ├── bootstrap.dart     # 引擎启动引导
│   ├── app/               # 应用层（屏幕/控制器）
│   └── core/              # 核心层
│       ├── ffi/           # dart:ffi 绑定与动态库查找
│       └── rpc/           # EngineRepository 契约 + FFI 适配器
├── backend_go/            # Go 引擎源码（编译为 legodo_engine.dll）
├── app/                   # 早期 Python 后端实现（仅作历史参照，不参与构建）
├── windows/               # Windows 平台配置
├── android/               # Android 平台配置
├── test/                  # 测试（含需真引擎的活臂）
├── build_release.py       # 多平台一键打包脚本（见 README_BUILD.md）
└── pubspec.yaml           # Flutter 依赖配置
```

## 启动方式

### 开发模式（Debug）

先编译引擎动态库，再用 `LEGODO_ENGINE_DLL` 环境变量指给 Flutter 进程：

```bash
cd backend_go
go build -buildmode=c-shared -o legodo_engine.dll ./pkg/ffi/

cd ..
LEGODO_ENGINE_DLL=$PWD/backend_go/legodo_engine.dll flutter run -d windows
```

不设该环境变量时，运行时只认 exe 同目录的 `legodo_engine.dll`（见 `lib/core/ffi/engine_ffi.dart` 的 `_findLibraryPath()`）。

### 打包（Release）

见 [README_BUILD.md](README_BUILD.md)：

```bash
python build_release.py windows
```

## 架构说明

### 单进程 FFI 架构

```
┌────────────────────────────────────────┐
│          legodo_client.exe             │
│  ┌────────────┐   ┌──────────────────┐ │
│  │  WebView   │   │  UI (Flutter)    │ │
│  │  (native)  │   └────────┬─────────┘ │
│  └────────────┘            │ dart:ffi  │
│                            ▼           │
│  ┌────────────────────────────────┐    │
│  │  legodo_engine.dll (Go)        │    │
│  │  ├─ ServiceHub（服务注册与生命周期）│  │
│  │  ├─ 规则引擎 / goja JS 运行时  │    │
│  │  ├─ HTTP 客户端（连接池）      │    │
│  │  └─ EventRing（事件环）        │    │
│  └────────────────────────────────┘    │
└────────────────────────────────────────┘
```

### 通信协议

- **前端 → 后端**：`Call(action, paramsJSON)` FFI 直调，Go 侧唯一数据入口；返回统一信封 `{ok, data, error, id, v}`
- **后端 → 前端**：事件环轮询 —— 以游标调用 `Call("engine_poll", {"since": n})` 取进度/完成事件
- **生命周期**：`Initialize()`（启动全部服务）→ `Call(…)` → `Cleanup()`；返回的 C 字符串用 `FreeString()` 释放
- **认证**：无（单进程内存直连）

### 引擎数据目录

书源/书架等数据库默认位于系统数据目录（Windows：`%LOCALAPPDATA%\LegadoBookSourceCheck\`），可用 `LEGADO_BOOKSOURCE_DATA_DIR` 覆盖（与旧 Python 后端契约一致）。

## 功能模块

- 书架管理、书源编辑、找书搜索
- 阅读器（正文/漫画/RSS/朗读）
- 工具集（浏览器探针/TTS/字体分析等）
- 设置中心（主题/快捷键/WebDAV）

## 依赖说明

见 `pubspec.yaml`。核心依赖：

- `webview_flutter` / `flutter_tts`：WebView 与系统 TTS（Dart 原生实现）
- `ffi`：dart:ffi 加载 Go 引擎
- `flutter_riverpod`：状态管理
- `go_router`：路由
- `file_picker`：文件选择

---

**最后更新**：2026-10-16
