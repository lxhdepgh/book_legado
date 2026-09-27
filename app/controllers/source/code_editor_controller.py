"""CodeEditorController：规则编辑器的选源 / 选字段 / 保存即校验编排（§1.6 / 契约 §9）。

三条链路：

- **载入**：进页面先经 ``SourceService.list_sources`` 填「书源」下拉（不等书源列表页
  跳过来才有内容——以前直接进这页签就是块空编辑器）；选中一条经 ``load_source``
  取回 dict 灌进编辑器；
- **编辑单位**：字段下拉选「整份书源 JSON」时编辑整份文本；选某条规则字段（如
  ``ruleSearch.name``）时只编辑那一条，保存时由 :meth:`_merged` 合并回原 dict。
  切字段前先把当前文本暂存进 dict，不覆盖 ``_current`` 的其它字段；
- **保存即校验**：SourceService.save_source（校验+落库）→ CompatAuditService
  .audit_source 审计提示渲染进视图审计区（§3.7 触发点②）。

试跑调试（M5）：规则引擎单字段求值，样本是视图里粘进来的**示例正文**——本页不联网
取样本（记忆 legado-live-scan-av-block），没给样本就不调引擎（回一句"示例为空"的
按钮等于坏按钮）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging

from app.controllers.base import BaseController

logger = logging.getLogger(__name__)

try:
    from app.services.sources.source_service import SourceService
except Exception:  # pragma: no cover
    class SourceService:  # type: ignore[no-redef]
        pass

try:
    from app.services.sources.compat_audit_service import CompatAuditService
except Exception:  # pragma: no cover
    class CompatAuditService:  # type: ignore[no-redef]
        pass

try:
    from app.services.engine.rule_engine_service import RuleEngineService
except Exception:  # pragma: no cover
    class RuleEngineService:  # type: ignore[no-redef]
        pass

#: 规则组 → 页面上的人话名字（顺序即下拉自上而下的顺序）。
_GROUP_LABELS = {
    "ruleSearch": "搜索",
    "ruleExplore": "发现",
    "ruleBookInfo": "详情",
    "ruleToc": "目录",
    "ruleContent": "正文",
    "ruleReview": "书评",
}

#: 常见规则字段 → 中文名（没列出的按原键名显示，不猜语义）。
_FIELD_LABELS = {
    "bookList": "书单", "name": "书名", "bookUrl": "书源地址", "author": "作者",
    "intro": "简介", "kind": "分类", "coverUrl": "封面", "lastChapter": "最新章",
    "updateTime": "更新时间", "wordCount": "字数", "tocUrl": "目录页",
    "chapterList": "章节列表", "chapterName": "章节名", "chapterUrl": "章节链接",
    "nextTocUrl": "目录翻页", "content": "正文", "title": "标题",
    "nextContentUrl": "正文翻页", "subContent": "子正文",
    "checkKeyWord": "检测关键字", "init": "初始化", "reviewUrl": "书评页",
}


class CodeEditorController(BaseController):
    """规则编辑器：选源 / 选字段 / 载入 / 保存即校验 / 单字段试跑。"""

    destination = "sources.editor"

    def on_attach(self) -> None:
        from app.views.source.code_editor_view import WHOLE_SOURCE
        self._whole = WHOLE_SOURCE
        self._source = self._safe_service(SourceService)
        self._audit = self._safe_service(CompatAuditService)
        self._rule_engine = self._safe_service(RuleEngineService)
        self._current: dict | None = None   # 当前编辑的书源 dict
        self._field = WHOLE_SOURCE          # 当前编辑目标（整份 JSON 或字段键）
        # 整份 JSON 模式下试跑取哪条字段规则（历史默认，保留可改）。
        self._debug_field = "bookNameRule"

        self._view.on_save = self.save
        self._view.on_run_debug = self.run_debug
        self._view.on_changed = self.mark_dirty
        self._view.on_source_picked = self.select_source
        self._view.on_field_picked = self.select_field
        self._view.on_reload_sources = self.refresh_sources
        self.refresh_sources()

    def _safe_service(self, stype):
        try:
            return self.service(stype)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 书源下拉
    # ------------------------------------------------------------------ #
    def refresh_sources(self, _=None) -> None:
        """拉一遍书源列表填下拉。没有 SourceService（精简装配）就直说。"""
        if self._source is None:
            self._view.set_status("SourceService 未就绪，无法列出书源")
            return
        future = self.ui_call(self._load_sources())
        future.add_done_callback(self.on_done(self._log_only))

    async def _load_sources(self) -> None:
        records = await self._source.list_sources() or []
        options = []
        for record in records:
            url = self._field_of(record, "bookSourceUrl")
            name = self._field_of(record, "bookSourceName") or url
            if url:
                options.append((url, name))
        if self._view is None:            # 拉列表的工夫里切了页
            return
        self._view.set_source_options(
            options, current=self._current_url())
        if not options:
            self._view.set_status("还没有书源，先到「导入」页导入一份")
        else:
            self._view.set_status(f"共 {len(options)} 个书源，选一个来编辑")
        self.flush()

    @staticmethod
    def _field_of(record, key: str) -> str:
        if isinstance(record, dict):
            return str(record.get(key) or "")
        return str(getattr(record, key, "") or "")

    def _current_url(self) -> str:
        return str((self._current or {}).get("bookSourceUrl") or "")

    def select_source(self, url: str = "", _=None) -> None:
        """下拉选了某个书源：取回整条记录并载入编辑器。"""
        url = str(url or "").strip()
        if not url or url == self._current_url():
            return
        if self._source is None:
            self._view.set_status("SourceService 未就绪，未载入")
            return
        future = self.ui_call(self._open_source(url))
        future.add_done_callback(self.on_done(self._log_only))

    async def _open_source(self, url: str) -> None:
        record = await self._source.load_source(url)
        if record is None:
            if self._view is not None:
                self._view.set_status(f"没找到书源 {url}")
            return
        if self._view is None:            # 取记录的工夫里切了页：不碰控件
            return
        to_dict = getattr(record, "to_dict", None)
        self.load_source(to_dict() if callable(to_dict) else dict(record))

    @staticmethod
    def _log_only(future) -> None:
        """回调只负责把异常露出来（``on_done`` 已补 flush）。"""
        future.result()

    # ------------------------------------------------------------------ #
    # 载入 / 编辑字段
    # ------------------------------------------------------------------ #
    def load_source(self, record: dict) -> None:
        """载入待编辑书源：整条 JSON 进编辑器，字段下拉按这份 JSON 里有什么来列。"""
        record = dict(record or {})
        self._current = record
        self._field = self._whole
        name = record.get("bookSourceName") or record.get("bookSourceUrl") or ""
        self._view.set_title(f"编辑规则：{name}")
        # 只把这一条**并进**下拉（列表可能已经拉好了），不能整表换成一条——
        # 从书源列表跳过来时把用户能选的其它源抹掉，切源就得退回上一页。
        self._view.add_source_option(str(record.get("bookSourceUrl") or ""),
                                     str(name or record.get("bookSourceUrl") or ""))
        self._view.set_field_options(self._field_options(record), self._field)
        self._show_field()
        self._view.set_status("改完点「保存并校验」")

    @staticmethod
    def _field_options(record: dict) -> list[tuple[str, str]]:
        """列出这份书源里**实际存在**的规则字段（键序按组固定，组内按名排序）。"""
        options: list[tuple[str, str]] = []
        for group, group_label in _GROUP_LABELS.items():
            rules = record.get(group)
            if not isinstance(rules, dict):
                continue
            for field in sorted(rules):
                if not isinstance(rules[field], str):
                    continue
                options.append((f"{group}.{field}",
                                f"{group_label} · {_FIELD_LABELS.get(field, field)}"))
        return options

    def select_field(self, key: str = "", _=None) -> None:
        """换编辑目标：先把当前文本暂存进内存里那份 dict，再显示新目标。

        暂存这一跳不写库也不调校验：切字段不该留下"半个字没保存就丢了"的坑。
        """
        key = str(key or "").strip()
        if not key or key == self._field:
            return
        self._stash_text()
        # 用事件递来的键，不回读下拉的 .value：控件状态什么时候落定不归控制器管，
        # 回读拿到旧值就是"点了字段没换过去"。
        self._field = key
        self._show_field()

    def _stash_text(self) -> None:
        """把编辑框里的文本写回 ``_current`` 对应字段（只动内存）。"""
        if self._field == self._whole or self._current is None:
            return
        group, _, field = self._field.partition(".")
        if not group or not field:
            return
        rules = self._current.get(group)
        if isinstance(rules, dict):
            rules[field] = self._view.get_text()

    def _show_field(self) -> None:
        if self._field == self._whole:
            record = self._current or {}
            self._view.set_text(json.dumps(record, ensure_ascii=False, indent=2)
                                if record else "")
        else:
            group, _, field = self._field.partition(".")
            rules = (self._current or {}).get(group)
            value = rules.get(field) if isinstance(rules, dict) else ""
            self._view.set_text(str(value or ""))
            self._view.set_status(f"只编辑 {self._field}；保存会合并回原书源")

    def _merged(self, text: str) -> dict | str:
        """把编辑框内容变成待保存的书源 dict；返回 (dict, 错误说明)。

        字段模式下没有 ``_current``（还没选源就点保存）时**明确报错**，不能凭空
        造一条只有这一个字段的书源去覆盖库里那条。
        """
        if self._field == self._whole:
            try:
                source = json.loads(text)
            except json.JSONDecodeError as exc:
                return None, f"JSON 语法错误：{exc}"
            if not isinstance(source, dict):
                return None, "书源规则应为 JSON 对象"
            return source, ""
        if not isinstance(self._current, dict):
            return None, "先在上方选一个书源，再编辑这条规则"
        merged = copy.deepcopy(self._current)
        group, _, field = self._field.partition(".")
        merged.setdefault(group, {})[field] = str(text or "").strip()
        return merged, ""

    def mark_dirty(self, text: str = "", _=None) -> None:
        """编辑即提示未保存：避免改完直接切页丢改动。"""
        if self._view is not None:
            scope = "整份书源 JSON" if self._field == self._whole else self._field
            self._view.set_status(f"有未保存修改（{scope}）")

    # ------------------------------------------------------------------ #
    def save(self, text: str) -> None:
        """保存即校验：合并成整条书源 → 落库 → 审计提示。"""
        source, error = self._merged(text)
        if source is None:
            self._view.set_audit_report([error])
            return
        if self._source is None:
            self._view.set_status("SourceService 未就绪，未保存")
            return
        future = self.ui_call(self._source.save_source(source))
        future.add_done_callback(self.on_done(lambda f: self._after_save(source, f)))

    def _after_save(self, source: dict, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            future.result()
        except Exception as exc:
            self._view.set_audit_report([f"保存失败：{exc}"])
            return
        self._current = source          # 库里现在就是这份，字段模式接着编辑不错位
        self._view.set_status("已保存，正在审计…")
        if self._audit is None:
            self._view.set_audit_report([])
            return
        audit_future = self.ui_call(self._audit.audit_source(source))
        audit_future.add_done_callback(self.on_done(self._render_audit))

    def _render_audit(self, future) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        try:
            report = future.result()
        except Exception as exc:
            self._view.set_audit_report([f"审计失败：{exc}"])
            return
        lines = []
        for field_name in ("missing_java_apis", "missing_object_apis",
                           "unknown_fields", "unsupported_pseudos",
                           "js_syntax_errors", "regex_errors"):
            bucket = getattr(report, field_name, None)
            if bucket:
                items = bucket if isinstance(bucket, (list, tuple)) else [bucket]
                lines.append(f"{field_name}: " + "、".join(str(x) for x in items[:5]))
        self._view.set_audit_report(lines)

    # ------------------------------------------------------------------ #
    # 试跑调试：单字段求值（样本由用户粘进视图，本页不联网）
    # ------------------------------------------------------------------ #
    def run_debug(self, text: str) -> None:
        """把当前编辑的规则（或整份 JSON 里的目标字段）对示例正文求值。"""
        if self._field == self._whole:
            try:
                source = json.loads(text)
            except json.JSONDecodeError as exc:
                self._view.set_debug_result([f"JSON 语法错误：{exc}"])
                return
            if not isinstance(source, dict):
                self._view.set_debug_result(["书源规则应为 JSON 对象"])
                return
            rule = self._pick_rule(source, self._debug_field)
            if not rule:
                self._view.set_debug_result([
                    f"整份 JSON 模式下试跑的是 {self._debug_field}，这份书源里没有；"
                    "在「规则字段」里选一条规则再试。"])
                return
        else:
            rule = str(text or "").strip()
            if not rule:
                self._view.set_debug_result(["这条规则是空的，没什么可试跑的。"])
                return
        sample = str(self._view.get_sample() or "").strip()
        if not sample:
            self._view.set_debug_result([f"规则：{rule}",
                                        "先在「示例正文」里粘一段网页源码或 JSON。"
                                        "本页不联网抓样本（不自动打开书源站点）。"])
            return
        if self._rule_engine is None:
            self._view.set_debug_result([
                "RuleEngineService 未就绪（规则已解析）", f"规则：{rule}"])
            return
        future = self.ui_call(self._eval_field(rule, sample))
        future.add_done_callback(self.on_done(lambda f: self._render_debug(f, rule)))

    @staticmethod
    def _pick_rule(source: dict, field: str) -> str:
        """从书源 dict 取目标字段规则：先顶层，再 Legado 常见嵌套容器。"""
        direct = source.get(field)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for container in ("ruleSearch", "ruleExplore", "ruleBookInfo",
                          "ruleToc", "ruleContent", "ruleReview"):
            sub = source.get(container)
            if isinstance(sub, dict):
                value = sub.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _kind_of(rule: str, sample: str) -> str:
        """引擎按哪种解析器求值：``$`` 开头是 JSONPath，样本像 JSON 也算。"""
        if str(rule or "").lstrip().startswith("$"):
            return "json"
        return "json" if str(sample or "").lstrip()[:1] in {"{", "["} else "html"

    async def _eval_field(self, rule: str, sample: str) -> list[str]:
        """经 RuleEngineService 句柄做单字段求值；回落直接调引擎模块纯函数。"""
        kind = self._kind_of(rule, sample)
        method = (getattr(self._rule_engine, "extract_values", None)
                  or getattr(self._rule_engine, "eval_field", None))
        if method is not None:
            result = method(rule, sample, kind)
            if asyncio.iscoroutine(result):
                result = await result
            return list(result or [])
        from app.services.engine import rule_engine
        nodes, _ = rule_engine.select_nodes(sample, kind, "")
        return rule_engine.extract_values(nodes, rule, kind)

    def _render_debug(self, future, rule: str) -> None:
        if self._view is None:  # 视图已 detach：在途结果丢弃
            return
        lines = [f"规则：{rule}"]
        try:
            values = future.result() or []
        except Exception as exc:
            lines.append(f"求值失败：{exc}")
            self._view.set_debug_result(lines)
            return
        if not values:
            lines.append("示例里没取到值：换一段示例正文，或按上面的规则改写选择器。")
        else:
            lines.extend(f"→ {v}" for v in values[:10])
        self._view.set_debug_result(lines)
