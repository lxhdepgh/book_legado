"""出口序列化：把服务返回值压成 JSON 原生值（设计 §5.3）。

服务面今天允许返回 ``list[BookSource]``、``dataclass``、``Path`` 这类富对象
（``app/services/sources/source_service.py:288`` 的 ``list_sources`` 就是一例）——
在进程内它们是值，过了协议面就是**炸弹**：JSON 编码要么当场抛，要么把
``repr(obj)`` 塞进字符串里骗过客户端。

本模块把这个转换收在**边界**上，而不是逐个改 32 个服务的签名：

- :func:`to_json` 递归降级，永不抛；
- :func:`find_leak` 给护栏用——它扫一遍已经编码完毕的载荷，指出第一个
  不是 ``dict/list/str/int/float/bool/None`` 的叶子在哪。断言的是"没发生"，
  不是"没失败"（记忆 ``guard-tests-assert-absence``）。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Set
from dataclasses import is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any

#: JSON 原生类型全集。护栏按这个集合判"干净"。
PLAIN_TYPES: tuple[type, ...] = (dict, list, str, int, float, bool, type(None))


def to_json(value: Any) -> Any:
    """把任意服务返回值转成可 JSON 编码的原生结构（永不抛异常）。

    降级顺序就是优先级：**有 ``to_dict()`` 的用自己的口径**（``BookSource.to_dict``
    刻意不丢 false/0/空串，那是导入导出的口径，协议面必须跟它一致），
    dataclass 按字段展开，容器递归，标量按类型点名换，实在认得的才 ``str()``。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity 不是合法 JSON（严格解析器直接拒），客户端拿到的会是
        # "进度是 NaN" 这种查都查不出来的形状。折成 None 让界面按"没有数"渲染。
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, datetime) or isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_json(value.value)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_json(to_dict())
        except Exception:  # 口径坏了不拖垮整条响应：退回字段展开
            pass
    if is_dataclass(value) and not isinstance(value, type):
        return to_json({f.name: getattr(value, f.name, None)
                        for f in value.__dataclass_fields__.values()})
    if isinstance(value, Mapping):
        return {str(to_json(k)): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    if isinstance(value, Set):
        # 集合没有稳定顺序：直接编码会把同一份数据渲染成两种排列，界面按顺序
        # 比对的金样本因此随机红。按序列化后的文本排一次。
        return sorted((to_json(item) for item in value), key=lambda item: str(item))
    try:
        vars(value)
    except TypeError:
        return str(value)
    return to_json({name: attr for name, attr in vars(value).items()
                    if not name.startswith("_")})


def project(value: Any, fields: tuple[str, ...]) -> Any:
    """按 action 表声明的白名单裁掉**行列表**里每一行的键（``Method.fields``）。

    只做"留哪些键"，不做任何形状改造：行必须是 dict，否则当场抛——
    静默原样返回等于把裁剪写成一件没发生的事，而那正是这一格存在的唯一理由。
    键在某一行里不存在就**不补**（``BookSource.to_dict`` 按 Gson 丢 null，
    ``loginUrl`` 属于"这条源设了才有"的那一类）。
    """
    if not isinstance(value, list):
        raise TypeError(f"出口投影只吃行列表，收到 {type(value).__name__}")
    kept = []
    for row in value:
        if not isinstance(row, dict):
            raise TypeError(f"出口投影的行必须是 dict，收到 {type(row).__name__}")
        kept.append({name: row[name] for name in fields if name in row})
    return kept


def find_leak(value: Any, path: str = "$") -> str | None:
    """返回第一个"不是 JSON 原生值"的叶子路径；干净则返回 None。

    故意只按**类型**判、不看内容：协议面上出现 ``app.*`` 实例就等于把服务对象的
    生命周期交给了网关，无论它长得多像 dict 都不行。
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path}[key {key!r}]"
            found = find_leak(item, f"{path}[{key!r}]")
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_leak(item, f"{path}[{index}]")
            if found:
                return found
        return None
    return f"{path}=<{type(value).__module__}.{type(value).__name__}>"


def encode(payload: Any) -> str:
    """序列化成 JSON 文本；兜不住时退化成对 payload 的 ``to_json`` 再编码。

    走到第二层说明第一层撞上了非 JSON 类型（本不该发生，护栏会把它钉住）——
    这里仍**不抛**，因为抛出去就是把一个 traceback 甩给客户端的连接线程。
    """
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(to_json(payload), ensure_ascii=False, default=str)
