# 构建与打包指南

本项目包含两个前端子项目，各自有独立的打包流程。

---

## Flet 子项目（Python 桌面端 + Android）

**入口脚本**：`legodo_flet/build_release.py`

```bash
cd legodo_flet

# Windows 单文件 exe（默认，体积小适合分发）
python build_release.py windows

# Windows 目录版（启动快，适合开发测试）
python build_release.py windows --onedir

# macOS / Linux
python build_release.py macos
python build_release.py linux

# Android APK（可直接安装）
python build_release.py apk

# Android AAB（Google Play 上传）
python build_release.py aab
```

产物位置：
- Windows: `legodo_flet/legodo_flet_windows.zip`
- macOS: `legodo_flet/legodo_flet_macos.tar.gz`
- Linux: `legodo_flet/legodo_flet_linux.tar.gz`
- Android APK: `legodo_flet/app.apk`
- Android AAB: `legodo_flet/app.aab`

**架构说明**：Flet 是纯 Python 项目，UI 和后端在同一个进程（ServiceHub）。桌面端使用 `flet pack` 打包为独立可执行文件；Android 使用 `flet build apk/aab` 通过 Flutter 工具链编译。

详见 `legodo_flet/BUILD.md`。

---

## Dart/Flutter 子项目（多平台）

**入口脚本**：`legodo_dart/build_release.py`

```bash
cd legodo_dart
python build_release.py windows   # Windows 桌面版
python build_release.py apk       # Android APK
python build_release.py ios       # iOS IPA（需 macOS + Xcode）
```

产物位置：
- 桌面版：`legodo_dart/legodo_client_windows.zip`
- Android：`legodo_dart/build/app/outputs/flutter-apk/app-release.apk`

详见 `legodo_dart/DEPLOYMENT.md`。

---

## 架构说明

两个子项目共享同一套 Python 后端引擎（LegadoReader），但前端技术栈不同：

| 特性 | Flet | Dart/Flutter |
|------|------|--------------|
| 前端语言 | Python | Dart |
| 后端集成 | 同进程 ServiceHub | 子进程 RPC |
| 打包方式 | flet pack（桌面）/ flet build（Android） | Flutter build + PyInstaller |
| 适用平台 | 桌面端（Windows/macOS/Linux）+ Android | 仅桌面端（Windows/Linux/macOS） |

**关键区别**：Flet 基于 Python，可通过 `flet build apk/aab` 打包为 Android 应用（Python 代码由 Flutter 工具链编译）。Dart/Flutter 版本因后端依赖限制，目前仅支持桌面端。

选择建议：
- **纯桌面场景**：两者均可，Dart/Flutter UI 更流畅
- **需要移动端**：选 Flet（Python 原生支持 Android 打包）
- **快速原型**：Flet（纯 Python，无需 Dart 工具链）
