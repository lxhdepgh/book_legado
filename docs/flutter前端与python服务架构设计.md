# Flutter 前端 + Python 服务：架构设计（提案，未开工）

日期 2026-10-08。作者：Qoder（与用户讨论后起草）。
状态：**提案**。§8 的决策门没通过之前不写生产代码。

本文所有"本项目"数字均为本轮实测（`wc -l` / `grep -c`，落点给 file:line）；
所有"真果鉴"数字来自浅克隆到 `D:/tmp/guoapp1` 的 `github.com/jerunshi/guoapp1`
（pubspec 名 `duanju_app`），同样给 file:line。两份数字都不许凭印象引用。

---

## 0. 结论先说

1. **可行。** 本项目后端已经高度"服务化"：91 个零 flet 的服务文件、32 个注册服务、
   375 个 `async def`、22 个 `@dataclass(frozen=True)` 事件（字段全是 JSON 标量/表，
   **没有一个携带 flet 控件或服务对象**）。换前端不动业务内核。
2. **传输方式跟真果鉴不同，但纪律照抄。** 真果鉴的核心是 **in-process c-shared 的 Go**，
   Dart 只看到 2 个导出符号（`native/bridge/main.go` 全文 23 行）。Python 学不了这条：
   嵌 libpython 会把 GIL、打包、崩溃域全摊在自己身上。**我们的等价物是本地环回进程 +
   同样粗的"一个动词 + action 表 + JSON 信封"**（§4）。
3. **别学它的前端组织方式。** `lib/` 是 85 文件 30,348 行的**一个平目录**，状态管理是
   裸 `setState`（229 处）、导航是 Navigator 1.0（`Navigator.` 109 处，go_router 0），
   `player_screen.dart` 单文件 2,105 行同时管 UI/播放器/健康检查/存档计时。这是它最大的债，
   不是亮点。我们按 2026 主流走 **feature-first + Riverpod + go_router + UI/data 分层**
   （Flutter 官方架构指南推荐的正是 UI 层/数据层 + 依赖注入 + 分层测试）。
4. **值得抄的是它的"边界纪律"**，那正是我们已经被咬过的地方：错误信封永远合法、
   服务端 `recover()` 兜底、每个 action 一对客户端/服务端超时、(session, sequence)
   单飞取消、能力开关握手防前后端版本错配、流媒体走环回 HTTP 而不是把字节塞进 RPC。
5. **建议的开工顺序**：先只做 §9 的 **G0（RPC 网关 + 无头入口，1–1.5 周）**。
   它独立就有三项收益（引擎可无头跑、协议可给桌面与 Android 共用、
   跨循环编组那一整层消失），且**不承诺任何 UI 重写**。G0 跑通 2–3 个方法后再决定要不要进 G1。

---

## 1. 参照取证：真果鉴（guoapp1）

| 维度 | 实测 | 对我们的意义 |
| --- | --- | --- |
| 边界形状 | `DuanjuRequest(*C.char) *C.char` + `DuanjuFree`，两个符号，无结构体布局要版本 | 粗颗粒是对的；我们把它从"函数调用"换成"HTTP/WS 一次往返" |
| 分发 | `nativeDispatch` switch **36 个 action**（`native/core/app_runtime.go:341-420`），一个扁平 `nativeInput` 装下所有字段（`:96-122`） | 照抄"一个动词 + action 表"；**它的债是没有 schema 文件**（字段在 Go 结构体、Dart `_fromJson`、调用点手写三处重复，Dart 侧 112 个 `fromJson`）——我们用 JSON Schema 单一来源避免（§4.5） |
| 信封 | `{ok, error, code, data}`，入口 `defer recover()` 保证永远回一个合法信封（`app_runtime.go:289-293`），请求体 `>1<<20` 直接拒（`:296`） | 采纳，含大小上限 |
| 超时 | 每个 action 客户端预算 70s/20s/15s/620s（`lib/core_bridge.dart:573-582`）+ 服务端 `context.WithTimeout`（`app_runtime.go:363-371`）**成对** | 采纳，本项目长任务（全库检测、批量下载）必须两侧都设 |
| 取消 | (session, sequence) 单飞，旧 sequence 判 `context.Canceled`，16 并发读 / 128 session 上限（`native/core/app_requests.go:23-49`），Dart 侧镜像同一契约（`core_bridge.dart:251-283`） | 采纳——我们的换源扫描、检测队列正需要"新请求顶掉旧的" |
| 版本握手 | 只有能力位比对：`build['allSources'] != allSourcesEnabled` 就抛"应用与原生核心的站源版本不一致"（`core_bridge.dart:637-640`）；**同时 dispatch 里硬编码 `"version":"0.2.17"` 而 pubspec 是 0.2.67** —— 陈旧常量 | 抄握手，**别抄硬编码版本**：`protocol_version` 由构建期注入单一来源 |
| 流媒体 | 唯一"必须活着"的东西走环回 HTTP：`127.0.0.1:0` + token session + HLS 重签 + Range，8 session / 10 min idle（`native/core/app_stream.go:55-65`） | 直接对应我们的封面/漫画/音视频/TTS 音频（§4.4） |
| 线程约束 | 全程没有 `runtime.LockOSThread`——Go 的 movable stack 让任何线程都安全，代价是"绝不从 UI isolate 调"，于是**每次调用起一条 isolate**（`core_bridge.dart:573`），且 `DynamicLibrary.open` + 两次 `lookupFunction` **每次调用重做**（`:29-44`） | 我们的处境相反：Python 有 GIL 但原生句柄**线程绑定**（QuickJS 一线程一 runtime、pyttsx3/SAPI COM 套间，本轮刚修）。所以服务端必须显式声明"哪条循环/哪条线程碰什么"（§5.4），不能像它那样随意起线程 |
| 进度 | 没有回调通道，全靠 2s `Timer.periodic` 轮询下载进度（`lib/downloads_screen.dart:67`） | **不抄**。本项目进度已经走事件总线（22 个事件，`app/core/events.py`），推进 WS 推送即可 |
| 测试切分 | Dart 侧用 `FixtureRepository extends AppRepository` 替身（`test/fixtures.dart:34`），只有 1 个 Dart 用例碰真库；Go 侧 25 个 `*_test.go`、4,420 行、96 个 `Test*`，跑 `-race` | **最值得抄**：抽象仓库类 = 测试接缝 + "本平台不支持"的表达；两侧各自测自己的面，不互相打穿 |
| 抽象仓库 | `AppRepository` 约 40 个方法，默认实现一律 `throw AppFailure('当前环境不支持…')`（`core_bridge.dart:69-187`），`NativeRepository` 实现它（`:189`），局部文件用装饰器 `_LocalFileRepository`（`lib/local_media_screen.dart:383`） | 照抄到 §6.3 |
| 打包 | `scripts/build_native.py` → `go build -buildmode=c-shared`；Android NDK r28 分 ABI 进 `jniLibs`；Windows `x86_64-w64-mingw32-gcc` 出 dll 由 `windows/CMakeLists.txt:110` 安装；iOS 用 `DynamicLibrary.process()` + 一个链接锚点 `.m` 调 `DuanjuRequest(NULL)` 防符号被 strip | 我们的 Python 侧用 PyInstaller（已在 `docs/M7打包说明.md`），iOS 这条路直接排除（Python 不能进 App Store）→ **首发只做 Windows + Android** |
| 原生依赖 | 把 `media_kit_libs_windows_video` 放进 `packages/` 本地覆盖，硬 pin `mpv-dev-…7z` + ANGLE 的**文件名与 SHA256**（`CMakeLists.txt:66-100`），并加 `_DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR` 绕 MinGW/libmpv 静态初始化崩溃 | 采纳"vendored + hash pin"这条规矩（我们的 `flet.exe`、playwright chromium 现在都是浮动依赖） |
| CI | `python3 -m unittest discover -s scripts`、`go test -race ./...`（两档 build flag）、`flutter test --dart-define=DISABLE_REMOTE_IMAGES=true`——**所有测试步骤 `continue-on-error: true`** | **不抄**：本项目的护栏是全绿的硬门（1845 passed），红就是红 |
| 工程规矩 | `AGENTS.md`：单一 README、不写解释性注释、`scripts/finish_task.py --message` 落版本快照、"生成物不当已验证" | 前两条与本项目现有红线一致，可并进来 |

---

## 2. 本项目现状（要迁的面与不用动的面）

**要迁的（前端面）**

- `app/views/` 35 文件 7,735 行 + `app/controllers/` 26 文件 7,672 行 = **15,407 行 / 61 文件**。
- Flet 词汇量：58 种 `ft.X`、995 处构造、1,731 处引用。最大几块：`reader_controller.py` 1,499、
  `reading/reader_view.py` 1,215、`navigation.py` 781。
- 窄窗机制：`navigation.py:44 NARROW_WIDTH_THRESHOLD = 720.0`、`is_narrow` `:88`、
  档位只按"跨档"重建 `:771-780`，档位**推给**视图（`_notify_band` `:384` → `on_band_change`）。
- UI 侧测试：68 个测试文件里 **44 个**摸 UI，166 处控件树断言（`.controls` / `isinstance(ft.*)`），
  外加那套 Playwright 语义树像素探针——迁移期这些要重建，是成本不是收益。

**不用动的（服务面）**

- `main.py:99-208` `_build_hub()` 注册 32 个服务；`app/services/**` 里 **91 个文件零 flet**，
  只有 4 个例外（§7）。
- 视图/服务边界本来就干净：13 个视图文件顶部写着"红线 §7.5：本文件不 import app.services.*"，
  数据靠注入。控制器是 `await 服务()` 再推给视图 setter——**这段 await 就是未来的 RPC 调用**，
  迁移是机械替换，不是重设计。
- 存储：`app/storage/schema.py` 15 张表、`SCHEMA_VERSION = 8`，`PRAGMA user_version` 迁移
  （`app/core/storage.py:220-229`）；数据根 `LEGADO_BOOKSOURCE_DATA_DIR` / `_CACHE_DIR`。
  **引擎与 UI 不共享任何内存对象，只共享这个目录**——这是能进程分离的根本原因。

**需要补的**

- 序列化边界太薄：全局只有 1 个 `to_dict`（`app/models/book_source.py:251`）和 3 处 `asdict(`，
  而 `SourceService.list_sources() -> list[BookSource]`（`app/services/sources/source_service.py:288`）
  直接把对象漏到服务面外。27 个服务层 dataclass 需要一个统一的出口序列化策略。
- 已有协议雏形可以直接长大：`app/services/tools/web_bridge_service.py` 已是
  aiohttp `AppRunner` + `TCPSite` 绑 **127.0.0.1**（`:38`，默认端口 0 由系统给 `:211-215`）、
  Bearer/`?token=` 中间件（`:361-371`）、`/api/*` + `/ws` 事件流（`:344-352`）、
  `_event_to_dict` + `_safe_json` 序列化（`:506`、`:521`）。

---

## 3. 目标拓扑

```
┌──────────────── Flutter 进程（桌面 window / Android activity）────────────────┐
│ presentation  Riverpod + go_router（ShellRoute = NAV_META 那条导航脊柱）        │
│ domain        各 feature 的 Notifier / 实体（纯 Dart，可单测）                   │
│ data          EngineRepository（抽象）→ RpcEngineRepository / FakeRepository    │
└───────────────┬───────────────────────────────▲───────────────┬───────────────┘
        请求/取消│WS 事件、进度                    │封面/漫画/音频/视频 │
                ▼                               │（Range + token） │
┌──────────── loopback HTTP + WS（127.0.0.1，随机端口 + 一次性 token）──────────┐
│ GatewayService（新增，注册进 hub）= 一个动词 `call(action, payload)` + 事件流   │
│ 32 个既有服务（async，零改动）── EngineHub 专属循环                              │
│ 原生句柄拥有者：legado-quickjs 派发线程 / tts-system 线程（各自单线程，不许跨） │
│ SQLite + 缓存目录（唯一共享态，按现有的 *_DIR 环境变量定位）                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

要点：

- **一条 UI 循环都不剩**。今天为"引擎循环 ↔ 界面循环"编的那层 glue
  （`app/core/engine_worker.py:245 set_ui_loop` / `:253 ui_loop` / `:258 call_on_ui`，
  以及 `app/controllers/base.py:193 ui_call`、`:218 ui_await`、`:236 on_done` + 手搓节流）
  整层删除。这是本次迁移**唯一确定发生的技术债减少**。
- **不选 in-process**：Dart 里嵌 CPython 需要 `Py_Initialize` + GIL 与打包（PyInstaller 产物
  变成 dll 塞进 Flutter 工程），崩溃域跨 ABI，收益只有省一次本地往返。真果鉴能走 in-process
  是因为 Go 天生能出 c-shared；我们没有这个选项。
- **老板键/托盘/关窗行为**移到 Flutter（`window_manager` + 托盘插件）。本项目实测过的
  "关窗后进程 0.4s 消失"（今天修的 5.4s 见 `docs/dev-logs/2026-10-07.md` §10）迁移后必须
  重新量，且要把"Flutter 退出前通知引擎优雅停"写进协议（§4.6）。

---

## 4. 协议设计（单一来源，两侧生成）

### 4.1 一个动词
```
POST /rpc   {"v":1,"id":"c-17","action":"bookshelf.list","seq":17,"payload":{...}}
→           {"v":1,"id":"c-17","ok":true,"data":[...],"error":null,"code":null}
```
信封固定 `{v,id,ok,data,error,code}`；**任何内部异常也必须回这个形状**（服务端
`try/except BaseException` 兜底，学 `app_runtime.go:289-293`），错误里带
`kind`（`unavailable | timeout | cancelled | bad_request | engine | schema`）。
请求体上限 1 MiB（同它）。

### 4.2 action 表（显式，不反射）
一张 `app/gateway/methods.py` 的声明表：`action → (服务, 方法, 入参 schema, 出参 schema, 超时, 可取消)`。
命名空间按现有服务域切：`bookshelf.* source.* check.* reader.* rss.* readaloud.*
dict.* replace.* font.* backup.* webdav.* theme.* boss.* bridge.* engine.*`。
**显式表是硬要求**：反射扫 32 个服务会把 `-> list[BookSource]` 这类泄漏直接顶上协议面。

表里还有一格 `fields` = **出口字段白名单**（只对"行列表"载荷生效，在 `to_json` 之后、
发信封之前由 `serialize.project` 逐行收口）。它不是为了好看：第八十五轮 G2-F 量过，
`source.list` 那 920 行的 data 是 6,969,039 B，其中列表那一屏从不显示的 `exploreUrl` 占
40.1 %、`jsLib` 占 23.5 %、五组 `rule*` 占 24.0 %（逐键字节账：`.probe/g2f_payload_anatomy.py`），
Dart 解它要转码 13.4 ms + `jsonDecode` 64.4 ms，而 Python 侧整发（含解）才 101.2 ms ⇒ 客户端付的是服务端的 1.7 倍。收成六格后同一发是 351,554 B，
总账回到 1.39 倍。**这一格算进 `schema_digest`**：改白名单就是改契约，老客户端会在握手时被拒，
不会悄悄拿到一份字段变薄的载荷。`source.get` **故意不投影**——编辑器的回填以它为底本，
把瘦身行存回去等于把 `exploreUrl`/`jsLib` 写成空。

### 4.3 事件流（不轮询）
`GET /ws`（沿用 `web_bridge_service.py:344-352`），帧：
`{type:"event", name:"RssRefreshProgress", payload:{...}}`。事件名 = `app/core/events.py`
那 22 个 dataclass 的类名，payload = dataclass 字段（已验证全 JSON 可序列化）。
补两条规矩：进度事件**服务端节流**（现有 0.5s 合并窗口 `app/controllers/base.py` 的
`_coalesced_flush` 语义搬到网关，下发合流但事件一条不丢）；`ReadAloudState`/
`MediaPlayState` 这类状态事件在网关侧留**最近一份快照**，客户端重连可回放（对齐记忆里
"跨页任务要服务侧可回放快照"那条）。

### 4.4 二进制不走 RPC
封面、漫画页、TTS/音视频：环回 HTTP `GET /media/{key}` + Range + 短 token（形态照抄
`app_stream.go:55-65`），Flutter 侧交给 `Image.network`/`media_kit`。理由：把 MP3/JPEG
塞进 JSON 信封会同时炸内存和延迟。**`/media` 的 key 空间就是现有 `BookCacheService`
的缓存键，不新造一套**。

### 4.5 schema 是单一来源
`app/gateway/schema/<action>.json`（JSON Schema，含 `required` 与类型），Python 侧用它校验
入参，Flutter 侧用一个约 200 行的 `scripts/gen_dart_api.py` 从中生成 Dart 请求/响应类型与
`fromJson`。**这条是专门用来不抄真果鉴的**：它的字段在 Go 结构体、Dart `_fromJson`、
调用点三处手写重复（112 个 `fromJson`），是它最容易被改错的地方。
契约测试（§10）用黄金样本钉两侧一致。

### 4.6 超时 / 取消 / 停服
- 表里每个 action 一对预算（客户端 + 服务端），长任务（全库检测、批量下载）另立
  `job.*` 异步句柄：`job.start` 回 `job_id`，进度走事件流，`job.cancel` 可打断。
- **单飞取消**：同一 `action + scope` 只允许在飞一个，新 `seq` 顶掉旧的（照抄
  `app_requests.go:23-49` 的语义，含并发上限）。换源扫描/搜索必须用这个，否则会出现
  "旧请求晚到把新结果盖掉"。
- 握手：`engine.hello` 回 `{protocol_version, build, capabilities[], schema_digest}`，
  版本或摘要不符**直接拒绝启动 UI**，报错文案照真果鉴那句"应用与核心版本不一致"。
  `protocol_version` 由构建期从单一常量注入（不许硬编码）。
- 停服：`engine.shutdown`（客户端主动）+ 服务端在**最后一个会话断开后 N 秒自杀**
  （默认 15s，可配），避免 Flutter 崩了留一个无头 Python。这条必须有实测退出码与延迟读数。

---

## 5. Python 侧改造清单

| # | 改动 | 落点 | 护栏（红=改动失效） |
| --- | --- | --- | --- |
| 5.1 | `GatewayService`（注册进 hub，依赖 EventBus/Storage/Settings） | 新 `app/services/gateway/service.py`；复用 `web_bridge_service.py` 的 runner/token 中间件形态 | 无头启动 → 真发一个 RPC → 事件按序到达；断连重连能拿到状态快照 |
| 5.2 | 显式 action 表 + JSON Schema | 新 `app/gateway/methods.py`、`app/gateway/schema/` | "表里的 action 必须能反射到真方法"（防记忆里"孤儿方法"那一族）；schema 与 dataclass 字段一致 |
| 5.3 | 出口序列化：`-> list[BookSource]` 这类在**边界**转 dict，再按 §4.2 的 `fields` 白名单**逐行收口** | `source_service.py:288` 等 27 个 dataclass；统一 `gateway/serialize.py`（`to_json` + `project`） | 断言 RPC 响应里不含任何 `app.*` 实例（`type(...) is dict/list/str/int/float/bool/None`）；白名单里的键必须真是模型字段、必须盖住界面在读的那几格、错形状必须抛、改白名单必须让 `schema_digest` 跟着动（`tests/test_gateway_contract.py::ExitProjectionTests`） |
| 5.4 | 单循环化：`set_ui_loop` / `call_on_ui` 在无头模式退化为恒等 | `engine_worker.py:245,253,258`、`base.py:193,218,236` | 无头模式下断言"没有任何回调试图投递 UI 循环"；原生句柄仍只在自己的线程上被碰（QuickJS 派发线程、`tts-system`，见记忆 `tts-pyttsx3-com-same-thread`、`quickjs-native-crash-risk`） |
| 5.5 | 无头入口 `main.py --serve` | `main.py:99-208` 装配不变，去掉 `ft.run`（`:384`） | 护栏：**serve 路径 `import flet` 必须为 0**（扫 AST，不许豁免） |
| 5.6 | 关闭路径 | 复用今天定的规矩：原生对象由**自己的拥有线程**释放，`close()` 只排活不 join | 实测退出码 0 + 关窗→消失延迟读数（今天基线 0.4/0.5s，迁移后必须重新量再写进文档） |

红线更新：现在的 §7.5"视图不 import 服务"换成"协议面不出现服务实例"，两侧同一条 CI 检查。

---

## 6. Flutter 侧架构（主流形状，不抄平目录）

### 6.1 目录：feature-first 模块化单体
```
lib/
  core/{rpc,events,media,di,theme,logging,widgets}/
  features/<name>/{data,domain,presentation}/   # bookshelf, find_books, sources,
                                                 # check, reader, rss, readaloud,
                                                 # dict, replace, font, tools, settings
  app/{router.dart, shell.dart, bootstrap.dart}
```
真果鉴是一个平目录 85 文件、单文件最大 2,105 行——**反面样本**。我们的 UI 面
15,407 行 Python 迁过去大概率落在 2–3 万行 Dart，必须靠目录切边界。

### 6.2 状态与导航
- **Riverpod**（`flutter_riverpod` + codegen-free 写法起步），异步数据一律
  `AsyncNotifierProvider`；理由：可测（provider override 就是注替身）、无 `BuildContext` 依赖、
  与 §6.3 的仓库抽象天然配合。**不用裸 setState**（它 229 处，正是 god-file 的成因）。
- **go_router** + `ShellRoute` 承载导航脊柱；路由表照现有 `NAV_META` 与"组内页签"形状平移
  （记忆 `navigation-rail-old-architecture-shape`：同类功能合成页签、切签不退订、
  进阅读先落书架）。目的地不是 View 而是 shell 的一个 child。
- 窄窗：`navigation.py:44` 那个 720 阈值 + "只按跨档重建"（`:771-780`）换成
  `LayoutBuilder` + 一个 `Breakpoint` 枚举，**档位语义与"切档才重建"这条要照搬**，
  它是上一轮实测换来的（`docs/dev-logs/2026-10-07.md` §1–§5）。阅读器三档宽度
  `{620,700,760}`（`reader_view.py:60`）作为默认值带过去。

### 6.3 数据层：抽象仓库 + 两个实现（抄它最有效的一条）
```dart
abstract class EngineRepository {              // 默认实现一律 throw
  Future<List<BookshelfItem>> listBookshelf(); // ~40 个方法起步
  Stream<EngineEvent> events();
}
class RpcEngineRepository implements EngineRepository { /* 走 §4 */ }
class FakeEngineRepository extends EngineRepository { /* 测试替身，黄金样本喂数据 */ }
```
`AppRepository` 默认体抛 `AppFailure('当前环境不支持…')` 这一手同时解决了两件事：
测试接缝 + 平台能力缺位的表达（它用于 iOS 无后台下载，`lib/main.dart:83-98`）。
本项目的等价场景是 `--serve` 远程引擎、以及某平台上 WebView 规则源不可用。

### 6.4 功能替换（4 个 UI 耦合服务的去处，§7 表）
- 阅读排版：`RichText` 之上自己算分页——`TextPainter`/`Paragraph` 逐页切、
  书签/进度按**字符偏移**锚定（沿用我们现在的锚定语义，不落 flet 的滚动位置）。
  CJK 断行、标点禁则、竖排（如果要）是 Flutter 的强项，也是这次迁移的主要产品收益之一。
- 翻页动画 / 沉浸模式 / 亮度：Flutter 原生能力，`reader_view.py` 1,215 + `reader_controller.py`
  1,499 行里相当一部分是当初绕 flet 限制写的，迁过去会**缩小**。
- 音频/视频/漫画：`media_kit`（真果鉴用它，Windows 侧靠本地覆盖 + SHA256 pin 才稳）
  或 `audioplayers`；TTS 出声仍在 Python（SAPI 线程亲和已修），Flutter 只管播放控制与
  迷你播放条，状态由 `ReadAloudState` 事件驱动。
- 托盘/老板键：`window_manager` + 系统托盘插件（真果鉴也用 `window_manager`）。

---

## 7. 32 个服务 → action 命名空间 → Flutter feature → 现有 UI 行数

| 服务域（`main.py` 注册处） | action 前缀 | Flutter feature | 现在的前端行数落点 |
| --- | --- | --- | --- |
| Storage/Settings/EventBus/EngineHub | `engine.*` | `core/`（连接、握手、健康） | `navigation.py` 781（壳） |
| RuleEngine | `engine.rule.*`（调试/取回，**默认不开放**） | `features/sources`(规则编辑) | `rule_repair_*` 485+ |
| Source/SourceCheck/CompatAudit/OnlineImport | `source.*` `check.*` | `find_books` `sources` `check` | `find_books_controller` 510、`book_source_list_controller` 426 |
| Reading/BookCache/Bookmark/ContentSearch/Download/ImageBook | `reader.*` `book.*` `download.*` | `reader` `bookshelf` | `reader_view` 1,215 + `reader_controller` 1,499 |
| Rss/LocalBook/ReadAloud | `rss.*` `local.*` `readaloud.*` | `rss` `local_books` `readaloud` | `rss_view` 453、`read_aloud_bar` |
| WebDav/BookExport/ReplaceRule/Dict/Font/QrCode/Update | `webdav.*` `export.*` `replace.*` `dict.*` `font.*` `qr.*` `update.*` | `tools` `settings` | `code_editor_controller` 401 等 |
| Theme/Background | `theme.*` | `core/theme` | M5 主题面 |
| MediaPlay(`media_play.py`) / BossKey(`boss_key_service.py`) / Browser(`browser_service.py`) / WebPageRenderer(`web_page_renderer.py`) | **不进 RPC** | Flutter 原生（media_kit / window_manager / webview） | 见 §8 风险 R3 |

`WebBridgeService` 保持现状（它是给外部工具用的），与 `GatewayService` 共用序列化与鉴权代码，
不合并进程。

---

## 8. 风险（按严重度）与开工前验证

| # | 风险 | 为什么要怕 | 前置验证（G0 内完成） |
| --- | --- | --- | --- |
| R1 | **靠 WebView 才能取正文的书源规则** | 记忆里定过："WebView 只由 URL option 打开"。今天浏览器路由是 `browser_service` 在 headless Chromium 与**可见 WebView** 之间选；进程分离后可见 WebView 归 Flutter，取到的内容要回传引擎再跑规则 | 挑 3 条真用 `<useweb>`/`url,{}` 的源，做一条"Flutter 侧 webview → `engine.webContentReady` RPC → 规则继续跑"的竖切，量通不通、慢多少。**通不过就不迁移** |
| R2 | 原生句柄线程亲和复发 | QuickJS 同 Context 跨线程是 `rc=3221226505` 直接带进程；SAPI 跨线程是静默死锁（本轮 6/6 挂 60s 实测）。网关是新加的一条并发来源 | 网关只调服务、绝不直接碰原生对象；护栏沿用"替身记线程 id"的手法（`tests/test_read_aloud_m4.py` 已有范例） |
| R3 | 4 个 UI 耦合服务的拆分面被低估 | 它们今天靠注入 `page`/控件工作（`media_play.py:129,174` duck-typed flet_video；`browser_service.py:80` 收 page provider） | G0 里先给这 4 个加"无 UI 也能启动"的降级路径并测试，迁移时才有落点 |
| R4 | 性能预期不能靠"Flutter 一定快" | 我们的大列表是实测压出来的（917 真实行 238ms→12ms，虚拟化 + resize 闸门），迁移收益要重新证 | 迁移前后各量同一份书单：首屏、1000 行滚动帧、拖窗每拍；按记忆 `ab-bench-must-interleave` A/B 交替跑。**2026-10-08 迁移前基线已量完**（`.probe/r4_service_baseline.py` + `.probe/r4_ui_baseline.py`，同一份 920 行真库副本）：服务面 S 跨进程 p50 80.6 ms / 6.97 MB，P 同函数进程内 74.6 ms，H 小载荷 1.2 ms ⇒ 那一跳 ≈6 ms、那 920 行数据 ≈79 ms；冷启动到握手 640 ms。界面面首屏 120 + 10 + 561 ms（装配/show/行落满）；滚动每拍**下发 390 ms**，而同页同处理器**空列表 3 ms**（130×，故这是行数的钱不是这台的底价），心跳落后一起从 181 ms 塌回 ≈0；`on_resize` 处理器 **0 ms**、宽档内根树换 **0** 次（第七十八轮的闸门在位），尺寸变更送达 171 ms。本机 `EnumWindows` 25s 不返回 ⇒ OS 真拖那臂作废，改 `page.window` 下发臂并在读数里明写 |
| R5 | 双构建链与 CI | Flutter SDK + VS/MinGW + PyInstaller 三条链；真果鉴为此把测试步骤全设成 `continue-on-error`——我们不学 | 一条流水线：`pytest -q`（硬门）+ `flutter analyze` + `flutter test`（硬门）+ 契约黄金样本；产物两侧对齐 `protocol_version` |
| R6 | 现有 UI 测试资产作废 | **2026-10-08 数过**（起草时那句"44 个文件、166 处控件树断言"是估的）：`tests/` 72 个文件、28 个导入 flet、这 28 个里 1854 行断言（占全库 5099 行的 36%）；且**没有一条真起过 flet 客户端**（`ft.run` 出现 0 次），断言读的全是内存对象树属性——像素/语义树那批仪器在 `.probe/`（19 支 Playwright + 37 支 `ft.run`）。清点与逐文件归堆见 `docs/界面测试资产清点.md` | 割接前把"布局契约"类断言翻成 Dart golden/尺寸测试；翻译不了的列清单**明确放弃**，别假装等价（已清点成 A 1315 / B 69 / C 470 三堆，各写了处置与"为什么没有等价物"） |
| R7 | 移动端是新增面 | Android 上 QuickJS/pyttsx3/playwright 的可用性都要重证；Python 侧不能进 iOS | 首发 Windows；Android 作为协议验证的副产品，不承诺功能齐 |

---

## 9. 迁移路线与决策门（strangler，每道门都能停）

| 门 | 内容 | 规模 | **通过的判据（必须当场量）** |
| --- | --- | --- | --- |
| **G0** | `GatewayService` + action 表 + schema + `--serve` 无头 + 单循环化 + §5 六条护栏 | 1–1.5 周 | 无头进程能被 curl 驱动完成"列书架 / 起一次检测并收进度事件"；serve 路径 import flet = 0；退出码 0 且关窗→消失延迟有读数；全库回归仍全绿 |
| **G1** | Flutter 壳 + `RpcEngineRepository` + 握手 + `bookshelf` 一屏（真数据） | 1 周 | Flutter 那屏的行数/内容与 flet 屏一致（同书单比对——靶子：R4 那份副本上 `bookshelf.list` = **7 行 / 25,012 B**、`source.list` = 920 行 / 6,969,038 B（**这是 §4.2 出口投影之前的载荷**），冷启动到握手 640 ms 也要有同臂读数）；R1 竖切有结论 |
| **G2** | 高频只读面：`find_books` / `sources` / `check` / 设置页 | 2–3 周 | R4 三项量过且 ≥ flet：对照靶就是第八十二轮同一份 920 行副本上的读数（滚动每拍下发 390 ms、其中空列表底价 3 ms；`source.list` 跨进程 p50 80.6 ms；首屏行落满 561 ms）；契约测试覆盖这 4 个域的全部 action。**2026-10-09 第八十五轮交**：三项各有 Dart 侧读数与自己的底价臂（跨进程 Dart A 30.9 ms / Python 同副本重测 20.5 + 1.7，界面两臂只与自家底价比——口径不同不许直读）；31 条动作 30 份真进程靶子；那 1.7 倍归因到载荷后用 §4.2 的出口投影改判成 1.39 倍 |
| **G3** | **阅读器**（最大单块，排版/翻页/进度/书签/换源） | 2–3 周 | 同一本书翻页落点与 flet 版可比（按字符偏移断言）；R1 涉及的源在真机上读到正文。**底座那一半已交（2026-10-09 第八十六轮）**：位置契约这把尺立起来了（章内字符偏移由段号导出，进度/FTS/flet 命中跳转三口共用），`reader.*` 13 条动作各有真进程靶子；**两条判据都要等 G3-C 的 Dart 屏**才第一次有两条读数可比 |
| **G4** | `rss` / 漫画 / 朗读 / 工具域 / 老板键托盘；删 flet 依赖与 4 个 UI 耦合服务；PyInstaller spec 去 flet* | 2–3 周 | flet 从 `requirements.txt` 消失后全库仍绿；打包冒烟（照真果鉴 `smoke_windows.py` 那条：解压产物跑一次 `--package-smoke`） |

累计 **9–12 周**单人全职（G0 之外）。参照系：真果鉴这条路线做出来的是
`lib/` 30,348 行 Dart + Go 核心 19,445 行 + 30 个 Dart 测试文件 + 4,420 行 Go 测试。
**每一道门结束都要能回答"继续还是退回"**——退回成本是 G0/G1 期间 flet 一行没动。

> **进度（2026-10-08）**：G0 四条判据逐条有当场读数（回归 `1898 passed`、serve 路径
> flet 系模块 **0** 行、`engine.shutdown` 0.09s rc=0、关窗→退出 0.5–0.6s rc=0），
> §5 六条护栏全部落到测试里；**R1 竖切判"通"**——跨进程那一趟比进程内只多 1.7 ms 的
> 线上开销（两臂交替、中位数），三条真形状端到端逐条与进程内一致。
> R1 另交一条**必须先做的约束**：`reading_engine._bind_interaction` 那条同步
> `.result()` 的 JS 桥一旦落在网关循环上就自锁（正对照在工作线程 56 ms 回）。
> **§8 R3 同日交**：四个 UI 耦合服务各有了一条"无 UI 也能启动"的降级路径并钉进测试
> （只记账的视频后端 / 老板键诚实拒绝 / `BrowserService` 三级链"静默内核 → 远端宿主 →
> 当场拒" / `looks_like_gate` 搬到 flet-free 的 `block_pages`），`serve()` 里新加那一跳
> `_wire_headless_webview` 就是 R1 那条竖切在产品里的落点；9 处源码级变异全被抓住
> （回归 `1912 passed`，无头装配 33 个服务全 `RUNNING`、flet 模块 **0**）。
> **§8 R2 同日交**：R1 留下的那一笔同步桥拆成两半、各钉一条——`@webjs:` 那条分支改成
> `await asyncio.to_thread(...)`（不再把脚下这条循环交出去等一次 WebView 渲染），
> 新的 `_bridge_from_js` 进桥先问"调用线程正在跑的就是我要等的那条循环吗"、是则当场抛，
> 护栏族 `JsBridgeThreadAffinityTests` 的正对照是同一条桥从工作线程给得出结果；
> 顺带补上反向答复里缺的 `js_result` 那一腿（`@webjs:` 的脚本完成值此前永远回不来）。
> 5 处源码级变异全被抓住，其中拆掉那句问话的一臂 `2 failed in 85.95s` 就是互等的形状
> （回归 `1914 passed`，连跑两轮）。
> **§8 R4 同日交（迁移前基线，两支新探针 + 产品代码零改动）**：服务面三臂拆开了
> "那一跳"与"那份数据"——同一份 920 行真库副本上，跨进程 RPC p50 **80.6 ms / 6.97 MB**、
> 同函数进程内 **74.6 ms**、小载荷 RPC 地板 **1.2 ms**，即那一跳 ≈6 ms、那 920 行 ≈79 ms，
> 冷启动到握手 640 ms；界面面首屏 120 + 10 + 561 ms，滚动每拍**下发 390 ms**，
> 而同页同处理器的**空列表臂 3 ms**（130× ⇒ 这是行数的钱，不是这台的底价），
> `on_resize` 处理器 **0 ms** 且宽档内根树换 **0** 次（第七十八轮闸门的回执）。
> 两臂载荷按 UTF-8 **字节**比（按字符数会虚出 326 KB 假差量），形状不一致就抛。
> **§8 R6 同日交**：`tests/` 数过是 72 文件 / 28 导入 flet / 1,854 行断言（全库 36%），
> 而**没有一条真起过 flet 客户端**（`ft.run` 0 次）——像素与语义树那批仪器都在 `.probe/`；
> 清点与逐文件归堆见 `docs/界面测试资产清点.md`，A 1,315 / B 69 / C 470 三堆各写了处置。
> **G1 同日交（Flutter 壳 + `RpcEngineRepository` + 握手 + `bookshelf` 一屏）**：
> 同书单比对落在**一条实弹臂**上（`.probe/g1_live_run.py` 起真 `--serve` 子进程，语料与
> 靶子同一份 920 行副本）——握手 **624 / 631 / 683 ms**（三发同臂，中位 631）、
> `bookshelf.list` **7 行 / 5–6 ms**、那一发的 HTTP 响应体 **24,981 B**——判据里那个
> 25,012 B 与它是**同一个口径**（R4 数的是 `ok_envelope(...)` 的 UTF-8 字节 = 响应体）。
> 另外两个口径各报各的：`engine.hello` 那一发 418 B、Dart 侧 `jsonEncode` 的数据体
> 24,368 B（与样本文件里那个 24,909 B 的差全是默认分隔符的空格）。
> 三个口径不许相加——第一版就是把 hello 那 418 B 读成了书架那一发的信封，
> 计数器按 action 拆开之后才现形（`docs/dev-logs/2026-10-08.md` 第八十三轮 §2 末）。
> 参照物不是 Dart 自己算的：
> `.probe/g1_flet_projection.py` 无头走 flet 的 `_to_render_record` + `_row` + `_load`，
> 把 7 行的书名/副标题和状态行 `书架共 7 本` 落成
> `tests/gateway/projection/bookshelf.list.json`，Dart 那两侧（屏幕臂与实弹臂）都只许对上这张表。
> 收摊那一臂：`engine.shutdown` 之后 rc=**0** 且端口被拒（正对照 = 刚起的那台连得上）。
> 三处产品缺陷是**测试抓出来的、不是读代码读出来的**：关窗用的 `AppLifecycleListener`
> 因为 `late final` 唯一的读发生在 `dispose` 而**从未注册**（引擎留成孤儿进程）、
> provider 默认重试策略让一次失败背靠背发出 **11** 发 `bookshelf.list` 并把错误屏翻回
> 转圈、壳的宽档分支漏 `child`（窄档那一半前一交已记）。
> Dart `69 passed / 2 skipped`（跳过的那两臂要真 python 环境，跑法见上面那个探针）、
> 跨语言守卫新加一臂"书架那三处文案以 flet 源码为准"（两个方向各变异一次，都被抓住）、
> Python 全库 `1923 passed, 161 subtests passed in 124.92s`。
> G1 同时暴露一条服务语义缺口：`--idle-seconds` 在 RPC-only 这条路上**从没武装过**，
> 客户端不告而别时引擎永留。同日第八十四轮已修（走法一的收窄版：只有**成功办完的动作**
> 才算见过客户端，`/rpc` 与 `/media` 都掐表/武装，在途 job 推迟一个窗口再看；
> 探针豁免搬去 `--idle-seconds 0`）。现场臂（真子进程、不发 `engine.shutdown` 直接闭嘴）
> 10 发里 8 发 `1.06–1.08s` 自己收摊、rc 全 0、端口全拒连；Python 全库随之变成
> `1928 passed, 161 subtests passed in 152.08s`。见 `docs/known-issues/005`。
> 修完才看得见的一条新问题（收尾偶发 +5.00s，49 发 4 发，`engine_worker.py:228` 的
> join 走满，归因未定）另记 `docs/known-issues/006`。
> 读数、仪器与变异核验见 `docs/dev-logs/2026-10-08.md`。
> **G2 同日交（2026-10-09，第八十五轮）**：action 表从 9 条长到 **31 条**
> （`call` 23 / `sync` 6 / `job` 2，9 个命名空间），模型不过 RPC——服务侧补了 5 条 JSON 门面
> （`shelve_by_url`、`browse_categories`、`browse_books`、`search_in_scope`、`read_keys`），
> 外加一条 `search.scopes`（`ReadingService.scope_table`：**"哪些源能动"只留一份算法**）。
> 语料库立了新规矩：**每份样本盖着抓取那趟握手的 `schema_digest`**（现值 `ffef64386bec`），
> 改表或改 schema 不重抓就全库红；真 `--serve` 子进程一趟落 **31** 份样本（30 条动作各有靶子，
> 唯一例外 `engine.webContentReady` 的理由写死在例外表里，覆盖那支钉的是"缺的集合**恰好等于**
> 例外表"），`mode=job` 的两份另带 `/ws` 那一段交付。
> Dart 面：仓库面 29 发 + `find_books`/`sources`/`check`/`appearance` 四屏 + `/ws` 事件流，
> `golden_widget_test.dart` 用 `FakeEngineRepository` **吃黄金样本的原始 dict**、答案与
> **flet 自己渲染出来的**投影表逐字比（`.probe/g2e_projection_tables.py` 直接调
> `_summary_line` / `BookSourceListView()._row` 再读回 `ft.Text.value`，答案不许手填）。
> **§9 G2 判据（R4 三项 ≥ flet）的读数**——靶子在同一份 920 行副本上**当天重测**
> （`.probe/g2f_dart_baseline.py`，输出 `.probe/g2f_dart_baseline_run8.log`）：
> 跨进程那发 Dart **A 全程 p50 30.9 ms**（B 只到线 22.6，与 Python S 同跨度之比 1.1 倍），
> 底价臂 `engine.status` 2.4 ms；首屏 Dart 920 行 **110.5 ms** 对空列表底价 **35.0 ms**
> （Δ75.5，一帧只挂 7/920 行）；滚动每拍 **62.7 ms** 对"落点不动"底价 **1.3 ms**
> （Δ61.4，24/24 拍一格没换）。界面那两臂与 flet 的数**不可直读**（`flutter_test` 无 GPU
> 光栅化 + ListView 虚拟化），只与各臂自己的底价比，日志末尾明写这一条口径。
> **量出来的那 1.7 倍不是 Dart 的错，是载荷的错**：`.probe/g2f_payload_anatomy.py` 逐键数字节，
> 那一发 6,969,039 B 里 `exploreUrl` 40.1 % + `jsLib` 23.5 % + 五组 `rule*` 24.0 % 全是列表屏
> 不显示的规则原文（Dart 解它：转码 13.4 + `jsonDecode` 64.4 ms，而 Python 侧整发才 101.2 ms）。
> 于是 §4.2 那张显式表新增一格 `fields`（**出口字段白名单**，`serialize.project` 在 `to_json`
> 之后、发信封之前逐行收口，且**算进 `schema_digest`**），`source.list` 从 6,969,039 B 到
> **351,554 B（5.1 %）**，总账 **1.7 倍 → 1.39 倍**；`source.get` 故意不投影（编辑器的回填底本），
> flet 投影表重生成后那 920 行的 `title`/`subtitle` **一字节没变**。
> 变异核验四批 **45 发全红**（跨语言守卫 10 / Dart 测试面 14 / 出口投影 6 / 基准仪器 15，
> 每批先做选择器自检），语料库那五笔账的变异证据长在守卫里
> （`test_mutation_controls_show_the_guards_are_not_deaf`）。回归：Python
> `1956 passed, 161 subtests passed in 188.44s`；Dart `164 passed / 3 skipped`、
> `flutter analyze` 只剩 6 条既有 info。读数、仪器坑（含"每行键数固定"这个被 Gson 丢 null
> 打死的假守卫、探针里的字节底价该换成现读键集）见 `docs/dev-logs/2026-10-09.md`。
> **G3-A + G3-B 同日交（2026-10-09，第八十六轮）**：先立**一把尺**再开协议面——
> 章内字符偏移 = `"\n".join(chapter_paragraphs(display_blocks(...)))` 的下标
> （`app/core/text_layout.py`，段间那 1 个字符叫 `PARAGRAPH_JOINER_LEN`），三个此前各数一遍的
> 读数口（进度存读、FTS 索引的段首偏移、flet 命中跳转）现在共用它；17 条守卫 + 13 发变异全红。
> `reader.*` 那 **13 条动作**（表从 31 长到 **44**：`call` 35 / `sync` 7 / `job` 2，10 个命名空间）
> 各配一份入参 schema（`app/gateway/schema/*.json` 44 份）、一根真进程靶子：会话是**单例**，
> 所以门面**显式吃 url**、对不上就抛 `ReaderSessionError`（不靠隐式状态猜），出参
> `ChapterItem`/`ChapterContent`/`ReadingPosition` 全部序列化成 dict（二进制块只报
> `binary`+`url` 不给值，§4.4）。语料 56 份 / `digest=34822a1a5e7e`，reader 面读数
> **目录 6 章 / 正文 8 段 5,802 字 / 书内命中 4 条**，`reader.toc` 走 §4.2 那格出口投影
> （8 格），并收下语料里**第一发故意失败**的信封（`reader.toc--no-session`：拒答而不是回空目录，
> 回空目录看起来像书坏了）。三处产品缺陷是活臂撞出来的：RPC 存的位置被 `reader.close` 盖回默认值、
> 受保护插图过协议面就失去可渲染地址（`GET /media/{key}` 尚未建，地址是客户端唯一把手）、
> `to_json` 不按类型泄漏所以泄漏守卫必须打裸返回值；第四条是**仪器自己造信号**——扫 Dart
> 调用点那把尺把 `action_budgets.dart` 的表键读成调用点，"每一发都有人调"当场失去牙齿。
> 协议先行、界面未接的那 13 发记在**具名欠据表** `_UNWIRED_PENDING_GATE`（判据仍是键集相等，
> 两个方向都咬）。变异核验 **39 发全红 / 26 条守卫各有靶子**。回归：Python
> `1995 passed, 161 subtests passed in 141.61s`；Dart `164 passed / 3 skipped`、
> `flutter analyze` 仍是 6 条既有 info。账见 `docs/dev-logs/2026-10-09.md` 第八十六轮。

---

## 10. 测试策略

- **契约**：每个 action 一份黄金样本（请求/schema/响应三元组，`tests/gateway/golden/*.json`），
  两侧各自比对；schema 改了没同步 → 红。按记忆 `guard-tests-assert-absence` 写"没发生"断言
  （例如"响应里出现了 `app.` 实例"、"某个 action 没有对应服务方法"），别只断言"成功返回"。
  G1 另外立了 `tests/gateway/projection/*.json`：那一摞不是信封样本而是**flet 那屏自己算出来的
  参照表**（书名/副标题/状态行的逐行文本），它没有请求也没有响应，混进 `golden/` 只会逼
  守卫为它开一条特例。
- **Python 面**：现有回归（2026-10-09 第八十六轮后实测 `1995 passed, 161 subtests passed`）基本不动；
  网关测试全离线（`127.0.0.1` 假服务，沿用
  记忆里"离线回环探针"的套路，不批量访问真实站点）。
  一条跑法上的坑：一次全量会在机器上留下约 **1100 个 TIME_WAIT**（Windows 默认 120s 退），
  紧接着再跑一遍或紧跟一轮多进程探针，就会在随机的某条用例上撞 `WinError 10055`
  （建 loop 时 `socket.socketpair()` 拿不到缓冲区），受害者每次都不一样、且都与网关无关。
  看到"随机的 loop/socket 建立失败"先数 TIME_WAIT，别先怀疑代码。
- **Dart 面**：`FakeEngineRepository` 喂黄金样本跑 widget 测试（真果鉴的
  `test/fixtures.dart:34` 那一手）；排版/窄窗用 golden test；`integration_test/` 打一个
  真 `--serve` 引擎跑 3 条端到端。
  **这条不是"平移"而是"新建"**：R6 数过 `tests/` 里 `ft.run` 出现 **0** 次，现有 1,854 行
  界面断言读的全是内存对象树属性，像素/语义树的仪器都在 `.probe/`（见
  `docs/界面测试资产清点.md`）。所以 Dart widget/golden 那一层是本项目第一次有真客户端在
  被测——别用"翻过去多少条"当进度，要用"这一屏的 A 堆断言有没有对应的 Dart 绿"当进度。
- **性能**：A/B 交替，两臂同机同数据；先跑的那臂会背冷启动的钱（记忆 `ab-bench-must-interleave`）。
  每一组对照都得有一臂**只差被测那个变量**的底价臂（R4 用的是"同一页同处理器的空列表"与
  "同进程同出口的小载荷 RPC"），否则读数归不到因（记忆 `measurement-corpus-must-not-be-self-proving`）；
  载荷一律按 UTF-8 **字节**比，不按字符数。

---

## 11. 不做的事

- 不做 iOS（Python 侧进不了 App Store）。
- 不借道"嵌 CPython 到 Flutter 进程"（§3）。
- 不改规则引擎语义：Legado 对齐结论（`docs/找书与换源逻辑对齐.md`）先读再动，
  迁移期一条规则都不许"顺手改"。
- 不在迁移期加新功能。UI 重写 + 协议 + 排版三件事已经够多。
- 不抄真果鉴的：平目录 `lib/`、裸 `setState`、硬编码版本号、2s 轮询进度、
  `continue-on-error` 的 CI。

## 12. 相关

- `docs/current-system-design.md`（§2 总体架构、§4 服务清单、§5 UI 层、§9 打包——本文的对照面）
- `docs/界面测试资产清点.md`（§8 R6：A/B/C 三堆、逐文件归堆表、割接纪律）
- `docs/桌面端重构设计.md`、`docs/阅读器UI响应式与沉浸阅读设计.md`
- `docs/dev-logs/2026-10-07.md` §10（线程亲和修法与 0.4s 退出读数）、`docs/known-issues/004_…md`
- `docs/dev-logs/2026-10-08.md`（第七十九~八十四轮：G0 护栏、R1/R2/R3/R4/R6 的读数与仪器坑、G1、005）
- `docs/dev-logs/2026-10-09.md`（第八十五轮：**G2 整道门**——31 条动作、语料库被现契约钉住、
  Dart 四屏、R4 三项复量，以及那个把 1.7 倍改判成 1.39 倍的出口投影）
- `docs/M7打包说明.md`（PyInstaller 命令与 flet* hidden-import 清单位置）
- 参照仓库：`github.com/jerunshi/guoapp1`（浅克隆 `D:/tmp/guoapp1`，不在本项目版本控制内）
- 记忆：`tts-pyttsx3-com-same-thread`、`quickjs-native-crash-risk`、`engine-hub-loop-split`、
  `ui-progress-must-travel-as-events`、`refactor-regression-orphan-methods`、
  `guard-tests-assert-absence`、`ab-bench-must-interleave`、`offline-loopback-probe`、
  `legado-analyzer-empirical-semantics`、`navigation-rail-old-architecture-shape`、
  `measurement-corpus-must-not-be-self-proving`、`probe-stand-in-must-not-be-the-measured-workload`、
  `flet-desktop-probe-instrumentation`、`flet-0-86-narrow-window-sizing-contract`
