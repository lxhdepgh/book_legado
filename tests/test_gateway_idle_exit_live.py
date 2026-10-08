"""known-issues 005 的现场臂：纯 HTTP 客户端不告而别，引擎必须自己走。

``tests/test_gateway_inproc.py`` 里那几条断的是 ``exit_event`` 被置位（服务面语义），
这里断的是**症状本身**：真起一个 ``main.py --serve``，只用 ``POST /rpc`` 跟它说话
（一次 /ws 都不连，跟 G1 的 Flutter 客户端同形），然后闭嘴——进程要在窗口之后自己
退出、端口跟着放掉。为什么非得真进程：``exit_event`` 置位与"进程真没了"之间还隔着
``main.serve()`` 的收尾（``hub.shutdown()``），那一段正是 004 里 5.4s 收尾延迟住的地方。

一次启动量三段，顺序是有意的：

1. **端口连得上**（仪器在岗）——否则后面"连不上"可能只是探针自己坏了；
2. **一直在调用的客户端不被打断**——正对照；缺了它，把窗口改成 0 也能让第 3 段绿；
3. **闭嘴之后自己走**，且退出码 0、端口拒连。

全程零外网：临时数据目录，一个动作只发 ``engine.hello``。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "LEGODO_GATEWAY "

#: 空闲窗口。取 1s 是为了让这条臂便宜；判据读的是"窗口之后自己走"，不是这个数本身。
IDLE_SECONDS = 1.0
#: 第 2 段的调用间隔：必须**明显小于**窗口，否则量的不是"掐表接在请求上"。
KEEPALIVE_INTERVAL = 0.3


class _Engine:
    """一台真无头引擎 + 它的数据目录与 stderr 落点。"""

    def __init__(self) -> None:
        self.data = Path(tempfile.mkdtemp(prefix="gw-idle-"))
        self.err_path = self.data / "stderr.log"
        self.proc: subprocess.Popen[str] | None = None
        self.host = ""
        self.port = 0
        self.token = ""
        self.pid = 0
        self._err_file = None

    def boot(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   LEGADO_BOOKSOURCE_DATA_DIR=str(self.data),
                   LEGADO_BOOKSOURCE_CACHE_DIR=str(self.data))
        err_file = open(self.err_path, "w", encoding="utf-8", errors="replace")
        self._err_file = err_file
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py"), "--serve", "--port", "0",
             "--idle-seconds", str(IDLE_SECONDS)],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=err_file,
            text=True, encoding="utf-8", errors="replace")
        hand = self._read_handshake(timeout=120)
        self.host, self.port = hand["host"], int(hand["port"])
        self.token, self.pid = hand["token"], int(hand["pid"])

    def reap(self) -> None:
        """收摊：这条臂不许自己变成 005 的第二个现场。"""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        if self._err_file is not None:
            self._err_file.close()

    def _read_handshake(self, timeout: float) -> dict:
        assert self.proc is not None and self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(f"子进程没打握手行就关了 stdout；stderr：{self.tail()}")
            line = line.strip()
            if line.startswith(PREFIX):
                return json.loads(line[len(PREFIX):])
        raise AssertionError(f"{timeout}s 没等到握手行；stderr：{self.tail()}")

    def tail(self, lines: int = 25) -> str:
        try:
            text = self.err_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "（stderr 还没落盘）"
        return "\n    | ".join(text.splitlines()[-lines:]) or "（空）"

    def hello(self, timeout: float = 30) -> dict:
        """一发真 ``engine.hello``。连接一发一关：RPC-only 客户端就是这个形状。"""
        body = json.dumps({"v": 1, "id": "idle-live", "action": "engine.hello",
                           "seq": 1, "payload": {}}).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/rpc", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def connectable(self, timeout: float = 3) -> bool:
        """端口还连不连得上——"连不上"这条判据自己也得有仪器在岗的证据。"""
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class GatewayIdleExitLiveTests(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = _Engine()
        # 先登记收摊再 boot：握手失败的那条路也得把已经拉起来的子进程带走。
        self.addCleanup(self.engine.reap)
        self.engine.boot()

    def test_rpc_only_client_vanishing_takes_the_engine_down(self) -> None:
        engine = self.engine

        # ---- 1. 仪器在岗：这台引擎现在真的能连上 ----
        self.assertTrue(engine.connectable(),
                        f"握手报了端口却连不上，后面两条判据都没有意义；stderr：{engine.tail()}")

        # ---- 2. 正对照：一直在调用的客户端不许被倒计时打断 ----
        rounds = 10
        talking = time.monotonic()
        last_call = talking
        for index in range(rounds):
            env = engine.hello()
            self.assertTrue(env["ok"], f"第 {index} 发 engine.hello 没成功：{env}")
            self.assertTrue(engine.alive(),
                            f"第 {index} 发之后进程没了：活着的客户端被空闲倒计时杀了"
                            f"（窗口 {IDLE_SECONDS}s，间隔 {KEEPALIVE_INTERVAL}s）")
            last_call = time.monotonic()
            time.sleep(KEEPALIVE_INTERVAL)
        span = last_call - talking
        self.assertGreater(span, IDLE_SECONDS * 2,
                           f"这一段只跨了 {span:.2f}s，不够两个窗口："
                           f"证不到每发调用都把表掐了回去")

        # ---- 3. 不告而别：不发 engine.shutdown，闭嘴等它自己走 ----
        # 起点是**最后一次成功动作**（不是最后那次 sleep 之后）：窗口从回完话那一刻
        # 就开始走，量晚了会把窗口的尾巴算进"收尾"里，读数偏小。
        self.assertIsNotNone(engine.proc)
        rc = engine.proc.wait(timeout=60)
        lag = time.monotonic() - last_call
        self.assertEqual(rc, 0,
                         f"空闲自杀的退出码应为 0（非 0 = 收尾没走干净）；stderr：{engine.tail()}")
        self.assertGreaterEqual(lag, IDLE_SECONDS * 0.9,
                                f"{lag:.2f}s 就退了，比窗口 {IDLE_SECONDS}s 还快 = 不是空闲触发的")
        self.assertLess(lag, 30.0,
                        f"置了退出标志却花了 {lag:.2f}s 才真没 = 收尾挂住了")
        self.assertFalse(engine.connectable(),
                         "进程退了但端口还连得上 = 还有别的进程占着它")
        print(f"\nLIVE 005 闭嘴 {IDLE_SECONDS}s 窗口 → 进程自己走：{lag:.2f}s rc={rc} "
              f"pid={engine.pid} 端口={engine.port}（前面 {rounds} 发存活对照，"
              f"跨 {span:.1f}s）")


if __name__ == "__main__":
    unittest.main()
