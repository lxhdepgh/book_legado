"""Compare the Python Java-type bridge with fixed Rhino 1.7.13-2."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.legado_js import JsContext, LegadoJsRuntime  # noqa: E402


EXPRESSIONS = (
    "typeof Packages.java.lang.Long.parseLong('7')",
    "Packages.java.lang.Long.parseLong('9223372036854775807').toString()",
    "Packages.java.lang.Long.valueOf('9223372036854775807').toString()",
    "Packages.java.lang.Long.valueOf('9223372036854775807')+''",
    "Packages.java.lang.Long.MAX_VALUE.toString()",
    "Packages.java.lang.Integer.valueOf(7) instanceof Packages.java.lang.Integer",
    "Packages.java.lang.Integer.parseInt('7') instanceof Packages.java.lang.Integer",
    "Packages.java.lang.Integer.valueOf(7)==7",
    "Packages.java.lang.Integer.valueOf(7)===7",
    "Packages.java.lang.Boolean.valueOf('false').booleanValue()",
    "Packages.java.lang.Byte.valueOf('127').byteValue()",
    "Packages.java.lang.Double.valueOf('1.5').toString()",
    "Packages.java.lang.Double.parseDouble('NaN').toString()",
    "Packages.java.lang.Double.valueOf('1.5') instanceof Packages.java.lang.Double",
    "Packages.java.lang.Double.valueOf('1.5').intValue()",
    "Packages.java.lang.Float.valueOf('1.25').toString()",
    "Packages.java.lang.Character.valueOf('A').charValue()",
    "Packages.java.lang.Character.valueOf('A') instanceof Packages.java.lang.Character",
    "Packages.java.lang.Character.isDigit('1')",
    "Packages.java.lang.Character.digit('f',16)",
    "String(Packages.java.lang.Integer.valueOf(7).getClass())",
    "String(Packages.java.lang.Integer.TYPE)",
    "typeof Packages.java.lang.Class",
    "String(Packages.java.lang.Class)",
    "String(Packages.java.lang.Class.forName)",
    "(function(){try{Packages.java.lang.Byte.valueOf('128');return 'no-error'}"
    "catch(e){return e instanceof Packages.java.lang.NumberFormatException}})()",
    "typeof new Error('x').stack",
    "Object.keys({b:1,2:2,1:3,a:4}).join(',')",
    "Object.getPrototypeOf({})===Object.prototype",
    "String(undefined)+'|'+String(null)+'|'+(undefined==null)+'|'+(undefined===null)",
    "Object.prototype.toString.call({})+'|'+Object.prototype.toString.call([])",
    "JSON.stringify({a:undefined,b:null})",
    "(function(){try{throw new Error('boom')}catch(e){return e.name+'|'+e.message+'|'+typeof e.stack}})()",
    "(function(){try{null.missing}catch(e){return e.name+'|'+typeof e.stack}})()",
    "(function(){try{JSON.parse('{')}catch(e){return e.name+'|'+typeof e.stack}})()",
    "(function(){implicitOracle=7;return this.implicitOracle})()",
    "typeof globalThis",
    "(/a/g.test('a'))+'|'+(/a/i.test('A'))+'|'+('aba'.replace(/a/g,'x'))",
    "new Date(0).getTime()+'|'+new Date(0).getTimezoneOffset()",
    "isNaN(NaN)+'|'+isFinite(Infinity)+'|'+String(-0)+'|'+(1/(-0))",
    "(function(){var a=[1,2];a.push(3);a.splice(1,1,4);return a.join(',')})()",
    "(function(){var o={a:1};var p=o;o.b=2;return (p===o)+'|'+p.b})()",
    "(function(){var a=[1];var p=a;p[0]=2;return (p===a)+'|'+a[0]})()",
    "Object.keys([,'x']).join(',')",
    "Object.prototype.propertyIsEnumerable.call({a:1},'a')",
    "(function(){try{throw new TypeError('bad')}catch(e){return e instanceof Error}})()",
    "(function(){return arguments.length+'|'+arguments[0]})(3)",
)


def java_value(expression: str) -> str:
    output = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "legado-rhino-oracle"
    jar = (ROOT / "base_history/legado-3.23.110211/modules/rhino1.7.3/lib/"
           "rhino-1.7.13-2.jar")
    result = subprocess.run(
        ["java", "-cp", os.pathsep.join((str(output), str(jar))),
         "RhinoOracle", f"String({expression})"],
        check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def main() -> int:
    runtime = LegadoJsRuntime()
    source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
    differences = []
    for expression in EXPRESSIONS:
        expected = java_value(expression)
        actual = runtime.evaluate("@js:String(" + expression + ")", JsContext(source=source))
        if actual != expected:
            differences.append((expression, expected, actual))
    for expression, expected, actual in differences:
        print(f"[{expression}] EXPECTED {expected!r}; ACTUAL {actual!r}")
    print(f"{len(EXPRESSIONS) - len(differences)} matched; {len(differences)} differed")
    return bool(differences)


if __name__ == "__main__":
    raise SystemExit(main())
