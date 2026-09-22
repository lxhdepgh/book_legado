"""Line accounting for the languages embedded in the app's Python sources.

Counts every physical line exactly once: a line inside a non-docstring string
literal is attributed to the language that literal is written in, everything
else counts as Python.  Run from the repository root.
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path


JS_HINTS = (
    "function(", "function (", "=>", "var ", "let ", "const ", "===", "!==",
    "document.", "window.", "JSON.parse", "JSON.stringify", "return ",
)
HTML_HINTS = ("<div", "<html", "<head", "<body", "</", "<!DOCTYPE", "<input")


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _classify(text: str) -> str:
    stripped = text.strip("rbfu\"'")
    js = sum(1 for hint in JS_HINTS if hint in stripped)
    html = sum(1 for hint in HTML_HINTS if hint in stripped)
    if html > js:
        return "html"
    if js:
        return "javascript"
    return "text"


def count_file(path: Path) -> Counter:
    source = path.read_text(encoding="utf-8")
    total = source.count("\n") + (0 if source.endswith("\n") else 1)
    counter = Counter(python=total)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return counter
    docstrings = _docstring_lines(tree)
    reader = io.StringIO(source).readline
    try:
        tokens = list(tokenize.generate_tokens(reader))
    except (tokenize.TokenError, IndentationError):
        return counter
    fstring_kinds = {getattr(tokenize, "FSTRING_START", -1): "start",
                     getattr(tokenize, "FSTRING_MIDDLE", -1): "middle",
                     getattr(tokenize, "FSTRING_END", -1): "end"}
    open_fstring: tuple[int, list[str]] | None = None
    for token in tokens:
        kind = fstring_kinds.get(token.type)
        if kind == "start":
            open_fstring = (token.start[0], [])
            continue
        if kind == "middle":
            if open_fstring is not None:
                open_fstring[1].append(token.string)
            continue
        if kind == "end":
            if open_fstring is None:
                continue
            start_line, parts = open_fstring
            open_fstring = None
            covered = {line for line in range(start_line, token.end[0] + 1)
                       if line not in docstrings}
            language = _classify("".join(parts))
            if not covered or language == "text":
                continue
            counter[language] += len(covered)
            counter["python"] -= len(covered)
            continue
        if token.type != tokenize.STRING:
            continue
        start, end = token.start[0], token.end[0]
        covered = {line for line in range(start, end + 1) if line not in docstrings}
        if not covered:
            continue
        language = _classify(token.string)
        if language == "text":
            continue
        counter[language] += len(covered)
        counter["python"] -= len(covered)
    return counter


def main(roots: list[str]) -> None:
    total: Counter = Counter()
    js_files: list[tuple[int, str]] = []
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            counter = count_file(path)
            total.update(counter)
            if counter["javascript"]:
                js_files.append((counter["javascript"], str(path)))
    grand = sum(total.values())
    print(f"{'language':<12}{'lines':>8}{'share':>9}")
    for language, lines in total.most_common():
        print(f"{language:<12}{lines:>8}{lines / grand * 100:>8.1f}%")
    print(f"{'total':<12}{grand:>8}")
    print("\nJavaScript inside Python string literals:")
    for lines, name in sorted(js_files, reverse=True):
        print(f"  {lines:>5}  {name}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["app"])
