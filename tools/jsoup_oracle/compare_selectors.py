"""Compare Python selectors with the fixed Jsoup 1.16.2 development oracle."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.legado_rule_parser import parse_markup, select_jsoup  # noqa: E402


MARKUP = """<main id="Root" class="Box Alpha" data-v="A]B" data-num="x12y">
<section id="one" class="ITEM alpha" data-v="AbC"><p>Alpha (one)</p></section>
<section id="two" class="item BETA" data-v="a]b"><p>beta two</p></section>
<section id="three" class="Item gamma" data-v="xyz"><span>Gamma</span></section>
</main>"""

SELECTORS = (
    "main", ".item", "[data-v=abc]", "[data-v!=ABC]", "[data-v^=A]",
    "[data-v$=B]", "[data-v*=]", "[^data-]", "[data-num~=\\d+]",
    "[data-v~=[A-Z]]", r"[data-v~=A\]B]", r"[data-v~=[^\]]+]",
    "section:contains(ALPHA)", r"section:matches(?i)gamma",
    r"section:matchesOwn(^$)", "section:not(.BETA)",
    "main:has(section#two)", "section:not(:has(:matches(beta)))",
    "section:eq(-1)", "section:lt(2)", "section:gt(0)",
    "section,,p", "section:", "section:not()", "[data-v~=[]",
)


def java_ids(selector: str) -> tuple[str, ...] | str:
    output = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "legado-jsoup-oracle"
    classpath = os.pathsep.join((
        str(output), str(ROOT / "base_history/dependencies/jsoup-1.16.2.jar")))
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8",
                                     newline="", delete=False) as stream:
        stream.write(MARKUP)
        name = stream.name
    try:
        result = subprocess.run(
            ["java", "-cp", classpath, "JsoupOracle", "html", name, "id", selector],
            capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)
    if result.returncode:
        return "ERROR"
    import base64
    lines = result.stdout.splitlines()
    return tuple(base64.b64decode(line).decode("utf-8") for line in lines[1:])


def python_ids(selector: str) -> tuple[str, ...] | str:
    try:
        nodes = select_jsoup(parse_markup(MARKUP), selector)
        # Outer markup is intentionally avoided: the existing serialization
        # oracle covers it, while source order uniquely identifies this matrix.
        return tuple(str(node.get("id") or
                         ("#root" if node.name == "[document]" else node.name))
                     for node in nodes)
    except Exception:
        return "ERROR"


def main() -> int:
    differences = []
    for selector in SELECTORS:
        expected = java_ids(selector)
        actual = python_ids(selector)
        if expected != actual:
            differences.append((selector, expected, actual))
    for selector, expected, actual in differences:
        print(f"[{selector}] EXPECTED {expected!r}; ACTUAL {actual!r}")
    print(f"{len(SELECTORS) - len(differences)} matched; {len(differences)} differed")
    return bool(differences)


if __name__ == "__main__":
    raise SystemExit(main())
