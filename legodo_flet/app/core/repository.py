"""数据源抽象层（对齐 Dart 的 engine_repository.dart）。

职责：
- 定义所有与后端交互的方法签名
- 抛出的异常统一为 EngineFailure
- 支持测试时注入 FakeEngineRepository

设计原则：
- Controller 不再直接调用 hub.get(Service)
- 通过构造函数注入 repository: EngineRepository
- 测试时可注入 FakeEngineRepository 返回黄金样本
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- #
# 统一异常类型
# --------------------------------------------------------------------------- #
class EngineFailure(Exception):
    """引擎失败异常（对齐 Dart 的 EngineFailure）。
    
    Parameters
    ----------
    kind : str
        失败类型："timeout" | "transport" | "schema" | "engine"
    message : str
        人类可读的错误消息
    action : str, optional
        关联的 action 名称（如 "bookshelf.list"）
    """
    
    def __init__(self, kind: str, message: str, action: str | None = None):
        self.kind = kind
        self.message = message
        self.action = action
        super().__init__(message)
    
    @property
    def is_transient(self) -> bool:
        """是否可重试（超时/传输抖动算可重试，schema/engine 需改代码）。"""
        return self.kind in ("timeout", "transport")
    
    def __repr__(self) -> str:
        return f"EngineFailure(kind={self.kind!r}, message={self.message!r}, action={self.action!r})"


# --------------------------------------------------------------------------- #
# Repository 接口
# --------------------------------------------------------------------------- #
class EngineRepository(ABC):
    """引擎仓库抽象（所有数据访问的统一入口）。
    
    方法表 = app/gateway/methods.py 那张表，按域分组：
    - 书架域: get_shelf_items, open_book
    - 书源域: get_sources, import_sources, enable_sources, disable_sources, delete_sources
    - 阅读器域: get_chapters, get_content, get_toc
    - 搜索域: start_search
    - 检测域: start_check
    - RSS 域: get_rss_feeds, refresh_rss
    - 设置域: get_settings, save_settings
    """
    
    # -- 书架域 --------------------------------------------------------- #
    @abstractmethod
    async def get_shelf_items(self) -> list[dict]:
        """获取书架项目列表。
        
        Returns
        -------
        list[dict]
            每项包含：name, author, cover_url, last_read, progress
        """
        ...
    
    @abstractmethod
    async def open_book(self, book_id: str, shelve: bool = True) -> dict:
        """打开一本书（返回会话信息）。
        
        Parameters
        ----------
        book_id : str
            书籍标识
        shelve : bool
            是否加入书架（试读时为 False）
        
        Returns
        -------
        dict
            会话信息：session_id, book_id, chapters_count
        """
        ...
    
    # -- 书源域 --------------------------------------------------------- #
    @abstractmethod
    async def get_sources(self, group: str | None = None) -> list[dict]:
        """获取书源列表。
        
        Parameters
        ----------
        group : str, optional
            分组过滤（None = 全部）
        
        Returns
        -------
        list[dict]
            每项包含：url, name, group, enabled
        """
        ...
    
    @abstractmethod
    async def import_sources(self, content: str, overwrite: bool = False) -> dict:
        """导入书源（返回导入结果）。
        
        Parameters
        ----------
        content : str
            书源内容（JSON/文本）
        overwrite : bool
            是否覆盖同名书源
        
        Returns
        -------
        dict
            导入结果：total, success, failed, details
        """
        ...
    
    @abstractmethod
    async def enable_sources(self, urls: list[str]) -> dict:
        """启用书源。"""
        ...
    
    @abstractmethod
    async def disable_sources(self, urls: list[str]) -> dict:
        """禁用书源。"""
        ...
    
    @abstractmethod
    async def delete_sources(self, urls: list[str]) -> dict:
        """删除书源。"""
        ...
    
    # -- 阅读器域 ------------------------------------------------------- #
    @abstractmethod
    async def get_chapters(self, session_id: str) -> list[dict]:
        """获取章节目录。
        
        Parameters
        ----------
        session_id : str
            阅读会话 ID
        
        Returns
        -------
        list[dict]
            每项包含：title, chapter_id, index
        """
        ...
    
    @abstractmethod
    async def get_content(self, chapter_id: str) -> dict:
        """获取章节正文。
        
        Parameters
        ----------
        chapter_id : str
            章节 ID
        
        Returns
        -------
        dict
            正文内容：text, blocks, next_chapter_id
        """
        ...
    
    @abstractmethod
    async def get_toc(self, session_id: str) -> list[dict]:
        """获取目录（同 get_chapters，保留别名兼容）。"""
        ...
    
    # -- 搜索域 --------------------------------------------------------- #
    @abstractmethod
    async def start_search(self, keyword: str, scope: dict | None = None) -> dict:
        """启动搜索任务（mode="job"，返回受理凭据）。
        
        Parameters
        ----------
        keyword : str
            搜索关键词
        scope : dict, optional
            搜索范围（书源列表/分组）
        
        Returns
        -------
        dict
            受理凭据：{"started": true, "action": "search.start"}
            （注意：这不是结果，真结果走事件流）
        """
        ...
    
    # -- 检测域 --------------------------------------------------------- #
    @abstractmethod
    async def start_check(self, sources: list[str]) -> dict:
        """启动检测任务（mode="job"）。
        
        Parameters
        ----------
        sources : list[str]
            待检测的书源 URL 列表
        
        Returns
        -------
        dict
            受理凭据：{"started": true, "action": "check.start"}
        """
        ...
    
    # -- RSS 域 --------------------------------------------------------- #
    @abstractmethod
    async def get_rss_feeds(self) -> list[dict]:
        """获取 RSS 订阅列表。"""
        ...
    
    @abstractmethod
    async def refresh_rss(self) -> dict:
        """刷新所有 RSS 订阅（mode="job"）。"""
        ...
    
    # -- 设置域 --------------------------------------------------------- #
    @abstractmethod
    async def get_settings(self, keys: list[str] | None = None) -> dict:
        """获取设置项。
        
        Parameters
        ----------
        keys : list[str], optional
            要读取的设置键（None = 全部）
        
        Returns
        -------
        dict
            设置键值对
        """
        ...
    
    @abstractmethod
    async def save_settings(self, settings: dict) -> dict:
        """保存设置项。
        
        Parameters
        ----------
        settings : dict
            要保存的设置键值对
        
        Returns
        -------
        dict
            保存结果：{"saved": true}
        """
        ...
