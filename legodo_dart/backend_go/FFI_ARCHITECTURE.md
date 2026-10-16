# Go 后端 FFI 架构说明

## 架构对比

### 方案 A：独立进程 + WebSocket RPC（历史方案，已弃用——WS 链已从双侧拆除）

```
┌─────────────┐         WebSocket          ┌──────────────┐
│  Flutter    │  ←─────────────────────→   │  Go Server   │
│  (Dart UI)  │      TCP :8080             │  (HTTP+WS)   │
└─────────────┘                            └──────────────┘
     │                                             │
     │                                             │
     ▼                                             ▼
  WebView/TTS                                  Rule Engine
                                               JS Runtime
                                               HTTP Client
```

**优点：**
- 进程隔离，崩溃不影响主程序
- 可独立调试和重启后端
- 支持远程部署

**缺点：**
- 需要单独启动和管理进程
- WebSocket 通信有网络开销
- 分发时需要两个可执行文件
- Windows 下需处理防火墙提示

---

### 方案 B：动态库 + FFI 直接调用（现行方案）

```
┌──────────────────────────────────────────┐
│         Flutter Application               │
│  ┌─────────────┐      dart:ffi           │
│  │  Dart UI    │  ←──────────────→       │
│  └─────────────┘   C ABI Interface       │
│                           │               │
│                           ▼               │
│                  ┌─────────────────┐      │
│                  │  Go DLL/SO/DYLIB│      │
│                  │  - Rule Engine  │      │
│                  │  - JS Runtime   │      │
│                  │  - HTTP Client  │      │
│                  └─────────────────┘      │
└──────────────────────────────────────────┘
```

**优点：**
- **零网络延迟**：函数调用开销 ~微秒级
- **单进程架构**：内存占用更低（~35MB vs ~60MB）
- **简化分发**：只需一个 exe + 一个 dll
- **无需防火墙配置**：无网络端口监听
- **冷启动更快**：无进程启动开销

**缺点：**
- 崩溃会影响整个应用
- 调试相对复杂
- 需要处理平台差异（dll/so/dylib）

---

## 实现细节

### 1. Go FFI 接口层

文件：`backend_go/pkg/ffi/engine.go`

```go
//export Initialize
func Initialize()                             // 启动 ServiceHub（幂等，必须先于 Call）

//export Call
func Call(action *C.char, paramsJSON *C.char) *C.char  // 唯一数据入口

//export StatusReport
func StatusReport() *C.char                   // 只读就绪快照（服务状态列表）

//export Cleanup
func Cleanup()                                // 停止全部服务

//export FreeString
func FreeString(s *C.char)                    // 释放返回的 C 字符串
```

**关键点：**
- 使用 `//export` 注释导出 C 函数
- 字符串通过 `*C.char` 传递（UTF-8 编码）
- 返回值需调用方手动释放（`FreeString`）
- 所有 RPC action（含事件轮询 `engine_poll`）统一走 `Call`，返回 `{ok, data, error, id, v}` 信封；新增 action 只需改 Go 侧分发表，无需新增 FFI 导出

### 2. 编译命令

#### Windows (DLL)
```bash
cd backend_go
set CGO_ENABLED=1
set GOOS=windows
set GOARCH=amd64
go build -buildmode=c-shared -o legodo_engine.dll ./pkg/ffi/
```

#### Linux (SO)
```bash
cd backend_go
CGO_ENABLED=1 GOOS=linux GOARCH=amd64 \
  go build -buildmode=c-shared -o liblegodo_engine.so ./pkg/ffi/
```

#### macOS (DYLIB)
```bash
cd backend_go
CGO_ENABLED=1 GOOS=darwin GOARCH=amd64 \
  go build -buildmode=c-shared -o liblegodo_engine.dylib ./pkg/ffi/
```

### 3. Dart FFI 绑定

文件：`lib/core/ffi/engine_ffi.dart`（`GoFFIClient`）

```dart
typedef CallC = ffi.Pointer<ffi.Int8> Function(
    ffi.Pointer<ffi.Int8>, ffi.Pointer<ffi.Int8>); // action, paramsJSON

class GoFFIClient {
  late ffi.DynamicLibrary _dl;
  late CallDart _call;

  void load() {
    _dl = ffi.DynamicLibrary.open(_findLibraryPath());
    _initialize = _dl.lookupFunction<InitializeC, InitializeDart>('Initialize');
    _call       = _dl.lookupFunction<CallC, CallDart>('Call');
    _cleanup    = _dl.lookupFunction<CleanupC, CleanupDart>('Cleanup');
    _freeString = _dl.lookupFunction<FreeStringC, FreeStringDart>('FreeString');
  }

  Map<String, dynamic> call(String action, Map<String, dynamic> params) { ... }
}
```

动态库查找顺序（`_findLibraryPath()`）：`LEGODO_ENGINE_DLL` 环境变量覆盖 → 否则 exe 同目录的 `legodo_engine.dll`。

### 4. 适配器模式

文件：`lib/core/rpc/ffi_engine_adapter.dart`

```dart
class FFIEngineAdapter implements EngineRepository {
  /// 生产走 GoFFIClient.instance 单例；[client] 只为离屏测试注入替身。
  FFIEngineAdapter({GoFFIClient? client}) : _injected = client;

  GoFFIClient get ffi => _injected ?? GoFFIClient.instance;

  Future<void> connect() async {
    ffi.load();
    ffi.initialize();
    _eventCursor = 0;            // 引擎侧事件环随 Initialize 重开，游标归零
  }

  // 数据请求：ffi.call(action, params) → 解析 {ok, data, error} 信封
  // 进度事件：ffi.call('engine_poll', {'since': _eventCursor}) 游标轮询
}
```

---

## 性能对比

| 指标 | WebSocket RPC | FFI 调用 | 提升 |
|------|--------------|----------|------|
| 冷启动时间 | ~2s (进程启动) | ~0.3s (库加载) | **6.7x** |
| 单次调用延迟 | ~5ms (网络往返) | ~0.01ms (函数调用) | **500x** |
| 内存占用 | ~60MB (双进程) | ~35MB (单进程) | **42%** |
| APK 潜力 | 120MB (含 Python) | 35MB (纯 Go) | **71%** |

---

## 打包方式

当前为纯 FFI 单进程架构（独立进程 + WebSocket 模式已拆除）：

```bash
python build_release.py windows
```

生成的分发包包含：
- `legodo_client.exe` (Flutter UI)
- `legodo_engine.dll` (Go 引擎，与 exe 同目录，dart:ffi 加载)
- `data/`（Flutter 资源与 AOT 快照）

---

## 注意事项

### 1. CGO 依赖

动态库模式需要启用 CGO，这意味着：
- Windows 需要安装 MinGW-w64 或 TDM-GCC
- Linux 需要安装 gcc
- macOS 需要安装 Xcode Command Line Tools

**检查方法：**
```bash
go env CGO_ENABLED  # 应输出 1
gcc --version        # 确认编译器可用
```

### 2. 跨平台编译

从 Windows 编译 Linux/macOS 动态库时：
- 需要安装对应的交叉编译工具链
- 或使用 Docker 容器进行编译

**推荐做法：**
- 在目标平台上直接编译
- 使用 CI/CD 流水线自动化

### 3. 内存管理

Go 导出的字符串需手动释放：

```dart
final resultPtr = _searchBooks(sourcePtr, keywordPtr);
try {
  final result = resultPtr.cast<Utf8>().toDartString();
  _freeString(resultPtr);  // ⚠️ 必须调用
} finally {
  calloc.free(sourcePtr);
  calloc.free(keywordPtr);
}
```

### 4. 错误处理

Go 函数返回 JSON 格式的错误信息：

```json
{"error": "invalid source JSON: ..."}
```

Dart 侧需解析并转换为 `EngineFailure` 异常。

---

## 未来优化方向

1. **批量操作**：减少 FFI 调用次数（一次传多个源）
2. **异步回调**：Go 侧通过 callback 推送进度事件
3. **缓存层**：在 Go 侧实现内存缓存，避免重复计算
4. **并发控制**：Go goroutine 池限制并发数
5. **热更新**：支持运行时重新加载动态库

---

## 参考文档

- [dart:ffi 官方文档](https://dart.dev/guides/libraries/c-interop)
- [Go cgo 文档](https://pkg.go.dev/cmd/cgo)
- [FFI 最佳实践](https://docs.flutter.dev/platform-integration/android/c-interop)
