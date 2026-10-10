"""测试用的假仓库（返回黄金样本）。

用法：
    from app.testing.fake_repository import FakeEngineRepository
    
    # 正常模式
    repo = FakeEngineRepository(shelf_items=[...])
    
    # 失败模式（测试错误处理）
    repo.set_fail_next(True)
    
    # 延迟模式（测试加载状态）
    repo.set_delay(1.0)  # 每次调用延迟 1 秒
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.core.repository import EngineFailure, EngineRepository


#: 书架黄金样本（用于测试）
DEFAULT_SHELF_ITEMS = [
    {
        "book_id": "test-book-1",
        "name": "测试书籍 1",
        "author": "作者 A",
        "cover_url": "",
        "last_read": "第 10 章",
        "progress": 0.5,
    },
    {
        "book_id": "test-book-2",
        "name": "测试书籍 2",
        "author": "作者 B",
        "cover_url": "",
        "last_read": "第 3 章",
        "progress": 0.2,
    },
]

#: 书源黄金样本
DEFAULT_SOURCES = [
    {
        "url": "https://example.com/source1",
        "name": "示例书源 1",
        "group": "默认分组",
        "enabled": True,
    },
    {
        "url": "https://example.com/source2",
        "name": "示例书源 2",
        "group": "测试分组",
        "enabled": False,
    },
]

#: 章节目录黄金样本
DEFAULT_CHAPTERS = [
    {"title": "第一章 开始", "chapter_id": "ch-1", "index": 0},
    {"title": "第二章 发展", "chapter_id": "ch-2", "index": 1},
    {"title": "第三章 高潮", "chapter_id": "ch-3", "index": 2},
]

#: 正文章节黄金样本
DEFAULT_CONTENT = {
    "text": "这是测试正文内容。\n\n第二段内容。",
    "blocks": [
        {"kind": "text", "value": "这是测试正文内容。"},
        {"kind": "text", "value": "第二段内容。"},
    ],
    "next_chapter_id": "ch-2",
}


class FakeEngineRepository(EngineRepository):
    """返回预定义数据的测试替身。
    
    Parameters
    ----------
    shelf_items : list[dict], optional
        书架项目列表（默认使用 DEFAULT_SHELF_ITEMS）
    sources : list[dict], optional
        书源列表（默认使用 DEFAULT_SOURCES）
    chapters : list[dict], optional
        章节目录（默认使用 DEFAULT_CHAPTERS）
    content : dict, optional
        章节正文（默认使用 DEFAULT_CONTENT）
    """
    
    def __init__(
        self,
        shelf_items: list[dict] | None = None,
        sources: list[dict] | None = None,
        chapters: list[dict] | None = None,
        content: dict | None = None,
    ):
        self._shelf_items = shelf_items or DEFAULT_SHELF_ITEMS
        self._sources = sources or DEFAULT_SOURCES
        self._chapters = chapters or DEFAULT_CHAPTERS
        self._content = content or DEFAULT_CONTENT
        
        #: 是否在下一次调用时失败
        self._fail_next = False
        #: 调用延迟（秒，用于测试加载状态）
        self._delay = 0.0
        #: 调用计数（用于验证调用次数）
        self.call_count: dict[str, int] = {}
    
    # -- 控制方法 ------------------------------------------------------- #
    def set_fail_next(self, value: bool):
        """设置下一次调用失败（用于测试错误处理）。"""
        self._fail_next = value
    
    def set_delay(self, seconds: float):
        """设置调用延迟（用于测试加载状态）。"""
        self._delay = max(0.0, seconds)
    
    def reset_call_count(self):
        """重置调用计数。"""
        self.call_count.clear()
    
    def _count_call(self, action: str):
        """记录调用次数。"""
        self.call_count[action] = self.call_count.get(action, 0) + 1
    
    async def _maybe_fail(self):
        """如果设置了 fail_next，则抛出异常。"""
        if self._fail_next:
            self._fail_next = False
            raise EngineFailure("timeout", "模拟超时（测试用）")
        
        if self._delay > 0:
            await asyncio.sleep(self._delay)
    
    # -- 书架域 --------------------------------------------------------- #
    async def get_shelf_items(self) -> list[dict]:
        self._count_call("get_shelf_items")
        await self._maybe_fail()
        return self._shelf_items
    
    async def open_book(self, book_id: str, shelve: bool = True) -> dict:
        self._count_call("open_book")
        await self._maybe_fail()
        return {
            "session_id": f"fake-session-{book_id}",
            "book_id": book_id,
            "chapters_count": len(self._chapters),
        }
    
    # -- 书源域 --------------------------------------------------------- #
    async def get_sources(self, group: str | None = None) -> list[dict]:
        self._count_call("get_sources")
        await self._maybe_fail()
        if group:
            return [s for s in self._sources if s.get("group") == group]
        return self._sources
    
    async def import_sources(self, content: str, overwrite: bool = False) -> dict:
        self._count_call("import_sources")
        await self._maybe_fail()
        return {
            "total": 1,
            "success": 1,
            "failed": 0,
            "details": [],
        }
    
    async def enable_sources(self, urls: list[str]) -> dict:
        self._count_call("enable_sources")
        await self._maybe_fail()
        return {"enabled": len(urls)}
    
    async def disable_sources(self, urls: list[str]) -> dict:
        self._count_call("disable_sources")
        await self._maybe_fail()
        return {"disabled": len(urls)}
    
    async def delete_sources(self, urls: list[str]) -> dict:
        self._count_call("delete_sources")
        await self._maybe_fail()
        return {"deleted": len(urls)}
    
    # -- 阅读器域 ------------------------------------------------------- #
    async def get_chapters(self, session_id: str) -> list[dict]:
        self._count_call("get_chapters")
        await self._maybe_fail()
        return self._chapters
    
    async def get_content(self, chapter_id: str) -> dict:
        self._count_call("get_content")
        await self._maybe_fail()
        return self._content
    
    async def get_toc(self, session_id: str) -> list[dict]:
        """获取目录（同 get_chapters，保留别名兼容）。"""
        return await self.get_chapters(session_id)
    
    # -- 搜索域 --------------------------------------------------------- #
    async def start_search(self, keyword: str, scope: dict | None = None) -> dict:
        self._count_call("start_search")
        await self._maybe_fail()
        return {"started": True, "action": "search.start"}
    
    # -- 检测域 --------------------------------------------------------- #
    async def start_check(self, sources: list[str]) -> dict:
        self._count_call("start_check")
        await self._maybe_fail()
        return {"started": True, "action": "check.start"}
    
    # -- RSS 域 --------------------------------------------------------- #
    async def get_rss_feeds(self) -> list[dict]:
        self._count_call("get_rss_feeds")
        await self._maybe_fail()
        return []
    
    async def refresh_rss(self) -> dict:
        self._count_call("refresh_rss")
        await self._maybe_fail()
        return {"started": True, "action": "rss.refresh_all"}
    
    # -- 设置域 --------------------------------------------------------- #
    async def get_settings(self, keys: list[str] | None = None) -> dict:
        self._count_call("get_settings")
        await self._maybe_fail()
        return {}
    
    async def save_settings(self, settings: dict) -> dict:
        self._count_call("save_settings")
        await self._maybe_fail()
        return {"saved": True}
