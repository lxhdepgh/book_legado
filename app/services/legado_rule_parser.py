"""Project-local parser for the commonly used Legado rule dialects."""
from __future__ import annotations

import json
import html as html_lib
import math
import ast
import re
from decimal import Decimal
import xml.etree.ElementTree as ET
from xml.dom import Node, minidom
from datetime import datetime
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from bs4 import (BeautifulSoup, CData, Comment, Doctype, NavigableString,
                 ProcessingInstruction, Tag)
from soupsieve.util import SelectorSyntaxError
import soupsieve

from test_book_sources_async import jsonpath_get
from app.services.java_regex import (compile_pattern, finditer as java_finditer,
                                     search as java_search)


class JavaKeySet(list):
    """Observable equivalent of LinkedHashMap.LinkedKeySet."""


class JavaArrayNull:
    """A null item rendered by Kotlin Iterable.joinToString as `null`."""


def _json_smart_float(value: str):
    decimal = Decimal(value)
    floating = float(value)
    return floating if Decimal(str(floating)) == decimal else decimal


def parse_json_smart(value: str):
    """Use json-smart's lossless numeric split for JSON rule input."""
    return json.loads(value, parse_int=int, parse_float=_json_smart_float)


class RuleParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class JavaDateValue:
    """Observable java.util.Date value returned by JsoupXpath format-date."""
    value: datetime

    def __str__(self) -> str:
        local = self.value.astimezone()
        offset = local.utcoffset()
        seconds = int(offset.total_seconds()) if offset is not None else 0
        zones = {0: "UTC", 8 * 3600: "CST", -5 * 3600: "EST",
                 -4 * 3600: "EDT", -6 * 3600: "CST", -7 * 3600: "MST",
                 -8 * 3600: "PST"}
        zone = zones.get(seconds, local.tzname() or "GMT")
        return local.strftime(f"%a %b %d %H:%M:%S {zone} %Y")


def _xpath_external_element(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    for attribute in ("elem", "obj"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, ET.Element):
            return candidate
    return value if isinstance(value, ET.Element) else None


def _xpath_external_text(value) -> str:
    element = _xpath_external_element(value)
    if element is None:
        return "" if value is None else str(value)
    tag = parse_markup(ET.tostring(element, encoding="unicode")).find(True)
    return _jsoup_text(tag) if tag is not None else ""


def _xpath_external_num(value):
    match = re.search(r"\d*\.?\d+", _xpath_external_text(value))
    if not match:
        return []
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _xpath_external_matches(value, pattern) -> bool:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(pattern, (list, tuple)):
        pattern = pattern[0] if pattern else ""
    value = getattr(value, "value", value)
    pattern = getattr(pattern, "value", pattern)
    try:
        return compile_pattern(str(pattern)).fullmatch(str(value)) is not None
    except re.error as exc:
        raise RuleParseError(f"无效的 JsoupXpath 正则：{pattern}") from exc


@lru_cache(maxsize=1)
def _legado_xpath_parser():
    from elementpath import XPath2Parser

    def parser_factory(namespaces=None, **kwargs):
        kwargs.setdefault("compatibility_mode", True)
        parser = XPath2Parser(namespaces, **kwargs)
        parser.external_function(_xpath_external_num, name="jx-num")
        parser.external_function(_xpath_external_text, name="jx-all-text")
        parser.external_function(_xpath_external_matches, name="jx-matches")
        return parser

    return parser_factory


def parse_markup(value: Any, content_type: str = "") -> BeautifulSoup:
    """Parse response markup with AnalyzeByXPath's XML declaration rule."""
    text = "" if value is None else str(value)
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    is_xml = (text.lstrip().lower().startswith("<?xml") or
              media_type in {"application/xml", "text/xml",
                             "application/xhtml+xml", "application/rss+xml",
                             "application/atom+xml"} or media_type.endswith("+xml"))
    def preserve_self_closing(document: BeautifulSoup) -> BeautifulSoup:
        occurrences: dict[str, list[bool]] = {}
        for match in re.finditer(
                r"<\s*([A-Za-z_][\w:.-]*)(?:\s[^<>]*?)?\s*(/?)>", text):
            name = match.group(1).casefold()
            occurrences.setdefault(name, []).append(bool(match.group(2)))
        document._legado_self_closing_names = {
            name for name, flags in occurrences.items() if any(flags)}
        positions: dict[str, int] = {}
        for node in document.find_all(True):
            name = str(node.name).casefold()
            position = positions.get(name, 0)
            flags = occurrences.get(name, ())
            if position < len(flags) and flags[position]:
                node._legado_self_closing = True
            positions[name] = position + 1
        return document

    if not is_xml:
        # Jsoup 1.16.2 uses the HTML5 tree builder: implied html/head/body and
        # tbody nodes, plus p/li/td adoption and auto-closing, are observable
        # to selectors. html5lib is the closest Python tree builder and shares
        # those structural rules.
        # html5lib ignores the XML slash on unknown HTML-namespace tags and
        # would let them swallow following siblings. Jsoup records the slash
        # as Tag.selfClosing and closes that node immediately.
        parser_text = re.sub(
            r"<\s*([A-Za-z_][\w:.-]*)([^<>]*?)/\s*>",
            lambda match: (match.group(0) if match.group(1).casefold() in
                           _JSOUP_EMPTY_TAGS else
                           f"<{match.group(1)}{match.group(2)}></{match.group(1)}>") ,
            text,
        )
        return preserve_self_closing(BeautifulSoup(parser_text, "html5lib"))
    try:
        xml_root = minidom.parseString(text).documentElement
    except Exception:
        # Jsoup's XML tree builder is deliberately tolerant: an end tag pops
        # through a mismatched stack and EOF closes any remaining elements.
        # Keep this small recovery parser case-sensitive and XML-oriented;
        # falling into an HTML parser changes namespaces, PI and whitespace.
        document = BeautifulSoup("", "html.parser")
        stack: list[Tag | BeautifulSoup] = [document]
        tokens = re.finditer(
            r"<!--[\s\S]*?(?:-->|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|"
            r"<\?[\s\S]*?(?:\?>|$)|<!DOCTYPE(?:\"[^\"]*\"|'[^']*'|[^>])*>|"
            r"</?(?:\"[^\"]*\"|'[^']*'|[^>])*>|[^<]+|<",
            text,
            re.I,
        )
        for match in tokens:
            token = match.group(0)
            if token.startswith("<?"):
                body = token[2:-2 if token.endswith("?>") else None].strip()
                if not body.lower().startswith("xml"):
                    stack[-1].append(ProcessingInstruction(body))
                continue
            if token.startswith("<!--"):
                body = token[4:-3 if token.endswith("-->") else None]
                stack[-1].append(Comment(body))
                continue
            if token.startswith("<![CDATA["):
                body = token[9:-3 if token.endswith("]]>") else None]
                stack[-1].append(CData(body))
                continue
            if token[:9].upper() == "<!DOCTYPE":
                stack[-1].append(Doctype(token[9:-1].strip()))
                continue
            if token.startswith("</"):
                name = token[2:-1].strip().split(None, 1)[0]
                for index in range(len(stack) - 1,
                                   max(0, len(stack) - 257), -1):
                    if isinstance(stack[index], Tag) and stack[index].name == name:
                        del stack[index:]
                        break
                continue
            if token.startswith("<") and token.endswith(">"):
                body = token[1:-1].strip()
                self_closing = body.endswith("/")
                if self_closing:
                    body = body[:-1].rstrip()
                name_match = re.match(r"([^\s/>]+)", body)
                if not name_match:
                    stack[-1].append(NavigableString(token))
                    continue
                name = name_match.group(1)
                node = document.new_tag(name)
                attributes = body[name_match.end():]
                for attr in re.finditer(
                        r"([^\s=/>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s/>]+)))?",
                        attributes):
                    value = next((item for item in attr.groups()[1:]
                                  if item is not None), "")
                    if attr.group(1) not in node.attrs:
                        node.attrs[attr.group(1)] = html_lib.unescape(value)
                stack[-1].append(node)
                if self_closing:
                    node._legado_self_closing = True
                else:
                    stack.append(node)
                continue
            stack[-1].append(NavigableString(html_lib.unescape(token)))
        document._legado_xml = True
        return preserve_self_closing(document)
    document = BeautifulSoup("", "html.parser")

    def append(parent: Tag | BeautifulSoup, element) -> None:
        node = document.new_tag(str(element.tagName))
        for index in range(element.attributes.length):
            attribute = element.attributes.item(index)
            node.attrs[str(attribute.name)] = str(attribute.value)
        parent.append(node)
        for child in element.childNodes:
            if child.nodeType == Node.ELEMENT_NODE:
                append(node, child)
            elif child.nodeType == Node.TEXT_NODE:
                node.append(NavigableString(child.data))
            elif child.nodeType == Node.CDATA_SECTION_NODE:
                node.append(CData(child.data))
            elif child.nodeType == Node.COMMENT_NODE:
                node.append(Comment(child.data))
            elif child.nodeType == Node.PROCESSING_INSTRUCTION_NODE:
                node.append(ProcessingInstruction(
                    f"{child.target} {child.data}".strip()))

    append(document, xml_root)
    document._legado_xml = True
    return preserve_self_closing(document)


def _to_element_tree(value: Any) -> ET.ElementTree:
    if isinstance(value, ET.Element):
        return ET.ElementTree(value)
    is_tag_context = isinstance(value, Tag) and not isinstance(value, BeautifulSoup)
    if isinstance(value, BeautifulSoup):
        source = value
    elif isinstance(value, Tag):
        source = parse_markup(value)
    else:
        source = parse_markup(value)
    root = ET.Element("document")

    def append(parent: ET.Element, tag: Tag,
               inherited_namespaces: dict[str, str] | None = None) -> None:
        namespaces = dict(inherited_namespaces or {})
        attrs = {}
        for key, attr_value in tag.attrs.items():
            name = str(key)
            value = " ".join(attr_value) if isinstance(attr_value, list) else str(attr_value)
            if name == "xmlns":
                namespaces[""] = value
                continue
            if name.startswith("xmlns:"):
                namespaces[name.split(":", 1)[1]] = value
                continue
            if ":" in name and name.split(":", 1)[0] in namespaces:
                prefix, local = name.split(":", 1)
                name = f"{{{namespaces[prefix]}}}{local}"
            attrs[name] = value
        element_name = tag.name
        if ":" in element_name and element_name.split(":", 1)[0] in namespaces:
            prefix, local = element_name.split(":", 1)
            element_name = f"{{{namespaces[prefix]}}}{local}"
        elif "" in namespaces:
            element_name = f"{{{namespaces['']}}}{element_name}"
        current = ET.SubElement(parent, element_name, attrs)
        last = None
        for child in tag.children:
            if isinstance(child, Tag):
                append(current, child, namespaces)
                last = current[-1]
            elif isinstance(child, Comment):
                current.append(ET.Comment(str(child)))
                last = current[-1]
            elif isinstance(child, ProcessingInstruction):
                target, _, data = str(child).partition(" ")
                current.append(ET.ProcessingInstruction(target, data))
                last = current[-1]
            else:
                text = str(child)
                if last is None: current.text = (current.text or "") + text
                else: last.tail = (last.tail or "") + text

    last = None
    for child in source.children:
        if isinstance(child, Tag):
            append(root, child)
            last = root[-1]
        elif not isinstance(child, (Comment, ProcessingInstruction)):
            text = str(child)
            if last is None:
                root.text = (root.text or "") + text
            else:
                last.tail = (last.tail or "") + text
    if not len(root):
        return ET.ElementTree(root)
    if getattr(source, "_legado_xml", False) or is_tag_context:
        return ET.ElementTree(root[0])
    html_node = next((child for child in root if child.tag == "html"), None)
    if html_node is None:
        html_node = ET.Element("html")
        ET.SubElement(html_node, "head")
        body = ET.SubElement(html_node, "body")
        body.text = root.text
        markup = str(source)
        destination = body
        if markup.endswith("</td>"):
            destination = ET.SubElement(ET.SubElement(body, "table"), "tr")
        elif markup.endswith(("</tr>", "</tbody>")):
            destination = ET.SubElement(body, "table")
        for child in list(root):
            root.remove(child)
            destination.append(child)
    return ET.ElementTree(html_node)


def _jx_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _recursive_xpath_text_values(root: Any, rule: str) -> list[str] | None:
    match = re.fullmatch(r"(.*?)//text\(\)(?:\[(.+)\])?", rule, flags=re.S)
    if not match:
        return None
    base_rule, predicate = match.group(1).rstrip("/"), (match.group(2) or "").strip()
    bases = (xpath_values(root, base_rule) if base_rule else
             [_to_element_tree(root).getroot()])
    index = int(predicate) if predicate.isdigit() else None
    excluded_class = None
    excluded = re.fullmatch(
        r"not\(ancestor::([\w*:-]+)\[@class=['\"]([^'\"]+)['\"]\]\)",
        predicate)
    if excluded:
        excluded_class = (excluded.group(1), excluded.group(2))
    result: list[str] = []

    def allowed(ancestors: tuple[ET.Element, ...]) -> bool:
        if excluded_class is None:
            return True
        tag_name, class_name = excluded_class
        return not any(
            (tag_name == "*" or ancestor.tag == tag_name) and
            class_name in str(ancestor.get("class", "")).split()
            for ancestor in ancestors)

    def walk(element: ET.Element,
             ancestors: tuple[ET.Element, ...] = ()) -> None:
        lineage = ancestors + (element,)
        direct_position = 0
        if element.text is not None:
            direct_position += 1
            if (index is None or direct_position == index) and allowed(lineage):
                result.append(_jx_text(element.text))
        for child in element:
            if isinstance(child.tag, str):
                walk(child, lineage)
            if child.tail is not None:
                direct_position += 1
                if ((index is None or direct_position == index) and
                        allowed(lineage)):
                    result.append(_jx_text(child.tail))

    for base in bases:
        if isinstance(base, ET.Element):
            walk(base)
    return result


def _xpath_text_axis_values(root: Any, rule: str) -> list[str] | None:
    match = re.fullmatch(
        r"(.*)/(following-sibling|preceding-sibling|following|preceding)::text\(\)",
        rule, flags=re.S)
    if not match:
        return None
    import elementpath
    tree = _to_element_tree(root)
    base_rule, axis = match.groups()
    expression = "." + base_rule if base_rule.startswith("//") else base_rule
    contexts = []
    for value in elementpath.select(
            tree, expression, parser=_legado_xpath_parser()):
        element = _xpath_external_element(value)
        if element is not None:
            contexts.append(element)
    document_root = tree.getroot()
    parents = {child: parent for parent in document_root.iter()
               for child in parent if isinstance(child.tag, str)}
    records: list[tuple[int, str, ET.Element]] = []
    starts: dict[ET.Element, int] = {}
    ends: dict[ET.Element, int] = {}
    order = 0

    def record(text: str | None, parent: ET.Element) -> None:
        nonlocal order
        if text is not None:
            records.append((order, _jx_text(text), parent))
            order += 1

    def walk(element: ET.Element) -> None:
        starts[element] = order
        record(element.text, element)
        for child in element:
            if isinstance(child.tag, str):
                walk(child)
            record(child.tail, element)
        ends[element] = order

    walk(document_root)
    result: list[str] = []
    for context in contexts:
        parent = parents.get(context)
        if axis.endswith("sibling") and parent is not None:
            children = [child for child in parent if isinstance(child.tag, str)]
            position = children.index(context)
            sibling_texts = ([parent.text] + [child.tail for child in children])
            if axis == "following-sibling":
                selected = sibling_texts[position + 1:]
            else:
                selected = list(reversed(sibling_texts[:position + 1]))
            result.extend(_jx_text(value) for value in selected if value is not None)
        elif axis == "following":
            result.extend(text for index, text, _ in records
                          if index >= ends.get(context, 0))
        else:
            ancestors = set()
            ancestor = parents.get(context)
            while ancestor is not None:
                ancestors.add(ancestor)
                ancestor = parents.get(ancestor)
            result.extend(text for index, text, owner in reversed(records)
                          if index < starts.get(context, 0) and owner not in ancestors)
    return result


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


def split_rule_expression(value: str, separators: tuple[str, ...],
                          code: bool = False) -> tuple[list[str], str]:
    """Port RuleAnalyzer.splitRule's observable operator grouping.

    The first separator found outside ``[]``/``()`` becomes ``elementsType``;
    subsequent recursion handles other operator types. ``code`` selects the
    JSONPath escaping rules used by chompCodeBalanced.
    """
    text = str(value or "")

    def positions(target: str | None = None):
        found, stack, quote = [], [], ""
        raw_separator = False
        index = 0
        while index < len(text):
            if any(text.startswith(item, index) for item in separators):
                raw_separator = True
            char = text[index]
            if char == "\\" and (code or not quote):
                index += 2
                continue
            if quote:
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char in "'\"":
                quote = char
                index += 1
                continue
            if char in "[(":
                stack.append("]" if char == "[" else ")")
                index += 1
                continue
            if char in "])" and stack and char == stack[-1]:
                stack.pop()
                index += 1
                continue
            if not stack:
                candidates = ((target,) if target else separators)
                matched = next((item for item in candidates
                                if text.startswith(item, index)), None)
                if matched:
                    found.append((index, matched))
                    index += len(matched)
                    continue
            index += 1
        # RuleAnalyzer validates only the balanced group that hides the next
        # raw separator. Once an earlier outside separator is found, later
        # malformed fallback branches remain lazy and are not inspected yet.
        if stack and raw_separator and not found:
            raise RuleParseError(f"{text}后未平衡")
        return found

    outside = positions()
    if not outside:
        return [text], ""
    operator = outside[0][1]
    cuts = positions(operator)
    parts, start = [], 0
    for index, _ in cuts:
        parts.append(text[start:index])
        start = index + len(operator)
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()], operator


@lru_cache(maxsize=2048)
def _path_steps(rule: str) -> tuple[tuple[str, str], ...]:
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
    return tuple(result)


@lru_cache(maxsize=2048)
def _step_parts(step: str) -> tuple[str, tuple[str, ...]]:
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
    return tag, tuple(predicates)


def _attr(node: Tag, name: str) -> str:
    # Jsoup's abs: prefix asks Element.attr for the underlying attribute and
    # resolves it against baseUri. URL resolution is performed by the caller,
    # which owns the response URL in the Python engine.
    if str(name).lower().startswith("abs:"):
        name = str(name)[4:]
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
    def negative_contains(match):
        filters.append(("not_contains_ci", match.group(1).strip().strip("'\"")))
        return ""
    value = re.sub(r":not\(\s*:contains\(([^()]*)\)\s*\)",
                   negative_contains, value)
    def negative_contains_own(match):
        filters.append(("not_own_contains_ci",
                        match.group(1).strip().strip("'\"")))
        return ""
    value = re.sub(r":not\(\s*:containsOwn\(([^()]*)\)\s*\)",
                   negative_contains_own, value)
    def contains(match):
        filters.append(("contains_ci", match.group(1).strip().strip("'\"")))
        return ""
    value = re.sub(r":contains\(([^()]*)\)", contains, value)
    def contains_own(match):
        literal = match.group(1).strip().strip("'\"")
        filters.append(("own_contains_ci", literal))
        return ""
    value = re.sub(r":containsOwn\(([^()]*)\)", contains_own, value)
    def eq(match):
        index = int(match.group(1))
        return f":nth-child({index + 1})"
    value = re.sub(r":eq\(\s*(\d+)\s*\)", eq, value)
    value = re.sub(r":lt\(\s*(\d+)\s*\)",
                   lambda m: f":nth-child(-n+{int(m.group(1))})", value)
    value = re.sub(r":gt\(\s*(\d+)\s*\)",
                   lambda m: f":nth-child(n+{int(m.group(1)) + 2})", value)
    return value.strip() or "*", filters


def select_jsoup(context: Any, selector: str) -> list[Tag]:
    """Select with the Jsoup pseudos used by Legado book-source rules."""
    attribute_filters = []
    attribute_prefixes = []

    def attribute_regex(match):
        attribute_filters.append((match.group(1), match.group(2)))
        return f"[{match.group(1)}]"

    def extract_attribute_regex(value: str) -> str:
        """Extract Jsoup's ``[key~=java-regex]`` using balanced brackets."""
        output, cursor = [], 0
        while cursor < len(value):
            if value[cursor] != "[":
                output.append(value[cursor])
                cursor += 1
                continue
            end, depth, escaped, quote = cursor + 1, 1, False, ""
            while end < len(value) and depth:
                char = value[end]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif quote:
                    if char == quote:
                        quote = ""
                elif char in "'\"":
                    quote = char
                elif char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                end += 1
            if depth:
                if "~=" in value[cursor:]:
                    raise RuleParseError("无效的 Jsoup 属性正则选择器")
                output.append(value[cursor:])
                break
            content = value[cursor + 1:end - 1]
            match = re.match(r"^\s*([\w:-]+)\s*~=([\s\S]*)$", content)
            if match:
                attribute_filters.append((match.group(1), match.group(2)))
                output.append(f"[{match.group(1)}]")
            else:
                output.append(value[cursor:end])
            cursor = end
        return "".join(output)

    selector = extract_attribute_regex(str(selector))
    document_not_equal = re.fullmatch(
        r'\s*\[([\w:-]+)!=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\]]*))\]\s*',
        selector)
    had_hex_escape = bool(re.search(r"\\[0-9A-Fa-f]{1,6}", selector))
    if had_hex_escape:
        selector = re.sub(r"\\([0-9A-Fa-f]{1,6})", r"\1", selector)
        selector = re.sub(
            r"([.#])([0-9])",
            lambda match: f"{match.group(1)}\\3{match.group(2)} ", selector)
    selector = re.sub(r",(?:\s*,)+\s*(?=\S)", ",", selector)
    wildcard_namespace = re.fullmatch(r"\s*\*\|([\w-]+)\s*", selector)
    if wildcard_namespace:
        local_name = wildcard_namespace.group(1).casefold()
        return [node for node in context.find_all(True)
                if (str(node.name).casefold() == local_name or
                    str(node.name).casefold().endswith(":" + local_name))]
    # SoupSieve has no wildcard namespace for literal colon-named HTML tags.
    # Expand the wildcard from the actual DOM names so it remains usable in
    # compound selectors such as ``div > *|span`` and ``p + *|content``.
    def wildcard_namespace_compound(match):
        boundary, local = match.groups()
        local_folded = local.casefold()
        names = {str(node.name) for node in context.find_all(True)
                 if (str(node.name).casefold() == local_folded or
                     str(node.name).casefold().endswith(":" + local_folded))}
        names.add(local)
        choices = ", ".join(sorted(
            name.replace(":", r"\:") for name in names))
        return f"{boundary}:is({choices})"

    selector = re.sub(
        r"(^|[\s>,+~(])\*\|([A-Za-z_][\w-]*)",
        wildcard_namespace_compound, selector)
    # Jsoup uses CSS namespace notation for literal colon-named HTML/XML tags
    # (media|content -> <media:content>). SoupSieve needs the colon escaped.
    selector = re.sub(
        r"(^|[\s>,+~(])([A-Za-z_][\w-]*)\|([A-Za-z_][\w-]*)",
        lambda match: (f"{match.group(1)}{match.group(2)}\\:"
                       f"{match.group(3)}"),
        selector,
    )
    def attribute_starting(match):
        attribute_prefixes.append(match.group(1).casefold())
        return "*"

    selector = re.sub(r"\[\^([\w:-]+)\]", attribute_starting, selector)
    # Jsoup accepts spaces and literal colons in an unquoted value. Backslash
    # escapes are not decoded there, while SoupSieve would add false matches.
    unsupported_unquoted_attribute = False

    def quote_unquoted_attribute(match):
        nonlocal unsupported_unquoted_attribute
        raw_value = match.group(3)
        if "\\" in raw_value:
            unsupported_unquoted_attribute = True
        return (f'[{match.group(1)}{match.group(2)}"'
                f'{raw_value.replace(chr(34), chr(92)+chr(34))}"]')

    selector = re.sub(
        r"\[([\w:-]+)([~|^$*]?=)([^\]\"']+)\]",
        quote_unquoted_attribute,
        selector,
    )
    if unsupported_unquoted_attribute:
        return []
    css, filters = _selector_regex_filters(selector)
    # Jsoup's class and non-regex attribute evaluators compare ASCII/Unicode
    # case-insensitively. CSS/SoupSieve defaults these values to sensitive.
    css = re.sub(
        r"(?<![\w\\])\.((?:\\[^\s]|[\w-])+)",
        lambda match: f'[class~="{match.group(1)}" i]', css)
    css = re.sub(
        r'(\[[\w:-]+(?:!=|\^=|\$=|\*=|\|=|=)'
        r'(?:"[^"]*"|\'[^\']*\'|[^\]\s]+))\]',
        r'\1 i]', css)
    try:
        nodes = list(context.select(css))
        if (isinstance(context, Tag) and
                not isinstance(context, BeautifulSoup) and
                soupsieve.match(css, context)):
            nodes.insert(0, context)
        elif isinstance(context, BeautifulSoup):
            # Jsoup's Document extends Element and Collector tests the root as
            # well as its descendants. SoupSieve intentionally omits the
            # BeautifulSoup document node.
            document_matches = css == "*"
            not_equal = re.fullmatch(
                r'\[([\w:-]+)!=(?:"([^"]*)"|\'([^\']*)\') i\]', css)
            if not_equal:
                expected = not_equal.group(2) or not_equal.group(3) or ""
                document_matches = _attr(context, not_equal.group(1)).casefold() != expected.casefold()
            elif document_not_equal:
                expected = next((part for part in document_not_equal.groups()[1:]
                                 if part is not None), "").strip()
                document_matches = (_attr(context, document_not_equal.group(1)).casefold() !=
                                    expected.casefold())
            if document_matches:
                nodes.insert(0, context)
    except SelectorSyntaxError:
        if had_hex_escape:
            return []
        raise
    for attribute, pattern in attribute_filters:
        try:
            regex = compile_pattern(pattern)
        except re.error as exc:
            raise RuleParseError(f"无效的属性正则：{attribute}~={pattern}") from exc
        nodes = [node for node in nodes if regex.search(_attr(node, attribute))]
    for prefix in attribute_prefixes:
        nodes = [node for node in nodes if any(
            str(name).casefold().startswith(prefix) for name in node.attrs)]
    for own, pattern in filters:
        if own == "contains_ci":
            expected = pattern.casefold()
            nodes = [node for node in nodes if expected in _jsoup_text(node).casefold()]
            continue
        if own == "own_contains_ci":
            expected = pattern.casefold()
            nodes = [node for node in nodes
                     if expected in _jsoup_own_text(node).casefold()]
            continue
        if own == "not_contains_ci":
            expected = pattern.casefold()
            nodes = [node for node in nodes
                     if expected not in _jsoup_text(node).casefold()]
            continue
        if own == "not_own_contains_ci":
            expected = pattern.casefold()
            nodes = [node for node in nodes
                     if expected not in _jsoup_own_text(node).casefold()]
            continue
        try:
            regex = compile_pattern(pattern)
        except re.error as exc:
            raise RuleParseError(f"无效的 Jsoup 正则选择器：{pattern}") from exc
        if own == "negative_descendant":
            nodes = [node for node in nodes if not any(
                regex.search(_jsoup_text(descendant))
                for descendant in node.find_all(True))]
        elif own:
            nodes = [node for node in nodes if regex.search(_jsoup_own_text(node))]
        else:
            nodes = [node for node in nodes if regex.search(_jsoup_text(node))]
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
    union_parts = split_balanced(str(rule), "|")
    if len(union_parts) > 1 and any(re.search(
            r"/(?:html|outerHtml|allText|num)\(\)", part)
            for part in union_parts):
        return [item for part in union_parts for item in xpath_values(root, part)]
    axis_text = _xpath_text_axis_values(root, str(rule))
    if axis_text is not None:
        return axis_text
    recursive_text = _recursive_xpath_text_values(root, str(rule))
    if recursive_text is not None:
        return recursive_text
    custom = re.fullmatch(
        r"(substring-ex|substring-after-last|substring-before-last|format-date|sum)\((.*)\)",
        str(rule).strip(), flags=re.S)
    if custom:
        arguments = split_balanced(custom.group(2), ",")

        def argument_value(argument: str):
            value = argument.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                return value[1:-1]
            if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                return float(value) if "." in value else int(value)
            selected = xpath_values(root, value)
            if not selected:
                return ""
            first = selected[0]
            return ("".join(first.itertext()) if isinstance(first, ET.Element)
                    else str(first))

        name = custom.group(1)
        if name == "sum" and len(arguments) == 1:
            selected = xpath_values(root, arguments[0])
            numbers = []
            for item in selected:
                if isinstance(item, ET.Element):
                    value = (item.text or "") + "".join(
                        child.tail or "" for child in item)
                    value = _jx_text(value)
                else:
                    value = str(item).strip()
                if not re.fullmatch(r"\d*\.?\d+", value):
                    return [""]
                numbers.append(float(value))
            total = sum(numbers)
            return [int(total) if total.is_integer() else total]
        values = [argument_value(argument) for argument in arguments]
        if name == "format-date" and len(values) in {2, 3}:
            java_pattern = str(values[1])
            python_pattern = java_pattern
            for java_token, python_token in (
                    ("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"),
                    ("dd", "%d"), ("HH", "%H"), ("hh", "%I"),
                    ("mm", "%M"), ("ss", "%S"), ("aa", "%p"),
                    ("a", "%p")):
                python_pattern = python_pattern.replace(java_token, python_token)
            parsed = datetime.strptime(str(values[0]), python_pattern)
            if not re.search(r"[yMd]", java_pattern):
                parsed = parsed.replace(year=1970, month=1, day=1)
            return [JavaDateValue(parsed.astimezone())]
        if name == "substring-ex" and len(values) == 3:
            text, start, end = str(values[0]), int(values[1]), int(values[2])
            if end < 0:
                end += len(text)
            return [text[start:end]]
        if len(values) == 2:
            text, separator = map(str, values)
            if separator not in text:
                return [""]
            before, after = text.rsplit(separator, 1)
            return [after if name == "substring-after-last" else before]
    try:
        import elementpath
        tree = _to_element_tree(root)
        node_test = ""
        if rule.endswith("/html()"):
            rule, node_test = rule[:-7], "html"
        elif rule.endswith("/outerHtml()"):
            rule, node_test = rule[:-12], "outer_html"
        elif rule.endswith("/allText()"):
            rule, node_test = rule[:-10], "all_text"
        elif rule.endswith("/num()"):
            rule, node_test = rule[:-6], "num"
        elif rule.endswith("@html") and not rule.endswith("/@html"):
            rule, node_test = rule[:-5], "html"
        # JsoupXpath 2.5.3 extensions expressed through equivalent XPath 2
        # constructs understood by elementpath.
        rule = re.sub(r"\bfollowing-sibling-one::([\w*:-]+)",
                      r"following-sibling::\1[1]", rule)
        rule = re.sub(r"\bpreceding-sibling-one::([\w*:-]+)",
                      r"preceding-sibling::\1[1]", rule)
        rule = re.sub(r"\bfirst\(\)", "1", rule)
        segment = r"[\w*:-]+(?:\[[^\[\]]+\])?"
        embedded_path = rf"((?:\.{{1,2}}|self::{segment}|{segment})(?:/{segment})+)"
        rule = re.sub(embedded_path + r"/num\(\)", r"jx-num(\1)", rule)
        rule = re.sub(embedded_path + r"/allText\(\)",
                      r"jx-all-text(\1)", rule)
        rule = re.sub(r"(?<![\w-])num\(\)", "jx-num(.)", rule)
        rule = re.sub(r"(?<![\w-])allText\(\)", "jx-all-text(.)", rule)
        operand = r"(?:@[\w:-]+|text\(\)|\.|['\"][^'\"]*['\"]|[\w.:-]+)"
        translations = (
            (r"({0})\s*\^=\s*({0})".format(operand), r"starts-with(\1,\2)"),
            (r"({0})\s*\$=\s*({0})".format(operand), r"ends-with(\1,\2)"),
            (r"({0})\s*\*=\s*({0})".format(operand), r"contains(\1,\2)"),
            (r"({0})\s*!~=?\s*({0})".format(operand), r"not(jx-matches(\1,\2))"),
            (r"({0})\s*~=?\s*({0})".format(operand), r"jx-matches(\1,\2)"),
        )
        for pattern, replacement in translations:
            rule = re.sub(pattern, replacement, rule)
        expression = "." + rule if rule.startswith("//") else rule
        namespaces = {}
        source_root = (root.find(True) if isinstance(root, BeautifulSoup)
                       else root if isinstance(root, Tag) else None)
        if source_root is not None:
            for name, value in source_root.attrs.items():
                if name == "xmlns":
                    namespaces[""] = str(value)
                elif str(name).startswith("xmlns:"):
                    namespaces[str(name).split(":", 1)[1]] = str(value)
        selected = elementpath.select(
            tree, expression, namespaces=namespaces or None,
            parser=_legado_xpath_parser())
        values = list(selected) if isinstance(selected, (list, tuple)) else [selected]
        if node_test:
            elements = [item for item in values if isinstance(item, ET.Element)]
            if node_test == "outer_html":
                return [ET.tostring(item, encoding="unicode") for item in elements]
            if node_test == "html":
                rendered = []
                for item in elements:
                    inner = item.text or ""
                    inner += "".join(ET.tostring(child, encoding="unicode")
                                     for child in item)
                    rendered.append(inner)
                return rendered
            if node_test == "all_text":
                result = []
                for item in elements:
                    markup = ET.tostring(item, encoding="unicode")
                    tag = parse_markup(markup).find(True)
                    result.append(_jsoup_text(tag) if tag is not None else "")
                return result
            numbers = []
            for item in elements:
                markup = ET.tostring(item, encoding="unicode")
                tag = parse_markup(markup).find(True)
                whole_text = _jsoup_text(tag) if tag is not None else ""
                match = re.search(r"\d*\.?\d+", whole_text)
                if match:
                    number = float(match.group(0))
                    numbers.append(int(number) if number.is_integer() else number)
            return numbers
        if re.search(r"/preceding(?:-sibling)?::", rule):
            values.reverse()
        if "text()" in rule:
            return [re.sub(r"\s+", " ", value).strip()
                    if isinstance(value, str) else value for value in values]
        return values
    except ImportError as exc:
        raise RuleParseError("XPath 引擎 elementpath 未安装") from exc
    except Exception as exc:
        raise RuleParseError(f"XPath 解析失败：{rule}（{exc}）") from exc


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


def _apply_selection(values: list[Any], selection: str, exclude: bool) -> list[Any]:
    if not selection:
        return values
    indices = _selection_indices(selection, len(values))
    if exclude:
        blocked = set(indices)
        return [item for position, item in enumerate(values)
                if position not in blocked]
    return [values[position] for position in indices]


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
    else: selector = part
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

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center",
    "dd", "details", "dir", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hgroup", "hr", "html", "legend", "li", "main", "menu",
    "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
}
_VOID_TAG_PATTERN = re.compile(
    r"<(area|base|basefont|bgsound|br|col|embed|hr|img|input|keygen|link|meta|param|source|track|wbr)(\b[^<>]*?)/>",
    re.I)
_BOOLEAN_ATTR_PATTERN = re.compile(
    r'\s(allowfullscreen|async|autofocus|checked|compact|controls|declare|default|defaultchecked|defaultmuted|defaultselected|defer|disabled|enabled|formnovalidate|hidden|indeterminate|inert|ismap|itemscope|loop|multiple|muted|nohref|noresize|noshade|novalidate|nowrap|open|pauseonexit|readonly|required|reversed|scoped|seamless|selected|sortable|truespeed|typemustmatch)(?:=""|="\1")',
    re.I)

_JSOUP_BLOCK_TAGS = {
    "html", "head", "body", "frameset", "script", "noscript", "style",
    "meta", "link", "title", "frame", "noframes", "section", "nav",
    "aside", "hgroup", "header", "footer", "p", "h1", "h2", "h3",
    "h4", "h5", "h6", "ul", "ol", "pre", "div", "blockquote", "hr",
    "address", "figure", "figcaption", "form", "fieldset", "ins", "del",
    "dl", "dt", "dd", "li", "table", "caption", "thead", "tfoot",
    "tbody", "colgroup", "col", "tr", "th", "td", "video", "audio",
    "canvas", "details", "menu", "plaintext", "template", "article",
    "main", "svg", "math", "center", "dir", "applet", "marquee",
    "listing",
}
_JSOUP_INLINE_TAGS = {
    "object", "base", "font", "tt", "i", "b", "u", "big", "small",
    "em", "strong", "dfn", "code", "samp", "kbd", "var", "cite",
    "abbr", "time", "acronym", "mark", "ruby", "rt", "rp", "rtc",
    "a", "img", "br", "wbr", "map", "q", "sub", "sup", "bdo",
    "iframe", "embed", "span", "input", "select", "textarea", "label",
    "button", "optgroup", "option", "legend", "datalist", "keygen",
    "output", "progress", "meter", "area", "param", "source", "track",
    "summary", "command", "device", "basefont", "bgsound", "menuitem",
    "data", "bdi", "s", "strike", "nobr", "rb", "text", "mi", "mo",
    "msup", "mn", "mtext",
}
_JSOUP_FORMAT_INLINE_TAGS = {
    "title", "a", "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
    "address", "li", "th", "td", "script", "style", "ins", "del", "s",
}
_JSOUP_PRESERVE_WHITESPACE_TAGS = {"pre", "plaintext", "title", "textarea"}
_JSOUP_EMPTY_TAGS = {
    "meta", "link", "base", "frame", "img", "br", "wbr", "embed", "hr",
    "input", "keygen", "col", "command", "device", "area", "basefont",
    "bgsound", "menuitem", "param", "source", "track",
}


def _jsoup_escape(value: Any, attribute: bool = False,
                  xml: bool = False, xhtml: bool = False) -> str:
    text = str(value)
    text = text.replace("&", "&amp;").replace(
        "\u00a0", "&#xa0;" if xml or xhtml else "&nbsp;")
    if not attribute or xml or xhtml:
        text = text.replace("<", "&lt;")
    if not attribute:
        text = text.replace(">", "&gt;")
    if attribute:
        text = text.replace('"', "&quot;")
    return text


def _jsoup_preserves_whitespace(node: Any) -> bool:
    current = node
    # Jsoup deliberately caps the ancestor search at six levels.
    for _ in range(6):
        if not isinstance(current, Tag):
            return False
        if str(current.name).lower() in _JSOUP_PRESERVE_WHITESPACE_TAGS:
            return True
        current = current.parent
    return False


def _jsoup_format_as_block(node: Tag) -> bool:
    return str(node.name).lower() not in (
        _JSOUP_INLINE_TAGS | _JSOUP_FORMAT_INLINE_TAGS)


def _jsoup_is_block(node: Tag) -> bool:
    return str(node.name).lower() in _JSOUP_BLOCK_TAGS


def _jsoup_effectively_first(node: Any) -> bool:
    previous = node.previous_sibling
    return previous is None or (isinstance(previous, NavigableString) and
                                not str(previous).strip())


def _jsoup_should_indent(node: Tag) -> bool:
    parent = node.parent if isinstance(node.parent, Tag) else None
    formatted_as_block = (_jsoup_is_block(node) or
                          (parent is not None and _jsoup_format_as_block(parent)))
    is_inline = (str(node.name).lower() in _JSOUP_INLINE_TAGS or
                 str(node.name).lower() not in _JSOUP_BLOCK_TAGS)
    inlineable = (is_inline and
                  (parent is None or _jsoup_is_block(parent)) and
                  not _jsoup_effectively_first(node) and
                  str(node.name).lower() != "br")
    return (formatted_as_block and not inlineable and
            not _jsoup_preserves_whitespace(node.parent))


def _jsoup_attributes(node: Tag, xml: bool, xhtml: bool = False) -> str:
    rendered: list[str] = []
    for raw_key, raw_value in node.attrs.items():
        key = str(raw_key)
        value = (" ".join(str(item) for item in raw_value)
                 if isinstance(raw_value, list) else
                 "" if raw_value is None else str(raw_value))
        collapse = (not xml and
                    (raw_value is None or
                     ((not value or value.casefold() == key.casefold()) and
                      _BOOLEAN_ATTR_PATTERN.fullmatch(f' {key}="{value}"') is not None)))
        rendered.append(" " + key if collapse else
                        f' {key}="{_jsoup_escape(value, attribute=True, xml=xml, xhtml=xhtml)}"')
    return "".join(rendered)


def _jsoup_serialize(value: Any, *, inner: bool = False) -> str:
    root = value
    document = root if isinstance(root, BeautifulSoup) else root.find_parent()
    while isinstance(document, Tag) and not isinstance(document, BeautifulSoup):
        document = document.parent
    settings = getattr(document, "__dict__", {})
    default_xml = bool(settings.get("_legado_xml", False))
    syntax = str(settings.get("_legado_output_syntax",
                              "xml" if default_xml else "html"))
    xml = syntax.lower() == "xml"
    pretty = bool(settings.get("_legado_output_prettyPrint", not default_xml))
    outline = bool(settings.get("_legado_output_outline", False))
    indent_amount = int(settings.get("_legado_output_indentAmount", 1))
    max_padding = int(settings.get("_legado_output_maxPaddingWidth", 30))
    xhtml = str(settings.get("_legado_output_escapeMode",
                             "xhtml" if default_xml else "base")).lower() == "xhtml"
    output: list[str] = []

    def indent(depth: int) -> None:
        output.append("\n" + " " * min(depth * indent_amount, max_padding))

    def should_indent(node: Tag) -> bool:
        parent = node.parent if isinstance(node.parent, Tag) else None
        formatted_as_block = (_jsoup_is_block(node) or outline or
                              (parent is not None and _jsoup_format_as_block(parent)))
        is_inline = (str(node.name).lower() in _JSOUP_INLINE_TAGS or
                     str(node.name).lower() not in _JSOUP_BLOCK_TAGS)
        inlineable = (is_inline and (parent is None or _jsoup_is_block(parent)) and
                      not _jsoup_effectively_first(node) and not outline and
                      str(node.name).lower() != "br")
        return (pretty and formatted_as_block and not inlineable and
                not _jsoup_preserves_whitespace(node.parent))

    def siblings(node: Any) -> list[Any]:
        return list(node.parent.children) if getattr(node, "parent", None) else [node]

    def render(node: Any, depth: int) -> None:
        if isinstance(node, Doctype):
            output.append(f"<!{'DOCTYPE' if xml else 'doctype'} {node}>")
            return
        if isinstance(node, Comment):
            if (pretty and _jsoup_effectively_first(node) and isinstance(node.parent, Tag) and
                    _jsoup_format_as_block(node.parent)):
                indent(depth)
            output.extend(("<!--", str(node), "-->"))
            return
        if isinstance(node, ProcessingInstruction):
            output.extend(("<?", str(node).rstrip("?"), "?>"))
            return
        if isinstance(node, CData):
            output.extend(("<![CDATA[", str(node), "]]>"))
            return
        if isinstance(node, NavigableString):
            parent = node.parent if isinstance(node.parent, Tag) else None
            if parent is not None and str(parent.name).lower() in {"script", "style"}:
                output.append(str(node))
                return
            preserve = _jsoup_preserves_whitespace(parent) or not pretty
            text = str(node)
            if not preserve:
                sequence = siblings(node)
                index = sequence.index(node)
                blank = not text.strip()
                previous = sequence[index - 1] if index else None
                following = sequence[index + 1] if index + 1 < len(sequence) else None
                could_skip = ((isinstance(following, Tag) and
                               should_indent(following)) or
                              (isinstance(following, NavigableString) and
                               not str(following).strip()) or
                              (isinstance(previous, Tag) and
                               (_jsoup_is_block(previous) or
                                str(previous.name).lower() == "br")))
                if blank and could_skip:
                    return
                text = re.sub(r"[\t\n\f\r ]+", " ", text)
                block_like = parent is not None and (
                    _jsoup_is_block(parent) or _jsoup_format_as_block(parent))
                if block_like and index == 0:
                    text = text.lstrip(" ")
                if block_like and index == len(sequence) - 1:
                    text = text.rstrip(" ")
                if (index == 0 and parent is not None and
                        _jsoup_format_as_block(parent) and not blank):
                    indent(depth)
                elif isinstance(previous, Tag) and str(previous.name).lower() == "br":
                    indent(depth)
            output.append(_jsoup_escape(text, xml=xml, xhtml=xhtml))
            return
        if not isinstance(node, Tag):
            return
        name = str(node.name)
        lowered = name.lower()
        if should_indent(node) and output:
            indent(depth)
        output.extend(("<", name, _jsoup_attributes(node, xml, xhtml)))
        children = list(node.children)
        shared_self_closing = (xml and lowered in
                               getattr(document, "_legado_self_closing_names", set()))
        self_closing = not children and (lowered in _JSOUP_EMPTY_TAGS or
                                         shared_self_closing or
                                         bool(getattr(node, "_legado_self_closing", False)))
        if self_closing:
            output.append(" />" if xml or lowered not in _JSOUP_EMPTY_TAGS else ">")
            return
        output.append(">")
        for child in children:
            render(child, depth + 1)
        if (pretty and children and (
                (_jsoup_format_as_block(node) and
                 not _jsoup_preserves_whitespace(node.parent)) or
                (outline and (len(children) > 1 or
                              (len(children) == 1 and isinstance(children[0], Tag)))))):
            indent(depth)
        output.extend(("</", name, ">"))

    nodes = (list(root.children) if isinstance(root, BeautifulSoup) or
             (inner and isinstance(root, Tag)) else [root])
    for child in nodes:
        render(child, 0 if inner else 0)
    result = "".join(output)
    return result.strip() if inner else result


def _jsoup_html_markup(value: Any) -> str:
    """Serialize BeautifulSoup HTML with Jsoup's HTML syntax settings."""
    if isinstance(value, (Tag, BeautifulSoup, NavigableString)):
        return _jsoup_serialize(value)
    rendered = _BOOLEAN_ATTR_PATTERN.sub(r" \1", str(value))
    return _VOID_TAG_PATTERN.sub(r"<\1\2>", rendered)


def _append_jsoup_text(buffer: list[str], value: str) -> None:
    value = re.sub(r"[ \t\n\f\r]+", " ", str(value))
    value = value.replace("\u200b", "").replace("\u00ad", "")
    if not value:
        return
    if buffer and buffer[-1].endswith(" ") and value.startswith(" "):
        value = value[1:]
    buffer.append(value)


def _jsoup_boundary(buffer: list[str]) -> None:
    if buffer and not buffer[-1].endswith((" ", "\u00a0")):
        buffer.append(" ")


def _jsoup_text(node: Tag) -> str:
    buffer: list[str] = []

    def walk(current) -> None:
        if isinstance(current, Comment):
            return
        if isinstance(current, NavigableString):
            if getattr(current.parent, "name", "") not in {"script", "style"}:
                _append_jsoup_text(buffer, str(current))
            return
        if not isinstance(current, Tag):
            return
        block = current.name.lower() in _BLOCK_TAGS
        if current.name.lower() == "br" or block:
            _jsoup_boundary(buffer)
        for child in current.children:
            walk(child)
        if block:
            _jsoup_boundary(buffer)

    walk(node)
    return "".join(buffer).strip(" \t\n\f\r\u00a0")


def _jsoup_own_text(node: Tag) -> str:
    buffer: list[str] = []
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            _append_jsoup_text(buffer, str(child))
        elif isinstance(child, Tag) and child.name.lower() == "br":
            _jsoup_boundary(buffer)
    return "".join(buffer).strip(" \t\n\f\r\u00a0")


def _ascii_trim(value: str) -> str:
    return str(value).strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"
                            "\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13"
                            "\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d"
                            "\x1e\x1f\x20")


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
            pattern = compile_pattern(regex_suffix.group(3))
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
    parts, _ = split_rule_expression(rule, ("@",), code=False)
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
    # AnalyzeByJSoup accepts Jsoup Elements as content.  Its selectors run
    # against every element in that collection while preserving order.
    contexts = (list(root) if isinstance(root, (list, tuple)) and
                all(hasattr(item, "select") for item in root) else [root])
    for part in parts:
        selector, selection, exclude = _jsoup_part(part)
        found = []
        for context in contexts:
            try:
                if selector == "children":
                    found.extend(_apply_selection(
                        list(context.find_all(True, recursive=False)),
                        selection, exclude))
                else:
                    found.extend(_apply_selection(
                        list(select_jsoup(context, selector)), selection, exclude))
            except Exception as exc: raise RuleParseError(f"无法解析 JSoup 规则：{part}") from exc
        contexts = found
    if not attr: return contexts
    lowered = attr.lower()
    if lowered == "text":
        return [value for n in contexts if (value := _jsoup_text(n))]
    if lowered == "textnodes":
        return ["\n".join(_ascii_trim(str(text)) for text in
                           n.find_all(string=True, recursive=False)
                           if _ascii_trim(str(text))) for n in contexts]
    if lowered == "owntext":
        return [value for value in (_jsoup_own_text(n) for n in contexts) if value]
    if lowered == "html":
        rendered = []
        for node in contexts:
            # AnalyzeByJSoup removes these nodes from the shared Elements
            # collection before outerHtml(), so later field rules observe the
            # same DOM mutation.
            for unwanted in node.select("script, style"):
                unwanted.decompose()
            rendered.append(_jsoup_html_markup(node))
        combined = "\n".join(value for value in rendered if value)
        return [combined] if combined else []
    if lowered == "all":
        combined = "\n".join(_jsoup_html_markup(node) for node in contexts)
        return [combined] if combined else []
    name = attr[5:] if lowered.startswith("attr.") else attr
    lookup_name = name[4:] if name.lower().startswith("abs:") else name
    values = []
    for node in contexts:
        if not node.has_attr(lookup_name):
            continue
        value = _attr(node, name)
        if value.strip() and value not in values:
            values.append(value)
    return values


def _jsonpath_function_argument(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    try:
        return parse_json_smart(value)
    except (TypeError, ValueError):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            return value[1:-1]
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value


def _apply_jsonpath_function(values: list[Any], name: str,
                             arguments: str) -> list[Any]:
    """Apply the tail functions supplied by Jayway JsonPath 2.8."""
    target = values[0] if len(values) == 1 and isinstance(
        values[0], (dict, str)) else values
    sequence = list(target.values()) if isinstance(target, dict) else (
        list(target) if isinstance(target, (list, tuple, str)) else [target])
    args = [_jsonpath_function_argument(part) for part in
            split_balanced(arguments, ",")] if arguments.strip() else []
    try:
        if name == "length":
            return [len(target)]
        if name == "keys":
            return [JavaKeySet(target.keys())] if isinstance(target, dict) else []
        if name == "first":
            return sequence[:1]
        if name == "last":
            return sequence[-1:]
        if name == "index":
            index = int(args[0])
            return [sequence[index]] if -len(sequence) <= index < len(sequence) else []
        if name in {"min", "max", "sum", "avg", "stddev"}:
            numbers = [float(value) for value in sequence
                       if isinstance(value, (int, float)) and not isinstance(value, bool)]
            if not numbers:
                return []
            if name == "min": return [min(numbers)]
            if name == "max": return [max(numbers)]
            if name == "sum": return [sum(numbers)]
            average = sum(numbers) / len(numbers)
            if name == "avg": return [average]
            return [math.sqrt(sum((value - average) ** 2 for value in numbers)
                              / len(numbers))]
        if name == "append":
            return sequence + args
        if name == "concat":
            result = list(sequence)
            for argument in args:
                result.extend(argument if isinstance(argument, list) else [argument])
            return result
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return []
    return []


_JSONPATH_FUNCTIONS = {
    "min", "max", "avg", "stddev", "length", "sum", "keys",
    "concat", "append", "first", "last", "index",
}


def _jsonpath_function_stage(expression: str):
    """Find the first top-level Jayway function and its remaining path."""
    quote = ""
    escaped = False
    square = round_ = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            square += 1
        elif char == "]":
            square = max(0, square - 1)
        elif char == "(":
            round_ += 1
        elif char == ")":
            round_ = max(0, round_ - 1)
        elif char == "." and square == 0 and round_ == 0:
            match = re.match(r"\.([A-Za-z]+)\(", expression[index:])
            if match and match.group(1) in _JSONPATH_FUNCTIONS:
                open_pos = index + len(match.group(0)) - 1
                cursor, depth, inner_quote, inner_escaped = open_pos + 1, 1, "", False
                while cursor < len(expression):
                    current = expression[cursor]
                    if inner_quote:
                        if inner_escaped:
                            inner_escaped = False
                        elif current == "\\":
                            inner_escaped = True
                        elif current == inner_quote:
                            inner_quote = ""
                    elif current in "'\"":
                        inner_quote = current
                    elif current == "(":
                        depth += 1
                    elif current == ")":
                        depth -= 1
                        if depth == 0:
                            return (expression[:index], match.group(1),
                                    expression[open_pos + 1:cursor],
                                    expression[cursor + 1:])
                    cursor += 1
                return None
        index += 1
    return None


_MISSING = object()


def _predicate_parts(value: str, operator: str) -> list[str]:
    parts, start, quote, escaped, depth = [], 0, "", False, 0
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and value.startswith(operator, index):
            parts.append(value[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    if parts:
        parts.append(value[start:].strip())
    return parts or [value.strip()]


def _strip_predicate_parentheses(value: str) -> str:
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        depth, quote, escaped, closes_at_end = 0, "", False, False
        for index, char in enumerate(value):
            if quote:
                if escaped: escaped = False
                elif char == "\\": escaped = True
                elif char == quote: quote = ""
            elif char in "'\"": quote = char
            elif char == "(": depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(value) - 1
                    break
        if not closes_at_end:
            break
        value = value[1:-1].strip()
    return value


def _predicate_path_value(item: Any, operand: str, document: Any):
    operand = operand.strip()
    if operand == "@":
        return item
    if operand == "$":
        return document
    if operand.startswith(("@.", "@[", "$.", "$[")):
        value = item if operand[0] == "@" else document
        path = operand[2:] if operand[1] == "." else operand[1:]
        tokens = re.findall(
            r"([^.\[\]]+)|\[(-?\d+)\]|\[['\"]((?:\\.|[^'\"])*)['\"]\]",
            path)
        for key, raw_index, quoted_key in tokens:
            if raw_index:
                if not isinstance(value, (list, tuple)):
                    return _MISSING
                index = int(raw_index)
                if not -len(value) <= index < len(value):
                    return _MISSING
                value = value[index]
            else:
                key = quoted_key or key
                if not isinstance(value, dict) or key not in value:
                    return _MISSING
                value = value[key]
        return value
    lowered = operand.lower()
    if lowered == "true": return True
    if lowered == "false": return False
    if lowered == "null": return None
    try:
        return json.loads(operand)
    except (TypeError, ValueError):
        try:
            return ast.literal_eval(operand)
        except (ValueError, SyntaxError):
            return operand.strip("'\"")


def _jayway_predicate(item: Any, predicate: str, document: Any) -> bool:
    predicate = _strip_predicate_parentheses(predicate)
    parts = _predicate_parts(predicate, "||")
    if len(parts) > 1:
        return any(_jayway_predicate(item, part, document) for part in parts)
    parts = _predicate_parts(predicate, "&&")
    if len(parts) > 1:
        return all(_jayway_predicate(item, part, document) for part in parts)
    if predicate.startswith("!") and not predicate.startswith("!="):
        return not _jayway_predicate(item, predicate[1:], document)

    collection = re.fullmatch(
        r"(?s)(@(?:\.[\w-]+|\[-?\d+\]|\[['\"][^'\"]+['\"]\])*)\s+"
        r"(in|nin|subsetof|anyof|noneof|size|empty)\s+(.+)", predicate, re.I)
    if collection:
        value = _predicate_path_value(item, collection.group(1), document)
        expected = _predicate_path_value(item, collection.group(3), document)
        if value is _MISSING:
            return False
        op = collection.group(2).lower()
        right = expected if isinstance(expected, (list, tuple, set)) else [expected]
        left = value if isinstance(value, (list, tuple, set)) else [value]
        if op == "in": return value in right
        if op == "nin": return value not in right
        if op == "subsetof": return all(entry in right for entry in left)
        if op == "anyof": return any(entry in right for entry in left)
        if op == "noneof": return not any(entry in right for entry in left)
        if op == "size":
            try: return len(value) == int(expected)
            except (TypeError, ValueError): return False
        if op == "empty":
            empty = value is None or value == "" or (
                isinstance(value, (list, tuple, dict, set)) and not value)
            return empty is bool(expected)

    comparison = re.fullmatch(
        r"(?s)(.+?)\s*(==|!=|<=|>=|<|>|=~)\s*(.+)", predicate)
    if comparison:
        left = _predicate_path_value(item, comparison.group(1), document)
        right_text = comparison.group(3).strip()
        if left is _MISSING:
            return False
        if comparison.group(2) == "=~":
            literal = re.fullmatch(r"/((?:\\.|[^/])*)/([A-Za-z]*)", right_text)
            if literal:
                flags = 0
                if "i" in literal.group(2): flags |= re.I
                if "m" in literal.group(2): flags |= re.M
                if "s" in literal.group(2): flags |= re.S
                return java_search(literal.group(1), str(left), flags) is not None
            right = _predicate_path_value(item, right_text, document)
            return java_search(str(right), str(left)) is not None
        right = _predicate_path_value(item, right_text, document)
        try:
            return {
                "==": left == right, "!=": left != right,
                "<": left < right, ">": left > right,
                "<=": left <= right, ">=": left >= right,
            }[comparison.group(2)]
        except TypeError:
            return False
    return _predicate_path_value(item, predicate, document) is not _MISSING


def _jsonpath_filter_stage(expression: str):
    start = expression.find("[?(")
    if start < 0:
        return None
    cursor, depth, quote, escaped = start + 3, 1, "", False
    while cursor < len(expression):
        char = expression[cursor]
        if quote:
            if escaped: escaped = False
            elif char == "\\": escaped = True
            elif char == quote: quote = ""
        elif char in "'\"": quote = char
        elif char == "(": depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and cursor + 1 < len(expression) and expression[cursor + 1] == "]":
                return (expression[:start], expression[start + 3:cursor],
                        expression[cursor + 2:])
        cursor += 1
    return None


def _jsonpath_is_definite(expression: str) -> bool:
    quote, escaped, square = "", False, 0
    bracket_start = -1
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if escaped: escaped = False
            elif char == "\\": escaped = True
            elif char == quote: quote = ""
        elif char in "'\"":
            quote = char
        elif expression.startswith("..", index) or char == "*":
            return False
        elif expression.startswith("[?(", index):
            return False
        elif char == "[":
            square += 1
            if square == 1:
                bracket_start = index + 1
        elif char == "]":
            if square == 1:
                body = expression[bracket_start:index]
                if ":" in body or "," in body:
                    return False
            square = max(0, square - 1)
        index += 1
    return True


def json_values(root: Any, rule: str, _preserve_array: bool = False) -> list[Any]:
    if rule.lower().startswith("@json:"): rule = rule.split(":", 1)[1].strip()
    expression = rule.strip()
    expression = re.sub(r"\](?=[A-Za-z_$])", "].", expression)
    # Jayway accepts JavaScript-style regex literals in predicates, whereas
    # jsonpath-ng expects a quoted regular-expression string.
    def regex_literal(match: re.Match) -> str:
        pattern, flags = match.group(1), match.group(2)
        inline = "".join(flag for flag in flags if flag in "imsx")
        if inline:
            pattern = f"(?{inline}){pattern}"
        return "=~ " + json.dumps(pattern)

    expression = re.sub(
        r"=~\s*/((?:\\.|[^/])*)/([A-Za-z]*)", regex_literal, expression)
    if expression and not expression.startswith(("$", "@")):
        expression = "$." + expression
    filter_stage = _jsonpath_filter_stage(expression)
    if filter_stage:
        base, predicate, suffix = filter_stage
        selected = [item for item in json_values(root, base)
                    if _jayway_predicate(item, predicate, root)]
        if not suffix:
            return selected
        result = []
        for item in selected:
            result.extend(json_values(item, "$" + suffix, True))
        return result
    function = _jsonpath_function_stage(expression)
    if function:
        base, name, arguments, suffix = function
        applied = _apply_jsonpath_function(
            json_values(root, base), name, arguments)
        if not suffix:
            return applied
        if not applied:
            return []
        if name == "keys":
            return []
        scalar_functions = {
            "min", "max", "avg", "stddev", "length", "sum",
            "first", "last", "index",
        }
        function_root = applied[0] if name in scalar_functions else applied
        return json_values(function_root, "$" + suffix)
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
    return ([JavaArrayNull() if item is None else item for item in values[0]]
            if (not _preserve_array and _jsonpath_is_definite(expression)
                and len(values) == 1 and isinstance(values[0], list)) else values)


def regex_values(root: Any, rule: str) -> list[list[str]]:
    """Return ``[group0, group1, ...]`` records for Legado ``:regex`` mode."""
    content = (root if isinstance(root, str) else
               json.dumps(root, ensure_ascii=False) if isinstance(root, (dict, list))
               else str(root))
    # AnalyzeByRegex receives SourceRule.rule.splitNotBlank("&&"); unlike
    # JSONPath/Jsoup/XPath combinations this delimiter is not balanced.
    patterns = [item.strip() for item in
                (rule[1:] if rule.startswith(":") else rule).split("&&")
                if item.strip()]
    if not patterns:
        return []
    for pattern in patterns[:-1]:
        try:
            content = "".join(match.group(0) for match in java_finditer(pattern, content))
        except re.error as exc:
            raise RuleParseError(f"无效的正则列表规则：{pattern}") from exc
        if not content:
            return []
    try:
        return [[match.group(0), *(value or "" for value in match.groups())]
                for match in java_finditer(patterns[-1], content)]
    except re.error as exc:
        raise RuleParseError(f"无效的正则列表规则：{patterns[-1]}") from exc


def evaluate(root: Any, rule: str, kind: str) -> list[Any]:
    """Evaluate Legado || / && / %% combinations with the matching sub-engine."""
    rule = (rule or "").strip()
    if not rule: return []
    if rule.startswith(":"):
        return regex_values(root, rule)
    parts, separator = split_rule_expression(
        rule, ("&&", "||", "%%"), code=kind == "json")
    if separator:
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
