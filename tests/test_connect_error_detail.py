"""连接失败那句的护栏：现场 `… ssl:False [None]` 要能自己说清"拨到哪、谁抛的"。

这一句在检测、换源、章节加载三处都是用户唯一看得见的线索。补全段是纯函数
`_connect_failure_tail`，所以能离线钉；两条"不许发生"的断言（拒连不许出现
"未给出错误文本"、正常请求不许查地址）都配了"该发生的仍然发生"。
"""
from __future__ import annotations

import asyncio
import aiohttp
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.engine import rule_engine
from app.services.engine.rule_engine import (ReaderEngine, RequestConnectionError,
                                             in_sinkhole_range, set_dns_ip)

SINKHOLE_IP = "198.18.8.94"


def _connection_key(host="host", port=443):
    """按生产请求的键形造替身：https 且 `ssl=False`（`rule_engine.py:1419` 的 kwargs），
    这样 aiohttp 那句就长回现场的 `…:443 ssl:False […]`。"""
    return ConnectionKey(host, port, True, False, None, None, None, host)


def _connector_error(os_error, host="host", port=443):
    """按生产连接器的手法造一条连接层异常。"""
    return aiohttp.ClientConnectorError(_connection_key(host, port), os_error)


def _session_raising(error):
    """一 request 就抛给定异常的假会话；其余与 aiohttp 的用法一致。"""

    class Session:
        closed = False

        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def close(self):
            pass

        def request(self, *_args, **_kwargs):
            raise error

    return Session


class ConnectFailureSentenceTests(unittest.IsolatedAsyncioTestCase):
    """整句形状：原文一字不动，补充段只说"是谁、拨到哪儿"。"""

    async def _sentence(self, error, *, source=None, url="https://host/page",
                        method="POST", resolve=("93.184.216.34",),
                        resolve_fails=False) -> tuple[str, list]:
        """走真 `_request_transport` 拿用户读到的那句；返回 (句子, 解析器调用记录)。

        用 POST 是为了绕开连接层重拨（预算只给幂等 GET），否则每条用例要先睡 1.5 秒。
        `resolve` 把解析器钉成给定地址：用例不许依赖本机 DNS 当天答什么。
        """
        looked_up: list = []

        async def fake_getaddrinfo(host, port, **_kwargs):
            looked_up.append((str(host), port))
            if resolve_fails:
                raise OSError(11001, "getaddrinfo failed")
            return [(0, 0, 0, "", (address, port)) for address in resolve]

        with patch("app.services.engine.rule_engine.aiohttp.ClientSession",
                   _session_raising(error)), \
                patch("app.services.engine.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()), \
                patch.object(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo):
            engine = ReaderEngine()
            try:
                with self.assertRaises(RequestConnectionError) as caught:
                    await engine._request_transport({"url": url, "method": method}, source)
                return str(caught.exception), looked_up
            finally:
                await engine.aclose()

    async def test_handshake_reset_names_the_class_and_the_sinkhole_address(self):
        """现场那条 `[None]`：抛的是不带参数的 ConnectionResetError，方括号里全是空的。"""
        error = _connector_error(ConnectionResetError())
        # 先把替身钉在现场形状上：aiohttp 那方括号填的是 os_error.strerror，
        # 不带参数的 ConnectionResetError 只会得到 [None]。
        self.assertIn("ssl:False [None]", str(error))
        set_dns_ip("host", SINKHOLE_IP)
        try:
            sentence, looked_up = await self._sentence(error)
        finally:
            set_dns_ip("host", None)
        self.assertTrue(sentence.startswith(f"连接失败：{error}"), sentence)
        self.assertIn("底层 ConnectionResetError 未给出错误文本", sentence)
        self.assertIn(f"拨到 {SINKHOLE_IP}", sentence)
        self.assertIn("198.18.0.0/15", sentence)
        # 钉表命中时不许再解析一次——解析出来的可能不是当时拨的那个地址。
        self.assertEqual(looked_up, [])

    async def test_refused_connection_adds_only_the_dialled_address(self):
        """strerror 已经说清了是谁抛的，不许再补一句"未给出错误文本"。"""
        error = _connector_error(
            ConnectionRefusedError(22, "远程计算机拒绝网络连接。", None, 1225, None))
        sentence, looked_up = await self._sentence(error)
        self.assertTrue(sentence.startswith(f"连接失败：{error}"), sentence)
        self.assertIn("[远程计算机拒绝网络连接。]", sentence)
        self.assertIn("拨到 93.184.216.34", sentence)
        self.assertNotIn("未给出错误文本", sentence)
        self.assertNotIn("198.18.0.0/15", sentence)
        self.assertEqual([host for host, _ in looked_up], ["host"])

    async def test_dns_failure_fabricates_no_address(self):
        """解析不出来时重新解析还是解析不出来：宁可一句都不补，也不许编一个地址。"""
        error = aiohttp.ClientConnectorDNSError(
            _connection_key("nope.invalid"),
            OSError(11001, "getaddrinfo failed"))
        self.assertIn("[getaddrinfo failed]", str(error))
        sentence, looked_up = await self._sentence(
            error, url="https://nope.invalid/x", resolve_fails=True)
        self.assertTrue(sentence.startswith(f"连接失败：{error}"), sentence)
        self.assertEqual([host for host, _ in looked_up], ["nope.invalid"])
        self.assertNotIn("拨到", sentence)
        self.assertNotIn("未给出错误文本", sentence)

    async def test_proxy_path_does_not_resolve_the_target_host(self):
        """走代理时拨的是代理的地址，解析目标域名只会误导人。"""
        source = SimpleNamespace(bookSourceUrl="https://source", enabledCookieJar=False,
                                 header='{"proxy":"http://127.0.0.1:1080"}')
        error = _connector_error(ConnectionResetError())
        sentence, looked_up = await self._sentence(error, source=source)
        self.assertEqual(looked_up, [])
        self.assertNotIn("拨到", sentence)
        # 该发生的仍然发生：底层是谁抛的照旧要写出来。
        self.assertIn("底层 ConnectionResetError 未给出错误文本", sentence)


class SinkholeRangeTests(unittest.TestCase):
    """吸洞段判据：只认 RFC 2544 的 198.18.0.0/15，别的段一律不误导人。"""

    def test_reserved_benchmarking_range_hits(self):
        for address in ("198.18.0.0", SINKHOLE_IP, "198.19.255.255"):
            self.assertTrue(in_sinkhole_range(address), address)

    def test_everything_else_misses(self):
        for address in ("198.17.0.1", "198.20.0.1", "127.0.0.1", "38.47.117.159",
                        "fe80::1%12", "::1", "", None, "host", 443):
            self.assertFalse(in_sinkhole_range(address), address)

    def test_ipv6_zone_id_is_stripped_before_deciding(self):
        self.assertTrue(in_sinkhole_range("198.18.8.94%3"))
        self.assertTrue(in_sinkhole_range("::ffff:198.18.8.94"))


class HealthyRequestPaysNothingTests(unittest.IsolatedAsyncioTestCase):
    """补全只长在"已经拨不通"那条路上：正常请求一分钱不付。"""

    async def test_working_local_source_never_triggers_the_address_lookup(self):
        async def home(request):
            return web.Response(text="<html><body>ok</body></html>")

        app_ = web.Application()
        app_.router.add_route("*", "/", home)
        runner = web.AppRunner(app_)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        looked_up: list = []
        real_lookup = rule_engine._dialed_addresses

        async def spy(host, port_):
            looked_up.append(host)
            return await real_lookup(host, port_)

        engine = ReaderEngine(timeout=5.0, connect_timeout=3.0)
        try:
            with patch("app.services.engine.rule_engine._dialed_addresses", spy):
                text, _, final_url = await engine._request_transport(
                    {"url": f"http://127.0.0.1:{port}/"})
        finally:
            await engine.aclose()
            await runner.cleanup()
        self.assertEqual(text, "<html><body>ok</body></html>")
        self.assertIn(f":{port}", final_url)
        self.assertEqual(looked_up, [])


class DialledAddressLookupTests(unittest.IsolatedAsyncioTestCase):
    """`_dialed_addresses` 自己：钉表优先、解析失败给空、报错路上不再制造失败。"""

    async def test_pinned_table_wins_and_skips_resolution(self):
        set_dns_ip("pinned.example", "203.0.113.9")
        try:
            async def should_not_be_called(*_args, **_kwargs):
                raise AssertionError("钉表命中时不该再解析")

            with patch.object(asyncio.get_running_loop(), "getaddrinfo",
                              should_not_be_called):
                self.assertEqual(await rule_engine._dialed_addresses("pinned.example", 443),
                                 ["203.0.113.9"])
        finally:
            set_dns_ip("pinned.example", None)

    async def test_lookup_is_case_insensitive_like_the_resolver(self):
        set_dns_ip("mixed.example", "203.0.113.7")
        try:
            self.assertEqual(await rule_engine._dialed_addresses("MIXED.Example", 443),
                             ["203.0.113.7"])
        finally:
            set_dns_ip("mixed.example", None)

    async def test_no_host_returns_empty_instead_of_raising(self):
        """报错的路上不许再制造一次失败：没有主机名就给空，不碰解析器。"""
        self.assertEqual(await rule_engine._dialed_addresses("", 443), [])
        self.assertEqual(await rule_engine._dialed_addresses(None, 443), [])

    async def test_duplicate_addresses_are_collapsed(self):
        infos = [("203.0.113.5", 0), ("203.0.113.5", 0), ("203.0.113.6", 0)]

        async def fake_getaddrinfo(host, port, **_kwargs):
            return [(0, 0, 0, "", (address, port)) for address, _ in infos]

        with patch.object(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo):
            self.assertEqual(await rule_engine._dialed_addresses("dup.example", 80),
                             ["203.0.113.5", "203.0.113.6"])


if __name__ == "__main__":
    unittest.main()
