"""RssService：RSS 订阅源 / 文章列表 / 已读 / 收藏（M4 内容形态域，契约 §3）。

对标 Legado 4 的 RSS 四实体（rssSources / rssArticles / rssReadRecords /
rssStars），按集成契约 §3 授权**简化**为两张表：

- ``rss_sources``：订阅源（id/name/url/enabled/source_group/custom_order +
  规则订阅字段 rule_*）；
- ``rss_articles``：文章（source_id/title/url/author/pub_date/content/image +
  ``read_flag``/``star_flag`` 两个简化列，承载 Legado 的 rssReadRecords/rssStars）。

抓取与解析分流（契约 §3）：

- **feed 抓取**走 RuleEngineService 网络栈（会话复用 / UA / 超时 / cookie 语义
  一致，§2 网络组修复落点）；本服务不另起 aiohttp 会话；
- **标准 feed**（RSS 2.0 / Atom / RDF）用 ``feedparser`` 解析（本服务懒加载，
  仅此服务 import，未启用 RSS 时不加载）；
- **规则订阅**（源配置带 rule_articles 等规则字段时）用 legado_rule_parser
  体系兜底解析文章列表 / 正文——标准 feed 解析不动，仅在源带规则时接管。

事件与状态：每源刷新完成发 :class:`RssRefreshedEvent`（source_id, new_count）；
刷新进度写 ``status.progress``（0-1），供仪表盘 / RSS 视图进度条消费。

硬约束：零 flet import；服务间只经 EventBus / 显式接口 / hub.require；
跨平台纯 Python（无平台分支）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from app.core.events import (RssRefreshedEvent, RssRefreshProgressEvent)
from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService

logger = logging.getLogger(__name__)

# fetcher 注入面：async callable(url) -> str（返回 feed 原文）。
# 默认走 RuleEngineService 网络栈；测试注入本地文件读取以离线跑（不联网）。
Fetcher = Callable[[str], Awaitable[str]]


class RssService(Service):
    """RSS 源 / 文章的 CRUD + 刷新 + 已读 / 收藏。"""

    name = "RssService"
    # 依赖：存储落库 + RuleEngineService 网络栈（契约 §1 依赖列）。
    # SettingsService 仅由 RuleEngineService 自身消费 UA/超时偏好，本服务不直读，
    # 故不列入 dependencies 以减少启动耦合（契约列它是"可用偏好"，非硬依赖）。
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None,
                 rule_engine=None, fetcher: Fetcher | None = None) -> None:
        super().__init__()
        self._storage = storage
        self._engine_service = rule_engine
        # fetcher 注入：None 时用 _default_fetch（RuleEngine 网络栈）。
        self._fetcher = fetcher
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        if self._engine_service is None and self.hub is not None:
            # RuleEngineService 是阅读域重服务，经 hub.require 取原始实例；
            # 取引擎实例走其 LRU 池（_get_engine），复用同一 aiohttp 会话。
            try:
                from app.services.engine.rule_engine_service import (
                    RuleEngineService)
                self._engine_service = self.hub.require(RuleEngineService)
            except Exception:  # 阅读域未注册时降级：只能用注入的 fetcher
                logger.info("RssService：RuleEngineService 未注册，"
                            "网络抓取依赖注入 fetcher")
        logger.info("RssService 已启动")

    def _conn(self) -> LockedConnection | None:
        return self._storage.db if self._storage is not None else None

    # ------------------------------------------------------------------ #
    # 源 CRUD（契约 §3：list_sources / add_source / remove_source）
    # ------------------------------------------------------------------ #
    async def list_sources(self) -> list[dict]:
        """列出全部订阅源（按 custom_order、name 排序）。"""
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT id, name, url, enabled, source_group, custom_order,"
                "       type, last_update_time"
                " FROM rss_sources"
                " ORDER BY custom_order ASC, name ASC"
            ).fetchall()
        return [
            {
                "id": r[0], "name": r[1], "url": r[2],
                "enabled": bool(r[3]), "group": r[4],
                "custom_order": int(r[5]), "type": int(r[6]),
                "last_update_time": float(r[7]),
            }
            for r in rows
        ]

    async def article_counts(self) -> dict[str, dict]:
        """每源的文章总数与未读数 ``{source_id: {"total": n, "unread": m}}``。

        订阅列表要有未读角标，逐源调 ``list_articles`` 就是 N 次查询；一条
        GROUP BY 出全部。没有文章的源不出现在结果里（界面按缺省 0 处理）。
        """
        conn = self._conn()
        if conn is None:
            return {}
        with self._lock:
            rows = conn.execute(
                "SELECT source_id, COUNT(*), SUM(CASE WHEN read_flag=0 THEN 1 ELSE 0 END)"
                " FROM rss_articles GROUP BY source_id").fetchall()
        return {str(r[0]): {"total": int(r[1] or 0), "unread": int(r[2] or 0)}
                for r in rows}

    async def add_source(self, name: str, url: str,
                         source_group: str = "") -> str:
        """新增订阅源，返回源 id（即规范化 url，对标 Legado sourceUrl 主键）。

        同一 url 重复添加时更新 name/group（ON CONFLICT），不产生重复行。
        """
        url = str(url or "").strip()
        if not url:
            raise ValueError("RSS 源 url 不能为空")
        name = str(name or "").strip() or url
        source_id = url  # id 即 url，与 Legado sourceUrl 主键语义一致
        conn = self._conn()
        if conn is not None:
            with self._lock:
                conn.execute(
                    "INSERT INTO rss_sources(id, name, url, source_group)"
                    " VALUES(?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET"
                    "   name=excluded.name, source_group=excluded.source_group",
                    (source_id, name, url, str(source_group or "")))
        return source_id

    async def remove_source(self, source_id: str) -> bool:
        """删除订阅源及其全部文章（含已读/收藏状态）。"""
        conn = self._conn()
        changed = False
        if conn is not None:
            with self._lock:
                conn.execute("DELETE FROM rss_articles WHERE source_id=?",
                             (str(source_id),))
                cur = conn.execute("DELETE FROM rss_sources WHERE id=?",
                                   (str(source_id),))
                changed = cur.rowcount > 0
        return changed

    # ------------------------------------------------------------------ #
    # 导入导出（WebDAV 备份成员 rssSources.json；方法名即服务侧鸭子探测的契约）
    # ------------------------------------------------------------------ #
    async def export_sources_json(self) -> str:
        """导出订阅源列表（Legado rssSources 语义：源地址即主键，不含文章）。"""
        rows = await self.list_sources()
        return json.dumps(
            [{"name": r["name"], "url": r["url"], "group": r["group"] or "",
              "enabled": r["enabled"], "custom_order": r["custom_order"],
              "type": r["type"]} for r in rows], ensure_ascii=False, indent=2)

    async def import_sources_json(self, text: str) -> tuple[int, list[str]]:
        """从 JSON 导入订阅源；``add_source`` 按 url 合并，重复导入不产生新行。"""
        try:
            data = json.loads(text)
        except Exception as exc:
            return 0, [f"JSON 解析失败: {exc}"]
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0, ["RSS 源 JSON 必须是对象或对象数组"]
        known = {r["url"] for r in await self.list_sources()}
        errors: list[str] = []
        added = 0
        for item in data:
            if not isinstance(item, dict):
                errors.append("跳过非对象源")
                continue
            url = str(item.get("url", "") or item.get("sourceUrl", "") or "").strip()
            if not url:
                errors.append("跳过无 url 的源")
                continue
            if url in known:
                continue
            try:
                await self.add_source(str(item.get("name", "") or ""), url,
                                      str(item.get("group", "") or ""))
            except Exception as exc:
                errors.append(f"保存失败: {exc}")
                continue
            known.add(url)
            added += 1
        return added, errors

    # ------------------------------------------------------------------ #
    # 刷新（契约 §3：refresh_source / refresh_all）
    # ------------------------------------------------------------------ #
    async def refresh_source(self, source_id: str) -> int:
        """抓取并刷新单个源，返回新增文章数；完成发 RssRefreshedEvent。"""
        source_id = str(source_id or "")
        conn = self._conn()
        if conn is None or not source_id:
            return 0
        row = conn.execute(
            "SELECT id, url, rule_articles FROM rss_sources WHERE id=?",
            (source_id,)).fetchone()
        if row is None:
            logger.warning("refresh_source：源不存在 %s", source_id)
            return 0
        url, rule_articles = row[1], row[2]

        self._set_progress(0.0)
        try:
            raw = await self._fetch(url)
        except Exception as exc:  # 抓取失败不阻断 refresh_all，记状态后上抛
            self.status.last_error = f"feed 抓取失败: {exc}"
            logger.exception("RssService 抓取失败 %s", url)
            raise
        finally:
            self._set_progress(0.5)

        # 分流：带规则字段走 legado_rule_parser 兜底，否则 feedparser 标准解析
        if str(rule_articles or "").strip():
            articles = self._parse_rule_feed(raw, rule_articles)
        else:
            articles = self._parse_feedparser(raw)

        new_count = self._upsert_articles(source_id, articles)
        now = time.time()
        with self._lock:
            conn.execute(
                "UPDATE rss_sources SET last_update_time=? WHERE id=?",
                (now, source_id))
        self._set_progress(1.0)
        self._publish(RssRefreshedEvent(source_id=source_id, new_count=new_count))
        return new_count

    async def refresh_all(self) -> int:
        """刷新全部启用源，返回新增文章总数；逐源发 RssRefreshedEvent。"""
        sources = await self.list_sources()
        enabled = [s for s in sources if s["enabled"]]
        total_new = 0
        # 逐源发进度：多个源串行抓 feed，没有事件的话界面在整批期间一动不动。
        self._publish_progress(0, len(enabled), "")
        for index, src in enumerate(enabled):
            # 进度按源数推进：0.1 起步，每源占一段
            self._publish_progress(index, len(enabled), src["name"] or src["id"])
            self._set_progress(0.1 + 0.8 * (index / max(1, len(enabled))))
            try:
                total_new += await self.refresh_source(src["id"])
            except Exception:
                # 单源失败不拖垮其余源（§4.4 异常策略哲学）
                logger.warning("refresh_all：源 %s 刷新失败，继续其余", src["id"])
        self._publish_progress(len(enabled), len(enabled), "")
        self._set_progress(1.0)
        return total_new

    # ------------------------------------------------------------------ #
    # 文章列表 / 已读 / 收藏（契约 §3）
    # ------------------------------------------------------------------ #
    async def list_articles(self, source_id: str,
                            unread_only: bool = False) -> list[dict]:
        """列出某源文章（按发布时间 / 更新时间倒序）。"""
        conn = self._conn()
        if conn is None:
            return []
        sql = (
            "SELECT id, source_id, title, url, author, pub_date,"
            "       content, image, read_flag, star_flag, updated_at"
            " FROM rss_articles WHERE source_id=?"
        )
        params: list[Any] = [str(source_id)]
        if unread_only:
            sql += " AND read_flag=0"
        sql += " ORDER BY pub_date DESC, id DESC"
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": int(r[0]), "source_id": r[1], "title": r[2], "url": r[3],
                "author": r[4], "pub_date": r[5], "content": r[6], "image": r[7],
                "read": bool(r[8]), "starred": bool(r[9]),
                "updated_at": float(r[10]),
            }
            for r in rows
        ]

    async def mark_read(self, article_id: int) -> bool:
        """标记文章已读（read_flag=1，折叠 Legado rssReadRecords）。"""
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            cur = conn.execute(
                "UPDATE rss_articles SET read_flag=1, updated_at=?"
                " WHERE id=?",
                (time.time(), int(article_id)))
        return cur.rowcount > 0

    async def star(self, article_id: int, starred: bool) -> bool:
        """设置文章收藏（star_flag，折叠 Legado rssStars）。"""
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            cur = conn.execute(
                "UPDATE rss_articles SET star_flag=?, updated_at=?"
                " WHERE id=?",
                (1 if starred else 0, time.time(), int(article_id)))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # 内部：抓取（RuleEngineService 网络栈）
    # ------------------------------------------------------------------ #
    async def _fetch(self, url: str) -> str:
        """取 feed 原文。注入 fetcher 时用之（测试离线），否则走 RuleEngine 网络栈。"""
        if self._fetcher is not None:
            return await self._fetcher(url)
        if self._engine_service is None:
            raise RuntimeError("未注入 fetcher 且 RuleEngineService 不可用，"
                               "无法抓取 feed")
        # 复用 RuleEngineService 的默认引擎实例（LRU 池 + 同一 aiohttp 会话 /
        # UA / 超时 / cookie）。_get_engine/_request 是引擎网络栈入口，契约 §3
        # 要求 feed 抓取走该栈，故直接复用其私有入口（不另起会话）。
        engine = await self._engine_service._get_engine(None)
        body, _content_type, _final_url = await engine._request(
            {"url": url, "method": "GET"})
        return body

    # ------------------------------------------------------------------ #
    # 内部：标准 feed 解析（feedparser，懒加载）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_feedparser(raw: str) -> list[dict]:
        """用 feedparser 解析 RSS/Atom/RDF 原文为文章字典列表（懒加载）。"""
        import feedparser  # 懒加载：仅本服务 import（契约 §3 硬约束）

        parsed = feedparser.parse(raw)
        articles: list[dict] = []
        for entry in getattr(parsed, "entries", []) or []:
            link = (entry.get("link") or "").strip()
            if not link:
                continue  # 无链接的条目无法作为文章唯一键，跳过
            title = entry.get("title") or link
            author = entry.get("author") or ""
            pub_date = (entry.get("published") or entry.get("updated") or "")
            content = (entry.get("summary") or entry.get("description") or "")
            # 封面图：feedparser 把媒体图放进 media_thumbnail / media_content
            image = ""
            thumbs = entry.get("media_thumbnail") or []
            if thumbs:
                image = thumbs[0].get("url", "") if isinstance(thumbs, list) else ""
            articles.append({
                "title": str(title), "url": link, "author": str(author),
                "pub_date": str(pub_date), "content": str(content),
                "image": str(image),
            })
        return articles

    # ------------------------------------------------------------------ #
    # 内部：规则订阅兜底（legado_rule_parser 体系）
    # ------------------------------------------------------------------ #
    def _parse_rule_feed(self, raw: str, rule_articles: str) -> list[dict]:
        """源带规则字段时，用 legado_rule_parser 解析 HTML 文章列表（兜底）。

        M4 最小可用：仅按 ``rule_articles`` 抽出列表节点，再按节点内链接 /
        标题做近似提取；完整 Legado RssSource 规则（ruleTitle/ruleLink/…）
        逐字段求值随 M5 RSS 编辑器补全，此处结构先就位。
        """
        from app.services.engine.legado_rule_parser import (
            evaluate, parse_markup)

        root = parse_markup(raw)
        nodes = evaluate(root, rule_articles, "html", list_context=True)
        articles: list[dict] = []
        for node in nodes:
            links = evaluate(node, "tag@href", "html")
            titles = evaluate(node, "text", "html")
            link = str(links[0]) if links else ""
            title = str(titles[0]) if titles else (link or "(无标题)")
            if not link:
                continue
            articles.append({
                "title": title, "url": link, "author": "",
                "pub_date": "", "content": "", "image": "",
            })
        return articles

    # ------------------------------------------------------------------ #
    # 内部：文章落库（按 (source_id,url) 去重，保留已读/收藏状态）
    # ------------------------------------------------------------------ #
    def _upsert_articles(self, source_id: str, articles: list[dict]) -> int:
        conn = self._conn()
        if conn is None or not articles:
            return 0
        now = time.time()
        with self._lock:
            existing = {
                r[0] for r in conn.execute(
                    "SELECT url FROM rss_articles WHERE source_id=?",
                    (source_id,)).fetchall()
            }
            new_count = 0
            for art in articles:
                url = art["url"]
                if url in existing:
                    # 已存在：刷新标题/正文/日期，但**不动** read_flag/star_flag
                    # （用户已读/收藏状态不得被刷新覆盖）
                    conn.execute(
                        "UPDATE rss_articles SET title=?, content=?,"
                        " pub_date=?, author=?, updated_at=?"
                        " WHERE source_id=? AND url=?",
                        (art["title"], art["content"], art["pub_date"],
                         art["author"], now, source_id, url))
                    continue
                conn.execute(
                    "INSERT INTO rss_articles"
                    "(source_id, title, url, author, pub_date, content, image,"
                    " updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (source_id, art["title"], url, art["author"],
                     art["pub_date"], art["content"], art["image"], now))
                existing.add(url)
                new_count += 1
        return new_count

    # ------------------------------------------------------------------ #
    def _set_progress(self, value: float) -> None:
        self.status.progress = float(value)
        self.status.updated_at = time.time()

    def _publish(self, event: RssRefreshedEvent) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def _publish_progress(self, done: int, total: int, current: str) -> None:
        if self.bus is not None:
            self.bus.publish(
                RssRefreshProgressEvent(done=int(done), total=int(total),
                                        current=str(current or "")))
