import asyncio
import aiohttp
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey
import base64
import hashlib
import json
import gzip
import hmac
import math
import re
import ssl
import threading
import time
import warnings
import zipfile
import zlib
from io import BytesIO
from email.message import Message
from urllib.error import HTTPError, URLError
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from yarl import URL

from bs4 import BeautifulSoup
from PIL import Image

from app.models.book_source import BookSource
from app.services.legado_rule_parser import (JavaDateValue, _jsoup_html_markup,
                                              _selection_indices,
                                              evaluate, jsoup_values,
                                              parse_markup,
                                              select_jsoup,
                                              split_rule_expression)
from app.services.compat_audit import SUPPORTED_JAVA_APIS, SUPPORTED_OBJECT_APIS
from app.services.icu_charset import detect_charset
from app.services.legado_js import (JsBridgeError, JsContext, LegadoJsRuntime,
                                    classify_js_bridge_error)
from app.services.request_compat import (encode_form_fields,
                                         encode_url_query_fields, escape_form,
                                         escape_url_query, legado_escape,
                                         normalize_url_option)
from app.services.reading_engine import (ContentEmptyError, LegadoReadingEngine,
                                         TocEmptyError, _legado_truthy,
                                         _apply_replace_regex,
                                         _format_content_page,
                                         chapter_display_title,
                                         parse_content_blocks,
                                         process_book_content,
                                         render_use_html_spans)
from app.services.source_service import SourceService
from app.services.source_rule import compile_rule_program
from app.services.custom_url import CustomUrl
from app.services.java_regex import search as java_regex_search, sub as java_regex_sub
from app.services.public_suffix import effective_tld_plus_one
from app.services.rule_engine import (BookItem, ChapterItem, ReaderEngine,
                                      RequestConnectionError,
                                      RequestTimeoutError,
                                      _format_book_intro,
                                      display_intro,
                                      html_to_text,
                                      merge_search_items,
                                      parse_explore_categories,
                                      resolve_explore_categories, select_nodes)
from app.services.rule_engine import extract_value
from app.services.search_model import SearchBookStore, SearchModel
from app.services.reading_cache import ReadingCache
from app.services.legado_url import parse_request_config, parse_search_url


# The 听友M (tingyou.fm) jsLib decrypts XChaCha20-Poly1305 payloads by running
# HChaCha20 in JS and handing the result to JCE ChaCha20-Poly1305.
TING_KEY = "ea9d9d4f9a983fe6f6382f29c7b46b8d6dc47abc6da36662e6ddff8c78902f65"
TING_JS_LIB = """
function ting_hchacha_run(state){
  function qr(s,ai,bi,ci,di){
    s[ai]=(s[ai]+s[bi])>>>0; s[di]=(((s[di]^s[ai])<<16)|((s[di]^s[ai])>>>16))>>>0;
    s[ci]=(s[ci]+s[di])>>>0; s[bi]=(((s[bi]^s[ci])<<12)|((s[bi]^s[ci])>>>20))>>>0;
    s[ai]=(s[ai]+s[bi])>>>0; s[di]=(((s[di]^s[ai])<<8)|((s[di]^s[ai])>>>24))>>>0;
    s[ci]=(s[ci]+s[di])>>>0; s[bi]=(((s[bi]^s[ci])<<7)|((s[bi]^s[ci])>>>25))>>>0;
  }
  for(var i=0;i<10;i++){
    qr(state,0,4,8,12); qr(state,1,5,9,13); qr(state,2,6,10,14); qr(state,3,7,11,15);
    qr(state,0,5,10,15); qr(state,1,6,11,12); qr(state,2,7,8,13); qr(state,3,4,9,14);
  }
}
function ting_hchacha(keyBytes, nonce16){
  var s=[
    0x61707865,0x3320646e,0x79622d32,0x6b206574,
    keyBytes[0]|keyBytes[1]<<8|keyBytes[2]<<16|keyBytes[3]<<24,
    keyBytes[4]|keyBytes[5]<<8|keyBytes[6]<<16|keyBytes[7]<<24,
    keyBytes[8]|keyBytes[9]<<8|keyBytes[10]<<16|keyBytes[11]<<24,
    keyBytes[12]|keyBytes[13]<<8|keyBytes[14]<<16|keyBytes[15]<<24,
    keyBytes[16]|keyBytes[17]<<8|keyBytes[18]<<16|keyBytes[19]<<24,
    keyBytes[20]|keyBytes[21]<<8|keyBytes[22]<<16|keyBytes[23]<<24,
    keyBytes[24]|keyBytes[25]<<8|keyBytes[26]<<16|keyBytes[27]<<24,
    keyBytes[28]|keyBytes[29]<<8|keyBytes[30]<<16|keyBytes[31]<<24,
    nonce16[0]|nonce16[1]<<8|nonce16[2]<<16|nonce16[3]<<24,
    nonce16[4]|nonce16[5]<<8|nonce16[6]<<16|nonce16[7]<<24,
    nonce16[8]|nonce16[9]<<8|nonce16[10]<<16|nonce16[11]<<24,
    nonce16[12]|nonce16[13]<<8|nonce16[14]<<16|nonce16[15]<<24,
  ];
  ting_hchacha_run(s);
  var out=new Uint8Array(32);
  var ws=[s[0],s[1],s[2],s[3],s[12],s[13],s[14],s[15]];
  for(var i=0;i<8;i++){var w=ws[i];out[i*4]=w&255;out[i*4+1]=(w>>>8)&255;
    out[i*4+2]=(w>>>16)&255;out[i*4+3]=(w>>>24)&255;}
  return out;
}
function __hex2bytes(hex){var len=hex.length;var out=new Uint8Array(len>>1);
  for(var i=0;i<out.length;i++){out[i]=parseInt(hex.slice(2*i,2*i+2),16);}return out;}
function __toByteArr(u8){var ba=new Packages.java.io.ByteArrayOutputStream();
  for(var i=0;i<u8.length;i++){ba.write(u8[i]&255);}return ba.toByteArray();}
function ting_decryptPayload(hexPayload, hexKey){
  var k=__hex2bytes(hexKey);
  var buf=__hex2bytes(hexPayload);
  var nonce=new Uint8Array(24);
  for(var i=0;i<24;i++){nonce[i]=buf[i+1];}
  var ctArea=buf.subarray(25);
  var ct=new Uint8Array(ctArea.length);
  for(var j=0;j<ctArea.length;j++){ct[j]=ctArea[j];}
  var ver=buf[0];
  if(ver===2){
    var rev=new Uint8Array(ct.length);
    for(var r=0;r<ct.length;r++){rev[r]=ct[ct.length-1-r];}
    ct=rev;
  }
  var subkey=ting_hchacha(k, nonce.subarray(0,16));
  var nonce12=new Uint8Array(12);
  for(var n4=0;n4<8;n4++){nonce12[4+n4]=nonce[16+n4];}
  var cipher=Packages.javax.crypto.Cipher.getInstance("ChaCha20-Poly1305");
  var sk=new Packages.javax.crypto.spec.SecretKeySpec(__toByteArr(subkey), "ChaCha20");
  var gcm=new Packages.javax.crypto.spec.GCMParameterSpec(128, __toByteArr(nonce12));
  cipher.init(javax.crypto.Cipher.DECRYPT_MODE, sk, gcm);
  var pb=cipher.doFinal(__toByteArr(ct));
  return new java.lang.String(pb, "UTF-8");
}
"""


def xchacha20_payload(plaintext: str, version: int = 1) -> str:
    """The ``version || nonce24 || ciphertext+tag`` blob the jsLib above reads."""
    from Crypto.Cipher import ChaCha20_Poly1305
    nonce = bytes(range(24))
    ciphertext, tag = ChaCha20_Poly1305.new(
        key=bytes.fromhex(TING_KEY), nonce=nonce).encrypt_and_digest(
        plaintext.encode())
    body = ciphertext + tag
    return (bytes([version]) + nonce +
            (body[::-1] if version == 2 else body)).hex()


class SearchModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_model_paging_bounded_workers_merge_and_persist(self):
        active = 0
        maximum = 0
        calls = []

        async def search_one(source, key, page):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append((source, key, page))
            await asyncio.sleep(0.01)
            active -= 1
            return [BookItem(
                "Key", author="A", url=f"url-{source}-{page}",
                origin=f"source-{source}", origin_order=int(source))]

        with TemporaryDirectory() as directory:
            store = SearchBookStore(Path(directory) / "search.sqlite3")
            updates = []
            lifecycle = []
            model = SearchModel(
                search_one, thread_count=2, store=store,
                on_update=lambda books: updates.append(list(books)),
                on_start=lambda: lifecycle.append("start"),
                on_finish=lambda empty: lifecycle.append(("finish", empty)))
            first = await model.search(10, "Key", [1, 2, 3, 4])
            second = await model.search(10, "ignored")

            self.assertEqual(maximum, 2)
            self.assertEqual([page for _, _, page in calls], [1] * 4 + [2] * 4)
            self.assertEqual(model.page, 2)
            self.assertEqual(first[0].origin_set(),
                             ["source-1", "source-2", "source-3", "source-4"])
            self.assertEqual(second[0].origin_set(),
                             ["source-1", "source-2", "source-3", "source-4"])
            self.assertIsNotNone(store.get("url-4-2"))
            self.assertEqual(store.first_by_name_author("Key", "A").origin_order, 1)
            self.assertGreaterEqual(len(updates), 9)
            self.assertEqual(lifecycle,
                             ["start", ("finish", False),
                              "start", ("finish", False)])

    async def test_new_search_id_cancels_old_generation(self):
        started = asyncio.Event()

        async def search_one(source, key, page):
            if key == "old":
                started.set()
                await asyncio.sleep(10)
            return [BookItem(key, url=f"{key}-{source}", origin=str(source))]

        model = SearchModel(search_one, thread_count=1)
        old = asyncio.create_task(model.search(1, "old", [1]))
        await started.wait()
        fresh = await model.search(2, "new", [2])
        with self.assertRaises(asyncio.CancelledError):
            await old
        self.assertEqual([(item.name, item.url) for item in fresh],
                         [("new", "new-2")])
        self.assertEqual(model.search_id, 2)


class QuickJsSchedulingTests(unittest.IsolatedAsyncioTestCase):
    """一个 QuickJS worker 是硬约束，排队顺序和取消必须是可控的。"""

    BUSY = "@js:(function(){var t=Date.now();while(Date.now()-t<400){};return 'x'})()"

    @staticmethod
    def _context():
        return JsContext(source=None)

    async def test_a_reading_rule_does_not_wait_for_the_queued_sweep(self):
        reader = LegadoJsRuntime()
        sweep = LegadoJsRuntime()
        sweep.background = True
        finished = []

        def run_source(index):
            sweep.evaluate(self.BUSY, self._context())
            finished.append(index)

        sources = [asyncio.create_task(asyncio.to_thread(run_source, index))
                   for index in range(3)]
        await asyncio.sleep(0.1)
        await reader.evaluate_async(self.BUSY, self._context())

        self.assertLessEqual(
            len(finished), 1,
            "阅读页的求值必须插到还排在队列里的扫描工作前面")
        await asyncio.gather(*sources)

    async def test_a_cancelled_evaluation_never_reaches_the_worker(self):
        runtime = LegadoJsRuntime()
        started = time.perf_counter()
        blocker = asyncio.create_task(runtime.evaluate_async(
            self.BUSY, self._context()))
        await asyncio.sleep(0.15)
        dropped = asyncio.create_task(runtime.evaluate_async(
            self.BUSY, self._context()))
        follower = asyncio.create_task(runtime.evaluate_async(
            self.BUSY, self._context()))
        await asyncio.sleep(0.05)
        dropped.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await dropped
        await asyncio.gather(blocker, follower)

        # 三条 0.4s 的任务只该付出两条的时间：被取消的那条不能占 worker。
        self.assertLess(time.perf_counter() - started, 1.1)

    async def test_async_evaluation_still_sees_the_python_bridge(self):
        runtime = LegadoJsRuntime()
        context = self._context()
        script = '@js:(function(){return java.base64Decode(java.base64Encode("abc"))})()'
        self.assertEqual(await runtime.evaluate_async(script, context), "abc")
        self.assertEqual(runtime.evaluate(script, context), "abc")

    async def test_deep_recursion_raises_instead_of_killing_the_process(self):
        runtime = LegadoJsRuntime()
        with self.assertRaises(Exception) as caught:
            runtime.evaluate(
                "@js:(function(){function f(){return f()+1}; return f()}())",
                self._context())
        self.assertIn("stack overflow", str(caught.exception))
        self.assertEqual(runtime.evaluate("@js:2+2", self._context()), 4)


class RuleParserTests(unittest.TestCase):
    def test_content_page_formats_html_images_before_global_replace(self):
        formatted = _format_content_page(
            '<p>A&nbsp;B</p><!--x--><i>C</i>'
            '<img src="fallback.jpg" data-src="../image.jpg">'
            '<img src="asset.png, {\"headers\":{\"X\":\"1\"}}">',
            "https://example.test/book/chapter/1")
        self.assertEqual(
            formatted,
            '　　A B\n　　C<img src="https://example.test/book/image.jpg">'
            '<img src="https://example.test/book/chapter/asset.png,'
            '{\"headers\":{\"X\":\"1\"}}">')

    def test_content_page_keeps_usehtml_spans_verbatim(self):
        formatted = _format_content_page(
            '<p>普通 <usehtml><span style="color:red">红色&nbsp;文字</span>'
            '<i>斜体</i></usehtml>尾巴</p>', "https://example.test/ch/1")
        self.assertEqual(
            formatted,
            '　　普通 <usehtml><span style="color:red">红色&nbsp;文字</span>'
            '<i>斜体</i></usehtml>尾巴')

    def test_book_intro_with_raw_render_markers_bypasses_formatter(self):
        for marker in ("<usehtml>", "<md>", "<useweb>"):
            raw = f"  {marker}<b>简介</b>"
            self.assertEqual(_format_book_intro(raw), f"{marker}<b>简介</b>")
        self.assertEqual(_format_book_intro("<p>简介</p><br>正文"),
                         "　　简介\n　　正文")

    def test_display_intro_names_the_renderer_and_unwraps_its_payload(self):
        for marker, kind in (("<usehtml>", "html"), ("<md>", "md"),
                             ("<useweb>", "web")):
            closing = f"</{marker[1:]}"
            self.assertEqual(
                display_intro(f"  {marker}\n<b>简介</b>\n{closing}"),
                (kind, "\n<b>简介</b>\n"))
            # substring(prefix, lastIndexOf("<")): a payload keeps its own markup.
            self.assertEqual(display_intro(f"{marker}a <i>b</i>{closing}"),
                             (kind, "a <i>b</i>"))
        self.assertEqual(display_intro("<p>简介</p>"), ("text", "　　简介"))
        self.assertEqual(display_intro(""), ("text", ""))
        # A marker without its closing tag cannot be unwrapped, so it stays text.
        self.assertEqual(display_intro("<usehtml>残缺"), ("text", "<usehtml>残缺"))

    def test_html_to_text_drops_what_the_html_renderer_never_paints(self):
        self.assertEqual(
            html_to_text("<style>p{color:red}</style>"
                         "<script>var a=1;</script><p>正文&nbsp;一</p>"),
            "　　正文 一")
        # The card a <useweb> intro carries, reduced to what a reader would see.
        kind, payload = display_intro(
            "<useweb>\n<style>.card{color:red}</style>\n"
            '<div class="card"><div class="title">七侠镇</div>'
            '<div class="intro-text">正文简介</div></div>\n</useweb>')
        self.assertEqual(kind, "web")
        self.assertEqual(html_to_text(payload), "　　七侠镇\n　　正文简介")

    def test_use_html_spans_in_content_render_as_text(self):
        page = _format_content_page(
            '<p>前 <usehtml><style>x{}</style><b>加粗</b>尾</usehtml>后</p>',
            "https://example.test/c/1")
        self.assertIn("<usehtml>", page)
        self.assertEqual(render_use_html_spans(page), "　　前 加粗尾后")
        # Content without a span is already formatted; the pass must not touch it.
        self.assertEqual(render_use_html_spans("　　普通正文"), "　　普通正文")

    def test_search_model_merge_origin_precision_and_rank_order(self):
        existing = [
            BookItem("Key", author="A", origin="s1", url="u1"),
            BookItem("Key Story", author="B", origin="s1", url="u2"),
            BookItem("Other", author="C", origin="s1", url="u3"),
        ]
        incoming = [
            BookItem("Key", author="A", origin="s2", url="u4"),
            BookItem("Key", author="D", origin="s3", url="u5"),
            BookItem("Key Story", author="B", origin="s2", url="u6"),
            BookItem("Noise", author="Z", origin="s4", url="u7"),
        ]
        merged = merge_search_items(existing, incoming, "Key")
        self.assertEqual([(item.name, item.author, item.origin_set()) for item in merged], [
            ("Key", "A", ["s1", "s2"]), ("Key", "D", ["s3"]),
            ("Key Story", "B", ["s1", "s2"]), ("Other", "C", ["s1"]),
            ("Noise", "Z", ["s4"]),
        ])
        precise = merge_search_items([], incoming, "Key", precision=True)
        self.assertEqual([(item.name, item.author) for item in precise],
                         [("Key", "A"), ("Key", "D"), ("Key Story", "B")])

    def test_content_processor_title_duplicate_and_purify_order(self):
        chapter = ChapterItem("[广告] 第一章", "https://source/chapter/1")
        book = BookItem("书名", read_config={
            "useReplaceRule": True,
            "titleReplaceRules": [{
                "pattern": r"^\[广告\]\s*", "replacement": "",
                "isRegex": True, "isEnabled": True}],
            "contentReplaceRules": [{
                "pattern": "广告词", "replacement": "正文",
                "isRegex": False, "isEnabled": True}],
            "paragraphIndent": "　　",
        })
        content, title = process_book_content(
            "书名 [广告] 第一章\n  第一段  \n广告词", book, chapter)
        self.assertEqual(title, "第一章")
        self.assertEqual(content, "　　第一段\n　　正文")
        self.assertEqual(chapter_display_title(chapter, [{
            "pattern": ".*", "replacement": "", "isRegex": True}]),
                         "[广告] 第一章")

    def test_get_element_select_remove_mutates_parent_html(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")
        html = ('<div class="read_chapterDetail"><p>drop</p>'
                '<script>drop()</script><span>keep</span></div>')
        try:
            value = runtime.evaluate(
                '@js:var el=java.getElement(".read_chapterDetail");'
                'el.select("p,script").remove();el.html()',
                JsContext(source=source, result=html,
                          base_url=source.bookSourceUrl),
            )
        finally:
            runtime.close()

        self.assertNotIn("drop", value)
        self.assertIn("<span>keep</span>", value)

    def test_html_terminal_removes_script_and_style_from_shared_dom(self):
        root = BeautifulSoup(
            "<main><p>text</p><script>bad()</script><style>.x{}</style></main>",
            "html.parser")
        rendered = evaluate(root, "main@html", "html")
        self.assertEqual(rendered, ["<main>\n <p>text</p>\n</main>"])
        self.assertEqual(root.select("script, style"), [])

    def test_jsoup_abs_attribute_uses_response_base_url(self):
        root = BeautifulSoup('<a href="../chapter/1">one</a>', "html.parser")
        self.assertEqual(
            extract_value(root, "a@abs:href", "html",
                          "https://example.com/book/index.html"),
            "https://example.com/chapter/1")

    def test_jsoup_indices_are_applied_per_parent_element(self):
        root = BeautifulSoup(
            "<section><ul><li>A0</li><li>A1</li></ul>"
            "<ul><li>B0</li><li>B1</li></ul></section>", "html.parser")
        self.assertEqual(evaluate(root, "ul@li.0@text", "html"), ["A0", "B0"])
        self.assertEqual(evaluate(root, "ul@li!0@text", "html"), ["A1", "B1"])
        self.assertEqual(evaluate(root, "ul@li[-1:0]@text", "html"),
                         ["A1", "A0", "B1", "B0"])

    def test_jsonpath_rule_dispatches_even_when_body_sniffed_as_html(self):
        body = json.dumps({"data": {"list": [{"name": "A"}, {"name": "B"}]}},
                          ensure_ascii=False)
        root = parse_markup(body)
        # Verified against Legado's own AnalyzeByJSonPath (json-path 2.10 +
        # JsonSmart): a named step does NOT traverse arrays implicitly.
        self.assertEqual(evaluate(root, "$.data.list.name", "html"), [])
        self.assertEqual(evaluate(root, "$.data.list[*].name", "html"), ["A", "B"])
        self.assertEqual(evaluate(root, "$.data.list[0].name", "html"), ["A"])
        self.assertEqual(evaluate(root, "@json:$.data.list", "html"),
                         [{"name": "A"}, {"name": "B"}])

    def test_json_payload_root_dispatches_without_dollar_prefix(self):
        payload = {"data": {"list": [{"name": "A"}]}}
        self.assertEqual(evaluate(payload, "data.list[*].name", "html"), ["A"])

    def test_json_filter_combinators_survive_html_kind_split(self):
        body = json.dumps({"data": {"list": [{"name": "A", "ok": True},
                                             {"name": "B", "ok": False}]}},
                          ensure_ascii=False)
        root = parse_markup(body)
        self.assertEqual(
            evaluate(root, "$.data.list[?@.ok==true].name", "html"), ["A"])

    def test_html_root_with_non_json_text_still_uses_jsoup(self):
        root = parse_markup("<main><p>hi</p></main>")
        self.assertEqual(evaluate(root, "main@p@text", "html"), ["hi"])
        self.assertEqual(evaluate(root, "$.data.list", "html"), [])

    def test_jsoup_bare_colon_selects_standalone_indexes_not_range(self):
        root = BeautifulSoup(
            "<div><p>0</p><p>1</p><p>2</p><p>3</p><p>4</p></div>",
            "html.parser")
        self.assertEqual(evaluate(root, "div@p.0:2:4@text", "html"),
                         ["0", "2", "4"])
        self.assertEqual(evaluate(root, "div@p.3:1@text", "html"),
                         ["3", "1"])
        self.assertEqual(evaluate(root, "div@p.-1:2@text", "html"),
                         ["4", "2"])
        self.assertEqual(evaluate(root, "div@p!1:3@text", "html"),
                         ["0", "2", "4"])
        self.assertEqual(evaluate(root, "div@p[-1:1]@text", "html"),
                         ["4", "3", "2", "1"])

    def test_jsoup_elements_single_index_truth_table(self):
        cases = {
            "0,0,-1,9,-9": [0, 4],
            "1:3:2": [1, 3],
            "3:1:2": [3, 1],
            "-1:0": [4, 3, 2, 1, 0],
            "10:-10": [4, 3, 2, 1, 0],
            "0:4:-1": [0, 4],
            "0:4:-5": [0, 1, 2, 3, 4],
            "::": [0, 1, 2, 3, 4],
            "0:4:10": [0],
        }
        for expression, expected in cases.items():
            self.assertEqual(_selection_indices(expression, 5), expected,
                             expression)

    def test_jsoup_text_own_text_and_text_nodes_whitespace(self):
        root = BeautifulSoup(
            "<div id='x'> A\t<span>B</span>C<div>D\n E</div>"
            "<br>F&nbsp;G<script>hidden</script>\u200b\u00ad</div>",
            "html.parser")
        self.assertEqual(evaluate(root, "#x@text", "html"), ["A BC D E F\u00a0G"])
        self.assertEqual(evaluate(root, "#x@ownText", "html"), ["A C F\u00a0G"])

        nodes = BeautifulSoup("<p>  A  <b>x</b>\u00a0B\u00a0 </p>", "html.parser")
        self.assertEqual(evaluate(nodes, "p@textNodes", "html"), ["A\n\u00a0B\u00a0"])

    def test_javascript_element_text_and_data_match_jsoup_nodes(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        try:
            value = runtime.evaluate(
                '@js:var e=java.getElement("#x");[e.text(),e.ownText(),e.data()]',
                JsContext(source=source,
                          result=("<div id='x'>A<span>B</span>C"
                                  "<script>if (a < b) x()</script></div>"),
                          base_url="https://x"))
        finally:
            runtime.close()
        self.assertEqual(value, ["ABC", "AC", "if (a < b) x()"])

    def test_jsoup_html_serialization_uses_html_void_tags(self):
        root = BeautifulSoup(
            '<div><img src="x"><br><input disabled></div>', "html.parser")
        self.assertEqual(evaluate(root, "div@html", "html"),
                         ['<div>\n <img src="x">\n <br><input disabled>\n</div>'])

    def test_jsoup_outer_html_matches_fixed_pretty_print_and_entities(self):
        root = parse_markup(
            '<div id="X" data-v="A B" class="a:b"> t '
            '<span>A</span><span>B</span><p>P<b>Q</b></p>'
            '<img disabled=""><svg><foreignObject><path/></foreignObject></svg>'
            '<media:content url="u" />&nbsp;</div>')
        self.assertEqual(
            _jsoup_html_markup(root.select_one("div")),
            '<div id="X" data-v="A B" class="a:b">\n'
            ' t <span>A</span><span>B</span>\n'
            ' <p>P<b>Q</b></p><img disabled>\n'
            ' <svg>\n'
            '  <foreignObject>\n'
            '   <path />\n'
            '  </foreignObject>\n'
            ' </svg><media:content url="u" />&nbsp;\n'
            '</div>')

    def test_tolerant_xml_recovery_matches_fixed_jsoup(self):
        cases = {
            '<?xml version="1.0"?><root><A><b>1</A><c>2</root>':
                '<root><A><b>1</b></A><c>2</c></root>',
            '<?xml version="1.0"?><root><a x=1><b>2</root>':
                '<root><a x="1"><b>2</b></a></root>',
            '<?xml version="1.0"?><root>before<br/>after &bogus;</root>':
                '<root>before<br />after &amp;bogus;</root>',
            '<?xml version="1.0"?><root><!-- open <a> --><?pi x?><x></root>':
                '<root><!-- open <a> --><?pi x?><x></x></root>',
            '<?xml version="1.0"?><root a="1>2" a="later" A="upper">x</root>':
                '<root a="1>2" A="upper">x</root>',
        }
        for markup, expected in cases.items():
            with self.subTest(markup=markup):
                root = parse_markup(markup, "application/xml")
                self.assertEqual(_jsoup_html_markup(root.select_one("root")), expected)

    def test_jsoup_contains_is_case_insensitive_and_matches_use_jsoup_text(self):
        root = BeautifulSoup(
            "<div id='a'>Alpha<span>Beta</span></div>"
            "<div id='b'><span>alpha</span></div>", "html.parser")
        self.assertEqual([node["id"] for node in evaluate(
            root, "div:contains(ALPHABETA)", "html", list_context=True)], ["a"])
        self.assertEqual([node["id"] for node in evaluate(
            root, "div:containsOwn(ALPHA)", "html", list_context=True)], ["a"])
        self.assertEqual([node["id"] for node in evaluate(
            root, r"div:matchesOwn(^Alpha$)", "html", list_context=True)], ["a"])
        self.assertEqual([node["id"] for node in evaluate(
            root, "div:not(:contains(BETA))", "html", list_context=True)], ["b"])
        self.assertEqual([node["id"] for node in evaluate(
            root, "div:not(:containsOwn(ALPHA))", "html", list_context=True)], ["b"])

    def test_jsoup_namespace_selectors_match_colon_named_tags(self):
        root = BeautifulSoup(
            '<rss><media:content id="a"></media:content>'
            '<x:content id="b"></x:content><content id="c"></content></rss>',
            "html.parser")
        self.assertEqual(
            [node["id"] for node in evaluate(
                root, "media|content", "html", list_context=True)],
            ["a"],
        )
        self.assertEqual(
            [node["id"] for node in evaluate(
                root, "*|content", "html", list_context=True)],
            ["a", "b", "c"],
        )

    def test_java_get_elements_for_in_only_visits_numeric_records(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")
        html = '<ul><li>A</li><li>B</li></ul>'
        try:
            value = runtime.evaluate(
                '@js:var es=java.getElements("li"),keys=[];'
                'for(var i in es)keys.push(i);keys.join(",")',
                JsContext(source=source, result=html,
                          base_url=source.bookSourceUrl),
            )
        finally:
            runtime.close()

        self.assertEqual(value, "0,1")

    def test_bare_html_attribute_rule_reads_current_element(self):
        node = BeautifulSoup(
            '<li onclick="openBook(42)"><h4>Story</h4></li>', "html.parser"
        ).li

        value = extract_value(
            node, r"onclick##.*\((\d*)\)##https://site/story/$1", "html")

        self.assertEqual(value, "https://site/story/42")

    def test_jsonpath_dot_wildcard_iterates_array_children(self):
        root = {"data": {"chapters": [
            {"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]}}
        self.assertEqual(
            evaluate(root, "$.data.chapters.*", "json"),
            root["data"]["chapters"],
        )

    def test_jsonpath_recursive_descent_matches_jayway_sample_rules(self):
        root = {"data": {"list": [{"name": "A"}, {"name": "B"}]}}
        self.assertEqual(evaluate(root, "$..list[*]", "json"),
                         root["data"]["list"])
        self.assertEqual(evaluate(root, "$..data[*]", "json"), [root["data"]])
        self.assertEqual(evaluate(root, "$..list[*].name", "json"), ["A", "B"])

    def test_json_list_rule_falls_through_object_reads(self):
        root = {"data": {"page": {"count": 2},
                         "list": [{"name": "A"}, {"name": "B"}]}}
        # Jayway reads a definite path into ArrayList; a JSON object throws, so
        # Legado's || chain keeps looking for a branch that yields an array.
        self.assertEqual(evaluate(root, "$.data", "json"), [root["data"]])
        self.assertEqual(evaluate(root, "$.data", "json", list_context=True),
                         [])
        self.assertEqual(
            evaluate(root, "$.data||$.data.list", "json", list_context=True),
            root["data"]["list"])
        self.assertEqual(
            evaluate(root, "$.data||$.data.list[*]", "json",
                     list_context=True),
            root["data"]["list"])

    def test_jsoup_list_rule_keeps_trailing_segment_as_selector(self):
        root = BeautifulSoup(
            '<div class="list"><a href="/1">One</a><em>Two</em></div>',
            "html.parser")
        # AnalyzeByJSoup.getElements feeds every '@' segment to the selector
        # engine; only the string path reads the last one as a value.
        self.assertEqual(
            [node.get_text() for node in
             evaluate(root, "div.list@em", "html", list_context=True)],
            ["Two"])
        self.assertEqual(evaluate(root, "div.list@text", "html"), ["OneTwo"])
        self.assertEqual(evaluate(root, "div.list@text", "html",
                                  list_context=True), [])
        self.assertEqual(
            len(evaluate(root, "div.list@mio-tile", "html",
                         list_context=True)), 0)

    def test_jsoup_text_inserts_space_at_svg_and_script_boundaries(self):
        node = BeautifulSoup(
            '<em class="x"><svg><use xlink:href="#i-o"></use></svg>'
            '<span class="vam">其他</span>'
            '<svg><use xlink:href="#i-pen"></use></svg>'
            '<span class="vam">连山会</span></em>', "html.parser").em
        self.assertEqual(jsoup_values(node, "text"), ["其他 连山会"])
        script = BeautifulSoup('<p class="x">one<script>a=1</script>'
                               'two</p>', "html.parser").p
        self.assertEqual(jsoup_values(script, "text"), ["one two"])
        plain = BeautifulSoup('<p class="x"><span>one</span>'
                              '<span>two</span></p>', "html.parser").p
        self.assertEqual(jsoup_values(plain, "text"), ["onetwo"])

    def test_jsoup_list_rule_treats_every_segment_as_selector(self):
        # AnalyzeRule.getElements (book/chapter lists, bookInfo.init) runs every
        # '@' segment through getElementsSingle, so the trailing one selects too.
        root = BeautifulSoup(
            '<ul><li><a href="/1">One</a></li><li>Two</li></ul>',
            "html.parser")
        nodes = evaluate(root, "ul@li", "html", list_context=True)
        self.assertEqual([node.get_text(strip=True) for node in nodes],
                         ["One", "Two"])
        self.assertEqual(evaluate(root, "ul@li@a@href", "html"), ["/1"])

    def test_jsoup_accessor_tail_is_not_a_selector(self):
        # The non-list path mirrors getResultList: rules[0..last-1] select,
        # then getResultLast reads the trailing segment as text/textNodes/
        # ownText/html/all or as an attribute name.  Selector-shaped tails
        # therefore resolve to a blank attribute and drop out of the result,
        # while "title"/"href" tails read the attribute like Legado does.
        root = BeautifulSoup(
            '<div class="book-list clearfix"><a href="/1" title="tip">'
            'One</a></div>', "html.parser")
        self.assertEqual(
            evaluate(root, "class.book-list clearfix@tag.li", "html"), [])
        self.assertEqual(
            evaluate(root, "class.book-list clearfix@a@tag.li", "html"), [])
        self.assertEqual(
            evaluate(root, "class.book-list clearfix@a@title", "html"),
            ["tip"])
        self.assertEqual(evaluate(root, "class.book-list clearfix@a@href",
                                  "html"), ["/1"])
        # The same rule on the list path selects with every segment instead.
        self.assertEqual(evaluate(
            root, "class.book-list clearfix@a@href", "html",
            list_context=True), [])

    def test_standalone_jsoup_value_rule_uses_current_element(self):
        node = BeautifulSoup(
            '<li><a href="/1">One</a></li>', "html.parser").li
        self.assertEqual(evaluate(node, "text", "html"), ["One"])
        self.assertEqual(
            evaluate(node, "html", "html"),
            ['<li><a href="/1">One</a></li>'],
        )

    def test_standalone_bare_rule_reads_attribute_on_element_root(self):
        # getResultList seeds its element list with the current element, so a
        # single-segment rule never selects; getResultLast's else branch reads
        # it as an attribute name.  ElementsSingle turns it into a CSS
        # selector on the list path, where no element is named "href".
        node = BeautifulSoup('<a href="/1" title="tip">One</a>',
                             "html.parser").a
        self.assertEqual(evaluate(node, "href", "html"), ["/1"])
        self.assertEqual(evaluate(node, "href", "html", list_context=True), [])

    def test_js_failure_classification_separates_engine_source_and_network(self):
        self.assertEqual(classify_js_bridge_error(
            "暂不支持 java.someApi"), "js_needed")
        self.assertEqual(classify_js_bridge_error(
            "TypeError: cannot read property '1' of null"), "parse_error")
        self.assertEqual(classify_js_bridge_error(
            "InternalError\nBridge: JsBridgeError: JS network error: SSL EOF"),
            "http_error")

    def test_valid_list_rule_with_no_matches_is_empty_not_engine_error(self):
        nodes, kind = select_nodes("<html><p>nothing</p></html>", "text/html",
                                   ".bookbox")
        self.assertEqual((nodes, kind), ([], "html"))

    def test_invalid_list_selector_propagates_from_parser(self):
        with self.assertRaises(Exception):
            select_nodes("<div>x</div>", "text/html", "[invalid")

    def test_failed_list_rule_does_not_infer_book_cards(self):
        html = "".join(
            f'<li class="book-item"><a href="/{index}">Book</a></li>'
            for index in range(4))
        nodes, kind = select_nodes(html, "text/html", ".missing")
        self.assertEqual((nodes, kind), ([], "html"))

    def test_failed_field_rule_does_not_guess_anchor_or_image(self):
        node = BeautifulSoup('<div><a href="/guessed">Text</a>'
                             '<img src="/cover.jpg"></div>', "html.parser").div
        self.assertEqual(extract_value(node, ".missing@href", "html"), "")
        self.assertEqual(extract_value(node, ".missing@src", "html"), "")

    def test_charset_escape_matches_legado_utf16_parameter_encoding(self):
        self.assertEqual(legado_escape("书😀 a"), "%u4E66%uD83D%uDE00%20a")
        self.assertEqual(escape_form("q=书 源&empty="),
                         "q=%u4E66%20%u6E90&empty=")
        self.assertEqual(escape_form("书键=值&&flag"),
                         "%E4%B9%A6%E9%94%AE=%u503C&flag=")
        self.assertEqual(
            escape_url_query("https://source/search?q=书 源&page=1"),
            "https://source/search?q=%u4E66%20%u6E90&page=1")

    def test_url_encoder_uses_declared_charset_and_preserves_encoded_values(self):
        self.assertEqual(encode_form_fields("x=𠀀", "gbk"), "x=%3F")
        self.assertEqual(encode_form_fields("q=书 源&done=%CA%E9", "gbk"),
                         "q=%CA%E9+%D4%B4&done=%CA%E9")
        self.assertEqual(encode_form_fields("flag&&empty=", "utf-8"),
                         "flag=&empty=")
        self.assertEqual(
            encode_url_query_fields("https://source/search?q=书 源&page=1", "gbk"),
            "https://source/search?q=%CA%E9+%D4%B4&page=1",
        )

    def test_post_body_charset_only_controls_analyzed_fields(self):
        encode = ReaderEngine._encode_request_body
        self.assertEqual(encode("q=书", "gbk"), b"q=%CA%E9")
        self.assertEqual(encode('{"q":"书"}', "gbk"),
                         '{"q":"书"}'.encode("utf-8"))
        self.assertEqual(encode('{"q":"书"}', "utf-8",
                                "application/json; charset=gbk"),
                         '{"q":"书"}'.encode("gbk"))
        self.assertEqual(encode("书键=值&&flag", "escape"),
                         b"%E4%B9%A6%E9%94%AE=%u503C&flag=")

    def test_fallback_rule_short_circuits_after_first_nonempty_branch(self):
        soup = BeautifulSoup('<p class="ok">value</p>', "html.parser")
        values = evaluate(soup, '.ok@text||[invalid', "html")
        self.assertEqual(values, ["value"])

    def test_combination_operator_precedence_matches_legado(self):
        soup = BeautifulSoup('<i>A</i><b>C</b>', "html.parser")
        values = evaluate(soup, 'i@text&&em@text||b@text', "html")
        self.assertEqual(values, ["A", "C"])

        # RuleAnalyzer uses the first outside operator as elementsType. A
        # leading successful fallback therefore prevents the later && branch.
        values = evaluate(soup, 'i@text||em@text&&b@text', "html")
        self.assertEqual(values, ["A"])

    def test_rule_analyzer_code_and_rule_balancing_differ_on_escapes(self):
        self.assertEqual(
            split_rule_expression(
                r"$.x[?(@.v=='a\'&&b')]||$.fallback", ("&&", "||"), True),
            ([r"$.x[?(@.v=='a\'&&b')]", "$.fallback"], "||"),
        )
        with self.assertRaises(Exception):
            split_rule_expression(
                r"div[a='a\'&&b']||span", ("&&", "||"), False)

    def test_regex_chain_ampersands_are_not_balanced(self):
        with self.assertRaises(Exception):
            evaluate("a&&b", ":(a&&b)", "html")

    def test_jsoup_at_chain_uses_rule_balancing(self):
        root = BeautifulSoup(
            '<div data-key="a@b"><span>value</span></div>', "html.parser")
        self.assertEqual(
            evaluate(root, "div[data-key='a@b']@span@text", "html"),
            ["value"],
        )

    def test_get_string_joins_matches_but_url_mode_uses_first(self):
        root = BeautifulSoup('<a href="/1">A</a><a href="/2">B</a>',
                             "html.parser")
        self.assertEqual(extract_value(root, "a@text", "html"), "A\nB")
        self.assertEqual(extract_value(
            root, "a@href", "html", "https://source/book"),
            "https://source/1")

    def test_regex_list_mode_returns_capture_records_for_field_templates(self):
        html = ('101" class="isvip name">第一章<'
                '102" class="name">第二章<')
        records = evaluate(
            html, r':(?s)(\d+)" class="(isvip)?[^\"]*name[^>]*>([^<]*)',
            "html")
        self.assertEqual(records[0][1:], ["101", "isvip", "第一章"])
        self.assertEqual(records[1][1:], ["102", "", "第二章"])
        self.assertEqual(extract_value(records[0], "$3", "html"), "第一章")
        self.assertEqual(
            extract_value(records[0], "https://source/chapter/$1", "html"),
            "https://source/chapter/101")

    def test_regex_triple_hash_returns_only_first_transformed_match(self):
        root = BeautifulSoup('<p>before ID:42 after ID:99</p>', "html.parser")

        self.assertEqual(
            extract_value(root, r'p@text##ID:(\d+)##$1###', "html"),
            "42",
        )
        self.assertEqual(
            extract_value(root, r'p@text##missing##x###', "html"),
            "",
        )

    def test_java_regex_named_groups_linebreak_and_absolute_end(self):
        match = java_regex_search(r"(?<word>book)\Rend\z", "book\r\nend")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("word"), "book")
        self.assertEqual(java_regex_sub(
            r"(?<left>A)-(?<right>B)", "${right}${left}", "A-B"), "BA")

    def test_java_regex_quote_backreference_whitespace_and_replacement_escaping(self):
        self.assertIsNotNone(java_regex_search(r"\Qa+b?\E", "x a+b? y"))
        self.assertIsNotNone(java_regex_search(
            r"(?<x>ab)\k<x>\h\v", "abab \n"))
        self.assertEqual(java_regex_sub(r"(A)", r"\$1-$1-\\", "A"),
                         "$1-A-\\")

    def test_java_regex_unicode_properties_use_python_unicode_database(self):
        self.assertEqual(java_regex_search(r"\p{L}+", "123书Ab").group(0), "书Ab")
        self.assertEqual(java_regex_search(r"\p{P}+", "word，。!").group(0), "，。!")
        self.assertEqual(java_regex_search(r"\p{IsHan}+", "A书籍B").group(0), "书籍")
        self.assertEqual(java_regex_search(r"\P{N}+", "12章A").group(0), "章A")

    def test_java_regex_contiguous_match_anchor(self):
        from app.services.java_regex import finditer as java_regex_finditer
        matches = [item.group(0) for item in java_regex_finditer(r"\G\d,?", "1,2,3x4")]
        self.assertEqual(matches, ["1,", "2,", "3"])
        self.assertEqual(java_regex_sub(r"\G\d,?", "x", "1,2,3x4"), "xxxx4")

    def test_java_regex_character_class_intersection(self):
        self.assertEqual(
            java_regex_search(r"[a-z&&[^aeiou]]+", "aei-book").group(0), "b")
        self.assertEqual(
            java_regex_search(r"[\p{L}&&[^\p{Lu}]]+", "ABC书abc").group(0),
            "书abc")

    def test_java_regex_inline_java_flags(self):
        self.assertIsNotNone(java_regex_search(r"(?iu)ä", "Ä"))
        self.assertIsNotNone(java_regex_search(r"(?d)^.$", "\r"))
        self.assertIsNotNone(java_regex_search(r"(?U)\w+", "书籍"))

    def test_java_regex_group_zero_null_group_and_invalid_reference(self):
        self.assertEqual(java_regex_sub(r"(a)", "$0", "a"), "a")
        self.assertEqual(java_regex_sub(r"(a)?b", "$1", "b"), "")
        with self.assertRaises(re.error):
            java_regex_sub(r"(a)", "$2", "a")

    def test_java_regex_zero_length_and_canonical_equivalence(self):
        self.assertEqual(java_regex_sub(r"", "-", "ab"), "-a-b-")
        self.assertEqual(java_regex_sub(r"(?=b)", "-", "ab"), "a-b")
        self.assertIsNotNone(java_regex_search("(?c)å", "a\u030a"))
        self.assertIsNotNone(java_regex_search("(?c)a\u030a", "å"))

    def test_jayway_regex_literal_and_function_chains(self):
        data = {
            "items": [{"name": "Alpha"}, {"name": "beta"}],
            "numbers": [1, 2, 3],
        }
        self.assertEqual(
            evaluate(data, r"$.items[?(@.name =~ /^a/i)].name", "json"),
            ["Alpha"],
        )
        self.assertEqual(
            evaluate(data, "$.numbers.append(4).length()", "json"), [4])
        self.assertEqual(
            evaluate(data, "$.numbers.concat([4,5]).sum()", "json"), [15.0])

    def test_jayway_collection_filter_operators(self):
        data = {"items": [
            {"id": 1, "tags": ["x", "y"]},
            {"id": 2, "tags": []},
            {"id": 3, "tags": ["z"]},
        ]}
        self.assertEqual(evaluate(
            data, "$.items[?(@.id in [1,3])].id", "json"), [1, 3])
        self.assertEqual(evaluate(
            data, "$.items[?(@.id nin [1,3])].id", "json"), [2])
        self.assertEqual(evaluate(
            data, "$.items[?(@.tags subsetof ['x','y'])].id", "json"), [1, 2])
        self.assertEqual(evaluate(
            data, "$.items[?(@.tags anyof ['x'])].id", "json"), [1])
        self.assertEqual(evaluate(
            data, "$.items[?(@.tags noneof ['x','y'])].id", "json"), [2, 3])
        self.assertEqual(evaluate(
            data, "$.items[?(@.tags size 0)].id", "json"), [2])
        self.assertEqual(evaluate(
            data, "$.items[?(@.tags empty true)].id", "json"), [2])

    def test_jayway_functions_can_be_intermediate_path_stages(self):
        data = {
            "object": {"first": 1, "second": 2},
            "items": [{"name": "A"}, {"name": "B"}],
            "numbers": [1, 2],
        }
        self.assertEqual(evaluate(data, "$.object.keys()[0]", "json"), [])
        self.assertEqual(evaluate(data, "$.items.first().name", "json"), ["A"])
        self.assertEqual(evaluate(data, "$.items.last().name", "json"), ["B"])
        self.assertEqual(evaluate(
            data, '$.items.append({"name":"C"})[-1].name', "json"), ["C"])
        self.assertEqual(evaluate(
            data, "$.numbers.append(3).index(-1)", "json"), [3])

    def test_jayway_compound_predicate_precedence_and_truthiness(self):
        data = {"items": [
            {"id": 1, "ok": False, "name": "Alpha"},
            {"id": 2, "ok": False, "name": "beta"},
            {"id": 3, "ok": True, "name": "Alpine"},
            {"id": 4, "name": "missing-ok"},
        ]}
        self.assertEqual(evaluate(
            data, "$.items[?(@.id >= 2 && @.ok == true)].id", "json"), [3])
        self.assertEqual(evaluate(
            data, "$.items[?(@.id == 1 || @.id == 3)].id", "json"), [1, 3])
        self.assertEqual(evaluate(
            data, "$.items[?((@.id >= 2 && @.ok == true) || @.id == 1)].id",
            "json"), [1, 3])
        # A bare path is an existence predicate in Jayway, even when false.
        self.assertEqual(evaluate(data, "$.items[?(@.ok)].id", "json"), [1, 2, 3])
        self.assertEqual(evaluate(
            data, r"$.items[?(@.name =~ /^Al/i && @.id != 1)].id", "json"), [3])
        self.assertEqual(evaluate(
            data, "$.items[?(@['id'] >= 3)].name", "json"),
            ["Alpine", "missing-ok"])

    def test_jayway_definite_and_indefinite_array_results_keep_shape(self):
        data = {"items": [{"tags": ["x", "y"]}]}
        self.assertEqual(evaluate(data, "$.items[0].tags", "json"), ["x", "y"])
        self.assertEqual(evaluate(data, "$.items[*].tags", "json"), [["x", "y"]])
        self.assertEqual(evaluate(data, "$.items[?(@.tags)].tags", "json"),
                         [["x", "y"]])

    def test_every_fixed_js_extension_method_is_exposed_by_python_bridge(self):
        root = Path("base_history/legado-3.23.110211/app/src/main/java/io/legado/app/help")
        names = set()
        for filename in ("JsExtensions.kt", "JsEncodeUtils.kt"):
            text = (root / filename).read_text(encoding="utf-8")
            names.update(re.findall(
                r"^\s*fun\s+([A-Za-z_$][\w$]*)\s*\(", text, flags=re.M))
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example"})
        missing = [name for name in sorted(names) if runtime.evaluate(
            f"@js:typeof java.{name}", JsContext(source=source)) != "function"]
        self.assertEqual(len(names), 86)
        self.assertEqual(missing, [])

    def test_book_source_uses_fixed_review_rule_fields_and_ignores_unknowns(self):
        source = BookSource.from_dict({
            "bookSourceName": "source", "bookSourceUrl": "https://source",
            "ruleReview": {"reviewUrl": "/review", "contentRule": ".text@text",
                           "reviewList": ".review"},
        })
        self.assertEqual(source.ruleReview["reviewUrl"], "/review")
        self.assertEqual(source.to_dict()["ruleReview"]["contentRule"], ".text@text")
        self.assertNotIn("reviewList", source.ruleReview)

    def test_book_source_keeps_current_content_rule_fields(self):
        source = BookSource.from_dict({
            "bookSourceUrl": "https://source",
            "ruleContent": {"content": "p@text", "subContent": "#sub@html",
                            "callBackJs": "result", "reviewList": "discard"},
        })
        self.assertEqual(source.ruleContent["subContent"], "#sub@html")
        self.assertEqual(source.ruleContent["callBackJs"], "result")
        self.assertNotIn("reviewList", source.ruleContent)
        self.assertEqual(source.to_dict()["ruleContent"]["subContent"], "#sub@html")

    def test_book_source_gson_coercion_matches_fixed_deserializers(self):
        source = BookSource.from_dict({
            "bookSourceUrl": 123, "bookSourceComment": {"a": 1},
            "bookSourceType": "1", "customOrder": 3.9,
            "enabled": "false", "lastUpdateTime": "42",
            "ruleSearch": {"name": 7, "intro": False, "kind": {"x": 1},
                           "unknown": "discard"},
        })
        self.assertEqual(source.bookSourceUrl, "123")
        self.assertEqual(source.bookSourceComment, '{"a":1}')
        self.assertEqual(source.bookSourceType, 0)
        self.assertEqual(source.customOrder, 3)
        self.assertFalse(source.enabled)
        self.assertEqual(source.lastUpdateTime, 42)
        self.assertEqual(source.ruleSearch,
                         {"name": "7", "intro": "false", "kind": '{"x":1}'})

    def test_book_source_gson_boolean_and_long_boundaries(self):
        source = BookSource.from_dict({
            "bookSourceUrl": "https://source", "enabled": None,
            "enabledExplore": "TRUE", "enabledCookieJar": None,
            "lastUpdateTime": "42.0", "respondTime": "180001",
        })
        self.assertTrue(source.enabled)
        self.assertTrue(source.enabledExplore)
        self.assertIsNone(source.enabledCookieJar)
        self.assertEqual(source.lastUpdateTime, 42)
        self.assertEqual(source.respondTime, 180001)
        with self.assertRaisesRegex(ValueError, "boolean field"):
            BookSource.from_dict({"bookSourceUrl": "x", "enabled": 1})
        with self.assertRaisesRegex(ValueError, "invalid long"):
            BookSource.from_dict({"bookSourceUrl": "x", "lastUpdateTime": 1.5})
        with self.assertRaisesRegex(ValueError, "invalid long"):
            BookSource.from_dict({"bookSourceUrl": "x", "respondTime": 1.5})
        self.assertEqual(BookSource.from_dict({
            "bookSourceUrl": "x", "lastUpdateTime": 2 ** 63,
        }).lastUpdateTime, 2 ** 63 - 1)
        self.assertEqual(BookSource.from_dict({
            "bookSourceUrl": "x", "lastUpdateTime": -(2 ** 63) - 1,
        }).lastUpdateTime, -(2 ** 63))
        with self.assertRaisesRegex(ValueError, "invalid long"):
            BookSource.from_dict({"bookSourceUrl": "x",
                                  "lastUpdateTime": 10 ** 30})

    def test_book_source_accepts_legacy_string_encoded_rule_objects(self):
        source = BookSource.from_dict({
            "bookSourceName": "source",
            "bookSourceUrl": "https://source",
            "ruleSearch": '{"bookList":"$.data[*]","name":"$.name"}',
        })

        self.assertEqual(source.ruleSearch["bookList"], "$.data[*]")
        self.assertEqual(source.ruleSearch["name"], "$.name")
        self.assertIsNone(source.ruleToc)

        with self.assertRaisesRegex(ValueError, "invalid serialized ruleToc"):
            BookSource.from_dict({"bookSourceUrl": "https://source",
                                  "ruleToc": "not-json"})

    def test_book_source_missing_fields_use_legado_defaults(self):
        source = BookSource.from_dict({
            "bookSourceName": "source", "bookSourceUrl": "https://source",
        })

        self.assertEqual(source.bookSourceType, 0)
        self.assertTrue(source.enabledExplore)
        self.assertTrue(source.enabledCookieJar)
        self.assertIsNone(source.ruleSearch)
        self.assertIsNone(source.bookSourceGroup)
        self.assertNotIn("ruleSearch", source.to_dict())
        self.assertNotIn("bookSourceGroup", source.to_dict())

    def test_book_source_file_accepts_single_json_object_like_import_view_model(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps({
                "bookSourceName": "single",
                "bookSourceUrl": "https://single.example",
            }), encoding="utf-8")
            sources = BookSource.from_json(path)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].bookSourceName, "single")

    def test_book_source_entity_identity_is_url_only_like_legado(self):
        first = BookSource(bookSourceName="one", bookSourceUrl="https://same")
        second = BookSource(bookSourceName="two", bookSourceUrl="https://same")
        other = BookSource(bookSourceName="one", bookSourceUrl="https://other")

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertNotEqual(first, other)

    def test_book_source_configuration_equal_normalizes_null_and_empty(self):
        first = BookSource(bookSourceUrl="https://same", bookSourceGroup="")
        second = BookSource(bookSourceUrl="https://same", bookSourceGroup=None)
        self.assertTrue(first.equal(second))
        second.ruleContent = {"content": "@text"}
        self.assertFalse(first.equal(second))

    def test_book_source_rule_getters_are_lazy_and_mutating(self):
        source = BookSource(bookSourceUrl="https://source")
        source.ruleSearch = None
        rule = source.getSearchRule()
        rule["bookList"] = ".book"
        self.assertIs(rule, source.ruleSearch)
        self.assertEqual(source.getSearchRule()["bookList"], ".book")

    def test_book_source_groups_follow_legado_split_rules(self):
        source = BookSource(bookSourceGroup="a；b")
        self.assertTrue(source.hasGroup("b"))
        self.assertIs(source.addGroup("b,c"), source)
        self.assertEqual(set(source._groups(source.bookSourceGroup)), {"a", "b", "c"})
        self.assertIs(source.removeGroup("a；c"), source)
        self.assertEqual(source.bookSourceGroup, "b")

    def test_book_source_invalid_groups_use_fixed_chinese_names(self):
        source = BookSource(bookSourceGroup="正常，已失效；校验超时,失效站点")
        self.assertEqual(set(source._groups(source.getInvalidGroupNames())),
                         {"已失效", "校验超时", "失效站点"})
        source.removeInvalidGroups()
        self.assertEqual(source.bookSourceGroup, "正常")

    def test_book_source_group_methods_preserve_fixed_null_branch(self):
        source = BookSource(bookSourceGroup=None)
        self.assertIs(source.removeGroup("a"), source)
        self.assertIsNone(source.bookSourceGroup)
        source.addGroup("a； b")
        self.assertEqual(source.bookSourceGroup, "a； b")

    def test_book_source_export_preserves_false_zero_and_empty_values(self):
        source = BookSource(
            bookSourceUrl="https://source", enabled=False,
            enabledExplore=False, enabledCookieJar=False, customOrder=0,
            ruleSearch={},
        )
        exported = source.to_dict()
        self.assertIs(exported["enabled"], False)
        self.assertIs(exported["enabledExplore"], False)
        self.assertIs(exported["enabledCookieJar"], False)
        self.assertEqual(exported["customOrder"], 0)
        self.assertEqual(exported["ruleSearch"], {})

    def test_old_book_source_is_migrated_with_historical_legado_rules(self):
        source = BookSource.from_dict({
            "bookSourceUrl": "https://old.example", "bookSourceName": "old",
            "bookSourceType": "AUDIO", "enable": False, "serialNumber": 7,
            "httpUserAgent": "OldUA", "ruleFindUrl": "",
            "ruleSearchUrl": "https://old.example/s?key=searchKey&page=searchPage|charset=gbk",
            "ruleSearchList": "div|li", "ruleSearchName": "h3@text",
            "ruleBookContent": "$body@html", "ruleBookContentReplace": "a#b",
        })
        self.assertEqual(source.bookSourceType, 1)
        self.assertFalse(source.enabled)
        self.assertFalse(source.enabledExplore)
        self.assertEqual(source.customOrder, 7)
        self.assertEqual(source.header, '{"User-Agent":"OldUA"}')
        self.assertIn("{{key}}", source.searchUrl)
        self.assertIn('"charset":"gbk"', source.searchUrl)
        self.assertEqual(source.ruleSearch["bookList"], "div||li")
        self.assertEqual(source.ruleContent["content"], "body@html")
        self.assertEqual(source.ruleContent["replaceRegex"], "a##b")
        self.assertNotIn("author", source.to_dict()["ruleSearch"])

    def test_old_url_migration_preserves_embedded_javascript_placeholders(self):
        from app.models.legacy_book_source import to_new_url
        migrated = to_new_url("https://x/{{searchKey+'x'}}?p=searchPage@q=searchKey")
        self.assertIn("{{key+'x'}}", migrated)
        self.assertIn('"method":"POST"', migrated)
        self.assertIn('"body":"q={{key}}"', migrated)

    def test_base64_decode_accepts_urlsafe_value_without_padding(self):
        self.assertEqual(LegadoJsRuntime._base64_decode("eyJhIjoxfQ"), '{"a":1}')

    def test_js_lib_mapping_loads_remote_library_before_rule(self):
        runtime = LegadoJsRuntime()
        runtime._ajax = lambda _url: "function helper(){ return 7; }"
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source", "jsLib": json.dumps({
                "helper": "https://cdn.example/helper.js",
            })},
        )
        self.assertEqual(runtime.evaluate("@js:helper()", JsContext(source=source)), 7)

    def test_js_lib_json_ignores_inline_values_like_shared_js_scope(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "jsLib": json.dumps({"inline": "var leakedFromMap=9;"}),
        })
        self.assertEqual(runtime.evaluate(
            "@js:typeof leakedFromMap", JsContext(source=source)), "undefined")

    def test_js_runtimes_share_one_permissive_ssl_context(self):
        """建默认 SSL 上下文要枚举整个 Windows 证书库，换源扫描付不起这个钱。"""
        from app.services import legado_js

        created = []
        real = ssl.create_default_context

        def counting_context(*args, **kwargs):
            created.append(1)
            return real(*args, **kwargs)

        with patch.object(legado_js, "_SSL_CONTEXT", None), \
                patch("app.services.legado_js.ssl.create_default_context",
                      side_effect=counting_context):
            runtimes = [LegadoJsRuntime() for _ in range(3)]

        contexts = {handler._context
                    for runtime in runtimes
                    for handler in runtime.opener.handlers
                    if getattr(handler, "_context", None) is not None}
        self.assertEqual(len(created), 1)
        self.assertEqual(len(contexts), 1)
        context = contexts.pop()
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_js_lib_only_treats_json_objects_as_remote_mappings(self):
        runtime = LegadoJsRuntime()
        for library in ('"var leaked=1"', '["var leaked=1"]'):
            source = SimpleNamespace(to_dict=lambda library=library: {
                "bookSourceUrl": "https://source/" + library[0], "jsLib": library,
            })
            self.assertEqual(runtime.evaluate(
                "@js:typeof leaked", JsContext(source=source)), "undefined")
            self.assertFalse(runtime.evaluate(
                "@js:source.getShareScope()===null", JsContext(source=source)))
        blank = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source/blank", "jsLib": "  \n ",
        })
        self.assertTrue(runtime.evaluate(
            "@js:source.getShareScope()===null", JsContext(source=blank)))

    def test_js_lib_state_is_shared_across_rule_evaluations(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "jsLib": "var sharedCount=0;function nextShared(){return ++sharedCount;}",
        })
        self.assertEqual(runtime.evaluate(
            "@js:nextShared()", JsContext(source=source)), 1)
        self.assertEqual(runtime.evaluate(
            "@js:nextShared()", JsContext(source=source)), 2)

    def test_js_lib_closure_identity_survives_rule_evaluations(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "jsLib": (
                "function counter(){let value=0;return ()=>++value;}"
                "const nextValue=counter();"
            ),
        })
        self.assertEqual(runtime.evaluate(
            "@js:nextValue()", JsContext(source=source)), 1)
        self.assertEqual(runtime.evaluate(
            "@js:nextValue()", JsContext(source=source)), 2)

    def test_rule_lexical_scope_is_fresh_over_shared_js_lib(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "jsLib": "const helper=1;"})
        for _ in range(2):
            self.assertEqual(runtime.evaluate(
                "@js:let local=helper+1;local", JsContext(source=source)), 2)

    def test_java_security_message_digest_signs_like_the_jdk(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "jsLib": (
                "var QM_KEY='d3dGiJc651gSQ8w1';"
                "function qmMd5(s){"
                "var b=Packages.java.security.MessageDigest.getInstance('MD5')"
                ".digest(new Packages.java.lang.String(s).getBytes('UTF-8'));"
                "var h='';"
                "for(var i=0;i<b.length;i++){var x=(b[i]&0xff).toString(16);"
                "h+=(x.length<2?'0':'')+x;}"
                "return h;}"
                "function qmSign(o){"
                "return qmMd5(Object.keys(o).sort().map(function(k){"
                "return k+'='+o[k];}).join('')+QM_KEY);}"
            )})
        context = JsContext(source=source)
        self.assertEqual(runtime.evaluate("@js:qmMd5('中文')", context),
                         hashlib.md5("中文".encode()).hexdigest())
        expected = hashlib.md5(
            ("gender=3imei_ip=2937357107page=1wd=斗破" + "d3dGiJc651gSQ8w1")
            .encode()).hexdigest()
        self.assertEqual(runtime.evaluate(
            "@js:qmSign({gender:'3',imei_ip:'2937357107',page:1,wd:'斗破'})",
            context), expected)

    def test_java_security_message_digest_accumulates_updates(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var md=java.security.MessageDigest.getInstance('MD5');"
            "md.update(new java.lang.String('ab').getBytes('UTF-8'));"
            "md.update(new java.lang.String('c').getBytes('UTF-8'));"
            "var bytes=md.digest();var out='';"
            "for(var i=0;i<bytes.length;i++)"
            "out+=(bytes[i]&0xff).toString(16).padStart(2,'0');"
            "return out+'/'+md.getAlgorithm()+'/'+md.getDigestLength()})()",
            JsContext(source=source)), hashlib.md5(b"abc").hexdigest() + "/MD5/16")

    def test_byte_array_output_stream_follows_the_java_write_contract(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        context = JsContext(source=source)
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var out=new Packages.java.io.ByteArrayOutputStream();"
            "out.write(233);out.write([1,2]);out.write([9,8,7],1,2);"
            "out.write(new Uint8Array([255]));out.writeBytes([4]);"
            "var written=String(out.toByteArray())+'/'+out.size();"
            "out.reset();"
            "return written+'/'+out.size()+'/'+String(out.toByteArray())})()",
            context), "-23,1,2,8,7,-1,4/7/0/")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var out=new Packages.java.io.ByteArrayOutputStream();"
            "out.write(new Uint8Array([228,184,173]));"
            "return out.toString('UTF-8')+'/'+String(out)})()",
            context), "中/中")

    def test_cipher_chacha20_poly1305_decrypts_xchacha20_payloads(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "jsLib": TING_JS_LIB})
        context = JsContext(source=source)
        plaintext = json.dumps({"data": [{"id": 7, "title": "三体"}]},
                               ensure_ascii=False)
        for version in (1, 2):
            self.assertEqual(runtime.evaluate(
                "@js:String(ting_decryptPayload('%s','%s'))" % (
                    xchacha20_payload(plaintext, version), TING_KEY),
                context), plaintext)

    def test_cipher_aes_gcm_verifies_the_tag_that_jce_appends(self):
        from Crypto.Cipher import AES
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        key, nonce = bytes.fromhex("00112233445566778899aabbccddeeff"), bytes(12)
        ciphertext, tag = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(
            "正文".encode())
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var c=Packages.javax.crypto.Cipher.getInstance('AES/GCM/NoPadding');"
            "var s=Packages.javax.crypto.spec;"
            "c.init(2,new s.SecretKeySpec(%s,'AES'),new s.GCMParameterSpec(128,%s));"
            "return new java.lang.String(c.doFinal(%s),'UTF-8')})()" % (
                json.dumps(list(key)), json.dumps(list(nonce)),
                json.dumps(list(ciphertext + tag))),
            JsContext(source=source)), "正文")
        tampered = json.dumps(list(ciphertext + bytes([tag[0] ^ 1]) + tag[1:]))
        with self.assertRaises(JsBridgeError) as caught:
            runtime.evaluate(
                "@js:(function(){var c=Packages.javax.crypto.Cipher.getInstance('AES/GCM/NoPadding');"
                "var s=Packages.javax.crypto.spec;"
                "c.init(2,new s.SecretKeySpec(%s,'AES'),new s.GCMParameterSpec(128,%s));"
                "return new java.lang.String(c.doFinal(%s),'UTF-8')})()" % (
                    json.dumps(list(key)), json.dumps(list(nonce)), tampered),
                JsContext(source=source))
        self.assertIn("MAC check failed", str(caught.exception))

    def test_hutool_digest_util_hex_helpers_match_hashlib(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        context = JsContext(source=source)
        for expression, expected in (
                ("Packages.cn.hutool.crypto.digest.DigestUtil.md5Hex('abc')",
                 hashlib.md5(b"abc").hexdigest()),
                ("Packages.cn.hutool.crypto.digest.DigestUtil.sha256Hex('abc')",
                 hashlib.sha256(b"abc").hexdigest())):
            self.assertEqual(runtime.evaluate("@js:" + expression, context), expected)

    def test_java_lang_string_constructor_returns_java_byte_array_and_text(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        context = JsContext(source=source)
        self.assertEqual(runtime.evaluate(
            "@js:JSON.stringify(Array.from(new Packages.java.lang.String('中文')"
            ".getBytes('UTF-8')))", context),
            json.dumps([value - 256 if value > 127 else value
                        for value in "中文".encode()], separators=(",", ":")))
        self.assertEqual(runtime.evaluate(
            "@js:String(new Packages.java.lang.String("
            "new Packages.java.lang.String('中文').getBytes('UTF-8'),'UTF-8'))",
            context), "中文")
        self.assertEqual(runtime.evaluate(
            "@js:String(new Packages.java.lang.String("
            "new Packages.java.lang.String('0123456789').getBytes('UTF-8'),2,3,'UTF-8'))",
            context), "234")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var s=new Packages.java.lang.String('abcd');"
            "return s.length()+'/'+s.length+'/'+(s+'!')+'/'+s.match(/b/)[0]})()",
            context), "4/4/abcd!/b")

    def test_java_util_arrays_copy_of_pads_and_truncates_like_java(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var A=Packages.java.util.Arrays;"
            "return JSON.stringify([Array.from(A.copyOf([1,2,3,4],2)),"
            "Array.from(A.copyOf([1],4))])})()", JsContext(source=source)),
            "[[1,2],[1,0,0,0]]")

    def test_java_util_zip_inflater_writes_into_the_caller_buffer(self):
        raw = b"chapter text " * 20
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = compressor.compress(raw) + compressor.flush()
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var b=" + json.dumps(list(payload)) + ";"
            "var out=Packages.java.util.Arrays.copyOf([]," + str(len(raw)) + ");"
            "var inf=new Packages.java.util.zip.Inflater(true);"
            "inf.setInput(b,0,b.length);var count=inf.inflate(out);inf.end();"
            "return count+':'+String(new Packages.java.lang.String(out,'UTF-8'))})()",
            JsContext(source=source)), f"{len(raw)}:" + raw.decode())

    def test_java_lang_system_reports_wall_clock_millis(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        before = time.time() * 1000
        value = runtime.evaluate(
            "@js:Packages.java.lang.System.currentTimeMillis()",
            JsContext(source=source))
        after = time.time() * 1000
        # QuickJS 读的是高精度时钟，Python 在 Windows 上的 time.time() 会落后
        # 它十几毫秒，逐毫秒卡窗口必翻车；这里只要求对得上墙钟。
        self.assertLess(abs(value - (before + after) / 2), 1000, value)
        self.assertEqual(runtime.evaluate(
            "@js:Packages.java.lang.System.getProperty('line.separator')",
            JsContext(source=source)), "\n")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var a=[0,0,0];"
            "Packages.java.lang.System.arraycopy([7,8],0,a,1,2);"
            "return a.join(',')})()", JsContext(source=source)), "0,7,8")

    def test_java_nio_byte_buffer_backs_the_key_schedules(self):
        """weiRead derives key/iv from ByteBuffer.allocate(n).array()."""
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var keyIv=Packages.java.nio.ByteBuffer.allocate(32).array();"
            "for(var i=0;i<32;i++){var mapped=(i*7)&0xff;"
            "keyIv[i]=mapped>127?mapped-256:mapped;}"
            "return keyIv.length+':'+keyIv[0]+','+keyIv[31]})()",
            JsContext(source=source)), "32:0,-39")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var b=Packages.java.nio.ByteBuffer.wrap([0,0,1,2]);"
            "return b.getInt()+'|'+b.remaining()})()", JsContext(source=source)),
            "258|0")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var b=Packages.java.nio.ByteBuffer.allocate(4);"
            "b.put([1,2]);b.flip();var out=[];var one=new Array(1);"
            "while(b.hasRemaining()){b.get(one);out.push(one[0]);}"
            "return out.join(',')+'|'+b.array().join(',')})()",
            JsContext(source=source)), "1,2|1,2,0,0")

    def test_java_zip_inflater_input_stream_drains_raw_deflate(self):
        raw = b"decoded chapter " * 12
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        payload = compressor.compress(raw) + compressor.flush()
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var input=" + json.dumps(list(payload)) + ";"
            "var stream=new Packages.java.util.zip.InflaterInputStream("
            "new Packages.java.io.ByteArrayInputStream(input),"
            "new Packages.java.util.zip.Inflater(true));"
            "var out=new Packages.java.io.ByteArrayOutputStream();"
            "var buffer=Packages.java.nio.ByteBuffer.allocate(64).array();"
            "var count;"
            "while((count=stream.read(buffer))!=-1)out.write(buffer,0,count);"
            "stream.close();"
            "return String(new Packages.java.lang.String(out.toByteArray(),'UTF-8'))})()",
            JsContext(source=source)), raw.decode())

    def test_java_byte_array_input_stream_reads_like_the_jdk(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var s=new Packages.java.io.ByteArrayInputStream([65,66,67,68]);"
            "var head=s.read();var buffer=new Array(2);var count=s.read(buffer);"
            "var skipped=s.skip(1);var tail=s.read();"
            "return [head,String.fromCharCode(count?buffer[0]:0),"
            "String.fromCharCode(buffer[1]),skipped,s.available(),tail].join('|')})()",
            JsContext(source=source)), "65|B|C|1|0|-1")

    def test_java_security_secure_random_stays_in_range(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var random=new Packages.java.security.SecureRandom();"
            "var bytes=new Array(16);random.nextBytes(bytes);"
            "var ok=true;for(var i=0;i<16;i++)"
            "if(typeof bytes[i]!=='number'||bytes[i]<-128||bytes[i]>127)ok=false;"
            "var tries=[];for(var n=0;n<200;n++){var v=random.nextInt(9);"
            "if(v<0||v>8)ok=false;tries.push(v);}"
            "return ok+'|'+new Packages.java.util.HashSet(tries).length})()",
            JsContext(source=source)), "true|9")

    def test_java_security_spec_and_interfaces_are_importable(self):
        """A 台湾小说网-style jsLib imports java.security.spec by package."""
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:var imports=new JavaImporter();"
            "imports.importPackage(Packages.java.security,"
            "Packages.java.security.interfaces,Packages.java.security.spec);"
            "(function(){with(imports){"
            "var spec=new X509EncodedKeySpec([1,2,3]);"
            "return typeof RSAPublicKey+'|'+spec.getFormat()+'|'"
            "+spec.getEncoded().join(',')+'|'+new SecureRandom().getAlgorithm()}})()",
            JsContext(source=source)), "function|X.509|1,2,3|NativePRNG")

    def test_java_jsoup_parser_selects_the_dialect(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var Parser=Packages.org.jsoup.parser.Parser;"
            "var doc=Packages.org.jsoup.Jsoup.parse("
            "'<div id=\"a\">hi</div>','',Parser.xmlParser());"
            "return Parser.htmlParser().getName()+':'+doc.select('div#a').text()})()",
            JsContext(source=source)), "html:hi")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){importClass(Packages.org.jsoup.Jsoup);"
            "return typeof Jsoup.parse})()", JsContext(source=source)), "function")

    def test_android_cookie_managers_share_the_cookie_store(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var manager=Packages.android.webkit.CookieManager"
            ".getInstance();"
            "Packages.io.legado.app.help.http.CookieStore.setCookie("
            "'https://example.com/','sid=1; token=2');"
            "return manager.getCookie('https://example.com/')"
            "+'|'+Packages.io.legado.app.help.http.CookieStore.getCookie("
            "'https://example.com/')})()",
            JsContext(source=source)), "sid=1; token=2|sid=1; token=2")

    def test_bitmap_factory_decode_stream_reads_pixels(self):
        from PIL import Image
        image = Image.new("RGB", (8, 5), (0xAB, 0x11, 0x22))
        image.putpixel((1, 0), (0xCD, 0x33, 0x44))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var bytes=" + json.dumps(list(buffer.getvalue())) + ";"
            "var bitmap=Packages.android.graphics.BitmapFactory.decodeStream("
            "new Packages.java.io.ByteArrayInputStream(bytes));"
            "if(bitmap===null)return 'null';"
            "return [bitmap.getWidth(),bitmap.getHeight(),"
            "(bitmap.getPixel(0,0)>>16)&255,(bitmap.getPixel(1,0)>>16)&255].join(',')})()",
            JsContext(source=source)), "8,5,171,205")
        self.assertEqual(runtime.evaluate(
            "@js:String(Packages.android.graphics.BitmapFactory.decodeStream("
            "new Packages.java.io.ByteArrayInputStream([])))",
            JsContext(source=source)), "null")

    def test_unimplemented_java_api_fails_loudly_with_its_path(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        for expression, path in (
                ("Packages.java.util.Objects.requireNonNull(1)",
                 "Packages.java.util.Objects.requireNonNull"),
                ("new Packages.java.nio.charset.Charset('x')",
                 "Packages.java.nio.charset.Charset"),
                ("Packages.com.example.Widget.missing()",
                 "Packages.com.example.Widget.missing"),
                ("Packages.java.lang.Class.forName('x')",
                 "Packages.java.lang.Class.forName")):
            value = runtime.evaluate(
                "@js:(function(){try{" + expression +
                ";return 'no-throw'}catch(e){return e.name+'|'+e.javaApi}})()",
                JsContext(source=source))
            self.assertEqual(value, f"UnsupportedJavaApiError|{path}", expression)

    def test_unimplemented_java_api_stays_probeable(self):
        """Feature detection reads typeof/truthiness; only the call throws."""
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:[typeof Packages.java.util.Objects,"
            "Boolean(Packages.java.util.Objects),"
            "typeof Packages,"
            "Object.keys(Packages.java.util).indexOf('ArrayList')>=0].join('|')",
            JsContext(source=source)), "function|true|object|true")
        self.assertEqual(runtime.evaluate(
            "@js:(function(){var out=typeof Packages!='undefined'"
            "?new Packages.java.util.ArrayList():[];out.add(1);return out.size()})()",
            JsContext(source=source)), 1)

    def test_java_api_inventory_covers_namespaces_classes_and_statics(self):
        from app.services.legado_js import implemented_java_api_paths
        paths = implemented_java_api_paths()
        for name in ("java.lang", "java.lang.System", "java.lang.System.currentTimeMillis",
                     "java.util.Arrays.copyOf", "java.nio.ByteBuffer.allocate",
                     "javax.crypto.Cipher.getInstance", "javax.crypto.spec.SecretKeySpec",
                     "org.jsoup.Jsoup.parse", "org.jsoup.parser.Parser.xmlParser",
                     "android.webkit.CookieManager.getInstance",
                     "io.legado.app.help.http.CookieStore.setCookie",
                     "cn.hutool.core.util.ZipUtil.unGzip"):
            self.assertIn(name, paths)
        # 未模拟的路径不会进清单，否则缺口就报不出来了。
        for name in ("java.util.Objects", "com.example.Widget",
                     "java.nio.charset.Charset"):
            self.assertNotIn(name, paths)

    def test_missing_java_package_scan_reports_full_reference_paths(self):
        from app.services.compat_audit import (scan_missing_java_packages,
                                              source_texts)
        self.assertEqual(scan_missing_java_packages([
            "@js:new Packages.com.acme.Tool().name",
            "<js>var a=Packages.java.util.Objects.requireNonNull(x)</js>"]),
            ["com.acme.Tool", "java.util.Objects.requireNonNull"])
        self.assertEqual(scan_missing_java_packages([
            "Packages.java.lang.String.valueOf(1)",
            "Packages.javax.crypto.spec.SecretKeySpec(Packages.android.util.Base64)"]), [])
        self.assertEqual(scan_missing_java_packages([]), [])
        self.assertEqual(list(source_texts(
            {"ruleSearch": {"bookList": "$.list"},
             "ruleToc": {"chapterList": ["@js:Packages.com.a.B.c()"]}})),
            ["$.list", "@js:Packages.com.a.B.c()"])

    def test_book_source_corpus_needs_no_unimplemented_java_api(self):
        from app.services.compat_audit import scan_missing_java_packages
        corpus = sorted(Path("resource").glob("*.json"))
        if not corpus:
            self.skipTest("resource/ 下没有书源库文件")
        for path in corpus:
            gaps = scan_missing_java_packages([path.read_text(encoding="utf-8")])
            self.assertEqual(gaps, [], f"{path.name} 需要未模拟的 API：{gaps}")

    def test_empty_login_maps_are_null_like_fixed_base_source(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[source.getLoginInfoMap(),source.getLoginHeaderMap()]',
            JsContext(source=source))
        self.assertEqual(value, [None, None])

    def test_injected_json_objects_use_java_map_string_form(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[String(result),JSON.stringify(result),result.id]',
            JsContext(source=source, result={"id": 123, "name": "Book"}))
        self.assertEqual(value, [
            "{id=123, name=Book}", '{"id":123,"name":"Book"}', 123])
        array_value = runtime.evaluate(
            '@js:[String(result),JSON.parse(result)[0].id,result.map(x=>x.id)]',
            JsContext(source=source, result=[{"id": 7}, {"id": 8}]))
        self.assertEqual(array_value, ['[{"id":7},{"id":8}]', 7, [7, 8]])

    def test_lowercase_json_template_extracts_field_without_js(self):
        async def exercise():
            engine = ReaderEngine()
            source = SimpleNamespace(
                to_dict=lambda: {}, bookSourceUrl="https://x")
            return engine._field_value(
                {"id": 42}, "https://x/book/{{@json:id}}/", "json", source,
                "")

        self.assertEqual(asyncio.run(exercise()), "https://x/book/42/")

    def test_double_at_template_runs_nested_selector_rule(self):
        async def exercise():
            engine = ReaderEngine()
            source = SimpleNamespace(
                to_dict=lambda: {}, bookSourceUrl="https://x")
            node = BeautifulSoup(
                '<div><span class="tag">Comic</span></div>',
                "html.parser").div
            return engine._field_value(
                node, "{{@@class.tag@text}}", "html", source, "")

        self.assertEqual(asyncio.run(exercise()), "Comic")

    def test_python_bridge_error_keeps_underlying_network_message(self):
        runtime = LegadoJsRuntime()
        runtime._ajax = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("network unavailable"))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        with self.assertRaisesRegex(Exception, "network unavailable"):
            runtime.evaluate('@js:java.ajax("/x")', JsContext(source=source))

    def test_put_bridge_error_keeps_underlying_state_message(self):
        runtime = LegadoJsRuntime()
        runtime._put = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("state serialization failed"))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        with self.assertRaisesRegex(Exception, "state serialization failed"):
            runtime.evaluate('@js:java.put("key", {value:1})',
                             JsContext(source=source))

    def test_http_response_bridge_exposes_legado_response_methods(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda _request, *_: json.dumps({
            "body": "ok", "headers": {"etag": "v1"},
            "headersMulti": {"etag": ["v1"]}, "message": "OK",
            "status": 200, "url": "https://final.example/", "responseKind": "str",
        })
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source.example"})
        value = runtime.evaluate(
            '@js:var r=java.connect("/x");'
            '[String(r),r.body(),r.headers().get("etag"),r.code(),r.message(),'
            'r.errorBody(),typeof r.header,r.raw().request().url()]',
            JsContext(source=source),
        )
        self.assertEqual(value, [
            "Response{protocol=http/1.1, code=200, message=OK, "
            "url=https://final.example/}", "ok", "v1", 200, "OK", None,
            "undefined", "https://final.example/",
        ])

    def test_transport_preserves_reason_and_repeated_response_headers(self):
        runtime = LegadoJsRuntime()

        class Response:
            status = 302
            reason = "Found"
            def __init__(self):
                self.headers = Message()
                self.headers["Location"] = "/first"
                self.headers["Location"] = "/second"
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b""
            def geturl(self): return "https://example.com/start"

        class Opener:
            def open(self, _request, timeout=None): return Response()

        with patch("app.services.legado_js.build_opener",
                   lambda *_handlers: Opener()):
            data = json.loads(runtime._request_json(
                'https://example.com/start,{"__rawJsoupRequest":true,'
                '"__followRedirects":false}', cookie_enabled=False))
        self.assertEqual(data["message"], "Found")
        self.assertEqual(data["headersMulti"]["location"], ["/first", "/second"])
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        value = json.loads(runtime.evaluate(
            '@js:result=JSON.stringify([result.statusMessage(),'
            'result.headers("Location"),result.header("Location"),'
            'result.headers().Location,result.headers().location])',
            JsContext(source=source, response_data=data))
        )["body"]
        self.assertEqual(json.loads(value),
                         ["Found", ["/first", "/second"], "/second",
                          "/second", "/second"])

    def test_js_analyze_url_webview_get_and_post_follow_fixed_order(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime.browser_fetcher = lambda url, _title, options: (
            calls.append((url, dict(options))) or {
                "body": "rendered", "url": url + "#done", "status": 200,
                "headers": {"Content-Type": "text/html"}})
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://example.com", "enabledCookieJar": False})

        get_value = runtime.evaluate(
            '@js:java.ajax("https://example.com/get,{\\"webView\\":true}")',
            JsContext(source=source))
        self.assertEqual(get_value, "rendered")
        self.assertEqual(calls[0][0], "https://example.com/get")
        self.assertNotIn("initialHtml", calls[0][1])

        class Response:
            status = 200
            reason = "OK"
            def __init__(self):
                self.headers = Message()
                self.headers["Content-Type"] = "text/html; charset=utf-8"
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b"preliminary"
            def geturl(self): return "https://example.com/post-final"
        class Opener:
            def open(self, _request, timeout=None): return Response()

        with patch("app.services.legado_js.build_opener",
                   lambda *_handlers: Opener()):
            post_value = runtime.evaluate(
                '@js:java.ajax("https://example.com/post,{\\"method\\":\\"POST\\",'
                '\\"body\\":\\"x=1\\",\\"webView\\":true}")',
                JsContext(source=source))
        self.assertEqual(post_value, "rendered")
        self.assertEqual(calls[1][0], "https://example.com/post-final")
        self.assertEqual(calls[1][1]["initialHtml"], "preliminary")

    def test_js_analyze_url_unzips_text_and_adds_xml_declaration(self):
        runtime = LegadoJsRuntime()
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("body.txt", "zip text")

        class Response:
            status = 200
            reason = "OK"
            def __init__(self, body, content_type):
                self._body = body
                self.headers = Message()
                self.headers["Content-Type"] = content_type
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return self._body
            def geturl(self): return "https://example.com/data"

        responses = iter((
            Response(archive.getvalue(), "application/zip"),
            Response(b"<root/>", "application/xml; charset=utf-8"),
        ))
        runtime.opener = SimpleNamespace(open=lambda *_args, **_kwargs: next(responses))
        zipped = json.loads(runtime._request_json("https://example.com/archive"))
        xml = json.loads(runtime._request_json("https://example.com/xml"))
        self.assertEqual(zipped["body"], "zip text")
        self.assertEqual(zipped["bodyBytes"], list(archive.getvalue()))
        self.assertEqual(xml["body"], '<?xml version="1.0"?><root/>')

    def test_java_get_disables_redirect_following_like_jsoup(self):
        runtime = LegadoJsRuntime()
        captured = []
        runtime._request_json = lambda request, *_args: (
            captured.append(request) or json.dumps({
                "body": "", "headers": {"location": "/result?id=42"},
                "status": 302, "url": "https://source/search",
                "responseKind": "jsoup"}))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.get("https://source/search", {accept:"text/html"})'
            '.header("location")', JsContext(source=source))
        request = json.loads(captured[0].split(",", 1)[1])
        self.assertEqual(value, "/result?id=42")
        self.assertIs(request["__followRedirects"], False)
        self.assertEqual(request["headers"]["accept"], "text/html")

    def test_java_post_disables_redirect_following_like_legado_js_extensions(self):
        runtime = LegadoJsRuntime()
        captured = []
        runtime._request_json = lambda request, *_args: (
            captured.append(request) or json.dumps({
                "body": "", "headers": {"location": "/result?id=43"},
                "status": 302, "url": "https://source/search",
                "responseKind": "jsoup"}))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})

        value = runtime.evaluate(
            '@js:java.post("https://source/search", "q=book", {})'
            '.header("location")', JsContext(source=source))

        request = json.loads(captured[0].split(",", 1)[1])
        self.assertEqual(value, "/result?id=43")
        self.assertIs(request["__followRedirects"], False)
        self.assertEqual(request["method"], "POST")

    def test_analyze_url_get_response_requests_current_rule_url(self):
        runtime = LegadoJsRuntime()
        requested = []

        def respond(value, *_):
            requested.append(value)
            return json.dumps({"body": "fetched", "bodyBytes": [], "headers": {},
                               "status": 200, "url": str(value)})

        runtime._request_json = respond
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate('@js:java.getResponse().body().string()', JsContext(
            source=source, host_kind="url", base_url="https://source/current"))
        self.assertEqual(value, "fetched")
        self.assertEqual(requested, ["https://source/current"])

    def test_analyze_url_okhttp_response_and_sync_method_surface(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda value, *_args: json.dumps({
            "body": "bytes", "bodyBytes": [98, 121, 116, 101, 115],
            "headers": {"content-type": "text/plain"}, "status": 201,
            "message": "Created", "url": value, "protocol": "http/1.1"})
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            var response=java.getResponse(),body=response.body();
            [response.code(),response.message(),body.string(),body.bytes(),
             body.byteStream().read(),body.contentType(),String(response.close()),
             typeof response.errorBody,typeof response.raw,
             type(()=>java.getResponse(1)),type(()=>java.getByteArray(1)),
             type(()=>java.getInputStream(1)),type(()=>java.getStrResponse(1,2,3,4))];''',
            JsContext(source=source, host_kind="url",
                      base_url="https://source.test/data"))
        self.assertEqual(value[:3], [201, "Created", "bytes"])
        self.assertEqual(bytes(item & 255 for item in value[3]), b"bytes")
        self.assertEqual(value[4:], [
            98, "text/plain", "undefined", "undefined", "undefined",
            True, True, True, True,
        ])

    def test_analyze_url_sync_webview_arguments_and_data_uri_boundary(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime._request_json = lambda *args: (
            calls.append(args) or json.dumps({
                "body": "ok", "bodyBytes": [111, 107], "headers": {},
                "status": 200, "url": str(args[0])}))
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        context = JsContext(source=source, host_kind="url",
                            base_url="https://source.test/data")
        self.assertEqual(runtime.evaluate(
            '@js:java.getStrResponse("run()","ready",false).body()', context), "ok")
        self.assertEqual(calls[0][5:], (False, "run()", "ready"))
        runtime.evaluate('@js:java.getResponse()', context)
        runtime.evaluate('@js:java.getByteArray()', context)
        runtime.evaluate('@js:java.getInputStream()', context)
        self.assertTrue(all(call[5] is False for call in calls[1:]))

        data_context = JsContext(
            source=source, host_kind="url",
            base_url="data:text/plain;base64,aGVsbG8=?ignored")
        value = runtime.evaluate(
            '@js:[java.getByteArray(),java.getInputStream().read()]', data_context)
        self.assertEqual(bytes(item & 255 for item in value[0]), b"hello")
        self.assertEqual(value[1], ord("h"))
        self.assertEqual(len(calls), 4)

    def test_js_ajax_resolves_dynamic_and_login_source_headers_on_call(self):
        runtime = LegadoJsRuntime()
        captured = []
        runtime._request_json = lambda value, headers=None, *_args: (
            captured.append((value, headers)) or json.dumps({
                "body": "ok", "bodyBytes": [], "headers": {}, "status": 200,
                "url": value}))
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '@js:JSON.stringify({"X-Dynamic":baseUrl})'})
        runtime.evaluate(
            '@js:source.putLoginHeader(\'{"Authorization":"Bearer token"}\');'
            'java.ajax("/page")', JsContext(source=source))
        self.assertEqual(captured[0][1]["X-Dynamic"],
                         "https://source.example")
        self.assertEqual(captured[0][1]["Authorization"], "Bearer token")

    def test_java_host_surface_is_context_specific(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        rule = runtime.evaluate(
            "@js:[typeof java.getString,typeof java.getStrResponse]",
            JsContext(source=source))
        url = runtime.evaluate(
            "@js:[typeof java.getString,typeof java.getStrResponse,typeof java.notOfficial]",
            JsContext(source=source, host_kind="url"))
        self.assertEqual(rule, ["function", "undefined"])
        self.assertEqual(url, ["undefined", "function", "undefined"])

    def test_java_host_bindings_are_context_specific(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        chapter = ChapterItem(name="C", url="/1")
        rule = runtime.evaluate(
            "@js:[typeof key,typeof page,typeof speakText,typeof chapter,typeof src]",
            JsContext(source=source, key="k", page=2, speak_text="s",
                      chapter=chapter, src="body"))
        url = runtime.evaluate(
            "@js:[typeof key,typeof page,typeof speakText,typeof chapter,typeof src]",
            JsContext(source=source, host_kind="url", key="k", page=2,
                      speak_text="s", chapter=chapter, src="body"))
        self.assertEqual(rule, ["undefined", "undefined", "undefined",
                                "object", "string"])
        self.assertEqual(url, ["string", "number", "string",
                               "undefined", "undefined"])

    def test_base_source_eval_js_uses_source_host_and_fresh_bindings(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:source.evalJS('[java===source,baseUrl,typeof result,typeof book]')",
            JsContext(source=source, result="outer", book=SimpleNamespace(name="B")))
        self.assertEqual(value, [True, "https://source", "undefined", "undefined"])

    def test_analyze_rule_get_put_uses_fixed_variable_fallback_order(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        book = BookItem(name="Book", variables={"shared": "book", "bookOnly": "b"})
        chapter = ChapterItem(name="Chapter", url="/1",
                              variables={"shared": "chapter"})
        variables = {"shared": "rule", "ruleOnly": "r",
                     "source:data:sourceOnly": "s"}
        value = runtime.evaluate('''@js:
            var before=[java.get('bookName'),java.get('title'),java.get('shared'),
              java.get('bookOnly'),java.get('ruleOnly'),java.get('sourceOnly')];
            java.put('written','value');
            before.concat([chapter.getVariable('written'),book.getVariable('written')]);
        ''', JsContext(source=source, variables=variables, book=book, chapter=chapter))
        self.assertEqual(value, ["Book", "Chapter", "chapter", "b", "r", "s",
                                 "value", ""])
        self.assertEqual(chapter.variables["written"], "value")
        self.assertNotIn("written", book.variables)
        self.assertNotIn("written", variables)

    def test_analyze_url_get_put_stops_after_rule_data(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        runtime.evaluate("@js:source.put('sourceOnly','s')", JsContext(source=source))
        no_rule_data = runtime.evaluate(
            "@js:[java.put('x','v'),java.get('x'),java.get('sourceOnly')]",
            JsContext(source=source, host_kind="url"))
        book = BookItem(name="Book")
        with_rule_data = runtime.evaluate(
            "@js:[java.put('x','v'),java.get('x'),java.get('bookName')]",
            JsContext(source=source, host_kind="url", book=book,
                      rule_data_present=True, variables={}))
        self.assertEqual(no_rule_data, ["v", "", ""])
        self.assertEqual(with_rule_data, ["v", "v", "Book"])
        self.assertEqual(book.variables["x"], "v")

    def test_rule_data_object_methods_and_java_put_share_variable_map(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        book = BookItem(name="Book", variables={"initial": "b"})
        chapter = ChapterItem(name="Chapter", url="/1",
                              variables={"initial": "c"})
        value = runtime.evaluate('''@js:
          var before=[book.getVariable('initial'),chapter.getVariable('initial')];
          chapter.putVariable('direct','yes');java.put('throughJava','yes');
          chapter.putVariable('initial',null);
          before.concat([chapter.getVariable('direct'),chapter.getVariable('throughJava')]);
        ''', JsContext(source=source, book=book, chapter=chapter))
        self.assertEqual(value, ["b", "c", "yes", "yes"])
        self.assertEqual(chapter.variables, {"direct": "yes", "throughJava": "yes"})
        self.assertEqual(book.variables, {"initial": "b"})

    def test_book_and_chapter_variable_methods_match_rule_data_interface(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {})
        book, chapter = BookItem(name="B"), ChapterItem(name="C", url="/1")
        value = runtime.evaluate('''@js:
          var a=book.putVariable('x',7),b=chapter.putVariable('y',false);
          var custom=book.putCustomVariable('z');book.putVariable('x',null);
          function npe(call){try{call();return false}catch(e){return e.javaException
            instanceof Packages.java.lang.NullPointerException}}
          var reverse=book.setReverseToc(true);
          [a,b,custom,book.getVariable('x'),chapter.getVariable('y'),
           book.getCustomVariable(),npe(()=>book.getVariable(null)),reverse,
           book.getReverseToc(),typeof book.getDisplayTag];
        ''', JsContext(source=source, book=book, chapter=chapter))
        self.assertEqual(value, [True, True, None, "", "false", "z", True,
                                 None, True, "undefined"])
        self.assertEqual(book.variables, {"custom": "z"})
        self.assertEqual(chapter.variables, {"y": "false"})

    def test_analyze_rule_source_fallback_persists_between_scopes(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        runtime.evaluate("@js:java.put('token','saved')", JsContext(source=source))
        self.assertEqual(runtime.evaluate(
            "@js:java.get('token')", JsContext(source=source)), "saved")

    def test_js_requests_inherit_source_headers(self):
        runtime = LegadoJsRuntime()
        captured = {}
        def request(value, headers=None, *_):
            captured.update(headers or {})
            return json.dumps({"body": "ok", "headers": {}, "status": 200,
                               "url": str(value)})
        runtime._request_json = request
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "header": '{"X-Source":"enabled"}'})
        value = runtime.evaluate('@js:java.ajax("/path")', JsContext(source=source))
        self.assertEqual(value, "ok")
        self.assertEqual(captured["X-Source"], "enabled")
        self.assertIn("Mozilla/5.0", captured["User-Agent"])

    def test_http_response_cookie_map_and_java_collection_views(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda _request, *_: json.dumps({
            "body": "", "headers": {"set-cookie": "sid=abc; Path=/"},
            "status": 200, "url": "https://source/", "responseKind": "jsoup"})
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var cookies=java.head("https://source",{}).cookies();
            var values=[0,1,2,3];values.subList(1,3).clear();
            [cookies.get("sid"),cookies.entrySet().toArray().join("; "),
             String(cookies),values];''', JsContext(source=source))
        self.assertEqual(value, ["abc", "sid=abc", "{sid=abc}", [0, 3]])

    def test_upstream_state_url_and_response_apis(self):
        source = SimpleNamespace(bookSourceUrl="https://example.com/root/",
                                 to_dict=lambda: {"bookSourceUrl": "https://example.com/root/"})
        runtime = LegadoJsRuntime()
        value = runtime.evaluate("""
            source.putLoginHeader('{"X-Test":"yes"}');
            source.putLoginInfo('{"name":"reader"}');
            var parsed=java.toURL('../book?id=7',baseUrl);
            var before=[source.getLoginHeaderMap().get('X-Test'),
              source.getLoginInfoMap().get('name'),parsed.host,parsed.pathname,
              parsed.searchParams.get('id'),result];
            source.removeLoginHeader();source.removeLoginInfo();
            cache.putMemory('temporary','value');cache.deleteMemory('temporary');
            before.concat([source.getLoginHeader(),source.getLoginInfo(),cache.getFromMemory('temporary')]);
        """, JsContext(source=source, result="payload", base_url=source.bookSourceUrl))
        self.assertEqual(value, ["yes", "reader", "example.com", "/book", "7",
                                 "payload", None, None, None])

    def test_base_source_methods_match_fixed_legado_null_and_return_semantics(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "bookSourceName": "Name",
            "loginUi": '[{"name":"Account","type":"text"}]',
            "loginUrl": "@js:function login(){source.put('called','yes')}"
        })
        value = runtime.evaluate('''@js:
            var absent=[source.getLoginHeader(),source.getLoginHeaderMap(),
              source.getLoginInfo(),source.getLoginInfoMap()];
            var setReturn=source.setVariable(null);
            var infoReturn=source.putLoginInfo('{"id":"7"}');
            source.login();
            [source.getTag(),source.getKey(),source.getSource()===source,
             source.loginUi()[0].name,source.getLoginJs().startsWith('function'),
             source.get('called'),setReturn,infoReturn,source.evalJS('typeof login')].concat(absent);
        ''', JsContext(source=source))
        self.assertEqual(value, ["Name", "https://source", True, "Account", True,
                                 "yes", None, True, "undefined",
                                 None, None, None, None])

    def test_book_source_methods_are_visible_in_javascript(self):
        runtime = LegadoJsRuntime()
        source = BookSource(bookSourceName="Name", bookSourceUrl="https://source",
                            bookSourceGroup="group")
        expected_hash = 0
        for char in source.bookSourceUrl:
            expected_hash = ((31 * expected_hash + ord(char) + 2**31) % 2**32) - 2**31
        value = runtime.evaluate('''@js:
            var search=source.getSearchRule();search.name='h1@text';
            [source.tag,source.getDisPlayNameGroup(),source.hashCode(),
             source.equals({bookSourceUrl:'https://source'}),
             source.equals({bookSourceUrl:'https://other'}),source.ruleSearch.name,
             source.equal(source)];
        ''', JsContext(source=source))
        self.assertEqual(value, ["Name", "Name (group)", expected_hash,
                                 True, False, "h1@text", True])
        self.assertEqual(source.ruleSearch["name"], "h1@text")

    def test_every_audited_source_method_exists_on_runtime_bridge(self):
        runtime = LegadoJsRuntime()
        source = BookSource(bookSourceUrl="https://source")
        method_names = sorted(SUPPORTED_OBJECT_APIS["source"])
        missing = [name for name in method_names if runtime.evaluate(
            f"@js:typeof source.{name}", JsContext(source=source)) != "function"]
        self.assertEqual(missing, [])

    def test_book_source_group_methods_are_visible_and_sync_to_python(self):
        runtime = LegadoJsRuntime()
        source = BookSource(
            bookSourceName="Name", bookSourceUrl="https://source",
            bookSourceGroup="正常；已失效", variableComment="变量说明",
        )
        value = runtime.evaluate('''@js:
            var same=source.addGroup('新组，校验超时')===source;
            var before=[source.hasGroup('新组'),source.getInvalidGroupNames()];
            source.removeInvalidGroups();source.removeGroup('新组');
            [same,before,source.bookSourceGroup,
             source.getDisplayVariableComment('其他说明')];
        ''', JsContext(source=source))
        self.assertEqual(value, [True, [True, "已失效, 校验超时"], "正常",
                                 "变量说明\n其他说明"])
        self.assertEqual(source.bookSourceGroup, "正常")
        empty = BookSource(bookSourceUrl="https://empty", bookSourceGroup=None)
        null_branch = runtime.evaluate('''@js:
            source.removeGroup('x');var before=source.bookSourceGroup;
            source.addGroup('a； b');[before,source.bookSourceGroup];
        ''', JsContext(source=empty))
        self.assertEqual(null_branch, [None, "a； b"])
        self.assertEqual(empty.bookSourceGroup, "a； b")

    def test_source_login_ui_uses_fixed_row_ui_gson_shape(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "loginUi": json.dumps([{
                "name": 7, "type": False, "action": {"x": 1},
                "unknown": "discard",
            }]),
        })
        value = runtime.evaluate('''@js:
            var row=source.loginUi()[0];
            [row.name,row.type,row.action,row.unknown];
        ''', JsContext(source=source))
        self.assertEqual(value, ["7", "false", '{"x":1}', None])
        invalid = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "loginUi": '["row"]',
        })
        self.assertIsNone(runtime.evaluate("@js:source.loginUi()",
                                           JsContext(source=invalid)))

    def test_source_non_null_string_parameters_and_public_surface(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        value = runtime.evaluate('''@js:
            function fails(call){try{call();return false}catch(e){
              return e.javaException instanceof Packages.java.lang.NullPointerException}}
            [typeof source.putVariable,
             fails(()=>source.put(null,'v')),fails(()=>source.put('k',null)),
             fails(()=>source.get(null)),fails(()=>source.putLoginHeader(null)),
             fails(()=>source.putLoginInfo(null)),
             source.setVariable(undefined),source.getVariable(),
             source.put('undefined',undefined),source.get('undefined'),
             (()=>{try{source.get();return false}catch(e){return e instanceof TypeError}})()];
        ''', JsContext(source=source))
        self.assertEqual(value, ["undefined", True, True, True, True, True,
                                 None, "undefined", "undefined", "undefined", True])

    def test_put_login_header_replaces_source_cookie_and_remove_clears_it(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source.example/path"})
        value = runtime.evaluate('''@js:
            source.putLoginHeader('{"Cookie":"sid=abc"}');
            var before=cookie.getKey(source.getKey(),'sid');
            source.removeLoginHeader();
            [before,cookie.getKey(source.getKey(),'sid'),source.getLoginHeader()];
        ''', JsContext(source=source))
        self.assertEqual(value, ["abc", "", None])

    def test_source_get_header_map_evaluates_dynamic_header_and_default_ua(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '@js:JSON.stringify({"X-Key":source.getKey()})',
        })
        value = runtime.evaluate(
            '@js:[source.getHeaderMap().get("X-Key"),'
            'source.getHeaderMap().containsKey("User-Agent")]',
            JsContext(source=source))
        self.assertEqual(value, ["https://source.example", True])
        self.assertEqual(runtime.evaluate(
            '@js:source.getHeaderMap().get("User-Agent")', JsContext(source=source)),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36")

    def test_source_dynamic_header_stringifies_before_gson_parse(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '@js:({"X-Extra":"object"})',
        })
        value = runtime.evaluate(
            '@js:[source.getHeaderMap().get("X-Extra"),'
            'source.getHeaderMap().containsKey("User-Agent")]',
            JsContext(source=source))
        self.assertEqual(value, [None, True])

    def test_source_dynamic_header_propagates_script_and_block_errors(self):
        runtime = LegadoJsRuntime()
        throwing = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '@js:throw new Error("header failed")',
        })
        with self.assertRaisesRegex(JsBridgeError, "header failed"):
            runtime.evaluate("@js:source.getHeaderMap()", JsContext(source=throwing))
        malformed_header = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example", "header": "<js>({})",
        })
        with self.assertRaisesRegex(JsBridgeError, "substring range"):
            runtime.evaluate("@js:source.getHeaderMap()",
                             JsContext(source=malformed_header))
        malformed_login = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example", "loginUrl": "<js>login()",
        })
        with self.assertRaisesRegex(JsBridgeError, "substring range"):
            runtime.evaluate("@js:source.getLoginJs()", JsContext(source=malformed_login))

    def test_source_get_header_map_optionally_merges_login_header(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example", "header": '{"A":"source"}'})
        value = runtime.evaluate('''@js:
            source.putLoginHeader('{"A":"login","B":"extra"}');
            [source.getHeaderMap().get('A'),source.getHeaderMap().get('B'),
             source.getHeaderMap(true).get('A'),source.getHeaderMap(true).get('B')];
        ''', JsContext(source=source))
        self.assertEqual(value, ["source", None, "login", "extra"])

    def test_url_context_resolves_a_header_script_that_calls_js_extensions(self):
        # AnalyzeUrl resolves source.getHeaderMap(true) while it is being built.
        # 霹雳书屋's header calls java.getWebViewUA(), so resolving it before the
        # bridge finished defining java failed the whole search with
        # "TypeError: not a function".
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '@js:\nJSON.stringify({"User-Agent": java.getWebViewUA(),\n'
                      '  "Accept": "text/html"})',
        })
        value = runtime.evaluate(
            "@js:[String(java.headerMap.get('User-Agent')),"
            " String(java.headerMap.get('Accept'))]",
            JsContext(source=source, host_kind="url", key="key", page=1,
                      base_url="https://source.example"))
        self.assertEqual(value[1], "text/html")
        self.assertTrue(value[0].startswith("Mozilla/5.0"), value[0])

    def test_url_context_resolves_a_header_script_that_calls_the_js_lib(self):
        # SharedJsScope evaluates the jsLib before the rule scope, so a header
        # may call library helpers.  点众阅读's header reads a constant the
        # jsLib declares; resolving it inside the bootstrap failed with
        # "ReferenceError: 'AES_KEY' is not defined".
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "jsLib": 'const AES_KEY = "lib-secret";\n'
                     'function libToken() { return AES_KEY + "-token"; }',
            "header": '@js:\nJSON.stringify({"X-Token": libToken()})',
        })
        value = runtime.evaluate(
            "@js:String(java.headerMap.get('X-Token'))",
            JsContext(source=source, host_kind="url", key="key", page=1,
                      base_url="https://source.example"))
        self.assertEqual(value, "lib-secret-token")

    def test_documented_chapter_and_tts_script_globals(self):
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        chapter = SimpleNamespace(name="Chapter One")
        runtime = LegadoJsRuntime()
        rule_value = runtime.evaluate(
            "[title,nextChapterUrl,typeof speakText,typeof speakSpeed]",
            JsContext(source=source, chapter=chapter,
                      next_chapter_url="https://source/chapter-2",
                      speak_text="hello", speak_speed=12))
        url_value = runtime.evaluate(
            "[typeof title,typeof nextChapterUrl,speakText,speakSpeed]",
            JsContext(source=source, host_kind="url", chapter=chapter,
                      next_chapter_url="https://source/chapter-2",
                      speak_text="hello", speak_speed=12))
        self.assertEqual(rule_value, ["Chapter One", "https://source/chapter-2",
                                      "undefined", "undefined"])
        self.assertEqual(url_value, ["undefined", "undefined", "hello", 12])

    def test_get_string_overloads_parse_supplied_content(self):
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = LegadoJsRuntime().evaluate(
            '''var json='{"data":{"content":"fresh"}}';
               var html='<a href="../next">Next</a><a href="/last">Last</a>';
               [java.getString('$.data.content',json),
                java.getString('a@href',html,true),
                java.getStringList('a@href',html,true)];''',
            JsContext(source=source, base_url="https://source/book/1",
                      get_string=lambda _rule: "old"))
        self.assertEqual(value, ["fresh", "https://source/next",
                                 ["https://source/next", "https://source/last"]])

    def test_rhino_escape_unescape_and_encode_uri(self):
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = LegadoJsRuntime().evaluate(
            "[escape('书 A'),unescape('%u4E66%20A'),java.encodeURI('https://x/a b?q=书') ]",
            JsContext(source=source))
        self.assertEqual(value, ["%u4E66%20A", "书 A",
                                 "https%3A%2F%2Fx%2Fa+b%3Fq%3D%E4%B9%A6"])

    def test_js_http_bridge_decodes_data_uri_and_type_as_hex(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:java.ajax('data:text/plain;base64,5Lmm5rqQ,{"type":"token"}')''',
            JsContext(source=source))
        self.assertEqual(bytes.fromhex(value).decode("utf-8"), "书源")

    def test_http_error_is_exposed_as_response_instead_of_exception(self):
        runtime = LegadoJsRuntime()
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"

        class Opener:
            def open(self, request, timeout=None):
                raise HTTPError(request.full_url, 403, "Forbidden", headers,
                                BytesIO("验证页面".encode()))

        runtime.opener = Opener()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var r=java.connect("https://source/protected");[r.code(),r.body()]',
            JsContext(source=source),
        )
        self.assertEqual(value, [403, "验证页面"])

    def test_login_check_can_replace_response_with_replayed_request(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda _request, *_: json.dumps({
            "body": "verified", "headers": {"content-type": "text/plain"},
            "status": 200, "url": "https://source/final",
        })
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:if(result.code()===403){result=java.getStrResponse()} result',
            JsContext(
                source=source, host_kind="url", base_url="https://source/start",
                response_data={"body": "blocked", "headers": {},
                               "status": 403, "url": "https://source/start"},
            ),
        )
        self.assertEqual(json.loads(value)["body"], "verified")

    def test_cache_and_direct_object_mutations_survive_evaluations(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            bookSourceUrl="https://source", bookSourceName="source",
            to_dict=lambda: {"bookSourceUrl": "https://source", "bookSourceName": "source"},
        )
        book = BookItem(name="old", url="https://source/old")
        context = JsContext(source=source, book=book)
        aliases = runtime.evaluate(
            '@js:cache.put("object", {value: 9}, 60);'
            'var before=[book.bookUrl,book.origin,book.originName];'
            'book.name="new";book.bookUrl="https://source/new";'
            'book.coverUrl="https://source/cover.jpg";book.totalChapterNum=12;before;', context)
        self.assertEqual(aliases, ["https://source/old", "https://source", "source"])
        self.assertEqual(book.name, "new")
        self.assertEqual(book.url, "https://source/new")
        self.assertEqual(book.cover_url, "https://source/cover.jpg")
        self.assertEqual(book.total_chapter_num, 12)
        display = runtime.evaluate(
            '@js:book.kind="玄幻,冒险";book.wordCount="10万字";'
            'book.customTag="收藏";book.customCoverUrl="custom.jpg";'
            'book.durChapterIndex=3;book.totalChapterNum=8;'
            '[book.getKindList(),book.customTag,book.getDisplayCover(),'
            'book.getUnreadChapterNum()]', JsContext(source=source, book=book))
        self.assertEqual(display,
                         [["10万字", "玄幻", "冒险"], "收藏", "custom.jpg", 4])
        self.assertEqual(book.custom_tag, "收藏")
        self.assertEqual(book.custom_cover_url, "custom.jpg")
        self.assertEqual(runtime.evaluate(
            '@js:cache.get("object").value', JsContext(source=source)), 9)

        chapter = ChapterItem("old title", "chapter/1", base_url="https://source/book/")
        chapter_value = runtime.evaluate(
            '@js:var before=[chapter.title,chapter.chapterUrl,chapter.baseUrl];'
            'chapter.title="new title";chapter.bookUrl=book.bookUrl;'
            'chapter.resourceUrl="https://cdn/audio";chapter.isVolume=true;'
            'chapter.startFragmentId="start";chapter.endFragmentId="end";'
            'book.latestChapterTitle="latest";before;',
            JsContext(source=source, book=book, chapter=chapter))
        self.assertEqual(chapter_value,
                         ["old title", "chapter/1", "https://source/book/"])
        self.assertEqual(chapter.name, "new title")
        self.assertEqual(chapter.book_url, "https://source/new")
        self.assertEqual(chapter.resource_url, "https://cdn/audio")
        self.assertEqual(chapter.start_fragment_id, "start")
        self.assertEqual(chapter.end_fragment_id, "end")
        self.assertEqual(book.latest_chapter_title, "latest")
        self.assertTrue(chapter.is_volume)

    def test_cookie_store_set_get_and_remove(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        value = runtime.evaluate(
            '@js:cookie.setCookie(baseUrl,"sid=abc");'
            '[cookie.getCookie(baseUrl),cookie.getKey(baseUrl,"sid"),'
            'java.getCookie(baseUrl,"sid"),java.getCookie(baseUrl,null)]',
            JsContext(source=source, base_url="https://example.com/book/1"),
        )
        self.assertEqual(value, ["sid=abc", "abc", "abc", "sid=abc"])
        self.assertIsNone(runtime.evaluate(
            '@js:cookie.removeCookie(baseUrl)',
            JsContext(source=source, base_url="https://example.com/book/1"),
        ))
        self.assertEqual(runtime.export_cookie_header("https://example.com/"), "")

    def test_cookie_store_persists_and_does_not_invent_secure_attribute(self):
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        with TemporaryDirectory() as directory:
            first = LegadoJsRuntime(state_dir=directory)
            first.evaluate(
                '@js:cookie.setCookie(baseUrl,"sid=abc");',
                JsContext(source=source, base_url="https://example.com/book"))
            self.assertEqual(first.export_cookie_header(
                "http://example.com/next"), "sid=abc")
            second = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(second.export_cookie_header(
                "https://example.com/next"), "sid=abc")
            second.remove_cookie("https://example.com/")
            third = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(third.export_cookie_header(
                "https://example.com/next"), "")

    def test_cookie_store_set_replaces_and_replace_cookie_merges_persistent_map(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://example.com"})
        value = runtime.evaluate('''@js:
            var setResult=cookie.setCookie(baseUrl,
              "a=1; blank=; literal=null; flag; Path=/");
            var first=cookie.getCookie(baseUrl);
            var replaceResult=cookie.replaceCookie(baseUrl,"a=2; b=3");
            var second=cookie.getCookie(baseUrl);
            cookie.setCookie(baseUrl,"only=1");
            [typeof setResult,typeof replaceResult,first,second,
             cookie.getCookie(baseUrl)];''',
            JsContext(source=source, base_url="https://example.com/"))
        self.assertEqual(value, [
            "undefined", "undefined", "a=1; literal=null; Path=/",
            "a=2; literal=null; Path=/; b=3", "only=1",
        ])

    def test_cookie_store_get_prunes_random_keys_to_fixed_4096_limit(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime.store_set_cookie("https://example.com/", "; ".join(
                f"k{index}=" + str(index) * 600 for index in range(8)))
            with patch("app.services.legado_js.random.choice",
                       side_effect=lambda values: values[0]):
                cookie = runtime.export_cookie_header("https://example.com/")
            self.assertLessEqual(len(cookie), 4096)
            self.assertNotIn("k0=", cookie)
            restored = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(restored.export_cookie_header("https://example.com/"),
                             cookie)

    def test_cookie_store_interface_arity_null_and_map_methods(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://example.com"})
        value = runtime.evaluate('''@js:
            var parsed=cookie.cookieToMap("a=1; blank=; literal=null; flag");
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            [parsed.get("a"),parsed.containsKey("blank"),parsed.get("literal"),
             cookie.mapToCookie({a:1,b:"x"}),cookie.mapToCookie({}),
             cookie.mapToCookie(null),npe(()=>cookie.getCookie(null)),
             npe(()=>cookie.getKey(baseUrl,null)),npe(()=>cookie.setCookie(null,"a=1")),
             npe(()=>cookie.replaceCookie(baseUrl,null)),npe(()=>cookie.removeCookie(null)),
             npe(()=>cookie.cookieToMap(null)),type(()=>cookie.getCookie()),
             type(()=>cookie.setCookie(baseUrl))];''',
            JsContext(source=source, base_url="https://example.com/"))
        self.assertEqual(value, [
            "1", False, "null", "a=1; b=x", None, None,
            True, True, True, True, True, True, True, True,
        ])

    def test_cookie_store_clear_deletes_persistent_table_but_keeps_session(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime.store_set_cookie("https://example.com/", "saved=disk")
            runtime.set_cookie("https://example.com/", "sid=memory", session=True)
            source = SimpleNamespace(to_dict=lambda: {
                "bookSourceUrl": "https://example.com"})
            value = runtime.evaluate('''@js:
                var result=cookie.clear();
                function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
                [String(result),cookie.getCookie(baseUrl),type(()=>cookie.clear(1))];''',
                JsContext(source=source, base_url="https://example.com/"))
            self.assertEqual(value, ["undefined", "sid=memory", True])
            restored = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(restored.export_cookie_header("https://example.com/"), "")

    def test_js_extensions_to_url_and_log_type_public_contract(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://example.com/base/page"})
        value = runtime.evaluate('''@js:
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            var one=java.toURL("https://one.test/child?q=1");
            var two=java.toURL("child","https://host.test/root/");
            [one.host,one.pathname,one.searchParams.get("q"),two.host,two.pathname,
             String(java.logType(null)),npe(()=>java.toURL(null)),
             type(()=>java.toURL()),type(()=>java.toURL("a","b","c")),
             type(()=>java.logType())];''',
            JsContext(source=source, base_url="https://example.com/base/page"))
        self.assertEqual(value, [
            "one.test", "/child", "1", "host.test", "/root/child", "undefined",
            True, True, True, True,
        ])

    def test_response_cookie_session_and_persistent_layers_match_cookie_manager(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime.set_cookie("https://www.example.com/", "sid=persistent; Path=/")
            headers = Message()
            headers["Set-Cookie"] = "sid=session; Path=/private; Secure"
            headers["Set-Cookie"] = "temporary=memory; Path=/private"
            headers["Set-Cookie"] = "saved=disk; Max-Age=3600; Path=/private; Secure"
            runtime.save_response_cookies("https://www.example.com/private", headers)

            # CookieManager discards Path/Secure for its key/value stores and
            # overlays the in-memory session map over the persistent DB map.
            self.assertEqual(runtime.export_cookie_header(
                "http://api.example.com/other"),
                "sid=session; temporary=memory; saved=disk")
            state = json.loads((Path(directory) / "rule_engine_state.json").read_text(
                encoding="utf-8"))
            persisted = {item["name"]: item["value"] for item in state["cookies"]}
            self.assertEqual(persisted, {"sid": "persistent", "saved": "disk"})

            restored = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(restored.export_cookie_header(
                "http://api.example.com/other"), "sid=persistent; saved=disk")

    def test_js_requests_disable_jar_but_still_send_cookie_store(self):
        runtime = LegadoJsRuntime()
        runtime.set_cookie("https://example.com/", "sid=old; Path=/")
        captured = {}

        class Response:
            status = 200

            def __init__(self):
                self.headers = Message()
                self.headers["Content-Type"] = "text/plain; charset=utf-8"
                self.headers["Set-Cookie"] = "sid=new; Path=/"

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b"ok"
            def geturl(self): return "https://example.com/page"

        class Opener:
            def open(self, request, timeout=None):
                captured["cookie"] = request.get_header("Cookie")
                return Response()

        def opener(*handlers):
            captured["handlers"] = handlers
            return Opener()

        for enabled in (False, None):
            captured.clear()
            source = SimpleNamespace(to_dict=lambda enabled=enabled: {
                "bookSourceUrl": "https://example.com",
                "enabledCookieJar": enabled,
            })
            with patch("app.services.legado_js.build_opener", opener):
                value = runtime.evaluate(
                    '@js:java.ajax("https://example.com/page")',
                    JsContext(source=source),
                )
            self.assertEqual(value, "ok")
            self.assertEqual(captured["cookie"], "sid=old")
            self.assertFalse(any(type(item).__name__ == "HTTPCookieProcessor"
                                 for item in captured["handlers"]))
            self.assertEqual(runtime.export_cookie_header(
                "https://example.com/next"), "sid=old")

    def test_direct_jsoup_request_with_disabled_jar_does_not_load_cookie_store(self):
        runtime = LegadoJsRuntime()
        runtime.set_cookie("https://example.com/", "sid=stored")
        captured = {}

        class Response:
            status = 200
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b"ok"
            def geturl(self): return "https://example.com/"

        class Opener:
            def open(self, request, timeout=None):
                captured["cookie"] = request.get_header("Cookie")
                return Response()

        with patch("app.services.legado_js.build_opener",
                   lambda *_handlers: Opener()):
            result = json.loads(runtime._request_json(
                'https://example.com/,{"__rawJsoupRequest":true}',
                cookie_enabled=False))
        self.assertEqual(result["body"], "ok")
        self.assertIsNone(captured["cookie"])

    def test_legacy_cookie_state_is_normalized_to_cookie_store_domain(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "rule_engine_state.json"
            state.write_text(json.dumps({"cookies": [{
                "name": "sid", "value": "old", "domain": "www.example.com",
                "domainSpecified": False, "domainInitialDot": False,
                "path": "/private", "pathSpecified": True, "secure": True,
                "expires": None, "rest": {}}]}), encoding="utf-8")
            runtime = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(runtime.export_cookie_header(
                "http://api.example.com/other"), "sid=old")

    def test_public_suffix_database_matches_okhttp_cookie_domains(self):
        self.assertEqual(effective_tld_plus_one("www.example.com"), "example.com")
        self.assertEqual(effective_tld_plus_one("www.biquge.com.cn"),
                         "biquge.com.cn")
        self.assertEqual(effective_tld_plus_one("a.blogspot.com"),
                         "a.blogspot.com")
        self.assertEqual(effective_tld_plus_one("x.city.kawasaki.jp"),
                         "city.kawasaki.jp")
        self.assertEqual(effective_tld_plus_one("a.b.ck"), "a.b.ck")
        self.assertEqual(effective_tld_plus_one("x.www.ck"), "www.ck")
        self.assertEqual(effective_tld_plus_one("127.0.0.1"), "127.0.0.1")

    def test_cookie_store_shares_effective_domain_and_discards_path_secure(self):
        runtime = LegadoJsRuntime()
        runtime.set_cookie("https://www.example.com/private",
                           "sid=value; Path=/private; Secure")
        self.assertEqual(runtime.export_cookie_header(
            "http://api.example.com/other"), "sid=value")

    def test_cookie_map_and_memory_cache_aliases(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        value = runtime.evaluate(
            '@js:cache.putMemory("id",{value:3});'
            '[cache.getFromMemory("id").value,cookie.mapToCookie({a:1,b:"x"})]',
            JsContext(source=source),
        )
        self.assertEqual(value, [3, "a=1; b=x"])

    def test_font_replacement_requires_query_ttf_objects_like_historical_api(self):
        runtime = LegadoJsRuntime()
        runtime.font_store["wrong"] = ({ord("x"): ("shape-a",)}, {})
        runtime.font_store["right"] = ({}, {("shape-a",): ord("中")})
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.replaceFont("x!","wrong","right")', JsContext(source=source))
        self.assertEqual(value, "x!")
        self.assertEqual(runtime._replace_font("x!", "wrong", "right"), "中!")

    def test_packages_java_importer_crypto_and_collections(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var imports=new JavaImporter();
            imports.importPackage(Packages.java.lang,Packages.javax.crypto.spec,
              Packages.javax.crypto,Packages.java.util);
            with(imports){
              var key=SecretKeySpec(String("1234567890abcdef").getBytes(),"AES");
              var iv=IvParameterSpec(String("abcdef1234567890").getBytes());
              var enc=Cipher.getInstance("AES/CBC/PKCS5Padding");
              enc.init(Cipher.ENCRYPT_MODE,key,iv);
              var encrypted=enc.doFinal(String("正文").getBytes());
              var dec=Cipher.getInstance("AES/CBC/PKCS5Padding");
              dec.init(Cipher.DECRYPT_MODE,key,iv);
              var plain=String(dec.doFinal(encrypted));
              var list=new ArrayList(); list.add("a"); list.add("b");
              [plain,list.size(),list.get(1),String(UUID.randomUUID()).length];
            }''',
            JsContext(source=source),
        )
        self.assertEqual(value[:3], ["正文", 2, "b"])
        self.assertEqual(value[3], 36)

    def test_java_importer_constructor_and_official_hmac_support_classes(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var api=new JavaImporter(Packages.javax.crypto.Mac,
              Packages.javax.crypto.spec.SecretKeySpec,
              Packages.java.net.URLEncoder,Packages.java.lang.String,
              Packages.android.util.Base64,
              Packages.javax.xml.bind.DatatypeConverter);
            with(api){
              var mac=Mac.getInstance('HmacSHA1');
              mac.init(new SecretKeySpec(String('key').getBytes('UTF-8'),'HmacSHA1'));
              var signature=mac.doFinal(String('data').getBytes('UTF-8'));
              [Base64.encodeToString(signature,Base64.NO_WRAP),
               URLEncoder.encode('a b','UTF-8'),
               DatatypeConverter.printHexBinary(signature)];
            }''', JsContext(source=source))
        self.assertEqual(value, ["EEFSxb/coHvGM+69RhmfAlXJ9J0=", "a+b",
                                 "104152C5BFDCA07BC633EEBD46199F0255C9F49D"])

    def test_java_collection_iterator_stream_and_mutation_semantics(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var list=new Packages.java.util.ArrayList();
            list.add('a');list.add('c');list.add(1,'b');
            var iterator=list.iterator(),visited=[];
            while(iterator.hasNext()){
              var item=iterator.next();visited.push(item);if(item==='b')iterator.remove();
            }
            var mapped=list.stream().map(function(x){return x+x})
              .collect(Packages.java.util.stream.Collectors.toList());
            var set=new Packages.java.util.HashSet(['x','x']);
            [visited,list.toArray(),mapped,set.add('x'),set.add('y'),set.toArray(),
             Object.keys(list)];''', JsContext(source=source))
        self.assertEqual(value, [["a", "b", "c"], ["a", "c"], ["aa", "cc"],
                                 False, True, ["x", "y"], ["0", "1"]])

    def test_java_exception_hierarchy_and_boxed_parse_helpers(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var caught;
            try { throw new Packages.java.lang.IllegalArgumentException('bad'); }
            catch(e) { caught=[e instanceof Packages.java.lang.RuntimeException,
              e instanceof Packages.java.lang.Exception,
              e instanceof Packages.java.lang.Throwable,e.getMessage(),String(e)]; }
            [caught,Packages.java.lang.Integer.parseInt('ff',16),
             Packages.java.lang.Long.parseLong('9223372036854775807').toString(),
             Packages.java.lang.Boolean.parseBoolean('TRUE')];''',
            JsContext(source=source))
        self.assertEqual(value, [[True, True, True, "bad",
                                 "java.lang.IllegalArgumentException: bad"],
                                 255, "9223372036854776000", True])

    def test_rhino_boxed_primitives_long_precision_and_hidden_class(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var L=Packages.java.lang.Long,I=Packages.java.lang.Integer,
                B=Packages.java.lang.Boolean,Byte=Packages.java.lang.Byte;
            var boxed=L.valueOf('9223372036854775807'),numberError=false;
            try{Byte.valueOf('128')}catch(e){numberError=[
              e instanceof Packages.java.lang.NumberFormatException,
              e.javaException instanceof Packages.java.lang.NumberFormatException]}
            [typeof L.parseLong('7'),boxed.toString(),boxed+'',boxed.longValue().toString(),
             L.MAX_VALUE.toString(),I.valueOf(7) instanceof I,I.parseInt('7') instanceof I,
             I.valueOf(7)==7,I.valueOf(7)===7,B.valueOf('false').booleanValue(),
             B.valueOf(true) instanceof B,Byte.valueOf('127').byteValue(),numberError,
             I.valueOf(7).getClass(),I.TYPE];''',
            JsContext(source=source))
        self.assertEqual(value, [
            "number", "9223372036854775807", "9223372036854776000",
            "9223372036854776000", "9223372036854776000", True, False,
            True, False, False, True, 127, [False, True], None, None,
        ])

    def test_rhino_error_stack_and_null_undefined_boundaries(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source",
            "jsLib": "const sharedError=new Error('shared');"})
        expression = (
            "@js:[typeof new Error('x').stack,"
            "(function(){try{throw new Error('boom')}catch(e){return typeof e.stack}})(),"
            "(function(){try{null.x}catch(e){return e.name+'|'+typeof e.stack}})(),"
            "String(undefined),String(null),undefined==null,undefined===null,"
            "sharedError instanceof Error]"
        )
        expected = ["undefined", "undefined", "TypeError|string",
                    "undefined", "null", True, False, True]
        self.assertEqual(runtime.evaluate(expression, JsContext(source=source)), expected)
        self.assertEqual(runtime.evaluate(expression, JsContext(source=source)), expected)

    def test_jsoup_traversal_range_form_data_parser_and_quirks(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var doc=org.jsoup.Jsoup.parse("<form action='/go'><input name='a' value='1'>"
              +"<input name='c' checked></form><p>x<b>y</b></p>","https://host/base");
            var p=doc.selectFirst('p'),order=[];
            p.traverse(function(node,depth){order.push(node.nodeName()+'@'+depth)});
            var data=doc.forms().get(0).formData().map(function(item){return item.toString()});
            var range=p.sourceRange();
            p.filter({head:function(node){return node.nodeName()==='b'?
              org.jsoup.select.NodeFilter.FilterResult.REMOVE:
              org.jsoup.select.NodeFilter.FilterResult.CONTINUE}});
            [range.toString(),range.isTracked(),order.join(','),data.join(','),p.text(),
             doc.quirksMode(),String(doc.parser()).startsWith('org.jsoup.parser.Parser@')];''',
            JsContext(source=source))
        self.assertEqual(value, [
            "-1,-1:-1--1,-1:-1", False, "p@0,#text@1,b@1,#text@2",
            "a=1,c=", "x", "noQuirks", True])

    def test_jsoup_node_filter_and_form_element_follow_1162_long_tail(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var doc=org.jsoup.Jsoup.parse("<form id=f><input name=r type=reset value=R>"
              +"<input name=file type=file value=F><button name=b value=B></button>"
              +"<button name=x type=button value=X></button><input name=c type=checkbox checked>"
              +"<select name=s multiple><option value=one>1</option><option value=two>2</option></select>"
              +"</form><input form=f name=outside value=O>"
              +"<p><span><em>x</em></span><b>y</b><i>z</i></p>","https://host/");
            var log=[],p=doc.selectFirst('p');
            p.filter({head:function(n,d){log.push('h:'+n.nodeName()+d);
              if(n.nodeName()==='span')return org.jsoup.select.NodeFilter.FilterResult.SKIP_CHILDREN;
              if(n.nodeName()==='b')return org.jsoup.select.NodeFilter.FilterResult.SKIP_ENTIRELY;
              if(n.nodeName()==='i')return org.jsoup.select.NodeFilter.FilterResult.REMOVE;
              return org.jsoup.select.NodeFilter.FilterResult.CONTINUE},
              tail:function(n,d){log.push('t:'+n.nodeName()+d);return 'CONTINUE'}});
            var form=doc.forms().first();
            [form.elements().size(),form.formData().map(x=>x.toString()).join(','),
             log.join(','),p.text(),p.select('i').size()];''', JsContext(source=source))
        self.assertEqual(value, [
            6, "r=R,file=F,c=on,s=one",
            "h:p0,h:span1,t:span1,h:b1,h:i1,t:p0", "xy", 0])

    def test_symmetric_crypto_supports_android_jca_legacy_algorithms(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            function roundTrip(transformation,key,iv){
              var crypto=java.createSymmetricCrypto(transformation,key,iv);
              return crypto.decryptStr(crypto.encryptBase64('legacy'));
            }
            [roundTrip('Blowfish/CBC/PKCS5Padding','12345678','abcdefgh'),
             roundTrip('RC2/ECB/PKCS5Padding','12345678',null),
             roundTrip('RC4','12345678',null)];''', JsContext(source=source))
        self.assertEqual(value, ["legacy", "legacy", "legacy"])

    def test_symmetric_crypto_rejects_invalid_aes_key_instead_of_padding_it(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        with self.assertRaises(Exception):
            runtime.evaluate(
                "@js:java.createSymmetricCrypto('AES/ECB/PKCS5Padding','short').encryptBase64('x')",
                JsContext(source=source))

    def test_hutool_gzip_and_android_helpers(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        compressed = list(gzip.compress("章节".encode()))
        value = runtime.evaluate(
            '@js:[Packages.cn.hutool.core.util.ZipUtil.unGzip(result,"UTF-8"),'
            'Packages.android.text.TextUtils.isEmpty(""),'
            'Packages.android.util.Base64.encodeToString([1,2,3],2)]',
            JsContext(source=source, result=compressed),
        )
        self.assertEqual(value, ["章节", True, "AQID"])

    def test_android_bitmap_decode_cache_crop_and_encode(self):
        image = Image.new("RGB", (4, 10), (20, 40, 60))
        stream = BytesIO()
        image.save(stream, format="PNG")
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var bmp=Packages.android.graphics.BitmapFactory.decodeByteArray(result,0,result.length);
            cache.putMemory("image",bmp);
            bmp=cache.getFromMemory("image");
            var cropped=Packages.android.graphics.Bitmap.createBitmap(bmp,0,2,4,5,null,true);
            var output=new Packages.java.io.ByteArrayOutputStream();
            cropped.compress(Packages.android.graphics.Bitmap.CompressFormat.PNG,100,output);
            output.toByteArray();''',
            JsContext(source=source, result=stream.getvalue()),
        )
        decoded = Image.open(BytesIO(bytes(item & 255 for item in value)))
        self.assertEqual(decoded.size, (4, 5))

    def test_android_canvas_can_reassemble_bitmap_slices(self):
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        stream = BytesIO()
        image.save(stream, format="PNG")
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            importPackage(Packages.java.io,Packages.android.graphics);
            var piece=BitmapFactory.decodeByteArray(result,0,result.length);
            var target=Bitmap.createBitmap(4,4,Bitmap.Config.ARGB_8888);
            var canvas=new Canvas(target); canvas.drawBitmap(piece,1,1,null);
            var output=new ByteArrayOutputStream();
            target.compress(Bitmap.CompressFormat.PNG,100,output);
            output.toByteArray();''',
            JsContext(source=source, result=stream.getvalue()))
        decoded = Image.open(BytesIO(bytes(item & 255 for item in value))).convert("RGBA")
        self.assertEqual(decoded.size, (4, 4))
        self.assertEqual(decoded.getpixel((1, 1))[:3], (255, 0, 0))

    def test_rhino_import_class_exposes_jsoup_short_name(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:importClass(org.jsoup.Jsoup);Jsoup.parse("<b>A</b>").select("b").text()',
            JsContext(source=source))
        self.assertEqual(value, "A")

    def test_okhttp_packages_bridge_returns_response_bytes(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda value, *_: json.dumps({
            "body": "ABC", "bodyBytes": [65, 66, 67], "headers": {},
            "status": 200, "url": "https://source/data",
        })
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var imports=new JavaImporter();imports.importPackage(Packages.okhttp3);
            with(imports){
              var request=new Request.Builder().url("https://source/data")
                .addHeader("X-Test","1").get().build();
              var response=new OkHttpClient().newCall(request).execute();
              response.body().bytes();
            }''', JsContext(source=source))
        self.assertEqual(value, [65, 66, 67])

    def test_analyze_url_byte_array_and_input_stream_match_legado(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda value, *_: json.dumps({
            "body": "ABC", "bodyBytes": [65, 66, 67], "headers": {},
            "status": 200, "url": str(value),
        })
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var bytes=java.getByteArray();
            var stream=java.getInputStream();
            var first=stream.read(),remaining=stream.readAllBytes(),after=stream.available();
            stream.reset();var buffer=[0,0,0,0],count=stream.read(buffer,1,2);
            stream.mark(10);var marked=stream.read();stream.reset();var replay=stream.read();
            var eof=false;try{stream.skipNBytes(2)}catch(e){eof=e instanceof Packages.java.io.EOFException}
            [bytes,3,first,remaining,after,count,buffer,marked,replay,eof];
            ''', JsContext(source=source, host_kind="url",
                           base_url="https://source/data"))
        self.assertEqual(value, [
            [65, 66, 67], 3, 65, [66, 67], 0, 2, [0, 65, 66, 0],
            67, 67, True])

    def test_browser_source_state_and_collection_compatibility(self):
        runtime = LegadoJsRuntime()
        runtime.browser_fetcher = lambda url, title: (
            f"<p>{title}</p>", "text/html", url + "?done=1")
        source = SimpleNamespace(
            bookSourceUrl="https://source", header='{"X-Test":"1"}',
            to_dict=lambda: {"bookSourceUrl": "https://source",
                             "header": '{"X-Test":"1"}'},
        )
        value = runtime.evaluate(
            '''@js:
            source.setVariable("saved");
            source.putLoginInfo('{"user":"u"}');
            var response=java.startBrowserAwait("https://verify","验证");
            var values=["a","b"];
            [response.body(),response.url(),source.getVariable(),
             source.getLoginInfoMap().user,source.getHeaderMap()["X-Test"],
             values.size(),values.get(1),values.toArray().join("")];''',
            JsContext(source=source, variables={}),
        )
        self.assertEqual(value, ["<p>验证</p>", "https://verify?done=1", "saved",
                                 "u", "1", 2, "b", "ab"])

    def test_base_source_string_parameters_and_gson_string_map_values(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '{"number":7,"flag":true,"nested":{"x":1}}',
        })
        value = runtime.evaluate('''@js:
            source.putLoginInfo({user:'u'});
            var invalid=source.getLoginInfoMap();
            var objectText=""+source.getLoginInfo();
            source.putLoginInfo('{"number":7,"flag":true,"nested":{"x":1}}');
            var info=source.getLoginInfoMap(),header=source.getHeaderMap();
            [objectText,invalid,info.number,info.flag,info.nested,
             header.number,header.flag,header.nested];
        ''', JsContext(source=source))
        self.assertEqual(value, ["[object Object]", None,
                                 "7", "true", '{"x":1}',
                                 "7", "true", '{"x":1}'])

    def test_login_info_is_aes_encrypted_at_rest_and_restored(self):
        with TemporaryDirectory() as directory:
            source = SimpleNamespace(to_dict=lambda: {
                "bookSourceUrl": "https://source.example"})
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime.evaluate(
                '@js:source.putLoginInfo("{\\"account\\":\\"alice\\"}")',
                JsContext(source=source))
            raw_state = (Path(directory) / "rule_engine_state.json").read_text(
                encoding="utf-8")
            self.assertNotIn("alice", raw_state)
            restored = LegadoJsRuntime(state_dir=directory).evaluate(
                "@js:source.getLoginInfo()", JsContext(source=source))
        self.assertEqual(restored, '{"account":"alice"}')

    def test_login_info_invalid_ciphertext_returns_null_like_fixed_source(self):
        with TemporaryDirectory() as directory:
            source = SimpleNamespace(to_dict=lambda: {
                "bookSourceUrl": "https://source.example"})
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime.evaluate('@js:source.putLoginInfo("secret")',
                             JsContext(source=source))
            state_path = Path(directory) / "rule_engine_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["sources"]["https://source.example"]["source:loginInfo"] = "plaintext"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            value = LegadoJsRuntime(state_dir=directory).evaluate(
                "@js:source.getLoginInfo()", JsContext(source=source))
        self.assertIsNone(value)

    def test_start_browser_is_non_blocking_for_media_content_rules(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime.browser_starter = lambda url, title: calls.append((url, title))
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        value = runtime.evaluate(
            '''@js:
            var response=java.startBrowser("https://media/video.m3u8","播放");
            [response===undefined,"直链:\\nhttps://media/video.m3u8"];''',
            JsContext(source=source),
        )
        self.assertTrue(value[0])
        self.assertIn("https://media/video.m3u8", value[1])
        self.assertEqual(calls, [("https://media/video.m3u8", "播放")])

        rendered = runtime.evaluate(
            "@js:java.startBrowser(result,title);"
            "'【刷新正文】播放\\n✅直链:\\n'+result",
            JsContext(source=source, result="https://media/video.m3u8",
                      title="视频"),
        )
        document = parse_content_blocks(rendered, "https://source/chapter")
        self.assertEqual(calls[-1], ("https://media/video.m3u8", "视频"))
        self.assertEqual(document.blocks[-1].kind, "video")
        self.assertEqual(document.blocks[-1].value,
                         "https://media/video.m3u8")

    def test_webview_bridge_preserves_html_js_and_capture_options(self):
        runtime = LegadoJsRuntime()
        calls = []

        def fetch(url, title, options):
            calls.append((url, title, options))
            if options.get("sourceRegex"):
                return {"body": "https://cdn/a.jpg", "source": "https://cdn/a.jpg",
                        "url": url}
            if options.get("overrideUrlRegex"):
                return {"body": "https://host/final/42",
                        "overrideUrl": "https://host/final/42", "url": url}
            return {"body": "rendered", "url": url,
                    **({"cookie": "web=stored"} if len(calls) == 1 else {})}

        runtime.browser_fetcher = fetch
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.example",
            "header": '{"X-Source":"yes"}'})
        value = runtime.evaluate(
            '''@js:source.putLoginHeader('{"X-Login":"token"}');[
              java.webView('<p>seed</p>','https://host/base','document.body.innerText'),
              java.webViewGetSource(null,'https://host/page',null,'src="([^"]+)"'),
              java.webViewGetOverrideUrl('', 'https://host/start', '', 'https://host/final/[0-9]+')
            ]''', JsContext(source=source))
        self.assertEqual(value, ["rendered", "https://cdn/a.jpg", "https://host/final/42"])
        self.assertEqual(calls[0][2]["html"], "<p>seed</p>")
        self.assertEqual(calls[0][2]["javaScript"], "document.body.innerText")
        self.assertEqual(calls[1][2]["sourceRegex"], 'src="([^"]+)"')
        self.assertEqual(calls[2][2]["overrideUrlRegex"], "https://host/final/[0-9]+")
        self.assertEqual(calls[0][2]["cookieTag"], "https://source.example")
        self.assertEqual(calls[0][2]["headerMap"]["X-Source"], "yes")
        self.assertEqual(calls[0][2]["headerMap"]["X-Login"], "token")
        self.assertIn("User-Agent", calls[0][2]["headerMap"])
        self.assertEqual(runtime.export_cookie_header("https://source.example/"),
                         "web=stored")

    def test_ajax_and_connect_return_error_stack_like_js_extensions(self):
        runtime = LegadoJsRuntime()

        def fail(*_args):
            raise OSError("network-down")

        runtime._request_json = fail
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:var a=java.ajax('https://host/fail');"
            "var c=java.connect('https://host/fail');"
            "[a.indexOf('network-down')>=0,c.body().indexOf('network-down')>=0,c.url()]",
            JsContext(source=source))
        self.assertEqual(value, [True, True, "https://host/fail"])

    def test_chapter_is_vip_method_uses_python_chapter_state(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        chapter = ChapterItem("locked", "https://source/1", is_vip=True)
        self.assertTrue(runtime.evaluate(
            "@js:chapter.isVip()", JsContext(source=source, chapter=chapter)))
        paid = ChapterItem("paid", "https://source/2", is_pay=True)
        self.assertFalse(runtime.evaluate(
            "@js:chapter.isVip()", JsContext(source=source, chapter=paid)))

    def test_source_empty_dataclass_fields_are_visible_to_javascript(self):
        runtime = LegadoJsRuntime()
        source = BookSource(bookSourceUrl="https://source")
        self.assertEqual(
            runtime.evaluate(
                "@js:[source.bookSourceComment,source.bookSourceGroup]",
                JsContext(source=source)),
            [None, None],
        )

    def test_source_state_device_id_and_file_cache_persist(self):
        with TemporaryDirectory() as directory:
            source = SimpleNamespace(
                bookSourceUrl="https://source",
                to_dict=lambda: {"bookSourceUrl": "https://source"},
            )
            first = LegadoJsRuntime(state_dir=directory)
            first_id = first.device_id
            first.evaluate(
                '@js:source.setVariable("state");source.putLoginHeader("{\\"A\\":\\"B\\"}");'
                'cache.putFile("sort","9");cache.put("persist","value",60);'
                'cache.putMemory("memory",{value:7});'
                'infoMap.choice="selected";infoMap.save()',
                JsContext(source=source, variables={}),
            )
            second = LegadoJsRuntime(state_dir=directory)
            value = second.evaluate(
                '@js:[source.getVariable(),source.getLoginHeaderMap().A,'
                'cache.getFile("sort"),cache.get("persist"),cache.getFromMemory("memory"),'
                'java.deviceID,infoMap.choice,'
                'java.androidId(),java.deviceID(),String(java.androidId)]',
                JsContext(source=source, variables={}),
            )
            self.assertEqual(value, ["state", "B", "9", "value", None,
                                     first_id, "selected",
                                     first_id, first_id, first_id])

    def test_expired_cache_can_be_cleaned_during_fresh_runtime_startup(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "rule_engine_state.json"
            state_path.write_text(json.dumps({
                "cache": {
                    "expired": {"value": "old", "expires": 100.0},
                },
            }), encoding="utf-8")

            with patch("app.services.legado_js.time.time", return_value=101.0):
                runtime = LegadoJsRuntime(state_dir=directory)

            try:
                stored = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertTrue(runtime.device_id)
                self.assertEqual(stored["deviceId"], runtime.device_id)
                self.assertNotIn("expired", stored.get("cache", {}))
            finally:
                runtime.close()

    def test_cache_expiry_removes_database_and_file_entries(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            with patch("app.services.legado_js.time.time", return_value=100.0):
                runtime._cache_put("db", json.dumps("value"), 1)
                runtime._file_cache_put("file", json.dumps("value"), 1)
            with patch("app.services.legado_js.time.time", return_value=102.0):
                self.assertEqual(runtime._cache_get("db"), "")
                self.assertEqual(runtime._file_cache_get("file"), "")
            runtime._MEMORY_CACHE_LIMIT = 5
            runtime._memory_cache_put("old", "123")
            runtime._memory_cache_put("new", "456")
            self.assertEqual(runtime._memory_cache_get("old"), "")
            self.assertEqual(runtime._memory_cache_get("new"), "456")

    def test_symmetric_crypto_supports_string_and_byte_operations(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        script = '''@js:
        var aes=java.createSymmetricCrypto("AES/CBC/PKCS7Padding",
          "1234567890abcdef", "abcdef1234567890");
        var encrypted=aes.encryptBase64("正文");
        var plain=aes.decryptStr(encrypted);
        var des=java.createSymmetricCrypto("DES/ECB/PKCS5Padding", "12345678", "");
        [plain, des.decryptStr(des.encryptBase64("ok")), aes.decrypt(aes.encrypt([1,2,3]))];
        '''
        value = runtime.evaluate(script, JsContext(source=source))
        self.assertEqual(value[:2], ["正文", "ok"])
        self.assertEqual(value[2], [1, 2, 3])

    def test_packages_is_visible_through_rhino_global_this(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:function lib(){const {Packages}=this;return '
            'Packages.cn.hutool.core.codec.Base64.DEFAULT;} lib()',
            JsContext(source=source))
        self.assertEqual(value, 0)

    def test_rhino_global_this_completion_and_date_timezone_surface(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            implicitRhinoValue=7;
            [this.Packages===Packages,typeof globalThis,implicitRhinoValue,
             new Date(0).getHours(),new Date(0).getTimezoneOffset(),
             new Date(0).toString()];''', JsContext(source=source))
        offset = runtime._timezone_offset_minutes(0)
        self.assertEqual(value[:3], [True, "undefined", 7])
        self.assertEqual(value[3], (offset // 60) % 24)
        self.assertEqual(value[4], -offset)
        self.assertEqual(value[5], runtime._date_to_string(0))
        self.assertEqual(runtime.evaluate(
            "@js:1;2;", JsContext(source=source)), 2)
        self.assertIsNone(runtime.evaluate(
            "@js:var declaredOnly=1;", JsContext(source=source)))

    def test_time_chapter_number_and_list_bridges(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[java.timeFormatUTC(0,"yyyy-MM-dd"),'
            'java.toNumChapter("第一百零二章"),java.getStringList("items")]',
            JsContext(source=source, get_string_list=lambda _rule: ["a", "b"]),
        )
        self.assertEqual(value, ["1970-01-01", "第102章", ["a", "b"]])

    def test_log_returns_input_for_chained_source_scripts(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertTrue(runtime.evaluate(
            '''@js:JSON.parse(java.log('{"ok":true}')).ok''', JsContext(source)))

    def test_time_format_utc_accepts_date_and_millisecond_offset(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.timeFormatUTC(new Date(0),"yyyy-MM-dd HH",28800000)',
            JsContext(source=source))
        self.assertEqual(value, "1970-01-01 08")

    def test_js_extensions_base64_android_flags_and_time_signature(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:[java.base64Encode('a',0),java.base64Encode('a',1|2|8),"
            "java.base64Decode('YQ',8),java.base64DecodeToByteArray('',0),"
            "java.timeFormat(0,'ss'),java.utf8ToGbk('汉')]",
            JsContext(source=source),
        )
        self.assertEqual(value[:4], ["YQ==\n", "YQ", "a", None])
        self.assertEqual(value[4], datetime.fromtimestamp(0).strftime("%Y/%m/%d %H:%M"))
        self.assertEqual(value[5], "汉".encode("gbk").decode("utf-8", errors="replace"))
        self.assertEqual(runtime.evaluate(
            "@js:java.utf8ToGbk('𠀀')", JsContext(source=source)), "?")

    def test_js_extensions_ajax_list_concurrency_and_connect_header_overload(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        calls = []
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def request(value, _headers=None, _base=None, *_context):
            nonlocal active, maximum_active
            calls.append(value)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            url = str(value).split(",", 1)[0]
            return json.dumps({"body": url, "headers": {}, "status": 200, "url": url})

        runtime._request_json = request
        value = runtime.evaluate(
            "@js:var all=java.ajaxAll(['u1','u2','u3']);"
            "var one=java.ajax(['first','ignored']);"
            "var empty=java.ajax([]);"
            "var response=java.connect('target','{\\\"X-Test\\\":\\\"yes\\\"}');"
            "var objectHeader=java.connect('object',{X:'ignored'});"
            "function npe(call){try{call();return false}catch(e){return "
            "e.javaException instanceof Packages.java.lang.NullPointerException}}"
            "[all.map(x=>x.body()),one,empty,response.body(),objectHeader.body(),"
            "npe(()=>java.ajax(null)),npe(()=>java.ajaxAll(null)),"
            "npe(()=>java.connect(null)),"
            "(()=>{try{java.connect();return false}catch(e){return e instanceof TypeError}})()]",
            JsContext(source=source),
        )
        self.assertEqual(value, [["u1", "u2", "u3"], "first", "null", "target",
                                 "object", True, True, True, True])
        self.assertEqual(maximum_active, 3)
        self.assertTrue(any('"X-Test":"yes"' in call for call in calls))
        self.assertFalse(any('"X":"ignored"' in call for call in calls))

    def test_java_get_overload_uses_arity_without_absolute_url_heuristic(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        calls = []

        def request(value, *_args):
            calls.append(value)
            return json.dumps({"body": "network", "headers": {}, "status": 200,
                               "url": str(value).split(",", 1)[0]})

        runtime._request_json = request
        value = runtime.evaluate('''@js:
            java.put('https://host/path','stored');
            var local=java.get('https://host/path');
            var remote=java.get('https://host/path',{}).body();
            var objectText=java.put('object',{x:1});
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            [local,remote,objectText,java.get('object'),
             npe(()=>java.get('x',null)),npe(()=>java.post('x','b',null)),
             npe(()=>java.head('x',null)),
             (()=>{try{java.post('x','b');return false}catch(e){return e instanceof TypeError}})()];
        ''', JsContext(source=source))
        self.assertEqual(value, ["stored", "network", "[object Object]",
                                 "[object Object]", True, True, True, True])
        self.assertEqual(len(calls), 1)

    def test_inherited_js_encode_utils_digest_and_symmetric_helpers(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            """@js:
            var des=java.desEncodeToBase64String('content','12345678',
              'DES/CBC/PKCS5Padding','87654321');
            var triple=java.tripleDESEncodeBase64Str('content','123456789012345678901234',
              'CBC','PKCS5Padding','12345678');
            var crypto=java.createSymmetricCrypto('AES/CBC/PKCS5Padding',
              '1234567890abcdef','abcdef1234567890');
            [java.digestBase64Str('abc','SHA-256'),
             java.desBase64DecodeToString(des,'12345678','DES/CBC/PKCS5Padding','87654321'),
             java.tripleDESDecodeStr(triple,'123456789012345678901234','CBC','PKCS5Padding','12345678'),
             crypto.encryptHex('a')];""",
            JsContext(source=source),
        )
        self.assertEqual(value[:3], [
            base64.b64encode(hashlib.sha256(b"abc").digest()).decode(),
            "content", "content",
        ])
        self.assertRegex(value[3], r"^[0-9a-f]{32}$")

    def test_inherited_js_encode_utils_rsa_and_signature_objects(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            """@js:
            var rsa=java.createAsymmetricCrypto('RSA/ECB/PKCS1Padding');
            var publicCipher=rsa.encryptBase64('public-data');
            var privateCipher=rsa.encryptHex('private-data',false);
            var signer=java.createSign('SHA256withRSA');
            var signature=signer.sign('message');
            var signatureHex=signer.signHex('message');
            var oaep=java.createAsymmetricCrypto('RSA/ECB/OAEPWithSHA-256AndMGF1Padding');
            var oaepCipher=oaep.encryptBase64('oaep-sha256');
            var raw=java.createAsymmetricCrypto('RSA/ECB/NoPadding');
            var rawCipher=raw.encrypt([1,2,3]);
            var rawPlain=raw.decrypt(rawCipher,false);
            [rsa.decryptStr(publicCipher,false),rsa.decryptStr(privateCipher,true),
             signer.verify('message',signature),signer.verify('other',signature),
             signer.verify('message',signatureHex),oaep.decryptStr(oaepCipher,false),
             rawPlain.slice(-3)];""",
            JsContext(source=source),
        )
        self.assertEqual(value, ["public-data", "private-data", True, False, True,
                                 "oaep-sha256", [1, 2, 3]])

    def test_symmetric_crypto_null_key_is_random_and_object_local(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            """@js:
            var first=java.createSymmetricCrypto('AES/CBC/PKCS5Padding',null,null);
            var second=java.createSymmetricCrypto('AES/CBC/PKCS5Padding',null,null);
            var a=first.encryptBase64('content'),b=second.encryptBase64('content');
            [a!==b,first.decryptStr(a),second.decryptStr(b)];""",
            JsContext(source=source),
        )
        self.assertEqual(value, [True, "content", "content"])

    def test_rhino_sleep_and_java_thread_sleep_are_blocking(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        started = time.perf_counter()
        value = runtime.evaluate(
            "@js:sleep(20);Packages.java.lang.Thread.sleep(20);'done'",
            JsContext(source=source),
        )
        self.assertEqual(value, "done")
        self.assertGreaterEqual(time.perf_counter() - started, 0.035)

    def test_js_extensions_html_format_ports_format_keep_img(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:java.htmlFormat(' <div>A&nbsp;&nbsp;<span>B</span><br> ' +"
            "'C&thinsp;<img class=\\\"x\\\" data-src=\\\"/lazy\\\" src=\\\"/old\\\"></div> ')",
            JsContext(source=source),
        )
        self.assertEqual(value, "　　A B\n　　C<img src=\"/lazy\">")

    def test_query_ttf_returns_historical_query_object_methods(self):
        from fontTools.fontBuilder import FontBuilder
        from fontTools.pens.ttGlyphPen import TTGlyphPen

        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder([".notdef", "A"])
        builder.setupCharacterMap({65: "A"})
        empty_pen = TTGlyphPen(None)
        glyph_pen = TTGlyphPen(None)
        glyph_pen.moveTo((100, 0))
        glyph_pen.lineTo((500, 800))
        glyph_pen.lineTo((900, 0))
        glyph_pen.closePath()
        builder.setupGlyf({".notdef": empty_pen.glyph(), "A": glyph_pen.glyph()})
        builder.setupHorizontalMetrics({".notdef": (1000, 0), "A": (1000, 0)})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "RuleFont", "styleName": "Regular"})
        builder.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                         usWinAscent=800, usWinDescent=200)
        builder.setupPost()
        builder.setupMaxp()
        output = BytesIO()
        builder.save(output)
        encoded = base64.b64encode(output.getvalue()).decode()

        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:var font=java.queryBase64TTF('" + encoded + "');"
            "var glyph=font.getGlyfByCode(65);"
            "[font.getNameById(1),font.inLimit(65),font.inLimit(66),glyph.length>0,"
            "font.getCodeByGlyf(glyph),font.codeToGlyph.get('65'),"
            "java.queryBase64TTF('')]",
            JsContext(source=source),
        )
        self.assertEqual(value[:5], ["RuleFont", True, False, True, 65])
        self.assertEqual(value[5], runtime._font_operation(
            next(iter(runtime.font_store)), "glyph", "65"))
        self.assertIsNone(value[6])

    def test_js_url_matches_historical_fields_including_origin_quirk(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:var a=java.toURL('/p?a=hello+world','https://host:8080/base');"
            "var b=java.toURL('https://host/plain');"
            "[a.host,a.pathname,a.searchParams.get('a'),a.origin,"
            "Object.prototype.hasOwnProperty.call(a,'href'),b.searchParams]",
            JsContext(source=source),
        )
        self.assertEqual(value, [
            "host", "/p", "hello world",
            "https://host:https://host:8080/p?a=hello+world:8080", False, None,
        ])

    def test_to_num_chapter_only_rewrites_the_historical_title_pattern(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:[java.toNumChapter('前言十二'),java.toNumChapter('卷一 第１２ 章 后'),"
            "java.toNumChapter('第未知章'),java.toNumChapter(null)]",
            JsContext(source=source),
        )
        self.assertEqual(value, ["前言十二", "卷一 第12章 后", "第-1章", None])

    def test_jsoup_bridge_supports_select_text_and_attribute(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var d=org.jsoup.Jsoup.parse(result);'
            '[d.select("li").text(),d.select("li a").attr("href"),d.select("li").size()]',
            JsContext(source=source, result='<ul><li>A <a href="/x">X</a></li><li>B</li></ul>'),
        )
        self.assertEqual(value, ["A X B", "/x", 2])

    def test_jsoup_connect_builder_uses_shared_http_bridge(self):
        runtime = LegadoJsRuntime()
        requests = []

        def request(value, *_):
            requests.append(value)
            return json.dumps({"body": "<title>Page</title><p>Body</p>",
                               "headers": {}, "status": 200,
                               "url": "https://final.example/"})

        runtime._request_json = request
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var d=org.jsoup.Jsoup.connect("https://source/page")'
            '.userAgent("agent").header("X-Test","1").get();'
            '[d.title(),d.select("p").text(),d.location()]',
            JsContext(source=source),
        )
        self.assertEqual(value, ["Page", "Body", "https://final.example/"])
        self.assertIn('"X-Test":"1"', requests[0])

    def test_jsoup_elements_preserve_nodes_and_are_iterable(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var items=org.jsoup.Jsoup.parse(result).select("li");'
            '[items.length,items.first().attr("data-id"),items.get(1).ownText(),'
            'Array.from(items).map(x=>x.select("a").attr("href"))]',
            JsContext(source=source, result=(
                '<ul><li data-id="1">A<a href="/a">X</a></li>'
                '<li data-id="2">B<a href="/b">Y</a></li></ul>')),
        )
        self.assertEqual(value, [2, "1", "B", ["/a", "/b"]])

    def test_jsoup_mutable_remove_parents_and_collection_remove(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<main><div id="p"><span>A</span></div><i>B</i></main>');
            var parent=d.select('#p').first(),child=d.select('span').first();
            var contains=child.parents().contains(parent);
            d.select('i').remove();
            var list=d.select('div,span');list.remove(1);
            [contains,d.text(),list.size(),String(d).includes('<i>')];''',
            JsContext(source=source))
        self.assertEqual(value, [True, "A", 1, False])

    def test_jsoup_document_element_and_elements_long_tail_methods(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<main><p id="a" class="x">A</p><p>B</p><input></main>');
            var first=d.getElementById('a'), second=first.nextElementSibling();
            var before=[first.id(),first.className(),first.childrenSize(),
              first.elementSiblingIndex(),second.previousElementSibling().id(),
              first.siblingElements().size(),d.getElementsByTag('p').size(),
              d.getElementsByClass('x').size(),d.getElementsByAttribute('id').size()];
            first.attr('data-v','1').addClass('y').removeClass('x').toggleClass('z');
            second.text('C').tagName('section');
            d.select('input').val('V');
            d.select('section').html('<b>D</b>');
            [before,first.attr('data-v'),first.className(),second.tagName(),
             second.child(0).tagName(),d.select('input').val(),d.text()];''',
            JsContext(source=source))
        self.assertEqual(value, [
            ["a", "x", 0, 0, "a", 2, 2, 1, 1],
            "1", "y z", "section", "b", "V", "A D",
        ])

    def test_jsoup_object_views_filter_and_wrap_mutations(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<main><div id="a" class="x y" data-k="v"> A <span>B</span><!--c--></div><script>raw</script><p>C</p></main>');
            var e=d.select('#a').first(),attrs=e.attributes(),dataset=e.dataset();
            var before=[attrs.get('id'),attrs.hasKeyIgnoreCase('ID'),dataset.get('k'),
              e.classNames().contains('y'),e.classNames().size(),e.textNodes().size(),
              e.textNodes().get(0).getWholeText(),d.select('script').dataNodes().get(0).getWholeData(),
              e.comments().get(0).getData(),e.is('div#a'),e.closest('main').tagName(),
              e.selectFirst('span').text(),e.select('div#a').size(),
              d.select('div,p').not('.x').text(),e.getElementsByTag('div').size(),
              e.getElementsByClass('X').size(),e.getElementsByAttribute('DATA-K').size(),
              e.getElementsByAttributeValue('data-k','V').size(),
              e.getElementsContainingText('B').size(),
              e.getElementsMatchingOwnText('^A$').size()];
            attrs.put('title','T');dataset.put('k','w');
            e.prependText('P').appendText('Q');e.appendElement('i').text('I');
            e.wrap('<section class="w"><article></article></section>');
            var wrapped=d.select('section.w > article > div#a').size();
            d.select('article').unwrap();
            [before,e.attr('title'),e.attr('data-k'),wrapped,
             d.select('section.w > div#a').size(),d.select('div,p').is('p')];''',
            JsContext(source=source))
        self.assertEqual(value, [
            ["a", True, "v", True, 2, 1, " A ", "raw", "c",
             True, "main", "B", 1, "C", 1, 1, 1, 1, 2, 1],
            "T", "w", 1, 1, True,
        ])

    def test_jsoup_html_mutation_uses_element_fragment_context(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<table><tbody></tbody></table><select></select>');
            d.select('tbody').html('<tr><td>A</td></tr>');
            d.select('tr').append('<td>B</td>');
            d.select('select').append('<option value="1">One</option>');
            [d.select('tbody > tr').size(),d.select('tr > td').eachText(),
             d.select('option').val(),d.select('table').text()];''',
            JsContext(source=source))
        self.assertEqual(value, [1, ["A", "B"], "1", "A B"])

    def test_jsoup_css_xpath_tag_and_url_long_tail(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<main><section><p class="x">A</p><p class="x">B</p></section><a href="../c">C</a></main>',
              'https://example.test/a/b');
            var second=d.select('p').get(1),found=d.selectXpath('//section/p[2]').first();
            [second.cssSelector(),found===second,second.tag().getName(),
             second.tag().normalName(),second.tag().isBlock(),second.baseUri(),
             d.select('a').first().absUrl('href'),d.expectFirst('main').tagName(),
             d.tagName(),d.nodeName()];''',
            JsContext(source=source))
        self.assertEqual(value, [
            "html > body > main > section > p.x:nth-child(2)", True,
            "p", "p", True, "https://example.test/a/b",
            "https://example.test/c", "main", "#root", "#document",
        ])

    def test_jsoup_document_output_settings_are_observable(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<div title="<">&nbsp;<p>A</p><br></div>');
            var out=d.outputSettings();out.prettyPrint(false);
            var compact=d.select('div').outerHtml();
            out.prettyPrint(true).indentAmount(2);
            var indented=d.select('div').outerHtml().includes('\\n  <p>');
            out.prettyPrint(false).syntax(org.jsoup.nodes.Document.OutputSettings.Syntax.xml)
              .escapeMode(org.jsoup.nodes.Entities.EscapeMode.xhtml);
            [compact,indented,d.select('div').outerHtml(),out.syntax(),out.escapeMode()];''',
            JsContext(source=source))
        self.assertEqual(value, [
            '<div title="<">&nbsp;<p>A</p><br></div>', True,
            '<div title="&lt;">&#xa0;<p>A</p><br /></div>', "xml", "xhtml",
        ])

    def test_jsoup_node_insertions_move_shared_identity(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var d=org.jsoup.Jsoup.parse('<main><div id="a"><i>A</i></div><section id="b"><b>B</b></section></main>');
            var a=d.getElementById('a'),b=d.getElementById('b'),i=a.child(0),bold=b.child(0);
            b.prependChild(i);bold.appendTo(a);a.insertChildren(0,b.children());
            [i.parent().id(),bold.parent().id(),a.children().eachText(),b.childrenSize(),
             d.select('i').first()===i,d.select('i').first().__handle===i.__handle];''',
            JsContext(source=source))
        self.assertEqual(value, ["a", "a", ["A", "B"], 0, True, True])

    def test_jsoup_matches_and_matches_own_are_regex_filters(self):
        markup = ('<div><a href="/1"><span>下一页</span></a>'
                  '<a href="/2">下一 页</a><a href="/3">正文</a></div>')
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        runtime_value = LegadoJsRuntime().evaluate(
            '''var d=org.jsoup.Jsoup.parse(result);
               [d.select("a:matches(下一页)").attr("href"),
                d.select("a:matchesOwn(下\\\\一?\\\\s*页)").attr("href")];''',
            JsContext(source=source, result=markup))
        self.assertEqual(runtime_value, ["/1", "/2"])
        self.assertEqual(extract_value(
            BeautifulSoup(markup, "html.parser"),
            r"a:matches(下一页)@href", "html"), "/1")
        soup = BeautifulSoup("<ul><li>A</li><li>B <b>x</b></li><li>C</li></ul>",
                             "html.parser")
        self.assertEqual(extract_value(soup, "li:eq(1)@text", "html"), "B x")
        self.assertEqual(extract_value(soup, "li:lt(2)@text", "html"), "A\nB x")
        self.assertEqual(extract_value(soup, "li:containsOwn(B)@text", "html"), "B x")
        author = BeautifulSoup('<li><span class="author">作者：张三</span></li>',
                               "html.parser")
        self.assertEqual(extract_value(
            author, "li span.author@text:regex('作者：(.*?)')", "html"), "张三")
        children = BeautifulSoup(
            '<div class="box"><p><a>A</a></p><section><a>B</a></section></div>',
            "html.parser")
        self.assertEqual(extract_value(
            children, "class.box@children.1@a@text", "html"), "B")
        text_nodes = BeautifulSoup('<p>正文<span>广告</span>结尾</p>', "html.parser")
        self.assertEqual(extract_value(text_nodes, "p@textNodes", "html"),
                         "正文\n结尾")

    def test_concurrent_jsoup_evaluations_keep_dom_handles_isolated(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        def parse(value):
            return runtime.evaluate(
                "var d=org.jsoup.Jsoup.parse(result);d.select('p').text()",
                JsContext(source=source, result=f"<p>{value}</p>"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(parse, [f"chapter-{i}" for i in range(40)]))
        self.assertEqual(values, [f"chapter-{i}" for i in range(40)])

    def test_concurrent_evaluations_use_one_native_thread(self):
        class RecordingRuntime(LegadoJsRuntime):
            def _evaluate_locked(self, script, context):
                return threading.get_ident()

        runtime = RecordingRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            thread_ids = list(pool.map(
                lambda _: runtime.evaluate("1", JsContext(source=source)),
                range(80),
            ))
        self.assertEqual(len(set(thread_ids)), 1)

    def test_jsoup_elements_has_class_checks_all_selected_nodes(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var node=org.jsoup.Jsoup.parse(result);'
            '[node.select("img").hasClass("locked"),'
            'node.select("img").hasClass("missing")]',
            JsContext(source=source,
                      result='<div><img><img class="locked other"></div>'))
        self.assertEqual(value, [True, False])

    def test_jsoup_elements_eq_returns_empty_elements_not_null(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var node=org.jsoup.Jsoup.parse(result);'
            '[node.select(".missing").eq(0).html(),'
            'node.select("p").eq(0).text(),'
            'node.select("p").eq(0).size()]',
            JsContext(source=source, result='<div><p>Text</p></div>'))
        self.assertEqual(value, ["", "Text", 1])

    def test_rhino_array_destructuring_callback_is_normalized(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[["a",1],["b",2]].map([name,id]=>name+id)',
            JsContext(source=source),
        )
        self.assertEqual(value, ["a1", "b2"])

    def test_rhino_function_parameter_may_be_redeclared(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:function encrypt(text,key,iv){let key="K";let iv="I";'
            'return text+key+iv;} encrypt("T","x","y")',
            JsContext(source=source))
        self.assertEqual(value, "TKI")

    def test_rule_var_can_reuse_js_lib_lexical_name(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source", "jsLib": "let shared;"})
        value = runtime.evaluate(
            '@js:var shared="content";shared', JsContext(source=source))
        self.assertEqual(value, "content")

    def test_rule_lexical_can_reuse_injected_context_name(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:let page=java.get("stored")||page;page+1',
            JsContext(source=source, host_kind="url", page=4, variables={}))
        self.assertEqual(value, 5)

    def test_shadowed_source_global_is_not_written_back_as_model_state(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        value = runtime.evaluate(
            '@js:var source="temporary-url";"ok"', JsContext(source=source))
        self.assertEqual(value, "ok")
        self.assertEqual(source.bookSourceUrl, "https://source")

    def test_hmac_algorithm_prefix_is_case_insensitive(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        values = runtime.evaluate(
            '@js:[java.HMacHex("data","HmacMD5","key"),'
            'java.HMacHex("data","HmacSHA256","key")]',
            JsContext(source=source))
        self.assertEqual(values, [
            hmac.new(b"key", b"data", "md5").hexdigest(),
            hmac.new(b"key", b"data", "sha256").hexdigest(),
        ])

    def test_crypto_bridge_preserves_original_failure(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        with self.assertRaisesRegex(JsBridgeError, "crypto-test-failure"):
            with patch.object(runtime, "_crypto",
                              side_effect=ValueError("crypto-test-failure")):
                runtime.evaluate(
                    '@js:java.aesBase64DecodeToString("x","key",'
                    '"AES/CBC/PKCS5Padding","iv")',
                    JsContext(source=source))

    def test_java_get_string_defaults_to_current_result_payload(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[java.getString("$.data.token"),'
            'java.getStringList("$.data.items[*]")]',
            JsContext(source=source,
                      result='{"data":{"token":"abc","items":[1,2]}}'))
        self.assertEqual(value, ["abc", ["1", "2"]])

    def test_java_set_content_switches_default_rule_payload(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.setContent("<ul><li>A</li><li>B</li></ul>");'
            '[java.getString("li@text"),java.getElements("li").size()]',
            JsContext(source=source, result="<p>Old</p>",
                      get_string=lambda _rule: "old-callback"))
        self.assertEqual(value, ["A\nB", 2])

    def test_binary_result_and_buffer_concat_for_image_decode(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:Buffer.concat([result.slice(1), [9]])',
            JsContext(source=source, result=b"\x01\x02\x03"),
        )
        self.assertEqual(value, [2, 3, 9])

    def test_image_decode_receives_url_as_src_and_bytes_as_result(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[src,result[0],baseUrl]',
            JsContext(source=source, result=b"\x07\x08", src="https://img/1.jpg",
                      base_url="https://img/1.jpg"),
        )
        self.assertEqual(value, ["https://img/1.jpg", 7, "https://img/1.jpg"])

    def test_convenience_crypto_and_traditional_chinese_bridges(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var aes=java.createSymmetricCrypto("AES/CBC/PKCS7Padding",
              "1234567890abcdef","abcdef1234567890");
            var encrypted=aes.encryptBase64("漫畫");
            [java.aesBase64DecodeToString(encrypted,"1234567890abcdef",
              "AES/CBC/PKCS7Padding","abcdef1234567890"),java.t2s("漫畫")];''',
            JsContext(source=source),
        )
        self.assertEqual(value, ["漫畫", "漫画"])

    def test_legacy_aes_helper_family_matches_symmetric_crypto(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var transformation="AES/CBC/PKCS7Padding",key="1234567890abcdef",
                iv="abcdef1234567890";
            var encoded=java.aesEncodeToBase64String("content",key,transformation,iv);
            var encodedBytes=java.aesEncodeToBase64ByteArray("content",key,transformation,iv);
            [java.aesBase64DecodeToString(encoded,key,transformation,iv),
             java.aesEncodeToString(encoded,key,transformation,iv),
             java.bytesToStr(encodedBytes),
             java.aesBase64DecodeToByteArray(encoded,key,transformation,iv)];''',
            JsContext(source=source))
        self.assertEqual(value[:2], ["content", "content"])
        self.assertTrue(value[2])
        self.assertEqual(bytes(item & 255 for item in value[3]), b"content")

    def test_java_byte_arrays_use_signed_values_and_round_trip_as_unsigned_bytes(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:[java.hexDecodeToByteArray('80ff7f'),"
            "java.strToBytes('ÿ','ISO-8859-1'),"
            "java.bytesToStr([-1],'ISO-8859-1')]",
            JsContext(source=source),
        )
        self.assertEqual(value, [[-128, -1, 127], [-1], "ÿ"])

    def test_iso_8859_1_binary_string_preserves_nul_and_all_byte_values(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var bytes=[]; for(var i=0;i<256;i++) bytes.push(i);
            var text=java.bytesToStr(bytes,"ISO-8859-1");
            var roundTrip=java.strToBytes(text,"ISO-8859-1");
            [text.length,text.charCodeAt(0),text.charCodeAt(255),
             roundTrip.length,roundTrip[0],roundTrip[128],roundTrip[255],
             text.getBytes("latin-1").length];''',
            JsContext(source=source),
        )
        self.assertEqual(value, [256, 0, 255, 256, 0, -128, -1, 256])

    def test_latin1_binary_split_keeps_cbc_ciphertext_block_aligned(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var key=java.strToBytes("1234567890abcdef","UTF-8");
            var iv=[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15];
            var cipher=java.createSymmetricCrypto("AES/CBC/PKCS7Padding",key,iv);
            var encrypted=cipher.encrypt(java.strToBytes("binary bridge","UTF-8"));
            var raw=iv.concat(encrypted);
            var binary=java.bytesToStr(raw,"ISO-8859-1");
            var restoredIv=java.strToBytes(binary.substring(0,16),"ISO-8859-1");
            var restoredCipher=java.strToBytes(binary.substring(16),"ISO-8859-1");
            var decryptor=java.createSymmetricCrypto(
              "AES/CBC/PKCS7Padding",key,restoredIv);
            [binary.length,restoredCipher.length,
             java.bytesToStr(decryptor.decrypt(restoredCipher),"UTF-8")];''',
            JsContext(source=source),
        )
        self.assertEqual(value, [32, 16, "binary bridge"])

    def test_js_extensions_s2t_and_zip_content_overloads(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("folder/test.txt", "\u9605\u8bfb".encode("gb18030"))
            archive.writestr("folder/skipped.txt", b"skipped")
        archive_hex = output.getvalue().hex()
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            "@js:[java.s2t('\\u6c49\\u9f99'),"
            f"java.getZipStringContent('{archive_hex}','folder/test.txt','gb18030'),"
            f"java.getZipByteArrayContent('{archive_hex}','folder/test.txt'),"
            f"java.getZipByteArrayContent('{archive_hex}','folder/skipped.txt')]",
            JsContext(source=source),
        )
        self.assertEqual(value[:2], ["\u6f22\u9f8d", "\u9605\u8bfb"])
        self.assertEqual(bytes(item & 255 for item in value[2]), "\u9605\u8bfb".encode("gb18030"))
        self.assertIsNone(value[3])

    def test_network_archive_content_inherits_analyze_url_source_context(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("book.txt", "network")
        runtime = LegadoJsRuntime()
        captured = []

        def request(value, headers=None, base_url=None, cookie_enabled=True,
                    cookie_tag=None, allow_webview=True):
            captured.append((value, headers, base_url, cookie_enabled, cookie_tag,
                             allow_webview))
            return json.dumps({
                "body": "", "bodyBytes": list(output.getvalue()),
                "headers": {}, "status": 200, "url": value,
            })

        runtime._request_json = request
        source = SimpleNamespace(
            bookSourceUrl="https://source.test/root/", enabledCookieJar=False,
            header='@js:JSON.stringify({"X-Dynamic":source.get("token"),'
                   '"proxy":"http://proxy.test:8080"})',
        )
        request_url = ('https://files.test/book.zip,'
                       '{"headers":{"X-Option":"yes"},"retry":"2",'
                       '"webView":true}')
        value = runtime.evaluate(
            '@js:source.put("token","yes");'
            'source.putLoginHeader(\'{"X-Login":"ok"}\');'
            f'java.getZipStringContent({json.dumps(request_url)},"book.txt","utf-8")',
            JsContext(source=source))
        self.assertEqual(value, "network")
        self.assertEqual(captured, [(
            request_url,
            {"X-Dynamic": "yes", "proxy": "http://proxy.test:8080",
             "User-Agent": runtime.default_headers["User-Agent"], "X-Login": "ok"},
            "https://source.test/root/", False, "https://source.test/root/", False,
        )])

    def test_network_archive_non_2xx_keeps_body_and_cookie_jar_semantics(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("book.txt", "forbidden-body")

        class Response:
            status = 403
            reason = "Forbidden"
            version = 11

            def __init__(self):
                self.headers = Message()
                self.headers["Content-Type"] = "application/zip"
                self.headers["Set-Cookie"] = "session_key=memory; Path=/private"
                self.headers["Set-Cookie"] = (
                    "persistent_key=disk; Max-Age=3600; Path=/private; Secure")

            def read(self):
                return output.getvalue()

            def geturl(self):
                return "https://files.example.com/private/book.zip"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime._open_with_retry = lambda *_args: Response()
            enabled = SimpleNamespace(
                bookSourceUrl="https://source.example.com/",
                enabledCookieJar=True, header="")
            value = runtime.evaluate(
                '@js:java.getZipStringContent('
                '"https://files.example.com/private/book.zip","book.txt","utf-8")',
                JsContext(source=enabled))
            self.assertEqual(value, "forbidden-body")
            self.assertEqual(runtime.export_cookie_header(
                "http://api.example.com/other"),
                "session_key=memory; persistent_key=disk")
            restored = LegadoJsRuntime(state_dir=directory)
            self.assertEqual(restored.export_cookie_header(
                "http://api.example.com/other"), "persistent_key=disk")

        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            runtime._open_with_retry = lambda *_args: Response()
            disabled = SimpleNamespace(
                bookSourceUrl="https://source.example.com/",
                enabledCookieJar=False, header="")
            value = runtime.evaluate(
                '@js:java.getZipStringContent('
                '"https://files.example.com/private/book.zip","book.txt","utf-8")',
                JsContext(source=disabled))
            self.assertEqual(value, "forbidden-body")
            self.assertEqual(runtime.export_cookie_header(
                "https://api.example.com/other"), "")

    def test_network_archive_transport_exception_is_not_converted_to_empty_content(self):
        runtime = LegadoJsRuntime()
        runtime._open_with_retry = lambda *_args: (_ for _ in ()).throw(
            URLError("archive transport failed"))
        source = SimpleNamespace(
            bookSourceUrl="https://source.example.com/",
            enabledCookieJar=True, header="")
        with self.assertRaisesRegex(Exception, "archive transport failed"):
            runtime.evaluate(
                '@js:java.getZipByteArrayContent('
                '"https://files.example.com/book.zip","book.txt")',
                JsContext(source=source))

    def test_network_archive_http_error_branch_saves_response_cookie(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("book.txt", "error-body")
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)

            def fail(*_args):
                headers = Message()
                headers["Content-Type"] = "application/zip"
                headers["Set-Cookie"] = "challenge=accepted; Max-Age=3600"
                raise HTTPError(
                    "https://files.example.com/book.zip", 429,
                    "Too Many Requests", headers, BytesIO(output.getvalue()))

            runtime._open_with_retry = fail
            source = SimpleNamespace(
                bookSourceUrl="https://source.example.com/",
                enabledCookieJar=True, header="")
            value = runtime.evaluate(
                '@js:java.getZipStringContent('
                '"https://files.example.com/book.zip","book.txt","utf-8")',
                JsContext(source=source))
            self.assertEqual(value, "error-body")
            self.assertEqual(runtime.export_cookie_header(
                "https://api.example.com/"), "challenge=accepted")

    def test_js_extensions_webview_nullable_arguments_and_public_overloads(self):
        runtime = LegadoJsRuntime()
        calls = []

        def browser(url, title, options):
            calls.append((url, title, options))
            return {"body": "html-result", "source": "source-result",
                    "overrideUrl": "https://override.test/"}

        runtime.browser_fetcher = browser
        source = SimpleNamespace(
            bookSourceUrl="https://source.test/base", header="")
        value = runtime.evaluate('''@js:
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            [java.webView("<p>x</p>",null,null),
             java.webViewGetSource(null,"https://page.test",null,".*\\.js"),
             java.webViewGetOverrideUrl(null,"https://page.test",null,"https://.*"),
             npe(()=>java.webViewGetSource(null,null,null,null)),
             npe(()=>java.webViewGetOverrideUrl(null,null,null,null)),
             type(()=>java.webView(null,null)),
             type(()=>java.webViewGetSource(null,null,null)),
             type(()=>java.webViewGetOverrideUrl(null,null,null,null,"extra"))];''',
            JsContext(source=source, base_url="https://source.test/base"))
        self.assertEqual(value, [
            "html-result", "source-result", "https://override.test/",
            True, True, True, True, True,
        ])
        first = calls[0][2]
        self.assertEqual(first["html"], "<p>x</p>")
        self.assertIsNone(first["url"])
        self.assertIsNone(first["javaScript"])

    def test_fixed_js_extensions_public_methods_are_all_registered(self):
        source = Path(
            "base_history/legado-3.23.110211/app/src/main/java/"
            "io/legado/app/help/JsExtensions.kt").read_text(encoding="utf-8")
        fixed = set(re.findall(
            r"(?m)^    (?:(?:override|suspend)\s+)*fun\s+([A-Za-z0-9_]+)\s*\(",
            source))
        self.assertEqual(fixed - SUPPORTED_JAVA_APIS, set())

    def test_fixed_analyze_hosts_public_sync_methods_are_all_registered(self):
        root = Path(
            "base_history/legado-3.23.110211/app/src/main/java/"
            "io/legado/app/model/analyzeRule")
        fixed = set()
        for filename in ("AnalyzeRule.kt", "AnalyzeUrl.kt"):
            source = (root / filename).read_text(encoding="utf-8")
            fixed.update(re.findall(
                r"(?m)^    (?:override\s+)?fun\s+([A-Za-z0-9_]+)\s*\(",
                source))
        self.assertEqual(fixed - SUPPORTED_JAVA_APIS, set())

    def test_fixed_source_entity_public_methods_are_all_registered(self):
        root = Path(
            "base_history/legado-3.23.110211/app/src/main/java/"
            "io/legado/app/data/entities")
        fixed = set()
        for filename in ("BaseSource.kt", "BookSource.kt"):
            source = (root / filename).read_text(encoding="utf-8")
            fixed.update(re.findall(
                r"(?m)^    (?:override\s+)?fun\s+([A-Za-z0-9_]+)\s*\(",
                source))
        self.assertEqual(fixed - SUPPORTED_OBJECT_APIS["source"], set())

    def test_analyze_rule_set_redirect_url_state_and_host_isolation(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        value = runtime.evaluate('''@js:
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            var first=java.setRedirectUrl("https://target.test/a?q=1");
            var invalid=java.setRedirectUrl("not a url");
            [String(first),first.getHost(),first.getPath(),invalid===first,
             java.redirectUrl===first,npe(()=>java.setRedirectUrl(null)),
             type(()=>java.setRedirectUrl())];''', JsContext(source=source))
        self.assertEqual(value, [
            "https://target.test/a?q=1", "target.test", "/a", True,
            True, True, True,
        ])
        self.assertEqual(runtime.evaluate(
            '@js:[typeof java.setRedirectUrl,java.redirectUrl]',
            JsContext(source=source, host_kind="url",
                      base_url="https://source.test/path")),
            ["undefined", "https://source.test/path"])

    def test_analyze_rule_set_content_and_base_url_return_this(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            function fails(call){try{call();return false}catch(e){return /Content cannot be null/.test(String(e))}}
            var contentReturn=java.setContent("<p>A</p>","https://one.test/");
            var first=[contentReturn===java,java.baseUrl,java.getString("p@text")];
            var nullReturn=java.setBaseUrl(null),afterNull=java.baseUrl;
            var baseReturn=java.setBaseUrl("https://two.test/");
            first.concat([nullReturn===java,afterNull,baseReturn===java,java.baseUrl,
              fails(()=>java.setContent(null)),type(()=>java.setContent()),
              type(()=>java.setContent("x",null,"extra")),type(()=>java.setBaseUrl())]);''',
            JsContext(source=source))
        self.assertEqual(value, [
            True, "https://one.test/", "A", True, "https://one.test/",
            True, "https://two.test/", True, True, True, True,
        ])

    def test_analyze_rule_split_source_rule_cache_and_eval_js(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            function npe(call){try{call();return false}catch(e){return e.javaException
              instanceof Packages.java.lang.NullPointerException}}
            var first=java.splitSourceRuleCacheString("@Json:$.a<js>result</js>");
            var cached=java.splitSourceRuleCacheString("@Json:$.a<js>result</js>");
            var regex=java.splitSourceRule(":a+",true);
            var inherited=java.splitSourceRule("b+");
            [first.length,first[0].mode,first[0].rule,first[1].mode,
             cached===first,regex[0].mode,inherited[0].mode,
             java.evalJS("result+1",4),java.evalJS("baseUrl"),
             java.splitSourceRuleCacheString(null).length,
             type(()=>java.splitSourceRule()),type(()=>java.splitSourceRule("x",false,1)),
             npe(()=>java.evalJS(null)),type(()=>java.evalJS())];''',
            JsContext(source=source, result='{"a":1}',
                      base_url="https://source.test/base"))
        self.assertEqual(value, [
            2, "json", "$.a", "js", True, "regex", "regex",
            5, "https://source.test/base", 0, True, True, True, True,
        ])

    def test_analyze_url_user_agent_is_post_and_eval_js(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/", "header": ""})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            [java.getUserAgent(),java.isPost(),java.evalJS("result.x",{x:7}),
             typeof java.splitSourceRule,type(()=>java.getUserAgent(1)),
             type(()=>java.isPost(1))];''', JsContext(
                 source=source, host_kind="url",
                 base_url='https://api.test/path,{"method":"post"}',
                 request_headers={"User-Agent": "option-agent"}))
        self.assertEqual(value, [
            "option-agent", True, 7, "undefined", True, True,
        ])

    def test_analyze_url_init_url_replays_initial_public_state_and_returns_unit(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            java.ruleUrl="changed";java.url="changed";baseUrl="changed-base";
            [String(java.initUrl()),java.ruleUrl,java.url,type(()=>java.initUrl(1))];''',
            JsContext(source=source, host_kind="url",
                      base_url="https://source.test/original"))
        self.assertEqual(value, [
            "undefined", "https://source.test/original",
            "https://source.test/original", True,
        ])

    def test_analyze_url_nullable_source_disables_cookie_jar_and_binds_null(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime._request_json = lambda *args: (
            calls.append(args) or json.dumps({
                "body": "ok", "bodyBytes": [], "headers": {},
                "status": 200, "url": str(args[0])}))
        value = runtime.evaluate(
            '@js:[source===null,java.getStrResponse().body()]',
            JsContext(source=None, host_kind="url",
                      base_url="https://source.test/data"))
        self.assertEqual(value, [True, "ok"])
        self.assertIs(calls[0][3], False)
        self.assertEqual(calls[0][4], "")

    def test_analyze_rule_refresh_methods_delegate_and_return_unit(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime.reget_book_handler = lambda source, book: (
            calls.append(("reget", source, book)), setattr(book, "url", "new-url"))
        runtime.refresh_book_handler = lambda source, book: (
            calls.append(("refresh", source, book)), setattr(book, "toc_url", "new-toc"))
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/"})
        book = BookItem(name="Book", url="old-url")
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            [String(java.reGetBook()),book.bookUrl,String(java.refreshBook()),
             book.tocUrl,String(java.refreshTocUrl()),book.tocUrl,
             type(()=>java.reGetBook(1)),type(()=>java.refreshBook(1)),
             type(()=>java.refreshTocUrl(1))];''',
            JsContext(source=source, book=book))
        self.assertEqual(value, [
            "undefined", "new-url", "undefined", "new-toc",
            "undefined", "new-toc", True, True, True,
        ])
        self.assertEqual([item[0] for item in calls], ["reget", "refresh", "refresh"])
        self.assertEqual(runtime.evaluate(
            '@js:[typeof java.reGetBook,typeof java.refreshBook,typeof java.refreshTocUrl]',
            JsContext(source=source, book=book, host_kind="url")),
            ["undefined", "undefined", "undefined"])

    def test_reading_engine_refresh_handlers_follow_precise_search_side_effects(self):
        async def create_engine():
            return LegadoReadingEngine()

        engine = asyncio.run(create_engine())
        source = SimpleNamespace(bookSourceUrl="https://source.test/")
        book = BookItem(name="Book", author="Author", url="old",
                        variables={"kept": "yes"})
        detail_calls = []

        async def search(_source, _name, _page):
            return [
                BookItem(name="Book", author="Wrong", url="wrong"),
                BookItem(name="Book", author="Author", url="new",
                         variables={"token": "fresh"}),
            ]

        async def book_info(_source, target):
            detail_calls.append(target.url)
            target.toc_url = target.url + "/toc"
            return target

        engine.search = search
        engine.book_info = book_info
        engine._js_reget_book(source, book)
        self.assertEqual((book.url, book.toc_url, book.variables), (
            "new", "new/toc", {"kept": "yes", "token": "fresh"}))
        engine._js_refresh_book(source, book)
        self.assertEqual(detail_calls, ["new", "new"])

        book.author = "Missing"
        with self.assertRaisesRegex(RuntimeError, "未搜索到"):
            engine._js_reget_book(source, book)

    def test_analyze_url_get_glide_url_merges_cookie_and_marker(self):
        runtime = LegadoJsRuntime()
        runtime.store_set_cookie("https://img.example.com/", "saved=1; same=old")
        source = SimpleNamespace(to_dict=lambda: {
            "bookSourceUrl": "https://source.test/", "header": "",
            "enabledCookieJar": True})
        value = runtime.evaluate('''@js:
            function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
            var glide=java.getGlideUrl(),headers=glide.getHeaders();
            [String(glide),glide.getCacheKey(),headers.Cookie,headers.CookieJar,
             type(()=>java.getGlideUrl(1))];''', JsContext(
                 source=source, host_kind="url",
                 base_url="https://img.example.com/cover.jpg",
                 request_headers={"Cookie": "same=new; temporary=2"}))
        self.assertEqual(value, [
            "https://img.example.com/cover.jpg",
            "https://img.example.com/cover.jpg",
            "saved=1; same=new; temporary=2", "1", True,
        ])

    def test_js_extensions_download_and_external_cache_file_operations(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
            value = runtime.evaluate(
                """@js:
                var missing=java.downloadFile('e99885e8afbb','https://host/book.txt');
                var path=java.downloadFile('e99885e8afbb','https://host/book.txt,{"type":"txt"}');
                var file=java.getFile(path);
                var before=[path,file.exists(),java.readTxtFile(path),java.readFile(path)];
                var removed=java.deleteFile(path);
                [missing,before,removed,file.exists()];""",
                JsContext(source=source),
            )
            self.assertEqual(value[0], "")
            self.assertRegex(value[1][0], r"^/[0-9a-f]{16}\.txt$")
            self.assertEqual(value[1][1:3], [True, "\u9605\u8bfb"])
            self.assertEqual(bytes(item & 255 for item in value[1][3]), "\u9605\u8bfb".encode())
            self.assertEqual(value[2:], [True, False])

    def test_js_extensions_file_and_archive_signatures_match_kotlin(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            source = SimpleNamespace(to_dict=lambda: {
                "bookSourceUrl": "https://source"})
            value = runtime.evaluate('''@js:
                function npe(call){try{call();return false}catch(e){return e.javaException
                  instanceof Packages.java.lang.NullPointerException}}
                function type(call){try{call();return false}catch(e){return e instanceof TypeError}}
                [java.readFile("missing"),java.readTxtFile("missing"),
                 java.deleteFile("missing"),java.unArchiveFile(""),
                 npe(()=>java.getFile(null)),npe(()=>java.readFile(null)),
                 npe(()=>java.readTxtFile("x",null)),npe(()=>java.deleteFile(null)),
                 npe(()=>java.unzipFile(null)),npe(()=>java.getTxtInFolder(null)),
                 npe(()=>java.getZipByteArrayContent(null,"x")),
                 npe(()=>java.getZipStringContent("00","x",null)),
                 type(()=>java.getFile()),type(()=>java.readTxtFile()),
                 type(()=>java.readTxtFile("x","utf-8","extra")),
                 type(()=>java.unArchiveFile()),
                 type(()=>java.getZipByteArrayContent("00")),
                 type(()=>java.getZipStringContent("00"))];''',
                JsContext(source=source))
        self.assertEqual(value, [
            None, "", False, "", True, True, True, True, True, True,
            True, True, True, True, True, True, True, True,
        ])

    def test_download_file_single_url_uses_source_analyze_url_headers_and_cookie_flag(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            captured = []

            def request(value, headers=None, base_url=None, cookie_enabled=True):
                captured.append((value, headers, base_url, cookie_enabled))
                return json.dumps({"body": "file", "bodyBytes": [102, 105, 108, 101],
                                   "headers": {}, "status": 200, "url": value})

            runtime._request_json = request
            source = SimpleNamespace(
                bookSourceUrl="https://source", enabledCookieJar=False,
                header='{"X-Source":"yes"}',
            )
            path = runtime.evaluate(
                "@js:java.downloadFile('https://host/file.txt')",
                JsContext(source=source))
            self.assertEqual(runtime.evaluate(
                f"@js:java.readTxtFile({json.dumps(path)})",
                JsContext(source=source)), "file")
        self.assertEqual(captured[0][0], "https://host/file.txt")
        self.assertEqual(captured[0][1]["X-Source"], "yes")
        self.assertEqual(captured[0][2], "https://source")
        self.assertFalse(captured[0][3])
        self.assertTrue(LegadoJsRuntime._download_name(
            "https://host/file.txt#fragment").endswith(".ext"))

    def test_cache_file_records_relative_path_in_cache_manager(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            calls = []
            runtime._request_json = lambda value, *_args: (
                calls.append(value) or json.dumps({
                    "body": "cached", "bodyBytes": list(b"cached"),
                    "headers": {}, "status": 200, "url": value}))
            source = SimpleNamespace(bookSourceUrl="https://source", header="",
                                     enabledCookieJar=True)
            value = runtime.evaluate('''@js:
                var url='https://host/library.js';
                var first=java.cacheFile(url,60),second=java.cacheFile(url,60);
                var imported=java.importScript('https://host/other.js');
                [first,second,cache.get(java.md5Encode16(url)),imported];
            ''', JsContext(source=source))
        self.assertEqual(value[:2], ["cached", "cached"])
        self.assertRegex(value[2], r"^/[0-9a-f]{16}\.js$")
        self.assertEqual(value[3], "cached")
        self.assertEqual(calls, ["https://host/library.js", "https://host/other.js"])

    def test_import_script_uri_provider_and_blank_content_failure(self):
        runtime = LegadoJsRuntime()
        runtime.uri_reader = lambda uri: "var imported='" + uri + "';"
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        self.assertEqual(runtime.evaluate(
            "@js:java.importScript('content://scripts/main.js')",
            JsContext(source=source)), "var imported='content://scripts/main.js';")
        runtime.uri_reader = lambda _uri: "  "
        with self.assertRaises(Exception):
            runtime.evaluate("@js:java.importScript('content://scripts/empty.js')",
                             JsContext(source=source))

    def test_js_extensions_unarchive_and_consume_text_folder(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("a.txt", "A")
            archive.writestr("b.txt", "B")
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
            value = runtime.evaluate(
                "@js:var zip=java.downloadFile('" + output.getvalue().hex() +
                "','https://host/files.zip,{\"type\":\"zip\"}');var folder=java.unzipFile(zip);"
                "[folder,java.getTxtInFolder(folder),java.getFile(folder).exists()]",
                JsContext(source=source),
            )
            self.assertRegex(value[0], r"^ArchiveTemp\\[0-9a-f]{16}$")
            # EncodingDetect.getEncode(File) probes an 8,000-byte zero-padded
            # buffer.  For these one-byte files fixed ICU selects UTF-32LE and
            # Java String decoding yields the replacement character.
            self.assertEqual(value[1], "�\n�")
            self.assertFalse(value[2])

    def test_unarchive_initializes_temp_once_and_reuses_same_archive_folder(self):
        first, second = BytesIO(), BytesIO()
        with zipfile.ZipFile(first, "w") as archive:
            archive.writestr("kept.txt", "old")
            archive.writestr("changed.txt", "first")
        with zipfile.ZipFile(second, "w") as archive:
            archive.writestr("changed.txt", "second")
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            stale = runtime._file_path("ArchiveTemp/stale.txt")
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("remove-on-first-use", encoding="utf-8")
            archive_path = runtime._file_path("same.zip")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(first.getvalue())
            folder = runtime._unarchive_file("same.zip")
            self.assertFalse(stale.exists())
            archive_path.write_bytes(second.getvalue())
            self.assertEqual(runtime._unarchive_file("same.zip"), folder)
            target = runtime._file_path(folder)
            self.assertEqual((target / "kept.txt").read_text(), "old")
            self.assertEqual((target / "changed.txt").read_text(), "second")

    def test_get_txt_in_folder_consumes_existing_non_directory(self):
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            target = runtime._file_path("single.txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("content", encoding="utf-8")
            source = SimpleNamespace(to_dict=lambda: {
                "bookSourceUrl": "https://source"})
            value = runtime.evaluate(
                '@js:[java.getTxtInFolder("single.txt"),java.getFile("single.txt").exists()]',
                JsContext(source=source))
            self.assertEqual(value, ["", False])

    def test_js_extensions_7z_content_and_directory_extraction(self):
        import py7zr

        output = BytesIO()
        with py7zr.SevenZipFile(output, "w") as archive:
            archive.writestr("\u4e03\u538b\u7f29".encode(), "book.txt")
        archive_hex = output.getvalue().hex()
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
            value = runtime.evaluate(
                "@js:var data='" + archive_hex + "';"
                "var path=java.downloadFile(data,'https://host/book.7z,{\"type\":\"7z\"}');"
                "var folder=java.un7zFile(path);"
                "[java.get7zStringContent(data,'book.txt','utf-8'),"
                "java.get7zByteArrayContent(data,'book.txt'),"
                "java.readTxtFile(folder+'\\\\book.txt')]",
                JsContext(source=source),
            )
            self.assertEqual(value[0], "\u4e03\u538b\u7f29")
            self.assertEqual(bytes(item & 255 for item in value[1]), "\u4e03\u538b\u7f29".encode())
            self.assertEqual(value[2], "\u4e03\u538b\u7f29")

    def test_js_extensions_rar_content_and_directory_extraction(self):
        fixture = Path("base_history/rarfile/test/files/rar3-subdirs.rar")
        archive_hex = fixture.read_bytes().hex()
        with TemporaryDirectory() as directory:
            runtime = LegadoJsRuntime(state_dir=directory)
            source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
            value = runtime.evaluate(
                "@js:var data='" + archive_hex + "';"
                "var path=java.downloadFile(data,'https://host/book.rar,{\"type\":\"rar\"}');"
                "var folder=java.unrarFile(path);"
                "[java.getRarStringContent(data,'sub/dir1/file1.txt','utf-8'),"
                "java.getRarByteArrayContent(data,'sub/dir2/file2.txt'),"
                "java.readTxtFile(folder+'\\\\sub\\\\dir1\\\\file1.txt')]",
                JsContext(source=source),
            )
            self.assertEqual(value[0], "file1\n")
            self.assertEqual(bytes(item & 255 for item in value[1]), b"file2\n")
            self.assertEqual(value[2], "file1\n")

    def test_request_config_after_page_template(self):
        request = parse_search_url(
            'https://example.com/list/{{page}}.html,'
            '{"headers":{"User-Agent":"custom-agent"}}',
            "https://example.com", "", 2,
        )
        self.assertEqual(request["url"], "https://example.com/list/2.html")
        self.assertEqual(request["headers"], {"User-Agent": "custom-agent"})

    def test_url_option_normalization_matches_getters_and_atomic_gson_failure(self):
        option = normalize_url_option({
            "method": "put", "retry": "2", "serverID": "7",
            "headers": {"Null": None, "Flag": True, "Nested": {"x": 1}},
            "body": {"q": "书"}, "webView": 0,
        })
        self.assertEqual(option["method"], "GET")
        self.assertEqual(option["retry"], 2)
        self.assertEqual(option["serverID"], 7)
        self.assertEqual(option["headers"], {
            "Null": "null", "Flag": "true", "Nested": "{x=1}"})
        self.assertEqual(option["body"], '{"q":"书"}')
        self.assertEqual(normalize_url_option({
            "method": "POST", "retry": "not-an-int", "body": "ignored"}), {})
        self.assertEqual(ReaderEngine._request_retry_count({"retry": -1}), -1)

    def test_empty_type_is_non_null_and_requests_hex_body(self):
        async def run():
            return await ReaderEngine()._request_transport({
                "url": "data:text/plain;base64,QUI=", "method": "GET", "type": ""})
        body, content_type, final_url = asyncio.run(run())
        self.assertEqual((body, content_type, final_url),
                         ("4142", "text/plain", "data:text/plain;base64,QUI="))

    def test_lenient_gson_option_parser_preserves_quoted_apostrophes(self):
        self.assertEqual(parse_request_config(
            '''{method:'post',body:"author=O'Reilly",headers:{X:'yes'},}'''), {
                "method": "post", "body": "author=O'Reilly",
                "headers": {"X": "yes"},
            })

    def test_invalid_url_option_is_ignored_without_discarding_base_url(self):
        request = parse_search_url(
            '/search, {"method":"POST","retry":"bad","body":"q=x"}',
            "https://source.example", "", 1)
        self.assertEqual(request["url"], "https://source.example/search")
        self.assertEqual(request["method"], "GET")
        self.assertIsNone(request["headers"])

    def test_text_selector_and_attribute(self):
        label = "\u67e5\u770b\u66f4\u591a\u7ae0\u8282"
        root = BeautifulSoup(f'<a href="/toc">{label}</a>', "html.parser")
        self.assertEqual(evaluate(root, f"text.{label}@href", "html"), ["/toc"])

    def test_indices_exclusions_and_negative_indices(self):
        root = BeautifulSoup(
            '<div class="x"><i>0</i><i>1</i><i>2</i><i>3</i></div>',
            "html.parser",
        )
        # Bare notation ':' separates standalone indexes (AnalyzeByJSoup
        # findIndexSet), unlike the start:end:step range inside [...].
        self.assertEqual(evaluate(root, "class.x@i.1:3@text", "html"), ["1", "3"])
        self.assertEqual(evaluate(root, "class.x@i!0:2@text", "html"), ["1", "3"])
        self.assertEqual(evaluate(root, "class.x@i[-1:-2]@text", "html"), ["3", "2"])
        self.assertEqual(evaluate(root, "class.x@i[-2:]@text", "html"), ["2", "3"])
        self.assertEqual(evaluate(root, "class.x@i[2:]@text", "html"), ["2", "3"])
        self.assertEqual(evaluate(root, "class.x@i[0,2]@text", "html"), ["0", "2"])
        self.assertEqual(evaluate(root, "class.x@i[0:3:2]@text", "html"), ["0", "2"])
        self.assertEqual(evaluate(root, "class.x@i[3:0:-1]@text", "html"), ["3", "0"])
        self.assertEqual(evaluate(root, "class.x@i[!1,3]@text", "html"), ["0", "2"])
        self.assertEqual(evaluate(root, "class.x@i[-1:0]@text", "html"),
                         ["3", "2", "1", "0"])
        self.assertEqual(evaluate(root, "class.x@i[99]@text", "html"), [])

    def test_html_returns_outer_markup_without_script_or_style(self):
        root = BeautifulSoup(
            '<div id="body"><b>正文</b><script>bad()</script>'
            '<style>.bad{}</style></div>', "html.parser")
        value = evaluate(root, "#body@html", "html")[0]
        self.assertTrue(value.startswith('<div id="body">'))
        self.assertIn("<b>正文</b>", value)
        self.assertNotIn("script", value)
        self.assertNotIn("style", value)
        combined = evaluate(BeautifulSoup(
            "<p>A</p><p>B</p>", "html.parser"), "p@html", "html")
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0], "<p>A</p>\n<p>B</p>")

    def test_legado_truth_values_accept_any_non_false_marker(self):
        for value in ("1", "true", "vip", "locked", "是", "任意标记"):
            self.assertTrue(_legado_truthy(value), value)
        for value in (None, "", "null", "false", "NO", "not", "0"):
            self.assertFalse(_legado_truthy(value), value)

    def test_jsoup_attribute_regex_and_unquoted_complex_values(self):
        root = BeautifulSoup(
            '<meta property="category" content="玄幻">'
            '<meta property="author" content="作者">'
            '<div style="text-indent: 2em;">正文</div>', "html.parser")
        self.assertEqual(
            evaluate(root, '[property~=category|status]@content', "html"), ["玄幻"])
        self.assertEqual(
            evaluate(root, 'div[style=text-indent: 2em;]@text', "html"), ["正文"])
        self.assertEqual(
            evaluate(BeautifulSoup('<a href="/renwu/a">作者</a>', "html.parser"),
                     '[href*=/renwu/]@text', "html"), ["作者"])
        self.assertEqual(evaluate(root, 'div::text', "html"), ["正文"])
        search = BeautifulSoup(
            '<div class="results"><div class="vrResult"><h3><a>正常书籍</a></h3></div>'
            '<div class="vrResult"><h3><a>广告</a></h3></div></div>', "html.parser")
        kept = evaluate(
            search,
            '.results>.vrResult:has(h3 a):not(:has(:matches(^(广告|推广|手游)$)))',
            "html", list_context=True)
        self.assertEqual([item.get_text(strip=True) for item in kept], ["正常书籍"])

    def test_legacy_class_spacing_exclusion_and_base_url_template(self):
        root = BeautifulSoup(
            '<ul class="Gap one"><li>0</li><li>1</li><li>2</li></ul>',
            "html.parser")
        self.assertEqual(evaluate(root, 'class. Gap one@tag.li.!0:1@text', "html"),
                         ["2"])
        self.assertEqual(extract_value(root, '{baseUrl}/catalog', "html",
                                       'https://source/book'),
                         'https://source/book/catalog')
        indexed = BeautifulSoup(
            '<dd class="book_other">a</dd><dd class="book_other"><a>b</a></dd>'
            '<p><a>最新章</a></p>', "html.parser")
        self.assertEqual(evaluate(indexed, 'dd.book_other.1>a@text', "html"), ["b"])
        self.assertEqual(evaluate(indexed, 'p:contains(最新)a@text', "html"),
                         ["最新章"])
        self.assertEqual(evaluate(indexed, 'p[^missing]@text', "html"), [])

    def test_jsonpath_tolerates_legacy_missing_dot_after_array(self):
        self.assertEqual(evaluate({"categoryNames": [{"className": "玄幻"}]},
                                  "$.categoryNames[*]className", "json"), ["玄幻"])

    def test_full_jsonpath(self):
        data = {"items": [{"ok": True, "id": 1}, {"ok": False, "id": 2}]}
        self.assertEqual(evaluate(data, "$.items[?(@.ok == true)].id", "json"), [1])

    def test_jsonpath_values_use_java_json_string_forms(self):
        data = {"enabled": True, "disabled": False, "missing": None,
                "item": {"id": 1, "name": "书"}}
        self.assertEqual(extract_value(data, "$.enabled", "json"), "true")
        self.assertEqual(extract_value(data, "$.disabled", "json"), "false")
        self.assertEqual(extract_value(data, "$.missing", "json"), "")
        self.assertEqual(extract_value(data, "$.item", "json"),
                         "{id=1, name=书}")
        self.assertEqual(extract_value(data, "status={{$.enabled}}", "json"),
                         "status=true")

    def test_json_smart_numeric_types_order_and_container_text(self):
        raw = ('{"b":1,"2":"x","a":3,"1":"y",'
               '"large":2147483648,"huge":9223372036854775807,'
               '"too":9223372036854775808,"decimal":1.25,'
               '"precise":1.234567890123456789,"exp":1e3,'
               '"array":[1,"x",true,null,{"z":2}]}')
        root, kind = LegadoReadingEngine._root(raw, "application/json")
        self.assertEqual(kind, "json")
        self.assertEqual(list(root)[:4], ["b", "2", "a", "1"])
        self.assertIsInstance(root["large"], int)
        self.assertIsInstance(root["too"], int)
        self.assertIsInstance(root["decimal"], float)
        self.assertEqual(root["precise"], Decimal("1.234567890123456789"))
        self.assertEqual(root["exp"], 1000.0)
        self.assertEqual(extract_value(root, "$.array", "json"),
                         "1\nx\ntrue\nnull\n{z=2}")
        self.assertTrue(extract_value(root, "$", "json").startswith(
            "{b=1, 2=x, a=3, 1=y, large=2147483648"))

    def test_balanced_embedded_jsonpath_allows_closing_brace_in_filter(self):
        data = {"items": [{"text": "}"}, {"text": "other"}]}
        self.assertEqual(
            extract_value(data, "value={$.items[?(@.text == '}')].text}", "json"),
            "value=}")

    def test_get_string_unescapes_html4_after_jsonpath_pipeline(self):
        data = {"title": "A &amp; B &#x4E66;", "path": "/a?x=1&amp;y=2"}
        self.assertEqual(extract_value(data, "$.title", "json"), "A & B 书")
        self.assertEqual(extract_value(data, "{$.title}", "json"), "A & B 书")
        self.assertEqual(extract_value(data, "$.path", "json", "https://host/base"),
                         "https://host/a?x=1&y=2")

    def test_jsoup_attribute_results_drop_empty_values_and_duplicates(self):
        root = BeautifulSoup(
            '<a href="/same">A</a><a href="/same">B</a><a href="">C</a>'
            '<a>D</a><a href="/next">E</a>', "html.parser")
        self.assertEqual(evaluate(root, "a@href", "html"), ["/same", "/next"])

    def test_xpath_element_string_is_markup_not_python_object_repr(self):
        root = BeautifulSoup('<main><p class="x">正文<b>粗体</b></p></main>',
                             "html.parser")
        value = extract_value(root, "//p[@class='x']", "html")
        self.assertIn('<p class="x">', value)
        self.assertIn("正文", value)
        self.assertNotIn("Element at 0x", value)
        fragment = parse_markup("<p>fragment</p>")
        self.assertEqual(evaluate(
            fragment, "/html/body/p/text()", "html"), ["fragment"])
        cell = parse_markup("<td>cell</td>")
        self.assertEqual(evaluate(
            cell, "/html/body/text()", "html"), ["cell"])
        self.assertEqual(evaluate(
            cell, "/html/body/table/tr/td/text()", "html"), [])
        row = parse_markup("<tr><td>row</td></tr>")
        self.assertEqual(evaluate(
            row, "/html/body/text()", "html"), ["row"])
        self.assertEqual(evaluate(
            row, "/html/body/table/tr/td/text()", "html"), [])

    def test_xpath_jsoupxpath_node_tests(self):
        root = parse_markup(
            '<div id="x">A<b>B</b>C 12.5 kg</div>')
        self.assertEqual(evaluate(root, "//div/html()", "html"),
                         ["A<b>B</b>C 12.5 kg"])
        self.assertEqual(evaluate(root, "//div/outerHtml()", "html"),
                         ['<div id="x">A<b>B</b>C 12.5 kg</div>'])
        self.assertEqual(evaluate(root, "//div/allText()", "html"),
                         ["ABC 12.5 kg"])
        self.assertEqual(evaluate(root, "//div/num()", "html"), [12.5])

    def test_html5_tree_repair_matches_fixed_jsoup_structure(self):
        table = parse_markup("<table><tr><td>A<td>B</table>")
        self.assertEqual(
            [node.get_text() for node in select_jsoup(table, "table > tbody > tr > td")],
            ["A", "B"])
        blocks = parse_markup("<p>one<div>two</div>three")
        self.assertEqual([node.get_text() for node in select_jsoup(
            blocks, "body > p, body > div")], ["one", "two"])
        items = parse_markup("<ul><li>one<li>two</ul>")
        self.assertEqual([node.get_text() for node in select_jsoup(
            items, "li:nth-child(2)")], ["two"])

    def test_compound_wildcard_namespace_matches_fixed_jsoup(self):
        root = parse_markup(
            "<div><span>A</span><x:span>B</x:span>"
            "<p><x:span>C</x:span></p></div>")
        self.assertEqual([node.get_text() for node in select_jsoup(
            root, "div > *|span")], ["A", "B"])

    def test_jsoup_selector_escape_and_empty_group_quirks(self):
        root = parse_markup(
            '<div id="a:b" class="x+y 123" data-v="A:B C">One</div>'
            '<p data-v="A B">Two</p><span>Three</span>')
        self.assertEqual([node.get_text() for node in select_jsoup(
            root, r"#a\:b")], ["One"])
        self.assertEqual(select_jsoup(root, r"#a\3A b"), [])
        self.assertEqual(select_jsoup(root, r".\31 23"), [])
        self.assertEqual(select_jsoup(root, r"[data-v=A\:B\ C]"), [])
        self.assertEqual([node.get_text() for node in select_jsoup(
            root, "[data-v=A B]")], ["Two"])
        self.assertEqual([node.get_text() for node in select_jsoup(
            root, "div,,span")], ["One", "Three"])

    def test_jsoup_class_and_attribute_selectors_ignore_case(self):
        root = parse_markup('<main><div class="Mixed" data-v="AbCd">A</div></main>')
        for selector in (
                ".mixed", ".MIXED", "[data-v=abcd]", "[data-v^=AB]",
                "[data-v$=CD]", "[data-v*=BC]"):
            with self.subTest(selector=selector):
                self.assertEqual([node.name for node in select_jsoup(root, selector)],
                                 ["div"])

    def test_jsoup_attribute_regex_balancing_and_invalid_eq(self):
        root = parse_markup(
            '<main data-num="x12y" data-v="A]B"><section data-v="AbC">A</section>'
            '<section data-v="a]b">B</section></main>')
        cases = {
            r"[data-num~=\d+]": ["main"],
            r"[data-v~=[A-Z]]": ["main", "section"],
            r"[data-v~=A\]B]": ["main"],
            r"[data-v~=[^\]]+]": ["main", "section", "section"],
            "[^data-]": ["main", "section", "section"],
        }
        for selector, expected in cases.items():
            with self.subTest(selector=selector):
                self.assertEqual([node.name for node in select_jsoup(root, selector)],
                                 expected)
        with self.assertRaises(Exception):
            select_jsoup(root, "section:eq(-1)")
        with self.assertRaises(Exception):
            select_jsoup(root, "[data-v~=[]")

    def test_jsoup_document_root_participates_in_selection(self):
        root = parse_markup('<main data-v="ABC"><p>A</p></main>')
        selected = select_jsoup(root, "[data-v!=ABC]")
        self.assertIs(selected[0], root)
        self.assertEqual([node.name for node in selected[1:]],
                         ["html", "head", "body", "p"])

    def test_jsoup_js_dom_invalid_selector_propagates(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        with self.assertRaises(Exception):
            runtime.evaluate(
                "@js:org.jsoup.Jsoup.parse('<p>A</p>').select('p:eq(-1)')",
                JsContext(source=source))

    def test_xpath_jsoupxpath_extended_operators_and_axes(self):
        root = parse_markup(
            '<ul><li code="alpha-1">A</li><li code="beta-2">B</li>'
            '<li code="alpha-3">C</li></ul>')
        self.assertEqual(evaluate(
            root, "//li[@code^='alpha']/text()", "html"), ["A", "C"])
        self.assertEqual(evaluate(
            root, "//li[@code$='2']/text()", "html"), ["B"])
        self.assertEqual(evaluate(
            root, "//li[@code*='ha-']/text()", "html"), ["A", "C"])
        self.assertEqual(evaluate(
            root, "//li[@code~='^beta.*']/text()", "html"), ["B"])
        self.assertEqual(evaluate(
            root, "//li[@code!~='^beta.*']/text()", "html"), ["A", "C"])
        self.assertEqual(evaluate(
            root, "//li[2]/following-sibling-one::li/text()", "html"), ["C"])
        self.assertEqual(evaluate(
            root, "//li[3]/preceding-sibling-one::li/text()", "html"), ["B"])

    def test_xpath_jsoupxpath_extended_string_functions(self):
        root = parse_markup("<p>a/b/c.txt</p>")
        self.assertEqual(evaluate(
            root, "@XPath:substring-ex(//p/text(),2,-4)", "html"), ["b/c"])
        self.assertEqual(evaluate(
            root, "@XPath:substring-after-last(//p/text(),'/')", "html"), ["c.txt"])
        self.assertEqual(evaluate(
            root, "@XPath:substring-before-last(//p/text(),'/')", "html"), ["a/b"])
        self.assertEqual(evaluate(
            root, "@XPath:substring-before-last(//p/text(),'|')", "html"), [""])
        date_value = evaluate(
            root,
            "@XPath:format-date('2019-01-21 19:05:42','yyyy-MM-dd HH:mm:ss')",
            "html")[0]
        self.assertIsInstance(date_value, JavaDateValue)
        self.assertIn("Jan 21 19:05:42", str(date_value))

    def test_xpath_num_uses_all_text_and_accepts_leading_decimal(self):
        root = parse_markup("<div>label <b>.69</b></div>")
        self.assertEqual(evaluate(root, "//div/num()", "html"), [0.69])
        nested = parse_markup(
            "<ul><li><span>900</span></li><li><span>1200</span></li></ul>")
        self.assertEqual(evaluate(
            nested, "//li[./span/num()>1000]/span/text()", "html"), ["1200"])

    def test_jsoupxpath_253_official_douban_matrix(self):
        html = Path(
            "base_history/JsoupXpath-2.5.3/src/test/resources/d_test.html"
        ).read_text(encoding="utf-8")
        root = parse_markup(html)
        cases = (
            ("//ul[@class='subject-list']/li[position()<3][last()]"
             "/div/h2/allText()",
             "黑客与画家 : 硅谷创业之父Paul Graham文集"
             "T2-黑客与画家 : 硅谷创业之父Paul Graham文集"),
            ("//ul[@class='subject-list']/li[2]/div/h2//text()",
             "黑客与画家: 硅谷创业之父Paul Graham文集"
             "T2-黑客与画家: 硅谷创业之父Paul Graham文集"),
            ("//ul[@class='subject-list']/li[first()]/div/h2/allText()",
             "失控 : 全人类的最终命运和结局"
             "T2-失控 : 全人类的最终命运和结局"),
            ("//ul[@class='subject-list']/li[./div/div/span[@class='pl']"
             "/num()>(1000+90*(2*50))][last()][1]/div/h2/allText()",
             "长尾理论长尾理论"),
            ("//ul[@class='subject-list']/li[contains(./div/div/"
             "span[@class='pl']//text(),'14582')]/div/h2//text()",
             "黑客与画家: 硅谷创业之父Paul Graham文集"
             "T2-黑客与画家: 硅谷创业之父Paul Graham文集"),
            ("//*[@id='subject_list']/ul[1]/li[8]/div[2]/div[2]/"
             "span[3]/num()", "3734"),
            ("//a[@id]/@href | //*[@id='subject_list']/ul[1]/li[8]/"
             "div[2]/div[2]/span[3]/num()",
             "https://www.douban.com/doumail/3734"),
        )
        for xpath, expected in cases:
            with self.subTest(xpath=xpath):
                self.assertEqual("".join(map(str, evaluate(root, xpath, "html"))),
                                 expected)
        self.assertEqual(len(evaluate(root, "//ul[@class]", "html")), 3)

    def test_jsoupxpath_253_official_text_node_matrix(self):
        root = parse_markup(
            '<p><span class="text-muted">分类：</span>动漫'
            '<span class="split-line"></span>'
            '<span class="text-muted hidden-xs">地区：</span>日本'
            '<span class="split-line"></span>'
            '<span class="text-muted hidden-xs">年份：</span>2010</p>')
        self.assertEqual(evaluate(root, "//text()[3]", "html"), ["2010"])
        self.assertEqual("".join(evaluate(root, "//text()", "html")),
                         "分类：动漫地区：日本年份：2010")
        child_order = parse_markup("<p> one <span> two</span> three </p>")
        self.assertEqual(evaluate(child_order, "//text()[2]", "html"),
                         ["three"])
        ancestry = parse_markup(
            "<div class='a'> a <div>need</div> "
            "<div class='e'> not need</div> c </div>")
        selected = set(evaluate(
            ancestry,
            "//div[@class='a']//text()[not(ancestor::div[@class='e'])]",
            "html"))
        self.assertEqual(selected, {"", "a", "need", "c"})

    def test_jsoupxpath_253_official_axis_matrix(self):
        root = parse_markup(
            "<div class='a'>1</div><div>2</div><div class='a'>3</div>"
            "<div>4</div><div>5</div>11<tag>6</tag>12"
            "<div>7<span>8</span></div>")
        cases = (
            ("//div[text()='5']/following-sibling::div/text()", "7"),
            ("//div[text()='5']/following-sibling::tag/text()", "6"),
            ("//div[text()='5']/following::text()", "11 6 12 7 8"),
            ("//div[text()='4']/following::div/text()", "5 7"),
            ("//div[text()='3']/preceding::text()", "2 1"),
        )
        for xpath, expected in cases:
            with self.subTest(xpath=xpath):
                self.assertEqual(" ".join(evaluate(root, xpath, "html")), expected)
        self.assertEqual(evaluate(
            root, "//div[text()='7']/preceding-sibling::div/text()", "html")[0],
            "5")
        self.assertEqual(evaluate(
            root, "//div[text()='5']/following-sibling::text()", "html")[0],
            "11")
        self.assertEqual(evaluate(
            root, "//div[text()='7']/preceding-sibling::text()", "html")[0],
            "12")

    def test_jsoupxpath_253_official_sum_and_num_matrix(self):
        root = parse_markup(Path(
            "base_history/JsoupXpath-2.5.3/src/test/resources/issue66.html"
        ).read_text(encoding="utf-8"))
        self.assertEqual(evaluate(
            root, "@XPath:count(//bookstore/book)", "html"), [4])
        self.assertEqual(evaluate(
            root, "@XPath:sum(//bookstore/book/year[num()<2005])", "html"),
            [4006])
        self.assertEqual(evaluate(
            root, "@XPath:sum(//bookstore/book/title)", "html"), [""])

    def test_jxnode_number_boolean_and_element_bridge_identity(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var numbers=java.getElements('@XPath:count(//p)');
            var flags=java.getElements('@XPath:boolean(//p)');
            var nodes=java.getElements('@XPath://p');
            [numbers.size(),numbers[0].isNumber(),numbers[0].asDouble(),
             numbers[0].value(),numbers[0].asString(),
             flags[0].isBoolean(),flags[0].asBoolean(),flags[0].value(),
             nodes[0].isElement(),nodes[0].asString(),
             nodes[0].asElement().tagName()];''',
            JsContext(source=source, result="<main><p>A</p></main>"))
        self.assertEqual(value, [1, True, 1, 1, "1", True, True, True,
                                 True, "<p>A</p>", "p"])

    def test_jsoup_get_elements_preserves_shared_dom_identity_and_mutation(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var nodes=java.getElements('div');
            var same=nodes.contains(nodes[0]);
            nodes[0].remove();
            [same,nodes[1].text(),java.getString('div@text')];''',
            JsContext(source=source, result="<main><div>A</div><div>B</div></main>"))
        self.assertEqual(value, [True, "B", "B"])

    def test_jsoupxpath_253_official_detail_tail_text(self):
        root = parse_markup(Path(
            "base_history/JsoupXpath-2.5.3/src/test/resources/d_detail_page.html"
        ).read_text(encoding="utf-8"))
        self.assertEqual(evaluate(
            root,
            "//*[@id='info']/span[contains(text(),'页数')]"
            "/following-sibling::text()",
            "html")[0], "956")
        self.assertEqual(evaluate(
            root,
            "//*[@id='info']/span[contains(text(),'定价')]"
            "/following-sibling::text()",
            "html")[0], "139.00元")

    def test_jsoupxpath_253_official_mixed_direct_text_index(self):
        root = parse_markup(
            "<div class='total'><p><span> 4497</span>总字数</p>"
            "<p><span> 0</span>总推荐</p></div>")
        self.assertEqual(evaluate(
            root, "//*[text()='总字数']//text()", "html"),
            ["4497", "总字数"])
        self.assertEqual(evaluate(
            root, "//*[text()='总字数']//text()[1]", "html"),
            ["4497", "总字数"])
        self.assertEqual(evaluate(
            root, "//*[text()='总字数']//text()[0]", "html"), [])
        self.assertEqual(evaluate(
            root, "//*[text()='总字数']//text()[2]", "html"), [])

    def test_xpath_xml_declaration_preserves_element_case(self):
        root = parse_markup(
            '<?xml version="1.0"?><Root><Item ID="A">One</Item>'
            '<item>two</item></Root>')
        self.assertEqual(evaluate(root, "//Item/text()", "html"), ["One"])
        self.assertEqual(evaluate(root, "//item/text()", "html"), ["two"])
        nodes, kind = select_nodes(
            '<Root><Item>One</Item><item>two</item></Root>',
            "application/xml", "//Item")
        self.assertEqual(kind, "html")
        self.assertEqual(len(nodes), 1)

    def test_xpath_xml_namespace_and_cdata_are_preserved(self):
        root = parse_markup(
            '<?xml version="1.0"?><feed xmlns:x="urn:test">'
            '<x:Item><![CDATA[A < B]]></x:Item></feed>')
        self.assertEqual(evaluate(root, "//x:Item/text()", "html"), ["A < B"])
        special = parse_markup(
            '<?xml version="1.0"?><root><!--note--><?next page?></root>')
        self.assertEqual(len(evaluate(special, "//comment()", "html")), 1)
        self.assertEqual(len(evaluate(
            special, "//processing-instruction('next')", "html")), 1)

    def test_javascript_get_elements_returns_jxnode_element_wrapper(self):
        runtime = LegadoJsRuntime()
        try:
            value = runtime.evaluate(
                '@js:var node=java.getElements("@XPath://p")[0];'
                '[node.isElement(),node.asString(),node.asElement().html()]',
                JsContext(source=SimpleNamespace(to_dict=lambda: {}),
                          result="<root><p><b>text</b></p></root>",
                          base_url="https://x"))
        finally:
            runtime.close()
        self.assertEqual(value, [True, "<p><b>text</b></p>", "<b>text</b>"])

    def test_jayway_jsonpath_tail_functions(self):
        data = {"numbers": [1, 2, 3, 4], "object": {"a": 1, "b": 2}}
        self.assertEqual(evaluate(data, "$.numbers.length()", "json"), [4])
        self.assertEqual(evaluate(data, "$.numbers.min()", "json"), [1.0])
        self.assertEqual(evaluate(data, "$.numbers.max()", "json"), [4.0])
        self.assertEqual(evaluate(data, "$.numbers.sum()", "json"), [10.0])
        self.assertEqual(evaluate(data, "$.numbers.avg()", "json"), [2.5])
        self.assertAlmostEqual(evaluate(data, "$.numbers.stddev()", "json")[0],
                               math.sqrt(1.25))
        self.assertEqual(evaluate(data, "$.object.keys()", "json"), [["a", "b"]])
        self.assertEqual(extract_value(data, "$.object.keys()", "json"), "[a, b]")
        self.assertEqual(evaluate(data, "$.numbers.first()", "json"), [1])
        self.assertEqual(evaluate(data, "$.numbers.last()", "json"), [4])
        self.assertEqual(evaluate(data, "$.numbers.index(-2)", "json"), [3])
        self.assertEqual(evaluate(data, "$.numbers.index(20)", "json"), [])
        self.assertEqual(evaluate(data, "$.numbers.append(5)", "json"),
                         [1, 2, 3, 4, 5])
        self.assertEqual(evaluate(data, "$.numbers.concat([5, 6])", "json"),
                         [1, 2, 3, 4, 5, 6])

    def test_forced_css_and_inline_rule(self):
        root = BeautifulSoup('<a href="/next">next</a>', "html.parser")
        self.assertEqual(evaluate(root, "@css:a:contains(next)@href", "html"), ["/next"])
        self.assertEqual(
            extract_value(root, "https://x{{@@a@href}}", "html"),
            "https://x/next",
        )

    def test_and_combines_field_values(self):
        root = BeautifulSoup("<b>A</b><i>B</i>", "html.parser")
        self.assertEqual(extract_value(root, "b@text&&i@text", "html"), "A\nB")

    def test_extracted_text_keeps_literal_double_hash(self):
        root = BeautifulSoup("<p>title ## section</p>", "html.parser")
        self.assertEqual(extract_value(root, "p@text", "html"), "title ## section")


class VariableTests(unittest.IsolatedAsyncioTestCase):

    async def test_fixed_http_interceptor_headers_and_null_ua(self):
        self.assertEqual(
            ReaderEngine().headers["User-Agent"],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36")

        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers, url = {}, "https://host.example/page"
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self): return b"ok"

        captured = []

        class Session:
            closed = False
            def __init__(self, *_args, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def request(self, *_args, **kwargs):
                # Wire headers now travel with the request, not the session.
                captured.append(kwargs["headers"])
                return Response()

        source = SimpleNamespace(
            bookSourceUrl="https://source", concurrentRate="",
            enabledCookieJar=False,
            header={"User-Agent": "null", "Cache-Control": "source-value",
                    "Cookie": "sid=temporary; local=1"})
        engine = ReaderEngine()
        engine.js.set_cookie("https://host.example/", "sid=stored; shared=1")
        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            await engine._request({"url": "https://host.example/page"}, source)
        headers = captured[0]
        self.assertNotIn("User-Agent", headers)
        self.assertEqual(headers.getall("Keep-Alive"), ["300"])
        self.assertEqual(headers.getall("Connection"), ["Keep-Alive"])
        self.assertEqual(headers.getall("Cache-Control"),
                         ["source-value", "no-cache"])
        self.assertEqual(headers["Cookie"],
                         "sid=temporary; shared=1; local=1")

    async def test_okhttp_transparent_gzip_conditions_and_headers(self):
        payload = b"fixed gzip body"
        compressed = gzip.compress(payload)

        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers = {"Content-Encoding": "gzip",
                       "Content-Length": str(len(compressed)),
                       "Content-Type": "text/plain"}
            url = "https://host/page"
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self): return compressed

        sessions, requests = [], []

        class Session:
            closed = False
            def __init__(self, *_args, **kwargs): sessions.append(kwargs)
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def request(self, *_args, **kwargs):
                requests.append(kwargs)
                return Response()

        engine = ReaderEngine()
        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            decoded = await engine._request(
                {"url": "https://host/default", "type": "hex"})
            decoded_headers = dict(engine.last_response["headers"])
            encoded = await engine._request({
                "url": "https://host/explicit", "type": "hex",
                "headers": {"Accept-Encoding": "gzip"}})
        self.assertEqual(decoded[0], payload.hex())
        self.assertNotIn("content-encoding", decoded_headers)
        self.assertNotIn("content-length", decoded_headers)
        self.assertEqual(encoded[0], compressed.hex())
        self.assertEqual(engine.last_response["headers"]["content-encoding"],
                         "gzip")
        self.assertFalse(sessions[0]["auto_decompress"])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(requests[0]["headers"]["Accept-Encoding"], "gzip")
        self.assertEqual(requests[1]["headers"].getall("Accept-Encoding"),
                         ["gzip"])

    async def test_a_source_reuses_one_connection_across_requests(self):
        peers = set()

        async def handler(request):
            peers.add(request.transport.get_extra_info("peername")[:2])
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/page", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        source = SimpleNamespace(bookSourceUrl=base, enabledCookieJar=True,
                                 header="", concurrentRate="")
        engine = ReaderEngine()
        try:
            await engine._request({"url": base + "/page"}, source)
            await engine._request({"url": base + "/page"}, source)
        finally:
            await engine.aclose()
            await runner.cleanup()
        # Both calls came out of the same pooled socket, as OkHttp would.
        self.assertEqual(len(peers), 1)
        self.assertEqual(engine._sessions, {})

    async def test_a_dead_pooled_connection_gets_one_extra_attempt(self):
        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers, url = {}, "https://host/page"
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self): return b"ok"

        calls = []

        class Session:
            closed = False
            def __init__(self, *_args, **_kwargs): pass
            def request(self, *_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise aiohttp.ServerDisconnectedError("keep-alive 已断开")
                return Response()

        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            result = await ReaderEngine()._request({"url": "https://host/page"})
        self.assertEqual(result[0], "ok")
        self.assertEqual(len(calls), 2)

        class AlwaysDead(Session):
            def request(self, *_args, **_kwargs):
                calls.append(1)
                raise aiohttp.ServerDisconnectedError("keep-alive 已断开")

        calls.clear()
        with patch("app.services.rule_engine.aiohttp.ClientSession", AlwaysDead), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            with self.assertRaises(RuntimeError):
                await ReaderEngine()._request({"url": "https://host/page"})
        # 只补一次：第二条连接同样断开就说明站点真的不可用。
        self.assertEqual(len(calls), 2)

    async def test_engine_pools_one_session_per_cookie_jar_and_proxy(self):
        engine = ReaderEngine()
        first = SimpleNamespace(bookSourceUrl="https://pooled.example")
        second = SimpleNamespace(bookSourceUrl="https://other.example")
        cookie_off = SimpleNamespace(bookSourceUrl="https://pooled.example",
                                     enabledCookieJar=False)
        plain = engine._session_for(first, "", False)
        self.assertIs(plain, engine._session_for(first, "", False))
        self.assertIsNot(plain, engine._session_for(second, "", False))
        self.assertIsNot(plain, engine._session_for(first, "http://1.2.3.4:8080", False))
        # 关掉 cookie 罐的书源共用无状态的那个会话。
        self.assertIs(engine._session_for(cookie_off, "", False),
                      engine._session_for(None, "", False))
        await engine.aclose()
        self.assertTrue(plain.closed)
        self.assertEqual(engine._sessions, {})

    async def test_connect_timeout_caps_only_the_connection_phase(self):
        seen = {}

        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers, url = {}, "https://host/page"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return b"ok"

        class Session:
            closed = False

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **kwargs):
                seen["timeout"] = kwargs.get("timeout")
                return Response()

        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            await ReaderEngine(timeout=10, connect_timeout=5)._request(
                {"url": "https://host/page"})
        # 换源扫描靠 5 秒连接上限掐掉挂死域名，正文读取仍可用满 10 秒。
        self.assertEqual(seen["timeout"].total, 10)
        self.assertEqual(seen["timeout"].connect, 5)

    async def test_engine_without_connect_timeout_matches_old_behaviour(self):
        seen = {}

        class Session:
            closed = False

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **kwargs):
                seen["timeout"] = kwargs.get("timeout")
                raise asyncio.TimeoutError("slow")

        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            with self.assertRaises(RequestTimeoutError):
                await ReaderEngine(timeout=15)._request({"url": "https://host/page"})
        self.assertEqual(seen["timeout"].total, 15)
        self.assertIsNone(seen["timeout"].connect)

    async def test_transport_failures_keep_their_kind(self):
        """超时和拒连要能分辨：一个可能是站点慢，另一个只说明主机不可达。"""
        class Failure:
            def __init__(self, error):
                self.error = error

            async def __aenter__(self):
                raise self.error

            async def __aexit__(self, *_args):
                return False

        def session_raising(error):
            class Session:
                closed = False

                def __init__(self, *_args, **_kwargs):
                    pass

                def request(self, *_args, **_kwargs):
                    return Failure(error)

            return Session

        refused = aiohttp.ClientConnectorError(
            ConnectionKey("host", 80, False, None, None, None, None),
            OSError(61, "拒绝连接"))
        for error, expected in ((asyncio.TimeoutError("slow"), RequestTimeoutError),
                                (refused, RequestConnectionError)):
            with patch("app.services.rule_engine.aiohttp.ClientSession",
                       session_raising(error)), \
                    patch("app.services.rule_engine.aiohttp.TCPConnector",
                          lambda **_kwargs: object()):
                with self.assertRaises(expected) as caught:
                    await ReaderEngine()._request({"url": "https://host/page"})
            # 上层（检测、换源）是按 RuntimeError 兜底的，类型必须兼容。
            self.assertIsInstance(caught.exception, RuntimeError)

    async def test_a_timeout_says_how_long_we_waited(self):
        """aiohttp 的超时异常字面为空，照抄只会得到一句“连接超时：”。"""
        class Session:
            closed = False

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise asyncio.TimeoutError()

        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            with self.assertRaises(RequestTimeoutError) as caught:
                await ReaderEngine(timeout=10)._request({"url": "https://host/page"})

        self.assertEqual(str(caught.exception), "连接超时：10 秒无响应")

    async def test_a_timeout_reason_from_the_transport_is_kept(self):
        class Session:
            closed = False

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise asyncio.TimeoutError("read timeout")

        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            with self.assertRaises(RequestTimeoutError) as caught:
                await ReaderEngine(timeout=10)._request({"url": "https://host/page"})

        self.assertEqual(str(caught.exception), "连接超时：read timeout")

    async def test_redirect_method_body_and_cross_origin_authorization(self):
        observed = {}

        async def final_302(request):
            observed["302"] = (request.method, await request.read())
            return web.Response(text="ok302")

        async def final_307(request):
            observed["307"] = (request.method, await request.read())
            return web.Response(text="ok307")

        async def cross_target(request):
            observed["authorization"] = request.headers.get("Authorization")
            return web.Response(text="cross")

        async def redirect_302(_request):
            return web.Response(status=302, headers={"Location": "/final302"})

        async def redirect_307(_request):
            return web.Response(status=307, headers={"Location": "/final307"})

        second_app = web.Application()
        second_app.router.add_route("*", "/target", cross_target)
        second_runner = web.AppRunner(second_app)
        await second_runner.setup()
        second_site = web.TCPSite(second_runner, "127.0.0.1", 0)
        await second_site.start()
        second_port = second_site._server.sockets[0].getsockname()[1]

        first_app = web.Application()
        first_app.router.add_route("*", "/final302", final_302)
        first_app.router.add_route("*", "/final307", final_307)
        async def redirect_cross(_request):
            return web.Response(status=302, headers={
                "Location": f"http://127.0.0.1:{second_port}/target"})

        first_app.router.add_post("/start302", redirect_302)
        first_app.router.add_post("/start307", redirect_307)
        first_app.router.add_get("/cross", redirect_cross)
        first_runner = web.AppRunner(first_app)
        await first_runner.setup()
        first_site = web.TCPSite(first_runner, "127.0.0.1", 0)
        await first_site.start()
        first_port = first_site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{first_port}"
        engine = ReaderEngine()
        try:
            await engine._request({
                "url": base + "/start302", "method": "POST", "body": "q=x"})
            await engine._request({
                "url": base + "/start307", "method": "POST", "body": "q=x"})
            await engine._request({
                "url": base + "/cross",
                "headers": {"Authorization": "Bearer secret"}})
        finally:
            await first_runner.cleanup()
            await second_runner.cleanup()
        self.assertEqual(observed["302"], ("GET", b""))
        self.assertEqual(observed["307"], ("POST", b"q=x"))
        self.assertIsNone(observed["authorization"])

    async def test_request_retry_defaults_and_explicit_count_match_legado(self):
        engine = ReaderEngine()
        self.assertEqual(engine._request_retry_count({}), 0)
        self.assertEqual(engine._request_retry_count({"retry": 3}), 3)
        self.assertEqual(engine._request_retry_count({"retry": -1}), -1)
        self.assertEqual(engine._request_retry_count({"retry": "invalid"}), 0)

    def test_custom_url_python_port_preserves_attributes_and_serialization(self):
        value = CustomUrl('https://host/path , {"type":"zip","retry":2}')
        self.assertEqual(value.get_url(), "https://host/path")
        self.assertEqual(value.get_attr(), {"type": "zip", "retry": 2})
        value.put_attribute("retry", None).put_attribute("serverID", 9)
        self.assertEqual(str(value),
                         'https://host/path,{"type":"zip","serverID":9}')
        self.assertEqual(str(CustomUrl("https://host/path,{invalid}")),
                         "https://host/path")

    def test_js_request_retry_only_repeats_unsuccessful_http_responses(self):
        runtime = LegadoJsRuntime()

        class Opener:
            def __init__(self, failures):
                self.failures = list(failures)
                self.calls = 0

            def open(self, *_args, **_kwargs):
                self.calls += 1
                value = self.failures.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        def http_error(code=503):
            return HTTPError("https://host", code, "failed", Message(), BytesIO(b"x"))

        success = object()
        http_opener = Opener([http_error(), success])
        self.assertIs(runtime._open_with_retry(object(), 2, http_opener), success)
        self.assertEqual(http_opener.calls, 2)

        final_opener = Opener([http_error(500), http_error(502)])
        final = runtime._open_with_retry(object(), 1, final_opener)
        self.assertIsInstance(final, HTTPError)
        self.assertEqual(final.code, 502)
        final.close()

        connection_opener = Opener([URLError("offline"), success])
        with self.assertRaises(URLError):
            runtime._open_with_retry(object(), 3, connection_opener)
        self.assertEqual(connection_opener.calls, 1)

    async def test_reader_request_retries_http_status_but_not_connection_failure(self):
        class Response:
            def __init__(self, status, body):
                self.status, self._body = status, body
                self.headers, self.charset = {}, "utf-8"
                self.content_type, self.url = "text/plain", "https://host/page"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return self._body

        calls = []
        responses = [Response(503, b"retry"), Response(200, b"ok")]

        class Session:
            closed = False
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def request(self, *_args, **_kwargs):
                calls.append(1)
                return responses.pop(0)

        engine = ReaderEngine()
        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector", lambda **_kwargs: object()):
            result = await engine._request({"url": "https://host/page", "retry": 3})
        self.assertEqual(result[0], "ok")
        self.assertEqual(len(calls), 2)

        class Failure:
            async def __aenter__(self):
                raise asyncio.TimeoutError("offline")

            async def __aexit__(self, *_args):
                return False

        calls.clear()

        class FailedSession(Session):
            def request(self, *_args, **_kwargs):
                calls.append(1)
                return Failure()

        # A pooled engine would keep reusing the session patched above.
        with patch("app.services.rule_engine.aiohttp.ClientSession", FailedSession), \
                patch("app.services.rule_engine.aiohttp.TCPConnector", lambda **_kwargs: object()):
            with self.assertRaises(RuntimeError):
                await ReaderEngine()._request({"url": "https://host/page", "retry": 3})
        self.assertEqual(len(calls), 1)

    def test_url_option_charset_does_not_override_response_decoding(self):
        utf8 = "返回正文".encode("utf-8")
        gbk = '<meta charset="gbk">中文'.encode("gb18030")
        self.assertEqual(ReaderEngine._decode_response_body(utf8, "utf-8"),
                         "返回正文")
        self.assertEqual(ReaderEngine._decode_response_body(
            b"\xef\xbb\xbf" + utf8, None),
                         "返回正文")
        self.assertEqual(ReaderEngine._decode_response_body(gbk, None),
                         '<meta charset="gbk">中文')

        class Response:
            status = 200
            headers = Message()
            headers["Content-Type"] = "text/plain; charset=utf-8"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return utf8

            def geturl(self):
                return "https://source.example"

        runtime = LegadoJsRuntime()
        runtime.opener = SimpleNamespace(open=lambda *_args, **_kwargs: Response())
        response = json.loads(runtime._request_json(
            'https://source.example,{"charset":"gbk"}'))
        self.assertEqual(response["body"], utf8.decode("utf-8"))

    def test_html_encoding_meta_scan_matches_fixed_encoding_detect(self):
        declared = (b'<meta http-equiv="content-type" '
                    b'content="text/html;charset=gbk">')
        self.assertEqual(ReaderEngine._html_declared_encoding(declared), "gbk")
        ignored = b'<div data-charset="gbk"></div>'
        self.assertIsNone(ReaderEngine._html_declared_encoding(ignored))
        odd_spacing = (b'<meta http-equiv="content-type" '
                       b'content="text/html;charset = gbk">')
        self.assertEqual(ReaderEngine._html_declared_encoding(odd_spacing), "= gbk")
        with self.assertRaises(LookupError):
            ReaderEngine._decode_response_body(odd_spacing + b"text", None)

    def test_xml_bodies_keep_the_html_parser_without_warning(self):
        raw = ('<?xml version="1.0" encoding="utf-8"?><rss><channel>'
               '<meta charset="gbk"/></channel></rss>').encode("utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            declared = ReaderEngine._html_declared_encoding(raw)
        self.assertEqual(declared, "gbk")
        self.assertNotIn("XMLParsedAsHTMLWarning",
                         [type(warning.message).__name__ for warning in caught])

    def test_icu_detector_matches_fixed_east_asian_oracle_fixtures(self):
        chinese = ("中文章节内容测试" * 10).encode("gb18030")
        unusual_big5 = ("日本語の本文です" * 10).encode("big5")
        self.assertEqual(detect_charset(chinese), "GB18030")
        # The fixed bundled ICU4J detector deliberately supplies this oracle;
        # UnicodeDammit instead reports Big5 for the same ambiguous bytes.
        self.assertEqual(detect_charset(unusual_big5), "KOI8-R")

    def test_fixed_icu_disabled_models_unicode_scores_and_greek_ngrams(self):
        self.assertEqual(
            detect_charset(("한국어 본문입니다" * 5).encode("gb18030")),
            "GB18030")
        self.assertEqual(
            detect_charset(("Ελληνικό κείμενο" * 5).encode("gb18030")),
            "windows-1253")
        self.assertEqual(
            detect_charset(("café déjà vu français" * 2).encode("iso-8859-1")),
            "UTF-16LE")
        self.assertEqual(
            detect_charset("中文章节内容测试".encode("utf-16le")),
            "windows-1252")

    def test_xml_post_uses_same_default_json_media_type_as_legado(self):
        self.assertEqual(ReaderEngine._default_post_content_type(
            "<request><key>value</key></request>"),
            "application/json; charset=UTF-8")

    def test_zip_response_first_entry_and_xml_declaration_match_legado(self):
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("folder/", b"")
            archive.writestr("content.txt", "压缩正文".encode("utf-8"))
            archive.writestr("ignored.txt", b"ignored")
        raw = ReaderEngine._uncompress_response_body(
            payload.getvalue(), "application/zip; charset=utf-8")
        self.assertEqual(raw.decode("utf-8"), "压缩正文")
        self.assertEqual(ReaderEngine._normalize_response_text(
            "<book>正文</book>", "application/xml; charset=utf-8"),
            '<?xml version="1.0"?><book>正文</book>')
        declared = '<?xml version="1.0"?><book />'
        self.assertEqual(ReaderEngine._normalize_response_text(
            declared, "text/xml"), declared)
    async def test_template_selector_then_postfix_js_keeps_pipeline(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")
        chapter = SimpleNamespace(title="Second", name="Second")
        node = {"items": [
            {"title": "First", "url": "/1"},
            {"title": "Second", "url": "/2"},
        ]}

        value = engine._field_value(
            node,
            '$.items[?(@.title=="{{chapter.title}}")].url\n'
            '@js:String(result)+"?ready=1"',
            "json", source, source.bookSourceUrl, chapter=chapter,
        )

        self.assertEqual(value, "https://example.com/2?ready=1")

    async def test_dynamic_url_js_result_runs_second_template_pass(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")

        value = await engine._resolve_rule_url(
            '@js:"https://example.com/result?page={{page-1}}&key={{key}}"',
            source, key="book", page=3,
        )

        self.assertEqual(value, "https://example.com/result?page=2&key=book")

    async def test_inline_url_template_preserves_numeric_zero_only(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")

        first_page = await engine._resolve_rule_url(
            "https://example.com/list?page={{page - 1}}&flag={{false}}",
            source, page=1,
        )
        second_page = await engine._resolve_rule_url(
            "https://example.com/list?offset={{(page - 1) * 20}}",
            source, page=2,
        )

        self.assertEqual(first_page,
                         "https://example.com/list?page=0&flag=")
        self.assertEqual(second_page,
                         "https://example.com/list?offset=20")

    async def test_url_templates_are_not_truncated_at_thirty_two_stages(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        template = "https://x/" + "/".join("{{page}}" for _ in range(40))
        value = await engine._resolve_rule_url(template, source, page=7)
        self.assertEqual(value, "https://x/" + "/".join(["7"] * 40))

    async def test_bad_optional_field_does_not_abort_other_list_records(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes = [
            BeautifulSoup('<li><b>header</b></li>', "html.parser").li,
            BeautifulSoup('<li><a href="/book/42">Book</a></li>', "html.parser").li,
        ]
        rules = {
            "name": "a@text||b@text", "bookUrl": "a@href",
            "coverUrl": '<js>String(result).match(/id=(\\d+)/)[1]</js>',
        }
        books = engine._books_from_nodes(
            nodes, "html", rules, "https://x", source)
        usable = [book for book in books if book.name and book.url]
        self.assertEqual([(book.name, book.url) for book in usable],
                         [("header", "https://x"),
                          ("Book", "https://x/book/42")])

    async def test_bad_mandatory_search_field_propagates(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        node = BeautifulSoup('<li><a href="/42">Book</a></li>',
                             "html.parser").li
        with self.assertRaises(JsBridgeError):
            engine._books_from_nodes(
                [node], "html", {
                    "name": '<js>String(result).match(/missing=(\\d+)/)[1]</js>',
                    "bookUrl": "a@href",
                }, "https://x", source)

    async def test_source_concurrent_rate_supports_interval_and_window_forms(self):
        engine = ReaderEngine()
        interval_source = SimpleNamespace(
            bookSourceUrl="https://interval", concurrentRate="25")
        starts = []

        async def interval_call():
            await engine._respect_concurrent_rate(interval_source)
            starts.append(time.monotonic())

        await asyncio.gather(*(interval_call() for _ in range(3)))
        self.assertGreaterEqual(starts[-1] - starts[0], 0.045)

        window_source = SimpleNamespace(
            bookSourceUrl="https://window", concurrentRate="2/40")
        starts.clear()

        async def window_call():
            await engine._respect_concurrent_rate(window_source)
            starts.append(time.monotonic())

        # AnalyzeUrl 3.23.110211 checks frequency > N (not >= N), so 2/T
        # permits three starts in the first window and delays the fourth.
        await asyncio.gather(*(window_call() for _ in range(4)))
        self.assertGreaterEqual(starts[-1] - starts[0], 0.035)
        self.assertLess(starts[2] - starts[0], 0.02)

    def test_proxy_parser_matches_fixed_legado_grammar(self):
        self.assertEqual(
            ReaderEngine._normalize_proxy("http://proxy.example:8080"),
            "http://proxy.example:8080",
        )
        self.assertEqual(
            ReaderEngine._normalize_proxy(
                "http://proxy.example:8080@user name@p@ss"),
            "http://user%20name:p@proxy.example:8080",
        )
        self.assertEqual(
            ReaderEngine._normalize_proxy("socks5://127.0.0.1:1080"),
            "socks5://127.0.0.1:1080",
        )
        with self.assertRaises(ValueError):
            ReaderEngine._normalize_proxy("proxy.example:8080")

    async def test_socks_proxy_is_installed_as_session_connector(self):
        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers, url = {}, "https://host/page"

            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self): return b"ok"

        sessions, requests = [], []

        class Session:
            closed = False
            def __init__(self, *_args, **kwargs): sessions.append(kwargs)
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def request(self, *_args, **kwargs):
                requests.append(kwargs)
                return Response()

        socks_connector = object()
        source = SimpleNamespace(
            bookSourceUrl="https://source", enabledCookieJar=False,
            header='{"proxy":"socks5://127.0.0.1:1080@user@pass"}',
        )
        with patch("app.services.rule_engine.ProxyConnector.from_url",
                   return_value=socks_connector) as make_socks, \
                patch("app.services.rule_engine.aiohttp.ClientSession", Session):
            result = await ReaderEngine()._request({
                "url": "https://host/page",
                "proxy": "socks5://ignored.example:1080",
            }, source)
        self.assertEqual(result[0], "ok")
        make_socks.assert_called_once_with(
            "socks5://user:pass@127.0.0.1:1080", ssl=False)
        self.assertIs(sessions[0]["connector"], socks_connector)
        self.assertNotIn("proxy", requests[0])

    async def test_concurrent_rate_is_shared_between_engine_instances(self):
        source = SimpleNamespace(
            bookSourceUrl=f"https://shared-{time.monotonic_ns()}",
            concurrentRate="30")
        starts = []

        async def call(engine):
            await engine._respect_concurrent_rate(source)
            starts.append(time.monotonic())

        await asyncio.gather(call(ReaderEngine()), call(ReaderEngine()))
        self.assertGreaterEqual(starts[-1] - starts[0], 0.025)

    async def test_bare_concurrent_rate_holds_record_until_request_finishes(self):
        release_first = asyncio.Event()
        starts = []

        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            headers, url = {}, "https://host/page"

            def __init__(self, index): self.index = index
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self):
                if self.index == 0:
                    await release_first.wait()
                return b"ok"

        class Session:
            closed = False
            def __init__(self, *_args, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def request(self, *_args, **_kwargs):
                starts.append(time.monotonic())
                return Response(len(starts) - 1)

        source = SimpleNamespace(
            bookSourceUrl=f"https://occupied-{time.monotonic_ns()}",
            concurrentRate="10", header="", enabledCookieJar=False)
        engine = ReaderEngine()
        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            first = asyncio.create_task(engine._request(
                {"url": "https://host/first"}, source))
            while not starts:
                await asyncio.sleep(0)
            second = asyncio.create_task(engine._request(
                {"url": "https://host/second"}, source))
            await asyncio.sleep(0.03)
            self.assertEqual(len(starts), 1)
            release_first.set()
            await asyncio.gather(first, second)
        self.assertEqual(len(starts), 2)

    async def test_cookie_jars_respect_source_enable_and_isolation(self):
        engine = ReaderEngine()
        first = SimpleNamespace(bookSourceUrl="https://first",
                                enabledCookieJar=True)
        second = SimpleNamespace(bookSourceUrl="https://second",
                                 enabledCookieJar=True)
        disabled = SimpleNamespace(bookSourceUrl="https://disabled",
                                   enabledCookieJar=False)
        first_jar = engine._cookie_jar_for_source(first)
        self.assertIs(first_jar, engine._cookie_jar_for_source(first))
        self.assertIsNot(first_jar, engine._cookie_jar_for_source(second))
        self.assertIsInstance(engine._cookie_jar_for_source(disabled),
                              aiohttp.DummyCookieJar)
        self.assertIsInstance(engine._cookie_jar_for_source(None),
                              aiohttp.DummyCookieJar)
        nullable = SimpleNamespace(bookSourceUrl="https://nullable",
                                   enabledCookieJar=None)
        self.assertIsInstance(engine._cookie_jar_for_source(nullable),
                              aiohttp.DummyCookieJar)

    async def test_source_cookie_jar_persists_between_engine_instances(self):
        source = SimpleNamespace(
            bookSourceUrl="https://source.example", enabledCookieJar=True)
        with TemporaryDirectory() as directory:
            first = ReaderEngine(state_dir=directory)
            jar = first._cookie_jar_for_source(source)
            jar.update_cookies({"sid": "abc"},
                               response_url=URL("https://source.example/book"))
            first._persist_source_cookie_jar(source)
            second = ReaderEngine(state_dir=directory)
            restored = second._cookie_jar_for_source(source).filter_cookies(
                URL("https://source.example/next"))
            self.assertEqual(restored["sid"].value, "abc")

    async def test_reader_and_js_requests_share_cookie_manager_state(self):
        captured = {}

        class Headers(dict):
            def getall(self, name, default):
                value = self.get(name)
                return [value] if value else default

        class Response:
            status, charset, content_type = 200, "utf-8", "text/plain"
            url = "https://source.example/page"
            headers = Headers({"Set-Cookie": "server=2; Path=/"})

            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def read(self): return b"ok"

        class Session:
            closed = False
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def request(self, *_args, **kwargs):
                captured["headers"] = kwargs.get("headers", {})
                return Response()

        source = SimpleNamespace(
            bookSourceUrl="https://source.example", enabledCookieJar=True,
            header="", to_dict=lambda: {"bookSourceUrl": "https://source.example"})
        engine = ReaderEngine()
        engine.js.set_cookie("https://source.example/", "client=1; Path=/")
        with patch("app.services.rule_engine.aiohttp.ClientSession", Session), \
                patch("app.services.rule_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            await engine._request({"url": "https://source.example/page"}, source)
        self.assertEqual(captured["headers"]["Cookie"], "client=1")
        self.assertIn("server=2", engine.js.export_cookie_header(
            "https://source.example/next"))

    def test_legado_page_alternative_template(self):
        first = parse_search_url("/list/<,2,3>?q={{key}}", "https://source.example", "书", 1)
        fourth = parse_search_url("/list/<,2,3>?q={{key}}", "https://source.example", "书", 4)
        self.assertEqual(first["url"], "https://source.example/list/?q=%E4%B9%A6")
        self.assertEqual(fourth["url"], "https://source.example/list/3?q=%E4%B9%A6")

    async def test_data_uri_request_supports_legado_type_hex_transport(self):
        engine = ReaderEngine()
        source = SimpleNamespace(bookSourceUrl="https://source", to_dict=lambda: {})
        request = parse_search_url(
            'data:text/plain;base64,5Lmm5rqQ,{"type":"token"}',
            source.bookSourceUrl, "", 1)
        body, content_type, _ = await engine._request(request, source)
        self.assertEqual(bytes.fromhex(body).decode("utf-8"), "书源")
        self.assertEqual(content_type, "text/plain")

    async def test_url_option_js_updates_url_and_request_headers(self):
        engine = ReaderEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", header='{"Base":"1"}',
            to_dict=lambda: {"bookSourceUrl": "https://source",
                             "header": '{"Base":"1"}'})
        request = await engine._apply_request_option_js({
            "url": "https://source/start", "headers": {"Request": "2"},
            "js": "java.headerMap.put('Cookie','verified=1');java.url+'/next'",
        }, source)
        self.assertEqual(request["url"], "https://source/start/next")
        self.assertEqual(request["headers"]["Base"], "1")
        self.assertEqual(request["headers"]["Request"], "2")
        self.assertEqual(request["headers"]["Cookie"], "verified=1")

    async def test_dynamic_source_header_is_evaluated_per_request(self):
        engine = ReaderEngine()
        source_data = {
            "bookSourceUrl": "https://source.example",
            "header": '@js:JSON.stringify({Referer:baseUrl,"X-Token":token()})',
            "jsLib": 'function token(){return "dynamic"}',
        }
        source = SimpleNamespace(**source_data, to_dict=lambda: source_data)
        headers = await engine._resolved_source_headers(
            source, "https://source.example/chapter/1")
        self.assertEqual(headers["Referer"], "https://source.example")
        self.assertEqual(headers["X-Token"], "dynamic")
        self.assertIn("User-Agent", headers)

    async def test_request_source_headers_overlay_cached_login_header(self):
        engine = ReaderEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source.example", header='{"A":"source"}',
            enabledCookieJar=True,
        )
        engine.js.evaluate(
            '@js:source.putLoginHeader(\'{"A":"login","Authorization":"token"}\')',
            JsContext(source=source, variables=engine.js_variables))
        headers = await engine._resolved_source_headers(source)
        self.assertEqual(headers["A"], "login")
        self.assertEqual(headers["Authorization"], "token")

    async def test_book_list_put_variables_are_applied_per_record(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        soup = BeautifulSoup(
            '<li><a href="/1">One</a><span>Alice</span></li>'
            '<li><a href="/2">Two</a><span>Bob</span></li>', "html.parser")
        selector, put_rule = engine._list_rule_parts(
            "li@put:{author:'span@text'}")
        nodes = evaluate(soup, selector, "html", list_context=True)
        books = engine._books_from_nodes(
            nodes, "html", {"name": "a@text", "bookUrl": "a@href",
                            "author": "@get:{author}"},
            "https://source", source, list_put=put_rule)
        self.assertEqual([(book.name, book.author) for book in books],
                         [("One", "Alice"), ("Two", "Bob")])

    async def test_book_list_is_not_truncated_after_one_hundred_results(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        soup = BeautifulSoup("".join(
            f'<li><a href="/{index}">Book {index}</a></li>'
            for index in range(125)), "html.parser")
        books = engine._books_from_nodes(
            list(soup.select("li")), "html",
            {"name": "a@text", "bookUrl": "a@href"},
            source.bookSourceUrl, source)
        self.assertEqual(len(books), 125)
        self.assertEqual(books[-1].name, "Book 124")

    async def test_request_rule_runs_leading_js_before_plain_url(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://example.com")
        value = await engine._resolve_rule_url(
            '<js>java.put("token","ok")</js>/search?key={{key}}&page={{page}}',
            source, "book", 3,
        )
        self.assertEqual(value, "/search?key=book&page=3")
        self.assertEqual(engine.js_variables["token"], "ok")

    async def test_request_rule_js_stages_share_analyze_url_result(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {},
                                 bookSourceUrl="https://example.com")
        value = await engine._resolve_rule_url(
            'first<js>String(result).startsWith("first") ? "one" : "bad"'
            '</js>/@result/<js>String(result)+"/three"</js>',
            source,
        )
        self.assertEqual(value, "/one//three")

    async def test_request_rule_runs_trailing_js_with_plain_prefix_as_result(self):
        engine = ReaderEngine()
        engine.js._request_json = lambda _value, *_: json.dumps({
            "body": "", "headers": {}, "status": 200,
            "url": "https://example.com/result/searchid/9.html"})
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"},
                                 bookSourceUrl="https://example.com")
        value = await engine._resolve_rule_url(
            'https://example.com/search,{"method":"POST","body":"q={{key}}"}\n'
            '@js:java.connect(String(result).replace("{{key}}",key)).url()',
            source, "book", 1)
        self.assertEqual(value, "https://example.com/result/searchid/9.html")

    async def test_put_get_chain(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        value = engine._field_value(
            {"name": "Book", "id": 42},
            "$.name@put:{bid:$.id}", "json", source, "",
        )
        self.assertEqual(value, "Book")
        self.assertEqual(engine.js_variables["bid"], "42")
        self.assertEqual(
            engine._field_value({}, "https://x/toc?id=@get:{bid}", "json", source, ""),
            "https://x/toc?id=42",
        )

    async def test_put_block_keeps_following_js_and_runs_before_it(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        value = engine._field_value(
            {"name": "书名", "id": 42},
            "$.name@put:{saved:$.id}@js:String(java.get('saved'))+'-'+result",
            "json", source, source.bookSourceUrl)
        self.assertEqual(value, "42-书名")
        self.assertEqual(engine.js_variables["saved"], "42")

    def test_list_put_parser_preserves_suffix_pipeline(self):
        selector, put_rule = ReaderEngine._list_rule_parts(
            "$.items[*]@put:{page:$.page}@js:result")
        self.assertEqual(selector, "$.items[*]@js:result")
        self.assertEqual(put_rule, "{page:$.page}")

    def test_python_source_rule_compiler_preserves_stage_order_and_puts(self):
        program = compile_rule_program(
            "$.name@put:{id:$.id}<js>result+'!'</js>##!##?@js:result")
        self.assertEqual([stage.mode for stage in program.stages],
                         ["json", "js", "default", "js"])
        self.assertEqual(program.stages[0].rule, "$.name")
        self.assertEqual(program.stages[0].put_rule, "{id:$.id}")
        self.assertEqual(program.stages[1].rule, "result+'!'")

    def test_python_source_rule_compiler_splits_webjs_stage(self):
        program = compile_rule_program("$.list@webjs:JSON.stringify(result)")
        self.assertEqual([stage.mode for stage in program.stages],
                         ["json", "webjs"])
        self.assertEqual(program.stages[1].rule, "JSON.stringify(result)")
        leading = compile_rule_program("@webjs:document.title")
        self.assertEqual(leading.stages[0].mode, "webjs")
        self.assertEqual(leading.selector_and_suffix(),
                         ("", "@webjs:document.title"))
        # WebJS_PATTERN requires at least 5 characters, shorter stays plain.
        self.assertEqual(compile_rule_program("@webjs:ab").stages[0].mode,
                         "default")

    def test_webjs_rule_stage_evaluates_in_browser_with_result_binding(self):
        runtime = LegadoJsRuntime()
        calls = []

        def fetcher(url, _title, options):
            calls.append((url, dict(options)))
            return {"body": "", "url": url, "status": 200, "headers": {},
                    "jsResult": "第一章"}

        runtime.browser_fetcher = fetcher
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://example"})
        value = runtime.evaluate(
            "@webjs:JSON.parse(result)[0] + '章'",
            JsContext(source=source, result='["一","二"]',
                      base_url="https://example.com/book/1",
                      src="<html><body>page</body></html>"))
        self.assertEqual(value, "第一章")
        _url, options = calls[0]
        self.assertEqual(options["mode"], "webJs")
        self.assertEqual(options["url"], "https://example.com/book/1")
        self.assertEqual(options["html"], "<html><body>page</body></html>")
        self.assertIn('window.result = "[\\"一\\",\\"二\\"]"',
                      options["javaScript"])
        self.assertIn("__legadoJsResult", options["javaScript"])
        self.assertIn("JSON.parse(result)[0]", options["javaScript"])

    def test_webjs_rule_stage_reports_script_errors_and_missing_backend(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://e"})
        with self.assertRaises(JsBridgeError):
            runtime.evaluate("@webjs:any script text", JsContext(source=source))
        runtime.browser_fetcher = lambda url, _title, options: {
            "body": "", "jsResult": "__LEGADO_JS_ERROR__:ReferenceError: x is not defined"}
        with self.assertRaises(JsBridgeError):
            runtime.evaluate("@webjs:any script text", JsContext(source=source))

    def test_python_source_rule_compiler_matches_historical_mode_promotion(self):
        cases = {
            "@XPath://a": ("xpath", "//a"),
            "@Json:$.items": ("json", "$.items"),
            "@@div@text": ("default", "div@text"),
            "prefix@get:{key}": ("regex", "prefix@get:{key}"),
            "$1-title": ("regex", "$1-title"),
            "div@text##x##{{result}}": ("default", "div@text##x##{{result}}"),
        }
        for raw, expected in cases.items():
            stage = compile_rule_program(raw).stages[0]
            self.assertEqual((stage.mode, stage.rule), expected)
        stage = compile_rule_program(":ID:(\\d+)", all_in_one=True).stages[0]
        self.assertEqual((stage.mode, stage.rule), ("regex", "ID:(\\d+)"))

    def test_python_source_rule_compiler_honors_analyzer_context_mode(self):
        json_stage = compile_rule_program("name", is_json=True).stages[0]
        self.assertEqual((json_stage.mode, json_stage.rule), ("json", "name"))
        explicit_xpath = compile_rule_program(
            "@XPath://item", is_json=True).stages[0]
        self.assertEqual(
            (explicit_xpath.mode, explicit_xpath.rule), ("xpath", "//item"))
        regex_program = compile_rule_program(
            "$.name<js>result</js>@XPath://item", is_regex=True)
        self.assertEqual(
            [(stage.mode, stage.rule) for stage in regex_program.stages],
            [("regex", "$.name"), ("js", "result"),
             ("regex", "@XPath://item")],
        )

    def test_source_rule_program_cache_is_complete_and_context_keyed(self):
        plain = compile_rule_program("name<js>result</js>")
        self.assertIs(plain, compile_rule_program("name<js>result</js>"))
        json_program = compile_rule_program(
            "name<js>result</js>", is_json=True)
        regex_program = compile_rule_program(
            "name<js>result</js>", is_regex=True)
        self.assertEqual(plain.stages[0].mode, "default")
        self.assertEqual(json_program.stages[0].mode, "json")
        self.assertEqual(regex_program.stages[0].mode, "regex")
        self.assertIsNot(plain, json_program)
        self.assertIsNot(json_program, regex_program)

    def test_regex_make_up_rule_keeps_group_zero_literal(self):
        self.assertEqual(
            extract_value(["whole", "first"], "$0-$1-$2", "json"),
            "$0-first-",
        )
        self.assertEqual(
            extract_value(("whole", "first"), "$1", "json"), "$1")

    async def test_get_prefix_is_passed_to_following_javascript_stage(self):
        engine = ReaderEngine()
        engine.js_variables["images"] = "a.jpg\nb.jpg"
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        value = engine._field_value(
            {}, "@get:{images}@js:result.split('\\n').join('|')",
            "json", source, source.bookSourceUrl)
        self.assertEqual(value, "a.jpg|b.jpg")

    async def test_post_js_stage_keeps_compiled_mode_instead_of_sniffing_output(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        root = BeautifulSoup("<div>initial</div>", "html.parser")
        self.assertEqual(
            engine._field_value(
                root, "<js>({foo:'bar'})</js>foo", "html", source,
                source.bookSourceUrl),
            "",
        )
        self.assertEqual(
            engine._field_value(
                root, "<js>({foo:'bar'})</js>@Json:$.foo", "html", source,
                source.bookSourceUrl),
            "bar",
        )
        self.assertEqual(
            engine._field_value(
                root, "<js>({foo:'bar'})</js><js>result.foo</js>", "html",
                source, source.bookSourceUrl),
            "bar",
        )
        self.assertEqual(
            engine._field_value(
                root, "div@text<js>({foo:'bar'})</js>foo", "html", source,
                source.bookSourceUrl),
            "",
        )

    async def test_java_get_string_native_object_uses_only_first_source_rule(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        try:
            value = runtime.evaluate(
                "@js:var o={foo:'bar'};java.put('key','foo');["
                "java.getString(\"foo<js>result+'!'</js>\",o),"
                "java.getString('@Json:$.foo',o),"
                "java.getStringList(\"foo<js>result+'!'</js>\",o).join('|'),"
                "java.getString('@get:{key}',o),"
                "java.getString('prefix@get:{key}',o),"
                "java.getString('prefix@get:{key}##prefix##X',o),"
                "java.getString('{{result.foo}}',o),"
                "java.getString('key-{{result.foo}}',o),"
                "java.getString('n={{1+1}}',o)]",
                JsContext(source=source, result="", base_url="https://x"))
        finally:
            runtime.close()
        self.assertEqual(
            value,
            ["bar", "", "bar", "bar", "prefixfoo", "Xfoo", "",
             "key-bar", "n=2"],
        )

    async def test_get_inside_javascript_is_interpolated_before_execution(self):
        engine = ReaderEngine()
        engine.js_variables["id"] = "42"
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        value = engine._field_value(
            {}, "@js:'book-'+'@get:{id}'", "json", source,
            source.bookSourceUrl)
        self.assertEqual(value, "book-42")

    async def test_inline_javascript_and_nested_rule_templates(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        node = {"id": 7}
        self.assertEqual(
            engine._field_value(node, "https://x/{{$.id}}/{{result.id+1}}", "json",
                                source, "", page=2),
            "https://x/7/8",
        )
        self.assertEqual(
            engine._field_value(node, '{{java.put("saved","novel")}}', "json",
                                source, "", key="novel"),
            "novel",
        )
        self.assertEqual(engine.js_variables["saved"], "novel")
        self.assertEqual(
            engine._field_value(node, '@js:java.timeFormat({{$.id}},"ss")', "json",
                                source, ""),
            datetime.fromtimestamp(0.007).strftime("%Y/%m/%d %H:%M"),
        )
        self.assertEqual(
            engine._field_value(
                {}, '<js>JSON.stringify({items:[{name:"A"}]})</js> $.items[0].name',
                "json", source, ""),
            "A",
        )
        with self.assertRaises(json.JSONDecodeError):
            engine._field_value(
                {}, '<js>"<div><a href=\\"/x\\">X</a></div>"</js> a@href',
                "json", source, "https://example.com/base")
        html = BeautifulSoup('<div class="btn"><a href="/download">D</a></div>',
                             "html.parser")
        self.assertEqual(
            engine._field_value(
                html, '@js:java.getElements(".btn a").attr("href")',
                "html", source, ""),
            "/download",
        )
        self.assertEqual(
            engine._field_value(
                {}, '<js>"chapter(12)"</js>##^.*?\\((\\d+)\\).*$##第$1页',
                "json", source, ""),
            "第12页",
        )
        self.assertEqual(
            engine._field_value(
                BeautifulSoup("<p>A：B</p>", "html.parser"),
                'p@text<js>##：##,</js>', "html", source, ""),
            "A,B",
        )
        self.assertEqual(
            engine._field_value(
                {}, '@js:"A-B"##(A)-(B)##$2$1', "json", source, ""),
            "BA",
        )

    async def test_field_rule_runs_multiple_js_and_selector_stages(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        root = BeautifulSoup("<p>A</p>", "html.parser")
        self.assertEqual(engine._field_value(
            root, 'p@text<js>result+"B"</js><js>result+"C"</js>',
            "html", source, ""), "ABC")
        self.assertEqual(engine._field_value(
            root,
            'p@text<js>JSON.stringify({data:{name:result+"B"}})</js>'
            '$.data.name<js>result+"C"</js>',
            "html", source, ""), "ABC")
        self.assertEqual(engine._field_value(
            root, 'p@text<js>##A##X</js><js>result+"Y"</js>',
            "html", source, ""), "XY")
        self.assertEqual(engine._field_value(
            {"name": "A"}, '{{$.name}} B<js>result.replace("B","C")</js>'
            '<js>result+"!"</js>', "json", source, ""), "A C!")
        self.assertEqual(engine._field_value(
            {"id": 123}, '<js>String(result).match(/id=(\\d+)/)[1]</js>',
            "json", source, ""), "123")

    async def test_search_list_pipeline_keeps_all_rule_stages(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_rule_pipeline(
            '{"items":[{"name":"A"},{"name":"B"}]}', "application/json",
            '<js>result</js>$.items[*]<js>result.filter(x=>x.name==="B")</js>'
            '$.[*]', source, "https://x", "B", 1)
        self.assertEqual(kind, "json")
        self.assertEqual(nodes, [{"name": "B"}])

    async def test_search_list_pipeline_keeps_precompiled_mode_after_js(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_rule_pipeline(
            "<p>initial</p>", "text/html",
            "<js>({items:[{name:'A'}]})</js>items", source, "https://x")
        self.assertEqual((nodes, kind), ([], "html"))

        nodes, kind = await engine._list_rule_pipeline(
            "<p>initial</p>", "text/html",
            "<js>({items:[{name:'A'}]})</js>@Json:$.items[*]",
            source, "https://x")
        self.assertEqual((nodes, kind), ([{"name": "A"}], "json"))

        with self.assertRaises(json.JSONDecodeError):
            await engine._list_rule_pipeline(
                '{"seed":1}', "application/json",
                '<js>"<a href=\\"/x\\">X</a>"</js>a', source, "https://x")

    async def test_empty_declared_json_list_is_empty_not_jsoup_error(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_rule_pipeline(
            "", "application/json", "$.body.list[*]", source, "https://x")
        self.assertEqual((nodes, kind), ([], "json"))

    async def test_search_selector_js_receives_jsoup_nodes(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_rule_pipeline(
            '<ul><li><a href="/42">Book</a></li></ul>', "text/html",
            "li@js:result.toArray().map(o => ({name:o.select('a').text(), "
            "bookUrl:o.select('a').attr('href')}))",
            source, "https://x")
        self.assertEqual(kind, "json")
        self.assertEqual(nodes, [{"name": "Book", "bookUrl": "/42"}])

    async def test_leading_js_get_elements_returns_html_nodes(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_rule_pipeline(
            '<ul><li><a href="/42">Book</a></li></ul>', "text/html",
            "@js:java.getElements('li')", source, "https://x")
        self.assertEqual(kind, "html")
        self.assertEqual(nodes[0].select_one("a").get_text(strip=True), "Book")


class ExploreCategoryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _rank_source() -> BookSource:
        """爱丽丝书屋's rank rules, which parse two different list layouts."""
        return BookSource(
            bookSourceName="爱丽丝书屋",
            bookSourceUrl="https://alice.example/",
            exploreUrl=json.dumps([{
                "title": "总排行",
                "url": "https://alice.example/other/rank_hits/order/hits.html",
            }], ensure_ascii=False),
            ruleExplore={
                "author": ".four@text",
                "bookList": "div.rec_rullist > ul, li:has(span.s2 > a)",
                "bookUrl": ".two a@href||.s2 a@href",
                "kind": ".sev a@text||.fenleibt@text",
                "lastChapter": ".three a@text",
                "name": ".two a@text||.s2 a@text##^\\[[^]]+\\]\\s*",
                "wordCount": ".five@text",
            },
        )

    async def test_alice_rank_both_observed_list_layouts_are_parsed(self):
        source = self._rank_source()
        engine = LegadoReadingEngine()
        compact_html = '''
        <ul class="rank-list">
          <li><span class="s2"><a href="/novel/1.html">
            <span class="fenleibt">[系统]</span> 第一部
          </a></span><span class="five">12.3万</span></li>
          <li><span class="s2"><a href="/novel/2.html">
            <span class="fenleibt">[都市]</span> 第二部
          </a></span><span class="five">8万</span></li>
        </ul>'''

        table_html = '''
        <div class="clearfix rec_rullist"><ul>
          <li class="sev"><span><a>系统小说</a></span></li>
          <li class="two"><a href="/novel/3.html">第三部</a></li>
          <li class="three"><a href="/book/3.html">第十章</a></li>
          <li class="four">作者甲</li><li class="five">22万</li>
        </ul></div>'''

        responses = iter((compact_html, table_html))

        async def request(_request, _source):
            return next(responses), "text/html", source.bookSourceUrl + "rank.html"

        engine._request = request
        try:
            categories = await resolve_explore_categories(source, engine.js)
            compact_books = await engine.explore(source, categories[0], 1)
            table_books = await engine.explore(source, categories[0], 1)
        finally:
            await engine.aclose()

        self.assertEqual([book.name for book in compact_books], ["第一部", "第二部"])
        self.assertEqual(compact_books[0].url, source.bookSourceUrl + "novel/1.html")
        self.assertEqual([book.name for book in table_books], ["第三部"])
        self.assertEqual(table_books[0].author, "作者甲")
        self.assertEqual(table_books[0].last_chapter, "第十章")
        self.assertEqual(table_books[0].url, source.bookSourceUrl + "novel/3.html")

    def test_static_formats_and_group_headers(self):
        source = SimpleNamespace(
            exploreUrl='[{"title":"分组","url":""},'
                       '{"title":"玄幻","url":"/x/{{page}}"}]')
        self.assertEqual(
            [(item.title, item.url) for item in parse_explore_categories(source)],
            [("玄幻", "/x/{{page}}")],
        )

    async def test_javascript_generated_categories(self):
        source = SimpleNamespace(
            exploreUrl='@js:[{title:"排行",url:"/rank/{{page}}"}]',
            bookSourceUrl="https://example.com",
            to_dict=lambda: {"bookSourceUrl": "https://example.com"},
        )
        categories = await resolve_explore_categories(source)
        self.assertEqual([(item.title, item.url) for item in categories],
                         [("排行", "/rank/{{page}}")])

    async def test_info_map_uses_select_default(self):
        source = SimpleNamespace(
            exploreUrl='''@js:
                const list = [{title:"选择榜单",type:"select",
                    chars:["热门榜","完结榜"],default:"完结榜"}];
                const selected = String(infoMap["选择榜单"]);
                list.push({title:selected,url:selected === "完结榜" ? "/full" : "/hot"});
                infoMap.save();
                JSON.stringify(list);''',
            bookSourceUrl="https://example.com", jsLib="",
            to_dict=lambda: {"bookSourceUrl": "https://example.com", "jsLib": ""},
        )
        categories = await resolve_explore_categories(source)
        self.assertEqual([(item.title, item.url) for item in categories],
                         [("完结榜", "/full")])

    async def test_source_js_lib_is_loaded_before_explore_script(self):
        source = SimpleNamespace(
            exploreUrl='@js:makeCategories()', bookSourceUrl="https://example.com",
            jsLib='function makeCategories(){return [{title:"分类",url:"/list"}]}',
            to_dict=lambda: {"bookSourceUrl": "https://example.com",
                             "jsLib": 'function makeCategories(){return [{title:"分类",url:"/list"}]}'},
        )
        categories = await resolve_explore_categories(source)
        self.assertEqual([(item.title, item.url) for item in categories],
                         [("分类", "/list")])


    async def test_categories_use_persistent_source_variable_runtime(self):
        with TemporaryDirectory() as root:
            source_data = {"bookSourceUrl": "https://example.com"}
            source = SimpleNamespace(
                exploreUrl='@js:JSON.parse(source.getVariable())',
                bookSourceUrl="https://example.com", jsLib="",
                to_dict=lambda: source_data)
            writer = LegadoJsRuntime(state_dir=root)
            writer.evaluate(
                'source.setVariable(\'[{"title":"Saved","url":"/saved"}]\')',
                JsContext(source=source))
            writer.close()
            runtime = LegadoJsRuntime(state_dir=root)
            try:
                categories = await resolve_explore_categories(source, runtime)
            finally:
                runtime.close()
            self.assertEqual([(item.title, item.url) for item in categories],
                             [("Saved", "/saved")])


class BookshelfPersistenceTests(unittest.TestCase):
    def test_bookshelf_is_stored_outside_resource_directory(self):
        with TemporaryDirectory() as root:
            resource_dir = Path(root) / "resource"
            data_dir = Path(root) / "user-data"
            resource_dir.mkdir()
            service = SourceService(resource_dir, data_dir=data_dir)
            source = SimpleNamespace(bookSourceUrl="https://source", bookSourceName="书源")
            book = BookItem(name="测试书", url="https://source/book/1")

            self.assertTrue(service.add_to_bookshelf(source, book))
            self.assertTrue((data_dir / "bookshelf.json").exists())
            self.assertFalse((resource_dir / "bookshelf.json").exists())

            restored = SourceService(resource_dir, data_dir=data_dir)
            restored.load_bookshelf()
            self.assertEqual(restored.bookshelf[0]["name"], "测试书")


    def test_bookshelf_rule_state_and_reading_position_are_persisted(self):
        with TemporaryDirectory() as root:
            resource_dir = Path(root) / "resource"
            data_dir = Path(root) / "user-data"
            resource_dir.mkdir()
            service = SourceService(resource_dir, data_dir=data_dir)
            source = SimpleNamespace(bookSourceUrl="https://source", bookSourceName="source")
            book = BookItem(name="book", url="https://source/book")
            service.add_to_bookshelf(source, book)
            chapter = ChapterItem("chapter", "https://source/chapter", index=7)
            self.assertTrue(service.update_bookshelf_state(
                source.bookSourceUrl, book.url, {"token": "saved"}, chapter))
            restored = SourceService(resource_dir, data_dir=data_dir)
            restored.load_bookshelf()
            self.assertEqual(restored.bookshelf[0]["variables"], {"token": "saved"})
            self.assertEqual(restored.bookshelf[0]["reading"]["chapter_index"], 7)


class ReadingPaginationTests(unittest.IsolatedAsyncioTestCase):

    async def test_bare_href_reads_current_chapter_node_attribute(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<ul><li><a href="/chapter/1">One</a></li>'
                    '<li><a href="/chapter/2">Two</a></li></ul>',
                    "text/html", "https://source.example/book")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example", ruleBookInfo={},
            ruleToc={"chapterList": "li@tag.a", "chapterName": "text",
                     "chapterUrl": "href"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"})
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source.example/book"))
        self.assertEqual(
            [(item.name, item.url) for item in chapters],
            [("One", "https://source.example/chapter/1"),
             ("Two", "https://source.example/chapter/2")])

    async def test_empty_saved_toc_url_retries_detail_page_once(self):
        requested = []

        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                requested.append(value)
                if value.endswith("/1.html"):
                    return ("<main>Chapter content</main>", "text/html", value)
                return (
                    '<ul class="clearfix"><li><a href="/book/1.html">'
                    'Chapter 1</a></li><li><a href="/book/2.html">'
                    'Chapter 2</a></li></ul>', "text/html", value)

        source = SimpleNamespace(
            bookSourceUrl="https://source.example", ruleBookInfo={},
            ruleToc={"chapterList": ".clearfix@li@tag.a",
                     "chapterName": "text", "chapterUrl": "href"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"})
        book = BookItem("Book", url="https://source.example/book/index.html",
                        toc_url="https://source.example/book/1.html")
        chapters = await Engine().chapters(source, book)
        self.assertEqual(requested, ["https://source.example/book/1.html",
                                     "https://source.example/book/index.html"])
        self.assertEqual([item.name for item in chapters],
                         ["Chapter 1", "Chapter 2"])
    async def test_virtual_data_url_decodes_without_network_request(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(bookSourceUrl="https://example.com")
        value = "data:bookId;base64,MTIzNDU=,{\"type\":\"ywc\"}"

        body, content_type, final_url = await engine._request_value(value, source)

        self.assertEqual(body, "3132333435")
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertEqual(final_url, value)
        await engine.aclose()

    async def test_strict_json_detail_field_runs_inline_templates(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        value = engine._strict_value(
            {"site": "Fantasy", "status": "50"},
            '标签：{{$.site}}｜{{java.getString("$.status")=="50"?"完结":"连载"}}',
            "json", source, "https://source")
        self.assertEqual(value, "标签：Fantasy｜完结")

    async def test_inline_single_at_template_executes_nested_html_rule(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        root = BeautifulSoup(
            '<ul class="meta"><li>A</li><li>B</li></ul>'
            '<p class="intro">Story</p>', "html.parser")
        value = engine._strict_value(
            root, '信息：{{@.meta li.1@text}}｜{{@.intro@text}}',
            "html", source, "https://source")
        self.assertEqual(value, "信息：B｜Story")

    async def test_strict_templated_relative_url_is_resolved(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://api.source", to_dict=lambda: {})
        value = engine._strict_value(
            {"id": 42}, "/chapter?id={{$.id}}", "json", source,
            "https://api.source/book/1", is_url=True)
        self.assertEqual(value, "https://api.source/chapter?id=42")

    async def test_html_field_js_receives_jsoup_element(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        node = BeautifulSoup(
            '<li><img class="locked"><a>Chapter</a></li>',
            "html.parser").li
        value = engine._field_value(
            node, '@js:result.select("img").hasClass("locked")',
            "html", source, "https://source")
        self.assertEqual(value, "True")

    async def test_html_selector_post_js_receives_extracted_string(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        node = BeautifulSoup(
            '<a href="/chapter/42">Chapter</a>', "html.parser")
        value = engine._field_value(
            node, 'a@href<js>result.match(/(\\d+)$/)[1]</js>',
            "html", source, "https://source")
        self.assertEqual(value, "42")

    async def test_field_javascript_null_result_becomes_empty_string(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        self.assertEqual(
            engine._field_value(
                {}, '@js:var missing;missing', "json", source,
                "https://source"),
            "",
        )

    async def test_get_interpolation_inside_at_js_continues_execution(self):
        engine = LegadoReadingEngine()
        engine.js_variables["cid"] = "42"
        source = SimpleNamespace(
            bookSourceUrl="https://source", to_dict=lambda: {})
        value = engine._field_value(
            {}, '@js:var id="@get:{cid}";"/chapter/"+id',
            "json", source, "https://source")
        self.assertEqual(value, "https://source/chapter/42")

    async def test_next_page_values_accept_python_style_js_array_string(self):
        engine = LegadoReadingEngine()
        engine._field_value = lambda *args, **kwargs: "['/p2', '/p3']"
        source = SimpleNamespace(to_dict=lambda: {})
        self.assertEqual(
            engine._next_page_values(
                {}, "<js>pages</js>", "json", source,
                "https://source.example/toc/1"),
            ["https://source.example/p2", "https://source.example/p3"],
        )

    async def test_regex_only_chapter_url_does_not_fall_back_to_anchor(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<ul><li><a href="/chapter-42">Chapter 42</a></li></ul>',
                    "text/html", "https://source.example/book/1")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleBookInfo={},
            ruleToc={
                "chapterList": "li",
                "chapterName": "a@text",
                "chapterUrl":
                    '##href="(https://source.example/chapter-\\d+?)"##$1,'
                    '{"webView": true}###',
            },
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source.example/book/1"))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].url, "https://source.example/book/1")

    async def test_fragment_chapter_url_resolves_against_detail_page(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<div class="album"><a href="#download">Download</a></div>',
                    "text/html", "https://source.example/book/1")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleBookInfo={},
            ruleToc={"chapterList": ".album", "chapterName": "a@text",
                     "chapterUrl": "a@href"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source.example/book/1"))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(
            chapters[0].url, "https://source.example/book/1#download")

    async def test_bare_relative_chapter_url_resolves_against_current_page(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<ul><li><a href="356532.html">Chapter</a></li></ul>',
                    "text/html", "https://source.example/book/356531.html")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example", ruleBookInfo={},
            ruleToc={"chapterList": "li", "chapterName": "a@text",
                     "chapterUrl": "a@href"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source.example/book/356531.html"))
        self.assertEqual(
            chapters[0].url, "https://source.example/book/356532.html")

    async def test_javascript_chapter_href_falls_back_to_current_page(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<body><a href="javascript:void(0);">Details</a></body>',
                    "text/html", "https://source.example/entry")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example", ruleBookInfo={},
            ruleToc={"chapterList": "body", "chapterName": '@js:"Details"'},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Entry", url="https://source.example/entry"))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].url, "https://source.example/entry")

    async def test_javascript_next_page_url_is_ignored(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {})
        root = BeautifulSoup(
            '<a id="next" href="javascript:;">Next</a>', "html.parser")
        self.assertEqual(
            engine._next_page_values(
                root, "#next@href", "html", source,
                "https://source.example/chapter/1"),
            [],
        )

    async def test_javascript_toc_url_falls_back_to_detail_page(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<a class="toc" href="javascript:;">TOC</a>'
                    '<ul><li><a href="/chapter/1">Chapter 1</a></li></ul>',
                    "text/html", "https://source.example/book/1")

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleBookInfo={"tocUrl": "a.toc@href"},
            ruleToc={"chapterList": "li", "chapterName": "a@text",
                     "chapterUrl": "a@href"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source.example/book/1"))
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].url, "https://source.example/chapter/1")

    async def test_intentional_page_named_toc_entries_are_preserved(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return ('{"items":[{"title":"第001页",'
                        '"url":"https://source/reviews?page=1"}]}',
                        "application/json", value)

        source = SimpleNamespace(
            bookSourceUrl="https://source",
            ruleBookInfo={},
            ruleToc={"chapterList": "$.items[*]", "chapterName": "$.title",
                     "chapterUrl": "$.url"},
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        chapters = await Engine().chapters(
            source, BookItem("Book", url="https://source/book"))
        self.assertEqual([(item.name, item.url) for item in chapters], [
            ("第001页", "https://source/reviews?page=1")])

    async def test_chapters_update_book_total_chapter_num(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return ('{"items":[{"name":"One","url":"/1"},'
                        '{"name":"Two","url":"/2"}]}',
                        "application/json", value)

        source = SimpleNamespace(
            bookSourceUrl="https://source", ruleBookInfo={},
            ruleToc={"chapterList": "$.items[*]", "chapterName": "$.name",
                     "chapterUrl": "$.url"},
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        book = BookItem("Book", url="https://source/book")
        chapters = await Engine().chapters(source, book)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(book.total_chapter_num, 2)

    async def test_toc_format_js_runs_after_indexing_with_shared_g_int(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (
                    '<ul><li><a href="/1">One</a></li>'
                    '<li><a href="/2">Two</a></li></ul>',
                    "text/html", "https://source.example/toc",
                )

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleBookInfo={},
            ruleToc={
                "chapterList": "li",
                "chapterName": "a@text",
                "chapterUrl": "a@href",
                "formatJs": 'if(typeof java!=="undefined"||typeof source!=="undefined"'
                            '||typeof book!=="undefined"||typeof result!=="undefined")'
                            'throw "leaked";gInt++;title+"-"+index+"-"+gInt',
            },
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        chapters = await Engine().chapters(
            source, BookItem(name="Book", url="https://source.example/book"))

        self.assertEqual([chapter.name for chapter in chapters],
                         ["One-1-1", "Two-2-2"])

    async def test_detail_init_runs_complete_stage_pipeline(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://x",
            ruleBookInfo={"init":
                '<js>JSON.stringify({items:[{name:"A"},{name:"B"}]})</js>'
                '$.items[1]<js>JSON.stringify({name:result[0].name+"!"})</js>'},
            to_dict=lambda: {})
        root, kind, raw = await engine._initialize_detail(
            source, BookItem("book"), "ignored", "text/html", "https://x/book")
        self.assertEqual(kind, "json")
        self.assertEqual(json.loads(root), {"name": "B!"})
        self.assertEqual(json.loads(raw), {"name": "B!"})

    async def test_detail_init_preserves_elements_collection_and_null_failure(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://x", ruleBookInfo={"init": "section"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        root, kind, raw = await engine._initialize_detail(
            source, BookItem("book"),
            "<section><b>A</b></section><section><b>B</b></section>",
            "text/html", "https://x/book")
        self.assertEqual(kind, "html")
        self.assertEqual(len(root), 2)
        self.assertEqual(engine._strict_value(
            root, "b@text", kind, source, "https://x/book",
            raw_result=raw, collect_all=True), "A\nB")

        source.ruleBookInfo["init"] = "<js>null</js>"
        with self.assertRaisesRegex(AssertionError, "Content cannot be null"):
            await engine._initialize_detail(
                source, BookItem("book"), "body", "text/plain", "https://x/book")

    async def test_book_info_and_toc_reuse_search_cached_html(self):
        class NoNetworkEngine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                raise AssertionError("cached HTML must avoid network")

        engine = NoNetworkEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://x", bookSourceType=0,
            ruleBookInfo={"name": "h1@text", "tocUrl": ""},
            ruleToc={"chapterList": "li", "chapterName": "a@text",
                     "chapterUrl": "a@href"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        body = '<h1>Cached</h1><li><a href="/one">One</a></li>'
        book = BookItem("", url="https://x/book", info_html=body)
        await engine.book_info(source, book)
        self.assertEqual((book.name, book.type, book.toc_url, book.toc_html),
                         ("Cached", 8, "https://x/book", body))
        chapters = await engine.chapters(source, book)
        self.assertEqual([(item.name, item.url) for item in chapters],
                         [("One", "https://x/one")])

    async def test_content_need_save_title_catalog_and_no_notice_heuristic(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                return ("<h1>Updated</h1><p>本章未完，请点击下一页继续阅读</p>",
                        "text/html", "https://x/chapter")

        source = SimpleNamespace(
            bookSourceUrl="https://x",
            ruleContent={"title": "h1@text", "content": "p@text"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        with TemporaryDirectory() as directory:
            engine = Engine(state_dir=directory)
            book = BookItem("Book", url="https://x/book")
            chapter = ChapterItem("Old", "https://x/chapter", index=0,
                                  book_url=book.url)
            engine.current_book = book
            engine.current_chapters = [chapter]
            document = await engine.content_document(
                source, chapter, need_save=False)
            cache = ReadingCache(directory)
            self.assertIn("本章未完", document.plain_text)
            self.assertIsNone(cache.load_content(
                source.bookSourceUrl, book.url, chapter.url))
            self.assertEqual(cache.load_catalog(
                source.bookSourceUrl, book.url)[0].name, "Updated")

            await engine.content_document(source, chapter, need_save=True)
            self.assertIsNotNone(cache.load_content(
                source.bookSourceUrl, book.url, chapter.url))

    async def test_put_rule_scopes_to_chapter_not_the_engine_bag(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, url, *args, **kwargs):
                page = "1" if url.endswith("/one") else "2"
                return (f"<p>body{page}</p><b id='cid'>value{page}</b>",
                        "text/html", url)

        source = SimpleNamespace(
            bookSourceUrl="https://x",
            ruleContent={"content": "@put:{token:#cid@text}p@text##body",
                         "nextContentUrl": ""},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        engine = Engine()
        book = BookItem("Book", url="https://x/book")
        engine.current_book = book
        first = ChapterItem("One", "https://x/one", index=0, book_url=book.url)
        second = ChapterItem("Two", "https://x/two", index=1, book_url=book.url)
        engine.current_chapters = [first, second]
        await engine.content_document(source, first, need_save=False)
        # AnalyzeRule.put routes to chapter ?: book ?: ruleData; a chapter
        # variable must not leak into the engine bag or the next chapter.
        self.assertEqual(first.variables["token"], "value1")
        self.assertNotIn("token", engine.js_variables)
        self.assertNotIn("token", second.variables)
        self.assertEqual(engine._get_variable("token", book, first), "value1")
        self.assertEqual(engine._get_variable("token", book, second), "")
        await engine.content_document(source, second, need_save=False)
        self.assertEqual(second.variables["token"], "value2")
        self.assertEqual(first.variables["token"], "value1")

    async def test_subcontent_appends_unformatted_value_after_pages(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                return ("<h1>One</h1><div id='sub'><p>A</p><p>B</p></div>",
                        "text/html", "https://x/chapter")

        source = SimpleNamespace(
            bookSourceUrl="https://x",
            ruleContent={"content": "h1@text", "subContent": "#sub@html"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        chapter = ChapterItem("Chapter", "https://x/chapter")
        document = await Engine().content_document(source, chapter,
                                                   need_save=False)
        # BookContent adds subContent raw (no HtmlFormatter) after the
        # formatted page list, so the joined text keeps paragraph markup.
        self.assertEqual(
            document.raw_content,
            "One\n<div id=\"sub\">\n <p>A</p>\n <p>B</p>\n</div>")

    async def test_empty_content_rule_and_synthetic_volume_bypass_request(self):
        class NoNetworkEngine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                raise AssertionError("WebBook bypass must not request")

        source = SimpleNamespace(ruleContent={}, to_dict=lambda: {})
        chapter = ChapterItem("Chapter", "https://x/chapter")
        self.assertEqual((await NoNetworkEngine().content_document(
            source, chapter)).plain_text, "https://x/chapter")

        source.ruleContent = {"content": "body@text"}
        volume = ChapterItem("Volume", "Volume0", is_volume=True,
                             tag="volume introduction")
        self.assertEqual((await NoNetworkEngine().content_document(
            source, volume)).plain_text, "volume introduction")

    async def test_plain_content_api_keeps_fixed_img_markup(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, *args, **kwargs):
                return ('<p>Text</p><img src="/cover.jpg">', "text/html",
                        "https://x/chapter")

        source = SimpleNamespace(
            bookSourceUrl="https://x", ruleContent={"content": "body@html"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        value = await Engine().content(
            source, ChapterItem("Chapter", "https://x/chapter"))
        self.assertIn('<img src="https://x/cover.jpg">', value)

    async def test_web_file_detail_requires_download_url_list(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleBookInfo={"downloadUrls": "a@href"})
        root = BeautifulSoup("<p>none</p>", "html.parser")
        with self.assertRaisesRegex(RuntimeError, "下载链接为空"):
            engine._apply_book_info(
                source, BookItem("File", type=136), root, "html", str(root),
                "https://x/file", "https://x/file")

    async def test_list_rule_runs_js_selector_js_pipeline(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_nodes(
            '{"items":[{"name":"A"},{"name":"B"}]}', "application/json",
            '<js>result</js>$.items[*]<js>result.map(x=>({name:x.name+"!"}))</js>'
            '$.[*]', source, "https://x")
        self.assertEqual(kind, "json")
        self.assertEqual(nodes, [{"name": "A!"}, {"name": "B!"}])

    async def test_reading_list_pipeline_keeps_precompiled_mode_after_js(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_nodes(
            "<p>initial</p>", "text/html",
            "<js>({items:[{name:'A'}]})</js>items", source, "https://x")
        self.assertEqual((nodes, kind), ([], "html"))
        nodes, kind = await engine._list_nodes(
            "<p>initial</p>", "text/html",
            "<js>({items:[{name:'A'}]})</js>@Json:$.items[*]",
            source, "https://x")
        self.assertEqual((nodes, kind), ([{"name": "A"}], "json"))
        with self.assertRaises(json.JSONDecodeError):
            await engine._list_nodes(
                '{"seed":1}', "application/json",
                '<js>"<a href=\\"/x\\">X</a>"</js>a', source, "https://x")

    async def test_reading_rule_outer_layers_do_not_swallow_selector_errors(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        root = BeautifulSoup("<div>x</div>", "html.parser")
        # A lone "[invalid" never reaches the selector engine - getResultLast
        # reads it as an attribute name - so it resolves to "" like Legado.
        self.assertEqual(
            engine._strict_value(root, "[invalid", "html", source, "https://x"), "")
        with self.assertRaises(Exception):
            engine._strict_value(root, "[invalid@text", "html", source, "https://x")
        with self.assertRaises(Exception):
            await engine._list_nodes(
                "<div>x</div>", "text/html", "[invalid", source, "https://x")

    async def test_list_post_selector_js_keeps_original_src(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_nodes(
            '{"marker":"original","items":[{"name":"A"}]}',
            "application/json",
            '$.items[*]@js:result.map(x=>({name:x.name,marker:JSON.parse(src).marker}))',
            source, "https://x")
        self.assertEqual(kind, "json")
        self.assertEqual(nodes, [{"name": "A", "marker": "original"}])

    async def test_html_list_post_js_has_elements_sort_and_select(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_nodes(
            '<div class="row" data-x="2"><a>B</a></div>'
            '<div class="row" data-x="1"><a>A</a></div>',
            "text/html",
            '.row@js:result.sort((a,b)=>a.attr("data-x")-b.attr("data-x"));'
            'result.select("a")',
            source, "https://x")
        self.assertEqual(kind, "html")
        self.assertEqual([node.get_text(strip=True) for node in nodes], ["A", "B"])

    async def test_book_info_loads_detail_without_fetching_toc(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                self.calls.append(value)
                return ("<h1>Detail name</h1><span class='author'>Writer</span>"
                        "<p class='intro'>Full introduction</p>",
                        "text/html", value)

        source = SimpleNamespace(
            bookSourceUrl="https://source",
            ruleBookInfo={"canReName": "true", "name": "h1@text",
                          "author": ".author@text", "intro": ".intro@text"},
            to_dict=lambda: {},
        )
        book = BookItem("Search name", url="https://source/book/1")
        engine = Engine()
        result = await engine.book_info(source, book)
        self.assertIs(result, book)
        self.assertEqual(engine.calls, ["https://source/book/1"])
        self.assertEqual(book.name, "Detail name")
        self.assertEqual(book.author, "Writer")
        self.assertEqual(book.intro, "Full introduction")

    async def test_search_keeps_javascript_action_book_urls_like_fixed_booklist(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                return (
                    '<ul><li><a href="javascript:;">Ad</a></li>'
                    '<li><a href="book/1.html">Book</a></li></ul>',
                    "text/html", "https://source.example/search")

        source = BookSource(
            bookSourceUrl="https://source.example",
            searchUrl="/search?q={{key}}",
            ruleSearch={"bookList": "li", "name": "a@text",
                        "bookUrl": "a@href"},
        )
        books = await Engine().search(source, "Book")
        self.assertEqual([(book.name, book.url) for book in books], [
            ("Ad", "javascript:;"),
            ("Book", "https://source.example/book/1.html")])

    async def test_search_book_fields_use_fixed_bookhelp_formatting(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                return (
                    '<article><a href="/b">Novel　 作者 Someone</a>'
                    '<i>作 者： Alice 著</i><div class="intro"><p>A&nbsp; B</p><b>C</b></div>'
                    '<span class="kind">Fantasy</span><span class="words">12500</span>'
                    '<span class="last">Chapter 8</span></article>',
                    "text/html", "https://source.example/search")

        source = BookSource(
            bookSourceUrl="https://source.example", bookSourceName="Origin",
            searchUrl="/search", ruleSearch={
                "bookList": "article", "name": "a@text", "author": "i@text",
                "intro": ".intro@html", "bookUrl": "a@href",
                "kind": ".kind@text", "wordCount": ".words@text",
                "lastChapter": ".last@text"})
        book = (await Engine().search(source, "Novel"))[0]
        self.assertEqual(
            (book.name, book.author, book.intro, book.kind, book.word_count,
             book.latest_chapter_title, book.origin, book.origin_name),
            ("Novel　", "Alice", "　　A\xa0 B\n　　C", "Fantasy", "1.2万字",
             "Chapter 8", "https://source.example", "Origin"))

    async def test_search_empty_list_falls_back_to_detail_rules(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                return ('<h1>Detail</h1><a class="toc" href="/toc">Toc</a>',
                        "text/html", "https://source.example/book/1")

        source = BookSource(
            bookSourceUrl="https://source.example", searchUrl="/book/1",
            ruleSearch={"bookList": ".missing", "name": ".name@text",
                        "bookUrl": "a@href"},
            ruleBookInfo={"name": "h1@text", "tocUrl": "a.toc@href"})
        books = await Engine().search(source, "Detail")
        self.assertEqual(len(books), 1)
        self.assertEqual((books[0].name, books[0].url, books[0].toc_url,
                          books[0].info_html.startswith("<h1>")),
                         ("Detail", "https://source.example/book/1",
                          "https://source.example/toc", True))

    async def test_search_matching_book_url_pattern_parses_detail_directly(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                return ('<h1>Direct</h1><ul><li>wrong list</li></ul>',
                        "text/html", "https://source.example/book/9")

        source = BookSource(
            bookSourceUrl="https://source.example", searchUrl="/book/9",
            bookUrlPattern=r"https://source\.example/book/\d+",
            ruleSearch={"bookList": "li", "name": "li@text", "bookUrl": ""},
            ruleBookInfo={"name": "h1@text"})
        books = await Engine().search(source, "Direct")
        self.assertEqual([(book.name, book.url) for book in books],
                         [("Direct", "https://source.example/book/9")])

    async def test_content_title_and_image_style_are_preserved(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return ("<h1>Resolved title</h1><main>Text</main>",
                        "text/html", value)

        source = SimpleNamespace(
            ruleContent={"title": "h1@text", "content": "main@text",
                         "imageStyle": "FULL"},
            to_dict=lambda: {},
        )
        chapter = ChapterItem("fallback", "https://source/chapter")
        document = await Engine().content_document(source, chapter)
        self.assertEqual(document.title, "Resolved title")
        self.assertEqual(chapter.name, "Resolved title")
        self.assertEqual(document.image_style, "FULL")
        self.assertEqual(document.plain_text, "Text")

    async def test_toc_pre_update_js_can_replace_book_toc_url(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                self.calls.append(value)
                if value == "https://source/book":
                    return '<a class="toc" href="/old-toc">toc</a>', "text/html", value
                return '<ul><li><a href="/c1">Chapter 1</a></li></ul>', "text/html", value

        engine = Engine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", ruleBookInfo={"tocUrl": "a.toc@href"},
            ruleToc={"preUpdateJs": 'book.tocUrl=book.tocUrl.replace("old","new")',
                     "chapterList": "li", "chapterName": "a@text",
                     "chapterUrl": "a@href"},
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        chapters = await engine.chapters(source, BookItem("Book", url="https://source/book"))
        self.assertEqual(engine.calls[:2],
                         ["https://source/book", "https://source/new-toc"])
        self.assertEqual(chapters[0].name, "Chapter 1")

    async def test_book_info_rules_update_complete_book_metadata(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleBookInfo={
            "name": "h1@text", "canReName": "true", "author": ".author@text",
            "intro": ".intro@text", "coverUrl": "img@src", "kind": ".kind@text",
            "lastChapter": ".last@text", "wordCount": ".words@text",
            "downloadUrls": "a.download@href"})
        root = BeautifulSoup(
            '<h1>New name</h1><b class="author">Alice</b><p class="intro">Intro</p>'
            '<img src="/cover.jpg"><i class="kind">Fantasy</i>'
            '<em class="last">Chapter 9</em><span class="words">10K</span>'
            '<a class="download" href="/book.txt">TXT</a>', "html.parser")
        book = BookItem("Old name", url="https://source/book", type=136)
        engine._apply_book_info(source, book, root, "html", str(root),
                                "https://source/book")
        self.assertEqual(
            (book.name, book.author, book.intro, book.cover_url, book.kind,
             book.last_chapter, book.word_count, book.download_urls),
            ("New name", "Alice", "Intro", "https://source/cover.jpg", "Fantasy",
             "Chapter 9", "10K", ["https://source/book.txt"]))

    async def test_book_info_nonblank_can_rename_and_formats_fields(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleBookInfo={
            "name": "h1@text", "canReName": "false", "author": ".author@text",
            "intro": ".intro@html", "wordCount": ".words@text"})
        root = BeautifulSoup(
            '<h1>Title 作者 ignored</h1><b class="author">作者: Bob 著</b>'
            '<div class="intro"><p>A&nbsp;B</p><i>C</i></div>'
            '<span class="words">20000</span>', "html.parser")
        book = BookItem("Old", author="Old author")
        engine._apply_book_info(source, book, root, "html", str(root),
                                "https://source/book")
        self.assertEqual((book.name, book.author, book.intro, book.word_count),
                         ("Title", "Bob", "　　A\xa0B\n　　C", "2万字"))

    async def test_existing_book_name_still_executes_name_rule_put_side_effect(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleBookInfo={
            "name": "$.title@put:{cid:$.id}"})
        book = BookItem("Search name", url="https://source/book")
        engine._apply_book_info(
            source, book, {"title": "Detail name", "id": 42}, "json",
            '{"title":"Detail name","id":42}', "https://source/book")
        self.assertEqual(book.name, "Search name")
        # AnalyzeRule.put stores into chapter ?: book: detail parsing has no
        # chapter, so the side effect belongs to the book entity, not the bag.
        self.assertEqual(book.variables["cid"], "42")
        self.assertNotIn("cid", engine.js_variables)

    async def test_desktop_browser_response_imports_cookies(self):
        browser_cookies = iter(("token=verified", ""))
        async def browser(url, source):
            return {"body": "<main>ok</main>", "url": "https://verify.other/final",
                    "headers": {"content-type": "text/html; charset=utf-8"},
                    "status": 200, "cookie": next(browser_cookies)}

        engine = LegadoReadingEngine(webview_fetcher=browser)
        source = SimpleNamespace(bookSourceUrl="https://source.example", to_dict=lambda: {})
        engine.js.set_cookie(source.bookSourceUrl, "old=stale")
        value = await engine._fetch_browser_response("https://source.example/check", source)
        self.assertEqual(value, ("<main>ok</main>", "text/html", "https://verify.other/final"))
        self.assertEqual(engine.js.export_cookie_header(source.bookSourceUrl),
                         "token=verified")
        self.assertEqual(engine.js.export_cookie_header("https://verify.other/final"), "")
        await engine._fetch_browser_response("https://source.example/check", source)
        self.assertEqual(engine.js.export_cookie_header(source.bookSourceUrl), "")

    def test_url_option_webview_uses_kotlin_when_not_python_truthiness(self):
        use = LegadoReadingEngine._url_option_uses_webview
        self.assertEqual(
            [use(value) for value in (None, "", False, "false", 0, "False",
                                      True, "true", [], {})],
            [False, False, False, False, True, True, True, True, True, True],
        )

    async def test_post_webview_runs_http_first_then_login_check(self):
        events = []

        async def browser(url, _source, options):
            events.append(("browser", url, options.get("html")))
            return {"body": "rendered", "url": url + "/web",
                    "headers": {"content-type": "text/html"}}

        class Engine(LegadoReadingEngine):
            async def _request_transport(self, request, source=None):
                events.append(("http", request["method"], request.get("body")))
                self.last_response = {
                    "body": "posted", "bodyBytes": list(b"posted"), "headers": {},
                    "status": 200, "url": "https://source/redirect"}
                return "posted", "text/html", "https://source/redirect"

        source = SimpleNamespace(
            bookSourceUrl="https://source", enabledCookieJar=False,
            loginCheckJs="result", to_dict=lambda: {
                "bookSourceUrl": "https://source", "enabledCookieJar": False})
        value = await Engine(webview_fetcher=browser)._request({
            "url": "https://source/start", "method": "POST", "body": "q=x",
            "webView": True}, source)
        self.assertEqual(events, [
            ("http", "POST", "q=x"),
            ("browser", "https://source/redirect", "posted")])
        self.assertEqual(value, ("rendered", "text/html",
                                 "https://source/redirect/web"))

    async def test_login_ui_schema_and_action_share_login_url_scope(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            loginUi='[{name:"Account",type:"text"},'
                    '{name:"Sign in",type:"button",action:"Login()"}]',
            loginUrl='function Login(){source.putLoginInfo(result.Account);'
                     'return "ok:"+result.Account}',
            to_dict=lambda: {
                "bookSourceUrl": "https://source",
                "loginUi": '[{name:"Account",type:"text"},'
                           '{name:"Sign in",type:"button",action:"Login()"}]',
                "loginUrl": 'function Login(){source.putLoginInfo(result.Account);'
                            'return "ok:"+result.Account}',
            },
        )
        schema = await engine.login_schema(source)
        self.assertEqual([item["type"] for item in schema], ["text", "button"])
        result = await engine.execute_login_action(
            source, schema[1]["action"], {"Account": "alice"})
        self.assertEqual(result, "ok:alice")
        saved = engine.js.evaluate(
            "@js:source.getLoginInfo()",
            JsContext(source=source, variables=engine.js_variables),
        )
        self.assertEqual(saved, "alice")

    async def test_login_submit_persists_fields_before_invoking_login(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source", loginUi='[{name:"Account",type:"text"}]',
            loginUrl=(
                'function login(){source.putLoginHeader('
                'JSON.stringify({Account:source.getLoginInfoMap().Account}))}'
            ),
            to_dict=lambda: {
                "bookSourceUrl": "https://source",
                "loginUi": '[{name:"Account",type:"text"}]',
                "loginUrl": (
                    'function login(){source.putLoginHeader('
                    'JSON.stringify({Account:source.getLoginInfoMap().Account}))}')},
        )
        await engine.submit_login(source, {"Account": "alice"})
        schema = await engine.login_schema(source)
        header = engine.js.evaluate(
            "@js:source.getLoginHeader()",
            JsContext(source=source, variables=engine.js_variables))
        self.assertEqual(schema[0]["value"], "alice")
        self.assertEqual(json.loads(header), {"Account": "alice"})

    async def test_login_target_accepts_relative_and_json_url_but_not_notes(self):
        relative = SimpleNamespace(bookSourceUrl="https://source/base", loginUrl="/login")
        configured = SimpleNamespace(
            bookSourceUrl="https://source", loginUrl='{"url":"https://auth/login"}')
        note = SimpleNamespace(bookSourceUrl="https://source", loginUrl="login in toc rule")
        self.assertEqual(LegadoReadingEngine.login_target(relative),
                         "https://source/login")
        self.assertEqual(LegadoReadingEngine.login_target(configured),
                         "https://auth/login")
        self.assertEqual(LegadoReadingEngine.login_target(note), "")

    async def test_cover_decode_receives_bytes_and_returns_data_uri(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                self.last_response = {"bodyBytes": [1, 2, 3], "url": request["url"]}
                return "", "application/octet-stream", request["url"]

        engine = Engine()
        source = SimpleNamespace(
            coverDecodeJs="result.concat([4])",
            to_dict=lambda: {"bookSourceUrl": "https://source"},
            header="",
        )
        value = await engine.decode_cover(source, "https://source/cover.bin")
        self.assertEqual(value, "data:image/jpeg;base64,AQIDBA==")

    async def test_cover_decode_rejects_non_byte_results_and_runs_on_empty_body(self):
        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                self.last_response = {"bodyBytes": [], "url": request["url"]}
                return "", "application/octet-stream", request["url"]

        engine = Engine()
        source = SimpleNamespace(
            coverDecodeJs='"AQID"',
            to_dict=lambda: {"bookSourceUrl": "https://source"}, header="",
        )
        self.assertEqual(await engine.decode_cover(
            source, "https://source/cover.bin"), "")
        source.coverDecodeJs = "throw new Error('decode failed')"
        self.assertEqual(await engine.decode_cover(
            source, "https://source/cover.bin"), "")
        source.coverDecodeJs = "[9]"
        self.assertEqual(await engine.decode_cover(
            source, "https://source/cover.bin"),
            "data:image/jpeg;base64,CQ==")

    async def test_media_fetch_applies_inline_options_then_block_headers(self):
        requests = []

        class Engine(LegadoReadingEngine):
            async def _request(self, request, source=None):
                requests.append(request)
                self.last_response = {"bodyBytes": [7, 8]}
                return "", "image/jpeg", request["url"]

        payload = await Engine().fetch_media(
            'https://img/1.jpg,{"headers":{"Referer":"https://source/"}}',
            SimpleNamespace(bookSourceUrl="https://source"),
            extra_headers={"Referer": "https://block/"})
        self.assertEqual(b"\x07\x08", payload)
        self.assertEqual("https://img/1.jpg", requests[0]["url"])
        self.assertEqual("https://block/", requests[0]["headers"]["Referer"])

    async def test_verification_code_is_available_while_resolving_url(self):
        async def get_code(image, source):
            self.assertEqual(image, "captcha-image")
            return "4321"

        engine = LegadoReadingEngine(verification_fetcher=get_code)
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            searchUrl='@js:"/verify?code=" + java.getVerificationCode("captcha-image")',
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        value = await engine._resolve_rule_url(source.searchUrl, source)
        self.assertEqual(value, "/verify?code=4321")

    async def test_dynamic_request_result_expands_returned_page_template(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            searchUrl=(
                '@js:"/search,{\\\"body\\\":{\\\"page\\\":{{page}}},'
                '\\\"method\\\":\\\"POST\\\"}"'),
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        value = await engine._resolve_rule_url(
            source.searchUrl, source, key="book", page=7)
        self.assertEqual(
            value, '/search,{"body":{"page":7},"method":"POST"}')

    async def test_pay_action_uses_current_book_and_chapter(self):
        engine = LegadoReadingEngine()
        engine.current_book = BookItem("book", "https://source/book")
        chapter = ChapterItem("chapter", "https://source/chapter")
        source = SimpleNamespace(
            ruleContent={"payAction": "book.name + ':' + chapter.title"},
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        self.assertEqual(await engine.execute_pay_action(source, chapter), "book:chapter")

    async def test_json_content_js_runs_before_json_field_heuristic(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        value = engine._strict_value(
            {"items": ["a", "b"]},
            '@js:JSON.parse(result).items.map(x => `<p>${x}</p>`).join("")',
            "json", source, "https://x", raw_result='{"items":["a","b"]}',
        )
        self.assertEqual(value, "<p>a</p><p>b</p>")

    async def test_strict_value_applies_regex_suffix(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        root = BeautifulSoup('<span class="author">作者：张三</span>', "html.parser")
        self.assertEqual(engine._strict_value(
            root, r'.author@text##^作者：##', "html", source, "https://x"),
            "张三")
        self.assertEqual(engine._strict_value(
            root, r'##.*作者：([^<]+).*##$1', "html", source, "https://x",
            raw_result='<p>作者：李四</p>'), "李四")

    async def test_all_field_and_content_regex_paths_use_java_syntax(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        root = BeautifulSoup("<p>unused</p>", "html.parser")
        self.assertEqual(engine._strict_value(
            root, r"##(?<first>book)\Rend\z##${first}",
            "html", source, "https://x", raw_result="book\r\nend"), "book")
        self.assertEqual(
            await engine._replace_content_stages(
                "book\r\nend", r"(?<first>book)\Rend\z##${first}",
                source, ChapterItem("chapter", "https://x/1")),
            "book")

    async def test_content_rule_collects_all_matching_paragraphs(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleContent={})
        root = BeautifulSoup(
            '<div class="read_chapterDetail"><p>book title</p>'
            '<p>first paragraph</p><p>second paragraph</p></div>',
            "html.parser",
        )

        value = engine._strict_value(
            root, ".read_chapterDetail p@all", "html", source,
            "https://example.com/1.html", collect_all=True,
        )

        self.assertIn("book title", value)
        self.assertIn("first paragraph", value)
        self.assertIn("second paragraph", value)

    async def test_strict_json_fields_keep_jayway_java_string_forms(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleContent={})
        data = {"enabled": True, "item": {"id": 1}, "missing": None}
        self.assertEqual(engine._strict_value(
            data, "$.enabled", "json", source, ""), "true")
        self.assertEqual(engine._strict_value(
            data, "$.item", "json", source, ""), "{id=1}")
        self.assertEqual(engine._strict_value(
            data, "$.missing", "json", source, ""), "")

    async def test_content_pages_keep_order_and_stop_at_next_chapter(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                self.calls.append(value)
                pages = {
                    "https://x/c1": '<main>A</main><a class="page" href="/c1_2">2</a>'
                                    '<a class="page" href="/c1_3">3</a>'
                                    '<a class="page" href="/c2">next chapter</a>',
                    "https://x/c1_2": "<main>B</main>",
                    "https://x/c1_3": "<main>C</main>",
                }
                return pages[value], "text/html", value

        source = SimpleNamespace(
            ruleContent={"content": "main@text", "nextContentUrl": "a.page@href"},
            to_dict=lambda: {},
        )
        engine = Engine()
        document = await engine.content_document(
            source, ChapterItem("one", "https://x/c1"), "https://x/c2")
        self.assertEqual(engine.calls, ["https://x/c1", "https://x/c1_2", "https://x/c1_3"])
        self.assertEqual(document.plain_text, "A\nB\nC")

    async def test_content_pagination_is_not_truncated_after_twenty_pages(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                number = int(str(value).rsplit("/", 1)[-1])
                next_url = (f"https://source.example/page/{number + 1}"
                            if number < 22 else "")
                return (json.dumps({"content": f"Page {number}",
                                    "next": next_url}),
                        "application/json", str(value))

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleContent={"content": "$.content", "nextContentUrl": "$.next"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"},
        )
        document = await Engine().content_document(
            source, ChapterItem(name="Chapter",
                                url="https://source.example/page/1"))

        self.assertIn("Page 1", document.plain_text)
        self.assertIn("Page 22", document.plain_text)

    async def test_multiple_content_pages_are_concurrent_ordered_and_not_recursive(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                number = int(str(value).rsplit("/", 1)[-1])
                self.calls.append(number)
                if number > 1:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    await asyncio.sleep(0.02)
                    self.active -= 1
                next_value = ["https://x/2", "https://x/3"] if number == 1 else (
                    ["https://x/4"] if number == 2 else [])
                return (json.dumps({"content": f"P{number}", "next": next_value}),
                        "application/json", str(value))

        source = SimpleNamespace(
            ruleContent={"content": "$.content", "nextContentUrl": "$.next"},
            to_dict=lambda: {},
        )
        engine = Engine()
        document = await engine.content_document(
            source, ChapterItem("chapter", "https://x/1"))
        self.assertEqual(document.plain_text, "P1\nP2\nP3")
        self.assertEqual(engine.calls, [1, 2, 3])
        self.assertEqual(engine.max_active, 2)

    async def test_multiple_toc_pages_are_concurrent_ordered_and_not_recursive(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                self.calls.append(str(value))
                if str(value).endswith("detail"):
                    body = {"toc": "https://x/1"}
                else:
                    number = int(str(value).rsplit("/", 1)[-1])
                    if number > 1:
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                        await asyncio.sleep(0.02)
                        self.active -= 1
                    next_value = ["https://x/2", "https://x/3"] if number == 1 else (
                        ["https://x/4"] if number == 2 else [])
                    body = {"chapters": [{"name": f"C{number}",
                                           "url": f"https://x/c{number}"}],
                            "next": next_value}
                return json.dumps(body), "application/json", str(value)

        source = SimpleNamespace(
            bookSourceUrl="https://x", ruleBookInfo={"tocUrl": "$.toc"},
            ruleToc={"chapterList": "$.chapters[*]", "chapterName": "$.name",
                     "chapterUrl": "$.url", "nextTocUrl": "$.next"},
            to_dict=lambda: {},
        )
        engine = Engine()
        chapters = await engine.chapters(
            source, BookItem("book", url="https://x/detail"))
        self.assertEqual([item.name for item in chapters], ["C1", "C2", "C3"])
        self.assertNotIn("https://x/4", engine.calls)
        self.assertEqual(engine.max_active, 2)

    async def test_toc_reverse_dedup_and_book_state_match_historical_order(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                body = {"items": [
                    {"name": "A-first", "url": "/same"},
                    {"name": "B", "url": "/b"},
                    {"name": "A-last", "url": "/same"},
                ]}
                return json.dumps(body), "application/json", "https://x/toc"

        async def names(prefix, reverse_toc):
            source = SimpleNamespace(
                bookSourceUrl="https://x",
                ruleBookInfo={},
                ruleToc={"chapterList": prefix + "$.items[*]",
                         "chapterName": "$.name", "chapterUrl": "$.url"},
                to_dict=lambda: {},
            )
            book = BookItem("book", url="https://x/toc",
                            read_config={"reverseToc": reverse_toc})
            chapters = await Engine().chapters(source, book)
            self.assertEqual(book.latest_chapter_title, chapters[-1].name)
            self.assertEqual(book.dur_chapter_title, chapters[0].name)
            self.assertEqual(book.last_check_count, 2)
            self.assertGreater(book.last_check_time, 0)
            return [item.name for item in chapters]

        self.assertEqual(await names("", False), ["B", "A-last"])
        self.assertEqual(await names("-", False), ["B", "A-first"])
        self.assertEqual(await names("", True), ["A-last", "B"])
        self.assertEqual(await names("-", True), ["A-first", "B"])

    async def test_volume_without_url_uses_title_and_page_index(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                body = {"items": [{"name": "Volume", "volume": True}]}
                return json.dumps(body), "application/json", "https://x/toc"

        source = SimpleNamespace(
            bookSourceUrl="https://x", ruleBookInfo={},
            ruleToc={"chapterList": "$.items[*]", "chapterName": "$.name",
                     "chapterUrl": "$.url", "isVolume": "$.volume"},
            to_dict=lambda: {},
        )
        chapters = await Engine().chapters(
            source, BookItem("book", url="https://x/toc"))
        self.assertEqual(chapters[0].url, "Volume0")
        self.assertEqual(chapters[0].base_url, "https://x/toc")

    async def test_empty_toc_and_non_volume_content_raise_historical_errors(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return "{}", "application/json", str(value)

        toc_source = SimpleNamespace(
            bookSourceUrl="https://x", ruleBookInfo={},
            ruleToc={"chapterList": "$.items[*]"}, to_dict=lambda: {})
        with self.assertRaises(TocEmptyError):
            await Engine().chapters(
                toc_source, BookItem("book", url="https://x/toc"))

        content_source = SimpleNamespace(
            ruleContent={"content": "$.content"}, to_dict=lambda: {})
        with self.assertRaises(ContentEmptyError):
            await Engine().content_document(
                content_source, ChapterItem("chapter", "https://x/1"))
        volume = await Engine().content_document(
            content_source, ChapterItem("volume", "https://x/1", is_volume=True))
        self.assertEqual(volume.plain_text, "")

    async def test_image_decode_failure_returns_empty_like_image_utils_null(self):
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def raise_for_status(self): return None
            async def read(self): return b"encrypted"

        class Session:
            closed = False
            def __init__(self, *_args, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            def get(self, *_args, **_kwargs): return Response()

        source = SimpleNamespace(
            bookSourceUrl="https://x", header="",
            ruleContent={"imageDecode": "throw new Error('bad')"},
            to_dict=lambda: {"bookSourceUrl": "https://x"})
        document = parse_content_blocks(
            '<img src="https://x/encrypted.jpg">', "https://x/")
        engine = LegadoReadingEngine()
        with patch("app.services.reading_engine.aiohttp.ClientSession", Session), \
                patch("app.services.reading_engine.aiohttp.TCPConnector",
                      lambda **_kwargs: object()):
            await engine._load_protected_images(document, source)
        self.assertEqual(document.blocks[0].value, b"")

    async def test_replace_regex_runs_after_content_pages_are_combined(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                pages = {
                    "https://x/1": '<main>left</main><a href="/2">next</a>',
                    "https://x/2": '<main>right</main>',
                }
                return pages[value], "text/html", value

        source = SimpleNamespace(
            ruleContent={"content": "main@text", "nextContentUrl": "a@href",
                         "replaceRegex": "left\\s+right##combined"},
            to_dict=lambda: {},
        )
        document = await Engine().content_document(
            source, ChapterItem("chapter", "https://x/1"))
        self.assertEqual(document.plain_text, "combined")

    def test_invalid_replace_regex_falls_back_to_literal_replacement(self):
        self.assertEqual(_apply_replace_regex(r"a\b\c", "##\\"), "abc")

    async def test_content_delays_html_unescape_until_after_structure_detection(self):
        class Engine(LegadoReadingEngine):
            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                return (json.dumps({"content":
                                    "正文 &lt;b&gt;不是标签&lt;/b&gt; &amp; 结束"}),
                        "application/json", str(value))

        source = SimpleNamespace(
            bookSourceUrl="https://source.example",
            ruleContent={"content": "$.content"},
            to_dict=lambda: {"bookSourceUrl": "https://source.example"})
        document = await Engine().content_document(
            source, ChapterItem("chapter", "https://source.example/1"))
        self.assertEqual(document.plain_text, "正文 <b>不是标签</b> & 结束")

    async def test_content_does_not_guess_pagination_without_next_rule(self):
        class Engine(LegadoReadingEngine):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _request_value(self, value, source, base_url="", page=1,
                                     request_defaults=None):
                self.calls.append(str(value))
                return ('<main>正文</main><a href="/2">下一页</a>',
                        "text/html", str(value))

        source = SimpleNamespace(
            ruleContent={"content": "main@text"}, to_dict=lambda: {})
        engine = Engine()
        document = await engine.content_document(
            source, ChapterItem("chapter", "https://source.example/1"))
        self.assertEqual(document.plain_text, "正文")
        self.assertEqual(engine.calls, ["https://source.example/1"])

    async def test_replace_regex_runs_every_regex_and_js_stage(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, ruleContent={})
        chapter = ChapterItem("chapter", "https://x/1")
        value = await engine._replace_content_stages(
            "A dirty", '<js>result+" B"</js>##dirty##clean'
            '<js>result.replace("B","C")</js><js>##^A##X</js>',
            source, chapter)
        self.assertEqual(value, "X clean C")

    async def test_replace_regex_expands_chapter_title_template(self):
        # SourceRule.makeUpRule expands {{ }} before splitting on "##", and
        # the JS body runs with the current content as `result`.  A source
        # that writes ruleContent.replaceRegex = "##{{chapter.title}}" must
        # therefore delete every copy of the chapter title.
        engine = LegadoReadingEngine()
        engine.current_book = BookItem("book", "https://x/book")
        source = SimpleNamespace(to_dict=lambda: {}, ruleContent={})
        chapter = ChapterItem("第1章 出海", "https://x/1")
        value = await engine._replace_content_stages(
            "第1章 出海\n正文\n第1章 出海", "##{{chapter.title}}",
            source, chapter)
        self.assertEqual(value, "正文")


if __name__ == "__main__":
    unittest.main()
