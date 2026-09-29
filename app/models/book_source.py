"""
Legado 书源数据模型
"""
from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse


#: 规则段的合法字段表——逐段照 Legado ``data/entities/rule/*.kt`` 的 data class
#: 构造参数写定（SearchRule/ExploreRule/BookInfoRule/TocRule/ContentRule/ReviewRule，
#: ExploreRule 与 SearchRule 复用 BookListRule）。
#: ``from_dict`` 按这张表**丢弃**表外字段，``compat_audit_service._audit_one`` 的
#: 「未知规则字段」判定的也是这张表（逐段）——合法字段只能有一份定义，两处各写一份时
#: 漂移会同时冤枉合法源和放过被丢的字段（09-29 第四十五轮实测：冤枉 12、放过 146/206）。
RULE_FIELDS = {
    "ruleSearch": {"checkKeyWord", "bookList", "name", "author", "intro", "kind",
                   "lastChapter", "updateTime", "bookUrl", "coverUrl", "wordCount"},
    "ruleExplore": {"bookList", "name", "author", "intro", "kind", "lastChapter",
                    "updateTime", "bookUrl", "coverUrl", "wordCount"},
    "ruleBookInfo": {"init", "name", "author", "intro", "kind", "lastChapter",
                     "updateTime", "coverUrl", "tocUrl", "wordCount", "canReName",
                     "downloadUrls"},
    "ruleToc": {"preUpdateJs", "chapterList", "chapterName", "chapterUrl", "formatJs",
                "isVolume", "isVip", "isPay", "updateTime", "nextTocUrl"},
    "ruleContent": {"content", "subContent", "title", "nextContentUrl", "webJs",
                    "sourceRegex", "replaceRegex", "imageStyle", "imageDecode",
                    "payAction", "callBackJs"},
    "ruleReview": {"reviewUrl", "avatarRule", "contentRule", "postTimeRule",
                   "reviewQuoteUrl", "voteUpUrl", "voteDownUrl", "postReviewUrl",
                   "postQuoteUrl", "deleteUrl"},
}


def _gson_string(value: Any) -> str | None:
    """Fixed StringJsonDeserializer semantics used by source/rule fields."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _gson_boolean(value: Any, default: bool, nullable: bool = False) -> bool | None:
    """Gson's built-in Boolean adapter plus primitive-field null skipping."""
    if value is None:
        return None if nullable else default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    raise ValueError("boolean field must be true, false, a string, or null")


def _gson_long(value: Any, default: int = 0) -> int:
    """Mirror JsonReader.nextLong, including integral decimal strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("long field cannot be boolean")
    try:
        if isinstance(value, int) and -(2 ** 63) <= value <= 2 ** 63 - 1:
            result = value
        elif isinstance(value, float):
            number = value
            result = None
        elif isinstance(value, str):
            try:
                result = int(value, 10)
                if not -(2 ** 63) <= result <= 2 ** 63 - 1:
                    raise ValueError
            except ValueError:
                number = float(value)
                result = None
        elif isinstance(value, int):
            number = float(value)
            result = None
        else:
            raise ValueError
        if result is None:
            if not math.isfinite(number):
                raise ValueError
            if number >= float(2 ** 63 - 1):
                result = 2 ** 63 - 1
            elif number <= float(-(2 ** 63)):
                result = -(2 ** 63)
            else:
                result = int(number)
            # JsonReader accepts the fallback only if conversion back to
            # double equals the parsed value.  This intentionally preserves
            # Gson's near-boundary saturation caused by IEEE-754 rounding.
            if float(result) != number:
                raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid long value: {value!r}") from exc
    return result


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


@dataclass(eq=False)
class BookSource:
    """Legado 书源数据模型，对应 JSON 中的单个书源对象"""

    bookSourceName: str = ""
    bookSourceUrl: str = ""
    bookSourceGroup: str | None = None
    bookSourceType: int = 0
    bookSourceComment: str | None = None
    enabled: bool = True
    enabledCookieJar: bool | None = True
    enabledExplore: bool = True
    customOrder: int = 0
    lastUpdateTime: int = 0
    respondTime: int = 180000
    weight: int = 0

    # URL 相关
    searchUrl: str | None = None
    exploreUrl: str | None = None
    header: str | None = None
    bookUrlPattern: str | None = None
    loginUrl: str | None = None
    loginCheckJs: str | None = None
    loginUi: str | None = None

    # 规则相关
    ruleSearch: dict | None = None
    ruleBookInfo: dict | None = None
    ruleContent: dict | None = None
    ruleToc: dict | None = None
    ruleExplore: dict | None = None
    ruleReview: dict | None = None

    # 高级
    concurrentRate: str | None = None
    exploreScreen: str | None = None
    jsLib: str | None = None
    variableComment: str | None = None
    coverDecodeJs: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> BookSource:
        if any(key in data for key in ("ruleSearchList", "ruleFindList",
                                        "ruleBookContent", "ruleSearchUrl")):
            from .legacy_book_source import normalize_old_book_source
            data = normalize_old_book_source(data)
        """从 JSON 字典创建 BookSource 实例"""
        # This mapping is already keyed by field name; rebuilding a set for
        # every imported source adds avoidable startup work.
        field_names = cls.__dataclass_fields__
        kwargs = {k: v for k, v in data.items() if k in field_names}
        string_fields = {
            "bookSourceName", "bookSourceUrl", "bookSourceGroup", "bookUrlPattern",
            "jsLib", "concurrentRate", "header", "loginUrl", "loginUi",
            "loginCheckJs", "coverDecodeJs", "bookSourceComment", "variableComment",
            "exploreUrl", "exploreScreen", "searchUrl",
        }
        for name in string_fields & kwargs.keys():
            kwargs[name] = _gson_string(kwargs[name])
        for name, default in (("bookSourceType", 0), ("customOrder", 0),
                              ("weight", 0)):
            if name in kwargs:
                value = kwargs[name]
                kwargs[name] = (int(value) if isinstance(value, (int, float))
                                and not isinstance(value, bool) else default)
        for name, default, nullable in (("enabled", True, False),
                                        ("enabledExplore", True, False),
                                        ("enabledCookieJar", True, True)):
            if name in kwargs:
                kwargs[name] = _gson_boolean(kwargs[name], default, nullable)
        for name, default in (("lastUpdateTime", 0), ("respondTime", 180000)):
            if name in kwargs:
                kwargs[name] = _gson_long(kwargs[name], default)
        # Legado's rule deserializers accept both a JSON object and a JSON
        # primitive containing the serialized object.  Older/shared sources
        # commonly use the latter form.
        for name in RULE_FIELDS:
            value = kwargs.get(name)
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid serialized {name}") from exc
                if not isinstance(decoded, dict):
                    raise ValueError(f"serialized {name} is not a JSON object")
                kwargs[name] = decoded
                value = decoded
            if isinstance(value, dict):
                kwargs[name] = {key: _gson_string(item) for key, item in value.items()
                                if key in RULE_FIELDS[name]}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """转换为可序列化的字典"""
        result = {}
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            # Gson excludes null values by default, but does not discard false,
            # zero or empty strings/objects.  Those values are observable when a
            # source is exported and imported again.
            if val is not None:
                result[f] = ({key: item for key, item in val.items()
                              if item is not None}
                             if f in RULE_FIELDS and isinstance(val, dict)
                             else val)
        # 始终输出的关键字段
        # lastUpdateTime 总是写入
        return result

    def __eq__(self, other: object) -> bool:
        """Room entity identity follows Legado: the source URL is the key."""
        return isinstance(other, BookSource) and self.bookSourceUrl == other.bookSourceUrl

    def __hash__(self) -> int:
        return hash(self.bookSourceUrl)

    def getTag(self) -> str:
        return self.bookSourceName

    def getKey(self) -> str:
        return self.bookSourceUrl

    def _get_rule(self, name: str) -> dict:
        value = getattr(self, name)
        if value is None:
            value = {}
            setattr(self, name, value)
        return value

    def getSearchRule(self) -> dict:
        return self._get_rule("ruleSearch")

    def getExploreRule(self) -> dict:
        return self._get_rule("ruleExplore")

    def getBookInfoRule(self) -> dict:
        return self._get_rule("ruleBookInfo")

    def getTocRule(self) -> dict:
        return self._get_rule("ruleToc")

    def getContentRule(self) -> dict:
        return self._get_rule("ruleContent")

    def getReviewRule(self) -> dict:
        return self._get_rule("ruleReview")

    @staticmethod
    def _groups(value: str | None) -> list[str]:
        return [item.strip() for item in re.split(r"[,;，；]", value or "")
                if item.strip()]

    def getDisPlayNameGroup(self) -> str:
        if not (self.bookSourceGroup or "").strip():
            return self.bookSourceName
        return f"{self.bookSourceName} ({self.bookSourceGroup})"

    def addGroup(self, groups: str) -> BookSource:
        if self.bookSourceGroup is not None:
            values = self._groups(self.bookSourceGroup)
            for value in self._groups(groups):
                if value not in values:
                    values.append(value)
            self.bookSourceGroup = ",".join(values)
        if not (self.bookSourceGroup or "").strip():
            self.bookSourceGroup = groups
        return self

    def removeGroup(self, groups: str) -> BookSource:
        if self.bookSourceGroup is None:
            return self
        removed = set(self._groups(groups))
        self.bookSourceGroup = ",".join(
            value for value in self._groups(self.bookSourceGroup)
            if value not in removed
        )
        return self

    def hasGroup(self, group: str) -> bool:
        return group in self._groups(self.bookSourceGroup)

    def getInvalidGroupNames(self) -> str:
        return ", ".join(group for group in self._groups(self.bookSourceGroup)
                         if "失效" in group or group == "校验超时")

    def removeInvalidGroups(self) -> None:
        self.removeGroup(self.getInvalidGroupNames())

    def getDisplayVariableComment(self, otherComment: str) -> str:
        if not (self.variableComment or "").strip():
            return otherComment
        return f"{self.variableComment}\n{otherComment}"

    @staticmethod
    def _equal_text(left: str | None, right: str | None) -> bool:
        return left == right or (not left and not right)

    def equal(self, source: BookSource) -> bool:
        """Configuration equality used by Legado's import/update workflow."""
        text_fields = (
            "bookSourceName", "bookSourceUrl", "bookSourceGroup",
            "bookUrlPattern", "bookSourceComment", "concurrentRate", "header",
            "loginUrl", "loginUi", "loginCheckJs", "coverDecodeJs",
            "exploreUrl", "searchUrl",
        )
        if any(not self._equal_text(getattr(self, name), getattr(source, name))
               for name in text_fields):
            return False
        scalar_fields = (
            "bookSourceType", "customOrder", "enabled", "enabledExplore",
            "enabledCookieJar",
        )
        if any(getattr(self, name) != getattr(source, name)
               for name in scalar_fields):
            return False
        return all(left() == right() for left, right in (
            (self.getSearchRule, source.getSearchRule),
            (self.getExploreRule, source.getExploreRule),
            (self.getBookInfoRule, source.getBookInfoRule),
            (self.getTocRule, source.getTocRule),
            (self.getContentRule, source.getContentRule),
        ))

    @classmethod
    def from_json(cls, path: str) -> list[BookSource]:
        """从 JSON 文件加载书源列表"""
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [cls.from_dict(item) for item in data]
        if isinstance(data, dict):
            return [cls.from_dict(data)]
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
