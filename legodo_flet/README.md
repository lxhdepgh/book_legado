# Legodo Flet 前端（Python UI）

基于 Flet 0.86.x 的 Python 前端实现，提供完整的书架、阅读、书源管理等 UI 功能。

## 项目定位

- **适用场景**：快速原型开发、桌面端调试、Android APK 打包
- **架构特点**：服务层与 UI 层解耦，支持独立运行或连接外部后端
- **技术栈**：Flet (Flutter Python 封装) + Material/Cupertino Widgets

## 目录结构

```
legodo_flet/
├── app/                    # 应用代码
│   ├── main.py            # 入口文件
│   ├── controllers/       # 控制器层
│   ├── views/             # 视图层（UI 组件）
│   ├── core/              # 核心工具（Repository 抽象层等）
│   └── gateway/           # RPC 通信客户端
├── requirements.txt       # Python 依赖（Flet 标记为可选）
├── requirements-desktop.txt  # 桌面端额外依赖
├── LegadoReader.spec      # PyInstaller 打包配置
└── build_backend.ps1      # Python 后端打包脚本
```

## 启动方式

### 1. 独立模式（本地 ServiceHub + Flet UI）

```bash
cd legodo_flet
python app/main.py
```

### 2. 无头后端模式（仅 GatewayService）

```bash
python app/main.py --serve
```

### 3. 环回模式（连接外部 Python 后端）

```bash
python app/main.py --connect http://127.0.0.1:PORT --token TOKEN
```

### 4. 冒烟测试（验证冻结包依赖）

```bash
python app/main.py --smoke-test
```

## 打包方式

### Windows 桌面版（PyInstaller）

```powershell
# 使用 PowerShell 脚本
.\build_backend.ps1

# 或手动执行
pyinstaller --clean LegadoReader.spec
```

输出目录：`dist/LegadoReader/`

### Android APK（Flet CLI）

```bash
pip install flet>=0.86.5
flet build apk --release
```

输出：`build/app/outputs/flutter-apk/app-release.apk`  
预期体积：80-120 MB（含 Python 解释器 + 所有依赖）

## 功能模块

- **书架**：书籍列表、阅读进度管理
- **书源**：列表/编辑/检测/导入/规则修复
- **找书**：多源并发搜索
- **阅读**：正文/漫画/RSS/朗读/书签/全文搜索/换源
- **工具**：浏览器探针/字体分析/二维码/TTS/更新检查/WebDAV
- **设置**：外观主题/快捷键配置

## 依赖说明

见 `requirements.txt`。Flet 依赖已标记为可选（G4-F2），因为生产环境使用 Flutter/Dart 前端。

---

**最后更新**：2026-10-11  
**参考提交**：`f867150`（最后纯 Flet 版本）
