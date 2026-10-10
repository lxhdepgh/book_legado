"""RPC 实现的引擎仓库（对接 GatewayService）。

用法：
    from app.gateway.client import GatewayClient
    from app.gateway.rpc_repository import RpcEngineRepository
    
    client = GatewayClient(hub)
    repo = RpcEngineRepository(client)
    items = await repo.get_shelf_items()
"""
from __future__ import annotations

from typing import Any

from app.core.repository import EngineFailure, EngineRepository
from app.gateway.client import GatewayClient


class RpcEngineRepository(EngineRepository):
    """通过 RPC 调用 GatewayService 的实现。
    
    Parameters
    ----------
    client : GatewayClient
        RPC 客户端实例
    """
    
    def __init__(self, client: GatewayClient):
        self._client = client
    
    # -- 内部工具 ------------------------------------------------------- #
    async def _call(self, action: str, params: dict | None = None) -> Any:
        """统一 RPC 调用包装（捕获异常并转为 EngineFailure）。
        
        Parameters
        ----------
        action : str
            action 名称（如 "bookshelf.list"）
        params : dict, optional
            请求参数
        
        Returns
        -------
        Any
            RPC 返回的数据
        
        Raises
        ------
        EngineFailure
            RPC 调用失败时抛出
        """
        try:
            return await self._client.call(action, params or {})
        except TimeoutError as exc:
            raise EngineFailure("timeout", f"{action} 超时", action=action) from exc
        except ConnectionError as exc:
            raise EngineFailure("transport", f"{action} 连接失败: {exc}", action=action) from exc
        except Exception as exc:
            raise EngineFailure("engine", f"{action} 失败: {exc}", action=action) from exc
    
    # -- 书架域 --------------------------------------------------------- #
    async def get_shelf_items(self) -> list[dict]:
        result = await self._call("bookshelf.list")
        return result.get("items", [])
    
    async def open_book(self, book_id: str, shelve: bool = True) -> dict:
        return await self._call("reader.open", {"book_id": book_id, "shelve": shelve})
    
    # -- 书源域 --------------------------------------------------------- #
    async def get_sources(self, group: str | None = None) -> list[dict]:
        params = {"group": group} if group else {}
        result = await self._call("sources.list", params)
        return result.get("sources", [])
    
    async def import_sources(self, content: str, overwrite: bool = False) -> dict:
        return await self._call("sources.import", {
            "content": content,
            "overwrite": overwrite,
        })
    
    async def enable_sources(self, urls: list[str]) -> dict:
        return await self._call("sources.enable", {"urls": urls})
    
    async def disable_sources(self, urls: list[str]) -> dict:
        return await self._call("sources.disable", {"urls": urls})
    
    async def delete_sources(self, urls: list[str]) -> dict:
        return await self._call("sources.delete", {"urls": urls})
    
    # -- 阅读器域 ------------------------------------------------------- #
    async def get_chapters(self, session_id: str) -> list[dict]:
        result = await self._call("reader.chapters", {"session_id": session_id})
        return result.get("chapters", [])
    
    async def get_content(self, chapter_id: str) -> dict:
        return await self._call("reader.content", {"chapter_id": chapter_id})
    
    async def get_toc(self, session_id: str) -> list[dict]:
        """获取目录（同 get_chapters，保留别名兼容）。"""
        return await self.get_chapters(session_id)
    
    # -- 搜索域 --------------------------------------------------------- #
    async def start_search(self, keyword: str, scope: dict | None = None) -> dict:
        params = {"keyword": keyword}
        if scope:
            params["scope"] = scope
        return await self._call("search.start", params)
    
    # -- 检测域 --------------------------------------------------------- #
    async def start_check(self, sources: list[str]) -> dict:
        return await self._call("check.start", {"sources": sources})
    
    # -- RSS 域 --------------------------------------------------------- #
    async def get_rss_feeds(self) -> list[dict]:
        result = await self._call("rss.list")
        return result.get("feeds", [])
    
    async def refresh_rss(self) -> dict:
        return await self._call("rss.refresh_all")
    
    # -- 设置域 --------------------------------------------------------- #
    async def get_settings(self, keys: list[str] | None = None) -> dict:
        params = {"keys": keys} if keys else {}
        return await self._call("settings.get", params)
    
    async def save_settings(self, settings: dict) -> dict:
        return await self._call("settings.save", {"settings": settings})
