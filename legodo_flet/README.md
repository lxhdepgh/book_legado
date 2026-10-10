# Legodo Flet 前端（Android 打包专用）

本目录包含从 commit `f867150`（2026-10-08）恢复的完整 Flet UI 代码，用于 Android APK 打包。

## 架构说明

- **服务层**：复用主项目 `app/services/` 下的 91 个服务文件（零 Flet 依赖）
- **UI 层**：本目录 `app/views/` + `app/controllers/`（62 个 Python 文件）
- **通信协议**：可选环回 HTTP + WebSocket（连接外部后端）或进程内直接调用

## 启动方式

### 独立模式（Flet 自带后端）

```bash
cd legodo_flet
python app/main.py
```

### 环回模式（连接外部 Python 后端）

```bash
python app/main.py --connect http://127.0.0.1:PORT --token TOKEN
```

## Android 打包

```bash
# 1. 安装 Flet CLI
pip install flet>=0.86.5

# 2. 构建 APK
cd legodo_flet
flet build apk --release

# 输出：build/app/outputs/flutter-apk/app-release.apk
```

**预期体积**：80-120 MB（含 Python 解释器 + 所有 pip 依赖）

## 功能模块

- **书架**：书籍列表、阅读进度管理
- **书源**：列表/编辑/检测/导入/规则修复
- **找书**：多源并发搜索
- **阅读**：正文/漫画/RSS/朗读/书签/全文搜索/换源
- **工具**：浏览器探针/词典/导出/字体分析/二维码/替换规则/TTS/更新检查/WebDAV
- **设置**：外观主题/快捷键配置

## 依赖

见 `requirements.txt`（Flet 依赖已标记为可选）

## 技术栈

- **Flet 0.86.5**：Flutter 渲染引擎的 Python 封装
- **Material/Cupertino Widgets**：标准 Flutter 设计系统
- **PyInstaller**：Windows 桌面端打包工具
- **Flet CLI**：Android/iOS 打包工具

---

**恢复日期**：2026-10-11  
**参考提交**：`f867150`（最后纯 Flet 版本）
