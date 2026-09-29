"""审计的字段合法性只有一份定义，且已删的整库入口不会长回来（09-29 第四十五轮，任务 #77/#80/#81/#82）。

钉的四件事，都来自本轮实测：

1. **整库审计没有第二份判定**——``compat_audit.audit_source_corpus`` 在 app/ 与 tests/
   里零调用方，且和逐源编排 ``_audit_one`` 是同一套判定写两遍。两份平行实现就是这次
   字段表漂移的成因，删掉；``_audit_one`` 是唯一的逐源编排。
2. **合法字段只有一张表**（``book_source.RULE_FIELDS``，逐段照 Legado
   ``data/entities/rule/*.kt`` 写定）。旧的 ``compat_audit.KNOWN_RULE_FIELDS`` 是手写的
   第二份，实测冤枉 12 条合法字段、对 18 个「字段嵌进不认它的段」的组合一个都没报未知。
3. **审计逐段判定与模型逐段丢弃是同一个谓词**：模型 ``from_dict`` 丢掉的字段，审计必须
   报出来（放过 = 0）；表内字段不许报（冤枉 = 0）。真实语料 917 条上，逐段判定相对旧的
   跨段并集**不新增任何警告**（新增警告源 0 条 / 917，涉及字段 0 种）——所以严格化不要付噪音代价。
4. **一句缺口对一句文案**：旧的 ``_suggest(*buckets)`` 按下标判空，参数一换序就整条错位，
   实测「未知规则字段」被报成"Java/对象 API 未实现"、「缺对象方法」被报成"含未支持的
   jsoup 伪类"、选择器语法错一句建议都没有。

参考源码 ``other_history/`` 与语料 ``resource/*.json`` 都在 .gitignore 里，所以本文件
不读它们：Kotlin 侧的字段名以字面量写死在测试里并标出出处，量化的对照过程留在
``.probe/audit_known_fields_drift.py``、``.probe/audit_corpus_real_fields.py``。
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from app.controllers.source.code_editor_controller import (CodeEditorController,
                                                           _GROUP_LABELS)
from app.models.book_source import BookSource, RULE_FIELDS
from app.services import compat_audit as ca
from app.services.sources import online_import_service as ois
from app.services.sources.compat_audit_service import AuditReport, _audit_one

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Legado ``rule/ReviewRule.kt`` 的 10 个字段 + ``ContentRule.kt`` 的副文/回调。
#: 旧的手写字段表把这 12 条全判成"未知规则字段"——带段评规则的正常源被冤枉。
LEGADO_FIELDS_WRONGED_BEFORE = (
    ("ruleReview", "reviewUrl"), ("ruleReview", "avatarRule"),
    ("ruleReview", "contentRule"), ("ruleReview", "postTimeRule"),
    ("ruleReview", "reviewQuoteUrl"), ("ruleReview", "voteUpUrl"),
    ("ruleReview", "voteDownUrl"), ("ruleReview", "postReviewUrl"),
    ("ruleReview", "postQuoteUrl"), ("ruleReview", "deleteUrl"),
    ("ruleContent", "subContent"), ("ruleContent", "callBackJs"),
)

#: 只存在于 BookSource **顶层**、不属于任何规则段的字段。嵌进段里时模型会丢，
#: 旧字段表认它们 → 审计报 clean、规则静默失效。
TOP_LEVEL_ONLY = ("bookUrlPattern", "loginCheckJs", "coverDecodeJs")

SECTION_NAMES = tuple(RULE_FIELDS)


def one(section: str, field: str) -> dict:
    """一条只在该段放了该字段的源（值取通用形状，不触发别的缺口）。"""
    return {"bookSourceUrl": f"https://{section}.example",
            "bookSourceName": section, section: {field: ".a@text"}}


def kept_by_model(section: str, field: str) -> bool:
    """``from_dict`` 之后，这个段里的这个字段还在不在。"""
    source = BookSource.from_dict(one(section, field))
    return field in source._get_rule(section)  # noqa: SLF001


def scan_field_tables(source: str, name: str) -> list[str]:
    """找出「按规则段索引的字段表」字面量——这种表全库只该有一份。

    判定要收得紧：键里出现段名，且值全是集合/字典/列表**字面量**。
    界面里 ``{"ruleReview": "书评", ...}`` 那种中文名映射不是字段表，不许报。
    段名阈值取最严的 1（只要沾一个段名就算）：实测整个 app/ 里没有第二处
    "以规则段名为键、值为字面量" 的字典，放宽到 1 不误报，收紧到"≥5 段"反倒会漏掉
    只抄了四段的第二份表。
    """
    findings = []
    try:
        tree = ast.parse(source)
    except SyntaxError:  # 交给调用侧断言"没有文件解析失败"
        return findings
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not node.keys:
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        hit = sum(1 for k in keys if k in SECTION_NAMES)
        literal_values = all(isinstance(v, (ast.Set, ast.Dict, ast.List))
                             for v in node.values)
        if hit >= 1 and literal_values:
            findings.append(f"{name}:{node.lineno} 又写了一份按规则段索引的字段表")
    return findings


# --------------------------------------------------------------------------- #
# 1. 整库入口删掉后，判定只剩一份；能力没跟着被删
# --------------------------------------------------------------------------- #
class SingleAuditImplementationTests(unittest.TestCase):
    def test_corpus_entry_point_and_orphans_are_gone(self):
        """钉"没长回来"：整库入口和它的孤儿闭包不该再存在于 compat_audit。"""
        leftovers = [n for n in ("audit_source_corpus", "CorpusAudit",
                                 "ScriptSyntaxIssue", "RuleSyntaxIssue",
                                 "KNOWN_RULE_FIELDS")
                     if hasattr(ca, n)]
        self.assertFalse(leftovers,
                         f"compat_audit 里又出现了第二份审计：{leftovers}")

    def test_per_source_audit_still_detects_every_class(self):
        """反空转：上一条不能靠"审计什么都不检测了"来成立——七类缺口逐类埋点。"""
        plants = {
            "unknown_fields": ({"ruleContent": {"content": "p@text", "bogus": "x"}},
                               "bogus"),
            "missing_java_apis": ({"ruleBookInfo": {"init": "@js:java.nope()"}}, "nope"),
            "missing_object_apis": ({"ruleBookInfo": {"init": "@js:book.getNoSuch()"}},
                                    "book.getNoSuch"),
            "unsupported_pseudos": ({"ruleContent": {"content": "li:foo(a){{book.name}}@text"}},
                                    "foo"),
            "js_syntax_errors": ({"ruleBookInfo": {"init": "@js:function("}},
                                 "SyntaxError"),
            "regex_errors": ({"ruleContent": {"content": "p@text",
                                              "sourceRegex": "([a"}},
                            "unterminated"),
            "rule_syntax_errors": ({"ruleContent": {"content": "text[foo"}},
                                   "Malformed attribute selector"),
        }
        for bucket, (extra, needle) in plants.items():
            with self.subTest(类别=bucket):
                data = {"bookSourceUrl": "https://plant.example"}
                data.update(extra)
                report = _audit_one(data)
                found = getattr(report, bucket)
                self.assertTrue(found, f"{bucket} 埋点没被检出：{report}")
                self.assertTrue(any(needle in str(x) for x in found),
                                f"{bucket} 检出内容里没有 {needle!r}：{found}")
                self.assertFalse(report.clean, f"{bucket} 检出缺口却报 clean")


# --------------------------------------------------------------------------- #
# 2. 合法字段表只有一份定义
# --------------------------------------------------------------------------- #
class FieldTableSingleDefinitionTests(unittest.TestCase):
    def test_production_code_defines_the_rule_field_table_once(self):
        findings, unreadable, scanned = [], [], 0
        for path in sorted(APP.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                unreadable.append(f"{path.as_posix()}: {exc}")
                continue
            try:
                findings += scan_field_tables(text, path.as_posix())
            except SyntaxError as exc:
                unreadable.append(f"{path.as_posix()}: {exc}")
            else:
                scanned += 1
        self.assertFalse(unreadable, f"这些模块没被解析，等于没扫：{unreadable}")
        self.assertGreater(scanned, 100, f"只扫了 {scanned} 个文件，多半是路径口径错了")
        # 唯一的一份就是模型里的 RULE_FIELDS
        self.assertEqual(len(findings), 1,
                         "规则字段表被写成了两份以上（两份就会漂移，本轮实测冤枉 12 条）：\n"
                         + "\n".join(findings))
        self.assertIn("book_source.py", findings[0])

    def test_scanner_flags_a_planted_second_table(self):
        """反空转：植入一份并集字段表必须扫出来，否则第一条是空转。"""
        planted = (
            "KNOWN_RULE_FIELDS = {\n"
            "    'ruleSearch': {'name'},\n"
            "    'ruleExplore': {'name'},\n"
            "    'ruleBookInfo': {'name'},\n"
            "    'ruleToc': {'name'},\n"
            "    'ruleContent': {'content'},\n"
            "    'ruleReview': {'reviewUrl'},\n"
            "}\n")
        self.assertTrue(scan_field_tables(planted, "<植入>"),
                        "扫描器认不出手写的第二份字段表，那条系统性护栏在空转")

    def test_scanner_does_not_flag_label_maps(self):
        """反空转另一面：界面按段显示的中文名映射不是字段表，不许报。"""
        labels = (
            "SECTION_LABELS = {\n"
            "    'ruleSearch': '搜索',\n"
            "    'ruleExplore': '发现',\n"
            "    'ruleBookInfo': '详情',\n"
            "    'ruleToc': '目录',\n"
            "    'ruleContent': '正文',\n"
            "    'ruleReview': '书评',\n"
            "}\n")
        self.assertEqual(scan_field_tables(labels, "<中文名映射>"), [])


# --------------------------------------------------------------------------- #
# 3. 审计的 unknown_fields == 模型的逐段丢弃
# --------------------------------------------------------------------------- #
class FieldAuthorityTests(unittest.TestCase):
    def test_legado_declared_fields_are_never_reported_unknown(self):
        """修前实测这 12 条全被报成未知规则字段，其中 10 条是段评规则。"""
        for section, field in LEGADO_FIELDS_WRONGED_BEFORE:
            with self.subTest(段=section, 字段=field):
                self.assertIn(field, RULE_FIELDS[section],
                              f"模型段表里都没有 {section}.{field}，先去核对 Legado 规则类")
                report = _audit_one(one(section, field))
                self.assertNotIn(field, report.unknown_fields,
                                 f"{section}.{field} 是合法字段却被报成未知")
                self.assertTrue(report.clean, f"{section}.{field} 报 clean=False：{report}")

    def test_every_field_dropped_by_model_is_reported(self):
        """模型会丢的字段，审计必须逐段报出来——「放过」应当是 0。"""
        universe = frozenset().union(*RULE_FIELDS.values()) | frozenset(TOP_LEVEL_ONLY)
        checked = silent = 0
        for section in SECTION_NAMES:
            for field in sorted(universe - RULE_FIELDS[section]):
                checked += 1
                if kept_by_model(section, field):
                    continue
                if field not in _audit_one(one(section, field)).unknown_fields:
                    silent += 1
                    self.fail(f"{section}.{field} 模型会丢，审计却没报未知（放过）")
        self.assertGreater(checked, 200,
                           f"只对照了 {checked} 个组合，口径不对（应覆盖全部段×字段）")

    def test_top_level_fields_nested_in_a_rule_section_are_reported(self):
        """修前 18 个「顶层字段嵌进段里」的组合，unknown_fields 一个都没报。"""
        combos = 0
        for section in SECTION_NAMES:
            for field in TOP_LEVEL_ONLY:
                if field in RULE_FIELDS[section]:
                    continue
                combos += 1
                report = _audit_one(one(section, field))
                self.assertIn(field, report.unknown_fields,
                              f"{section}.{field} 不在任何段表里却没报未知")
                self.assertFalse(report.clean)
        self.assertEqual(combos, 18, f"组合数 {combos}，与实测的 18 不符")

    def test_a_real_unknown_field_is_still_caught(self):
        """反空转：字段表放宽后不能连真错都不报。"""
        report = _audit_one({"bookSourceUrl": "https://x",
                             "ruleBookInfo": {"name": ".n@text", "bookName": ".n@text"}})
        self.assertEqual(report.unknown_fields, ["bookName"])


# --------------------------------------------------------------------------- #
# 4. 一句缺口对一句文案
# --------------------------------------------------------------------------- #
class SuggestionLabelTests(unittest.TestCase):
    #: 每类缺口 → 该出现的一句话；同表其它类的措辞不许串味。
    LABELS = {
        "unknown_fields": "含 Legado 规则 schema 之外的字段",
        "missing_java_apis": "用到的 Java/对象 API 在本运行时未实现",
        "missing_object_apis": "用到的 Java/对象 API 在本运行时未实现",
        "unsupported_pseudos": "含未支持的 jsoup 伪类",
        "js_syntax_errors": "存在 JS 语法错误",
        "regex_errors": "存在正则语法错误",
        "rule_syntax_errors": "存在选择器语法错误",
    }

    def test_each_gap_class_gets_its_own_sentence(self):
        for bucket, phrase in self.LABELS.items():
            with self.subTest(类别=bucket):
                suggestions = _suggestions_for(bucket)
                self.assertTrue(any(phrase in s for s in suggestions),
                                f"{bucket} 的文案里没有「{phrase}」：{suggestions}")
                others = {p for k, p in self.LABELS.items() if k != bucket} - {phrase}
                if bucket in ("missing_java_apis", "missing_object_apis"):
                    # 这两类故意共用一句（处置一样：脚本报错）
                    others -= {self.LABELS["missing_java_apis"],
                               self.LABELS["missing_object_apis"]}
                hit = [p for p in others if any(p in s for s in suggestions)]
                self.assertFalse(hit, f"{bucket} 串到了别类的文案：{hit}")

    def test_clean_source_gets_no_sentence(self):
        """反空转的另一面：不能靠"什么都报"来让上一条通过。"""
        self.assertEqual(_suggestions_for("clean"), [])


def _suggestions_for(bucket: str) -> list[str]:
    """按缺口类别埋一条只命中该类的源，返回它的建议文案。"""
    plants = {
        "unknown_fields": {"ruleContent": {"content": "p@text", "bogus": "x"}},
        "missing_java_apis": {"ruleBookInfo": {"init": "@js:java.nope()"}},
        "missing_object_apis": {"ruleBookInfo": {"init": "@js:book.getNoSuch()"}},
        "unsupported_pseudos": {"ruleContent": {"content": "li:foo(a){{book.name}}@text"}},
        "js_syntax_errors": {"ruleBookInfo": {"init": "@js:function("}},
        "regex_errors": {"ruleContent": {"content": "p@text", "sourceRegex": "([a"}},
        "rule_syntax_errors": {"ruleContent": {"content": "text[foo"}},
        "clean": {"ruleContent": {"content": "p@text"}},
    }
    data = {"bookSourceUrl": "https://label.example"}
    data.update(plants[bucket])
    report = _audit_one(data)
    if bucket != "clean":
        assert getattr(report, bucket), f"埋点 {bucket} 没检出，文案对照无意义"
    return list(report.suggestions)


# --------------------------------------------------------------------------- #
# 5. 导入面的 warning 字段表与 AuditReport 对齐
# --------------------------------------------------------------------------- #
class ImportWarningSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def test_warning_names_are_all_real_report_fields(self):
        """修前这份表挂着 7 个已删类型的名字，getattr 永远是 None（白写）。"""
        real = {f.name for f in dataclasses.fields(AuditReport)}
        dead = [n for n in ois._AUDIT_WARNING_FIELDS if n not in real]  # noqa: SLF001
        self.assertFalse(dead, f"warning 字段表里有 AuditReport 没有的名字：{dead}")

    def test_every_gap_field_is_surfaced_as_warning(self):
        """修前真实字段 rule_syntax_errors 没列进来 → 选择器语法错在导入面一条不出。"""
        gaps = {f.name for f in dataclasses.fields(AuditReport)
                if f.name not in ("source_id", "suggestions")}
        missing = gaps - set(ois._AUDIT_WARNING_FIELDS)  # noqa: SLF001
        self.assertFalse(missing, f"这些缺口字段不会变成导入 warning：{sorted(missing)}")

    async def test_selector_error_reaches_the_import_warning(self):
        """端到端：只命中选择器语法错的源，导入结果里必须看得见。"""
        class _ReportOnlyAudit:
            """替身只负责返回一份真 AuditReport，验证的是字段表而不是审计本身。"""

            def __init__(self) -> None:
                self.calls = 0

            async def audit_source(self, source_json) -> AuditReport:
                self.calls += 1
                return _audit_one(dict(source_json))

        audit = _ReportOnlyAudit()
        svc = ois.OnlineImportService(audit_service=audit)
        warnings = await svc._audit_sources([  # noqa: SLF001
            {"bookSourceUrl": "https://sel.example", "bookSourceName": "坏选择器",
             "ruleContent": {"content": "text[foo"}}])
        self.assertEqual(audit.calls, 1, "审计压根没被调用，warning 是凭空来的")
        self.assertTrue(any("rule_syntax_errors" in w and "Malformed attribute selector" in w
                           for w in warnings),
                        f"选择器语法错没进导入 warning：{warnings}")


# --------------------------------------------------------------------------- #
# 6. 段清单（"遍历哪几段"）也只有一份定义
# --------------------------------------------------------------------------- #:
#: 全库唯一一处**故意**手写的段清单：QuickJS 里 `source.equal()` 的比较范围。它的五段
#: 是保真结果不是漂移——Legado ``BookSource.kt`` 的 ``fun equal`` 就只比这五个 getter，
#: 不含 ruleReview；改成 RULE_FIELDS 等于改"两份书源算不算同一份"的语义。
#: 出处：other_history/legado-main/.../data/entities/BookSource.kt（``fun equal``）。
SECTION_LIST_ALLOWLIST = frozenset({"app/services/engine/legado_js.py"})


def scan_section_lists(source: str, name: str) -> list[str]:
    """找出**手写**的规则段清单：列表/元组/集合字面量，或字符串里的段名数组。

    字段表只有一份还不够——同一批段名还会被抄成"这个循环要遍历哪几段"。那种清单
    漏一段是静默失效，不报错也不红：本轮实测抓到两处，``check_service._source_uses_js``
    只遍历手写五段（少 ruleReview，JS 闸门对段评规则里的 @js: 视而不见），
    ``CodeEditorController._pick_rule`` 手写全六段（加段时会取不到那一格的规则，
    「试跑当前规则」静默回空）。

    阈值取最严的 2（一段清单至少也该有两个段名），实测整个 app/ 在这个阈值下只有
    ``legado_js.py`` 那一处字符串命中——也就是说 app/ 里 Python 字面量形式的手写段
    清单已经归零，任何新增都会红。
    """
    findings = []
    try:
        tree = ast.parse(source)
    except SyntaxError:  # 交给调用侧断言"没有文件解析失败"
        return findings
    sections = set(SECTION_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            names = {e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if len(names & sections) >= 2:
                findings.append(f"{name}:{node.lineno} 手写了规则段清单（{len(names & sections)} 段）")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and len(node.value) > 50:
            hits = sum(1 for s in SECTION_NAMES
                       if f"'{s}'" in node.value or f'"{s}"' in node.value)
            if hits >= 2:
                findings.append(f"{name}:{node.lineno} 字符串里手写了规则段清单（{hits} 段）")
    return findings


class SectionListSingleDefinitionTests(unittest.TestCase):
    def test_production_code_has_no_hand_written_section_list(self):
        """段清单不许再手写；唯一的例外是 Legado equal 的五段，白名单里点名。"""
        findings = []
        for path in sorted(APP.rglob("*.py")):
            findings += scan_section_lists(
                path.read_text(encoding="utf-8-sig"),
                path.relative_to(APP.parent).as_posix())
        unexpected = [f for f in findings
                      if f.split(":")[0] not in SECTION_LIST_ALLOWLIST]
        self.assertFalse(unexpected,
                         "规则段清单又被手写成第二份（漏一段就是静默失效）：\n"
                         + "\n".join(unexpected))
        self.assertEqual(len(findings), 1,
                         f"白名单那处手写段清单不见了——是保真清单被删了，还是扫描器空了？"
                         f"实际：{findings}")

    def test_scanner_flags_a_planted_section_list(self):
        """反空转：植入一份手写五段必须扫出来（就是本轮修掉的那个形状）。"""
        planted = ("def _needs_js(source):\n"
                   "    for name in ('ruleSearch', 'ruleExplore', 'ruleBookInfo',\n"
                   "                 'ruleToc', 'ruleContent'):\n"
                   "        pass\n")
        self.assertTrue(scan_section_lists(planted, "<植入>"),
                        "扫描器认不出手写的五段清单，那条系统性护栏在空转")

    def test_scanner_does_not_flag_looping_the_model_table(self):
        """反空转另一面：`for name in RULE_FIELDS` 与中文名映射都不许报。"""
        good = ("from app.models.book_source import RULE_FIELDS\n"
                "LABELS = {'ruleSearch': '搜索', 'ruleReview': '书评'}\n"
                "def walk(source):\n"
                "    return [getattr(source, name, {}) for name in RULE_FIELDS]\n")
        self.assertEqual(scan_section_lists(good, "<正确写法>"), [])

    def test_editor_reaches_rules_in_every_model_section(self):
        """试跑取值覆盖每一段：段清单退回手写时，缺的那一格会静默回空字符串。"""
        for section in SECTION_NAMES:
            with self.subTest(段=section):
                picked = CodeEditorController._pick_rule(  # noqa: SLF001
                    {section: {"contentRule": "@js:1+1"}}, "contentRule")
                self.assertEqual(picked, "@js:1+1",
                                 f"{section} 里的规则取不到：段清单没覆盖这一段")

    def test_editor_group_labels_cover_the_model_sections(self):
        """段少了中文名 → 那一段的规则在编辑器下拉里根本不出现，用户看不见。"""
        self.assertEqual(list(_GROUP_LABELS), list(SECTION_NAMES),
                         "规则组中文名映射与模型段表不再同集合同序（下拉顺序即这个顺序）")


if __name__ == "__main__":
    unittest.main()
