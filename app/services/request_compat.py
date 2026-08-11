"""Legado-compatible request parameter encoding helpers."""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urlsplit, urlunsplit


_ESCAPE_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"
)


def java_value_string(value) -> str:
    """Java/Kotlin ``Any?.toString`` for Gson-produced option values."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{java_value_string(key)}={java_value_string(item)}"
            for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(java_value_string(item) for item in value) + "]"
    return str(value)


def normalize_url_option(config) -> dict:
    """Apply AnalyzeUrl.UrlOption getters and its all-or-nothing Gson parse."""
    if not isinstance(config, dict):
        return {}
    result = dict(config)
    method = java_value_string(result.get("method")) if "method" in result else ""
    result["method"] = "POST" if method.lower() == "post" else "GET"
    for name in ("retry", "serverID"):
        if name not in result or result[name] is None or result[name] == "":
            continue
        value = result[name]
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            integer = int(number)
            if number != integer:
                raise ValueError
            if name == "retry" and not (-2147483648 <= integer <= 2147483647):
                raise ValueError
            result[name] = integer
        except (TypeError, ValueError, OverflowError):
            # GSON.fromJsonObject(...).getOrNull() drops the complete option.
            return {}
    headers = result.get("headers")
    if isinstance(headers, str):
        try:
            parsed_headers = json.loads(headers)
            headers = parsed_headers if isinstance(parsed_headers, dict) else None
        except json.JSONDecodeError:
            headers = None
    if isinstance(headers, dict):
        result["headers"] = {
            java_value_string(key): java_value_string(value)
            for key, value in headers.items()
        }
    else:
        result["headers"] = None
    body = result.get("body")
    if isinstance(body, (dict, list)):
        result["body"] = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"))
    elif body is not None:
        result["body"] = java_value_string(body)
    return result


def legado_escape(value: str) -> str:
    """Match JavaScript/Legado ``escape`` including UTF-16 ``%uXXXX`` output."""
    output: list[str] = []
    for char in str(value):
        code = ord(char)
        if char in _ESCAPE_SAFE:
            output.append(char)
        elif code < 256:
            output.append(f"%{code:02X}")
        elif code <= 0xFFFF:
            output.append(f"%u{code:04X}")
        else:
            code -= 0x10000
            output.append(f"%u{0xD800 + (code >> 10):04X}")
            output.append(f"%u{0xDC00 + (code & 0x3FF):04X}")
    return "".join(output)


def escape_form(value: str) -> str:
    """Escape values, then apply OkHttp addEncoded's UTF-8 key encoding."""
    fields: list[str] = []
    for field in str(value).split("&"):
        if not field.strip():
            continue
        name, separator, item = field.partition("=")
        encoded_name = quote_plus(name, safe="%", encoding="utf-8")
        fields.append(encoded_name + "=" + legado_escape(item if separator else ""))
    return "&".join(fields)


def escape_url_query(url: str) -> str:
    parts = urlsplit(str(url))
    if not parts.query:
        return str(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       escape_form(parts.query), parts.fragment))


def encode_form_fields(value: str, charset: str = "utf-8") -> str:
    """Match AnalyzeUrl.analyzeFields/URLEncoder for ordinary form fields."""
    normalized = str(charset or "utf-8")
    fields: list[str] = []
    for field in str(value).split("&"):
        if not field.strip():
            continue
        name, separator, item = field.partition("=")
        if not separator:
            item = ""
        # Legado preserves values which already contain URL encoding.
        encoded = item if re.search(r"%[0-9a-fA-F]{2}", item) else quote_plus(
            item, safe="", encoding=normalized, errors="replace")
        encoded_name = quote_plus(name, safe="%", encoding="utf-8")
        fields.append(f"{encoded_name}={encoded}")
    return "&".join(fields)


def encode_url_query_fields(url: str, charset: str = "utf-8") -> str:
    parts = urlsplit(str(url))
    if not parts.query:
        return str(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       encode_form_fields(parts.query, charset), parts.fragment))
