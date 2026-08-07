"""基于 QuickJS 的轻量 Legado JavaScript 兼容层。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import io
import gzip
import json
import re
import ssl
import time
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener


class JsRuntimeUnavailable(RuntimeError):
    pass


class JsBridgeError(RuntimeError):
    pass


@dataclass
class JsContext:
    source: object
    key: str = ""
    page: int = 1
    result: str = ""
    base_url: str = ""
    variables: dict = field(default_factory=dict)
    get_string: object = None
    get_string_list: object = None
    get_elements: object = None
    book: object = None
    chapter: object = None
    response_data: dict | None = None
    src: object = None
    request_headers: dict | None = None
    title: str = ""
    next_chapter_url: str = ""
    speak_text: str = ""
    speak_speed: int = 0


class LegadoJsRuntime:
    _PERSIST_LOCK = threading.RLock()
    # The Windows quickjs extension can access-violate when independent
    # Context objects are created/evaluated concurrently in different threads.
    # Network work remains concurrent; only native JS execution is serialized.
    _QUICKJS_LOCK = threading.RLock()

    def __init__(self, timeout: int = 15, state_dir: str | Path | None = None):
        self.timeout = timeout
        self.cookies = CookieJar()
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), HTTPSHandler(context=ssl_context))
        self.default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "Chrome/120 Mobile Safari/537.36"
            ),
        }
        self.base_url = ""
        self.cache_store = {}
        self.state_dir = Path(state_dir) if state_dir else None
        self.persistent_state = self._load_persistent_state()
        self.device_id = str(self.persistent_state.get("deviceId") or uuid.uuid4().hex)
        if self.persistent_state.get("deviceId") != self.device_id:
            self.persistent_state["deviceId"] = self.device_id
            self._save_persistent_state()
        self.font_store = {}
        self.bitmap_store = {}
        self.dom_store = {}
        self._thread_state = threading.local()
        self.browser_fetcher = None
        self.verification_code_provider = None

    @property
    def _state_path(self) -> Path | None:
        return self.state_dir / "rule_engine_state.json" if self.state_dir else None

    def _load_persistent_state(self) -> dict:
        path = self._state_path
        if path is None:
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _save_persistent_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        with self._PERSIST_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(self.persistent_state, ensure_ascii=False,
                                            separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)

    def _hydrate_source_state(self, context: JsContext, source_key: str) -> None:
        values = (self.persistent_state.get("sources", {}).get(source_key, {})
                  if source_key else {})
        for key, value in values.items():
            context.variables.setdefault(str(key), value)

    def _file_cache_get(self, key) -> str:
        if self.state_dir is None:
            return self._cache_get(key)
        path = self.state_dir / "rule-cache" / (hashlib.sha256(
            str(key).encode()).hexdigest() + ".json")
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

    def _file_cache_put(self, key, value):
        if self.state_dir is None:
            return self._cache_put(key, value)
        directory = self.state_dir / "rule-cache"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(str(key).encode()).hexdigest() + ".json")
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(str(value), encoding="utf-8")
        temporary.replace(path)
        return value

    def close(self):
        """Release references retained by urllib handlers after a JS check."""
        try:
            self.cookies.clear()
        except KeyError:
            pass
        self.opener = None

    @staticmethod
    def available() -> bool:
        try:
            import quickjs  # noqa: F401
            return True
        except ImportError:
            return False

    def evaluate_url_option(self, script: str, context: JsContext) -> dict:
        """Run UrlOption.js and return both its completion URL and header map."""
        cleaned = self._clean_script(str(script or ""))
        wrapper = (
            "@js:(function(){var __optionValue=eval(" +
            json.dumps(cleaned, ensure_ascii=False) +
            ");return JSON.stringify({url:__optionValue==null?java.url:String(__optionValue),"
            "headers:java.headerMap});})()"
        )
        value = self.evaluate(wrapper, context)
        try:
            result = json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            result = None
        return result if isinstance(result, dict) else {}

    def evaluate(self, script: str, context: JsContext):
        with self._QUICKJS_LOCK:
            return self._evaluate_locked(script, context)

    def _evaluate_locked(self, script: str, context: JsContext):
        try:
            import quickjs
        except ImportError as exc:
            raise JsRuntimeUnavailable("缺少 quickjs，请执行 pip install quickjs") from exc

        source_data = context.source.to_dict() if hasattr(context.source, "to_dict") else dict(context.source)
        raw_headers = source_data.get("header", {}) or {}
        if isinstance(raw_headers, str):
            try:
                raw_headers = json.loads(raw_headers)
            except json.JSONDecodeError:
                raw_headers = {}
        if not isinstance(raw_headers, dict):
            raw_headers = {}
        evaluation_base = context.base_url or source_data.get("bookSourceUrl", "")

        ctx = quickjs.Context()
        ctx.set_memory_limit(128 * 1024 * 1024)
        ctx.add_callable("__ajax", lambda value: (
            self._ajax(value, raw_headers, evaluation_base)))
        ctx.add_callable("__request", lambda value: (
            self._request_json(value, raw_headers, evaluation_base)))
        ctx.add_callable("__post", lambda url, body="": self._ajax(
            f'{url},{{"method":"POST","body":{json.dumps(str(body))}}}',
            raw_headers, evaluation_base))
        ctx.add_callable("__md5", lambda value: hashlib.md5(str(value).encode()).hexdigest())
        ctx.add_callable("__digest", self._digest)
        ctx.add_callable("__hmac", self._hmac)
        ctx.add_callable("__b64decode", self._base64_decode)
        ctx.add_callable("__b64encode", lambda value: base64.b64encode(str(value).encode()).decode())
        ctx.add_callable("__b64bytes", self._base64_bytes)
        ctx.add_callable("__hexbytes", self._hex_bytes)
        ctx.add_callable("__bytesstr", self._bytes_to_string)
        ctx.add_callable("__strbytes", self._string_to_bytes)
        ctx.add_callable("__uuid", lambda: str(uuid.uuid4()))
        ctx.add_callable("__time_format", self._time_format)
        ctx.add_callable("__num_chapter", self._to_num_chapter)
        ctx.add_callable("__cache_get", self._cache_get)
        ctx.add_callable("__cache_put", self._cache_put)
        ctx.add_callable("__cache_delete", self._cache_delete)
        ctx.add_callable("__file_cache_get", self._file_cache_get)
        ctx.add_callable("__file_cache_put", self._file_cache_put)
        ctx.add_callable("__get", lambda name: context.variables.get(str(name)))
        ctx.add_callable("__get_json", lambda name: json.dumps(
            context.variables.get(str(name)), ensure_ascii=False))
        ctx.add_callable("__get_string", lambda rule, content=None, is_url=False:
                         self._context_get_string(context, rule, content, is_url))
        ctx.add_callable("__get_list", lambda rule, content=None, is_url=False: json.dumps(
            self._context_get_list(context, rule, content, is_url), ensure_ascii=False))
        ctx.add_callable("__get_elements", lambda rule, content=None: json.dumps(
            self._context_get_elements(context, rule, content), ensure_ascii=False))
        ctx.add_callable("__put", lambda name, value: self._put(context, name, value))
        ctx.add_callable("__cookie_get", lambda url: self._cookie_string(str(url)))
        ctx.add_callable("__cookie_set", self.set_cookie)
        ctx.add_callable("__cookie_remove", self.remove_cookie)
        ctx.add_callable("__crypto", self._crypto)
        ctx.add_callable("__unsupported", lambda name: self._unsupported(name))
        ctx.add_callable("__css_query", self._css_query)
        ctx.add_callable("__element_op", self._element_operation)
        ctx.add_callable("__dom_parse", self._dom_parse)
        ctx.add_callable("__dom_query", self._dom_query)
        ctx.add_callable("__dom_op", self._dom_operation)
        ctx.add_callable("__dom_remove", self._dom_remove)
        ctx.add_callable("__dom_parents", self._dom_parents)
        ctx.add_callable("__dom_contains", self._dom_contains)
        ctx.add_callable("__t2s", self._traditional_to_simplified)
        ctx.add_callable("__html_format", lambda value: html.unescape(str(value)))
        ctx.add_callable("__font_query", self._query_font)
        ctx.add_callable("__font_replace", self._replace_font)
        ctx.add_callable("__gzip", self._gzip)
        ctx.add_callable("__md5bytes", lambda value: json.dumps(list(
            hashlib.md5(str(value).encode()).digest())))
        ctx.add_callable("__b64encode_bytes", self._base64_encode_bytes)
        ctx.add_callable("__bitmap_decode", self._bitmap_decode)
        ctx.add_callable("__bitmap_info", self._bitmap_info)
        ctx.add_callable("__bitmap_crop", self._bitmap_crop)
        ctx.add_callable("__bitmap_create", self._bitmap_create)
        ctx.add_callable("__bitmap_draw", self._bitmap_draw)
        ctx.add_callable("__bitmap_encode", self._bitmap_encode)
        ctx.add_callable("__browser", self._browser)
        ctx.add_callable("__verification", self._verification_code)
        ctx.add_callable("__to_url", self._to_url)

        self._thread_state.dom_store = {}
        self._hydrate_source_state(context, str(source_data.get("bookSourceUrl", "")))
        if isinstance(raw_headers, dict):
            raw_headers = dict(raw_headers)
            if isinstance(context.request_headers, dict):
                raw_headers.update({str(k): str(v)
                                    for k, v in context.request_headers.items()})
        self.base_url = context.base_url or source_data.get("bookSourceUrl", "")
        def plain(value):
            if value is None:
                return {}
            if hasattr(value, "__dataclass_fields__"):
                return {name: getattr(value, name) for name in value.__dataclass_fields__}
            if isinstance(value, dict):
                return value
            if hasattr(value, "__dict__"):
                return {str(name): item for name, item in vars(value).items()
                        if not callable(item)}
            return {}
        book_data = plain(context.book)
        chapter_data = plain(context.chapter)
        if book_data:
            aliases = {
                "bookUrl": "url", "tocUrl": "toc_url", "coverUrl": "cover_url",
                "wordCount": "word_count", "lastChapterTitle": "last_chapter",
                "originName": "origin_name", "totalChapterNum": "total_chapter_num",
                "durChapterIndex": "dur_chapter_index",
                "durChapterTitle": "dur_chapter_title", "canUpdate": "can_update",
                "customIntro": "custom_intro", "customTag": "custom_tag",
                "customCoverUrl": "custom_cover_url",
                "latestChapterTime": "latest_chapter_time",
                "lastCheckTime": "last_check_time", "lastCheckCount": "last_check_count",
                "durChapterPos": "dur_chapter_pos", "durChapterTime": "dur_chapter_time",
                "originOrder": "origin_order", "readConfig": "read_config",
                "infoHtml": "info_html", "tocHtml": "toc_html",
                "variable": "variables",
            }
            for legado_name, python_name in aliases.items():
                book_data.setdefault(legado_name, book_data.get(python_name))
            if not book_data.get("origin"):
                book_data["origin"] = source_data.get("bookSourceUrl", "")
            if not book_data.get("originName"):
                book_data["originName"] = source_data.get("bookSourceName", "")
        if chapter_data:
            chapter_data.setdefault("title", chapter_data.get("name", ""))
            chapter_data.setdefault("chapterUrl", chapter_data.get("url", ""))
            chapter_data.setdefault("baseUrl", chapter_data.get("base_url", ""))
            chapter_data.setdefault("bookUrl", chapter_data.get("book_url", ""))
            chapter_data.setdefault("resourceUrl", chapter_data.get("resource_url", ""))
            chapter_data.setdefault("isVolume", chapter_data.get("is_volume", False))
            chapter_data.setdefault("isVip", chapter_data.get("is_vip", False))
            chapter_data.setdefault("isPay", chapter_data.get("is_pay", False))
            chapter_data.setdefault("variable", chapter_data.get("variables", {}))
        if book_data:
            book_data.setdefault("bookUrl", book_data.get("url", ""))
            book_data.setdefault("tocUrl", book_data.get("toc_url", ""))
        if chapter_data:
            chapter_data.setdefault("title", chapter_data.get("name", ""))
        result_data = context.result
        if isinstance(result_data, (bytes, bytearray)):
            result_data = list(result_data)
        if not isinstance(result_data, (str, int, float, bool, list, dict, type(None))):
            result_data = str(result_data)
        result_expression = json.dumps(result_data, ensure_ascii=False)
        src_data = context.src if context.src is not None else result_data
        if isinstance(src_data, (bytes, bytearray)):
            src_data = list(src_data)
        if not isinstance(src_data, (str, int, float, bool, list, dict, type(None))):
            src_data = str(src_data)
        src_expression = json.dumps(src_data, ensure_ascii=False)
        if context.response_data is not None:
            result_expression = ("__response(" + json.dumps(
                json.dumps(context.response_data, ensure_ascii=False)) + ")")
        bootstrap = f"""
        var key = {json.dumps(context.key)};
        var page = {int(context.page)};
        var result = {result_expression};
        var src = {src_expression};
        var baseUrl = {json.dumps(context.base_url or source_data.get('bookSourceUrl', ''))};
        var title = {json.dumps(context.title or chapter_data.get('title') or chapter_data.get('name') or '')};
        var nextChapterUrl = {json.dumps(context.next_chapter_url or '')};
        var speakText = {json.dumps(context.speak_text or '')};
        var speakSpeed = {int(context.speak_speed or 0)};
        function escape(value) {{
          let output='';const safe=/[A-Za-z0-9@*_+./-]/;
          for(let i=0;i<String(value).length;i++){{
            const char=String(value).charAt(i),code=String(value).charCodeAt(i);
            output += safe.test(char) ? char : (code < 256
              ? '%' + code.toString(16).toUpperCase().padStart(2,'0')
              : '%u' + code.toString(16).toUpperCase().padStart(4,'0'));
          }}
          return output;
        }}
        function unescape(value) {{
          return String(value).replace(/%u([0-9a-fA-F]{{4}})|%([0-9a-fA-F]{{2}})/g,
            (_all,wide,byte)=>String.fromCharCode(parseInt(wide || byte,16)));
        }}
        const __sourceData = {json.dumps(source_data, ensure_ascii=False)};
        const __bookData = {json.dumps(book_data, ensure_ascii=False)};
        const __chapterData = {json.dumps(chapter_data, ensure_ascii=False)};
        var book = Object.assign({{}}, __bookData, {{
          getVariable: name => __get('book:' + String(name)) || '',
          putVariable: (name, value) => __put('book:' + String(name), value),
          getCustomVariable: name => __get('book:' + String(name)) || '',
          putCustomVariable: value => __put('book:custom', value),
          setReverseToc: value => {{ book.reverseToc = Boolean(value); return book.reverseToc; }},
          getReverseToc: () => Boolean(book.reverseToc || (book.readConfig || {{}}).reverseToc),
          getKindList: () => [book.wordCount].concat(String(book.kind || '').split(/[,\\n]/))
            .map(value=>String(value || '').trim()).filter(Boolean),
          getDisplayTag: () => book.customTag || book.kind || '',
          getDisplayCover: () => book.customCoverUrl || book.coverUrl || '',
          getDisplayIntro: () => book.customIntro || book.intro || '',
          getUnreadChapterNum: () => Math.max(Number(book.totalChapterNum || 0)-Number(book.durChapterIndex || 0)-1,0)
        }});
        var chapter = Object.assign({{}}, __chapterData, {{
          getVariable: name => __get('chapter:' + String(name)) || '',
          putVariable: (name, value) => __put('chapter:' + String(name), value),
          isVip: () => Boolean(__chapterData.isVip || __chapterData.is_vip ||
                               __chapterData.isPay || __chapterData.is_pay)
        }});
        function __map(value) {{
          const target=value && typeof value === 'object' ? value : {{}};
          if (!Object.prototype.hasOwnProperty.call(target,'get')) Object.defineProperties(target,{{
            get:{{value:function(key){{return this[String(key)]}},enumerable:false}},
            put:{{value:function(key,value){{const old=this[String(key)];this[String(key)]=value;return old}},enumerable:false}},
            containsKey:{{value:function(key){{return Object.prototype.hasOwnProperty.call(this,String(key))}},enumerable:false}},
            keySet:{{value:function(){{return Object.keys(this)}},enumerable:false}},
            values:{{value:function(){{return Object.keys(this).map(key=>this[key])}},enumerable:false}},
            entrySet:{{value:function(){{
              const entries=Object.keys(this).map(key=>String(key)+'='+String(this[key]));
              entries.toArray=()=>Array.from(entries);entries.size=()=>entries.length;
              return entries;
            }},enumerable:false}},
            remove:{{value:function(key){{const old=this[String(key)];delete this[String(key)];return old}},enumerable:false}},
            clear:{{value:function(){{Object.keys(this).forEach(key=>delete this[key])}},enumerable:false}},
            size:{{value:function(){{return Object.keys(this).length}},enumerable:false}},
            isEmpty:{{value:function(){{return Object.keys(this).length===0}},enumerable:false}},
            toString:{{value:function(){{return '{{'+Object.keys(this).map(
              key=>String(key)+'='+String(this[key])).join(', ')+'}}'}},enumerable:false}}
          }});
          return target;
        }}
        function __responseCookies(headers) {{
          const raw=String((headers||{{}})['set-cookie']||'');const result={{}};
          raw.split(/,(?=[^;,=]+=[^;,]+)/).forEach(cookie=>{{
            const first=cookie.split(';',1)[0],at=first.indexOf('=');
            if(at>0)result[first.slice(0,at).trim()]=first.slice(at+1).trim();
          }});return __map(result);
        }}
        var source = Object.assign({{}}, __sourceData, {{
          getKey: () => __sourceData.bookSourceUrl || '',
          getSource: () => source,
          getHeaderMap: () => __map(java.headerMap),
          getVariable: () => __get('sourceVariable') || '',
          setVariable: value => __put('sourceVariable', String(value)),
          putVariable: value => __put('sourceVariable', String(value)),
          getLoginHeader: () => __get('source:loginHeader') || '',
          putLoginHeader: value => __put('source:loginHeader', String(value)),
          removeLoginHeader: () => __put('source:loginHeader', ''),
          getLoginHeaderMap: () => {{
            const value=__get('source:loginHeader') || '';
            try {{ return __map(typeof value === 'string' ? JSON.parse(value.replace(/^&/,'')) : value); }}
            catch (_) {{ return {{}}; }}
          }},
          getLoginInfo: () => __get('source:loginInfo') || '',
          putLoginInfo: value => __put('source:loginInfo', typeof value === 'string' ? value : JSON.stringify(value)),
          removeLoginInfo: () => __put('source:loginInfo', ''),
          getLoginInfoMap: () => {{
            const value=__get('source:loginInfo') || '';
            try {{ return __map(typeof value === 'string' ? JSON.parse(value) : value); }}
            catch (_) {{ return {{}}; }}
          }},
          key: __sourceData.bookSourceUrl || ''
        }});
        function __response(raw) {{
          const data = JSON.parse(raw);
          return {{
            body: () => data.body,
            bodyAsBytes: () => Array.isArray(data.bodyBytes) ? data.bodyBytes :
              JSON.parse(__strbytes(String(data.body || ''), 'utf-8')),
            header: name => data.headers[String(name).toLowerCase()] || '',
            headers: name => name === undefined ? data.headers :
              (data.headers[String(name).toLowerCase()] == null ? [] :
                [data.headers[String(name).toLowerCase()]]),
            code: () => data.status,
            status: () => data.status,
            statusCode: () => data.status,
            statusMessage: () => String(data.message || ''),
            message: () => String(data.message || ''),
            isSuccessful: () => data.status >= 200 && data.status < 300,
            errorBody: () => data.status >= 200 && data.status < 300 ? null : data.body,
            cookies: () => __responseCookies(data.headers),
            url: () => data.url,
            raw: () => ({{
              code: () => data.status,
              headers: () => data.headers,
              request: () => ({{url: () => data.url}})
            }}),
            toString: () => data.body,
            valueOf: () => data.body
          }};
        }}
        function __inputStream(bytes) {{
          const data=Array.from(bytes || []).map(value => Number(value) & 255);
          let position=0;
          return {{
            read: buffer => {{
              if (buffer === undefined) return position < data.length ? data[position++] : -1;
              if (position >= data.length) return -1;
              const count=Math.min(buffer.length, data.length-position);
              for (let i=0;i<count;i++) buffer[i]=data[position++];
              return count;
            }},
            readAllBytes: () => data.slice(position),
            available: () => data.length-position,
            skip: count => {{
              const skipped=Math.min(Math.max(0,Number(count)||0),data.length-position);
              position+=skipped; return skipped;
            }},
            reset: () => {{ position=0; }},
            close: () => undefined
          }};
        }}
        function __http(method, url, body, headers) {{
          return __response(__request(String(url) + ',' + JSON.stringify({{
            method: method, body: body == null ? '' : String(body), headers: headers || {{}}
          }})));
        }}
        function __element(value) {{
          const markup = String(value == null ? '' : value);
          return {{
            text: () => __element_op(markup, 'text', ''),
            ownText: () => __element_op(markup, 'ownText', ''),
            html: () => __element_op(markup, 'html', ''),
            outerHtml: () => __element_op(markup, 'outerHtml', ''),
            attr: name => __element_op(markup, 'attr', String(name)),
            hasAttr: name => Boolean(__element_op(markup, 'hasAttr', String(name))),
            hasClass: name => String(__element_op(markup, 'attr', 'class')).split(/\\s+/).includes(String(name)),
            data: () => __element_op(markup, 'html', ''),
            tagName: () => __element_op(markup, 'tagName', ''),
            select: rule => __elements(markup, String(rule)),
            toString: () => markup,
            valueOf: () => markup
          }};
        }}
        function __elements(html, selector) {{
          const query = String(selector || '');
          const values = operation => JSON.parse(__css_query(String(html), query, operation, ''));
          const api = {{
            text: () => values('text').join(' '),
            html: () => values('html').join(''),
            outerHtml: () => values('outerHtml').join(''),
            attr: name => JSON.parse(__css_query(String(html), query, 'attr', String(name)))[0] || '',
            size: () => values('outerHtml').length,
            isEmpty: () => values('outerHtml').length === 0,
            first: () => __element(values('outerHtml')[0] || ''),
            last: () => __element(values('outerHtml').slice(-1)[0] || ''),
            get: index => __element(values('outerHtml')[Number(index)] || ''),
            eq: index => __element(values('outerHtml')[Number(index)] || ''),
            eachText: () => values('text'),
            eachAttr: name => JSON.parse(__css_query(String(html), query, 'attr', String(name))),
            select: child => __elements(values('outerHtml').join(''), String(child)),
            toString: () => values('outerHtml').join('')
          }};
          const markup = values('outerHtml');
          api.length = markup.length;
          markup.forEach((value, index) => {{ api[index] = __element(value); }});
          api[Symbol.iterator] = function* () {{
            for (let index = 0; index < markup.length; index++) yield api[index];
          }};
          return api;
        }}
        function __elementList(markup) {{
          const values = Array.from(markup || []).map(value => __element(value));
          values.text = () => values.map(value => value.text()).join(' ');
          values.html = () => values.map(value => value.html()).join('');
          values.outerHtml = () => values.map(value => value.outerHtml()).join('');
          values.attr = name => values.length ? values[0].attr(name) : '';
          values.first = () => values.length ? values[0] : null;
          values.get = index => values[Number(index)] || null;
          values.size = () => values.length;
          values.isEmpty = () => values.length === 0;
          values.toArray = () => Array.from(values);
          return values;
        }}
        function __domNode(handle) {{
          if (!handle) return null;
          const api={{__handle:String(handle)}};
          api.text=()=>__dom_op(api.__handle,'text','');
          api.ownText=()=>__dom_op(api.__handle,'ownText','');
          api.html=()=>__dom_op(api.__handle,'html','');
          api.outerHtml=()=>__dom_op(api.__handle,'outerHtml','');
          api.attr=name=>__dom_op(api.__handle,'attr',String(name));
          api.hasAttr=name=>Boolean(__dom_op(api.__handle,'hasAttr',String(name)));
          api.hasClass=name=>String(api.attr('class')).split(/\\s+/).includes(String(name));
          api.data=()=>api.html();api.tagName=()=>__dom_op(api.__handle,'tagName','');
          api.select=rule=>__domNodes(JSON.parse(__dom_query(api.__handle,String(rule))));
          api.parents=()=>__domNodes(JSON.parse(__dom_parents(api.__handle)));
          api.remove=()=>{{__dom_remove(api.__handle);return api}};
          api.toString=()=>api.outerHtml();api.valueOf=()=>api.outerHtml();
          return api;
        }}
        function __domNodes(input) {{
          const handles=Array.from(input||[]).map(String),api={{}};
          const refresh=()=>{{
            Object.keys(api).filter(k=>/^\\d+$/.test(k)).forEach(k=>delete api[k]);
            handles.forEach((h,i)=>api[i]=__domNode(h));api.length=handles.length;
          }};
          api.text=()=>handles.map(h=>__dom_op(h,'text','')).join(' ');
          api.html=()=>handles.map(h=>__dom_op(h,'html','')).join('');
          api.outerHtml=()=>handles.map(h=>__dom_op(h,'outerHtml','')).join('');
          api.attr=name=>handles.length?__dom_op(handles[0],'attr',String(name)):'';
          api.size=()=>handles.length;api.isEmpty=()=>handles.length===0;
          api.first=()=>handles.length?__domNode(handles[0]):null;
          api.last=()=>handles.length?__domNode(handles[handles.length-1]):null;
          api.get=index=>handles[Number(index)]?__domNode(handles[Number(index)]):null;
          api.eq=api.get;api.eachText=()=>handles.map(h=>__dom_op(h,'text',''));
          api.eachAttr=name=>handles.map(h=>__dom_op(h,'attr',String(name)));
          api.select=child=>__domNodes(handles.flatMap(
            h=>JSON.parse(__dom_query(h,String(child)))));
          api.parents=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_parents(h))));
          api.contains=node=>Boolean(node&&__dom_contains(JSON.stringify(handles),node.__handle));
          api.remove=index=>{{
            if(index===undefined){{handles.forEach(h=>__dom_remove(h));handles.splice(0);refresh();return api}}
            const p=Number(index);if(p<0||p>=handles.length)return null;
            const removed=handles.splice(p,1)[0];refresh();return __domNode(removed);
          }};
          api.subList=(start,end)=>({{clear:()=>{{
            handles.splice(Number(start),Math.max(0,Number(end)-Number(start)));refresh();
          }}}});
          api.toArray=()=>handles.map(__domNode);api.toString=()=>api.outerHtml();
          api[Symbol.iterator]=function*(){{for(const h of handles)yield __domNode(h)}};
          refresh();return api;
        }}
        function __document(html, location) {{
          const root=__dom_parse(String(html == null ? '' : html));
          return {{
            __handle:root,
            select: selector => __domNodes(JSON.parse(__dom_query(root,String(selector)))),
            getElementById: id => __domNodes(JSON.parse(__dom_query(root,'#'+String(id)))).first(),
            text: () => __dom_op(root,'text',''),
            title: () => __domNodes(JSON.parse(__dom_query(root,'title'))).text(),
            body: () => __domNodes(JSON.parse(__dom_query(root,'body'))).first(),
            head: () => __domNodes(JSON.parse(__dom_query(root,'head'))).first(),
            location: () => String(location || baseUrl),
            html: () => __dom_op(root,'html',''),
            outerHtml: () => __dom_op(root,'outerHtml',''),
            toString: () => __dom_op(root,'outerHtml','')
          }};
        }}
        function __jsoupConnect(url) {{
          const state={{url:String(url),headers:{{}},data:{{}},method:'GET'}};
          const api={{
            header:(name,value)=>{{state.headers[String(name)]=String(value);return api;}},
            headers:value=>{{Object.assign(state.headers,value || {{}});return api;}},
            userAgent:value=>{{state.headers['User-Agent']=String(value);return api;}},
            timeout:()=>api,ignoreContentType:()=>api,ignoreHttpErrors:()=>api,
            followRedirects:()=>api,maxBodySize:()=>api,
            data:(name,value)=>{{state.data[String(name)]=String(value);return api;}},
            method:value=>{{state.method=String(value).toUpperCase();return api;}},
            execute:()=>__http(state.method,state.url,
              Object.keys(state.data).map(k=>encodeURIComponent(k)+'='+encodeURIComponent(state.data[k])).join('&'),
              state.headers),
            get:()=>{{const response=api.execute();return __document(response.body(),response.url());}},
            post:()=>{{state.method='POST';const response=api.execute();return __document(response.body(),response.url());}}
          }};
          return api;
        }}
        var org = {{jsoup: {{
          Jsoup: {{
            parse: (html,location) => __document(html,location),
            connect: url => __jsoupConnect(url)
          }},
          nodes: {{Document:function(){{}},Element:function(){{}}}}
        }}}};
        String.prototype.getBytes = function(charset) {{
          return JSON.parse(__strbytes(String(this), String(charset || 'utf-8')));
        }};
        Object.defineProperties(Array.prototype, {{
          toArray: {{value:function(){{return Array.from(this)}},enumerable:false}},
          size: {{value:function(){{return this.length}},enumerable:false}},
          get: {{value:function(index){{return this[Number(index)]}},enumerable:false}},
          isEmpty: {{value:function(){{return this.length===0}},enumerable:false}},
          add: {{value:function(value){{this.push(value);return true}},enumerable:false}},
          contains: {{value:function(value){{return this.includes(value)}},enumerable:false}},
          subList: {{value:function(start,end){{
            const parent=this,from=Number(start),to=Number(end);
            const view=parent.slice(from,to);
            Object.defineProperty(view,'clear',{{
              value:()=>parent.splice(from,Math.max(0,to-from)),enumerable:false
            }});
            return view;
          }},enumerable:false}},
          clear: {{value:function(){{this.splice(0)}},enumerable:false}}
        }});
        function __JavaString(value) {{
          return Array.isArray(value) ? __bytesstr(JSON.stringify(value), 'utf-8') : String(value);
        }}
        function __ArrayList() {{
          const values = [];
          values.add = value => {{ values.push(value); return true; }};
          values.size = () => values.length;
          values.get = index => values[Number(index)];
          values.toArray = () => Array.from(values);
          return values;
        }}
        function __bitmap(handle) {{
          const api = {{__bitmap:String(handle)}};
          api.getWidth = () => JSON.parse(__bitmap_info(api.__bitmap))[0];
          api.getHeight = () => JSON.parse(__bitmap_info(api.__bitmap))[1];
          api.compress = (format,quality,stream) => {{
            stream._bytes = JSON.parse(__bitmap_encode(api.__bitmap,String(format),Number(quality)));
            return true;
          }};
          return api;
        }}
        function __restore(value) {{
          if (value && typeof value === 'object' && value.__bitmap) return __bitmap(value.__bitmap);
          return value;
        }}
        function __SecretKeySpec(bytes, algorithm) {{ return {{bytes:Array.from(bytes || []), algorithm:String(algorithm)}}; }}
        function __IvParameterSpec(bytes) {{ return {{bytes:Array.from(bytes || [])}}; }}
        function __DESKeySpec(bytes) {{ return {{bytes:Array.from(bytes || []), algorithm:'DES'}}; }}
        const __Cipher = {{
          ENCRYPT_MODE: 1, DECRYPT_MODE: 2,
          getInstance: transformation => {{
            const state = {{transformation:String(transformation), mode:2, key:[], iv:[]}};
            return {{
              init: (mode, key, iv) => {{
                state.mode=Number(mode); state.key=Array.from((key && key.bytes) || key || []);
                state.iv=Array.from((iv && iv.bytes) || iv || []);
              }},
              doFinal: value => JSON.parse(__crypto(
                state.mode === 1 ? 'encryptBytes' : 'decryptBytes', state.transformation,
                JSON.stringify(state.key), JSON.stringify(state.iv), JSON.stringify(Array.from(value || []))))
            }};
          }}
        }};
        const __Base64 = {{
          DEFAULT: 0, NO_PADDING: 1, NO_WRAP: 2, CRLF: 4, URL_SAFE: 8,
          encodeToString: (value, flags) => __b64encode_bytes(JSON.stringify(Array.from(value || [])), Number(flags || 0)),
          decode: value => JSON.parse(__b64bytes(Array.isArray(value)
            ? __bytesstr(JSON.stringify(value), 'utf-8') : String(value))),
          getDecoder: () => ({{decode: value => JSON.parse(__b64bytes(String(value)))}}),
          getEncoder: () => ({{encodeToString: value => __b64encode_bytes(JSON.stringify(Array.from(value || [])), 0)}})
        }};
        const Packages = {{java:{{}},javax:{{}},android:{{}},cn:{{}},org:org,io:{{}}}};
        Packages.java.lang = {{String:__JavaString,Thread:{{sleep:()=>undefined}}}};
        Packages.java.util = {{
          ArrayList:__ArrayList,
          Arrays:{{copyOfRange:(value,start,end)=>Array.from(value || []).slice(Number(start),Number(end))}},
          Base64:__Base64,
          UUID:{{randomUUID:()=>__uuid()}}
        }};
        Packages.java.io = {{ByteArrayOutputStream:function(){{
          this._bytes=[]; this.toByteArray=()=>Array.from(this._bytes); this.close=()=>undefined;
        }}}};
        Packages.javax.crypto = {{Cipher:__Cipher}};
        Packages.javax.crypto.SecretKeyFactory = {{getInstance:algorithm=>({{
          generateSecret:spec=>({{bytes:spec.bytes,algorithm:String(algorithm)}})
        }})}};
        Packages.javax.crypto.spec = {{
          SecretKeySpec:__SecretKeySpec,IvParameterSpec:__IvParameterSpec,DESKeySpec:__DESKeySpec
        }};
        Packages.android.util = {{Base64:__Base64}};
        Packages.android.text = {{TextUtils:{{isEmpty:value=>value == null || String(value).length === 0}}}};
        Packages.android.graphics = {{
          BitmapFactory:{{decodeByteArray:(value,start,length)=>__bitmap(__bitmap_decode(
            JSON.stringify(Array.from(value || []).slice(Number(start),Number(start)+Number(length)))))}},
          Bitmap:{{
            CompressFormat:{{JPEG:'JPEG',PNG:'PNG',WEBP:'WEBP'}},
            Config:{{ARGB_8888:'ARGB_8888'}},
            createBitmap:(...args)=>args[0] && args[0].__bitmap
              ? __bitmap(__bitmap_crop(args[0].__bitmap,Number(args[1]),Number(args[2]),Number(args[3]),Number(args[4])))
              : __bitmap(__bitmap_create(Number(args[0]),Number(args[1])))
          }},
          Canvas:function(bitmap){{
            this.drawBitmap=(source,x,y,paint)=>__bitmap_draw(
              bitmap.__bitmap,source.__bitmap,Number(x),Number(y));
          }}
        }};
        Packages.cn.hutool = {{core:{{codec:{{Base64:__Base64}},util:{{}}}},crypto:{{digest:{{}}}}}};
        Packages.cn.hutool.core.util.ZipUtil = {{unGzip:(value,charset)=>{{
          const raw=__gzip(JSON.stringify(Array.from(value || [])), String(charset || ''));
          return charset ? raw : JSON.parse(raw);
        }}}};
        Packages.cn.hutool.crypto.digest.DigestUtil = {{
          md5:value=>JSON.parse(__md5bytes(String(value)))
        }};
        Packages.okhttp3 = (() => {{
          function RequestBuilder() {{
            this._url=''; this._headers={{}};
            this.url=value=>{{this._url=String(value);return this}};
            this.get=()=>this;
            this.addHeader=(name,value)=>{{this._headers[String(name)]=String(value);return this}};
            this.build=()=>({{url:this._url,headers:this._headers}});
          }}
          function OkHttpClient() {{
            this.newCall=request=>({{execute:()=>{{
              const response=__response(__request(String(request.url)+','+JSON.stringify({{
                method:'GET',headers:request.headers||{{}}
              }})));
              return {{body:()=>({{bytes:()=>response.bodyAsBytes()}}),
                       code:()=>response.code(),headers:()=>response.headers(),
                       close:()=>undefined}};
            }}}});
          }}
          return {{OkHttpClient:OkHttpClient,Request:{{Builder:RequestBuilder}}}};
        }})();
        Packages.io.legado = {{app:{{help:{{http:{{}}}},model:{{analyzeRule:{{}}}}}}}};
        Packages.io.legado.app.help.http.StrResponse = (url,body)=>__response(JSON.stringify({{
          body:String(body == null ? '' : body),headers:{{}},status:200,url:String(url || baseUrl)
        }}));
        Packages.io.legado.app.model.analyzeRule.AnalyzeRule = ()=>({{
          cacheFile:url=>__ajax(String(url))
        }});
        function JavaImporter() {{
          const target = {{}};
          target.importPackage = (...packages) => {{
            packages.forEach(pkg => {{ if (pkg) Object.keys(pkg).forEach(key => target[key]=pkg[key]); }});
            return target;
          }};
          return target;
        }}
        function importPackage(...packages) {{
          packages.forEach(pkg => {{ if (pkg) Object.keys(pkg).forEach(key => globalThis[key]=pkg[key]); }});
          return globalThis;
        }}
        function importClass(...classes) {{
          classes.forEach(value => {{
            if (value === org.jsoup.Jsoup) globalThis.Jsoup = value;
          }});
          return globalThis;
        }}
        function sleep(_milliseconds) {{ return undefined; }}
        var Buffer = {{
          concat: arrays => Array.prototype.concat.apply([], Array.from(arrays || []).map(x => Array.from(x)))
        }};
        var java = {{
          ajax: value => __ajax(String(value)),
          getString: (rule, content, isUrl) =>
            __get_string(String(rule), content, Boolean(isUrl)),
          getElement: rule => __element(__get_string(String(rule))),
          getStringList: (rule, content, isUrl) =>
            JSON.parse(__get_list(String(rule), content, Boolean(isUrl))),
          getElements: (rule, content) =>
            __elementList(JSON.parse(__get_elements(String(rule), content))),
          setContent: (value, parent) => {{
            src = String(value == null ? '' : value); result = src;
            if (parent != null && String(parent)) {{ baseUrl=String(parent); java.url=baseUrl; }}
            return src;
          }},
          get: name => __get(String(name)),
          put: (name, value) => __put(String(name), value),
          get: (value, headers) => (headers !== undefined || /^https?:\\/\\//i.test(String(value)))
            ? __http('GET', value, '', headers)
            : __get(String(value)),
          post: (url, body, headers) => __http('POST', url, body, headers),
          head: (url, headers) => __http('HEAD', url, '', headers),
          ajaxAll: urls => Array.from(urls || []).map(url => __response(__request(String(url)))),
          getStrResponse: value => __response(__request(String(value || java.ruleUrl || baseUrl))),
          getByteArray: value => __response(__request(String(value || java.ruleUrl || baseUrl))).bodyAsBytes(),
          getInputStream: value => __inputStream(
            __response(__request(String(value || java.ruleUrl || baseUrl))).bodyAsBytes()),
          getResponse: () => __response(__request(String(java.ruleUrl || java.url || baseUrl))),
          getHeaderMap: () => java.headerMap,
          getCookie: (url, name) => name === undefined
            ? __cookie_get(String(url || baseUrl)) : cookie.getKey(url, name),
          getKey: (url, name) => cookie.getKey(url, name),
          setCookie: (url, value) => cookie.setCookie(url, value),
          replaceCookie: (url, value) => cookie.replaceCookie(url, value),
          removeCookie: url => cookie.removeCookie(url),
          md5Encode: value => __md5(String(value)),
          md5Encode16: value => __md5(String(value)).slice(8, 24),
          base64Decode: value => __b64decode(String(value)),
          base64Encode: value => __b64encode(String(value)),
          base64DecodeToByteArray: value => JSON.parse(__b64bytes(String(value))),
          hexDecodeToByteArray: value => JSON.parse(__hexbytes(String(value))),
          hexDecodeToString: value => __bytesstr(__hexbytes(String(value)), 'utf-8'),
          hexEncodeToString: value => Array.isArray(value)
            ? value.map(x => (Number(x) & 255).toString(16).padStart(2, '0')).join('')
            : Array.from(unescape(encodeURIComponent(String(value))))
                .map(x => x.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
          strToBytes: (value, charset) => JSON.parse(__strbytes(String(value), String(charset || 'utf-8'))),
          bytesToStr: (value, charset) => __bytesstr(JSON.stringify(Array.from(value || [])), String(charset || 'utf-8')),
          digestHex: (value, algorithm) => __digest(String(value), String(algorithm || 'MD5')),
          HMacBase64: (value, algorithm, secret) => __hmac(String(value), String(algorithm), String(secret), true),
          HMacHex: (value, algorithm, secret) => __hmac(String(value), String(algorithm), String(secret), false),
          aesBase64DecodeToString: (value, key, transformation, iv) =>
            __crypto('decryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          aesBase64DecodeToByteArray: (value, key, transformation, iv) => JSON.parse(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value))),
          aesDecodeToByteArray: (value, key, transformation, iv) => JSON.parse(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''),
              __strbytes(String(value), 'utf-8'))),
          aesDecodeToString: (value, key, transformation, iv) => __bytesstr(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''),
              __strbytes(String(value), 'utf-8')), 'utf-8'),
          aesEncodeToByteArray: (value, key, transformation, iv) => JSON.parse(
            __crypto('encryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value))),
          aesEncodeToString: (value, key, transformation, iv) => __bytesstr(
            __crypto('encryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)), 'utf-8'),
          aesEncodeToBase64String: (value, key, transformation, iv) =>
            __crypto('encryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          aesEncodeToBase64ByteArray: (value, key, transformation, iv) => JSON.parse(__strbytes(
            __crypto('encryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)), 'utf-8')),
          desEncodeToBase64String: (value, key, transformation, iv) =>
            __crypto('encryptStr', String(transformation || 'DES/ECB/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          tripleDESEncodeBase64Str: (value, key, mode, padding, iv) =>
            __crypto('encryptStr', 'DESede/' + String(mode || 'CBC') + '/' + String(padding || 'PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          randomUUID: () => __uuid(),
          timeFormat: (value, pattern) => __time_format(value, String(pattern || 'yyyy-MM-dd HH:mm:ss'), false),
          timeFormatUTC: (value, pattern) => __time_format(value, String(pattern || 'yyyy-MM-dd HH:mm:ss'), true),
          toNumChapter: value => __num_chapter(String(value)),
          t2s: value => __t2s(String(value)),
          htmlFormat: value => __html_format(String(value)),
          queryTTF: value => __font_query(String(value || '')),
          queryBase64TTF: value => __font_query(String(value || '')),
          replaceFont: (text, font1, font2) => __font_replace(String(text), String(font1 || ''), String(font2 || '')),
          getBook: () => book,
          getVerificationCode: image => __verification(String(image || '')),
          lang: 'zh-CN',
          encodeURI: value => encodeURI(String(value)),
          log: value => value,
          logType: value => value,
          toast: value => undefined,
          longToast: value => undefined,
          refreshExplore: () => undefined,
          refreshTocUrl: () => undefined,
          initUrl: () => baseUrl,
          reGetBook: () => book,
          toURL: (value, parent) => {{
            const absolute = JSON.parse(__to_url(String(value || ''), String(parent || baseUrl)));
            absolute.searchParams=__map(absolute.searchParams);
            absolute.toString=()=>absolute.href;
            return absolute;
          }},
          setBaseUrl: value => {{ baseUrl = String(value); java.url = baseUrl; return baseUrl; }},
          importScript: value => __ajax(String(value)),
          webView: (htmlValue, url, js) => {{
            const target=String(url || (typeof htmlValue === 'string' && /^https?:/i.test(htmlValue) ? htmlValue : baseUrl));
            return __response(__browser(target, String(js || ''))).body();
          }},
          webViewGetOverrideUrl: (htmlValue, url, js, _regex) =>
            __response(__browser(String(url || baseUrl), String(js || ''))).url(),
          webViewGetSource: (htmlValue, url, js, sourceRegex) => {{
            const body=String(java.webView(htmlValue,url,js) || '');
            if (!sourceRegex) return body;
            const match=body.match(new RegExp(String(sourceRegex)));
            return match ? (match[1] == null ? match[0] : match[1]) : null;
          }},
          connect: value => __response(__request(String(value))),
          startBrowser: (url, title) => __response(__browser(String(url || baseUrl), String(title || ''))),
          startBrowserAwait: (url, title) => __response(__browser(String(url || baseUrl), String(title || ''))),
          createSymmetricCrypto: (transformation, key, iv) => ({{
            decryptStr: value => __crypto('decryptStr', String(transformation), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
            encryptStr: value => __crypto('encryptStr', String(transformation), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
            decrypt: value => JSON.parse(__crypto('decryptBytes', String(transformation), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value))),
            encrypt: value => JSON.parse(__crypto('encryptBytes', String(transformation), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value))),
            encryptBase64: value => __crypto('encryptStr', String(transformation), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value))
          }})
        }};
        java.ruleUrl = baseUrl;
        java.redirectUrl = baseUrl;
        java.url = baseUrl;
        java.key = key;
        java.headerMap = __map({json.dumps(raw_headers if isinstance(raw_headers, dict) else {}, ensure_ascii=False)});
        java.webViewUA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36';
        java.getWebViewUA = () => java.webViewUA;
        java.androidId = java.deviceID = {json.dumps(self.device_id)};
        java = new Proxy(java, {{
          get: (target, name) => Object.prototype.hasOwnProperty.call(target, name)
            ? target[name]
            : ((...args) => __unsupported('java.' + String(name)))
        }});
        function replace(pattern, replacement) {{
          result = String(result == null ? '' : result).replace(pattern, replacement);
          return result;
        }}
        function Reload(url) {{ return __ajax(String(url)); }}
        var cache = {{
          get: key => {{
            const raw = __cache_get(String(key));
            return raw === '' ? null : __restore(JSON.parse(raw));
          }},
          put: (key, value, ttl) => {{
            __cache_put(String(key), JSON.stringify(value), Number(ttl || 0));
            return value;
          }},
          delete: key => __cache_delete(String(key)),
          getFromMemory: key => {{
            const raw = __cache_get(String(key));
            return raw === '' ? null : __restore(JSON.parse(raw));
          }},
          putMemory: (key, value) => {{
            __cache_put(String(key), JSON.stringify(value), 0);
            return value;
          }},
          getFile: key => {{
            const raw = __file_cache_get(String(key));
            return raw === '' ? null : __restore(JSON.parse(raw));
          }},
          putFile: (key, value) => {{
            __file_cache_put(String(key), JSON.stringify(value));
            return value;
          }},
          deleteMemory: key => __cache_delete(String(key))
        }};
        var cookie = {{
          getCookie: url => __cookie_get(String(url)),
          getKey: (url, name) => {{
            const all = __cookie_get(String(url));
            const found = all.split(';').map(x => x.trim().split('=')).find(x => x[0] === name);
            return found ? found.slice(1).join('=') : '';
          }},
          mapToCookie: value => {{
            let data = value;
            if (typeof data === 'string') {{
              try {{ data = JSON.parse(data); }} catch (_) {{ return data; }}
            }}
            if (!data || typeof data !== 'object') return '';
            return Object.keys(data).map(key => key + '=' + data[key]).join('; ');
          }},
          setCookie: (url, value) => __cookie_set(String(url || baseUrl), String(value || '')),
          replaceCookie: (url, value) => {{
            __cookie_remove(String(url || baseUrl));
            return __cookie_set(String(url || baseUrl), String(value || ''));
          }},
          removeCookie: url => __cookie_remove(String(url || baseUrl))
        }};
        function __infoDefault(name) {{
          const pools = [];
          try {{ if (typeof list !== 'undefined' && Array.isArray(list)) pools.push(list); }} catch (_) {{}}
          try {{ if (Array.isArray(result)) pools.push(result); }} catch (_) {{}}
          for (const pool of pools) {{
            const item = pool.find(entry => entry && String(entry.title) === String(name));
            if (item) return item.default != null ? item.default :
              (Array.isArray(item.chars) && item.chars.length ? item.chars[0] : '');
          }}
          return '';
        }}
        const __infoStored = (() => {{
          try {{ let value=JSON.parse(__get_json('infoMap')); if(typeof value==='string') value=JSON.parse(value); return value && typeof value==='object' ? value : {{}}; }}
          catch (_) {{ return {{}}; }}
        }})();
        const infoMap = new Proxy(__infoStored, {{
          get: (target, name) => name === 'save' ? (() => {{
            __put('infoMap', JSON.stringify(target)); return true;
          }}) :
            (Object.prototype.hasOwnProperty.call(target, name) ? target[name] : __infoDefault(name)),
          set: (target, name, value) => {{ target[name] = value; return true; }}
        }});
        """
        clean = self._clean_script(script)
        if context.response_data is not None:
            clean += """
            ;JSON.stringify((function(value){
              if (value && typeof value.body === 'function') {
                return {body:String(value.body()), headers:value.headers(),
                  status:Number(value.code()), url:String(value.url())};
              }
              return {body:String(value == null ? '' : value), headers:{},
                status:200, url:String(baseUrl)};
            })(result))
            """
        js_lib = self._resolve_js_lib(source_data.get("jsLib", ""))
        try:
            value = ctx.eval(bootstrap + "\n" + js_lib + "\n" + clean)
            self._sync_context_objects(ctx, context)
            if hasattr(value, "json"):
                return json.loads(value.json())
            return value
        except Exception as exc:
            raise JsBridgeError(str(exc)) from exc

    @staticmethod
    def _external_rule_root(content):
        from bs4 import BeautifulSoup
        if hasattr(content, "json"):
            content = content.json()
        if isinstance(content, (dict, list)):
            return content, "json"
        text = str(content if content is not None else "")
        if text.lstrip().startswith(("{", "[")):
            try:
                return json.loads(text), "json"
            except json.JSONDecodeError:
                pass
        return BeautifulSoup(text, "html.parser"), "html"

    def _context_get_string(self, context: JsContext, rule, content=None,
                            is_url=False):
        from app.services.rule_engine import extract_value
        if isinstance(content, bool):
            is_url, content = content, None
        if content is None:
            return context.get_string(str(rule)) if context.get_string else ""
        root, kind = self._external_rule_root(content)
        return extract_value(root, str(rule), kind,
                             context.base_url if is_url else "")

    def _context_get_list(self, context: JsContext, rule, content=None,
                          is_url=False):
        from app.services.rule_engine import extract_values
        if isinstance(content, bool):
            is_url, content = content, None
        if content is None:
            if context.get_string_list:
                return context.get_string_list(str(rule))
            return ([context.get_string(str(rule))] if context.get_string else [])
        root, kind = self._external_rule_root(content)
        return extract_values(root, str(rule), kind,
                              context.base_url if is_url else "")

    def _context_get_elements(self, context: JsContext, rule, content=None):
        from app.services.legado_rule_parser import evaluate
        if content is None:
            if context.get_elements:
                return context.get_elements(str(rule))
            return context.get_string_list(str(rule)) if context.get_string_list else []
        root, kind = self._external_rule_root(content)
        return [str(item) for item in evaluate(root, str(rule), kind)]

    @staticmethod
    def _sync_context_objects(ctx, context: JsContext):
        """Copy direct JS mutations back to Python book/chapter/source objects."""
        try:
            state = json.loads(ctx.eval(
                "JSON.stringify({source:source,book:book,chapter:chapter})"))
        except Exception:
            return
        for target, name in ((context.source, "source"), (context.book, "book"),
                             (context.chapter, "chapter")):
            if target is None:
                continue
            values = state.get(name) or {}
            if name == "book":
                for legado_name, python_name in {
                    "bookUrl": "url", "tocUrl": "toc_url",
                    "coverUrl": "cover_url", "wordCount": "word_count",
                    "lastChapterTitle": "last_chapter", "originName": "origin_name",
                    "totalChapterNum": "total_chapter_num",
                    "durChapterIndex": "dur_chapter_index",
                    "durChapterTitle": "dur_chapter_title", "canUpdate": "can_update",
                    "customIntro": "custom_intro", "customTag": "custom_tag",
                    "customCoverUrl": "custom_cover_url",
                    "latestChapterTime": "latest_chapter_time",
                    "lastCheckTime": "last_check_time", "lastCheckCount": "last_check_count",
                    "durChapterPos": "dur_chapter_pos", "durChapterTime": "dur_chapter_time",
                    "originOrder": "origin_order", "readConfig": "read_config",
                    "infoHtml": "info_html", "tocHtml": "toc_html",
                    "variable": "variables",
                }.items():
                    if legado_name in values:
                        values[python_name] = values[legado_name]
            if name == "chapter":
                if "title" in values:
                    values["name"] = values["title"]
                if "chapterUrl" in values:
                    values["url"] = values["chapterUrl"]
                for legado_name, python_name in {
                    "baseUrl": "base_url", "bookUrl": "book_url",
                    "resourceUrl": "resource_url", "isVolume": "is_volume",
                    "isVip": "is_vip", "isPay": "is_pay",
                    "variable": "variables",
                }.items():
                    if legado_name in values:
                        values[python_name] = values[legado_name]
            for key, value in values.items():
                if isinstance(target, dict):
                    if key in target:
                        target[key] = value
                elif hasattr(target, key):
                    try:
                        setattr(target, key, value)
                    except (AttributeError, TypeError):
                        pass

    @staticmethod
    def _clean_script(script: str) -> str:
        value = script.strip()
        if value.startswith("@js:"):
            value = value[4:].strip()
        if value.startswith("<js>") and value.endswith("</js>"):
            value = value[4:-5].strip()
        # Rhino accepts an unparenthesized array destructuring parameter in
        # arrow callbacks; ECMAScript/QuickJS requires parentheses.
        value = re.sub(
            r"(\.(?:map|flatMap|filter|forEach|find|some|every|reduce)\s*\()"
            r"\s*(\[[^\]\r\n]+\])\s*=>",
            r"\1(\2)=>", value,
        )
        value = value.replace("\u200b", "").replace("\ufeff", "")
        # Normalize a common community-source typo only where the character is
        # being used as a JavaScript statement terminator.
        value = re.sub(r"(?<=[\]\)])；", ";", value)
        return value

    def _put(self, context: JsContext, name, value):
        context.variables[str(name)] = value
        if str(name) in {"sourceVariable", "source:loginHeader", "source:loginInfo", "infoMap"}:
            source_data = (context.source.to_dict() if hasattr(context.source, "to_dict")
                           else dict(context.source))
            source_key = str(source_data.get("bookSourceUrl", ""))
            if source_key:
                with self._PERSIST_LOCK:
                    disk_state = self._load_persistent_state()
                    if self.device_id:
                        disk_state.setdefault("deviceId", self.device_id)
                    sources = disk_state.setdefault("sources", {})
                    sources.setdefault(source_key, {})[str(name)] = value
                    self.persistent_state = disk_state
                    self._save_persistent_state()
        return value

    def _cache_get(self, key) -> str:
        item = self.cache_store.get(str(key))
        if not item:
            return ""
        value, expires = item
        if expires and expires <= time.time():
            self.cache_store.pop(str(key), None)
            return ""
        return value

    def _cache_put(self, key, value, ttl=0):
        seconds = max(0.0, float(ttl or 0))
        self.cache_store[str(key)] = (str(value), time.time() + seconds if seconds else 0)
        return value

    def _cache_delete(self, key):
        self.cache_store.pop(str(key), None)
        return None

    @staticmethod
    def _time_format(value, pattern="yyyy-MM-dd HH:mm:ss", utc=False) -> str:
        number = float(value)
        if abs(number) < 100_000_000_000:
            number *= 1000
        moment = datetime.fromtimestamp(number / 1000, timezone.utc if utc else None)
        source_pattern = str(pattern or "yyyy-MM-dd HH:mm:ss")
        fmt = source_pattern
        for java, python in (("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"),
                             ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
                             ("SSS", "%f")):
            fmt = fmt.replace(java, python)
        rendered = moment.strftime(fmt)
        return rendered[:-3] if "SSS" in source_pattern else rendered

    @staticmethod
    def _to_num_chapter(value) -> str:
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                  "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        units = {"十": 10, "百": 100, "千": 1000, "万": 10000}

        def convert(match):
            total = section = number = 0
            for char in match.group(0):
                if char in digits:
                    number = digits[char]
                else:
                    unit = units[char]
                    if unit == 10000:
                        total += (section + number) * unit
                        section = number = 0
                    else:
                        section += (number or 1) * unit
                        number = 0
            return str(total + section + number)

        return re.sub(r"[零〇一二两三四五六七八九十百千万]+", convert, str(value))

    @staticmethod
    def _base64_decode(value) -> str:
        """Legado-compatible standard and URL-safe Base64 decoding."""
        text = str(value).strip()
        text += "=" * ((4 - len(text) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode(text)
        except (ValueError, binascii.Error):
            raw = base64.b64decode(text)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _base64_bytes(value) -> str:
        text = str(value).strip()
        text += "=" * ((4 - len(text) % 4) % 4)
        return json.dumps(list(base64.urlsafe_b64decode(text)))

    @classmethod
    def _base64_encode_bytes(cls, value, flags=0) -> str:
        raw = cls._byte_values(value)
        encoded = (base64.urlsafe_b64encode(raw) if int(flags or 0) & 8
                   else base64.b64encode(raw)).decode("ascii")
        if int(flags or 0) & 1:
            encoded = encoded.rstrip("=")
        return encoded

    @classmethod
    def _gzip(cls, value, charset="") -> str:
        raw = gzip.decompress(cls._byte_values(value))
        if charset:
            encoding = str(charset).replace("GBK", "gb18030").replace(
                "ISO-8859-1", "latin-1")
            return raw.decode(encoding, errors="replace")
        return json.dumps(list(raw))

    @staticmethod
    def _hex_bytes(value) -> str:
        text = re.sub(r"\s+", "", str(value))
        if len(text) % 2:
            text = "0" + text
        return json.dumps(list(bytes.fromhex(text)))

    @staticmethod
    def _byte_values(value) -> bytes:
        if hasattr(value, "json"):
            value = value.json()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value.encode()
        return bytes((int(item) & 255) for item in (value or []))

    @classmethod
    def _bytes_to_string(cls, value, charset="utf-8") -> str:
        encoding = str(charset or "utf-8").replace("ISO-8859-1", "latin-1")
        return cls._byte_values(value).decode(encoding, errors="replace")

    @staticmethod
    def _string_to_bytes(value, charset="utf-8") -> str:
        encoding = str(charset or "utf-8").replace("ISO-8859-1", "latin-1")
        return json.dumps(list(str(value).encode(encoding, errors="replace")))

    @staticmethod
    def _hash_name(algorithm: str) -> str:
        return re.sub(r"[^a-z0-9]", "", algorithm.lower()).replace("sha", "sha", 1)

    @classmethod
    def _digest(cls, value, algorithm="MD5") -> str:
        name = cls._hash_name(str(algorithm))
        return hashlib.new(name, str(value).encode()).hexdigest()

    @classmethod
    def _hmac(cls, value, algorithm, secret, as_base64=False) -> str:
        name = cls._hash_name(str(algorithm).replace("HMAC", "")) or "sha1"
        result = hmac.new(str(secret).encode(), str(value).encode(), name).digest()
        return base64.b64encode(result).decode() if as_base64 else result.hex()

    def _resolve_js_lib(self, value) -> str:
        """Load Legado jsLib mappings while preserving inline library code."""
        if not value:
            return ""
        data = value
        if isinstance(value, str):
            text = value.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return text
        if isinstance(data, dict):
            entries = data.values()
        elif isinstance(data, list):
            entries = data
        else:
            return str(data)
        scripts = []
        for entry in entries:
            text = str(entry or "").strip()
            if text.startswith(("http://", "https://", "/")):
                scripts.append(self._ajax(text))
            elif text:
                scripts.append(text)
        return "\n".join(scripts)

    @staticmethod
    def _to_url(value, parent=""):
        absolute = urljoin(str(parent or ""), str(value or ""))
        parsed = urlsplit(absolute)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        return json.dumps({
            "href": absolute,
            "host": parsed.netloc,
            "origin": origin,
            "pathname": parsed.path,
            "searchParams": dict(parse_qsl(parsed.query, keep_blank_values=True)),
        }, ensure_ascii=False)

    @staticmethod
    def _unsupported(name):
        raise JsBridgeError(f"暂不支持 {name}")

    def _dom_register(self, node) -> str:
        handle = uuid.uuid4().hex
        self._current_dom_store()[handle] = node
        return handle

    def _current_dom_store(self) -> dict:
        store = getattr(self._thread_state, "dom_store", None)
        if store is None:
            store = self.dom_store
        return store

    def _dom_parse(self, markup) -> str:
        from bs4 import BeautifulSoup
        return self._dom_register(BeautifulSoup(str(markup or ""), "html.parser"))

    def _dom_query(self, handle: str, selector: str) -> str:
        from app.services.legado_rule_parser import select_jsoup
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return "[]"
        try:
            nodes = select_jsoup(node, str(selector or "*"))
        except Exception:
            nodes = []
        return json.dumps([self._dom_register(item) for item in nodes])

    def _dom_operation(self, handle: str, operation="text", argument=""):
        from bs4 import BeautifulSoup, Tag
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return ""
        if operation == "text":
            return node.get_text(" ", strip=True)
        if operation == "ownText":
            if not isinstance(node, Tag):
                return node.get_text(" ", strip=True)
            return " ".join(text.strip() for text in node.find_all(
                string=True, recursive=False) if text.strip())
        if operation == "html":
            return node.decode_contents() if hasattr(node, "decode_contents") else str(node)
        if operation == "outerHtml":
            return str(node)
        if operation == "attr" and isinstance(node, Tag):
            value = node.get(str(argument), "")
            return " ".join(value) if isinstance(value, list) else str(value)
        if operation == "hasAttr":
            return isinstance(node, Tag) and node.has_attr(str(argument))
        if operation == "tagName":
            return node.name if isinstance(node, Tag) else ""
        return ""

    def _dom_remove(self, handle: str) -> bool:
        node = self._current_dom_store().get(str(handle))
        if node is None or not hasattr(node, "extract"):
            return False
        node.extract()
        return True

    def _dom_parents(self, handle: str) -> str:
        from bs4 import Tag
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return "[]"
        parents = [item for item in getattr(node, "parents", []) if isinstance(item, Tag)]
        return json.dumps([self._dom_register(item) for item in parents])

    def _dom_contains(self, handles_json: str, target_handle: str) -> bool:
        store = self._current_dom_store()
        target = store.get(str(target_handle))
        if target is None:
            return False
        try:
            handles = json.loads(handles_json)
        except (TypeError, json.JSONDecodeError):
            handles = []
        return any(store.get(str(handle)) is target for handle in handles)

    @staticmethod
    def _css_query(html, selector, operation="text", attribute="") -> str:
        from bs4 import BeautifulSoup
        from app.services.legado_rule_parser import select_jsoup

        soup = BeautifulSoup(str(html), "html.parser")
        try:
            nodes = select_jsoup(soup, str(selector or "html"))
        except Exception:
            nodes = []
        if operation == "attr":
            values = []
            for node in nodes:
                value = node.get(str(attribute), "")
                values.append(" ".join(value) if isinstance(value, list) else str(value))
        elif operation == "html":
            values = [node.decode_contents() for node in nodes]
        elif operation == "outerHtml":
            values = [str(node) for node in nodes]
        else:
            values = [node.get_text(" ", strip=True) for node in nodes]
        return json.dumps(values, ensure_ascii=False)

    @staticmethod
    def _element_operation(markup, operation="text", argument=""):
        from bs4 import BeautifulSoup, Tag

        soup = BeautifulSoup(str(markup), "html.parser")
        node = next((item for item in soup.contents if isinstance(item, Tag)), None)
        if node is None:
            return str(markup) if operation in {"text", "ownText", "html", "outerHtml"} else ""
        if operation == "text":
            return node.get_text(" ", strip=True)
        if operation == "ownText":
            return " ".join(text.strip() for text in node.find_all(string=True, recursive=False)
                            if text.strip())
        if operation == "html":
            return node.decode_contents()
        if operation == "outerHtml":
            return str(node)
        if operation == "attr":
            value = node.get(str(argument), "")
            return " ".join(value) if isinstance(value, list) else str(value)
        if operation == "hasAttr":
            return node.has_attr(str(argument))
        if operation == "tagName":
            return node.name
        return ""

    @staticmethod
    def _traditional_to_simplified(value) -> str:
        try:
            from opencc import OpenCC
            return OpenCC("t2s").convert(str(value))
        except ImportError as exc:
            raise JsBridgeError("缺少繁简转换依赖 opencc-python-reimplemented") from exc

    def _bitmap_decode(self, value: str) -> str:
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(self._byte_values(value)))
            image.load()
            handle = uuid.uuid4().hex
            self.bitmap_store[handle] = image.copy()
            image.close()
            return handle
        except Exception as exc:
            raise JsBridgeError(f"bitmap decode failed: {exc}") from None

    def _browser(self, url: str, title="") -> str:
        if self.browser_fetcher is None:
            raise JsBridgeError("interactive browser is not configured")
        value = self.browser_fetcher(str(url), str(title))
        if isinstance(value, dict):
            result = value
        elif isinstance(value, (list, tuple)):
            body = value[0] if value else ""
            content_type = value[1] if len(value) > 1 else "text/html"
            final_url = value[2] if len(value) > 2 else url
            result = {"body": body, "headers": {"content-type": content_type},
                      "status": 200, "url": final_url}
        else:
            result = {"body": str(value or ""), "headers": {}, "status": 200, "url": url}
        result.setdefault("headers", {})
        result.setdefault("status", 200)
        result.setdefault("url", url)
        result.setdefault("body", "")
        return json.dumps(result, ensure_ascii=False)

    def _verification_code(self, image: str) -> str:
        if self.verification_code_provider is None:
            raise JsBridgeError("verification-code input is not configured")
        return str(self.verification_code_provider(str(image)) or "")

    def _bitmap_info(self, handle: str) -> str:
        image = self.bitmap_store.get(str(handle))
        if image is None:
            raise JsBridgeError("bitmap handle is no longer available")
        return json.dumps([image.width, image.height])

    def _bitmap_crop(self, handle: str, x, y, width, height) -> str:
        image = self.bitmap_store.get(str(handle))
        if image is None:
            raise JsBridgeError("bitmap handle is no longer available")
        left, top = max(0, int(x)), max(0, int(y))
        right = min(image.width, left + max(0, int(width)))
        bottom = min(image.height, top + max(0, int(height)))
        cropped = image.crop((left, top, right, bottom))
        new_handle = uuid.uuid4().hex
        self.bitmap_store[new_handle] = cropped
        return new_handle

    def _bitmap_create(self, width, height) -> str:
        from PIL import Image
        handle = uuid.uuid4().hex
        self.bitmap_store[handle] = Image.new(
            "RGBA", (max(1, int(width)), max(1, int(height))), (0, 0, 0, 0))
        return handle

    def _bitmap_draw(self, target_handle: str, source_handle: str, x, y) -> bool:
        target = self.bitmap_store.get(str(target_handle))
        source = self.bitmap_store.get(str(source_handle))
        if target is None or source is None:
            raise JsBridgeError("bitmap handle is no longer available")
        position = (int(x), int(y))
        if target.mode == "RGBA":
            target.alpha_composite(source.convert("RGBA"), dest=position)
        else:
            target.paste(source, position)
        return True

    def _bitmap_encode(self, handle: str, image_format="JPEG", quality=100) -> str:
        image = self.bitmap_store.get(str(handle))
        if image is None:
            raise JsBridgeError("bitmap handle is no longer available")
        output = io.BytesIO()
        fmt = str(image_format or "JPEG").upper()
        if fmt == "JPG":
            fmt = "JPEG"
        writable = image.convert("RGB") if fmt == "JPEG" and image.mode not in ("RGB", "L") else image
        writable.save(output, format=fmt, quality=max(1, min(100, int(quality or 100))))
        return json.dumps(list(output.getvalue()))

    def _query_font(self, value: str) -> str:
        """Parse a URL, file or Base64 TTF into a glyph-outline lookup."""
        try:
            from fontTools.ttLib import TTFont

            key = hashlib.md5(value.encode()).hexdigest()
            if key in self.font_store:
                return key
            if re.match(r"^https?://", value, re.I):
                if self.opener is None:
                    raise JsBridgeError("JS HTTP runtime has been closed")
                with self.opener.open(Request(value, headers=self.default_headers),
                                      timeout=self.timeout) as response:
                    raw = response.read()
            else:
                try:
                    raw = base64.b64decode(value)
                except (ValueError, binascii.Error):
                    with open(value, "rb") as stream:
                        raw = stream.read()
            font = TTFont(io.BytesIO(raw), lazy=False)
            cmap = font.getBestCmap() or {}
            glyf = font.get("glyf")
            forward, reverse = {}, {}
            for code, glyph_name in cmap.items():
                try:
                    coordinates, end_points, _flags = glyf[glyph_name].getCoordinates(glyf)
                    signature = (tuple(tuple(point) for point in coordinates), tuple(end_points))
                except Exception:
                    signature = (glyph_name,)
                forward[int(code)] = signature
                reverse.setdefault(signature, int(code))
            self.font_store[key] = (forward, reverse)
            font.close()
            return key
        except Exception as exc:
            raise JsBridgeError(f"font parse failed: {exc}") from None

    def _replace_font(self, text: str, source_key: str, target_key: str) -> str:
        source = self.font_store.get(source_key)
        target = self.font_store.get(target_key)
        if not source or not target:
            return text
        source_forward, _ = source
        _, target_reverse = target
        return "".join(chr(target_reverse.get(source_forward.get(ord(char)), ord(char)))
                       if ord(char) in source_forward else char for char in text)

    def _cookie_string(self, url: str) -> str:
        url = self._cookie_url(url)
        request = Request(url)
        self.cookies.add_cookie_header(request)
        return request.get_header("Cookie", "")

    def set_cookie(self, url: str, cookie_text: str) -> bool:
        """Import a Cookie/Set-Cookie string into the JS HTTP cookie jar."""
        target = urlsplit(self._cookie_url(url))
        domain = target.hostname or ""
        if not domain:
            return False
        parsed = SimpleCookie()
        parsed.load(str(cookie_text or ""))
        for name, morsel in parsed.items():
            cookie_domain = (morsel["domain"] or domain).lstrip(".")
            path = morsel["path"] or "/"
            secure = bool(morsel["secure"]) or target.scheme.lower() == "https"
            self._discard_cookie(cookie_domain, path, name)
            self.cookies.set_cookie(Cookie(
                version=0, name=name, value=morsel.value, port=None,
                port_specified=False, domain=cookie_domain,
                domain_specified=bool(morsel["domain"]),
                domain_initial_dot=str(morsel["domain"]).startswith("."),
                path=path, path_specified=bool(morsel["path"]), secure=secure,
                expires=None, discard=True, comment=None, comment_url=None,
                rest={"HttpOnly": morsel["httponly"]} if morsel["httponly"] else {},
                rfc2109=False,
            ))
        return bool(parsed)

    def remove_cookie(self, url: str) -> bool:
        """Remove cookies visible to *url*, matching Legado's CookieStore API."""
        visible = {(item.domain, item.path, item.name) for item in self.cookies
                   if self._domain_matches(
                       urlsplit(self._cookie_url(url)).hostname or "", item.domain)}
        for domain, path, name in visible:
            self._discard_cookie(domain, path, name)
        return bool(visible)

    def import_cookie_header(self, url: str, cookie_text: str) -> None:
        if cookie_text:
            self.set_cookie(url, cookie_text)

    def export_cookie_header(self, url: str) -> str:
        return self._cookie_string(url)

    def _discard_cookie(self, domain: str, path: str, name: str) -> None:
        try:
            self.cookies.clear(domain, path, name)
        except KeyError:
            pass

    def _cookie_url(self, url: str) -> str:
        value = str(url or self.base_url or "").strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
            value = "https://" + value.lstrip("/")
        return value

    @staticmethod
    def _domain_matches(host: str, domain: str) -> bool:
        normalized = domain.lstrip(".").lower()
        host = host.lower()
        return host == normalized or host.endswith("." + normalized)

    @classmethod
    def _crypto(cls, operation: str, transformation: str, key: str, iv: str,
                value: str) -> str:
        """Legado-compatible AES, DES and DESede string/byte operations."""
        try:
            from Crypto.Cipher import AES, DES, DES3
            from Crypto.Util.Padding import pad, unpad

            def decode_argument(raw):
                try:
                    decoded = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    decoded = raw
                return (cls._byte_values(decoded) if isinstance(decoded, list)
                        else str(decoded or "").encode())

            spec = transformation.upper()
            algorithm = spec.split("/", 1)[0]
            cipher_type = AES if algorithm == "AES" else (DES3 if algorithm in {
                "DESEDE", "TRIPLEDES", "3DES"} else DES if algorithm == "DES" else None)
            if cipher_type is None:
                raise ValueError(f"unsupported transformation: {transformation}")
            key_bytes, iv_bytes = decode_argument(key), decode_argument(iv)
            block_size = cipher_type.block_size
            if cipher_type is DES3:
                key_bytes = DES3.adjust_key_parity(key_bytes[:24].ljust(24, b"\0"))
            elif cipher_type is AES:
                size = 16 if len(key_bytes) <= 16 else 24 if len(key_bytes) <= 24 else 32
                key_bytes = key_bytes[:size].ljust(size, b"\0")
            else:
                key_bytes = key_bytes[:8].ljust(8, b"\0")
            cipher = (cipher_type.new(key_bytes, cipher_type.MODE_CBC,
                                      iv=iv_bytes[:block_size].ljust(block_size, b"\0"))
                      if "/CBC" in spec else cipher_type.new(key_bytes, cipher_type.MODE_ECB))
            decoded_value = json.loads(value)
            decrypting = operation.startswith("decrypt")
            if decrypting:
                raw = (cls._byte_values(decoded_value) if isinstance(decoded_value, list)
                       else base64.b64decode(str(decoded_value)))
                output = cipher.decrypt(raw)
                if "PKCS" in spec:
                    output = unpad(output, block_size)
                elif "ZERO" in spec:
                    output = output.rstrip(b"\0")
            else:
                raw = (cls._byte_values(decoded_value) if isinstance(decoded_value, list)
                       else str(decoded_value).encode())
                if "NOPADDING" not in spec:
                    raw = (raw.ljust((len(raw) + block_size - 1) // block_size * block_size, b"\0")
                           if "ZERO" in spec else pad(raw, block_size))
                output = cipher.encrypt(raw)
            if operation.endswith("Bytes"):
                return json.dumps(list(output))
            return (output.decode("utf-8", errors="replace") if decrypting else
                    base64.b64encode(output).decode("ascii"))
        except Exception as exc:
            raise JsBridgeError(f"crypto operation failed: {exc}") from None

    def _ajax(self, request_value: str, source_headers=None, base_url=None) -> str:
        return json.loads(self._request_json(
            request_value, source_headers, base_url))["body"]

    def _open_with_retry(self, request: Request, retry=0, opener=None):
        attempts = max(0, min(10, int(retry or 0))) + 1
        last_error = None
        for attempt in range(attempts):
            try:
                return (opener or self.opener).open(request, timeout=self.timeout)
            except HTTPError:
                raise
            except URLError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
        raise last_error

    def _request_json(self, request_value: str, source_headers=None, base_url=None) -> str:
        from test_book_sources_async import encode_url, parse_request_config, split_post_url
        from app.services.request_compat import escape_form, escape_url_query

        url = request_value
        config = {}
        if re.search(r",\s*\{", request_value):
            parts = split_post_url(request_value)
            if parts:
                url, raw_config = parts
                config = parse_request_config(raw_config) or {}
        method = str(config.get("method", "GET")).upper()
        body = config.get("body")
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        request_charset = str(config.get("charset") or "utf-8")
        if request_charset.lower() == "escape":
            if (body is not None and
                    not str(body).lstrip().startswith(("{", "[", "<"))):
                body = escape_form(str(body))
                request_charset = "ascii"
            else:
                request_charset = "utf-8"
        if request_charset.lower() in {"gbk", "gb2312", "gb_2312-80"}:
            request_charset = "gb18030"
        data = (str(body).encode(request_charset, errors="replace")
                if body is not None else None)
        headers = dict(self.default_headers)
        source_headers = dict(source_headers or {})
        proxy = config.get("proxy")
        for name in list(source_headers):
            if str(name).lower() == "proxy":
                proxy = proxy or source_headers.pop(name)
        headers.update({str(k): str(v) for k, v in source_headers.items()})
        request_headers = config.get("headers") or {}
        if isinstance(request_headers, str):
            try:
                request_headers = json.loads(request_headers)
            except json.JSONDecodeError:
                request_headers = {}
        if isinstance(request_headers, dict):
            headers.update({str(k): str(v) for k, v in request_headers.items()})
        if method == "POST" and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = (
                "application/json; charset=UTF-8" if str(body or "").lstrip().startswith(
                    ("{", "[")) else
                "application/x-www-form-urlencoded; charset=UTF-8")
        url = urljoin(str(base_url if base_url is not None else self.base_url), url.strip())
        if str(config.get("charset") or "").lower() == "escape":
            url = escape_url_query(url)
        request = Request(encode_url(url), data=data, headers=headers, method=method)
        if self.opener is None:
            raise JsBridgeError("JS HTTP runtime has been closed")
        try:
            request_opener = None
            if proxy:
                proxy = str(proxy).strip()
                proxy = proxy if "://" in proxy else "http://" + proxy
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                request_opener = build_opener(
                    HTTPCookieProcessor(self.cookies), HTTPSHandler(context=ssl_context),
                    ProxyHandler({"http": proxy, "https": proxy}))
            with self._open_with_retry(request, config.get("retry", 0), request_opener) as response:
                raw = b"" if method == "HEAD" else response.read()
                charset = config.get("charset") or response.headers.get_content_charset() or "utf-8"
                if str(charset).lower() in {"gbk", "gb2312", "gb_2312-80"}:
                    charset = "gb18030"
                result = {
                    "body": (raw.hex() if config.get("type") not in (None, "") else
                             raw.decode(charset, errors="replace")),
                    "bodyBytes": list(raw),
                    "headers": {str(k).lower(): str(v) for k, v in response.headers.items()},
                    "status": int(getattr(response, "status", None) or 200),
                    "url": response.geturl(),
                }
                return json.dumps(result, ensure_ascii=False)
        except HTTPError as exc:
            try:
                raw = b"" if method == "HEAD" else exc.read()
                charset = config.get("charset")
                if not charset and exc.headers:
                    charset = exc.headers.get_content_charset()
                charset = charset or "utf-8"
                if str(charset).lower() in {"gbk", "gb2312", "gb_2312-80"}:
                    charset = "gb18030"
                result = {
                    "body": (raw.hex() if config.get("type") not in (None, "") else
                             raw.decode(charset, errors="replace")),
                    "bodyBytes": list(raw),
                    "headers": ({str(k).lower(): str(v) for k, v in exc.headers.items()}
                                if exc.headers else {}),
                    "status": int(exc.code),
                    "url": exc.geturl() or url,
                }
                return json.dumps(result, ensure_ascii=False)
            finally:
                exc.close()
        except URLError as exc:
            raise JsBridgeError(f"JS network error: {exc.reason}") from None


def split_js_rule(rule: str) -> tuple[str | None, str]:
    """返回 JS 预处理脚本和其后的普通解析规则。"""
    value = (rule or "").strip()
    if value.startswith("<js>") and "</js>" in value:
        end = value.find("</js>") + 5
        return value[:end], value[end:].strip()
    if value.startswith("@js:"):
        return value, ""
    return None, value
