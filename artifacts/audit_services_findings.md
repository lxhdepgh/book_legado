# Legado 桌面端重构（M1→M7 新树）只读架构审核报告

- 范围：`app/core/*`、`app/services/**`（5 域 + 根级服务）、`app/storage/schema.py`、`main.py::_build_hub()`
- 方式：只读源码走查 + 只读 AST 探针（未 import 业务模块、未跑 pytest、未改动任何源文件）
- 解释器：`D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe`
- 日期：2026-09-25

分类口径：**已确认问题** = 源码可定位、逻辑闭合；**需用户确认** = 依赖运行期/三方行为（flet loop、sqlite 线程模型）才能最终判定；**设计取舍** = 作者有意为之但有代价。

---

## 一、按清单逐项结论

### 清单 1 — 服务间循环依赖 / 懒解析掩盖真实依赖

- 成环：**未发现环**。`ServiceHub._topo_order()`（hub.py:131）Kahn 排序对真正的环会直接 `raise RuntimeError`，装配期即暴露。
- 但存在**系统性"假依赖声明"**：很多服务 `dependencies=()`，却在 `on_start` 里经 `hub.require` 取 Storage/Settings/RuleEngine/SourceService。AST 探针交叉比对结果（声明 vs 实际 require）：

  | 服务 | 声明 dependencies | on_start 实际 require |
  |---|---|---|
  | ReadAloudService | `()` | StorageService, SettingsService, RuleEngineService |
  | WebDavService | `()` | SettingsService, StorageService, SourceService |
  | BookExportService | `()` | StorageService, SourceService |
  | UpdateService | `()` | StorageService, SettingsService |
  | ThemeService | `()` | SettingsService |
  | BrowserService | `()` | StorageService |
  | CompatAuditService | `()` | StorageService |
  | SourceCheckService | `()` | SettingsService, SourceService, CompatAuditService（dotted） |
  | RuleRepairService | `()` | SourceService（dotted） |
  | LocalBookService / MediaPlayService | `()` | StorageService |

  后果：拓扑序只看声明的 tuple，**不保证 SettingsService 等先于消费者 on_start**。

### 清单 2 — 事件订阅泄漏

- 全 services 仅两处 `bus.subscribe`：`WebBridgeService`（web_bridge_service.py:135,356）与 `RuleRepairService`（rule_repair_service.py:47 预留）。
- 两者都在 `on_stop` 里退订（web_bridge_service.py:85-90 遍历 `_unsubs` 并 clear；rule_repair_service.py:60-65）。**未发现泄漏**。
- `SettingsService._subs`（settings.py:36）在 `on_stop` 调 `_unbind` 但**未清空 `self._subs`**，进程级影响可忽略。

### 清单 3 — 异步静默失败

- **DownloadService.enqueue 是同步方法**（download_service.py:69），内部 `asyncio.ensure_future(self._run_cache(...))`（:80）。控制器 `bookshelf_controller.toggle_cache`（:103-112）在 flet UI 线程同步调 `hub.get(DownloadService).enqueue(...)`；enqueue 非协程方法，EngineProxy 不派发，直接在 UI 线程执行。`asyncio.ensure_future` 依赖"当前线程有运行中的 loop"：
  - 若 UI 线程无 loop → 抛 `RuntimeError`，被控制器 try/except 吞成"整书缓存服务暂未就绪"（与真实原因不符）；
  - 若有 loop → 协程跑在 UI loop 而非 EngineHub 守护 loop，违背"统一执行域"设计。
  - 这是本审核最明确的"异步方法被同步方式触发"反例。
- `SourceCheckService._maybe_audit`（source_check_service.py:202）在同步回调 `on_result` 里 `asyncio.create_task`：依赖 `on_result` 确实跑在引擎 loop 内（check_service.check_sources 为 async），需确认；`create_task` 未收进 task set，异常已被 `_audit_safe` 兜底，风险低。
- `EventBus.publish` 返回的 future 普遍无人 await：可接受，因为 `_invoke`（bus.py:71-77）内部 try/except 吞掉异常，不会触发 "exception never retrieved"。

### 清单 4 — EventBus / EngineHub / ServiceHub 分工

- EngineProxy 边界正确：`__getattr__`（engine_worker.py:116-122）只对 `inspect.iscoroutinefunction` 的类方法派发，实例属性（`current_book`/`_session` 等）与 property 结果直达。
- **灰色地带 `interaction_loop`**：`_dispatch`（engine_worker.py:130-133）在首次 await 代理协程时，把 `asyncio.get_running_loop()` 记到 `engine.interaction_loop`。该值随后被 `reading_engine._call_ui`（reading_engine.py:531-537）用于把登录/验证码回调 marshal 回 UI loop。风险：首次代理协程调用若发生在非 UI loop（如 start_all 阶段、或某次 `engine_call`），`interaction_loop` 会被绑错，UI 回调派发到错误 loop。当前视图路径下首次调用来自 UI，暂未爆雷，但耦合脆弱。
- `submit` 的 future 异常：直接代理调用经 `asyncio.wrap_future` 透传给 await 方；总线侧已由 `_invoke` 隔离。OK。
- `shutdown()`（hub.py:114-119）用 `asyncio.run(self.stop_all())` 新建临时 loop 跑逆序停止；此时 EngineHub 守护 loop 仍在（它在拓扑逆序里最后停）。但 RuleEngineService.on_stop 在这个**新临时 loop** 上 `await engine.aclose()`，而 aiohttp session 是在守护 loop 创建的——跨 loop 关 aiohttp 会话，潜在隐患。

### 清单 5 — Service 生命周期

- 状态机：`start()` 失败 → `SUSPENDED` + `last_error` + re-raise（lifecycle.py:99-106），`start_all` 单服务失败不阻断其余（hub.py:94-98）。语义正确。
- `suspend()`/`resume()`（lifecycle.py:121-127）**没有** start() 那样的异常兜底：`on_suspend` 抛错会停在原状态且广播缺失，与 start 的 SUSPENDED 语义不一致。
- on_start 同步阻塞：`StorageService.on_start` 里 `migrate()` 做文件读 + sqlite 批量写（storage.py:79），`SourceService.on_start` 里 `self.load()` 整表读盘（sources/source_service.py:96）。这些跑在 `main()` 的 `asyncio.run(hub.start_all())`（main.py:164）上——**该 loop 在 `ft.run()` 之前**，因此只拖启动、不拖 UI。可接受，但启动期 migrate 持锁做文件 I/O 会阻塞同 loop 上其它 on_start。

### 清单 6 — 存储（重点）

- schema v6 迁移幂等：`CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version` 逐版本推进（storage.py:92-104, schema.py:269-346）。幂等性 OK。
- `StorageService.migrate()`：`m6.legacy_migrated` 标记判重（storage.py:128-134），导入用 `INSERT OR REPLACE`，旧文件只改名 `.legacy-bak` 不删（:156-166）。幂等与回滚设计正确。
- **【高】sqlite 跨线程锁未被遵守（用户点名项，确认成立）**：
  - `StorageService._db_lock`（RLock，storage.py:49）只在 `ensure_schema`/`migrate` 内部用。
  - 但以下访问方全部**裸用 `self._storage.db` 而不经 `_db_lock`**：
    - `SettingsService.on_start`（settings.py:49）与 `SettingsService.set`（settings.py:70）——UI 线程同步写；
    - `SourceService.load/load_bookshelf/save/save_bookshelf/update_cover`（sources/source_service.py:170,188,211-215,239-243,421）；
    - `ReadingService.save_progress/restore_position`（reading_service.py:330,362）；
    - 以及 rss_service/read_aloud/media_play/local_book/dict_service/font_service/replace_rule/compat_audit/content_search/bookmark_service 全部直接 `self._storage.db`。
  - 各家自搞小锁，**互相不互斥**：`BookmarkService._lock`（bookmark_service.py:29）、`SearchBookStore._lock`（search_model.py:27）都是独立对象，与 `StorageService._db_lock` 不是同一把。
  - 真正并发的线程：UI 线程（SettingsService.set、控制器）+ EngineHub 守护线程（各服务 on_start/协程）+ `asyncio.to_thread` 工作线程（SearchModel 落库 search_model.py:234、CompatAuditService audit compat_audit_service.py:209）。同一 `sqlite3.Connection`（`check_same_thread=False`、autocommit）被多线程并发 `execute`。
  - 后果：Python sqlite3 单连接跨线程并发使用不是线程安全（C 层 serialized 不等于 Python 对象状态安全），可能抛 "recursive use" 或静默交错；尤其 `SourceService.save()` 是 `DELETE FROM book_sources` + `executemany INSERT` 两条 autocommit 语句，**非原子**——崩溃在两条之间 = 空库；并发读方可在窗口内看到空表。这相对旧 JSON "tmp+replace 原子写"（source_service.py:444-447）是一次可靠性回退。

### 清单 7 — 红线（服务层 flet）

- `import flet` 在 services 下仅 `web_page_renderer.py:20`（及 :89 懒 import `flet_webview`）命中，符合"唯一例外"。`media_play.py`/`browser_service.py` 仅注释提及。**红线遵守**。
- 服务间协作路径：均经 `hub.require`（原始实例）/`hub.get`（代理）/EventBus，未发现服务直接 import 视图。

### 清单 8 — on_start 协作者解析 / 静默降级

- 普遍模式：`try: self.hub.require(X) except: self._x = None`（sources/source_service.py:87-93；source_check_service.py:211-221；rule_repair_service.py:121-135；online_import_service.py:129-139；web_bridge_service.py:93-105）。
- 后果：协作者未注册/启动失败时，本服务照样 `RUNNING`，运行时功能静默空转。例：CompatAuditService 若 SUSPENDED，SourceService._auditor=None → `save_source` 跳过审计，用户无感知（sources/source_service.py:118-127）。
- **【中】启动序导致 SettingsService 被晚启动**：拓扑序里 SettingsService 因 `dependencies=(StorageService,)` 而入队靠后；而 `WebBridgeService`（deps=()）等先于它 on_start。WebBridge.on_start 调 `_enabled()` 读 `webbridge.enabled`（web_bridge_service.py:76），此时 SettingsService 缓存还没从库加载 → 读到默认 False。SettingsService.on_start 只 load 缓存、不补发 SettingsChangedEvent（settings.py:43-51），所以网桥**重启后不会按上次持久化开关自动拉起**，除非用户再切一次开关。

---

## 二、逐条发现表

| 编号 | 分类 | 发现（文件:行 / 方法） | 影响 | 修复建议 | 优先级 |
|---|---|---|---|---|---|
| F1 | 已确认问题 | 同一 sqlite 主连接被多线程裸用，且锁不统一：`StorageService._db_lock` 与 `BookmarkService._lock`、`SearchBookStore._lock` 是三把不同锁；SettingsService/SourceService/ReadingService 完全无锁（storage.py:49, settings.py:70, sources/source_service.py:211, reading_service.py:330, search_model.py:234） | 跨线程并发 `execute` 可能抛错/交错；搜索落库走 to_thread，与 UI 线程 SettingsService.set 真并发 | 统一收敛为 StorageService 暴露的一把 `db_lock`（或 contextmanager），所有服务访问主库必须经它；bookmark/search_store 改用 storage 的锁 | 高 |
| F2 | 已确认问题 | `SourceService.save()`/`save_bookshelf()` 用 `DELETE FROM ...` + `executemany INSERT`，autocommit、无事务（sources/source_service.py:211-215, 239-243） | 两条语句间崩溃 = 空表；并发读者看到空窗口；相对旧 JSON tmp+replace 原子写是回退 | 包一层 `BEGIN IMMEDIATE`/`COMMIT`（或 `with self._storage:` 事务），与锁一起拿 | 高 |
| F3 | 已确认问题 | `DownloadService.enqueue` 同步方法内 `asyncio.ensure_future(self._run_cache(...))`，被 UI 线程经 EngineProxy 直接同步调（download_service.py:80, bookshelf_controller.py:106） | 无 UI loop 时抛 RuntimeError 被吞成"服务暂未就绪"；有 loop 时协程跑在 UI loop 而非守护 loop | 把 enqueue 改 async，或内部改 `engine_hub().submit(self._run_cache(...))`；控制器改 `engine_call(...)` | 高 |
| F4 | 已确认问题 | `dependencies` 声明与 on_start 实际 require 系统性不符（见清单 1 表），拓扑序不保证真实协作者先就绪 | 服务在协作者未 on_start 时拿到"已注册未启动"实例，读到空缓存/空列表 | 把 on_start require 的类补进 `dependencies` tuple（ReadAloud/WebDav/Update/Theme/Browser/CompatAudit/SourceCheck/RuleRepair/LocalBook/MediaPlay），让 Kahn 序正确 | 中 |
| F5 | 已确认问题 | 拓扑序使 `WebBridgeService.on_start` 早于 `SettingsService.on_start`，读 `webbridge.enabled` 得到空缓存；SettingsService 启动后不补发事件（web_bridge_service.py:76, settings.py:43-51） | 用户上次开启的网桥重启后不自动拉起，需手动再切一次 | WebBridge.on_start 订阅后主动 `_apply_settings()` 重读一次当前值；或把 WebBridge 加入 `dependencies=(SettingsService,)` | 中 |
| F6 | 已确认问题 | `hub.require` 包 try/except 后静默置 None，协作者缺失时服务仍 RUNNING 空转（sources/source_service.py:87-93, source_check_service.py:211-221, rule_repair_service.py:121-135） | 审计/检测/导入能力静默失效，仪表盘却显示 RUNNING | on_start 解析不到必需协作者时，写 `status.last_error` 并显式 SUSPENDED（或至少 logger.error + 仪表盘可见告警） | 中 |
| F7 | 已确认问题 | `EngineProxy._dispatch` 把首次 await 代理协程时的 `get_running_loop()` 写死为 `engine.interaction_loop`（engine_worker.py:130-133） | 若首次代理调用不在 UI loop，登录/验证码回调会被 marshal 到错误 loop | 由视图层显式经 `BrowserInteractionCallback`（interfaces.py）注入 UI loop，删掉自动绑定；或首次调用前由装配处显式 set | 中 |
| F8 | 已确认问题 | `ServiceHub.shutdown()` 用新建临时 loop 跑 stop_all，而 `RuleEngineService.on_stop` 在该临时 loop 上关 aiohttp 会话（会话创建于守护 loop）（hub.py:117, rule_engine_service.py:117-132） | 退出时关 aiohttp/QuickJS 跨 loop，可能告警或资源泄漏（进程退出期，影响有限） | stop_all 改在 EngineHub 守护 loop 上调度（`engine.submit` + 等待 future），而非新建 asyncio.run | 低 |
| F9 | 已确认问题 | `Service.suspend()`/`resume()` 无 start() 那样的异常兜底与 last_error 记录（lifecycle.py:121-127） | on_suspend 抛错时状态机停在 RUNNING，与 start 失败进 SUSPENDED 不一致 | 与 start() 对齐：try/except + last_error + 状态迁移 | 低 |
| F10 | 设计取舍 | `EventBus.publish` 返回的 future 普遍不被 await；`_invoke` 已吞异常（bus.py:46-53, 71-77） | 无"never retrieved"告警；但处理器异常只进日志，调用方无感知 | 保持现状即可；若需可观测性，可在 _invoke 里把异常同步到对应服务 status | 低 |
| F11 | 需用户确认 | `SourceCheckService._maybe_audit` 在同步回调 on_result 里 `asyncio.create_task`（source_check_service.py:202） | 若 on_result 实际跑在 to_thread 工作线程而非引擎 loop，会抛 "no running event loop" | 确认 check_service.check_sources 的 on_result 调用线程；若在工作线程，改 `asyncio.run_coroutine_threadsafe` 到守护 loop | 低 |
| F12 | 设计取舍 | `StorageService.on_start` 的 `migrate()` 在 `asyncio.run(start_all)` 上同步做文件 I/O + 批量写（storage.py:79） | 只拖启动、不拖 UI（ft.run 尚未开始）；但持 `_db_lock` 做文件读会阻塞同 loop | 可接受；若启动偏慢，可把 migrate 挪到后台线程并在后续服务前 barrier | 低 |
| F13 | 已确认问题 | `SettingsService._subs` 在 on_stop 未清空（settings.py:53-54 只 `_unbind`） | 进程级单例，影响可忽略；重复 _bind 测试场景可能残留 | on_stop 追加 `self._subs.clear()` | 低 |

---

## 三、"确定能修且低风险、改完可被全量测试守护"的项

- **F1（统一 db 锁）**：在 `StorageService` 暴露 `db_lock`（或 `@contextmanager db_guard()`），把 bookmark/search_store 自建锁替换为引用它，其余裸 `execute` 处包进 guard。纯加锁、不改逻辑，现有存储/进度测试可守护。
- **F2（save 事务化）**：SourceService.save/save_bookshelf 的 DELETE+INSERT 包进一个事务，行为对外不变（整表镜像），可被现有书源 CRUD 测试守护。
- **F3（enqueue 走守护 loop）**：DownloadService.enqueue 内部改 `engine_hub().submit(...)`，控制器无需改；下载/进度测试可守护。
- **F9（suspend/resume 异常兜底）**：与 start() 对齐的纯状态机补齐，低风险。
- **F13（_subs 清理）**：一行清理。
- **F4（补 dependencies tuple）**：纯声明补全，让拓扑序正确；需核对补全后不引入新环（当前无环，补全只会让序更严格）。

> F5/F6/F7 涉及行为语义调整（网桥启动时机、降级可见性、interaction_loop 绑定来源），建议先确认产品预期再改；F8 是退出路径，优先级低但建议一并收敛。
