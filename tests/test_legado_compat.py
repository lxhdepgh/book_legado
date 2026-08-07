import asyncio
import json
import gzip
import hmac
import threading
import time
from io import BytesIO
from email.message import Message
from urllib.error import HTTPError
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bs4 import BeautifulSoup
from PIL import Image

from app.models.book_source import BookSource
from app.services.legado_rule_parser import evaluate
from app.services.compat_audit import audit_source_corpus
from app.services.desktop_browser import EdgeCdpSession
from app.services.legado_js import JsBridgeError, JsContext, LegadoJsRuntime
from app.services.request_compat import escape_form, escape_url_query, legado_escape
from app.services.reading_engine import (LegadoReadingEngine, _legado_truthy,
                                         parse_content_blocks)
from app.services.source_service import SourceService
from app.services.rule_engine import (BookItem, ChapterItem, ReaderEngine,
                                      parse_explore_categories,
                                      resolve_explore_categories, select_nodes)
from app.services.rule_engine import extract_value
from test_book_sources_async import classify_js_bridge_error, parse_search_url


class RuleParserTests(unittest.TestCase):
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

    def test_jsoup_at_chain_treats_terminal_html_tag_as_selector(self):
        root = BeautifulSoup(
            '<ul><li><a href="/1">One</a></li><li>Two</li></ul>',
            "html.parser")
        nodes = evaluate(root, "ul@li", "html")
        self.assertEqual([node.get_text(strip=True) for node in nodes],
                         ["One", "Two"])
        self.assertEqual(evaluate(root, "ul@li@a@href", "html"), ["/1"])

    def test_standalone_jsoup_value_rule_uses_current_element(self):
        node = BeautifulSoup(
            '<li><a href="/1">One</a></li>', "html.parser").li
        self.assertEqual(evaluate(node, "text", "html"), ["One"])
        self.assertEqual(
            evaluate(node, "html", "html"),
            ['<li><a href="/1">One</a></li>'],
        )

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

    def test_charset_escape_matches_legado_utf16_parameter_encoding(self):
        self.assertEqual(legado_escape("书😀 a"), "%u4E66%uD83D%uDE00%20a")
        self.assertEqual(escape_form("q=书 源&empty="),
                         "q=%u4E66%20%u6E90&empty=")
        self.assertEqual(
            escape_url_query("https://source/search?q=书 源&page=1"),
            "https://source/search?q=%u4E66%20%u6E90&page=1")

    def test_fallback_rule_short_circuits_after_first_nonempty_branch(self):
        soup = BeautifulSoup('<p class="ok">value</p>', "html.parser")
        values = evaluate(soup, '.ok@text||[invalid', "html")
        self.assertEqual(values, ["value"])

    def test_combination_operator_precedence_matches_legado(self):
        soup = BeautifulSoup('<i>A</i><b>C</b>', "html.parser")
        values = evaluate(soup, 'i@text&&em@text||b@text', "html")
        self.assertEqual(values, ["A", "C"])

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

    def test_complete_source_corpus_loads_and_javascript_compiles(self):
        audit = audit_source_corpus(Path("resource/bookSource.json"))
        self.assertEqual(audit.source_count, 1127)
        self.assertEqual(audit.model_count, audit.source_count)
        self.assertGreater(audit.script_count, 1400)
        self.assertGreater(audit.rule_count, 24900)
        self.assertGreater(audit.regex_count, 450)
        self.assertEqual(audit.syntax_issues, [])
        self.assertEqual(audit.rule_syntax_issues, [])
        self.assertEqual(audit.unknown_rule_fields, [])
        self.assertEqual(audit.unsupported_java_apis, [])
        self.assertEqual(audit.unsupported_object_apis, [])
        self.assertEqual(audit.unsupported_jsoup_pseudos, [])

    def test_book_source_preserves_review_rules_for_forward_compatibility(self):
        source = BookSource.from_dict({
            "bookSourceName": "source", "bookSourceUrl": "https://source",
            "ruleReview": {"reviewList": ".review", "content": ".text@text"},
        })
        self.assertEqual(source.ruleReview["reviewList"], ".review")
        self.assertEqual(source.to_dict()["ruleReview"]["content"], ".text@text")

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

    def test_empty_login_maps_keep_java_map_methods(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:[source.getLoginInfoMap().get("token"),'
            'source.getLoginHeaderMap().containsKey("Authorization")]',
            JsContext(source=source))
        self.assertEqual(value, [None, False])

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
            "status": 200, "url": "https://final.example/",
        })
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:var r=java.connect("/x");'
            '[String(r),r.body(),r.header("etag"),r.code(),r.raw().request().url()]',
            JsContext(source=source),
        )
        self.assertEqual(value, ["ok", "ok", "v1", 200, "https://final.example/"])

    def test_java_get_disables_redirect_following_like_jsoup(self):
        runtime = LegadoJsRuntime()
        captured = []
        runtime._request_json = lambda request, *_args: (
            captured.append(request) or json.dumps({
                "body": "", "headers": {"location": "/result?id=42"},
                "status": 302, "url": "https://source/search"}))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.get("https://source/search", {accept:"text/html"})'
            '.header("location")', JsContext(source=source))
        request = json.loads(captured[0].split(",", 1)[1])
        self.assertEqual(value, "/result?id=42")
        self.assertIs(request["followRedirects"], False)
        self.assertEqual(request["headers"]["accept"], "text/html")

    def test_java_post_disables_redirect_following_like_legado_js_extensions(self):
        runtime = LegadoJsRuntime()
        captured = []
        runtime._request_json = lambda request, *_args: (
            captured.append(request) or json.dumps({
                "body": "", "headers": {"location": "/result?id=43"},
                "status": 302, "url": "https://source/search"}))
        source = SimpleNamespace(
            to_dict=lambda: {"bookSourceUrl": "https://source"})

        value = runtime.evaluate(
            '@js:java.post("https://source/search", "q=book", {})'
            '.header("location")', JsContext(source=source))

        request = json.loads(captured[0].split(",", 1)[1])
        self.assertEqual(value, "/result?id=43")
        self.assertIs(request["followRedirects"], False)
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
        value = runtime.evaluate('@js:java.getResponse().body()', JsContext(
            source=source, base_url="https://source/current"))
        self.assertEqual(value, "fetched")
        self.assertEqual(requested, ["https://source/current"])

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
        self.assertEqual(captured, {"X-Source": "enabled"})

    def test_http_response_cookie_map_and_java_collection_views(self):
        runtime = LegadoJsRuntime()
        runtime._request_json = lambda _request, *_: json.dumps({
            "body": "", "headers": {"set-cookie": "sid=abc; Path=/"},
            "status": 200, "url": "https://source/"})
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '''@js:
            var cookies=java.head("https://source").cookies();
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
            var parsed=java.toURL('../book?id=7');
            var before=[source.getLoginHeaderMap().get('X-Test'),
              source.getLoginInfoMap().get('name'),parsed.host,parsed.pathname,
              parsed.searchParams.get('id'),result];
            source.removeLoginHeader();source.removeLoginInfo();
            cache.putMemory('temporary','value');cache.deleteMemory('temporary');
            before.concat([source.getLoginHeader(),source.getLoginInfo(),cache.getFromMemory('temporary')]);
        """, JsContext(source=source, result="payload", base_url=source.bookSourceUrl))
        self.assertEqual(value, ["yes", "reader", "example.com", "/book", "7",
                                 "payload", "", "", None])

    def test_documented_chapter_and_tts_script_globals(self):
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        chapter = SimpleNamespace(name="Chapter One")
        value = LegadoJsRuntime().evaluate(
            "[title,nextChapterUrl,speakText,speakSpeed]",
            JsContext(source=source, chapter=chapter,
                      next_chapter_url="https://source/chapter-2",
                      speak_text="hello", speak_speed=12))
        self.assertEqual(value, ["Chapter One", "https://source/chapter-2", "hello", 12])

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
                                 "https://x/a%20b?q=%E4%B9%A6"])

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
                source=source, base_url="https://source/start",
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
            '[book.getKindList(),book.getDisplayTag(),book.getDisplayCover(),'
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
            'chapter.resourceUrl="https://cdn/audio";chapter.isVolume=true;before;',
            JsContext(source=source, book=book, chapter=chapter))
        self.assertEqual(chapter_value,
                         ["old title", "chapter/1", "https://source/book/"])
        self.assertEqual(chapter.name, "new title")
        self.assertEqual(chapter.book_url, "https://source/new")
        self.assertEqual(chapter.resource_url, "https://cdn/audio")
        self.assertTrue(chapter.is_volume)

    def test_cookie_store_set_get_and_remove(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        value = runtime.evaluate(
            '@js:cookie.setCookie(baseUrl,"sid=abc; Path=/");'
            '[cookie.getCookie(baseUrl),cookie.getKey(baseUrl,"sid"),java.getCookie(baseUrl,"sid")]',
            JsContext(source=source, base_url="https://example.com/book/1"),
        )
        self.assertEqual(value, ["sid=abc", "abc", "abc"])
        self.assertTrue(runtime.evaluate(
            '@js:cookie.removeCookie(baseUrl)',
            JsContext(source=source, base_url="https://example.com/book/1"),
        ))
        self.assertEqual(runtime.export_cookie_header("https://example.com/"), "")

    def test_cookie_map_and_memory_cache_aliases(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://example.com"})
        value = runtime.evaluate(
            '@js:cache.putMemory("id",{value:3});'
            '[cache.getFromMemory("id").value,cookie.mapToCookie({a:1,b:"x"})]',
            JsContext(source=source),
        )
        self.assertEqual(value, [3, "a=1; b=x"])

    def test_font_replacement_bridge_matches_glyph_signatures(self):
        runtime = LegadoJsRuntime()
        runtime.font_store["wrong"] = ({ord("x"): ("shape-a",)}, {})
        runtime.font_store["right"] = ({}, {("shape-a",): ord("中")})
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        value = runtime.evaluate(
            '@js:java.replaceFont("x!","wrong","right")', JsContext(source=source))
        self.assertEqual(value, "中!")

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
        decoded = Image.open(BytesIO(bytes(value)))
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
        decoded = Image.open(BytesIO(bytes(value))).convert("RGBA")
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
            var bytes=java.getByteArray("https://source/data");
            var stream=java.getInputStream("https://source/data");
            [bytes,stream.available(),stream.read(),stream.readAllBytes()];
            ''', JsContext(source=source))
        self.assertEqual(value, [[65, 66, 67], 3, 65, [66, 67]])

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
            source.putLoginInfo({user:"u"});
            var response=java.startBrowserAwait("https://verify","验证");
            var values=["a","b"];
            [response.body(),response.url(),source.getVariable(),
             source.getLoginInfoMap().user,source.getHeaderMap()["X-Test"],
             values.size(),values.get(1),values.toArray().join("")];''',
            JsContext(source=source, variables={}),
        )
        self.assertEqual(value, ["<p>验证</p>", "https://verify?done=1", "saved",
                                 "u", "1", 2, "b", "ab"])

    def test_start_browser_is_non_blocking_for_media_content_rules(self):
        runtime = LegadoJsRuntime()
        calls = []
        runtime.browser_fetcher = lambda url, title: calls.append((url, title))
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            to_dict=lambda: {"bookSourceUrl": "https://source"},
        )
        value = runtime.evaluate(
            '''@js:
            var response=java.startBrowser("https://media/video.m3u8","播放");
            [response.url(),"直链:\\nhttps://media/video.m3u8"];''',
            JsContext(source=source),
        )
        self.assertEqual(value[0], "https://media/video.m3u8")
        self.assertIn("https://media/video.m3u8", value[1])
        self.assertEqual(calls, [])

        rendered = runtime.evaluate(
            "@js:java.startBrowser(result,title);"
            "'【刷新正文】播放\\n✅直链:\\n'+result",
            JsContext(source=source, result="https://media/video.m3u8",
                      title="视频"),
        )
        document = parse_content_blocks(rendered, "https://source/chapter")
        self.assertEqual(document.blocks[-1].kind, "video")
        self.assertEqual(document.blocks[-1].value,
                         "https://media/video.m3u8")

    def test_chapter_is_vip_method_uses_python_chapter_state(self):
        runtime = LegadoJsRuntime()
        source = SimpleNamespace(to_dict=lambda: {"bookSourceUrl": "https://source"})
        chapter = ChapterItem("locked", "https://source/1", is_vip=True)
        self.assertTrue(runtime.evaluate(
            "@js:chapter.isVip()", JsContext(source=source, chapter=chapter)))

    def test_source_empty_dataclass_fields_are_visible_to_javascript(self):
        runtime = LegadoJsRuntime()
        source = BookSource(bookSourceUrl="https://source")
        self.assertEqual(
            runtime.evaluate(
                "@js:[source.bookSourceComment,source.bookSourceGroup]",
                JsContext(source=source)),
            ["", ""],
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
                'cache.putFile("sort",{value:9});infoMap.choice="selected";infoMap.save()',
                JsContext(source=source, variables={}),
            )
            second = LegadoJsRuntime(state_dir=directory)
            value = second.evaluate(
                '@js:[source.getVariable(),source.getLoginHeaderMap().A,'
                'cache.getFile("sort").value,java.deviceID,infoMap.choice,'
                'java.androidId(),java.deviceID(),String(java.androidId)]',
                JsContext(source=source, variables={}),
            )
            self.assertEqual(value, ["state", "B", 9, first_id, "selected",
                                     first_id, first_id, first_id])

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
            JsContext(source=source, page=4, variables={}))
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
             java.bytesToStr(encodedBytes),
             java.aesBase64DecodeToByteArray(encoded,key,transformation,iv)];''',
            JsContext(source=source))
        self.assertEqual(value[0], "content")
        self.assertTrue(value[1])
        self.assertEqual(bytes(value[2]), b"content")

    def test_request_config_after_page_template(self):
        request = parse_search_url(
            'https://example.com/list/{{page}}.html,'
            '{"headers":{"User-Agent":"custom-agent"}}',
            "https://example.com", "", 2,
        )
        self.assertEqual(request["url"], "https://example.com/list/2.html")
        self.assertEqual(request["headers"], {"User-Agent": "custom-agent"})

    def test_text_selector_and_attribute(self):
        label = "\u67e5\u770b\u66f4\u591a\u7ae0\u8282"
        root = BeautifulSoup(f'<a href="/toc">{label}</a>', "html.parser")
        self.assertEqual(evaluate(root, f"text.{label}@href", "html"), ["/toc"])

    def test_indices_exclusions_and_negative_indices(self):
        root = BeautifulSoup(
            '<div class="x"><i>0</i><i>1</i><i>2</i><i>3</i></div>',
            "html.parser",
        )
        self.assertEqual(evaluate(root, "class.x@i.1:3@text", "html"), ["1", "2", "3"])
        self.assertEqual(evaluate(root, "class.x@i!0:2@text", "html"), ["3"])
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
            "html")
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
                         [("Book", "https://x/book/42")])

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

        await asyncio.gather(*(window_call() for _ in range(3)))
        self.assertGreaterEqual(starts[-1] - starts[0], 0.035)

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
        self.assertEqual(headers, {
            "Referer": "https://source.example/chapter/1", "X-Token": "dynamic"})

    async def test_book_list_put_variables_are_applied_per_record(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://source")
        soup = BeautifulSoup(
            '<li><a href="/1">One</a><span>Alice</span></li>'
            '<li><a href="/2">Two</a><span>Bob</span></li>', "html.parser")
        selector, put_rule = engine._list_rule_parts(
            "li@put:{author:'span@text'}")
        nodes = evaluate(soup, selector, "html")
        books = engine._books_from_nodes(
            nodes, "html", {"name": "a@text", "bookUrl": "a@href",
                            "author": "@get:{author}"},
            "https://source", source, list_put=put_rule)
        self.assertEqual([(book.name, book.author) for book in books],
                         [("One", "Alice"), ("Two", "Bob")])

    async def test_request_rule_runs_leading_js_before_plain_url(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://example.com")
        value = await engine._resolve_rule_url(
            '<js>java.put("token","ok")</js>/search?key={{key}}&page={{page}}',
            source, "book", 3,
        )
        self.assertEqual(value, "/search?key=book&page=3")
        self.assertEqual(engine.js_variables["token"], "ok")

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

    async def test_inline_javascript_and_nested_rule_templates(self):
        engine = ReaderEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        node = {"id": 7}
        self.assertEqual(
            engine._field_value(node, "https://x/{{$.id}}/{{page+1}}", "json",
                                source, "", page=2),
            "https://x/7/3",
        )
        self.assertEqual(
            engine._field_value(node, '{{key;java.put("saved",key)}}', "json",
                                source, "", key="novel"),
            "novel",
        )
        self.assertEqual(engine.js_variables["saved"], "novel")
        self.assertEqual(
            engine._field_value(node, '@js:java.timeFormat({{$.id}},"ss")', "json",
                                source, ""),
            "07",
        )
        self.assertEqual(
            engine._field_value(
                {}, '<js>JSON.stringify({items:[{name:"A"}]})</js> $.items[0].name',
                "json", source, ""),
            "A",
        )
        self.assertEqual(
            engine._field_value(
                {}, '<js>"<div><a href=\\"/x\\">X</a></div>"</js> a@href',
                "json", source, "https://example.com/base"),
            "https://example.com/x",
        )
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
    async def test_alice_rank_current_list_markup_is_parsed(self):
        source = BookSource.from_json(Path("resource/bookSource.json"))[1]
        engine = LegadoReadingEngine()
        html = '''
        <ul class="rank-list">
          <li><span class="s2"><a href="/novel/1.html">
            <span class="fenleibt">[系统]</span> 第一部
          </a></span><span class="five">12.3万</span></li>
          <li><span class="s2"><a href="/novel/2.html">
            <span class="fenleibt">[都市]</span> 第二部
          </a></span><span class="five">8万</span></li>
        </ul>'''

        async def request(_request, _source):
            return html, "text/html", source.bookSourceUrl + "rank.html"

        engine._request = request
        try:
            categories = await resolve_explore_categories(source, engine.js)
            books = await engine.explore(source, categories[0], 1)
        finally:
            engine.js.close()

        self.assertEqual([book.name for book in books], ["第一部", "第二部"])
        self.assertEqual(books[0].url, source.bookSourceUrl + "novel/1.html")

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
    async def test_virtual_data_url_decodes_without_network_request(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(bookSourceUrl="https://example.com")
        value = "data:bookId;base64,MTIzNDU=,{\"type\":\"ywc\"}"

        body, content_type, final_url = await engine._request_value(value, source)

        self.assertEqual(body, "3132333435")
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertEqual(final_url, value)
        engine.js.close()

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

    async def test_regex_only_chapter_url_falls_back_to_anchor_and_keeps_options(self):
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
        self.assertEqual(
            chapters[0].url,
            'https://source.example/chapter-42,{"webView": true}',
        )

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
            ruleToc={"chapterList": ".album", "chapterName": "a@text"},
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
        self.assertEqual(root, {"name": "B!"})
        self.assertEqual(json.loads(raw), {"name": "B!"})

    async def test_list_rule_runs_js_selector_js_pipeline(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, bookSourceUrl="https://x")
        nodes, kind = await engine._list_nodes(
            '{"items":[{"name":"A"},{"name":"B"}]}', "application/json",
            '<js>result</js>$.items[*]<js>result.map(x=>({name:x.name+"!"}))</js>'
            '$.[*]', source, "https://x")
        self.assertEqual(kind, "json")
        self.assertEqual(nodes, [{"name": "A!"}, {"name": "B!"}])

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

    async def test_search_filters_javascript_action_book_urls(self):
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
            ("Book", "https://source.example/book/1.html")])

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
        book = BookItem("Old name", url="https://source/book")
        engine._apply_book_info(source, book, root, "html", str(root),
                                "https://source/book")
        self.assertEqual(
            (book.name, book.author, book.intro, book.cover_url, book.kind,
             book.last_chapter, book.word_count, book.download_urls),
            ("New name", "Alice", "Intro", "https://source/cover.jpg", "Fantasy",
             "Chapter 9", "10K", "https://source/book.txt"))

    async def test_existing_book_name_still_executes_name_rule_put_side_effect(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(ruleBookInfo={
            "name": "$.title@put:{cid:$.id}"})
        book = BookItem("Search name", url="https://source/book")
        engine._apply_book_info(
            source, book, {"title": "Detail name", "id": 42}, "json",
            '{"title":"Detail name","id":42}', "https://source/book")
        self.assertEqual(book.name, "Search name")
        self.assertEqual(engine.js_variables["cid"], "42")

    async def test_desktop_browser_response_imports_cookies(self):
        async def browser(url, source):
            return {"body": "<main>ok</main>", "url": "https://source.example/final",
                    "headers": {"content-type": "text/html; charset=utf-8"},
                    "status": 200, "cookie": "token=verified"}

        engine = LegadoReadingEngine(webview_fetcher=browser)
        source = SimpleNamespace(bookSourceUrl="https://source.example", to_dict=lambda: {})
        value = await engine._fetch_browser_response("https://source.example/check", source)
        self.assertEqual(value, ("<main>ok</main>", "text/html", "https://source.example/final"))
        self.assertIn("token=verified", engine.js.export_cookie_header(
            "https://source.example/final"))

    async def test_edge_cookie_header_only_includes_matching_domain(self):
        cookies = [
            {"name": "a", "value": "1", "domain": ".example.com"},
            {"name": "b", "value": "2", "domain": "other.test"},
        ]
        self.assertEqual(EdgeCdpSession.cookie_header(
            cookies, "https://www.example.com/book"), "a=1")

    async def test_login_ui_schema_and_action_share_login_url_scope(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(
            bookSourceUrl="https://source",
            loginUi='[{name:"Account",type:"text"},'
                    '{name:"Sign in",type:"button",action:"Login()"}]',
            loginUrl='function Login(){source.putLoginInfo(infoMap.Account);'
                     'return "ok:"+infoMap.Account}',
            to_dict=lambda: {
                "bookSourceUrl": "https://source",
                "loginUi": '[{name:"Account",type:"text"},'
                           '{name:"Sign in",type:"button",action:"Login()"}]',
                "loginUrl": 'function Login(){source.putLoginInfo(infoMap.Account);'
                            'return "ok:"+infoMap.Account}',
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

    async def test_replace_regex_runs_every_regex_and_js_stage(self):
        engine = LegadoReadingEngine()
        source = SimpleNamespace(to_dict=lambda: {}, ruleContent={})
        chapter = ChapterItem("chapter", "https://x/1")
        value = await engine._replace_content_stages(
            "A dirty", '<js>result+" B"</js>##dirty##clean'
            '<js>result.replace("B","C")</js><js>##^A##X</js>',
            source, chapter)
        self.assertEqual(value, "X clean C")


if __name__ == "__main__":
    unittest.main()
