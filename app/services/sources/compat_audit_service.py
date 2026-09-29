"""CompatAuditService：JS/Java API 支持面静态审计的服务外壳（M3，契约 §6 / 设计 §3.7）。

外壳化 ``app/services/compat_audit.py``（M1 已迁，静态审计能力全保留）：

- :meth:`audit_source` 对**单个书源**做静态审计，复用 compat_audit 的既有清单与
  校验函数（``SUPPORTED_JAVA_APIS`` / ``SUPPORTED_OBJECT_APIS`` / ``KNOWN_RULE_FIELDS``
  / ``SUPPORTED_JSOUP_PSEUDOS`` / ``scan_missing_java_packages`` / ``_validate_*`` /
  ``_script_blocks``），**不改动** compat_audit.py 一行；
- 三个触发点：①导入（OnlineImportService / SourceService.import_sources_json）
  ②编辑保存（SourceService.save_source）③检测流水线每源 asyncio.to_thread 复跑
  （SourceCheckService 内调用）；
- 审计记录落 ``audit_log`` 表（schema v3：id/source_id/missing_apis(JSON)/ts）——
  这是外壳新增职责；静态审计本身零改动。

红线：本服务零 flet import；静态审计失败不抛出到调用方（导入即审报 warning 不阻断）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.core.lifecycle import Service
from app.core.storage import StorageService
from app.services import compat_audit as ca

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditReport:
    """单个书源的静态审计报告（契约 §6 数据类）。

    六个清单字段均为字符串列表（不可变语义：接收方不得回写）；
    ``suggestions`` 为给用户看的处置建议。空清单即"该项无缺口"。
    """

    source_id: str
    missing_java_apis: list[str] = field(default_factory=list)
    missing_object_apis: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    unsupported_pseudos: list[str] = field(default_factory=list)
    js_syntax_errors: list[str] = field(default_factory=list)
    regex_errors: list[str] = field(default_factory=list)
    # 规则（选择器/伪类）静态校验错误：与 regex_errors 分列，便于视图区分提示。
    rule_syntax_errors: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """是否完全没有缺口（无错误、无未支持 API、无未知字段）。"""
        return not (self.missing_java_apis or self.missing_object_apis
                    or self.unknown_fields or self.unsupported_pseudos
                    or self.js_syntax_errors or self.regex_errors
                    or self.rule_syntax_errors)


def _audit_one(data: dict) -> AuditReport:
    """对单个书源 dict 跑静态审计（纯 CPU，设计为 asyncio.to_thread 调用）。

    逻辑逐条镜像 ``compat_audit.audit_source_corpus`` 的单源切片——只是把语料级
    遍历收敛到一个书源，**复用同一组校验函数与清单**，不复制判定规则本身。
    """
    source_id = str(data.get("bookSourceUrl") or "")
    corpus_text = json.dumps(data, ensure_ascii=False)

    # —— 未知规则字段（§3.7：KNOWN_RULE_FIELDS 比对）——
    used_fields: set[str] = set()
    for section in ("ruleSearch", "ruleExplore", "ruleBookInfo",
                    "ruleToc", "ruleContent", "ruleReview"):
        sec = data.get(section)
        if isinstance(sec, dict):
            used_fields.update(sec)
    unknown_fields = sorted(used_fields - ca.KNOWN_RULE_FIELDS)

    # —— 缺失的 Java API 调用（java.X( 与 Packages.<path> 两路合并）——
    missing_packages = ca.scan_missing_java_packages([corpus_text])
    used_java = set(re.findall(
        r"(?<!Packages\.)(?<![\w$])java\.([A-Za-z_$][\w$]*)\s*\(", corpus_text))
    missing_apis = sorted(used_java - ca.SUPPORTED_JAVA_APIS)
    # 契约字段 missing_java_apis 同时承载 Packages 路径缺口与 java.X 缺口。
    missing_java_apis = sorted(set(missing_apis) | set(missing_packages))

    # —— 缺失的对象方法（source./book./chapter./cache./cookie.）——
    unsupported_objects: set[str] = set()
    for object_name, supported in ca.SUPPORTED_OBJECT_APIS.items():
        called = set(re.findall(
            rf"(?<![\w$]){object_name}\.([A-Za-z_$][\w$]*)\s*\(", corpus_text))
        unsupported_objects.update(
            f"{object_name}.{name}" for name in called - supported)
    unsupported_objects.discard("chapter.substring")  # 语料特例，同 corpus 版
    missing_object_apis = sorted(unsupported_objects)

    # —— 未支持的 jsoup 伪类 ——
    used_pseudos: set[str] = set()
    for rule in ca._rule_strings(data):
        plain = re.sub(r"<js>[\s\S]*?</js>", "", rule, flags=re.I)
        plain = re.sub(r"\{\{[\s\S]*?\}\}", "", plain)
        if plain.lstrip().startswith(("@js:", "//javascript:")):
            continue
        plain = re.split(r"@js:", plain, maxsplit=1, flags=re.I)[0]
        used_pseudos.update(re.findall(r":([A-Za-z-]+)\s*\(", plain))
    unsupported_pseudos = sorted(used_pseudos - ca.SUPPORTED_JSOUP_PSEUDOS)

    # —— 规则（选择器）静态校验 ——
    rule_errors: list[str] = []
    for field_path, rule_value in ca._rule_entries(data):
        try:
            ca._validate_rule_syntax(rule_value)
        except Exception as exc:  # 校验函数风格不一，统一捕获记一条
            rule_errors.append(f"{field_path}: {str(exc).splitlines()[0]}")

    # —— 正则字段静态校验 ——
    regex_errors: list[str] = []
    for field_path, value in ca._rule_entries_by_name(data, ca.REGEX_FIELD_NAMES):
        if not value.strip():
            continue
        try:
            ca._validate_regex_field(field_path, value)
        except Exception as exc:
            regex_errors.append(f"{field_path}: {str(exc).splitlines()[0]}")

    # —— JS 块预编译查语法（quickjs 不在则跳过，与 corpus 版降级一致）——
    js_errors: list[str] = []
    try:
        import quickjs
        from app.services.engine.legado_js import LegadoJsRuntime
    except ImportError:
        quickjs = None  # type: ignore
        LegadoJsRuntime = None  # type: ignore
    if quickjs is not None:
        # 每次**新建**一条 Context 是刻意的：崩溃按 runtime 算，不按新建算
        # （实测面见 legado_js._QUICKJS_LOCK 上方注释）。提到模块级共用一条反而是
        # 那个会带走进程的形状，护栏 QuickJsRuntimeOverlapTests 盯着这两件事。
        # 换成"收进 dispatcher/锁"也不划算：600 段预编译 108 ms → 541 ms（5.0×），
        # 换不来任何安全。
        for field_path, script in ca._script_blocks(data, 0):
            prepared = re.sub(r"\{\{[\s\S]*?\}\}", "0", script).split("##", 1)[0]
            prepared = LegadoJsRuntime._clean_script(prepared)  # type: ignore[union-attr]
            try:
                quickjs.Context().eval(f"(function(){{\n{prepared}\n}})")
            except Exception as exc:
                js_errors.append(f"{field_path}: {str(exc).splitlines()[0]}")

    suggestions = _suggest(
        unknown_fields, unsupported_pseudos, missing_java_apis,
        missing_object_apis, js_errors, regex_errors)
    return AuditReport(
        source_id=source_id,
        missing_java_apis=missing_java_apis,
        missing_object_apis=missing_object_apis,
        unknown_fields=unknown_fields,
        unsupported_pseudos=unsupported_pseudos,
        js_syntax_errors=js_errors,
        regex_errors=regex_errors,
        rule_syntax_errors=rule_errors,
        suggestions=suggestions,
    )


def _suggest(*buckets) -> list[str]:
    """按缺口类别给一句处置建议（导入即审随 warning 报出，不阻断）。"""
    suggestions: list[str] = []
    if buckets[0] or buckets[1] or buckets[2]:
        suggestions.append("用到的 Java/对象 API 在本运行时未实现，对应脚本阶段运行期会失败")
    if buckets[3]:
        suggestions.append("含未支持的 jsoup 伪类，相关选择器可能解析为空")
    if buckets[4]:
        suggestions.append("存在 JS 语法错误，脚本阶段无法预编译")
    if buckets[5]:
        suggestions.append("存在正则语法错误，替换/匹配规则可能不生效")
    return suggestions


class CompatAuditService(Service):
    """单源静态审计 + audit_log 落库（§3.7）。"""

    name = "CompatAuditService"
    #: 拓扑依赖：StorageService（audit_log 表）；SourceService 经 on_start 懒解析。
    dependencies: tuple[type, ...] = (StorageService,)

    def __init__(self, storage=None, source_service=None) -> None:
        super().__init__()
        self._storage = storage
        self._source_service = source_service
        self._lock = threading.Lock()

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            from app.core.storage import StorageService
            self._storage = self.hub.require(StorageService)
        if self._source_service is None and self.hub is not None:
            self._source_service = self._resolve(
                "app.services.sources.source_service", "SourceService")
        logger.info("CompatAuditService 已启动")

    async def on_stop(self) -> None:
        logger.info("CompatAuditService 已停止")

    # ------------------------------------------------------------------ #
    # 对外入口（契约 §6）
    # ------------------------------------------------------------------ #
    async def audit_source(self, source_json) -> AuditReport:
        """审计单个书源并落 audit_log；返回纯数据报告（不抛到导入方）。

        ``source_json`` 可为 dict 或 BookSource（带 to_dict）。审计是纯 CPU 活，
        放线程池跑，别卡住事件循环（§3.7 触发点③的 asyncio.to_thread 语义）。
        """
        data = (source_json.to_dict()
                if hasattr(source_json, "to_dict") else dict(source_json))
        report = await asyncio.to_thread(_audit_one, data)
        try:
            self._persist(report)
        except Exception:  # 落库失败不影响审计结论返回
            logger.warning("audit_log 落库失败，已忽略", exc_info=True)
        return report

    # ------------------------------------------------------------------ #
    # audit_log 落库（外壳新增职责）
    # ------------------------------------------------------------------ #
    def _conn(self):
        return self._storage.db if self._storage is not None else None

    def _persist(self, report: AuditReport) -> None:
        conn = self._conn()
        if conn is None:
            return
        payload = {
            "missing_java_apis": report.missing_java_apis,
            "missing_object_apis": report.missing_object_apis,
            "unknown_fields": report.unknown_fields,
            "unsupported_pseudos": report.unsupported_pseudos,
            "js_syntax_errors": report.js_syntax_errors,
            "regex_errors": report.regex_errors,
        }
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn.execute(
                "INSERT INTO audit_log(source_id, missing_apis, ts) VALUES(?,?,?)",
                (report.source_id,
                 json.dumps(payload, ensure_ascii=False), ts))

    def list_logs(self, source_id: str | None = None, limit: int = 100) -> list[dict]:
        """查询审计历史（视图回溯用；只读）。"""
        conn = self._conn()
        if conn is None:
            return []
        if source_id:
            rows = conn.execute(
                "SELECT id, source_id, missing_apis, ts FROM audit_log "
                "WHERE source_id=? ORDER BY id DESC LIMIT ?",
                (str(source_id), int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, source_id, missing_apis, ts FROM audit_log "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [
            {"id": int(r[0]), "source_id": r[1],
             "missing_apis": json.loads(r[2]) if r[2] else {},
             "ts": r[3]}
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve(module_path: str, class_name: str):
        """经 importlib 懒解析并行交付的服务类（未注册/未交付返回 None）。"""
        try:
            import importlib
            mod = importlib.import_module(module_path)
            return getattr(mod, class_name)
        except Exception:
            return None
