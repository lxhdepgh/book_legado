# M4 RssService 实现记录（内容形态域 · Agent A 线）

> 范围：M4 四线并行中的 **RSS 一线**。其余三线（LocalBookService /
> ReadAloudService+MediaPlayService / 漫画通道 ImageBookService）由其他子代理交付，
> 本记录不含其内容。
> 权威来源：《M4内容形态域契约.md》§3 / §8；《桌面端重构设计.md》§1.2 / §4.2 / §4.3。

## 1. 交付文件清单

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `app/services/content/rss_service.py` | 新建 | RssService（契约 §3 全部接口） |
| `app/storage/schema.py` | 追加 | 在共享 `_migrate_v4` 内追加 rss_sources/rss_articles 两表 DDL；SCHEMA_VERSION 已为 4 |
| `app/core/events.py` | 追加 | `RssRefreshedEvent(source_id, new_count)` |
| `requirements.txt` | 追加 | `feedparser>=6.0.10` |
| `tests/test_rss_m4.py` | 新建 | 9 条离线测试（≥8） |
| `docs/M4Rss实现记录.md` | 新建 | 本文件 |

未改动：`tests/test_core_lifecycle.py` 的 SCHEMA_VERSION 断言——已被并行
ReadAloud 线提前改为 `== 4`（本线无需再改）。

## 2. 测试数字（venv 解释器 + 项目根）

- `python -m pytest tests/test_rss_m4.py -q --no-header -p no:cacheprovider --basetemp=.cache/pytest/tmp_m4`
  → **9 passed**
- `python -m pytest tests/test_core_lifecycle.py -q --no-header -p no:cacheprovider --basetemp=.cache/pytest/tmp_m4`
  → **6 passed**（v4 断言 `version == SCHEMA_VERSION == 4` 全绿）

venv：`D:\env_manger\minicaoda\envs\legodo_booksource_check`（Python 3.12.13）。

## 3. RssService 接口落实（契约 §3）

| 契约接口 | 落实 |
| --- | --- |
| `list_sources() -> list` | 返回源字典列表（id/name/url/enabled/group/custom_order/type/last_update_time） |
| `add_source(name, url)` | 返回 source_id（=规范化 url，对标 Legado sourceUrl 主键）；同 url 重复 add 走 ON CONFLICT 更新 name |
| `remove_source(id)` | 删源 + 级联删其文章 |
| `refresh_source(id)` | 抓取 → 解析 → upsert → 发 RssRefreshedEvent；返回新增数 |
| `refresh_all()` | 逐启用源刷新，单源失败不阻断其余 |
| `list_articles(source_id, unread_only=False)` | 按发布时间倒序；unread_only 过滤 read_flag=0 |
| `mark_read(article_id)` | read_flag=1 |
| `star(article_id, starred)` | star_flag 置位 |
| feed 抓取走 RuleEngineService 网络栈 | 默认 `_fetch` 经 `engine_service._get_engine(None)._request({url,method:GET})`，复用同一 aiohttp 会话 / UA / 超时 / cookie；测试经 `fetcher` 注入本地文件读取，不联网 |
| 标准 feed 用 feedparser | `_parse_feedparser` 内 `import feedparser`（懒加载，仅本服务） |
| 规则订阅用 legado_rule_parser 兜底 | 源 `rule_articles` 非空时走 `_parse_rule_feed`（parse_markup + evaluate list_context），M5 再补全 ruleTitle/ruleLink 逐字段求值 |
| RssRefreshedEvent | 每源刷新完成发 `(source_id, new_count)` |
| 刷新进度写 status.progress | 刷新中 0→0.5→1.0；refresh_all 按源数分段推进 |

## 4. schema v4 与 Legado 4 实体对照（简化说明，契约 §3 授权）

v4 由四线共享（ReadAloud 线先写入 tts_profiles + books.play_position，本线在
**同一 `_migrate_v4` 函数内追加** rss 表，未整体替换）。

Legado 4 四张 RSS 实体表 → 桌面端两张表：

| Legado 4 实体 | 桌面端 v4 落点 |
| --- | --- |
| `RssSource`（rssSources，sourceUrl 主键） | `rss_sources`：id↔sourceUrl、name↔sourceName、url↔sourceUrl、enabled↔enabled、source_group↔sourceGroup、custom_order↔customOrder、type↔type、last_update_time↔lastUpdateTime；rule_articles/rule_title/rule_pub_date/rule_description/rule_image/rule_link/rule_content ↔ Legado 同名 rule* 字段 |
| `RssArticle`（rssArticles，origin+link+sort 主键，read Boolean） | `rss_articles`：source_id↔origin、title↔title、url↔link、author↔feed author、pub_date↔pubDate、content↔description/content、image↔image；**read_flag ↔ RssArticle.read** |
| `RssReadRecord`（rssReadRecords，record=link 主键，记已读+readTime） | **不单独建表** → 折叠为 `rss_articles.read_flag=1`；readTime 用行内 updated_at 近似 |
| `RssStar`（rssStars，origin+link 主键，收藏快照+starTime） | **不单独建表** → 折叠为 `rss_articles.star_flag=1`；starTime 用行内 updated_at 近似 |

即"已读 / 收藏"由文章行上两个 0/1 列承载，而非 Legado 的两张关联表——
桌面端单用户、无跨源去重需求，省去 join。去重键 `UNIQUE(source_id, url)`；
刷新 upsert 时**不动** read_flag/star_flag，保护用户已读/收藏状态。

## 5. 新依赖 feedparser

- `pip install feedparser` → 6.0.14，wheel 为 `py3-none-any`（纯 Python，三端无原生分支）；
  传递依赖 `feedparser-sgmllib` 同样纯 Python（替代 stdlib sgmllib）。
- 仅 `app/services/content/rss_service.py` 内懒加载 `import feedparser`，未启用 RSS 不加载。
- `requirements.txt` 追加 `feedparser>=6.0.10`（带注释说明纯 Python/跨平台）。

## 6. 跨平台冒烟声明

纯 Python，无平台 API 依赖：SQLite 经 StorageService；feedparser 纯 Python；
网络抓取复用 RuleEngineService 的 aiohttp 栈（三端一致）；无 os.startfile /
ctypes / 平台分支。Win/macOS/Linux 行为一致。

## 7. 与契约的偏差

无功能性偏差。两处说明性偏离（均在契约允许范围内）：

1. **SettingsService 未列入 dependencies**：契约 §1 列 RssService 依赖
   RuleEngineService / StorageService / SettingsService。本服务不直读偏好
   （UA/超时由 RuleEngineService 自身从 SettingsService 注入 `_prefs`），
   故 dependencies 只声明 `(StorageService,)`，避免无谓启动耦合；
   RuleEngineService 经 on_start 时 `hub.require` 软取，未注册则降级为"仅注入
   fetcher 可用"。
2. **规则订阅解析为最小兜底**：`_parse_rule_feed` 仅按 rule_articles 抽列表节点 +
   近似 title/link，未逐字段求值 ruleTitle/rulePubDate/ruleContent（M5 RSS 规则
   编辑器补全）；标准 feed（feedparser）路径完整可用，是 M4 主路径。

## 8. 并行线协调备注

- `_migrate_v4` 为共享函数：ReadAloud 线写 tts_profiles + books.play_position，
  本线追加 rss 表，二者并存于同一函数（均 CREATE IF NOT EXISTS，幂等）。
- `RssRefreshedEvent` 与 ReadAloud/MediaPlay 事件同批落在 events.py M4 段，
  签名按契约 §2 `(source_id, new_count)`，未与并行线冲突。
- 不提交 git；不做 M4 之外工作。
