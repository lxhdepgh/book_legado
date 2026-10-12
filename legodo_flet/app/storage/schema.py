"""主库 schema（版本化）与迁移函数（§9.1）。

schema 定义与逐版本迁移逻辑的唯一权威位置；StorageService（app/core/storage.py）
在启动时按 ``PRAGMA user_version`` 从当前版本逐版本执行 :data:`MIGRATIONS`。

M1 范围仅 v1（settings / cookies 两张表）；后续里程碑新增表/列时在此追加迁移
函数并 +1 :data:`SCHEMA_VERSION`，迁移脚本本体按设计放 ``app/storage/migrations/``。
"""
from __future__ import annotations

import sqlite3

# 当前 schema 版本：新增表/列时在此追加迁移函数并 +1
SCHEMA_VERSION = 8


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1：settings 键值表 + cookies 持久罐表（对标 Legado AppConfig 与 CookieStore）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings ("
        "  key   TEXT PRIMARY KEY,"
        "  value TEXT"
        ")")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cookies ("
        "  key        TEXT PRIMARY KEY,"
        "  jar        TEXT NOT NULL,"
        "  updated_at REAL NOT NULL"
        ")")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2：阅读域结构化表（M2，契约 §9）。

    - ``books``：一本书一行，承载书元数据 + 连续滚动进度（替代 bookshelf.json 的
      reading 块）。M2 新进度读写只走本表，旧 bookshelf.json 不做介质导入（M6）。
    - ``bookmarks``：用户主动书签，与自动续读进度是两条独立数据（§1.13）。
    - FTS5 虚表 ``content_search`` 由 ContentSearchService 运行时
      ``CREATE VIRTUAL TABLE`` 建立，不进版本化迁移（契约 §9 末段）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS books ("
        "  book_url      TEXT PRIMARY KEY,"
        "  source_url    TEXT NOT NULL DEFAULT '',"
        "  name          TEXT NOT NULL DEFAULT '',"
        "  author        TEXT NOT NULL DEFAULT '',"
        "  kind          TEXT NOT NULL DEFAULT '',"
        "  cover_url     TEXT NOT NULL DEFAULT '',"
        "  chapter_index INTEGER NOT NULL DEFAULT 0,"
        "  scroll_offset REAL   NOT NULL DEFAULT 0,"
        "  scroll_ratio  REAL   NOT NULL DEFAULT 0,"
        "  chapter_pos   INTEGER NOT NULL DEFAULT 0,"
        "  font_size     INTEGER NOT NULL DEFAULT 16,"
        "  in_bookshelf  INTEGER NOT NULL DEFAULT 1,"
        "  shelf_group   TEXT NOT NULL DEFAULT '',"
        "  updated_at    REAL   NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bookmarks ("
        "  id            INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  book_url      TEXT NOT NULL,"
        "  chapter_index INTEGER NOT NULL DEFAULT 0,"
        "  offset        REAL   NOT NULL DEFAULT 0,"
        "  summary       TEXT NOT NULL DEFAULT '',"
        "  created_at     REAL   NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookmarks_book "
        "ON bookmarks(book_url)")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3：书源兼容审计历史表（M3，契约 §8 / §6）。

    - ``audit_log``：每次导入即审 / 保存即审 / 检测流水线复跑一条审计记录，
      用于回溯"这条书源当时报了哪些 API 支持面缺口"。
      ``missing_apis`` 存 JSON 文本（缺失 java/object API、未知字段、伪类、
      JS/regex 语法错误等汇总），``ts`` 为 ISO 时间戳。
    - 其余书源域数据 M3 仍读写旧 bookSource.json，不建表（M6 存储收敛才迁）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  source_id    TEXT NOT NULL DEFAULT '',"
        "  missing_apis TEXT NOT NULL DEFAULT '',"
        "  ts           TEXT NOT NULL DEFAULT ''"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_source "
        "ON audit_log(source_id)")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4：M4 内容形态域（契约 §8 / §6.3）。

    本函数是 M4 四线并行共享的迁移点：
    - 
    - ``books.play_position`` 视频/音频书播放位置列；
    - RSS 线（RssService）应在本函数内**追加** ``rss_sources``/``rss_articles``
      等表 DDL（不要整体替换本函数）；LocalBook 线无新增表（复用 books 登记）。

    - ``books.play_position``：视频书/音频书在当前 ``chapter_index`` 内的播放
      位置（秒）。v2 books 表已有 ``chapter_index``（复用为播放章节索引），
      仅缺"秒级播放位置"一列，故以 ALTER TABLE 补齐（§8：以 v2 实际列名为准）。
      ⚠️ "复用 chapter_index"这条在界面接出播放面板后被推翻，播放索引改存
      ``books.play_chapter_index``，见 :func:`_migrate_v7`。
    """
        conn.execute(
            "ALTER TABLE books ADD COLUMN "
            "play_position REAL NOT NULL DEFAULT 0")

    # ------------------------------------------------------------------ #
    # RSS 线（RssService）追加：rss_sources / rss_articles（契约 §3）。
    # 对标 Legado 4 四张 RSS 实体表，按契约授权简化为两张表。
    # ------------------------------------------------------------------ #
    # ``rss_sources`` ↔ Legado ``RssSource``（rssSources）：
    #   id ↔ sourceUrl（桌面端以 url 规范化串为身份主键）；name ↔ sourceName；
    #   url ↔ sourceUrl；enabled ↔ enabled；source_group ↔ sourceGroup；
    #   custom_order ↔ customOrder；type ↔ type（0 网页/1 图片/2 视频）；
    #   last_update_time ↔ lastUpdateTime。规则订阅字段
    #   （rule_articles/rule_title/rule_pub_date/rule_description/rule_image/
    #   rule_link/rule_content）↔ Legado RssSource 同名 rule* 字段——留空表示
    #   标准 feed（feedparser 解析），非空时走 legado_rule_parser 体系兜底。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rss_sources ("
        "  id               TEXT PRIMARY KEY,"
        "  name             TEXT NOT NULL DEFAULT '',"
        "  url              TEXT NOT NULL DEFAULT '',"
        "  enabled          INTEGER NOT NULL DEFAULT 1,"
        "  source_group     TEXT NOT NULL DEFAULT '',"
        "  custom_order     INTEGER NOT NULL DEFAULT 0,"
        "  type             INTEGER NOT NULL DEFAULT 0,"
        "  last_update_time REAL   NOT NULL DEFAULT 0,"
        "  rule_articles    TEXT NOT NULL DEFAULT '',"
        "  rule_title       TEXT NOT NULL DEFAULT '',"
        "  rule_pub_date    TEXT NOT NULL DEFAULT '',"
        "  rule_description TEXT NOT NULL DEFAULT '',"
        "  rule_image       TEXT NOT NULL DEFAULT '',"
        "  rule_link        TEXT NOT NULL DEFAULT '',"
        "  rule_content     TEXT NOT NULL DEFAULT ''"
        ")")
    # ``rss_articles`` ↔ Legado ``RssArticle``（rssArticles）：
    #   source_id ↔ origin；title ↔ title；url ↔ link；author ↔ feed author；
    #   pub_date ↔ pubDate；content ↔ description/content；image ↔ image。
    # **简化列与 Legado 4 实体对照（契约 §3 授权简化，在此写明）**：
    #   - Legado ``RssArticle.read``（Boolean）→ 本表 read_flag（0/1）；
    #   - Legado ``rssReadRecords`` 表（record=link 主键，记已读链接+readTime）
    #     → 不单独建表，折叠为 rss_articles.read_flag=1，readTime 用行内
    #     updated_at 近似；
    #   - Legado ``rssStars`` 表（origin+link 主键，收藏快照）→ 不单独建表，
    #     折叠为 rss_articles.star_flag=1，starTime 用行内 updated_at 近似。
    # 即"已读/收藏"由文章行上两个 0/1 列承载，而非 Legado 的两张关联表——
    # 桌面端单用户、无跨源去重需求，省去 join。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rss_articles ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  source_id   TEXT NOT NULL,"
        "  title       TEXT NOT NULL DEFAULT '',"
        "  url         TEXT NOT NULL DEFAULT '',"
        "  author      TEXT NOT NULL DEFAULT '',"
        "  pub_date    TEXT NOT NULL DEFAULT '',"
        "  content     TEXT NOT NULL DEFAULT '',"
        "  image       TEXT NOT NULL DEFAULT '',"
        "  read_flag   INTEGER NOT NULL DEFAULT 0,"
        "  star_flag   INTEGER NOT NULL DEFAULT 0,"
        "  updated_at  REAL   NOT NULL DEFAULT 0,"
        "  UNIQUE(source_id, url)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rss_articles_source "
        "ON rss_articles(source_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rss_articles_star "
        "ON rss_articles(star_flag)")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5：M5 工具域（契约 §14 / §6.3）。

    本函数是 M5 四线并行共享的迁移点：
    - 本线（ReplaceRuleService + DictService + FontService + QrCodeService）
      交付：``replace_rules`` / ``dict_rules`` / ``fonts`` 三张表；
    - 下载线（BookExportService / DownloadService）在本函数内**追加**
      ``download_tasks`` 表 DDL（不要整体替换本函数）。

    - ``replace_rules``：用户级全局替换规则（对标 Legado ReplaceRule 实体）。
      ``scope`` 取值 ``content``（正文，默认）/ ``title``（章节标题），
      对应 Legado ``isTitle``；``is_regex`` 对应 ``isRegex``（默认开，与
      reading_engine._apply_purify_rules 的默认一致）。应用顺序：书源内嵌
      规则先于全局规则（ContentProcessor 两级叠加，§1.7）。
    - ``dict_rules``：划词词典规则（§1.8）。``url`` 为释义页模板 URL，
      ``rule_content`` 等规则字段以 JSON 文本存 ``rules_json``——把词典
      规则视为"目标为释义页"的特殊书源，复用 RuleEngineService 解析，
      不另写解析器。
    - ``fonts``：用户导入的字体文件登记（§1.9）。``family`` 为 fontTools
      解析出的族名；``path`` 指向 app_paths 数据目录 fonts/ 下的实际文件；
      系统字体只读枚举不落本表。
    - ``download_tasks``：下载任务中心（§6.3 草案）。``kind`` 区分章节/
      附件/图片批次；``files`` 存 JSON 文本（产物文件清单）；进度写
      ``progress``（0-1）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS replace_rules ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name        TEXT NOT NULL DEFAULT '',"
        "  pattern     TEXT NOT NULL DEFAULT '',"
        "  replacement TEXT NOT NULL DEFAULT '',"
        "  scope       TEXT NOT NULL DEFAULT 'content',"
        "  is_regex    INTEGER NOT NULL DEFAULT 1,"
        "  enabled     INTEGER NOT NULL DEFAULT 1,"
        "  created_at  REAL NOT NULL DEFAULT 0,"
        "  updated_at  REAL NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_replace_rules_scope "
        "ON replace_rules(scope, enabled)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS dict_rules ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name        TEXT NOT NULL DEFAULT '',"
        "  url         TEXT NOT NULL DEFAULT '',"
        "  rules_json  TEXT NOT NULL DEFAULT '{}',"
        "  enabled     INTEGER NOT NULL DEFAULT 1,"
        "  created_at  REAL NOT NULL DEFAULT 0,"
        "  updated_at  REAL NOT NULL DEFAULT 0"
        ")")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS fonts ("
        "  id     INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name   TEXT NOT NULL DEFAULT '',"
        "  family TEXT NOT NULL DEFAULT '',"
        "  path   TEXT NOT NULL DEFAULT '',"
        "  UNIQUE(path)"
        ")")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS download_tasks ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  book_url    TEXT NOT NULL DEFAULT '',"
        "  kind        TEXT NOT NULL DEFAULT '',"
        "  status      TEXT NOT NULL DEFAULT 'pending',"
        "  progress    REAL NOT NULL DEFAULT 0,"
        "  files       TEXT NOT NULL DEFAULT '[]',"
        "  created_at  REAL NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_tasks_book "
        "ON download_tasks(book_url)")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6：M6 存储收敛（契约 §2/§3 / 设计 §6.3）。

    本函数建 M6 三张主表，承接旧介质（bookSource.json / bookshelf.json /
    search-books.sqlite3）。旧介质的数据由 ``StorageService.migrate()`` 在
    on_start 时**只读导入**一次（导入是数据搬运，不属于 schema DDL，故放在
    storage.py 而非此处），本函数只负责建表骨架：

    - ``book_sources`` ← 旧 bookSource.json（契约 §2）：
      ``id``=bookSourceUrl 主键；``name/url/group/enabled/custom_order/
      check_state/respond_time`` 为查询友好的投影列；``json`` 列存
      :meth:`BookSource.to_dict()` 全文（互操作格式，导入导出直接消费）。
      ``check_state`` 为检测状态列（SourceCheckService 后续写入），迁移期留空。
    - ``bookshelf`` ← 旧 bookshelf.json（契约 §2）：
      ``book_url`` 主键；``source_url/name/author/kind/cover_url/in_bookshelf``
      投影列；``reading``(JSON) 阅读块**原样保留**；``vars``(JSON) 规则变量；
      ``payload`` 列存整条旧记录全文（保留 toc_url/intro/last_chapter 等
      Legado 别名字段，供继承的旧书架方法无损往返）；``updated_at`` 写库时间。
    - ``search_books`` ← 旧 search-books.sqlite3（契约 §2/§3）：
      ``book_url`` 主键；``name/author/kind/origin/source_group`` 为契约投影列；
      ``origin_order/saved_at/payload`` 为换源种子运行列（对齐旧 Room 表，
      TTL 24h 以 ``saved_at`` 毫秒为锚）；``updated_at``(REAL) 为契约 TTL 列。
      与 :class:`~app.services.search_model.SearchBookStore` 文件模式共建同一
      组查询列（name,author,origin_order），使其可无缝切主库后端。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS book_sources ("
        "  id           TEXT PRIMARY KEY,"
        "  name         TEXT NOT NULL DEFAULT '',"
        "  url          TEXT NOT NULL DEFAULT '',"
        "  `group`      TEXT NOT NULL DEFAULT '',"
        "  enabled      INTEGER NOT NULL DEFAULT 1,"
        "  custom_order INTEGER NOT NULL DEFAULT 0,"
        "  check_state  TEXT NOT NULL DEFAULT '',"
        "  respond_time INTEGER NOT NULL DEFAULT 0,"
        "  json         TEXT NOT NULL DEFAULT '{}'"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_book_sources_group "
        "ON book_sources(`group`, enabled)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS bookshelf ("
        "  book_url     TEXT PRIMARY KEY,"
        "  source_url   TEXT NOT NULL DEFAULT '',"
        "  name         TEXT NOT NULL DEFAULT '',"
        "  author       TEXT NOT NULL DEFAULT '',"
        "  kind         TEXT NOT NULL DEFAULT '',"
        "  cover_url    TEXT NOT NULL DEFAULT '',"
        "  in_bookshelf INTEGER NOT NULL DEFAULT 1,"
        "  reading      TEXT NOT NULL DEFAULT '{}',"
        "  vars         TEXT NOT NULL DEFAULT '{}',"
        "  payload      TEXT NOT NULL DEFAULT '{}',"
        "  updated_at   REAL   NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookshelf_source "
        "ON bookshelf(source_url)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_books ("
        "  book_url     TEXT PRIMARY KEY,"
        "  name         TEXT NOT NULL DEFAULT '',"
        "  author       TEXT NOT NULL DEFAULT '',"
        "  kind         TEXT NOT NULL DEFAULT '',"
        "  origin       TEXT NOT NULL DEFAULT '',"
        "  source_group TEXT NOT NULL DEFAULT '',"
        "  origin_order INTEGER NOT NULL DEFAULT 0,"
        "  saved_at     INTEGER NOT NULL DEFAULT 0,"
        "  payload      TEXT NOT NULL DEFAULT '{}',"
        "  updated_at   REAL   NOT NULL DEFAULT 0"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_books_name_author "
        "ON search_books(name, author, origin_order)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_books_origin "
        "ON search_books(origin)")


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7：``books.play_chapter_index`` —— 播放进度不再占用阅读进度。

    v4 只补了秒级的 ``play_position``，当时按"视频书的播放章节就是 ``chapter_index``"
    的设想复用了那一列（见 :func:`_migrate_v4`）。阅读器接出视频面板后这个设想站
    不住了：一本正文里嵌了几条视频的小说，用户点「在本页播放」放第 50 章的视频，
    就会把 ``chapter_index`` 改写成 50 —— 下次"继续阅读"从第 3 章被扔到第 50 章。

    所以播放列表的索引单列一列存；``play_position``（秒）留在原处不动。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
    if "play_chapter_index" not in cols:
        conn.execute(
            "ALTER TABLE books ADD COLUMN "
            "play_chapter_index INTEGER NOT NULL DEFAULT 0")


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v8：``books.chapter_paragraph`` —— 续读位置存**章内段序号**，不再只存裸像素。

    ``scroll_offset`` 是像素，而像素属于当时那一套装尺（字号/行距/版心/缩进）。换装尺
    重开一本书，同一个像素压着的已经不是原来那一段：#88 现场取证（同一篇 300 段对话体，
    存档 16 号、重开 20 号）中位差 **22 段**、最大 **73 段**，同套装尺重开才是 0 段。
    段序号与装尺无关，所以它才是那个能跨会话用的不变量（就地重排的 #87 已经先用过它）。

    列**可空**：NULL = 这条记录是加列之前存的，只能按老办法回像素。不做回填——回填要把
    像素反查成段号，而反查需要当时的版心高，那一栏没存过，猜出来的只会是个像模像样的错值。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
    if "chapter_paragraph" not in cols:
        conn.execute("ALTER TABLE books ADD COLUMN chapter_paragraph INTEGER")


# 逐版本迁移函数表：版本号 -> (conn) -> None
MIGRATIONS: dict[int, object] = {
    1: _migrate_v1, 2: _migrate_v2, 3: _migrate_v3, 4: _migrate_v4,
    5: _migrate_v5, 6: _migrate_v6, 7: _migrate_v7, 8: _migrate_v8}
