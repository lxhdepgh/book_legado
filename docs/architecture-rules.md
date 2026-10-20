# 架构约束（architecture-rules）

> 状态：2026-10-20 按现主线重写。现架构 = **G3**：`legodo_dart` Flutter + `backend_go` Go，
> 单进程 FFI（基线见 [current-system-design.md](file:///D:/WorkSpace/PycharmProjects/legodo_booksource_check/docs/current-system-design.md)）。
>
> 上一版"红线 v2"（2026-09-24 M7 回写、2026-09-27 校正）整篇绑 G1 的根 `app/` Flet 进程内架构，
> 全文保留在 `docs/archive/flet时代设计与验收/架构红线-Flet进程内-G1版.md`。
> **§2 逐条列了旧条目为什么失效**——别照着旧文去仓库根找 `app/services/`，那棵树 2026-10-12 就搬走了。
>
> 旧文那句「红线以设计文档 §5.3 为权威来源」一并作废：新版设计基线没有 §5.3，权威就是本文。
> 与代码冲突时以代码为准并更新本文。

---

## 1. 生效中的红线

### A. 协议与接线

- **A1 唯一的跨语言 ABI 是 `Call(action, paramsJSON)` + `engine_poll` 轮询**
  （`backend_go/pkg/ffi/engine.go:163-181`；Dart 只 lookup `Initialize`/`Call`/`Cleanup`/`FreeString`，
  `lib/core/ffi/engine_ffi.dart:218-221`）。加能力走"服务端加 action + 界面加调用"，
  **不许**为单个功能另开一条 C 导出、第二条进程或一个端口。
- **A2 改发送面必须整表机械重提取**。钉子是 `backend_go/pkg/ffi/route_test.go:141`
  （现值：同步 88 / 直连 1 / 合计 89），提取器现读 Dart 侧（`:129-136`），另配形状夹具（`:117-119`）。
  反例真发生过：`bookshelf_delete` 自 A7 起没进过那张表，而"覆盖 Dart 全部发送面"那句照旧写着。
- **A3 不许假设事件有序或逐条到达**。引擎侧无推送，Dart 每 250 ms 带游标轮询
  （`lib/core/rpc/ffi_engine_adapter.dart:293,323`），游标按**服务端帧**推进（`:305`），
  同批同名事件在出口合流成一条（`coalescableEventNames:130`、`coalesceBatch:134-151`）。
  进度反馈必须走事件流，不许靠服务回调顺带刷一次 UI。
- **A4 界面不得自造判据、自拼地址、自行清洗服务端字段**。协议面缺判据时钉服务端常量或加服务端字段；
  能力没做就显式进 `unimplementedActions`（`engine.go:283-314`）让服务端回"没做"，
  而不是在界面上凑一个能显示的形状。载荷形状要由界面测试钉成"越界就红"
  （现例：`legodo_dart/test/export_screen_test.dart` 用 `expect(find.byType(TextField), findsNothing)`
  钉死"不许在屏上手填标识或路径"）。

### B. 行为对齐

- **B1 规则行为以 Legado 原版为准**。新规则能力先对照 `reference_code/` 与只读参考树
  `legodo_dart/app/`（Go 端口的清单来源），行为差异记到 `docs/桌面与安卓实现差异.md` 并给理由。
  `@js:` 走 **goja**（`backend_go/go.mod:11`；`pkg/jsruntime/runtime.go:83` 建 VM，
  VM 池 `:36`、中断看门狗 `:66`；调用点 `internal/engine/rule_engine.go:329,413,677`、`explore.go:207`）。
  旧红线「只走 QuickJS、不引入新 JS 引擎」**已被取代**，但精神不变：不另起第三套 JS 路径，
  不引入第二套 Chromium 渲染依赖。
- **B2 找书 / 换源的并发与取消语义是行为红线**（旧红线 7 原文继续有效）。
  Go 侧现值：`SearchMaxThread = 32`（`internal/services/search_model.go:16`，注释说明 Legado 的 9
  只是 OkHttp 派发池）、`SearchStoreTTL = 24h`（`:22`）、`SwitchConcurrency = 8`
  （`internal/services/switch_service.go:35`）、`SwitchTimeout = 10s`（`:37`）。
  searchId / 页码 / generation 取消、本地种子 + 联网扫描 + 候选排序、notShelf 语义不得破坏。
  **改这两块前先读 `docs/找书与换源逻辑对齐.md`。**
- **B3 存储收敛**：结构化数据一律进主库 `legado.db`（`core/storage.go:40`；建表区 `:162-337`，
  15 张基表 + 7 张迁移期 `*_v2`）；"书"的记录不许出现第三份重复库；新功能不许新增无管理的散落文件；
  JSON 仅作互操作 / 备份格式，运行时不读写。

### C. 进程与路径

- **C1 DLL 只认 exe 同目录**（`lib/core/ffi/engine_ffi.dart:254-271`）。禁止把绝对路径写进代码或
  产物清单——换一台机器即坏。`LEGODO_ENGINE_DLL` 只给测试活臂注入用（`:257`）。
- **C2 数据目录只有一个来源**：`LEGADO_BOOKSOURCE_DATA_DIR`，缺省走 `defaultDataDir()`
  （`internal/services/register.go:65-69`、`:30-49`）。新功能不得另立落盘根目录。
- **C3 平台专属能力走适配层**：WebView / TTS / 文件选择这类由框架决定形状的能力，落在 Dart 侧插件
  （`webview_flutter`、`flutter_tts`，`pubspec.yaml:37,44`）与 Go 侧分支里，业务层不写平台 if。

### D. 分层与依赖方向（现状）

- **D1 界面层不碰 ABI**：`lib/features/` 下当场 grep 到 **0 个**文件 import
  `core/ffi/engine_ffi`；取数一律经 `domain/*_notifier.dart` → Riverpod `core/di/providers.dart`
  （presentation 层最高频的 import 就是它，12 处）→ `EngineRepository` 契约
  （`lib/core/rpc/engine_repository.dart:119`）→ FFI 适配器。新增直连 DLL 的调用即违规。
- **D2 Go 服务不得感知界面**：引擎侧没有 UI 概念，一切反馈经事件环出到 Dart。服务之间只经
  hub / EventBus / 显式接口，不得直改对方内部状态（`internal/services/hub.go`）。
- **D3 目录包边界即依赖边界**：`internal/services` 不 import `pkg/ffi`，`core/` 不认识业务服务。

### E. 工程纪律

- **E1 智能体不擅自提交**；不 `checkout` / `reset` / `stash` 开发者未提交的改动（`git-workflow.md`）。
- **E2 密钥不入库**（`secrets-policy.md`）。
- **E3 高风险操作先预览再执行**：删除 / 覆盖 / 发布 / 批量修改前先给清单。
- **E4 文档与日志里的数字必须当场测得**，并带口径（哪条命令、哪个 `文件:行`）；引用他处的数字要写出处。
  测不出来就把那句删掉，不留旧值或估计值。
- **E5 护栏必须能自证"接上了"**：扫到 0 个对象的守卫要判红，不许判绿。
  本仓库现有一批正是反例 → `docs/known-issues/008_ui_safety_guards_scan_a_deleted_root_app.md`。

## 2. 已失效条目（旧文里还写着，别去找路）

| 旧条目（G1 版） | 为什么失效 | 现在的归处 |
|---|---|---|
| 红线 1「服务默认零 flet import，唯一例外 `web_page_renderer.py`」+ 红线 6「views 不 import services 具体模块」 | 执法者 `tests/test_ui_safety.py:24-25` 把根写死成仓库根 `app/`；那棵树已搬到 `legodo_flet/`，而 `rglob` 对不存在的路径**返回空、不报错** → 恒绿（当场 `2 passed in 0.02s`） | 判据本身仍成立（现场指 `legodo_flet/app` 时：服务层只命中白名单那一个文件、views 命中 0），但**没人守**；见 known-issues 008 与本文 E5 |
| 红线 1 里那组行号（例外本体 `:20`/`:89`，白名单常量 `_FLET_ALLOWED` 在 `:28`） | 指的是根 `app/` 那份；flet 树里同一个文件的两处 import 实测在 `legodo_flet/app/services/web_page_renderer.py:19`（`import flet as ft`）与 `:75`（`import flet_webview as ftw`） | 同上 |
| 红线 3「`@js:` 规则只走 QuickJS（`legado_js.py`）」 | Go 引擎用 goja；`legado_js.py` 不在主线 | B1 |
| 红线 4 后半句「QuickJS 单线程串行模型为已知上限，以看门狗缓解，不为此换引擎」 | 引擎已换；看门狗这一形态在 goja 上另有实现（`pkg/jsruntime/runtime.go:66`） | B1 |
| 红线 2「请求-响应经 `ServiceHub.get()`（协程方法自动经 EngineProxy 派发）/ `hub.require()`」 | 那是 Python 进程内的派发器；Go 侧对应物是 `internal/services/hub.go`（`registerNamed:71`、`StartAll:107`、`topoOrder:158`） | D2 |
| §1 分层方向 `views → controllers → services → core → storage`；§2.1 整张包表（`app/core`、`app/services/{engine,sources,reading,content,tools}`、平铺 14 模块、`app/views`、`app/controllers`、`app/storage/schema.py`） | Dart 侧改为 `features/<域>/{data,domain,presentation}` + Riverpod；Go 侧见设计基线 §4 | D1–D3 |
| §2.2 只读参考 `base_history/`、`other_history/`、`old_code/`；§2.3 差分工具 `tools/legado_diff/`（jsoup / rhino / charset / legado 四套 oracle） | 这几目录都不在工作树；`pytest.ini` 现在的 `norecursedirs` 只剩 `reference_code build dist node_modules .* legodo_dart/build legodo_dart/.dart_tool` | B1 的参考面（`reference_code/`、`legodo_dart/app/`） |
| 红线 9 的「全量 pytest **1215 passed + 28 子测试**」、§2.3 的「`test_legado_compat.py` 488 条」 | 那扇门现在只剩 flet 契约（根 `tests/` 80 文件 / 1547 个 `test_`，其中 77 个 import `app.*`）；主线的门是 `go test` + `flutter test` | E4、`local-testing-guide.md` |
| §4 M1–M7 执行工程约束（conda 打包、`python main.py --smoke-test`、`--basetemp=.cache/pytest/tmp_m7`、里程碑 `collect_ignore`） | 冒烟入口随根 `app/` 删除；conda 那条只对 Python 侧仍成立 | `local-testing-guide.md` |
| 红线 10「新外部依赖必须 Win/macOS/Linux 三平台可用（wheels 或纯 Python）」 | 依赖面已分裂成 `pubspec.yaml` / `go.mod` / `legodo_flet/requirements*.txt` 三份 | C3 |

红线 5（只读参考不得改）、7（搜索/换源语义）、8（存储收敛）、11–13（不擅自提交 / 密钥不入库 /
预览-确认）**原样有效**，分别并入 B1、B2、B3、E1–E3。
红线 9 那句教训仍然有效并且已并入 E4：**报出的数字必须带当天跑了几次、有没有换过等待条件**。

## 3. 这些红线由谁执法

| 红线 | 执法者（当场确认存在） |
|---|---|
| A1 / A2 | `backend_go/pkg/ffi/route_test.go`（`:117-119` 形状夹具、`:141` 计数钉） |
| A3 | `legodo_dart/test/engine_event_coalesce_test.dart`、`ffi_frames_test.dart` |
| A4 / D1 | 各屏自己的 `*_test.dart` 形状钉（现例 `export_screen_test.dart`） |
| B2 | `docs/找书与换源逻辑对齐.md` + `internal/services` 的换源 / 搜索测试 |
| E5 | **无** |

`E5` 这一格空着，是本文件目前最该补的一条：判据写得再清楚，读点指错就恒绿。
