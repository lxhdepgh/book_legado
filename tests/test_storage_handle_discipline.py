"""#78 护栏：共享主库连接只能经**持锁句柄**使用，写不会掉进别人的事务里。

主库是一条 ``check_same_thread=False`` + ``isolation_level=None`` 的共享连接。
绕过 ``_db_lock`` 的语句不是"少等了一会儿"，而是**静默并进别人未提交的事务**：
对方 ROLLBACK 把它一起带走，调用方看到的还是成功；两条线程同时裸写还会互踩
``sqlite3.InterfaceError`` 丢行。本机实测（``.probe/raw_db_handle_cost.py``，本地
临时库）：书签/设置/阅读进度三种形状在别人事务回滚后计数都是 **0**（且都没报错）；
8 线程 × 240 条裸写只活下 **199~202** 行、抛 38~41 次 InterfaceError，持锁写
240/240 零异常。

修法是收口分配点（``StorageService.db`` 交 :class:`LockedConnection`），所以这里
钉三件事：

1. **后果没再发生**（写不丢、读不脏、并发不互踩）；
2. **口真的堵住了**——句柄只放语句面，``commit``/``cursor``/``close`` 一律撞墙
   （``commit()`` 那条不是洁癖：裸 commit 落在别人未提交的事务上是**替别人提交**，
   正是 ``rule_engine`` 里 cookies 罐那句 ``db.commit()`` 的坏法）；
3. **没有别的路绕过去**（AST 扫全库生产代码：从 ``.db`` / ``storage_db()`` 落地的
   名字上不许出现会话级原语，也不许直接从主库所有者上摸裸访问器）。

第 3 条的扫描器本身也要防"扫不出=没有"：第一轮盘点就是因为 ``ast.parse`` 撞上
UTF-8 BOM 抛 SyntaxError、被 ``except: continue`` 吃掉，把 8 个模块静默漏掉了
（``.probe/scan_raw_db_writes.py`` 现在会点名报 skipped）。所以这里的扫描函数让
解析失败**冒出来**，系统性那条还断言"一个文件都没解析失败、且真的扫了上百个文件"。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest

from app.core.settings import SettingsService
from app.core.storage import LockedConnection, StorageService
from app.services.app_paths import AppPaths
from app.services.engine.rule_engine import BookItem
from app.services.reading.bookmark_service import BookmarkService
from app.services.search_model import SearchBookStore

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
# 连接的所有者自己当然要用裸连接；schema 迁移函数拿到的就是那条连接。
SCAN_EXEMPT = {(APP / "core" / "storage.py").resolve(),
               (APP / "storage" / "schema.py").resolve()}
# 在共享句柄上调用这些 = 绕过锁的会话级动作。
SESSION_PRIMITIVES = ("commit", "rollback", "close", "cursor", "executescript")
# 句柄来源属性名 + 模块级访问器函数名。
HANDLE_ATTRS = {"db", "raw", "_conn", "_guarded"}
ACCESSOR_NAMES = {"storage_db", "default_storage"}
STORAGE_NAMES = {"storage", "_storage"}


def _new_storage(tmp: str) -> StorageService:
    storage = StorageService(AppPaths(data=pathlib.Path(tmp) / "data",
                                      cache=pathlib.Path(tmp) / "cache"))
    asyncio.run(storage.on_start())
    return storage


def _rollback_window(storage, hold=0.25):
    """起一条线程握着 ``transaction()``，中途回滚；返回 (线程, 收尾时间盒)。

    ``closed_at`` 由那条线程在 ROLLBACK 落回后写入——用它判断窗口里的第二条写
    到底是**等**到事务收尾的，还是直接插进那笔事务的。
    """
    opened = threading.Event()
    box = {"closed_at": 0.0}

    def holder():
        try:
            with storage.transaction():
                storage.db.execute(
                    "INSERT INTO bookmarks(book_url, chapter_index, offset,"
                    " summary, created_at) VALUES('foreign',0,0,'',0)")
                opened.set()
                time.sleep(hold)
                raise RuntimeError("那笔写砸了")
        except RuntimeError:
            box["closed_at"] = time.perf_counter()

    t = threading.Thread(target=holder)
    t.start()
    if not opened.wait(3):
        t.join(5)
        raise AssertionError("开窗失败，这条护栏什么都没测到")
    return t, box


class StorageHandleDisciplineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = _new_storage(self._tmp.name)
        self.addCleanup(lambda: asyncio.run(self.storage.on_stop()))

    def count(self, sql: str, *params) -> int:
        return int(self.storage.db.execute(sql, params).fetchone()[0])


# --------------------------------------------------------------------- #
# 1) 后果：写不丢 / 读不脏 / 并发不互踩
# --------------------------------------------------------------------- #
class ForeignTransactionWritesSurviveTests(StorageHandleDisciplineTestBase):
    def test_service_writes_survive_a_foreign_rollback(self):
        """三条真实形状：INSERT+lastrowid（书签）、UPSERT（设置）、进度行。

        修前实测每条在别人事务回滚后计数都是 0，而调用方没报错
        （``.probe/raw_db_handle_cost.py`` 第 1 段）。
        """
        bookmark = BookmarkService(storage=self.storage)
        settings = SettingsService(storage=self.storage)

        def shapes():
            yield ("书签", lambda: asyncio.run(bookmark.add_bookmark(
                "https://b1", 3, 12.0, "摘要")),
                "SELECT COUNT(*) FROM bookmarks WHERE book_url=?", "https://b1")
            yield ("设置", lambda: settings.set("probe.key", "v"),
                   "SELECT COUNT(*) FROM settings WHERE key=?", "probe.key")

            def write_progress():
                self.storage.db.execute(
                    "INSERT INTO books(book_url, source_url, name, chapter_index,"
                    " scroll_offset, updated_at) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(book_url) DO UPDATE SET"
                    " chapter_index=excluded.chapter_index",
                    ("https://b2", "https://s", "书", 2, 0.5, 0.0))
            yield ("进度", write_progress,
                   "SELECT COUNT(*) FROM books WHERE book_url=?", "https://b2")

        for label, write, count_sql, param in shapes():
            with self.subTest(形状=label):
                t, box = _rollback_window(self.storage)
                error = None
                try:
                    write()
                except BaseException as exc:      # noqa: BLE001
                    error = exc
                t.join(5)
                self.assertIsNone(
                    error, f"{label}：写被别人的事务弹回来了（{error!r}）")
                self.assertGreater(box["closed_at"], 0.0,
                                   "窗口没建立，计数为 1 说明不了什么")
                self.assertEqual(
                    self.count(count_sql, param), 1,
                    f"{label}：调用方没报错，但行跟着别人的 ROLLBACK 没了——"
                    f"写又落到裸句柄上了")

    def test_concurrent_writes_all_land(self):
        """8 线程 × 240 条：修前三遍只活下 199~202 行、抛 38~41 次 InterfaceError，现在一条不少。"""
        errors: list[BaseException] = []
        guard = threading.Lock()

        def one(i: int):
            try:
                self.storage.db.execute(
                    "INSERT INTO bookmarks(book_url, chapter_index, offset,"
                    " summary, created_at) VALUES(?,?,?,?,?)",
                    (f"https://c{i}", 0, 0.0, "", i))
            except BaseException as exc:      # noqa: BLE001
                with guard:
                    errors.append(exc)

        total = 240
        threads = [threading.Thread(
            target=lambda chunk=range(start, total, 8): [one(i) for i in chunk])
            for start in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors,
                         f"并发写互踩：{type(errors[0]).__name__}: {errors[0]}"
                         if errors else "")
        self.assertEqual(self.count("SELECT COUNT(*) FROM bookmarks"), total,
                         "并发裸写丢行")

    def test_write_waits_for_the_open_transaction_instead_of_joining_it(self):
        """并进那笔事务的另一半证据：这条写是**等**到窗口关掉的，不是插进去的。"""
        t, box = _rollback_window(self.storage)
        started = time.perf_counter()
        self.storage.db.execute(
            "INSERT INTO bookmarks(book_url, chapter_index, offset, summary,"
            " created_at) VALUES('waited',0,0,'',0)")
        finished = time.perf_counter()
        t.join(5)
        self.assertGreater(box["closed_at"], started, "窗口没建立，比较没意义")
        self.assertGreaterEqual(finished, box["closed_at"],
                                "写没等别人事务收尾就落下去了——它并进那笔里去了")
        rows = {r[0] for r in self.storage.db.execute(
            "SELECT book_url FROM bookmarks")}
        self.assertEqual(rows, {"waited"},
                         f"别人的行没回滚干净（并进了那笔事务）：{rows}")

    def test_read_during_an_open_transaction_sees_no_uncommitted_rows(self):
        """读的那半边（#78 遗留的 list_logs 形状）：锁让读排在事务之后，脏读没了。

        同一条连接上，别人未提交的行本来就读得到；句柄持锁之后这条读必须等到
        ROLLBACK。
        """
        t, _ = _rollback_window(self.storage)
        rows = self.storage.db.execute("SELECT book_url FROM bookmarks").fetchall()
        t.join(5)
        self.assertEqual(rows, [], f"读到了别人未提交的行：{[r[0] for r in rows]}")

    def test_store_batch_rollback_does_not_take_other_writers_with_it(self):
        """种子库整批回滚时，别人同时写的一行不该被一起带走。

        ``SearchBookStore._batch`` 以前在共享连接上自己 BEGIN/COMMIT，套的是
        store **私有**的那把锁，所以它的回滚会吞掉窗口里任何人的写；现在它交
        ``LockedConnection.transaction()``，主库锁从头握到尾。
        """
        store = SearchBookStore(conn=self.storage.db)
        inside = threading.Event()
        box = {"closed_at": 0.0}

        def failing_batch():
            try:
                with store._connect() as db, store._batch(db):
                    db.execute(
                        "INSERT OR REPLACE INTO search_books(book_url,name,author,"
                        "origin,origin_order,saved_at,payload) "
                        "VALUES('tx/1','书','甲','o',0,1,'{}')")
                    inside.set()
                    time.sleep(0.25)
                    raise RuntimeError("种子写砸了")
            except RuntimeError:
                box["closed_at"] = time.perf_counter()

        t = threading.Thread(target=failing_batch)
        t.start()
        if not inside.wait(3):
            t.join(5)
            self.fail("批量窗口没建立，这条护栏什么都没测到")
        self.storage.db.execute(
            "INSERT INTO bookmarks(book_url, chapter_index, offset, summary,"
            " created_at) VALUES('during-batch',0,0,'',0)")
        t.join(5)
        self.assertEqual(self.count("SELECT COUNT(*) FROM search_books"), 0,
                         "这一批没回滚")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM bookmarks WHERE book_url='during-batch'"),
            1, "别人的写被这一批一起带走了")

    def test_store_batch_goes_through_the_locked_transaction(self):
        """上一条的接线证据：批量确实走 ``transaction()``，不是走自己的 BEGIN。"""
        calls: list[str] = []
        original = LockedConnection.__dict__["transaction"]

        def spy(self):
            calls.append("transaction")
            return original.__get__(self, LockedConnection)()

        LockedConnection.transaction = spy
        self.addCleanup(lambda: setattr(LockedConnection, "transaction", original))
        SearchBookStore(conn=self.storage.db).insert(
            [BookItem(name=f"接线书{i}", author="甲", url=f"tx/{i}", origin="o")
             for i in range(20)])
        self.assertEqual(calls, ["transaction"], "批量没走持锁事务，回滚仍会吞别人的写")
        self.assertEqual(
            self.count("SELECT COUNT(*) FROM search_books WHERE author='甲'"), 20)

    def test_nested_transaction_joins_instead_of_raising(self):
        """重入只加入外层那笔：``_batch`` 靠这条，抢先 BEGIN 会撞 sqlite 的错误。"""
        with self.storage.transaction() as outer:
            outer.execute(
                "INSERT INTO bookmarks(book_url, chapter_index, offset, summary,"
                " created_at) VALUES('outer',0,0,'',0)")
            with self.storage.transaction() as inner:
                inner.execute(
                    "INSERT INTO bookmarks(book_url, chapter_index, offset,"
                    " summary, created_at) VALUES('inner',0,0,'',0)")
        rows = {r[0] for r in self.storage.db.execute("SELECT book_url FROM bookmarks")}
        self.assertEqual(rows, {"outer", "inner"})


# --------------------------------------------------------------------- #
# 2) 口真的堵住了
# --------------------------------------------------------------------- #
class HandleSurfaceTests(StorageHandleDisciplineTestBase):
    def test_db_hands_out_the_locked_handle_not_the_connection(self):
        handle = self.storage.db
        self.assertIsInstance(handle, LockedConnection)
        self.assertNotIsInstance(handle, sqlite3.Connection)
        self.assertIsInstance(self.storage.raw, sqlite3.Connection)
        # 反空转：句柄得真能用。
        self.assertEqual(int(handle.execute("SELECT 1").fetchone()[0]), 1)

    def test_session_primitives_are_not_on_the_handle(self):
        """``commit()`` 不在句柄上：裸 commit 会把别人半成品事务替人提交掉。

        cookies 罐那句 ``db.commit()``（rule_engine）就是这个坏法；修后走持锁
        语句 + autocommit，不再需要（也不允许）自己收尾。
        """
        for name in SESSION_PRIMITIVES:
            with self.subTest(原语=name):
                with self.assertRaises(AttributeError) as ctx:
                    getattr(self.storage.db, name)
                self.assertIn("raw", str(ctx.exception),
                              f"{name} 撞墙了却没指路到 StorageService.raw，"
                              f"报错会被人当成句柄的 bug 绕开")
        self.assertFalse(self.storage.db.in_transaction)

    def test_handle_identity_is_stable(self):
        """同一个句柄对象：每条语句现造一个包装，锁的开销就变成对象 churn。"""
        self.assertIs(self.storage.db, self.storage.db)

    def test_unstarted_service_has_no_handle(self):
        """未启动时 ``db`` 是 None——调用点都靠这句判据回退旧行为。"""
        storage = StorageService(AppPaths(data=pathlib.Path(self._tmp.name) / "x",
                                          cache=pathlib.Path(self._tmp.name) / "y"))
        self.assertIsNone(storage.db)
        self.assertIsNone(storage.raw)


# --------------------------------------------------------------------- #
# 3) 没有第二条路绕过去（AST 扫全库生产代码）
# --------------------------------------------------------------------- #
def _base_is_storage_obj(node) -> bool:
    """``self._storage`` / ``storage`` / ``default_storage()`` 这类主库所有者表达式。"""
    if isinstance(node, ast.Name):
        return node.id in STORAGE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in STORAGE_NAMES or _base_is_storage_obj(node.value)
    if isinstance(node, ast.Call):
        fn = node.func
        return isinstance(fn, ast.Name) and fn.id in ACCESSOR_NAMES
    return False


def _handle_names(func) -> set[str]:
    """函数里从共享句柄落地的局部名（``.db``/``.raw``/``._conn``/``storage_db()``）。"""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        derived = False
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute) and sub.attr in HANDLE_ATTRS:
                derived = True
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "storage_db"):
                derived = True
        if not derived:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def scan_handle_bypass(source: str, path: str) -> list[str]:
    """返回违规描述列表。"扫不出违规"和"没有违规"是两回事，见模块 docstring。"""
    tree = ast.parse(source)      # 解析失败必须冒出来，不让调用方静默跳过
    findings: list[str] = []
    for func in [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        names = _handle_names(func)
        for node in ast.walk(func):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in {"raw", "_conn", "_guarded"}:
                if _base_is_storage_obj(node.value):
                    findings.append(
                        f"{path}:{node.lineno} 从主库所有者上摸裸访问器 .{node.attr}")
            elif (isinstance(node.value, ast.Name)
                    and node.value.id in names
                    and node.attr in SESSION_PRIMITIVES):
                findings.append(
                    f"{path}:{node.lineno} 在共享句柄 {node.value.id}."
                    f"{node.attr}() 上绕开了锁")
    return findings


def scan_production_tree() -> tuple[list[str], list[str], int]:
    findings, unreadable, scanned = [], [], 0
    for path in sorted(APP.rglob("*.py")):
        if path.resolve() in SCAN_EXEMPT:
            continue
        try:
            findings += scan_handle_bypass(
                path.read_text(encoding="utf-8-sig"), path.as_posix())
        except SyntaxError as exc:
            unreadable.append(f"{path.as_posix()}: {exc}")
        else:
            scanned += 1
    return findings, unreadable, scanned


class NoBypassPathTests(unittest.TestCase):
    def test_no_production_code_bypasses_the_locked_handle(self):
        findings, unreadable, scanned = scan_production_tree()
        self.assertFalse(unreadable,
                         f"这些模块没被解析，等于没扫：{unreadable}")
        self.assertGreater(scanned, 100, f"只扫了 {scanned} 个文件，多半是路径口径错了")
        self.assertFalse(findings,
                         "生产代码里又出现了绕开持锁句柄的写法：\n" + "\n".join(findings))

    def test_scanner_flags_planted_violations(self):
        """反空转：扫描器自己坏掉时，上一条会绿得和"没有违规"一模一样。"""
        planted = [
            # 从 storage.db 落地的名字上调 commit —— 正是 cookies 罐的坏法
            ("def save(self):\n"
             "    conn = self._storage.db\n"
             "    conn.execute('INSERT INTO t VALUES(1)')\n"
             "    conn.commit()\n", "commit"),
            # 从模块访问器落地的名字上开游标（引擎里 storage_db() 那条路）
            ("def load(self):\n"
             "    db = storage_db()\n"
             "    cur = db.cursor()\n"
             "    cur.execute('DELETE FROM t')\n", "cursor"),
            # 直接从主库所有者上摸裸连接
            ("def leak(self):\n"
             "    return self._storage.raw\n", "raw"),
        ]
        for src, expect in planted:
            found = scan_handle_bypass(src, "<植入>")
            self.assertTrue(any(expect in f for f in found),
                            f"植入的 {expect} 绕锁写法没被扫出来（{found}），"
                            f"系统性那条现在是空转")

    def test_scanner_does_not_flag_the_locked_idiom(self):
        """反空转的另一面：合规写法不许被报成违规，否则这条护栏会逼人绕过它。

        第二样例是真实的假阳性形状——``ContentSearchService`` 自己有个叫 ``_conn``
        的方法，它不是裸访问器。
        """
        ok = (
            "def save(self):\n"
            "    conn = self._storage.db\n"
            "    conn.execute('INSERT INTO t VALUES(1)')\n"
            "    rows = conn.execute('SELECT 1').fetchall()\n"
            "    with conn.transaction():\n"
            "        conn.executemany('INSERT INTO t VALUES(?)', [(2,)])\n"
            "    return rows\n")
        self.assertEqual(scan_handle_bypass(ok, "<合规>"), [])
        method_not_accessor = (
            "def search(self):\n"
            "    conn = self._conn()\n"
            "    if conn is None:\n"
            "        return []\n"
            "    return conn.execute('SELECT 1').fetchall()\n")
        self.assertEqual(scan_handle_bypass(method_not_accessor, "<合规方法>"), [])


if __name__ == "__main__":
    unittest.main()
