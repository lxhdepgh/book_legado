# 系统设计文档（current-system-design）

> 状态：已补全（2026-09-24）。初始化骨架生成于 2026-09-01，本版依据代码现状、开发记录与对齐文档整理；若与代码行为冲突，以代码为准并更新本文。

## 1. 项目定位

Legado 书源连通性 / 搜索可用性检测与内置阅读器的桌面应用（Flet），参考实现来自 Legado Android 版规则体系。

核心链路：导入书源 → 兼容性审计 → 四段检测 → 找书 / 换源 → 内置阅读。项目核心价值是与 Legado 规则行为对齐的兼容层（规则解析、JS 运行时、HTTP 语义），而非 UI 本身。

## 2. 总体架构

```
main.py            # 应用入口（--smoke-test 冒烟；ft.run(app.build, AppView.FLET_APP)）
app/
├── app.py         # Flet 应用装配：三页导航（书源 / 阅读 / 配置），FilePicker 服务注册
├── platform_support.py  # 平台判定（桌面 / 移动）
├── ui_feedback.py # SnackBar 提示工具
├── models/        # 数据模型：book_source / legacy_book_source / appearance_settings
├── services/      # 核心服务层（规则引擎、检测、缓存、阅读引擎等，见 2.1）
├── views/         # 页面视图：书源管理 / 检测 / 导入 / 阅读 / 设置等（见 2.2）
├── themes/        # 内置主题
└── resources/     # publicsuffixes.gz（公共后缀表）
```

页面路由：`app.py` 维护三个目的地（书源 / 阅读 / 配置）。窄窗口（<600px）用底部 NavigationBar，宽窗口用 NavigationRail（≥900px 展开）；阅读器沉浸模式隐藏两侧导航。`ReadingView` 被实例化两次：`discovery_only=True` 的找书页（只承载浏览/搜索，开读把书交给阅读页）与完整阅读页。

### 2.1 服务层模块职责

按职责分四组：

**规则兼容层（核心资产，与 Legado 对齐）**

| 模块 | 体积 | 职责 |
| --- | --- | --- |
| `legado_rule_parser.py` | 93KB | 选择器 / 规则字符串工具层：jsoup 选择器（`select_jsoup`）、XPath 求值（`xpath_values`）、JSONPath（`jsonpath_get`）、规则表达式拆分（`split_rule_expression`）、jsoup 序列化等。无业务状态 |
| `rule_engine.py` | 98KB | `ReaderEngine` 基类 + 通用规则求值管线：HTTP 栈（aiohttp 会话、cookie jar、并发/频率限制）、请求选项 JS、字段提取（`extract_values` / `_field_value`）、列表规则管线（`_list_rule_pipeline`）、搜索（`search`）、探索（`explore` / `explore_document`）、目录（`chapters`）、正文（`content`）、变量（put/get）、四桶合并（`merge_search_items`）。搜索与探索用基类实现 |
| `reading_engine.py` | 82KB | `LegadoReadingEngine(ReaderEngine)`，阅读专用路径：重写目录/正文解析（`chapters` / `content` / `content_document`）、书籍详情（`book_info`）、媒体请求头与受保护图片、登录（`login_schema` / `submit_login`）、付费（`execute_pay_action`）、浏览器回退（`webview_fetcher` / `verification_fetcher`） |
| `legado_js.py` | 346KB | QuickJS 运行时封装：`LegadoJsRuntime`（@js: 规则求值、Rhino/Java API shim）、`JsContext`、`_QuickJsDispatcher`（前台/后台优先级队列，独立线程）、`implemented_java_api_paths()`（已实现 API 面清单，供检测缺口判定） |

配套 Java 兼容件：`java_regex.py`、`icu_charset.py`、`custom_url.py`、`public_suffix.py`、`query_ttf.py`（字体解析）。

**业务服务层**

| 模块 | 职责 |
| --- | --- |
| `source_service.py` | 书源与书架的唯一入口：书源加载/保存/导入/过滤/分组/统计/批量操作；书架（bookshelf.json）读写、notShelf 语义（`is_shelved` / `shelved_books` / `promote_to_bookshelf` / `discard_unshelved_books`）、换源落库（`switch_bookshelf_source`） |
| `check_service.py` | 四段检测（连通性探测 → 搜索可用性 → 分类/标签 → 阅读链路），并发信号量、结果分类（ok / auto_disabled / unknown / blocked）、自动停用 |
| `search_model.py` | `SearchModel`（Legado SearchModel.kt 移植：searchId/页码/generation 取消/32 并发/每源 30s 兜底超时）+ `SearchBookStore`（SQLite 搜索结果库，TTL 24h） |
| `rule_repair.py` | 内容规则修复：章节空内容时挖掘候选选择器、生成修复报告（`RepairReport` / `RuleFix`） |
| `reading_cache.py` / `book_cache_service.py` / `reading_progress.py` | 目录/正文缓存、整书离线缓存（4 工作单元、断点续传）、进度/滚动位置持久化 |
| `compat_audit.py` | JS/Java API 支持面审计 |
| `engine_worker.py` | `EngineHub`（独立事件循环守护线程）+ `EngineProxy`（协程方法分发代理），UI 与引擎解耦 |

**浏览器层**

| 模块 | 职责 |
| --- | --- |
| `headless_browser.py` | 无头 Chromium（Playwright）：`HeadlessBrowser`、`available_fetcher()`（内核缺失返回 None）、投屏（CDP screencast） |
| `interactive_browser.py` | 桌面投屏窗口交互（点击/右键/滚轮/拖拽/键盘） |
| `web_page_renderer.py` | `WebPageRenderer`，把 webView/验证码请求接到浏览器层 |
| `block_pages.py` | 本机拦截（杀软/WAF）判定 |

**工具与配置**：`app_paths.py`（跨平台可写目录，支持环境变量覆盖）、`theme_service.py`（外观与内置主题）。

### 2.2 视图层

| 视图 | 职责 |
| --- | --- |
| `ReadingView`（172KB / 209 方法） | 阅读侧全部界面：找书（发现/搜索/范围/登录）、书架（列表/网格/过滤/排序/整书缓存/批量刷新）、换源对话框、阅读器（目录/正文渲染/预取/滚动持久化/快捷键/规则修复）。已知结构性问题，重构方向见 `architecture-rules.md` §2.3 |
| `CheckView` | 检测页：并发、范围、抽样、自动停用、失败重试、结果过滤/排序/导出 |
| `BookSourceView` / `ManageView` / `ImportView` | 书源管理：列表、批量操作、分组、导入导出 |
| `SettingsView` / `AppearanceView` | 配置与外观（实时预览） |
| `RuleRepairUi` | 内容规则修复界面 |

## 3. 数据流

四条主链路（对应参考实现：书源管理 / SearchModel 找书 / ChangeBookSource 换源 / ReadBook 阅读）：

### 3.1 书源管理链路

```
导入/加载（source_service：bookSource.json）
  → 兼容性审计（compat_audit：JS/Java API 支持面）
  → 四段检测（check_service：连通性 → 搜索可用性 → 分类/标签 → 阅读链路）
  → 失败可修复（rule_repair / RuleRepairUi）→ 重新检测
```

### 3.2 找书链路

```
分类探索（explore）或全书源搜索（SearchModel：32 并发；引擎层每源 10s 超时 / 连接 5s，模型层 30s 兜底）
  → 四桶合并去重（merge_search_items：equal/tags/contains/other + origins 聚合）
  → 结果落库（SearchBookStore，主键 book_url，REPLACE，TTL 24h）
  → 试读（open_book shelve=False，notShelf 语义，不进书架）
```

### 3.3 书架链路

```
书架持久化（bookshelf.json，in_bookshelf 标记过滤）
  → 换源（本地种子 SearchBookStore → 用户触发联网扫描 → 候选排序 → switch_bookshelf_source 续读）
  → 整书缓存（WholeBookCacheService）/ 批量刷新
```

### 3.4 阅读链路

```
目录（chapters）
  → 正文（content / content_document：清洗 → 分块 → 媒体请求头 → 渲染）
  → 预取 10 章 / 容量上限 64MB（PREFETCH_CHAPTER_COUNT / PREFETCH_CACHE_LIMIT）
  → 进度 / 滚动位置持久化（ReadingProgressStore）
```

## 4. 关键设计决策

### 4.1 与 Legado 原版对齐策略

- 参考源码 `base_history/`、`other_history/`（只读，禁止修改、禁止被测试收集）。
- 行为对齐做到行号级：`docs/找书与换源逻辑对齐.md` 逐条映射 SearchModel.kt / ChangeBookSourceViewModel.kt 等，含「已对齐 / 已采纳（差异说明）/ 不采纳（理由）」三类结论。
- 对齐验证依赖差分工具 `tools/legado_diff/`（jsoup / rhino / legado Java 对比）与 `tests/test_legado_compat.py`（422KB）。

### 4.2 规则求值三层结构

parser（语法/选择器工具）→ rule_engine（通用求值管线）→ reading_engine（阅读专用路径）。搜索/探索走基类，阅读走子类；@js: 规则一律 QuickJS（legado_js），Python 侧不做 JS 解释。

### 4.3 UI 与引擎的线程模型

- 引擎协程跑在 `EngineHub` 专属事件循环（守护线程），UI 通过 `EngineProxy` 等待 Future——避免 3000 章目录的同步规则求值冻结 Flet 事件循环（2026-09-24 引入，见 `engine_worker.py` 头注释）。
- QuickJS 自带独立线程 + 前台/后台优先级队列（`_QuickJsDispatcher`），@js: 规则调度不受 Python 侧影响。
- 引擎需要触碰 UI 控件时通过 `interaction_loop` 注入（WebView、验证码回调），属已知灰色地带（见 architecture-rules 红线 6）。

### 4.4 搜索与换源模型

- `SearchModel` 完整移植 Legado 生命周期：searchId 区分新搜索/翻页、页码模型内维护、并发上限 32（比 Legado OkHttp 派发线程 9 大，理由见 `search_model.py:123-125`：worker 直接持有请求，上限落在 flow 层）、模型层每源 30s 兜底超时（视图层引擎实际先以 10s 总时长 / 5s 连接出局，见 `reading_view.py` `SWITCH_TIMEOUT`）、四桶合并 + origins 聚合 + precision。
- 结果落库使换源列表离线可用；TTL 24h，首次取用时清理过期行。
- 换源「先读本地、为空才联网」：本项目联网改为用户点击触发；命中判定用书名**包含**（比 Legado 精确相等放宽，理由见对齐文档 §2.2：搜索框允许改关键词、站点书名常带卷/后缀）。
- 试读不加入书架（notShelf）：记录带 `in_bookshelf=False`，书架渲染/继续阅读/批量刷新只认 `shelved_books()`；退出阅读询问是否放入书架，选否删除记录与缓存；启动时 `discard_unshelved_books()` 清残留。
- 换源排序用「书名精确相等 → 作者包含 → 候选优先级（同域名 > 同站点品牌词 > 未被判失效）→ 书源名」，替代 Legado 的 customOrder + 用户评分（本项目无评分体系）。

### 4.5 浏览器分级

`{"webView":true}` 与 `ruleContent.webJs` 需要真实浏览器：

| 情形 | 机制 | 平台 |
| --- | --- | --- |
| 无人值守渲染 | 内置无头 Chromium（Playwright） | 桌面 |
| 需人处理（登录/验证码/质询） | 投屏进应用窗口的内置 Chromium（CDP screencast） | Windows / Linux |
| 需人处理（同上） | 应用窗口内 `ftw.WebView`（即 Legado BackstageWebView 同一机制） | Android / iOS / macOS |

内核缺失时 `available_fetcher()` 返回 None，检测与阅读退回原有行为（网页阶段记为「需浏览器未抽检」），不报错。

### 4.6 存储选择

- 书源 / 书架用 JSON：人类可读、便于导入导出与迁移。
- 搜索结果用 SQLite：多字段查询 / 排序 / 索引。
- 章节正文用文件缓存目录（reader-cache/）。
- 数据与缓存分离，支持 `LEGADO_BOOKSOURCE_DATA_DIR` / `LEGADO_BOOKSOURCE_CACHE_DIR` 覆盖。
- 已知问题：存储介质分散（7 种），收敛目标见 architecture-rules 红线 8。

## 5. 持久化与缓存

详见 `docs/数据与缓存目录.md`。摘要：

- 数据目录（各系统标准位置，见 `app_paths.py`）：`bookSource.json`、`bookshelf.json`、`appearance.json`、规则状态、cookie、`reader-layout.json`（阅读器偏好）、`search-books.sqlite3`（搜索结果库）。
- 缓存目录：`reader-cache/`（目录+正文）、`chromium-profile/`（无头浏览器登录态）、`chromium-interactive-profile/`（投屏验证窗口）。
- 迁移：旧数据目录下的阅读缓存首次启动复制到新缓存目录；Linux/macOS 旧 `~/.legado_booksource_check` 迁移到标准目录。
- 整书离线缓存：见 `docs/整书离线缓存.md`（4 并发、单章 3 次重试、断点续传、图片嵌入、不参与容量淘汰）。

## 6. 打包与分发

详见 `docs/backend-operations-manual.md`：

- 依赖分两份：`requirements.txt` 全平台通用；`requirements-desktop.txt` 追加 playwright（轮子带平台专属 node 驱动，不进通用清单）。
- PyInstaller：`LegadoReader.spec`；playwright node 驱动随包分发，Chromium 内核体积过大不入包，目标机缺失时 `python -m playwright install chromium`。
- 冒烟测试：`python main.py --smoke-test`（冻结包内动态依赖自检）。
- Android：系统 WebView（`ftw.WebView`），只装 `requirements.txt`，不装 playwright。
