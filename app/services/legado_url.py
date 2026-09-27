"""Legado ``searchUrl`` / ``bookSourceUrl`` template parsing."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin, urlsplit


def clean_url(raw_url: str) -> str | None:
    """清理 bookSourceUrl, 提取真正的 URL 部分"""
    if not raw_url:
        return None
    url = raw_url.strip()
    url = url.replace("\uff03", "#").replace("\uff20", "@")
    url = url.rstrip(",，。、；;:：")
    if "##" in url:
        url = url.split("##")[0]
    if "#" in url:
        url = url.split("#")[0]
    url = url.rstrip(",，。、；;:：")
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    url = url.rstrip("/")
    return url


#: 已经自带协议的地址不再加工：``scheme://`` 之外还要认无头内核真会收到的
#: ``data:`` / ``about:`` 这类不跟 ``//`` 的 scheme（"host:8080/path" 这种
#: 光看冒号分不清是不是协议，所以要求 ``://`` 或落在名单里）。
_SCHEME_PATTERN = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.\-]*://|(?:data|about|blob|javascript|mailto):)",
    re.IGNORECASE)


def fix_url(raw_url: str) -> str:
    """给没有协议的地址补 ``https://``（对齐 Legado ``UrlUtil.fixUrl``）。

    浏览器内核只认绝对 URL：地址栏手输的 ``www.baidu.com`` 原样递给
    ``page.goto`` 得到的是 ``Protocol error (Page.navigate): Cannot navigate
    to invalid URL``——一句话把整次渲染判死，用户看不到"为什么不行"。
    已带协议的地址原样返回，所以重复调用无害。
    """
    url = str(raw_url or "").strip()
    if not url or _SCHEME_PATTERN.match(url):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return f"https://{url}"


def encode_url(url: str) -> str:
    """非 ASCII 字符编为百分号/IDNA 格式"""
    try:
        url.encode("ascii")
        return url
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        parts = urlsplit(url)
    except Exception:
        return quote(url, safe=":/?#[]@!$&'()*+,;=-._~%")
    if parts.scheme and parts.netloc:
        netloc_encoded = []
        for seg in parts.netloc.split("."):
            try:
                seg.encode("ascii")
                netloc_encoded.append(seg)
            except (UnicodeEncodeError, UnicodeDecodeError):
                netloc_encoded.append(seg.encode("idna").decode("ascii"))
        safe_netloc = ".".join(netloc_encoded)
        safe_path = quote(parts.path, safe="/%")
        safe_query = quote(parts.query, safe="=&%")
        result = f"{parts.scheme}://{safe_netloc}{safe_path}"
        if safe_query:
            result += f"?{safe_query}"
        return result
    return quote(url, safe=":/?#[]@!$&'()*+,;=-._~%")


def split_post_url(text: str) -> tuple | None:
    """分割 POST 风格搜索URL: 'path,{"body":"..."}' → (path, config_json)"""
    # 必须定位逗号后的配置对象，不能把 URL 模板 {{page}} 的左花括号
    # 误判为配置开头。
    config_match = re.search(r",\s*(\{)", text)
    if not config_match:
        return None
    brace_idx = config_match.start(1)
    url_part = text[:brace_idx].strip().rstrip(",")
    config_part = text[brace_idx:]
    return url_part, config_part


def parse_request_config(config_str: str) -> dict | None:
    """兼容标准 JSON 及常见的 Legado 宽松对象写法。"""
    try:
        value = json.loads(config_str)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # Gson's JsonReader is lenient: accept unquoted keys and single-quoted
    # strings without corrupting apostrophes inside ordinary double strings.
    try:
        relaxed = re.sub(r"([{,]\s*)([A-Za-z_][\w-]*)(\s*:)", r'\1"\2"\3', config_str)
        output, index = [], 0
        while index < len(relaxed):
            char = relaxed[index]
            if char == '"':
                start = index
                index += 1
                while index < len(relaxed):
                    if relaxed[index] == "\\":
                        index += 2
                    elif relaxed[index] == '"':
                        index += 1
                        break
                    else:
                        index += 1
                output.append(relaxed[start:index])
                continue
            if char == "'":
                index += 1
                value = []
                while index < len(relaxed):
                    if relaxed[index] == "\\" and index + 1 < len(relaxed):
                        value.append(relaxed[index + 1])
                        index += 2
                    elif relaxed[index] == "'":
                        index += 1
                        break
                    else:
                        value.append(relaxed[index])
                        index += 1
                output.append(json.dumps("".join(value), ensure_ascii=False))
                continue
            output.append(char)
            index += 1
        relaxed = re.sub(r",\s*([}\]])", r"\1", "".join(output))
        value = json.loads(relaxed)
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def resolve_search_url(text: str, base_url: str, keyword: str, page: int) -> str | None:
    """
    尝试将 searchUrl 模板解析为可用的 URL。
    返回完整 URL 或 None。
    """
    url = text.strip()

    # 替换 {{key}} / {{searchKey}} / {{keyword}}
    for var in ("{{key}}", "{{searchKey}}", "{{keyword}}", "{{keys}}"):
        url = url.replace(var, keyword)
    url = url.replace("{{page}}", str(page))
    url = url.replace("{{pg}}", str(page))
    url = url.replace("{{page-1}}", str(max(page - 1, 0)))
    url = url.replace("{{page+1}}", str(page + 1))
    url = url.replace("{{baseUrl}}", base_url.rstrip("/") + "/")

    # Legado page alternatives: <first-page,second-page,later-pages>.  Once
    # the requested page exceeds the list, the final alternative is reused.
    def select_page(match):
        choices = [item.strip() for item in match.group(1).split(",")]
        if not choices:
            return ""
        return choices[min(max(int(page), 1) - 1, len(choices) - 1)]

    url = re.sub(r"<([^<>]*)>", select_page, url)

    # 检查是否还有未替换的 {{...}} 模板 (复杂表达式)
    if "{{" in url and "}}" in url:
        # 简单尝试替换 source.key 为 base_url
        if "{{source.key}}" in url or "{{sourceKey}}" in url:
            url = url.replace("{{source.key}}", base_url)
            url = url.replace("{{sourceKey}}", base_url)
            if "{{" not in url or "}}" not in url:
                return url  # 替换后没有模板了
        return None  # 仍有复杂模板, 无法处理

    # Legado also uses data: URLs as an internal transport for encoded IDs.
    if url.startswith("data:"):
        return url

    # 如果是相对路径, 拼接 base_url
    if not (url.startswith("http://") or url.startswith("https://")):
        url = urljoin(base_url.rstrip("/") + "/", url)

    # 必须是 http/https
    if not (url.startswith("http://") or url.startswith("https://")):
        return None

    # URL 编码
    url = encode_url(url)
    return url


def parse_search_url(search_url: str, base_url: str, keyword: str,
                     page: int = 1) -> dict | None:
    """
    解析 searchUrl, 返回请求参数或 None。

    返回: {"url": str, "method": "GET"/"POST", "body": str|None,
           "headers": dict|None, "parse_method": str} 或 None
    """
    if not search_url:
        return None

    text = search_url.strip()

    # 一些书源先给出可直接请求的 URL/POST 配置，后面再附加可选的 @js
    # 跳转或翻页处理。基础搜索检测优先使用前面的普通请求部分。
    js_suffix = re.search(r"\n\s*@js:\s*", text, flags=re.I)
    if js_suffix and js_suffix.start() > 0:
        plain_prefix = text[:js_suffix.start()].strip()
        if plain_prefix.startswith(("http://", "https://", "/")) or ",{" in plain_prefix:
            text = plain_prefix

    # @js: 标记 - 需要 JS 引擎
    if text.startswith("@js:"):
        return None

    # <js> 标记
    if text.startswith("<js") and "</js>" in text:
        return None

    # {{ 开头且包含 source. 等复杂模板
    if text.startswith("{{") and ("source." in text or "java." in text):
        return None

    # ========== 处理 POST 风格: /path,{"body":"...","method":"POST"} ==========
    if "," in text and re.search(r",\s*\{", text):
        # 可能是 URL,JSON_CONFIG 格式
        parts = split_post_url(text)
        if parts:
            url_part, config_str = parts
            # 对 url_part 做模板替换
            resolved_url = resolve_search_url(url_part, base_url, keyword, page)
            if not resolved_url:
                return None
            # 解析 config
            from app.services.request_compat import normalize_url_option
            config = normalize_url_option(parse_request_config(config_str))
            method = config.get("method", "GET")
            body = config.get("body")
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            elif body is not None and not isinstance(body, str):
                body = str(body)
            # body 中可能有 {{key}}
            if body:
                body = body.replace("{{key}}", keyword).replace("{{page}}", str(page))
            headers = config.get("headers")
            request = dict(config)
            request.update({
                "url": resolved_url,
                "method": method,
                "body": body,
                "headers": headers,
                "parse_method": "post_template",
            })
            return request

    # ========== 处理 GET 模板 URL ==========
    resolved = resolve_search_url(text, base_url, keyword, page)
    if resolved:
        return {
            "url": resolved,
            "method": "GET",
            "body": None,
            "headers": None,
            "parse_method": "get_template",
        }

    return None
