#!/usr/bin/env python3
"""
Legado (阅读) 书源连通性 + 搜索可用性测试 (AsyncIO 版)

测试层级:
  第1层 - 域名/服务器连通性: GET bookSourceUrl, 检查 HTTP 状态码
  第2层 - 搜索功能可用性:   用 test_keyword 搜索, 尝试按 bookList 规则提取结果
"""

import json
import sys
import asyncio
from datetime import datetime
from urllib.parse import urlparse, quote, urlsplit, urljoin
import re
import os
import aiohttp

# Windows consoles commonly default to GBK, while book-source names and error
# messages may contain emoji or other Unicode characters.  Keep the regression
# runner from failing after a successful test merely while printing its report.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

CONCURRENCY = 30
REQUEST_TIMEOUT = 15
# 搜索测试用的默认关键词
DEFAULT_TEST_KEYWORD = "仙"


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


# ========== URL 工具函数 ==========

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


# ========== 搜索 URL 解析 ==========

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
            config = parse_request_config(config_str)
            if config is None:
                return None
            method = (config.get("method") or "GET").upper()
            body = config.get("body", "")
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


def parse_request_config(config_str: str) -> dict | None:
    """兼容标准 JSON 及常见的 Legado 宽松对象写法。"""
    try:
        value = json.loads(config_str)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # 兼容 headers:、method: 等未加引号的键，以及单引号字符串。
    try:
        relaxed = re.sub(r"([{,]\s*)([A-Za-z_][\w-]*)(\s*:)", r'\1"\2"\3', config_str)
        relaxed = relaxed.replace("'", '"')
        value = json.loads(relaxed)
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


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


# ========== bookList 结果提取 ==========

def extract_by_booklist(response_text: str, content_type: str,
                        book_list_rule: str) -> int:
    """
    尝试用 bookList 规则从响应中提取书籍数量。
    返回找到的条目数, 或 -1 (无法解析), -2 (需要JS)
    """
    if not book_list_rule or not response_text:
        return -1

    rule = usable_non_js_rule(book_list_rule)

    # JS 相关规则 - 无法处理
    if rule.startswith("@js:") or rule.startswith("<js") or rule == "":
        return -2

    # @css: 开头
    if rule.startswith("@css:"):
        css = rule[5:].strip()
        return _extract_by_css(response_text, css)

    # @JSON: 开头
    if rule.startswith("@JSON:") or rule.startswith("@Json:"):
        json_path = rule.split(":", 1)[1].strip() if ":" in rule else ""
        return _extract_by_json(response_text, json_path)

    # 尝试 JSON 解析 (如果响应是 JSON)
    is_json = "json" in content_type.lower() or response_text.strip().startswith(("[", "{"))
    if is_json:
        # JSONPath 模式: $.a.b.c
        if rule.startswith("$.") or rule.startswith("$.."):
            return _extract_by_json(response_text, rule)
        # data.xxx 模式
        if not rule.startswith((".", "#", "[", "@", "/", "<")):
            return _extract_by_json(response_text, rule)

    # CSS 选择器 或 Legado 混合选择器
    if HAS_BS4:
        # 对 Legado 混合选择器, 提取 CSS 部分 (忽略 @tag 等后缀)
        simple_css = simplify_legado_selector(rule)
        if simple_css:
            return _extract_by_css(response_text, simple_css)

    return -1


def usable_non_js_rule(rule: str) -> str:
    """从“JS预处理 + 普通规则”组合中提取仍可尝试的普通规则。"""
    text = (rule or "").strip()
    if not text:
        return text
    # <js>...</js> 后常跟 CSS/JSONPath，先用原始响应尝试该规则。
    match = re.match(r"<js>[\s\S]*?</js>\s*([\s\S]+)$", text, flags=re.I)
    if match and match.group(1).strip():
        return match.group(1).strip()
    # 普通 JSONPath 后附加 JS 增强时，先执行前置 JSONPath。
    match = re.match(r"([^\n]+)\s*<js>[\s\S]*?</js>", text, flags=re.I)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text


def simplify_legado_selector(rule: str) -> str | None:
    """
    简化 Legado 混合选择器为基本 CSS 选择器。
    例如:
      class.col-6.col-md-4...@tag.a!0  → .col-6.col-md-4...
      .content@p@text                    → .content
      id.book-list@tag.li!-1             → #book-list
      class.ser-ret@li||class.j_bookList@li → .ser-ret (第一个)
    """
    # 如果有 || 取第一个
    if "||" in rule:
        rule = rule.split("||")[0].strip()

    # 去掉 @ 后面的部分
    if "@" in rule:
        rule = rule.split("@")[0].strip()

    # 处理 class.xxx / id.xxx 格式
    if rule.startswith("class."):
        rule = "." + rule[6:]
    elif rule.startswith("id."):
        rule = "#" + rule[3:]
    elif rule.startswith("tag."):
        rule = rule[4:]

    # 清理末尾的 !0, !-1 等
    rule = re.sub(r"!\s*-?\d+\s*$", "", rule).strip()

    if not rule:
        return None

    # 验证是否像 CSS 选择器
    if rule.startswith((".", "#", "[", "a", "div", "li", "ul", "p", "span",
                        "table", "tr", "td", "th", "h1", "h2", "h3", "h4",
                        "body", "head", "html", "meta", "img", "input",
                        "form", "button", "select", "option", "nav",
                        "section", "article", "header", "footer", "main")):
        return rule

    # 如果是纯 JSONPath (如 data.books), 不是 CSS, 返回 None
    if "." in rule and not rule.startswith("."):
        return None

    return rule if rule.startswith((".", "#", "[")) else None


def _extract_by_css(html_text: str, css_selector: str) -> int:
    """使用 CSS 选择器提取元素数量"""
    if not HAS_BS4:
        return -1
    if not css_selector:
        return -1
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select(css_selector)
        return len(items)
    except Exception:
        return -1


def jsonpath_get(data, path: str) -> list:
    """
    简单的 JSONPath 实现。
    支持: $.a.b.c, $.a[*].b, data.books, $..key (递归)
    返回匹配值列表。
    """
    # 去掉 $.
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$.."):
        path = path[1:]  # 保留一个 .

    # 处理 $..xxx (递归)
    if path.startswith(".."):
        results = []
        _recursive_find(data, path[2:], results)
        return results

    current = [data]
    # 按 . 分割路径
    parts = path.split(".")
    for part in parts:
        if not part:
            continue
        next_current = []
        # 处理 [*]、[0]、[:10] 等数组访问
        array_match = re.match(r"^(.*)\[([^]]+)\]$", part)
        if array_match:
            key, expression = array_match.groups()
            for item in current:
                if key and isinstance(item, dict):
                    val = item.get(key, [])
                elif not key:
                    val = item
                else:
                    val = []
                if not isinstance(val, list):
                    continue
                if expression == "*":
                    next_current.extend(val)
                elif expression.startswith(":"):
                    try:
                        next_current.extend(val[:int(expression[1:])])
                    except ValueError:
                        pass
                else:
                    try:
                        next_current.append(val[int(expression)])
                    except (ValueError, IndexError):
                        pass
        else:
            for item in current:
                if isinstance(item, dict):
                    val = item.get(part)
                    if val is not None:
                        if isinstance(val, list):
                            next_current.extend(val)
                        else:
                            next_current.append(val)
                elif isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, dict):
                            val = sub.get(part)
                            if val is not None:
                                if isinstance(val, list):
                                    next_current.extend(val)
                                else:
                                    next_current.append(val)
        current = next_current
        if not current:
            break
    return current


def _recursive_find(data, key: str, results: list):
    """递归查找所有 key"""
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                results.append(v)
            else:
                _recursive_find(v, key, results)
    elif isinstance(data, list):
        for item in data:
            _recursive_find(item, key, results)


def _extract_by_json(response_text: str, json_path: str) -> int:
    """使用 JSONPath 提取元素数量"""
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return -1

    if not json_path:
        # 如果顶层是数组, 返回长度
        if isinstance(data, list):
            return len(data)
        return 1 if data else -1

    results = jsonpath_get(data, json_path)
    if isinstance(results, list):
        return len(results)
    return 1 if results else 0


# ========== 主测试函数 ==========

TEST_KEYWORDS = ["仙", "小说", "剑来", "我"]  # 按优先级


def pick_test_keyword(source: dict) -> str:
    """从 source 的 checkKeyWord 或默认列表中选择测试关键词"""
    ckw = source.get("ruleSearch", {}).get("checkKeyWord", "")
    if ckw and isinstance(ckw, str) and len(ckw) <= 20:
        return ckw.strip()
    # 尝试默认关键词
    return DEFAULT_TEST_KEYWORD


async def test_one(source: dict, idx: int, session: aiohttp.ClientSession,
                   sem: asyncio.Semaphore, timeout: int,
                   search_enabled: bool = True) -> dict:
    """异步测试单个书源 (第1层 + 第2层)"""
    name = source.get("bookSourceName", f"#{idx}")
    raw_url = source.get("bookSourceUrl", "")
    group = source.get("bookSourceGroup", "")
    enabled = source.get("enabled", True)

    result = {
        "idx": idx,
        "name": name,
        "group": group,
        "enabled": enabled,
        "raw_url": raw_url,
        "clean_url": None,
        # 第1层
        "status": "skip",
        "status_code": None,
        "response_time_ms": None,
        "error": None,
        # 第2层
        "search_tried": False,
        "search_status": None,   # ok / skip / js_needed / post_template / parse_error
        "search_method": None,
        "search_keyword": None,
        "search_url_used": None,
        "book_count": None,
    }

    url = clean_url(raw_url)
    if not url:
        result["status"] = "skip"
        return result

    url = encode_url(url)
    result["clean_url"] = url

    # ---- 第1层: 基础连通性 ----
    async with sem:
        start = asyncio.get_event_loop().time()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                   ssl=False) as resp:
                elapsed = (asyncio.get_event_loop().time() - start) * 1000
                result["status_code"] = resp.status
                result["response_time_ms"] = round(elapsed, 1)
                result["status"] = "ok" if resp.status < 400 else ("warn" if resp.status < 500 else "fail")
        except asyncio.TimeoutError:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            result["response_time_ms"] = round(elapsed, 1)
            result["error"] = "Timeout"
            result["status"] = "err"
            return result
        except aiohttp.ClientResponseError as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            result["status_code"] = e.status
            result["response_time_ms"] = round(elapsed, 1)
            msg = e.message or f"HTTP {e.status}"
            result["error"] = msg[:80]
            result["status"] = "warn" if e.status < 500 else "fail"
            return result
        except (aiohttp.ClientConnectorError, aiohttp.ClientError) as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            result["response_time_ms"] = round(elapsed, 1)
            result["error"] = _shorten_err(str(e))
            result["status"] = "down"
            return result
        except Exception as e:
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            result["response_time_ms"] = round(elapsed, 1)
            result["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            result["status"] = "err"
            return result

    # 只有第1层成功才尝试第2层
    if result["status"] != "ok":
        return result
    if not search_enabled:
        result["search_status"] = "skip"
        result["search_method"] = "connect-only"
        return result

    # ---- 第2层: 搜索可用性 ----
    search_url_raw = source.get("searchUrl", "")
    rule_search = source.get("ruleSearch", {}) or {}
    book_list_rule = rule_search.get("bookList", "")

    if not search_url_raw or not book_list_rule:
        result["search_status"] = "skip"
        result["search_method"] = "无searchUrl或bookList"
        return result

    keyword = pick_test_keyword(source)
    result["search_keyword"] = keyword

    # Interactive Legado bridges intentionally wait for a human/browser.  They
    # are supported by the application, but must not block an unattended corpus
    # regression (some captcha rules wait up to three minutes).
    interactive_rule_text = json.dumps(source, ensure_ascii=False, default=str)
    if re.search(r"\b(?:getVerificationCode|webView|startBrowser)\s*\(", interactive_rule_text):
        result["search_status"] = "js_needed"
        result["search_method"] = "interactive-browser-or-captcha"
        result["book_count"] = -1
        result["error"] = "Engine compatibility: requires interactive browser/captcha input"
        return result

    # Exercise the exact production request and rule pipeline used by the UI.
    from app.models.book_source import BookSource
    from app.services.rule_engine import ReaderEngine, UnsupportedRule
    from app.services.legado_js import JsBridgeError
    engine = ReaderEngine(timeout)
    try:
        books = await engine.search(BookSource.from_dict(source), keyword, 1)
        result["search_tried"] = True
        result["search_method"] = "ReaderEngine.search"
        result["book_count"] = len(books)
        result["search_status"] = "ok" if books else "no_results"
    except asyncio.TimeoutError:
        result["search_status"] = "timeout"
        result["book_count"] = -1
    except aiohttp.ClientResponseError as exc:
        result["search_status"] = "http_error"
        result["book_count"] = -1
        result["error"] = f"Engine search HTTP {exc.status}: {exc.message}"
    except UnsupportedRule as exc:
        result["search_status"] = "js_needed"
        result["book_count"] = -1
        result["error"] = f"Engine compatibility: {type(exc).__name__}: {str(exc)[:160]}"
    except JsBridgeError as exc:
        result["search_status"] = classify_js_bridge_error(str(exc))
        result["book_count"] = -1
        result["error"] = f"Engine script: {type(exc).__name__}: {str(exc)[:240]}"
    except RuntimeError as exc:
        message = str(exc)
        if "连接失败" in message or "Server disconnected" in message:
            result["search_status"] = "http_error"
        else:
            result["search_status"] = "error"
        result["book_count"] = -1
        result["error"] = f"Engine search: {type(exc).__name__}: {message[:160]}"
    except Exception as exc:
        result["search_status"] = "error"
        result["book_count"] = -1
        result["error"] = f"Engine search: {type(exc).__name__}: {str(exc)[:160]}"
    finally:
        engine.js.close()
    return result

    # 解析 searchUrl
    parsed = parse_search_url(search_url_raw, url, keyword)
    if parsed is None:
        if True:
            try:
                from app.models.book_source import BookSource
                from app.services.rule_engine import ReaderEngine
                engine = ReaderEngine(timeout)
                try:
                    js_value = await engine._resolve_rule_url(
                        search_url_raw, BookSource.from_dict(source), keyword, 1)
                finally:
                    engine.js.close()
                if isinstance(js_value, str):
                    parsed = parse_search_url(js_value, url, keyword)
                if parsed:
                    result["search_method"] = "js_compat"
                else:
                    raise RuntimeError("JS 未返回有效请求地址")
            except Exception as exc:
                result["search_status"] = "js_needed"
                result["search_method"] = f"JS兼容层未完成: {str(exc)[:70]}"
                result["error"] = f"JS search URL: {type(exc).__name__}: {str(exc)[:160]}"
                return result
        elif "{{" in search_url_raw and "}}" in search_url_raw:
            result["search_status"] = "js_needed"
            result["search_method"] = "复杂模板(含{{...}})"
            return result
        elif "," in search_url_raw and '"body"' in search_url_raw:
            result["search_status"] = "parse_error"
            result["search_method"] = "POST模板(解析失败)"
            return result
        else:
            result["search_status"] = "parse_error"
            result["search_method"] = f"无法解析: {search_url_raw[:60]}"
            return result

    result["search_tried"] = True
    result["search_url_used"] = parsed["url"][:200]
    result["search_method"] = parsed["parse_method"]

    async with sem:
        try:
            if parsed["method"] == "POST":
                post_data = parsed["body"] or ""
                # 判断 content-type
                post_headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if parsed.get("headers"):
                    if isinstance(parsed["headers"], dict):
                        post_headers.update(parsed["headers"])
                async with session.post(
                    parsed["url"], data=post_data, headers=post_headers,
                    timeout=aiohttp.ClientTimeout(total=timeout), ssl=False,
                    allow_redirects=True,
                ) as resp:
                    resp_text = await resp.text()
                    resp_status = resp.status
            else:
                async with session.get(
                    parsed["url"],
                    timeout=aiohttp.ClientTimeout(total=timeout), ssl=False,
                    allow_redirects=True,
                ) as resp:
                    resp_text = await resp.text()
                    resp_status = resp.status

            if resp_status >= 400:
                result["search_status"] = "http_error"
                result["book_count"] = -1
                result["error"] = (result.get("error") or "") + f" 搜索返回 {resp_status}"
                return result

            content_type = resp.content_type or ""
            js_rule = None
            remaining_rule = book_list_rule
            if book_list_rule.strip().startswith(("@js:", "<js>")):
                try:
                    from app.services.legado_js import LegadoJsRuntime, JsContext, split_js_rule
                    js_rule, remaining_rule = split_js_rule(book_list_rule)
                    runtime = LegadoJsRuntime(timeout)
                    try:
                        transformed = await asyncio.to_thread(
                            runtime.evaluate, js_rule,
                            JsContext(source=source, key=keyword, page=1,
                                      result=resp_text, base_url=parsed["url"]),
                        )
                    finally:
                        runtime.close()
                    resp_text = transformed if isinstance(transformed, str) else json.dumps(transformed, ensure_ascii=False)
                    content_type = "application/json" if resp_text.lstrip().startswith(("[", "{")) else "text/html"
                except Exception as exc:
                    result["search_status"] = "js_needed"
                    result["search_method"] += f" + JS兼容层未完成: {str(exc)[:60]}"
                    result["error"] = f"JS bookList: {type(exc).__name__}: {str(exc)[:160]}"
                    return result

            count = extract_by_booklist(resp_text, content_type, remaining_rule)

            if count == -2:
                result["search_status"] = "js_needed"
                result["search_method"] += " + bookList含JS/@js"
                return result

            result["book_count"] = max(count, 0)

            if count > 0:
                result["search_status"] = "ok"
            elif count == 0:
                result["search_status"] = "no_results"
            else:
                result["search_status"] = "parse_error"

        except asyncio.TimeoutError:
            result["search_status"] = "timeout"
            result["book_count"] = -1
        except aiohttp.ClientResponseError as e:
            result["search_status"] = "http_error"
            result["book_count"] = -1
            result["error"] = (result.get("error") or "") + f" 搜索HTTP {e.status}"
        except (aiohttp.ClientError, Exception) as e:
            result["search_status"] = "error"
            result["book_count"] = -1
            err_str = str(e)[:80]
            result["error"] = (result.get("error") or "") + f" 搜索失败: {err_str}"

    return result


def _shorten_err(err: str) -> str:
    for kw, short in [
        ("SSLEOFError", "SSL EOF"), ("UNEXPECTED_EOF", "SSL EOF"),
        ("CERTIFICATE_VERIFY_FAILED", "SSL CERT"),
        ("WRONG_VERSION_NUMBER", "SSL VER"),
        ("TLSV1_UNRECOGNIZED_NAME", "SSL NAME"),
        ("getaddrinfo failed", "DNS失败"),
        ("Cannot connect to host", "无法连接"),
        ("Server disconnected", "断开连接"),
    ]:
        if kw in err:
            return short
    return err[:80]


def fmt_time(ms: float | None) -> str:
    if ms is None:
        return "---"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def color_tag(s: str) -> str:
    m = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]",
         "down": "[DOWN]", "err": "[ERR]", "skip": "[SKIP]"}
    return m.get(s, "[?]")


def save_passed_sources(sources: list, results: list, original_path: str) -> str | None:
    """过滤第1层通过的书源并保存到新文件, 返回新文件路径"""
    passed_indices = set()
    for r in results:
        if r["status"] == "ok":
            passed_indices.add(r["idx"] - 1)  # 1-based → 0-based
    if not passed_indices:
        return None
    base, ext = os.path.splitext(original_path)
    output_path = f"{base}_passed{ext}"
    passed_sources = [sources[i] for i in sorted(passed_indices)]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(passed_sources, f, ensure_ascii=False, indent=2)
    return output_path


async def main():
    if len(sys.argv) < 2:
        print("用法: python test_book_sources_async.py <书源JSON文件> [并发数] [超时秒数]")
        sys.exit(1)

    json_path = sys.argv[1]
    concurrency = CONCURRENCY
    if len(sys.argv) >= 3:
        try:
            concurrency = int(sys.argv[2])
        except ValueError:
            pass
    timeout = REQUEST_TIMEOUT
    if len(sys.argv) >= 4:
        try:
            timeout = int(sys.argv[3])
        except ValueError:
            pass
    search_enabled = not (len(sys.argv) >= 5 and
                          sys.argv[4].strip().lower() in {"connect", "connect-only"})
    source_offset = 0
    source_limit = None
    if len(sys.argv) >= 6:
        try:
            source_offset = max(0, int(sys.argv[5]))
        except ValueError:
            pass
    if len(sys.argv) >= 7:
        try:
            source_limit = max(1, int(sys.argv[6]))
        except ValueError:
            pass

    print("=" * 82)
    print("  Legado (阅读) 书源测试 - 第1层(连通) + 第2层(搜索)")
    print(f"  文件: {json_path}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  并发: {concurrency}  |  超时: {timeout}s"
          f"  |  BeautifulSoup: {'OK' if HAS_BS4 else '未安装'}")
    print("=" * 82)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            sources = json.load(f)
    except Exception as e:
        print(f"\n  错误: 无法读取 JSON: {e}")
        sys.exit(1)

    if not isinstance(sources, list):
        print("\n  错误: JSON 格式不对")
        sys.exit(1)

    partial_run = source_offset > 0 or source_limit is not None
    if partial_run:
        end = None if source_limit is None else source_offset + source_limit
        sources = sources[source_offset:end]

    total = len(sources)
    print(f"\n  共 {total} 条书源, 开始测试...\n")

    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    all_results = []
    # 第1层统计
    l1_stats = {"ok": 0, "warn": 0, "fail": 0, "down": 0, "err": 0, "skip": 0}
    # 第2层统计 (从第1层ok中细分)
    l2_stats = {"ok": 0, "skip": 0, "js_needed": 0, "parse_error": 0,
                "no_results": 0, "http_error": 0, "timeout": 0, "error": 0}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async def isolated_test(source, index):
            try:
                return await test_one(source, index, session, sem, timeout,
                                      search_enabled=search_enabled)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {
                    "idx": index,
                    "name": str(source.get("bookSourceName") or "?"),
                    "status": "err", "status_code": None,
                    "response_time_ms": None,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "search_status": "error", "search_keyword": None,
                    "book_count": -1,
                }

        tasks = [isolated_test(s, source_offset + i + 1)
                 for i, s in enumerate(sources)]

        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            all_results.append(r)
            l1_stats[r["status"]] = l1_stats.get(r["status"], 0) + 1
            if r.get("search_status"):
                l2_stats[r["search_status"]] = l2_stats.get(r["search_status"], 0) + 1
            done += 1
            pct = done / total * 100
            print(f"\r  第1层 [{done}/{total}] {pct:.0f}%  "
                  f"OK={l1_stats['ok']} WARN={l1_stats['warn']}"
                  f" DOWN={l1_stats['down']} ERR={l1_stats['err']}"
                  f" SKIP={l1_stats['skip']}", end="", flush=True)

    all_results.sort(key=lambda x: x.get("idx", 0))

    # ---- 汇总 ----
    print("\n\n" + "=" * 82)

    print("  第1层 - 域名/服务器连通性")
    print("=" * 82)
    tested_l1 = total - l1_stats.get("skip", 0)
    print(f"    总数: {total}  |  已测试: {tested_l1}")
    print(f"    [OK]   正常:         {l1_stats.get('ok', 0):3d}")
    print(f"    [WARN] 客户端错误:   {l1_stats.get('warn', 0):3d}")
    print(f"    [FAIL] 服务端错误:   {l1_stats.get('fail', 0):3d}")
    print(f"    [DOWN] 无法连接:     {l1_stats.get('down', 0):3d}")
    print(f"    [ERR]  异常:         {l1_stats.get('err', 0):3d}")
    print(f"    [SKIP] 非URL:        {l1_stats.get('skip', 0):3d}")

    total_for_l2 = l1_stats.get("ok", 0)
    print(f"\n  {'='*78}")
    print(f"  第2层 - 搜索可用性 (在 {total_for_l2} 个OK书源中尝试)")
    print(f"  {'='*78}")
    l2_tried = total_for_l2 - l2_stats.get("skip", 0)
    print(f"    进入第2层测试: {l2_tried}")
    print(f"    [ok]        搜索成功(有结果): {l2_stats.get('ok', 0):3d}")
    print(f"    [skip]      无需搜索:         {l2_stats.get('skip', 0):3d}")
    print(f"    [js_needed] 需JS引擎:         {l2_stats.get('js_needed', 0):3d}")
    print(f"    [no_results]搜索0结果:        {l2_stats.get('no_results', 0):3d}")
    print(f"    [parse_err] 响应解析失败:     {l2_stats.get('parse_error', 0):3d}")
    print(f"    [http_err]  搜索HTTP错误:     {l2_stats.get('http_error', 0):3d}")
    print(f"    [timeout]   搜索超时:         {l2_stats.get('timeout', 0):3d}")
    print(f"    [error]     搜索异常:         {l2_stats.get('error', 0):3d}")

    # ---- 详细结果 ----
    print("\n" + "=" * 82)
    print("  详细结果")
    print("=" * 82)
    print(f"  {' ':>3s} 第1层   第2层     响应   状态码  搜索词   书籍数  书源名称")
    print(f"  {'-'*3} -----   -----   ------  ------  ------   -----  {'--'*30}")
    for r in all_results:
        idx = r.get("idx", 0)
        name = r.get("name", "?")
        st1 = color_tag(r.get("status", "?"))
        st2 = r.get("search_status") or "-"
        rt = fmt_time(r.get("response_time_ms"))
        code = str(r.get("status_code")) if r.get("status_code") else "--"
        kw = r.get("search_keyword", "") or ""
        bc = r.get("book_count")
        bc_str = str(bc) if bc is not None else "-"
        kw_display = kw[:6] if kw else "-"

        # 第2层简写
        s2_map = {"ok": "OK", "skip": "--", "js_needed": "JS",
                  "parse_error": "PE", "no_results": "NR",
                  "http_error": "HE", "timeout": "TO", "error": "ER"}
        st2s = s2_map.get(st2, st2[:2].upper() if st2 else "--")

        print(f"  [{idx:3d}] {st1}  {st2s:>5s}  {rt:>7s}  {code:>4s}"
              f"  {kw_display:>6s}  {bc_str:>5s}  {name}")
        if r.get("error") and r["error"] not in ("", None):
            err = r["error"][:90]
            print(f"        {err}")

    # 异常列表 (第1层异常)
    bad_l1 = [r for r in all_results if r["status"] in ("down", "err", "fail", "warn")]
    if bad_l1:
        print("\n" + "=" * 82)
        print("  第1层 - 异常书源列表")
        print("=" * 82)
        for r in bad_l1:
            idx = r.get("idx", 0)
            name = r.get("name", "?")
            st = r["status"]
            err = (r.get("error") or "")[:80]
            print(f"  [{idx:3d}] [{st.upper():5s}] {name}")
            if err:
                print(f"         {err}")

    # ---- 保存通过的书源 ----
    output_path = (None if partial_run else
                   save_passed_sources(sources, all_results, json_path))
    if output_path:
        c = len([r for r in all_results if r["status"] == "ok"])
        removed = len(all_results) - c
        print(f"\n  通过: {c}  |  移除: {removed}  |  保存到: {output_path}")

    print("\n" + "=" * 82)
    print("  测试完成")
    print("=" * 82)


if __name__ == "__main__":
    asyncio.run(main())
