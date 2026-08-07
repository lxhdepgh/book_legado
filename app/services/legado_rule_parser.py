"""Project-local parser for the commonly used Legado rule dialects."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from test_book_sources_async import jsonpath_get, simplify_legado_selector


class RuleParseError(RuntimeError):
    pass


def _to_element_tree(value: Any) -> ET.Element:
    if isinstance(value, ET.Element):
        return value
    if isinstance(value, BeautifulSoup):
        source = value
    elif isinstance(value, Tag):
        source = BeautifulSoup(str(value), "html.parser")
    else:
        source = BeautifulSoup(str(value), "html.parser")
    root = ET.Element("document")

    def append(parent: ET.Element, tag: Tag) -> None:
        attrs = {str(k): " ".join(v) if isinstance(v, list) else str(v)
                 for k, v in tag.attrs.items()}
        current = ET.SubElement(parent, tag.name.lower(), attrs)
        last = None
        for child in tag.children:
            if isinstance(child, Tag):
                append(current, child)
                last = current[-1]
            else:
                text = str(child)
                if last is None: current.text = (current.text or "") + text
                else: last.tail = (last.tail or "") + text

    for child in source.children:
        if isinstance(child, Tag): append(root, child)
    return root


def split_balanced(value: str, separator: str) -> list[str]:
    """Split a Legado expression without cutting quoted/bracketed fragments."""
    parts, start, quote = [], 0, ""
    square = round_ = curly = 0
    i = 0
    while i < len(value):
        char = value[i]
        if quote:
            if char == quote and (i == 0 or value[i - 1] != "\\"):
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "[": square += 1
        elif char == "]": square = max(0, square - 1)
        elif char == "(": round_ += 1
        elif char == ")": round_ = max(0, round_ - 1)
        elif char == "{": curly += 1
        elif char == "}": curly = max(0, curly - 1)
        elif not (square or round_ or curly) and value.startswith(separator, i):
            parts.append(value[start:i].strip())
            i += len(separator)
            start = i
            continue
        i += 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _path_steps(rule: str) -> list[tuple[str, str]]:
    rule = rule.strip()
    if rule.startswith("."):
        rule = rule[1:]
    result, quote, square, round_ = [], "", 0, 0
    i = 0
    while i < len(rule):
        if rule.startswith("//", i): axis, i = "//", i + 2
        elif rule.startswith("/", i): axis, i = "/", i + 1
        else: axis = "//" if not result else "/"
        start = i
        while i < len(rule):
            char = rule[i]
            if quote:
                if char == quote and rule[i - 1] != "\\": quote = ""
            elif char in "'\"": quote = char
            elif char == "[": square += 1
            elif char == "]": square -= 1
            elif char == "(": round_ += 1
            elif char == ")": round_ -= 1
            elif char == "/" and not square and not round_: break
            i += 1
        step = rule[start:i].strip()
        if step: result.append((axis, step))
    return result


def _step_parts(step: str) -> tuple[str, list[str]]:
    tag = step.split("[", 1)[0].strip() or "*"
    predicates, quote, depth, start = [], "", 0, -1
    for i, char in enumerate(step[len(tag):], len(tag)):
        if quote:
            if char == quote and step[i - 1] != "\\": quote = ""
        elif char in "'\"": quote = char
        elif char == "[":
            if depth == 0: start = i + 1
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0 and start >= 0: predicates.append(step[start:i].strip())
    return tag, predicates


def _attr(node: Tag, name: str) -> str:
    value = node.get(name, "")
    return " ".join(value) if isinstance(value, list) else str(value)


def _selector_regex_filters(selector: str) -> tuple[str, list[tuple[object, str]]]:
    """Remove Jsoup regex pseudos and return filters for post-selection."""
    value = str(selector or "*")
    filters: list[tuple[bool, str]] = []
    compound = ":not(:has(:matches("
    while compound in value:
        start = value.find(compound)
        cursor, depth, escaped, inner_end = start + len(compound), 3, False, None
        while cursor < len(value):
            char = value[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 2 and inner_end is None:
                    inner_end = cursor
                if depth == 0:
                    break
            cursor += 1
        if depth or inner_end is None:
            break
        filters.append(("negative_descendant", value[start + len(compound):inner_end]))
        value = value[:start] + value[cursor + 1:]
    for token, own in ((":matchesOwn(", True), (":matches(", False)):
        search_from = 0
        while True:
            start = value.find(token, search_from)
            if start < 0:
                break
            i, depth, escaped, square = start + len(token), 1, False, False
            while i < len(value):
                char = value[i]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "[":
                    square = True
                elif char == "]":
                    square = False
                elif not square and char == "(":
                    depth += 1
                elif not square and char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth:
                break
            filters.append((own, value[start + len(token):i]))
            value = value[:start] + value[i + 1:]
            search_from = start
    value = re.sub(r":contains\(([^()]*)\)", r":-soup-contains(\1)", value)
    def contains_own(match):
        literal = match.group(1).strip().strip("'\"")
        filters.append((True, re.escape(literal)))
        return ""
    value = re.sub(r":containsOwn\(([^()]*)\)", contains_own, value)
    def eq(match):
        index = int(match.group(1))
        return ":last-child" if index == -1 else f":nth-child({index + 1})"
    value = re.sub(r":eq\(\s*(-?\d+)\s*\)", eq, value)
    value = re.sub(r":lt\(\s*(\d+)\s*\)",
                   lambda m: f":nth-child(-n+{int(m.group(1))})", value)
    value = re.sub(r":gt\(\s*(\d+)\s*\)",
                   lambda m: f":nth-child(n+{int(m.group(1)) + 2})", value)
    return value.strip() or "*", filters


def select_jsoup(context: Any, selector: str) -> list[Tag]:
    """Select with the Jsoup pseudos used by Legado book-source rules."""
    attribute_filters = []

    def attribute_regex(match):
        attribute_filters.append((match.group(1), match.group(2)))
        return f"[{match.group(1)}]"

    selector = re.sub(r"\[([\w:-]+)~=([^\]]+)\]", attribute_regex, str(selector))
    # Jsoup accepts unquoted attribute values containing ':' and spaces;
    # SoupSieve follows stricter CSS syntax, so quote them before compiling.
    selector = re.sub(
        r"\[([\w:-]+)([~|^$*]?=)([^\]\"']+)\]",
        lambda match: (f'[{match.group(1)}{match.group(2)}"'
                       f'{match.group(3).replace(chr(34), chr(92)+chr(34))}"]'),
        selector,
    )
    css, filters = _selector_regex_filters(selector)
    nodes = list(context.select(css))
    for attribute, pattern in attribute_filters:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise RuleParseError(f"无效的属性正则：{attribute}~={pattern}") from exc
        nodes = [node for node in nodes if regex.search(_attr(node, attribute))]
    for own, pattern in filters:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise RuleParseError(f"无效的 Jsoup 正则选择器：{pattern}") from exc
        if own == "negative_descendant":
            nodes = [node for node in nodes if not any(
                regex.search(descendant.get_text(" ", strip=True))
                for descendant in node.find_all(True))]
        elif own:
            nodes = [node for node in nodes if regex.search(" ".join(
                str(text) for text in node.find_all(string=True, recursive=False)))]
        else:
            nodes = [node for node in nodes if regex.search(node.get_text(" ", strip=True))]
    return nodes


def _apply_predicates(nodes: list[Tag], predicates: list[str]) -> list[Tag]:
    for predicate in predicates:
        p = predicate.strip()
        if p.isdigit():
            index = int(p) - 1
            nodes = [nodes[index]] if 0 <= index < len(nodes) else []
            continue
        if p == "last()":
            nodes = nodes[-1:] 
            continue
        match = re.fullmatch(r"position\(\)\s*=\s*(\d+)", p)
        if match:
            index = int(match.group(1)) - 1
            nodes = [nodes[index]] if 0 <= index < len(nodes) else []
            continue
        match = re.fullmatch(r"@([\w:-]+)\s*=\s*(['\"])(.*?)\2", p)
        if match:
            nodes = [n for n in nodes if _attr(n, match.group(1)) == match.group(3)]
            continue
        match = re.fullmatch(r"@([\w:-]+)", p)
        if match:
            nodes = [n for n in nodes if n.has_attr(match.group(1))]
            continue
        match = re.fullmatch(r"contains\(\s*@([\w:-]+)\s*,\s*(['\"])(.*?)\2\s*\)", p)
        if match:
            nodes = [n for n in nodes if match.group(3) in _attr(n, match.group(1))]
            continue
        match = re.fullmatch(r"contains\(\s*(?:text\(\)|\.)\s*,\s*(['\"])(.*?)\1\s*\)", p)
        if match:
            nodes = [n for n in nodes if match.group(2) in n.get_text(" ", strip=True)]
            continue
        match = re.fullmatch(r"text\(\)\s*=\s*(['\"])(.*?)\1", p)
        if match:
            nodes = [n for n in nodes if n.get_text("", strip=True) == match.group(2)]
            continue
        raise RuleParseError(f"暂不支持的 XPath 条件：[{p}]")
    return nodes


def xpath_values(root: Any, rule: str) -> list[Any]:
    # Legado delegates XPath to JXDocument. elementpath gives us the equivalent
    # XPath 1/2 implementation without a native extension, so Android builds stay portable.
    try:
        import elementpath
        tree = _to_element_tree(root)
        scalar_html = False
        if rule.endswith("/html()"):
            rule, scalar_html = rule[:-7], True
        elif rule.endswith("@html") and not rule.endswith("/@html"):
            rule, scalar_html = rule[:-5], True
        expression = "." + rule if rule.startswith("//") else rule
        values = list(elementpath.select(tree, expression))
        if scalar_html:
            return [ET.tostring(item, encoding="unicode") for item in values
                    if isinstance(item, ET.Element)]
        return values
    except ImportError:
        pass
    except Exception as exc:
        raise RuleParseError(f"XPath 解析失败：{rule}（{exc}）") from exc
    contexts = [root]
    steps = _path_steps(rule)
    scalar = None
    if steps and steps[-1][1] == "text()": scalar = "text"; steps.pop()
    elif steps and steps[-1][1].startswith("@"):
        scalar = steps[-1][1][1:]; steps.pop()
    for axis, raw_step in steps:
        tag, predicates = _step_parts(raw_step)
        next_contexts = []
        for context in contexts:
            if not hasattr(context, "find_all"): continue
            candidates = context.find_all(tag if tag != "*" else True, recursive=axis == "//")
            next_contexts.extend(_apply_predicates(list(candidates), predicates))
        contexts = next_contexts
    if scalar == "text": return [n.get_text(" ", strip=True) for n in contexts]
    if scalar: return [_attr(n, scalar) for n in contexts if n.has_attr(scalar)]
    return contexts


def _selection_indices(expression: str, length: int) -> list[int]:
    """Expand Legado's inclusive index, range, step and comma syntax."""
    if length <= 0:
        return []

    def normalized(value: int | None, default: int) -> int:
        if value is None:
            return default
        value = value + length if value < 0 else value
        return min(max(value, 0), length - 1)

    result = []
    for item in expression.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            if not re.fullmatch(r"-?\d+", item):
                continue
            raw_index = int(item)
            index = raw_index + length if raw_index < 0 else raw_index
            if 0 <= index < length and index not in result:
                result.append(index)
            continue
        parts = item.split(":")
        if len(parts) not in (2, 3) or any(
                value and not re.fullmatch(r"-?\d+", value.strip())
                for value in parts):
            continue
        start_value = int(parts[0]) if parts[0].strip() else None
        end_value = int(parts[1]) if parts[1].strip() else None
        start = normalized(start_value, 0)
        end = normalized(end_value, length - 1)
        raw_step = int(parts[2]) if len(parts) == 3 and parts[2].strip() else 1
        step = (raw_step if raw_step > 0 else
                raw_step + length if -raw_step < length else 1)
        if start == end or step >= length:
            indexes = [start]
        else:
            indexes = (range(start, end + 1, step) if end >= start else
                       range(start, end - 1, -step))
        for index in indexes:
            if index not in result:
                result.append(index)
    return result


def _jsoup_part(part: str) -> tuple[str, str, bool]:
    part = str(part).strip()
    part = re.sub(r"\[\^([^\]]+)\]", r"[class^=\1]", part)
    part = re.sub(r"\)(?=[A-Za-z][\w-]*(?:[.#\[]|@|$))", ") ", part)
    selection, exclude = "", False
    match = re.search(r"\[(!?)([-\d,:\s]+)\]$", part)
    if match:
        exclude = match.group(1) == "!"
        selection, part = match.group(2), part[:match.start()]
    else:
        match = re.search(r"\.?(!)(-?\d+(?::-?\d+)*)$", part)
        if match:
            exclude, selection, part = True, match.group(2), part[:match.start()]
        else:
            match = re.search(r"\.(-?\d+(?::-?\d+)*)$", part)
            if match:
                selection, part = match.group(1), part[:match.start()]
    part = re.sub(r"\.(\d+)(?=\s|[>+~]|$)",
                  lambda match: f":nth-of-type({int(match.group(1)) + 1})", part)
    part = re.sub(r"(?:\./|\.)$", "", part)
    if part.startswith("class."): selector = "." + part[6:].strip().replace(" ", ".")
    elif part.startswith("tag."): selector = part[4:]
    elif part.startswith("id."): selector = "#" + part[3:]
    elif part.startswith("text."):
        escaped = part[5:].replace("\\", "\\\\").replace('"', '\\"')
        selector = f'*:-soup-contains("{escaped}")'
    else: selector = simplify_legado_selector(part) or part
    return selector, selection, exclude


_VALUE_NAMES = {"text", "textnodes", "owntext", "html", "all"}
_HTML_TAG_NAMES = {
    "a", "abbr", "address", "article", "aside", "audio", "b", "blockquote",
    "body", "br", "button", "canvas", "caption", "code", "col", "dd",
    "details", "div", "dl", "dt", "em", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hr",
    "html", "i", "iframe", "img", "input", "label", "legend", "li", "link",
    "main", "meta", "nav", "ol", "option", "p", "picture", "pre", "script",
    "section", "select", "small", "source", "span", "strong", "style", "sub",
    "summary", "sup", "table", "tbody", "td", "textarea", "tfoot", "th",
    "thead", "time", "title", "tr", "track", "u", "ul", "video",
}


def jsoup_values(root: Any, rule: str) -> list[Any]:
    if isinstance(root, ET.Element):
        root = BeautifulSoup(ET.tostring(root, encoding="unicode"), "html.parser")
    rule = re.sub(r"::text\s*$", "@text", str(rule), flags=re.I)
    regex_suffix = re.search(
        r"@([\w.:-]+):regex\(\s*(['\"])(.*?)\2\s*\)\s*$", rule)
    if regex_suffix:
        base_rule = rule[:regex_suffix.start()] + "@" + regex_suffix.group(1)
        values = jsoup_values(root, base_rule)
        try:
            pattern = re.compile(regex_suffix.group(3))
        except re.error as exc:
            raise RuleParseError(f"无效的字段正则：{regex_suffix.group(3)}") from exc
        extracted = []
        for value in values:
            text = str(value)
            match = pattern.fullmatch(text) or pattern.search(text)
            if match:
                extracted.append(match.group(1) if match.lastindex else match.group(0))
        return extracted
    force_css = rule.lower().startswith("@css:")
    if force_css:
        rule = re.sub(r":contains\(([^)]*)\)", r":-soup-contains(\1)",
                      rule[5:].strip())
    parts = split_balanced(rule, "@")
    attr = ""
    terminal = parts[-1].lower() if parts else ""
    if terminal in _VALUE_NAMES:
        attr = parts.pop()
    elif len(parts) > 1 and (
            terminal.startswith("attr.") or
            (terminal not in _HTML_TAG_NAMES and
             re.fullmatch(r"[\w:-]+", parts[-1]))):
        attr = parts.pop()
    if force_css:
        parts = ["@".join(parts)]
    contexts = [root]
    for part in parts:
        selector, selection, exclude = _jsoup_part(part)
        found = []
        for context in contexts:
            try:
                if selector == "children":
                    found.extend(context.find_all(True, recursive=False))
                else:
                    found.extend(select_jsoup(context, selector))
            except Exception as exc: raise RuleParseError(f"无法解析 JSoup 规则：{part}") from exc
        if selection:
            indices = _selection_indices(selection, len(found))
            if exclude:
                blocked = set(indices)
                found = [item for position, item in enumerate(found)
                         if position not in blocked]
            else:
                found = [found[position] for position in indices]
        contexts = found
    if not attr: return contexts
    lowered = attr.lower()
    if lowered == "text": return [n.get_text(" ", strip=True) for n in contexts]
    if lowered == "textnodes":
        return ["\n".join(str(text).strip() for text in
                           n.find_all(string=True, recursive=False)
                           if str(text).strip()) for n in contexts]
    if lowered == "owntext": return [" ".join(n.find_all(string=True, recursive=False)).strip() for n in contexts]
    if lowered == "html":
        rendered = []
        for node in contexts:
            clone = BeautifulSoup(str(node), "html.parser")
            for unwanted in clone.select("script, style"):
                unwanted.decompose()
            target = clone.find(True)
            rendered.append(str(target) if target is not None else "")
        combined = "\n".join(value for value in rendered if value)
        return [combined] if combined else []
    if lowered == "all":
        combined = "\n".join(str(node) for node in contexts)
        return [combined] if combined else []
    name = attr[5:] if lowered.startswith("attr.") else attr
    return [_attr(n, name) for n in contexts if n.has_attr(name)]


def json_values(root: Any, rule: str) -> list[Any]:
    if rule.lower().startswith("@json:"): rule = rule.split(":", 1)[1].strip()
    expression = rule.strip()
    expression = re.sub(r"\](?=[A-Za-z_$])", "].", expression)
    if expression and not expression.startswith(("$", "@")):
        expression = "$." + expression
    try:
        from jsonpath_ng.ext import parse
        values = [match.value for match in parse(expression or "$").find(root)]
        # Jayway accepts dot-wildcard on arrays (``$.items.*``), while
        # jsonpath-ng only returns array children for bracket wildcard.
        if not values and ".*" in expression:
            bracket_expression = re.sub(r"\.\*(?=\.|\[|$)", "[*]", expression)
            if bracket_expression != expression:
                values = [match.value for match in
                          parse(bracket_expression).find(root)]
    except Exception:
        values = jsonpath_get(root, rule)
    return values[0] if len(values) == 1 and isinstance(values[0], list) else values


def regex_values(root: Any, rule: str) -> list[list[str]]:
    """Return ``[group0, group1, ...]`` records for Legado ``:regex`` mode."""
    content = (root if isinstance(root, str) else
               json.dumps(root, ensure_ascii=False) if isinstance(root, (dict, list))
               else str(root))
    patterns = split_balanced(rule[1:] if rule.startswith(":") else rule, "&&")
    if not patterns:
        return []
    for pattern in patterns[:-1]:
        try:
            content = "".join(match.group(0) for match in re.finditer(pattern, content))
        except re.error as exc:
            raise RuleParseError(f"无效的正则列表规则：{pattern}") from exc
        if not content:
            return []
    try:
        return [[match.group(0), *(value or "" for value in match.groups())]
                for match in re.finditer(patterns[-1], content)]
    except re.error as exc:
        raise RuleParseError(f"无效的正则列表规则：{patterns[-1]}") from exc


def evaluate(root: Any, rule: str, kind: str) -> list[Any]:
    """Evaluate Legado || / && / %% combinations with the matching sub-engine."""
    rule = (rule or "").strip()
    if not rule: return []
    if rule.startswith(":"):
        return regex_values(root, rule)
    # Match Legado's operator precedence: concatenation first, then zip, with
    # fallback evaluated inside each branch.
    for separator in ("&&", "%%", "||"):
        parts = split_balanced(rule, separator)
        if len(parts) <= 1: continue
        # Legado's fallback operator is lazy: once a branch yields values the
        # remaining selectors must not run (they may target a different payload).
        if separator == "||":
            for part in parts:
                group = evaluate(root, part, kind)
                if group:
                    return group
            return []
        groups = [evaluate(root, part, kind) for part in parts]
        if separator == "&&": return [item for group in groups for item in group]
        return [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
    if kind == "json": return json_values(root, rule)
    if rule.startswith(("/", "./", "@XPath:", "@xpath:")):
        if rule.lower().startswith("@xpath:"): rule = rule.split(":", 1)[1].strip()
        return xpath_values(root, rule)
    return jsoup_values(root, rule)
