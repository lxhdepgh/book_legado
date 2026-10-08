"""协议面契约护栏（设计 §4 / §5.2）。

这里钉的是**静态事实**：action 表、入参 schema、信封、边界序列化、schema 子集校验器
五者之间必须逐字对齐。为什么每一条都要钉：

- 表与真方法脱钩 → 客户端调用得到一个 AttributeError 折成的 ``engine`` 错误，
  而服务其实好好的（记忆 ``refactor-regression-orphan-methods``：重构后方法还在、
  接线断了，测试全绿）；
- 表与 schema 脱钩 → 一侧改字段、另一侧静默不生效，客户端按旧字段发了就永远丢参；
- ``mode`` 与"方法是不是协程"脱钩 → ``sync`` 打到协程方法上，网关会把**协程对象本体**
  序列化回客户端（看起来是一次成功、data 里是一坨 ``<coroutine>``）；
- 边界序列化破口 → JSON 编码在最后一米抛，客户端解析器遇到第二种形状。

运行期那一半（真起一个网关、真发 HTTP/WS）在 ``tests/test_gateway_inproc.py``。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.gateway import jsonschema_min as schema
from app.gateway.methods import BY_ACTION, METHODS, resolve_service_class
from app.gateway.protocol import (
    CODE_ENGINE, ERROR_CODES, MAX_BODY_BYTES, PROTOCOL_VERSION, digest,
    err_envelope, ok_envelope,
)
from app.gateway.serialize import find_leak, to_json


class ActionTableTests(unittest.TestCase):
    """action 表本身：每条登记都必须解析得到真东西。"""

    def test_every_action_resolves_to_a_real_callable(self) -> None:
        broken = []
        for method in METHODS:
            try:
                cls = resolve_service_class(method.service)
            except Exception as exc:
                broken.append(f"{method.action}: {method.service} 解析失败 {exc!r}")
                continue
            if not callable(getattr(cls, method.method, None)):
                broken.append(
                    f"{method.action}: {cls.__name__}.{method.method} 不存在")
        self.assertEqual(broken, [], "action 表指向了不存在的方法：\n" + "\n".join(broken))

    def test_params_match_the_method_signature(self) -> None:
        """表里写的每个参数名都得是目标方法的一个关键字参数（改名即红）。"""
        problems = []
        for method in METHODS:
            func = getattr(resolve_service_class(method.service), method.method)
            if method.params and not _accepts_kwargs(func, method.params):
                problems.append(
                    f"{method.action}: {method.method} 不接受 {method.params}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_mode_declares_how_the_target_must_look(self) -> None:
        """``call``/``job`` 必须是协程方法，``sync`` 必须不是——否则派发方式错了。"""
        wrong = []
        for method in METHODS:
            func = getattr(resolve_service_class(method.service), method.method)
            coroutine = asyncio.iscoroutinefunction(func)
            if method.mode in ("call", "job") and not coroutine:
                wrong.append(f"{method.action}: mode={method.mode} 但目标不是协程方法")
            if method.mode == "sync" and coroutine:
                wrong.append(
                    f"{method.action}: mode=sync 但目标是协程方法"
                    "（网关不会 await，客户端会收到协程对象）")
            if method.mode not in ("call", "job", "sync"):
                wrong.append(f"{method.action}: 未知 mode={method.mode}")
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_timeouts_are_positive_and_actions_unique(self) -> None:
        seen = [m.action for m in METHODS]
        self.assertEqual(len(seen), len(set(seen)), f"action 重名：{seen}")
        bad = [m.action for m in METHODS if not m.timeout > 0]
        self.assertEqual(bad, [], f"预算必须为正：{bad}")

    def test_every_action_has_a_summary(self) -> None:
        """每条动作都要写一句"这是干什么的"：动作表是给下一个接手的人读的。"""
        silent = [m.action for m in METHODS if not m.summary.strip()]
        self.assertEqual(silent, [], f"缺摘要的动作：{silent}")


class SchemaAlignmentTests(unittest.TestCase):
    """schema 目录 ↔ action 表：字段只许有一处定义（设计 §4.5）。"""

    def test_schema_dir_and_action_table_are_the_same_set(self) -> None:
        self.assertEqual(schema.known_actions(), sorted(BY_ACTION),
                         "入参 schema 目录与 action 表不是同一批动作")

    def test_params_equal_schema_properties(self) -> None:
        drift = []
        for action, method in sorted(BY_ACTION.items()):
            props = sorted((schema.load(action).get("properties") or {}))
            if props != sorted(method.params):
                drift.append(f"{action}: schema {props} != 表 {sorted(method.params)}")
        self.assertEqual(drift, [], "字段两侧各写一遍已经飘了：\n" + "\n".join(drift))

    def test_every_schema_is_a_closed_object(self) -> None:
        """每个入参 schema 都必须是 ``additionalProperties: false`` 的对象。

        放开未知字段等于把"客户端多发了一个键"这种拼写错误变成静默丢弃——
        而它恰恰是最难查的一类前端 bug。
        """
        open_ones = [action for action in BY_ACTION
                     if schema.load(action).get("type") != "object"
                     or schema.load(action).get("additionalProperties") is not False]
        self.assertEqual(open_ones, [], f"schema 未收口的动作：{open_ones}")

    def test_unsupported_keyword_is_rejected_at_any_depth(self) -> None:
        """表外关键词必须**当场拒**，且嵌套层也查（"文件里写了、运行时不生效"）。"""
        nested = {
            "type": "object",
            "properties": {"groups": {"type": "array", "items": {
                "type": "string", "minLength": 2}}},
        }
        with self.assertRaises(ValueError):
            schema._reject_unknown_keywords(nested, "$", Path("<内存里的 schema>"))
        # 正对照：同一份去掉 minLength 就是干净的（否则"抛了"可能是无条件抛）。
        clean = json.loads(json.dumps(nested))
        del clean["properties"]["groups"]["items"]["minLength"]
        schema._reject_unknown_keywords(clean, "$", Path("<内存里的 schema>"))


class EnvelopeTests(unittest.TestCase):
    """信封形状是冻结的：客户端解析器只认这一种。"""

    KEYS = ["code", "data", "error", "id", "ok", "v"]

    def test_both_envelopes_have_the_same_keys(self) -> None:
        self.assertEqual(sorted(ok_envelope("x", 1)), self.KEYS)
        self.assertEqual(sorted(err_envelope("x", "timeout", "m")), self.KEYS)

    def test_unknown_error_code_degrades_to_engine(self) -> None:
        """最后一道兜底自己不许成为炸点。"""
        self.assertEqual(err_envelope("x", "nonsense", "m")["code"], CODE_ENGINE)
        for code in ERROR_CODES:
            self.assertEqual(err_envelope("x", code, "m")["code"], code)

    def test_digest_is_order_insensitive_but_content_sensitive(self) -> None:
        left = digest({"a": 1, "b": [1, 2]})
        right = digest({"b": [1, 2], "a": 1})
        self.assertEqual(left, right, "同一组内容必须永远得到同一个串")
        self.assertNotEqual(left, digest({"a": 1, "b": [1, 3]}))
        self.assertEqual(len(left), 12)

    def test_protocol_version_and_body_cap(self) -> None:
        self.assertEqual(PROTOCOL_VERSION, 1)
        self.assertEqual(MAX_BODY_BYTES, 1 << 20)


@dataclass
class _Widget:
    name: str
    count: int


class SerializeBoundaryTests(unittest.TestCase):
    """出口序列化：永不抛、永不漏对象。"""

    def test_real_book_source_uses_its_own_dict_shape(self) -> None:
        """``BookSource.to_dict`` 刻意保留 false/0/空串（导入导出口径），协议面跟它一致。"""
        from app.models.book_source import BookSource

        raw = {"bookSourceUrl": "https://x.example", "bookSourceName": "口径",
               "enabled": False, "customOrder": 0, "bookSourceGroup": ""}
        encoded = to_json(BookSource.from_dict(raw))
        self.assertIs(encoded["enabled"], False)
        self.assertEqual(encoded["customOrder"], 0)
        self.assertEqual(encoded["bookSourceGroup"], "")
        self.assertIsNone(find_leak(encoded))

    def test_every_kind_of_service_value_becomes_json_native(self) -> None:
        from datetime import datetime
        from enum import Enum
        from pathlib import PureWindowsPath

        class _Color(Enum):
            RED = "red"

        values = {
            "dataclass": _Widget("w", 3),
            "path": PureWindowsPath("C:/x/y.jpg"),
            "enum": _Color.RED,
            "datetime": datetime(2026, 10, 8, 12, 0),
            "set": {"b", "a"},
            "bytes": b"utf8-ok",
            "nan": float("nan"),
            "inf": float("inf"),
            "nested": [{"tup": (1, 2)}, {1: "int-key"}, {True: "bool-key"}],
            "object": object(),
        }
        encoded = to_json(values)
        json.dumps(encoded)                       # 不许抛
        self.assertIsNone(find_leak(encoded))
        self.assertIsNone(encoded["nan"], "NaN 不是合法 JSON，必须折成 None")
        self.assertIsNone(encoded["inf"])
        self.assertEqual(encoded["set"], ["a", "b"], "集合要稳定顺序")
        self.assertEqual(encoded["enum"], "red")
        self.assertEqual(encoded["nested"][1], {"1": "int-key"},
                         "非字符串键要折成字符串，否则 json 两侧口径不一致")
        self.assertEqual(list(encoded["nested"][2]), ["True"],
                         "布尔键经 str() 成 'True'：Python 的 json.dumps 会把 True "
                         "写成裸 true，两边形状不同就是这一条要显式钉住的原因")

    def test_find_leak_actually_catches_a_service_object(self) -> None:
        """正对照：仪器必须能认出一个真对象，否则"全程没漏"只是没接上。"""
        leaked = {"books": [_Widget("漏", 1)]}
        path = find_leak(leaked)
        self.assertIsNotNone(path)
        self.assertIn("_Widget", path)
        self.assertIn("[0]", path)

    def test_to_json_never_raises_on_a_broken_to_dict(self) -> None:
        class _Broken:
            def __init__(self) -> None:
                self.value = 1

            def to_dict(self):
                raise RuntimeError("口径坏了")

        self.assertEqual(to_json(_Broken()), {"value": 1})


def _accepts_kwargs(func, names: tuple[str, ...]) -> bool:
    target = inspect.unwrap(func)
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return False
    for kind in signature.parameters.values():
        if kind.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    accepted = set(signature.parameters) - {"self", "cls"}
    return set(names) <= accepted


class EventStreamConfigTests(unittest.TestCase):
    """网关的事件面配置：写错一个事件名就是"配置在、没人发"。"""

    def test_coalesced_and_snapshot_names_are_real_events(self) -> None:
        from app.services.gateway.service import (
            COALESCED_EVENTS, SNAPSHOT_EVENTS, event_types)

        known = {cls.__name__ for cls in event_types()}
        for label, group in (("COALESCED_EVENTS", COALESCED_EVENTS),
                             ("SNAPSHOT_EVENTS", SNAPSHOT_EVENTS)):
            ghost = sorted(set(group) - known)
            self.assertEqual(ghost, [], f"{label} 里有不存在的事件：{ghost}")

    def test_event_types_cover_the_frozen_table(self) -> None:
        """事件面靠反射；护栏把它钉成一个**下限清单**，少一个就是有人改名或漏 import。"""
        from app.services.gateway.service import event_types

        names = {cls.__name__ for cls in event_types()}
        expected = {
            "ServiceStateChangedEvent", "SettingsChangedEvent", "ProgressSavedEvent",
            "BookmarkChangedEvent", "DownloadProgressEvent", "SearchFinishedEvent",
            "ReadingSessionChangedEvent", "SourceChangedEvent", "CheckFinishedEvent",
            "RepairReportReadyEvent", "RepairProgressEvent", "ImportFinishedEvent",
            "ImportProgressEvent", "ReadAloudStateEvent", "MediaPlayStateEvent",
            "RssRefreshedEvent", "RssRefreshProgressEvent", "ReplaceRulesChangedEvent",
            "BackupFinishedEvent", "BackupProgressEvent", "UpdateAvailableEvent",
            "ExportProgressEvent",
        }
        self.assertEqual(sorted(expected - names), [],
                         "这些事件不在反射结果里（改名？模块没被 import？）")

    def test_hello_capabilities_are_backed_by_routes(self) -> None:
        """能力位不是装饰：每报一条能力，路由表里就得真有一条。"""
        from app.services.gateway.service import (
            CAPABILITIES, GatewayService, WEBVIEW_TIMEOUT)

        self.assertEqual(sorted(CAPABILITIES),
                         sorted(["rpc", "events", "media.covers", "job.actions",
                                 "webview.reverse"]))
        self.assertTrue(any(m.mode == "job" for m in METHODS),
                        "报了 job.actions 能力却没有任何 job 动作")
        # webview.reverse 的落点是一条**反向**通路：/ws 上发 request 帧、/rpc 上收答复。
        # 两侧都得真在，缺一条就是"引擎发出去没人接、接了没人解"。
        self.assertIn("engine.webContentReady", BY_ACTION,
                      "报了 webview.reverse 能力却没有回话动作")
        self.assertTrue(hasattr(GatewayService, "request_web_content"),
                        "回话动作有了，但服务端那头发 request 帧的一面不在")
        self.assertGreater(WEBVIEW_TIMEOUT, 0,
                           "反向等待没有预算就是无限等；这条能力就成了死锁")


class JsonSchemaSubsetTests(unittest.TestCase):
    """校验器只支持那十个关键词——支持了的就得真生效。"""

    def test_types_enums_and_bounds(self) -> None:
        schema.validate({"a": 1}, {"type": "object",
                                   "properties": {"a": {"type": "integer",
                                                        "minimum": 1}}})
        with self.assertRaises(schema.SchemaError):
            schema.validate({"a": True}, {"properties": {"a": {"type": "integer"}}},
                            "正对照：bool 不算 integer")
        with self.assertRaises(schema.SchemaError):
            schema.validate({"a": 0}, {"properties": {"a": {"type": "integer",
                                                            "minimum": 1}}})
        with self.assertRaises(schema.SchemaError):
            schema.validate({"a": "x"}, {"properties": {"a": {"enum": ["y"]}}})
        with self.assertRaises(schema.SchemaError):
            schema.validate({"a": [1, "two"]},
                            {"properties": {"a": {"type": "array",
                                                  "items": {"type": "integer"}}}})
        with self.assertRaises(schema.SchemaError):
            schema.validate({"a": 1, "b": 2}, {"type": "object", "properties": {"a": {}},
                                               "additionalProperties": False})
        with self.assertRaises(schema.SchemaError):
            schema.validate({}, {"type": "object", "required": ["a"]})

    def test_null_arms_of_a_union(self) -> None:
        schema.validate({"groups": None},
                        {"properties": {"groups": {"type": ["array", "null"]}}})
        schema.validate({"groups": ["a"]},
                        {"properties": {"groups": {"type": ["array", "null"]}}})
        with self.assertRaises(schema.SchemaError):
            schema.validate({"groups": 1},
                            {"properties": {"groups": {"type": ["array", "null"]}}})

    def test_unknown_type_name_is_loud(self) -> None:
        with self.assertRaises(ValueError):
            schema.validate(1, {"type": "int"})


if __name__ == "__main__":
    unittest.main()
