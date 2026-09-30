"""每响应 cookie 落库的脏闸门：护栏钉的是"没发生"，不是"失败了"。

被护住的形状（`.probe/check_slowdown_cookiejar_gate.py` 实测，同一台机器）：

| 整库 cookie 罐 | 旧写法·响应没动罐 | 现写法·响应没动罐 | 现写法·真动了罐 |
|---|---|---|---|
| 16 条 | 0.83 ms | 0.001 ms | 1.03 ms |
| 512 条 | 2.24 ms | 0.001 ms | 3.76 ms |
| 2048 条 | 6.78 ms | 0.001 ms | 12.51 ms |

改之前这一段**每个 HTTP 响应**都在引擎循环线程上无条件跑完整条：整罐重排 +
整份 JSON 原子写 + 整罐 `json.dumps` + 一条抢 `_db_lock` 的 `INSERT OR REPLACE`。
单价随整库罐规模线性涨，所以症状是"越跑越慢"而不是"某一次慢"。

闸门看两个版本号（`LegadoJsRuntime.cookie_revision` 只在罐**真的**变了才 +1；
`ReaderEngine._cookie_persist_revision` 是上一次落库时看到的版本）。这里断言的是
**写没发生**（记账替身数语句、包一层数文件写），而不是"返回值对不对"——返回值
两臂都对，闸门坏了照样对。

一个坑（探针踩过，故也进护栏）：`Set-Cookie` 不带 `Expires`/`Max-Age` 是 session
cookie，按 Legado 语义**不进**持久罐，所以那种响应本来就不该算脏。用 session cookie
做"脏臂"会量出 0，把闸门写成"永远不开"也能全绿。见
`test_a_session_only_cookie_is_not_dirty`。
"""
from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services.engine.rule_engine import ReaderEngine

URL = "https://source.example.com"


def _source():
    return SimpleNamespace(bookSourceUrl=URL, enabledCookieJar=True,
                           header="", concurrentRate="", proxy=None,
                           to_dict=lambda: {"bookSourceUrl": URL})


class _RecordingDb:
    """记账替身：只数语句、留下参数，不碰真主库（也别把真库灌脏）。"""

    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))

    @property
    def jar_writes(self) -> int:
        return sum(1 for sql, _ in self.statements if sql.startswith("INSERT OR REPLACE"))

    def last_jar(self):
        for sql, params in reversed(self.statements):
            if sql.startswith("INSERT OR REPLACE"):
                return json.loads(params[1])
        return None


class CookieJarWriteGateTests(unittest.IsolatedAsyncioTestCase):
    async def _engine(self, directory: str) -> tuple[ReaderEngine, _RecordingDb, list[int]]:
        """建引擎 + 记账替身，并给 JSON 那一写套一层计数（保留真实行为）。"""
        engine = ReaderEngine(state_dir=directory)
        db = _RecordingDb()
        saves: list[int] = []
        original = engine.js._save_persistent_state

        def counting_saver():
            saves.append(1)
            original()

        engine.js._save_persistent_state = counting_saver
        return engine, db, saves

    async def test_an_unchanged_jar_writes_nothing_at_all(self):
        """罐没动 → 整段跳过：0 条 SQL、0 份 JSON 重写。这是闸门唯一的存在理由。"""
        with TemporaryDirectory() as directory:
            engine, db, saves = await self._engine(directory)
            # 先把罐灌成生产里的样子（非空）。空罐是另一条路：快照本就为 None，
            # 没什么可落，闸门开不开都不会写——拿它测"没写"是自证。
            engine.js.save_response_cookies(
                URL, {"set-cookie": "seed=1; Path=/; Max-Age=86400"})
            with patch("app.core.storage.storage_db", lambda: db):
                engine._persist_source_cookie_jar(_source())   # 第一笔：补写基线
                baseline_sql, baseline_files = db.jar_writes, len(saves)
                revision = engine.js.cookie_revision
                for _ in range(20):
                    engine._persist_source_cookie_jar(_source())
            self.assertGreaterEqual(baseline_sql, 1, "第一笔响应就该落库")
            self.assertEqual(db.jar_writes, baseline_sql, "闸门之后还在写 SQLite")
            self.assertEqual(len(saves), baseline_files, "闸门之后还在重写 JSON")
            self.assertEqual(engine.js.cookie_revision, revision)

    async def test_a_persistent_cookie_reopens_the_gate(self):
        """罐真变了必须立刻放行，且落的是**整罐**快照而不是那一条。

        先灌一条并落库一次，把"已落库的版本号"记上——**这步不能省**：空罐时
        `persistent_cookie_snapshot()` 返回 None，那一笔压根不写，写侧的版本号也就
        没被填（还是 None）。拿那个形状去测"变了要写"，就算版本号永远不 +1
        （`0 == None` 恒假 → 每响应都写）也能蒙过去——第五十九轮变异 G5 就是这么
        漏掉第一版的。
        """
        with TemporaryDirectory() as directory:
            engine, db, _saves = await self._engine(directory)
            engine.js.save_response_cookies(
                URL, {"set-cookie": "seed=1; Path=/; Max-Age=86400"})
            with patch("app.core.storage.storage_db", lambda: db):
                engine._persist_source_cookie_jar(_source())
                writes = db.jar_writes
                self.assertEqual(writes, 1, "非空罐的第一笔就该落库并填上版本号")
                self.assertIsNotNone(engine._cookie_persist_revision)
                engine.js.save_response_cookies(
                    URL, {"set-cookie": "sid=new; Path=/; Max-Age=86400"})
                engine._persist_source_cookie_jar(_source())
                self.assertEqual(db.jar_writes, writes + 1, "脏罐只该补一笔，且必须补")
                jar = db.last_jar()
                names = {item["name"] for item in jar}
                self.assertIn("sid", names)
                # 整罐：既有基线里的条目不能因为闸门而漏。
                self.assertIn("seed", names)
                for item in jar:
                    self.assertIn("name", item)

    async def test_a_session_only_cookie_is_not_dirty(self):
        """无期限的 Set-Cookie 不进持久罐 → 本来就不该算脏（脏臂用它会假量 0）。"""
        with TemporaryDirectory() as directory:
            engine, db, saves = await self._engine(directory)
            engine.js.save_response_cookies(
                URL, {"set-cookie": "seed=1; Path=/; Max-Age=86400"})
            with patch("app.core.storage.storage_db", lambda: db):
                engine._persist_source_cookie_jar(_source())
                writes, files, revision = db.jar_writes, len(saves), engine.js.cookie_revision
                self.assertEqual((writes, revision), (1, 1), "种子 cookie 该已落库")
                engine.js.save_response_cookies(URL, {"set-cookie": "temp=v; Path=/"})
                engine._persist_source_cookie_jar(_source())
            self.assertEqual(engine.js.cookie_revision, revision,
                             "session cookie 被算进了持久罐版本号")
            self.assertEqual(db.jar_writes, writes)
            self.assertEqual(len(saves), files)

    async def test_the_gate_sits_on_the_request_path_not_only_on_the_method(self):
        """接线核验：走真实 `_request`，响应头决定脏不脏。

        只测方法本身挡不住"调用点被搬走/删掉"（记忆 `refactor-regression-orphan-methods`：
        测试全绿也可能把并发/清理搬成没人调用）。

        同样要先落一次库把版本号填上——空罐起步时写侧永远是"没记过"，闸门等于不存在
        （变异 G5 就是这么混过去的）。
        """
        with TemporaryDirectory() as directory:
            engine, db, _saves = await self._engine(directory)

            class Headers(dict):
                def getall(self, name, default):
                    value = self.get(name)
                    return [value] if value else default

            class Response:
                status, charset, content_type = 200, "utf-8", "text/plain"
                url = f"{URL}/page"

                def __init__(self, set_cookie):
                    self.headers = Headers({"Set-Cookie": set_cookie} if set_cookie else {})

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                async def read(self):
                    return b"ok"

            queue: list = []

            class Session:
                closed = False

                def __init__(self, *_args, **_kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                def request(self, *_args, **_kwargs):
                    return Response(queue.pop(0))

            source = _source()
            with patch("app.core.storage.storage_db", lambda: db):
                engine.js.save_response_cookies(
                    URL, {"set-cookie": "seed=1; Path=/; Max-Age=86400"})
                engine._persist_source_cookie_jar(source)   # 当作"上一次响应已落库"
                primed = db.jar_writes
                self.assertEqual(primed, 1, "种子没把已落库版本号填上，闸门等于没测")
                queue.append("sid=first; Path=/; Max-Age=86400")   # 会变罐
                queue.append(None)                                 # 原样再要一次
                with patch("app.services.engine.rule_engine.aiohttp.ClientSession", Session), \
                        patch("app.services.engine.rule_engine.aiohttp.TCPConnector",
                              lambda **_kwargs: object()):
                    await engine._request({"url": f"{URL}/page"}, source)
                    after_first = db.jar_writes
                    await engine._request({"url": f"{URL}/page"}, source)
            self.assertEqual(after_first, primed + 1,
                             "带持久 cookie 的响应没多落那一笔（调用点没接上/版本号不涨？）")
            self.assertEqual(db.jar_writes, after_first,
                             "不带 cookie 的响应还在落库（闸门没生效）")
