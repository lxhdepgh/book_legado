"""基于 QuickJS 的轻量 Legado JavaScript 兼容层。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import io
import ipaddress
import gzip
import json
import os
import random
import re
import ssl
import time
import threading
import uuid
import zipfile
import zlib
import tempfile
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, quote_plus, unquote_plus, urljoin, urlsplit
from urllib.request import (HTTPRedirectHandler, HTTPCookieProcessor, HTTPSHandler,
                            ProxyHandler, Request, build_opener)


class JsRuntimeUnavailable(RuntimeError):
    pass


class JsBridgeError(RuntimeError):
    pass


_SSL_CONTEXT_LOCK = threading.Lock()
_SSL_CONTEXT = None


def _unverified_ssl_context():
    """One permissive context per process.

    ``create_default_context`` enumerates the whole Windows certificate store;
    measured at ~7ms per call, which a sweep over hundreds of sources would pay
    hundreds of times over for a context the JS bridge never verifies against.
    """
    global _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            _SSL_CONTEXT = context
        return _SSL_CONTEXT


def classify_js_bridge_error(message: str) -> str:
    """Separate missing engine APIs from source-script/network failures."""
    text = str(message or "")
    lowered = text.lower()
    if "bridge:" in lowered and any(token in lowered for token in (
            "network", "urlerror", "timeout", "ssl", "connection")):
        return "http_error"
    if any(token in text for token in ("暂不支持", "interactive browser",
                                        "verification-code", "未配置网页交互")):
        return "js_needed"
    return "parse_error"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class JsContext:
    source: object
    # The object bound as ``java`` is not one universal Legado API.  Rules use
    # AnalyzeRule, URL expressions use AnalyzeUrl, and BaseSource.evalJS binds
    # the source entity itself.
    host_kind: str = "rule"
    key: str = ""
    page: int = 1
    result: str = ""
    base_url: str = ""
    variables: dict | None = None
    # Whether AnalyzeRule/AnalyzeUrl was constructed with a RuleDataInterface.
    # ``variables`` is its Python variableMap representation.
    rule_data_present: bool | None = None
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
    result_html_nodes: bool = False
    index: int = 0
    g_int: int = 0


class LegadoJsRuntime:
    _PERSIST_LOCK = threading.RLock()
    # The Windows quickjs extension can access-violate when independent
    # Context objects are created/evaluated concurrently in different threads.
    # Network work remains concurrent; only native JS execution is serialized.
    _QUICKJS_LOCK = threading.RLock()
    # QuickJS binds its native runtime data to the first Python thread that
    # enters it.  A lock is not sufficient: sequential calls from different
    # asyncio worker threads still trip ``runtime_data->thread_state`` on
    # Windows.  Keep every Context operation on one process-wide thread.
    _QUICKJS_EXECUTOR = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="legado-quickjs")
    _QUICKJS_THREAD = threading.local()
    _MEMORY_CACHE_LIMIT = 50 * 1024 * 1024

    def __init__(self, timeout: int = 15, state_dir: str | Path | None = None):
        self.timeout = timeout
        self.cookies = CookieJar()
        self._session_persistent_shadow = {}
        self.opener = build_opener(HTTPCookieProcessor(self.cookies),
                                   HTTPSHandler(context=_unverified_ssl_context()))
        self.default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36"
            ),
        }
        self.base_url = ""
        self.cache_store = {}
        self.memory_cache_store = {}
        self._memory_cache_size = 0
        self.state_dir = Path(state_dir) if state_dir else None
        self._temporary_files = None if self.state_dir else tempfile.TemporaryDirectory(
            prefix="legado-js-")
        self.file_root = ((self.state_dir / "external-cache") if self.state_dir else
                          Path(self._temporary_files.name))
        self._archive_temp_initialized = False
        self.persistent_state = self._load_persistent_state()
        # Cache hydration can remove expired entries and persist the cleaned
        # state.  The persistence path includes deviceId, so it must exist
        # before any hydration routine is allowed to write.
        self.device_id = str(
            self.persistent_state.get("deviceId") or uuid.uuid4().hex)
        if self.persistent_state.get("deviceId") != self.device_id:
            self.persistent_state["deviceId"] = self.device_id
            self._save_persistent_state()
        self._hydrate_cookie_state()
        self._hydrate_cache_state()
        self.font_store = {}
        self.bitmap_store = {}
        self.dom_store = {}
        self.reget_book_handler = None
        self.refresh_book_handler = None
        # SharedJsScope keeps the evaluated jsLib scope alive.  A JSON
        # snapshot is insufficient because function closures, object identity
        # and non-enumerable state must survive later rule evaluations.
        self._shared_js_contexts = {}
        self._js_lib_code_cache = {}
        self._thread_state = threading.local()
        self.browser_fetcher = None
        self.browser_starter = None
        self.uri_reader = None
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

    def _hydrate_cookie_state(self) -> None:
        """Restore the persistent CookieManager surface used by JS requests."""
        from app.services.public_suffix import effective_tld_plus_one

        for item in self.persistent_state.get("cookies", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            try:
                raw_domain = str(item.get("domain", "")).lstrip(".")
                cookie_domain = effective_tld_plus_one(raw_domain)
                try:
                    ipaddress.ip_address(cookie_domain)
                    domain_specified = False
                except ValueError:
                    domain_specified = "." in cookie_domain
                stored_domain = f".{cookie_domain}" if domain_specified else cookie_domain
                self.cookies.set_cookie(Cookie(
                    version=0, name=str(item["name"]),
                    value=str(item.get("value", "")), port=None,
                    port_specified=False, domain=stored_domain,
                    domain_specified=domain_specified,
                    domain_initial_dot=domain_specified,
                    path="/", path_specified=True,
                    secure=False, expires=item.get("expires"),
                    discard=False, comment=None, comment_url=None,
                    rest=dict(item.get("rest") or {}), rfc2109=False))
            except (KeyError, TypeError, ValueError):
                continue

    def _persist_cookie_state(self) -> None:
        if self.state_dir is None:
            return
        persistent = [item for item in self.cookies
                      if not bool((item._rest or {}).get("LegadoSession"))]
        by_key = {(str(item.domain).lstrip("."), item.name): item
                  for item in persistent}
        for key, item in self._session_persistent_shadow.items():
            by_key.setdefault(key, item)
        ordered = []
        for old in self.persistent_state.get("cookies", []):
            key = (str(old.get("domain", "")).lstrip("."), str(old.get("name", "")))
            item = by_key.pop(key, None)
            if item is not None:
                ordered.append(item)
        persistent = ordered + list(by_key.values())
        self.persistent_state["cookies"] = [{
            "name": item.name, "value": item.value,
            "domain": item.domain,
            "domainSpecified": item.domain_specified,
            "domainInitialDot": item.domain_initial_dot,
            "path": item.path, "pathSpecified": item.path_specified,
            "secure": item.secure, "expires": item.expires,
            "rest": dict(item._rest or {}),
        } for item in persistent]
        self._save_persistent_state()

    def _hydrate_source_state(self, context: JsContext, source_key: str) -> None:
        values = (self.persistent_state.get("sources", {}).get(source_key, {})
                  if source_key else {})
        for key, value in values.items():
            if key == "source:loginInfo" and value is not None:
                value = self._decrypt_login_info(str(value))
            context.variables.setdefault(str(key), value)

    def _login_info_key(self) -> bytes:
        return self.device_id.encode("utf-8")[:16].ljust(16, b"\0")

    def _encrypt_login_info(self, value: str) -> str:
        from Crypto.Cipher import AES
        raw = str(value).encode("utf-8")
        padding = 16 - len(raw) % 16
        encrypted = AES.new(self._login_info_key(), AES.MODE_ECB).encrypt(
            raw + bytes([padding]) * padding)
        return base64.b64encode(encrypted).decode("ascii")

    def _decrypt_login_info(self, value: str) -> str | None:
        """Decrypt fixed BaseSource state; invalid ciphertext is unreadable."""
        from Crypto.Cipher import AES
        try:
            raw = AES.new(self._login_info_key(), AES.MODE_ECB).decrypt(
                base64.b64decode(value, validate=True))
            padding = raw[-1]
            if padding < 1 or padding > 16 or raw[-padding:] != bytes([padding]) * padding:
                raise ValueError("invalid PKCS5 padding")
            return raw[:-padding].decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            # BaseSource.getLoginInfo catches Hutool/AES failures and returns
            # null; exposing old plaintext would add a non-Legado fallback.
            return None

    def _hydrate_cache_state(self) -> None:
        now = time.time()
        changed = False
        for key, item in list((self.persistent_state.get("cache") or {}).items()):
            if not isinstance(item, dict):
                continue
            expires = float(item.get("expires") or 0)
            if expires and expires <= now:
                self.persistent_state.setdefault("cache", {}).pop(str(key), None)
                changed = True
                continue
            # Disk Cache stores value.toString(); JSON-wrap it only as the
            # transport envelope consumed by the JS bridge.
            self.cache_store[str(key)] = (
                json.dumps(str(item.get("value") or ""), ensure_ascii=False), expires)
        if changed:
            self._persist_cache_state()

    def _persist_cache_state(self) -> None:
        if self.state_dir is None:
            return
        disk_state = self._load_persistent_state()
        disk_state.setdefault("deviceId", self.device_id)
        current = self.persistent_state.get("cache", {})
        disk_state["cache"] = current
        self.persistent_state = disk_state
        self._save_persistent_state()

    def _file_cache_get(self, key) -> str:
        if self.state_dir is None:
            return self._cache_get(key)
        metadata = (self.persistent_state.get("fileCache") or {}).get(str(key), {})
        expires = float(metadata.get("expires") or 0) if isinstance(metadata, dict) else 0
        if expires and expires <= time.time():
            self._file_cache_delete(key)
            return ""
        path = self.state_dir / "rule-cache" / (hashlib.sha256(
            str(key).encode()).hexdigest() + ".json")
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

    def _file_cache_put(self, key, value, ttl=0):
        if self.state_dir is None:
            return self._cache_put(key, value)
        directory = self.state_dir / "rule-cache"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(str(key).encode()).hexdigest() + ".json")
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(str(value), encoding="utf-8")
        temporary.replace(path)
        seconds = max(0.0, float(ttl or 0))
        self.persistent_state.setdefault("fileCache", {})[str(key)] = {
            "expires": time.time() + seconds if seconds else 0}
        self._save_persistent_state()
        return value

    def _file_cache_delete(self, key):
        if self.state_dir is not None:
            path = self.state_dir / "rule-cache" / (hashlib.sha256(
                str(key).encode()).hexdigest() + ".json")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self.persistent_state.setdefault("fileCache", {}).pop(str(key), None)
            self._save_persistent_state()

    def close(self):
        """Release references retained by urllib handlers after a JS check."""
        try:
            self.cookies.clear()
        except KeyError:
            pass
        self.opener = None
        if self._temporary_files is not None:
            self._temporary_files.cleanup()
            self._temporary_files = None

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
        context.host_kind = "url"
        value = self.evaluate(wrapper, context)
        try:
            result = json.loads(value) if isinstance(value, str) else value
        except (TypeError, json.JSONDecodeError):
            result = None
        return result if isinstance(result, dict) else {}

    def evaluate(self, script: str, context: JsContext):
        stripped = str(script or "").strip()
        if stripped.lower().startswith("@webjs:"):
            # BackstageWebView rule mode: the script runs in the rendered page,
            # never in the QuickJS runtime.
            return self._evaluate_webjs(stripped[7:], context)
        if getattr(self._QUICKJS_THREAD, "active", False):
            # A Python callback made by QuickJS may evaluate another rule.
            # Running it inline avoids deadlocking the single worker.
            return self._evaluate_serialized(script, context)
        return self._QUICKJS_EXECUTOR.submit(
            self._evaluate_on_quickjs_thread, script, context).result()

    def _evaluate_on_quickjs_thread(self, script: str, context: JsContext):
        self._QUICKJS_THREAD.active = True
        try:
            return self._evaluate_serialized(script, context)
        finally:
            self._QUICKJS_THREAD.active = False

    def _evaluate_serialized(self, script: str, context: JsContext):
        with self._QUICKJS_LOCK:
            return self._evaluate_locked(script, context)

    def _evaluate_locked(self, script: str, context: JsContext):
        try:
            import quickjs
        except ImportError as exc:
            raise JsRuntimeUnavailable("缺少 quickjs，请执行 pip install quickjs") from exc

        rule_data_present = (context.variables is not None if
                             context.rule_data_present is None else
                             bool(context.rule_data_present))
        context.rule_data_present = rule_data_present
        if context.variables is None:
            context.variables = {}
        source_is_null = context.source is None
        if source_is_null:
            source_data = {}
        elif hasattr(context.source, "to_dict"):
            source_data = context.source.to_dict()
        elif isinstance(context.source, dict):
            source_data = dict(context.source)
        else:
            source_data = {str(name): value for name, value in
                           vars(context.source).items() if not callable(value)}
        for name in getattr(context.source, "__dataclass_fields__", {}):
            source_data.setdefault(name, getattr(context.source, name, None))
        raw_headers = source_data.get("header", {}) or {}
        if isinstance(raw_headers, str):
            try:
                raw_headers = json.loads(raw_headers)
            except json.JSONDecodeError:
                raw_headers = {}
        if not isinstance(raw_headers, dict):
            raw_headers = {}
        evaluation_base = context.base_url or source_data.get("bookSourceUrl", "")
        # JsExtensions uses ``getSource()?.enabledCookieJar == true``:
        # an explicit Kotlin null disables the jar, while an omitted field is
        # the BookSource constructor default (true).
        cookie_enabled = (not source_is_null and
                          source_data.get("enabledCookieJar", True) is True)
        source_key = str(source_data.get("bookSourceUrl") or "")

        raw_js_lib = source_data.get("jsLib", "")
        if context.host_kind == "format":
            raw_js_lib = ""
        if raw_js_lib is None or not str(raw_js_lib).strip():
            raw_js_lib = ""
        js_lib_key = hashlib.md5(str(raw_js_lib).encode()).hexdigest()
        if js_lib_key not in self._js_lib_code_cache:
            self._js_lib_code_cache[js_lib_key] = self._clean_script(
                self._resolve_js_lib(raw_js_lib))
        js_lib = self._js_lib_code_cache[js_lib_key]
        ctx = self._shared_js_contexts.get(js_lib_key) if js_lib else None
        is_new_shared_context = ctx is None
        if ctx is None:
            ctx = quickjs.Context()
            ctx.set_memory_limit(128 * 1024 * 1024)
            if js_lib:
                self._shared_js_contexts[js_lib_key] = ctx
        self._thread_state.bridge_error = ""
        def network_call(method, *args):
            return method(*args, cookie_enabled, source_key)

        def bridge_headers(value=None):
            if not value:
                return {}
            try:
                parsed = json.loads(str(value))
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, json.JSONDecodeError):
                return {}

        def invoke_book_handler(handler, source, book):
            self._bridge_call(handler, source, book)
            data = ({name: getattr(book, name) for name in
                     getattr(book, "__dataclass_fields__", {})}
                    if book is not None else {})
            data["bookUrl"] = data.get("url", "")
            data["tocUrl"] = data.get("toc_url", "")
            data["variable"] = data.get("variables", {})
            return json.dumps(data, ensure_ascii=False, default=str)

        ctx.add_callable("__ajax", lambda value, headers=None: self._bridge_call(
            network_call, self._ajax, value, bridge_headers(headers), evaluation_base))
        ctx.add_callable("__request", lambda value, headers=None: self._bridge_call(
            network_call, self._request_json, value, bridge_headers(headers), evaluation_base))
        ctx.add_callable("__request_mode", lambda value, headers=None, allow_webview=False,
                         webview_js=None, source_regex=None: self._bridge_call(
            self._request_json, value, bridge_headers(headers), evaluation_base,
            cookie_enabled, source_key, bool(allow_webview), webview_js, source_regex))
        ctx.add_callable("__connect", lambda value, headers=None: network_call(
            self._connect_json, value, bridge_headers(headers), evaluation_base))
        ctx.add_callable("__ajax_all", lambda values, headers=None: self._bridge_call(
            network_call, self._ajax_all, values, bridge_headers(headers), evaluation_base))
        ctx.add_callable("__post", lambda url, body="": network_call(self._ajax,
            f'{url},{{"method":"POST","body":{json.dumps(str(body))}}}',
            {}, evaluation_base))
        ctx.add_callable("__md5", lambda value: hashlib.md5(str(value).encode()).hexdigest())
        ctx.add_callable("__digest", self._digest)
        ctx.add_callable("__digest_base64", self._digest_base64)
        ctx.add_callable("__digest_bytes", self._digest_bytes)
        ctx.add_callable("__hmac", lambda value, algorithm, secret, as_base64=False:
                         self._bridge_call(self._hmac, value, algorithm, secret,
                                           as_base64))
        ctx.add_callable("__b64decode", self._base64_decode)
        ctx.add_callable("__b64encode", lambda value: base64.b64encode(str(value).encode()).decode())
        ctx.add_callable("__b64bytes", self._base64_bytes)
        ctx.add_callable("__b64decode_flags", self._base64_decode_flags)
        ctx.add_callable("__b64encode_flags", self._base64_encode_text)
        ctx.add_callable("__hexbytes", self._hex_bytes)
        ctx.add_callable("__bytesstr", self._bytes_to_string)
        ctx.add_callable("__strbytes", self._string_to_bytes)
        ctx.add_callable("__uuid", lambda: str(uuid.uuid4()))
        ctx.add_callable("__random_bytes", lambda count: json.dumps(
            list(os.urandom(max(0, int(count))))))
        ctx.add_callable("__time_format", lambda value, pattern, utc=False:
                         self._bridge_call(self._time_format, value, pattern, utc))
        ctx.add_callable("__tz_offset", self._timezone_offset_minutes)
        ctx.add_callable("__date_string", self._date_to_string)
        ctx.add_callable("__num_chapter", self._to_num_chapter)
        ctx.add_callable("__cache_get", self._cache_get)
        ctx.add_callable("__cache_put", self._cache_put)
        ctx.add_callable("__cache_delete", self._cache_delete)
        ctx.add_callable("__memory_cache_get", self._memory_cache_get)
        ctx.add_callable("__memory_cache_put", self._memory_cache_put)
        ctx.add_callable("__memory_cache_delete", self._memory_cache_delete)
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
        ctx.add_callable("__split_source_rule", lambda value, all_in_one=False,
                         is_json=False, is_regex=False: json.dumps([
                             {"mode": stage.mode, "rule": stage.rule,
                              "raw": stage.raw, "putRule": stage.put_rule}
                             for stage in __import__(
                                 "app.services.source_rule", fromlist=[
                                     "compile_rule_program"]).compile_rule_program(
                                         str(value), bool(all_in_one), bool(is_json),
                                         bool(is_regex)).stages
                         ], ensure_ascii=False))
        ctx.add_callable("__put", lambda name, value: self._bridge_call(
            self._put, context, name, value))
        ctx.add_callable("__java_get", lambda name: self._bridge_call(
            self._java_get, context, name, rule_data_present))
        ctx.add_callable("__java_put", lambda name, value: self._bridge_call(
            self._java_put, context, name, value, rule_data_present))
        ctx.add_callable("__cookie_get", lambda url: self._cookie_string(str(url)))
        ctx.add_callable("__cookie_set", self.store_set_cookie)
        ctx.add_callable("__cookie_replace", self.store_replace_cookie)
        ctx.add_callable("__cookie_remove", self.remove_cookie)
        ctx.add_callable("__cookie_clear", self.store_clear_cookies)
        ctx.add_callable("__crypto", lambda *args: self._bridge_call(
            self._crypto, *args))
        ctx.add_callable("__unsupported", lambda name: self._unsupported(name))
        ctx.add_callable("__css_query", self._css_query)
        ctx.add_callable("__element_op", self._element_operation)
        ctx.add_callable("__dom_parse", self._dom_parse)
        ctx.add_callable("__dom_query", self._dom_query)
        ctx.add_callable("__dom_xpath", self._dom_xpath)
        ctx.add_callable("__dom_op", self._dom_operation)
        ctx.add_callable("__dom_remove", self._dom_remove)
        ctx.add_callable("__dom_parents", self._dom_parents)
        ctx.add_callable("__dom_related", self._dom_related)
        ctx.add_callable("__dom_mutate", self._dom_mutate)
        ctx.add_callable("__dom_matches", self._dom_matches)
        ctx.add_callable("__dom_collect", self._dom_collect)
        ctx.add_callable("__dom_setting", self._dom_setting)
        ctx.add_callable("__dom_move", self._dom_move)
        ctx.add_callable("__dom_contains", self._dom_contains)
        ctx.add_callable("__dom_traverse", self._dom_traverse)
        ctx.add_callable("__dom_form_data", self._dom_form_data)
        ctx.add_callable("__url_resolve", lambda value, base="":
                         urljoin(str(base or ""), str(value or "")))
        ctx.add_callable("__t2s", self._traditional_to_simplified)
        ctx.add_callable("__s2t", self._simplified_to_traditional)
        ctx.add_callable("__html_format", self._html_format)
        ctx.add_callable("__zip_content", lambda value, member, as_bytes=False,
                         charset="", headers="{}": self._bridge_call(
                             self._zip_content, value, member, as_bytes, charset,
                             bridge_headers(headers), evaluation_base,
                             cookie_enabled, source_key))
        ctx.add_callable("__archive_content", lambda archive_type, value, member,
                         as_bytes=False, charset="", headers="{}":
                         self._bridge_call(
                             self._archive_content, archive_type, value, member,
                             as_bytes, charset, bridge_headers(headers),
                             evaluation_base, cookie_enabled, source_key))
        ctx.add_callable("__file_path", lambda value: str(self._file_path(value)))
        ctx.add_callable("__file_read", self._file_read)
        ctx.add_callable("__file_delete", self._file_delete)
        ctx.add_callable("__download_file", lambda value, url="", headers="{}":
                         self._download_file(
                             value, url, json.loads(headers or "{}"),
                             evaluation_base, cookie_enabled))
        ctx.add_callable("__cache_file", lambda url, save_time=0, headers="{}":
                         self._cache_file(
                             url, save_time, json.loads(headers or "{}"),
                             evaluation_base, cookie_enabled))
        ctx.add_callable("__import_script", lambda path, headers="{}":
                         self._import_script(
                             path, json.loads(headers or "{}"),
                             evaluation_base, cookie_enabled))
        ctx.add_callable("__unarchive_file", self._unarchive_file)
        ctx.add_callable("__text_in_folder", self._text_in_folder)
        ctx.add_callable("__font_query", self._query_font)
        ctx.add_callable("__font_replace", self._replace_font)
        ctx.add_callable("__font_op", self._font_operation)
        ctx.add_callable("__gzip", self._gzip)
        ctx.add_callable("__inflate", self._inflate)
        ctx.add_callable("__md5bytes", lambda value: json.dumps(list(
            hashlib.md5(str(value).encode()).digest())))
        ctx.add_callable("__b64encode_bytes", self._base64_encode_bytes)
        ctx.add_callable("__bitmap_decode", self._bitmap_decode)
        ctx.add_callable("__bitmap_info", self._bitmap_info)
        ctx.add_callable("__bitmap_pixel", self._bitmap_pixel)
        ctx.add_callable("__bitmap_crop", self._bitmap_crop)
        ctx.add_callable("__bitmap_create", self._bitmap_create)
        ctx.add_callable("__bitmap_draw", self._bitmap_draw)
        ctx.add_callable("__bitmap_encode", self._bitmap_encode)
        ctx.add_callable("__browser", self._browser)
        ctx.add_callable("__webview", lambda html_value, url, js, source_regex,
                         override_regex, headers=None: self._webview(
                             html_value, url, js, source_regex, override_regex,
                             cookie_tag=source_key,
                             header_map=bridge_headers(headers)))
        ctx.add_callable("__browser_start", self._start_browser)
        ctx.add_callable("__verification", self._verification_code)
        ctx.add_callable("__to_url", self._to_url)
        ctx.add_callable("__encode_uri", self._encode_uri)
        ctx.add_callable("__utf8_to_gbk", self._utf8_to_gbk)
        ctx.add_callable("__asym_generate", self._asymmetric_generate)
        ctx.add_callable("__asym_op", self._asymmetric_operation)
        ctx.add_callable("__sign_generate", self._sign_generate)
        ctx.add_callable("__sign_op", self._sign_operation)
        ctx.add_callable("__sym_generate", self._symmetric_generate)
        ctx.add_callable("__sleep", lambda milliseconds: time.sleep(float(milliseconds) / 1000))
        ctx.add_callable("__reget_book", lambda: invoke_book_handler(
            self.reget_book_handler, context.source, context.book)
                         if self.reget_book_handler is not None else "{}")
        ctx.add_callable("__refresh_book", lambda: invoke_book_handler(
            self.refresh_book_handler, context.source, context.book)
                         if self.refresh_book_handler is not None else "{}")

        self._thread_state.dom_store = {}
        self._thread_state.dom_reverse = {}
        self._hydrate_source_state(context, str(source_data.get("bookSourceUrl", "")))
        for prefix, target in (("book:", context.book),
                               ("chapter:", context.chapter)):
            variables = self._object_variables(target)
            if variables is not None:
                for name, value in variables.items():
                    context.variables.setdefault(prefix + str(name), value)
        if isinstance(raw_headers, dict):
            raw_headers = dict(raw_headers)
            if isinstance(context.request_headers, dict):
                raw_headers.update({str(k): str(v)
                                    for k, v in context.request_headers.items()})
        self.base_url = context.base_url or source_data.get("bookSourceUrl", "")
        request_method = "GET"
        if context.host_kind == "url":
            try:
                from app.services.legado_url import parse_request_config, split_post_url
                from app.services.request_compat import normalize_url_option
                parts = split_post_url(str(evaluation_base))
                if parts:
                    request_method = str(normalize_url_option(
                        parse_request_config(parts[1])).get("method") or "GET")
            except Exception:
                request_method = "GET"
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
                "latestChapterTitle": "latest_chapter_title",
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
            chapter_data.setdefault("startFragmentId", chapter_data.get("start_fragment_id"))
            chapter_data.setdefault("endFragmentId", chapter_data.get("end_fragment_id"))
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
        const __global = globalThis;
        const __hostKind = {json.dumps(context.host_kind)};
        var key = {json.dumps(context.key)};
        var page = {int(context.page)};
        var result = {result_expression};
        var src = {src_expression};
        const __resultHtmlNodes = {str(bool(context.result_html_nodes)).lower()};
        var baseUrl = {json.dumps(context.base_url or source_data.get('bookSourceUrl', ''))};
        const __initialAnalyzeUrl = baseUrl;
        var title = {json.dumps(context.title or chapter_data.get('title') or chapter_data.get('name') or '')};
        var nextChapterUrl = {json.dumps(context.next_chapter_url or '')};
        var speakText = {json.dumps(context.speak_text or '')};
        var speakSpeed = {int(context.speak_speed or 0)};
        var index = {int(context.index or 0)};
        var gInt = {int(context.g_int or 0)};
        var __contentChanged = false;
        var __currentContent = null;
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
        var __RhinoNativeError = (typeof __RhinoNativeError === 'undefined')
          ? Error : __RhinoNativeError;
        var __RhinoError = (typeof __RhinoError === 'undefined') ? (function(message) {{
          const error = new __RhinoNativeError(message);
          try {{ Object.defineProperty(error,'stack',{{value:undefined,
            writable:true,configurable:true,enumerable:false}}); }} catch (_) {{}}
          Object.setPrototypeOf(error, __RhinoError.prototype);
          return error;
        }}) : __RhinoError;
        if (!Object.prototype.hasOwnProperty.call(__RhinoError.prototype,'__rhino')) {{
          __RhinoError.prototype = Object.create(__RhinoNativeError.prototype);
          Object.defineProperty(__RhinoError.prototype, 'constructor',
            {{value:__RhinoError,writable:true,configurable:true}});
          Object.defineProperty(__RhinoError.prototype, '__rhino', {{value:true}});
          __RhinoError.prototype.name = 'Error';
          Object.defineProperty(__RhinoError, Symbol.hasInstance, {{
            value:instance=>instance instanceof __RhinoNativeError,
            configurable:true
          }});
        }}
        Error = __RhinoError;
        const __sourceData = {json.dumps(source_data, ensure_ascii=False)};
        const __staticSourceHeaders = {json.dumps(
            raw_headers if isinstance(raw_headers, dict) else {}, ensure_ascii=False)};
        const __requestHeaderData = {json.dumps(
            context.request_headers if isinstance(context.request_headers, dict) else {},
            ensure_ascii=False)};
        const __requestMethod = {json.dumps(request_method)};
        const __bookData = {json.dumps(book_data, ensure_ascii=False)};
        const __chapterData = {json.dumps(chapter_data, ensure_ascii=False)};
        var book = Object.assign({{}}, __bookData, {{
          getVariable: function(name) {{
            if (arguments.length!==1) throw new TypeError("Can't find method BaseBook.getVariable");
            return __get('book:' + __nonnullString(name,'key')) || '';
          }},
          putVariable: function(name,value) {{
            if (arguments.length!==2) throw new TypeError("Can't find method BaseBook.putVariable");
            __put('book:' + __nonnullString(name,'key'), value===null?null:String(value));
            return true;
          }},
          getCustomVariable: () => __get('book:custom') || '',
          putCustomVariable: function(value) {{
            if (arguments.length!==1) throw new TypeError("Can't find method BaseBook.putCustomVariable");
            __put('book:custom',value===null?null:String(value));
          }},
          setReverseToc: value => {{
            if (!book.readConfig || typeof book.readConfig!=='object') book.readConfig={{}};
            book.readConfig.reverseToc=Boolean(value);
          }},
          getReverseToc: () => Boolean((book.readConfig || {{}}).reverseToc),
          getKindList: () => [book.wordCount].concat(String(book.kind || '').split(/[,\\n]/))
            .map(value=>String(value || '').trim()).filter(Boolean),
          getDisplayCover: () => book.customCoverUrl || book.coverUrl || '',
          getDisplayIntro: () => book.customIntro || book.intro || '',
          getUnreadChapterNum: () => Math.max(Number(book.totalChapterNum || 0)-Number(book.durChapterIndex || 0)-1,0)
        }});
        var chapter = Object.assign({{}}, __chapterData, {{
          getVariable: function(name) {{
            if (arguments.length!==1) throw new TypeError("Can't find method BookChapter.getVariable");
            return __get('chapter:' + __nonnullString(name,'key')) || '';
          }},
          putVariable: function(name,value) {{
            if (arguments.length!==2) throw new TypeError("Can't find method BookChapter.putVariable");
            __put('chapter:' + __nonnullString(name,'key'),value===null?null:String(value));
            return true;
          }},
          isVip: () => Boolean(__chapterData.isVip || __chapterData.is_vip)
        }});
        if (__hostKind === 'url') {{
          chapter=undefined;title=undefined;src=undefined;nextChapterUrl=undefined;
        }} else {{
          key=undefined;page=undefined;speakText=undefined;speakSpeed=undefined;
        }}
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
        function __javaObject(value) {{
          if (!value || typeof value !== 'object') return value;
          if (Array.isArray(value)) {{
            value.forEach(__javaObject);
            if (value.some(item => item && typeof item === 'object')) {{
              Object.defineProperty(value, 'toString', {{enumerable:false,value:function(){{
                return JSON.stringify(this);
              }}}});
            }}
            return value;
          }}
          Object.keys(value).forEach(key => __javaObject(value[key]));
          if (Object.getPrototypeOf(value) === Object.prototype &&
              !Object.prototype.hasOwnProperty.call(value, 'toString')) {{
            Object.defineProperty(value, 'toString', {{enumerable:false,value:function(){{
              return '{{' + Object.keys(this).map(key =>
                String(key) + '=' + String(this[key])).join(', ') + '}}';
            }}}});
          }}
          return value;
        }}
        result = __javaObject(result);
        src = __javaObject(src);
        function __signedByte(value) {{
          const number=Number(value)&255;
          return number > 127 ? number - 256 : number;
        }}
        function __byteArray(value) {{
          return Array.from(value || []).map(item => __signedByte(item));
        }}
        function __isLatin1(charset) {{
          return String(charset || '').replace(/[-_\\s]/g,'').toLowerCase() === 'iso88591' ||
            String(charset || '').replace(/[-_\\s]/g,'').toLowerCase() === 'latin1';
        }}
        function __latin1BytesToString(value) {{
          let output='';
          for (const item of Array.from(value || []))
            output += String.fromCharCode(Number(item) & 255);
          return output;
        }}
        function __latin1StringToBytes(value) {{
          const text=String(value), output=[];
          for (let index=0;index<text.length;index++) output.push(text.charCodeAt(index)&255);
          return __byteArray(output);
        }}
        function __responseCookies(headers) {{
          const raw=String((headers||{{}})['set-cookie']||'');const result={{}};
          raw.split(/,(?=[^;,=]+=[^;,]+)/).forEach(cookie=>{{
            const first=cookie.split(';',1)[0],at=first.indexOf('=');
            if(at>0)result[first.slice(0,at).trim()]=first.slice(at+1).trim();
          }});return __map(result);
        }}
        function __stringMap(value) {{
          if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
          const result={{}};
          Object.keys(value).forEach(key=>{{
            const item=value[key];
            result[key]=item == null ? null :
              (typeof item === 'object' ? JSON.stringify(item) : String(item));
          }});
          return __map(result);
        }}
        function __javaStringHash(value) {{
          let hash=0;for(const char of String(value)){{
            const code=char.codePointAt(0);
            if(code>0xffff){{
              const n=code-0x10000;
              hash=Math.imul(31,hash)+(0xd800+(n>>10));hash|=0;
              hash=Math.imul(31,hash)+(0xdc00+(n&1023));hash|=0;
            }} else {{hash=Math.imul(31,hash)+code;hash|=0;}}
          }}return hash;
        }}
        function __nonnullString(value,name) {{
          if (value === null) throw __wrappedJavaException(
            new __NullPointerException('Parameter specified as non-null is null: '+name));
          return String(value);
        }}
        function __sourceArity(name,actual,expected) {{
          if (actual !== expected)
            throw new TypeError("Can't find method BaseSource."+name+" with "+actual+" arguments");
        }}
        var source = Object.assign({{}}, __sourceData, {{
          getTag: () => __sourceData.bookSourceName || '',
          getKey: () => __sourceData.bookSourceUrl || '',
          getSource: () => source,
          getHeaderMap: hasLoginHeader => {{
            const headers=__resolvedSourceHeaders();
            if (hasLoginHeader) {{
              const login=source.getLoginHeaderMap();
              if (login) Object.assign(headers,login);
            }}
            return __map(headers);
          }},
          getVariable: () => __get('sourceVariable') || '',
          setVariable: function(value) {{
            __sourceArity('setVariable',arguments.length,1);
            __put('sourceVariable', value === null ? null : String(value));
          }},
          put: function(name,value) {{
            __sourceArity('put',arguments.length,2);
            const key=__nonnullString(name,'key'),text=__nonnullString(value,'value');
            __put('source:data:'+key,text);return text;
          }},
          get: function(name) {{
            __sourceArity('get',arguments.length,1);
            return __get('source:data:'+__nonnullString(name,'key')) || '';
          }},
          getLoginHeader: () => __get('source:loginHeader'),
          putLoginHeader: function(value) {{
            __sourceArity('putLoginHeader',arguments.length,1);
            const text=__nonnullString(value,'header');
            try {{
              const headers=JSON.parse(text);
              const value=headers.Cookie == null ? headers.cookie : headers.Cookie;
              if (value != null) cookie.replaceCookie(source.getKey(),String(value));
            }} catch (_) {{}}
            __put('source:loginHeader',text);
          }},
          removeLoginHeader: () => {{
            __put('source:loginHeader',null);cookie.removeCookie(source.getKey());
          }},
          getLoginHeaderMap: () => {{
            const value=__get('source:loginHeader');
            if (value == null) return null;
            try {{ return __stringMap(typeof value === 'string' ? JSON.parse(value) : value); }}
            catch (_) {{ return null; }}
          }},
          getLoginInfo: () => __get('source:loginInfo'),
          putLoginInfo: function(value) {{
            __sourceArity('putLoginInfo',arguments.length,1);
            const text=__nonnullString(value,'info');
            try {{__put('source:loginInfo',text);return true;}}
            catch (_) {{return false;}}
          }},
          removeLoginInfo: () => {{__put('source:loginInfo',null);}},
          getLoginInfoMap: () => {{
            const value=__get('source:loginInfo');
            if (value == null) return null;
            try {{ return __stringMap(typeof value === 'string' ? JSON.parse(value) : value); }}
            catch (_) {{ return null; }}
          }},
          loginUi: () => {{
            try {{const value=JSON.parse(String(__sourceData.loginUi || ''));return Array.isArray(value)?value:null;}}
            catch (_) {{return null;}}
          }},
          getLoginJs: () => {{
            const value=__sourceData.loginUrl;
            if (value == null) return null;
            const text=String(value);
            if (text.startsWith('@js:')) return text.slice(4);
            if (text.startsWith('<js>')) {{
              const end=text.lastIndexOf('<');
              if (end < 4) throw new RangeError('substring range');
              return text.slice(4,end);
            }}
            return text;
          }},
          login: function() {{
            const script=source.getLoginJs();
            if (script == null || String(script).trim() === '') return;
            const run=function() {{
              eval(String(script));
              if (typeof login === 'function') login.apply(this);
              else throw 'Function login not implements!!!';
            }};
            return run.call(__global);
          }},
          evalJS: value => (function(java,source,baseUrl,cookie,cache,key,page,
              result,src,book,chapter,title,nextChapterUrl,speakText,speakSpeed) {{
            return eval(String(value));
          }})(source,source,source.getKey(),cookie,cache,undefined,undefined,
             undefined,undefined,undefined,undefined,undefined,undefined,
             undefined,undefined),
          getShareScope: () => (__sourceData.jsLib != null &&
            String(__sourceData.jsLib).trim() !== '') ? __global : null,
          hashCode: () => __javaStringHash(source.bookSourceUrl || ''),
          equals: other => !!other && String(other.bookSourceUrl || '') === String(source.bookSourceUrl || ''),
          getSearchRule: () => source.ruleSearch || (source.ruleSearch={{}}),
          getExploreRule: () => source.ruleExplore || (source.ruleExplore={{}}),
          getBookInfoRule: () => source.ruleBookInfo || (source.ruleBookInfo={{}}),
          getTocRule: () => source.ruleToc || (source.ruleToc={{}}),
          getContentRule: () => source.ruleContent || (source.ruleContent={{}}),
          getReviewRule: () => source.ruleReview || (source.ruleReview={{}}),
          getDisPlayNameGroup: () => String(source.bookSourceGroup || '').trim()
            ? String(source.bookSourceName || '')+' ('+String(source.bookSourceGroup)+')'
            : String(source.bookSourceName || ''),
          addGroup: function(groups) {{
            __sourceArity('addGroup',arguments.length,1);
            const incoming=__nonnullString(groups,'groups');
            if (source.bookSourceGroup !== null && source.bookSourceGroup !== undefined) {{
              const values=__sourceGroups(source.bookSourceGroup);
              __sourceGroups(incoming).forEach(value=>values.add(value));
              source.bookSourceGroup=Array.from(values).join(',');
            }}
            if (!String(source.bookSourceGroup||'').trim()) source.bookSourceGroup=incoming;
            return source;
          }},
          removeGroup: function(groups) {{
            __sourceArity('removeGroup',arguments.length,1);
            if (source.bookSourceGroup === null || source.bookSourceGroup === undefined)
              return source;
            const removed=__sourceGroups(__nonnullString(groups,'groups'));
            const values=Array.from(__sourceGroups(source.bookSourceGroup))
              .filter(value=>!removed.has(value));
            source.bookSourceGroup=values.join(',');return source;
          }},
          hasGroup: function(group) {{
            __sourceArity('hasGroup',arguments.length,1);
            return __sourceGroups(source.bookSourceGroup)
              .has(__nonnullString(group,'group'));
          }},
          getInvalidGroupNames: () => Array.from(__sourceGroups(source.bookSourceGroup))
            .filter(value=>value.includes('失效')||value==='校验超时').join(', '),
          removeInvalidGroups: () => {{source.removeGroup(source.getInvalidGroupNames());}},
          getDisplayVariableComment: function(otherComment) {{
            __sourceArity('getDisplayVariableComment',arguments.length,1);
            const other=__nonnullString(otherComment,'otherComment');
            return String(source.variableComment||'').trim()
              ? String(source.variableComment)+'\\n'+other : other;
          }},
          equal: other => {{
            if (!other || typeof other !== 'object') return false;
            const texts=['bookSourceName','bookSourceUrl','bookSourceGroup',
              'bookUrlPattern','bookSourceComment','concurrentRate','header',
              'loginUrl','loginUi','loginCheckJs','coverDecodeJs','exploreUrl','searchUrl'];
            if (texts.some(key=>!__sourceTextEqual(source[key],other[key]))) return false;
            const scalars=['bookSourceType','customOrder','enabled','enabledExplore','enabledCookieJar'];
            if (scalars.some(key=>source[key]!==other[key])) return false;
            return ['ruleSearch','ruleExplore','ruleBookInfo','ruleToc','ruleContent']
              .every(key=>__stableJson(source[key]||{{}})===__stableJson(other[key]||{{}}));
          }},
          key: __sourceData.bookSourceUrl || ''
        }});
        function __sourceGroups(value) {{
          return new Set(String(value == null?'':value).split(/[,;，；]/)
            .map(item=>item.trim()).filter(Boolean));
        }}
        function __sourceTextEqual(left,right) {{
          return left===right || ((left==null||left==='')&&(right==null||right===''));
        }}
        function __stableJson(value) {{
          if (Array.isArray(value)) return '['+value.map(__stableJson).join(',')+']';
          if (value && typeof value==='object') return '{{'+Object.keys(value).sort()
            .filter(key=>value[key]!==undefined).map(key=>JSON.stringify(key)+':'+__stableJson(value[key]))
            .join(',')+'}}';
          return JSON.stringify(value);
        }}
        source.tag=source.getTag();
        // Rhino can resolve the Kotlin loginUi property as text and the
        // loginUi() method as a call.  A callable value with source-text
        // coercion preserves both observable forms in JavaScript.
        const __loginUiRaw=String(__sourceData.loginUi || '');
        function __gsonString(value) {{
          if (value == null) return null;
          if (typeof value === 'boolean') return value ? 'true' : 'false';
          if (typeof value === 'object') return JSON.stringify(value);
          return String(value);
        }}
        source.loginUi=function(){{
          try {{
            const value=JSON.parse(__loginUiRaw);
            if (!Array.isArray(value)) return null;
            return value.map(item=>{{
              if (!item || typeof item !== 'object' || Array.isArray(item))
                throw new TypeError('Expected BEGIN_OBJECT');
              const row={{name:null,type:null,action:null}};
              ['name','type','action'].forEach(key=>{{
                if (Object.prototype.hasOwnProperty.call(item,key))
                  row[key]=__gsonString(item[key]);
              }});
              return row;
            }});
          }} catch (_) {{return null;}}
        }};
        source.loginUi.toString=()=>__loginUiRaw;
        source.loginUi.valueOf=()=>__loginUiRaw;
        function __resolvedSourceHeaders() {{
          let headers=Object.assign({{}},__staticSourceHeaders);
          const declaration=__sourceData.header;
          if (typeof declaration === 'string') {{
            let script=declaration,dynamic=false;
            if (/^@js:/i.test(script)) {{script=script.slice(4);dynamic=true;}}
            else if (/^<js>/i.test(script)) {{
              const end=script.lastIndexOf('<');
              if (end < 4) throw new RangeError('substring range');
              script=script.slice(4,end);dynamic=true;
            }}
            if (dynamic) {{
              const previousBaseUrl=baseUrl;
              baseUrl=String(source.getKey());
              let evaluated;
              try {{evaluated=String(eval(script));}}
              finally {{baseUrl=previousBaseUrl;}}
              try {{
                const value=JSON.parse(evaluated);
                if (value && typeof value === 'object' && !Array.isArray(value))
                  headers=Object.assign({{}},value);
              }} catch (_) {{headers={{}};}}
            }}
          }}
          if (!Object.keys(headers).some(key=>key.toLowerCase()==='user-agent'))
            headers['User-Agent']={json.dumps(self.default_headers.get('User-Agent', ''), ensure_ascii=False)};
          return __stringMap(headers) || __map({{}});
        }}
        function __response(raw) {{
          const data = JSON.parse(raw);
          const kind=String(data.responseKind || 'str');
          const multi=data.headersMulti || {{}};
          const headerValues=name=>{{
            const key=String(name).toLowerCase();
            return Array.isArray(multi[key]) ? multi[key].map(String) :
              (data.headers[key] == null ? [] : [String(data.headers[key])]);
          }};
          const headerData=__map(Object.assign({{}},data.headers || {{}}));
          headerData.get=name=>{{const values=headerValues(name);return values.length?values[values.length-1]:null;}};
          headerData.values=name=>headerValues(name);
          const headers=new Proxy(headerData,{{get:(target,name)=>{{
            if(typeof name==='symbol'||Object.prototype.hasOwnProperty.call(target,name))return target[name];
            const key=Object.keys(target).find(item=>item.toLowerCase()===String(name).toLowerCase());
            return key===undefined?undefined:target[key];
          }},has:(target,name)=>Object.keys(target).some(
            item=>item.toLowerCase()===String(name).toLowerCase())}});
          const response={{
            body: () => data.body,
            code: () => data.status,
            message: () => String(data.message || ''),
            headers: () => headers,
            isSuccessful: () => data.status >= 200 && data.status < 300,
            errorBody: () => null,
            url: () => data.url,
            raw: () => ({{
              code: () => data.status,
              message: () => String(data.message || ''),
              headers: () => headers,
              request: () => ({{url: () => data.url}})
            }}),
            toString: () => 'Response{{protocol='+String(data.protocol || 'http/1.1')+
              ', code='+Number(data.status)+
              ', message='+String(data.message || '')+', url='+String(data.url)+'}}'
          }};
          if(kind==='jsoup'){{
            response.bodyAsBytes=()=>__byteArray(Array.isArray(data.bodyBytes) ? data.bodyBytes :
              JSON.parse(__strbytes(String(data.body || ''), 'utf-8')));
            response.header=name=>{{const values=headerValues(name);return values.length?values[values.length-1]:'';}};
            response.headers=name=>name===undefined?headers:headerValues(name);
            response.status=()=>data.status;
            response.statusCode=()=>data.status;
            response.statusMessage=()=>String(data.message || '');
            response.cookies=()=>__responseCookies(Object.assign({{}},data.headers,{{
              'set-cookie':headerValues('set-cookie').join(',')
            }}));
          }}
          if(kind==='okhttp'){{
            response.header=(name,defaultValue)=>{{const value=headers.get(name);
              return value===null?(defaultValue===undefined?null:defaultValue):value;}};
            const bodyObject={{
              bytes:()=>__byteArray(Array.isArray(data.bodyBytes)?data.bodyBytes:[]),
              string:()=>String(data.body==null?'':data.body),
              byteStream:()=>__inputStream(Array.isArray(data.bodyBytes)?data.bodyBytes:[]),
              contentType:()=>headers.get('content-type'),
              contentLength:()=>Array.isArray(data.bodyBytes)?data.bodyBytes.length:-1,
              close:()=>undefined
            }};
            response.body=()=>bodyObject;
            response.close=()=>undefined;
            response.request=()=>({{url:()=>data.url}});
            response.protocol=()=>String(data.protocol || 'http/1.1');
            delete response.errorBody;delete response.raw;
          }}
          return response;
        }}
        function __responseKind(raw,kind){{const data=JSON.parse(raw);
          data.responseKind=kind;return JSON.stringify(data);}}
        function __responseBytes(raw) {{
          const data=JSON.parse(raw);
          return __byteArray(Array.isArray(data.bodyBytes) ? data.bodyBytes :
            JSON.parse(__strbytes(String(data.body || ''), 'utf-8')));
        }}
        function __analyzeUrlDataBytes() {{
          const target=String(java.ruleUrl||java.url||baseUrl).split('?',1)[0];
          const match=/data:.*?;base64,(.*)/.exec(target);
          return match ? __byteArray(JSON.parse(__b64bytes(match[1]))) : null;
        }}
        function __inputStream(bytes) {{
          const data=Array.from(bytes || []).map(value => Number(value) & 255);
          let position=0,markPosition=0;
          return {{
            read: (buffer,offset,length) => {{
              if (buffer === undefined) return position < data.length ? data[position++] : -1;
              if (position >= data.length) return -1;
              const start=offset===undefined?0:Math.max(0,Number(offset)||0);
              const requested=length===undefined?buffer.length-start:Math.max(0,Number(length)||0);
              if(start>buffer.length||start+requested>buffer.length)
                throw new __IndexOutOfBoundsException('buffer range');
              if(requested===0)return 0;
              const count=Math.min(requested, data.length-position);
              for (let i=0;i<count;i++) buffer[start+i]=__byteArray([data[position++]])[0];
              return count;
            }},
            readAllBytes: () => {{const result=__byteArray(data.slice(position));position=data.length;return result;}},
            readNBytes: (target,offset,length) => {{
              if(typeof target==='number'){{
                const count=Math.min(Math.max(0,Number(target)||0),data.length-position);
                const result=__byteArray(data.slice(position,position+count));position+=count;return result;
              }}
              if(target===undefined)return __byteArray([]);
              const count=Math.min(Math.max(0,Number(length)||0),data.length-position);
              if(Number(offset)<0||Number(offset)+count>target.length)
                throw new __IndexOutOfBoundsException('buffer range');
              for(let i=0;i<count;i++)target[Number(offset)+i]=__byteArray([data[position++]])[0];
              return count;
            }},
            available: () => data.length-position,
            skip: count => {{
              const skipped=Math.min(Math.max(0,Number(count)||0),data.length-position);
              position+=skipped; return skipped;
            }},
            skipNBytes: count => {{
              const requested=Math.max(0,Number(count)||0),skipped=Math.min(requested,data.length-position);
              position+=skipped;if(skipped<requested)throw new __EOFException();
            }},
            mark: () => {{markPosition=position;}},
            markSupported: () => true,
            reset: () => {{ position=markPosition; }},
            close: () => undefined
          }};
        }}
        function __http(method, url, body, headers, followRedirects) {{
          return __response(__request(String(url) + ',' + JSON.stringify({{
            method: method, body: body == null ? '' : String(body),
            headers: headers || {{}}, __followRedirects: followRedirects !== false,
            __rawJsoupRequest: true
          }})));
        }}
        function __element(value) {{
          const markup = String(value == null ? '' : value);
          return {{
            __markup: markup,
            text: () => __element_op(markup, 'text', ''),
            ownText: () => __element_op(markup, 'ownText', ''),
            html: () => __element_op(markup, 'html', ''),
            outerHtml: () => __element_op(markup, 'outerHtml', ''),
            attr: name => __element_op(markup, 'attr', String(name)),
            hasAttr: name => Boolean(__element_op(markup, 'hasAttr', String(name))),
            hasClass: name => String(__element_op(markup, 'attr', 'class')).split(/\\s+/).includes(String(name)),
            data: () => __element_op(markup, 'data', ''),
            tagName: () => __element_op(markup, 'tagName', ''),
            select: rule => __elements(markup, String(rule)),
            toString: () => markup,
            valueOf: () => markup,
            toJSON: () => markup
          }};
        }}
        function __jxNode(data) {{
          const kind=String(data.__jxType||'string'),raw=data.value;
          const stringValue=()=>raw == null ? '' : String(raw);
          const node={{
            isElement:()=>kind==='element',isString:()=>kind==='string',
            isNumber:()=>kind==='number',isBoolean:()=>kind==='boolean',
            isDate:()=>kind==='date',asString:stringValue,toString:stringValue,
            asDouble:()=>Number(raw),asLong:()=>Number(raw),
            asBoolean:()=>Boolean(raw),
            asDate:()=>new Date(Number(raw)),
            asElement:()=>kind==='element'?__element(stringValue()):null,
            value:()=>kind==='element'?__element(stringValue()):
              kind==='date'?new Date(Number(raw)):raw,
            sel:rule=>kind==='element'?java.getElements(String(rule),stringValue()):null,
            selOne:rule=>{{const values=kind==='element'?java.getElements(String(rule),stringValue()):null;
              return values&&values.length?values[0]:null;}}
          }};
          return node;
        }}
        function __ruleElementList(values) {{
          const items=Array.from(values||[]);
          if(items.some(value=>value&&typeof value==='object'&&value.__domHandle))
            return __domNodes(items.map(value=>value.__domHandle));
          return items.some(value=>value&&typeof value==='object'&&value.__jxType)
            ? items.map(value=>__jxNode(value)) : __elementList(items);
        }}
        function __elements(html, selector) {{
          const query = String(selector || '');
          const values = operation => JSON.parse(__css_query(String(html), query, operation, ''));
          const api = {{
            text: () => values('text').join(' '),
            html: () => values('html').join(''),
            outerHtml: () => values('outerHtml').join(''),
            attr: name => JSON.parse(__css_query(String(html), query, 'attr', String(name)))[0] || '',
            hasClass: name => JSON.parse(__css_query(
              String(html), query, 'attr', 'class')).some(value =>
                String(value).split(/\\s+/).includes(String(name))),
            size: () => values('outerHtml').length,
            isEmpty: () => values('outerHtml').length === 0,
            first: () => __element(values('outerHtml')[0] || ''),
            last: () => __element(values('outerHtml').slice(-1)[0] || ''),
            get: index => __element(values('outerHtml')[Number(index)] || ''),
            eq: index => __elements(
              values('outerHtml')[Number(index)] || '', '*'),
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
          values.hasClass = name => values.some(value => value.hasClass(name));
          values.first = () => values.length ? values[0] : null;
          values.get = index => values[Number(index)] || null;
          values.eq = index => __elementList(
            values[Number(index)] ? [values[Number(index)].outerHtml()] : []);
          values.select = child => {{
            const selected = __elements(
              values.map(value => value.outerHtml()).join(''), String(child));
            return __elementList(Array.from({{length:selected.length}},
              (_unused,index) => selected[index].outerHtml()));
          }};
          values.size = () => values.length;
          values.isEmpty = () => values.length === 0;
          values.toArray = () => Array.from(values);
          // Rhino exposes Java List/Elements methods through the prototype;
          // `for (i in elements)` therefore visits record indexes only.
          // Direct assignments above are enumerable in QuickJS, so hide them.
          ['text','html','outerHtml','attr','hasClass','first','get','eq',
           'select','size','isEmpty','toArray'].forEach(name =>
            Object.defineProperty(values,name,{{value:values[name],writable:true,
              configurable:true,enumerable:false}}));
          return values;
        }}
        function __javaStringNodes(values, kind) {{
          const nodes=JSON.parse(values).map(value=>({{
            getWholeText:()=>String(value),getWholeData:()=>String(value),
            text:()=>String(value),getData:()=>String(value),
            toString:()=>String(value),valueOf:()=>String(value)
          }}));
          nodes.size=()=>nodes.length;nodes.isEmpty=()=>nodes.length===0;
          nodes.get=index=>nodes[Number(index)]||null;nodes.toArray=()=>Array.from(nodes);
          return nodes;
        }}
        function __attributes(raw, handle) {{
          const values=__map(JSON.parse(raw));
          values.hasKey=key=>values.containsKey(String(key));
          values.getIgnoreCase=key=>{{const found=Object.keys(values).find(
            item=>item.toLowerCase()===String(key).toLowerCase());return found===undefined?'':values[found]}};
          values.hasKeyIgnoreCase=key=>Object.keys(values).some(
            item=>item.toLowerCase()===String(key).toLowerCase());
          values.asList=()=>Object.keys(values).map(key=>({{
            getKey:()=>key,getValue:()=>values[key],toString:()=>key+'="'+values[key]+'"'
          }}));
          return new Proxy(values,{{
            set:(target,key,value)=>{{target[key]=value;if(typeof key==='string')
              __dom_mutate(handle,'attr',key,String(value));return true}},
            deleteProperty:(target,key)=>{{delete target[key];if(typeof key==='string')
              __dom_mutate(handle,'removeAttr',key,'');return true}}
          }});
        }}
        function __dataset(raw, handle) {{
          const values=__map(JSON.parse(raw));
          return new Proxy(values,{{
            set:(target,key,value)=>{{target[key]=value;if(typeof key==='string')
              __dom_mutate(handle,'attr','data-'+key,String(value));return true}},
            deleteProperty:(target,key)=>{{delete target[key];if(typeof key==='string')
              __dom_mutate(handle,'removeAttr','data-'+key,'');return true}}
          }});
        }}
        function __stringSet(raw) {{
          const values=JSON.parse(raw);
          values.contains=value=>values.includes(String(value));
          values.size=()=>values.length;values.isEmpty=()=>values.length===0;
          values.toArray=()=>Array.from(values);return values;
        }}
        function __outputSettings(handle) {{
          const api={{}};
          ['prettyPrint','outline','indentAmount','maxPaddingWidth','syntax',
           'escapeMode','charset'].forEach(name=>api[name]=value=>value===undefined?
            __dom_setting(handle,name):(__dom_setting(handle,name,value),api));
          return api;
        }}
        const __untrackedPosition={{pos:()=>-1,lineNumber:()=>-1,columnNumber:()=>-1,
          isTracked:()=>false,toString:()=>'-1,-1:-1'}};
        const __untrackedRange={{start:()=>__untrackedPosition,end:()=>__untrackedPosition,
          isTracked:()=>false,track:()=>__untrackedRange,
          toString:()=>'-1,-1:-1--1,-1:-1'}};
        const __domNodeCache=new Map();
        const __domBaseUris=new Map();
        const __domQuirks=new Map();
        function __domBaseUri(api) {{const root=api.root();return String(
          __domBaseUris.get(root?root.__handle:api.__handle)||baseUrl||'')}}
        function __domNode(handle) {{
          if (!handle) return null;
          handle=String(handle);if(__domNodeCache.has(handle))return __domNodeCache.get(handle);
          const api={{__handle:String(handle)}};
          __domNodeCache.set(handle,api);
          api.text=value=>value===undefined?__dom_op(api.__handle,'text',''):
            (__dom_mutate(api.__handle,'text','',String(value)),api);
          api.ownText=()=>__dom_op(api.__handle,'ownText','');
          api.wholeText=()=>__dom_op(api.__handle,'wholeText','');
          api.wholeOwnText=()=>__dom_op(api.__handle,'wholeOwnText','');
          api.hasText=()=>Boolean(__dom_op(api.__handle,'hasText',''));
          api.html=value=>value===undefined?__dom_op(api.__handle,'html',''):
            (__dom_mutate(api.__handle,'html','',String(value)),api);
          api.outerHtml=()=>__dom_op(api.__handle,'outerHtml','');
          api.attr=(name,value)=>value===undefined?__dom_op(api.__handle,'attr',String(name)):
            (__dom_mutate(api.__handle,'attr',String(name),String(value)),api);
          api.hasAttr=name=>Boolean(__dom_op(api.__handle,'hasAttr',String(name)));
          api.id=value=>value===undefined?__dom_op(api.__handle,'id',''):
            (__dom_mutate(api.__handle,'attr','id',String(value)),api);
          api.className=value=>value===undefined?__dom_op(api.__handle,'className',''):
            (__dom_mutate(api.__handle,'attr','class',String(value)),api);
          api.val=value=>value===undefined?__dom_op(api.__handle,'val',''):
            (__dom_mutate(api.__handle,'val','',String(value)),api);
          api.hasClass=name=>String(api.attr('class')).split(/\\s+/).includes(String(name));
          api.data=()=>__dom_op(api.__handle,'data','');
          api.tagName=value=>value===undefined?__dom_op(api.__handle,'tagName',''):
            (__dom_mutate(api.__handle,'tagName','',String(value)),api);
          api.tag=()=>({{getName:()=>api.tagName(),normalName:()=>api.normalName(),
            isBlock:()=>api.isBlock(),toString:()=>api.tagName()}});
          api.nodeName=()=>__dom_op(api.__handle,'nodeName','');
          api.normalName=()=>__dom_op(api.__handle,'normalName','');
          api.isBlock=()=>Boolean(__dom_op(api.__handle,'isBlock',''));
          api.attributes=()=>__attributes(__dom_op(api.__handle,'attributes',''),api.__handle);
          api.dataset=()=>__dataset(__dom_op(api.__handle,'dataset',''),api.__handle);
          api.classNames=()=>__stringSet(__dom_op(api.__handle,'classNames',''));
          api.textNodes=()=>__javaStringNodes(__dom_op(api.__handle,'textNodes',''),'text');
          api.dataNodes=()=>__javaStringNodes(__dom_op(api.__handle,'dataNodes',''),'data');
          api.comments=()=>__javaStringNodes(__dom_op(api.__handle,'comments',''),'comment');
          api.childrenSize=()=>Number(__dom_op(api.__handle,'childrenSize',''));
          api.childNodeSize=()=>Number(__dom_op(api.__handle,'childNodeSize',''));
          api.elementSiblingIndex=()=>Number(__dom_op(api.__handle,'elementSiblingIndex',''));
          api.select=rule=>__domNodes(JSON.parse(__dom_query(api.__handle,String(rule))));
          api.selectXpath=rule=>__domNodes(JSON.parse(__dom_xpath(api.__handle,String(rule))));
          api.parents=()=>__domNodes(JSON.parse(__dom_parents(api.__handle)));
          api.children=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'children')));
          api.siblingElements=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'siblings')));
          api.parent=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'parent'))).first();
          api.nextElementSibling=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'next'))).first();
          api.previousElementSibling=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'previous'))).first();
          api.nextElementSiblings=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'nextAll')));
          api.previousElementSiblings=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'previousAll')));
          api.firstElementChild=()=>api.children().first();
          api.lastElementChild=()=>api.children().last();
          api.firstElementSibling=()=>api.parent()?api.parent().children().first():api;
          api.lastElementSibling=()=>api.parent()?api.parent().children().last():api;
          api.root=()=>__domNodes(JSON.parse(__dom_related(api.__handle,'root'))).first();
          api.ownerDocument=()=>api.root();
          api.childNodes=()=>__domNodes(JSON.parse(__dom_traverse(api.__handle,false))
            .map(item=>item.handle));
          api.sourceRange=()=>__untrackedRange;
          api.endSourceRange=()=>__untrackedRange;
          const traversal=()=>JSON.parse(__dom_traverse(api.__handle,true));
          api.traverse=visitor=>{{const records=traversal();records.forEach(item=>{{
            const node=__domNode(item.handle);
            if(typeof visitor==='function')visitor(node,item.depth);
            else if(visitor&&typeof visitor.head==='function')visitor.head(node,item.depth);
          }});records.slice().reverse().forEach(item=>{{if(visitor&&typeof visitor.tail==='function')
            visitor.tail(__domNode(item.handle),item.depth)}});return api}};
          api.forEach=consumer=>{{traversal().forEach(item=>consumer(__domNode(item.handle)));return api}};
          api.filter=filter=>{{const records=traversal();let skipDepth=-1;
            let stopped=false;
            const visit=index=>{{const item=records[index],node=__domNode(item.handle);
              const head=typeof filter==='function'?filter:filter&&filter.head;
              let result=head?String(head(node,item.depth)):'CONTINUE';
              if(result==='STOP'){{stopped=true;return index+1}}
              let next=index+1;
              if(result==='CONTINUE'){{while(next<records.length&&records[next].depth>item.depth){{
                if(records[next].depth===item.depth+1)next=visit(next);else next++;
                if(stopped)return next;
              }}}} else {{while(next<records.length&&records[next].depth>item.depth)next++;}}
              if(result==='CONTINUE'||result==='SKIP_CHILDREN'){{
                const tail=filter&&typeof filter.tail==='function'?filter.tail:null;
                result=tail?String(tail(node,item.depth)):'CONTINUE';
                if(result==='STOP')stopped=true;
              }}
              if(result==='REMOVE')__dom_remove(item.handle);
              return next;
            }};
            if(records.length)visit(0);return api}};
          api.outputSettings=()=>__outputSettings(api.root().__handle);
          api.child=index=>api.children().get(Number(index));
          api.selectFirst=rule=>api.select(rule).first();
          api.expectFirst=rule=>{{const found=api.selectFirst(rule);if(found===null)
            throw new __IllegalArgumentException('No elements matched the query '+String(rule));return found}};
          api.cssSelector=()=>__dom_op(api.__handle,'cssSelector','');
          api.baseUri=()=>__domBaseUri(api);
          api.absUrl=name=>{{const value=api.attr(name);if(!value)return '';
            try{{return __url_resolve(value,api.baseUri())}}catch(e){{return ''}}}};
          api.is=rule=>Boolean(__dom_matches(api.__handle,String(rule)));
          api.closest=rule=>{{let node=api;while(node){{if(node.is(rule))return node;node=node.parent()}}return null}};
          api.getAllElements=()=>__domNodes([api.__handle].concat(
            JSON.parse(__dom_query(api.__handle,'*'))));
          const collect=(kind,first,second)=>__domNodes(JSON.parse(
            __dom_collect(api.__handle,kind,String(first===undefined?'':first),
              String(second===undefined?'':second))));
          api.getElementById=id=>collect('id',id).first();
          api.getElementsByTag=name=>collect('tag',name);
          api.getElementsByClass=name=>collect('class',name);
          api.getElementsByAttribute=name=>collect('attr',name);
          api.getElementsByAttributeStarting=name=>collect('attrStarting',name);
          api.getElementsByAttributeValue=(name,value)=>collect('attrValue',name,value);
          api.getElementsByAttributeValueNot=(name,value)=>collect('attrValueNot',name,value);
          api.getElementsByAttributeValueStarting=(name,value)=>collect('attrValueStarting',name,value);
          api.getElementsByAttributeValueEnding=(name,value)=>collect('attrValueEnding',name,value);
          api.getElementsByAttributeValueContaining=(name,value)=>collect('attrValueContaining',name,value);
          api.getElementsByAttributeValueMatching=(name,value)=>collect('attrValueMatching',name,value);
          api.getElementsByIndexEquals=index=>collect('indexEquals',index);
          api.getElementsByIndexLessThan=index=>collect('indexLess',index);
          api.getElementsByIndexGreaterThan=index=>collect('indexGreater',index);
          api.getElementsContainingText=value=>collect('containsText',value);
          api.getElementsContainingOwnText=value=>collect('containsOwnText',value);
          api.getElementsMatchingText=value=>collect('matchesText',value);
          api.getElementsMatchingOwnText=value=>collect('matchesOwnText',value);
          api.removeAttr=name=>(__dom_mutate(api.__handle,'removeAttr',String(name),''),api);
          api.addClass=name=>(__dom_mutate(api.__handle,'addClass',String(name),''),api);
          api.removeClass=name=>(__dom_mutate(api.__handle,'removeClass',String(name),''),api);
          api.toggleClass=name=>(__dom_mutate(api.__handle,'toggleClass',String(name),''),api);
          api.clearAttributes=()=>(__dom_mutate(api.__handle,'clearAttributes','',''),api);
          api.empty=()=>(__dom_mutate(api.__handle,'empty','',''),api);
          api.append=value=>(value&&value.__handle?__dom_move(api.__handle,'append',value.__handle,0):
            __dom_mutate(api.__handle,'append','',String(value)),api);
          api.prepend=value=>(value&&value.__handle?__dom_move(api.__handle,'prepend',value.__handle,0):
            __dom_mutate(api.__handle,'prepend','',String(value)),api);
          api.before=value=>(value&&value.__handle?__dom_move(api.__handle,'before',value.__handle,0):
            __dom_mutate(api.__handle,'before','',String(value)),api);
          api.after=value=>(value&&value.__handle?__dom_move(api.__handle,'after',value.__handle,0):
            __dom_mutate(api.__handle,'after','',String(value)),api);
          api.appendChild=child=>(__dom_move(api.__handle,'append',child.__handle,0),api);
          api.prependChild=child=>(__dom_move(api.__handle,'prepend',child.__handle,0),api);
          api.appendTo=parent=>(__dom_move(parent.__handle,'append',api.__handle,0),api);
          api.insertChildren=(index,children)=>{{Array.from(children||[]).forEach((child,offset)=>
            __dom_move(api.__handle,'insert',child.__handle,Number(index)+offset));return api}};
          api.appendChildren=children=>api.insertChildren(api.childNodeSize(),children);
          api.prependChildren=children=>api.insertChildren(0,children);
          api.appendText=value=>(__dom_mutate(api.__handle,'appendText','',String(value)),api);
          api.prependText=value=>(__dom_mutate(api.__handle,'prependText','',String(value)),api);
          api.appendElement=name=>(api.append('<'+String(name)+'></'+String(name)+'>'),api.children().last());
          api.prependElement=name=>(api.prepend('<'+String(name)+'></'+String(name)+'>'),api.children().first());
          api.wrap=value=>(__dom_mutate(api.__handle,'wrap','',String(value)),api);
          api.clone=()=>{{if(api.nodeName()==='#document')return __document(api.outerHtml(),api.baseUri());
            const root=__dom_parse(api.outerHtml());return __domNodes(
              JSON.parse(__dom_collect(root,'tag',api.tagName(),''))).first()}};
          api.shallowClone=()=>{{const copy=api.clone();copy.empty();return copy}};
          api.remove=()=>{{__dom_remove(api.__handle);return api}};
          if(api.normalName()==='form'){{
            api.elements=()=>__domNodes(JSON.parse(__dom_form_data(api.__handle,true)));
            api.formData=()=>JSON.parse(__dom_form_data(api.__handle)).map(pair=>({{
              key:()=>pair[0],value:()=>pair[1],toString:()=>pair[0]+'='+pair[1]
            }}));
            api.submit=()=>{{const target=api.absUrl('action')||api.baseUri();
              if(!target)throw new __IllegalArgumentException('Could not determine a form action URL for submit. Ensure you set a base URI when parsing.');
              const connection=__jsoupConnect(target);const method=String(api.attr('method')||'GET');
              connection.method(method);api.formData().forEach(item=>connection.data(item.key(),item.value()));
              return connection}};
          }}
          api.toString=()=>api.outerHtml();api.valueOf=()=>api.outerHtml();
          api.toJSON=()=>api.outerHtml();
          return api;
        }}
        function __domNodes(input) {{
          const handles=Array.from(input||[]).map(String),api={{}};
          const refresh=()=>{{
            Object.keys(api).filter(k=>/^\\d+$/.test(k)).forEach(k=>delete api[k]);
            handles.forEach((h,i)=>api[i]=__domNode(h));api.length=handles.length;
          }};
          api.text=value=>value===undefined?handles.map(h=>__dom_op(h,'text','')).join(' '):
            (handles.forEach(h=>__dom_mutate(h,'text','',String(value))),api);
          api.hasClass=name=>handles.some(h=>String(
            __dom_op(h,'attr','class')).split(/\\s+/).includes(String(name)));
          api.hasAttr=name=>handles.some(h=>Boolean(__dom_op(h,'hasAttr',String(name))));
          api.hasText=()=>handles.some(h=>Boolean(__dom_op(h,'hasText','')));
          api.html=value=>value===undefined?handles.map(h=>__dom_op(h,'html','')).join(''):
            (handles.forEach(h=>__dom_mutate(h,'html','',String(value))),api);
          api.outerHtml=()=>handles.map(h=>__dom_op(h,'outerHtml','')).join('');
          api.attr=(name,value)=>value===undefined?
            (handles.length?__dom_op(handles[0],'attr',String(name)):''):
            (handles.forEach(h=>__dom_mutate(h,'attr',String(name),String(value))),api);
          api.val=value=>value===undefined?
            (handles.length?__dom_op(handles[0],'val',''):''):
            (handles.forEach(h=>__dom_mutate(h,'val','',String(value))),api);
          api.removeAttr=name=>(handles.forEach(h=>__dom_mutate(h,'removeAttr',String(name),'')),api);
          api.addClass=name=>(handles.forEach(h=>__dom_mutate(h,'addClass',String(name),'')),api);
          api.removeClass=name=>(handles.forEach(h=>__dom_mutate(h,'removeClass',String(name),'')),api);
          api.toggleClass=name=>(handles.forEach(h=>__dom_mutate(h,'toggleClass',String(name),'')),api);
          api.tagName=name=>(handles.forEach(h=>__dom_mutate(h,'tagName','',String(name))),api);
          api.empty=()=>(handles.forEach(h=>__dom_mutate(h,'empty','','')),api);
          api.append=value=>(handles.forEach(h=>__dom_mutate(h,'append','',String(value))),api);
          api.prepend=value=>(handles.forEach(h=>__dom_mutate(h,'prepend','',String(value))),api);
          api.before=value=>(handles.forEach(h=>__dom_mutate(h,'before','',String(value))),api);
          api.after=value=>(handles.forEach(h=>__dom_mutate(h,'after','',String(value))),api);
          api.size=()=>handles.length;api.isEmpty=()=>handles.length===0;
          api.first=()=>handles.length?__domNode(handles[0]):null;
          api.last=()=>handles.length?__domNode(handles[handles.length-1]):null;
          api.get=index=>handles[Number(index)]?__domNode(handles[Number(index)]):null;
          api.eq=index=>__domNodes(handles[Number(index)]?[handles[Number(index)]]:[]);
          api.eachText=()=>handles.map(h=>__dom_op(h,'text',''));
          api.eachAttr=name=>handles.map(h=>__dom_op(h,'attr',String(name)));
          api.textNodes=()=>__javaStringNodes(JSON.stringify(handles.flatMap(
            h=>JSON.parse(__dom_op(h,'textNodes','')))),'text');
          api.dataNodes=()=>__javaStringNodes(JSON.stringify(handles.flatMap(
            h=>JSON.parse(__dom_op(h,'dataNodes','')))),'data');
          api.comments=()=>__javaStringNodes(JSON.stringify(handles.flatMap(
            h=>JSON.parse(__dom_op(h,'comments','')))),'comment');
          api.select=child=>__domNodes(handles.flatMap(
            h=>JSON.parse(__dom_query(h,String(child)))));
          api.parents=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_parents(h))));
          api.next=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_related(h,'next'))));
          api.nextAll=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_related(h,'nextAll'))));
          api.prev=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_related(h,'previous'))));
          api.prevAll=()=>__domNodes(handles.flatMap(h=>JSON.parse(__dom_related(h,'previousAll'))));
          api.is=selector=>handles.some(h=>__dom_matches(h,String(selector)));
          api.not=selector=>__domNodes(handles.filter(h=>!__dom_matches(h,String(selector))));
          api.forms=()=>__domNodes(handles.filter(h=>__dom_op(h,'normalName','')==='form'));
          api.contains=node=>Boolean(node&&__dom_contains(JSON.stringify(handles),node.__handle));
          api.remove=index=>{{
            if(index===undefined){{handles.forEach(h=>__dom_remove(h));handles.splice(0);refresh();return api}}
            const p=Number(index);if(p<0||p>=handles.length)return null;
            const removed=handles.splice(p,1)[0];refresh();return __domNode(removed);
          }};
          api.subList=(start,end)=>({{clear:()=>{{
            handles.splice(Number(start),Math.max(0,Number(end)-Number(start)));refresh();
          }}}});
          api.wrap=value=>(handles.forEach(h=>__dom_mutate(h,'wrap','',String(value))),api);
          api.unwrap=()=>(handles.forEach(h=>__dom_mutate(h,'unwrap','','')),api);
          api.clone=()=>__domNodes(handles.map(h=>__domNode(h).clone().__handle));
          api.toArray=()=>handles.map(__domNode);api.toString=()=>api.outerHtml();
          api[Symbol.iterator]=function*(){{for(const h of handles)yield __domNode(h)}};
          // Java Elements exposes methods through its class, not as enumerable
          // record properties.  Keep QuickJS `for (key in elements)` aligned.
          ['text','hasClass','hasAttr','hasText','html','outerHtml','attr','val','size','isEmpty','first',
           'last','get','eq','eachText','eachAttr','select','parents','contains',
           'removeAttr','addClass','removeClass','toggleClass','tagName','empty',
           'append','prepend','before','after','next','nextAll','prev','prevAll',
           'textNodes','dataNodes','comments','is','not','forms','wrap','unwrap','clone',
           'remove','subList','toArray','toString','length'].forEach(name =>
            Object.defineProperty(api,name,{{value:api[name],writable:true,
              configurable:true,enumerable:false}}));
          refresh();return api;
        }}
        function __document(html, location) {{
          const root=__dom_parse(String(html == null ? '' : html));
          __domBaseUris.set(String(root),String(location || baseUrl || ''));
          const api=__domNode(root);
          api.title=value=>{{const title=api.select('title').first();if(value===undefined)
            return title?title.text():'';if(title)title.text(String(value));else
            api.head().appendElement('title').text(String(value));return api}};
          api.body=()=>api.select('body').first();
          api.head=()=>api.select('head').first();
          api.location=()=>String(location || baseUrl);
          api.charset=value=>value===undefined?__dom_setting(root,'charset'):
            (__dom_setting(root,'charset',String(value)),api);
          api.forms=()=>api.select('form');
          api.parser=()=>({{toString:()=> 'org.jsoup.parser.Parser@0'}});
          api.quirksMode=value=>value===undefined?
            (__domQuirks.get(String(root))||'noQuirks'):
            (__domQuirks.set(String(root),String(value)),api);
          api.createElement=name=>{{const parsed=__document('<'+String(name)+'></'+String(name)+'>',api.location());
            return parsed.getElementsByTag(String(name)).first()}};
          return api;
        }}
        if (__resultHtmlNodes && Array.isArray(result)) {{
          result = __elementList(result);
        }} else if (__resultHtmlNodes && typeof result === 'string') {{
          result = __element(result);
        }}
        function __jsoupConnect(url) {{
          const state={{url:String(url),headers:{{}},data:[],method:'GET',followRedirects:true}};
          const api={{
            header:(name,value)=>{{state.headers[String(name)]=String(value);return api;}},
            headers:value=>{{Object.assign(state.headers,value || {{}});return api;}},
            userAgent:value=>{{state.headers['User-Agent']=String(value);return api;}},
            timeout:()=>api,ignoreContentType:()=>api,ignoreHttpErrors:()=>api,
            followRedirects:value=>{{state.followRedirects=Boolean(value);return api;}},maxBodySize:()=>api,
            data:(name,value)=>{{state.data.push([String(name),String(value)]);return api;}},
            method:value=>{{state.method=String(value).toUpperCase();return api;}},
            execute:()=>__http(state.method,state.url,
              state.data.map(item=>encodeURIComponent(item[0])+'='+encodeURIComponent(item[1])).join('&'),
              state.headers,state.followRedirects),
            get:()=>{{const response=api.execute();return __document(response.body(),response.url());}},
            post:()=>{{state.method='POST';const response=api.execute();return __document(response.body(),response.url());}}
          }};
          return api;
        }}
        // Jsoup 在 Java 里是类，做成函数才能让 importClass(Jsoup) 的引用比较成立。
        function __Jsoup() {{}}
        __Jsoup.parse=(html,location)=>__document(html,location);
        __Jsoup.connect=url=>__jsoupConnect(url);
        var org = {{jsoup: {{
          Jsoup: __Jsoup,
          nodes: {{Document:function(){{}},Element:function(){{}}}}
        }}}};
        org.jsoup.nodes.Document.OutputSettings={{Syntax:{{html:'html',xml:'xml'}}}};
        org.jsoup.nodes.Document.QuirksMode={{noQuirks:'noQuirks',quirks:'quirks',
          limitedQuirks:'limitedQuirks'}};
        org.jsoup.nodes.Range={{Untracked:__untrackedRange}};
        // Jsoup.parse 的第三个参数只用来切换解析器对象，文档树仍由本运行时的
        // HTML 解析器构建（见 __document）。
        const __jsoupParser=name=>({{getName:()=>name,toString:()=>'org.jsoup.parser.Parser@'+name}});
        org.jsoup.parser={{Parser:{{
          htmlParser:()=>__jsoupParser('html'),
          xmlParser:()=>__jsoupParser('xml')
        }}}};
        org.jsoup.nodes.Entities={{EscapeMode:{{xhtml:'xhtml',base:'base',extended:'extended'}}}};
        org.jsoup.select={{NodeFilter:{{FilterResult:{{CONTINUE:'CONTINUE',SKIP_CHILDREN:'SKIP_CHILDREN',
          SKIP_ENTIRELY:'SKIP_ENTIRELY',REMOVE:'REMOVE',STOP:'STOP'}}}},NodeVisitor:function(){{}}}};
        String.prototype.getBytes = function(charset) {{
          if (__isLatin1(charset)) return __latin1StringToBytes(this);
          return __byteArray(JSON.parse(__strbytes(String(this), String(charset || 'utf-8'))));
        }};
        const __NativeDate=Date;
        function __localDate(value) {{
          const milliseconds=value.getTime();
          return new __NativeDate(milliseconds+Number(__tz_offset(milliseconds))*60000);
        }}
        Date.prototype.getTimezoneOffset=function(){{return -Number(__tz_offset(this.getTime()))}};
        Date.prototype.getFullYear=function(){{return __localDate(this).getUTCFullYear()}};
        Date.prototype.getMonth=function(){{return __localDate(this).getUTCMonth()}};
        Date.prototype.getDate=function(){{return __localDate(this).getUTCDate()}};
        Date.prototype.getDay=function(){{return __localDate(this).getUTCDay()}};
        Date.prototype.getHours=function(){{return __localDate(this).getUTCHours()}};
        Date.prototype.getMinutes=function(){{return __localDate(this).getUTCMinutes()}};
        Date.prototype.getSeconds=function(){{return __localDate(this).getUTCSeconds()}};
        Date.prototype.toString=function(){{return __date_string(this.getTime())}};
        function __iterator(values) {{
          let index=0,last=-1;
          return {{
            hasNext:()=>index<values.length,
            next:()=>{{if(index>=values.length)throw new Error('NoSuchElementException');last=index;return values[index++];}},
            remove:()=>{{if(last<0)throw new Error('IllegalStateException');values.splice(last,1);index=last;last=-1;}},
            forEachRemaining:consumer=>{{while(index<values.length)consumer(values[index++]);}}
          }};
        }}
        function __stream(input) {{
          const values=Array.from(input||[]);
          return {{
            map:mapper=>__stream(values.map(mapper)),
            filter:predicate=>__stream(values.filter(predicate)),
            distinct:()=>__stream(values.filter((value,index,array)=>array.indexOf(value)===index)),
            sorted:comparator=>__stream(Array.from(values).sort(comparator)),
            limit:count=>__stream(values.slice(0,Math.max(0,Number(count)))),
            skip:count=>__stream(values.slice(Math.max(0,Number(count)))),
            forEach:consumer=>values.forEach(consumer),
            count:()=>values.length,
            findFirst:()=>({{isPresent:()=>values.length>0,get:()=>values[0],orElse:fallback=>values.length?values[0]:fallback}}),
            toArray:()=>Array.from(values),
            collect:collector=>collector&&collector.__collector==='set'
              ? __HashSet(values):Array.from(values),
            iterator:()=>__iterator(values)
          }};
        }}
        if (!Object.prototype.hasOwnProperty.call(Array.prototype,'toArray'))
        Object.defineProperties(Array.prototype, {{
          toArray: {{value:function(){{return Array.from(this)}},enumerable:false}},
          size: {{value:function(){{return this.length}},enumerable:false}},
          get: {{value:function(index){{return this[Number(index)]}},enumerable:false}},
          isEmpty: {{value:function(){{return this.length===0}},enumerable:false}},
          add: {{value:function(index,value){{
            if(arguments.length>1){{this.splice(Number(index),0,value);return undefined;}}
            this.push(index);return true;
          }},enumerable:false}},
          set: {{value:function(index,value){{const old=this[Number(index)];this[Number(index)]=value;return old}},enumerable:false}},
          remove: {{value:function(value){{
            const index=typeof value==='number'?Number(value):this.indexOf(value);
            return index>=0&&index<this.length?this.splice(index,1)[0]:false;
          }},enumerable:false}},
          contains: {{value:function(value){{return this.includes(value)}},enumerable:false}},
          iterator: {{value:function(){{return __iterator(this)}},enumerable:false}},
          stream: {{value:function(){{return __stream(this)}},enumerable:false}},
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
        function __javaStringText(value, count, offset, length, charset) {{
          if (count === 0) return '';
          if (!Array.isArray(value)) return String(value);
          const bytes = Array.from(value);
          if (count >= 4) return __bytesstr(JSON.stringify(bytes.slice(
            Number(offset), Number(offset) + Number(length))), String(charset));
          if (count === 3) return __bytesstr(JSON.stringify(bytes.slice(
            Number(offset), Number(offset) + Number(length))), 'utf-8');
          if (count === 2) return __bytesstr(JSON.stringify(bytes), String(offset));
          return __bytesstr(JSON.stringify(bytes), 'utf-8');
        }}
        function __javaStringObject(value, count, offset, length, charset) {{
          const text = __javaStringText(value, count, offset, length, charset);
          const size = () => text.length;
          size.valueOf = size; size.toString = () => String(text.length);
          const wrap = string => __javaStringObject(string, 1);
          const api = {{
            toString: () => text, valueOf: () => text, toJSON: () => text,
            // Java exposes length() while scripts keep writing myStr.length.
            length: size,
            getBytes: charset_ => __byteArray(JSON.parse(__strbytes(
              text, String(charset_ === undefined ? 'utf-8' : charset_)))),
            isEmpty: () => text.length === 0,
            charAt: index => text.charAt(Number(index)),
            charCodeAt: index => text.charCodeAt(Number(index)),
            codePointAt: index => text.codePointAt(Number(index)),
            toCharArray: () => Array.from(text),
            substring: (start, end) => wrap(text.substring(
              Number(start), end === undefined ? undefined : Number(end))),
            contains: value => text.includes(String(value)),
            startsWith: (value, position) => text.startsWith(
              String(value), Number(position === undefined ? 0 : position)),
            endsWith: value => text.endsWith(String(value)),
            equals: value => text === String(value),
            equalsIgnoreCase: value => text.toLowerCase() === String(value).toLowerCase(),
            compareTo: value => {{
              const other = String(value);
              return text < other ? -1 : text > other ? 1 : 0;
            }},
            concat: value => wrap(text + String(value)),
            trim: () => wrap(text.trim()),
            toUpperCase: () => wrap(text.toUpperCase()),
            toLowerCase: () => wrap(text.toLowerCase()),
            indexOf: (value, from) => text.indexOf(
              String(value), Number(from === undefined ? 0 : from)),
            lastIndexOf: (value, from) => from === undefined
              ? text.lastIndexOf(String(value))
              : text.lastIndexOf(String(value), Number(from)),
            replace: (search, replacement) => search instanceof RegExp
              ? wrap(text.replace(search, replacement))
              // java.lang.String.replace swaps every literal occurrence.
              : wrap(text.split(String(search)).join(String(replacement))),
            replaceAll: (pattern, replacement) => wrap(
              text.replace(new RegExp(String(pattern), 'g'), String(replacement))),
            split: (pattern, limit) => {{
              const parts = text.split(new RegExp(String(pattern)));
              if (limit === undefined) {{
                while (parts.length && parts[parts.length - 1] === '') parts.pop();
              }} else if (Number(limit) > 0 && parts.length > Number(limit)) {{
                parts.length = Number(limit);
              }}
              return parts;
            }},
            matches: pattern => new RegExp('^(?:' + String(pattern) + ')$').test(text),
            hashCode: () => {{
              let hash = 0;
              for (let index = 0; index < text.length; index += 1)
                hash = (Math.imul(31, hash) + text.charCodeAt(index)) | 0;
              return hash;
            }},
            getClass: () => null
          }};
          api[Symbol.toPrimitive] = hint => hint === 'number' ? Number(text) : text;
          return new Proxy(api, {{
            get: (target, name) => {{
              if (Reflect.has(target, name)) return Reflect.get(target, name);
              if (typeof name !== 'string') return undefined;
              const member = text[name];
              return typeof member === 'function' ? member.bind(text) : member;
            }}
          }});
        }}
        function __JavaString(value, offset, length, charset) {{
          if (new.target) return __javaStringObject(
            value, arguments.length, offset, length, charset);
          return Array.isArray(value) ? __bytesstr(JSON.stringify(value), 'utf-8') : String(value);
        }}
        __JavaString.valueOf = value => value == null ? 'null' : String(value);
        function __javaException(name) {{
          function JavaException(message) {{
            const error=new Error(String(message == null ? '' : message));
            Object.setPrototypeOf(error,JavaException.prototype);error.name=name;
            return error;
          }}
          JavaException.prototype=Object.create(Error.prototype);
          Object.defineProperty(JavaException.prototype,'constructor',{{value:JavaException}});
          JavaException.prototype.getMessage=function(){{return this.message}};
          JavaException.prototype.getLocalizedMessage=function(){{return this.message}};
          JavaException.prototype.toString=function(){{return name+(this.message?': '+this.message:'')}};
          return JavaException;
        }}
        function __ArrayList() {{
          return arguments.length ? Array.from(arguments[0] || []) : [];
        }}
        function __HashSet(values) {{
          const target=Array.from(values||[]).filter((value,index,array)=>array.indexOf(value)===index);
          Object.defineProperty(target,'add',{{enumerable:false,value:function(value){{
            if(this.includes(value))return false;this.push(value);return true;
          }}}});return target;
        }}
        function __bitmap(handle) {{
          const api = {{__bitmap:String(handle)}};
          api.getWidth = () => JSON.parse(__bitmap_info(api.__bitmap))[0];
          api.getHeight = () => JSON.parse(__bitmap_info(api.__bitmap))[1];
          api.getPixel = (x,y) => __bitmap_pixel(api.__bitmap,Number(x),Number(y));
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
        function __GCMParameterSpec(tLen, iv) {{
          return {{bytes:Array.from(iv || []), tLen:Number(tLen || 128)}};
        }}
        const __Cipher = {{
          ENCRYPT_MODE: 1, DECRYPT_MODE: 2,
          getInstance: transformation => {{
            const state = {{transformation:String(transformation), mode:2, key:[], iv:[],
                           tagBits:128}};
            return {{
              init: (mode, key, iv) => {{
                state.mode=Number(mode); state.key=Array.from((key && key.bytes) || key || []);
                state.iv=Array.from((iv && iv.bytes) || iv || []);
                if (iv && iv.tLen) state.tagBits=Number(iv.tLen);
              }},
              doFinal: value => __byteArray(JSON.parse(__crypto(
                state.mode === 1 ? 'encryptBytes' : 'decryptBytes', state.transformation,
                JSON.stringify(state.key), JSON.stringify(state.iv),
                JSON.stringify(Array.from(value || [])), state.tagBits)))
            }};
          }}
        }};
        const __Base64 = {{
          DEFAULT: 0, NO_PADDING: 1, NO_WRAP: 2, CRLF: 4, URL_SAFE: 8,
          encodeToString: (value, flags) => __b64encode_bytes(JSON.stringify(Array.from(value || [])), Number(flags || 0)),
          decode: value => __byteArray(JSON.parse(__b64bytes(Array.isArray(value)
            ? __bytesstr(JSON.stringify(value), 'utf-8') : String(value)))),
          getDecoder: () => ({{decode: value => __byteArray(JSON.parse(__b64bytes(String(value))))}}),
          getEncoder: () => ({{encodeToString: value => __b64encode_bytes(JSON.stringify(Array.from(value || [])), 0)}})
        }};
        const __Mac = {{getInstance: algorithm => {{
          const state={{algorithm:String(algorithm),key:[]}};
          return {{
            init:key=>{{state.key=Array.from((key&&key.bytes)||key||[]);}},
            doFinal:value=>__byteArray(JSON.parse(__hexbytes(__hmac(
              __bytesstr(JSON.stringify(Array.from(value||[])),'utf-8'),state.algorithm,
              __bytesstr(JSON.stringify(state.key),'utf-8'),false))))
          }};
        }}}};
        const __URLEncoder={{encode:(value,charset)=>
          __encode_uri(String(value),String(charset||'utf-8'))}};
        const __DatatypeConverter={{
          printHexBinary:value=>Array.from(value||[]).map(item=>(Number(item)&255)
            .toString(16).padStart(2,'0')).join('').toUpperCase(),
          parseHexBinary:value=>__byteArray(JSON.parse(__hexbytes(String(value))))
        }};
        const __javaNamespaceOriginals=new WeakMap();
        const Packages = __javaNamespace('', {{java:{{}},javax:{{}},android:{{}},cn:{{}},
          org:org,io:{{}}}});
        const __Throwable=__javaException('java.lang.Throwable');
        const __Exception=__javaException('java.lang.Exception');
        __Exception.prototype=Object.create(__Throwable.prototype);
        __Exception.prototype.constructor=__Exception;
        __Exception.prototype.toString=function(){{return 'java.lang.Exception'+(this.message?': '+this.message:'')}};
        const __RuntimeException=__javaException('java.lang.RuntimeException');
        __RuntimeException.prototype=Object.create(__Exception.prototype);
        __RuntimeException.prototype.constructor=__RuntimeException;
        __RuntimeException.prototype.toString=function(){{return 'java.lang.RuntimeException'+(this.message?': '+this.message:'')}};
        const __NullPointerException=__javaException('java.lang.NullPointerException');
        __NullPointerException.prototype=Object.create(__RuntimeException.prototype);
        __NullPointerException.prototype.constructor=__NullPointerException;
        const __IllegalArgumentException=__javaException('java.lang.IllegalArgumentException');
        __IllegalArgumentException.prototype=Object.create(__RuntimeException.prototype);
        __IllegalArgumentException.prototype.constructor=__IllegalArgumentException;
        __IllegalArgumentException.prototype.toString=function(){{return 'java.lang.IllegalArgumentException'+(this.message?': '+this.message:'')}};
        const __IndexOutOfBoundsException=__javaException('java.lang.IndexOutOfBoundsException');
        __IndexOutOfBoundsException.prototype=Object.create(__RuntimeException.prototype);
        __IndexOutOfBoundsException.prototype.constructor=__IndexOutOfBoundsException;
        const __IOException=__javaException('java.io.IOException');
        __IOException.prototype=Object.create(__Exception.prototype);
        __IOException.prototype.constructor=__IOException;
        const __EOFException=__javaException('java.io.EOFException');
        __EOFException.prototype=Object.create(__IOException.prototype);
        __EOFException.prototype.constructor=__EOFException;
        const __NumberFormatException=__javaException('java.lang.NumberFormatException');
        __NumberFormatException.prototype=Object.create(__IllegalArgumentException.prototype);
        __NumberFormatException.prototype.constructor=__NumberFormatException;
        function __wrappedJavaException(javaException) {{
          const error=new Error(String(javaException));error.name='JavaException';
          error.javaException=javaException;
          error.toString=function(){{return 'JavaException: '+String(javaException)}};
          return error;
        }}
        function __unsupportedJavaApi(path) {{
          const error=new Error('未实现的 Java API：Packages.'+path
            +'，本运行时未模拟该接口');
          error.name='UnsupportedJavaApiError';
          error.javaApi='Packages.'+path;
          return error;
        }}
        function __javaInterface(name) {{
          const target=function(){{throw __unsupportedJavaApi(name);}};
          target.toString=()=>'[JavaInterface '+name+']';
          Object.defineProperty(target,'name',{{value:String(name).split('.').pop()}});
          return target;
        }}
        function __javaMember(path) {{
          // 未模拟的成员必须保持「可探测」——typeof 仍是 function、取值仍为真——
          // 只在真正调用或实例化的那一刻抛错，并且报错里带上完整路径。
          const callable=function(){{throw __unsupportedJavaApi(path);}};
          Object.defineProperty(callable,'name',{{value:String(path).split('.').pop()}});
          callable.toString=()=>'[JavaPackage '+path+']';
          return new Proxy(callable,{{get:(target,key)=>{{
            if(key===Symbol.toPrimitive||key==='toString'||key==='valueOf')
              return target.toString;
            if(key==='then'||key===Symbol.iterator||key===Symbol.toStringTag)
              return undefined;
            if(key==='name'||key==='length')return target[key];
            return __javaMember(path+'.'+String(key));
          }},apply:()=>{{throw __unsupportedJavaApi(path)}},
             construct:()=>{{throw __unsupportedJavaApi(path)}}}});
        }}
        function __javaNamespace(name,members) {{
          // 直接代理真实成员表而不是复制，importPackage/importClass 才能靠
          // 引用相等认出 __Cipher、__MessageDigest 这些类对象。
          const wrapped=new Map();
          const path=key=>name?name+'.'+key:String(key);
          const proxy=new Proxy(members,{{get:(object,key)=>{{
            if(Object.prototype.hasOwnProperty.call(object,key)){{
              const value=object[key];
              if(value===null||typeof value!=='object'||
                 Object.getPrototypeOf(value)!==Object.prototype||
                 __javaNamespaceOriginals.has(value))return value;
              if(!wrapped.has(key))wrapped.set(key,__javaNamespace(path(key),value));
              return wrapped.get(key);
            }}
            if(key==='then'||key===Symbol.iterator||key===Symbol.toStringTag)
              return undefined;
            if(key==='toString'||key==='valueOf'||key===Symbol.toPrimitive)
              return object.toString;
            return __javaMember(path(String(key)));
          }}}});
          __javaNamespaceOriginals.set(proxy,members);
          return proxy;
        }}
        function __unwrapJava(value) {{
          return value&&typeof value==='object'&&__javaNamespaceOriginals.has(value)
            ? __javaNamespaceOriginals.get(value) : value;
        }}
        function __javaPackage(name) {{ return __javaMember(name); }}
        function __javaApiInventory() {{
          // 列出现在真正解析得到的全部 Packages.* 路径：命名空间、类对象和静态
          // 成员都会登记，未模拟的路径不会出现在这里（取值只会得到 __javaMember
          // 桩）。检测端据此在跑规则之前就知道书源需要哪些没模拟的接口。
          const out=[];
          const seen=new Set();
          (function walk(node,prefix,depth){{
            const kind=typeof node;
            if(depth>12||node===null||
               (kind!=='object'&&kind!=='function')||seen.has(node))return;
            seen.add(node);
            // 类对象是函数，静态方法就挂在函数自身上；Object.keys 不会枚举
            // prototype/length/name，所以直接往下走不会失控。
            for(const key of Object.keys(node)){{
              const child=node[key];
              const path=prefix?prefix+'.'+key:key;
              out.push(path);
              if(child!==null)walk(child,path,depth+1);
            }}
          }})(Packages,'',0);
          return JSON.stringify(out);
        }}
        function __parseJavaInteger(value,radix,min,max) {{
          const text=String(value),base=radix===undefined?10:Number(radix);
          if(!Number.isInteger(base)||base<2||base>36||!text.length)
            throw __wrappedJavaException(new __NumberFormatException('For input string: "'+text+'"'));
          let sign=1n,index=0;if(text[0]==='-'||text[0]==='+'){{sign=text[0]==='-'?-1n:1n;index=1}}
          if(index===text.length)throw __wrappedJavaException(new __NumberFormatException('For input string: "'+text+'"'));
          let number=0n;for(;index<text.length;index++){{
            const code=text[index].toLowerCase().charCodeAt(0);
            const digit=code>=48&&code<=57?code-48:code>=97&&code<=122?code-87:-1;
            if(digit<0||digit>=base)throw __wrappedJavaException(new __NumberFormatException('For input string: "'+text+'"'));
            number=number*BigInt(base)+BigInt(digit);
          }}
          number*=sign;if(number<BigInt(min)||number>BigInt(max))
            throw __wrappedJavaException(new __NumberFormatException('For input string: "'+text+'"'));
          return number;
        }}
        function __javaIntegralClass(name,min,max,bits) {{
          function Box(value,radix) {{if(!(this instanceof Box))return new Box(value,radix);
            Object.defineProperty(this,'__value',{{value:__parseJavaInteger(value,radix,min,max)}})}}
          Box.valueOf=(value,radix)=>new Box(value,radix);
          const parseName=name==='Integer'?'parseInt':'parse'+name;
          Box[parseName]=(value,radix)=>Number(__parseJavaInteger(value,radix,min,max));
          Box.MIN_VALUE=Number(BigInt(min));Box.MAX_VALUE=Number(BigInt(max));
          Box.TYPE=null;Box.compare=(left,right)=>Number(left)<Number(right)?-1:Number(left)>Number(right)?1:0;
          Box.toString=(value,radix)=>BigInt(Math.trunc(Number(value))).toString(Number(radix||10));
          Box.prototype.toString=function(radix){{return this.__value.toString(Number(radix||10))}};
          Box.prototype.valueOf=function(){{return Number(this.__value)}};
          Box.prototype.byteValue=function(){{return Number(BigInt.asIntN(8,this.__value))}};
          Box.prototype.shortValue=function(){{return Number(BigInt.asIntN(16,this.__value))}};
          Box.prototype.intValue=function(){{return Number(BigInt.asIntN(32,this.__value))}};
          Box.prototype.longValue=function(){{return Number(BigInt.asIntN(64,this.__value))}};
          Box.prototype.floatValue=Box.prototype.longValue;Box.prototype.doubleValue=Box.prototype.longValue;
          Box.prototype.compareTo=function(other){{return this.__value<other.__value?-1:this.__value>other.__value?1:0}};
          Box.prototype.equals=function(other){{return other instanceof Box&&this.__value===other.__value}};
          Box.prototype.hashCode=function(){{const low=BigInt.asIntN(32,this.__value);
            const high=BigInt.asIntN(32,this.__value>>32n);return Number(BigInt.asIntN(32,low^high))}};
          Box.prototype.getClass=()=>null;Box.prototype.toJSON=Box.prototype.valueOf;
          return Box;
        }}
        const __Byte=__javaIntegralClass('Byte','-128','127',8);
        const __Short=__javaIntegralClass('Short','-32768','32767',16);
        const __Integer=__javaIntegralClass('Integer','-2147483648','2147483647',32);
        const __Long=__javaIntegralClass('Long','-9223372036854775808','9223372036854775807',64);
        function __javaFloatingClass(name,isFloat) {{
          function Box(value) {{if(!(this instanceof Box))return new Box(value);
            let number=Number(value);if(Number.isNaN(number)&&String(value)!=='NaN')
              throw __wrappedJavaException(new __NumberFormatException('For input string: "'+String(value)+'"'));
            Object.defineProperty(this,'__value',{{value:isFloat?Math.fround(number):number}})}}
          Box.valueOf=value=>new Box(value);Box['parse'+name]=value=>new Box(value).__value;
          Box.NaN=NaN;Box.POSITIVE_INFINITY=Infinity;Box.NEGATIVE_INFINITY=-Infinity;Box.TYPE=null;
          Box.isNaN=value=>Number.isNaN(Number(value));Box.isInfinite=value=>!Number.isFinite(Number(value));
          Box.compare=(left,right)=>Number(left)<Number(right)?-1:Number(left)>Number(right)?1:
            Object.is(Number(left),-0)&&Object.is(Number(right),0)?-1:Object.is(Number(left),0)&&Object.is(Number(right),-0)?1:0;
          Box.prototype.valueOf=function(){{return this.__value}};
          Box.prototype.toString=function(){{return String(this.__value)}};
          Box.prototype.byteValue=function(){{return Number(BigInt.asIntN(8,BigInt(Math.trunc(this.__value))))}};
          Box.prototype.shortValue=function(){{return Number(BigInt.asIntN(16,BigInt(Math.trunc(this.__value))))}};
          Box.prototype.intValue=function(){{return Math.trunc(this.__value)}};
          Box.prototype.longValue=Box.prototype.intValue;Box.prototype.floatValue=Box.prototype.valueOf;
          Box.prototype.doubleValue=Box.prototype.valueOf;Box.prototype.isNaN=function(){{return Number.isNaN(this.__value)}};
          Box.prototype.isInfinite=function(){{return !Number.isFinite(this.__value)}};
          Box.prototype.getClass=()=>null;Box.prototype.toJSON=Box.prototype.valueOf;
          return Box;
        }}
        const __Float=__javaFloatingClass('Float',true),__Double=__javaFloatingClass('Double',false);
        function __Character(value) {{if(!(this instanceof __Character))return new __Character(value);
          const text=String(value);Object.defineProperty(this,'__value',{{value:text.length?text.codePointAt(0):0}})}}
        __Character.valueOf=value=>new __Character(value);__Character.TYPE=null;
        __Character.isDigit=value=>/^\\p{{Nd}}$/u.test(String(value));
        __Character.isLetter=value=>/^\\p{{L}}$/u.test(String(value));
        __Character.isLetterOrDigit=value=>/^[\\p{{L}}\\p{{Nd}}]$/u.test(String(value));
        __Character.digit=(value,radix)=>{{const code=String(value).toLowerCase().charCodeAt(0),
          digit=code>=48&&code<=57?code-48:code>=97&&code<=122?code-87:-1;
          return digit>=0&&digit<Number(radix)?digit:-1}};
        __Character.prototype.charValue=function(){{return this.__value}};
        __Character.prototype.valueOf=__Character.prototype.charValue;
        __Character.prototype.toString=function(){{return String.fromCodePoint(this.__value)}};
        __Character.prototype.getClass=()=>null;__Character.prototype.toJSON=__Character.prototype.toString;
        function __BooleanBox(value) {{if(!(this instanceof __BooleanBox))return new __BooleanBox(value);
          this.__value=typeof value==='string'?value.toLowerCase()==='true':Boolean(value)}}
        __BooleanBox.parseBoolean=value=>String(value).toLowerCase()==='true';
        __BooleanBox.valueOf=value=>new __BooleanBox(value);__BooleanBox.TYPE=null;
        __BooleanBox.TRUE=new __BooleanBox(true);__BooleanBox.FALSE=new __BooleanBox(false);
        __BooleanBox.prototype.booleanValue=function(){{return this.__value}};
        __BooleanBox.prototype.valueOf=__BooleanBox.prototype.booleanValue;
        __BooleanBox.prototype.toString=function(){{return this.__value?'true':'false'}};
        __BooleanBox.prototype.equals=function(other){{return other instanceof __BooleanBox&&this.__value===other.__value}};
        __BooleanBox.prototype.getClass=()=>null;__BooleanBox.prototype.toJSON=__BooleanBox.prototype.booleanValue;
        Packages.java.lang = __javaNamespace('java.lang', {{
          String:__JavaString,Thread:{{sleep:value=>__sleep(Number(value))}},
          System:{{
            currentTimeMillis:()=>Date.now(),
            nanoTime:()=>Math.round(Date.now()*1e6),
            lineSeparator:()=>'\\n',
            getProperty:key=>key==='line.separator'?'\\n'
              :key==='os.name'?'Linux'
              :key==='file.separator'?'/'
              :key==='java.version'?'0'
              :key==='user.dir'?'/'
              :key==='file.encoding'?'UTF-8':null,
            arraycopy:(source,sourceIndex,target,targetIndex,length)=>{{
              const values=Array.from(source||[]),start=Number(sourceIndex),
                offset=Number(targetIndex),count=Number(length);
              if(count<0||start<0||offset<0||start+count>values.length||
                 offset+count>Number(target.length||0))
                throw __wrappedJavaException(new __IndexOutOfBoundsException(
                  'arraycopy: length '+count+' is negative'));
              for(let index=0;index<count;index++)
                target[offset+index]=values[start+index];
            }},
            gc:()=>undefined
          }},
          Throwable:__Throwable,Exception:__Exception,RuntimeException:__RuntimeException,
          NullPointerException:__NullPointerException,
          IllegalArgumentException:__IllegalArgumentException,
          IndexOutOfBoundsException:__IndexOutOfBoundsException,
          NumberFormatException:__NumberFormatException,
          Byte:__Byte,Short:__Short,Integer:__Integer,Long:__Long,
          Float:__Float,Double:__Double,Boolean:__BooleanBox,Character:__Character,
          Class:__javaMember('java.lang.Class'),
          ClassLoader:__javaMember('java.lang.ClassLoader'),
          Runtime:__javaMember('java.lang.Runtime')
        }});
        Packages.java.net = __javaNamespace('java.net', {{URLEncoder:__URLEncoder}});
        Packages.java.util = __javaNamespace('java.util', {{
          ArrayList:__ArrayList,
          HashMap:function(value){{return __map(Object.assign({{}},value||{{}}))}},
          HashSet:function(value){{return __HashSet(value)}},
          Arrays:{{
            copyOfRange:(value,start,end)=>Array.from(value || []).slice(Number(start),Number(end)),
            copyOf:(value,newLength)=>{{
              const copied=Array.from(value || []).slice(0, Number(newLength));
              while (copied.length < Number(newLength)) copied.push(0);
              return __byteArray(copied);
            }},
            asList:(...values)=>values.length===1&&Array.isArray(values[0])?values[0]:values
          }},
          Base64:__Base64,
          UUID:{{randomUUID:()=>__uuid()}},
          stream:{{Collectors:{{
            toList:()=>({{__collector:'list'}}),toSet:()=>({{__collector:'set'}})
          }}}}
        }});
        function __streamBytes(value) {{
          if(value===null||value===undefined)return [];
          if(Array.isArray(value)||ArrayBuffer.isView(value))return __byteArray(value);
          if(typeof value.toByteArray==='function')return __byteArray(value.toByteArray());
          if(typeof value.__drain==='function')return value.__drain();
          return [];
        }}
        function __ByteArrayInputStream(value) {{
          this._bytes=__streamBytes(value);
          this._position=0;
          this._mark=0;
        }}
        __ByteArrayInputStream.prototype.__drain=function() {{
          const rest=this._bytes.slice(this._position);
          this._position=this._bytes.length;
          return rest;
        }};
        __ByteArrayInputStream.prototype.read=function(target,offset,length) {{
          if(target===undefined||target===null)
            return this._position<this._bytes.length
              ? this._bytes[this._position++] : -1;
          const start=Number(offset||0);
          const count=length===undefined?Number(target.length||0)-start:Number(length);
          if(count<=0)return 0;
          let written=0;
          while(written<count&&this._position<this._bytes.length)
            target[start+written++]=this._bytes[this._position++];
          return written===0?-1:written;
        }};
        __ByteArrayInputStream.prototype.available=function() {{
          return this._bytes.length-this._position;
        }};
        __ByteArrayInputStream.prototype.skip=function(count) {{
          const step=Math.max(0,Math.min(Number(count)||0,this.available()));
          this._position+=step;return step;
        }};
        __ByteArrayInputStream.prototype.mark=function(value) {{
          this._mark=this._position;
        }};
        __ByteArrayInputStream.prototype.reset=function() {{this._position=this._mark;}};
        __ByteArrayInputStream.prototype.markSupported=()=>true;
        __ByteArrayInputStream.prototype.close=()=>undefined;
        __ByteArrayInputStream.prototype.toString=()=>'java.io.ByteArrayInputStream';
        Packages.java.io = __javaNamespace('java.io', {{IOException:__IOException,EOFException:__EOFException,
          File:__javaMember('java.io.File'),
          ByteArrayInputStream:__ByteArrayInputStream,
          ByteArrayOutputStream:function(){{
          this._bytes=[];
          this.write=function(value,offset,length){{
            if(Array.isArray(value)||ArrayBuffer.isView(value)){{
              const start=Number(offset||0);
              const end=length===undefined?value.length:start+Number(length);
              for(let index=start;index<end;index++)this._bytes.push(__signedByte(value[index]));
            }} else {{
              this._bytes.push(__signedByte(value));
            }}
          }};
          this.writeBytes=value=>this.write(value);
          this.toByteArray=()=>__byteArray(this._bytes);
          this.size=()=>this._bytes.length;
          this.reset=()=>{{this._bytes=[];}};
          this.flush=()=>undefined;
          this.toString=charset=>__bytesstr(JSON.stringify(this._bytes),
            String(charset||'utf-8'));
          this.close=()=>undefined;
        }}}});
        function __ByteBuffer(bytes) {{
          const state={{bytes:__byteArray(bytes||[]),position:0,limit:0,mark:-1,
                      order:'BIG_ENDIAN'}};
          state.limit=state.bytes.length;
          const api={{}};
          const readNumber=size=>{{
            if(state.position+size>state.limit)
              throw __wrappedJavaException(new __IndexOutOfBoundsException(
                'ByteBuffer underflow at position '+state.position));
            const raw=state.bytes.slice(state.position,state.position+size);
            state.position+=size;
            const ordered=state.order==='LITTLE_ENDIAN'?raw.slice().reverse():raw;
            return ordered.reduce((total,item)=>total*256+(item<0?item+256:item),0);
          }};
          const writeNumber=(size,value)=>{{
            if(state.position+size>state.limit)
              throw __wrappedJavaException(new __IndexOutOfBoundsException(
                'ByteBuffer overflow at position '+state.position));
            const raw=[];
            let rest=Math.trunc(Number(value));
            for(let index=0;index<size;index++){{
              raw.push(__signedByte(((rest%256)+256)%256));
              rest=Math.floor(rest/256);
            }}
            const ordered=state.order==='LITTLE_ENDIAN'?raw:raw.reverse();
            for(let index=0;index<size;index++)state.bytes[state.position++]=ordered[index];
          }};
          api.array=()=>state.bytes;
          api.arrayOffset=()=>0;
          api.hasArray=()=>true;
          api.isDirect=()=>false;
          api.isReadOnly=()=>false;
          api.capacity=()=>state.bytes.length;
          api.limit=value=>{{if(value===undefined)return state.limit;
            state.limit=Math.max(0,Math.min(Number(value),state.bytes.length));return api;}};
          api.position=value=>{{if(value===undefined)return state.position;
            state.position=Number(value);return api;}};
          api.remaining=()=>Math.max(0,state.limit-state.position);
          api.hasRemaining=()=>state.limit>state.position;
          api.flip=()=>{{state.limit=state.position;state.position=0;state.mark=-1;return api;}};
          api.rewind=()=>{{state.position=0;state.mark=-1;return api;}};
          api.clear=()=>{{state.position=0;state.limit=state.bytes.length;state.mark=-1;return api;}};
          api.mark=()=>{{state.mark=state.position;return api;}};
          api.reset=()=>{{state.position=state.mark<0?0:state.mark;return api;}};
          api.order=value=>{{if(value===undefined)return state.order;
            state.order=String(value);return api;}};
          api.put=(value,offset,length)=>{{
            if(value===undefined)return api;
            if(typeof value==='number'){{writeNumber(1,value);return api;}}
            const values=__byteArray(value);
            const start=Number(offset||0);
            const count=length===undefined?values.length-start:Number(length);
            for(let index=0;index<count;index++)api.put(values[start+index]);
            return api;
          }};
          api.get=(target,offset,length)=>{{
            if(target===undefined)return readNumber(1);
            const start=Number(offset||0);
            const count=length===undefined?Number(target.length||0)-start:Number(length);
            for(let index=0;index<count;index++)target[start+index]=readNumber(1);
            return api;
          }};
          ['Int','Short','Long','Float','Double'].forEach(name=>{{
            const size=name==='Short'?2:name==='Int'||name==='Float'?4:8;
            const isFloat=name==='Float';
            api['put'+name]=value=>{{
              writeNumber(size,isFloat?Math.fround(Number(value)):value);
              return api;
            }};
            api['get'+name]=()=>{{
              const value=readNumber(size);
              return isFloat?Math.fround(value):value;
            }};
          }});
          api.putChar=value=>{{writeNumber(2,String(value).charCodeAt(0));return api;}};
          api.getChar=()=>String.fromCharCode(api.getShort());
          api.slice=()=>__ByteBuffer(state.bytes.slice(state.position,state.limit));
          api.duplicate=()=>{{const copy=__ByteBuffer(state.bytes.slice(0));
            copy.position(state.position).limit(state.limit).order(state.order);return copy;}};
          api.asReadOnlyBuffer=()=>api.duplicate();
          api.toString=()=>'java.nio.HeapByteBuffer[pos='+state.position
            +' lim='+state.limit+' cap='+state.bytes.length+']';
          return api;
        }}
        const __ByteBufferClass={{allocate:capacity=>
          __ByteBuffer(Array(Math.max(0,Math.trunc(Number(capacity)||0))).fill(0)),
          wrap:value=>__ByteBuffer(value),
          allocateDirect:capacity=>__ByteBufferClass.allocate(capacity)}};
        function __InflaterInputStream(stream,inflater) {{
          this._stream=stream;
          this._inflater=inflater||null;
          this._output=null;
          this._position=0;
        }}
        __InflaterInputStream.prototype.__inflated=function() {{
          if(this._output===null){{
            const raw=JSON.parse(__inflate(JSON.stringify(__streamBytes(this._stream)),
              Boolean(this._inflater&&this._inflater.__nowrap()),0));
            this._output=__byteArray(raw);
            this._position=0;
          }}
          return this._output;
        }};
        __InflaterInputStream.prototype.read=function(target,offset,length) {{
          const values=this.__inflated();
          if(target===undefined||target===null)
            return this._position<values.length?values[this._position++]:-1;
          const start=Number(offset||0);
          const count=length===undefined?Number(target.length||0)-start:Number(length);
          if(count<=0)return 0;
          let written=0;
          while(written<count&&this._position<values.length)
            target[start+written++]=values[this._position++];
          return written===0?-1:written;
        }};
        __InflaterInputStream.prototype.available=function() {{
          return Math.max(0,this.__inflated().length-this._position);
        }};
        __InflaterInputStream.prototype.skip=function(count) {{
          const step=Math.max(0,Math.min(Number(count)||0,this.available()));
          this._position+=step;return step;
        }};
        __InflaterInputStream.prototype.close=function() {{
          if(this._stream&&typeof this._stream.close==='function')this._stream.close();
          this._stream=null;
        }};
        __InflaterInputStream.prototype.markSupported=()=>false;
        function __randomUint32() {{
          const raw=JSON.parse(__random_bytes(4));
          return ((Number(raw[0])&255)|((Number(raw[1])&255)<<8)|
                  ((Number(raw[2])&255)<<16)|((Number(raw[3])&255)<<24))>>>0;
        }}
        function __SecureRandom(seed) {{
          let spare=null;
          this.nextInt=bound=>{{
            if(bound===undefined||bound===null)return __randomUint32()|0;
            const maximum=Math.trunc(Number(bound));
            if(maximum<=0)throw __wrappedJavaException(new __IllegalArgumentException(
              'bound must be positive'));
            if(maximum===1)return 0;
            if((maximum&(maximum-1))===0)
              return Number(BigInt(__randomUint32())%BigInt(maximum));
            for(;;){{
              const candidate=Number(BigInt(__randomUint32())%BigInt(maximum));
              if(candidate<maximum)return candidate;
            }}
          }};
          this.nextLong=()=>{{
            const raw=JSON.parse(__random_bytes(8));
            return Number(raw.reduce((total,item)=>
              total*256n+BigInt(Number(item)&255),0n)%(1n<<63n));
          }};
          this.nextDouble=()=>{{
            const high=Number(BigInt(__randomUint32())>>11);
            const low=Number(BigInt(__randomUint32())>>11);
            return (high*67108864+low)/9007199254740992;
          }};
          this.nextFloat=()=>Math.fround(this.nextDouble());
          this.nextBoolean=()=>(__randomUint32()&1)===1;
          this.nextBytes=value=>{{
            const raw=JSON.parse(__random_bytes(value?value.length:0));
            for(let index=0;index<raw.length;index++)
              value[index]=Number(raw[index])>127?Number(raw[index])-256:Number(raw[index]);
          }};
          this.nextGaussian=()=>{{
            if(spare!==null){{const value=spare;spare=null;return value;}}
            let first=0,second=0,square=0;
            do{{first=this.nextDouble()*2-1;second=this.nextDouble()*2-1;
                square=first*first+second*second;}}while(square>=1||square===0);
            const scale=Math.sqrt(-2*Math.log(square)/square);
            spare=second*scale;return first*scale;
          }};
          this.setSeed=()=>undefined;
          this.generateSeed=count=>__byteArray(JSON.parse(__random_bytes(Number(count)||0)));
          this.getAlgorithm=()=>'NativePRNG';
          this.toString=()=>'java.security.SecureRandom';
        }}
        function __encodedKeySpec(name,format) {{
          return function(value){{
            const bytes=__byteArray(value||[]);
            this.encoded=bytes;
            this.getEncoded=()=>bytes;
            this.getFormat=()=>format;
            this.toString=()=>name;
          }};
        }}
        const __MessageDigest = {{
          getInstance: algorithm => {{
            const name = String(algorithm);
            const pending = [];
            const append = values => {{ for (const item of values) pending.push(item); }};
            return {{
              getAlgorithm: () => name,
              getDigestLength: () => JSON.parse(__digest_bytes(name, '[]')).length,
              update: (value, offset, length) => {{
                const bytes = Array.from(value || []);
                if (offset === undefined || offset === null) append(bytes);
                else append(bytes.slice(Number(offset), Number(offset) + Number(length)));
              }},
              reset: () => {{ pending.length = 0; }},
              digest: value => {{
                const bytes = value === undefined || value === null
                  ? pending.slice() : pending.concat(Array.from(value));
                pending.length = 0;
                return __byteArray(JSON.parse(__digest_bytes(name, JSON.stringify(bytes))));
              }}
            }};
          }},
          isEqual: (left, right) => {{
            const a = Array.from(left || []), b = Array.from(right || []);
            if (a.length !== b.length) return false;
            let difference = 0;
            for (let index = 0; index < a.length; index += 1)
              difference |= Number(a[index]) ^ Number(b[index]);
            return difference === 0;
          }}
        }};
        function __Inflater(nowrap) {{
          const state = {{input: [], nowrap: Boolean(nowrap)}};
          this.setInput = (value, offset, length) => {{
            const bytes = Array.from(value || []);
            const slice = offset === undefined ? bytes
              : bytes.slice(Number(offset), Number(offset) + Number(length));
            for (const item of slice) state.input.push(item);
          }};
          this.inflate = out => {{
            const raw = JSON.parse(__inflate(
              JSON.stringify(state.input), state.nowrap, out ? out.length : 0));
            if (!out) return __byteArray(raw);
            const count = Math.min(raw.length, out.length);
            for (let index = 0; index < count; index += 1)
              out[index] = raw[index] > 127 ? raw[index] - 256 : raw[index];
            return count;
          }};
          this.finished = () => true;
          this.needsInput = () => false;
          this.getRemaining = () => 0;
          this.__nowrap = () => state.nowrap;
          this.reset = () => {{ state.input.length = 0; }};
          this.end = () => {{ state.input.length = 0; }};
        }}
        Packages.java.security = __javaNamespace('java.security', {{
          MessageDigest:__MessageDigest,
          SecureRandom:__SecureRandom,
          KeyFactory:{{getInstance:algorithm=>({{
            generatePublic:spec=>({{algorithm:String(algorithm),encoded:spec.encoded,
              getAlgorithm:()=>String(algorithm),getEncoded:()=>spec.encoded}}),
            generatePrivate:spec=>({{algorithm:String(algorithm),encoded:spec.encoded,
              getAlgorithm:()=>String(algorithm),getEncoded:()=>spec.encoded}})
          }})}},
          spec:__javaNamespace('java.security.spec', {{
            X509EncodedKeySpec:__encodedKeySpec('X509EncodedKeySpec','X.509'),
            PKCS8EncodedKeySpec:__encodedKeySpec('PKCS8EncodedKeySpec','PKCS#8'),
            RSAPublicKeySpec:function(modulus,exponent){{
              this.getModulus=()=>modulus;this.getPublicExponent=()=>exponent;
              this.toString=()=>'RSAPublicKeySpec';
            }},
            RSAPrivateKeySpec:function(modulus,exponent){{
              this.getModulus=()=>modulus;this.getPrivateExponent=()=>exponent;
              this.toString=()=>'RSAPrivateKeySpec';
            }},
            ECGenParameterSpec:function(name){{
              this.getName=()=>String(name);this.toString=()=>'ECGenParameterSpec';
            }},
            KeySpec:__javaInterface('java.security.spec.KeySpec')
          }}),
          interfaces:__javaNamespace('java.security.interfaces', {{
            RSAKey:__javaInterface('java.security.interfaces.RSAKey'),
            RSAPublicKey:__javaInterface('java.security.interfaces.RSAPublicKey'),
            RSAPrivateKey:__javaInterface('java.security.interfaces.RSAPrivateKey'),
            RSAPrivateCrtKey:__javaInterface('java.security.interfaces.RSAPrivateCrtKey'),
            ECKey:__javaInterface('java.security.interfaces.ECKey'),
            ECPublicKey:__javaInterface('java.security.interfaces.ECPublicKey'),
            ECPrivateKey:__javaInterface('java.security.interfaces.ECPrivateKey'),
            DSAKey:__javaInterface('java.security.interfaces.DSAKey')
          }})
        }});
        Packages.java.util.zip = __javaNamespace('java.util.zip', {{
          Inflater:__Inflater,InflaterInputStream:__InflaterInputStream
        }});
        Packages.java.nio = __javaNamespace('java.nio', {{ByteBuffer:__ByteBufferClass}});
        Packages.javax.crypto = __javaNamespace('javax.crypto', {{Cipher:__Cipher,Mac:__Mac}});
        Packages.javax.crypto.SecretKeyFactory = {{getInstance:algorithm=>({{
          generateSecret:spec=>({{bytes:spec.bytes,algorithm:String(algorithm)}})
        }})}};
        Packages.javax.crypto.spec = __javaNamespace('javax.crypto.spec', {{
          SecretKeySpec:__SecretKeySpec,IvParameterSpec:__IvParameterSpec,
          DESKeySpec:__DESKeySpec,GCMParameterSpec:__GCMParameterSpec
        }});
        Packages.javax.xml = __javaNamespace('javax.xml', {{
          bind:{{DatatypeConverter:__DatatypeConverter}}
        }});
        Packages.android.util = __javaNamespace('android.util', {{Base64:__Base64}});
        Packages.android.text = __javaNamespace('android.text', {{
          TextUtils:{{isEmpty:value=>value == null || String(value).length === 0}}
        }});
        const __CookieManager = {{
          getInstance:()=>({{
            getCookie:url=>{{const value=__cookie_get(String(url||''));return value?value:null;}},
            setCookie:(url,value)=>{{__cookie_set(String(url||''),String(value||''));}},
            removeAllCookies:()=>{{__cookie_clear();}},
            removeSessionCookies:()=>undefined,
            setAcceptCookie:()=>undefined,
            setAcceptThirdPartyCookies:()=>undefined,
            acceptCookie:()=>true,
            hasCookies:()=>Boolean(__cookie_get(String(baseUrl||''))),
            flush:()=>undefined
          }})
        }};
        Packages.android.webkit = __javaNamespace('android.webkit', {{
          CookieManager:__CookieManager
        }});
        Packages.android.graphics = __javaNamespace('android.graphics', {{
          BitmapFactory:{{
            decodeByteArray:(value,start,length)=>__bitmap(__bitmap_decode(
              JSON.stringify(Array.from(value || []).slice(Number(start),Number(start)+Number(length))))),
            decodeStream:stream=>{{
              // Android 的 decodeStream 对不可解码的输入返回 null，而不是抛异常。
              const bytes=__streamBytes(stream);
              if(!bytes.length)return null;
              try {{ return __bitmap(__bitmap_decode(JSON.stringify(bytes))); }}
              catch(ignoreBrokenImage) {{ return null; }}
            }}
          }},
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
        }});
        Packages.cn.hutool = __javaNamespace('cn.hutool', {{
          core:{{codec:{{Base64:__Base64}},util:{{}}}},crypto:{{digest:{{}}}}
        }});
        Packages.cn.hutool.core.util.ZipUtil = {{unGzip:(value,charset)=>{{
          const raw=__gzip(JSON.stringify(Array.from(value || [])), String(charset || ''));
          return charset ? raw : __byteArray(JSON.parse(raw));
        }}}};
        Packages.cn.hutool.crypto.digest.DigestUtil = {{
          md5:value=>__byteArray(JSON.parse(__md5bytes(String(value)))),
          md5Hex:value=>__digest(String(value), 'MD5'),
          digestHex:value=>__digest(String(value), 'MD5'),
          sha1:value=>__byteArray(JSON.parse(__digest_bytes(
            'SHA-1', JSON.stringify(String(value))))),
          sha1Hex:value=>__digest(String(value), 'SHA-1'),
          sha256:value=>__byteArray(JSON.parse(__digest_bytes(
            'SHA-256', JSON.stringify(String(value))))),
          sha256Hex:value=>__digest(String(value), 'SHA-256'),
          sha512Hex:value=>__digest(String(value), 'SHA-512')
        }};
        Packages.okhttp3 = __javaNamespace('okhttp3', (() => {{
          function RequestBuilder() {{
            this._url=''; this._headers={{}};
            this.url=value=>{{this._url=String(value);return this}};
            this.get=()=>this;
            this.addHeader=(name,value)=>{{this._headers[String(name)]=String(value);return this}};
            this.build=()=>({{url:this._url,headers:this._headers}});
          }}
          function OkHttpClient() {{
            this.newCall=request=>({{execute:()=>{{
              const raw=__request(String(request.url)+','+JSON.stringify({{
                method:'GET',headers:request.headers||{{}}
              }}));
              const response=__response(raw);
              return {{body:()=>({{bytes:()=>__responseBytes(raw)}}),
                       code:()=>response.code(),headers:()=>response.headers(),
                       close:()=>undefined}};
            }}}});
          }}
          return {{OkHttpClient:OkHttpClient,Request:{{Builder:RequestBuilder}}}};
        }})());
        Packages.io.legado = __javaNamespace('io.legado', {{
          app: {{help: {{http: {{}}}}, model: {{analyzeRule: {{}}}}}}
        }});
        Packages.io.legado.app.help.http.CookieStore = {{
          setCookie:(url,value)=>__cookie_set(String(url||''),String(value||'')),
          getCookie:url=>__cookie_get(String(url||''))||'',
          replaceCookie:(url,value)=>__cookie_replace(
            String(url||''),String(value||'')),
          removeCookie:url=>__cookie_remove(String(url||'')),
          clear:()=>{{__cookie_clear();}}
        }};
        Packages.io.legado.app.help.http.StrResponse = (url,body)=>__response(JSON.stringify({{
          body:String(body == null ? '' : body),headers:{{}},status:200,url:String(url || baseUrl)
        }}));
        Packages.io.legado.app.model.analyzeRule.AnalyzeRule = ()=>({{
          cacheFile:url=>__ajax(String(url))
        }});
        __global.Packages = Packages;
        function __importValue(target,value) {{
          value=__unwrapJava(value);
          if (!value) return;
          const known=[[__JavaString,'String'],[__Cipher,'Cipher'],[__Mac,'Mac'],
            [__SecretKeySpec,'SecretKeySpec'],[__IvParameterSpec,'IvParameterSpec'],
            [__DESKeySpec,'DESKeySpec'],[__GCMParameterSpec,'GCMParameterSpec'],
            [__Base64,'Base64'],
            [__MessageDigest,'MessageDigest'],[__Inflater,'Inflater'],
            [__URLEncoder,'URLEncoder'],[__DatatypeConverter,'DatatypeConverter']];
          const match=known.find(item=>item[0]===value);
          if(match) {{target[match[1]]=value;return;}}
          if(typeof value==='object') Object.keys(value).forEach(key=>target[key]=value[key]);
        }}
        function JavaImporter(...initial) {{
          const target = {{}};
          target.importPackage = (...packages) => {{
            packages.forEach(pkg => __importValue(target,pkg));
            return target;
          }};
          initial.forEach(value=>__importValue(target,value));
          return target;
        }}
        function importPackage(...packages) {{
          packages.forEach(pkg => {{ if (pkg) Object.keys(pkg).forEach(key => __global[key]=pkg[key]); }});
          return __global;
        }}
        function importClass(...classes) {{
          classes.forEach(value => {{
            value=__unwrapJava(value);
            if (value === org.jsoup.Jsoup) __global.Jsoup = value;
            else __importValue(__global,value);
          }});
          return __global;
        }}
        function sleep(milliseconds) {{ return __sleep(Number(milliseconds)); }}
        function __file(value) {{
          const path=__file_path(String(value || ''));
          return {{
            path:path,absolutePath:path,
            exists:()=>__file_read(String(value || ''),true,'') !== '',
            readBytes:()=>{{const raw=__file_read(String(value || ''),false,'__bytes__');return raw === '' ? null : __byteArray(JSON.parse(raw));}},
            toString:()=>path
          }};
        }}
        function __oneArchive(args,value,name) {{
          if(args.length!==1)throw new TypeError("Can't find method JsExtensions."+name);
          if(value===null)throw __wrappedJavaException(new __NullPointerException(
            'Parameter specified as non-null is null: zipPath'));
          return __unarchive_file(String(value));
        }}
        function __asymmetric(algorithm) {{
          const generated=JSON.parse(__asym_generate(String(algorithm)));
          const target={{_algorithm:String(algorithm),_public:generated.publicKey,
            _private:generated.privateKey}};
          target.setPublicKey=function(value){{this._public=value;return this}};
          target.setPrivateKey=function(value){{this._private=value;return this}};
          target.encrypt=function(value,usePublicKey){{return __byteArray(JSON.parse(__asym_op(
            'encrypt',this._algorithm,JSON.stringify(this._public),JSON.stringify(this._private),
            JSON.stringify(value),usePublicKey===undefined?true:Boolean(usePublicKey))))}};
          target.encryptHex=function(value,usePublicKey){{return __asym_op(
            'encryptHex',this._algorithm,JSON.stringify(this._public),JSON.stringify(this._private),
            JSON.stringify(value),usePublicKey===undefined?true:Boolean(usePublicKey))}};
          target.encryptBase64=function(value,usePublicKey){{return __asym_op(
            'encryptBase64',this._algorithm,JSON.stringify(this._public),JSON.stringify(this._private),
            JSON.stringify(value),usePublicKey===undefined?true:Boolean(usePublicKey))}};
          target.decrypt=function(value,usePublicKey){{return __byteArray(JSON.parse(__asym_op(
            'decrypt',this._algorithm,JSON.stringify(this._public),JSON.stringify(this._private),
            JSON.stringify(value),usePublicKey===undefined?true:Boolean(usePublicKey))))}};
          target.decryptStr=function(value,usePublicKey){{return __asym_op(
            'decryptStr',this._algorithm,JSON.stringify(this._public),JSON.stringify(this._private),
            JSON.stringify(value),usePublicKey===undefined?true:Boolean(usePublicKey))}};
          return target;
        }}
        function __signer(algorithm) {{
          const generated=JSON.parse(__sign_generate(String(algorithm)));
          const target={{_algorithm:String(algorithm),_public:generated.publicKey,
            _private:generated.privateKey}};
          target.setPublicKey=function(value){{this._public=value;return this}};
          target.setPrivateKey=function(value){{this._private=value;return this}};
          target.sign=function(value){{return __byteArray(JSON.parse(__sign_op('sign',this._algorithm,
            JSON.stringify(this._public),JSON.stringify(this._private),JSON.stringify(value),'')))}};
          target.signHex=function(value){{return __sign_op('signHex',this._algorithm,
            JSON.stringify(this._public),JSON.stringify(this._private),JSON.stringify(value),'')}};
          target.verify=function(value,signature){{return __sign_op('verify',this._algorithm,
            JSON.stringify(this._public),JSON.stringify(this._private),JSON.stringify(value),
            JSON.stringify(signature))==='true'}};
          return target;
        }}
        function __font(handle) {{
          if (!handle) return null;
          const target={{_handle:String(handle)}};
          target.getNameById=value=>__font_op(target._handle,'name',String(value));
          target.inLimit=value=>__font_op(target._handle,'limit',String(value))==='true';
          target.getGlyfByCode=value=>__font_op(target._handle,'glyph',String(value));
          target.getCodeByGlyf=value=>Number(__font_op(target._handle,'code',String(value)));
          Object.defineProperties(target,{{
            codeToGlyph:{{get:()=>__map(JSON.parse(__font_op(target._handle,'codeMap','')))}},
            glyphToCode:{{get:()=>__map(JSON.parse(__font_op(target._handle,'glyphMap','')))}}
          }});
          return target;
        }}
        var Buffer = {{
          concat: arrays => Array.prototype.concat.apply([], Array.from(arrays || []).map(x => Array.from(x)))
        }};
        var java = {{
          evalJS: function(jsValue,resultValue) {{
            if(arguments.length!==1&&arguments.length!==2)
              throw new TypeError("Can't find method evalJS");
            if(jsValue===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: jsStr'));
            if(arguments.length===2)return (function(result){{return eval(String(jsValue));}})(resultValue);
            return eval(String(jsValue));
          }},
          ajax: function(value) {{
            if (arguments.length!==1) throw new TypeError("Can't find method JsExtensions.ajax");
            if (value===null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null: url'));
            const target=Array.isArray(value)?(value.length?value[0]:null):value;
            return __ajax(String(target),JSON.stringify(
              Object.assign({{}},source.getHeaderMap(true))));
          }},
          getString: (rule, content, isUrl) =>
            __get_string(String(rule), content === undefined && __contentChanged
              ? __currentContent : content, Boolean(isUrl)),
          getElement: rule => {{
            const values=JSON.parse(__get_elements(
              String(rule), __contentChanged ? __currentContent : undefined));
            if(values[0]&&typeof values[0]==='object'&&values[0].__domHandle)
              return __domNode(values[0].__domHandle);
            const markup=String(values[0] || '');
            if (/<[A-Za-z][^>]*>/.test(markup)) {{
              const root=__dom_parse(markup);
              const handles=JSON.parse(__dom_query(root,'*'));
              if(handles.length) return __domNode(handles[0]);
            }}
            if(values[0]&&typeof values[0]==='object'&&values[0].__jxType)
              return __jxNode(values[0]);
            return __element(markup);
          }},
          getStringList: (rule, content, isUrl) =>
            JSON.parse(__get_list(String(rule),
              content === undefined && __contentChanged ? __currentContent : content,
              Boolean(isUrl))),
          getElements: (rule, content) =>
            __ruleElementList(JSON.parse(__get_elements(String(rule),
              content === undefined && __contentChanged ? __currentContent : content))),
          setContent: function(value,parent) {{
            if(arguments.length!==1&&arguments.length!==2)
              throw new TypeError("Can't find method AnalyzeRule.setContent");
            if(value===null)throw new Error('内容不可空（Content cannot be null）');
            __currentContent = value;
            __contentChanged = true;
            if (arguments.length===2&&parent != null) {{
              baseUrl=String(parent);java.baseUrl=baseUrl;
            }}
            return java;
          }},
          get: function(value, headers) {{
            if (arguments.length!==1 && arguments.length!==2)
              throw new TypeError("Can't find method AnalyzeRule.get");
            if (value===null || (arguments.length===2 && headers==null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            if (__hostKind==='url') {{
              if (arguments.length!==1) throw new TypeError("Can't find method AnalyzeUrl.get");
              return __java_get(String(value));
            }}
            if (arguments.length===2) return __response(__request(
              String(value)+','+JSON.stringify({{method:'GET',headers:headers,
                __followRedirects:false}})));
            return __java_get(String(value));
          }},
          put: function(name,value) {{
            if (arguments.length!==2) throw new TypeError("Can't find method AnalyzeRule.put");
            if (name===null || value===null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null'));
            const text=String(value);return __java_put(String(name),text);
          }},
          post: function(url,body,headers) {{
            if (arguments.length!==3) throw new TypeError("Can't find method JsExtensions.post");
            if (url===null || body===null || headers==null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null'));
            return __http('POST',url,body,headers,false);
          }},
          head: function(url,headers) {{
            if (arguments.length!==2) throw new TypeError("Can't find method JsExtensions.head");
            if (url===null || headers==null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null'));
            return __http('HEAD',url,'',headers,false);
          }},
          ajaxAll: function(urls) {{
            if (arguments.length!==1) throw new TypeError("Can't find method JsExtensions.ajaxAll");
            if (urls===null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null: urlList'));
            return JSON.parse(__ajax_all(JSON.stringify(Array.from(urls)),JSON.stringify(
              Object.assign({{}},source.getHeaderMap(true)))))
              .map(raw => __response(JSON.stringify(raw)));
          }},
          getStrResponse: function(jsStr,sourceRegex,useWebView) {{
            if(arguments.length>3)throw new TypeError("Can't find method AnalyzeUrl.getStrResponse");
            return __response(__request_mode(String(java.ruleUrl||java.url||baseUrl),
              JSON.stringify(Object.assign({{}},java.headerMap)),
              arguments.length<3?true:Boolean(useWebView),
              jsStr==null?null:String(jsStr),sourceRegex==null?null:String(sourceRegex)));
          }},
          getByteArray: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.getByteArray");
            const embedded=__analyzeUrlDataBytes();if(embedded!==null)return embedded;
            return __responseBytes(__request_mode(String(java.ruleUrl||java.url||baseUrl),
              JSON.stringify(Object.assign({{}},java.headerMap)),false,null,null));
          }},
          getInputStream: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.getInputStream");
            const embedded=__analyzeUrlDataBytes();if(embedded!==null)return __inputStream(embedded);
            return __inputStream(__responseBytes(__request_mode(String(java.ruleUrl||java.url||baseUrl),
              JSON.stringify(Object.assign({{}},java.headerMap)),false,null,null)));
          }},
          getResponse: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.getResponse");
            return __response(__responseKind(__request_mode(String(java.ruleUrl||java.url||baseUrl),
              JSON.stringify(Object.assign({{}},java.headerMap)),false,null,null),'okhttp'));
          }},
          getGlideUrl: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.getGlideUrl");
            const stored=cookie.cookieToMap(__cookie_get(String(java.url||baseUrl)));
            const temporary=cookie.cookieToMap(java.headerMap.Cookie||'');
            const merged=Object.assign({{}},stored,temporary);
            const cookieText=cookie.mapToCookie(merged);
            if(cookieText)java.headerMap.Cookie=cookieText;
            if(__sourceData.enabledCookieJar===true)java.headerMap.CookieJar='1';
            const target={{url:String(java.url||baseUrl),headers:java.headerMap}};
            target.toString=()=>target.url;target.getCacheKey=()=>target.url;
            target.getHeaders=()=>target.headers;return target;
          }},
          getHeaderMap: () => java.headerMap,
          getCookie: function(url,name) {{
            if (arguments.length!==1 && arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.getCookie");
            if (url===null) throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: tag'));
            return name == null ? __cookie_get(String(url)) : cookie.getKey(url,name);
          }},
          getKey: (url, name) => cookie.getKey(url, name),
          setCookie: (url, value) => cookie.setCookie(url, value),
          replaceCookie: (url, value) => cookie.replaceCookie(url, value),
          removeCookie: url => cookie.removeCookie(url),
          md5Encode: value => __md5(String(value)),
          md5Encode16: value => __md5(String(value)).slice(8, 24),
          base64Decode: (value, charsetOrFlags) => typeof charsetOrFlags === 'string'
            ? __bytesstr(__b64bytes(String(value)), String(charsetOrFlags))
            : __b64decode_flags(String(value),Number(charsetOrFlags || 0)),
          base64Encode: (value, flags) => __b64encode_flags(
            String(value), flags === undefined ? 2 : Number(flags)),
          base64DecodeToByteArray: (value, flags) => {{
            if (value == null || String(value).trim() === '') return null;
            return __byteArray(JSON.parse(__b64decode_flags(String(value),Number(flags || 0),true)));
          }},
          hexDecodeToByteArray: value => __byteArray(JSON.parse(__hexbytes(String(value)))),
          hexDecodeToString: value => __bytesstr(__hexbytes(String(value)), 'utf-8'),
          hexEncodeToString: value => Array.isArray(value)
            ? value.map(x => (Number(x) & 255).toString(16).padStart(2, '0')).join('')
            : Array.from(unescape(encodeURIComponent(String(value))))
                .map(x => x.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
          strToBytes: (value, charset) => __isLatin1(charset)
            ? __latin1StringToBytes(value)
            : __byteArray(JSON.parse(__strbytes(String(value), String(charset || 'utf-8')))),
          bytesToStr: (value, charset) => __isLatin1(charset)
            ? __latin1BytesToString(value)
            : __bytesstr(JSON.stringify(Array.from(value || [])), String(charset || 'utf-8')),
          digestHex: (value, algorithm) => __digest(String(value), String(algorithm || 'MD5')),
          digestBase64Str: (value, algorithm) => __digest_base64(
            String(value), String(algorithm || 'MD5')),
          HMacBase64: (value, algorithm, secret) => __hmac(String(value), String(algorithm), String(secret), true),
          HMacHex: (value, algorithm, secret) => __hmac(String(value), String(algorithm), String(secret), false),
          aesBase64DecodeToString: (value, key, transformation, iv) =>
            __crypto('decryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          aesBase64DecodeToByteArray: (value, key, transformation, iv) => __byteArray(JSON.parse(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)))),
          aesDecodeToByteArray: (value, key, transformation, iv) => __byteArray(JSON.parse(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''),
              __strbytes(String(value), 'utf-8')))),
          aesDecodeToString: (value, key, transformation, iv) => __bytesstr(
            __crypto('decryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''),
              __strbytes(String(value), 'utf-8')), 'utf-8'),
          aesEncodeToByteArray: (value, key, transformation, iv) => __byteArray(JSON.parse(
            __crypto('encryptBytes', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)))),
          // Historical JsEncodeUtils contains this counter-intuitive implementation:
          // aesEncodeToString delegates to decryptStr rather than encrypting.
          aesEncodeToString: (value, key, transformation, iv) =>
            __crypto('decryptStr', String(transformation || 'AES/CBC/PKCS5Padding'),
              JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          aesEncodeToBase64String: (value, key, transformation, iv) =>
            __crypto('encryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          aesEncodeToBase64ByteArray: (value, key, transformation, iv) => __byteArray(JSON.parse(__strbytes(
            __crypto('encryptStr', String(transformation || 'AES/CBC/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)), 'utf-8'))),
          aesDecodeArgsBase64Str: (value, key, mode, padding, iv) => __crypto(
            'decryptStr','AES/'+String(mode)+'/'+String(padding),
            __b64decode_flags(String(key),2,true),__b64decode_flags(String(iv),2,true),JSON.stringify(value)),
          aesEncodeArgsBase64Str: (value, key, mode, padding, iv) => __crypto(
            'encryptStr','AES/'+String(mode)+'/'+String(padding),JSON.stringify(String(key)),
            JSON.stringify(String(iv)),JSON.stringify(value)),
          desDecodeToString: (value, key, transformation, iv) => __crypto(
            'decryptStr',String(transformation || 'DES/CBC/PKCS5Padding'),JSON.stringify(key),JSON.stringify(iv || ''),JSON.stringify(value)),
          desBase64DecodeToString: (value, key, transformation, iv) => __crypto(
            'decryptStr',String(transformation || 'DES/CBC/PKCS5Padding'),JSON.stringify(key),JSON.stringify(iv || ''),JSON.stringify(value)),
          desEncodeToString: (value, key, transformation, iv) => __bytesstr(
            __crypto('encryptBytes',String(transformation || 'DES/CBC/PKCS5Padding'),JSON.stringify(key),JSON.stringify(iv || ''),JSON.stringify(value)),'utf-8'),
          desEncodeToBase64String: (value, key, transformation, iv) =>
            __crypto('encryptStr', String(transformation || 'DES/ECB/PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          tripleDESDecodeStr: (value, key, mode, padding, iv) => __crypto(
            'decryptStr','DESede/'+String(mode || 'CBC')+'/'+String(padding || 'PKCS5Padding'),JSON.stringify(key),JSON.stringify(iv || ''),JSON.stringify(value)),
          tripleDESDecodeArgsBase64Str: (value, key, mode, padding, iv) => __crypto(
            'decryptStr','DESede/'+String(mode || 'CBC')+'/'+String(padding || 'PKCS5Padding'),
            __b64decode_flags(String(key),2,true),JSON.stringify(String(iv || '')),JSON.stringify(value)),
          tripleDESEncodeBase64Str: (value, key, mode, padding, iv) =>
            __crypto('encryptStr', 'DESede/' + String(mode || 'CBC') + '/' + String(padding || 'PKCS5Padding'), JSON.stringify(key), JSON.stringify(iv || ''), JSON.stringify(value)),
          tripleDESEncodeArgsBase64Str: (value, key, mode, padding, iv) => __crypto(
            'encryptStr','DESede/'+String(mode || 'CBC')+'/'+String(padding || 'PKCS5Padding'),
            __b64decode_flags(String(key),2,true),JSON.stringify(String(iv || '')),JSON.stringify(value)),
          randomUUID: () => __uuid(),
          timeFormat: value => __time_format(
            value instanceof Date ? value.getTime() : value,
            'yyyy/MM/dd HH:mm', false),
          timeFormatUTC: (value, pattern, offset) => __time_format(
            value instanceof Date ? value.getTime() : value,
            String(pattern || 'yyyy-MM-dd HH:mm:ss'),
            offset === undefined ? true : Number(offset)),
          toNumChapter: value => value == null ? null : __num_chapter(String(value)),
          t2s: value => __t2s(String(value)),
          s2t: value => __s2t(String(value)),
          utf8ToGbk: value => __utf8_to_gbk(String(value)),
          htmlFormat: value => __html_format(String(value)),
          getZipByteArrayContent: function(value, path) {{
            if(arguments.length!==2)throw new TypeError("Can't find method JsExtensions.getZipByteArrayContent");
            if(value===null||path===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null'));
            const raw=__zip_content(String(value),String(path),true,'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
            return raw === '' ? null : __byteArray(JSON.parse(raw));
          }},
          getZipStringContent: function(value, path, charset) {{
            if(arguments.length!==2&&arguments.length!==3)
              throw new TypeError("Can't find method JsExtensions.getZipStringContent");
            if(value===null||path===null||(arguments.length===3&&charset===null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            return __zip_content(String(value),String(path),false,
              arguments.length===3?String(charset):'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          get7zByteArrayContent: function(value, path) {{
            if(arguments.length!==2)throw new TypeError("Can't find method JsExtensions.get7zByteArrayContent");
            if(value===null||path===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null'));
            const raw=__archive_content('7z',String(value),String(path),true,'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
            return raw === '' ? null : __byteArray(JSON.parse(raw));
          }},
          get7zStringContent: function(value, path, charset) {{
            if(arguments.length!==2&&arguments.length!==3)
              throw new TypeError("Can't find method JsExtensions.get7zStringContent");
            if(value===null||path===null||(arguments.length===3&&charset===null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            return __archive_content('7z',String(value),String(path),false,
              arguments.length===3?String(charset):'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          getRarByteArrayContent: function(value, path) {{
            if(arguments.length!==2)throw new TypeError("Can't find method JsExtensions.getRarByteArrayContent");
            if(value===null||path===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null'));
            const raw=__archive_content('rar',String(value),String(path),true,'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
            return raw === '' ? null : __byteArray(JSON.parse(raw));
          }},
          getRarStringContent: function(value, path, charset) {{
            if(arguments.length!==2&&arguments.length!==3)
              throw new TypeError("Can't find method JsExtensions.getRarStringContent");
            if(value===null||path===null||(arguments.length===3&&charset===null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            return __archive_content('rar',String(value),String(path),false,
              arguments.length===3?String(charset):'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          getFile: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method JsExtensions.getFile");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: path'));
            return __file(String(value));
          }},
          readFile: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method JsExtensions.readFile");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: path'));
            const raw=__file_read(String(value),false,'__bytes__');
            return raw === '' ? null : __byteArray(JSON.parse(raw));
          }},
          readTxtFile: function(value, charset) {{
            if(arguments.length!==1&&arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.readTxtFile");
            if(value===null||(arguments.length===2&&charset===null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            return __file_read(String(value),false,
              arguments.length===2?String(charset):'');
          }},
          deleteFile: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method JsExtensions.deleteFile");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: path'));
            return __file_delete(String(value));
          }},
          downloadFile: function(value,url) {{
            if (arguments.length!==1 && arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.downloadFile");
            if (value===null || (arguments.length===2 && url===null))
              throw __wrappedJavaException(new __NullPointerException(
                'Parameter specified as non-null is null'));
            return __download_file(String(value),arguments.length===2?String(url):'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          cacheFile: function(url,saveTime) {{
            if (arguments.length!==1 && arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.cacheFile");
            if (url===null) throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: urlStr'));
            return __cache_file(String(url),arguments.length===2?Number(saveTime):0,
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          unzipFile: function(value) {{return __oneArchive(arguments,value,'unzipFile');}},
          un7zFile: function(value) {{return __oneArchive(arguments,value,'un7zFile');}},
          unrarFile: function(value) {{return __oneArchive(arguments,value,'unrarFile');}},
          unArchiveFile: function(value) {{return __oneArchive(arguments,value,'unArchiveFile');}},
          getTxtInFolder: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method JsExtensions.getTxtInFolder");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: path'));
            return __text_in_folder(String(value));
          }},
          queryTTF: value => value == null ? null : __font(__font_query(String(value))),
          queryBase64TTF: value => value == null || String(value).trim() === ''
            ? null : __font(__font_query(String(value))),
          replaceFont: (text, font1, font2) => __font_replace(String(text),
            font1 && font1._handle || '',font2 && font2._handle || ''),
          getBook: () => book,
          getSource: () => source,
          getVerificationCode: image => __verification(String(image || '')),
          lang: 'zh-CN',
          encodeURI: (value, charset) => __encode_uri(String(value),String(charset || 'utf-8')),
          log: value => value,
          logType: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method JsExtensions.logType");
            return undefined;
          }},
          toast: value => undefined,
          longToast: value => undefined,
          refreshExplore: () => undefined,
          refreshTocUrl: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeRule.refreshTocUrl");
            Object.assign(book,JSON.parse(__refresh_book()));return undefined;
          }},
          initUrl: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.initUrl");
            java.ruleUrl=__initialAnalyzeUrl;java.url=__initialAnalyzeUrl;
            return undefined;
          }},
          reGetBook: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeRule.reGetBook");
            Object.assign(book,JSON.parse(__reget_book()));return undefined;
          }},
          refreshBook: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeRule.refreshBook");
            Object.assign(book,JSON.parse(__refresh_book()));return undefined;
          }},
          toURL: function(value, parent) {{
            if(arguments.length!==1&&arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.toURL");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: url'));
            const absolute = JSON.parse(__to_url(String(value),
              arguments.length===1?'':String(parent==null?'':parent)));
            absolute.searchParams=absolute.searchParams == null ? null : __map(absolute.searchParams);
            return absolute;
          }},
          setRedirectUrl: function(value) {{
            if(arguments.length!==1)throw new TypeError(
              "Can't find method AnalyzeRule.setRedirectUrl");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: url'));
            const text=String(value),match=text.match(
              /^([A-Za-z][A-Za-z0-9+.-]*):\\/\\/([^\\/?#]+)([^?#]*)/);
            if(!match)return java.redirectUrl;
            const protocol=match[1],host=match[2].replace(/:\\d+$/,''),path=match[3]||'';
            const target={{host:host,path:path,protocol:protocol,toString:()=>text,
              getHost:()=>host,getPath:()=>path,getProtocol:()=>protocol}};
            java.redirectUrl=target;return target;
          }},
          splitSourceRule: function(value,allInOne) {{
            if(arguments.length!==1&&arguments.length!==2)
              throw new TypeError("Can't find method AnalyzeRule.splitSourceRule");
            const text=value==null?'':String(value),all=arguments.length===2?Boolean(allInOne):false;
            const lead=String(__currentContent==null?result:__currentContent).trim();
            const isJson=lead.startsWith('{{')||lead.startsWith('[');
            const stages=JSON.parse(__split_source_rule(text,all,isJson,__ruleRegexMode));
            if(all&&text.startsWith(':'))__ruleRegexMode=true;
            return stages;
          }},
          splitSourceRuleCacheString: function(value) {{
            if(arguments.length!==1)throw new TypeError(
              "Can't find method AnalyzeRule.splitSourceRuleCacheString");
            const text=value==null?'':String(value);
            if(!Object.prototype.hasOwnProperty.call(__sourceRuleCache,text))
              __sourceRuleCache[text]=java.splitSourceRule(text,false);
            return __sourceRuleCache[text];
          }},
          setBaseUrl: function(value) {{
            if(arguments.length!==1)throw new TypeError(
              "Can't find method AnalyzeRule.setBaseUrl");
            if(value!==null){{baseUrl=String(value);java.baseUrl=baseUrl;}}
            return java;
          }},
          importScript: function(value) {{
            if (arguments.length!==1) throw new TypeError("Can't find method JsExtensions.importScript");
            if (value===null) throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: path'));
            return __import_script(String(value),JSON.stringify(
              Object.assign({{}},source.getHeaderMap(true))));
          }},
          webView: function(htmlValue,url,js) {{
            if(arguments.length!==3)throw new TypeError("Can't find method JsExtensions.webView");
            return __webview(htmlValue==null?null:String(htmlValue),
              url==null?null:String(url),js==null?null:String(js),'','',JSON.stringify(
                Object.assign({{}},source.getHeaderMap(true))));
          }},
          webViewGetOverrideUrl: function(htmlValue,url,js,regex) {{
            if(arguments.length!==4)throw new TypeError(
              "Can't find method JsExtensions.webViewGetOverrideUrl");
            if(regex===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: overrideUrlRegex'));
            return __webview(htmlValue==null?null:String(htmlValue),
              url==null?null:String(url),js==null?null:String(js),'',String(regex),
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          webViewGetSource: function(htmlValue,url,js,sourceRegex) {{
            if(arguments.length!==4)throw new TypeError(
              "Can't find method JsExtensions.webViewGetSource");
            if(sourceRegex===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: sourceRegex'));
            return __webview(htmlValue==null?null:String(htmlValue),
              url==null?null:String(url),js==null?null:String(js),String(sourceRegex),'',
              JSON.stringify(Object.assign({{}},source.getHeaderMap(true))));
          }},
          connect: function(value, header) {{
            if (arguments.length!==1 && arguments.length!==2)
              throw new TypeError("Can't find method JsExtensions.connect");
            if (value===null) throw __wrappedJavaException(
              new __NullPointerException('Parameter specified as non-null is null: urlStr'));
            let headers={{}};
            if (header != null && String(header)) {{
              try {{ headers=JSON.parse(String(header)); }} catch (_) {{ headers={{}}; }}
            }}
            const inherited=arguments.length===1
              ? Object.assign({{}},source.getHeaderMap(true)) : {{}};
            return __response(__connect(String(value)+','+JSON.stringify({{headers:headers}}),
              JSON.stringify(inherited)));
          }},
          // Android starts an Activity and the Java method returns Unit.
          startBrowser: (url, title) => {{__browser_start(String(url),String(title));return undefined;}},
          startBrowserAwait: (url, title) => __response(__browser(String(url || baseUrl), String(title || ''))),
          createSymmetricCrypto: (transformation, key, iv) => {{
            const generated=JSON.parse(__sym_generate(String(transformation),key == null,iv == null));
            const actualKey=key == null ? generated.key : key;
            const actualIv=iv == null ? generated.iv : iv;
            return {{
              decryptStr: value => __crypto('decryptStr', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)),
              encryptStr: value => __crypto('encryptStr', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)),
              decrypt: value => __byteArray(JSON.parse(__crypto('decryptBytes', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)))),
              encrypt: value => __byteArray(JSON.parse(__crypto('encryptBytes', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)))),
              encryptBase64: value => __crypto('encryptStr', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)),
              encryptHex: value => JSON.parse(__crypto('encryptBytes', String(transformation), JSON.stringify(actualKey), JSON.stringify(actualIv || ''), JSON.stringify(value)))
                .map(x=>(Number(x)&255).toString(16).padStart(2,'0')).join('')
            }};
          }},
          createAsymmetricCrypto: transformation => __asymmetric(String(transformation)),
          createSign: algorithm => __signer(String(algorithm))
        }};
        var __sourceRuleCache={{}};var __ruleRegexMode=false;
        if (__hostKind !== 'url') {{
          delete java.getStrResponse; delete java.getByteArray;
          delete java.getInputStream; delete java.getResponse;
          delete java.getHeaderMap; delete java.initUrl;
          delete java.getUserAgent; delete java.isPost;
          delete java.getGlideUrl;
        }} else {{
          delete java.getString; delete java.getElement;
          delete java.getStringList; delete java.getElements;
          delete java.setContent; delete java.setBaseUrl;
          delete java.setRedirectUrl;
          delete java.splitSourceRule;delete java.splitSourceRuleCacheString;
          delete java.reGetBook;delete java.refreshBook;delete java.refreshTocUrl;
        }}
        if (__hostKind === 'url') {{
          java.ruleUrl=baseUrl;java.redirectUrl=baseUrl;java.url=baseUrl;java.key=key;
          java.getUserAgent=function(){{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.getUserAgent");
            for(const key of Object.keys(java.headerMap))
              if(String(key).toLowerCase()==='user-agent')return String(java.headerMap[key]);
            return String(__staticSourceHeaders['User-Agent']||'');
          }};
          java.isPost=function(){{
            if(arguments.length!==0)throw new TypeError("Can't find method AnalyzeUrl.isPost");
            return __requestMethod==='POST';
          }};
        }} else {{
          delete java.ruleUrl;java.redirectUrl=null;delete java.url;java.baseUrl=baseUrl;
          delete java.key;delete java.headerMap;
        }}
        java.webViewUA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36';
        java.getWebViewUA = () => java.webViewUA;
        const __deviceId = {json.dumps(self.device_id)};
        const __deviceValue = () => __deviceId;
        __deviceValue.toString = () => __deviceId;
        __deviceValue.valueOf = () => __deviceId;
        __deviceValue.toJSON = () => __deviceId;
        __deviceValue[Symbol.toPrimitive] = () => __deviceId;
        java.androidId = java.deviceID = __deviceValue;
        java = new Proxy(java, {{
          get: (target, name) => Object.prototype.hasOwnProperty.call(target, name)
            ? target[name]
            : undefined
        }});
        // Rhino exposes every package at top level, so `java.util.Base64` and
        // `javax.crypto.Cipher` resolve without the Packages prefix.
        var javax = {{}}, android = {{}};
        java.lang = Packages.java.lang;
        java.util = Packages.java.util;
        java.io = Packages.java.io;
        java.net = Packages.java.net;
        java.security = Packages.java.security;
        javax.crypto = Packages.javax.crypto;
        javax.xml = Packages.javax.xml;
        android.util = Packages.android.util;
        function replace(pattern, replacement) {{
          result = String(result == null ? '' : result).replace(pattern, replacement);
          return result;
        }}
        function Reload(url) {{ return __ajax(String(url),JSON.stringify(
          Object.assign({{}},source.getHeaderMap(true)))); }}
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
            const raw = __memory_cache_get(String(key));
            return raw === '' ? null : __restore(JSON.parse(raw));
          }},
          putMemory: (key, value) => {{
            __memory_cache_put(String(key), JSON.stringify(value));
            return value;
          }},
          getFile: key => {{
            const raw = __file_cache_get(String(key));
            return raw === '' ? null : __restore(JSON.parse(raw));
          }},
          putFile: (key, value, ttl) => {{
            __file_cache_put(String(key), JSON.stringify(String(value)),Number(ttl || 0));
            return value;
          }},
          deleteMemory: key => __memory_cache_delete(String(key))
        }};
        var cookie = {{
          getCookie: function(url) {{
            if(arguments.length!==1)throw new TypeError("Can't find method CookieStore.getCookie");
            if(url===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: url'));
            return __cookie_get(String(url));
          }},
          getKey: function(url, name) {{
            if(arguments.length!==2)throw new TypeError("Can't find method CookieStore.getKey");
            if(url===null||name===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null'));
            const all = __cookie_get(String(url));
            const found = all.split(';').map(x => x.trim().split('=')).find(x => x[0] === String(name));
            return found ? found.slice(1).join('=') : '';
          }},
          cookieToMap: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method CookieStore.cookieToMap");
            if(value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: cookie'));
            const result={{}};
            String(value).split(/;\\s*/).forEach(pair=>{{const at=pair.indexOf('=');
              if(at<0)return;const key=pair.slice(0,at).trim(),item=pair.slice(at+1).trim();
              if(item||item==='null')result[key]=item;}});
            return __map(result);
          }},
          mapToCookie: function(value) {{
            if(arguments.length!==1)throw new TypeError("Can't find method CookieStore.mapToCookie");
            if(value===null)return null;
            let data = value;
            if (typeof data === 'string') {{
              try {{ data = JSON.parse(data); }} catch (_) {{ return data; }}
            }}
            if (!data || typeof data !== 'object') return null;
            const keys=Object.keys(data);return keys.length?
              keys.map(key => key + '=' + data[key]).join('; '):null;
          }},
          setCookie: function(url, value) {{
            if(arguments.length!==2)throw new TypeError("Can't find method CookieStore.setCookie");
            if(url===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: url'));
            __cookie_set(String(url),value==null?'':String(value));return undefined;
          }},
          replaceCookie: function(url, value) {{
            if(arguments.length!==2)throw new TypeError("Can't find method CookieStore.replaceCookie");
            if(url===null||value===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null'));
            __cookie_replace(String(url),String(value));return undefined;
          }},
          removeCookie: function(url) {{
            if(arguments.length!==1)throw new TypeError("Can't find method CookieStore.removeCookie");
            if(url===null)throw __wrappedJavaException(new __NullPointerException(
              'Parameter specified as non-null is null: url'));
            __cookie_remove(String(url));return undefined;
          }},
          clear: function() {{
            if(arguments.length!==0)throw new TypeError("Can't find method CookieStore.clear");
            __cookie_clear();return undefined;
          }}
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
        var infoMap = new Proxy(__infoStored, {{
          get: (target, name) => name === 'save' ? (() => {{
            __put('infoMap', JSON.stringify(target)); return true;
          }}) :
            (Object.prototype.hasOwnProperty.call(target, name) ? target[name] : __infoDefault(name)),
          set: (target, name, value) => {{ target[name] = value; return true; }}
        }});
        if (__hostKind === 'format') {{
          java=undefined;source=undefined;book=undefined;result=undefined;
          src=undefined;baseUrl=undefined;key=undefined;page=undefined;
          speakText=undefined;speakSpeed=undefined;nextChapterUrl=undefined;
          cookie=undefined;cache=undefined;infoMap=undefined;
        }}
        // AnalyzeUrl resolves the source header while it is constructed.  The
        // resolution is deferred to __initUrlHeaderMap() because a @js: header
        // may call any of the JsExtensions the context defines further down
        // (java.getWebViewUA, java.androidId, the Packages aliases, cookie...),
        // and it may also call jsLib functions, so the call site sits after the
        // library program rather than here.  Evaluating it in place would reach
        // java before it is complete and fail with "TypeError: not a function".
        function __initUrlHeaderMap() {{
          if (__hostKind !== 'url') return;
          java.headerMap=__map(Object.assign({{}},source.getHeaderMap(true),
            __requestHeaderData));
        }}
        """
        clean = self._clean_script(script)
        # Rhino book-source scripts frequently redeclare injected context
        # globals at top level (``let page = java.get('page')``). QuickJS
        # rejects a lexical declaration over our bootstrap ``var`` bindings.
        # A plain assignment preserves Rhino's effective value and scope.
        context_names = {
            "key", "page", "result", "src", "baseUrl", "title",
            "nextChapterUrl", "speakText", "speakSpeed", "book", "chapter",
            "source", "java", "cache", "infoMap", "Packages",
        }
        for context_name in context_names:
            clean = re.sub(
                rf"(?m)^(\s*)(?:let|const)\s+{re.escape(context_name)}\s*=",
                rf"\1{context_name} =", clean)
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
        # Rhino allows a later top-level ``var`` declaration to reuse a name
        # introduced by a library's top-level ``let``/``const``. QuickJS
        # rejects the combined program before execution. Treat the later var
        # as an assignment, matching Rhino's effective shared-scope behavior.
        lexical_names = set(re.findall(
            r"(?m)^\s*(?:let|const)\s+([A-Za-z_$][\w$]*)", js_lib))
        for lexical_name in lexical_names:
            clean = re.sub(
                rf"(?m)^(\s*)var\s+{re.escape(lexical_name)}\s*=",
                rf"\1{lexical_name} =", clean)
        # The bootstrap is evaluated repeatedly in a persistent QuickJS
        # global.  Only its top-level const bindings need to become var;
        # nested block-scoped declarations retain their JavaScript semantics.
        if js_lib:
            bootstrap = re.sub(r"(?m)^        const ", "        var ", bootstrap)
            bootstrap = bootstrap.replace(
                "        var __global = globalThis;",
                "        var __global = this; var globalThis = __global;")
        try:
            # Rhino returns undefined for a declaration-only rule.  Isolate
            # that narrow case so bootstrap statement completions cannot leak
            # into its result, while ordinary rules retain the shared top-level
            # lexical scope used by infoMap defaults and jsLib declarations.
            # BaseSource.evalJS creates a fresh runtime scope whose prototype
            # is SharedJsScope.  Direct eval inside an IIFE gives each rule the
            # same fresh lexical scope while its jsLib functions and closures
            # remain in the persistent global scope.
            if js_lib:
                rule_program = (";(function(source){return eval(" +
                                json.dumps(clean, ensure_ascii=False) + ")})(" +
                                ("null" if source_is_null else "source") + ")")
                library_program = js_lib if is_new_shared_context else ""
                value = ctx.eval(
                    bootstrap + "\n;delete __global.globalThis;\n" +
                    library_program + "\n;__initUrlHeaderMap();\n" +
                    rule_program)
            else:
                declaration_only = bool(re.fullmatch(
                    r"\s*(?:(?:var|let|const)\s+[^;]+;|"
                    r"(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\([\s\S]*|"
                    r"class\s+[A-Za-z_$][\w$]*[\s\S]*)\s*", clean))
                if source_is_null:
                    rule_program = (";(function(source){return eval(" +
                                    json.dumps(clean, ensure_ascii=False) + ")})(null)")
                else:
                    rule_program = (";eval(" + json.dumps(clean, ensure_ascii=False) + ")"
                                    if declaration_only else clean)
                value = ctx.eval(bootstrap + "\n;delete __global.globalThis;\n" +
                                 ";__initUrlHeaderMap();\n" + rule_program)
            self._sync_context_objects(ctx, context)
            self._sync_rule_variables(context)
            if hasattr(value, "json"):
                return json.loads(value.json())
            return value
        except Exception as exc:
            bridge_error = str(getattr(self._thread_state, "bridge_error", "") or "")
            detail = str(exc)
            location = re.search(r"<input>:(\d+)", detail)
            if location:
                rule_line = int(location.group(1)) - (
                    bootstrap.count("\n") + js_lib.count("\n") +
                    5)
                if rule_line > 0:
                    detail = f"{detail.rstrip()}\nRule line: {rule_line}"
            if bridge_error and bridge_error not in detail:
                detail = f"{detail.rstrip()}\nBridge: {bridge_error}"
            raise JsBridgeError(detail) from exc
        finally:
            # Do not let an exception traceback release the native Context on
            # the caller's thread; QuickJS requires destruction on its owner.
            if not js_lib:
                del ctx

    def _bridge_call(self, function, *args):
        """Preserve Python callback failures hidden by QuickJS as generic errors."""
        try:
            return function(*args)
        except Exception as exc:
            self._thread_state.bridge_error = f"{type(exc).__name__}: {exc}"
            raise

    @staticmethod
    def _external_rule_root(content):
        from app.services.legado_rule_parser import parse_json_smart, parse_markup
        if hasattr(content, "json"):
            content = content.json()
        if isinstance(content, (dict, list)):
            return content, "json"
        text = str(content if content is not None else "")
        if text.lstrip().startswith(("{", "[")):
            try:
                return parse_json_smart(text), "json"
            except json.JSONDecodeError:
                pass
        return parse_markup(text), "html"

    def _context_rule_root(self, context: JsContext, content=None):
        """Reuse AnalyzeByJSoup/JXDocument state for one RuleAnalyzer context."""
        if content is not None:
            return self._external_rule_root(content)
        marker = id(context.result) if not isinstance(context.result, str) else context.result
        if getattr(context, "_rule_root_marker", object()) != marker:
            context._rule_root_cache = self._external_rule_root(context.result)
            context._rule_root_marker = marker
        return context._rule_root_cache

    def _native_object_source_rule(self, context: JsContext, rule,
                                   native: dict) -> tuple[str, bool]:
        """Run SourceRule.makeUpRule enough to select the NativeObject branch.

        Returns the completed rule and whether getParamSize() is greater than
        one (in which case AnalyzeRule returns the completed rule itself).
        """
        from app.services.source_rule import compile_rule_program
        program = compile_rule_program(str(rule))
        if not program.stages:
            return "", False
        raw = program.stages[0].rule
        token = re.compile(r"@get:\{([^}]+?)\}|\{\{([\s\S]*?)\}\}", re.I)
        pieces, count, cursor = [], 0, 0
        for match in token.finditer(raw):
            literal = raw[cursor:match.start()]
            if literal:
                pieces.append(literal)
                count += max(1, len(re.findall(r"\$\d{1,2}",
                                               literal.split("##", 1)[0])))
            if match.group(1) is not None:
                name = match.group(1)
                if name == "bookName" and context.book is not None:
                    value = getattr(context.book, "name", "")
                elif name == "title" and context.chapter is not None:
                    value = getattr(context.chapter, "name", "") or getattr(
                        context.chapter, "title", "")
                else:
                    value = self._java_get(
                        context, name, bool(context.rule_data_present))
                pieces.append("" if value is None else str(value))
            else:
                expression = (match.group(2) or "").strip()
                if expression.startswith(("@", "$.", "$[", "//")):
                    pieces.append(self._context_get_string(context, expression))
                else:
                    # quickjs.Context is not re-entrant from a Python callback.
                    # Use another embedded runtime while preserving the same
                    # host objects and shared variable mapping.
                    nested = LegadoJsRuntime(self.timeout, state_dir=self.state_dir)
                    try:
                        value = nested.evaluate(
                            "@js:" + expression,
                            JsContext(
                                source=context.source, host_kind=context.host_kind,
                                key=context.key,
                                page=context.page, result=native,
                                base_url=context.base_url,
                                variables=context.variables,
                                rule_data_present=context.rule_data_present,
                                book=context.book,
                                chapter=context.chapter, title=context.title,
                                next_chapter_url=context.next_chapter_url,
                                src=context.src,
                            ),
                        )
                    finally:
                        nested.close()
                    pieces.append(
                        "" if value is None else
                        f"{value:.0f}" if isinstance(value, float) and
                        value.is_integer() else str(value))
            count += 1
            cursor = match.end()
        tail = raw[cursor:]
        if tail or not count:
            pieces.append(tail)
            count += max(1, len(re.findall(
                r"\$\d{1,2}", tail.split("##", 1)[0])))
        return "".join(pieces), count > 1

    def _context_get_string(self, context: JsContext, rule, content=None,
                            is_url=False):
        from app.services.rule_engine import (ReaderEngine, _apply_regex_suffix,
                                               extract_value)
        if isinstance(content, bool):
            is_url, content = content, None
        if content is None:
            if context.get_string:
                return context.get_string(str(rule))
        native = content
        if hasattr(native, "json"):
            try:
                native = json.loads(native.json())
            except (TypeError, ValueError, json.JSONDecodeError):
                native = None
        if isinstance(native, dict):
            completed, direct = self._native_object_source_rule(
                context, rule, native)
            key, marker, suffix = completed.partition("##")
            value = key.strip() if direct else native.get(key.strip())
            rendered = ("" if value is None else
                        ReaderEngine._rhino_result_string(value))
            if marker:
                rendered = _apply_regex_suffix(rendered, marker + suffix)
            return rendered
        root, kind = self._context_rule_root(context, content)
        return extract_value(root, str(rule), kind,
                             context.base_url if is_url else "")

    def _context_get_list(self, context: JsContext, rule, content=None,
                          is_url=False):
        from app.services.rule_engine import (ReaderEngine, _apply_regex_suffix,
                                               extract_values)
        if isinstance(content, bool):
            is_url, content = content, None
        if content is None:
            if context.get_string_list:
                return context.get_string_list(str(rule))
            if context.get_string:
                return [context.get_string(str(rule))]
        native = content
        if hasattr(native, "json"):
            try:
                native = json.loads(native.json())
            except (TypeError, ValueError, json.JSONDecodeError):
                native = None
        if isinstance(native, dict):
            completed, direct = self._native_object_source_rule(
                context, rule, native)
            key, marker, suffix = completed.partition("##")
            value = key.strip() if direct else native.get(key.strip())
            if value is None:
                return []
            rendered = ReaderEngine._rhino_result_string(value)
            if marker:
                rendered = _apply_regex_suffix(rendered, marker + suffix)
            return rendered.split("\n")
        root, kind = self._context_rule_root(context, content)
        return extract_values(root, str(rule), kind,
                              context.base_url if is_url else "")

    def _context_get_elements(self, context: JsContext, rule, content=None):
        import xml.etree.ElementTree as ET
        from app.services.legado_rule_parser import JavaDateValue, evaluate
        from app.services.rule_engine import _legado_value_string
        if content is None:
            if context.get_elements:
                return context.get_elements(str(rule))
            if context.get_string_list:
                return context.get_string_list(str(rule))
        root, kind = self._context_rule_root(context, content)
        values = evaluate(root, str(rule), kind, list_context=True)
        is_xpath = str(rule).lstrip().lower().startswith("@xpath:") or str(
            rule).lstrip().startswith(("/", "("))
        if not is_xpath:
            from bs4 import Tag
            return [({"__domHandle": self._dom_register(item)}
                     if isinstance(item, Tag) else _legado_value_string(item))
                    for item in values]
        result = []
        for item in values:
            if isinstance(item, ET.Element):
                result.append({"__jxType": "element", "value":
                               ET.tostring(item, encoding="unicode")})
            elif isinstance(item, bool):
                result.append({"__jxType": "boolean", "value": item})
            elif isinstance(item, (int, float)):
                result.append({"__jxType": "number", "value": item})
            elif isinstance(item, JavaDateValue):
                result.append({"__jxType": "date", "value":
                               int(item.value.timestamp() * 1000)})
            else:
                result.append({"__jxType": "string", "value":
                               _legado_value_string(item)})
        return result

    @staticmethod
    def _sync_context_objects(ctx, context: JsContext):
        """Copy direct JS mutations back to Python book/chapter/source objects."""
        try:
            context.g_int = int(ctx.eval(
                "typeof gInt === 'number' && isFinite(gInt) ? gInt : 0"))
        except Exception:
            pass
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
            # A remote script may shadow one of Rhino's global context names
            # (for example ``var source = url``).  Legado simply discards that
            # temporary global when the evaluation ends; it never attempts to
            # copy string/array properties back into the Kotlin model object.
            if not isinstance(values, dict):
                continue
            if name == "book":
                for legado_name, python_name in {
                    "bookUrl": "url", "tocUrl": "toc_url",
                    "coverUrl": "cover_url", "wordCount": "word_count",
                    "lastChapterTitle": "last_chapter", "originName": "origin_name",
                    "latestChapterTitle": "latest_chapter_title",
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
                    "startFragmentId": "start_fragment_id",
                    "endFragmentId": "end_fragment_id",
                    "isVip": "is_vip", "isPay": "is_pay",
                }.items():
                    if legado_name in values:
                        values[python_name] = values[legado_name]
            for key, value in values.items():
                # RuleDataInterface.variableMap is updated through get/put;
                # the serialized entity field is not a mutable JS property.
                if key in {"variables", "variableMap", "variable"}:
                    continue
                if isinstance(target, dict):
                    if key in target:
                        target[key] = value
                elif hasattr(target, key):
                    try:
                        setattr(target, key, value)
                    except (AttributeError, TypeError):
                        pass

    @classmethod
    def _sync_rule_variables(cls, context: JsContext) -> None:
        for prefix, target in (("book:", context.book),
                               ("chapter:", context.chapter)):
            variables = cls._object_variables(target)
            if variables is None:
                continue
            for name, value in list(context.variables.items()):
                if not str(name).startswith(prefix):
                    continue
                key = str(name)[len(prefix):]
                if value is None:
                    variables.pop(key, None)
                else:
                    variables[key] = str(value)

    @staticmethod
    @lru_cache(maxsize=2048)
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
        # Rhino permits a function parameter to be redeclared with let/const;
        # QuickJS treats that as an early syntax error. Assignment preserves
        # Rhino's effective value while allowing the library to compile.
        def normalize_parameters(match):
            header, params, body = match.group(1), match.group(2), match.group(3)
            for parameter in re.findall(r"[A-Za-z_$][\w$]*", params):
                body = re.sub(
                    rf"\b(?:let|const)\s+{re.escape(parameter)}\s*=",
                    f"{parameter} =", body)
            return header + body

        value = re.sub(
            r"(function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)\s*\{)"
            r"([\s\S]*?)(?=\r?\nfunction\s+[A-Za-z_$]|\Z)",
            normalize_parameters, value)
        value = value.replace("\u200b", "").replace("\ufeff", "")
        # Normalize a common community-source typo only where the character is
        # being used as a JavaScript statement terminator.
        value = re.sub(r"(?<=[\]\)])；", ";", value)
        return value

    @staticmethod
    def _object_variables(value) -> dict | None:
        if value is None:
            return None
        if isinstance(value, dict):
            variables = value.get("variables")
            return variables if isinstance(variables, dict) else None
        variables = getattr(value, "variables", None)
        return variables if isinstance(variables, dict) else None

    def _java_get(self, context: JsContext, name, rule_data_present: bool) -> str:
        """Mirror the distinct AnalyzeRule/AnalyzeUrl variable fallback chains."""
        key = str(name)
        if key == "bookName" and context.book is not None:
            return str(getattr(context.book, "name", "") or "")
        if key == "title" and context.chapter is not None:
            return str(getattr(context.chapter, "title", None) or
                       getattr(context.chapter, "name", "") or "")
        targets = [self._object_variables(context.chapter)]
        targets.append(self._object_variables(context.book))
        if rule_data_present:
            targets.append(context.variables)
        for target in targets:
            if isinstance(target, dict):
                value = target.get(key)
                if value is not None and str(value) != "":
                    return str(value)
        if context.host_kind != "url":
            value = context.variables.get("source:data:" + key)
            if value is not None and str(value) != "":
                return str(value)
        return ""

    def _java_put(self, context: JsContext, name, value,
                  rule_data_present: bool) -> str:
        key, text = str(name), str(value)
        chapter_variables = self._object_variables(context.chapter)
        if chapter_variables is not None:
            chapter_variables[key] = text
            context.variables["chapter:" + key] = text
            return text
        book_variables = self._object_variables(context.book)
        if book_variables is not None:
            book_variables[key] = text
            context.variables["book:" + key] = text
            return text
        if rule_data_present:
            context.variables[key] = text
            return text
        if context.host_kind != "url":
            self._put(context, "source:data:" + key, text)
        return text

    def _put(self, context: JsContext, name, value):
        name = str(name)
        persistent = (name in {"sourceVariable", "source:loginHeader", "source:loginInfo", "infoMap"}
                      or name.startswith("source:data:"))
        if persistent and value is None:
            context.variables.pop(name, None)
        else:
            context.variables[name] = value
        if persistent:
            if hasattr(context.source, "to_dict"):
                source_data = context.source.to_dict()
            elif isinstance(context.source, dict):
                source_data = dict(context.source)
            else:
                source_data = {str(key): item for key, item in
                               vars(context.source).items() if not callable(item)}
            source_key = str(source_data.get("bookSourceUrl", ""))
            if source_key:
                with self._PERSIST_LOCK:
                    disk_state = self._load_persistent_state()
                    if self.device_id:
                        disk_state.setdefault("deviceId", self.device_id)
                    sources = disk_state.setdefault("sources", {})
                    source_state = sources.setdefault(source_key, {})
                    if value is None:
                        source_state.pop(name, None)
                    else:
                        source_state[name] = (self._encrypt_login_info(str(value))
                                              if name == "source:loginInfo" else value)
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
            self.persistent_state.setdefault("cache", {}).pop(str(key), None)
            self._persist_cache_state()
            return ""
        return value

    def _cache_put(self, key, value, ttl=0):
        seconds = max(0.0, float(ttl or 0))
        expires = time.time() + seconds if seconds else 0
        self.cache_store[str(key)] = (str(value), expires)
        try:
            decoded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            decoded = str(value)
        if decoded is None:
            disk_value = "null"
        elif isinstance(decoded, bool):
            disk_value = "true" if decoded else "false"
        elif isinstance(decoded, dict):
            disk_value = "[object Object]"
        elif isinstance(decoded, list):
            disk_value = ",".join(str(item) for item in decoded)
        else:
            disk_value = str(decoded)
        self.persistent_state.setdefault("cache", {})[str(key)] = {
            "value": disk_value, "expires": expires}
        self._persist_cache_state()
        return value

    def _cache_delete(self, key):
        self.cache_store.pop(str(key), None)
        self._memory_cache_delete(key)
        self.persistent_state.setdefault("cache", {}).pop(str(key), None)
        self._file_cache_delete(key)
        self._persist_cache_state()
        return None

    def _memory_cache_get(self, key):
        name = str(key)
        value = self.memory_cache_store.pop(name, "")
        if value != "":
            self.memory_cache_store[name] = value
        return value

    def _memory_cache_put(self, key, value):
        name, rendered = str(key), str(value)
        previous = self.memory_cache_store.pop(name, None)
        if previous is not None:
            self._memory_cache_size -= len(previous.encode("utf-8"))
        self.memory_cache_store[name] = rendered
        self._memory_cache_size += len(rendered.encode("utf-8"))
        while (self._memory_cache_size > self._MEMORY_CACHE_LIMIT and
               self.memory_cache_store):
            oldest = next(iter(self.memory_cache_store))
            removed = self.memory_cache_store.pop(oldest)
            self._memory_cache_size -= len(removed.encode("utf-8"))
        return value

    def _memory_cache_delete(self, key):
        removed = self.memory_cache_store.pop(str(key), None)
        if removed is not None:
            self._memory_cache_size -= len(removed.encode("utf-8"))

    @staticmethod
    def _time_format(value, pattern="yyyy-MM-dd HH:mm:ss", utc=False) -> str:
        number = float(value)
        numeric_offset = (not isinstance(utc, bool) and
                          isinstance(utc, (int, float)))
        if numeric_offset:
            number += float(utc)
        moment = datetime.fromtimestamp(
            number / 1000, timezone.utc if utc or numeric_offset else None)
        source_pattern = str(pattern or "yyyy-MM-dd HH:mm:ss")
        fmt = source_pattern
        for java, python in (("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"),
                             ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
                             ("SSS", "%f")):
            fmt = fmt.replace(java, python)
        rendered = moment.strftime(fmt)
        return rendered[:-3] if "SSS" in source_pattern else rendered

    @staticmethod
    def _timezone_offset_minutes(value) -> int:
        moment = datetime.fromtimestamp(float(value) / 1000, timezone.utc).astimezone()
        offset = moment.utcoffset()
        return int(offset.total_seconds() // 60) if offset is not None else 0

    @staticmethod
    def _date_to_string(value) -> str:
        moment = datetime.fromtimestamp(float(value) / 1000, timezone.utc).astimezone()
        weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        offset = moment.strftime("%z") or "+0000"
        zone = str(moment.tzname() or "GMT")
        if not zone.isascii() and offset == "+0800":
            zone = "CST"
        return (f"{weekdays[moment.weekday()]} {months[moment.month - 1]} "
                f"{moment.day:02d} {moment.year:04d} {moment:%H:%M:%S} "
                f"GMT{offset} ({zone})")

    @staticmethod
    def _to_num_chapter(value) -> str:
        text = str(value)
        match = re.search(r"(第)(.+?)(章)", text)
        if not match:
            return text
        inner = match.group(2).translate(str.maketrans(
            "０１２３４５６７８９", "0123456789"))
        inner = re.sub(r"\s+", "", inner)
        try:
            number = int(inner)
        except ValueError:
            digits = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2,
                      "两": 2, "三": 3, "叁": 3, "四": 4, "肆": 4,
                      "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7,
                      "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
            units = {"十": 10, "拾": 10, "百": 100, "佰": 100,
                     "千": 1000, "仟": 1000, "万": 10000, "亿": 100000000}
            if not inner or any(char not in digits and char not in units for char in inner):
                number = -1
            else:
                total = section = current = 0
                for char in inner:
                    if char in digits:
                        current = digits[char]
                    else:
                        unit = units[char]
                        if unit >= 10000:
                            section = (section + current) * unit
                            total += section
                            section = current = 0
                        else:
                            section += (current or 1) * unit
                            current = 0
                number = total + section + current
        replacement = f"{match.group(1)}{number}{match.group(3)}"
        return text[:match.start()] + replacement + text[match.end():]

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
    def _base64_decode_flags(value, flags=0, as_bytes=False) -> str:
        text = re.sub(r"\s+", "", str(value))
        text += "=" * ((4 - len(text) % 4) % 4)
        try:
            raw = (base64.urlsafe_b64decode(text) if int(flags or 0) & 8
                   else base64.b64decode(text))
        except (ValueError, binascii.Error) as exc:
            raise JsBridgeError(f"base64 decode failed: {exc}") from None
        return json.dumps(list(raw)) if as_bytes else raw.decode("utf-8", errors="replace")

    @staticmethod
    def _base64_encode_text(value, flags=2) -> str:
        raw = str(value).encode()
        numeric_flags = int(flags or 0)
        encoded = (base64.urlsafe_b64encode(raw) if numeric_flags & 8
                   else base64.b64encode(raw)).decode("ascii")
        if numeric_flags & 1:
            encoded = encoded.rstrip("=")
        if not numeric_flags & 2:
            separator = "\r\n" if numeric_flags & 4 else "\n"
            encoded = separator.join(encoded[index:index + 76]
                                     for index in range(0, len(encoded), 76)) + separator
        return encoded

    @staticmethod
    def _encode_uri(value: str, charset="utf-8") -> str:
        try:
            return quote_plus(str(value), safe="", encoding=str(charset), errors="strict")
        except (LookupError, UnicodeError):
            return ""

    @staticmethod
    def _utf8_to_gbk(value: str) -> str:
        # JsExtensions encodes the Unicode string as GBK and constructs the
        # final Java String with Android's UTF-8 default charset.
        return str(value).encode("gbk", errors="replace").decode(
            "utf-8", errors="replace")

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
            encoding = str(charset).replace("ISO-8859-1", "latin-1")
            return raw.decode(encoding, errors="replace")
        return json.dumps(list(raw))

    @classmethod
    def _inflate(cls, value, nowrap=False, limit=0) -> str:
        window = -zlib.MAX_WBITS if nowrap else zlib.MAX_WBITS
        try:
            raw = zlib.decompressobj(window).decompress(
                cls._byte_values(value), int(limit or 0))
        except zlib.error as exc:
            raise JsBridgeError(f"java.util.zip.DataFormatException: {exc}") from exc
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
    def _digest_bytes(cls, algorithm, value) -> str:
        name = cls._hash_name(str(algorithm))
        # java.security.MessageDigest calls "SHA" what hashlib names sha1.
        if name in ("sha", ""):
            name = "sha1"
        try:
            digest = hashlib.new(name, cls._byte_values(value)).digest()
        except ValueError as exc:
            raise JsBridgeError(
                f"java.security.NoSuchAlgorithmException: {algorithm}") from exc
        return json.dumps(list(digest))

    @classmethod
    def _digest_base64(cls, value, algorithm="MD5") -> str:
        name = cls._hash_name(str(algorithm))
        return base64.b64encode(hashlib.new(name, str(value).encode()).digest()).decode()

    @classmethod
    def _hmac(cls, value, algorithm, secret, as_base64=False) -> str:
        name = cls._hash_name(re.sub(
            r"^hmac", "", str(algorithm), flags=re.I)) or "sha1"
        result = hmac.new(str(secret).encode(), str(value).encode(), name).digest()
        return base64.b64encode(result).decode() if as_base64 else result.hex()

    def _resolve_js_lib(self, value) -> str:
        """Load only JSON-object URL mappings; evaluate every other value whole."""
        if not value:
            return ""
        data = value
        if isinstance(value, str):
            text = value.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return text
            if not isinstance(data, dict):
                return text
        if isinstance(data, dict):
            entries = data.values()
        else:
            return str(data)
        scripts = []
        for entry in entries:
            text = str(entry or "").strip()
            if text.startswith(("http://", "https://")):
                scripts.append(self._ajax(text))
        return "\n".join(scripts)

    @staticmethod
    def _to_url(value, parent=""):
        absolute = urljoin(str(parent or ""), str(value or ""))
        parsed = urlsplit(absolute)
        if not parsed.scheme or not parsed.hostname:
            raise JsBridgeError(f"invalid URL: {absolute}")
        origin = (f"{parsed.scheme}://{parsed.hostname}:{absolute}:{parsed.port}"
                  if parsed.port else f"{parsed.scheme}://{parsed.hostname}:{absolute}")
        params = None
        if parsed.query:
            params = {}
            for item in parsed.query.split("&"):
                pieces = item.split("=")
                if len(pieces) < 2:
                    raise JsBridgeError(f"invalid query parameter: {item}")
                params[pieces[0]] = unquote_plus(pieces[1], encoding="utf-8")
        return json.dumps({
            "host": parsed.hostname,
            "origin": origin,
            "pathname": parsed.path,
            "searchParams": params,
        }, ensure_ascii=False)

    @staticmethod
    def _unsupported(name):
        raise JsBridgeError(f"暂不支持 {name}")

    def _dom_register(self, node) -> str:
        reverse = getattr(self._thread_state, "dom_reverse", None)
        if reverse is None:
            reverse = {}
            self._thread_state.dom_reverse = reverse
        identity = id(node)
        existing = reverse.get(identity)
        if existing is not None and self._current_dom_store().get(existing) is node:
            return existing
        handle = uuid.uuid4().hex
        self._current_dom_store()[handle] = node
        reverse[identity] = handle
        return handle

    def _current_dom_store(self) -> dict:
        store = getattr(self._thread_state, "dom_store", None)
        if store is None:
            store = self.dom_store
        return store

    def _dom_parse(self, markup) -> str:
        from app.services.legado_rule_parser import parse_markup
        return self._dom_register(parse_markup(str(markup or "")))

    def _dom_query(self, handle: str, selector: str) -> str:
        from app.services.legado_rule_parser import select_jsoup
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return "[]"
        nodes = select_jsoup(node, str(selector or "*"))
        return json.dumps([self._dom_register(item) for item in nodes])

    def _dom_xpath(self, handle: str, xpath: str) -> str:
        import elementpath
        from bs4 import BeautifulSoup, Tag
        from app.services.legado_rule_parser import _legado_xpath_parser, _to_element_tree
        node = self._current_dom_store().get(str(handle))
        if not isinstance(node, Tag):
            return "[]"
        tree = _to_element_tree(node)
        expression = str(xpath or "")
        if expression.startswith("//"):
            expression = "." + expression
        selected = elementpath.select(tree, expression, parser=_legado_xpath_parser())
        selected = list(selected) if isinstance(selected, (list, tuple)) else [selected]
        et_nodes = list(tree.getroot().iter())
        source_root = (node.find("html") if isinstance(node, BeautifulSoup) else node)
        source_nodes = ([source_root] + list(source_root.find_all(True))
                        if isinstance(source_root, Tag) else [])
        nodes = []
        for item in selected:
            if item in et_nodes:
                index = et_nodes.index(item)
                if index < len(source_nodes):
                    nodes.append(source_nodes[index])
        return json.dumps([self._dom_register(item) for item in nodes])

    def _dom_operation(self, handle: str, operation="text", argument=""):
        from bs4 import BeautifulSoup, Comment, NavigableString, Tag
        from app.services.legado_rule_parser import (_jsoup_html_markup, _jsoup_serialize,
                                                     _jsoup_own_text, _jsoup_text)
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return ""
        if operation == "text":
            return _jsoup_text(node) if isinstance(node, Tag) else str(node)
        if operation == "ownText":
            if not isinstance(node, Tag):
                return node.get_text(" ", strip=True)
            return _jsoup_own_text(node)
        if operation == "wholeText" and isinstance(node, Tag):
            return "".join(str(item) for item in node.find_all(string=True))
        if operation == "wholeOwnText" and isinstance(node, Tag):
            return "".join(str(item) for item in node.children
                           if isinstance(item, str))
        if operation == "html":
            return (_jsoup_serialize(node, inner=True)
                    if hasattr(node, "decode_contents") else str(node))
        if operation == "outerHtml":
            return _jsoup_html_markup(node)
        if operation == "data" and isinstance(node, Tag):
            return "".join(str(text) for text in node.find_all(string=True)
                           if getattr(text.parent, "name", "") in {"script", "style"})
        if operation == "attr" and isinstance(node, Tag):
            value = node.get(str(argument), "")
            return " ".join(value) if isinstance(value, list) else str(value)
        if operation == "hasAttr":
            return isinstance(node, Tag) and node.has_attr(str(argument))
        if operation == "tagName":
            if isinstance(node, BeautifulSoup):
                return "#root"
            return node.name if isinstance(node, Tag) else ""
        if operation == "nodeName":
            if isinstance(node, BeautifulSoup):
                return "#document"
            if isinstance(node, Comment):
                return "#comment"
            if isinstance(node, NavigableString):
                return "#text"
            return str(node.name) if isinstance(node, Tag) else ""
        if operation == "normalName":
            return str(node.name).lower() if isinstance(node, Tag) else ""
        if operation == "cssSelector" and isinstance(node, Tag):
            import soupsieve
            from app.services.legado_rule_parser import select_jsoup
            def component(item: Tag) -> str:
                tag_name = soupsieve.escape(str(item.name)).replace(r"\:", "|")
                classes = item.get("class", [])
                if isinstance(classes, str):
                    classes = classes.split()
                result = tag_name + "".join("." + soupsieve.escape(str(name))
                                             for name in classes)
                parent = item.parent
                if not isinstance(parent, Tag) or isinstance(parent, BeautifulSoup):
                    return result
                siblings = [child for child in parent.children
                            if isinstance(child, Tag) and
                            child.name == item.name and
                            set(child.get("class", []) if isinstance(child.get("class", []), list)
                                else str(child.get("class", "")).split()) == set(classes)]
                if len(siblings) > 1:
                    elements = [child for child in parent.children if isinstance(child, Tag)]
                    result += f":nth-child({elements.index(item) + 1})"
                return " > " + result

            identifier = str(node.get("id", ""))
            if identifier:
                candidate = "#" + soupsieve.escape(identifier)
                owner = node
                while isinstance(owner.parent, Tag):
                    owner = owner.parent
                if len(select_jsoup(owner, candidate)) == 1:
                    return candidate
            parts, current = [], node
            while isinstance(current, Tag) and not isinstance(current, BeautifulSoup):
                parts.insert(0, component(current))
                current = current.parent
            return "".join(parts).lstrip(" >")
        if operation == "id" and isinstance(node, Tag):
            return str(node.get("id", ""))
        if operation == "className" and isinstance(node, Tag):
            value = node.get("class", "")
            return " ".join(value) if isinstance(value, list) else str(value)
        if operation == "val" and isinstance(node, Tag):
            if str(node.name).lower() == "textarea":
                return node.get_text()
            return str(node.get("value", ""))
        if operation == "childrenSize" and isinstance(node, Tag):
            return len(node.find_all(True, recursive=False))
        if operation == "childNodeSize" and isinstance(node, Tag):
            return len(node.contents)
        if operation == "hasText" and isinstance(node, Tag):
            return bool(_jsoup_text(node))
        if operation == "isBlock" and isinstance(node, Tag):
            from app.services.legado_rule_parser import _jsoup_is_block
            return _jsoup_is_block(node)
        if operation == "elementSiblingIndex" and isinstance(node, Tag):
            siblings = ([item for item in node.parent.children if isinstance(item, Tag)]
                        if isinstance(node.parent, Tag) else [node])
            return siblings.index(node)
        if operation == "attributes" and isinstance(node, Tag):
            return json.dumps({
                str(key): (" ".join(str(item) for item in item_value)
                           if isinstance(item_value, list) else str(item_value))
                for key, item_value in node.attrs.items()
            }, ensure_ascii=False)
        if operation == "dataset" and isinstance(node, Tag):
            return json.dumps({
                str(key)[5:]: (" ".join(str(item) for item in item_value)
                               if isinstance(item_value, list) else str(item_value))
                for key, item_value in node.attrs.items()
                if str(key).lower().startswith("data-")
            }, ensure_ascii=False)
        if operation == "classNames" and isinstance(node, Tag):
            value = node.get("class", [])
            return json.dumps(value if isinstance(value, list) else str(value).split(),
                              ensure_ascii=False)
        if operation == "textNodes" and isinstance(node, Tag):
            return json.dumps([str(item) for item in node.children
                               if isinstance(item, NavigableString) and
                               not isinstance(item, Comment)], ensure_ascii=False)
        if operation == "dataNodes" and isinstance(node, Tag):
            return json.dumps([str(item) for item in node.children
                               if isinstance(item, NavigableString) and
                               str(node.name).lower() in {"script", "style"}],
                              ensure_ascii=False)
        if operation == "comments" and isinstance(node, Tag):
            return json.dumps([str(item) for item in node.descendants
                               if isinstance(item, Comment)], ensure_ascii=False)
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

    def _dom_related(self, handle: str, relation: str) -> str:
        from bs4 import Tag
        node = self._current_dom_store().get(str(handle))
        if not isinstance(node, Tag):
            return "[]"
        relation = str(relation)
        if relation == "children":
            nodes = [item for item in node.children if isinstance(item, Tag)]
        elif relation == "siblings":
            nodes = [item for item in node.parent.children if isinstance(item, Tag)] \
                if isinstance(node.parent, Tag) else []
            nodes = [item for item in nodes if item is not node]
        elif relation == "parent":
            nodes = [node.parent] if isinstance(node.parent, Tag) else []
        elif relation == "next":
            item = node.find_next_sibling(True)
            nodes = [item] if item is not None else []
        elif relation == "nextAll":
            nodes = list(node.find_next_siblings(True))
        elif relation == "previous":
            item = node.find_previous_sibling(True)
            nodes = [item] if item is not None else []
        elif relation == "previousAll":
            nodes = list(node.find_previous_siblings(True))
        elif relation == "root":
            current = node
            while isinstance(current.parent, Tag):
                current = current.parent
            nodes = [current]
        else:
            nodes = []
        return json.dumps([self._dom_register(item) for item in nodes])

    def _dom_mutate(self, handle: str, operation: str, argument="", value=""):
        from bs4 import NavigableString, Tag
        from app.services.legado_rule_parser import parse_markup
        node = self._current_dom_store().get(str(handle))
        if not isinstance(node, Tag):
            return False
        operation, argument = str(operation), str(argument)

        def fragment_children(context: Tag | None, markup) -> list:
            name = str(getattr(context, "name", "body") or "body").lower()
            marker = "__legado_fragment_context__"
            if name in {"tbody", "thead", "tfoot"}:
                source = f'<table><{name} id="{marker}">{markup}</{name}></table>'
            elif name == "tr":
                source = f'<table><tbody><tr id="{marker}">{markup}</tr></tbody></table>'
            elif name in {"td", "th"}:
                source = (f'<table><tbody><tr><{name} id="{marker}">{markup}'
                          f'</{name}></tr></tbody></table>')
            elif name == "colgroup":
                source = f'<table><colgroup id="{marker}">{markup}</colgroup></table>'
            elif name in {"option", "optgroup"}:
                source = f'<select><{name} id="{marker}">{markup}</{name}></select>'
            else:
                source = f'<{name} id="{marker}">{markup}</{name}>'
            fragment = parse_markup(source)
            holder = fragment.find(id=marker)
            return list(holder.contents) if holder is not None else []

        if operation == "attr":
            node.attrs[argument] = str(value)
        elif operation == "removeAttr":
            node.attrs.pop(argument, None)
        elif operation == "clearAttributes":
            node.attrs.clear()
        elif operation in {"addClass", "removeClass", "toggleClass"}:
            classes = list(node.get("class", []))
            if isinstance(node.get("class"), str):
                classes = str(node.get("class")).split()
            present = argument in classes
            if operation == "addClass" and not present:
                classes.append(argument)
            elif operation == "removeClass" and present:
                classes = [item for item in classes if item != argument]
            elif operation == "toggleClass":
                classes = ([item for item in classes if item != argument]
                           if present else classes + [argument])
            if classes:
                node.attrs["class"] = classes
            else:
                node.attrs.pop("class", None)
        elif operation == "text":
            node.clear()
            node.append(NavigableString(str(value)))
        elif operation == "val":
            if str(node.name).lower() == "textarea":
                node.clear()
                node.append(NavigableString(str(value)))
            else:
                node.attrs["value"] = str(value)
        elif operation == "html":
            children = fragment_children(node, value)
            node.clear()
            for child in children:
                node.append(child.extract())
        elif operation in {"append", "prepend", "before", "after"}:
            context = node if operation in {"append", "prepend"} else (
                node.parent if isinstance(node.parent, Tag) else None)
            children = fragment_children(context, value)
            if operation in {"prepend", "after"}:
                children.reverse()
            for child in children:
                child = child.extract()
                if operation == "append":
                    node.append(child)
                elif operation == "prepend":
                    node.insert(0, child)
                elif operation == "before":
                    node.insert_before(child)
                else:
                    node.insert_after(child)
        elif operation in {"appendText", "prependText"}:
            child = NavigableString(str(value))
            node.append(child) if operation == "appendText" else node.insert(0, child)
        elif operation == "wrap":
            wrappers = fragment_children(
                node.parent if isinstance(node.parent, Tag) else None, value)
            wrapper = next((item for item in wrappers if isinstance(item, Tag)), None)
            if wrapper is None or node.parent is None:
                return False
            wrapper = wrapper.extract()
            node.insert_before(wrapper)
            deepest = wrapper
            while True:
                child = next((item for item in deepest.children
                              if isinstance(item, Tag)), None)
                if child is None:
                    break
                deepest = child
            deepest.append(node.extract())
        elif operation == "unwrap":
            if node.parent is None:
                return False
            for child in list(node.contents):
                node.insert_before(child.extract())
            node.extract()
        elif operation == "empty":
            node.clear()
        elif operation == "tagName":
            node.name = str(value)
        else:
            return False
        return True

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

    def _dom_matches(self, handle: str, selector: str) -> bool:
        from bs4 import Tag
        from app.services.legado_rule_parser import select_jsoup
        node = self._current_dom_store().get(str(handle))
        if not isinstance(node, Tag):
            return False
        root = node
        while isinstance(root.parent, Tag):
            root = root.parent
        return any(item is node for item in select_jsoup(root, str(selector)))

    def _dom_collect(self, handle: str, kind: str, first="", second="") -> str:
        from bs4 import Tag
        from app.services.java_regex import search as java_search
        from app.services.legado_rule_parser import _jsoup_own_text, _jsoup_text
        node = self._current_dom_store().get(str(handle))
        if not isinstance(node, Tag):
            return "[]"
        nodes = [node, *node.find_all(True)]
        kind, first, second = str(kind), str(first), str(second)

        def attribute(item: Tag, key: str):
            actual = next((name for name in item.attrs
                           if str(name).casefold() == key.casefold()), None)
            if actual is None:
                return None
            value = item.attrs[actual]
            return (" ".join(str(part) for part in value)
                    if isinstance(value, list) else str(value))

        if kind == "tag":
            nodes = [item for item in nodes
                     if str(item.name).casefold() == first.casefold()]
        elif kind == "id":
            nodes = [item for item in nodes if str(item.get("id", "")) == first]
        elif kind == "class":
            nodes = [item for item in nodes if any(
                str(value).casefold() == first.casefold()
                for value in (item.get("class", []) if isinstance(item.get("class", []), list)
                              else str(item.get("class", "")).split()))]
        elif kind == "attr":
            nodes = [item for item in nodes if attribute(item, first) is not None]
        elif kind == "attrStarting":
            nodes = [item for item in nodes if any(
                str(key).casefold().startswith(first.casefold()) for key in item.attrs)]
        elif kind.startswith("attrValue"):
            def value_match(item):
                value = attribute(item, first)
                folded = "" if value is None else value.casefold()
                expected = second.casefold()
                return {
                    "attrValue": value is not None and folded == expected,
                    "attrValueNot": value is None or folded != expected,
                    "attrValueStarting": value is not None and folded.startswith(expected),
                    "attrValueEnding": value is not None and folded.endswith(expected),
                    "attrValueContaining": value is not None and expected in folded,
                    "attrValueMatching": value is not None and
                        java_search(second, value) is not None,
                }[kind]
            nodes = [item for item in nodes if value_match(item)]
        elif kind.startswith("index"):
            index = int(first)
            positions = []
            for item in nodes:
                siblings = ([child for child in item.parent.children if isinstance(child, Tag)]
                            if isinstance(item.parent, Tag) else [item])
                positions.append(siblings.index(item))
            compare = {
                "indexEquals": lambda value: value == index,
                "indexLess": lambda value: value < index,
                "indexGreater": lambda value: value > index,
            }[kind]
            nodes = [item for item, position in zip(nodes, positions) if compare(position)]
        elif kind == "containsText":
            nodes = [item for item in nodes
                     if first.casefold() in _jsoup_text(item).casefold()]
        elif kind == "containsOwnText":
            nodes = [item for item in nodes
                     if first.casefold() in _jsoup_own_text(item).casefold()]
        elif kind == "matchesText":
            nodes = [item for item in nodes if java_search(first, _jsoup_text(item))]
        elif kind == "matchesOwnText":
            nodes = [item for item in nodes if java_search(first, _jsoup_own_text(item))]
        return json.dumps([self._dom_register(item) for item in nodes])

    def _dom_setting(self, handle: str, name: str, value=None):
        from bs4 import BeautifulSoup, Tag
        node = self._current_dom_store().get(str(handle))
        document = node
        while isinstance(document, Tag) and not isinstance(document, BeautifulSoup):
            document = document.parent
        if not isinstance(document, BeautifulSoup):
            return None
        key = f"_legado_output_{str(name)}"
        defaults = {
            "prettyPrint": not bool(getattr(document, "_legado_xml", False)),
            "outline": False, "indentAmount": 1, "maxPaddingWidth": 30,
            "syntax": "xml" if getattr(document, "_legado_xml", False) else "html",
            "escapeMode": "xhtml" if getattr(document, "_legado_xml", False) else "base",
            "charset": "UTF-8",
        }
        if value is not None:
            if name in {"prettyPrint", "outline"}:
                value = bool(value)
            elif name in {"indentAmount", "maxPaddingWidth"}:
                value = int(value)
            else:
                value = str(value)
            setattr(document, key, value)
        return document.__dict__.get(key, defaults.get(str(name)))

    def _dom_move(self, handle: str, operation: str, child_handle: str,
                  index=0) -> bool:
        from bs4 import Tag
        store = self._current_dom_store()
        node, child = store.get(str(handle)), store.get(str(child_handle))
        if not isinstance(node, Tag) or child is None or child is node:
            return False
        child.extract()
        operation = str(operation)
        if operation == "append":
            node.append(child)
        elif operation == "prepend":
            node.insert(0, child)
        elif operation == "before" and node.parent is not None:
            node.insert_before(child)
        elif operation == "after" and node.parent is not None:
            node.insert_after(child)
        elif operation == "insert":
            position = max(0, min(int(index), len(node.contents)))
            node.insert(position, child)
        else:
            return False
        return True

    def _dom_traverse(self, handle: str, descendants=True) -> str:
        """Return Jsoup NodeTraversor order with stable shared-node handles."""
        from bs4 import Tag
        node = self._current_dom_store().get(str(handle))
        if node is None:
            return "[]"
        records = []

        def visit(item, depth):
            records.append({"handle": self._dom_register(item), "depth": depth})
            if isinstance(item, Tag):
                for child in item.contents:
                    visit(child, depth + 1)

        if descendants:
            visit(node, 0)
        elif isinstance(node, Tag):
            for child in node.contents:
                records.append({"handle": self._dom_register(child), "depth": 0})
        return json.dumps(records)

    def _dom_form_data(self, handle: str, elements_only=False) -> str:
        """Port FormElement.formData's successful-control collection."""
        from bs4 import Tag
        form = self._current_dom_store().get(str(handle))
        if not isinstance(form, Tag) or str(form.name).lower() != "form":
            return "[]"
        # HtmlTreeBuilder 1.16.2 associates controls encountered while this form
        # is on the stack; it does not attach later controls via form="id".
        controls = list(form.find_all(["input", "select", "textarea", "button", "keygen"]))
        if elements_only:
            return json.dumps([self._dom_register(item) for item in controls])
        values = []
        for element in controls:
            name = str(element.get("name") or "")
            if not name or element.has_attr("disabled"):
                continue
            tag = str(element.name).lower()
            kind = str(element.get("type") or "").lower()
            # button is form-listable but not form-submittable in Tag metadata.
            if tag == "button" or kind in {"button", "image"}:
                continue
            if kind in {"checkbox", "radio"} and not element.has_attr("checked"):
                continue
            if tag == "select":
                options = element.find_all("option")
                selected = [item for item in options if item.has_attr("selected")]
                if not selected and options:
                    selected = options[:1]
                for option in selected:
                    values.append([name, str(option.get("value")
                                             if option.has_attr("value")
                                             else option.get_text())])
                continue
            value = (element.get_text() if tag == "textarea" else
                     str(element.get("value") or ""))
            if kind in {"checkbox", "radio"} and not value:
                value = "on"
            values.append([name, value])
        return json.dumps(values, ensure_ascii=False)

    @staticmethod
    def _css_query(html, selector, operation="text", attribute="") -> str:
        from app.services.legado_rule_parser import (_jsoup_html_markup, _jsoup_serialize,
                                                     _jsoup_text,
                                                     parse_markup, select_jsoup)

        soup = parse_markup(str(html))
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
            values = [_jsoup_serialize(node, inner=True) for node in nodes]
        elif operation == "outerHtml":
            values = [_jsoup_html_markup(node) for node in nodes]
        else:
            values = [_jsoup_text(node) for node in nodes]
        return json.dumps(values, ensure_ascii=False)

    @staticmethod
    def _element_operation(markup, operation="text", argument=""):
        from bs4 import Tag
        from app.services.legado_rule_parser import (_jsoup_html_markup, _jsoup_serialize,
                                                     _jsoup_own_text, _jsoup_text,
                                                     parse_markup)

        soup = parse_markup(str(markup))
        node = next((item for item in soup.find_all(True)
                     if str(item.name).lower() not in {"html", "head", "body"}), None)
        if node is None:
            return str(markup) if operation in {"text", "ownText", "html", "outerHtml"} else ""
        if operation == "text":
            return _jsoup_text(node)
        if operation == "ownText":
            return _jsoup_own_text(node)
        if operation == "html":
            return _jsoup_serialize(node, inner=True)
        if operation == "outerHtml":
            return _jsoup_html_markup(node)
        if operation == "data":
            return "".join(str(text) for text in node.find_all(string=True)
                           if getattr(text.parent, "name", "") in {"script", "style"})
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

    @staticmethod
    def _html_format(value) -> str:
        """Port HtmlFormatter.formatKeepImg without a redirect URL."""
        text = str(value or "")
        text = re.sub(r"(?:&nbsp;)+", " ", text)
        text = re.sub(r"&ensp;|&emsp;", " ", text)
        text = re.sub(r"&thinsp;|&zwnj;|&zwj;", "", text)
        text = re.sub(r"</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>", "\n", text)
        text = re.sub(r"<!--[^>]*-->", "", text)
        text = re.sub(r"</?(?!img)[a-zA-Z]+(?=[ >])[^<>]*>", "", text)
        text = re.sub(r"\s*\n+\s*", "\n　　", text)
        text = re.sub(r"^[\n\s]+", "　　", text)
        text = re.sub(r"[\n\s]+$", "", text)

        def normalize_image(match):
            markup = match.group(0)
            src_match = re.search(r"\ssrc\s*=\s*\"([^\"]*)\"", markup, re.I)
            src = src_match.group(1) if src_match else ""
            if "{" in src and re.search(r"\s*,\s*(?=\{)", src):
                src = re.sub(r"\s*,\s*(?=\{)", ",", src, count=1)
            else:
                data_match = re.search(r"\sdata-[^=>]*=\s*\"([^\"]*)\"", markup, re.I)
                if data_match:
                    src = data_match.group(1)
            return f'<img src="{src}">'

        return re.sub(r"<img[^>]*>", normalize_image, text, flags=re.I)

    @staticmethod
    def _simplified_to_traditional(value) -> str:
        try:
            from opencc import OpenCC
            return OpenCC("s2t").convert(str(value))
        except ImportError as exc:
            raise JsBridgeError("missing simplified/traditional conversion dependency") from exc

    def _zip_content(self, value: str, member: str, as_bytes=False, charset="",
                     source_headers=None, base_url=None, cookie_enabled=True,
                     cookie_tag=None) -> str:
        """Mirror JsExtensions.getZip*Content for a URL or hexadecimal archive."""
        try:
            if re.match(r"^https?://", value, re.I):
                response = json.loads(self._request_json(
                    value, source_headers, base_url, cookie_enabled, cookie_tag,
                    False))
                raw = bytes(response.get("bodyBytes") or [])
            else:
                raw = bytes.fromhex(value)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                # The fixed Kotlin loop calls nextEntry once in the while
                # condition and once again at the end of the body, so only
                # entries 1, 3, 5... are observable.
                visible = {info.filename: info for info in archive.infolist()[::2]}
                info = visible.get(member)
                if info is None:
                    return ""
                content = archive.read(info)
            if as_bytes:
                return json.dumps(list(content))
            if charset:
                return content.decode(charset)
            return self._decode_detected_text(content)
        except (OSError, ValueError, zipfile.BadZipFile, LookupError) as exc:
            raise JsBridgeError(f"zip content read failed: {exc}") from None

    def _archive_input(self, value: str, source_headers=None, base_url=None,
                       cookie_enabled=True, cookie_tag=None) -> bytes:
        if re.match(r"^https?://", value, re.I):
            response = json.loads(self._request_json(
                value, source_headers, base_url, cookie_enabled, cookie_tag,
                False))
            return bytes(response.get("bodyBytes") or [])
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise JsBridgeError(f"archive hex decode failed: {exc}") from None

    @classmethod
    def _decode_archive_text(cls, content: bytes, charset="") -> str:
        if charset:
            try:
                return content.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise JsBridgeError(f"archive text decode failed: {exc}") from None
        return cls._decode_detected_text(content)

    @staticmethod
    def _decode_detected_text(content: bytes, file_sample=False) -> str:
        """Port EncodingDetect.getEncode for byte arrays and File samples."""
        from app.services.icu_charset import detect_charset
        sample = content
        if file_sample:
            sample = content[:8000] + b"\0" * max(0, 8000 - len(content[:8000]))
        encoding = detect_charset(sample) or "UTF-8"
        try:
            return content.decode(encoding, errors="replace")
        except LookupError as exc:
            raise JsBridgeError(f"unsupported detected charset: {encoding}") from exc

    def _archive_content(self, archive_type: str, value: str, member: str,
                         as_bytes=False, charset="", source_headers=None,
                         base_url=None, cookie_enabled=True, cookie_tag=None) -> str:
        raw = self._archive_input(value, source_headers, base_url,
                                  cookie_enabled, cookie_tag)
        try:
            if archive_type == "7z":
                import py7zr
                with tempfile.TemporaryDirectory(prefix="legado-7z-") as directory:
                    root = Path(directory).resolve()
                    with py7zr.SevenZipFile(io.BytesIO(raw), mode="r") as archive:
                        if member not in archive.getnames():
                            return ""
                        target = (root / member.replace("\\", "/")).resolve()
                        try:
                            target.relative_to(root)
                        except ValueError:
                            raise JsBridgeError("archive member escapes extraction root") from None
                        archive.extract(path=root, targets=[member])
                    content = target.read_bytes()
            elif archive_type == "rar":
                import rarfile
                self._configure_rarfile(rarfile)
                with rarfile.RarFile(io.BytesIO(raw)) as archive:
                    try:
                        content = archive.read(member)
                    except KeyError:
                        return ""
            else:
                raise JsBridgeError(f"unsupported archive type: {archive_type}")
        except JsBridgeError:
            raise
        except Exception as exc:
            raise JsBridgeError(f"{archive_type} content read failed: {exc}") from None
        return (json.dumps(list(content)) if as_bytes else
                self._decode_archive_text(content, charset))

    def _file_path(self, value: str) -> Path:
        root = self.file_root.resolve()
        relative = str(value or "").replace("\\", "/").lstrip("/")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise JsBridgeError("file path escapes the external cache root") from None
        return path

    def _file_read(self, value: str, existence=False, charset="") -> str:
        path = self._file_path(value)
        if existence:
            return "1" if path.exists() else ""
        if not path.is_file():
            return ""
        content = path.read_bytes()
        if charset == "__bytes__":
            return json.dumps(list(content))
        if charset:
            try:
                return content.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise JsBridgeError(f"file decode failed: {exc}") from None
        return self._decode_detected_text(content, file_sample=True)

    def _file_delete(self, value: str) -> bool:
        path = self._file_path(value)
        if not path.exists():
            return False
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
        else:
            path.unlink()
        return True

    def _download_file(self, value: str, url="", source_headers=None,
                       base_url=None, cookie_enabled=True) -> str:
        target_url = str(url or value)
        if url:
            file_type = self._download_config_type(target_url)
            if not file_type:
                return ""
            try:
                content = bytes.fromhex(value)
            except ValueError as exc:
                raise JsBridgeError(f"download hex decode failed: {exc}") from None
            name = hashlib.md5(target_url.encode()).hexdigest()[8:24] + "." + file_type
        else:
            args = (target_url, source_headers, base_url)
            response = (self._request_json(*args) if cookie_enabled
                        else self._request_json(*args, False))
            content = bytes(json.loads(response).get("bodyBytes") or [])
            name = self._download_name(target_url)
        path = self._file_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return "/" + name

    @staticmethod
    def _download_config_type(target_url: str) -> str:
        from app.services.custom_url import CustomUrl
        value = CustomUrl(target_url).get_attr().get("type")
        return str(value or "")

    @staticmethod
    def _download_name(target_url: str) -> str:
        from app.services.custom_url import CustomUrl
        custom_url = CustomUrl(target_url)
        request_url = custom_url.get_url()
        default = custom_url.get_attr().get("type")
        suffix = request_url.rsplit(".", 1)[1] if "." in request_url else ""
        suffix = suffix.split("?", 1)[0]
        if len(suffix) > 5 or not re.fullmatch(r"[A-Za-z0-9]+", suffix or ""):
            suffix = str(default or "ext")
        return hashlib.md5(target_url.encode()).hexdigest()[8:24] + "." + suffix

    def _cache_file(self, url: str, save_time=0, source_headers=None,
                    base_url=None, cookie_enabled=True) -> str:
        key = hashlib.md5(str(url).encode()).hexdigest()[8:24]
        cached = self._cache_get(key)
        try:
            relative = json.loads(cached) if cached else ""
        except json.JSONDecodeError:
            relative = cached
        path = self._file_path(relative) if relative else None
        if path is None or not path.exists():
            relative = self._download_file(
                url, "", source_headers, base_url, cookie_enabled)
            self._cache_put(key, json.dumps(relative), save_time)
        return self._file_read(relative, False, "")

    def _import_script(self, path_value: str, source_headers=None,
                       base_url=None, cookie_enabled=True) -> str:
        path_value = str(path_value)
        if path_value.startswith("http"):
            result = self._cache_file(
                path_value, 0, source_headers, base_url, cookie_enabled)
        elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path_value):
            if self.uri_reader is not None:
                result = str(self.uri_reader(path_value) or "")
            elif path_value.lower().startswith("file:"):
                from urllib.parse import unquote, urlsplit
                uri_path = unquote(urlsplit(path_value).path)
                if re.match(r"^/[A-Za-z]:/", uri_path):
                    uri_path = uri_path[1:]
                result = self._read_absolute_text(Path(uri_path))
            else:
                raise JsBridgeError(f"URI reader is not configured for {path_value}")
        elif path_value.startswith("/storage"):
            result = self._read_absolute_text(Path(path_value))
        else:
            result = self._file_read(path_value, False, "")
        if not str(result).strip():
            raise JsBridgeError(f"{path_value} content fetch failed or is blank")
        return str(result)

    @staticmethod
    def _read_absolute_text(path: Path) -> str:
        raw = path.read_bytes()
        return raw.decode("utf-8", errors="replace")

    def _unarchive_file(self, value: str) -> str:
        archive_path = self._file_path(value)
        if not value or not archive_path.is_file():
            return ""
        archive_root = self._file_path("ArchiveTemp")
        if not self._archive_temp_initialized:
            if archive_root.exists():
                self._file_delete("ArchiveTemp")
            archive_root.mkdir(parents=True, exist_ok=True)
            self._archive_temp_initialized = True
        destination_relative = ("ArchiveTemp/" + hashlib.md5(
            archive_path.name.encode()).hexdigest()[8:24])
        destination = self._file_path(destination_relative)
        destination.mkdir(parents=True, exist_ok=True)
        try:
            root = destination.resolve()
            suffix = archive_path.suffix.lower()
            if suffix == ".zip":
                with zipfile.ZipFile(archive_path) as archive:
                    for info in archive.infolist():
                        target = (root / info.filename.replace("\\", "/")).resolve()
                        try:
                            target.relative_to(root)
                        except ValueError:
                            raise JsBridgeError("archive member escapes extraction root") from None
                    archive.extractall(root)
            elif suffix == ".7z":
                import py7zr
                with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                    for name in archive.getnames():
                        target = (root / name.replace("\\", "/")).resolve()
                        try:
                            target.relative_to(root)
                        except ValueError:
                            raise JsBridgeError("archive member escapes extraction root") from None
                    archive.extractall(root)
            elif suffix == ".rar":
                import rarfile
                self._configure_rarfile(rarfile)
                with rarfile.RarFile(archive_path) as archive:
                    for info in archive.infolist():
                        target = (root / info.filename.replace("\\", "/")).resolve()
                        try:
                            target.relative_to(root)
                        except ValueError:
                            raise JsBridgeError("archive member escapes extraction root") from None
                    archive.extractall(root)
            else:
                raise JsBridgeError("Unexpected file suffix: Only 7z rar zip Accepted")
        except JsBridgeError:
            self._file_delete(destination_relative)
            raise
        except Exception as exc:
            self._file_delete(destination_relative)
            raise JsBridgeError(f"archive extract failed: {exc}") from None
        return destination_relative.replace("/", "\\")

    @staticmethod
    def _configure_rarfile(rarfile_module) -> None:
        """Point rarfile at conda's bundled libarchive tool when it is off PATH."""
        candidate = Path(sys.prefix) / "Library" / "bin" / "bsdtar.exe"
        if candidate.is_file():
            rarfile_module.BSDTAR_TOOL = str(candidate)
            rarfile_module.tool_setup(force=True)

    def _text_in_folder(self, value: str) -> str:
        folder = self._file_path(value)
        if not folder.exists():
            return ""
        if not folder.is_dir():
            self._file_delete(value)
            return ""
        values = []
        for path in folder.iterdir():
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise JsBridgeError(f"folder entry read failed: {exc}") from None
            values.append(self._decode_detected_text(raw, file_sample=True))
        if not values:
            raise JsBridgeError("StringBuilder deleteCharAt(-1)")
        self._file_delete(value)
        return "\n".join(values)

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
        result = self._browser_result({"url": str(url), "title": str(title),
                                       "mode": "await"})
        return json.dumps(result, ensure_ascii=False)

    def _browser_result(self, options: dict) -> dict:
        if self.browser_fetcher is None:
            raise JsBridgeError("interactive browser is not configured")
        url = str(options.get("url") or "")
        title = str(options.get("title") or "")
        try:
            value = self.browser_fetcher(url, title, dict(options))
        except TypeError as first_error:
            try:
                value = self.browser_fetcher(url, title)
            except TypeError:
                raise first_error
        if isinstance(value, dict):
            result = dict(value)
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
        cookie_tag = str(options.get("cookieTag") or "")
        # BackstageWebView reads Android CookieManager for the finished page,
        # then stores that string under source.getKey(), not the redirected URL.
        if cookie_tag and "cookie" in result:
            cookie_text = str(result.get("cookie") or "")
            self.store_set_cookie(cookie_tag, cookie_text)
        return result

    @staticmethod
    def _bridge_response_payload(result: dict, response_kind="str") -> dict:
        """Normalize browser/HTTP results without collapsing repeated headers."""
        raw_headers = result.get("headers") or {}
        headers: dict[str, str] = {}
        multi: dict[str, list[str]] = {}
        for name, value in raw_headers.items():
            key = str(name).lower()
            values = ([str(item) for item in value] if isinstance(value, (list, tuple))
                      else [str(value)])
            multi[key] = values
            if values:
                headers[key] = values[-1]
        body = result.get("body")
        if isinstance(body, (bytes, bytearray)):
            body_bytes = bytes(body)
            body_text = body_bytes.decode("utf-8", errors="replace")
        else:
            body_text = str(body or "")
            body_bytes = body_text.encode("utf-8")
        return {
            "body": body_text, "bodyBytes": list(body_bytes),
            "headers": headers, "headersMulti": multi,
            "status": int(result.get("status") or 200),
            "message": str(result.get("message") or
                           ("OK" if str(response_kind) == "str" else "")),
            "url": str(result.get("url") or ""),
            "responseKind": str(response_kind),
            "protocol": str(result.get("protocol") or "http/1.1"),
        }

    def _webview(self, html_value: str, url: str, js: str,
                 source_regex: str, override_regex: str,
                 cookie_tag: str | None = None,
                 header_map: dict | None = None) -> str:
        options = {
            "mode": "webView",
            "html": None if html_value is None else str(html_value),
            "url": None if url is None else str(url),
            "javaScript": None if js is None else str(js),
            "sourceRegex": str(source_regex or ""),
            "overrideUrlRegex": str(override_regex or ""),
            "cookieTag": str(cookie_tag or ""),
            "headerMap": {str(k): str(v) for k, v in (header_map or {}).items()},
        }
        result = self._browser_result(options)
        if source_regex:
            if result.get("source") is not None:
                return str(result["source"])
            # BackstageWebView applies Regex.matches to resource request URLs;
            # it never searches the final DOM or returns a capture group.
            return ""
        if override_regex:
            if result.get("overrideUrl") is not None:
                return str(result["overrideUrl"])
            from app.services.java_regex import compile_pattern
            candidate = str(result.get("url") or "")
            if compile_pattern(str(override_regex)).fullmatch(candidate):
                return candidate
            return ""
        return str(result.get("body") or "")

    def _evaluate_webjs(self, script: str, context: JsContext) -> str:
        """BackstageWebView(isRule=true) equivalent for the @webjs: stage.

        The browser exposes the previous stage value as the JSON string
        ``window.result``; the rule script's completion value becomes the new
        rule result.
        """
        if self.browser_fetcher is None:
            raise JsBridgeError("@webjs: 规则需要浏览器后端，当前平台未配置")
        result = context.result
        result_json = result if isinstance(result, str) else json.dumps(
            "" if result is None else result, ensure_ascii=False)
        wrapped = (
            "window.result = " + json.dumps(result_json, ensure_ascii=False) +
            ";\nwindow.__legadoJsResult = (function(){try{return eval(" +
            json.dumps(str(script or ""), ensure_ascii=False) +
            ")}catch(error){return '__LEGADO_JS_ERROR__:' + error}})();")
        html = context.src if isinstance(context.src, str) and context.src else (
            context.result if isinstance(context.result, str)
            and context.result.lstrip().startswith("<") else "")
        fetched = self._browser_result({
            "mode": "webJs",
            "url": str(context.base_url or ""),
            "html": html or None,
            "javaScript": wrapped,
            "sourceRegex": "",
            "overrideUrlRegex": "",
            "headerMap": {},
        })
        value = fetched.get("jsResult")
        if isinstance(value, str) and value.startswith("__LEGADO_JS_ERROR__:"):
            raise JsBridgeError(value[len("__LEGADO_JS_ERROR__:"):])
        if value is None:
            raise JsBridgeError("webjs 脚本未返回结果")
        return value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False)

    def _start_browser(self, url: str, title: str) -> None:
        if self.browser_starter is not None:
            self.browser_starter(str(url), str(title))

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

    def _bitmap_pixel(self, handle: str, x, y) -> int:
        """Bitmap.getPixel: ARGB_8888 packed as a signed Java int."""
        image = self.bitmap_store.get(str(handle))
        if image is None:
            raise JsBridgeError("bitmap handle is no longer available")
        red, green, blue, alpha = image.convert("RGBA").getpixel((int(x), int(y)))
        packed = ((alpha & 255) << 24) | ((red & 255) << 16) | (
            (green & 255) << 8) | (blue & 255)
        return packed - 0x100000000 if packed >= 0x80000000 else packed

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
            from app.services.query_ttf import HistoricalQueryTTF

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
            font = HistoricalQueryTTF(raw)
            self.font_store[key] = font
            return key
        except Exception as exc:
            raise JsBridgeError(f"font parse failed: {exc}") from None

    def _replace_font(self, text: str, source_key: str, target_key: str) -> str:
        source = self.font_store.get(source_key)
        target = self.font_store.get(target_key)
        if not source or not target:
            return text
        # Retain the small internal tuple fixture used by older callers; JS can
        # only supply QueryTTF handles and therefore cannot observe this branch.
        if isinstance(source, tuple) and isinstance(target, tuple):
            source_forward, target_reverse = source[0], target[1]
            return "".join(chr(target_reverse.get(source_forward.get(ord(char)), ord(char)))
                           if ord(char) in source_forward else char for char in text)
        output = []
        for char in text:
            code = ord(char)
            if source.limit_min <= code <= source.limit_max:
                replacement = target.glyph_to_code.get(source.code_to_glyph.get(code, ""), 0)
                if replacement:
                    char = chr(replacement & 0xFFFF)
            output.append(char)
        return "".join(output)

    def _font_operation(self, handle: str, operation: str, argument="") -> str:
        font = self.font_store.get(str(handle))
        if not font:
            raise JsBridgeError("font handle is no longer available")
        if operation == "name":
            return font.get_name(int(argument))
        if operation == "limit":
            return str(font.limit_min <= int(argument) <= font.limit_max).lower()
        if operation == "glyph":
            return font.code_to_glyph.get(int(argument), "")
        if operation == "code":
            return str(font.glyph_to_code.get(str(argument), 0))
        if operation == "codeMap":
            return json.dumps(font.code_to_glyph, ensure_ascii=False)
        if operation == "glyphMap":
            return json.dumps(font.glyph_to_code, ensure_ascii=False)
        raise JsBridgeError(f"unsupported QueryTTF operation: {operation}")

    def _cookie_string(self, url: str) -> str:
        url = self._cookie_url(url)
        request = Request(url)
        self.cookies.add_cookie_header(request)
        value = request.get_header("Cookie", "")
        pairs = self._cookie_store_pairs(value)
        changed = False
        while len("; ".join(f"{k}={v}" for k, v in pairs.items())) > 4096 and pairs:
            key = random.choice(list(pairs))
            self._remove_cookie_key(url, key, persist=False)
            pairs.pop(key, None)
            changed = True
        if changed:
            self._persist_cookie_state()
        return "; ".join(f"{key}={item}" for key, item in pairs.items())

    def _remove_cookie_key(self, url: str, name: str, persist: bool = True) -> bool:
        from app.services.public_suffix import effective_tld_plus_one
        host = urlsplit(self._cookie_url(url)).hostname or ""
        domain = effective_tld_plus_one(host)
        removed = False
        for item in list(self.cookies):
            if (item.name == str(name) and effective_tld_plus_one(
                    str(item.domain).lstrip(".")) == domain):
                self._discard_cookie(item.domain, item.path, item.name)
                removed = True
        if self._session_persistent_shadow.pop((domain, str(name)), None) is not None:
            removed = True
        if removed and persist:
            self._persist_cookie_state()
        return removed

    def set_cookie(self, url: str, cookie_text: str, session: bool = False,
                   persist: bool = True) -> bool:
        """Import a Cookie/Set-Cookie string into the JS HTTP cookie jar."""
        from app.services.public_suffix import effective_tld_plus_one
        target = urlsplit(self._cookie_url(url))
        domain = target.hostname or ""
        if not domain:
            return False
        parsed = SimpleCookie()
        parsed.load(str(cookie_text or ""))
        for name, morsel in parsed.items():
            cookie_domain = effective_tld_plus_one(
                (morsel["domain"] or domain).lstrip("."))
            try:
                ipaddress.ip_address(cookie_domain)
                domain_cookie = False
            except ValueError:
                domain_cookie = "." in cookie_domain
            stored_domain = ("." + cookie_domain if domain_cookie else cookie_domain)
            path = "/"
            secure = False
            matching = [existing for existing in list(self.cookies)
                        if (existing.name == name and effective_tld_plus_one(
                            str(existing.domain).lstrip(".")) == cookie_domain)]
            session_existing = next((item for item in matching if bool(
                (item._rest or {}).get("LegadoSession"))), None)
            for existing in matching:
                is_session = bool((existing._rest or {}).get("LegadoSession"))
                if session:
                    if not is_session:
                        self._session_persistent_shadow[(cookie_domain, name)] = existing
                    self._discard_cookie(existing.domain, existing.path, name)
                elif not is_session:
                    self._discard_cookie(existing.domain, existing.path, name)
            rest = ({"HttpOnly": morsel["httponly"]}
                    if morsel["httponly"] else {})
            if session:
                rest["LegadoSession"] = True
            cookie_item = Cookie(
                version=0, name=name, value=morsel.value, port=None,
                port_specified=False, domain=stored_domain,
                domain_specified=domain_cookie, domain_initial_dot=domain_cookie,
                path=path, path_specified=False, secure=secure,
                expires=None, discard=True, comment=None, comment_url=None,
                rest=rest,
                rfc2109=False,
            )
            if not session and session_existing is not None:
                self._session_persistent_shadow[(cookie_domain, name)] = cookie_item
            else:
                if not session:
                    self._session_persistent_shadow.pop((cookie_domain, name), None)
                self.cookies.set_cookie(cookie_item)
        changed = bool(parsed)
        if changed and persist:
            self._persist_cookie_state()
        return changed

    def save_response_cookies(self, url: str, headers) -> None:
        """Port CookieManager.saveCookiesFromHeaders persistent/session split."""
        values = []
        if headers is not None:
            if hasattr(headers, "get_all"):
                values = list(headers.get_all("Set-Cookie") or [])
            elif hasattr(headers, "getall"):
                values = list(headers.getall("Set-Cookie", []) or [])
            elif isinstance(headers, dict):
                value = headers.get("Set-Cookie") or headers.get("set-cookie")
                values = value if isinstance(value, list) else ([value] if value else [])
        changed = False
        for value in values:
            parsed = SimpleCookie()
            parsed.load(str(value or ""))
            for name, morsel in parsed.items():
                cookie = f"{name}={morsel.value}"
                session = not bool(morsel["expires"] or morsel["max-age"])
                changed = self.set_cookie(
                    url, cookie, session=session, persist=False) or changed
        if changed:
            self._persist_cookie_state()

    @staticmethod
    def _cookie_store_pairs(cookie_text: str) -> dict[str, str]:
        result = {}
        for pair in re.split(r";\s*", str(cookie_text or "")):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key, value = key.strip(), value.strip()
            if value or value == "null":
                result[key] = value
        return result

    def _persistent_cookie_pairs(self, url: str) -> dict[str, str]:
        from app.services.public_suffix import effective_tld_plus_one
        host = urlsplit(self._cookie_url(url)).hostname or ""
        domain = effective_tld_plus_one(host)
        result = {}
        for item in self.cookies:
            if (effective_tld_plus_one(str(item.domain).lstrip(".")) == domain and
                    not bool((item._rest or {}).get("LegadoSession"))):
                result[item.name] = item.value
        for (item_domain, name), item in self._session_persistent_shadow.items():
            if item_domain == domain:
                result[name] = item.value
        return result

    def store_set_cookie(self, url: str, cookie_text: str | None) -> bool:
        """CookieStore.setCookie replaces only the persistent domain string."""
        from app.services.public_suffix import effective_tld_plus_one
        target = self._cookie_url(url)
        host = urlsplit(target).hostname or ""
        if not host:
            return False
        domain = effective_tld_plus_one(host)
        for item in list(self.cookies):
            if (effective_tld_plus_one(str(item.domain).lstrip(".")) == domain and
                    not bool((item._rest or {}).get("LegadoSession"))):
                self._discard_cookie(item.domain, item.path, item.name)
        for key in [key for key in self._session_persistent_shadow if key[0] == domain]:
            self._session_persistent_shadow.pop(key, None)
        for name, value in self._cookie_store_pairs(str(cookie_text or "")).items():
            try:
                ipaddress.ip_address(domain)
                domain_cookie = False
            except ValueError:
                domain_cookie = "." in domain
            stored_domain = "." + domain if domain_cookie else domain
            cookie_item = Cookie(
                version=0, name=name, value=value, port=None,
                port_specified=False, domain=stored_domain,
                domain_specified=domain_cookie, domain_initial_dot=domain_cookie,
                path="/", path_specified=False, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False)
            session_item = next((item for item in self.cookies
                                 if item.name == name and bool(
                                     (item._rest or {}).get("LegadoSession")) and
                                 effective_tld_plus_one(str(item.domain).lstrip(".")) == domain),
                                None)
            if session_item is not None:
                self._session_persistent_shadow[(domain, name)] = cookie_item
            else:
                self.cookies.set_cookie(cookie_item)
        self._persist_cookie_state()
        return True

    def store_replace_cookie(self, url: str, cookie_text: str) -> bool:
        """CookieStore.replaceCookie merges new persistent keys over old keys."""
        if not str(url or "") or not str(cookie_text or ""):
            return False
        values = self._persistent_cookie_pairs(url)
        values.update(self._cookie_store_pairs(cookie_text))
        merged = "; ".join(f"{name}={value}" for name, value in values.items())
        return self.store_set_cookie(url, merged)

    def store_clear_cookies(self) -> None:
        """CookieStore.clear deletes the persistent OkHttp table, not sessions."""
        for item in list(self.cookies):
            if not bool((item._rest or {}).get("LegadoSession")):
                self._discard_cookie(item.domain, item.path, item.name)
        self._session_persistent_shadow.clear()
        self._persist_cookie_state()

    def remove_cookie(self, url: str) -> bool:
        """Remove cookies visible to *url*, matching Legado's CookieStore API."""
        host = urlsplit(self._cookie_url(url)).hostname or ""
        visible = {(item.domain, item.path, item.name) for item in self.cookies
                   if self._domain_matches(host, item.domain)}
        for domain, path, name in visible:
            self._discard_cookie(domain, path, name)
        shadow = [key for key in self._session_persistent_shadow
                  if self._domain_matches(host, key[0])]
        for key in shadow:
            self._session_persistent_shadow.pop(key, None)
        if visible or shadow:
            self._persist_cookie_state()
        return bool(visible or shadow)

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
                value: str, tag_bits=128) -> str:
        """Legado-compatible JCA symmetric cipher string/byte operations."""
        try:
            from Crypto.Cipher import AES, ARC2, ARC4, Blowfish, DES, DES3
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
            key_bytes, iv_bytes = decode_argument(key), decode_argument(iv)
            if "GCM" in spec or algorithm.startswith("CHACHA20"):
                # AEAD transformations carry their tag appended to the ciphertext.
                return cls._aead(operation, algorithm, key_bytes, iv_bytes, value,
                                 tag_bits)
            cipher_type = AES if algorithm == "AES" else (DES3 if algorithm in {
                "DESEDE", "TRIPLEDES", "3DES"} else DES if algorithm == "DES" else
                Blowfish if algorithm == "BLOWFISH" else ARC2 if algorithm in {
                    "RC2", "ARC2"} else ARC4 if algorithm in {"RC4", "ARC4", "ARCFOUR"}
                else None)
            if cipher_type is None:
                raise ValueError(f"unsupported transformation: {transformation}")
            block_size = getattr(cipher_type, "block_size", 1)
            if cipher_type is DES3:
                if len(key_bytes) not in {16, 24}:
                    raise ValueError("DESede key must contain 16 or 24 bytes")
                key_bytes = DES3.adjust_key_parity(key_bytes)
            elif cipher_type is AES:
                if len(key_bytes) not in {16, 24, 32}:
                    raise ValueError("AES key must contain 16, 24 or 32 bytes")
            elif cipher_type is DES and len(key_bytes) != 8:
                raise ValueError("DES key must contain 8 bytes")
            if cipher_type is ARC4:
                cipher = cipher_type.new(key_bytes)
            elif "/CBC" in spec:
                actual_iv = iv_bytes if iv_bytes else bytes(block_size)
                if len(actual_iv) != block_size:
                    raise ValueError(f"IV must contain {block_size} bytes")
                cipher = cipher_type.new(key_bytes, cipher_type.MODE_CBC, iv=actual_iv)
            else:
                cipher = cipher_type.new(key_bytes, cipher_type.MODE_ECB)
            decoded_value = json.loads(value)
            decrypting = operation.startswith("decrypt")
            if decrypting:
                if isinstance(decoded_value, list):
                    raw = cls._byte_values(decoded_value)
                else:
                    encoded = str(decoded_value).strip()
                    raw = (bytes.fromhex(encoded) if re.fullmatch(
                        r"(?:[0-9A-Fa-f]{2})+", encoded) else base64.b64decode(encoded))
                output = cipher.decrypt(raw)
                if "PKCS" in spec:
                    output = unpad(output, block_size)
                elif "ZERO" in spec:
                    output = output.rstrip(b"\0")
            else:
                raw = (cls._byte_values(decoded_value) if isinstance(decoded_value, list)
                       else str(decoded_value).encode())
                if cipher_type is not ARC4 and "NOPADDING" not in spec:
                    raw = (raw.ljust((len(raw) + block_size - 1) // block_size * block_size, b"\0")
                           if "ZERO" in spec else pad(raw, block_size))
                output = cipher.encrypt(raw)
            if operation.endswith("Bytes"):
                return json.dumps(list(output))
            return (output.decode("utf-8", errors="replace") if decrypting else
                    base64.b64encode(output).decode("ascii"))
        except Exception as exc:
            raise JsBridgeError(f"crypto operation failed: {exc}") from None

    @classmethod
    def _aead(cls, operation: str, algorithm: str, key_bytes: bytes, nonce: bytes,
              value: str, tag_bits) -> str:
        """JCA authenticated ciphers, which append the tag to the ciphertext."""
        tag_length = max(1, int(tag_bits) // 8)
        if algorithm.startswith("CHACHA20"):
            from Crypto.Cipher import ChaCha20_Poly1305
            if len(key_bytes) != 32:
                raise ValueError("ChaCha20 key must contain 32 bytes")
            cipher = ChaCha20_Poly1305.new(key=key_bytes, nonce=nonce)
        else:
            from Crypto.Cipher import AES
            cipher = AES.new(key_bytes, AES.MODE_GCM, nonce=nonce)
        decoded_value = json.loads(value)
        raw = (cls._byte_values(decoded_value) if isinstance(decoded_value, list)
               else str(decoded_value).encode())
        decrypting = operation.startswith("decrypt")
        if decrypting:
            if len(raw) < tag_length:
                raise ValueError("ciphertext is shorter than the AEAD tag")
            output = cipher.decrypt_and_verify(raw[:-tag_length], raw[-tag_length:])
        else:
            ciphertext, tag = cipher.encrypt_and_digest(raw)
            output = ciphertext + tag
        if operation.endswith("Bytes"):
            return json.dumps(list(output))
        return (output.decode("utf-8", errors="replace") if decrypting else
                base64.b64encode(output).decode("ascii"))

    @staticmethod
    def _symmetric_generate(transformation: str, need_key=False, need_iv=False) -> str:
        algorithm = str(transformation).upper().split("/", 1)[0]
        key_size = 24 if algorithm in {"DESEDE", "TRIPLEDES", "3DES"} else (
            8 if algorithm == "DES" else 16)
        block_size = 8 if algorithm in {"DES", "DESEDE", "TRIPLEDES", "3DES"} else 16
        return json.dumps({
            "key": list(os.urandom(key_size)) if need_key else None,
            "iv": (list(os.urandom(block_size)) if need_iv and
                   "/ECB" not in str(transformation).upper() else None),
        })

    @classmethod
    def _crypto_argument_bytes(cls, value, encoded_text=False) -> bytes:
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            decoded = value
        if isinstance(decoded, list):
            return cls._byte_values(decoded)
        text = str(decoded or "")
        if not encoded_text:
            return text.encode()
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", text):
            return bytes.fromhex(text)
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            return text.encode()

    @staticmethod
    def _export_key_pair(key) -> str:
        return json.dumps({
            "privateKey": base64.b64encode(key.export_key(format="DER")).decode(),
            "publicKey": base64.b64encode(key.public_key().export_key(format="DER")).decode(),
        })

    @classmethod
    def _asymmetric_generate(cls, algorithm="RSA") -> str:
        if "RSA" not in str(algorithm).upper():
            raise JsBridgeError(f"unsupported asymmetric algorithm: {algorithm}")
        from Crypto.PublicKey import RSA
        return cls._export_key_pair(RSA.generate(2048))

    @classmethod
    def _import_rsa_key(cls, value):
        from Crypto.PublicKey import RSA
        raw = cls._crypto_argument_bytes(value, encoded_text=True)
        try:
            return RSA.import_key(raw)
        except (ValueError, IndexError, TypeError) as exc:
            raise JsBridgeError(f"invalid RSA key: {exc}") from None

    @staticmethod
    def _rsa_private_encrypt(data: bytes, key) -> bytes:
        size = key.size_in_bytes()
        output = bytearray()
        for start in range(0, len(data), size - 11):
            block = data[start:start + size - 11]
            encoded = b"\x00\x01" + b"\xff" * (size - len(block) - 3) + b"\x00" + block
            output.extend(pow(int.from_bytes(encoded, "big"), key.d, key.n).to_bytes(size, "big"))
        return bytes(output)

    @staticmethod
    def _rsa_public_decrypt(data: bytes, key) -> bytes:
        size = key.size_in_bytes()
        if len(data) % size:
            raise JsBridgeError("invalid RSA ciphertext length")
        output = bytearray()
        for start in range(0, len(data), size):
            decoded = pow(int.from_bytes(data[start:start + size], "big"), key.e, key.n).to_bytes(
                size, "big")
            if not decoded.startswith(b"\x00\x01") or b"\x00" not in decoded[2:]:
                raise JsBridgeError("invalid RSA private-key ciphertext padding")
            output.extend(decoded.split(b"\x00", 2)[2])
        return bytes(output)

    @staticmethod
    def _rsa_raw(data: bytes, key, private_operation: bool,
                 decrypting: bool) -> bytes:
        size = key.size_in_bytes()
        exponent = key.d if private_operation else key.e
        if exponent is None:
            raise JsBridgeError("RSA private key is required")
        chunks = []
        input_size = size if decrypting else size
        for start in range(0, len(data), input_size):
            block = data[start:start + input_size]
            number = int.from_bytes(block, "big")
            if number >= key.n:
                raise JsBridgeError("RSA input is larger than the modulus")
            chunks.append(pow(number, exponent, key.n).to_bytes(size, "big"))
        return b"".join(chunks)

    @classmethod
    def _asymmetric_operation(cls, operation, algorithm, public_key, private_key,
                              value, use_public=True) -> str:
        try:
            from Crypto.Cipher import PKCS1_OAEP, PKCS1_v1_5
            from Crypto.Hash import SHA1, SHA224, SHA256, SHA384, SHA512
            public = cls._import_rsa_key(public_key)
            private = cls._import_rsa_key(private_key)
            encrypting = str(operation).startswith("encrypt")
            data = cls._crypto_argument_bytes(value, encoded_text=not encrypting)
            selected = public if use_public else private
            oaep = "OAEP" in str(algorithm).upper()
            no_padding = "NOPADDING" in str(algorithm).upper()
            oaep_hash = next((item for name, item in (
                ("SHA-512", SHA512), ("SHA512", SHA512),
                ("SHA-384", SHA384), ("SHA384", SHA384),
                ("SHA-256", SHA256), ("SHA256", SHA256),
                ("SHA-224", SHA224), ("SHA224", SHA224))
                if name in str(algorithm).upper()), SHA1)
            if no_padding:
                result = cls._rsa_raw(data, selected, private_operation=not use_public,
                                      decrypting=not encrypting)
            elif encrypting and use_public:
                cipher = (PKCS1_OAEP.new(selected, hashAlgo=oaep_hash)
                          if oaep else PKCS1_v1_5.new(selected))
                limit = selected.size_in_bytes() - (
                    2 * oaep_hash.digest_size + 2 if oaep else 11)
                result = b"".join(cipher.encrypt(data[index:index + limit])
                                  for index in range(0, len(data), limit))
            elif encrypting:
                if oaep:
                    raise JsBridgeError("OAEP does not support private-key encryption")
                result = cls._rsa_private_encrypt(data, selected)
            elif use_public:
                if oaep:
                    raise JsBridgeError("OAEP does not support public-key decryption")
                result = cls._rsa_public_decrypt(data, selected)
            else:
                cipher = (PKCS1_OAEP.new(selected, hashAlgo=oaep_hash)
                          if oaep else PKCS1_v1_5.new(selected))
                size = selected.size_in_bytes()
                chunks = []
                for index in range(0, len(data), size):
                    chunk = data[index:index + size]
                    decoded = cipher.decrypt(chunk) if oaep else cipher.decrypt(chunk, None)
                    if decoded is None:
                        raise JsBridgeError("RSA decryption failed")
                    chunks.append(decoded)
                result = b"".join(chunks)
            if operation in {"encrypt", "decrypt"}:
                return json.dumps(list(result))
            if operation == "encryptHex":
                return result.hex()
            if operation == "encryptBase64":
                return base64.b64encode(result).decode()
            return result.decode("utf-8", errors="replace")
        except JsBridgeError:
            raise
        except Exception as exc:
            raise JsBridgeError(f"asymmetric crypto failed: {exc}") from None

    @classmethod
    def _sign_generate(cls, algorithm="SHA256withRSA") -> str:
        upper = str(algorithm).upper()
        if "ECDSA" in upper or ("EC" in upper and "RSA" not in upper):
            from Crypto.PublicKey import ECC
            return cls._export_key_pair(ECC.generate(curve="P-256"))
        if "DSA" in upper and "RSA" not in upper:
            from Crypto.PublicKey import DSA
            return cls._export_key_pair(DSA.generate(2048))
        return cls._asymmetric_generate("RSA")

    @classmethod
    def _sign_operation(cls, operation, algorithm, public_key, private_key,
                        value, signature="") -> str:
        try:
            from Crypto.Hash import MD5, SHA1, SHA224, SHA256, SHA384, SHA512
            from Crypto.PublicKey import DSA, ECC, RSA
            from Crypto.Signature import DSS, pkcs1_15
            upper = str(algorithm).upper().replace("-", "")
            hash_type = next((item for name, item in (
                ("SHA512", SHA512), ("SHA384", SHA384), ("SHA256", SHA256),
                ("SHA224", SHA224), ("SHA1", SHA1), ("MD5", MD5)) if name in upper), SHA256)
            digest = hash_type.new(cls._crypto_argument_bytes(value))
            if "ECDSA" in upper or ("EC" in upper and "RSA" not in upper):
                importer, factory = ECC.import_key, lambda key: DSS.new(key, "fips-186-3")
            elif "DSA" in upper and "RSA" not in upper:
                importer, factory = DSA.import_key, lambda key: DSS.new(key, "fips-186-3")
            else:
                importer, factory = RSA.import_key, pkcs1_15.new
            if operation.startswith("sign"):
                key = importer(cls._crypto_argument_bytes(private_key, encoded_text=True))
                result = factory(key).sign(digest)
                return result.hex() if operation == "signHex" else json.dumps(list(result))
            key = importer(cls._crypto_argument_bytes(public_key, encoded_text=True))
            signed = cls._crypto_argument_bytes(signature, encoded_text=True)
            try:
                factory(key).verify(digest, signed)
                return "true"
            except (ValueError, TypeError):
                return "false"
        except Exception as exc:
            raise JsBridgeError(f"signature operation failed: {exc}") from None

    def _ajax(self, request_value: str, source_headers=None, base_url=None,
              cookie_enabled=True, cookie_tag=None) -> str:
        try:
            args = (request_value, source_headers, base_url)
            raw = self._request_json(*args, cookie_enabled, cookie_tag)
            return json.loads(raw)["body"]
        except Exception:
            # JsExtensions.ajax wraps the complete AnalyzeUrl request in runCatching
            # and exposes the exception stack as the JavaScript return value.
            return traceback.format_exc()

    def _connect_json(self, request_value: str, source_headers=None,
                      base_url=None, cookie_enabled=True, cookie_tag=None) -> str:
        try:
            args = (request_value, source_headers, base_url)
            return self._request_json(*args, cookie_enabled, cookie_tag)
        except Exception:
            raw_url = re.split(r",\s*\{", str(request_value), maxsplit=1)[0]
            resolved = urljoin(str(base_url or self.base_url), raw_url.strip())
            return json.dumps({
                "body": traceback.format_exc(), "bodyBytes": [], "headers": {},
                "headersMulti": {}, "status": 200, "message": "OK",
                "url": (resolved if urlsplit(resolved).scheme in {"http", "https"}
                        and urlsplit(resolved).netloc else "http://localhost/"),
                "responseKind": "str", "protocol": "http/1.1",
            }, ensure_ascii=False)

    def _ajax_all(self, values: str, source_headers=None, base_url=None,
                  cookie_enabled=True, cookie_tag=None) -> str:
        try:
            urls = json.loads(values)
        except (TypeError, json.JSONDecodeError) as exc:
            raise JsBridgeError(f"ajaxAll requires an array: {exc}") from None
        if not isinstance(urls, list):
            raise JsBridgeError("ajaxAll requires an array")
        if not urls:
            return "[]"
        with ThreadPoolExecutor(max_workers=len(urls),
                                thread_name_prefix="legado-ajax") as executor:
            futures = [
                executor.submit(self._request_json, str(url), source_headers, base_url,
                                cookie_enabled, cookie_tag)
                for url in urls
            ]
            responses = [json.loads(future.result()) for future in futures]
        return json.dumps(responses, ensure_ascii=False)

    def _open_with_retry(self, request: Request, retry=0, opener=None):
        attempts = int(retry or 0) + 1
        for attempt in range(attempts):
            try:
                return (opener or self.opener).open(request, timeout=self.timeout)
            except HTTPError as exc:
                if attempt + 1 >= attempts:
                    return exc
                exc.close()

    def _request_json(self, request_value: str, source_headers=None, base_url=None,
                      cookie_enabled=True, cookie_tag=None,
                      allow_webview=True, webview_js=None,
                      source_regex=None) -> str:
        from app.services.legado_url import encode_url, parse_request_config, split_post_url
        from app.services.request_compat import (escape_form, escape_url_query,
                                                 normalize_url_option)

        url = request_value
        config = {}
        if re.search(r",\s*\{", request_value):
            parts = split_post_url(request_value)
            if parts:
                url, raw_config = parts
                config = normalize_url_option(parse_request_config(raw_config))
        method = str(config.get("method", "GET"))
        body = config.get("body")
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        request_charset = str(config.get("charset") or "utf-8")
        headers = dict(self.default_headers)
        source_headers = dict(source_headers or {})
        proxy = None
        for name in list(source_headers):
            if str(name) == "proxy":
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
        if body is not None:
            from app.services.rule_engine import ReaderEngine
            content_type = next((str(value) for key, value in headers.items()
                                 if str(key).lower() == "content-type"), "")
            data = ReaderEngine._encode_request_body(
                body, request_charset, content_type)
        else:
            data = None
        url = urljoin(str(base_url if base_url is not None else self.base_url), url.strip())
        if str(config.get("charset") or "").lower() == "escape":
            url = escape_url_query(url)
        from app.services.rule_engine import ReaderEngine
        # AnalyzeUrl always merges CookieStore into the request.  The direct
        # JsExtensions get/post/head helpers only opt into CookieManager via
        # the CookieJar marker, so a disabled jar must not load stored cookies.
        if cookie_enabled or config.get("__rawJsoupRequest") is not True:
            merged_cookie = ReaderEngine._merge_cookie_header(
                self._cookie_string(url), headers.get("Cookie"))
            if merged_cookie:
                headers["Cookie"] = merged_cookie
        request = Request(encode_url(url), data=data, headers=headers, method=method)
        if self.opener is None:
            raise JsBridgeError("JS HTTP runtime has been closed")
        webview_value = config.get("webView")
        use_webview = bool(allow_webview) and not (
            webview_value is None or webview_value is False or
            isinstance(webview_value, str) and webview_value in {"", "false"})
        if use_webview and method != "POST" and self.browser_fetcher is not None:
            browser = self._browser_result({
                **config, "mode": "webView", "url": url,
                "headerMap": dict(headers), "cookieTag": str(cookie_tag or ""),
                "javaScript": config.get("webJs") or webview_js,
                "sourceRegex": source_regex,
            })
            return json.dumps(self._bridge_response_payload(browser), ensure_ascii=False)
        try:
            request_opener = None
            if proxy or config.get("__followRedirects") is False or not cookie_enabled:
                handlers = ([HTTPCookieProcessor(self.cookies)] if cookie_enabled else [])
                if config.get("__followRedirects") is False:
                    handlers.append(_NoRedirectHandler())
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                handlers.append(HTTPSHandler(context=ssl_context))
            if proxy:
                proxy = str(proxy).strip()
                proxy = proxy if "://" in proxy else "http://" + proxy
                handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
            if proxy or config.get("__followRedirects") is False or not cookie_enabled:
                request_opener = build_opener(*handlers)
            with self._open_with_retry(request, config.get("retry", 0), request_opener) as response:
                raw = b"" if method == "HEAD" else response.read()
                from app.services.rule_engine import ReaderEngine
                charset = response.headers.get_content_charset()
                content_type = response.headers.get("Content-Type", "")
                text_raw = (raw if config.get("__rawJsoupRequest") is True else
                            ReaderEngine._uncompress_response_body(raw, content_type))
                body_text = ReaderEngine._decode_response_body(text_raw, charset)
                if config.get("__rawJsoupRequest") is not True:
                    body_text = ReaderEngine._normalize_response_text(
                        body_text, content_type)
                result = {
                    "body": (raw.hex() if config.get("type") is not None else body_text),
                    "bodyBytes": list(raw),
                    "headers": {str(k).lower(): str(v) for k, v in response.headers.items()},
                    "headersMulti": {
                        str(name).lower(): [str(item) for item in
                                            (response.headers.get_all(name) or [])]
                        for name in dict(response.headers.items())
                    },
                    "status": int(getattr(response, "status", None) or 200),
                    "message": str(getattr(response, "reason", "") or ""),
                    "url": response.geturl(),
                    "responseKind": ("jsoup" if config.get("__rawJsoupRequest") is True
                                     else "str"),
                    "protocol": ({10: "http/1.0", 11: "http/1.1"}.get(
                        getattr(response, "version", 11), "http/1.1")),
                }
                if use_webview and method == "POST" and self.browser_fetcher is not None:
                    browser = self._browser_result({
                        **config, "mode": "webView", "url": result["url"],
                        "html": result["body"], "initialHtml": result["body"],
                        "initialUrl": result["url"], "headerMap": dict(headers),
                        "cookieTag": str(cookie_tag or ""),
                        "javaScript": config.get("webJs") or webview_js,
                        "sourceRegex": source_regex,
                    })
                    result = self._bridge_response_payload(browser)
                if cookie_enabled:
                    self.save_response_cookies(result["url"], response.headers)
                return json.dumps(result, ensure_ascii=False)
        except HTTPError as exc:
            try:
                raw = b"" if method == "HEAD" else exc.read()
                from app.services.rule_engine import ReaderEngine
                charset = exc.headers.get_content_charset() if exc.headers else None
                result = {
                    "body": (raw.hex() if config.get("type") is not None else
                             ReaderEngine._decode_response_body(raw, charset)),
                    "bodyBytes": list(raw),
                    "headers": ({str(k).lower(): str(v) for k, v in exc.headers.items()}
                                if exc.headers else {}),
                    "headersMulti": ({
                        str(name).lower(): [str(item) for item in
                                            (exc.headers.get_all(name) or [])]
                        for name in dict(exc.headers.items())
                    } if exc.headers else {}),
                    "status": int(exc.code),
                    "message": str(getattr(exc, "reason", "") or ""),
                    "url": exc.geturl() or url,
                    "responseKind": ("jsoup" if config.get("__rawJsoupRequest") is True
                                     else "str"),
                    "protocol": "http/1.1",
                }
                if cookie_enabled:
                    self.save_response_cookies(result["url"], exc.headers)
                return json.dumps(result, ensure_ascii=False)
            finally:
                exc.close()
        except URLError as exc:
            raise JsBridgeError(f"JS network error: {exc.reason}") from None


@lru_cache(maxsize=2048)
def split_js_rule(rule: str) -> tuple[str | None, str]:
    """返回 JS 预处理脚本和其后的普通解析规则。"""
    from app.services.source_rule import compile_rule_program

    value = (rule or "").strip()
    program = compile_rule_program(value)
    return program.leading_js()


@lru_cache(maxsize=1)
def implemented_java_api_paths() -> frozenset[str]:
    """JS 模拟面真正解析得到的全部 ``Packages.<path>``。

    清单由前导脚本自己导出（``__javaApiInventory``），模拟面变动时不需要维
    护第二份手写白名单；未模拟的路径不会出现在这里。
    """
    runtime = LegadoJsRuntime()
    listed = runtime.evaluate("@js:__javaApiInventory()", JsContext(source=None))
    return frozenset(str(item) for item in json.loads(listed or "[]"))
