# Legodo 书源检查 — 构建指南（legodo_dart）

## 架构

Flutter（Dart UI）+ Go FFI 引擎，**单进程**：`legodo_client.exe` 通过 `dart:ffi` 加载同目录的 `legodo_engine.dll`（`Call(action, params)` 直调 + `engine_poll` 事件轮询）。

构建链**不涉及** Python 后端 / PyInstaller / Enigma Virtual Box（均为历史方案，已移除）。

## 一键打包（Windows）

```bash
cd legodo_dart
python build_release.py windows
```

可用参数：

| 参数 | 作用 |
|------|------|
| `--skip-go` | 跳过 Go 编译，复用 `backend_go/legodo_engine.dll` 已有产物 |
| `--skip-flutter` | 跳过 Flutter 编译，复用已有 `build/` 产物 |

输出：

- `dist/legodo_windows/` — 可直接运行的分发目录（双击 `legodo_client.exe`）
- `legodo_windows.zip` — 分发包（dist 目录打包）

其他平台目标：`python build_release.py android`（APK，需系统 `JAVA_HOME` 指向有效 JDK）、`linux`、`macos`。

## 打包内容（脚本自动收集与校验）

| 条目 | 说明 |
|------|------|
| `legodo_client.exe` | Flutter 前端 |
| `flutter_windows.dll`（+pdb） | Flutter 引擎 |
| `data/app.so` | AOT 快照（Windows `--release` 必需） |
| `data/icudtl.dat` | ICU 国际化数据 |
| `data/flutter_assets/` | 资源（shaders/字体/包）——**必须位于 `data/` 下**（runner 以 `DartProject(L"data")` 定位，放错位置会导致图标变方框） |
| `legodo_engine.dll` | Go 规则引擎（c-shared） |
| 插件 DLL | flutter_tts 等 |
| `README.txt` | 分发说明 |

脚本末尾校验关键文件齐全并打印清单，任一缺失即以 `[FATAL]` 退出。

## 前置要求（Windows）

- **Flutter SDK**：`flutter` 在 PATH
- **Go 工具链** + **MinGW-w64**（c-shared 需要 CGO）：脚本会在常见路径自动探测 `go`/`gcc`；缺失时打印安装指引（另见 `backend_go/INSTALL_MINGW.md`）
- **Python 3.10+**：运行 build_release.py 本身（项目使用 conda 环境 `legodo_booksource_check`）

## 运行时数据目录

打包版的书源/书架数据库默认位于系统数据目录（Windows：`%LOCALAPPDATA%\LegadoBookSourceCheck\`）。

隔离测试（不碰真实书库）：

```bash
LEGADO_BOOKSOURCE_DATA_DIR=/path/to/empty_dir ./legodo_client.exe
```
