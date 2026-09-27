"""M3 书源域 SourceService 外壳（集成契约 §3，权威：M3书源域契约.md）。

外壳化旧 ``app.services.source_service.SourceService``：

- **行为零改动**：书源 CRUD / 分组 / 统计 / 导入导出 / 书架 notShelf / 换源落库等
  既有方法体全部经继承原样复用（M3 仍读写旧 ``bookSource.json`` 与
  ``bookshelf.json``，M6 存储收敛才切 SQLite book_sources）；
- **外壳新增**：Service 生命周期（on_start/on_stop）、``SourceChangedEvent`` 发布、
  异步入口（list/load/save/delete/set_enabled/set_group/import/export）、
  1.18 改封面（books 表 cover_url 列 + ``image-cache/covers/`` 本地落点 + Pillow 归一）；
- **校验保存入口**：保存/导入前经 CompatAuditService（并行子代理落地）做
  导入即审/保存即审（§3.7 触发点①②）；审计服务未就绪时降级为不阻断。

红线：本模块零 flet import；服务间只经 EventBus / 显式注入 / ServiceHub 解析。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from pathlib import Path

from app.core.events import ImportProgressEvent, SourceChangedEvent
from app.core.lifecycle import Service
from app.core.settings import SettingsService
from app.core.storage import StorageService
from app.models.book_source import BookSource
# 拓扑声明依赖（与 ReadingService 同款：此处 import 仅用于 dependencies 排序）。
from app.services.engine.rule_engine_service import RuleEngineService
# 外壳化源：既有方法体原样继承，行为零改动。
from app.services.source_service import SourceService as _LegacySourceService

logger = logging.getLogger(__name__)

# 本地封面归一后的最长边（px）；封面统一 JPEG，落在 image-cache/covers/。
_COVER_MAX_EDGE = 480
_COVER_QUALITY = 88

# 导入逐条进度的最小发布间隔（秒）：几百条源逐条发会把界面循环刷屏，
# 控制器那边还会再做一层 0.5s 合并。
IMPORT_PUBLISH_INTERVAL = 0.2


class SourceService(_LegacySourceService, Service):
    """书源域服务：书源 CRUD/分组/导入导出/书架 notShelf/改封面/校验保存入口。

    协作者（StorageService/SettingsService/RuleEngineService/CompatAuditService）
    全部可在构造期注入（测试替身）；未注入时 on_start 经 ServiceHub 解析。
    """

    name = "SourceService"
    dependencies: tuple[type[Service], ...] = (
        StorageService, SettingsService, RuleEngineService)

    def __init__(self, resource_dir: str | Path | None = None,
                 data_dir: str | Path | None = None,
                 cache_dir: str | Path | None = None,
                 storage: StorageService | None = None,
                 settings: SettingsService | None = None,
                 engine=None, auditor=None) -> None:
        # 先装 Service 外壳（status/bus/hub），再装旧业务逻辑。
        Service.__init__(self)
        self._storage = storage
        self._settings = settings
        self._engine = engine
        self._auditor = auditor
        # resource_dir：只读资源兜底（bundled bookSource.json / bookshelf.json），
        # 缺省指向项目根/resource（与 main.py 一致）；目录缺失也容忍（首启空库）。
        if resource_dir is None:
            resource_dir = Path(__file__).resolve().parents[3] / "resource"
        # 注入 StorageService 时其 data/cache 即落盘根，避免污染用户真实目录。
        if storage is not None:
            data_dir = data_dir or storage.paths.data
            cache_dir = cache_dir or storage.paths.cache
        _LegacySourceService.__init__(
            self, resource_dir, data_dir=data_dir, cache_dir=cache_dir)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        if self._settings is None and self.hub is not None:
            self._settings = self.hub.require(SettingsService)
        if self._engine is None and self.hub is not None:
            self._engine = self.hub.require(RuleEngineService)
        # CompatAuditService 由并行子代理落地；未注册时保存即审降级为不阻断。
        # F6：真实环境（hub 非空）下审计必需协作者缺失须可见——服务仍 RUNNING，
        # 写 logger.error + last_error 供仪表盘标橙告警，不 SUSPENDED、不阻断 CRUD。
        if self._auditor is None and self.hub is not None:
            try:
                from app.services.sources.compat_audit_service import (
                    CompatAuditService)
                self._auditor = self.hub.require(CompatAuditService)
            except Exception:
                self._auditor = None
                logger.error(
                    "SourceService 协作者缺失：CompatAuditService 未注册，"
                    "导入即审/保存即审降级（不阻断书源 CRUD）")
                self.status.last_error = "CompatAuditService 未注册，审计链路降级"
        # 加载书源 + 书架（行为同旧 load()）；首启无文件时退化为空库，不崩启动。
        try:
            self.load()
        except (FileNotFoundError, RuntimeError, OSError):
            self._sources = []
            self.load_bookshelf()
        # 上次运行没读完的试读不跨会话：对齐 Legado 启动时清理 notShelf 残留
        # （MainViewModel.init → BookDao.deleteNotShelfBook）。
        try:
            discarded = self.discard_unshelved_books()
            if discarded:
                self._drop_book_caches(discarded)
                logger.info("已清理 %d 条上次遗留的试读记录", len(discarded))
        except Exception as exc:  # 清理失败不阻断启动
            logger.warning("试读残留清理失败：%s", exc)
        logger.info("SourceService 已启动，书源 %d 条", len(self._sources))

    def _drop_book_caches(self, keys) -> None:
        """按 (source_url, book_url) 清掉试读残留的目录/正文缓存。"""
        try:
            from app.services.reading_cache import ReadingCache
            cache = ReadingCache(self.cache_dir)
        except Exception as exc:  # 缓存只是磁盘垃圾，清不掉也不报错上来
            logger.debug("试读缓存清理跳过：%s", exc)
            return
        for source_url, book_url in keys:
            try:
                cache.remove_book(source_url, book_url)
            except Exception as exc:
                logger.debug("清理试读缓存失败（%s/%s）：%s",
                             source_url, book_url, exc)

    async def on_stop(self) -> None:
        # 同步落盘兜底（既有 save/save_bookshelf 本身即原子写）。
        try:
            if self._sources:
                self.save()
        except Exception:
            logger.exception("SourceService 停止落盘异常")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _publish(self, event) -> list:
        if self.bus is None:
            return []
        return self.bus.publish(event)

    async def _audit(self, source_json: dict):
        """导入即审/保存即审（§3.7 触发点①②）；审计服务未就绪返回 None。"""
        auditor = self._auditor
        method = getattr(auditor, "audit_source", None) if auditor else None
        if method is None:
            return None
        result = method(source_json)
        if inspect.isawaitable(result):
            result = await result
        return result

    @property
    def _covers_dir(self) -> Path:
        """本地封面落点：image-cache/covers/（优先经 StorageService 分配）。"""
        if self._storage is not None:
            try:
                return self._storage.cache_dir("image-cache/covers")
            except Exception:
                pass
        return Path(self.cache_dir) / "image-cache" / "covers"

    def _normalize_cover(self, source_path: Path, book_url: str) -> Path:
        """Pillow 归一封面：转 RGB、等比缩到最长边、存 JPEG 到 covers 目录。"""
        from PIL import Image

        covers = self._covers_dir
        covers.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(str(book_url).encode("utf-8")).hexdigest()[:16]
        out = covers / f"{digest}.jpg"
        with Image.open(source_path) as im:
            im = im.convert("RGB")
            im.thumbnail((_COVER_MAX_EDGE, _COVER_MAX_EDGE))
            im.save(out, format="JPEG", quality=_COVER_QUALITY)
        return out

    # ------------------------------------------------------------------ #
    # M6 介质切换：书源/书架读写落点 JSON → 主库表（契约 §3）
    #
    # 只重写持久化原语（load/save/load_bookshelf/save_bookshelf），继承自旧
    # SourceService 的全部内存列表操作（增删改/notShelf/换源/进度回写）原样复用：
    # 它们仍改 self._sources / self.bookshelf，末尾调 save*/save_bookshelf 落库。
    # 主库未注入（无服务环境）时回退旧 JSON 行为，保证独立可跑。
    # ------------------------------------------------------------------ #
    @property
    def _db(self):
        """主库连接；未注入 StorageService 返回 None（回退旧 JSON 路径）。"""
        return self._storage.db if self._storage is not None else None

    def load(self, path=None) -> list[BookSource]:
        """M6：主库就绪时从 book_sources 表加载（json 列还原全文）；否则回退旧 JSON。"""
        conn = self._db
        if conn is not None and path is None:
            rows = conn.execute(
                "SELECT json FROM book_sources "
                "ORDER BY custom_order, rowid").fetchall()
            self._sources = []
            for r in rows:
                try:
                    self._sources.append(BookSource.from_dict(json.loads(r[0])))
                except Exception:
                    logger.warning("跳过非法书源行: %.80s", r[0])
            self._current_path = None  # M6 运行时不再以 bookSource.json 为落点
            self.load_bookshelf()
            return self._sources
        return super().load(path)

    def load_bookshelf(self) -> list[dict]:
        """M6：主库就绪时从 bookshelf 表加载（payload 列还原整条记录）。"""
        conn = self._db
        if conn is not None:
            rows = conn.execute("SELECT payload FROM bookshelf").fetchall()
            self.bookshelf = []
            for r in rows:
                try:
                    item = json.loads(r[0])
                    if isinstance(item, dict):
                        self.bookshelf.append(item)
                except Exception:
                    logger.warning("跳过非法书架行: %.80s", r[0])
            return self.bookshelf
        return super().load_bookshelf()

    def save(self, path=None) -> bool:
        """M6：整表镜像 self._sources（对齐旧整写 JSON 语义），不再写 bookSource.json。"""
        conn = self._db
        if conn is not None and path is None:
            rows = []
            for s in self._sources:
                rows.append((
                    s.bookSourceUrl, s.bookSourceName, s.bookSourceUrl,
                    s.bookSourceGroup or "", 1 if s.enabled else 0,
                    int(s.customOrder or 0), "", int(s.respondTime or 0),
                    json.dumps(s.to_dict(), ensure_ascii=False)))
            # B2：DELETE+INSERT 包进同一事务（持 _db_lock），中途崩溃回滚不留空表。
            with self._storage.transaction() as tx:
                tx.execute("DELETE FROM book_sources")
                tx.executemany(
                    "INSERT INTO book_sources(id,name,url,`group`,enabled,"
                    " custom_order,check_state,respond_time,json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", rows)
            return True
        return super().save(path)

    def save_bookshelf(self):
        """M6：整表镜像 self.bookshelf（reading/vars 列投影 + payload 全文保真）。"""
        conn = self._db
        if conn is not None:
            now = time.time()
            rows = []
            for item in self.bookshelf:
                book_url = str(item.get("bookUrl") or item.get("url") or "")
                rows.append((
                    book_url,
                    str(item.get("source_url") or item.get("origin") or ""),
                    str(item.get("name") or ""),
                    str(item.get("author") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("cover_url") or ""),
                    1 if item.get("in_bookshelf", True) else 0,
                    json.dumps(item.get("reading") or {}, ensure_ascii=False),
                    json.dumps(item.get("variables") or {}, ensure_ascii=False),
                    json.dumps(item, ensure_ascii=False),
                    now))
            # B2：整表镜像包事务，与 save() 同款原子语义。
            with self._storage.transaction() as tx:
                tx.execute("DELETE FROM bookshelf")
                tx.executemany(
                    "INSERT OR REPLACE INTO bookshelf(book_url,source_url,name,"
                    " author,kind,cover_url,in_bookshelf,reading,vars,payload,"
                    " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            return
        return super().save_bookshelf()

    # ------------------------------------------------------------------ #
    # 书源 CRUD / 分组（异步入口，契约 §3）
    # ------------------------------------------------------------------ #
    async def list_sources(self) -> list[BookSource]:
        """返回全部书源模型（只读快照，调用方勿回写）。"""
        return list(self._sources)

    async def load_source(self, url: str) -> BookSource | None:
        """按 bookSourceUrl 取单条书源。"""
        url = str(url or "")
        return next((s for s in self._sources if s.bookSourceUrl == url), None)

    async def save_source(self, source_json: dict) -> dict:
        """校验（导入即审/保存即审）+ upsert + 落盘 + 发事件。

        Returns:
            {"saved": bool, "source_id": str, "errors": list, "audit": Any}
        """
        if not isinstance(source_json, dict):
            return {"saved": False, "source_id": "", "errors": ["书源必须是 JSON 对象"],
                    "audit": None}
        source_id = str(source_json.get("bookSourceUrl") or "").strip()
        if not source_id:
            return {"saved": False, "source_id": "",
                    "errors": ["缺少 bookSourceUrl"], "audit": None}
        try:
            model = BookSource.from_dict(source_json)
        except Exception as exc:
            return {"saved": False, "source_id": source_id,
                    "errors": [f"书源结构非法: {exc}"], "audit": None}
        errors: list[str] = []
        audit = None
        try:
            audit = await self._audit(source_json)
        except Exception as exc:  # 审计缺口只告警，不阻断保存（§3.7）
            errors.append(f"静态审计未执行: {exc}")
        for index, existing in enumerate(self._sources):
            if existing.bookSourceUrl == source_id:
                self._sources[index] = model
                break
        else:
            self._sources.append(model)
        self.save()
        self._publish(SourceChangedEvent(change_type="saved", source_id=source_id))
        return {"saved": True, "source_id": source_id, "errors": errors,
                "audit": audit}

    async def delete_source(self, url: str) -> bool:
        """按 bookSourceUrl 删除并落盘。"""
        url = str(url or "")
        before = len(self._sources)
        self._sources = [s for s in self._sources if s.bookSourceUrl != url]
        removed = len(self._sources) != before
        if removed:
            self.save()
            self._publish(SourceChangedEvent(change_type="deleted", source_id=url))
        return removed

    async def set_enabled(self, url: str, enabled: bool) -> bool:
        """启用/停用单条书源。"""
        url = str(url or "")
        for source in self._sources:
            if source.bookSourceUrl == url:
                source.enabled = bool(enabled)
                self.save()
                self._publish(SourceChangedEvent(
                    change_type="enabled", source_id=url))
                return True
        return False

    async def batch_delete(self, urls: list[str]) -> int:
        """批量删除书源（按 bookSourceUrl），返回删除条数（一次落盘+一次事件）。"""
        targets = {str(u) for u in (urls or []) if u}
        if not targets:
            return 0
        before = len(self._sources)
        self._sources = [s for s in self._sources if s.bookSourceUrl not in targets]
        removed = before - len(self._sources)
        if removed:
            self.save()
            self._publish(SourceChangedEvent(
                change_type="deleted",
                source_id=",".join(sorted(targets)[:8])))
        return removed

    async def batch_set_enabled(self, urls: list[str], enabled: bool) -> int:
        """批量启用/停用书源，返回实际变更条数。"""
        targets = {str(u) for u in (urls or []) if u}
        if not targets:
            return 0
        changed = 0
        for source in self._sources:
            if source.bookSourceUrl in targets and bool(source.enabled) != bool(enabled):
                source.enabled = bool(enabled)
                changed += 1
        if changed:
            self.save()
            self._publish(SourceChangedEvent(
                change_type="enabled",
                source_id=",".join(sorted(targets)[:8])))
        return changed

    async def set_group(self, url: str, group: str) -> bool:
        """重设单条书源分组（空串归未分组）。"""
        url = str(url or "")
        for source in self._sources:
            if source.bookSourceUrl == url:
                source.bookSourceGroup = (group or None) or None
                self.save()
                self._publish(SourceChangedEvent(
                    change_type="group", source_id=url))
                return True
        return False

    # ------------------------------------------------------------------ #
    # 导入导出（JSON 互操作，契约 §3）
    # ------------------------------------------------------------------ #
    async def import_sources_json(self, text: str, mode: str = "append", *,
                                  audit: bool = True) -> tuple[int, list]:
        """从 JSON 文本导入书源（按 bookSourceUrl 去重，逐条审计告警）。

        ``mode``：``append`` 追加进现有列表；``replace`` **整表替换**（旧单体的导入页
        有这个选择，分层重构后只剩追加）。替换只在解析出至少一条有效书源时才动手，
        否则保持原列表不变——不能让一个坏文件把整张书源表清空。
        逐条阶段发 :class:`ImportProgressEvent`（几百条源要逐条审计，是秒级过程，
        界面靠它走进度条）。

        ``audit``：**"导入即审"（§3.7 触发点①）的归属就在这一条**——扫码导入、云端
        恢复这些不经 `OnlineImportService` 门面的入口全靠它，所以默认开。门面的那条路
        为了把兼容性缺口写进 `ImportResult.warnings` 已经自己逐条审过，就传 `False`：
        同一条源审两遍不只是白烧 CPU，还会往 `audit_log` 里塞双份记录（回溯时看不出
        哪条是这次导入的）。

        Returns:
            (成功导入条数, 错误/告警字符串列表)
        """
        try:
            data = json.loads(text)
        except Exception as exc:
            return 0, [f"JSON 解析失败: {exc}"]
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0, ["书源 JSON 必须是对象或对象数组"]
        if mode not in ("append", "replace"):
            return 0, [f"未知导入模式：{mode!r}"]
        total = len(data)
        errors: list[str] = []
        state = {"published_at": time.monotonic()}

        def publish(done: int, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - state["published_at"] < IMPORT_PUBLISH_INTERVAL:
                return
            state["published_at"] = now
            self._publish(ImportProgressEvent(
                kind="book_source", phase="校验并落库", done=done, total=total))

        parsed: list[BookSource] = []
        existing = {s.bookSourceUrl for s in self._sources}
        seen: set[str] = set()
        for index, item in enumerate(data, start=1):
            try:
                model = BookSource.from_dict(item)
            except Exception as exc:
                errors.append(f"跳过非法书源: {exc}")
                publish(index)
                continue
            sid = str(model.bookSourceUrl or "").strip()
            if not sid:
                # 没有 bookSourceUrl 的条目（典型：把 TXT 目录规则当成书源文件选进来）
                # 落库后既匹配不上也删不掉，还会互相撞成同一条空源——必须在门口拒掉。
                errors.append(f"跳过缺少 bookSourceUrl 的条目（第 {index} 项）")
                publish(index)
                continue
            if mode == "append" and (sid in existing or sid in seen):
                publish(index)
                continue
            seen.add(sid)
            if audit:
                try:
                    await self._audit(item)
                except Exception as exc:  # 审计告警不阻断导入
                    errors.append(f"{sid} 审计告警: {exc}")
            parsed.append(model)
            publish(index)
        publish(total, force=True)

        if not parsed:
            return 0, errors
        previous = list(self._sources)
        self._sources = parsed if mode == "replace" else previous + parsed
        try:
            self.save()
        except Exception as exc:
            self._sources = previous
            errors.append(f"保存失败，已恢复原有书源：{exc}")
            return 0, errors
        self._publish(SourceChangedEvent(change_type="imported", source_id=""))
        return len(parsed), errors

    async def export_sources_json(self) -> str:
        """导出全部书源为 JSON 文本（互操作格式）。"""
        return json.dumps(
            [s.to_dict() for s in self._sources], ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 书架 notShelf（语义对齐 找书与换源逻辑对齐.md §1.5，契约 §3）
    # ------------------------------------------------------------------ #
    def is_shelved(self, item_or_url) -> bool:
        """记录是否入书架。

        - 传 ``dict``（旧调用）：按旧静态语义判定（无标记视为已入架）；
        - 传 ``book_url``（str）：在书架记录中按 url 查找后判定。
        双形态兼容是外壳适配层，不改变旧判定语义。
        """
        if isinstance(item_or_url, dict):
            return _LegacySourceService.is_shelved(item_or_url)
        book_url = str(item_or_url or "")
        for record in self.bookshelf:
            if self.bookshelf_key(record)[1] == book_url:
                return bool(record.get("in_bookshelf", True))
        return False

    # shelved_books / add_to_bookshelf / discard_unshelved_books 直接继承旧实现。

    # ------------------------------------------------------------------ #
    # 书架异步入口（UI 侧唯一调用面）
    #
    # 继承来的书架方法是同步的，直接在界面循环上调用会把 SQLite 写盘压在 UI 线程
    # 上；这里包成协程——经 EngineProxy 派发后整条读改写都落在 EngineHub 执行域，
    # 控制器只管 await。语义仍由旧实现承担，本层不做第二个真相源。
    # ------------------------------------------------------------------ #
    async def list_bookshelf(self, include_staged: bool = False) -> list[dict]:
        """书架记录快照；默认排除 notShelf 试读记录（对齐 :meth:`shelved_books`）。"""
        self.load_bookshelf()
        if include_staged:
            return list(self.bookshelf)
        return list(self.shelved_books())

    async def shelve_book(self, source, book, shelved: bool = True) -> bool:
        """加书架；``shelved=False`` 即 Legado notShelf 试读暂存。

        试读过的书再点"加入书架"：``add_to_bookshelf`` 会因同键已存在而原样返回
        False，用户看到的却是"点了没反应"。Legado 里同一行只换 ``inBookshelf``
        标记，所以入架动作补一次转正，把暂存记录留在架上。
        """
        added = bool(self.add_to_bookshelf(source, book, shelved=shelved))
        if shelved and self.promote_to_bookshelf(
                str(getattr(source, "bookSourceUrl", "") or ""),
                str(getattr(book, "url", "") or "")):
            added = True
        if added:
            self._publish(SourceChangedEvent(
                change_type="shelved" if shelved else "staged",
                source_id=getattr(book, "url", "") or ""))
        return added

    async def unshelve_book(self, source_url: str, book_url: str) -> bool:
        """移出书架并落库（UI 的"移除"必须是持久化动作，不是本地列表过滤）。"""
        removed = bool(self.remove_from_bookshelf(source_url, book_url))
        if removed:
            self._publish(SourceChangedEvent(
                change_type="unshelved", source_id=str(book_url or "")))
        return removed

    async def promote_book(self, source_url: str, book_url: str) -> bool:
        """试读记录转正为书架记录（退出时"是否加入书架"的"是"分支）。"""
        promoted = bool(self.promote_to_bookshelf(source_url, book_url))
        if promoted:
            self._publish(SourceChangedEvent(
                change_type="shelved", source_id=str(book_url or "")))
        return promoted

    # ------------------------------------------------------------------ #
    # 1.18 改封面（books 表 cover_url + image-cache/covers/ 本地落点 + Pillow 归一）
    # ------------------------------------------------------------------ #
    async def update_cover(self, book_url: str, cover_url_or_path: str) -> str:
        """覆盖一本书的封面。

        ``cover_url_or_path`` 为本地已存在文件时：Pillow 归一后落到
        ``image-cache/covers/``，cover_url 指向本地文件；否则按远端 URL 原样记录
        （候选图抓取/下载复用 RuleEngineService 图片通道，M3 不在服务内联网）。
        同时回写 bookshelf.json 记录与 SQLite ``books.cover_url`` 列。
        """
        book_url = str(book_url or "")
        resolved = str(cover_url_or_path or "")
        local = Path(resolved)
        if resolved and local.exists() and local.is_file():
            resolved = str(self._normalize_cover(local, book_url))
        changed = False
        for record in self.bookshelf:
            if self.bookshelf_key(record)[1] == book_url:
                record["cover_url"] = resolved
                changed = True
        if changed:
            self.save_bookshelf()
        conn = self._storage.db if self._storage is not None else None
        if conn is not None and book_url:
            try:
                # B1：写路径经 StorageService 持锁助手（同一把 _db_lock）。
                self._storage.execute(
                    "UPDATE books SET cover_url=?, updated_at=? WHERE book_url=?",
                    (resolved, time.time(), book_url))
            except Exception:
                logger.exception("回写 books.cover_url 失败（已忽略）")
        self._publish(SourceChangedEvent(
            change_type="cover_updated", source_id=book_url))
        return resolved
