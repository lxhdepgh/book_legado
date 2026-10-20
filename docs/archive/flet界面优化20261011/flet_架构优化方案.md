# Flet 架构优化方案

**日期**: 2026-10-11  
**状态**: 📋 待评估  
**背景**: 全面对比 Dart 架构后发现的关键缺失点

---

## 🔍 核心问题诊断

### 当前 Flet 架构（三层）

```
┌─────────────────────────────────┐
│        Views (UI 层)            │
│  - bookshelf_view.py            │
│  - reader_view.py               │
│  - source_list_view.py          │
└──────────────┬──────────────────┘
               │ 直接调用
┌──────────────▼──────────────────┐
│     Controllers (业务逻辑层)     │
│  - bookshelf_controller.py      │
│  - reader_controller.py         │
│  - source_list_controller.py    │
└──────────────┬──────────────────┘
               │ hub.get() 返回 EngineProxy
┌──────────────▼──────────────────┐
│    Services (服务层)             │
│  - BookshelfService             │
│  - ReaderService                │
│  - SourceService                │
└─────────────────────────────────┘
```

### Dart 架构（四层，含 Repository 抽象）

```
┌─────────────────────────────────┐
│       Screens (UI 层)           │
│  - bookshelf_screen.dart        │
│  - reader_screen.dart           │
└──────────────┬──────────────────┘
               │ Riverpod Provider
┌──────────────▼──────────────────┐
│    Notifiers (状态管理层)        │
│  - bookshelf_notifier.dart      │
│  - reader_notifier.dart         │
└──────────────┬──────────────────┘
               │ 调用 Repository
┌──────────────▼──────────────────┐
│   Repositories (数据源抽象层)    │
│  - EngineRepository (抽象)      │
│    ├─ RpcEngineRepository       │
│    └─ FakeEngineRepository      │
└──────────────┬──────────────────┘
               │ RPC / WebSocket
┌──────────────▼──────────────────┐
│    GatewayService (服务层)       │
│  - action 表 + JSON Schema      │
└─────────────────────────────────┘
```

### 关键差异

| 维度 | Flet | Dart | 影响 |
|------|------|------|------|
| **数据源抽象** | ❌ 无 Repository 层 | ✅ EngineRepository 抽象 | 🔴 高 |
| **测试替身** | Controller 直连 Service | 可注入 FakeRepository | 🟡 中 |
| **错误处理** | Controller 管理错误状态 | Stream<EngineFailure> | 🟡 中 |
| **状态管理** | Controller 持有状态 | Riverpod Provider | 🟢 低 |
| **事件流** | EventBus (自定义) | EngineEvent (统一契约) | 🟢 低 |

---

## 🎯 架构优化目标

### G1 判据（必须达成）

1. **引入 Repository 抽象层**
   - 定义统一的 `EngineRepository` 接口
   - 提供 `RpcEngineRepository`（真实 RPC 实现）
   - 提供 `FakeEngineRepository`（测试替身）

2. **Controller 改为依赖 Repository**
   - Controller 不再直接调用 `hub.get(Service)`
   - 通过构造函数注入 `repository: EngineRepository`
   - 支持测试时注入 `FakeEngineRepository`

3. **统一错误处理机制**
   - 定义 `EngineFailure` 事件类型
   - Repository 抛出统一异常
   - FailureBanner 订阅失败事件流

### G2 判据（可选增强）

1. **引入状态管理 Provider**
   - 类似 Riverpod 的轻量级状态容器
   - 自动管理生命周期（attach/detach）
   - 支持状态快照回放（用于进度恢复）

2. **完善事件契约**
   - 统一定义所有事件类型（对齐 Dart 的 `EngineEvent`）
   - 事件序列化/反序列化（JSON Schema 验证）
   - 事件回放机制（用于离线测试）

---

## 🛠️ 实施方案

### 方案 A：最小改动（推荐，2-3 天）

#### A1. 定义 EngineRepository 接口

**位置**: `legodo_flet/app/core/repository.py`

```python
"""数据源抽象层（对齐 Dart 的 engine_repository.dart）。

职责：
- 定义所有与后端交互的方法签名
- 抛出的异常统一为 EngineFailure
- 支持测试时注入 FakeEngineRepository
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class EngineFailure(Exception):
    """引擎失败异常（对齐 Dart 的 EngineFailure）。"""
    
    def __init__(self, kind: str, message: str, action: str | None = None):
        self.kind = kind  # "timeout" | "transport" | "schema" | "engine"
        self.message = message
        self.action = action
        super().__init__(message)
    
    @property
    def is_transient(self) -> bool:
        """是否可重试（超时/传输抖动算可重试）。"""
        return self.kind in ("timeout", "transport")


class EngineRepository(ABC):
    """引擎仓库抽象（所有数据访问的统一入口）。"""
    
    # -- 书架域 --------------------------------------------------------- #
    @abstractmethod
    async def get_shelf_items(self) -> list[dict]:
        """获取书架项目列表。"""
        ...
    
    @abstractmethod
    async def open_book(self, book_id: str) -> dict:
        """打开一本书（返回会话信息）。"""
        ...
    
    # -- 书源域 --------------------------------------------------------- #
    @abstractmethod
    async def get_sources(self) -> list[dict]:
        """获取书源列表。"""
        ...
    
    @abstractmethod
    async def import_sources(self, content: str) -> dict:
        """导入书源（返回导入结果）。"""
        ...
    
    # -- 阅读器域 ------------------------------------------------------- #
    @abstractmethod
    async def get_chapters(self, session_id: str) -> list[dict]:
        """获取章节目录。"""
        ...
    
    @abstractmethod
    async def get_content(self, chapter_id: str) -> dict:
        """获取章节正文。"""
        ...
    
    # -- 搜索域 --------------------------------------------------------- #
    @abstractmethod
    async def start_search(self, keyword: str, scope: dict) -> dict:
        """启动搜索任务（mode="job"，返回受理凭据）。"""
        ...
    
    # -- 检测域 --------------------------------------------------------- #
    @abstractmethod
    async def start_check(self, sources: list[str]) -> dict:
        """启动检测任务（mode="job"）。"""
        ...
```

#### A2. 实现 RpcEngineRepository

**位置**: `legodo_flet/app/gateway/rpc_repository.py`

```python
"""RPC 实现的引擎仓库（对接 GatewayService）。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.repository import EngineFailure, EngineRepository
from app.gateway.client import GatewayClient


class RpcEngineRepository(EngineRepository):
    """通过 RPC 调用 GatewayService 的实现。"""
    
    def __init__(self, client: GatewayClient):
        self._client = client
    
    async def get_shelf_items(self) -> list[dict]:
        try:
            result = await self._client.call("bookshelf.list")
            return result.get("items", [])
        except Exception as exc:
            raise EngineFailure("transport", f"获取书架失败: {exc}") from exc
    
    async def open_book(self, book_id: str) -> dict:
        try:
            return await self._client.call("reader.open", {"book_id": book_id})
        except Exception as exc:
            raise EngineFailure("engine", f"打开书籍失败: {exc}") from exc
    
    # ... 其他方法实现
```

#### A3. 实现 FakeEngineRepository

**位置**: `legodo_flet/app/testing/fake_repository.py`

```python"""测试用的假仓库（返回黄金样本）。"""
from __future__ import annotations

from app.core.repository import EngineRepository


class FakeEngineRepository(EngineRepository):
    """返回预定义数据的测试替身。"""
    
    def __init__(self, shelf_items: list[dict] | None = None):
        self._shelf_items = shelf_items or []
        self._fail_next = False
    
    def set_fail_next(self, value: bool):
        """设置下一次调用失败（用于测试错误处理）。"""
        self._fail_next = value
    
    async def get_shelf_items(self) -> list[dict]:
        if self._fail_next:
            from app.core.repository import EngineFailure
            raise EngineFailure("timeout", "模拟超时")
        return self._shelf_items
    
    async def open_book(self, book_id: str) -> dict:
        return {"session_id": "fake-session", "book_id": book_id}
    
    # ... 其他方法返回空数据或黄金样本
```

#### A4. 改造 Controller 依赖注入

**修改前** (`bookshelf_controller.py`):
```python
class BookshelfController(BaseController):
    def on_attach(self):
        self._service = self.service("BookshelfService")
    
    async def refresh(self):
        items = await self._service.list_books()
        self.view.render_books(items)
```

**修改后**:
```python
class BookshelfController(BaseController):
    def __init__(self, repository: EngineRepository, hub=None, bus=None):
        super().__init__(hub, bus)
        self._repo = repository  # 依赖注入
    
    async def refresh(self):
        try:
            items = await self._repo.get_shelf_items()
            self.view.render_books(items)
        except EngineFailure as exc:
            self.view.show_error(exc.message)
```

**装配位置** (`main.py`):
```python
# 生产环境
from app.gateway.rpc_repository import RpcEngineRepository
from app.gateway.client import GatewayClient

client = GatewayClient(hub)
repo = RpcEngineRepository(client)

# 测试环境
# from app.testing.fake_repository import FakeEngineRepository
# repo = FakeEngineRepository(shelf_items=[...])

controller = BookshelfController(repository=repo, hub=hub)
```

### 方案 B：架构级重构（中等工作量，1-2 周）

#### B1. 引入轻量级状态管理

**位置**: `legodo_flet/app/core/provider.py`

```python
"""轻量级状态管理（对齐 Riverpod 的核心概念）。

用法：
    shelf_provider = Provider(lambda ref: BookshelfState())
    
    class BookshelfView:
        def __init__(self, ref: ProviderReference):
            self.state = ref.watch(shelf_provider)
"""
from __future__ import annotations

from typing import Any, Callable


class ProviderReference:
    """Provider 引用（用于 watch/read）。"""
    
    def __init__(self):
        self._providers: dict[str, Any] = {}
        self._listeners: list[Callable] = []
    
    def watch(self, provider: "Provider") -> Any:
        """监听 Provider 状态变化。"""
        key = provider.key
        if key not in self._providers:
            self._providers[key] = provider.create(self)
        return self._providers[key]
    
    def read(self, provider: "Provider") -> Any:
        """读取 Provider 当前值（不监听变化）。"""
        key = provider.key
        if key not in self._providers:
            self._providers[key] = provider.create(self)
        return self._providers[key]


class Provider:
    """状态提供者。"""
    
    def __init__(self, create: Callable[[ProviderReference], Any]):
        self._create = create
        self._key = id(self)
    
    @property
    def key(self) -> int:
        return self._key
    
    def create(self, ref: ProviderReference) -> Any:
        return self._create(ref)


class StateNotifier[T]:
    """状态通知器（类似 Riverpod 的 AsyncNotifier）。"""
    
    def __init__(self, initial_state: T):
        self._state = initial_state
        self._listeners: list[Callable[[T], None]] = []
    
    @property
    def state(self) -> T:
        return self._state
    
    @state.setter
    def state(self, value: T):
        self._state = value
        for listener in self._listeners:
            listener(value)
    
    def add_listener(self, listener: Callable[[T], None]):
        self._listeners.append(listener)
    
    def remove_listener(self, listener: Callable[[T], None]):
        self._listeners.remove(listener)
```

#### B2. 统一事件契约

**位置**: `legodo_flet/app/core/engine_events.py`

```python
"""统一的事件契约（对齐 Dart 的 engine_event.dart）。

所有事件必须继承自 EngineEvent，并实现 to_dict()/from_dict()。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EngineEvent:
    """引擎事件基类。"""
    type: str
    timestamp: float = field(default_factory=lambda: time.time())
    
    def to_dict(self) -> dict:
        return {"type": self.type, "timestamp": self.timestamp}
    
    @classmethod
    def from_dict(cls, data: dict) -> "EngineEvent":
        return cls(type=data["type"], timestamp=data.get("timestamp", 0))


@dataclass(frozen=True)
class SearchFinishedEvent(EngineEvent):
    """搜索完成事件。"""
    total: int = 0
    results: list[dict] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        base = super().to_dict()
        base.update({"total": self.total, "results": self.results})
        return base


@dataclass(frozen=True)
class CheckFinishedEvent(EngineEvent):
    """检测完成事件。"""
    total: int = 0
    passed: int = 0
    failed: int = 0
```

### 方案 C：彻底重构（不推荐，1-2 个月）

完全迁移到 Flutter Web + Python 后端分离架构：

1. **前端**: Flutter Web 直接使用 Dart 代码
2. **后端**: Python FastAPI 提供 REST API
3. **通信**: HTTP + WebSocket（替代当前的 RPC）

**优点**:
- 充分利用 Flutter 生态
- 前后端完全解耦
- 支持浏览器部署

**缺点**:
- 需要重写所有 Python UI 代码
- 失去 Flet 的快速开发优势
- 维护成本翻倍

**结论**: 除非有明确的 Web 部署需求，否则不建议采用。

---

## 📋 实施清单

### 阶段 1：Repository 抽象层（方案 A，2-3 天）

- [ ] **A1**: 定义 `EngineRepository` 接口（~50 行）
- [ ] **A2**: 实现 `RpcEngineRepository`（~200 行）
- [ ] **A3**: 实现 `FakeEngineRepository`（~150 行）
- [ ] **A4**: 改造 3 个核心 Controller（书架/书源/阅读器）
- [ ] **A5**: 更新 main.py 装配逻辑
- [ ] **验收**: 单元测试覆盖率达到 80%

### 阶段 2：统一错误处理（方案 A 补充，1 天）

- [ ] **B1**: 定义 `EngineFailure` 异常类
- [ ] **B2**: 改造 FailureBanner 订阅失败事件
- [ ] **B3**: Controller 统一捕获 EngineFailure
- [ ] **验收**: 错误场景自动化测试通过

### 阶段 3：状态管理增强（方案 B，可选，1-2 周）

- [ ] **C1**: 实现轻量级 Provider 框架
- [ ] **C2**: 改造书架屏幕使用 Provider
- [ ] **C3**: 统一事件契约定义
- [ ] **验收**: 状态管理复杂度降低 30%

---

## 🧪 验收标准

### 功能验收

| 测试场景 | 预期结果 | 实际结果 |
|---------|---------|---------|
| 注入 FakeRepository | Controller 返回黄金样本 | ⏳ 待测 |
| 注入 RpcRepository | Controller 调用真实 RPC | ⏳ 待测 |
| Repository 抛出 EngineFailure | FailureBanner 自动显示 | ⏳ 待测 |
| 切换 Repository 实现 | 无需修改 Controller 代码 | ⏳ 待测 |

### 测试覆盖率

| 模块 | 目标覆盖率 | 测量方法 |
|------|-----------|---------|
| RpcEngineRepository | 80% | pytest --cov |
| FakeEngineRepository | 90% | pytest --cov |
| Controller 适配层 | 70% | pytest --cov |

### 性能指标

| 指标 | 目标值 | 测量方法 |
|------|--------|---------|
| Repository 调用延迟 | < 10ms（不含网络） | 计时器 |
| 内存占用增加 | < 5% | tracemalloc |
| 启动时间增加 | < 100ms | time.time() |

---

## 📊 预估工作量

| 阶段 | 工作量 | 风险 | 收益 |
|------|--------|------|------|
| **阶段 1** | 2-3 天 | 低 | 🔴 高（测试友好性大幅提升） |
| **阶段 2** | 1 天 | 低 | 🟡 中（错误处理统一） |
| **阶段 3** | 1-2 周 | 中 | 🟢 低（状态管理更清晰） |

**建议**: 优先完成阶段 1+2（3-4 天），阶段 3 视需求再定。

---

## 📚 相关文档

- `legodo_dart/lib/core/rpc/engine_repository.dart`: Dart Repository 参考实现
- `app/gateway/methods.py`: GatewayService action 表
- `app/core/events.py`: 当前事件定义
- 记忆 `flet-restoration-api-incompatibility`: Flet 恢复失败的教训

---

**总结**: Flet 当前架构的主要缺失是 **Repository 抽象层**，导致测试困难、错误处理不统一。引入 Repository 层只需 3-4 天，但能显著提升代码质量和测试友好性。状态管理和事件契约的增强是可选的，可根据实际需求决定。
