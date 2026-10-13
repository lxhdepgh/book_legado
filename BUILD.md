# 构建与打包指南

本项目包含两个前端子项目，各自有独立的打包流程。

---

## Flet 子项目（Python 桌面端）

**入口脚本**：`legodo_flet/build_release.py`

```bash
cd legodo_flet
python build_release.py windows   # Windows 桌面版
python build_release.py linux     # Linux 桌面版
python build_release.py macos     # macOS 桌面版
```

产物位置：`legodo_flet/dist/legodo_client_windows.zip`

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
| 打包方式 | PyInstaller（含 Android APK） | Flutter build + PyInstaller |
| 适用平台 | 桌面端 + Android | 仅桌面端（Windows/Linux/macOS） |

**关键区别**：由于后端是 Python，Dart/Flutter 版本无法直接打包为移动端（需要将 Python 运行时嵌入 APK，目前无成熟方案）。Flet 版本基于 Python，可通过 Chaquopy 或 Pydroid3 打包为 Android APK。

选择建议：
- **纯桌面场景**：两者均可，Dart/Flutter UI 更流畅
- **需要移动端**：选 Flet（Python 原生支持 Android 打包）
- **快速原型**：Flet（纯 Python，无需 Dart 工具链）
