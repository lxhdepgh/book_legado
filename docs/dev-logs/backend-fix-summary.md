# 后端交互问题修复总结

## 问题诊断

通过独立测试脚本 `test_backend_interaction.py`，发现 Dart 版本前后端交互失败的**根本原因**：

### 核心问题：Python 后端源码分裂

项目存在**两个 app/ 目录**：

1. **项目根目录 `app/`**（完整服务层）
   - ✅ 包含 `services/`、`core/`、`gateway/` 等完整服务代码
   - ✅ GatewayService、ServiceHub、所有业务服务都在这里
   - ❌ 没有 `main.py` 启动入口

2. **`legodo_flet/app/`**（Flet UI 层）
   - ✅ 包含 `views/`、`controllers/` 等 Flet UI 代码
   - ✅ 有 `main.py` 启动入口
   - ❌ **没有 `services/` 目录**，缺少所有服务层代码

### PyInstaller 打包错误

原 spec 文件指向 `legodo_flet/app/main.py`：

```python
a = Analysis(
    [os.path.join(_legodo_flet, "app", "main.py")],  # ❌ 只有 UI 层
    pathex=[_legodo_flet],
)
```

导致打包后的 exe **缺少服务层代码**，运行时：
- GatewayService.hub = None（没有 ServiceHub）
- 所有 action 调用失败："GatewayService 未找到 ServiceHub"
- WebSocket 立即关闭（因为没有活跃会话）

### Dart 侧表现

用户看到的错误：
```
EngineFailure(transport @action=ws): 引擎那头的 /ws 关掉了会话
```

这是**症状**，不是**病因**。真正的问题是后端服务层缺失。

## 修复方案

### 1. 复制 main.py 到根目录

```bash
cp legodo_flet/app/main.py app/main.py
```

这样根目录的 `app/` 就同时拥有：
- ✅ 完整的 `services/` 服务层
- ✅ `main.py` 启动入口（`--serve` 模式不依赖 Flet）

### 2. 修改 PyInstaller spec

更新 `legodo_dart/LegadoReader.spec`：

```python
# 计算项目根目录和 app 源码路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.join(_script_dir, "..")
_app_source = os.path.join(_project_root, "app")  # ✅ 使用根目录的 app/

a = Analysis(
    [os.path.join(_app_source, "main.py")],  # ✅ 打包入口：根目录的 main.py
    pathex=[_project_root],  # ✅ 项目根目录作为搜索路径
    ...
    datas=[
        (os.path.join(_app_source, "resources", "publicsuffixes.gz"), "app/resources"),
        (os.path.join(_app_source, "gateway", "schema", "*.json"), "app/gateway/schema"),
    ],
)
```

### 3. 重新打包

```powershell
cd legodo_dart
python build_release.py windows
```

## 验证方法

运行独立测试脚本验证修复：

```powershell
conda activate legodo_booksource_check
cd legodo_dart
python test_backend_interaction.py
```

预期结果：
- ✅ 测试 1: 后端启动 + 握手协议
- ✅ 测试 2: HTTP Action 调用（返回实际数据）
- ✅ 测试 3: WebSocket 事件流（保持连接）
- ✅ 测试 4: 完整请求-响应循环

## 架构说明

### 为什么 Flet 版本能工作？

Flet 版本在开发环境中直接运行 Python，解释器可以从项目根目录找到 `app.services` 模块。这不是架构正确，而是**开发环境的便利掩盖了问题**。

### 为什么 Dart 版本失败？

Dart 版本通过打包后的 exe 运行，exe 中只包含了 `legodo_flet/app/` 的代码，缺少服务层。这暴露了**源码组织的缺陷**。

### 正确的架构应该是？

**选项 A（当前修复）**：
- 根目录 `app/` 作为唯一的 Python 后端源码
- 包含完整的服务层 + main.py 启动入口
- `legodo_flet/app/` 只保留 Flet UI 代码

**选项 B（更彻底的分离）**：
- 创建独立的 `backend/` 目录存放 Python 后端
- 包含完整的服务层 + main.py
- `legodo_flet/app/` 和 `legodo_dart/lib/` 都作为客户端
- 需要重构导入路径和服务注册

当前采用**选项 A**，因为改动最小，且符合现有代码结构。

## 相关文件

- `legodo_dart/LegadoReader.spec` - PyInstaller 打包配置（已修复）
- `legodo_dart/build_release.py` - 一键打包脚本（已包含环境检测）
- `legodo_dart/test_backend_interaction.py` - 后端交互测试脚本
- `legodo_dart/test_backend_issues.py` - 问题诊断报告
- `app/main.py` - Python 后端启动入口（从 legodo_flet/app/ 复制）

## 记忆条目

已保存到项目记忆：
- `gateway-service-requires-servicehub.md` - GatewayService 必须挂载 ServiceHub
- `rpc-envelope-format.md` - RPC 信封格式规范
- `subproject-independence-principle.md` - 子项目独立性原则
