# 系统设计（当前架构：Flutter + Go 单进程 FFI）

> 状态：2026-10-20 重写，依据是**当场读码**（`legodo_dart/` 与 `legodo_dart/backend_go/`），
> 每条给 `文件:行`。上一版本文（112 KB，M7 回写版）描述的是**已从工作树删除的根 `app/` Python/Flet
> 进程内多服务架构**，全文移至 `docs/archive/flet时代设计与验收/系统设计基线-Flet进程内多服务.md`；
> 它继续作为"为什么长成这样"的过程档案，但**不再是现状的权威来源**。
>
> 读法与冲突裁决见 [文档总览.md](file:///D:/WorkSpace/PycharmProjects/legodo_booksource_check/docs/文档总览.md)。
> 本文与代码冲突时以代码为准并更新本文；红线见 `architecture-rules.md`。

---

## 1. 定位与三代沿革

Legado 书源连通性 / 搜索可用性检测与内置阅读器的桌面应用。核心价值不在 UI，而在
**与 Legado 原版规则行为对齐的兼容层**（规则解析、JS 运行时、HTTP 语义）。核心链路：
导入书源 → 兼容性审计 → 四段检测 → 找书 / 换源 → 内置阅读。

| 代 | 形态 | 现状 |
|---|---|---|
| G1 | 根 `app/`：Flet UI 与 20+ 业务服务同 Python 进程（M1–M7 重构产物） | 根 `app/` 已删；活着的 Python 树只剩 `legodo_flet/app/`（参考实现） |
| G2 | 双进程：Flutter exe 拉起后端（PyInstaller 的 `LegadoReader.exe`，或独立 Go WS 服务），over HTTP `/rpc` / `ws://…:8080/rpc` | **WS 链已从双侧拆除**；`deployment-guide` 那批"指南"作废并归档 |
| G3 | **本文**：`legodo_client.exe`（Flutter AOT）`LoadLibrary` 同目录 `legodo_engine.dll`（Go c-shared），无子进程、无端口 | 现主线 |

规模（2026-10-20 当场数）：

| 面 | 数 | 口径 |
|---|---|---|
| `legodo_dart/lib/` | 81 文件 / 20,244 行 | `find … -name "*.dart" \| wc -l`、`wc -l` |
| 屏与对话框 | 20 | `*_screen.dart` + `*_dialog.dart` |
| `legodo_dart/test/` | 36 文件 | 同口径 |
| `backend_go/` | 98 文件 / 34,644 行；`go list ./...` = 10 包 | 同口径 |
| FFI 动作分发面 | 89（同步 88 + 直连 1） | `go test ./pkg/ffi -run Route` 现绿，断言 `route_test.go:141` |
| 在册 Go 服务 | 23 | `register.go` 里 `NewBaseService("X")` 去重 |
| 根 `tests/`（Python 遗留门） | 80 文件 / 1547 个 `test_`，其中 77 个 import `app.*` | 面向 `legodo_flet/app/` |

## 2. 进程模型与启动链

```
legodo_client.exe  ──dart:ffi──►  legodo_engine.dll
   main.dart:16  bootstrapEngine()          lib/main.dart:16
     → bootstrap.dart:49-89                  FFIEngineAdapter() + connect()  :57-58
       → ffi_engine_adapter.dart:165-174     ffi.load() → ffi.initialize()
         → engine_ffi.dart:254 _findLibraryPath()      与 exe 同目录；LEGODO_ENGINE_DLL 仅测试注入
         → engine.go:65  Initialize()        NewServiceHub → 注册基础设施 → 注册业务服务
                                             → 挂 4096 容量 EventRing → SubscribeAll → StartAll(30s 超时)
     → bootstrap.dart:63-65                 连上后做一次书架探针注入
```

- **DLL 只认 exe 同目录**（`engine_ffi.dart:254-271`）。禁止把绝对路径写进代码——换机即坏。
- 数据目录取 `LEGADO_BOOKSOURCE_DATA_DIR`，缺省落 `%LOCALAPPDATA%\LegadoBookSourceCheck`
  （`register.go:65-69` + `defaultDataDir()` `register.go:30-49`；macOS/Linux 分支在同函数）。
  这条 env 变量是继承自 Python `app_paths.py` 的既有契约，语料与打包两侧共用。

## 3. Dart 侧分层

| 目录 | 职责 | 代表落点 |
|---|---|---|
| `lib/app/` | bootstrap、`router.dart`、`shell.dart`（壳宽窄阈值 720，`shell.dart:17`） | 3 文件 |
| `lib/core/ffi/` | DLL 加载、跨界字符串（自己 `HeapAlloc`/`HeapFree`，`engine_ffi.dart:81-82`） | `engine_ffi.dart` |
| `lib/core/rpc/` | 契约基类 `engine_repository.dart:119` + `ffi_engine_adapter.dart` 实装 + 失败模型 `envelope.dart` | — |
| `lib/core/events/` | 一帧的解码与事件名契约 | `engine_event.dart`、`event_names.dart` |
| `lib/core/di/` | Riverpod 装配（`flutter_riverpod: ^3.4.3`，`pubspec.yaml:37`） | `providers.dart:16-17`（repository）、`:43-45`（`StreamProvider` 事件流） |
| `lib/features/<域>/` | 7 域 57 文件，域内 `data / domain / presentation`；notifier 放 `domain/` | — |

**导航**：分组进路由表（3 条：reading / sources / settings，`router.dart:258-278`），组内页签是分组页
自己的状态、不在路由表里；整页目的地 9 条用根导航器 `context.push` 压在壳之上
（reader `:284-307`、browser、replaceRules、font、qr、export、webdav、webBridge、tts，`:309-371`）。

**事件**：引擎侧**没有推送**。Dart 每 250 ms 带游标轮询 `engine_poll {'since': cursor}`
（`ffi_engine_adapter.dart:293`，周期常量 `:323`）。游标按**服务端帧**推进、不按合流条数（`:305`），
`Initialize` 后归零（`:177`）。同一批里同名的多条在出口收成一条
（白名单常量 `coalescableEventNames:130`，目前只有 `SourceChangedEvent`；合流函数 `coalesceBatch:134-151`，
同名留最后一条、位置取首次出现）。⇒ 界面不许假设事件有序或逐条到达。

## 4. Go 侧分层

| 包 | 成员 | 边界 |
|---|---|---|
| `core/`（9 文件） | `lifecycle.go`（`Service` 接口 `:52`、`BaseService` `:79`）、`hub.go`、`storage.go`（877 行 SQLite）、`event_bus` / `event_ring` / `events`、`settings.go` | 基础设施，不认识业务 |
| `internal/services/`（48 文件，18 测） | 23 个在册服务 + `hub.go`（`registerNamed:71`、`StartAll:107`、`StopAll:126`、`StatusReport:139`、拓扑序 `topoOrder:158`）+ `register.go` 装配 | 业务层唯一有状态处 |
| `internal/engine/`（5 文件） | `rule_engine.go`、`explore.go`、`content_blocks.go` | 规则执行核心 |
| `pkg/ruleparser/`（10 文件） | CSS / JSONPath / XPath 解析 | 与 Legado 行为对齐的守门对象 |
| `pkg/jsruntime/`（3 文件） | goja VM 池 `runtime.go:36`、中断看门狗 `:66`、`goja.New()` `:83` | JS 运行时 |
| `pkg/ffi/`（13 文件，12 测） | `engine.go` 导出面 + 一堆形状/契约护栏 | 唯一对外 ABI |
| `internal/{httpclient,models,storage}/`、顶层 `models/` | — | **`internal/storage/` 全仓零 import**（`grep legodo_go/internal/storage` = 0 命中），是与 `core/storage.go` 平行的旧 schema，属待清理死码 |

**HTTP 语义**：全局单例 `ClientSession`（`register.go:105` 创建，`hub.go:38` 存，`hub.go:211` 共享）；
连接池 MaxIdleConns 100 / 每主机 10 / 闲置 90 s（`client.go:26-30`），30 s 超时、≤10 跳重定向（`:35-40`）。
cookie 罐是**自管的 per-host map**（`client.go:19 baseCookies`；`SetCookieJar:66-70`、`GetCookies:84-88`），
**没用** `net/http` 的 CookieJar——这保住了与 Legado 一致的域名行为。检测服务每轮另起独立 client
（`check_service.go:328,381`），不与阅读路径共罐。

**JS**：`@js:` 走 **goja**（`go.mod:11`；调用点 `internal/engine/rule_engine.go:329,413,677`、`explore.go:207`）。
旧红线那句「只走 QuickJS、不引入新 JS 引擎」已被这一条取代（裁决表 §2 第 1 行）。

## 5. 协议面（唯一 ABI）

- 对外只有 4 个被 Dart 用到的 C 符号：`Initialize`(`engine.go:64`)、`Call`(`:163`)、`Cleanup`(`:97`)、
  `FreeString`(`:52`)。另有第 5 个 `StatusReport`(`:184`) 已导出，但 `lib/` 内**未发现调用点**。
- `Call(action, paramsJSON)` → `route(h, actionStr, p)`（`engine.go:164-181`，`route()` 在 `:338-627` 一个 switch）。
- 动作名两种拼法以**成对 case 标签**实现（例：`:355 case "engine_status", "engine.status":`）；
  未实现表查键时先折平（`:320` 把点号换下划线并小写）。
- 覆盖面由仓库自己的提取器钉：`pkg/ffi/route_test.go:141` 断言 `syncCalls==88 / directCalls==1 /
  len(actions)==89`，提取器现读 Dart 发送面（`:129-136`），另有形状夹具（`:117-119`）。
  **改发送面必须整表机械重提取，不许手工补条**（历史上 `bookshelf_delete` 就是这样漏掉一整轮的）。
- 未识别的 action 走 `default` 回 `unimplemented: <action>` 错误信封（`:625-626`）。

## 6. 存储

单一主库 `%LOCALAPPDATA%\LegadoBookSourceCheck/legado.db`（`core/storage.go:40`），
缓存目录 `stateDir/cache`（`:32`），下载目录 `stateDir/downloads`（`:83`）。建表常量区
`core/storage.go:162-337`：**15 张基表**（settings / cookies / books / bookmarks / audit_log /
tts_profiles / rss_sources / rss_articles / replace_rules / dict_rules / fonts / download_tasks /
book_sources / bookshelf / search_books）+ 7 张迁移期 `*_v2` 重建表（`:423-699`）。

结构化数据一律进主库；JSON 只作互操作 / 备份格式，运行时不读写（红线 8）。
注意：`cookies` 表建了但 Go 非测试代码**无读写点**，与 `client.go` 的内存罐是两条路——待收口。

## 7. 明写不补的能力边界

`pkg/ffi/engine.go:283-314` 的 `unimplementedActions` 表是**服务端自己承认"没做"**的地方，
6 族 15 条：`browser_*`（`:284-288`）、`webbridge_*`（`:290-294`）、`webdav_*`（`:296-300`）、
`read_aloud_*`（`:302-307`，发声改由 Flutter 侧 `flutter_tts` 承担，`pubspec.yaml:44`）、
`bookshelf_import`（`:309-310`）、`source_audit`（`:312-313`）。二维码只做了生成，
识别侧显式回 `ErrQRDecodeUnimplemented`（`internal/services/qr_service.go:21,52`）。

界面**不许**为这些格子自造判据或自拼地址——要么补服务端这一格，要么显式回未实现。
缺口台账与拍板项在 [dart补缺方案.md](file:///D:/WorkSpace/PycharmProjects/legodo_booksource_check/docs/dart补缺方案.md)。

## 8. 两棵 Python 树的角色

| 树 | 角色 | 谁在用它 |
|---|---|---|
| `legodo_flet/app/`（152 个 py 文件 / 52,836 行） | 可运行的 Flet UI，**parity 参考实现**，不再演进 | 根 `tests/` 那 80 个 pytest 契约文件（`sys.path.insert(0, FLET)`，如 `test_gateway_contract_single_source.py:31,40`）；`docs/archive/flet时代设计与验收/flet子项目功能清单.md` 的静态盘点 |
| `legodo_dart/app/`（147 个 py 文件） | Go 端口的**参考来源**（`Dart-FFI-only切换与Go后端补全方案.md` §1.6 的端口清单出处） | 只读；不在其上开发 |

两棵树**包名都叫 `app`**：全局 `pytest.ini` 里加路径会指错，所以那句 `sys.path` 写在各测试文件里。

## 9. 守门口

| 门 | 命令 | 说明 |
|---|---|---|
| Go | `go vet ./...` + `go test ./... -count=1` | 10 包，其中 6 包有测试 |
| 协议面 | `go test ./pkg/ffi -run Route` | 发送面 89 条的钉子 |
| Dart | `dart analyze` + `flutter test` | `test/` 36 文件；3 条活臂要真 DLL（`LEGODO_ENGINE_DLL`，`engine_ffi.dart:257`） |
| 出包 | `cd legodo_dart && python build_release.py windows` | 引擎单编：`go build -buildmode=c-shared -o legodo_engine.dll ./pkg/ffi/`（需 `C:\mingw64\bin` 在 PATH 前缀） |
| Python 遗留 | `python -m pytest -q --basetemp=<新目录>` | 默认 basetemp 有 ACL 拒绝，会整批假红 |

改到 Go 的那步之后：重编 DLL 并同步 `dist/legodo_windows/`，用 `cmp` 核逐字节一致。
静态全绿不等于真链路通——活臂（真 DLL + 真语料）见 `docs/local-testing-guide.md`。

## 10. 已知观察项（本文写作时当场测得，未排期）

1. `internal/storage/` 全仓零 import——死码。
2. `StatusReport` 已导出但 Dart 侧无调用点。
3. SQLite `cookies` 表无消费方，与 `client.go` 的内存罐并行存在。
4. `flutter test` / `dart analyze` 这两条命令没有成文在任何 README 里，只在开发日志的验收段出现。
5. 根 `BUILD.md` 旧版整段描述 G2；已重写为指路页（`legodo_dart/README_BUILD.md` 才是构建权威）。
