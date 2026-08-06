"""Project-local parser for the commonly used Legado rule dialects."""
from __future__ import annotations

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


def _jsoup_part(part: str) -> tuple[str, int | None]:
    index = None
    match = re.search(r"\.(-?\d+)$", part)
    if match:
        index, part = int(match.group(1)), part[:match.start()]
    if part.startswith("class."): selector = "." + part[6:].replace(" ", ".")
    elif part.startswith("tag."): selector = part[4:]
    elif part.startswith("id."): selector = "#" + part[3:]
    else: selector = simplify_legado_selector(part) or part
    return selector, index


_VALUE_NAMES = {"text", "textnodes", "owntext", "html", "all"}


def jsoup_values(root: Any, rule: str) -> list[Any]:
    if isinstance(root, ET.Element):
        root = BeautifulSoup(ET.tostring(root, encoding="unicode"), "html.parser")
    force_css = rule.lower().startswith("@css:")
    if force_css: rule = rule[5:].strip()
    parts = [rule] if force_css else split_balanced(rule, "@")
    attr = ""
    if len(parts) > 1 and (parts[-1].lower() in _VALUE_NAMES or
                           re.fullmatch(r"(?:attr\.)?[\w:-]+", parts[-1])):
        attr = parts.pop()
    contexts = [root]
    for part in parts:
        selector, index = _jsoup_part(part)
        found = []
        for context in contexts:
            try: found.extend(context.select(selector))
            except Exception as exc: raise RuleParseError(f"无法解析 JSoup 规则：{part}") from exc
        if index is not None:
            try: found = [found[index]]
            except IndexError: found = []
        contexts = found
    if not attr: return contexts
    lowered = attr.lower()
    if lowered == "text": return [n.get_text(" ", strip=True) for n in contexts]
    if lowered == "textnodes": return [n.get_text("\n", strip=True) for n in contexts]
    if lowered == "owntext": return [" ".join(n.find_all(string=True, recursive=False)).strip() for n in contexts]
    if lowered == "html": return [n.decode_contents() for n in contexts]
    if lowered == "all": return [str(n) for n in contexts]
    name = attr[5:] if lowered.startswith("attr.") else attr
    return [_attr(n, name) for n in contexts if n.has_attr(name)]


def json_values(root: Any, rule: str) -> list[Any]:
    if rule.lower().startswith("@json:"): rule = rule.split(":", 1)[1].strip()
    values = jsonpath_get(root, rule)
    return values[0] if len(values) == 1 and isinstance(values[0], list) else values


def evaluate(root: Any, rule: str, kind: str) -> list[Any]:
    """Evaluate Legado || / && / %% combinations with the matching sub-engine."""
    rule = (rule or "").strip()
    if not rule: return []
    for separator in ("||", "&&", "%%"):
        parts = split_balanced(rule, separator)
        if len(parts) <= 1: continue
        groups = [evaluate(root, part, kind) for part in parts]
        if separator == "||": return next((group for group in groups if group), [])
        if separator == "&&": return [item for group in groups for item in group]
        return [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
    if kind == "json": return json_values(root, rule)
    if rule.startswith(("/", "./", "@XPath:", "@xpath:")):
        if rule.lower().startswith("@xpath:"): rule = rule.split(":", 1)[1].strip()
        return xpath_values(root, rule)
    return jsoup_values(root, rule)
