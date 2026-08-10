"""Compare Python book metadata formatting with fixed Legado source logic."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.rule_engine import (  # noqa: E402
    _format_book_author, _format_book_name, _format_word_count)

CASES = {
    "name": (" Novel  作者 Someone ", "Book 张三 著", "普通书名", "A\u3000 作者 B"),
    "author": ("作者： Alice 著", " 作 者 Bob", "Alice 著", "Alice"),
    "word": ("0", "-1", "1", "10000", "10001", "12500", "15500", "99999999", "10K"),
}
PYTHON = {"name": _format_book_name, "author": _format_book_author,
          "word": _format_word_count}


def oracle(operation: str, value: str) -> str:
    output = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "legado-book-oracle"
    result = subprocess.run(
        ["java", "-Dfile.encoding=UTF-8", "-cp", str(output),
         "BookFormatOracle", operation, value],
        check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def main() -> int:
    differences = []
    for operation, values in CASES.items():
        for value in values:
            expected, actual = oracle(operation, value), PYTHON[operation](value)
            if expected != actual:
                differences.append((operation, value, expected, actual))
    for operation, value, expected, actual in differences:
        print(f"[{operation} {value!r}] EXPECTED {expected!r}; ACTUAL {actual!r}")
    total = sum(map(len, CASES.values()))
    print(f"{total - len(differences)} matched; {len(differences)} differed")
    return bool(differences)


if __name__ == "__main__":
    raise SystemExit(main())
