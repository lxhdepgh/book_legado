"""信封与错误码：一次 RPC 往返的固定形状（设计 §4.1）。

    请求  {"v":1,"id":"c-17","action":"bookshelf.list","seq":17,"payload":{...}}
    响应  {"v":1,"id":"c-17","ok":true,"data":[...],"error":null,"code":null}

规矩只有一条：**任何内部异常也必须回这个形状**。服务端入口
``try/except BaseException`` 兜底（真果鉴 `native/core/app_runtime.go:289-293` 那一手），
因为"信封不合法"意味着客户端解析器当场炸，用户看到的是"点了没反应"，
比返回一个错误难查十倍。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: 协议版本：前后端错配由 :data:`capabilities` 握手拦下，不放行到业务调用。
PROTOCOL_VERSION = 1

#: 请求体上限（1 MiB）。超过直接拒，不做"先收完再看"——收完就已经把内存赌出去了。
MAX_BODY_BYTES = 1 << 20

#: 错误码（冻结词汇表）。客户端按 ``code`` 分支，绝不按 ``error`` 文案分支。
CODE_UNAVAILABLE = "unavailable"    # 目标服务没注册/没起来
CODE_TIMEOUT = "timeout"            # 超过该 action 的服务端预算
CODE_CANCELLED = "cancelled"        # 被同 action 的更新 seq 顶掉
CODE_BAD_REQUEST = "bad_request"    # 信封本身不合法（缺 id/action、未知 action）
CODE_ENGINE = "engine"              # 业务方法自己抛的异常
CODE_SCHEMA = "schema"              # payload 没过入参 schema

ERROR_CODES: tuple[str, ...] = (
    CODE_UNAVAILABLE, CODE_TIMEOUT, CODE_CANCELLED,
    CODE_BAD_REQUEST, CODE_ENGINE, CODE_SCHEMA,
)


def ok_envelope(request_id: Any, data: Any) -> dict:
    """成功信封。"""
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True,
            "data": data, "error": None, "code": None}


def err_envelope(request_id: Any, code: str, message: str) -> dict:
    """失败信封：``code`` 必须取自 :data:`ERROR_CODES`。

    传进来的 code 不在表里时**降级成 engine**而不是报错——这条是最后一道兜底，
    它自己不能再成为新的炸点。
    """
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False,
            "data": None, "error": str(message),
            "code": code if code in ERROR_CODES else CODE_ENGINE}


def digest(payload: Any) -> str:
    """把任意 JSON 结构压成 12 位摘要，用于 ``schema_digest`` 握手。

    ``sort_keys`` + 固定分隔符：**同一组内容必须永远得到同一个串**，否则每次启动
    客户端都会当成"版本不一致"拒绝连接。
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
