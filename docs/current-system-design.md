# 系统设计文档（current-system-design）

> **本文档描述重构完成后的当前系统设计（M7 完成态），旧单体 UI（`app/app.py` + 平铺
> `views/`）已删除；平铺 `app/services/*.py` 未删除，它们是分域外壳的共享实现基座。**
>
> 状态：M7 回写版（2026-09-24），2026-09-27 校正第 8 节的删除范围与下列基线计数。
> 本次桌面端重构（依据 `docs/桌面端重构设计.md`）已按
> M1–M7 全部落地：进程内多服务架构、26 个业务服务按域分包、`app/core/` 基础设施、
> SQLite 主库 + 文件目录 + 互操作 JSON 三类存储、导航容器 `NavigationHost` 与
> `ServiceDashboard` 上线。**M7 清理线删除的是旧单体 UI**（`app/app.py` + 旧平铺
> `app/views/*.py` + `app/ui_feedback.py`）**以及被搬进域包的那 13 个旧平铺服务副本**；
> **其余 14 个旧平铺 `app/services/*.py` 模块全部仍在树内、且仍是"在跑的基座"**——
> 各域外壳经 import / 继承复用它们（逐条归属见 §8.2），删掉任何一个都会打断生产链
> （见 `docs/M7清理回归记录.md`）。
>
> 若本文与代码行为冲突，以代码为准并更新本文；红线以 `docs/architecture-rules.md`
> （红线 v2）与设计文档 §5.3 为权威。

## 1. 项目定位

Legado 书源连通性 / 搜索可用性检测与内置阅读器的桌面应用（Flet），参考实现来自
Legado Android 版规则体系。

核心链路：导入书源 → 兼容性审计 → 四段检测 → 找书 / 换源 → 内置阅读。项目核心价值是
与 Legado 规则行为对齐的兼容层（规则解析、JS 运行时、HTTP 语义），而非 UI 本身。

M1–M7 重构后，业务组织形态从"旧单体（一个 `AppView` 装配 + 一个 172KB `ReadingView`）"
升级为**进程内多服务架构**：全部服务跑在主进程内，仿 Legado `service/` 包职责划分，
统一生命周期 / 启停 / 状态可见，UI 为轻壳 + Controller。

## 2. 总体架构

```
main.py                # 入口：_build_hub() 装配 4 基础设施 + 26 业务服务
                       #   → asyncio.run(hub.start_all()) → ft.run(build_app_shell)
                       #   --smoke-test：冻结包动态依赖自检
app/
├── core/              # 基础设施（进程内底座，零 flet）
│   ├── lifecycle.py   #   Service 基类 / ServiceState 状态机 / ServiceStatus 快照
│   ├── hub.py         #   ServiceHub：注册 / 拓扑启停 / status_report / 优雅停止
│   ├── bus.py         #   EventBus：typed 发布订阅（异步投递到 EngineHub 执行域）
│   ├── events.py      #   AppEvent 基类 + 全部事件子类（不可变 frozen dataclass）
│   ├── engine_worker.py # EngineHub：守护线程事件循环 + EngineProxy（全服务协程执行域）
│   ├── storage.py     #   StorageService：legado.db 主库 / schema 迁移 / 目录分配
│   ├── settings.py    #   SettingsService：settings 表键值 + 变更事件
│   ├── registry.py    #   ViewRegistry：声明式目的地路由表（分组导航元数据）
│   └── interfaces.py  #   BrowserInteractionCallback（跨层交互回调 Protocol）
├── services/          # 26 个业务服务，按域分包（§4）
│   ├── engine/        #   规则兼容层核心资产（9 模块，只加壳不改行为）+ RuleEngineService 外壳
│   ├── sources/       #   书源域 5 服务
│   ├── reading/       #   阅读域 6 服务（+engine 外壳共 7）
│   ├── content/       #   内容形态域 4 服务
│   ├── tools/         #   工具域 10 服务
│   └── *.py           #   ⚠ 14 个旧平铺模块**未删除**，是上面各域外壳的共享实现 /
│                      #   继承基座（source_service / check_service / rule_repair /
│                      #   compat_audit / headless_browser / interactive_browser /
│                      #   web_page_renderer / search_model / reading_cache /
│                      #   block_pages / legado_url / request_compat / source_rule /
│                      #   app_paths）；逐条归属见 §8
├── views/             # Flet 视图（按域分包，轻壳）
│   ├── navigation.py  #   NavigationHost：分组导航容器 + build_app_shell 入口装配
│   ├── service_dashboard.py # ServiceDashboard：服务状态 / 启停控制 / 事件流
│   ├── reading/       #   找书 / 书架 / 阅读器 / 漫画 / 订阅 / 朗读条 / 视频播放条 /
│   │                  #   书签面板 / 书内搜索 / 登录框 / 换源框
│   ├── source/        #   书源列表 / 导入 / 检测 / 规则编辑器 / 规则自愈
│   ├── tools/         #   更多工具总览 / 浏览器 / 词典 / 替换 / 字体 / 二维码 /
│   │                  #   导出 / 备份 / 更新 / 网络桥 / 朗读设置
│   └── settings/      #   appearance_view.py（主题三件套下发，见 §5.7）
├── controllers/       # 每视图一个 Controller（事件订阅 + 服务调用 + 节流）
├── models/            # 数据类（BookSource 等，与主库映射）
├── storage/
│   └── schema.py      # 主库 schema（版本化，当前 v7）+ 逐版本迁移函数表
├── themes/            # 内置主题纯数据（五命名皮肤 + 墨水屏，由 ThemeService 承接；
│                      #   旧 `app/themes/builtin_themes.py` 仍是 tools 域的数据基座）
└── resources/         # publicsuffixes.gz（公共后缀表，规则兼容层数据文件）
tests/                 # 46 个 test_*.py，共 1220 个 `def test_`（2026-09-27 实测）
                       #   其中 test_legado_compat.py 488 条零漂移守门
```

**基线（2026-09-27 实测，全程离线）**：注册服务 30 个 = 基础设施 4 + 业务 26
（`ServiceHub.status_report()` 输出 31 行，多的一行是总控自己的行）；`NAV_META` 23 个
目的地 = 11 页签 + 12 整页，分 3 组；`tests/` 46 个 `test_*.py` / 共 1220 个
`def test_`（`--collect-only` 1215 条）；`test_legado_compat.py` 488 条 `def test_`
（351 同步 + 137 异步）。当日全量 **1215 passed + 28 subtests，0 failed**（80.05s）。
中间过程值得一提，因为它正是 §5.11 那条契约的现场：同一套代码当日连跑三次得到过
`1210 passed + 1 failed`，红的是
`tests/test_rss_reading_page.py::RssServiceSurfaceTests::test_refresh_all_publishes_progress_in_order`
——它只 `_wait` 了 `RssRefreshedEvent`，却断言另一条流 `RssRefreshProgressEvent` 的最后一拍，
末拍还没投递到就开断言（6 次观察挂 2 次）。**同日已把等待条件改对**（改后单跑 6 次全绿，
并且拿"末拍 `done` 钉成 0"做过变异核验，用例确实会红），所以这条不再是 flaky。
这些数字会随里程碑推进变化，引用前先重测。

分层方向（红线 v2）：`views → controllers → services → core → storage`。
视图间零 import；跨视图协作经服务事件与 `ViewRegistry` 路由。

## 3. 启动装配（main.py）

`main.py` 是唯一入口：

1. `_build_hub()` 构造并按域注册全部服务（注册顺序即里程碑顺序：基础设施 → RuleEngineService
   → 阅读域 → 书源域 → 内容形态域 → 工具域）。服务依赖拓扑由各服务声明的
   `dependencies` 经 Kahn 拓扑排序决定启停顺序。
2. `asyncio.run(hub.start_all())`：先依赖后消费者依次 `start()`；单服务启动失败不阻断
   其余（进入 SUSPENDED + `last_error` 可见）。
3. `_apply_saved_theme(page, hub)` → `build_app_shell(page, hub)`：进 `ft.run` 的回调先把
   **已保存**的外观落到页面三件套，再装导航（否则不进外观页就永远是 Flet 默认灰主题，
   见 §5.7）。
4. `finally: hub.shutdown()`：主线程等守护 loop 上的逆序 `stop_all()` 落盘收尾，
   之后再关掉 EngineHub 那条线程（见 §4.4 不变式 2）。

`python main.py --smoke-test`：冻结包动态依赖自检（Crypto/PIL/fontTools/opencc/
elementpath/jsonpath_ng.ext/playwright/quickjs + 引擎核心资产导入 + publicsuffixes.gz
在位断言），打包回归必跑，exit 0 为通过。

## 4. 服务层（26 个业务服务 + 5 个基础设施）

### 4.1 基础设施（core/，非业务服务）

| 服务 | 模块 | 职责 |
| --- | --- | --- |
| ServiceHub | `core/hub.py` | 主进程唯一总控制器：注册 / 依赖拓扑解析 / 拓扑序启动 / 逆序停止 / `status_report()` 状态汇聚；`get()` 返回 EngineProxy 句柄，`require()` 返回原始实例（服务间装配用），`service_by_id()` 供仪表盘单行反查 |
| EventBus | `core/bus.py` | 进程内 typed 发布-订阅：`publish` 把每个处理器作为协程任务投递到 EngineHub 执行域（不阻塞调用方）；`subscribe` 返回退订句柄；单处理器异常不拖垮总线 |
| EngineHub | `core/engine_worker.py` | 全服务协程的统一执行域：daemon 线程跑独立事件循环，`submit` 经 `run_coroutine_threadsafe` 投递；`EngineProxy` 只把协程方法派发到该 loop，普通属性读写直达 |
| StorageService | `core/storage.py` | SQLite 主库 `legado.db` 独占访问（`check_same_thread=False` + RLock）；schema 版本化迁移；`cache_dir(kind)` / `download_dir()` 文件目录分配；M6 旧介质一次性只读导入（幂等，旧文件改 `.legacy-bak`） |
| SettingsService | `core/settings.py` | 全局偏好统一读写（内存缓存 + settings 表 JSON 落库 + 发布 `SettingsChangedEvent`）；吸收旧 appearance.json / reader-layout.json |

### 4.2 业务服务按域分包

各域包里的类是**外壳**：路由 / 事件 / 主库读写 / 配置包装。方法体大量来自仍留在
`app/services/` 平铺层的 14 个旧模块（继承 `SourceService`、直调 `check_service`、
懒 import `headless_browser` 等）——逐条归属见 §8.2，那些旧文件不是遗留、是活的基座。

**书源域 `services/sources/`（5）**

| 服务 | 职责 |
| --- | --- |
| `SourceService` | 书源 CRUD / 分组 / 统计 / 导入导出（JSON 互操作，`mode` 支持追加与整表替换，逐条发 `ImportProgressEvent`）/ 书架与 notShelf 语义 / 换源落库 / 封面字段维护；M6 起读写主库 `book_sources` / `bookshelf` |
| `SourceCheckService` | 四段检测（连通 → 搜索 → 分类 → 阅读链路）、九态结果分类、自动停用；`block_pages` 拦截识别随迁共享；每轮批量留可回放快照（`run_snapshot` / `await_run_finished`，见 §5.3） |
| `RuleRepairService` | 正文规则自愈：样本页挖掘候选选择器、焊死样本 HTML 回放验证、RepairReport（用户确认后才写回书源，不自动改源） |
| `CompatAuditService` | JS/Java API 支持面静态审计（导入即审 / 编辑保存即审 / 检测流水线复跑），缺口落 `audit_log` |
| `OnlineImportService` | 多类型在线导入分发（书源 / 替换规则 / 词典 / RSS / TTS / 主题 / TXT 目录规则），落库前统一过审计；书源以外类型当前无存储落点，只登记**给人看的说明**（文案里不出现里程碑编号），阶段进度发 `ImportProgressEvent`；模块函数 `preview_book_source_text` 供界面做导入前预览 |

**阅读域 `services/reading/`（6）+ 规则引擎外壳（services/engine/）**

| 服务 | 职责 |
| --- | --- |
| `RuleEngineService`（`services/engine/rule_engine_service.py` 外壳） | 规则求值核心：parser → rule_engine → reading_engine 三层 + legado_js + Java 兼容件；aiohttp 网络栈（会话复用 / 超时三档 / 全局 UA / Cookie 单罐 / 连接重拨 / deflate 解压 / 每主机并发 5）。核心资产 9 模块原位冻结，只加壳不改行为 |
| `ReadingService` | 目录 / 正文 / 媒体渲染数据、连续滚动进度（`chapter_index` + `scroll_offset/scroll_ratio` + `chapter_pos`）、阅读上下文、找书/换源 SearchModel 语义 |
| `BookmarkService` | 用户主动书签增删 / 列表 / 跳转定位（与自动续读进度是两条独立数据） |
| `ContentSearchService` | 书内全文搜索：SQLite FTS5 虚表（仅对已缓存章节建索引） |
| `BookCacheService` | 整书离线缓存：书内 4 并发 / 断点续传 / 图片内联 / 钉住不淘汰 |
| `DownloadService` | 下载任务中心：队列 / 进度事件（`DownloadProgressEvent`）/ 整书缓存与漫画预取的任务执行方 |
| `ImageBookService` | 图片型书籍（漫画）数据服务：图片章节解析 / 预取 / 缓存策略 |

**内容形态域 `services/content/`（4）**

| 服务 | 职责 |
| --- | --- |
| `ReadAloudService` | 朗读三后端（系统 TTS pyttsx3 / edge-tts 在线 / HTTP-TTS 书源规则），`TtsBackend` 抽象；无音频设备自动降级 |
| `MediaPlayService` | 视频书播放：`MediaBackend` 抽象（flet-video / 系统播放器降级）、章节播放列表、记忆位置落 `books.play_position`、播放列表索引落 `books.play_chapter_index`（v7）。界面面为阅读器底部 `VideoPlayBar`（`views/reading/video_play_bar.py`，7 槽 + `MediaPlayStateEvent` 订阅，见 §5.12） |
| `RssService` | RSS 源管理 / 文章列表 / 订阅 / 已读收藏（主库 `rss_sources` / `rss_articles`） |
| `LocalBookService` | 本地书导入解析：epub（ebooklib）/ pdf（pymupdf）/ txt 自写，归一化为与网络书源一致的数据模型落 `books` 表 |

**工具域 `services/tools/`（10）**

| 服务 | 职责 |
| --- | --- |
| `ReplaceRuleService` | 用户全局替换规则 CRUD + 正文/标题两级应用（书源内嵌 ∪ 全局，顺序对齐 Legado ContentProcessor） |
| `DictService` | 划词词典规则 CRUD + 释义请求（复用 RuleEngineService 管线） |
| `FontService` | 用户字体文件登记（fontTools 解析）+ 系统字体只读枚举 |
| `QrCodeService` | 二维码生成（qrcode+Pillow）+ 图片文件/截图识别（opencv QRCodeDetector，无摄像头扫码） |
| `BookExportService` | epub/txt 离线导出（复用已缓存章节，不重新联网） |
| `WebDavService` | WebDAV 备份 / 恢复（导出 zip → 上传；**确认过才**拉取 → 校验 → 覆盖前留底 → 导入，`restore(..., confirm=True)` 是硬闸，见 §5.8）；备份对象按 `BACKUP_TARGETS`（sources/bookshelf/replace/dict/rss/config）真裁剪成员，成员方法名即跨服务契约（`export_*_json` / `import_*_json`，见 §5.8） |
| `UpdateService` | GitHub/Gitee 发布检查、下载到 downloads/、用户手动安装（不做静默自替换） |
| `WebBridgeService` | 本地 HTTP + WebSocket 服务（默认绑 127.0.0.1、默认关闭），远程书源管理/调试薄协议层 |
| `BrowserService` | 浏览器会话统一管理：无头渲染（Playwright）/ CDP 投屏交互 / WebView 回退三模块收编，profile 目录与会话池由服务统一持有 |
| `ThemeService` | 五命名皮肤 + 墨水屏配色 + 亮暗切换；外观偏好持久化并入 SettingsService |

### 4.3 服务间通信

- **请求-响应**：经 `ServiceHub.get(X)` 拿 EngineProxy 句柄调用；协程方法一律派发到
  EngineHub 执行域，调用方（UI 或服务）不阻塞。服务间装配（on_start 期）用
  `hub.require(X)` 拿原始实例。
- **通知（解耦）**：EventBus 发布 frozen dataclass 事件（不可变快照，禁止塞对象引用）。
  `core/events.py` 声明 `AppEvent` 基类 + **22 个事件子类**，全部都有生产发布方：
  `ServiceStateChangedEvent` / `SettingsChangedEvent` /
  `ProgressSavedEvent` / `BookmarkChangedEvent` / `DownloadProgressEvent` /
  `SearchFinishedEvent` / `ReadingSessionChangedEvent` / `SourceChangedEvent` /
  `CheckFinishedEvent` / `RepairReportReadyEvent` / `RepairProgressEvent` /
  `ImportFinishedEvent` /
  `ImportProgressEvent` / `ReadAloudStateEvent` / `MediaPlayStateEvent` /
  `RssRefreshedEvent` / `RssRefreshProgressEvent` /
  `ReplaceRulesChangedEvent` / `BackupFinishedEvent` / `BackupProgressEvent` /
  `ExportProgressEvent` / `UpdateAvailableEvent`。
  **投递是异步的**（`publish` 只把处理器排进引擎循环），约束见 §5.11。

### 4.4 生命周期

状态机 `REGISTERED → INIT → STARTING → RUNNING ⇄ SUSPENDED → STOPPING → DISPOSED`。
`ServiceHub.start_all()` 按 Kahn 拓扑序启动、`stop_all()` 逆序停止；每服务暴露
`status: ServiceStatus`（state / last_error / started_at / updated_at / progress），
每次迁移发 `ServiceStateChangedEvent`。轻量服务注册即 RUNNING；重服务
（BrowserService 拉 Chromium、WebBridgeService 监听端口）懒启动，资源释放走 SUSPENDED。

三条硬不变式（都由测试钉住，改生命周期前先读）：

1. **`start/stop/suspend/resume` 是基类保留名**：服务子类里表示业务动作的方法绝不能
   取这四个名字——`ReadAloudService.stop()`（停止朗读）和 `MediaPlayService.stop() /
   resume()`（停止/继续播放）一度整块盖掉基类生命周期方法，结果是逆序停止轮到它们时
   只停播放、不迁移状态，退出后服务表上永久留着两行 RUNNING，仪表盘的「恢复」也拨不动
   状态机。业务面改名 `stop_speaking()` / `stop_playback()` / `resume_playback()`；
   护栏见 `tests/test_core_lifecycle.py::test_no_service_subclass_shadows_lifecycle_methods`。
2. **EngineHub 的 loop 由外层线程收尾**：`stop_all()` 本身就跑在 EngineHub 守护 loop 上，
   而 EngineHub 也是注册服务，所以它的 `on_stop()` 只能标记不能 `thread.join()`
   （join 当前线程 = `RuntimeError: cannot join current thread`，就是过去每次关闭都打的
   那条「服务 EngineHub 停止异常」）。真正的关停放在 `ServiceHub.shutdown()` 的
   `finally`，由主线程在 `stop_all` 跑完后执行；`EngineHub.shutdown()` 自带"不 join
   自己所在线程"的守卫。
3. **引擎循环上不许释放 COM/原生对象**：pyttsx3（Windows SAPI，comtypes）的最后一个引用
   在哪个线程被丢掉，`_events.__del__` 就在那个线程同步等 COM 线程。实测在引擎循环上
   `self._backends.clear()` 会**冻住整条 loop**（连 `asyncio.wait_for` 的超时都跑不到），
   退出时 `stop_all` 卡到 10s 超时、后面所有服务停不掉。`ReadAloudService.on_stop`
   因此把后端摘给一次性守护线程去释放。

## 5. UI 层

### 5.1 导航容器 NavigationHost（`views/navigation.py`）

- 顶级入口只有三个，沿用旧单体的形状：**书源 / 阅读 / 配置**
  （`ViewRegistry.NAV_META` 为权威清单，当前 23 个目的地 = 11 个页签 + 12 个整页）。
  **一个分组 = 侧栏一个顶级入口**，组内的非整页目的地不再各占一行，而是那一页顶部的
  Tab（`SectionView`）：书源 = 管理 / 导入 / 检测 / 规则 / 自愈；阅读 = 找书 / 书架 /
  订阅；配置 = 更多工具 / 外观 / 服务。
- 宽窗（≥720px）走 `NavigationRail`（`extended` + `use_indicator`），窄窗走底部
  `NavigationBar`；跨阈值 `on_resize` 重建外壳但保留当前内容视图。
- **阅读组的落地页签由「架上有书」决定**：`_anchor(group)` 的次序是
  记住的页签 > 书架探测 > 分组第一个页签，所以首屏和再点「阅读」入口都**先落书架**，
  空架才回落找书。探测由装配根注入（`main._shelf_has_books` →
  `SourceService.shelved_books()`，同步读 `on_start` 已载入的内存缓存，不联网不查库），
  `NavigationHost` / `build_app_shell` 只收 `shelf_probe` 可调用——视图层不 import 服务
  这条红线不变；探测抛错记 warning 并回落找书。该规则只对同时声明「找书 + 书架」的
  分组生效，其余分组仍落第一个页签。
- `hidden` 标记的语义是「**整页目的地**」（阅读器 / 漫画 / 词典 / 导出 / 备份…）：
  既不进侧栏也不进页签，`show()` 时直接替换内容区，侧栏高亮它所属的分组。
- 切换目的地经 `ViewRegistry` 工厂重建内容区。**组内切签不重建子视图、不退订控制器**
  （在飞的搜索/检测继续跑）；只在离开整个分组或切到整页目的地时统一 `detach()` 全部
  已建页签（Controller / 仪表盘防跨页订阅泄漏）。未注册目的地直接 KeyError（侧栏无死项）。
- **整页目的地的 `show()` 是幂等的**：已经在阅读器里再 `show(reading.reader)` 一次，
  保留现视图直接返回。原先无条件重建会把上一趟在途的 `open_book` 连视图一起摘掉
  （控制器 `_view` 变 `None` → `AttributeError`，界面停在详情页），用户侧的表现就是
  "连点两下开始阅读，结果一下都没反应"。
- 同组内的跨视图跳转（如书源列表 → 规则编辑器）由 `nav_actions` 槽位落到该分组的目标
  页签并带 payload，不重跑页面装配；返回分组页时恢复「该组上次停留的页签」，整页目的地
  （开读）不计入这份记忆。
- 阅读器 / 漫画沉浸模式由各自视图内部处理。

### 5.2 ServiceDashboard（`views/service_dashboard.py`）

「配置 → 服务」页：**当前实现已全部拆分服务**——启动后注册 31 行（实测
`ServiceHub` 总控 1 + 基础设施 4 + 业务 26，业务按域为 规则引擎 1 / 阅读 6 /
书源 5 / 内容形态 4 / 工具 10），本页把每一行的状态摊开给人看。

- **目的地形态**：`DashboardView` 继承 `ft.Column(expand=True, scroll=AUTO)`，
  与 `AppearanceView` 等其余目的地同构。**绝不能返回 `ft.View`**——M6 之后目的地
  是被 `SectionView` 嵌进页签容器的，`View` 是页面级路由容器，嵌在控件树里 Flutter
  不渲染，表现就是整页灰底（静态测试全绿照不出来）。
- 服务列表：名 / 所属域 / 状态 / 进度 / 最近错误 / 启动时间 + 每行挂起·恢复·停止。
  状态用中文标签（运行中 / 已挂起 / 停止中…），域由服务类的 `__module__` 前缀归类；
  `status_report()` 快照里 RUNNING 却带 `last_error` 的行（必需协作者缺失）状态格转橙。
  列宽不定，整表包在 `ft.Row(scroll=AUTO)` 里横向滚动。
- 启停控制：经 `hub.service_by_id()` 反查实例，`suspend/resume/stop` 协程经
  `EngineHub.submit` 派发，Flet 事件线程不阻塞；按钮可用性由状态机裁决
  （RUNNING 可挂起/停止，SUSPENDED 可恢复/停止，过渡态全禁），总控行无实例故全禁。
- 事件流面板：`BaseController.subscribe` 订阅 `ServiceStateChangedEvent` /
  `SettingsChangedEvent`，处理器编组回界面循环执行；环形缓冲保留最近 50 条。
- 心跳：进度由 RSS / 下载 / 检测**就地写** `status.progress`，没有事件可订阅，
  所以挂载期间每 2s 重读快照（`AUTO_REFRESH_SECONDS`）；快照签名未变则跳过重建，
  不无谓下发整页。离页时心跳任务 `cancel()`。

### 5.3 视图与 Controller

视图 = 轻壳（Flet 布局）+ `Controller`（`controllers/`，基类 `base.py`）：
attach 时订阅 EventBus 事件，detach 退订；协程调用经 `engine_call` 排进 EngineHub；
高频事件（下载进度 / 搜索流 / 导入逐条落库）0.5s 节流在 Controller 层做。
视图不 import 服务实现类，只经 `hub.get()` 按类型取句柄（红线 v2 由 import-lint 守门）。
**凡是"跑的过程中要动的界面"都必须由服务发事件**（`ui_call` 只在收尾 flush 一次，
见 §5.6），控制器里改完控件还须 `flush`（基类在订阅包装器与 `ui_call` 收尾各挂一次）。

**事件只覆盖"在线的那段"，长任务还要在服务侧留一份可回放快照**。切页 `detach()` 退了
订阅，但流水线照常在引擎循环上跑完——那期间发出的逐源事件没人接，回到该页看到的是空
进度条，用户侧的表现就是"切个页把检测搞停了"（实际前后端拆分没问题，断的是渲染）。
落法（`SourceCheckService` + `CheckController`，同思路可用于任何长任务）：

- 服务为每轮维护 `{"total","running","cancelled","summary","results"}`，发事件的同一条
  路径把逐源结果记进 `results`，收尾把 `summary` 写进同一份；
- 读它的两个入口 **必须是协程**（`run_snapshot()` / `await_run_finished()`）：协程经
  EngineProxy 跳回引擎循环，界面线程才不会读到流水线改到一半的撕裂进度；
- 重复触发在同一处收口：`check_all` 见已有批量在跑就不再派第二遍全量联网，而是接到
  那一轮的收尾（两份流水线同时打站点对既浪费又容易被封）；
- 控制器 `on_attach` 用 `ui_call(self._restore())` 回放：逐源结果 + 进度 + 运行态，
  这一轮不是本页签启动的就继续 `await_run_finished` 等收尾再画结论。回放排在界面队列
  尾部，**可能晚于用户点「开始」**，所以 `_restore` 要先看 `self._running`，并且不画
  "从没检测过"的空快照——否则旧快照/空快照会把刚开始的运行态盖回去（探针实测：开始
  按钮又变可点）。渲染接口自身要幂等（`CheckView.render_result` 按 `source_id` 覆盖）。

第三条硬约束（关闭时序）：**登记的界面循环可能已经 close**。Flet 收窗口时先把自己的
loop 关掉，服务还在发 `ServiceStateChangedEvent`，往死循环上派发就刷一屏
`RuntimeError: Event loop is closed`（控件也已经没人渲染了，等了也没用）。
`subscribe()` 的包装器因此先查 `loop.is_closed()`：关闭即静默丢弃（debug 日志），
`ui_call` 同条件退化成引擎循环执行协程——协程本身要跑完（收尾清理、aclose），
只是没人再 await 它。

由此推出一条调用侧约定：**编排协程里每次 `await` 之后再碰视图，都要先确认视图还在**
（`self._view is None` 即 return）。切页 `detach()` 随时可能落在 `await` 的空档上，
慢源开书/加载章节实测就是这样崩在 `_set_chapters`。第八轮把这条约定补全到了所有在途
路径（阅读器偏好回填 / 书签面板 / 书内搜索 / 续读补的第二帧滚动、漫画阅读器整条线、
字体与二维码的文件选择器回执）。两条细则：

- **守卫只挡控件，不挡会话状态**：`open_chapter` 里图链解析回来后先更新
  `_page_urls` 之类的会话字段，再判 `_view`——那些字段翻回来还要用，一起丢了等于
  用守卫把功能弄坏；
- **等回执的协程分两种失败**：切页了那句话就丢，其它失败必须说（选择器被取消的回执
  仍要落进状态行），所以收进一个本地的 `say()` 而不是整段 `try/except: pass`。
  护栏：`tests/test_controller_midflight_detach.py`（替身在 await 回来**之前** detach，
  断言"不抛 **且** 一次控件都没碰"——`ui_call` 的异常只进日志，只断言不抛会一路绿）。

**接"写入型"动作之前，先确认那个字段有人渲染**。书架换封面（09-27 第十九轮）是反面教材：
`SourceService.update_cover` 在 `app/` 里零调用方，按"孤儿方法 ⇒ 接上即可"的套路补一个
FilePicker 入口就能交差，而 grep `cover_url` 在 `app/views/**` 同样零命中——书架行画的是
`ft.Icon(BOOK)`，全项目唯一消费这个字段的地方是 epub 导出内嵌封面。只接动作的那一版，
用户看到的是"挑完图、状态行说一句、屏幕什么都不变"。落法：`BookshelfView._cover_control`
把 `record["cover_url"]` 原样交给 `ft.Image(src=…)`，取不到回落原图标
（`error_content` 收 `Control`），视图不判断地址是本地文件还是远端 URL——认地址是控制器
与服务的事（红线 §7.5）；入口在长按管理对话框的「更换封面」行，控制器写完**读回书架记录**
再报成败（`update_cover` 对不上 `book_url` 时照样返回 resolved 并发事件，
拿返回值当成功就是假话）。详见 `docs/known-issues/003_refactor_dropped_orchestration_capabilities.md`
的第五型。

### 5.4 阅读器左侧抽屉（`views/reading/reader_view.py`）

书签与书内搜索是同一类"章内定位列表"，共用一个左侧抽屉（宽 320px）：抽屉内一个
`ft.Tabs`，两个页签分别挂 `BookmarkPanel` / `ContentSearchBar`，工具栏上「书签 /
书内搜索」两个入口调 `show_side_panel(kind)` 只切页签、不重建面板；传 `""` 收起抽屉。
工具栏入口顺序固定为 退出阅读 · 上一章 · 目录 · 书签 · 书内搜索 · 进度 · 字号 · 下一章
（同类动作相邻，有测试锚点）。

**Stack 覆盖层必须自带不透明底色**：目录页 / 左抽屉都是叠在正文之上、`ft.Stack` 里的
覆盖层，`bgcolor` 只能挂在 `Container` 上（0.86 的 `Column` 没有这个参数）。目录页早期
是个透明 `Column`，正文直接从章节列表背后透出来——用户报的"选章节会叠加在正文上"。
底色 `ft.Colors.SURFACE`，与漫画阅读器一致；`tests/test_views_reading_m2.py::
ReaderChapterSelectTests` 钉住「点章节 → 目录收起 + 正文换章」。

**目录必须分批渲染**（第八轮）：网文源三四千章，`render_chapters` 一次建完整份
`ListTile` 就是一次可感知的冻结（重构时旧实装的批量追加被重写没了）。现在的形状是
`DIRECTORY_BATCH_SIZE=50` + `ListView.on_scroll` 离底 `DIRECTORY_LOAD_AHEAD=320`
补一批，状态行从"已加载 50/1001 章"走到"共 N 章"。三份状态必须分开：
`_directory_all`（原始目录）/ `_directory_visible`（关键字过滤 + 正倒序之后）/
`_directory_rendered`（已挂出行数）——清空搜索框能原样回来全靠这个区分。
过滤与排序也留在视图层：目录搜索框 `_directory_query` 一度渲染出来了却没有
`on_change`（打字没反应）、「倒序」只翻标志位（要点下次开书才生效），都属于
"入口在、线没接"。护栏：`tests/test_reader_directory_batching.py`。
`on_scroll` 是**控件事件回调**，Flet 在回调返回后自动 update，所以这条路径不需要
额外 `flush`（与 §5.3 那条"事件总线/后台协程要自己 flush"是两回事）。

**目录定位只能按像素，所以行高必须钉死**（第十轮）：分批之后"打开目录停在顶部"成了
新问题（读到第 2000 章的人得自己滚过去）。0.86 的客户端在这一条上有三个反直觉点，
都是探针实测出来的（`docs/dev-logs/2026-09-26.md` 第十轮 2）：
`ListView.scroll_to(scroll_key=…)` **静默不动**（`ft.Column` 上却管用，别照搬），
`item_extent` 客户端不认，只有 `scroll_to(offset=…)` 真的移动视口、且不受"行还没建"
限制。既然按 `行号 × 行周期` 算 offset，`_chapter_row()` 就必须让每一行等高：
`DIRECTORY_ROW_HEIGHT=44` + `DIRECTORY_ROW_SPACING=2`（行距只能挂在 `ListView.spacing`
上），章名一律单行 `ELLIPSIS`。**任何一行自己撑高，它下面所有行的落点全偏**——而这
种偏移单测看不出来（算的是常量），只有浏览器里的像素与截图认得。跨控制器/视图的锚点
是 **url 而不是行号**（`focus_chapter(url)`）：过滤和倒序都会挪行号。补批要"补一批 →
`update()` → 让出一次循环"，一口气建到目标行就等于把分批要避免的那次冻结又请回来。

### 5.5 阅读器：连续续读与排版偏好（`controllers/reader_controller.py`）

**读到底自动接下一章**（Legado 连续阅读，非翻页模式）：滚动事件距文档底部不足
`_APPEND_TRIGGER_DISTANCE=180px` 时把下一章**接**在当前正文尾部（分隔线 + 章名 +
正文），不是换掉整篇——整篇替换会连带把滚动位置打回顶部，看起来像书跳回第一章。
两章之间用迟滞区间复防：退回 500px 以上才允许再次触发，一条阈值线会在边界上反复
追加。追加那次滚动事件里记下的"追加前文档总高"就是新章在整篇文档里的起点，登记进
`ReadingSession` 的视口表（`mount`），`track()` 据此把滚动偏移换算回"当前可见章"，
工具栏计数与进度落库都跟着它走。卷标题与已加载过的 url 一律跳过。护栏：
`tests/test_reader_continuous_scroll.py`（真 ReadingService + 真 reader-cache +
真视图控制器，引擎用替身，全程离线）。

**同书重复开读要去重**：`open_book` 记住 `书源|书名` 键，同一控制器上第二次开同一本
书直接返回——两趟并发加载会互相把对方的正文换掉。

**后台预取（章尾不等网络）**：每挂出一章，控制器调
`ReadingService.prefetch_chapters(chapter)` 起**一轮**顺序预取（`PREFETCH_CHAPTER_COUNT=10`
章，跳过卷标题与空 url）。这一轮跑在引擎循环上、只往缓存里放东西，绝不碰控件；
`prefetch_chapters` 本身不等网络，所以界面那一趟只多一次极轻的派发。三层护栏：

- **同章在飞去重**（`_content_flight`）：读者翻到的正是预取在跑的那一章时接上同一个
  请求，不另发一条；等待方用 `asyncio.shield` 包住——换章/切页取消的是"等"，不是
  "请求"，跑完照样进缓存（`test_cancelling_the_reader_does_not_kill_the_shared_request`）。
- **副本有预算**（`PREFETCH_CACHE_LIMIT=64MB`）：超了只丢内存副本，磁盘 reader-cache
  照写，等于安静退回旧行为。副本在 `open_book`/`close_book`/`on_stop` 一起作废。
- **失败不留痕**：预取撞墙只 `logger.debug` 并收掉这一轮（连不通的源不该让后面九次
  请求排在读者那一章前面），真翻到这一章时照常重新请求。

`load_content` 的取用顺序因此是：**预取副本 > 磁盘缓存（追加路径不读它）> 联网**。
切页（`ReaderController.on_detach`）叫停轮子但留着副本：切页不等于退出阅读，回到
阅读器时预取过的章节还算数。护栏：`tests/test_reader_prefetch.py`（服务面，引擎替身）、
`tests/test_offline_source_chain.py`（**真引擎 + 127.0.0.1 假源**，从服务端数出预取
真的发了请求）、`tests/test_reader_continuous_scroll.py`（控制器触发与复用）。

**滚动下发有两条 Flet 硬约束**（`ReaderView._run`）：0.86 的
`ScrollableControl.scroll_to` 是**协程**，调用完就丢等于界面纹丝不动 + 满屏
`coroutine was never awaited`；而 `Page.run_task` **只收协程函数**，把协程对象直接
递过去会当场 `TypeError: handler must be a coroutine function`。所以 `scroll_by` /
`restore_scroll` 把协程交给 `_run`，由 `_await_coroutine` 包一层再 `run_task`；取不到
页面（控件没上页面时 `Control.page` 抛的是 RuntimeError，不是 AttributeError）或派发
失败时**必须 `close()` 掉协程**。护栏在 `test_reader_continuous_scroll.py` 里用一个
按 Flet 规矩记账的假 page（收到协程对象就抛 TypeError）钉住这两条。

**排版（旧单体 ReadingView 的字号/行距/段距/宽度/缩进/对齐/背景/状态栏）**：
正文渲染拆成两步——`content_plan(document)` 出**纯数据计划**（切段 + 标题识别 +
缩进，不碰 ft 控件，因此可以丢 `asyncio.to_thread` 跑后台线程），`render_plan(plan)`
在界面循环上照计划建控件。清洗（`render_use_html_spans`）与切段都在后台那步完成。

改偏好（含字号 ± 按钮）走 `apply_preferences`：合并 + 夹取范围后**在已挂出来的控件上
就地重排**（认 `data="reader-text"` / `data="reader-heading"` 标记），绝不重新拉取正文
——重渲染会把自动续读接上的多章文档换成"只有当前章"，滚动位置一并丢。可用区间：
字号 13-28、行距 1.3-2.2、段距 4-24、正文宽度 620/700/760、背景 5 色（跟随主题 /
纸黄 / 护眼 / 夜间 / 墨水墨绿）。偏好经 `ReadingService.save_reader_preference` 落库，
**attach 时读一次**（进程级，不必每本书重读），存的是夹取后的值。
**键名是一条命名空间约定**：阅读偏好一律 `reader.<ReaderView 偏好名>`
（`ReadingService._READER_PREFIX` + `ReaderView.DEFAULT_PREFERENCES`）。别的模块想改阅读
设置就必须写同一个键——字体页以前写 `font.family`，阅读器读 `reader.font_family`，
于是"选了字体、正文永远不变"；WebDAV 备份的白名单同理列了一套假键，偏好永远备不出去。
新增偏好名时三处（视图默认表、其它写入方、`WebDavService._export_settings`）一起对齐，
最后这一处现在有护栏：`PreferenceBackupWhitelistTests` 拿"对任何键都答存过"的替身设置跑
一遍导出，正反两个方向钉住白名单与偏好表一一对应（`shortcuts`/`tts_rate` 由控制器写，
是显式许可的例外）。
「阅读设置」弹层由视图 `build_settings_dialog` 造、控制器 show/pop。护栏：
`tests/test_reader_typography.py`。

**划词查词典**（§1.8）：正文 `ft.Text` 带 `selectable` + `on_selection_change`，选中变化
只把词亮到 `ReaderView.selection_bar`（那条「查词典」条），**一次拖拽一次网络都不发**——
一次拖动会发几十次 selection_change，每条都去抓释义页等于对着词典站点做 DDoS。点按钮才
经 `ReaderController.lookup_word` → `DictService.lookup` → 回填视图搭好的释义弹窗
（`build_definition_dialog` / `update_definition`，先搭后填）。三处刻意的行为：整篇换章
收起划词条（选区已经不在了，留着是假线索；追加下一章不动它）、弹窗换词后迟到结果按词号
丢弃（晚到的 A 词释义不许贴到 B 词弹窗上）、`DictService` 未注册时直说"词典服务未就绪"。
偏好键 `reader.text_selection` 可整条关掉划词（关掉同时收起已亮的条）。
**关掉的理由是真实的**：按住拖动选中与滚动在同一块正文上抢手势。
护栏：`tests/test_reader_text_selection_lookup.py`（选中不外呼 + 迟到结果不串窗两条性质）。

**详情对话框只留一层**：`FindBooksController.show_detail` 若已有开着的对话框就直接
返回。双击搜索结果会叠出两层一模一样的详情框，点「开始阅读」只 pop 掉最上面那层，
阅读器开好了屏上还盖着一层（`_detail_dialog.open` 判活）。

### 5.6 导入页：预览 → 确认 → 进度（`views/source/import_view.py`）

导入是**破坏性最强**的入口（一份文件能覆盖整张书源表），所以链路上刻意留了两道闸：

1. **预览卡**：选完文件只读不落库，由 `preview_book_source_text`（OnlineImportService
   模块函数，纯解析不经引擎代理）给出「总条数 / 可导入 / 缺 `bookSourceUrl` 跳过 /
   分组分布」。一条都读不出来（选错文件，典型是把 TXT 目录规则当书源）直接算
   `error`，界面只回一行说明，**不给一张"可导入 0 条"的卡再让用户点一次确认**。
2. **模式选择**：追加（按 `bookSourceUrl` 去重并进现有列表）/ 替换（整表覆盖）。
   替换在控制器侧再弹一次 `AlertDialog` 确认，并写明"将覆盖现有 N 条"（N 读真实
   服务侧列表）；拿不到 `page` 时**拒绝执行**而不是退回追加——破坏性动作宁可不作。

进度可见性走**事件**不走回调：服务侧逐条落库发 `ImportProgressEvent`
（`kind/phase/done/total`，`SourceService` 内 0.2s 节流、收尾强制补发最后一拍），
URL 拉取阶段发 `total=0` 的不定长一拍；控制器订阅后在界面循环上刷进度条。
不走"服务回调里直接改控件"，因为 `ui_call` 只在编排协程收尾时 flush 一次，
中途改的控件要等整表落完才下发（用户感受：进度条一直空、跑完好才被 100% 覆盖）。
小文件（几十条）跑不满一个节流窗口，看到的就是「不定长 → 1.0 → 收起」，属预期。

界面灰框收敛：旧版「资源类型」下拉列 7 项只有 1 项能导入（点了只回一句说明），
和 URL 输入框并排看着像三块没内容的灰砖——类型不进界面（服务侧 `kind` 仍是参数），
URL 行只留「输入框 + 导入按钮」，预览卡与进度卡**只在有内容时出现**。

**「导入即审」只有一处归属，在落库层**（第十一轮）：`SourceService.import_sources_json`
是所有导入路径唯一的漏斗（在线/本地门面、扫码导入、WebDAV 恢复三条都走它），所以
§3.7 触发点① 默认由它逐条过 `CompatAuditService.audit_source`。门面那条路例外——它要
把兼容性缺口写进 `ImportResult.warnings` 给界面看，于是自己先审一遍，再传
`audit=False` 让落库层闭嘴；审计服务没就绪时反过来（门面审不动，`audit=True`）。
**为什么非得钉死**：重构后两条链都在审，几百条的源文件白审两遍，更要紧的是每次审计
（含干净源）都往 `audit_log` 落一行——一个源两行，回溯时认不出哪行属于这次导入。
`audit` 是关键字参数且默认 `True`，新加的导入入口不传就等于有审计。护栏：
`test_import_audit_runs_once_per_source_not_twice`（门面侧数审计次数）+
`test_import_sources_json_audit_false_skips_the_auditor`（落库侧认开关）。

### 5.7 外观：主题三件套与实时预览（`views/settings/appearance_view.py`）

`ThemeService.apply()` 只给**纯颜色数据**（`ThemeData.light/dark` 两套角色字典），
把它落成 Flet 主题的全部逻辑在视图层 `apply_page_theme(page, data)`。

**不变式：主题必须三件套一起下发**——`page.theme` 只在**亮面**模式下生效，
`page.dark_theme` 管暗面，`page.theme_mode` 决定用哪一面（`SYSTEM` 时跟操作系统）。
重构后只设了 `page.theme`、`theme_mode` 留在默认 `SYSTEM`、`dark_theme` 从不赋值，
在深色桌面上就是"换皮肤/切深色毫无反应，整页仍是 Flet 默认灰"。三条配套：

- `ft.Theme` 除 `color_scheme` 外还带 `use_material3 / scaffold_bgcolor /
  card_bgcolor / divider_color`——少了 `scaffold_bgcolor` 时窗口底色由默认主题给，
  换皮肤只改到按钮，看不出"皮肤换了"。
- **墨水屏**两面共用同一套纯黑白，并强制 `theme_mode=LIGHT`（暗面桌面上深色底 +
  纯黑字会糊成一片），另给 `visual_density=COMPACT`。
- **启动即应用已保存偏好**（`main._apply_saved_theme`，见 §3 第 3 步）：装配根把服务
  数据交给视图层函数，视图不 import 服务（红线 §7.5）。

预览区是**一小组真控件**（FilledButton / OutlinedButton / Chip / TextField / Divider），
配色完全来自已下发的主题——预览即所见。它曾经是三块手写 `bgcolor` 的色板，还用
`ft.Container(expand=True)` 塞进 `ft.Row(wrap=True)`：wrap 与 expand 互斥，整行渲染成
一片灰色区域，且色板只涂三块底色，看不出控件会怎么变。

> Flet 0.86 这一类"表面 API 与记忆不一致"的坑**统一记在
> `docs/known-issues/001_flet_0_86_surface_breaks.md`**（症状 / 根因 / 复发时先查），
> 本文不另列清单，免得两处各自走样；那类缺陷由
> `tests/test_controller_service_surface.py` 的 AST 静态扫描在导入期就挑出来。

护栏：`tests/test_appearance_theme.py`（18 项：三件套下发 ×5、五皮肤 ×亮暗真构造
`ft.Theme`、预览区结构静态扫描"wrap Row 里不许有 expand 子控件"、控制器 pending→预览
不写库 / 预览失败必须写状态行、启动引导）；端到端取证走离线界面探针（真点：亮面
`(255,251,255)` → 深色 `(20,18,24)` → 墨水屏 98% 像素落在黑白两极；换皮肤后主按钮均色
`(84,100,189)` → `(69,139,123)`）。

### 5.8 「更多工具」七页的接线契约（`views/tools/**` + `controllers/tools/**`）

这七页（导出/替换/字体/词典/备份/二维码/更新）的服务**全都注册在 `main.py`**，历史上
"点了没反应"没有一条是服务缺失，断点全在接线。六条契约（护栏集中在
`tests/test_tools_chains.py`，40 项，全部吃**真 Flet 控件**）：

- **下拉/清单的值是 key，不是中文标签**：视图按 `(key, 文案)` 二元组建
  `ft.dropdown.Option(key, text)`，把 `option.text` 传下去等于服务收到「词典」两个字后
  什么也不做（备份的 `kind` 以前就只是事件标签，选什么都整库上传）。
- **能力要有调用方**：`list_exportable_books` / `list_remote` / 词典规则 CRUD 都曾建好
  了零调用方。新增服务方法时同一次改动里必须指出界面入口，否则它算未落地。
- **对话框用 `controller._page()`**：视图未挂载时 `view.page` 抛 `RuntimeError`（不是
  `AttributeError`），`getattr(view, "page", None)` 挡不住。拿不到 page 时**拒绝执行**
  而不是"问不了那就不问了直接干"（`WebDavController.restore` 返回"界面尚未就绪，
  本次恢复没有执行"）——闸的口不能开在异常分支上。
- **备份成员是跨服务契约**：`WebDavService` 鸭子探测
  `export_{sources,rules}_json` / `import_{sources,rules}_json`，
  `SourceService` / `ReplaceRuleService` / `DictService` / `RssService` 各自实现一对
  （导出去掉本地 id 与时间戳；导入按自然键去重、返回 `(新增数, 告警)`）。少一个方法，
  `BACKUP_TARGETS` 里对应那个 kind 就永远「没有可备份的内容」。
- **高风险写的闸在服务侧，不在界面侧**（第十二轮）：`WebDavService.restore(url, token,
  confirm=False)` 未确认时在**解析客户端之前**就返回，PROPFIND / GET 一个都不发。界面
  只负责"问一句"（模态框列包名、将被覆盖的书源条数、"此操作不可撤销"），因为绕过界面的
  调用方（脚本 / 网络桥 / 仪表盘）也得被挡住；同理 `confirm=True` 是调用方的显式声明，
  默认值保守。落库前先把**将被覆盖的那几样**现状抄进
  `<数据根>/restore-safety/<时间戳>/`（不放 cache；只抄与包成员名相交的那些；同一秒内
  两次恢复用 `_1` 后缀避让），路径随 `BackupResult.snapshot` 报进结果行。留底是
  **保险丝不是关卡**：任何失败只 warning + 照常恢复，不能反过来成为"点了没反应"的新理由。
  在途期间第二次确认直接拒——那一次的留底抄的是第一次恢复完的样子，真旧数据已经没了。
  护栏：`test_restore_without_confirm_never_downloads` 等 4 条（服务侧）+
  `test_tools_chains.py` 5 条（控制器侧：填路径不恢复 / 确认才派发 / 取消不派发 /
  在途拒绝 / 无 page 拒绝）。
  **假绿教训**：这条闸的第一版护栏用"假客户端抛错 + 断言 `"确认" in detail`"，把
  `if not confirm:` 短路掉**仍然绿**——抛的错被服务 `except` 吞成
  "下载备份包失败: 未确认的恢复不该下载备份包"，错误消息正好喂饱了断言。钉"这件事没发生"
  （`client.calls == []` + 闸门自己的措辞），不要钉"结果是失败的"（失败的原因有一百种）。
- **服务侧不校验的，界面必须当场拒**：`tts_profiles`（`tools.tts` 页，第九轮）的
  `add_profile` 什么都能存，而 HTTP-TTS 的地址模板缺 `{text}` 时服务只会照原样拼一次
  地址——第一条念完就再没声音。这种"存进去才发现是坏的"要在对话框里挡住，并且**话写在
  框里**（状态行在模态框背后看不见）、**不关框**（关了等于把用户填的内容一起扔了）。
  护栏：`tests/test_tts_profile_page.py`。

### 5.9 内容形态与网络能力的界面承接（本地书 / 网络桥 / RSS / 朗读）

这四条在 M4/M5 只有**服务面**：能力都在、事件都在，就是没人点得到。补界面时发现
的坑都在"界面与服务之间那层"，与具体功能无关：

- **本地书**：地址形态（`localsource://` 伪源、`file://` 书地址）只认在服务层
  （`local_book_service.is_local_source_url`、`ReadingService._local_source_for`），
  视图**不认得这个前缀**——控制器把 `local: bool` 随记录注进渲染字典，视图只据此收起
  「换源 / 整书缓存」两个对本地书无意义的入口。既然 `_to_render_record` 写键，就必须
  先 `dict(raw)` **复制**：`list_bookshelf()` 发的是 SourceService 内存里的那批 dict，
  直接写等于把界面派生字段落进 `bookshelf.json`。本地书重开靠
  `_local_source_for` 补一个只带 `bookSourceUrl` 的合成源对象，reader-cache 的键才命得中
  （查不到规则源不等于书坏了）。护栏：`tests/test_local_book_import_chain.py`、
  `tests/test_local_book_m4.py`。
- **网络桥**：`WebBridgeService`（回环绑定 + 令牌 + 默认不监听）此前唯一的入口是服务
  仪表盘上"停止整个服务"那颗按钮——地址看不懂、令牌拿不到。设置页
  （`views/tools/web_bridge_view.py`）只吃 dict 状态，写动作全走服务向导方法
  （`set_enabled` / `set_port` / 重新生成令牌），端点清单由服务侧导出而不是在界面里
  抄一份。护栏：`tests/test_web_bridge_settings_page.py`。
- **RSS**：阅读组第三个页签「订阅」（`RssView` 三栏：源 / 文章 / 正文）。刷新进度
  **必须走事件**（`RssRefreshProgressEvent`，见 §5.3 那条红线），不然逐源刷新期间界面
  停在 0；未读计数一次 `article_counts()` 的 GROUP BY 全量取出，不在界面侧按源逐次问。
  HTML→纯文本（BeautifulSoup，剥 `script`/`style`）在**控制器**做，视图只收文本。
  删源走确认对话框。护栏：`tests/test_rss_reading_page.py`（服务面另见
  `test_rss_m4.py`）。
- **朗读**：`ReadAloudBar` 挂在阅读器正文与状态栏之间，由 `ReaderController` 编排。
  五条不显然的：
  1. 粒度是**章**——系统 TTS 后端不上报位置（`ReadAloudStateEvent.position` 恒 0），
     所以播放条没有进度条也不做逐句高亮，只显示"正在念哪一章"；
  2. 朗读循环是一条长协程，而 `ui_call` 只在收尾 flush 一次，所以每章开念前必须
     就地 `set_state` + `flush`，否则连读时播放条永远停在第一章；
  3. 停止用**换号**（`_tts_generation`）而不是取消任务：再按一次、手动翻章、切页
     离开阅读器三条路径都换号并叫停后端，在途循环回到检查点自己收工。
  4. 语速档位（`RATE_PRESETS`，慢/正常/快/很快 → pyttsx3 词/分钟）落
     `reader.tts_rate`，与快捷键同类属"进程级读一次"的偏好（不参与重排）；**只有系统
     TTS 后端吃这个参数**，而服务自己会回退（system→edge→http），所以 `speaking` 态的
     标签必须带上 `active_backend.name` 的中文名——不显示是谁在念，用户就是对着一个
     不吃这参数的后端调语速，"调了没反应"重新变成查不出来的静默退化。
  5. 音色下拉的选项来自 `tts_profiles` 表（管理面在 `tools.tts`，见 §5.8），
     **key 是行 id 不是中文名**，首项 `BUILTIN_VOICE`（`builtin`）表示不启用任何语音档。
     选中之后的三样接法（`engine_type` / `url` / `headers`）必须一起进 speak 的 `opts`：
     `set_voice()` 只负责切后端，带不齐就是"选了一条不存在的引擎"。默认档落
     `reader.tts_profile`。正在念时改音色**不打断**当前这句（opts 每章算一次），
     标签要写"从下一章生效"。表里那条档被删了则下拉退回 `builtin`，不留幽灵值。
  `ReadAloudStateEvent` 的渲染按「当前书 + 当前章」过滤，上一章迟到的 `idle` 不能把
  界面从"正在朗读"拽回去。服务在 `hub` 里缺席时整条播放条不出现
  （`_safe_service`）。设计 §1.1 设想过**全局覆盖层**形态（切页继续念）：阅读器是整页
  视图、切页即拆，声音继续放着却没地方按停比安静更糟，因此**有意**做成随视图停。
  护栏：`tests/test_reader_read_aloud_page.py`（服务面另见 `test_read_aloud_m4.py`）。

### 5.10 书源登录：Legado 的三种形态（`views/reading/login_dialog.py` + `controllers/find_books_controller.py`）

引擎面早就按 Legado 实现了（`LegadoReadingEngine.login_schema / execute_login_action /
submit_login / login_target`，`tests/test_legado_compat.py` 有对拍），断的一直是界面侧的
**分流**。对照 `base_history/legado-3.23.110211/.../ui/login/SourceLoginActivity.kt`
（`initView`）与 `SourceLoginDialog.kt`，登录只有三条路，桌面端必须一样分：

| 形态 | Legado 判据 | 桌面端 |
| --- | --- | --- |
| ① 有表单 | `loginUi` 非空 | 铺输入框（`text`/`password`）**并回填 `source.getLoginInfoMap()` 的已存值**；`type:"button"` 的行铺**按钮**，点它只执行该行 `action`（带当前表单值进 `result`） |
| ② 网页登录 | `loginUi` 空 | Legado 开 `WebViewLoginFragment` 加载 `loginUrl`；桌面端**报地址**（可复制）+ 一个**显式**的「清除已存登录信息」 |
| ③ 脚本登录 | 无 `loginUi`、`loginUrl` 不是地址 | 直接把源的登录脚本跑一遍，结果写状态行，不弹框 |

三条契约（第十二轮之后由第十三轮补齐，护栏 `tests/test_source_login_chain.py`）：

- **`loginUi` 的行类型不是只有输入框**：控制器早先只映射 `key/label/hint/password`，
  于是 `{name:"登 录",type:"button",action:"Login()"}` 变成了一个标着"登 录"的**空输入
  框**，它的 `action` 被整行丢弃——靠按钮登录的源（扫码 / 过验证码 / 免密跳转那类）在
  桌面上根本登不进去，表现为 `execute_login_action` **生产零调用方**。行的 `action`
  非空就渲染按钮并绑 `on_action`，绝不进 `_collect_fields()`。
- **已存值只在引擎里，界面不接就等于没有**：`login_schema` 已经把
  `item["value"]` 填好（Legado 是 `editText.setText(loginInfo[name])`），控制器映射时
  漏读这个键 → 每次打开全空、密码要重打。
- **②那种源不能走表单这条路**：引擎的 `submit_login(source, {})` 语义是
  `source.removeLoginInfo()`（这条**照抄 Legado 是对的**——它的 `login()` 里
  `loginData.isEmpty() → removeLoginInfo()`），错的是界面给一个没有字段可填的源弹了
  一张空表单：用户点「登录」= 把自己的登录信息删了。所以"清除"必须是**看得见的那个
  按钮**（`show_web_login` 里连提交按钮都不出现），不是某个分支的副作用。
  同 §5.8「对话框用 `controller._page()`」那条：拿不到 page 就不弹、不执行。
- **网页登录要做成合法形态还差 Cookie 回流**：`webview_fetcher` 对登录页只是无头抓一次
  HTML，登录态不会回到该源引擎的 cookie 罐；JS 桥的 `browser_starter` 钩子在全项目
  **无人赋值**。所以 ② 目前只报地址（用户自己去「工具 → 内置浏览器」登录），
  就地登录 + 回流是记在 `docs/dev-logs/2026-09-26.md` 遗留里的服务侧缺口。

服务侧新增的两个转发是这条的接口面：`RuleEngineService.login_target(source)`
（同步纯函数，不碰引擎池，界面用它决定铺表单还是报地址）与
`run_login_action(source, action, values)`（转 `execute_login_action`）。

### 5.11 事件投递是异步的 + 进度事件覆盖现状

**`EventBus.publish` 不等处理器跑完**（`core/bus.py:46-53`）：它先在锁内抄一份处理器
快照，然后对**每个**处理器调一次 `self._engine.submit(...)` 把 `_invoke(handler, event)`
排进 EngineHub 的执行域，立刻返回 future 列表——调用方（发事件的服务）当场就返回了，
处理器稍后才在另一条循环上被执行。三条别人必须遵守的后果：

- **测试要"等投递"，不能发完就断言**：`publish` 返回时处理器一次都还没跑。要断言
  "界面收到了这个事件"，必须显式等事件真正落地（`_wait(...)` 之类的自旋等待），而且
  **等的那条流必须就是被断言的那条流**——§2 基线里那条用例当日就栽在"等了一种事件、
  却断言另一种事件的最后一拍"上（已按这条改对，改后单跑 6 次全绿）。
- **`detach()` 之后处理器仍可能被调到**：订阅退订只是把 handler 从集合里摘掉，**已
  经排进去的任务不会被撤回**。切页落在 `await` 空档上时，控制器 `subscribe` 的 handler
  完全可能在 `detach()` 之后才执行——所以**碰控件的事件处理器必须再查一次
  `self._view is None`** 才动控件，与 §5.3 那条编排协程约定同源，不是可选项。
- **同一事件的多个处理器之间没有顺序保证**，跨事件类型之间同样没有。要顺序就把它做成
  一个事件里的一个列表字段，别指望"先发的一定先被处理"。

**进度反馈的事件化覆盖现状**：长任务里发**进度**事件的有六处——
`DownloadProgressEvent`（整书离线缓存，`reading/download_service.py`）、
`ImportProgressEvent`（导入，`sources/source_service.py` 逐条 + `sources/online_import_service.py`
阶段）、`RssRefreshProgressEvent`（RSS 批量刷新，`content/rss_service.py`）、
`RepairProgressEvent`（正文规则自愈的阶段行，`sources/rule_repair_service.py`）、
`BackupProgressEvent`（备份/恢复的导出 → 校验 → 压缩 → 上传 → 落库逐拍，
`tools/web_dav_service.py`）、`ExportProgressEvent`（整本导出 epub/txt 的逐章拍，
`tools/book_export_service.py`）。后三条是近两轮补的：补之前它们只发收尾事件，
十几秒的过程里界面一潭死水。

导出这一条还多一个别人没有的坑：**它通体是同步的磁盘/CPU 工作，全程不 await**。
`publish` 只把处理器排进引擎循环，所以"发了事件"和"处理器跑了"之间差一次让出——
不让出，几百章攒下的进度拍会全部堆到导出返回之后，界面就是"条不动，然后一下子完成"。
`_paced` 每章 `await asyncio.sleep(0)` 就是干这个的（有护栏：慢缓存 + 同构派发总线，
变异核验拿掉 sleep 立刻红）。**任何"同步重活 + 进度事件"的组合都要照这条写。**

还差什么（下一轮的真实起点）：

| 长任务 | 现状 | 缺什么 |
| --- | --- | --- |
| 换源扫描 / 书源检测 | 有 `CheckFinishedEvent`，检测页进度走 `SourceCheckService.run_snapshot()` 轮询 | 跨页续跑时进度只在快照里，切走再切回靠轮询补 |

**「批量替换规则应用」不在这张表里，之前列进来是误判**：替换规则不是"批量任务"。
用户改的永远是规则表（`ReplaceRuleService` 只做 CRUD + 启停，改完发
`ReplaceRulesChangedEvent`），正文里的替换发生在阅读链路上——
`reading_engine.process_book_content` 每章就地调
`get_global_replace_rules(scope)`（provider 由 `set_global_replace_rules_provider`
在 `on_start` 注入）。项目里根本没有"把规则应用到全部书源/全部章节"这个动作，
所以也就没有可报的进度。

形状约束（补新事件时照这条走，服务侧发事件、不在服务里改控件）：**低频的阶段事件
不要节流**——0.5s 合并是给逐条高频事件的（下载进度、搜索结果流），阶段事件一共就
十几拍，节流反而会把最后几拍推到收尾之后，界面上就是"结果都出来了进度条还在转"。
跨页切走的还要在服务侧留一份可回放快照（`run_snapshot()` / `await_run_finished()`
那对协程入口）。新增进度事件时记得同时补进 §4.3 的事件清单。

### 5.12 视频书：一个播放面、懒建控件、跨 loop 编组（`views/reading/video_play_bar.py` + `controllers/reader_controller.py:1122-1330`）

正文里的视频块（书源规则产出的 `{{video.…}}` / `<video>` 归一化结果）过去由
`reader_view` 直接 `import flet_video` 自建控件——**每个视频块一个播放器，且完全绕过
`MediaPlayService`**，于是服务面上那套"播放列表 + 记忆位置 + 后端抽象"在界面上没有
承接方（M5 核对表 1.4 那格 ✗ 就是这么来的）。现在只有一条链：

`ReaderView._collect_videos()` → 条目行「在本页播放」→ `ReaderController.play_video` →
`MediaPlayService.open_book/play_chapter`（`FletVideoBackend`）→ `VideoPlayStateEvent`
→ `_on_media_play_state` 镜像回 `VideoPlayBar`。七槽绑定见 reader_controller.py:201-207。

四条别人必须守住的实现约束（都是这一轮踩出来的）：

1. **全场只有一个 `ftv.Video`**，挂在面板上（`ReaderView._video_control`）。两处各放一
   个播放器 = 一个出声一个出画，且服务的位置回读会指错对象。音频块不受这条约束，仍在
   正文里就地 `flet_audio`（它是"章内一段"，不是"一部片"）。
2. **`flet_video` 懒建**：原生插件，读小说的人不该在打开阅读器时就付它的加载成本；
   控件只在第一次起播时 import + 构造。
3. **先上屏再下发**：`playlist` 赋值和 `play()` 必须在控件已有 `page` 之后发，否则命令
   落进空管道。服务侧的顺序是 open → play → seek，位置回读用 `get_current_position()`。
4. **服务在引擎循环、控件在界面循环**：控制器碰控件一律走 `ui_call` / `ui_await`
   （§5.3、`engine-hub-loop-split`）。测试与探针要**自己** `set_ui_loop(...)`——生产由
   `build_app_shell` 登记，绕开装配层的探针不登记，则 `ui_call` 退回引擎守护循环，
   界面一帧也收不到，看着像"点了没反应"。

播放进度与阅读进度**分列**存储（v7 `books.play_chapter_index` + `play_position`，见 §6）；
书架行"读到第几章"仍只跟阅读进度，不随播放推进（差距已记 §1.4）。`ended` 未接控件事件，
所以**不会自动连播下一条**。护栏：`tests/test_reader_video_panel.py` + `test_media_play_m4.py`；
真实挂载面由离线浏览器探针取证（Playwright 点语义树 + 回环假视频源，见
`flet-web-probe-ui-verification`），静态测试桩掉了 `ftv.Video` 这一面。

## 6. 存储（三类介质，设计 §6.2 落地态）

| 介质 | 内容 | 位置 |
| --- | --- | --- |
| SQLite 主库 `legado.db` | 全部结构化数据，StorageService 独占，schema 版本化（当前 v7） | AppPaths.data（Windows `%LOCALAPPDATA%`，Linux/macOS XDG；可用 `LEGADO_BOOKSOURCE_DATA_DIR` 覆盖） |
| 文件目录 | `reader-cache/`（章节正文）、`image-cache/`（封面/漫画图）、`downloads/`（导出/下载产物）、`chromium-profile*/`（浏览器登录态） | AppPaths.cache / data；可用 `LEGADO_BOOKSOURCE_CACHE_DIR` 覆盖 |
| 互操作 JSON / zip | 书源导入导出、WebDAV 备份包——运行时不读写，仅在导入导出/备份恢复时生成与消费 | — |

主库 v7 表清单（`app/storage/schema.py` 为权威）：

- v1：`settings`（键值）、`cookies`（JS 罐持久层，对标 Legado CookieStore）
- v2：`books`（书元数据 + 连续滚动进度）、`bookmarks`
- v3：`audit_log`（审计历史）
- v4：`tts_profiles`、`rss_sources`、`rss_articles`（已读/收藏折叠为行内 0/1 列）、`books.play_position`（秒）
- v5：`replace_rules`、`dict_rules`、`fonts`、`download_tasks`
- v6：`book_sources`（json 列存互操作全文）、`bookshelf`（reading/vars/payload JSON）、`search_books`（换源种子，TTL 24h）
- v7：`books.play_chapter_index`——**播放进度不再占用阅读进度**。v4 起播放位置借用了
  `chapter_index` 表示"播到第几条"，于是视频书"读到第几章"会跟着播放走；播放列表索引
  从此单列一列存（`play_position` 留在原处不动）
- 运行时建：`content_search`（FTS5 虚表，ContentSearchService）

M6 迁移：启动时 `StorageService.migrate()` 从旧 `bookSource.json` / `bookshelf.json` /
`search-books.sqlite3` / appearance 等**只读导入一次**（幂等标记 `m6.legacy_migrated`），
导入成功旧文件改名 `.legacy-bak` 保留回退；异常介质不改名、不阻断启动。

## 7. 关键行为对齐（延续旧文档有价值段落）

### 7.1 规则求值三层结构与 Legado 兼容层

parser（语法/选择器工具）→ rule_engine（通用求值管线）→ reading_engine（阅读专用路径）。
搜索/探索走基类，阅读走子类；`@js:` 规则一律 QuickJS（legado_js），Python 侧不做 JS 解释。
行为对齐做到行号级（`docs/找书与换源逻辑对齐.md`），守门靠
`tests/test_legado_compat.py`（2026-09-27 实测 488 条 `def test_`，核心资产零漂移为通过）；
对拍的判据来自只读参考树 `base_history/`（Legado）与 `other_history/`（jsoup / rhino），
两者都不参与运行时依赖。⚠ 本节旧版点名的差分工具目录 `tools/legado_diff/` **在当前
工作树里已经不存在**（整个 `tools/` 连同 jsoup / rhino / charset / legado 四套 oracle
一起被移除，`HEAD` 里才还看得到），因此当前唯一的机械守门就是上面那个测试文件。
M1 网络组修复已落地：三档超时 / dnsIp 钉 IP / Cookie 单罐入 SQLite / deflate 解压 /
连接失败自动重拨 / 每主机并发 5 / 全局 UA 可配 / charset-normalizer 兜底探测。

### 7.2 搜索与换源模型

`SearchModel` 完整移植 Legado 生命周期：searchId 区分新搜索/翻页、页码模型内维护、
并发上限 32、模型层每源 30s 兜底超时；四桶合并 + origins 聚合。结果落主库
`search_books`（TTL 24h）。换源"先读本地种子、为空才用户触发联网扫描、候选排序"；
命中判定用书名包含。notShelf 语义不变（试读 `in_bookshelf=0` 不进书架，退出选否删记录，
启动清理残留）。

### 7.3 浏览器分级

`{webView:true}` 与 `webJs` 需要真实浏览器：无人值守走内置无头 Chromium（Playwright）；
需人处理在 Windows/Linux 走 CDP 投屏窗口（屏外停放 + screencast 推帧 + CDP input 回灌），
macOS/移动走 `ftw.WebView`。统一收编进 `BrowserService`，profile 目录隔离。
内核缺失时 `available_fetcher()` 返回 None，检测与阅读退回非浏览器路径、不报错。

**投屏会话只以 session id 跨过循环边界**：playwright 的 page / screenshot 对象绑在
EngineHub 专属循环上，视图层拿到句柄后从界面循环 `await` 一次——**不是抛错，是永久挂死**。
所以 `BrowserController` 手里只有 `open_page()` 返回的 `sid`，一切动作以协程形式转给服务
（`navigate_page` / `cast_gesture` / `cast_viewport` / `close_page`），手势名过 `GESTURES`
白名单；帧字节是唯一递到界面的东西（`on_frame` → 视图 `set_frame`）。装配根
`main._engine_browser_fetchers(browser)` 把**同一个** `BrowserService` 实例的
`render_webview` / `verification_fetcher` 绑进 `RuleEngineService`（内核缺失时给
`(None, None)`），`_wire_browser_page` 提供 page 给投屏面板。护栏：
`tests/test_browser_render_chain.py`（含装配身份断言：注册实例 == fetcher 的 `__self__`）。

## 8. 历史对照（旧单体 → 新架构映射）

M7 清理线**真正删掉的是旧单体 UI**（`app/app.py`、旧平铺 `app/views/*.py`、
`app/ui_feedback.py`）和被搬进域包的那批旧平铺服务；其余 **14 个旧平铺
`app/services/*.py` 全部仍在树内，而且是在跑的基座**——各域外壳经"继承类 / 直接调模块
函数 / 懒 import"复用它们，删掉任何一个都会打断生产链。三张表分别记这三种命运，
不要当成一处已经清空。

### 8.1 已删除（旧文件确实不在了）

| 旧（已删除） | 新（当前） |
| --- | --- |
| `main.py`（旧）+ `app/app.py` 三页装配 | `main.py`（_build_hub）+ `app/views/navigation.py: build_app_shell / NavigationHost` |
| `app/ui_feedback.py` / `app/models/appearance_settings.py` | 旧单体那层统一反馈随之移除，面向用户的反馈现在由 Controller 落状态行 / 弹层（见 §5.3、§5.8）；外观偏好改以快照形式存进 `core/settings.py`（见 §5.7） |
| `app/services/rule_engine.py` / `reading_engine.py` / `legado_rule_parser.py` / `legado_js.py` / `java_regex.py` / `icu_charset.py` / `custom_url.py` / `public_suffix.py` / `query_ttf.py` | **整体搬进** `app/services/engine/`（9 核心资产原位冻结）+ `rule_engine_service.py` 外壳；平铺旧副本已删 |
| `app/services/engine_worker.py` | `app/core/engine_worker.py`（升级为全服务执行域 Service）；旧垫片已删 |
| `app/services/book_cache_service.py` | `app/services/reading/book_cache_service.py`；旧副本已删 |
| `app/services/reading_progress.py` | `app/services/reading/reading_service.py`（进度入 books 表）；旧副本已删 |
| `app/services/theme_service.py` | `app/services/tools/theme_service.py`（外观偏好并入 SettingsService）+ `app/views/settings/appearance_view.py: build_theme / apply_page_theme`（旧 `apply(page, settings)` 里"页面三件套 + `_make_theme`"那半边的落点，见 §5.7）。⚠ 同一条里被点名的 `app/themes/builtin_themes.py` **没删**，见 §8.2 |
| `app/views/reading_view.py`（172KB 单体） | `app/views/reading/`（find_books / bookshelf / reader / manga / bookmark_panel / content_search_bar / login_dialog / source_switch_dialog / read_aloud_bar / rss_view） |
| `app/views/{book_source_view,manage_view,import_view,check_view,rule_repair_ui,settings_view,appearance_view}.py` | `app/views/source/` + `app/views/tools/` 各域视图 + `app/views/settings/appearance_view.py` + `service_dashboard.py` |

### 8.2 仍在用为基座（14 个旧平铺 `app/services/*.py`，均未删除）

这些是**活的依赖**，不是待清的遗留。归属方按 2026-09-27 实测的 import 站点列出。

| 仍在用的旧平铺模块 | 谁在用它（复用方式） |
| --- | --- |
| `source_service.py` | **被继承**：`sources/source_service.py:33` 取作 `_LegacySourceService`，`:46` `class SourceService(_LegacySourceService, Service)`——方法体原样复用，壳只加事件与主库读写 |
| `check_service.py` | **被直接调用**：`sources/source_check_service.py:29` import 模块本体，`:86` 用 `check_service.check_sources`、`:72` 用 `check_service.CHECK_CONCURRENCY`；`main.py:125` 与 `controllers/source/check_controller.py:23` 也引它 |
| `rule_repair.py` | `sources/rule_repair_service.py:26` import `RepairReport` / `diagnose_content_rule` |
| `compat_audit.py` | `sources/compat_audit_service.py:29` import 为 `ca`（`KNOWN_RULE_FIELDS` / `SUPPORTED_JAVA_APIS` / `scan_missing_java_packages`） |
| `headless_browser.py` | `tools/browser_service.py:154`（`chromium_installed`）/`:311`（`BROWSER`）/`:321`（`BrowserUnavailable`）懒 import；`check_service.py:13` 亦依赖 |
| `interactive_browser.py` | `tools/browser_service.py:258`（CDP 投屏会话工厂） |
| `web_page_renderer.py` | `tools/browser_service.py:337`（`render_webview`：webView / 验证码生产路径）。⚠ **全仓库唯一被授权 `import flet` 的服务层文件**（`:20` flet、`:89` flet_webview），例外与其白名单见 `docs/architecture-rules.md` 红线 1 |
| `search_model.py` | `reading/reading_service.py:39`（`SearchModel` / `SearchBookStore`，找书与换源链路整体复用） |
| `reading_cache.py` | `reading/book_cache_service.py:23`、`reading/reading_service.py:38`、`content/local_book_service.py:44`、`sources/source_service.py:124`、`engine/reading_engine.py:518/1710` |
| `block_pages.py` | `engine/rule_engine.py:32`、`reading/reading_service.py:30`、`sources/source_check_service.py:30`、`rule_repair.py:18`、`web_page_renderer.py:23` |
| `legado_url.py` | `engine/rule_engine.py:45`、`engine/reading_engine.py:26`、`engine/legado_js.py:853/6417`、`tools/browser_service.py:42`、`controllers/tools/browser_controller.py:25` |
| `request_compat.py` | `engine/rule_engine.py:50`、`engine/legado_js.py:854/6418`、`legado_url.py:241` |
| `source_rule.py` | `engine/rule_engine.py:53`、`engine/reading_engine.py:28`、`engine/legado_js.py:3858/6580` |
| `app_paths.py` | **连基础设施都依赖它**：`core/storage.py:25`（`AppPaths` / `default_app_paths`）+ `tools/font_service.py:125` + 上面各平铺模块 |
| `app/themes/builtin_themes.py` | 不在 services 平铺里，但同属"M7 没删的数据基座"：`tools/theme_service.py:25` 取 `BUILTIN_SKINS` / `EINK_PALETTE` / `Palette`（纯数据，零 flet） |

反过来说：§8.1 里"搬进域包"的那些旧副本确实是**旧文件已删 + 新位置是唯一实现**；
本表这 14 个则是**旧文件就是实现本身**，两者不要混。判断某个平铺模块能不能删，
先 `grep -rn "app\.services\.<名>" app tests main.py`，零引用才算。

### 8.3 形态与行为对照（没有文件删除，纯形状回归）

| 旧 | 新（当前） |
| --- | --- |
| `bookSource.json` / `bookshelf.json` / `search-books.sqlite3` / `appearance.json` / cookie .jar | 主库 `legado.db`（M6 一次性只读导入，旧文件 `.legacy-bak`） |
| 三页导航（书源/阅读/配置）+ 页内 `ft.Tabs`（找书/管理/导入/检测） | 三顶级入口 `NavigationHost` + 组内页签 `SectionView` + ViewRegistry 声明式路由（形状回归旧架构，清单归 NAV_META） |
| 导入页「选文件 → 预览卡（条数/分组/追加·替换）→ 确认 → 进度条」 | 同一条链回到 `SourceImportView` + `SourceImportController`（预览与模式一度被整块删掉，选完文件直接落库；进度条改了没人下发，见 §5.6） |
| `service_dashboard.py` 返回 `ft.View(route="/dashboard")` + 在 EngineHub 循环里直改控件 | `DashboardView(ft.Column)` + `DashboardController(BaseController)`（嵌进页签才渲染；事件编组回界面循环 + 2s 心跳，见 §5.2） |
| 服务子类上的业务动作方法 `stop()` / `resume()`（朗读停 / 播放停 / 继续播放） | `stop_speaking()` / `stop_playback()` / `resume_playback()`（让开基类生命周期保留名，见 §4.4 不变式 1） |
| 阅读器目录页 `ft.Column`（无底色）叠在正文上 | `ft.Container(bgcolor=SURFACE, content=Column)`（覆盖层必须不透明，见 §5.4） |
| 旧 `ReadingView` 读到底自动接下一章 + 字号/行距/段距/宽度/缩进/对齐/背景/状态栏 + 「阅读设置」弹层 | `ReaderController._on_reader_scroll` 迟滞追加 + `ReaderView.content_plan`/`render_plan`/`apply_preferences`/`build_settings_dialog`（见 §5.5） |
| 旧单体一次 `show` 一个页面，重复进入不重建 | `NavigationHost.show()` 对整页目的地幂等 + `open_book` 同书去重（重复重建曾把在途开书摘成 `_view=None`，见 §5.1） |
| 旧 `ReadingView._chapter_cache` + `PREFETCH_CHAPTER_COUNT=10` + `_content_flight` 在飞去重（视图里自带引擎租约 `_EngineLease`） | 整体上移到 `ReadingService.prefetch_chapters`（内存副本 + 预算 + 同章在飞去重 + shield），引擎仍归 `RuleEngineService` 池管，故不再需要租约（见 §5.5） |


## 9. 打包与分发

详见 `docs/M7打包说明.md`（M7 新写）与 `docs/backend-operations-manual.md`。摘要：

- 依赖分两份：`requirements.txt` 全平台通用；`requirements-desktop.txt` 追加
  playwright（桌面内置无头浏览器用）。
- PyInstaller 入口 `main.py`；`--add-data` 必须把 `app/resources/publicsuffixes.gz`
  放到包内 `app/resources/` 下（`public_suffix._DATA` 按 `__file__` 相对定位，冻结包
  即 `sys._MEIPASS/app/resources/`）；新依赖按服务懒加载，`--hidden-import` 清单见打包说明。
- 冒烟：`python main.py --smoke-test` 自检动态依赖，构建产物必须 exit 0。
- Chromium 内核体积过大不入包，目标机缺失时 `python -m playwright install chromium`。
