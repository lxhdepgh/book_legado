# 003 分层重构把编排从视图搬进服务时，能力被"最小可用版"替换掉

- 记录日期：2026-09-25
- 状态：本轮已修三项；「试读退出询问」同日晚些复原（见 §解决 末条）；
  2026-09-26/27 又复发五型，见 §后续复发——第一、二型当日已接出并补齐回归测试；
  第三型（进度事件从来没写过却被当成有）到第十八轮把两条真缺口补完
  （`RepairProgressEvent` / `BackupProgressEvent` / `ExportProgressEvent`），
  §5.11 缺口表只剩"换源·检测跨页进度走快照轮询"这一条改法选型；
  第四型（同步重活占死循环，事件到了没人消费）第十八轮已修；
  第五型（动作接出来了但没人渲染那个字段）第十九轮已修
- 影响面：换源扫描、书架试读残留回收、搜索结果种子库、阅读器退出收尾、整本导出进度、书架封面

## 症状

界面全部能点开、单测全绿（当日 843；2026-09-27 全量复测 1215 passed + 28 子测试），
但功能比旧版**退化**了——退化不是崩溃，没有任何报错，
只有"变慢 / 变空 / 越攒越多"这类只有对照文档才看得出来的症状：

| 能力 | 重构后 | 症状 |
| --- | --- | --- |
| 换源扫描并发 + 每源超时 | 逐源 `for ... await` 串行，无超时 | 书源几十上百个时整轮像卡死，对话框进度条永远 0 |
| 换源进度反馈 | `SourceSwitchDialog.set_progress()` 无人调用 | 点了「开始换源」看不到任何在跑的迹象 |
| 试读残留启动清理 | `discard_unshelved_books()` 无调用方 | 未转正的试读记录永久留在 `books` 表，缓存也不清 |
| 搜索结果 24h 过期 | `SearchBookStore.clear_expired()` 无调用方 | `search_books` 表只增不减；`storage.py` 注释还写着"运行时按 24h 清理" |
| 试读退出询问入架 | 整个动作没有界面入口（`close_book()` 无人调用） | 用户偏好功能消失（见记忆 `findbook-ui-layout-preference`） |

## 根因

旧实现这些逻辑写在巨型 `ReadingView` 里（3000+ 行）。分层重构按"视图纯控件 / 控制器装配 /
服务编排"重写时，是**照着新结构重写业务**而不是搬运，于是并发、超时、节流、清理这些
"非功能性的功能"最先被省略——它们不在任何一次点击的成功路径上，测试也就没锚点。

## 解决

- `ReadingService.switch_source()`：`SWITCH_CONCURRENCY = 32` 的 `asyncio.Queue` worker 池 +
  每源 `asyncio.wait_for(SWITCH_TIMEOUT = 10s)` + `_switch_failure_reason` 分类计数 +
  `on_progress(completed, total, failures)`（`SWITCH_PUBLISH_INTERVAL = 0.5s` 节流）；
  控制器把回调接到 `dialog.set_progress()` 与状态行。
  注：连接阶段超时不在此处设——引擎是池化共享的（`RuleEngineService`），
  所以用 `wait_for` 在调用点兜总时长，而不是给一轮扫描新建短命引擎。
- `SourceService.on_start()`：`discard_unshelved_books()` + 按返回的 key 清 `ReadingCache`。
- `ReadingService.on_start()`：`clear_expired(now - SEARCH_STORE_TTL_MS)`（24h）。
- 三项共四个回归测试：`test_reading_services.py`（并发+超时+进度、种子过期）、
  `test_views_reading_m2.py::SourceSwitchProgressTests`（进度接到对话框）、
  `test_sources_service_m3.py::test_on_start_discards_staged_leftovers_and_their_cache`（试读残留回收）。
- 试读退出询问：阅读器工具栏补「退出阅读」入口（旧单体里返回按钮在详情页，拆成新结构后
  阅读器成了独立目的地，谁都没带这个动作），`ReaderController.exit_reader()` 经新增的
  `ReadingService.staged_record()` 回拿暂存记录再询问，收尾走新增
  `promote_staged_book()` / `discard_staged_book()`（后者连带删 `books` 表进度行与 `ReadingCache`），
  `close_book()` 由此恢复调用方。回归测试：
  `test_views_reading_m2.py::ReaderExitStagedTests`（点按钮 → 弹询问 → 两条分支的调用顺序与落点）、
  `test_offline_source_chain.py`（真实服务上转正保留进度、丢弃清干净记录/进度/缓存）。

## 后续复发（2026-09-26 / 09-27）

同族问题又出现两次，形态不同但根因相同——**"能力存在"与"能力接出来"是两件事**：

| 日期 | 症状 | 证据 | 处置 |
| --- | --- | --- | --- |
| 09-26 | 听书 / RSS / 网络桥 / 本地书：**服务全注册、事件全定义，界面零订阅零调用** | `ReadAloudService.speak` / `RssService` / `MediaPlayService` 播放动作只有定义 | 第七、九轮补界面；`MediaPlayService`（视频书）至今仍只有服务面 |
| 09-27 | 整书缓存在途任务：`DownloadService.list_tasks()`（:99）与 `cancel()`（:88）**零调用方**，界面只有一行"最新进度"文本 | 两本书并行缓存说不清是哪本；切页回来任务"消失"（其实还在跑）；入队后没有任何取消入口 | 已接出（`bookshelf_view.py:27/:75/:110` + `bookshelf_controller.py:73/:115/:263/:285/:303`），回归测试 09-27 同日补齐：`test_views_reading_m2.py::BookshelfCacheTaskRowsTests`（4 项，逐条变异核验） |

还有第三型，比"搬丢"更隐蔽：**进度事件从来就没有存在过，却被文档写成有**。
2026-09-27 复核开发日志时发现旧记录声称"替换规则 12 个 / 规则修复 11 个 / 备份 4 个进度事件"，
当时这三条链路一个进度事件都不发，只发收尾的结果事件——

- 替换规则：`ReplaceRulesChangedEvent`（`replace_rule_service.py:132/197/210/225`）；
- 规则修复：`RepairReportReadyEvent`（`rule_repair_service.py:102`）；
- 备份/恢复：`BackupFinishedEvent`（`web_dav_service.py` 13 处发布点）。

**三条里只有两条是真缺口**：规则修复与备份/恢复当日已补
（`RepairProgressEvent` / `BackupProgressEvent`，同日第十七轮）；替换规则那条是**这次审计
自己造的谣**——项目里没有"批量替换"这个动作，替换在阅读链路里逐章就地做
（`reading_engine.process_book_content` → `get_global_replace_rules`），没有可以进度的那段路。
登记缺口前先确认那是不是一个跑得够久的事，与"能力存在 ≠ 能力接出来"是同一条纪律的两端。

同样的两型还出在**自愈界面**上：`RuleRepairService` 服务面齐、`app/views/source/rule_repair_view.py`
视图也在，但 `RuleRepairController._to_dict` 读的是 `RepairReport` 根本不存在的顶层属性
（真数据在 `report.fix` 里），结果是报告卡永远空白、确认按钮永远拒收——**三样都"接了"，
链路却是断的**。所以"接出来"的证据不能停在"视图有槽位 + 控制器有赋值"，还要有一条
**从服务真实返回值一路渲染到控件**的用例（本轮的 `RepairFlowTests` 用真 `RepairReport`
喂 `_on_report_ready`，就是为这一口而写）。

补齐前，全库只有三条长任务真的发进度事件：`DownloadProgressEvent` / `ImportProgressEvent` /
`RssRefreshProgressEvent`；三条缺口补完（`RepairProgressEvent` / `BackupProgressEvent` /
`ExportProgressEvent`，第十七、十八轮）后是六条。
那两条真缺口当时的症状与本文开头的"进度条永远 0"完全一样：
**从点击到出结果之间没有任何可显示的东西**——只是这次不是搬丢，是**从来没写过，
而被记录当成已经写过**。

第四型（09-27 第十八轮，整本导出）比前三型都更靠近"明明全对了却不动"：
**事件有了、订阅有了、进度行也有了，界面照样一潭死水**——因为这条链是**纯同步重活**
（逐章读盘 + `write_epub` 压缩），全程一次 `await` 都没有，而被占住的正是处理器要跑的
那条引擎循环。`publish` 只负责排队，队列在任务返回前没人消费。
前四条进度链都夹着网络 `await`，循环自然让出，所以这个前提从没被暴露过。
⇒ **"服务发了事件"与"处理器跑过了"之间还夹着一条：发事件的循环要肯让出来。**
带进度事件的本地批量任务（导出、打包、批量转码、整库重算）必须自带让出点，
护栏也要照这个形状写（总线替身 `publish` 即 `create_task`，断言"中途已送达"而不是"最后收到过"）。

同一轮还查出**成对计数被对称测试数据洗白**：`ReadingCache.cache_counts()` 返回
`(已缓存, 目录总数)`，调用点按 `(总数, 已缓存)` 解，唯一那条护栏的替身给的是 `(5, 5)`——
两边相等，怎么解都对，于是"没缓存的书被列进导出清单""半本的书显示成 5/2 章"一直活到本轮。
⇒ 凡成对/成组返回的计数，替身两边不许相等。

第五型（09-27 第十九轮，改封面）不是"搬丢调用方"，而是**找回调槽找回了一个没人看的字段**：
`update_cover` 确实零调用方，照惯例接上 FilePicker 就算补完——但 grep
`cover_url|coverUrl` 在 `app/views/**` 同样零命中，书架行/网格画的一直是 `ft.Icon(BOOK)`。
只接动作的那一版交付会是"挑完图片、状态行说一句、屏幕什么都不变"的**只写不见**功能
（唯一真消费方是 epub 导出 `_embed_cover`）。
⇒ **"某服务方法零调用方"只说明动作没接出来，不说明接出来就有功能。**
接写入型动作前先问这个字段谁在画、谁在消费；渲染方缺失时要一并补，或者把交付说明写成"只影响导出产物"。
同一条追问还带出一处假话：`update_cover` 无论书架记录对不对得上都返回 resolved 并发事件，
把返回值当成功就会在界面没变的时候说"封面已更新"——写入型动作的反馈要以**读回的记录**为准。

## 复发时先查

- **对照 `docs/找书与换源逻辑对齐.md` §4 的对齐清单读代码**，别只跑测试：
  退化型缺陷的表现是"少了调用方"，`grep <method_name>` 只命中定义处即为信号。
- 大重构后做一次"孤儿方法扫描"：公开服务方法（`discard_*` / `clear_expired` / `set_progress` /
  `close_book` / `promote_book`）若只有定义没有调用，基本就是搬丢了。
- 并发/超时/节流这类参数在旧文件里是模块常量（`SWITCH_*`），新文件里若找不到同名常量，
  说明那整块逻辑被重写过，需要逐项核对语义而非名字。
- **报"有进度事件"之前先问这条链有没有 `await`**：一条同步到底的批量任务，事件发得再准
  也上不了屏（第四型）。同样地，**报"某功能有护栏"之前先看替身两边是不是相等的数**（第二型）。
- **报"某服务方法零调用方 ⇒ 接上就完事"之前先 grep 那个字段有没有渲染方**（第五型）：
  写入型动作的闭环是"动作 → 落库 → 有人画它"，三段里缺第三段的交付长得和成功一样，
  只有对照屏幕才发现。反馈也别拿服务方法的返回值当成功——读回记录再报。
- **补的护栏也要变异核验，否则"接出来了"仍是假的**。09-27 第一版整书缓存测试只断言
  "读不到服务快照时一行都不画"——把 `except` 拆掉后 4 项照样全绿：编排协程跑在引擎循环上，
  异常只进 future 的日志，界面表现是"少了一块"而不是报错。补一条"它后面的步骤确实发生了"
  （状态行写了 `书架共 N 本`）才红得下来。凡是钉"没发生"的断言，都要配一条
  "该发生的仍然发生"，否则退化成静默少做时测试跟着一起静默。
