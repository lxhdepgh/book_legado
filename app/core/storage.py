"""StorageService：SQLite 主库 + 文件目录分配 + schema 迁移框架（§6）。

- 主库 ``legado.db`` 落在 :class:`~app.services.app_paths.AppPaths.data`，
  Python 标准库 sqlite3 独占访问；连接跨线程使用（EngineHub 在别的 loop 上
  读写），故 ``check_same_thread=False`` 并以一把互斥锁串行化使用。锁不靠调用方
  自觉：``db`` 交出去的是 :class:`LockedConnection`（每条语句自持锁），裸连接只
  经 :attr:`StorageService.raw` 拿，且只用于生命周期与测试内省；
- schema 版本化：``PRAGMA user_version`` + 逐版本迁移函数表，v1 建
  ``settings`` / ``cookies`` 两张表；schema 定义与迁移函数见
  ``app/storage/schema.py``（§9.1 权威位置）；
- 文件目录：``cache_dir(kind)`` 分配 reader-cache/image-cache 等缓存目录，
  ``download_dir()`` 分配导出/下载目录；
- 模块级访问器 :func:`default_storage` / :func:`storage_db`：引擎模块在无服务
  环境（单测/旧路径直跑）下取到 None，自行回退旧行为。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.core.lifecycle import Service
from app.services.app_paths import AppPaths, default_app_paths
from app.storage.schema import MIGRATIONS, SCHEMA_VERSION

logger = logging.getLogger(__name__)

# 迁移幂等标记键（settings 表）：值为 JSON 文本 "1"。二次启动见标记即跳过导入。
_MIGRATED_FLAG = "m6.legacy_migrated"
# 旧 search-books.sqlite3 的 TTL：24h（毫秒），导入时不做过期过滤（旧库本就
# 只存近期搜索结果），清理仍由 SearchBookStore.clear_expired 运行时按 24h 执行。


# 逐版本迁移函数表定义在 app/storage/schema.py（§9.1），此处直接消费。


class LockedConnection:
    """``StorageService.db`` 交出去的唯一句柄：每条语句自己持主库锁。

    为什么堵在这里，而不是把那 54 条调用点逐个改成 ``storage.execute()``（12 个
    模块、28 写 + 24 读 + 1 DDL + 1 条动词拼在变量里，``.probe/scan_raw_db_writes.py``
    数的）：病灶不是"少写了两个字母"，是**一条跨线程共享的 autocommit 连接**。
    裸句柄落在别人 ``transaction()`` 里时不报错，而是静默并进那笔未提交的事务，
    对方 ROLLBACK 就把它一起带走——实测书签 INSERT / 设置 UPSERT / 阅读进度 UPSERT
    三种形状都是 0.0~0.1 ms 返回、回滚之后计数 0，调用方看到的还是成功；两条线程
    同时裸写还会互踩 ``InterfaceError``（8 线程 × 240 条 × 3 遍只活下 199~202 行、
    抛 38~41 次，``.probe/raw_db_handle_cost.py``）。逐点改写挡不住下一个写这类
    代码的人，堵住分配口才挡得住。

    面只有一点点大，是数出来的（``.probe/attr_inventory_raw_db.py``）：这 54 条在
    连接上只碰 ``.execute``（40 条住在 ``async def``、14 条住在 ``def``），没有
    ``cursor()``/``executescript()``/``with conn:``/``row_factory``。所以这里只放
    **语句**，别的一律 ``AttributeError``——宁可让新用法在这里撞墙、显式去拿
    :attr:`StorageService.raw`，也不要它绕过锁。

    锁为什么盖到 ``execute()`` 就够（不用再包 fetch）：实测 SELECT 的读快照在
    ``execute()`` 里就定了，行是之后才 materialize（20000 行 execute 0.1~0.2 ms /
    fetchall 7.0~15.9 ms），而 execute 之后再提交的行，这个游标看不见（20000 行，
    不是 20001）。所以拿到锁里的那次 execute，就等于拿到了"别人事务之外"的快照。

    代价量过，两条都不成立所以都没做（``.probe/raw_db_handle_cost.py``）：无争用时
    锁根本量不出成本——同一条 INSERT 裸写 3190.4~3293.3 µs/条、走锁 3039.5~3185.9
    µs/条，三遍差值 −4.5~−232.9 µs（走锁那侧一次都没慢过，分母是逐条 fsync 的
    3.2 ms 本身）；也不需要顺手把 40 条住在 ``async def`` 里的写挪出界面循环——
    收进锁之后最坏要等的就是别人那条整表镜像，实测 1000 源 ``save()`` 持锁
    15.8~29.3 ms、600 书架 ``save_bookshelf()`` 8.4~15.6 ms。

    第四十八轮在今天的树上重量了同一件事（``.probe/ui_write_stall_attr.py``，
    99 处主库语句 / 66 条写，其中 25 条住在 ``async def`` 里 = 就在界面循环上）：
    界面循环上一次单条写自持锁中位 3.37 ms / 最坏 7.80 ms；排在四种真实持有者后面
    最坏 25.59 ms（1000 源镜像，它自己持锁中位 15.08 / 最坏 21.84 ms）。首启
    ``migrate()`` 外层持锁 35.7 ms，但结构上撞不上界面循环——``main.py`` 先
    ``asyncio.run(hub.start_all())`` 再 ``ft.run``。结论不变：**单条写不搬**。
    搬的只有那一条批量写（书内 FTS 索引逐块 INSERT），见
    ``ContentSearchService._rebuild_sync``。
    """

    __slots__ = ("_service",)

    def __init__(self, service: "StorageService") -> None:
        self._service = service

    def execute(self, sql: str, params=()):
        return self._service.execute(sql, params)

    def executemany(self, sql: str, seq):
        return self._service.executemany(sql, seq)

    @property
    def in_transaction(self) -> bool:
        """当前连接是否已在事务里（持锁读，避免读到别人半途的状态）。"""
        with self._service._db_lock:
            return self._service._conn.in_transaction

    def transaction(self):
        """一批语句占住同一把锁（委托给 :meth:`StorageService.transaction`）。"""
        return self._service.transaction()

    def __getattr__(self, name: str):
        raise AttributeError(
            f"主库共享连接只能经持锁句柄使用（execute/executemany/transaction/"
            f"in_transaction），{name!r} 不在其上；确需裸连接（生命周期、测试内省）"
            f"请用 StorageService.raw")


class StorageService(Service):
    """主库与缓存目录的唯一分配方（§6.2 三类介质中的前两类）。"""

    name = "StorageService"
    dependencies: tuple[type[Service], ...] = ()

    def __init__(self, paths: AppPaths | None = None) -> None:
        super().__init__()
        self._paths: AppPaths = paths if paths is not None else default_app_paths()
        self._conn: sqlite3.Connection | None = None
        # RLock：migrate() 外层持锁后，各介质导入助手仍可重入同一把锁。
        self._db_lock = threading.RLock()
        self._guarded: LockedConnection | None = None

    # ------------------------------------------------------------------ #
    # 访问面
    # ------------------------------------------------------------------ #
    @property
    def db(self) -> LockedConnection | None:
        """已打开的主库句柄（未启动为 None）；**每条语句自持 ``_db_lock``**。"""
        if self._conn is None:
            return None
        if self._guarded is None:
            self._guarded = LockedConnection(self)
        return self._guarded

    @property
    def raw(self) -> sqlite3.Connection | None:
        """真连接：只给生命周期（close）与测试内省（trace callback）用。

        生产写路径**不该**出现在这里——见 :class:`LockedConnection`。
        """
        return self._conn

    @property
    def db_path(self) -> Path:
        return self._paths.data / "legado.db"

    @property
    def paths(self) -> AppPaths:
        return self._paths

    # ------------------------------------------------------------------ #
    # 线程安全访问助手（§6：写路径统一过 _db_lock；批量写进事务）
    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params=()):
        """持锁执行单条语句，返回 cursor（SQL 与裸 conn.execute 一致）。"""
        assert self._conn is not None, "StorageService 未启动"
        with self._db_lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq):
        """持锁批量执行，返回 cursor。"""
        assert self._conn is not None, "StorageService 未启动"
        with self._db_lock:
            return self._conn.executemany(sql, seq)

    @contextmanager
    def transaction(self):
        """事务上下文管理器：持 _db_lock + BEGIN IMMEDIATE/COMMIT/ROLLBACK。

        连接以 ``isolation_level=None``（autocommit）打开，故显式包事务。
        批量写（DELETE+INSERT 整表镜像）经此原子化：中途异常回滚，不留空表。

        同一线程重入（``_db_lock`` 是 RLock）时**加入**外层那笔而不是再 BEGIN：
        换源扫描在一条事务里批量落种子，种子库 ``SearchBookStore._batch`` 走的正是
        这里，抢先 BEGIN 会撞上 ``Transaction already open``。检查放在锁内是必须的
        ——只有持锁者才可能开着事务，锁外的 ``in_transaction`` 读的是别人半途的状态。
        """
        assert self._conn is not None, "StorageService 未启动"
        with self._db_lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                # BaseException 而不是 Exception：中断/生成器被丢弃时也必须收尾，
                # 否则锁放手了、事务还开着，后面所有人的写都会接进这笔残局。
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # Service 生命周期
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        self._paths.data.mkdir(parents=True, exist_ok=True)
        self._paths.cache.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self.ensure_schema()
        # M6：schema 就绪后立刻把旧介质只读导入主库一次（幂等），再 bind 全局
        # 访问器——保证 SourceService 等后续服务 on_start 读到的已是主库数据。
        self.migrate()
        _bind(self)
        logger.info("StorageService 主库就绪: %s", self.db_path)

    async def on_stop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        _unbind(self)

    # ------------------------------------------------------------------ #
    # schema 迁移
    # ------------------------------------------------------------------ #
    def ensure_schema(self) -> int:
        """按 PRAGMA user_version 逐版本迁移，返回迁移后的版本号。"""
        assert self._conn is not None, "StorageService 未启动"
        with self._db_lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for target in range(version + 1, SCHEMA_VERSION + 1):
                migr = MIGRATIONS.get(target)
                if migr is None:
                    raise RuntimeError(f"缺少 schema v{target} 的迁移函数")
                migr(self._conn)
                self._conn.execute(f"PRAGMA user_version = {target}")
                logger.info("StorageService schema 迁移到 v%d", target)
        return SCHEMA_VERSION

    # ------------------------------------------------------------------ #
    # M6 旧介质只读迁移（契约 §2 / 设计 §6.4）
    # ------------------------------------------------------------------ #
    def migrate(self) -> dict:
        """启动时把旧介质只读导入主库一次，完成后旧文件改名 .legacy-bak。

        幂等：settings 表写入 ``m6.legacy_migrated`` 标记，二次调用（或二次启动）
        见标记即直接返回空报告，不重复导入。旧文件**只改名不删除**，回滚 = 恢复
        原文件名。导入任一介质异常都只记录、不阻断启动，且该文件不改名（保留现场）。

        Returns:
            迁移报告 dict：``book_sources/bookshelf/search_books/appearance``
            各类导入条数、``missing`` 字段缺失/非法清单、``backed_up`` 已改名文件、
            ``already_migrated`` 是否为二次跳过。
        """
        assert self._conn is not None, "StorageService 未启动"
        report: dict = {
            "book_sources": 0, "bookshelf": 0, "search_books": 0,
            "appearance": 0, "missing": [], "backed_up": [],
            "already_migrated": False,
        }
        with self._db_lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?",
                (_MIGRATED_FLAG,)).fetchone()
            if row is not None and row[0] == json.dumps("1"):
                report["already_migrated"] = True
                logger.info("StorageService.migrate 已迁移过，跳过")
                return report

            data = self._paths.data
            counts = {
                "book_sources": self._migrate_book_sources(data, report),
                "bookshelf": self._migrate_bookshelf(data, report),
                "search_books": self._migrate_search_books(data, report),
                "appearance": self._migrate_appearance(data, report),
            }
            report.update(counts)
            # 全部（部分失败也无妨）导入尝试结束后打幂等标记。
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_MIGRATED_FLAG, json.dumps("1")))
        logger.info("StorageService.migrate 完成: %s",
                    {k: report[k] for k in
                     ("book_sources", "bookshelf", "search_books",
                      "appearance", "backed_up")})
        return report

    # -- 各介质导入助手：返回导入条数，异常吞掉并记 report["missing"] ---- #
    def _rename_after(self, path: Path, report: dict) -> bool:
        """导入成功后把旧文件改名 .legacy-bak（不覆盖已有备份）。"""
        bak = path.with_name(path.name + ".legacy-bak")
        try:
            if not bak.exists():
                path.rename(bak)
            report["backed_up"].append(path.name + ".legacy-bak")
            return True
        except OSError as exc:
            report["missing"].append(f"改名备份失败 {path.name}: {exc}")
            return False

    def _migrate_book_sources(self, data: Path, report: dict) -> int:
        path = data / "bookSource.json"
        if not path.exists():
            return 0
        try:
            data_obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["missing"].append(f"bookSource.json 读取失败: {exc}")
            return 0
        items = data_obj if isinstance(data_obj, list) else [data_obj]
        rows: list[tuple] = []
        for item in items:
            if not isinstance(item, dict):
                report["missing"].append("bookSource: 非对象条目已跳过")
                continue
            bid = str(item.get("bookSourceUrl") or "").strip()
            if not bid:
                report["missing"].append("bookSource: 缺 bookSourceUrl 已跳过")
                continue
            enabled = item.get("enabled", True)
            rows.append((
                bid, str(item.get("bookSourceName") or ""), bid,
                str(item.get("bookSourceGroup") or ""),
                1 if enabled else 0,
                int(item.get("customOrder") or 0), "",
                int(item.get("respondTime") or 0),
                json.dumps(item, ensure_ascii=False)))
        # 一批只提交一次：主库连接是 isolation_level=None（autocommit），
        # 裸 executemany 会逐行提交、每行一次 fsync——本机合成 3300 行实测
        # 3.28 ms/行，首次启动光这段就 2620ms。见 _migrate_* 共同的这条约束。
        with self.transaction() as db:
            db.executemany(
                "INSERT OR REPLACE INTO book_sources"
                "(id,name,url,`group`,enabled,custom_order,check_state,"
                " respond_time,json) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        self._rename_after(path, report)
        return len(rows)

    def _migrate_bookshelf(self, data: Path, report: dict) -> int:
        path = data / "bookshelf.json"
        if not path.exists():
            return 0
        try:
            data_obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["missing"].append(f"bookshelf.json 读取失败: {exc}")
            return 0
        items = data_obj if isinstance(data_obj, list) else []
        now = time.time()
        rows: list[tuple] = []
        for item in items:
            if not isinstance(item, dict):
                report["missing"].append("bookshelf: 非对象条目已跳过")
                continue
            book_url = str(item.get("bookUrl") or item.get("url") or "").strip()
            if not book_url:
                report["missing"].append("bookshelf: 缺 bookUrl/url 已跳过")
                continue
            reading = item.get("reading") or {}
            variables = item.get("variables") or {}
            rows.append((
                book_url,
                str(item.get("source_url") or item.get("origin") or ""),
                str(item.get("name") or ""),
                str(item.get("author") or ""),
                str(item.get("kind") or ""),
                str(item.get("cover_url") or ""),
                1 if item.get("in_bookshelf", True) else 0,
                json.dumps(reading, ensure_ascii=False),
                json.dumps(variables, ensure_ascii=False),
                json.dumps(item, ensure_ascii=False),
                now))
        with self.transaction() as db:
            db.executemany(
                "INSERT OR REPLACE INTO bookshelf"
                "(book_url,source_url,name,author,kind,cover_url,in_bookshelf,"
                " reading,vars,payload,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows)
        self._rename_after(path, report)
        return len(rows)

    def _migrate_search_books(self, data: Path, report: dict) -> int:
        path = data / "search-books.sqlite3"
        if not path.exists():
            return 0
        rows: list[tuple] = []
        try:
            src = sqlite3.connect(str(path))
            try:
                found = src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name='search_books'").fetchone()
                if found is not None:
                    for r in src.execute(
                        "SELECT book_url,name,author,origin,origin_order,"
                        " saved_at,payload FROM search_books"):
                        book_url, name, author, origin, order, saved, payload = r
                        kind, source_group = "", ""
                        try:
                            decoded = json.loads(payload or "{}")
                            kind = str(decoded.get("kind") or "")
                            source_group = str(decoded.get("origin_name") or "")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                        rows.append((
                            book_url, name or "", author or "", kind,
                            origin or "", source_group,
                            int(order or 0), int(saved or 0), payload or "{}",
                            float((saved or 0) / 1000)))
            finally:
                src.close()
        except sqlite3.Error as exc:
            report["missing"].append(f"search-books.sqlite3 读取失败: {exc}")
            return 0
        with self.transaction() as db:
            db.executemany(
                "INSERT OR REPLACE INTO search_books"
                "(book_url,name,author,kind,origin,source_group,origin_order,"
                " saved_at,payload,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows)
        self._rename_after(path, report)
        return len(rows)

    def _migrate_appearance(self, data: Path, report: dict) -> int:
        path = data / "appearance.json"
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["missing"].append(f"appearance.json 读取失败: {exc}")
            return 0
        if not isinstance(raw, dict):
            report["missing"].append("appearance.json 非对象已跳过")
            return 0
        # 并入 settings 键 appearance.snapshot（与 M5 ThemeService 统一键一致）。
        # 已存在同键则不覆盖（M5 运行期已写过更新偏好者优先）。
        existing = self._conn.execute(
            "SELECT 1 FROM settings WHERE key='appearance.snapshot'").fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                ("appearance.snapshot", json.dumps(raw, ensure_ascii=False)))
        self._rename_after(path, report)
        return len(raw)

    # ------------------------------------------------------------------ #
    # 目录分配（§6.2 文件目录类介质）
    # ------------------------------------------------------------------ #
    def cache_dir(self, kind: str) -> Path:
        """分配并建好缓存子目录，如 reader-cache / downloads 之外的 image-cache。"""
        path = self._paths.cache / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def download_dir(self) -> Path:
        """下载与导出产物目录（落在 data 侧，§6.2 downloads/）。"""
        path = self._paths.data / "downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path


# ---------------------------------------------------------------------- #
# 模块级访问器：引擎模块在无服务环境下回退旧行为（返回 None）
# ---------------------------------------------------------------------- #
_STORAGE: StorageService | None = None


def default_storage() -> StorageService | None:
    """当前已启动的 StorageService；未启动返回 None。"""
    return _STORAGE


def storage_db() -> "LockedConnection | None":
    """当前主库句柄（持锁，非裸连接）；未启动返回 None（引擎模块据此回退旧文件路径）。"""
    return _STORAGE.db if _STORAGE is not None else None


def _bind(storage: StorageService) -> None:
    global _STORAGE
    _STORAGE = storage


def _unbind(storage: StorageService) -> None:
    global _STORAGE
    if _STORAGE is storage:
        _STORAGE = None
