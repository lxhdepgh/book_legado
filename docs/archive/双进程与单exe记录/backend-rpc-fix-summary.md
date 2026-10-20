# 后端 RPC 交互修复总结

## 问题诊断

通过独立测试脚本 `test_backend_interaction.py`，发现 Dart 版本前后端交互失败的**根本原因**：

### 核心问题 1：缺失 30 个 action 的 schema 文件

G4 阶段新增了多个域的功能（朗读、替换规则、WebDAV、字体管理、二维码、浏览器管理等），但只注册了 action 表，没有创建对应的入参 schema 文件。导致 RPC 调用时抛出 `FileNotFoundError`：

```
FileNotFoundError: action 'browser.close' 没有入参 schema: ...\app\gateway\schema\browser.close.json
```

**受影响的 action**（共 30 个）：
- browser.* (3): open_page, navigate, close
- export.* (2): epub, txt
- font.* (4): scan_system, add_user, remove, select
- qr.* (2): generate, decode
- readaloud.* (7): speak, stop, set_voice, set_rate, profiles, add_profile, remove_profile
- replace.* (5): list, add, update, delete, toggle_global
- update.* (2): check, ignore
- webbridge.* (3): start, stop, status
- webdav.* (3): backup, restore, list_backups

### 核心问题 2：测试脚本未正确初始化 ServiceHub

测试脚本直接实例化 `GatewayService` 而没有通过 `_build_hub()` 构建完整的 ServiceHub，导致：
- GatewayService.hub = None（没有 ServiceHub）
- 所有 RPC 调用失败："GatewayService 未挂到 ServiceHub"
- 业务服务未注册（SourceService、BookCacheService 等）

### 核心问题 3：action 名称拼写错误

测试脚本中使用了 `sources.list`（复数），但实际注册的 action 是 `source.list`（单数）。

## 修复方案

### 1. 批量创建缺失的 schema 文件

为 30 个缺失的 action 创建了入参 schema 文件，存放在 `app/gateway/schema/` 目录下。每个 schema 文件定义了：
- `type`: 参数类型（object）
- `properties`: 参数列表及类型约束
- `required`: 必填参数
- `additionalProperties`: false（禁止额外字段）

示例（`browser.open_page.json`）：
```json
{
  "type": "object",
  "description": "打开内置浏览器页面",
  "properties": {
    "url": {
      "type": "string",
      "description": "要打开的 URL"
    }
  },
  "required": ["url"],
  "additionalProperties": false
}
```

### 2. 修复测试脚本的后端初始化流程

修改 `test_backend_interaction.py` 的 `test_1_handshake()` 函数：

**修改前**（错误）：
```python
from app.services.gateway.service import GatewayService
gateway = GatewayService(port=0, idle_exit_seconds=30)
await gateway.start_server()
```

**修改后**（正确）：
```python
from app.main import _build_hub
from app.services.gateway.service import GatewayService

# 构建完整的 ServiceHub（包含所有业务服务）
hub = _build_hub()

# 创建并注册 GatewayService
gateway = GatewayService(port=0, idle_exit_seconds=30)
hub.register(gateway)

# 启动所有服务
await hub.start_all()

# 启动 Gateway HTTP/WS 服务器
await gateway.start_server()
```

### 3. 修正 action 名称

将测试脚本中的 `sources.list` 改为 `source.list`。

## 验证结果

运行独立测试脚本验证修复：

```powershell
conda activate legodo_booksource_check
cd legodo_dart
python test_backend_interaction.py
```

**预期结果**（已全部通过）：
- ✅ 测试 1: 后端启动 + 握手协议（Gateway.hub = True）
- ✅ 测试 2: HTTP Action 调用（返回 89 个可用 action）
- ✅ 测试 3: WebSocket 事件流（保持连接）
- ✅ 测试 4: 完整请求-响应循环
  - engine.hello: Error: None ✅
  - source.list: Error: None ✅
  - bookshelf.list: Error: None ✅

## 架构说明

### Schema 文件的单一来源原则

`app/gateway/schema/<action>.json` 是唯一的字段定义处：
- Python 服务端用它校验入参
- Dart 客户端的类型由它生成（未来 G1 阶段）
- **不许**在任一侧手写第二份字段表

### ServiceHub 的注册契约

所有业务服务必须通过 `ServiceHub.register()` 注册，并由 `hub.start_all()` 按依赖拓扑启动：
- GatewayService 只是网关入口，不负责业务逻辑
- 业务方法通过 `self.hub.get(ServiceClass)` 获取服务实例
- 未注册的服务会导致 RPC 调用失败

## 相关文件

- `app/gateway/schema/*.json` - 30 个新增的 schema 文件
- `legodo_dart/test_backend_interaction.py` - 后端交互测试脚本（已修复）
- `app/services/gateway/service.py` - GatewayService 实现
- `app/main.py` - `_build_hub()` 函数（服务注册中心）

## 记忆条目

建议保存到项目记忆：
- `rpc-schema-completeness` - 所有 action 必须有对应的 schema 文件
- `test-backend-initialization` - 测试脚本必须通过 `_build_hub()` 初始化完整后端
