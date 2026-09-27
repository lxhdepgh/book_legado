# -*- coding: utf-8 -*-
"""RuleEngineService 外壳测试（M1 任务 4，离线安全）。

覆盖：
- 生命周期状态迁移（start→RUNNING、stop→DISPOSED）；
- put/get 变量袋往返；
- 短命引擎实例池按 bookSourceUrl 复用 + LRU 淘汰；
- 外壳转发（search/book_info 等）与异常传播路径（用桩替换引擎方法，不触网）。

不依赖外网：网络方法一律用 monkeypatch 桩替换，或指向 127.0.0.1 立即拒绝连接。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.engine.rule_engine_service import RuleEngineService
from app.services.engine.reading_engine import LegadoReadingEngine


def _make_source(url: str = "https://src-a.example.com"):
    """构造最小书源（离线用，不触发真实网络）。"""
    return SimpleNamespace(
        bookSourceUrl=url,
        bookSourceName="test-source",
        bookSourceGroup="",
        bookSourceComment="",
        loginUrl="",
        header="{}",
        ruleSearch={"searchUrl": "http://127.0.0.1:1/search",
                    "checkWord": "", "exploreUrl": ""},
        ruleExplore={},
        ruleBookInfo={},
        ruleToc={},
        ruleContent={},
    )


def test_lifecycle_state_migration():
    """start() 后进入 RUNNING；stop() 后进入 DISPOSED。"""
    service = RuleEngineService(pool_size=2)
    assert str(service.status.state) in (
        "ServiceState.REGISTERED", "ServiceState.INIT",
        "REGISTERED", "INIT")

    async def flow():
        await service.start()
        assert service.status.state.value == "RUNNING"
        await service.stop()
        assert service.status.state.value == "DISPOSED"

    asyncio.run(flow())


def test_put_get_roundtrip():
    """put 写变量后 get 能读回（变量袋往返）。"""
    service = RuleEngineService()
    source = _make_source()

    async def flow():
        await service.start()
        await service.put(source, "loginName", "legado-user")
        assert await service.get(source, "loginName") == "legado-user"
        # 不存在的键回落默认值
        assert await service.get(source, "noSuchKey", "fallback") == "fallback"
        await service.stop()

    asyncio.run(flow())


def test_pool_reuses_engine_per_source():
    """同一 bookSourceUrl 两次取引擎应返回同一实例（池内复用）。"""
    service = RuleEngineService(pool_size=4)
    a = _make_source("https://src-a.example.com")
    b = _make_source("https://src-b.example.com")

    async def flow():
        await service.start()
        e1 = await service._get_engine(a)
        e2 = await service._get_engine(a)
        e3 = await service._get_engine(b)
        assert e1 is e2, "同源应复用同一引擎实例"
        assert e1 is not e3, "不同源应得到不同引擎实例"
        assert service._pool["https://src-a.example.com"] is e1
        await service.stop()

    asyncio.run(flow())


def test_pool_lru_eviction():
    """池超上限时按 LRU 淘汰最久未用实例。"""
    service = RuleEngineService(pool_size=1)
    a = _make_source("https://src-a.example.com")
    b = _make_source("https://src-b.example.com")

    async def flow():
        await service.start()
        e_a = await service._get_engine(a)
        _ = await service._get_engine(b)  # 触发淘汰 a
        # a 被淘汰后重新取会得到新实例
        e_a2 = await service._get_engine(a)
        assert e_a2 is not e_a
        assert len(service._pool) <= 1
        await service.stop()

    asyncio.run(flow())


def test_forwarding_search_calls_engine(monkeypatch):
    """search 转发：外壳应把参数原样转给 LegadoReadingEngine.search。"""
    service = RuleEngineService()
    captured = {}

    async def fake_search(self, source, keyword, page=1):
        captured["source"] = source
        captured["keyword"] = keyword
        captured["page"] = page
        return ["SENTINEL_BOOK"]

    monkeypatch.setattr(LegadoReadingEngine, "search", fake_search)
    source = _make_source()

    async def flow():
        await service.start()
        result = await service.search(source, "斗破苍穹", 3)
        await service.stop()
        return result

    result = asyncio.run(flow())
    assert result == ["SENTINEL_BOOK"]
    assert captured["keyword"] == "斗破苍穹"
    assert captured["page"] == 3


def test_forwarding_exception_propagates(monkeypatch):
    """异常传播：引擎层抛错应原样穿过外壳，不被吞。"""
    service = RuleEngineService()

    class _Boom(Exception):
        pass

    async def fake_book_info(self, source, book):
        raise _Boom("engine boom")

    monkeypatch.setattr(LegadoReadingEngine, "book_info", fake_book_info)
    source = _make_source()
    book = SimpleNamespace(name="x", url="https://x/y")

    async def flow():
        await service.start()
        with pytest.raises(_Boom):
            await service.book_info(source, book)
        await service.stop()

    asyncio.run(flow())
