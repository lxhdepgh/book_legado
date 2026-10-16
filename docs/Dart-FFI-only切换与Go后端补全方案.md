# Dart 前端 FFI-only 切换与 Go 后端补全方案

> 2026-10-15。本稿全部数字均为当场实测（analyze / grep / 文件清点），列出的行号以当前工作区为准。
> 参考实现：`legodo_dart/app/`（Python，本轮恢复的完整功能版）。

---

## 1. 实测前提

### 1.1 WS 链路已结构性死亡（四证）

| # | 证据 | 位置 |
|---|------|------|
| 1 | Dart 侧等 `LEGODO_GATEWAY ` 握手行（60s 超时），Go 侧 `main.go` 只打印「Legodo Go 后端已启动，监听 :8080」，从不打印握手行 | `lib/core/rpc/engine_process.dart` vs `backend_go/cmd/server/main.go` |
| 2 | Dart 事件流连 `/ws`，Go 服务器只挂 `/rpc`、`/rpc/ws`、`/health`，没有 `/ws` | `lib/core/events/engine_event_client.dart` vs `internal/gateway/gateway.go` |
| 3 | Go WS 网关 77 条注册表只实现 14 条，其余回 `unimplemented` | `internal/gateway/gateway.go` |
| 4 | `build_release.py` 不构建/不拷贝 `legodo_server.exe`；分发目录只有 `legodo_client.exe` + `legodo_engine.dll` | `dist/legodo_windows/` |

两条路里 **Path A（FFI）是唯一在跑的**：`lib/app/bootstrap.dart` 先试 `FFIEngineAdapter().connect()`，失败才落到 WS/进程路径。

### 1.2 FFI 路由盘点（Go `pkg/ffi/engine.go`）

- 路由表：174 个 case 标签（含点号/下划线双别名），约 60 个真实处理器，其余为 stub。
- Dart 适配器实际调用 66 个 action；**缺 2 个别名**：`engine_shutdown`（只有 `engine.shutdown`，engine.go:173）、`engine_web_content_ready`（只有 `engine.webContentReady`，engine.go:325）。
- **事件通道为零**：`route()` 没有任何事件出口；`core.EventBus` 存在且服务里已有 `Publish`（bookshelf/check/rss 三处），但没有任何订阅者把事件送出来。
- 事件名两套写法：Go `EventType()` 返回蛇形（`search_finished`，core/events.go:66），Dart/事件契约要求 Python 类名（`SearchFinishedEvent`，`lib/core/events/event_names.dart`）。Python 侧 wire 名 = `cls.__name__`（app/services/gateway/service.py:616-619）。

### 1.3 关键 action 的真实成色（read 过源码）

| action | 现状 | 位置 |
|--------|------|------|
| `search_start` | 同步循环扫完所有源、30s 死等、结果**收集后丢弃**、不发任何事件、不认 `search_id`/分页/停止 | engine.go:1263-1303 |
| `search_stop` | 直接 `okResult(true)` | engine.go:224-225 |
| `switch_scan` / `switch_snapshot` | 纯 stub（只回 `started/action`） | engine.go:1369-1374 起 |
| `switch_cancel` | **误调 CheckService.Cancel()**（复制粘贴错） | engine.go:236-240 |
| `switch_seeds` | 拿书名字串 contains 扫书架，不是搜索种子库 | engine.go:1325-1367 |
| `reader_staged*` | 三个空 stub，形状也不对（Dart 等 `StagedRecord?`，Go 回 `[]`） | engine.go:260-265 |
| check | `Start()` 里「TODO: actual HTTP request」，扫完**只发一条**汇总事件；`state_class` 写的是枚举串 `StateAvailable`；缺 `source_id`/`summary` 键 | internal/services/check_service.go:95-131 |
| `rss_refresh_all` | goroutine 起 `RefreshAll`，内部 **`time.Sleep(30s)` 固定等**；不发进度事件；`RssRefreshedEvent` 的 `SourceID=源名`、`NewCount` 恒 0 | rss_service.go:233-260, 109 |
| tools 域（font/webdav/export/browser/qr/webbridge/replace 变更） | stub `okResult` | engine.go:311-411 区间 |

### 1.4 Dart 侧票据

- `FFIEngineAdapter.events()` 直接抛「不支持事件流」（ffi_engine_adapter.dart:188-191）——而 7 条屏（找书/检测/换源/订阅/书源/仪表盘）全部通过 `listenToEngineEvents` 订阅 `engineEventsProvider`。
- `ffi_engine_adapter.dart` 12 个编译错误：`_toStrings(data['...'])` 把 `Object?` 传给 `List<Object?>?`（356-363、378×2、388×2）——`_toStrings` 参数改 `Object?` 一处即修。
- 死文件两个，**0 个 importer**：`websocket_engine_adapter.dart`（9 个 error）、`websocket_rpc_client.dart`（1 个 error）。

### 1.5 测试与杂物

- Go：`*_test.go` **0 个**；`backend_go/_test_goos.go` 是 3 行垃圾文件（`package main; fmt.Println("OK")`）。
- Dart：29 条 widget 测试红（待阶段 3）。
- 工作区有开发者未提交的 813 个 staged deletion，**全程不碰**（docs/git-workflow.md §3）。

### 1.6 参考实现（端口清单来源，legodo_dart/app/）

| 源 | 内容 | 端口目标 |
|----|------|----------|
| `gateway/methods.py` + `gateway/schema/*.json`（89 个） | action 总表：mode=call/job/sync | Go 路由语义与注册表 |
| `core/events.py`（24 个事件类） | 字段即 payload 键 | Go `core/events.go` 补缺+对齐 |
| `services/search_model.py`（298 行） | SearchModel：`search_id` 同号翻页/异号取消重开、generation、MAX_THREAD=32、每源 30s 超时、merge 去重、种子落库 | P2 新文件 `search_model.go` |
| `services/reading/reading_service.py`（1715 行） | `search_books` 955、`_publish_search_partial` 902、`searchable_sources` 910、`scope_table` 939、`search_in_scope` 1024、`search_scopes` 1041、`switch_seeds` 1088、`switch_source` 1104（SWITCH_CONCURRENCY=8、SWITCH_TIMEOUT=10s、0.5s 节流、starved 判定 114-134）、`rank_switch_matches` 1208、`switch_scan` 1251（合作式停止、run 快照、三种收尾都发 finished） | P2/P3 |
| `services/check_service.py`、`services/content/rss_service.py` | 检测/订阅的事件节奏（逐源 CheckFinishedEvent；RefreshAll 进度帧） | P4/P5 |
| `services/gateway/service.py:61-77, 603-629` | COALESCED_EVENTS（8 条，含进度族）、SNAPSHOT_EVENTS（ReadAloud/MediaPlay）、wire 名=类名 | P1 事件通道 |

---

## 2. 目标架构（FFI-only）

```
legodo_client.exe (Flutter)
   │ dart:ffi
   ▼
legodo_engine.dll (Go)
   Call(action, paramsJSON) → {"ok":true,"data":...,"error":...,"v":1}
   ├─ 同步 action：受理即回 / 直接回数据
   ├─ job action（search.start / switch.scan / rss.refresh_all / check.start）：
   │    受理回 {started, action}，进度与收尾走事件
   └─ engine_poll {since:N} → {events:[{seq,name,payload}], last_seq, lost}
         ▲ 环形缓冲（capacity=4096）订阅 EventBus 全部事件
Dart: FFIEngineAdapter.events() = Timer.periodic(250ms) 轮询 engine_poll
      游标推进；断流 addError；名字直接透传（已是类名）
```

设计要点（对照 Python 语义，不新造）：

1. **事件名即 Python 类名**（`SearchFinishedEvent` 等 7 条是 Dart 有屏在听的；其余照发不拦）。Go 侧在入环前把 `EventType()` 蛇形映射成类名（映射表放 core，一处定义）。
2. **job 语义不变**：受理回 `{started, action}`；结果只走事件（Dart 侧 find_books/check/switch/rss 四个 notifier 已按此写死）。
3. **关联 id 原样回带**：`search_id`/`switch_id`/`refresh_id` 由客户端生成、服务端原样进事件——界面靠它丢弃上一轮迟到帧（event_names.dart:26-33 已点名）。
4. **轮询窗口天然批量化**：Python 的 0.5s 合流在 FFI 下由 250ms 轮询窗口承担，Dart notifier 自己还有防抖窗口，第一波不做环内合流。
5. **ring 溢出**：`since < 最老 seq` 时从最老返回并带 `lost:true`（宁可丢旧不可断流）。

---

## 3. 工作分解与验收判据

### P0 清障（小）
- Go：route 补 `engine_shutdown`、`engine_web_content_ready` 两别名；删 `backend_go/_test_goos.go`。
- Dart：`_toStrings` 参数改 `Object?`（12 个 error 清零）；删两个零 importer 死文件。
- 判据：`go vet ./...`=0、`go build`=0；`dart analyze lib/core/rpc` 0 error。

### P1 事件通道（Go ring + Dart 流）
- Go `core`：`EventBus.SubscribeAll`；`EventRing`（4096、seq 单调、lost 标记、wire 名映射表）；`Initialize` 时订阅。
- Go route：加 `engine_poll`（点号/下划线双别名）。
- Dart：`FFIEngineAdapter.events()` 实现为轮询流（游标、250ms、错误 addError 且继续轮询；断开后重连自愈）。
- 判据：Go 单测——订阅→Publish→poll 读到且 seq 单调、类名正确、溢出置 lost；Dart 单测——帧解码纯函数（新增 `framesToEvents`）逐形状断言。

### P2 search 作业（最大一项）
- 新 `internal/services/search_model.go`：照 `search_model.py` 语义（generation/page/search_id、`min(thread_count,32,len)` 并发、每源 30s 超时、merge 去重、种子落 `search_books` 表——Go 表已存在 core/storage.go:301-313，列与 Python 略有出入需适配）。
- 重写 `doSearchStart`：起作业即回；逐源发 `SearchFinishedEvent{search_id, items, finished:false}`，收口发 `finished:true`；`search_stop` 走协作停止（generation 自增）。
- `search_scopes` 补范围语义（`searchable_sources`/`scope_table`：`__all__`/`__ungrouped__` 哨兵、`bookSourceGroup` 切分丢空）。
- 判据：Go 单测（假源并发计数=min(32,n)、同号翻页 page+1、异号取消旧轮、事件序列 partial×n+final×1、stop 后无新帧）。

### P3 switch 引擎
- 新文件（或并入 reading service）：`order_switch_sources`（可搜过滤+排己+同站优先）、`rank_switch_matches`、`switch_scan`（合作式停止、run 快照、三种收尾都发 finished、CW=8/T=10s/0.5s 节流/starved 单列）、`switch_cancel`（改指自己的 run，别再点 CheckService）、`switch_snapshot`、`switch_seeds`（搜索种子库 list_by_name_author）。
- 判据：Go 单测（progress/finished 事件帧键=switch_id/done/total/failures/starved 与 switch_id/candidates/cancelled/error；**快照键名 snake_case**（`switch_id`），见下方修订；cancel 后 done<total；种子返回同名同作者）。
- **修订（实现中实测更正）**：原判据写"快照 camelCase 键 `switchId` 照 Dart adapter:513-516"是错的。golden（`tests/gateway/golden/switch.snapshot.json` 等，真 Python 子进程捕获）、`serialize.py`（纯透传）、以及 Dart 侧 `SwitchRun.fromJson`/`SwitchCandidate.fromMap`/`BookRow.fromJson` 读的都是 snake_case；adapter 那几段手拼 camelCase 的解析器（`switchSnapshot`/`switchSeeds`/`applySwitch`）才是要改的离群点，已一并修正。`switch_id` 是**字符串**（Python `sid = str(switch_id or 0)`）。

### P4 check 对齐
- `check_service` 补真实浅检（走 rule engine 对 searchUrl 发请求）+ 逐源 `CheckFinishedEvent{source_id, state_class, summary, total}`；`state_class` 用可用/失效的类名而非枚举串；`check_start` 受理即回、`check_snapshot` 键对齐 Dart `_parseCheckRows`（sourceId/stateClass/summary 驼峰，adapter:945-949）。
- 判据：Go 单测逐源帧数=源数；坏源计入后 summary 非空。
- **修订（实现中实测更正）**：原判据写"`check_snapshot` 键对齐 Dart `_parseCheckRows`（sourceId/stateClass/summary 驼峰）"是错的。golden（`tests/gateway/golden/check.snapshot.json` / `check.source.json`，真 Python 子进程捕获）里逐源行键是 **snake_case**（`source_id`/`state_class`；报告 31 键含 `raw_url`/`status_code`/`response_time_ms`），`serialize.py` 纯透传；Dart 侧 `CheckSnapshot.fromJson`/`CheckRow.fromMap`/`CheckSourceReport.fromJson` 读的也是 snake_case，adapter 里手拼 camelCase 的 `_parseCheckRows`/`_parseCheckTiering`/`_parseCheckSummary` 与 checkSource 内联解析才是离群点，已删除并改走 `fromJson`。另：`check.source` 的 wire 入参键是 `source_json`（golden request + schema 同名），adapter 原发 `source` 已改；`depth` 由 Dart 显式传参（golden 里是 `'list'`）；`check.cancel` 的 data 是 `null`（golden），不是 `true`。逐源事件帧正好 6 个字段（`source_id/state_class/summary/done/total/timestamp`）。

### P5 rss 进度
- `RefreshAll` 真收口（WaitGroup 不等 sleep）+ `RssRefreshProgressEvent{refresh_id, done, total, current, failures}` 与 `RssRefreshedEvent{source_id, new_count}`；`new_count` 用真新增数。
- 判据：Go 单测——进度帧 done 递增、终帧 done=total、refreshed 的 new_count 非恒 0。
- **修订（实现中实测更正）**：两处：
  1. `RefreshAll` 由"WaitGroup 并发"改为**串行逐源循环**。golden `rss.refresh_all.json` 的 progress_sample（真 Python 捕获，3 源 5 帧）语义是**先发帧再抓源**：起始帧 `done=0,current=""`，第 i 帧 `done=i, current=第 i+1 个源的名字`，failure 只落在终帧——这个"current=即将开始的那个源"的口径本身就是串行序的产物；并发下帧序无法保证 `done` 递增。schema 亦写"串行逐源抓 feed"。真收口由"循环内无 sleep、逐帧真抓完再前进"满足。
  2. rss 全族 wire 原先与 golden 不符（Go 侧是自增整型源 id + stub 处理器），已整体对齐：源 id = **feed 地址字符串**（`rss.add_source` 回它、删除/刷新吃它，重复添加是更新）；行键 10/9/10（sources 带 join 出的 `total`/`unread`，articles 无正文，article 单取带已剥标签的 `text`）；`remove_source`/`mark_read`/`star` 回标量 bool、`refresh` 回标量 int（抓取失败=错误信封，源不存在=0）；`core/storage.go` 旧形状（`rss_type`/`text`/`created_at` 毫秒）经 `migrateRssTables()` 就地重建为新形状（url 主键、`source_group`、`content`、`updated_at` 秒浮点），索引移出建表串、在迁移之后建；adapter 那几段手拼 camelCase 解析全部改走权威 `fromJson`。

### P6 reader 形状核对
- `reader_staged*` 三 stub 按 Dart `StagedRecord?` 形状实现（试读暂存：去书架语义）；`reader_search_content` 与 `reader.position_*` 键逐一对 Dart 解析器核对（adapter:695-716、960-987）。
- 判据：Go 单测按 Dart 解析键断言；有缺口当场补。

### P7 拆除 WS 链
- Go：`cmd/server/`、`internal/gateway/`、`gateway/`（77 注册表）删除；`internal/services/event_bus.go` 重复 EventBus 一并清。
- Dart：删 `rpc_client.dart`、`rpc_engine_repository.dart`、`engine_process.dart`、`engine_event_client.dart`（+ P0 两个死文件）；`bootstrap.dart` 只留 FFI 路径 + 诊断屏。
- 测试：8 个引用 WS 的测试文件按 D2 处置。
- 判据：全仓 grep 无 `LEGODO_GATEWAY`/`/ws` 残留（除 docs 说明）；`dart analyze lib` 0 error。

### P8 测试与冒烟
- Go：P0-P6 单测落地 + `httptest` 假源 E2E（Initialize → `search_start` → poll 收到 `SearchFinishedEvent(finished:true)` 且 items 非空；换源轮同理）。
- Dart：阶段 3——29 条失败修复（含 reader_continuous 的 text_length 口径：353 vs 329，疑似段缩进 2 字符未计，需当场验证）。
- 端到端：`build_release.py` 出包 → 直接运行 exe → 找书/换源/检测/订阅各跑一轮，界面出结果。
- 判据：`go test ./...` 全绿；`flutter test` 全绿；出包 exe 冒烟通过。

---

## 4. 不做的事（边界）

- **readaloud.\* 7 个 action 不补**：Dart 适配器 0 调用、event_names 未登记（Flet 时代产物）。
- **tools 域不做真实现**：font/webdav/export/browser/qr/webbridge/replace 变更保持 stub（Dart 是占位页）。
- **不选 NativeCallable 回调**（见 D1）。
- **不搬 WS 的 snapshot/coalesce 全套语义**：轮询窗口承担批量化；SNAPSHOT_EVENTS 重放等有屏听 ReadAloud 再谈。
- **不 commit、不碰 813 个未提交删除**（git-workflow.md §3）。

---

## 5. 待拍板决策清单（附推荐）

**D1 事件通道机制**
- 推荐：**环形缓冲 + `engine_poll` 游标轮询**（250ms）。理由：与现有 `Call()` 单入口零冲突、Dart 侧一行 Timer、测试可离线造帧、无隔离区/线程跨界的原生风险。
- 备选：Go→Dart NativeCallable 回调。压后：跨 isolate/原生句柄的坑多，首波不值得。

**D2 拆除范围（本方案含"删"动作，需逐项授权）**
- 推荐：Dart 删 6 文件（rpc_client / rpc_engine_repository / engine_process / engine_event_client / websocket_engine_adapter / websocket_rpc_client）+ bootstrap 去 Path B；Go 删 cmd/server / internal/gateway / gateway / services/event_bus.go（重复件）；`_test_goos.go` 垃圾文件。
- 测试处置推荐：WS-only 的（rpc_client_test、r4_dart_rpc_baseline_test、bootstrap_test 中 WS 分支）删；live_engine_test / reader_live_arm_test / webview_reverse_channel_live_arm_test 改 FFI 活臂；engine_repository_test / webview_host_test 保留（repository 契约与假引擎不依赖 WS）。

**D3 首波端口范围**
- 推荐：P0-P5 + P7 + P8（即 7 条屏在听的事件族全通）。P6 只做"键核对+缺口补"，tools 域不动。
- 备选：再砍 P4/P5（检测/订阅先不上），先只通找书+换源。不推荐——那两屏现在也是双击就红的状态，一并修掉边际成本低。

---

## 6. 总验收（口径统一）

1. `go vet ./...` = 0，`go build` = 0，`go test ./...` 全绿（新增用例覆盖 P1-P6 判据）。
2. `dart analyze lib` 0 error；`flutter test` 全绿（含阶段 3 修复的 29 条）。
3. FFI 冒烟：假源 E2E——Initialize → search.start → poll 收 `SearchFinishedEvent`（类名、finished=true、items 可解析）。
4. 出包：单 exe + DLL 直接运行，四屏（找书/换源/检测/订阅）各出一轮真结果。
