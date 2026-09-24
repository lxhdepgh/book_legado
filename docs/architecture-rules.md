# 架构约束（architecture-rules）

> 状态：已补全（2026-09-24）。以下规则由代码现状与开发记录整理；与代码冲突时以代码为准并更新本文。

## 1. 分层与依赖方向

- 预期与现状一致：`views` → `services` → `models`，禁止反向依赖。
- `models` 不依赖 `services`、`views`；`services` 不依赖 `views`（唯一例外见红线 6 的 interaction_loop 回调）。
- 视图之间不互相 import：页面切换由 `app.py` 统一装配；`ReadingView` 的两个实例（发现页 / 阅读页）通过回调协作（`on_read_book` / `on_back_to_find_book`）。

## 2. 模块边界

### 2.1 服务层四组

| 组 | 成员 | 边界 |
| --- | --- | --- |
| 规则兼容层 | `legado_rule_parser` / `rule_engine` / `reading_engine` / `legado_js` + Java 兼容件 | 只认 `BookSource` 等模型对象，不感知 UI、不感知书源文件的业务细节 |
| 业务服务层 | `source_service` / `check_service` / `search_model` / `rule_repair` / `reading_cache` / `book_cache_service` / `reading_progress` / `compat_audit` / `engine_worker` | 可依赖规则兼容层与浏览器层 |
| 浏览器层 | `headless_browser` / `interactive_browser` / `web_page_renderer` / `block_pages` | 只负责「取到渲染后的页面 / 执行投屏交互」，不解析规则 |
| 工具与配置 | `app_paths` / `theme_service` / `themes` | 无业务状态 |

### 2.2 只读参考资料

- `base_history/`、`other_history/`（Legado / jsoup / rhino 参考源码）：**只读**，禁止修改；`pytest.ini` 已 `norecursedirs` 排除（`base_history other_history build dist node_modules .*`）。
- `other_history/` 在 `.gitignore` 中（vendored 参考下载，不入库）。

### 2.3 视图组织现状与重构方向

- `ReadingView` 当前聚合六大职责（找书 / 书架 / 阅读器 / 换源 / 登录 / 整书缓存），30+ 状态字段交织，是已知结构性问题。
- 拆分方向：按 section 拆为 `FindBooksView` / `BookshelfView` / `ReaderView`，共享状态收敛到 controller；**行为语义不变**。
- 回归护栏：`tests/test_reader_ux.py`（`AllSourceSearchTests`、`SourceSwitchTests` 等）；阅读器的滚动 / 渲染状态是最脆弱点，拆分时逐块增量并跑回归。

## 3. 红线清单

1. **不引入新的 JS 引擎**：`@js:` 规则只允许走 QuickJS（`legado_js.py`）；不得新增 Rhino / V8 等第二引擎。
2. **规则行为必须与 Legado 原版对齐**：新规则能力须先对照 `other_history/` 参考实现再进实现；行为差异必须记录到对齐文档并给出理由。
3. **不修改只读参考目录**：`base_history/`、`other_history/`。
4. **智能体不擅自提交**：不 checkout / reset / stash 开发者未提交的改动；提交前需开发者明确指示（`git-workflow.md`）。
5. **密钥不入库**：令牌 / 密码 / 私钥 / Cookie / 已填充 .env 值禁止写入仓库（`secrets-policy.md`）。
6. **规则求值不得跑在 UI 事件循环**：引擎必须挂在 `EngineHub` 上经 `EngineProxy` 调用；引擎触 UI 只允许通过 `interaction_loop` 注入的回调，禁止直接持有 Flet 控件。
7. **搜索 / 换源的并发与取消语义不得破坏**：`SearchModel` 的 searchId / 页码 / generation 取消、换源的本地种子 + 联网扫描 + 候选排序是行为红线。
8. **存储收敛目标**：新持久化优先复用现有介质（JSON / SQLite / reader-cache），不新增无管理的散落文件；「书」的记录（书架 / 试读 / 搜索结果）不允许出现第三份重复库。
9. **测试护栏**：新增逻辑尽量附带 `tests/test_*.py`；Legado 兼容行为至少覆盖 `test_legado_compat.py`；浏览器真实用例（`_RealRenderTests`）内核缺失时自动跳过。
