# 架构约束（architecture-rules）

> 状态：M7 回写版（2026-09-24），**2026-09-27 按代码现状校正**：§1 / §2.1 的
> `RuleEngineService` 域归属与 `tools → sources` 例外、§2.2 的 `pytest.ini` 引文、
> §2.3 的用例条数与已从工作树移除的 `tools/legado_diff/`、红线 1 的 flet 白名单例外。
> §3 红线升级为设计文档 §5.3 **红线 v2**（以设计文档为权威来源；本文 §3 是重编号版，
> 条号与 §5.3 对不上——例如本文红线 1「服务默认零 flet」即 §5.3 第 6 条，`tests/` 里的
> 护栏 docstring 用的是 §5.3 的号，引用时留意）；针对旧单体的条目（旧 §2.3
> ReadingView 拆分方向等）随 M7 删除旧结构一并删除——**M7 删除的只有旧单体 UI 与被搬
> 进域包的旧平铺服务，14 个旧平铺 `app/services/*.py` 仍在且是各域外壳的基座**（见
> 设计文档 §8.2）。以下规则由代码现状整理，与代码冲突时以代码为准并更新本文。

## 1. 分层与依赖方向

- 方向规则：`views → controllers → services → core → storage`；`models/` 仅被 services
  与 storage 依赖，不反向依赖。
- 视图之间不互相 import；跨视图协作经服务事件与 `ViewRegistry` 声明式路由
  （`views/navigation.py: NavigationHost`），禁止散落 if/elif 装配目的地。
- 服务域间单向依赖：`content/` 可依赖 `reading/` 与 `sources/`；`reading/` 可依赖
  `sources/` 与 `tools/browser_service`；`tools/` 只依赖 `engine/` 的 `RuleEngineService`
  与 `core/`，不反向依赖 content；任何域不得依赖 `views/`。目录包边界即依赖边界，
  跨域 import 视为违规（import-lint 守门）。
  **`RuleEngineService` 的域归属是 `services/engine/`，不是 `reading/`**（别照本条旧措辞
  去 `reading/` 找它）。
  **已接受的 `tools → sources` 例外**：`tools/book_export_service.py:24` 顶层
  `from app.services.sources.source_service import SourceService`（导出要读已缓存章节的
  书目元数据），是既存且已接受的依赖，不算违规新例；但**新代码不要再扩大这条面**——
  `tools/` 需要 sources 数据时优先走 `hub.get()` 拿句柄，而不是新增顶层 import。
- `BrowserService` 例外通道：交互回调接口 `BrowserInteractionCallback` 定义在
  `core/interfaces.py`（Protocol），视图层实现并注入，BrowserService 调用，不向其他
  服务扩散。

## 2. 模块边界

### 2.1 包结构（M7 完成态）

| 包 | 成员 | 边界 |
| --- | --- | --- |
| `app/core/` | lifecycle / hub / bus / events / engine_worker / storage / settings / registry / interfaces | 进程内基础设施，**零 flet import**；不感知任何业务服务细节 |
| `app/services/engine/` | legado_rule_parser / rule_engine / reading_engine / legado_js + java_regex / icu_charset / custom_url / public_suffix / query_ttf + rule_engine_service 外壳 | 规则兼容层核心资产：只加壳、不改行为；零 flet、零视图依赖 |
| `app/services/sources/` | Source / SourceCheck / RuleRepair / CompatAudit / OnlineImport 五服务 | 可依赖 engine 与 core |
| `app/services/reading/` | Reading / Bookmark / ContentSearch / BookCache / Download / ImageBook 六服务 | 可依赖 sources、engine、tools/BrowserService |
| `app/services/content/` | ReadAloud / MediaPlay / Rss / LocalBook 四服务 | 可依赖 reading、sources |
| `app/services/tools/` | ReplaceRule / Dict / Font / QrCode / BookExport / WebDav / Update / WebBridge / Browser / Theme 十服务 | 只依赖 engine/RuleEngineService 与 core（`book_export_service` → sources 是唯一既存例外，见 §1） |
| `app/services/*.py`（**平铺层，14 个模块未删除**） | source_service / check_service / rule_repair / compat_audit / headless_browser / interactive_browser / web_page_renderer / search_model / reading_cache / block_pages / legado_url / request_compat / source_rule / app_paths | 上表各域外壳的**共享实现与继承基座**（`core/storage.py` 也依赖 `app_paths`）；不是待清理的遗留，删除前须确认零引用。逐条归属见设计文档 §8.2 |
| `app/views/` | navigation / service_dashboard + reading/ source/ tools/ settings/ 各域视图 | 轻壳，只 import flet、core、controllers、models；**不 import services 具体模块** |
| `app/controllers/` | base + 每视图一个 Controller | 经 `hub.get(X)` 取服务句柄；attach/detach 对称订阅事件 |
| `app/storage/` | schema.py（版本化迁移唯一权威） | 结构化数据唯一落地点，StorageService 独占访问 |

### 2.2 只读参考资料

- `base_history/`、`other_history/`（Legado / jsoup / rhino 参考源码）：**只读**，
  禁止修改、禁止作为运行时代码依赖；`pytest.ini` 已 `norecursedirs` 排除
  （全文为 `base_history other_history old_code build dist node_modules .*`）。
- `other_history/` 在 `.gitignore` 中（vendored 参考下载，不入库）。
- `old_code/`（M7 旧结构恢复源）：同样只读，回滚时整体取回，不在其上做新开发。

### 2.3 规则兼容层守门（保留项）

- **核心资产只加壳不改行为**：`app/services/engine/` 下 9 个核心资产模块行为冻结，
  外壳（`rule_engine_service.py` 及各复用服务外壳）只做路由 / 事件 / 配置包装。
- 行为守门：`tests/test_legado_compat.py` 当前 **488 条** `def test_`（2026-09-27 实测；
  351 同步 + 137 异步）全绿为零漂移标准；对拍判据取自 §2.2 的只读参考树
  （`base_history/` 的 Legado、`other_history/` 的 jsoup / rhino）。
  ⚠ 旧版本这里写的"差分工具 `tools/legado_diff/`"**当前工作树里已经不存在**——整个
  `tools/`（`legado_diff/` 与 jsoup / rhino / charset / legado 四套 oracle）都已被移除，
  `pytest.ini` 也没有为它保留任何排除项。所以现阶段零漂移的**唯一**机械守门就是上面
  那个测试文件；要恢复差分能力得先重新引入工具目录，别照着本文去找一个不存在的路径。
- 行为修复（设计 §2 清单）只能在核心资产内**定向原位增补**，每项独立测试、独立记录；
  不得整体重写引擎或替换引擎（红线 3「不引入新 JS 引擎」）。
- 新增业务逻辑一律附 `tests/test_*.py`；浏览器真实用例（`_RealRenderTests` 等）
  内核缺失时自动跳过，不视为失败。

## 3. 红线清单 v2（设计文档 §5.3 为权威来源）

1. **服务默认零 flet import，唯一例外是 WebView 渲染桥**：`app/core/` 任何模块不得
   `import flet`；`app/services/` 下除白名单那**一个文件**外也不得 `import flet`
   （含 `from flet import ...`）；服务不得跑在 Flet 事件循环上、不得持有 Flet 控件。
   - **例外本体**：`app/services/web_page_renderer.py`（`:20` `import flet as ft`、
     `:89` 函数内 `import flet_webview as ftw`）。它是 in-app WebView / 投屏 / 验证码
     对话框的渲染桥，经 `tools/browser_service.py:337` 的 `render_webview()` 落在
     **生产路径**上（`{webView:true}` 与验证码请求都走它），删不掉、也不该为此把
     flet 拖进别的模块。2026-09-27 实测：这是全仓库服务层唯一 import flet 的文件。
   - **例外的执法者**：`tests/test_ui_safety.py::ServiceFletImportLintTests`
     的白名单常量 `_FLET_ALLOWED = {"web_page_renderer.py"}`（`:28`）——AST 扫
     `app/services/**`，白名单外的 flet import 一律算 offender 让测试失败。**新增服务
     文件需要碰 flet 时，正确做法是把这段逻辑挪进视图层或经
     `BrowserInteractionCallback` 注入，而不是往白名单里加名字。**
   - 白名单以外的服务触 UI 仅经 `BrowserService` 交互回调
     （`BrowserInteractionCallback`，`core/interfaces.py`）这一条显式通道。
   - 编号对照：本条即设计文档 §5.3 的**第 6 条**（`tests/test_ui_safety.py` 的 docstring
     写的是"红线 #6"，与本条同指一件事，不是另一条规则）。该护栏目前只机械覆盖
     `app/services/**` 的 flet 与 `app/views/**` 的 services import；`app/core/` 零 flet
     当前靠 grep 与代码评审保证，扩护栏时记得补上。
2. **服务间只经 EventBus / 显式接口 / hub**：服务 A 不得直改服务 B 内部状态；
   请求-响应经 `ServiceHub.get()`（协程方法自动经 EngineProxy 派发）或
   `hub.require()`（装配期原始实例）；解耦通知经 EventBus 发布 frozen dataclass
   事件快照，事件载荷禁止塞可变对象引用。
3. **不引入新 JS 引擎**：`@js:` 规则只走 QuickJS（`legado_js.py`）；不得新增
   Rhino / V8 / 第二套浏览器内 JS 执行路径；不引入第二套 Chromium 渲染依赖。
4. **规则行为必须与 Legado 原版对齐**：新规则能力先对照 `other_history/` 参考实现；
   行为差异记录到差异文档并给理由；`@js:` 求值的 QuickJS 单线程串行模型为已知上限，
   以看门狗（排队/执行超时取消）缓解，不为此换引擎。
5. **不修改只读参考目录**：`base_history/`、`other_history/`、`old_code/` 全程只读。
6. **views 不 import services 具体模块**：视图只能 import flet、core、自己的
   Controller、models；取服务一律经 `hub.get(ServiceType)` 按类型取句柄。
   import-lint 守门（M7 清理线重写为新结构红线检查：服务层 flet 只允许白名单那一个
   文件、views 零 services 具体 import；两条都在 `tests/test_ui_safety.py`）。
7. **搜索 / 换源的并发与取消语义不得破坏**：SearchModel searchId / 页码 /
   generation 取消、换源本地种子 + 联网扫描 + 候选排序、notShelf 语义为行为红线。
8. **存储收敛**：结构化数据一律进 StorageService 主库 `legado.db`；"书"的记录不允许
   出现第三份重复库；新功能禁止新增无管理散落文件；JSON 仅作互操作 / 备份格式，
   运行时不读写。
9. **测试护栏**：核心资产外壳化零漂移（§2.3）；新增逻辑附测试；全量 pytest 必须全绿。
   2026-09-27 当前全量：**1215 passed + 28 子测试，0 failed**。
   这条红线当日一度**不能只凭一次绿来记账**：全量连跑曾出现 `1210 passed + 1 failed`，
   红的是
   `tests/test_rss_reading_page.py::RssServiceSurfaceTests::test_refresh_all_publishes_progress_in_order`
   （6 次观察挂 2 次，成因是设计文档 §5.11 的异步投递契约——它等错了一条事件流）。
   **同日已修**：等待条件改成"末拍 `RssRefreshProgressEvent` 到位"，并拿"末拍 `done` 钉成 0"
   做变异核验确认用例确实会红。留下的教训仍然有效：**报告全绿可以只跑一次，
   但报出的数字必须带当天跑了几次、有没有换过等待条件**。
10. **跨平台**：新外部依赖必须 Windows / macOS / Linux 三平台可用（wheels 或纯 Python）；
    平台专属能力一律后端抽象（`TtsBackend` / `MediaBackend` / 文件选择等），
    禁止在业务层写平台分支。
11. **智能体不擅自提交**：不 checkout / reset / stash 开发者未提交的改动；提交前需
    开发者明确指示（`git-workflow.md`）。
12. **密钥不入库**：令牌 / 密码 / 私钥 / Cookie / 已填充 .env 值禁止写入仓库
    （`secrets-policy.md`）。
13. **高风险操作走预览-确认**：删除 / 覆盖 / 发布 / 批量修改前先预览；M7 冻结文件
    删除前必须先依赖扫描，不确定一律保留。

## 4. M1–M7 执行工程约束（新增）

- **解释器**：开发 / 测试 / 打包统一用项目 conda 环境 `legodo_booksource_check`
  （Python 3.12.x，见 `docs/local-testing-guide.md`：`conda activate legodo_booksource_check`）；
  不得用系统全局 Python 或其他环境跑测试 / 打包。
- **pytest 临时目录**：全量回归统一加 `--basetemp=.cache/pytest/tmp_m7`（或里程碑对应
  目录），避免与 `.pytest_cache` / 旧临时目录串扰；`pytest.ini` 已配
  `cache_dir = .cache/pytest`。
- **Windows MAX_PATH**：长路径测试缓存 / 打包产物落在项目内相对目录（`.cache/`、
  `build/`、`dist/`），不要放到用户 profile 深层嵌套目录；venv / 环境路径含空格时
  命令行一律加引号。
- **冒烟入口**：任何里程碑出口与打包回归必跑 `python main.py --smoke-test`，
  exit 0 为通过（冻结包动态依赖自检）。
- **conftest 收集策略**：里程碑过渡的 `collect_ignore` 清单随对应里程碑落地逐条移除；
  M7 出口清空，全量测试一起跑。
