"""基于 QuickJS 的轻量 Legado JavaScript 兼容层。"""
from __future__ import annotations

import base64
import hashlib
import json
import ssl
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


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


class LegadoJsRuntime:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.cookies = CookieJar()
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), HTTPSHandler(context=ssl_context))

    @staticmethod
    def available() -> bool:
        try:
            import quickjs  # noqa: F401
            return True
        except ImportError:
            return False

    def evaluate(self, script: str, context: JsContext):
        try:
            import quickjs
        except ImportError as exc:
            raise JsRuntimeUnavailable("缺少 quickjs，请执行 pip install quickjs") from exc

        ctx = quickjs.Context()
        ctx.set_memory_limit(128 * 1024 * 1024)
        ctx.add_callable("__ajax", self._ajax)
        ctx.add_callable("__post", lambda url, body="": self._ajax(f'{url},{{"method":"POST","body":{json.dumps(str(body))}}}'))
        ctx.add_callable("__md5", lambda value: hashlib.md5(str(value).encode()).hexdigest())
        ctx.add_callable("__b64decode", lambda value: base64.b64decode(str(value)).decode("utf-8", errors="replace"))
        ctx.add_callable("__b64encode", lambda value: base64.b64encode(str(value).encode()).decode())
        ctx.add_callable("__get", lambda name: context.variables.get(str(name)))
        ctx.add_callable("__get_string", lambda rule: context.get_string(str(rule)) if context.get_string else "")
        ctx.add_callable("__put", lambda name, value: self._put(context, name, value))
        ctx.add_callable("__cookie_get", lambda url: self._cookie_string(str(url)))
        ctx.add_callable("__unsupported", lambda name: self._unsupported(name))

        source_data = context.source.to_dict() if hasattr(context.source, "to_dict") else dict(context.source)
        bootstrap = f"""
        const key = {json.dumps(context.key)};
        const page = {int(context.page)};
        let result = {json.dumps(context.result)};
        const baseUrl = {json.dumps(context.base_url or source_data.get('bookSourceUrl', ''))};
        const __sourceData = {json.dumps(source_data, ensure_ascii=False)};
        const source = {{
          getKey: () => __sourceData.bookSourceUrl || '',
          getVariable: () => __get('sourceVariable') || '',
          setVariable: value => __put('sourceVariable', String(value)),
          key: __sourceData.bookSourceUrl || ''
        }};
        const java = {{
          ajax: value => __ajax(String(value)),
          getString: rule => __get_string(String(rule)),
          get: name => __get(String(name)),
          put: (name, value) => __put(String(name), value),
          post: (url, body) => __post(String(url), String(body || '')),
          md5Encode: value => __md5(String(value)),
          base64Decode: value => __b64decode(String(value)),
          base64Encode: value => __b64encode(String(value)),
          encodeURI: value => encodeURIComponent(String(value)),
          log: value => undefined,
          toast: value => undefined,
          longToast: value => undefined,
          webView: () => __unsupported('java.webView'),
          connect: value => {{
            const url = String(value);
            const body = __ajax(url);
            return {{
              body: () => body,
              headers: () => ({{}}),
              raw: () => ({{request: () => ({{url}})}})
            }};
          }},
          startBrowser: () => __unsupported('java.startBrowser'),
          createSymmetricCrypto: () => __unsupported('java.createSymmetricCrypto')
        }};
        const cookie = {{
          getCookie: url => __cookie_get(String(url)),
          getKey: (url, name) => {{
            const all = __cookie_get(String(url));
            const found = all.split(';').map(x => x.trim().split('=')).find(x => x[0] === name);
            return found ? found.slice(1).join('=') : '';
          }},
          removeCookie: () => undefined
        }};
        """
        clean = self._clean_script(script)
        try:
            value = ctx.eval(bootstrap + "\n" + clean)
            if hasattr(value, "json"):
                return json.loads(value.json())
            return value
        except Exception as exc:
            raise JsBridgeError(str(exc)) from exc

    @staticmethod
    def _clean_script(script: str) -> str:
        value = script.strip()
        if value.startswith("@js:"):
            value = value[4:].strip()
        if value.startswith("<js>") and value.endswith("</js>"):
            value = value[4:-5].strip()
        return value

    @staticmethod
    def _put(context: JsContext, name, value):
        context.variables[str(name)] = value
        return value

    @staticmethod
    def _unsupported(name):
        raise JsBridgeError(f"暂不支持 {name}")

    def _cookie_string(self, url: str) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.cookies
                         if cookie.domain in url)

    def _ajax(self, request_value: str) -> str:
        from test_book_sources_async import parse_request_config, split_post_url

        url = request_value
        config = {}
        if "," in request_value and ("method" in request_value or "body" in request_value or "headers" in request_value):
            parts = split_post_url(request_value)
            if parts:
                url, raw_config = parts
                config = parse_request_config(raw_config) or {}
        method = str(config.get("method", "GET")).upper()
        body = config.get("body")
        data = str(body).encode() if body is not None else None
        headers = config.get("headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                headers = {}
        request = Request(url.strip(), data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=self.timeout) as response:
            raw = response.read()
            charset = config.get("charset") or response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")


def split_js_rule(rule: str) -> tuple[str | None, str]:
    """返回 JS 预处理脚本和其后的普通解析规则。"""
    value = (rule or "").strip()
    if value.startswith("<js>") and "</js>" in value:
        end = value.find("</js>") + 5
        return value[:end], value[end:].strip()
    if value.startswith("@js:"):
        return value, ""
    return None, value
