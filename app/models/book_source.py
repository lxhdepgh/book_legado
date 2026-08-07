"""
Legado 书源数据模型
"""
from __future__ import annotations
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse


@lru_cache(maxsize=4096)
def _short_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        return parsed.netloc or value[:40]
    except Exception:
        return value[:40]


@lru_cache(maxsize=4096)
def _search_text(name: str, url: str, group: str, comment: str) -> str:
    return "\n".join((name, url, group, comment)).lower()


@dataclass
class BookSource:
    """Legado 书源数据模型，对应 JSON 中的单个书源对象"""

    bookSourceName: str = ""
    bookSourceUrl: str = ""
    bookSourceGroup: str = ""
    bookSourceType: int = 2
    bookSourceComment: str = ""
    enabled: bool = True
    enabledCookieJar: bool = False
    enabledExplore: bool = False
    customOrder: int = 0
    lastUpdateTime: int = 0
    respondTime: int = 180000
    weight: int = 0

    # URL 相关
    searchUrl: str = ""
    exploreUrl: str = ""
    header: str = ""
    bookUrlPattern: str = ""
    loginUrl: str = ""
    loginCheckJs: str = ""
    loginUi: str = ""

    # 规则相关
    ruleSearch: dict = field(default_factory=dict)
    ruleBookInfo: dict = field(default_factory=dict)
    ruleContent: dict = field(default_factory=dict)
    ruleToc: dict = field(default_factory=dict)
    ruleExplore: dict = field(default_factory=dict)
    ruleReview: dict = field(default_factory=dict)

    # 高级
    concurrentRate: str = ""
    exploreScreen: str = ""
    jsLib: str = ""
    variableComment: str = ""
    coverDecodeJs: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> BookSource:
        """从 JSON 字典创建 BookSource 实例"""
        # This mapping is already keyed by field name; rebuilding a set for
        # every imported source adds avoidable startup work.
        field_names = cls.__dataclass_fields__
        kwargs = {k: v for k, v in data.items() if k in field_names}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """转换为可序列化的字典"""
        result = {}
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            if val is not None and val != "" and val != {} and val != 0 and val != []:
                result[f] = val
        # 始终输出的关键字段
        result["bookSourceName"] = self.bookSourceName
        result["bookSourceUrl"] = self.bookSourceUrl
        result["bookSourceGroup"] = self.bookSourceGroup
        result["bookSourceType"] = self.bookSourceType
        result["enabled"] = self.enabled
        # lastUpdateTime 总是写入
        result["lastUpdateTime"] = self.lastUpdateTime
        return result

    @classmethod
    def from_json(cls, path: str) -> list[BookSource]:
        """从 JSON 文件加载书源列表"""
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [cls.from_dict(item) for item in data]
        return []

    @property
    def display_name(self) -> str:
        return self.bookSourceName or "(未命名)"

    @property
    def display_group(self) -> str:
        return self.bookSourceGroup or "未分组"

    @property
    def short_url(self) -> str:
        return _short_url(self.bookSourceUrl)

    @property
    def search_text(self) -> str:
        """Lower-cased text reused by management-page searches."""
        return _search_text(
            self.bookSourceName,
            self.bookSourceUrl,
            self.bookSourceGroup or "",
            self.bookSourceComment or "",
        )

    def __str__(self) -> str:
        return f"[{'+' if self.enabled else '-'}] {self.bookSourceName} ({self.display_group})"
