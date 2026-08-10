"""Small compatibility layer for Java Pattern/Matcher syntax used by Legado."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


_PROPERTY_ALIASES = {
    "letter": "L", "uppercaseletter": "Lu", "lowercaseletter": "Ll",
    "titlecaseletter": "Lt", "modifierletter": "Lm", "otherletter": "Lo",
    "mark": "M", "number": "N", "decimaldigitnumber": "Nd",
    "punctuation": "P", "symbol": "S", "separator": "Z",
    "spaceseparator": "Zs", "control": "Cc", "format": "Cf",
}


def _range_escape(start: int, end: int) -> str:
    def escaped(value: int) -> str:
        return (f"\\u{value:04X}" if value <= 0xFFFF else
                f"\\U{value:08X}")
    return escaped(start) if start == end else escaped(start) + "-" + escaped(end)


@lru_cache(maxsize=64)
def _unicode_property_body(name: str) -> str:
    normalized = name.replace("_", "").replace("-", "").lower()
    if normalized.startswith("is"):
        normalized = normalized[2:]
    if normalized in {"han", "cjk", "ideographic"}:
        return (r"\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
                r"\U00020000-\U0002FA1F\U00030000-\U000323AF")
    category = _PROPERTY_ALIASES.get(normalized, name)
    if len(category) not in {1, 2} or not category[0].isalpha():
        raise re.error(f"unsupported Java Unicode property: {name}")
    ranges = []
    start = previous = None
    for codepoint in range(0x110000):
        current = unicodedata.category(chr(codepoint))
        matched = current == category if len(category) == 2 else current.startswith(category)
        if matched:
            if start is None:
                start = previous = codepoint
            elif codepoint == previous + 1:
                previous = codepoint
            else:
                ranges.append(_range_escape(start, previous))
                start = previous = codepoint
    if start is not None:
        ranges.append(_range_escape(start, previous))
    return "".join(ranges)


def _translate_unicode_properties(value: str) -> str:
    def replace(match: re.Match) -> str:
        body = _unicode_property_body(match.group(2))
        return f"[^{body}]" if match.group(1) == "P" else f"[{body}]"
    return re.sub(r"\\([pP])\{([^{}]+)\}", replace, value)


@lru_cache(maxsize=32)
def _intersection_body(left: str, right: str, negate_right: bool) -> str:
    def operand(value: str) -> re.Pattern:
        translated = _translate_unicode_properties(value)
        expression = translated if translated.startswith("[") else f"[{translated}]"
        return re.compile(expression)
    left_pattern, right_pattern = operand(left), operand(right)
    ranges = []
    start = previous = None
    for codepoint in range(0x110000):
        char = chr(codepoint)
        matches = bool(left_pattern.fullmatch(char))
        right_matches = bool(right_pattern.fullmatch(char))
        matches = matches and (not right_matches if negate_right else right_matches)
        if matches:
            if start is None:
                start = previous = codepoint
            elif codepoint == previous + 1:
                previous = codepoint
            else:
                ranges.append(_range_escape(start, previous))
                start = previous = codepoint
    if start is not None:
        ranges.append(_range_escape(start, previous))
    return "".join(ranges)


def _translate_class_intersections(value: str) -> str:
    pattern = re.compile(r"\[([^\[\]]+?)&&\[?(\^?)([^\[\]]+?)\]?\]")
    previous = None
    while value != previous:
        previous = value
        value = pattern.sub(
            lambda match: "[" + _intersection_body(
                match.group(1), match.group(3), match.group(2) == "^") + "]",
            value)
    return value


def _translate_canonical_equivalence(value: str) -> str:
    """Expand literal runs to the NFC/NFD forms accepted by CANON_EQ.

    Java applies canonical equivalence to literal pattern text, not to regex
    operators or character classes.  Keeping the expansion at literal-run
    granularity also handles both a precomposed pattern (``å``) and an
    explicitly decomposed one (``a`` + combining ring).
    """
    result = []
    index = 0
    operators = set(r".^$*+?{}[]\|()")
    while index < len(value):
        char = value[index]
        if char == "\\":
            result.append(value[index:index + 2])
            index += 2
            continue
        if char in operators:
            result.append(char)
            index += 1
            continue
        end = index + 1
        while end < len(value) and value[end] not in operators and value[end] != "\\":
            end += 1
        literal = value[index:end]
        nfc = unicodedata.normalize("NFC", literal)
        nfd = unicodedata.normalize("NFD", literal)
        if nfc != nfd:
            alternatives = list(dict.fromkeys((literal, nfc, nfd)))
            result.append("(?:" + "|".join(re.escape(item) for item in alternatives) + ")")
        else:
            result.append(literal)
        index = end
    return "".join(result)


def translate_pattern(pattern: str) -> str:
    value = str(pattern or "")
    canonical_equivalence = bool(re.search(
        r"\(\?[idmsuxU]*c[idmsuxUc]*(?:\)|:)", value))
    quoted = []

    def quote_literal(match: re.Match) -> str:
        quoted.append(re.escape(match.group(1)))
        return f"\x00JAVA_QUOTED_{len(quoted) - 1}\x00"

    value = re.sub(r"\\Q([\s\S]*?)(?:\\E|$)", quote_literal, value)
    value = value.replace(r"\R", r"(?:\r\n|[\n\v\f\r\x85\u2028\u2029])")
    value = value.replace(r"\z", r"\Z")
    # compile_pattern has no matcher state; standalone compilation treats \G
    # as the beginning. finditer/sub below preserve the repeated-match state.
    value = value.replace(r"\G", r"\A")
    value = value.replace(r"\h", r"[ \t\xA0\u1680\u180E\u2000-\u200A\u202F\u205F\u3000]")
    value = value.replace(r"\H", r"[^ \t\xA0\u1680\u180E\u2000-\u200A\u202F\u205F\u3000]")
    value = value.replace(r"\v", r"[\n\x0B\f\r\x85\u2028\u2029]")
    value = value.replace(r"\V", r"[^\n\x0B\f\r\x85\u2028\u2029]")
    value = _translate_class_intersections(value)
    value = _translate_unicode_properties(value)
    value = re.sub(r"\\k<([A-Za-z][A-Za-z0-9_]*)>", r"(?P=\1)", value)
    value = re.sub(r"\(\?<([A-Za-z][A-Za-z0-9_]*)>", r"(?P<\1>", value)
    def inline_flags(match: re.Match) -> str:
        enabled, disabled = match.group(1), match.group(2) or ""
        scoped = match.group(3) == ":"
        keep_enabled = "".join(flag for flag in enabled if flag in "imsx")
        keep_disabled = "".join(flag for flag in disabled if flag in "imsx")
        flags = keep_enabled + ("-" + keep_disabled if keep_disabled else "")
        if scoped:
            return f"(?{flags}:" if flags else "(?:"
        return f"(?{flags})" if flags else ""

    # d (UNIX_LINES), u (UNICODE_CASE), and U (UNICODE_CHARACTER_CLASS)
    # correspond to Python's default Unicode/string line behavior.
    value = re.sub(r"\(\?([idmsuxUc]+)(?:-([idmsuxUc]+))?([:)])",
                   inline_flags, value)
    if canonical_equivalence:
        value = _translate_canonical_equivalence(value)
    for index, literal in enumerate(quoted):
        value = value.replace(f"\x00JAVA_QUOTED_{index}\x00", literal)
    return value


def translate_replacement(value: str) -> str:
    source = str(value or "")
    result = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            literal = source[index + 1]
            result.append("\\\\" if literal == "\\" else literal)
            index += 2
            continue
        if char == "$" and index + 1 < len(source):
            if source[index + 1] == "{":
                end = source.find("}", index + 2)
                name = source[index + 2:end] if end >= 0 else ""
                if end >= 0 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                    result.append(f"\\g<{name}>")
                    index = end + 1
                    continue
            match = re.match(r"\d+", source[index + 1:])
            if match:
                result.append(f"\\g<{match.group(0)}>")
                index += len(match.group(0)) + 1
                continue
        result.append("\\\\" if char == "\\" else char)
        index += 1
    return "".join(result)


def compile_pattern(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(translate_pattern(pattern), flags)


def search(pattern: str, value: str, flags: int = 0):
    if r"\G" in str(pattern):
        return compile_pattern(str(pattern).replace(r"\G", ""), flags).match(value)
    return compile_pattern(pattern, flags).search(value)


def finditer(pattern: str, value: str, flags: int = 0):
    if r"\G" in str(pattern):
        compiled = compile_pattern(str(pattern).replace(r"\G", ""), flags)
        position = 0
        while position <= len(value):
            match = compiled.match(value, position)
            if match is None:
                break
            yield match
            if match.end() == position:
                position += 1
            else:
                position = match.end()
        return
    yield from compile_pattern(pattern, flags).finditer(value)


def sub(pattern: str, replacement: str, value: str, count: int = 0,
        flags: int = 0) -> str:
    if r"\G" in str(pattern):
        rendered = translate_replacement(replacement)
        pieces = []
        position = 0
        replaced = 0
        for match in finditer(pattern, value, flags):
            if count and replaced >= count:
                break
            pieces.append(value[position:match.start()])
            pieces.append(match.expand(rendered))
            position = match.end()
            replaced += 1
        pieces.append(value[position:])
        return "".join(pieces)
    return compile_pattern(pattern, flags).sub(
        translate_replacement(replacement), value, count=count)
