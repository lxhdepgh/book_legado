"""Legado-compatible request parameter encoding helpers."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit, urlunsplit


_ESCAPE_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"
)


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
    """Escape each form/query name and value the way AnalyzeUrl does."""
    fields: list[str] = []
    for field in str(value).split("&"):
        name, separator, item = field.partition("=")
        encoded = legado_escape(name)
        if separator:
            encoded += "=" + legado_escape(item)
        fields.append(encoded)
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
    if normalized.lower() in {"gbk", "gb2312", "gb_2312-80"}:
        normalized = "gb18030"
    fields: list[str] = []
    for field in str(value).split("&"):
        name, separator, item = field.partition("=")
        if not separator:
            fields.append(name)
            continue
        # Legado preserves values which already contain URL encoding.
        encoded = item if re.search(r"%[0-9a-fA-F]{2}", item) else quote_plus(
            item, safe="", encoding=normalized, errors="replace")
        fields.append(f"{name}={encoded}")
    return "&".join(fields)


def encode_url_query_fields(url: str, charset: str = "utf-8") -> str:
    parts = urlsplit(str(url))
    if not parts.query:
        return str(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       encode_form_fields(parts.query, charset), parts.fragment))
