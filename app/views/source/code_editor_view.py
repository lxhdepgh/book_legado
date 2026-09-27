"""CodeEditorView：规则代码编辑器（§1.6 / 契约 §9）。

纯 Flet 自绘：**输入框 + 分词高亮层**两层同内容渲染，三端一致，不引入 Web 系
code editor（红线：不拖入第二套 Chromium）。词表来自
:mod:`app.services.engine.legado_rule_parser` 的 Legado 规则方言关键字。

保存即校验：点保存经所属 Controller 调 SourceService.save_source（校验+落库），
再经 CompatAuditService 审计并把缺口提示渲染到下方审计区（§3.7 触发点②）。
试跑调试（M5）：经 CodeEditorController 调 RuleEngineService.extract_values
对选中规则字段做单字段求值，结果渲染到调试区。

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
        kind = match.lastgroup or "plain"
        segments.append((match.group(0), kind))
        pos = match.end()
    if pos < len(text or ""):
        segments.append((text[pos:], "plain"))
    return segments or [("", "plain")]


class CodeEditorView(ft.Column):
    """规则编辑器：标题 + 输入框 + 分词高亮层 + 审计区 + 保存/试跑按钮。"""

    #: 各高亮类别的颜色（深色/浅色主题下均可读的一组中性高亮色）。
    _COLORS = {
        "jsblock": ft.Colors.PURPLE_400,
        "string": ft.Colors.GREEN_700,
        "jsonpath": ft.Colors.CYAN_700,
        "op": ft.Colors.ORANGE_700,
        "prefix": ft.Colors.BLUE_700,
        "plain": None,
    }

    def __init__(self) -> None:
        super().__init__(spacing=8, expand=True)

        # -- 回调槽（Controller 填充） --
        self.on_save = None             # (text: str) -> None
        self.on_run_debug = None        # (text: str) -> None
        self.on_changed = None          # (text: str) -> None

        self._title = ft.Text("规则编辑器", size=20, weight=ft.FontWeight.BOLD)

        # 输入框（编辑态）——输入即重算高亮层
        self._editor = ft.TextField(
            multiline=True, expand=True, min_lines=14, max_lines=24,
            text_size=13, shift_enter=True,
            hint_text="在此编辑 Legado 规则：选择器 @text/@href、$.JSONPath、<js>…</js>、|| 回退…",
            on_change=lambda e: self._on_text_changed())

        # 分词高亮层（与输入框同内容的只读着色渲染，纯展示）
        self._highlight = ft.Text("", selectable=False)
        self._highlight_host = ft.Container(
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=6, padding=8, expand=True,
            content=ft.Column([self._highlight], scroll=ft.ScrollMode.AUTO))

        # 审计结果区（保存即校验：CompatAuditService 提示）
        self._audit = ft.Text("", size=12, color=ft.Colors.GREY_700)
        self._audit_host = ft.Container(visible=False, padding=6,
                                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST)

        # 试跑调试结果区（M5：RuleEngineService.extract_values 单字段求值回显）。
        self._debug = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_700, selectable=True)
        self._debug_host = ft.Container(visible=False, padding=6,
                                         border=ft.Border.all(1, ft.Colors.BLUE_200),
                                         border_radius=6)

        self._status = ft.Text("", size=12, color=ft.Colors.GREY_600)

        self._save_btn = ft.FilledTonalButton(
            "保存并校验", icon=ft.Icons.SAVE,
            on_click=lambda e: self._fire(self.on_save, self._editor.value or ""))
        self._debug_btn = ft.OutlinedButton(
            "试跑调试", icon=ft.Icons.BUG_REPORT_OUTLINED,
            on_click=lambda e: self._fire(self.on_run_debug, self._editor.value or ""))

        self.controls = [
            self._title,
            self._editor,
            ft.Row([ft.Text("语法高亮预览", size=11, color=ft.Colors.GREY_500)]),
            self._highlight_host,
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
        """载入待编辑规则文本（Controller 选源后注入）。"""
        self._editor.value = text or ""
        self.render_highlight(self._editor.value or "")

    def set_title(self, text: str) -> None:
        self._title.value = text or "规则编辑器"

    def render_highlight(self, text: str) -> None:
        """按 tokenize_rule 重建高亮层 spans（输入框文本的着色副本）。"""
        spans = []
        for segment, kind in tokenize_rule(text):
            if not segment:
                continue
            spans.append(ft.TextSpan(
                segment,
                style=ft.TextStyle(color=self._COLORS.get(kind))))
        self._highlight.spans = spans or [ft.TextSpan("")]

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
