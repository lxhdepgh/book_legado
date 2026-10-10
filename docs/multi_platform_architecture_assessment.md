# 多平台架构评估报告

**日期**: 2026-10-10
**版本**: Flutter 迁移后 v1.0
**目标平台**: Windows / Android / (未来可能 iOS/macOS/Linux)

---

## 一、当前架构概览

### 1.1 双进程架构

```
┌─────────────────────┐         ┌──────────────────────┐
│   Flutter Client    │         │   Python Service     │
│   (UI + State)      │◄───────►│   (Engine + Logic)   │
│                     │  RPC/   │                      │
│ - Riverpod          │  HTTP   │ - GatewayService     │
│ - GoRouter          │         │ - Action Table       │
│ - Material UI       │         │ - Business Services  │
└─────────────────────┘         └──────────────────────┘
```

**通信协议**:
- **RPC over HTTP**: `POST /rpc` JSON-RPC 2.0 风格（同步调用）
- **Event Stream**: `GET /events` SSE（服务端推送进度/状态）
- **WebSocket**: `/ws`（反向 WebView 请求帧）

### 1.2 分层设计

| 层级 | Flutter 侧 | Python 侧 |
|------|-----------|----------|
| **表现层** | Material 3 Widgets | (已移除 Flet) |
| **状态层** | Riverpod Notifier | - |
| **数据层** | EngineRepository (抽象) | GatewayService + Action Table |
| **服务层** | - | SourceService/ReadingService/... |
| **存储层** | - | SQLite + File System |
| **引擎层** | - | Legado Rule Engine (QuickJS) |

---

## 二、多平台兼容性分析

### 2.1 已支持平台

| 平台 | Flutter UI | Python Backend | 状态 |
|------|-----------|---------------|------|
| **Windows x64** | ✅ 原生 | ✅ PyInstaller exe | **生产就绪** |
| **Android** | ✅ 原生 APK | ❌ 不适用 | **需重新设计** |

### 2.2 核心问题：Android 无法运行 Python 后端

**根本原因**:
1. Android 没有原生 Python 运行时
2. PyInstaller 打包的是 Windows PE 格式，无法在 ARM Linux 上运行
3. Legado 规则引擎依赖 QuickJS（C 扩展），跨平台编译复杂

**影响范围**:
- **所有业务逻辑**都在 Python 侧（书源解析/书架管理/阅读引擎/替换规则/字体扫描/备份恢复/...）
- Flutter 侧只有 UI 和状态管理，**零业务逻辑**
- 如果要在 Android 上运行，必须重写整个 Python 后端为 Kotlin/Java 或 Dart

---

## 三、架构重新设计方案

### 方案 A：保持双进程，Android 用 Termux/Chaquopy（不推荐）

**思路**: 在 Android 上嵌入 Python 运行时

**优点**:
- 代码复用率高（Python 后端无需重写）
- 快速验证可行性

**缺点**:
- **性能差**: Python 在 Android 上启动慢、内存占用高
- **体积大**: APK 需打包 Python 解释器 + 所有依赖（预计 >100MB）
- **生态差**: Chaquopy 商业许可、Termux 非官方支持
- **维护难**: 两个平台的打包流程完全不同

**结论**: ❌ **不推荐** — 技术债会越积越多

---

### 方案 B：Flutter 单体应用，业务逻辑迁到 Dart（推荐）

**思路**: 将 Python 侧的所有业务逻辑重写为 Dart

**架构变化**:
```
┌──────────────────────────────────────┐
│       Flutter Monolith App           │
│                                      │
│  ┌─────────────┐  ┌───────────────┐  │
│  │  UI Layer   │  │  Business     │  │
│  │  (Widgets)  │  │  Logic (Dart) │  │
│  └─────────────┘  └───────────────┘  │
│                                      │
│  ┌─────────────┐  ┌───────────────┐  │
│  │  State Mgmt │  │  Data Access  │  │
│  │  (Riverpod) │  │  (SQLite/Fs)  │  │
│  └─────────────┘  └───────────────┘  │
│                                      │
│  ┌─────────────────────────────────┐  │
│  │  Legado Rule Engine (Dart FFI)  │  │
│  └─────────────────────────────────┘  │
└──────────────────────────────────────┘
```

**优点**:
- ✅ **真正跨平台**: Windows/Android/iOS/macOS/Linux 一套代码
- ✅ **性能好**: 无进程间通信开销
- ✅ **体积小**: 单一可执行文件，无 Python 依赖
- ✅ **易维护**: 统一的技术栈，统一的打包流程

**缺点**:
- ⚠️ **工作量大**: 需要重写所有 Python 业务逻辑（约 50+ 服务类）
- ⚠️ **引擎移植**: Legado 规则引擎（QuickJS）需用 Dart FFI 封装或纯 Dart 重写

**工作量估算**:
| 模块 | Python 代码量 | Dart 重写预估 | 优先级 |
|------|-------------|--------------|--------|
| 书源管理 (SourceService) | ~2000 行 | 2-3 周 | P0 |
| 书架管理 (Bookshelf) | ~800 行 | 1 周 | P0 |
| 阅读引擎 (ReadingService) | ~3000 行 | 3-4 周 | P0 |
| 规则引擎 (LegadoParser) | ~5000 行 | 4-6 周 | P0 |
| 检查服务 (CheckService) | ~1500 行 | 2 周 | P1 |
| 工具域 (10 个服务) | ~3000 行 | 3-4 周 | P1 |
| RSS/漫画/朗读 | ~2000 行 | 2-3 周 | P2 |
| **合计** | **~17,300 行** | **17-25 周** | - |

**结论**: ✅ **强烈推荐** — 长期来看是最优解

---

### 方案 C：混合架构，核心引擎保留 Python，UI 层全 Dart（折中）

**思路**:
- **核心引擎**（Legado 规则解析）保留 Python，通过 FFI 或子进程调用
- **业务逻辑**（书源/书架/阅读/工具）全部迁到 Dart
- **数据存储**用 SQLite（Dart 侧直接访问）

**优点**:
- ✅ 避免重写最复杂的规则引擎（5000 行 QuickJS 集成）
- ✅ 大部分业务逻辑享受跨平台好处

**缺点**:
- ⚠️ 仍需处理 Python 子进程的跨平台打包
- ⚠️ 架构复杂度介于 A 和 B 之间

**结论**: ⚠️ **可选** — 如果规则引擎移植风险太高，可作为过渡方案

---

## 四、推荐路线图

### 阶段 1：当前状态巩固（已完成）✅

- [x] Flutter UI 框架搭建完成
- [x] 路由系统集成（GoRouter + TabbedGroup）
- [x] 工具域 10 个屏幕全部实现
- [x] Dart 仓库接口层完整（35 个 action）
- [x] Python 侧 action 表完整（35 个方法）

### 阶段 2：Windows 平台完善（进行中）🔄

- [ ] 浏览器工具 CDP 投屏 UI 实现
- [ ] 词典查词功能接入
- [ ] 替换规则 CRUD 完善（更新单个规则启用状态）
- [ ] 字体管理文件选择器集成
- [ ] WebDAV 配置界面
- [ ] 导出功能路径选择器
- [ ] TTS 语音档管理完善

### 阶段 3：架构决策与规划（待开始）⏳

- [ ] 团队讨论：选择方案 B（纯 Dart）还是方案 C（混合）
- [ ] 如果选 B：制定规则引擎移植计划
- [ ] 如果选 C：设计 Python 子进程通信协议

### 阶段 4：Android 适配（待开始）⏳

**如果选择方案 B（纯 Dart）**:
- [ ] 移植书源管理服务（SourceService → Dart）
- [ ] 移植书架管理（Bookshelf → Dart + SQLite）
- [ ] 移植阅读引擎（ReadingService → Dart）
- [ ] 移植规则引擎（LegadoParser → Dart FFI 或纯 Dart）
- [ ] 移植工具域服务（10 个服务 → Dart）
- [ ] Android UI 适配（Material 3 自动适配，工作量小）
- [ ] APK 打包与测试

**如果选择方案 C（混合）**:
- [ ] 设计 Python 子进程打包方案（PyInstaller for Android?）
- [ ] 移植业务逻辑到 Dart（排除规则引擎）
- [ ] 设计 Dart ↔ Python 通信协议（JSON-RPC over stdio）
- [ ] Android UI 适配 + 子进程管理

### 阶段 5：其他平台（可选）⏳

- [ ] macOS: Flutter 原生支持，工作量小
- [ ] Linux: Flutter 原生支持，工作量小
- [ ] iOS: 需注意 Apple 审核政策（电子书阅读器可能受限）

---

## 五、关键技术决策点

### 5.1 规则引擎移植策略

**选项 1**: Dart FFI 封装 QuickJS
- 优点：完全兼容 Legado 规则
- 缺点：需要维护 C 绑定，跨平台编译复杂

**选项 2**: 纯 Dart 重写 JS 引擎
- 优点：真正跨平台，无 native 依赖
- 缺点：工作量大（需实现 eval/正则/DOM 模拟）

**选项 3**: 使用现有 Dart JS 引擎（如 `dart_js` 或 `quickjs.dart`）
- 优点：减少重复造轮子
- 缺点：需验证兼容性和性能

**推荐**: 先调研选项 3，不可行再考虑选项 1

### 5.2 数据存储方案

**当前**: Python sqlite3 + 文件系统

**Dart 侧选项**:
- **SQLite**: `sqflite` (Android/iOS) + `sqlite3` (Windows/Linux/macOS)
- **Hive**: 轻量级 NoSQL，适合简单键值对
- **Isar**: 高性能 Dart 原生数据库

**推荐**: SQLite（与 Python 侧保持一致，便于数据迁移）

### 5.3 网络请求

**当前**: Python aiohttp

**Dart 侧**: `http` 包 + `dio`（高级功能）

**推荐**: `dio`（支持拦截器/重试/取消，与 aiohttp 功能对齐）

---

## 六、总结与建议

### 6.1 当前架构评分

| 维度 | Windows | Android | 综合 |
|------|---------|---------|------|
| **功能完整性** | 9/10 | 2/10 | 5.5/10 |
| **性能** | 8/10 | N/A | 8/10 |
| **可维护性** | 7/10 | 3/10 | 5/10 |
| **跨平台能力** | 10/10 | 0/10 | 5/10 |
| **开发效率** | 8/10 | 2/10 | 5/10 |

**综合评价**: ⚠️ **当前架构仅适用于 Windows 桌面场景**

### 6.2 最终建议

1. **短期（1-2 个月）**: 保持当前双进程架构，完善 Windows 平台功能
2. **中期（3-6 个月）**: 启动方案 B（纯 Dart 单体）迁移，优先移植核心引擎
3. **长期（6-12 个月）**: 完成 Android 适配，实现真正跨平台

**关键理由**:
- 双进程架构在 Windows 上工作良好，无需立即推翻
- 但若要支持 Android，**必须**将业务逻辑迁到 Dart
- 早迁比晚迁好：越早统一技术栈，后续维护成本越低

### 6.3 风险提示

⚠️ **规则引擎移植是最大风险点**:
- Legado 规则涉及 JS eval/正则/CSS 选择器/DOM 操作
- 纯 Dart 重写需确保 100% 兼容现有规则库
- 建议先做小规模 PoC（选 10-20 个典型规则验证）

⚠️ **数据迁移需谨慎**:
- Python SQLite schema 需与 Dart 侧保持一致
- 建议提供数据导出/导入工具，方便用户切换版本

---

**文档维护者**: Qoder Agent
**最后更新**: 2026-10-10
**下次评审**: 完成阶段 2 后（预计 2026-11）
