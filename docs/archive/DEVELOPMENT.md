# 开发约定 (Development Conventions)

> **已归档（2026-10-16）。** 本文写于 Python 单进程 / WebSocket RPC 时代：文中引用的
> 根 `app/` 共享后端、`LegadoReader.spec`、`test_backend_interaction.py`、
> `rpc_engine_repository.dart` 均已不存在。现行架构为 FFI-only：Flutter 前端
> （`legodo_dart/`）+ Go 引擎（`legodo_dart/backend_go/legodo_engine.dll`），
> 打包与构建见 `legodo_dart/README_BUILD.md`。仅作历史参考，不再维护。

## 子项目独立性原则

本项目采用**双前端 + 共享后端**架构：

```
legodo_booksource_check/
├── app/                    # 共享 Python 后端（服务层）
├── legodo_dart/            # Flutter 前端 + Dart 打包脚本
└── legodo_flet/            # Flet 前端（已归档，保留参考）
```

### 1. 打包脚本互不干扰

**核心规则：** 每个子项目的打包脚本必须完全独立，不得修改或依赖其他子项目的文件。

**具体要求：**

- ✅ `legodo_dart/build_release.py` 只能操作 `legodo_dart/` 目录内的文件
- ✅ `legodo_dart/LegadoReader.spec` 指向共享的 `app/` 后端源码
- ❌ 禁止在打包过程中修改 `app/` 目录的内容
- ❌ 禁止删除或覆盖其他子项目的构建产物

**历史教训：**

之前曾出现 `build_release.py` 试图从 `legodo_flet/app/` 复制代码到根目录 `app/`，导致两个子项目互相污染。正确做法是：

- 共享后端统一放在根目录 `app/`
- 各子项目通过 PyInstaller spec 或 Flutter 配置引用共享后端
- 打包时只生成各自的输出目录（`legodo_dart/backend/`、`legodo_dart/build/`）

### 2. 后端变更的影响评估

**核心规则：** 修改共享后端 (`app/`) 时，必须同时检查对所有子项目的影响。

**检查清单：**

当修改 `app/` 中的以下模块时，需要执行对应的验证：

| 修改范围 | 影响子项目 | 验证步骤 |
|---------|-----------|---------|
| `app/services/gateway/` | legodo_dart, legodo_flet | 重新打包两个子项目，测试 WebSocket 握手 |
| `app/services/sources/` | legodo_dart, legodo_flet | 运行书源列表/搜索/换源功能 |
| `app/services/content/` | legodo_dart, legodo_flet | 测试阅读器加载章节/图片 |
| `app/main.py` | legodo_dart, legodo_flet | 验证 `--serve` 模式启动和 ServiceHub 初始化 |
| `app/gateway/schema/*.json` | legodo_dart, legodo_flet | 更新 PyInstaller spec 的 `datas` 字段 |
| `requirements.txt` | legodo_dart, legodo_flet | 更新 PyInstaller spec 的 `hiddenimports` |

**强制要求：**

1. **修改后端 API 契约时**（新增/删除 action、修改 schema）：
   - 同步更新 `legodo_dart/lib/app/rpc_engine_repository.dart`
   - 运行 `test_backend_interaction.py` 验证 HTTP/WebSocket 协议

2. **修改服务依赖时**（新增第三方库、调整懒加载）：
   - 更新 `legodo_dart/LegadoReader.spec` 的 `hiddenimports` 和 `excludes`
   - 重新打包并测试新包能否正常启动（避免漏打包导致运行时 ImportError）

3. **修改资源文件路径时**（publicsuffixes.gz、schema JSON）：
   - 确认 PyInstaller spec 的 `datas` 字段包含新路径
   - 验证打包后的 exe 能通过相对路径找到资源（不能硬编码绝对路径）

**验证命令：**

```bash
# 修改后端后，快速验证 Dart 版本打包
cd legodo_dart
python build_release.py windows

# 测试打包后的 exe 能否正常握手
test_backend_interaction.py --exe backend/LegadoReader.exe
```

## 打包优化记录

### PyInstaller 体积优化（2026-10-12）

通过排除 Dart 版本不需要的依赖，将包体积从 **244MB 降至 80MB**（减少 67%）：

**排除项（~310MB）：**
- `playwright` (103MB) - Dart 用 `webview_flutter` 替代
- `flet/flet_desktop` (97MB) - `--serve` 无头模式不需要 Flet UI
- `cv2/OpenCV` (112MB) - QR码识别用轻量级 `qrcode` 库即可

**保留项（必需）：**
- `pymupdf` (38MB) - PDF/电子书处理
- `numpy` (27MB) - 数值计算核心
- `PIL/Pillow` (13MB) - 图像处理
- Python 运行时 + DLLs (~15MB)

**配置位置：** `legodo_dart/LegadoReader.spec` 的 `excludes` 列表

## 架构演进历史

- **G0-G4 阶段**（2026-08 ~ 2026-10）：从 Flet 单体迁移到 Flutter + RPC 双进程架构
- **G4-F2**（2026-10-10）：标记 Flet 依赖为可选，完成 Dart 工具域
- **单进程打包**（2026-10-12）：Flutter exe 自动拉起内置 Python 后端，使用相对路径查找机制

详见 `docs/M7打包说明.md` 和各阶段开发日志。
