"""JSON Schema 的**极小子集**校验器 + schema 文件加载（设计 §4.5）。

为什么不用 `jsonschema` 库：那是要往 `requirements.txt` 里加一条运行时依赖，而三端
（含 PyInstaller 冻结包）都得跟着背。协议面用到的 schema 特征就六个
（`type/required/properties/items/enum/additionalProperties`），自己实现约 80 行，
并且**只支持这六个**——写进护栏：schema 里出现别的关键词直接红，避免"文件里写了、
运行时静默不生效"这种最阴的形状。

单一来源规矩：`app/gateway/schema/<action>.json` 是唯一的字段定义处。
Python 用它校验入参；将来 Dart 侧的类型由它生成（G1），**不许**在任一侧手写第二份字段表。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

#: 本校验器支持的关键词；出现表外的一律按"schema 写错了"当场拒（见模块 docstring）。
#: 这里不含 ``default``：分发器只把 payload 里**出现**的键传给方法（缺省就走方法自己的
#: 形参默认值），校验器不会替谁填默认值。留着它等于开一个"写了但不生效"的门——
#: 正是本模块要拒的那种形状，所以直接把它请出白名单。
SUPPORTED_KEYWORDS: frozenset[str] = frozenset({
    "type", "required", "properties", "items", "enum",
    "additionalProperties", "description", "minimum", "maximum",
})

_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    """入参没过 schema。``path`` 指到出错的那个键，客户端才改得动。"""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def schema_path(action: str) -> Path:
    return _SCHEMA_DIR / f"{action}.json"


def _reject_unknown_keywords(node: Any, where: str, path: Path) -> None:
    """递归查每一层**子 schema** 的关键词：顶层查得掉、嵌套查不掉，就是"文件里写了、
    运行时静默不生效"漏掉的那一半（``properties.foo.minLength`` 那一类）。"""
    if isinstance(node, dict):
        unsupported = set(node) - SUPPORTED_KEYWORDS
        if unsupported:
            raise ValueError(f"schema 在 {where} 用了未实现的关键词 "
                             f"{sorted(unsupported)}：{path}")
        for key, value in node.items():
            if key == "enum":
                continue          # enum 的值是数据不是 schema
            if key in ("properties", "items", "additionalProperties"):
                if isinstance(value, dict):
                    for name, sub in (value.items() if key == "properties"
                                      else [("", value)]):
                        _reject_unknown_keywords(sub, f"{where}.{key}.{name}".rstrip("."),
                                                 path)
                elif isinstance(value, list):
                    for index, sub in enumerate(value):
                        _reject_unknown_keywords(sub, f"{where}.{key}[{index}]", path)


@lru_cache(maxsize=None)
def load(action: str) -> dict:
    """读某个 action 的入参 schema（带缓存；文件缺失/坏掉都是装配错误，直接抛）。"""
    path = schema_path(action)
    if not path.is_file():
        raise FileNotFoundError(f"action {action!r} 没有入参 schema：{path}")
    with path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError(f"schema 必须是 JSON 对象：{path}")
    _reject_unknown_keywords(schema, "$", path)
    return schema


def known_actions() -> list[str]:
    """schema 目录里都有哪几个 action（护栏用来和 action 表做双向比对）。"""
    return sorted(p.stem for p in _SCHEMA_DIR.glob("*.json"))


def validate(instance: Any, schema: dict, path: str = "$") -> None:
    """就地校验；不合规抛 :class:`SchemaError`。"""
    kind = schema.get("type")
    if kind is not None and not _type_ok(instance, kind):
        raise SchemaError(path, f"类型应为 {kind}，实为 {_name(instance)}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(path, f"取值要在 {schema['enum']} 里")
    if kind == "object" or isinstance(instance, dict):
        _validate_object(instance, schema, path)
    if kind == "array" or isinstance(instance, list):
        _validate_array(instance, schema, path)
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(path, f"不能小于 {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(path, f"不能大于 {schema['maximum']}")


def _validate_object(instance: Any, schema: dict, path: str) -> None:
    if not isinstance(instance, dict):
        return
    for key in schema.get("required", ()):
        if key not in instance:
            raise SchemaError(f"{path}.{key}", "缺少必填字段")
    props = schema.get("properties") or {}
    if schema.get("additionalProperties", True) is False:
        extra = [k for k in instance if k not in props]
        if extra:
            raise SchemaError(path, f"不认识的字段：{sorted(extra)}")
    for key, sub in props.items():
        if key in instance and isinstance(sub, dict):
            validate(instance[key], sub, f"{path}.{key}")


def _validate_array(instance: Any, schema: dict, path: str) -> None:
    if not isinstance(instance, list):
        return
    sub = schema.get("items")
    if isinstance(sub, dict):
        for index, item in enumerate(instance):
            validate(item, sub, f"{path}[{index}]")


def _type_ok(value: Any, kind: Any) -> bool:
    kinds = kind if isinstance(kind, (list, tuple)) else [kind]
    return any(_matches(value, one) for one in kinds)


def _matches(value: Any, kind: str) -> bool:
    allowed = _TYPES.get(str(kind))
    if allowed is None:
        raise ValueError(f"schema 里有不认识的类型 {kind!r}")
    if str(kind) == "integer" and isinstance(value, bool):
        return False
    if str(kind) == "number" and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _name(value: Any) -> str:
    return type(value).__name__
