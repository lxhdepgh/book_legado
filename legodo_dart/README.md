# Legodo Dart 前端（Flutter 主项目）

基于 Flutter 的跨平台桌面/移动端应用，WebView 和 TTS 使用 Dart 原生实现，核心规则引擎通过环回 RPC 调用 Python 后端。

## 项目定位

- **适用场景**：Windows/Linux/macOS 桌面端、Android/iOS 移动端生产环境
- **架构特点**：单进程双轨架构（Flutter exe 自动拉起内置 Python 后端）
- **技术栈**：Flutter + webview_flutter + flutter_tts + PyInstaller 后端

## 目录结构

```
legodo_dart/
├── lib/                    # Dart 源码
│   ├── main.dart          # 入口文件
│   ├── bootstrap.dart     # 引擎启动引导
│   ├── app/               # 应用层（屏幕/控制器）
│   └── core/              # 核心层（RPC 客户端/模型）
├── backend/               # PyInstaller 打包的 Python 后端
│   ├── LegadoReader.exe   # Windows 可执行文件
│   └── _internal/         # 运行时依赖
├── windows/               # Windows 平台配置
├── android/               # Android 平台配置
├── build_release.py       # 多平台一键打包脚本
├── build_release.ps1      # PowerShell 版本打包脚本
└── pubspec.yaml           # Flutter 依赖配置
```

## 启动方式

### 开发模式（Debug）

```bash
cd legodo_dart
flutter run -d windows    # Windows 桌面
flutter run -d chrome     # Web 浏览器
flutter run -d android    # Android 设备
```

### Release 构建

```bash
flutter build windows --release
```

输出：`build/windows/x64/runner/Release/legodo_client.exe`

## 打包方式

### 一键打包（含 Python 后端）

```bash
# Python 脚本（跨平台）
python build_release.py --target windows

# PowerShell 脚本（Windows）
.\build_release.ps1
```

该脚本会：
1. 使用 PyInstaller 打包 Python 后端到 `backend/` 目录
2. 构建 Flutter Release 版本
3. 将后端拷贝到 Flutter 输出目录

### 仅打包 Flutter 前端

```bash
flutter build windows --release
```

### 仅打包 Python 后端

```powershell
.\build_backend.ps1
# 或
pyinstaller --clean ../LegadoReader.spec
```

## 架构说明

### 单进程双轨架构

```
┌─────────────────────────────────────┐
│     legodo_client.exe (Flutter)     │
│  ┌──────────────┐  ┌─────────────┐  │
│  │  WebView     │  │  flutter_   │  │
│  │  (native)    │  │  tts        │  │
│  └──────────────┘  └─────────────┘  │
│                                     │
│  ┌────────────────────────────────┐ │
│  │  环回 HTTP (localhost:PORT)    │ │
│  └────────────────────────────────┘ │
│           ↑                         │
│  ┌────────────────────────────────┐ │
│  │  LegadoReader.exe (Python)     │ │
│  │  ├─ GatewayService (RPC)       │ │
│  │  ├─ RuleEngine (QuickJS)       │ │
│  │  └─ Playwright (CDP)           │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
```

### 通信协议

- **前端 → 后端**：HTTP POST `/rpc`，JSON-RPC 风格信封
- **后端 → 前端**：事件流（WebSocket 或 SSE，用于进度推送）
- **认证**：Bearer Token（启动时动态生成）

## 功能模块

与 Flet 前端功能对齐，包括：
- 书架管理、书源编辑、找书搜索
- 阅读器（正文/漫画/RSS/朗读）
- 工具集（浏览器探针/TTS/字体分析等）
- 设置中心（主题/快捷键/WebDAV）

## 依赖说明

见 `pubspec.yaml`。核心依赖：
- `webview_flutter`: WebView 渲染
- `flutter_tts`: 系统 TTS
- `http`: RPC 通信
- `riverpod`: 状态管理

---

**最后更新**：2026-10-11  
**架构版本**：G4 单进程双轨（Flutter + PyInstaller 后端）
