"""Static compatibility audit for a complete Legado book-source corpus."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.models.book_source import BookSource
from app.services.engine.legado_js import LegadoJsRuntime


@dataclass(slots=True)
class ScriptSyntaxIssue:
    source_index: int
    source_name: str
    field: str
    message: str


@dataclass(slots=True)
class RuleSyntaxIssue:
    source_index: int
    source_name: str
    field: str
    rule: str
    message: str


@dataclass(slots=True)
class CorpusAudit:
    source_count: int = 0
    model_count: int = 0
    script_count: int = 0
    syntax_issues: list[ScriptSyntaxIssue] = field(default_factory=list)
    rule_count: int = 0
    rule_syntax_issues: list[RuleSyntaxIssue] = field(default_factory=list)
    unsupported_java_apis: list[str] = field(default_factory=list)
    unsupported_java_packages: list[str] = field(default_factory=list)
    unsupported_object_apis: list[str] = field(default_factory=list)
    unsupported_jsoup_pseudos: list[str] = field(default_factory=list)
    regex_count: int = 0
    regex_syntax_issues: list[RuleSyntaxIssue] = field(default_factory=list)
    unknown_rule_fields: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return (self.source_count == self.model_count and not self.syntax_issues and
                not self.rule_syntax_issues and
                not self.regex_syntax_issues and
                not self.unknown_rule_fields and
                not self.unsupported_java_apis and
                not self.unsupported_java_packages and
                not self.unsupported_object_apis and
                not self.unsupported_jsoup_pseudos)


SUPPORTED_JAVA_APIS = {
    "HMacBase64", "HMacHex", "aesBase64DecodeToByteArray",
    "aesBase64DecodeToString", "aesDecodeToByteArray", "aesDecodeToString",
    "aesEncodeToBase64ByteArray", "aesEncodeToBase64String",
    "aesEncodeToByteArray", "aesEncodeToString", "ajax", "ajaxAll",
    "aesDecodeArgsBase64Str", "aesEncodeArgsBase64Str",
    "androidId", "base64Decode", "base64DecodeToByteArray", "base64Encode",
    "bytesToStr", "cacheFile", "connect", "createAsymmetricCrypto", "createSign",
    "createSymmetricCrypto", "deleteFile",
    "desBase64DecodeToString", "desDecodeToString", "desEncodeToBase64String",
    "desEncodeToString", "digestBase64Str", "downloadFile",
    "deviceID", "digestHex", "encodeURI", "get", "getBook", "getCookie",
    "getByteArray", "getElement", "getElements", "getFile", "getInputStream",
    "getSource", "getStrResponse", "getString", "getStringList", "getResponse",
    "getGlideUrl",
    "getVerificationCode", "getWebViewUA", "head", "headerMap",
    "get7zByteArrayContent", "get7zStringContent", "getRarByteArrayContent",
    "getRarStringContent", "getTxtInFolder", "getZipByteArrayContent",
    "getZipStringContent",
    "hexDecodeToByteArray", "hexDecodeToString", "hexEncodeToString", "htmlFormat",
    "importScript", "initUrl", "isPost", "key", "lang", "log", "logType",
    "longToast", "md5Encode",
    "md5Encode16", "post", "put", "queryBase64TTF", "queryTTF", "randomUUID",
    "readFile", "readTxtFile", "redirectUrl", "refreshExplore", "refreshTocUrl",
    "reGetBook", "refreshBook",
    "replaceFont", "ruleUrl",
    "s2t", "setBaseUrl", "setContent", "setRedirectUrl", "splitSourceRule",
    "splitSourceRuleCacheString", "startBrowser", "startBrowserAwait", "strToBytes",
    "t2s", "timeFormat", "timeFormatUTC", "toNumChapter", "toURL", "toast",
    "un7zFile", "unArchiveFile", "unrarFile", "unzipFile", "utf8ToGbk",
    "tripleDESDecodeArgsBase64Str", "tripleDESDecodeStr",
    "tripleDESEncodeArgsBase64Str", "tripleDESEncodeBase64Str", "url",
    "webView", "webViewGetSource", "webViewGetOverrideUrl", "webViewUA",
    "evalJS", "getUserAgent",
}
SUPPORTED_OBJECT_APIS = {
    "source": {
        "getTag", "getKey", "getSource", "getHeaderMap", "getVariable",
        "setVariable", "put", "get", "getLoginHeader", "putLoginHeader",
        "removeLoginHeader", "getLoginHeaderMap", "getLoginInfo",
        "putLoginInfo", "removeLoginInfo", "getLoginInfoMap", "loginUi",
        "getLoginJs", "login", "evalJS", "getShareScope", "hashCode", "equals",
        "getSearchRule", "getExploreRule", "getBookInfoRule", "getTocRule",
        "getContentRule", "getReviewRule", "getDisPlayNameGroup", "addGroup",
        "removeGroup", "hasGroup", "removeInvalidGroups", "getInvalidGroupNames",
        "getDisplayVariableComment", "equal",
    },
    "book": {"getVariable", "putVariable", "getCustomVariable", "putCustomVariable",
             "setReverseToc", "getReverseToc", "getKindList",
             "getDisplayCover", "getDisplayIntro", "getUnreadChapterNum"},
    "chapter": {"getVariable", "putVariable", "isVip"},
    "cache": {"get", "put", "delete", "getFile", "putFile", "deleteMemory",
              "getFromMemory", "putMemory"},
    "cookie": {"getCookie", "getKey", "setCookie", "replaceCookie",
               "removeCookie", "cookieToMap", "mapToCookie", "clear"},
}

RULE_FIELD_NAMES = {
    "bookList", "name", "author", "intro", "kind", "lastChapter",
    "updateTime", "bookUrl", "coverUrl", "wordCount", "tocUrl",
    "chapterList", "chapterName", "chapterUrl", "isVolume", "isVip",
    "isPay", "content", "nextContentUrl", "nextTocUrl", "downloadUrls",
    "title",
}
SCRIPT_FIELD_NAMES = {
    "formatJs", "preUpdateJs", "payAction", "imageDecode", "webJs",
    "loginCheckJs", "coverDecodeJs",
}
REGEX_FIELD_NAMES = {"replaceRegex", "sourceRegex", "bookUrlPattern"}
METADATA_RULE_FIELDS = {"checkKeyWord", "imageStyle"}
KNOWN_RULE_FIELDS = (RULE_FIELD_NAMES | SCRIPT_FIELD_NAMES |
                     REGEX_FIELD_NAMES | METADATA_RULE_FIELDS |
                     {"init", "canReName"})
SUPPORTED_JSOUP_PSEUDOS = {
    "contains", "containsOwn", "eq", "gt", "has", "lt", "matches",
    "matchesOwn", "not", "nth-child", "nth-of-type", "regex",
}

# [M2-2.1] JsoupXpath（seimicrawler JXDocument / Xsoup）私有构造静态缺口登记。
# 差分语料 tools/legado_diff（684 条记录，其中 xpath 7 条）全量回灌后，真实书源
# 命中的私有轴/函数已由 xpath_values 的方言改写表等价翻译：
#   following-sibling-one:: / preceding-sibling-one:: → following-sibling::*[1] 等；
#   first() → 1；尾部 html()/outerHtml()/allText()/num()；属性比较 ^= $= *= ~= !~；
#   自定义函数 substring-ex/substring-after-last/substring-before-last/format-date/sum。
# 语料中未发现其它未覆盖的私有轴/函数。下列条目是**已知无法在 elementpath（W3C
# 数据模型）上等价复现**的类别——它们依赖 jsoup 节点树身份，桌面以 html5lib/elementpath
# 复刻时不可能逐字节逼近，登记为已知上限（导入期静态提示，不在运行期静默取错值）。
# 仅登记常量与注释，不改变 compat_audit 既有审计行为。
UNTRANSLATABLE_XPATH_CONSTRUCTS = {
    # 依赖 jsoup 节点对象身份（==）而非文档位置的兄弟/祖先链式选取；
    # elementpath 按节点位置定位，无法表达“同一个 JXNode 引用”。
    "jsoup-node-identity-axes",
    # JXDocument.asString() 对 jsoup Elements 集合身份的取值语义；跨选中节点集
    # 的隐式上下文切换不在 elementpath 单表达式能力内。
    "jsoup-elements-asstring-identity",
}


JAVA_PACKAGE_REFERENCE = re.compile(
    r"Packages\.((?:[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*)")


def source_texts(value):
    """Every string inside a source dict/model, wherever it is nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from source_texts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from source_texts(child)


def scan_missing_java_packages(texts) -> list[str]:
    """书源用到的 ``Packages.<path>``，本运行时没有模拟的那些。

    清单来自 JS 模拟面自身（``implemented_java_api_paths``），所以只报真正解析
    不到的路径；模拟面以后补齐了，这里自动不再报。
    """
    references = set()
    for text in texts:
        references.update(JAVA_PACKAGE_REFERENCE.findall(str(text or "")))
    if not references:
        return []
    from app.services.engine.legado_js import implemented_java_api_paths
    try:
        implemented = implemented_java_api_paths()
    except Exception:
        # 没有 JS 运行时就无法区分"未模拟"和"能跑"，此时报缺口只会误导；
        # 真正用到时规则阶段仍会抛带路径的错误。
        return []
    return sorted(name for name in references if name not in implemented)


def _rule_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in RULE_FIELD_NAMES and isinstance(child, str):
                yield child
            elif isinstance(child, (dict, list)):
                yield from _rule_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _rule_strings(child)


def _rule_entries(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in RULE_FIELD_NAMES and isinstance(child, str):
                yield child_path, child
            elif isinstance(child, (dict, list)):
                yield from _rule_entries(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _rule_entries(child, f"{path}[{index}]")


def _rule_entries_by_name(value, names: set[str], path=""):
    """Yield string fields by schema name without treating metadata as selectors."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in names and isinstance(child, str):
                yield child_path, child
            elif isinstance(child, (dict, list)):
                yield from _rule_entries_by_name(child, names, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _rule_entries_by_name(child, names, f"{path}[{index}]")


def _validate_regex_field(field_path: str, value: str) -> None:
    """Compile regex fields according to the syntax consumed by the engine."""
    pattern = value.strip()
    if pattern.startswith(("@js:", "<js>")):
        return
    if field_path.endswith(".replaceRegex"):
        if pattern.startswith("##"):
            pattern = pattern[2:]
        pattern = pattern.split("##", 1)[0]
    if pattern:
        try:
            from app.services.engine.java_regex import compile_pattern
            compile_pattern(pattern)
        except re.error:
            if field_path.endswith(".replaceRegex"):
                # Historical AnalyzeRule deliberately catches invalid regex
                # and applies the pattern as a literal String replacement.
                return
            raise


def _validate_rule_syntax(rule: str) -> None:
    """Compile the non-JS dialect portions of one source rule."""
    from app.services.engine.legado_rule_parser import _jsoup_part, select_jsoup, split_balanced

    original = str(rule)
    if re.search(r"<js>|@js:", original, flags=re.I):
        # JavaScript blocks are compiled by the script audit below. Their
        # preceding/following stages are runtime-dependent and are covered by
        # engine integration tests rather than guessed as a single selector.
        return
    value = re.sub(r"<js>[\s\S]*?</js>", "", original, flags=re.I).strip()
    value = re.split(r"@js:", value, maxsplit=1, flags=re.I)[0].strip()
    if "{{" in value or "{$" in value or re.search(r"\{(?:baseUrl|bookUrl)\}", value):
        # Template bodies are validated separately as JS/rule stages; the
        # surrounding string is a rendered value rather than a selector.
        return
    value = re.sub(r"@(?:get|put):\{[^{}]*\}", "x", value, flags=re.I)
    value = value.lstrip("+-").strip()
    if not value or value.startswith(("http://", "https://", "data:", "<")):
        return
    if value.startswith("/") and not (
            value.startswith("//") or value.endswith(("/text()", "/html()")) or
            re.search(r"/@[\w:-]+$", value)):
        return
    if value.startswith(":"):
        for pattern in split_balanced(value[1:], "&&"):
            re.compile(pattern)
        return
    for operator in ("&&", "%%", "||"):
        parts = split_balanced(value, operator)
        if len(parts) > 1:
            for part in parts:
                _validate_rule_syntax(part)
            return
    selector = value.split("##", 1)[0].strip()
    selector = re.sub(r"::text\s*$", "@text", selector, flags=re.I)
    if not selector or re.fullmatch(r"(?:\$\d+|x)+", selector):
        return
    if re.fullmatch(r"-?\d+(?:\.\d+)?", selector):
        return
    lowered = selector.lower()
    if lowered.startswith("@json:") or selector.startswith(("$.", "$[")):
        from jsonpath_ng.ext import parse
        expression = (selector.split(":", 1)[1].strip()
                      if lowered.startswith("@json:") else selector)
        parse(re.sub(r"\](?=[A-Za-z_$])", "].", expression))
        return
    if (lowered.startswith("@xpath:") or selector.startswith(("//", "./")) or
            (selector.startswith("/") and
             (selector.endswith(("text()", "html()")) or "/@" in selector))):
        import elementpath
        expression = (selector.split(":", 1)[1].strip()
                      if lowered.startswith("@xpath:") else selector)
        if expression.endswith("/html()"):
            expression = expression[:-7]
        elif expression.endswith("@html") and not expression.endswith("/@html"):
            expression = expression[:-5]
        elementpath.Selector("." + expression if expression.startswith("//") else expression)
        return
    # Literal capture templates and fixed text are values, not selectors.
    if "$" in selector and not any(token in selector for token in ("@", ".", "#", "[")):
        return
    force_css = lowered.startswith("@css:")
    if force_css:
        selector = selector.split(":", 1)[1].strip()
        parts = [selector.rsplit("@", 1)[0] if "@" in selector else selector]
    else:
        parts = split_balanced(selector, "@")
        if len(parts) > 1:
            parts = parts[:-1]
    from bs4 import BeautifulSoup
    dummy = BeautifulSoup("<html><body><div></div></body></html>", "html.parser")
    for part in parts:
        if not part or part in {"text", "html", "all", "ownText", "textNodes"}:
            continue
        css, _, _ = _jsoup_part(part)
        try:
            select_jsoup(dummy, css)
        except Exception as css_error:
            # JSON sources commonly omit the optional '$.' prefix. If the
            # expression compiles as JSONPath it is valid for JSON responses.
            try:
                from jsonpath_ng.ext import parse
                expression = part if part.startswith("$") else (
                    "$" + part if part.startswith(".") else "$." + part)
                parse(expression)
            except Exception:
                raise css_error


def _script_blocks(value, source_index: int, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SCRIPT_FIELD_NAMES and isinstance(child, str) and child.strip():
                script = child.strip()
                if script.startswith("@js:"):
                    script = script[4:]
                elif script.startswith("<js>") and script.endswith("</js>"):
                    script = script[4:-5]
                yield f"{path}.{key}" if path else key, script
                continue
            yield from _script_blocks(child, source_index, f"{path}.{key}")
        login_ui = value.get("loginUi")
        login_url = value.get("loginUrl")
        if (isinstance(login_ui, str) and
                login_ui.lstrip().startswith(("[", "@js:", "<js>")) and
                isinstance(login_url, str) and
                re.search(r"\b(?:function|var|let|const)\b|eval\s*\(", login_url)):
            yield f"{path}.loginUrl#library", login_url.strip()
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _script_blocks(child, source_index, f"{path}[{index}]")
    elif isinstance(value, str):
        blocks = list(re.finditer(r"<js>([\s\S]*?)</js>", value, re.I))
        for index, match in enumerate(blocks):
            script = match.group(1).strip()
            # Some legacy sources use <js> merely to delimit a ## replacement.
            if not script.startswith("##"):
                yield f"{path}#js{index}", script
        if value.strip().startswith("@js:") and not blocks:
            yield path, value.strip()[4:]
        elif (path.endswith(".loginUi") and
              value.lstrip().startswith("[")):
            # loginUi is a JavaScript array/object expression rather than a
            # statement block; wrap it so QuickJS validates object keys too.
            yield path, "void (" + value.strip() + ")"


def audit_source_corpus(path: str | Path) -> CorpusAudit:
    """Load every source and compile every JS stage after Rhino normalization."""
    try:
        import quickjs
    except ImportError as exc:
        raise RuntimeError("quickjs is required for the Legado corpus audit") from exc

    path = Path(path)
    raw_sources = json.loads(path.read_text(encoding="utf-8"))
    models = BookSource.from_json(path)
    audit = CorpusAudit(source_count=len(raw_sources), model_count=len(models))
    used_rule_fields = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        for section_name in ("ruleSearch", "ruleExplore", "ruleBookInfo",
                             "ruleToc", "ruleContent", "ruleReview"):
            section = source.get(section_name)
            if isinstance(section, dict):
                used_rule_fields.update(section)
    audit.unknown_rule_fields = sorted(used_rule_fields - KNOWN_RULE_FIELDS)
    corpus_text = path.read_text(encoding="utf-8")
    audit.unsupported_java_packages = scan_missing_java_packages([corpus_text])
    used_java_apis = set(re.findall(
        r"(?<!Packages\.)(?<![\w$])java\.([A-Za-z_$][\w$]*)\s*\(", corpus_text))
    audit.unsupported_java_apis = sorted(used_java_apis - SUPPORTED_JAVA_APIS)
    unsupported_objects = set()
    for object_name, supported in SUPPORTED_OBJECT_APIS.items():
        called = set(re.findall(
            rf"(?<![\w$]){object_name}\.([A-Za-z_$][\w$]*)\s*\(", corpus_text))
        unsupported_objects.update(
            f"{object_name}.{name}" for name in called - supported)
    # One source intentionally reassigns `chapter` to a string before calling
    # substring; it is not a method on the Legado chapter object.
    unsupported_objects.discard("chapter.substring")
    audit.unsupported_object_apis = sorted(unsupported_objects)
    used_pseudos = set()
    for rule in _rule_strings(raw_sources):
        plain = re.sub(r"<js>[\s\S]*?</js>", "", rule, flags=re.I)
        plain = re.sub(r"\{\{[\s\S]*?\}\}", "", plain)
        if plain.lstrip().startswith(("@js:", "//javascript:")):
            continue
        plain = re.split(r"@js:", plain, maxsplit=1, flags=re.I)[0]
        used_pseudos.update(re.findall(r":([A-Za-z-]+)\s*\(", plain))
    audit.unsupported_jsoup_pseudos = sorted(
        used_pseudos - SUPPORTED_JSOUP_PSEUDOS)
    for source_index, source in enumerate(raw_sources):
        source_name = str(source.get("bookSourceName", ""))
        for field_path, rule in _rule_entries(source):
            audit.rule_count += 1
            try:
                _validate_rule_syntax(rule)
            except Exception as exc:
                audit.rule_syntax_issues.append(RuleSyntaxIssue(
                    source_index, source_name, field_path, rule,
                    str(exc).splitlines()[0]))
        for field_path, value in _rule_entries_by_name(source, REGEX_FIELD_NAMES):
            if not value.strip():
                continue
            audit.regex_count += 1
            try:
                _validate_regex_field(field_path, value)
            except Exception as exc:
                audit.regex_syntax_issues.append(RuleSyntaxIssue(
                    source_index, source_name, field_path, value,
                    str(exc).splitlines()[0]))
        for field_path, script in _script_blocks(source, source_index):
            audit.script_count += 1
            # Nested {{ rules }} are evaluated before the containing JS stage.
            prepared = re.sub(r"\{\{[\s\S]*?\}\}", "0", script).split("##", 1)[0]
            prepared = LegadoJsRuntime._clean_script(prepared)
            try:
                quickjs.Context().eval(f"(function(){{\n{prepared}\n}})")
            except Exception as exc:
                audit.syntax_issues.append(ScriptSyntaxIssue(
                    source_index, source_name, field_path, str(exc).splitlines()[0]))
    return audit
