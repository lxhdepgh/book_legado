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

> **落地形状（2026-10-10 第九十五轮，G3-D4）**：那一条"不新造"当场收成两种 key——
> 封面 `^[0-9a-f]{16}\.jpg$`（名字出自 `SourceService._normalize_cover` 写在
> `image-cache/covers` 上的文件名）与正文插图 `<sha256(源,书)>-<sha256(章)>-<块下标>`
> （三段就是 `reader-cache` 的目录名、内容文件名、`blocks` 下标）。**语法与取字节都住在
> `app/services/reading_cache.py`**（存它的那一方），网关只问 `is_media_key`/`media_bytes`。
> 分隔符是 `-` 不是 `/`：`{key}` 必须是单段路径，而这套语法里 `.` 与 `/` 表达不出来，
> 于是穿越是"拼不出"而不是"被拦下"。地址由服务端在 `reader.content` 的块表里发下
> （`mediaKey` 那一格恒在，空串=这块没有字节），客户端只贴不拼——**地址即凭据**。
> 已知代价与它的换法（G4 漫画）：按需读时每张图片摊一次章 JSON 的 parse，一页 30 张
> ≈786 KB / 中位 1.47 ms / 整页 44.4 ms，文字书够用，漫画要换成兄弟文件 + `read_bytes`。

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
| **G3** | **阅读器**（最大单块，排版/翻页/进度/书签/换源） | 2–3 周 | 同一本书翻页落点与 flet 版可比（按字符偏移断言）；R1 涉及的源在真机上读到正文。**底座那一半已交（2026-10-09 第八十六轮）**：位置契约这把尺立起来了（章内字符偏移由段号导出，进度/FTS/flet 命中跳转三口共用），`reader.*` 13 条动作各有真进程靶子。**flet 那一半的读数也已落表（第八十七轮）**：`tests/gateway/projection/reader.content*.json` 三张（黄金那一发 8 段 / 5,802 字 / 屏上 9 行 / 段首偏移 `[0,168,1578,1884,4053,4175,5286,5799]`，另两发合成输入补"媒体占行不产段"与"标题行认得出也占第 0 段"），答案由 flet 自己的函数现场读回、10 条守卫 + 16 发变异钉着。**Dart 的协议面那一半也交了（第八十八轮）**：7 个模型 620 行按样本 `field_names` 建、13 发各有 Dart 实现、13 格欠据清空，绑定表 25 类↔20 发样本 / 158 键，那把尺有了跨发互指读数（夹到末尾的 `chapter_pos` 5,802 == `content.totalChars`，客户端自数 5,805 以**不等号**钉死），23 发变异全红。**屏那一半也交了，判据的第一条读数到手（第八十九轮）**：Dart 阅读屏 921 行 +
状态机 799 行 + 第二把尺 297 行落地，语料从被忽略的 `.probe/` 搬进
`tests/gateway/fake_source_reader.py`（152 行，逐字移植当年产 56 发样本那一份）并被 7 条守卫
钉在样本与投影表上（离线纯管线复现 8 段 / 5,802 字 / 段首偏移整列）；实弹臂给出三头并排的读数
**服务端 8 段 5,802 字 · Dart 8 段 5,802 字 · flet 投影 8 段 5,802 字**，落点往返
`offsets[3] = 1,884` 存进去、读回来还是那一个段号，越界那一臂夹到章尾且不等于原值。
清点重测：44 条动作里 **1 条**无 Dart 字面量（反向通道那一发）、**19 条只在 `core/`**（上一轮 33），
`reader.*` 13 发里 12 发进了 `features/reader/`，`reader.position_get` 是唯一只证往返的那格（书架那句
"读到哪了"由 `bookshelf.list` 的 `reading` 供，flet 侧同样没有 UI 调用点）。扫调用点那把尺本轮
从"认字面量排邻居"改成**只认调用位**（`heroTag:` 与模式常量顶穿过 `Key(` 那条前缀）。
**R1 那一半的第一档也交了（2026-10-10 第九十一轮，G3-D1）**：`/ws` 反向 `request` 帧与
`engine.webContentReady` 在 Dart 侧有了模型、宿主接缝与真调用点，判据第二条第一次有两个宿主语言的
读数（Dart 自己当宿主：**服务端 4 段 1961 字 · Dart 4 段 1961 字 · 锚点 4 段 1961 字**、墙钟 909ms、
`engine.webContentReady 出 6142B 入 91B`；Python aiohost 同一条路 407ms 同数），
锚点是 `tests/gateway/projection/reader.content.webview.json`（语料新加的 `/g3web` 那一族
**两种形状**各过一遍纯管线），不作答那一臂把报错归回宿主那 20s 而不是 `reader.content` 的 60s。
**可见 WebView 插件不在本轮**（零新依赖、只留 seam；G4 选型清单在日志 §8）。
**连续阅读那一档也交了（2026-10-10 第九十二轮，G3-D2，离线面）**：flet 的"读到章尾自动接下一章"
在 Dart 侧落地——追加不动 `loadSeq`、不动视口，段起点按**已挂牌的段数**（不是行数：媒体块与刚补的
分隔线/章名都占行不产段）累加，落库三格换成**本章**口径；迟滞 180/500 与 flet 同数，
flet 那本 `_continuous_loaded` 台账**有意不抄**（它在"翻章→退回"之后会永久关掉自动接，Dart 查的是挂载表本身）。
屏侧的 GlobalKey 键改成**整篇段号**（撞一次键就把上一章那一行报成本章落点）。
本轮按"过程中不跑测试"的授权执行，**通过数与变异核验欠到批量验证那一趟**，日志里只留静态读数。
**服务面那一档已交（2026-10-10 第九十三轮，G3-D3 前半）**：`switch.seeds/scan/snapshot/cancel/apply`
五发登记上表（44→49），扫描走 `mode="job"`，进度与整份候选各一条事件，排队规则收进服务侧一份；
五发的黄金样本与进度面守卫在手。
**Dart 那一档也交了（2026-10-10 第九十四轮，G3-D3 后半）**：仓库面五发、两个模型文件、
换源屏与 notifier 落地，`_UNWIRED_PENDING_GATE` 那五张欠据划掉——当场复核 `never_called` 为**空**，
49 发第一次全有调用点。
**R1 那一半也交了（2026-10-10 第九十五轮，G3-D4）**：`GET /media/{key}` 现在是两种形状一套
Range——封面名（`^[0-9a-f]{16}\.jpg$`，出自 `SourceService._normalize_cover`）与正文插图名
（`<书摘要>-<章摘要>-<块下标>`，语法住在 `app/services/reading_cache.py`，网关只问
`is_media_key`/`media_bytes`）；协议面新增 `mediaKey` 那一格由服务端发、恒在，Dart 只贴不拼。
剩下的是 G4 的插件决定 |
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
> 受保护插图过协议面就失去可渲染地址（`GET /media/{key}` 尚未建，地址是客户端唯一把手——
> **那一发已在第九十五轮 G3-D4 建好**：字节改由块表里新增的 `mediaKey` 领地址，见 §4.4）、
> `to_json` 不按类型泄漏所以泄漏守卫必须打裸返回值；第四条是**仪器自己造信号**——扫 Dart
> 调用点那把尺把 `action_budgets.dart` 的表键读成调用点，"每一发都有人调"当场失去牙齿。
> 协议先行、界面未接的那 13 发记在**具名欠据表** `_UNWIRED_PENDING_GATE`（判据仍是键集相等，
> 两个方向都咬）。变异核验 **39 发全红 / 26 条守卫各有靶子**。回归：Python
> `1995 passed, 161 subtests passed in 141.61s`；Dart `164 passed / 3 skipped`、
> `flutter analyze` 仍是 6 条既有 info。账见 `docs/dev-logs/2026-10-09.md` 第八十六轮。
> **G3-C1 同日交（第八十七轮）**：判据里"flet 那一半的读数"落成
> `tests/gateway/projection/reader.content*.json` **三张表**（黄金那一发 8 段 / 5,802 字 /
> 屏上 9 行 / 26 发落点探针；另两发**合成输入**专门补"媒体占一行不产段"与"标题行既认得出
> 又占第 0 段"这两种会让行号与段号分裂的形状）。表里的答案全部由 flet 自己的函数现场读回
> （`ReaderView.content_plan` + `app.core.text_layout` 坐标族），一个数字不手写——两侧各跑
> 一遍同一个公式只能证明公式抄得对，证明不了屏上第 k 段就是索引里第 k 段。守卫
> `test_reader_position_projection.py` **10 条**（保鲜 / 与协议面同数 / 不是那三把错尺 /
> 行段与存读互逆），变异核验 **16 发全红**：一半改生产代码（尺动了表必须显出陈旧），
> 一半改表本身（把答案抄成按块数、漏算段间格、按显示形态、行号当段号）。两头发现在
> "改产物的变异打不红改代码那一族守卫"，靶子按这一条重配过一次。本轮**生产代码零改动**，
> 唯一一次写脏仓库是跑批用 `write_text` 还原把三份 LF 产物整文件换成 CRLF（`git diff` 在
> `core.autocrlf` 下看不出来，只有 `git status` 报 M），已按字节恢复并改成 `md5sum -c` 判等。
> 回归：Python `2005 passed, 161 subtests passed in 142.80s`；Dart 不变。账见同日志第八十七轮。
> **G3-C2 同日交（第八十八轮）**：Dart 的 reader 仓库面与 **7 个模型 620 行**落地（`ReadingPosition`
> / `ChapterRow` / `ChapterDocument` / `BookmarkRow` / `StagedRecord` / `ReaderSession` /
> `ContentSearchResult`，键名严格按样本 `field_names`），13 格 `_UNWIRED_PENDING_GATE` **清空**
> 而判据照跑（比键集，G3-D/G4 落地时同一张嘴继续咬）。键名绑定表从 15 类 / 91 键涨到
> **25 类 ↔ 20 发样本 / 实测 158 键**，两条正对照（feed ≥ 25、键 ≥ 150）钉着"扫描器没空转"。
> 那把尺第一次有了**跨发互指**的读数：`position_save--clamped.chapter_pos == reader.content.totalChars
> == 5,802`，而客户端按裸值自数是 **5,805**——golden 测试里断言的是**不等号**（写死 5,805 就等于
> 承认 Dart 侧也用那把错尺）；块 **3** ≠ 段 **8**、`written` **32**（4 章 × 8 段）≠ `hits` **4**、
> 书签 `chapter_ratio` **0.25** 在 add 请求与读回的清单上是同一个数。撞出并修掉一处产品缺陷：
> `reader.close` 的 `staged` 走服务端 `_book_item_dict` 的 getattr 兜底分支，回的是"键齐全、值全空"
> 的壳（+8 行走 dict 分支，同形状由 `test_reader_rpc_surface.py` 一条守卫钉）。**这一支语料里没有
> 证人**（4 发 close 的 `staged` 全为 null），没有为重抓语料而翻掉 **9 发带 `last_read_at` 时间戳**
> 的样本及其 sha256——代价讲清楚：Dart 那条断言现在钉的是"形状对齐"不是"服务端真会回非 null"。
> `reader.position_get` 的 null 同理是合成证人。清点口径当场量：44 条动作里 **1 条**在 Dart
> 没有任何字面量（`engine.webContentReady`，反向通道）、**33 条只在 `core/`**（协议面有、屏还没调），
> 所以这里的"Dart 有人落"= **协议面有实现**，界面调没调由 widget/golden 那一面负责（G3-C3）。
> 变异核验 **23 发全红 / 14 条守卫各有靶子**（2 发打现算侧、14 发改模型解译、7 发改 payload），
> 跑批前做选择器自检（干净态 23 发全打得中且全绿）。回归：Python
> `2006 passed, 1 warning, 161 subtests passed in 158.07s`；Dart `191 passed / 3 skipped`、
> `flutter analyze` 仍 6 条既有 info。账见同日志第八十八轮。
> **G3-C3 同日交（第八十九轮）**：判据"同一本书翻页落点与 flet 版可比（按字符偏移断言）"第一次有了
> **三头并排的读数**——`LIVE 第2章 服务端 8 段 5802 字 · Dart 8 段 5802 字 · flet 投影 8 段 5802 字 ·
> 目录 6 章`（真 `--serve` 子进程 + 真假源子进程，`.probe/g3c3_*`）。落地的是**屏**那一面：
> `reader_screen.dart` 921 行、`reader_controller.dart` 799 行（六条 flet 规矩：会话坐标以服务端回话为准 /
> 章坐标用 `reader.toc` 行序号 / 段号优先于像素 / 换章那一拍不落库 / 0.5s 合流 / 转正-放弃排在关会话之前），
> `text_layout.dart` 297 行是 `app/core/text_layout.py` 的**独立第二份实现**，两份的相等由服务端回话、
> flet 三张投影表、真子进程三处外部读数钉着（**不是**两份互点）。屏的参照不能由 Dart 自造，于是把语料
> 从被忽略的 `.probe/` 搬进 `tests/gateway/fake_source_reader.py`（152 行，HTML 逐字移植当年产 56 发
> 样本那一份），再用 7 条守卫把它钉在样本与投影表上：离线纯管线复现 8 段 / 5,802 字 / 段首偏移
> `[0,168,1578,1884,4053,4175,5286,5799]` 三处同数。搬进来第一件事是被自己的守卫抓的——
> `--port 0` 时 `/g3/source.json` 回 `...:0`（内核挑的真值在 `runner.addresses` 不在参数里），
> 修法配**双向读数**（删掉那一行就跑出 `:0`）。两处口径是被守卫教的：引擎 `@html` 取的是 `str(node)`
> 外层（`rule_engine.py:1885`）不是 `get_text()`，`a@href` 要读属性；`<usehtml>` 对尺**静默**（两族都剥标记），
> 所以那条守卫比的是渲染前的块。Flutter 侧三条仪器坑各有代码注释：路由级 `ProviderScope` **只覆盖 args
> 不够**（未覆盖的 provider 元素落在根容器、它读的也是根容器，于是 `extra` 明明进来而屏上说"没有收到书"）；
> **滚动通知到达时不许当场量几何**（像素先更新、行还挂在上一帧，量到的是滚之前那一段——合流只有 0.5s，
> 慢一帧就存错一整段）；`pumpAndSettle` **推不动 `Timer`**（节流那一发永远等不到）。落位不是 `ensureVisible`
> 一帧的事：`ListView` 只建视口附近那一截，5,802 字约 9 屏高，存到第 7 段时那一行还没有 context，
> 直接落位会**静默失效**（屏上是章首、用户以为进度丢了）→ 先按方向一屏一屏揭到它被建出来
> （方向不能写死向下：换章时正文整篇替换而像素跟着留下），窗口关掉再**补报一次当前视口**。
> 落点往返用投影表那一格当期望（`offsets[3] = 1,884` 存读同数、`paragraphIndex == 3`），越界那一臂发
> `total + 9999` 必须夹到 `<= totalChars`、段号 `== 段数 − 1`，且**不等于**刚存的那一个。
> 仪器回归一处（本轮唯一的红）：扫 Dart 调用点那把尺把 `heroTag: 'reader.prev'` 与
> `static const String kFontSizeMode = 'reader.font_size'` 读成"客户端在调用没登记过的动作"，
> `Key(` 那条前缀已被 `heroTag` 顶穿一次，遂把口径改成**只认调用位**（五种同形邻居自动出局；
> 代价写在 docstring：`call(kAction)` 会变隐形并报红，有意如此）。清点重测：44 条里 **1 条**无 Dart 字面量、
> **19 条只在 `core/`**（上一轮 33），`reader.*` 13 发里 **12 发**进了 `features/reader/`，
> `reader.position_get` 如实交代为"只有实弹臂在证往返"（书架那句"读到哪了"由 `bookshelf.list` 的
> `reading` 供，flet 侧同样没有 UI 调用点）。变异核验 **8 发里 7 发响 + 1 发是同值空转探针**
> （书名换成样本里本来就写着的那几个字等于没改，能区分的是改作者那一发）、另有 `PORT` 行**双向**臂与
> 底价对照（不改任何东西、六笔守卫全过）；两处仪器自己的错记进日志（变异臂里调**被打的那个属性** →
> `RecursionError`；子进程守卫不吃进程内补丁 → M6 改成**文件级**变异）。**Python 侧本轮零生产改动**。
> 回归：Python `2013 passed, 161 subtests passed in 144.38s`（+7 全在 fixture 守卫）；
> Dart `243 passed / 6 skipped`（带 `LEGODO_PYTHON` 则 `248 passed / 1 skipped`；本轮新增
> 屏 29 + 尺 23 + 实弹 3 = 55 条）；`flutter analyze` 仍 6 条既有 info。
> 账见同日志第八十九轮。**G3 只剩 R1 那一半**（G3-D）。
>
> **G3-D1 交（2026-10-10，第九十一轮，`docs/dev-logs/2026-10-10.md`）**：R1 那一半的**反向通道**
> 在 Dart 侧落地，判据第二条第一次有**两个宿主语言**的读数——Dart 自己当宿主那臂
> **服务端 4 段 1961 字 · Dart 4 段 1961 字 · 锚点 4 段 1961 字**（墙钟 909ms，
> `engine.webContentReady 出 6142B 入 91B`），Python 侧 aiohost 那臂同一条路 407ms 同数。
> 语料新加 `/g3web` 那一族的**两种形状**（墙带 `type="password"`、`?rendered=1` 是宿主答的那一版）
> 与 `/hits.json` **取页账**（被取的一方记账，不是宿主自报），锚点表
> `tests/gateway/projection/reader.content.webview.json`（host 4 段 / 1961 字 /
> 偏移 `[0,81,756,963]`，wall 1 段 / 8 字）由第 6 笔守卫在两形状上各复现一遍。
> Dart 侧三面：`core/events/webview_request.dart` 86 行（一帧**只有一个出口**——`decodeFrame`
> 现在对 request 帧抛 transport）、`core/webview/webview_host.dart` 187 行
> （`WebviewHost` 抽象 + `ScriptedWebviewHost` + `attachWebviewHost`；broadcast 不缓存，
> 所以 attach 必须赶在任何 `reader.content` 之前）、仓库面 `engine.webContentReady` 两处**不是风格**
> 的取舍：`singleFlight: false`（这一发是答**两条不同 request_id** 的渲染，顶替会把已交出的正文
> 读成"我没答上"；对照是 `client.inFlight` 为空，且两并发用**同一个** client）与
> `js_result` **按 null 不进 payload**（"宿主没跑脚本"≠"脚本返回了空"，下游读的是有没有这个键）。
> 服务端一处生产改动（本轮唯一）：`request_web_content` 到点那句裸 `TimeoutError` 归回
> `WebviewUnavailable("宿主在 20s 内没答这次渲染")`——它此前被 `_h_rpc` 折成
> "reader.content 超过服务端预算 60s"，**墙钟 21.4s 报 60s、指着错的预算**；这条错是 Dart 实弹臂
> 撞出来的，现由那一臂反向钉着（不作答臂 20725ms、错里必须含「宿主在」「20s」且不含「超过服务端预算」）。
> 可见 WebView 插件按上一轮决策**不进本轮**（零新依赖，只留 seam），G4 选型清单与三条硬判据
> （后台渲染 / cookie 同源 / `javaScript` 能跑）见日志 §8，pub 读数是当天取的：
> `flutter_inappwebview` 6.1.5（Windows 那半 0.6.0，两条都约两年没发版）、`webview_windows` 0.4.0
> （约两年）、`desktop_webview_window` 0.3.0（约四个月，三者里唯一还在动的）。
> 仪器两条：`engine.shutdown` 关的是整棵子进程树（SIGTERM 会留孤儿无头内核攥着 profile，
> "删临时目录"报 errno 32，形状与测试失败同；句柄释放比进程退还晚一拍，故删除带短重试、当趟第 2 次成功）；
> `flutter test --plain-name` **打不中测试时也退 0**，变异判据改成"退出码 + 选中数"。
> 变异核验 **18 发全红**（语料 5 / 服务端 5 / Dart 8，其中两发是跨语言的——`_onData` 那一行分岔
> 离线面完全绕得过，只有实弹臂看得见），干净状态 14 条 selector 每条"选中且绿"。
> 清点重测：44 条动作里**无 Dart 调用点的那一格从 1 变成 0**，"反向通道例外"那张表删掉、
> `_UNWIRED_PENDING_GATE` 成空表而判据照跑（口径提醒：调用位口径下字面量只长在 `core/rpc/`，
> 本轮那个"0 条不在 core/"与上一轮"19 条只在 core/"**没有可比性**）。
> 回归：Python `2014 passed, 161 subtests passed in 162.29s`；
> Dart `259 passed / 9 skipped`（带 env `267 passed / 1 skipped`；本轮新增 16 + 3 = 19 条）；
> `flutter analyze` 10 条 info（6 既有 + 本轮 4 条同族 `use_null_aware_elements`，
> 与相邻 `saveTheme` 同一写法，故意不改成 `?`）。
>
> **G3-D2 交（离线面，2026-10-10，第九十二轮，`docs/dev-logs/2026-10-10.md`）**：连续阅读
> （append 下一章）与文档级落点折算在 Dart 侧落地。三处形状与 flet 同数、一处有意不同：
> 迟滞 180/500 抄 `_APPEND_TRIGGER_DISTANCE`/`_APPEND_REARM_DISTANCE`；段起点按
> **已挂牌的段数**累加而非行数（本语言料三章齐挂是 **22 段 / 27 个控件**，媒体行与刚补的
> 分隔线/章名都占行不产段）；落库三格换成**本章**口径（`chapter_paragraph` 不许写整篇段号，
> 那正是"12 段的章报第 12 段、重开被夹到段尾"那个不可能读数的来路）；flet 的
> `_continuous_loaded` 台账**不抄**——它在"翻章 → 退回上一章"之后会永久关掉自动接，
> Dart 这里查挂载表本身（`page.mountFor(next) != null`），文档换了账就没了。
> 屏侧 `GlobalKey` 的键从"本章行号"改成**整篇段号**（两章各有自己的第 0 段，撞键就把
> 上一章那一行认成视口顶），行表 `documentLines` 成为 `itemCount`/`itemBuilder` 的**唯一**渲染源。
> 守卫新增 **30 条**（Dart 折算层 10 + Dart 屏 8 + Python 投影臂 9，另有位置契约臂 17→20）；
> 屏侧第一条钉的是"**两侧吃的是同一串字**"——把 Dart 手搓的 `/g3cont` 三章与 flet 表里每章
> `offsets` 逐列比，并当场钉住 `text_length` 那**已知口径差**（flet 是 `sum(len(段))`、
> Dart 的尺每段间多一个 `"\n"`，差 n−1 格），对不上则后面全在测空气。
> 仪器两条：`_driveToBottom` 必须**一屏一屏跳**（`ListView.builder` 的 `maxScrollExtent`
> 是按已建行均值估的，一次到位时通知里的 distance 是几百像素，追加根本不触发）；
> 迟滞那一臂的**前提**（distance < 180）与**结论**（没接第二遍）要当场各读一次，否则那一格是自证。
> `_triggerBand()` 写死 180 而不 import 那个私有常量。
> **本轮按"过程中不跑测试、迁移完成后统一验证"的授权执行**：全库通过数、`flutter test` 两档、
> 这 30 条守卫的变异核验、以及 D2 剩下的实弹臂（`/g3cont` 上真网关真引擎的三头读数）
> 全部**欠到批量验证那一趟**，这里不抄上一轮的值也不写估计值。静态面当场取到：
> `dart analyze test lib` → 10 issues found，0 error / 0 warning，且那 10 条全是既有 info、
> 本轮新文件一条都没有。
>
> **G3-D3 前半交（服务面，2026-10-10，第九十三轮，`docs/dev-logs/2026-10-10.md`）**：换源那一轮
> 联网扫描从界面回调搬进协议面。动作表 44→**49**：`switch.seeds`（本地种子，不联网）、
> `switch.scan`（**`mode="job"`**，30s）、`switch.snapshot`（回放"扫到第几源"）、
> `switch.cancel`（`mode="sync"`，合作式叫停——置一个服务端状态位而不是 `task.cancel()`，
> 取消会把已扫出的候选一起扔了）、`switch.apply`（按 url 取回候选那条源再写回）。
> 新增两条事件（反射出来的事件面现在是 **24 类**）：`SourceSwitchProgressEvent`（只带计数，进
> `COALESCED_EVENTS`）与 `SourceSwitchFinishedEvent`（整份排序候选，**不进**合流——
> 一条就是一行结果，合掉就是丢数据，守卫两头都钉）。关联沿用 `search.start` 那办法：
> `switch_id` 由客户端生成、服务端原样回带，因为 `_start_job` 的凭据只有 `{started, action}`。
> **扫描排队规则全库只剩一份**：`_site_token` 与那套过滤/排序从 `BookshelfController`
> 搬进 `ReadingService.order_switch_sources`（判据复用 `searchable_sources`，不另写一套），
> 控制器只交"从哪儿拿源"与"当前是哪条源"；**分组不存在时不回退到全部**——
> 那等于打了个不存在的组名就整个库联网扫一遍。`switch_scan` 一轮只跑一条（第二轮起时
> 先把在跑那轮叫停并等它收口），三种收尾（扫完/被叫停/起扫前出错）**都发** finished。
> 语料新档 `/g3sw` 六条源、**四页齐全**（搜索→书籍→目录→正文），章数按源互异；
> 该不该扫那一格由语料侧 `/hits.json` 的**前后差量**钉（d 停用、e 无 `searchUrl`、x 是当前源，
> 三条 delta 必须是 0，而同形状还在线上的 a/b/c 必须非 0）。黄金样本重抓到 **61 份**
> （48 主动作 + 13 变体），本轮五发各有样本，`switch.scan` 一份同时带
> `event_sample`（整份候选 3 行）与 `progress_sample`；进度面新开一支守卫
> `tests/test_gateway_job_progress_corpus.py`（**5 条 / 274 行**）——它"哪条 job 该有进度面"
> **从现场函数体推**而不是抄一张表，并点名要求 `switch.scan` 必须在推导结果里。
> 仪器三条规矩本轮各加一条（见日志 §5/§6）：路由**按段名指名**（`startswith("book/")` 那种
> 前缀判法让书籍页掉成搜索页，目录于是塌成 1 章；写成"其余都算搜索"又让 `/toc` 永远只剩一行）、
> 计数取数**不许用 `or`**（`int(p.get("failures") or -1)` 在真是 0 时也读出 −1，
> 那条"回环上不该有失败"永远红，且红的形状与"事件里没这个键"一模一样）、
> 样本落盘按**字节**判行列并重试行锁。
> **本轮按同一授权执行**：Python 全库、这 18 条新守卫（服务面 9 + 控制器委托 3 +
> 合流判据 1 + 进度面语料 5）的变异核验全部**欠到批量验证那一趟**，这里只有静态读数与
> 抓取当场从真进程取到的数（种子 2 行 / 本轮扫 3/3 源 / 候选 3 行 / 写回后目录 2 章）。
> Dart 那半（仓库面 5 发、两个模型、换源屏、widget/golden）在**同一门的第二次提交**，
> 五张具名欠据已写进 `_UNWIRED_PENDING_GATE`，划掉的判据是 `core/rpc/` 里出现这五个字面量。
>
> **G3-D3 后半交（Dart，2026-10-10，第九十四轮，`docs/dev-logs/2026-10-10.md` §「第九十四轮 下」）**：
> 判据当场复核——`_dart_action_calls()` 扫到 **49** 个 action、`never_called` 是**空**，
> `_UNWIRED_PENDING_GATE` 从 5 条回到 **0 条**（这张表自 G2-E 立起来第一次是空的，判据照跑）。
> 落的是 `source_switch_models.dart`（311 行，一半模型一半文案——文案收在一处是因为 flet 那一屏
> 状态/进度/候选共用一个 `_status` 槽，两侧都只留一个槽才比得出句子）、
> `source_switch_notifier.dart`（339 行）、`source_switch_dialog.dart`（207 行）、
> `source_switch_screen_test.dart`（502 行 / 18 条 widget），加仓库面 5 条与黄金契约 5 条；
> `_feeds` 绑定表 25→**30** 格。仓库面钉的是两处形状规矩：`switch.scan` 的回执只有
> `{started, action}`（不许顺手把 `data` 当候选解），`switch.cancel` 回的是**对象里那一格布尔**
> （缺它就抛，因为 `applied`/`cancelled` 这类布尔已经各背着两三种成因）；
> `switch.apply` 本轮改成硬要求 `applied` 是布尔。模型里最要紧的一格是
> `SwitchRun.fromFinishedEvent` **故意不读** done/total/failures/starved——收尾帧带的是结论，
> 读成 0 就会把跑完的一轮在界面上写成"扫了 0 个源"。
> 两条与 flet **有意不同**都写明在文件头：种子那一发失败时除了同一句文案还往 `failures` 推一条
> （"本地已有 0 个结果"与"这一发没打通"在界面上不能同形），写回成功后不自动 pop
> （句子留在对话框自己的状态格里，少一次 pop 就少一条"pop 与在途事件谁先到"的竞态）。
> 本轮最贵的一格是**协议面缺的那一个判据**：flet 收本地书换源入口靠的是控制器注入的
> `record["local"]`，而那一格从来不过协议面。选了"把服务端 `is_local_source_url` 的那一个
> 字面量钉进 Dart（`kLocalBookSourceUrl` + 精确相等）并让跨语言守卫钉住它"，
> 否掉了启发式 `startsWith('file')`、否掉了为一条 UI 规则改契约重抓 61 份样本、
> 也否掉了默默复刻一份谓词——**要么上钉常量，要么上钉服务端，不许界面自己发明判据**。
> 新守卫两支：`test_source_switch_wording_is_read_from_flet_into_dart`（用词表由 flet 的 AST 给：
> 对话框整份模块 + 控制器那五个函数，切出 **23** 个语词，去掉两句 logger-only 是 **21** 个，
> 逐个要在 Dart 的字面量里出现；排除表具名且当场反查它还在不在，死配置也红）与
> `test_local_book_source_url_is_one_constant_two_places`。
> 顺手挖出一条仪器债：`_py_source` 改读 **utf-8-sig**——`app/` 底下当场数过 **8 份**文件带 BOM
> （包括本轮要读的这份换源对话框），按 utf-8 读会把 U+FEFF 留给 `ast.parse`，
> 报出来的 SyntaxError 长得像"flet 源坏了"。内存变异六发（`.probe/g3d3_mutation_inprocess.py`，
> 退出码 0）：基线 2 绿 + M1–M6 全部响在该触的那一句上；M3 读到 15 个语词、跌破 18 的底价。
> **同一授权执行**：本轮没有套件读数，这一轮 2 支守卫的文件级变异核验与上一轮欠着的 18 发
> 一起欠在批量验证那一趟。
>
> **G3-D4 交（`/media` 键空间对齐，2026-10-10，第九十五轮，`docs/dev-logs/2026-10-10.md` §「第九十五轮 收」）**：
> §4.4 那一条"二进制不过协议面 + 回环 `GET /media/{key}`"从此有第二条形状。设计里那句
> **"不新造一套键空间"** 这轮落成了可执行的形式——正文插图的 key 就是缓存自己写在盘上的
> 那两个名字加一个下标：`<sha256(源,书)>-<sha256(章)>-<块下标>`，语法与两个出口
> （`is_media_key`/`media_bytes`）都住在 `app/services/reading_cache.py`，网关只问不猜
> （守卫扫 `app/**` 找 `[0-9a-f]{64}`，出现第二处就红；`media_key` 里出现 `hashlib` 也红）。
> 三条判断记下来：① 用 `-` 不用 `/`，让 `.` 与 `/` 在这套语法里**表达不出来**，
> 目录穿越不是"被拦下"而是"拼不出"；② 不另造短 id（映射表=第二份真相，三次哈希=盘上还认、
> 地址已死），键与文件同生同灭，四种"取不到"全在读同一个文件时数出来；
> ③ **地址即凭据，客户端不许手拼**——上一轮那条"要么上钉常量、要么上钉服务端"的阅读面版本，
> 协议面新增 `mediaKey` 一格、恒在（每行 7 键，白名单形状那两个方向才咬得住），
> Dart 的 `Uri` 只贴不拼，令牌走 query（`Image.network` 接不了自定义头），
> 代价写明在方法注释里：那一串每个字符都可渲染，不许进日志、不许当 widget key。
> 两条仪器结论值得进规矩：**`schema_digest` 不含出参内容**——加一整格 `mediaKey` 之后摘要
> 仍是 `879d1fbcfdf0`，黄金样本那一族的时效守卫对这一格是瞎的，所以语料锚从六笔涨到**七笔**
> （第 7 笔：样本里那格必须还能由 `ReadingCache.media_key` 从样本自己的载荷现算出来）；
> 下标当选择器的隐式前提（`display_blocks` 是 1:1）从注释升成守卫，因为哪天"空段丢弃"
> 造成的不是 404 而是**张冠李戴地画出另一张图，两侧都绿**。
> 内存臂 38 发（`.probe/g3d4_media_key_roundtrip.py`，退出码 0）+ 网关那四发逐条点验 +
> 变异 7 发全红（含 2 发还原后的正对照）；61 份黄金样本**重抓**（`reader.content`
> bytes 17834→18007、digest 不变）。**点名一笔债**：没做缓存格式迁移（`VERSION 2`/兄弟文件），
> 按需读的代价当场量得——一页 30 张图 → 章 JSON **786 KB**、取一张中位 **1.47 ms**、
> 整页 **44.4 ms**、底价（一次读完再解）**1.43 ms**，即每张图片摊一次 JSON parse；
> 文字书够用，**G4 漫画必须换形状**。G3 的 R1 那一半到此交完。
>
> **G4-B 前半 交（订阅面的服务面 + 协议面 + 语料 + 样本，2026-10-10，第九十六轮，
> `docs/dev-logs/2026-10-10.md` §「第九十六轮 开」）**：§4 那张域表里 `rss.*` 那一行从此有表
> ——动作 **49→58**，九发各有入参 schema（八发 RPC + `rss.refresh_all` 走 `mode="job"`，
> `rss.refresh` 给 60s 因为本轮实测"取不到的那一条"那一发是 7669.6 ms）。三条判断记下来：
> ① **去标签那把尺搬进服务侧**（`rss_service.plain_text`，控制器改成 import 它）——
> §4.2「不许界面自己发明判据」在显示口径上的版本：库里存 HTML 原文，`rss.article` 回的
> `text` 是现剥的那一份，flet 与 Dart 读同一个函数；`rss.articles` 那 9 格投影连 `content`
> 带 `text` 都不许有，正文按行算是几十倍载荷。② **进度帧的 `failures` 只在收尾那一帧**、
> `total` 开头定下中途不变、`refresh_id` 由客户端生成（job 的受理回包只有 `{started, action}`，
> 迟到帧只能靠它认本轮）；发布点必须写在**被派发方法自己的函数体**里，否则进度面那张
> 从函数体推导的表（`NAMED_FACES` 从一条涨到两条：`switch.scan` / `rss.refresh_all`）
> 看不见它。③ **语料里"取不到"用的是回环上没人监听的 8799，不是 HTTP 500**——
> `_request_transport` 对非 2xx 照样回 body、`block_reason` 只认已知标记词，
> 于是 5xx 在协议面上和"这篇 feed 是空的"同形（`ok:true, data:0`），而这正是这一族要防的
> ambiguity；连接层失败才真的抛 `RequestConnectionError`。黄金样本 **73 份**（61 重盖章 +
> 12 新）共用 digest `39b699f1d66d`——新增九发把旧章全冲掉，所以**必须一条链跑完**。
> 语料 `tests/gateway/fake_rss_feed.py`（133 行，入库）给三种形状：RSS 带标签 / RSS 纯文本 /
> Atom **整列空 author**。九发各挂一张具名欠据（`_UNWIRED_PENDING_GATE` 0→9），
> **后半 = Dart 那一屏**（模型 + 仓库面 + notifier + 宽窄两形 + 跨语言文案守卫），
> 划掉的判据仍是 `core/rpc/` 里出现这九个字面量。本轮按授权不跑套件：内存臂
> （3 支静态 + 4 发臂 + 全部正对照，退出码 0）与 14 发样本/进度守卫只算"仪器接上了"。
>
> **G4-B 后半 交（订阅面的 Dart 屏 + 投影表守卫 + 双向文案守卫，2026-10-10，第九十七轮，
> `docs/dev-logs/2026-10-10.md` §「第九十七轮 收」）**：五块落地（模型 448 行、通知器 527 行、
> 屏 658 行、两张测试面 747+369 行）；四张 flet 投影表各有自己的 builder 重跑函数
>（`tests/test_rss_projection_tables.py` 230 行新建，底价行数 11/13/9/6、渲染器点名 1/1/2/5、
> 恒真变异三发全红、语料证人三支）；时区前置条件 `_when` 的 `>1e9` 分支把表绑在生成机
>（`generated.local_utc_offset_minutes=480`、`probe` 点名脚本、Dart `_requireSameTimeZone`
> 硬 `StateError`）；REAL 列那格 `1700.0` 保真（不是 `1700`）；`jump_to_unreachable`
> 两侧输入不同形（flet 空选中→置灰 vs Dart 自动落首行→可跳），所以比的是「原地不动」；
> 界面面四条参照 key 补位（`rss.level.<名>.tip` / `rss.shape.flex.<名>`，因为这一版 Flutter
> 的 `find.ancestor` 没有 `skipOffstage`，而宽窗切换条是 Offstage 且 pane 外还套一条 Expanded）；
> 详情屏收藏按钮 tooltip 照搬 flet 不改（单独挂号）；跨语言文案守卫两个方向分开钉
>（正向 73 语词排除 6 → 67 保留，反向 dart_runs=69 dart_only=2 钉住）；欠据表 9→0，
> `action_table=58 call_sites=58 pending=[]`；自测 9 支 PASS + 6 发变异全红（行为级）。
>
> **G4-C 交（漫画媒体换形状，2026-10-10，第九十八轮，`docs/dev-logs/2026-10-10.md` §「第九十八轮 收」）**：
> `ReadingCache` VERSION 1→2，字节块落盘成兄弟文件（`.bin`），`media_bytes` 走 `Path.read_bytes`
> 不再 parse JSON；旧缓存兼容（先找 .bin，没有就回退到 JSON 里 base64）；协议面 key 空间与
> `is_media_key` 语法不动，网关那一路不受影响；七笔账全对上（30/30 正对照字节一字不差），
> 性能读数显示新形状避免了 JSON parse + base64 decode，真正收益在内存压力与架构清晰。

---

## 10. 测试策略

- **契约**：每个 action 一份黄金样本（请求/schema/响应三元组，`tests/gateway/golden/*.json`），
  两侧各自比对；schema 改了没同步 → 红。按记忆 `guard-tests-assert-absence` 写"没发生"断言
  （例如"响应里出现了 `app.` 实例"、"某个 action 没有对应服务方法"），别只断言"成功返回"。
  G1 另外立了 `tests/gateway/projection/*.json`：那一摞不是信封样本而是**flet 那屏自己算出来的
  参照表**（书名/副标题/状态行的逐行文本），它没有请求也没有响应，混进 `golden/` 只会逼
  守卫为它开一条特例。
- **Python 面**：现有回归（2026-10-10 第九十一轮后实测 `2014 passed, 161 subtests passed`）基本不动；
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
- **实弹臂**：现在落在 `flutter_client/test/*_live_arm_test.dart`（不是 `integration_test/`，
  因为它要的只是"真子进程 + 真 RPC"，不要设备），靠 `LEGODO_PYTHON`/`LEGODO_MAIN` 门控，
  没给就 `markTestSkipped`。两条硬规矩：引擎子进程一律用**空临时数据目录**起（真库只被 `mode=ro`
  读过，一行不写脏），假源走 `tests/gateway/fake_source_reader.py`（**仓库里那份语料**，
  不是 `.probe/` 里被忽略的那一份）——它的 `PORT` 行给内核真值、`/g3/source.json` 让客户端现取建源字典，
  所以参照表与语料同源、Dart 侧一个字都不用自己编。
  反向通道那一族另有三条仪器规矩（第九十一轮）：
  ① **内核在场是这条族的前置条件，不是环境噪音**——`main.py:_engine_browser_fetchers` 在
  `browser.available_fetcher() is None` 时返回 `(None, None)`，webView 选项的页会退回纯 HTTP，
  于是"宿主没被叫到"与"通道断了"**同形**。所以臂里先读 `chromium_installed()`/`browsers_path()`
  当先决读数、缺了就 `markTestSkipped` 并**把原因说出来**，绝不许静默通过。
  ② **收尾必须先 `engine.shutdown` 再 SIGTERM**：只 SIGTERM 网关会留下一棵孤儿 headless chromium
  抱着临时数据目录（删目录报 `errno 32`）；而且就算优雅收尾完了，**目录句柄的释放比进程退出晚一拍**，
  所以删除要短时间重试（本轮实测：两臂都在第 2 次尝试删净）。
  ③ **`flutter test --plain-name` 一条都没选中时也退 0**（pytest 的 `-k` 退 5），所以变异判据必须读
  "退出码 + 选中条数"两样，只看退出码会把"仪器没接上"记成"守卫很硬"。
- **调用点扫描的口径**：判据问的是"客户端在不在调这一发"，所以只认**长在调用位上的**字面量
  （`call('a.b')` 那一族）；`Key('a.b')`、`heroTag: 'a.b'`、预算表的 `'a.b': 60`、
  `static const String k = 'a.b'` 都是同形状的邻居不是调用点（第八十九轮就是这么把口径从
  "认字面量排邻居"改过来的）。代价：`call(kAction)` 那种提成常量的调用点会看不见并报红，有意如此。
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
  Dart 四屏、R4 三项复量，以及那个把 1.7 倍改判成 1.39 倍的出口投影；
  第八十六~八十九轮：**G3 的落点那一半**——位置契约这把尺、`reader.*` 13 发靶子、
  flet 三张投影表、Dart 的 reader 仓库面与阅读屏、三头并排的实弹读数）
- `docs/dev-logs/2026-10-10.md`（第九十一轮：**G3-D1 反向 WebView 通道**——`/ws` 的 `request` 帧
  在 Dart 侧有模型与宿主接缝、`engine.webContentReady` 有真调用点、语料 `/g3web` 两形状与取页账、
  两个宿主语言各自答回同一章（4 段 / 1961 字）、报错归回宿主那 20s、18 发变异全红、
  以及留给 G4 的 WebView 插件选型清单；
  第九十二轮：**G3-D2 连续阅读（离线面）**——append 下一章的迟滞/段起点折算/本章口径落库、
  屏侧 GlobalKey 改整篇段号、30 条新守卫，以及"按授权不跑测试、通过数与变异核验欠到批量验证"这一条；
  第九十三轮：**G3-D3 前半（服务面）**——`switch.*` 五发上表（49 条动作）、换源扫描走 `mode="job"`
  与两条新事件、排队规则收进服务侧一份、`/g3sw` 六源四页语料与 `/hits.json` 前后差量账、
  61 份黄金样本重抓、进度面那支从现场函数体推导的守卫；
  第九十四轮：**G3-D3 后半（Dart）**——仓库面五发与换源屏落地、欠据清零（49/49 有调用点）、
  语词表守卫（flet 的 AST 给 21 个语词）与"协议面缺的那一格：要么上钉常量要么上钉服务端"，
  以及 `_py_source` 那一条 BOM 仪器债；
  第九十五轮：**G3-D4（`/media` 键空间对齐）**——正文插图的第二种 key（`书摘要-章摘要-块下标`，
  语法住在 `reading_cache.py`）、协议面新增恒在的 `mediaKey` 一格、网关两种形状一套 Range、
  Dart 的 `mediaUri`（令牌走 query，代价写明）、语料锚从六笔涨到七笔（因为 `schema_digest`
  不含出参内容）、下标 1:1 那条隐式前提升成守卫，以及按需读那笔点名的债（786 KB/1.47 ms/44.4 ms）；
  第九十六轮：**G4-B 前半（订阅面上协议面）**——`rss.*` 九发上表（**58** 条动作）与九份 schema、
  去标签那把尺从控制器搬进服务侧（`rss.article` 回 `text`、列表九格连 `content` 都不带）、
  进度帧的 `failures` 只在末帧 + `refresh_id` 客户端生成、语料三形状（RSS 带标签 / 纯文本 /
  Atom 整列空 author）与"取不到"为什么用连接被拒而不是 500、73 份样本一条链共用
  `39b699f1d66d`、进度面那张推导表多点名一条（发布点必须在被派发方法体内），
  以及三处仪器坑（对源码 substring 判回包键、直接调 `TestCase` 实例把失败吞进 `TestResult`、
  合流窗口里把中间帧当末帧））；
  第九十七轮：**G4-B 后半（订阅面 Dart 屏 + 投影表守卫 + 双向文案守卫）**——五块落地
  （模型 448 / 通知器 527 / 屏 658 / 两张测试 747+369），四张 flet 投影表各有自己的 builder
  重跑函数（底价行数 11/13/9/6、渲染器点名 1/1/2/5、恒真变异三发全红、语料证人三支），
  时区前置条件 `_when` 的 `>1e9` 分支把表绑在生成机（`generated.local_utc_offset_minutes=480`、
  `probe` 点名脚本、Dart `_requireSameTimeZone` 硬 `StateError`），REAL 列那格 `1700.0` 保真，
  `jump_to_unreachable` 两侧输入不同形所以比的是「原地不动」，界面面四条参照 key 补位
  （`rss.level.<名>.tip` / `rss.shape.flex.<名>`，因为这一版 Flutter 的 `find.ancestor` 没有
  `skipOffstage`），详情屏收藏按钮 tooltip 照搬 flet 不改（单独挂号），跨语言文案守卫两个方向
  分开钉（正向 73 语词排除 6 → 67 保留，反向 dart_runs=69 dart_only=2 钉住），欠据表 9→0，
  `action_table=58 call_sites=58 pending=[]`，自测 9 支 PASS + 6 发变异全红（行为级）；
  第九十八轮：**G4-C（漫画媒体换形状）**——`ReadingCache` VERSION 1→2，字节块落盘成兄弟文件
  （`.bin`），`media_bytes` 走 `Path.read_bytes` 不再 parse JSON；旧缓存兼容（先找 .bin，没有就
  回退到 JSON 里 base64）；协议面 key 空间与 `is_media_key` 语法不动，网关那一路不受影响；
  七笔账全对上（30/30 正对照字节一字不差），性能读数显示新形状避免了 JSON parse + base64 decode，
  真正收益在内存压力与架构清晰）
- `docs/M7打包说明.md`（PyInstaller 命令与 flet* hidden-import 清单位置）
- 参照仓库：`github.com/jerunshi/guoapp1`（浅克隆 `D:/tmp/guoapp1`，不在本项目版本控制内）
- 记忆：`tts-pyttsx3-com-same-thread`、`quickjs-native-crash-risk`、`engine-hub-loop-split`、
  `ui-progress-must-travel-as-events`、`refactor-regression-orphan-methods`、
  `guard-tests-assert-absence`、`ab-bench-must-interleave`、`offline-loopback-probe`、
  `legado-analyzer-empirical-semantics`、`navigation-rail-old-architecture-shape`、
  `measurement-corpus-must-not-be-self-proving`、`probe-stand-in-must-not-be-the-measured-workload`、
  `flet-desktop-probe-instrumentation`、`flet-0-86-narrow-window-sizing-contract`
