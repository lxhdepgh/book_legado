"""CodeEditorView：规则编辑器（§1.6 / 契约 §9）。

形态（第二十三轮改）：**选书源 → 选规则字段 → 只编辑那一条规则**，保存时由控制器
把这条规则合并回书源 JSON 再落库。以前这里只有一个"整份 JSON 大文本框 + 一份同样
内容的只读着色副本"，两处实错：

- 从导航直接进「规则」页签是**空编辑器**（唯一的入口是书源列表的「编辑」跳转），
  用户看到的就是一块空白加一个不知道在编辑谁的按钮；
- 着色副本把整份 JSON 又渲染一遍：白白吃掉一屏高度，内容还和上面完全一样，
  看着像界面坏了。现在这块只报**识别出了哪些规则关键字**（带颜色、按类计数），
  不再重复正文。

试跑调试另要一段**示例正文**（粘贴网页源码 / JSON），本页面**不联网取样本**
（记忆 legado-live-scan-av-block：不批量打真书源）。没给示例就不调引擎，
免得按钮点了只回一句"示例为空"。

纯 Flet 自绘：输入框 + 分词着色，不引入 Web 系 code editor（红线：不拖入第二套
Chromium）。词表来自本文件的 Legado 规则方言常量。

红线（§7.5）：本文件零 app.services.* import；高亮词表是方言常量，不导入解析器模块。
"""
from __future__ import annotations

import re

import flet as ft


# --------------------------------------------------------------------------- #
# 高亮词表：取自 legado_rule_parser.py 实现的 Legado 规则方言关键字。
# （@取值后缀 / 前缀分析器 / 连接符 / JSONPath / JS 块 / XPath 函数）
# --------------------------------------------------------------------------- #
#: @ 后的取值关键字（jsoup_values / xpath_values 分支识别的取值方式）。
_RULE_VALUE_SUFFIXES = (
    "text", "ownText", "textNodes", "html", "content", "children", "tag",
    "href", "src", "class", "id", "js", "regex", "abs",
)

#: 前缀分析器（决定解析器走 css/json/xpath 分支）。
_RULE_PREFIXES = (
    "css:", "json:", "xpath:", "get:", "put:", "regex:",
)

#: XPath / jsoup 扩展函数。
_RULE_XPATH_FUNCS = ("html()", "outerHtml()", "allText()", "num()", "text()")

#: 连接符（split_rule_expression：懒回退 / 取交集 / 拉链）。
_RULE_OPERATORS = ("||", "&&", "%%")

#: 暴露给测试与 UI 的完整关键字集合。
RULE_HIGHLIGHT_KEYWORDS: frozenset[str] = frozenset(
    "@" + s for s in _RULE_VALUE_SUFFIXES
) | frozenset("@" + s for s in _RULE_PREFIXES) | frozenset(_RULE_XPATH_FUNCS)

#: 「整份书源 JSON」这一编辑目标的字段键（控制器同用一个字面量，见其惰性 import）。
WHOLE_SOURCE = "@whole"

#: 摘要里给各类关键字起的人话名字。
_KIND_LABELS = {
    "jsblock": "JS 块",
    "string": "字面量",
    "jsonpath": "JSONPath",
    "op": "取值/连接符",
    "prefix": "分析器前缀",
}

# 分词正则：顺序敏感（先长后短、先整体后单词）。
_TOKEN_RE = re.compile(
    r"(?P<jsblock><js>.*?</js>)"                       # <js>…</js> 整体
    r"|(?P<str>[\"'][^\"']*[\"'])"                    # 引号串
    r"|(?P<jsonpath>\$\.?[\[\w$.]*)"                  # $. / $[ JSONPath
    r"|(?P<op>\|\||&&|%%|@\w+:|@\w+|!!?\d*)"           # 连接符/取值关键字/下标
    r"|(?P<prefix>(?:class|id)\.)",                   # class. / id.
    flags=re.IGNORECASE | re.DOTALL,
)


def tokenize_rule(text: str) -> list[tuple[str, str]]:
    """把规则文本切成 (片段, 类别) 列表；类别 ∈
    jsblock/string/jsonpath/op/prefix/plain。纯函数，便于离线单测。"""
    segments: list[tuple[str, str]] = []
    pos = 0
    for match in _TOKEN_RE.finditer(text or ""):
        if match.start() > pos:
            segments.append((text[pos:match.start()], "plain"))
        # 组名是 str（Python 里 string 不是合法组名），对外统一叫 "string"：
        # _COLORS / _KIND_LABELS 与调用方认的都是后者。
        kind = {"str": "string"}.get(match.lastgroup or "", match.lastgroup) or "plain"
        segments.append((match.group(0), kind))
        pos = match.end()
    if pos < len(text or ""):
        segments.append((text[pos:], "plain"))
    return segments or [("", "plain")]


def _is_json_document(text: str) -> bool:
    """整份书源 JSON 与单条规则的分界：前者一进来就是 ``{`` / ``[``。

    只用来决定要不要把引号串当关键字——JSON 里每个键名都是引号串，全列出来等于
    把"识别结果"变成"把文本再抄一遍"。
    """
    return str(text or "").lstrip()[:1] in {"{", "["}


def _recognised(text: str, as_json: bool):
    """产出 (关键字, 类别)；引号串**钻进去看**里面的规则语法。

    分词正则把 ``".title@text"`` 整体判成一个引号串（字符串分支排在 @ 分支前面），
    于是整份 JSON 模式下"跳过引号串"会连里面的 ``@text`` 一起丢掉——正是要提醒用户的
    那个关键字。所以：串本身在 JSON 模式不列（键名太多），但串里的规则关键字照列。
    """
    for segment, kind in tokenize_rule(text):
        if kind == "plain" or not segment.strip():
            continue
        if kind == "string":
            if not as_json:
                yield segment, kind
            inner = segment[1:-1] if len(segment) >= 2 else ""
            for sub, sub_kind in tokenize_rule(inner):
                if sub_kind not in ("plain", "string") and sub.strip():
                    yield sub, sub_kind
            continue
        yield segment, kind


class CodeEditorView(ft.Column):
    """规则编辑器：选源 / 选字段 + 输入框 + 关键字识别 + 示例正文 + 保存/试跑。"""

    #: 各高亮类别的颜色（深色/浅色主题下均可读的一组中性高亮色）。
    _COLORS = {
        "jsblock": ft.Colors.PURPLE_400,
        "string": ft.Colors.GREEN_700,
        "jsonpath": ft.Colors.CYAN_700,
        "op": ft.Colors.ORANGE_700,
        "prefix": ft.Colors.BLUE_700,
        "plain": None,
    }

    #: 关键字条最多列这么多，超出只报总数（整份 JSON 能有几百个）。
    _MAX_TOKENS = 40

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_save = None             # (text: str) -> None
        self.on_run_debug = None        # (text: str) -> None
        self.on_changed = None          # (text: str) -> None
        #: 选书源 / 选规则字段：把"编辑谁"这件事从暗处挪到明处。
        self.on_source_picked = None    # (book_source_url: str) -> None
        self.on_field_picked = None     # (field_key: str) -> None
        self.on_reload_sources = None   # () -> None

        self._title = ft.Text("规则编辑器", size=20, weight=ft.FontWeight.BOLD)

        # -- 编辑目标：哪个书源、哪一条规则 --
        self._source_dd = ft.Dropdown(
            label="书源", width=300, dense=True, options=[],
            on_select=lambda e: self._fire(self.on_source_picked, e.control.value))
        self._field_dd = ft.Dropdown(
            label="规则字段", width=260, dense=True, value=WHOLE_SOURCE,
            options=[ft.dropdown.Option(WHOLE_SOURCE, "整份书源 JSON")],
            on_select=lambda e: self._fire(self.on_field_picked, e.control.value))
        self._reload_btn = ft.IconButton(
            ft.Icons.REFRESH, tooltip="重新载入书源列表",
            on_click=lambda e: self._fire(self.on_reload_sources))

        # 输入框（编辑态）——输入即重算关键字识别
        self._editor = ft.TextField(
            multiline=True, expand=True, min_lines=12, max_lines=24,
            text_size=13, shift_enter=True,
            hint_text="在此编辑规则：选择器 @text/@href、$.JSONPath、<js>…</js>、|| 回退…",
            on_change=lambda e: self._on_text_changed())

        # 关键字识别（只列识别出来的片段并按类着色；不再抄一遍正文）
        self._analysis_summary = ft.Text("", size=12, color=ft.Colors.GREY_600)
        self._highlight = ft.Text("", selectable=True, size=13)
        self._analysis_host = ft.Container(
            visible=False, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=6, padding=8,
            content=ft.Column([self._analysis_summary, self._highlight],
                              spacing=4, tight=True))

        # 试跑用的示例正文（粘贴，不联网）
        self._sample = ft.TextField(
            label="示例正文（粘贴网页源码 / JSON，试跑用；本页不联网取样本）",
            multiline=True, min_lines=3, max_lines=8, text_size=12,
            shift_enter=True, value="")

        # 审计结果区（保存即校验：CompatAuditService 提示）
        self._audit = ft.Text("", size=12, color=ft.Colors.GREY_700)
        self._audit_host = ft.Container(visible=False, padding=6,
                                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST)

        # 试跑调试结果区（RuleEngineService.extract_values 单字段求值回显）。
        self._debug = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_700,
                              selectable=True)
        self._debug_host = ft.Container(visible=False, padding=6,
                                        border=ft.Border.all(1, ft.Colors.BLUE_200),
                                        border_radius=6)

        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        self._save_btn = ft.FilledTonalButton(
            "保存并校验", icon=ft.Icons.SAVE,
            on_click=lambda e: self._fire(self.on_save, self._editor.value or ""))
        self._debug_btn = ft.OutlinedButton(
            "试跑当前规则", icon=ft.Icons.BUG_REPORT_OUTLINED,
            on_click=lambda e: self._fire(self.on_run_debug, self._editor.value or ""))

        self.controls = [
            self._title,
            ft.Row([self._source_dd, self._field_dd, self._reload_btn],
                   spacing=8, wrap=True),
            self._editor,
            self._analysis_host,
            self._sample,
            ft.Row([self._save_btn, self._debug_btn], spacing=8),
            self._audit_host,
            self._debug_host,
            self._status,
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fire(callback, *args):
        if callback is not None:
            callback(*args)

    def _on_text_changed(self) -> None:
        self.render_highlight(self._editor.value or "")
        self._fire(self.on_changed, self._editor.value or "")

    # ------------------------------------------------------------------ #
    # 渲染接口
    # ------------------------------------------------------------------ #
    def get_text(self) -> str:
        return self._editor.value or ""

    def set_text(self, text: str) -> None:
        """载入待编辑内容（整份 JSON 或单条规则，由控制器决定）。"""
        self._editor.value = text or ""
        self.render_highlight(self._editor.value or "")

    def get_sample(self) -> str:
        return self._sample.value or ""

    def set_sample(self, text: str) -> None:
        self._sample.value = text or ""

    def set_title(self, text: str) -> None:
        self._title.value = text or "规则编辑器"

    def set_source_options(self, options: list[tuple[str, str]],
                           current: str = "") -> None:
        """回填书源下拉（(bookSourceUrl, 显示名) 列表）。

        ``current`` 不在表里就落回没选中：flet 的下拉填一个 options 里没有的键会
        渲染成空白，看着像列表加载坏了。
        """
        items = list(options or [])
        self._source_dd.options = [ft.dropdown.Option(key, text)
                                   for key, text in items]
        keys = [key for key, _ in items]
        if current and current in keys:
            self._source_dd.value = current
        elif not current and keys:
            self._source_dd.value = keys[0]
        else:
            self._source_dd.value = None

    def add_source_option(self, url: str, label: str) -> None:
        """把一条书源**并进**下拉并选中（列表页跳转过来时表里可能还没有它）。"""
        url = str(url or "")
        if not url:
            return
        keys = [option.key for option in self._source_dd.options or []]
        if url not in keys:
            self._source_dd.options = list(self._source_dd.options or []) + [
                ft.dropdown.Option(url, label or url)]
        self._source_dd.value = url

    def set_field_options(self, options: list[tuple[str, str]],
                          current: str = WHOLE_SOURCE) -> None:
        """回填规则字段下拉；第一项固定是「整份书源 JSON」。"""
        items = [(WHOLE_SOURCE, "整份书源 JSON")] + [
            (key, text) for key, text in list(options or []) if key != WHOLE_SOURCE]
        self._field_dd.options = [ft.dropdown.Option(key, text)
                                  for key, text in items]
        keys = {k for k, _ in items}
        self._field_dd.value = current if current in keys else WHOLE_SOURCE

    def render_highlight(self, text: str) -> None:
        """把文本里**识别出来的规则关键字**列成一条带色的摘要（不重复正文）。

        整份 JSON 模式下引号串（键名）不算关键字——否则这条等于把 JSON 再抄一遍，
        正是上一版那块"高亮预览"被诟病的原因。
        """
        as_json = _is_json_document(text)
        seen: set[str] = set()
        picked: list[tuple[str, str]] = []
        counts: dict[str, int] = {}
        for segment, kind in _recognised(text, as_json):
            counts[kind] = counts.get(kind, 0) + 1
            if segment in seen:
                continue
            seen.add(segment)
            picked.append((segment, kind))
        total = sum(counts.values())
        self._analysis_host.visible = total > 0
        if not total:
            self._analysis_summary.value = ""
            self._highlight.spans = [ft.TextSpan("")]
            return
        self._analysis_summary.value = "识别到 {} 处规则关键字：{}".format(
            total, "、".join(f"{_KIND_LABELS.get(k, k)} {n}"
                            for k, n in sorted(counts.items())))
        shown = picked[:self._MAX_TOKENS]
        spans: list[ft.TextSpan] = []
        for n, (segment, kind) in enumerate(shown):
            if n:
                spans.append(ft.TextSpan("  ·  "))
            spans.append(ft.TextSpan(
                segment, style=ft.TextStyle(color=self._COLORS.get(kind))))
        if len(picked) > len(shown):
            spans.append(ft.TextSpan(
                f"  …（另有 {len(picked) - len(shown)} 个不重复关键字）"))
        self._highlight.spans = spans

    def set_audit_report(self, lines: list[str]) -> None:
        """渲染保存即校验的审计提示（缺口列表；空列表表示通过）。"""
        lines = list(lines or [])
        self._audit_host.visible = bool(lines)
        if lines:
            self._audit.value = "审计提示：\n" + "\n".join(lines)
        else:
            self._audit.value = "审计通过：未发现兼容性缺口。"
            self._audit_host.visible = True

    def set_status(self, text: str) -> None:
        self._status.value = text or ""

    def set_debug_result(self, lines: list[str]) -> None:
        """渲染试跑调试（单字段求值）结果；空列表表示无结果/未运行。"""
        lines = list(lines or [])
        self._debug_host.visible = bool(lines)
        self._debug.value = "试跑结果：\n" + "\n".join(lines) if lines else ""
