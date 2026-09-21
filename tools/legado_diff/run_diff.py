"""双引擎差分对比：同一份(响应体, 规则)分别喂给 Legado 原生 Kotlin 分析器与 Python 引擎。

legado-main 是 Android 应用，无法在桌面直接整机启动；但它的规则分析器
(AnalyzeByJSoup / AnalyzeByJSonPath / AnalyzeByXPath / AnalyzeByRegex / RuleAnalyzer)
是纯 JVM 代码，已原样编译进 tools/legado_diff/harness。网络抓取只发生一次
(用本项目引擎的请求管线)，两个引擎看到的是同一份 body，因此任何差异都是
规则引擎差异，而不是站点波动差异。

用法:
    python tools/legado_diff/run_diff.py --limit 25
输出:
    tools/legado_diff/report.md (+ 控制台摘要)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import flet  # noqa: F401  (确保 flet 环境完整，与主程序一致)

from app.services.source_service import SourceService
from app.services.rule_engine import ReaderEngine, _legado_value_string
from app.services.legado_rule_parser import (evaluate, parse_json_smart,
                                             parse_markup)
from test_book_sources_async import parse_search_url

HARNESS = (ROOT / "tools/legado_diff/harness/build/install/"
           "legado-engine-harness/bin/legado-engine-harness.bat")
WORKDIR = Path(__file__).resolve().parent


def _sniff_kind(text: str) -> str:
    try:
        parse_json_smart(text)
        return "json"
    except Exception:
        return "html"


def _canonical(kind: str, value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          default=str)
    return str(value)


def _python_comparable(kind: str, nodes) -> list:
    """Match what the harness returns: Legado's getList gives raw JSON values
    while getStringList gives element text."""
    if kind == "json":
        return [_canonical(kind, node) for node in nodes]
    if kind == "jsoup":
        from app.services.legado_rule_parser import _jsoup_text
        return [_jsoup_text(node) if hasattr(node, "children")
                else str(node) for node in nodes]
    return [_legado_value_string(node) for node in nodes]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


async def collect(engine, source, index, cases, python_results):
    search_url = str(getattr(source, "searchUrl", "") or "").strip()
    rule = str((getattr(source, "ruleSearch", None) or {}).get("bookList") or "").strip()
    if not search_url or not rule:
        return {"index": index, "name": source.bookSourceName, "status": "SKIP",
                "note": "缺少 searchUrl 或 bookList 规则"}
    keyword = str((getattr(source, "ruleSearch", None) or {}).get("checkKeyWord") or "我的")
    try:
        raw = await engine._resolve_rule_url(search_url, source, keyword, 1)
        request = parse_search_url(raw, source.bookSourceUrl, keyword, 1)
        if not request:
            return {"index": index, "name": source.bookSourceName, "status": "SKIP",
                    "note": "searchUrl 经 JS 求值后仍无可用请求"}
        text, content_type, _final = await engine._request(request, source)
    except Exception as exc:
        return {"index": index, "name": source.bookSourceName, "status": "FETCH_ERR",
                "note": str(exc)[:160]}
    kind = _sniff_kind(text)
    rule_stripped = rule.strip()
    if any(marker in rule for marker in ("<js>", "@js:", "<webjs>", "{{")):
        return {"index": index, "name": source.bookSourceName, "status": "SKIP",
                "note": "bookList 含 JS/模板，超出裸分析器对比范围"}
    # 分发顺序照搬 AnalyzeRule.kt:600-633：@json:/$./$[ 或内容是 JSON 先判 Json，
    # 再看 "/" 前缀走 XPath，其余交给 Jsoup。
    if (kind == "json" or rule_stripped.startswith(("$.", "$["))
            or rule_stripped.lower().startswith("@json:")):
        analyzer = "json"
    elif rule_stripped.startswith(("//", "./", "@xpath:")):
        analyzer = "xpath"
    else:
        analyzer = "jsoup"
    case_id = f"s{index}"
    cases.append({"id": case_id, "engine": analyzer, "content": text, "rule": rule})
    try:
        root = (parse_json_smart(text) if kind == "json"
                else parse_markup(text, content_type))
        nodes = evaluate(root, rule, kind, list_context=True)
        values = _python_comparable(analyzer, nodes)
    except Exception as exc:
        return {"index": index, "name": source.bookSourceName, "status": "PY_ERR",
                "note": f"{type(exc).__name__}: {str(exc)[:160]}"}
    python_results[case_id] = {"kind": analyzer, "rule": rule, "values": values}
    return {"index": index, "name": source.bookSourceName, "status": "COLLECTED",
            "note": f"{kind} · {len(values)} 条"}


def compare(python_results, legado_path: Path):
    legado = {}
    with open(legado_path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                item = json.loads(line)
                legado[item["id"]] = item
    rows = []
    for case_id, py in python_results.items():
        lg = legado.get(case_id)
        if lg is None:
            rows.append((case_id, "NO_LEGADO", py, None, None))
            continue
        if not lg.get("ok"):
            rows.append((case_id, "LEGADO_ERR", py, lg.get("error"), None))
            continue
        py_values = [_normalize(v) for v in py["values"]]
        lg_values = [_normalize(_canonical(py["kind"], v))
                     for v in (lg.get("values") or [])]
        if len(py_values) != len(lg_values):
            rows.append((case_id, "COUNT_DIFF", py,
                         f"py={len(py_values)} legado={len(lg_values)}",
                         (py_values[:2], lg_values[:2])))
        elif any(a != b for a, b in zip(py_values, lg_values)):
            first_diff = next(i for i, (a, b) in enumerate(zip(py_values, lg_values))
                              if a != b)

            def _text_only(value: str) -> str:
                return _normalize(re.sub(r"<[^>]+>", " ", value))

            kind = "FORMAT_DIFF" if all(
                _text_only(a) == _text_only(b)
                for a, b in zip(py_values, lg_values)) else "VALUE_DIFF"
            rows.append((case_id, kind, py,
                         f"第 {first_diff} 条起不一致",
                         (py_values[first_diff:first_diff + 2],
                          lg_values[first_diff:first_diff + 2])))
        else:
            rows.append((case_id, "MATCH", py, None, None))
    return rows


def write_report(path: Path, summary_rows, compare_rows, sources):
    by_id = {f"s{row['index']}": row for row in summary_rows}
    counts = {}
    for row in compare_rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    lines = ["# Legado(Kotlin) vs Python 引擎差分报告", "",
             f"- 参与对比书源: {len(sources)}（成功求值 {len(compare_rows)}）",
             f"- 结果分布: {json.dumps(counts, ensure_ascii=False)}"]
    matched = [row for row in compare_rows if row[1] == "MATCH"]
    strong = [row for row in matched if row[2]["values"]]
    per_analyzer = {}
    for row in compare_rows:
        per_analyzer[row[2]["kind"]] = per_analyzer.get(row[2]["kind"], 0) + 1
    lines.append(f"- 分析器覆盖: {json.dumps(per_analyzer, ensure_ascii=False)}")
    lines.append(f"- 有效 MATCH（两侧均取到非空值）: {len(strong)} / {len(matched)}；"
                 f"其余为两侧同为空的弱一致")
    lines.append("")
    for status in ("COUNT_DIFF", "VALUE_DIFF", "FORMAT_DIFF",
                   "LEGADO_ERR", "PY_ERR", "NO_LEGADO"):
        bad = [row for row in compare_rows if row[1] == status]
        if not bad:
            continue
        lines.append(f"## {status} ({len(bad)})")
        lines.append("")
        for case_id, _, py, note, sample in bad:
            source = by_id.get(case_id, {})
            lines.append(f"- [{case_id}] {source.get('name', '?')} · 规则 `{py['rule'][:80]}`")
            if note:
                lines.append(f"  - {note}")
            if sample:
                lines.append(f"  - python: {json.dumps(sample[0], ensure_ascii=False)[:300]}")
                lines.append(f"  - legado: {json.dumps(sample[1], ensure_ascii=False)[:300]}")
        lines.append("")
    lines.append("## MATCH 明细（前 10）")
    for case_id, _, py, _, _ in [row for row in compare_rows if row[1] == "MATCH"][:10]:
        lines.append(f"- [{case_id}] {by_id.get(case_id, {}).get('name', '?')} · "
                     f"{py['kind']} · {len(py['values'])} 条")
    path.write_text("\n".join(lines), encoding="utf-8")


def replay(cases_path: Path) -> int:
    """离线重放：只对已录制的响应体跑 Python 侧，再与同一文件的历史 Legado 结果比对。

    全库联网扫描会向每个书源主机发起真实请求，其中不少站点已被本机杀软拦截；
    回归验证不需要网络，用录制好的用例即可。
    """
    out_path = cases_path.with_name(cases_path.stem.replace("cases", "legado_out")
                                     + cases_path.suffix)
    if not out_path.exists():
        print(f"缺少对应的 Legado 结果文件: {out_path}")
        return 1
    python_results, summary_rows = {}, []
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        case_id, rule = case["id"], str(case["rule"])
        index = int(re.sub(r"\D", "", case_id) or 0)
        text = str(case["content"])
        kind = _sniff_kind(text)
        row = {"index": index, "name": "离线用例", "status": "COLLECTED", "note": ""}
        try:
            root = (parse_json_smart(text) if kind == "json"
                    else parse_markup(text, "text/html"))
            values = _python_comparable(case["engine"],
                                        evaluate(root, rule, kind, list_context=True))
        except Exception as exc:
            row.update(status="PY_ERR", note=f"{type(exc).__name__}: {str(exc)[:120]}")
            summary_rows.append(row)
            continue
        row["note"] = f"{case['engine']} · {len(values)} 条"
        python_results[case_id] = {"kind": case["engine"], "rule": rule, "values": values}
        summary_rows.append(row)
    summary_rows.sort(key=lambda item: item["index"])
    rows = compare(python_results, out_path)
    counts = {}
    for row in rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    report = cases_path.with_name(
        "replay_report" + cases_path.stem[len("cases"):] + ".md")
    write_report(report, summary_rows, rows, summary_rows)
    print(f"离线重放 {len(rows)} 用例: {json.dumps(counts, ensure_ascii=False)}")
    print(f"报告: {report}")
    return 1 if any(row[1] not in ("MATCH", "FORMAT_DIFF") for row in rows) else 0


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--offset", type=int, default=0,
                        help="跳过前 N 个已启用书源；崩溃可用分片二分定位")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--replay", metavar="CASES",
                        help="不联网：重放已录制的 (响应体, 规则) 用例文件")
    args = parser.parse_args()

    if args.replay:
        return replay(Path(args.replay))

    service = SourceService(ROOT / "resource")
    service.load()
    sources = [item for item in service.sources if item.enabled]
    sources = sources[args.offset:args.offset + args.limit]
    if not sources:
        print("没有已启用的书源可对比")
        return 1
    print(f"参与对比的书源: {len(sources)} 个")

    cases, python_results, summary_rows = [], {}, []
    engine = ReaderEngine(timeout=args.timeout)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def guarded(source, index):
        async with semaphore:
            return await collect(engine, source, index, cases, python_results)

    try:
        tasks = [guarded(source, index)
                 for index, source in enumerate(sources, args.offset + 1)]
        for task in asyncio.as_completed(tasks):
            summary_rows.append(await task)
    finally:
        engine.js.close()

    summary_rows.sort(key=lambda row: row["index"])
    for row in summary_rows:
        print(f"[{row['index']:>3}] {row['status']:<10} {row['name'][:24]:<24} {row['note']}")

    collected = [row for row in summary_rows if row["status"] == "COLLECTED"]
    if not collected:
        print("没有可对比的用例")
        return 1

    tag = f".{args.offset}" if args.offset else ""
    cases_path = WORKDIR / f"cases{tag}.jsonl"
    out_path = WORKDIR / f"legado_out{tag}.jsonl"
    with open(cases_path, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False))
            handle.write("\n")
    print(f"运行 Legado JVM harness（{len(cases)} 用例）…")
    proc = subprocess.run([str(HARNESS), str(cases_path), str(out_path)],
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
        return 1

    rows = compare(python_results, out_path)
    report = WORKDIR / f"report{tag}.md"
    write_report(report, summary_rows, rows, sources)
    counts = {}
    for row in rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    print(f"对比完成: {json.dumps(counts, ensure_ascii=False)}")
    print("SUMMARY " + json.dumps({"offset": args.offset, "limit": args.limit,
                                   "cases": len(rows), "counts": counts},
                                  ensure_ascii=False))
    print(f"报告: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
