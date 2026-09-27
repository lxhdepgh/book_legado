"""OkHttp 4.11 PublicSuffixDatabase compatibility for Legado CookieStore."""
from __future__ import annotations

import gzip
import ipaddress
import struct
from functools import lru_cache
from pathlib import Path


# [M1-任务1] 模块从 app/services/ 搬到 app/services/engine/，层级深了一层：
# 原 parents[1] 指向 app/，现 parents[2] 才指向 app/，故 resources 路径上移一级。
_DATA = Path(__file__).resolve().parents[2] / "resources" / "publicsuffixes.gz"


@lru_cache(maxsize=1)
def _rules() -> tuple[set[str], set[str]]:
    raw = gzip.open(_DATA, "rb").read()
    rule_size = struct.unpack(">I", raw[:4])[0]
    rule_end = 4 + rule_size
    exception_size = struct.unpack(">I", raw[rule_end:rule_end + 4])[0]
    rules = set(raw[4:rule_end].decode("utf-8").splitlines())
    exceptions = set(raw[rule_end + 4:rule_end + 4 + exception_size]
                     .decode("utf-8").splitlines())
    return rules, exceptions


def _unicode_labels(host: str) -> list[str]:
    labels = []
    for label in host.split("."):
        try:
            labels.append(label.encode("ascii").decode("idna"))
        except (UnicodeError, UnicodeDecodeError):
            labels.append(label)
    return labels


@lru_cache(maxsize=4096)
def effective_tld_plus_one(host: str) -> str:
    """Match PublicSuffixDatabase.getEffectiveTldPlusOne or return the host."""
    original = str(host or "").strip().strip(".").lower()
    if not original:
        return original
    try:
        ipaddress.ip_address(original)
        return original
    except ValueError:
        pass
    original_labels = original.split(".")
    labels = _unicode_labels(original)
    rules, exceptions = _rules()

    exception = next((".".join(labels[index:]) for index in range(len(labels))
                      if ".".join(labels[index:]) in exceptions), None)
    if exception is not None:
        offset = len(labels) - len(exception.split("."))
        return ".".join(original_labels[offset:])

    exact = ""
    wildcard = ""
    for index in range(len(labels)):
        candidate = ".".join(labels[index:])
        if candidate in rules and candidate.count(".") >= exact.count("."):
            exact = candidate
        if index + 1 < len(labels):
            candidate = "*." + ".".join(labels[index + 1:])
            if candidate in rules and candidate.count(".") >= wildcard.count("."):
                wildcard = candidate
    rule = exact if len(exact.split(".")) >= len(wildcard.split(".")) else wildcard
    rule_count = len(rule.split(".")) if rule else 1  # prevailing '*' rule
    if len(labels) == rule_count:
        return original
    offset = len(labels) - rule_count - 1
    return ".".join(original_labels[offset:])
