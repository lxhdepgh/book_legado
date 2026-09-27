# -*- coding: utf-8 -*-
"""[M2] 桌面端重构 M2 规则语义修复组的针对性回归测试。

每项至少一个用例；纯逻辑测试，直接 import 引擎模块，不依赖 Flet / 网络。
运行：python -m pytest tests/test_rule_semantics_m2.py -q
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, ".")

from app.services.engine import java_regex as jr
from app.services.engine.legado_rule_parser import (
    json_values, parse_markup, xpath_values,
)

# reading_engine 里的常量单独 import
from app.services.engine.reading_engine import _LEGADO_WORD_COUNT_RE


class TestM2_2_3_JavaPossessiveAtomic(unittest.TestCase):
    """[M2-2.3] (?> 原子组 / x*+ x++ x?+ 所有格走 regex 后端，普通 pattern 仍走 re。"""

    def test_atomic_group_compiles_and_matches(self):
        # 原子组提交后不回溯：(?>a+). 在 "aaa" 上 a+ 吃光后 . 无字符可用、不回溯 → 不命中；
        # 普通贪婪 a+. 则会回溯一格命中。
        self.assertIsNone(jr.search(r"(?>a+).", "aaa"))
        self.assertIsNotNone(jr.search(r"a+.", "aaa"))
        self.assertTrue(jr.search(r"(?>a+)b", "aaab"))

    def test_possessive_quantifiers(self):
        # \d++ 贪婪一次吃掉全部数字，不回溯
        hits = [m.group(0) for m in jr.finditer(r"\d++", "a12b34")]
        self.assertEqual(hits, ["12", "34"])
        self.assertTrue(jr.search(r"x*+", "xxx"))
        self.assertTrue(jr.search(r"x?+", "x"))

    def test_detection_skips_quoted_literals(self):
        # \Q..\E 里出现的假 (?> 不应触发 regex 后端
        self.assertFalse(jr._needs_regex_backend(r"a\Q(?>b\E"))
        self.assertTrue(jr._needs_regex_backend(r"(?>ab)"))
        self.assertTrue(jr._needs_regex_backend(r"\d++"))

    def test_normal_pattern_still_re_backend(self):
        p = jr.compile_pattern(r"\w+")
        self.assertEqual(type(p).__name__, "Pattern")
        self.assertEqual(jr.sub(r"\s+", "_", "a b  c"), "a_b_c")

    def test_replacement_dollar_unified(self):
        # Java 形式 $1 经 translate_replacement 在两后端统一
        self.assertEqual(jr.sub(r"(\w)(\d+)", r"[$1]", "a1"), "[a]")


class TestM2_2_4_JsonPathPredicate(unittest.TestCase):
    """[M2-2.4] 谓词数值强转 / =~ 正则 / 缺失键取空。"""

    def setUp(self):
        self.doc = {"items": [
            {"id": 1, "name": "alpha", "count": "3"},
            {"id": 2, "name": "beta", "count": 5},
            {"id": 3, "name": "gamma", "count": 10},
        ]}

    def test_numeric_coerce_string_eq_number(self):
        # "3" == 3 应命中（数值强转）
        out = json_values(self.doc, "$.items[?(@.count == 3)].name")
        self.assertEqual(out, ["alpha"])

    def test_numeric_coerce_order(self):
        out = json_values(self.doc, "$.items[?(@.count >= 4)].name")
        self.assertEqual(out, ["beta", "gamma"])

    def test_regex_literal(self):
        out = json_values(self.doc, r"$.items[?(@.name =~ /^al/)].name")
        self.assertEqual(out, ["alpha"])

    def test_missing_key_yields_empty(self):
        out = json_values(self.doc, "$.items[?(@.missing == 1)].name")
        self.assertEqual(out, [])

    def test_string_literal(self):
        out = json_values(self.doc, "$.items[?(@.name == 'beta')].name")
        self.assertEqual(out, ["beta"])


class TestM2_2_12_AbsAttrUrljoin(unittest.TestCase):
    """[M2-2.12] @abs:<attr> 一律按 base_url 解析，含非标属性名裸相对路径。"""

    BASE = "https://example.com/novel/ch1.html"

    def _extract(self, rule, html):
        from app.services.engine.rule_engine import extract_value
        node = parse_markup(html, "text/html")
        return extract_value(node, rule, "html", base_url=self.BASE)

    def test_abs_nonstd_attr_bare_relative(self):
        # 缺口用例：@abs:data-x 取裸相对路径 book/ch1 必须补全
        out = self._extract("@@div@abs:data-x", '<div data-x="book/ch1">x</div>')
        self.assertEqual(out, "https://example.com/novel/book/ch1")

    def test_abs_href_relative(self):
        out = self._extract("@@div@abs:href", '<div href="book/ch1">x</div>')
        self.assertEqual(out, "https://example.com/novel/book/ch1")

    def test_abs_already_absolute_unchanged(self):
        out = self._extract("@@div@abs:data-x",
                            '<div data-x="https://cdn.com/a/b.png">x</div>')
        self.assertEqual(out, "https://cdn.com/a/b.png")

    def test_abs_protocol_relative(self):
        out = self._extract("@@div@abs:data-x",
                            '<div data-x="//cdn.com/a/b.png">x</div>')
        self.assertEqual(out, "https://cdn.com/a/b.png")

    def test_plain_href_still_resolves(self):
        out = self._extract("@@div@href", '<div href="//cdn.com/a/b.png">x</div>')
        self.assertEqual(out, "https://cdn.com/a/b.png")


class TestM2_2_1_XPathDialect(unittest.TestCase):
    """[M2-2.1] 自定义函数与方言翻译表锁行为 + 缺口登记常量存在。"""

    def setUp(self):
        self.root = parse_markup(
            "<div class='list'><ul><li><a href='/b/1'>书一</a>"
            "<span class='u'>2024-01-01 12字</span></li></ul></div>", "text/html")

    def test_substring_ex(self):
        self.assertEqual(xpath_values(self.root, "substring-ex('hello world', 0, 5)"),
                         ["hello"])

    def test_substring_after_last(self):
        self.assertEqual(
            xpath_values(self.root, "substring-after-last('2024-01-01', '-')"),
            ["01"])

    def test_attribute_selection(self):
        self.assertEqual(xpath_values(self.root, "//li/a/@href"), ["/b/1"])

    def test_gap_registry_registered(self):
        from app.services.compat_audit import UNTRANSLATABLE_XPATH_CONSTRUCTS
        self.assertIsInstance(UNTRANSLATABLE_XPATH_CONSTRUCTS, set)
        self.assertTrue(len(UNTRANSLATABLE_XPATH_CONSTRUCTS) >= 1)


class TestM2_2_2_JsoupSerialization(unittest.TestCase):
    """[M2-2.2] text/textNodes 分支与空白（含 nbsp 保留）逐字节对齐 jsoup 1.16.2。"""

    def test_text_collapses_ascii_whitespace_keeps_nbsp(self):
        from app.services.engine.legado_rule_parser import evaluate
        root = parse_markup(
            "<div id='x'> A\t<span>B</span>C<div>D\n E</div>"
            "<br>F&nbsp;G</div>", "html.parser")
        self.assertEqual(evaluate(root, "#x@text", "html"), ["A BC D E F\u00a0G"])

    def test_textnodes_returns_direct_text_nodes(self):
        from app.services.engine.legado_rule_parser import evaluate
        root = parse_markup("<p>  A  <b>x</b>\u00a0B\u00a0 </p>", "html.parser")
        self.assertEqual(evaluate(root, "p@textNodes", "html"), ["A\n\u00a0B\u00a0"])


class TestM2_2_8_TocCountWords(unittest.TestCase):
    """[M2-2.8] updateTime 尾部"字数"抽进 wordCount，偏好默认开。"""

    def test_regex_split(self):
        s = "2024-01-01 12字"
        m = _LEGADO_WORD_COUNT_RE.search(s)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "12字")
        self.assertEqual(s.replace(m.group(0), "", 1).strip(), "2024-01-01")

    def test_word_count_prefix(self):
        s = "字数：32400字"
        m = _LEGADO_WORD_COUNT_RE.search(s)
        self.assertEqual(m.group(1).strip(), "32400字")

    def test_no_word_count_tail(self):
        self.assertIsNone(_LEGADO_WORD_COUNT_RE.search("2024-01-01"))

    def test_pref_default_on(self):
        from app.services.engine._prefs import toc_count_words
        self.assertTrue(toc_count_words())

    def test_chapter_item_has_word_count_field(self):
        from app.services.engine.rule_engine import ChapterItem
        item = ChapterItem(name="n", url="u", word_count="3240字")
        self.assertEqual(item.word_count, "3240字")


class TestM2_2_9_PerSourceVariableBag(unittest.TestCase):
    """[M2-2.9] 跨请求 put/get 普通变量，引擎重建后仍在。"""

    def test_cross_rebuild_variable_survives(self):
        from app.services.engine.rule_engine import ReaderEngine

        async def scenario():
            src = SimpleNamespace(bookSourceUrl="https://bag.example.com/book")
            e1 = ReaderEngine()
            e1._bind_source_bag(src)
            e1._put_variable("mytoken", "tok-123")
            # 模拟引擎被外壳池淘汰重建
            e2 = ReaderEngine()
            e2._bind_source_bag(src)
            return e2._get_variable("mytoken")

        self.assertEqual(asyncio.run(scenario()), "tok-123")

    def test_isolated_between_sources(self):
        from app.services.engine.rule_engine import ReaderEngine

        async def scenario():
            a = SimpleNamespace(bookSourceUrl="https://a.example.com/s")
            b = SimpleNamespace(bookSourceUrl="https://b.example.com/s")
            ea = ReaderEngine(); ea._bind_source_bag(a); ea._put_variable("k", "A")
            eb = ReaderEngine(); eb._bind_source_bag(b)
            return eb._get_variable("k")

        self.assertEqual(asyncio.run(scenario()), "")


if __name__ == "__main__":
    unittest.main()
