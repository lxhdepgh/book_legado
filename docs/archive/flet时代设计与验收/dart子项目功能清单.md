# legodo_dart 页面功能清单

> 第一百十七轮盘点，2026-10-17。口径与 `docs/flet子项目功能清单.md` 一致：逐屏读控件 →
> notifier/controller → `EngineRepository` 封装 → FFI action。"真"= 这条链在代码里通到底；
> 本轮**没有跑过应用**，全部静态判定。
>
> 与上一轮的差别：这次不看卡顿与契约，只看**每一屏上有多少个能点的控件、点了到底发生什么**。
> 缺口与待拍板项在 `docs/dart补缺方案.md`。

---

## 1. 总体形状

| 项 | 事实 | 落点 |
|---|---|---|
| 进程模型 | 单进程 FFI-only：`legodo_client.exe`（Flutter AOT）`LoadLibrary` 一个 `legodo_engine.dll`，无子进程、无 socket | `lib/core/ffi/engine_ffi.dart` |
| 唯一入口 | `Call(action, paramsJSON)` + `FreeString(ptr)`，两条 C 符号 | `lib/core/ffi/engine_ffi.dart:105-140` |
| 事件出口 | 没有推送：Dart 每 250 ms 带游标轮询 `engine_poll`，游标跨流重建保留 | `lib/core/rpc/ffi_engine_adapter.dart:257-297`、`_pollInterval` :299 |
| 事件合流 | 一个轮询周期 = 一批；白名单里同名的多条在出口收成一条 | `ffi_engine_adapter.dart:120`（`coalescableEventNames`）、`:124`（`coalesceBatch`）、调用点 :283 |
| 导航 | 只有**分组**进路由表（3 条）；组内页签是分组页自己的状态，不在路由表里 | `lib/app/router.dart:36-38`、`:226-344` |
| 整页目的地 | 阅读器 + 8 个工具页，用根导航器压在壳之上（`context.push`） | `router.dart:43-53`、`:262-342` |

### 规模（当场数）

| 面 | 数量 | 口径 |
|---|---|---|
| `lib/` Dart 文件 | 72 | `find legodo_dart/lib -name "*.dart" \| wc -l` |
| `lib/` 行数 | 16,746 | `wc -l` |
| 屏与对话框 | 19（17 屏 + `import_dialog` + `source_switch_dialog`） | `find lib -name "*_screen.dart" -o -name "*_dialog.dart"` |
| `backend_go/` Go 文件 / 行数 | 80 / 28,387 | 同口径 |
| `internal/services/` 文件 | 39 | `ls` |
| Go `route()` 里的 action | **90** | 正则抽 `case "…"`，点号与下划线两种拼法折叠去重（`.probe/t117_action_pano.js`） |
| Dart 适配器实际发出的 action | **82** | 同上：适配器文件里出现该 action 字面量 |
| `EngineRepository` 接口方法 | **84** | 正则抽声明（`.probe/t118_repo_calls.js`） |
| 其中在 `lib/`（除 `core/rpc` 自身）**零调用** | **11** | 同上：匹配 `.方法名` 调用与 tear-off，声明行排除；八条正对照全活（`listSources`/`listRssSources`/`chapterRows`/`openReader`/`checkStart`/`starRssArticle`/`shelveBook`/`discardStaged`），见 §4 |
| `test/` 文件 | 26 | `ls test \| wc -l` |

---

## 2. 逐屏清单

### 2.1 书源组（页签：管理 / 检测）

| 屏 | 控件 | 下游 action | 事件 | 判定 |
|---|---|---|---|---|
| **书源管理** `features/sources/presentation/sources_screen.dart`(555 行) | 刷新(:93) / 搜索框(:346) / 分组筛选(:358) / 状态筛选 仅启用·仅停用(:377) / 全选筛选结果(:392) / **导入**(:399) / **检测**(:405，切页签不退订，走 `GroupTabsScope.select('检测')` :116-117) | `source_list` | `SourceChangedEvent` → **整表重取**(:78-80) | 真 |
| ↑ 批量条 | 已选择 N 项 + 启用 / 禁用 / 删除 / 取消选择(:416-460) | `source_batch_enable`、`source_batch_delete`（删除带"前三条名字 + 等 N 条"二次确认 :243-279、`namesForBatchDeletion` :293-303） | ↑ | 真 |
| ↑ 行 | 勾选(:490) / 标题+副标题(:495-506) / **删除**(:510) / 启用开关(:517) | `source_enable`、`source_delete` | ↑ | 真 |
| ↑ **没有的** | **没有「编辑规则」入口**，**没有「导出」按钮**，**没有「自愈」按钮** | `source_get`/`source_save`/`source_group`/`source_export`/`source_audit` 五条封装在 lib 里零调用（§4） | — | 缺口 |
| **检测** `features/check/presentation/check_screen.dart`(221) | 刷新(:68) / 开始检测(:86) / 停止(:102) / 结果行(:204) | `check_start`、`check_snapshot`、`check_cancel`、`check_source` | `CheckFinishedEvent`(:42) | 真 |
| **导入对话框** `sources/presentation/import_dialog.dart`(431) | 选择本地 JSON 文件(:245) / 预览(:160) / URL 输入(:277)+导入(:289) / 追加·替换单选(:425) / 替换二次确认(:155-173) / 确认导入(:393) | `import_text`、`import_url`（kind 固定 `book_source`） | — | 真 |

### 2.2 阅读组（页签：找书 / 书架 / 订阅）

| 屏 | 控件 | 下游 action | 事件 | 判定 |
|---|---|---|---|---|
| **找书** `features/find_books/presentation/find_books_screen.dart`(538) | 三内页：书源列表(:168) → 分类浏览(:231)/搜索(:254,262) → 结果(:305)；刷新书源(:155)；停止(:270)；搜索范围(:281)；详情弹窗 关闭·加入书架·试读(:338/344/356)；返回(:414) | `browse_categories`、`browse_books`、`search_start`、`search_stop`、`search_scopes`、`bookshelf_add`、`reader.open` | `SearchFinishedEvent`(:83) → **累计快照替换**，不重取 | 真 |
| ↑ **没有的** | 无「登录书源」入口（引擎也没有 `login_*` 任何一条路由）；结果行不画封面；无「问题反馈」 | — | — | 缺口 |
| **书架** `features/bookshelf/presentation/bookshelf_screen.dart`(219) | 刷新(:33) / 行：封面槽(:79) + 标题(:80) + 副标题(:83) + **换源**(:93，本地书那一格收起 :90) / 点行开读(:102) / 空架(:144) / 失败可重试(:174) / 状态行「书架共 N 本」(:156-172) | `bookshelf_list`、`reader.open`、`switch.*` | **不听任何事件** | 真，但只有读与换源 |
| ↑ **没有的** | 无搜索框 / 无分组·排序下拉 / 无列表↔网格 / 无「移出书架」/ 无「更换封面」/ 无「整书缓存」/ 无「导入本地书」 | 三条引擎路由都在，但**接口与适配器里连这两条封装都不存在**（`lib/core/rpc/` 全量 grep `bookshelf_delete`/`bookshelf_update` 零命中）；而 `bookshelf_update` 那一发只读 `reading`/`vars`（`engine.go:730-742`），**改不了封面**（见 §3） | — | 缺口 |
| **换源对话框** `bookshelf/presentation/source_switch_dialog.dart`(207) | 种子 / 扫描 / 取消 / 候选确认 | `switch_seeds`、`switch_scan`、`switch_snapshot`、`switch_cancel`、`switch_apply` | `SourceSwitchProgress/Finished`(:77) | 真；**分组换源没有下拉**，`group` 恒为空串 |
| **订阅** `features/rss/presentation/rss_screen.dart`(685) | 9 个 rss 动作全接 | `rss_sources`/`articles`/`article`/`add_source`/`remove_source`/`refresh`/`refresh_all`/`mark_read`/`star` | `RssRefreshProgress`、`RssRefreshed`(:64) → 整表重取 | 真 |
| **阅读器** `features/reader/presentation/reader_screen.dart`(1,016) | 工具栏 7 键：退出(:264) / 小一号(:279) / 大一号(:287) / 段首缩进(:295) / 书内搜索(:303) / 书签(:309) / 目录(:315)；上一章(:379) / 下一章(:390)；正文行表(:411-443)；放入书架二次确认(:467-479)；目录抽屉 重取(:746)+列表(:760)；书签抽屉 加(:828)/删(:856)；书内搜索抽屉 输入(:912)/提交(:919)/结果(:938) | `reader.open`/`toc`/`content`/`position_save`/`bookmarks`/`bookmark_add`/`bookmark_remove`/`search_content`/`staged`/`staged_promote`/`staged_discard`/`close`、`settings_read`/`write` | **不听事件**；进度 500 ms 合流 | 真 |
| ↑ 媒体行 | `_MediaLine`(:420-425) 画的是**地址那一格**（`_mediaUriFor` :451-456 只贴不拼），不播 | — | — | 只显地址 |
| ↑ **排版能力** | `ReaderTypography` 只有 **2 个键**：`reader.font_size`、`reader.indent`（`domain/reader_controller.dart:105-155`，键名 :118-119） | `settings_read`/`settings_write` | — | 真，但对 flet 的 10 键（`reader_view.py:42-57`）少 8 键：行距、段距、版心宽、文本对齐、阅读背景、字族、状态栏显隐、划词开关 |
| ↑ **没有的** | 无沉浸模式 / 无目录倒序与分批懒加载 / 无修复条 / 无划词 / 无视频播放条与播放列表 / 无朗读入口 | — | — | 分开记：**朗读入口=缺口**（补缺方案 D4）；划词与视频条是框架形状差（§3.2-S1/S4），不记为缺口；沉浸模式与目录倒序是 dart 自己的待办，与 flet 无关 |

### 2.3 配置组（页签：外观 / 更新 / 工具）

| 屏 | 控件 | 下游 | 判定 |
|---|---|---|---|
| **外观** `settings/presentation/appearance_screen.dart`(243) | 重载(:40) / 模式三单选(:62-84) / 墨水屏开关(:92) / 皮肤下拉 `kAppSkins`(:100) / 预览小组(:118) / 恢复默认(:129) / 保存并应用(:134) | `theme_preferences`、`theme_save` | 真。**没有背景图那一行**是写明在文件头的决定（`:6-8`：协议面上没有对应动作，"放一个点开什么都不发生的按钮，比不放更糟"） |
| ↑ 预览小组 | `FilledButton(onPressed: null)`(:223)、`OutlinedButton(onPressed: null)`(:227)、`Chip`(:228)、`TextField(enabled: false)`(:233) | 无 | **刻意的死控件**：它们是配色缩样，注释说明控件真的跟着整页主题走（`:194-197`） |
| **更新** `settings/presentation/update_screen.dart`(168) | 检查更新(:106) / 版本卡(:129) / **下载到下载目录**(:147) / 忽略此版本(:157) | `update_check`、`update_ignore` | 半个：`:149` 是 `// TODO: 打开下载链接（需要 url_launcher 插件）`，点下去只把 `_status` 写成"下载功能待实现"(:150)。**「当前版本」那一格也是假的**：`hello()` 不发 RPC，返回常量 `build: 'go-ffi'`（`ffi_engine_adapter.dart:229-233`）；而**引擎侧没有一发能回它**——`engine.hello` 只是 `engine_status` 的别名（`engine.go:310`），`update_check` 只回远端 `UpdateInfo`（`:1269-1287`）。版本号在 Go 侧是 `AppVersion` 常量（`update_service.go:32`），有护栏测试钉住 `pubspec.yaml`（`update_service_test.go:259-270`） |
| **更多工具** `settings/presentation/tools_overview_screen.dart`(88) | 10 张卡片(:11-22) → `openToolPage` | `router.dart:66-76` | **两张点了没反应**：`openToolPage` 只有 8 个 case，「词典」连页面都没有（`_ => null` :75，静默不跳 :77-79），「更新」页其实存在（配置组第二个页签）但卡片没有它的 case |
| 替换规则 `replace_rules_screen.dart`(206) | 新建(:152) / 全局开关(:161) / 行开关(:185) / 删除(:193) | `replace_list`/`add`/`delete`/`toggle_global` | 半个：**行内开关 `onChanged` 是空的 TODO**（`:187-189`）→ 关掉某条规则这件事不发生；`replaceUpdate` 封装零调用 |
| 字体 `font_screen.dart`(173) | 扫描系统字体(:94) / 添加字体文件(:98) / 行选中(:128) / 用户字体删除(:147) | `font_scan_system`/`add_user`/`remove`/`select` | **半坏**：`_userFonts` 初值 `[]`(:19) 之后再没被赋过值（`_loadFonts` :29-46 只读系统字体那一条），于是"用户字体"整块（`:132-167`，含删除按钮 :147）**永不渲染**，`font_remove` 界面不可达；「当前字体」恒显"系统默认"(:112)，因为 `_currentFont` 只在点过之后才有值(:76) |
| 二维码 `qr_code_screen.dart`(175) | 文本框(:100) / 生成(:112) / **识别（Base64）**(具体形状见 :64 那句提示) | `qr_generate`、`qr_decode` | 半个：识别这一发吃的是**同一个输入框里手粘的 Base64 串**(:59-86)，没有选图入口——而 `file_picker` 在字体页(:4) 和导出页(:4) 都已经在用 |
| 导出 `export_screen.dart`(180) | 书籍标识输入框(:108) / 输出路径 + 选目录(:117-131) / 选 epub 目录(:137) / 选 txt 目录(:143) / 导出 epub(:152) / 导出 txt(:164) | `export_epub`、`export_txt` | 半个：`book_key` 要**人手打**「书源URL + 书籍ID」(:111-112)，而下面两条按钮点下去引擎回的是**显式未实现**（`engine.go:259-262`：导出器缺"把正文喂进去"那一步，不会写出任何文件） |
| WebDAV 备份 `webdav_screen.dart`(166) | 配置表单 + 备份/恢复/列表 | `webdav_backup`/`restore`/`list_backups` | 界面接线真，**引擎三条全在 unimplemented 表**（`engine.go:264-268`）→ 点下去必然报那句"未实现"。备份对象**没有 7 项可选** |
| 网络桥 `web_bridge_screen.dart`(211) | 开关 / 端口 / 刷新 / 令牌 | `webbridge_start`/`stop`/`status` | 同上：`engine.go:253-257` 三条未实现；文件 :50 那句注释自己写明了"这一发现在只会红"。**这条不是缺口**：flet 那边它是"再起一个 aiohttp HTTP+WS 监听"，而本项目已经把网关层拆掉换成 FFI——要补它等于推翻架构决定（`docs/dart补缺方案.md` §3.1-X6） |
| 浏览器 `browser_screen.dart`(102) | 地址栏(:66) / 后退(:77) / 前进(:81) / 刷新(:85) / `WebViewWidget`(:92) | **零 FFI**：整屏走 `webview_flutter`(:8)，不经引擎 | 真（引擎 `browser_*` 三条未实现 `engine.go:247-251`，但 Dart 这一屏不需要它们） |
| 朗读 `tts_screen.dart`(197) | 文本框(:83) / 语音下拉(:97) / 语速·音调·音量三滑杆(:115-172) / 播放(:177) | **零 FFI**：`FlutterTts`(:18)，不经引擎 | **演示页**：文本框的 controller 每次 build 现造(:89-91) 且 `onChanged: (text) {}` 是空的(:92)，而播放按钮把**硬编码那句**传进 `_speak`(:178)——输入框改成什么都不朗读什么。引擎 `read_aloud_*` 四条未实现（`engine.go:270+`）且适配器零调用 |

---

## 3. action 全景（当场抽表）

`.probe/t117_action_pano.js` 的输出，三个集合：Go `route()` 90 条、Dart 适配器发出 82 条、flet METHODS 81 条。

- **flet 声明的 81 条，Go 全有路由**（差集为空）。
- **Dart 发出的 82 条，Go 全有路由**（差集为空）。
- ⇒ 协议面两侧没有缺口，缺口全在 UI 层与"引擎有路由但没人调"。
- ⚠️ 那 90 条是把点号拼法归一成下划线后数出来的（`engine.status` 与 `engine_status` 算一条）。归一表里**恰好有一条是幻影**：`engine_hello`。`route()` 不做拼法归一，`Call` 把 action 原样递进去（`engine.go:141`），所以发 `engine_hello` 会落 `default:` 回 `unimplemented: engine_hello`（`:564-565`）；表里真正存在的只有点号那条 `engine.hello`，而它是 `engine_status` 的**别名**（`:310`），回的是服务状态表，不含版本/schema digest。除这一条外其余 89 条两种拼法都能送达。

### Go 有路由、Dart 适配器零调用（7 条）

| action | 引擎侧 | 为什么缺 |
|---|---|---|
| `bookshelf_delete` | 真实现：`svc.Remove(url)`，键先读 `url` 空则 `bookUrl`（`engine.go:744-752`） | 书架行菜单没有入口；且**适配器与接口都没有这条封装**，不是"有封装没人调" |
| `bookshelf_update` | 路由在，但那一发**只读 `reading` 与 `vars`**（`engine.go:730-742`）——写不了封面、写不了分组 | 同上。所以"更换封面"不能靠接这一发了事（缺的是引擎侧的字段） |
| `bookshelf_import` | **显式未实现**（`engine.go:277`） | 本地书导入没有后端 |
| `read_aloud_start` / `_stop` / `_pause` / `_progress` | **显式未实现**（`engine.go:270-275`、`:558-562`） | 朗读改走 `flutter_tts`，但这四条路由还挂在表上 |

### 界面调了、引擎回"未实现"的 17 条（`engine.go:246-282` 整表）

`browser_open_page` `browser_navigate` `browser_close`｜`webbridge_start` `webbridge_stop` `webbridge_status`｜
`export_epub` `export_txt`｜`webdav_backup` `webdav_restore` `webdav_list_backups`｜
`read_aloud_start` `read_aloud_stop` `read_aloud_pause` `read_aloud_progress`｜`source_audit`｜`bookshelf_import`

这是第一百一十二轮"死桩改显式错误"的产物：每一条都带一句人话说明**它不会做成什么**。
所以界面上那几屏不是崩，是必红——红得清楚。

### 注册了在跑、但没人碰得到的三个服务（当场逐个访问器数）

`hub.go` 的 19 个 `XxxService()` 访问器，去掉定义与测试后数调用点：**`BrowserService` 0、`ExportService` 0、`ReadAloudService` 0**，其余 16 个 ≥1。
三条都在真注册表里（`register.go:243`、`:263`、`:294`），`ReadAloudService.Speak/Pause/Resume` 那四处 `ReadAloudStateEvent` 发布点因此在生产里走不到。
它们是"上面那 17 条改成显式未实现"时没跟着清的壳——与 §5 的事件缺表同源：**能力没接线的时候，事件类型与服务壳会各自留着，看起来都像在干活**。

---

## 4. `EngineRepository` 的 11 条零调用封装

扫描口径：`lib/` 全量（排除 `core/rpc/` 自身），匹配 `.方法名`（含 tear-off 与换行链），声明行排除（`.probe/t118_repo_calls.js`）。
八条明知非零的对照全活，所以这个"11"不是仪器没接上的读数——上一版探针（`t117_repo_calls.js`）要求点号前面还有一个非点字符，把 `foo\n  .bar()` 与 `_attempt(_repo.discardStaged, …)` 两类写法读成了零调用，一度报出 22 条乃至 84 条。
这 11 条不是"接口冗余"，而是**接线断在界面侧**：服务面能做的事，屏幕上没有一个入口。

| 封装 | 引擎路由 | 该接在哪 |
|---|---|---|
| `loadSource` / `saveSource` | `source_get`/`source_save` 真 | 书源行「编辑规则」→ 规则编辑器 |
| `setSourceGroup` | `source_group` 真（第一百一十六轮刚修好它的整表刷写 bug） | 书源行的分组改名 |
| `exportSources` | `source_export` 真 | 书源工具条「导出」 |
| `auditSource` | `source_audit` **未实现** | 同上那条"自愈"卡片，等引擎 |
| `replaceUpdate` | `replace_update` 真 | 替换规则行内开关（`:188` 那个 TODO） |
| `readPosition` | `reader_position_get` 真 | 开书时恢复上次位置 |
| `serviceStatus` | `engine_status` 真 | **服务仪表盘**（整屏不存在） |
| `browserOpenPage` / `browserNavigate` / `browserClose` | 三条**未实现** | Dart 浏览器页走 webview_flutter，本来就不需要它们——这三条封装是死代码 |

---

## 5. 事件面

| 事实 | 数 | 口径 |
|---|---|---|
| Go 侧事件类型 | 24 | `grep -c "EventType() string" core/events.go` |
| Dart 登记了名字 | 8 | `lib/core/events/event_names.dart:15-43` |
| 其中真有屏在听 | 7 | `serviceStateChanged`(:24) **登记了但零消费者** |
| `listenToEngineEvents` 调用点 | 5 | sources :78 / source_switch :77 / rss :64 / check :42 / find_books :83 |

**没屏在听的 16 条**（`core/events.go`）：`ProgressSavedEvent`、`BookmarkChangedEvent`、
`ReadingSessionChangedEvent`、`MediaPlayStateEvent`、`ImportProgressEvent`、`ImportFinishedEvent`、
`DownloadProgressEvent`、`ExportProgressEvent`、`BackupProgressEvent`、`BackupFinishedEvent`、
`RepairProgressEvent`、`RepairReportReadyEvent`、`ReplaceRulesChangedEvent`、`SettingsChangedEvent`、
`ReadAloudStateEvent`、`UpdateAvailableEvent`。

这 16 条**不能整片算成"界面漏接"**——按"Go 侧非测试代码里有没有构造点"再切一刀（当场数）：

| 那 16 条分成两半 | 数 | 明细（构造点数） |
|---|---|---|
| 引擎真在发，屏没听 | **4** | `ReplaceRulesChangedEvent` 4、`ReadAloudStateEvent` 4（但发它的那个服务碰不到，见 §3）、`SettingsChangedEvent` 1、`ReadingSessionChangedEvent` 1 |
| 引擎**根本没有发布者** | **12** | `Download` / `Backup×2` / `Export` / `Import×2` / `MediaPlay` / `Repair×2` / `ProgressSaved` / `BookmarkChanged` / `UpdateAvailable` 全为 0 |

下半那 12 条跟着能力走（缓存、导出、备份、修复、视频、朗读、更新提醒在 Go 侧都还没写），列在这里的价值是：**它们精确指出了 flet 那边每一处是靠哪条事件活着的**。

文件头写明了这里的规矩（`event_names.dart:8-10`）：只列有屏在听的，等到有屏听那天再登记。
所以这张缺表是**自认的**，不是漂移——但它同时精确指出了"哪些屏在 flet 那边是靠事件活着的"：
书架（3 条）、阅读器（3 条）、导入进度（2 条）、导出/WebDAV/修复（4 条）。

两处后果值得单独记：
1. **书架不听事件** → 从阅读器退出（进度已落库）回到书架，那一行的"读到 章名"是旧的，只有手动刷新才新。
2. **改完不重取、直接整表重取** 的地方是 sources/rss/check；找书那一屏走的是累计快照替换（`SearchFinishedEvent`），两种形状并存且都对，但补缺时别把找书改成重取。

---

## 6. 测试覆盖（按屏）

| 屏 | 测试文件 |
|---|---|
| SourcesScreen | `test/golden_widget_test.dart`（5 行标题/副标题逐字对投影表、批量删除名字、筛选、失败态不画"共 0 条"）、`test/r4_dart_ui_baseline_test.dart` |
| CheckScreen / FindBooksScreen / AppearanceScreen | `test/golden_widget_test.dart`（同文件 :154/:235/:271 三个 group） |
| BookshelfScreen | `test/bookshelf_screen_test.dart` |
| ReaderScreen | `test/reader_screen_test.dart`、`test/reader_continuous_screen_test.dart` |
| RssScreen | `test/rss_screen_test.dart` |
| SourceSwitchDialog | `test/source_switch_screen_test.dart` |
| 导入对话框 | `test/source_import_dialog_test.dart` |
| ExportScreen / WebDavScreen / WebBridgeScreen | `test/tool_stub_screens_test.dart`（钉的是"点了会拿到那句未实现"） |
| **UpdateScreen、ToolsOverviewScreen、BrowserScreen、TtsScreen、FontScreen、QrCodeScreen、ReplaceRulesScreen** | **零引用**（逐个类名 grep `test/`） |

契约层与跨语言守卫在仓库根 `tests/`（如 `tests/test_gateway_contract_single_source.py`）。其中事件名那一条**只做单向检查**（`:646-661`）：
Dart 登记的每条名字都必须在 flet `app/core/events.py` 的类名集合里，**登记多了不会红，只有拼成引擎侧不存在的名才红**——
所以 `serviceStateChanged` 那样"登记了但零消费者"的一格在既有测试里是绿的。它自己的文件头（`event_names.dart:5-6`）写的"多一格、少一格、拼错都是红"比这条测试实际做的更严，那是一处口径漂移。

---

## 7. 屏内自己写明的"这里不发生什么"

补缺时先看这一节，避免把"已经声明过的不做"当成新发现：

| 落点 | 原文口径 |
|---|---|
| `appearance_screen.dart:6-8` | 不做背景图，理由是协议面上没有对应动作 |
| `router.dart:106-109` | 书源组不放 导入/规则/自愈 三个**占位页签**，理由是"占位会被当成已经接上，而它连 RPC 都没发过" |
| `router.dart:6-7` | 「进阅读先落书架」那条落地规矩还没接，缺口在"谁来持有那两档" |
| `router.dart:87` | 漫画阅读器还在 G4 |
| `replace_rules_screen.dart:188` | `// TODO: 更新单个规则的启用状态` |
| `update_screen.dart:149` | `// TODO: 打开下载链接（需要 url_launcher 插件）` |
| `bookshelf_screen.dart:110-111` | 封面槽只做"槽位在、尺寸对"，远程封面走 `GET /media/{key}` 是之后的活 |
| `ffi_engine_adapter.dart:230` | `hello()` 回常量，不发 RPC |
