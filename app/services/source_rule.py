"""Canonical Python compiler for historical Legado SourceRule programs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


_JS_PATTERN = re.compile(r"<js>([\s\S]*?)</js>|@js:([\s\S]*)", re.I)
_PUT_PATTERN = re.compile(r"@put:(\{[^}]+?\})", re.I)
_EVAL_PATTERN = re.compile(r"@get:\{[^}]+?\}|\{\{[\s\S]*?\}\}", re.I)
_CAPTURE_PATTERN = re.compile(r"\$\d{1,2}")


@dataclass(frozen=True, slots=True)
class PythonSourceRule:
    mode: str
    rule: str
    raw: str
    put_rule: str = ""


@dataclass(frozen=True, slots=True)
class PythonRuleProgram:
    original: str
    stages: tuple[PythonSourceRule, ...]

    @property
    def normalized(self) -> str:
        return "".join(stage.raw for stage in self.stages)

    def leading_js(self) -> tuple[str | None, str]:
        if self.stages and self.stages[0].mode == "js":
            return self.stages[0].raw, "".join(
                stage.raw for stage in self.stages[1:]).strip()
        return None, self.original.strip()

    def selector_and_suffix(self) -> tuple[str, str]:
        if not self.stages:
            return self.original, ""
        if self.stages[0].mode == "js":
            return "", self.original.strip()
        if len(self.stages) > 1 and self.stages[1].mode == "js":
            return self.stages[0].rule.strip(), "".join(
                stage.raw for stage in self.stages[1:]).strip()
        return self.original, ""


def strip_put_rules(value: str) -> tuple[str, str]:
    bodies = []

    def collect(match):
        bodies.append(match.group(1)[1:-1])
        return ""

    cleaned = _PUT_PATTERN.sub(collect, str(value or ""))
    combined = "{" + ",".join(bodies) + "}" if bodies else ""
    return cleaned.strip(), combined


def _prepare_plain(rule: str, all_in_one: bool, is_json: bool,
                   is_regex: bool) -> tuple[str, str]:
    value = rule.strip()
    lowered = value.lower()
    if all_in_one and value.startswith(":"):
        return "regex", value[1:]
    if is_regex:
        mode = "regex"
    elif lowered.startswith("@xpath:"):
        mode, value = "xpath", value[7:]
    elif lowered.startswith("@json:"):
        mode, value = "json", value[6:]
    elif value.startswith("@@"):
        mode, value = "default", value[2:]
    elif is_json or value.startswith(("$.", "$[")):
        mode = "json"
    elif value.startswith("/"):
        mode = "xpath"
    else:
        mode = "default"

    # SourceRule promotes interpolation/capture programs to Regex mode unless
    # the first interpolation follows an already separated ## selector.
    match = _EVAL_PATTERN.search(value)
    if match and mode not in {"js", "regex"}:
        prefix = value[:match.start()]
        if match.start() == 0 or "##" not in prefix:
            mode = "regex"
    before_suffix = value.split("##", 1)[0]
    if _CAPTURE_PATTERN.search(before_suffix) and mode not in {"js", "regex"}:
        mode = "regex"
    return mode, value


@lru_cache(maxsize=4096)
def compile_rule_program(value: str, all_in_one: bool = False,
                         is_json: bool = False,
                         is_regex: bool = False) -> PythonRuleProgram:
    original = str(value or "")
    stages = []
    cursor = 0
    for match in _JS_PATTERN.finditer(original):
        if match.start() > cursor:
            raw = original[cursor:match.start()]
            rule, put_rule = strip_put_rules(raw)
            if rule or put_rule:
                mode, rule = _prepare_plain(
                    rule, all_in_one, is_json, is_regex)
                stages.append(PythonSourceRule(
                    mode, rule, raw, put_rule))
        raw = match.group(0)
        script = match.group(2) if match.group(2) is not None else match.group(1)
        stages.append(PythonSourceRule("js", script or "", raw))
        cursor = match.end()
    if cursor < len(original):
        raw = original[cursor:]
        rule, put_rule = strip_put_rules(raw)
        if rule or put_rule:
            mode, rule = _prepare_plain(
                rule, all_in_one, is_json, is_regex)
            stages.append(PythonSourceRule(
                mode, rule, raw, put_rule))
    return PythonRuleProgram(original, tuple(stages))
