"""Compare Python serialization with the fixed Jsoup 1.16.2 development oracle."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.legado_rule_parser import (  # noqa: E402
    _jsoup_html_markup, _jsoup_serialize, parse_markup, select_jsoup)


CASES = (
    ("html", "div", '<div id="X" data-v="A B" class="a:b"> t '
     '<span>A</span><span>B</span><p>P<b>Q</b></p><img disabled="">'
     '<svg><foreignObject><path/></foreignObject></svg>'
     '<media:content url="u" />&nbsp;</div>'),
    ("html", "main", "<main><!-- c --><section>A<br> B</section> tail</main>"),
    ("html", "pre", "<pre> a\n  b &amp; c</pre>"),
    ("html", "table", "<table><tr><td>A</td><td>B</td></tr></table>"),
    ("html", "div", '<div title="a &quot; b &amp; c" hidden="hidden">&lt;x&gt;</div>'),
    ("html", "ul", "<ul>\n <li>one</li><li><b>two</b></li>\n</ul>"),
    ("html", "section", "<section><div>A</div> between <i>B</i><div>C</div></section>"),
    ("html", "script", '<script>if (a < b && c > d) x="&";</script>'),
    ("html", "textarea", "<textarea> a\n  b &amp; c</textarea>"),
    ("html", "x-box", "<x-box><x-empty/><x-full></x-full></x-box>"),
    ("html", "form", '<form><input checked="checked" multiple="">'
     '<button disabled="no">B</button></form>'),
    ("html", "p", "<p>A<br> B <em>C</em><!--tail--></p>"),
    ("xml", "root", '<?xml version="1.0"?><root z="1" a="2"><empty/>'
     '<full></full><![CDATA[a<b]]><!--c--></root>'),
    ("xml", "r", '<?xml version="1.0"?><r><A X="1"/><A X="2"></A>'
     '<?target value?></r>'),
    ("xml", "root", '<?xml version="1.0"?><root><A><b>1</A><c>2</root>'),
    ("xml", "root", '<?xml version="1.0"?><root><a x=1><b>2</root>'),
    ("xml", "root", '<?xml version="1.0"?><root>before<br/>after &bogus;</root>'),
    ("xml", "root", '<?xml version="1.0"?><root><!-- open <a> --><?pi x?><x></root>'),
    ("xml", "root", '<?xml version="1.0"?><root a="1>2" a="later" A="upper">x</root>'),
)


def oracle(mode: str, selector: str, markup: str, operation: str) -> str:
    output = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "legado-jsoup-oracle"
    classpath = os.pathsep.join((
        str(output), str(ROOT / "base_history/dependencies/jsoup-1.16.2.jar")))
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", newline="",
                                     delete=False) as stream:
        stream.write(markup)
        name = stream.name
    try:
        result = subprocess.run(
            ["java", "-cp", classpath, "JsoupOracle", mode, name, operation, selector],
            check=True, capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)
    lines = result.stdout.splitlines()
    if not lines or int(lines[0]) != 1:
        raise RuntimeError(f"oracle selector returned {lines!r}")
    return base64.b64decode(lines[1]).decode("utf-8")


def main() -> int:
    differences = []
    for mode, selector, markup in CASES:
        document = parse_markup(markup, "application/xml" if mode == "xml" else "")
        node = select_jsoup(document, selector)[0]
        for operation in ("outer", "html"):
            expected = oracle(mode, selector, markup, operation)
            actual = (_jsoup_html_markup(node) if operation == "outer" else
                      _jsoup_serialize(node, inner=True))
            if actual != expected:
                differences.append((mode, selector, operation, expected, actual))
    for mode, selector, operation, expected, actual in differences:
        print(f"[{mode} {selector} {operation}]\nEXPECTED {expected!r}\nACTUAL   {actual!r}")
    print(f"{len(CASES) * 2 - len(differences)} matched; {len(differences)} differed")
    return bool(differences)


if __name__ == "__main__":
    raise SystemExit(main())
