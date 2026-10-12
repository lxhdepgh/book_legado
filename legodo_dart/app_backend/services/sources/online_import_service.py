"""OnlineImportService：多类型资源导入分发器（§1.16 / 契约 §7）。

职责（红线：本服务**不持有任何规则落库逻辑**，一律委托对应域服务）：

- ``import_from_url``：aiohttp 拉取 URL 文本 → 按 ``kind`` 分发；
- ``import_from_file``：本地已读入的文本（复用旧 import_view 的书源 JSON 导入）；
- **懒路由**：书源即时走 :class:`SourceService`；替换规则 / 词典 / RSS / TTS / 主题 /
  TXT 目录规则目前没有导入落点，登记说明后 ImportFinishedEvent 照发
  （count=0，errors 含说明），不抛异常；
- 书源类型导入前统一过 :class:`CompatAuditService.audit_source`（导入即审，§3.7），
  有兼容性缺口只报 warning，**不阻断导入**；
- 阶段进度发 :class:`ImportProgressEvent`（URL 拉取发不定长的一拍，逐条落库由
  SourceService 发条数进度），界面据此走进度条；
- 完成发布 :class:`ImportFinishedEvent`（kind, count, errors）。

纯 Python + aiohttp；零 flet import。导入遇验证码留 BrowserService 接口占位（M5 接入）。

并行集成说明：SourceService / CompatAuditService 的新外壳可能与本服务并行开发、
尚未落盘——本模块对二者的类引用按 dotted path **懒解析**，缺失时降级为空依赖并
允许构造期/测试期直接注入替身实例，保证本服务独立可测、不因并行未交付而导入失败。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.events import ImportFinishedEvent, ImportProgressEvent
from app.core.lifecycle import Service

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 懒路由表（契约 §7 kind 映射）
# --------------------------------------------------------------------------- #
#: 即时可用：分发到 SourceService（M3 已在新树）。
_KIND_READY = {"book_source"}

#: 尚无落点：只登记说明，不做落库（界面把这些类型置灰，别让用户选了才知道没反应）。
#: 文案是给人看的，不写里程碑编号——原来的"待 M5 就绪"这类字样一路漏到了下拉框里。
_KIND_PENDING = {
    "txt_rules": "TXT 目录规则目前没有独立的存储落点，暂不支持导入",
    "replace_rules": "替换规则请在「配置 → 替换规则」页里添加，导入通道未接",
    "dict_rules": "词典规则请在「配置 → 词典」页里添加，导入通道未接",
    "rss": "RSS 源请在「配置 → 更多工具」的 RSS 页里添加，导入通道未接",
    "tts": "TTS 朗读在阅读器内设置，导入通道未接",
    "theme": "主题在「配置 → 外观」页里选择，导入通道未接",
}

#: 审计报告里需要汇总成 warning 的字段（对齐契约 §6 AuditReport 字段名）。
#: 这份表必须与 ``AuditReport`` 的字段**逐个对齐**：旧表里挂着 7 个"兼容旧 CorpusAudit"
#: 的名字，而整库入口删掉后 ``getattr(report, name)`` 对它们永远是 None（白写），
#: 真实字段 ``rule_syntax_errors`` 反倒没列进来——选择器语法错在导入面一条 warning 都不出。
_AUDIT_WARNING_FIELDS = (
    "missing_java_apis", "missing_object_apis", "unknown_fields",
    "unsupported_pseudos", "js_syntax_errors", "regex_errors",
    "rule_syntax_errors",
    # 审计没跑完（子进程被打穿）也要出一条：漏了它，"没审"在导入面看起来就是"干净"。
    "not_audited",
)


@dataclass
class ImportResult:
    """一次导入的结果快照（不可变语义，视图据此渲染）。"""

    kind: str
    count: int = 0
    errors: list = field(default_factory=list)      # 阻断条目/致命错误
    warnings: list = field(default_factory=list)    # 审计缺口/占位提示（不阻断）
    placeholder: bool = False                        # 是否走了"未就绪"占位


def preview_book_source_text(text: str) -> dict:
    """导入前给界面看的文件预览：总条数 / 分组分布 / 缺 url 的坏条目计数。

    旧单体导入页选完文件先显示这张卡，用户看清"要导多少个、都是哪些分组"、
    选追加还是替换，再点确认；分层重构后整块没了，变成选完文件直接落库。
    纯解析、不落库、不发事件，所以是模块函数（控制器直接调用，不经引擎代理）。

    Returns:
        ``{"total": int, "valid": int, "broken": int,
           "groups": [(组名, 条数), ...]（按条数降序）, "error": str}``
        —— ``error`` 非空表示文件根本读不了（JSON 语法/顶层类型不对），此时其余字段为 0。
    """
    preview = {"total": 0, "valid": 0, "broken": 0, "groups": [], "error": ""}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        preview["error"] = f"JSON 解析失败：{exc}"
        return preview
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        preview["error"] = "书源 JSON 顶层应为数组或对象"
        return preview
    groups: dict[str, int] = {}
    valid = broken = 0
    for item in data:
        if not isinstance(item, dict) or not str(
                item.get("bookSourceUrl") or "").strip():
            broken += 1
            continue
        valid += 1
        group = str(item.get("bookSourceGroup") or "未分组") or "未分组"
        groups[group] = groups.get(group, 0) + 1
    preview["total"] = len(data)
    preview["valid"] = valid
    preview["broken"] = broken
    preview["groups"] = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    if valid == 0:
        # 一条都导不进来（典型：把 TXT 目录规则/别的 JSON 当成书源选了）：
        # 直接说清楚，别给一张"可导入 0 条"的预览卡让用户再点一次确认。
        preview["error"] = (f"这 {len(data)} 项里没有带 bookSourceUrl 的书源，"
                            f"是不是选错了文件？")
        preview["groups"] = []
    return preview


def _resolve_class(dotted: str) -> type | None:
    """按 dotted path 懒解析类；目标模块/类未就绪（并行未交付）时返回 None。"""
    try:
        module_path, class_name = dotted.rsplit(".", 1)
        return getattr(importlib.import_module(module_path), class_name)
    except Exception:  # 并行线未落盘/符号缺失：降级，不阻断本服务导入
        return None


def _topology_dependencies() -> tuple[type, ...]:
    """构造期解析拓扑依赖；兄弟服务类缺失时退化为空（测试/并行期友好）。"""
    deps = []
    for dotted in (
        "app.services.sources.source_service.SourceService",
        "app.services.sources.compat_audit_service.CompatAuditService",
        "app.services.engine.rule_engine_service.RuleEngineService",
    ):
        resolved = _resolve_class(dotted)
        if resolved is not None:
            deps.append(resolved)
    return tuple(deps)


# 异步拉取器签名：(url, headers) -> str（响应文本）。测试注入本地文件读取替身。
HttpFetcher = Callable[[str, dict], "asyncio.Future | Any"]


class OnlineImportService(Service):
    """多类型资源在线/本地导入分发器（契约 §7 全部方法）。"""

    name = "OnlineImportService"
    dependencies: tuple[type, ...] = _topology_dependencies()

    def __init__(self, source_service=None, audit_service=None,
                 http_fetcher: HttpFetcher | None = None) -> None:
        super().__init__()
        # 构造期注入优先（测试替身 / 装配处桥接）；缺省在 on_start 经 hub 解析。
        self._source_override = source_service
        self._audit_override = audit_service
        self._fetcher = http_fetcher
        # 构造注入立即生效，使 import_from_* 在未跑 on_start 时也可用（测试/离线）。
        self._source = source_service
        self._audit = audit_service

    # ------------------------------------------------------------------ #
    # 生命周期：解析协作服务（兄弟类可能尚未注册，容忍 None）
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        self._source = self._resolve("app.services.sources.source_service.SourceService",
                                     self._source_override)
        self._audit = self._resolve("app.services.sources.compat_audit_service.CompatAuditService",
                                    self._audit_override)
        if self._source is None:
            logger.info("SourceService 尚未就绪，书源导入进入降级占位")

    def _resolve(self, dotted: str, override):
        """优先构造注入；否则经 hub.require 取原始实例；都没有返回 None。"""
        if override is not None:
            return override
        cls = _resolve_class(dotted)
        if cls is None or self.hub is None:
            return None
        try:
            return self.hub.require(cls)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 对外主入口（契约 §7）
    # ------------------------------------------------------------------ #
    async def import_from_url(self, url: str, kind: str,
                              headers: dict | None = None,
                              mode: str = "append") -> ImportResult:
        """从 URL 拉取资源文本并按 kind 分发导入。

        网络拉取失败/HTTP 非 2xx 记 error 并仍发 ImportFinishedEvent；
        验证码场景留 BrowserService 占位（M5）。
        """
        result = ImportResult(kind=kind)
        self._emit_progress(kind, "拉取在线文件")
        try:
            text = await self._fetch(url, headers or {})
        except Exception as exc:  # 网络错误不抛穿，记为一条 error
            result.errors.append(f"拉取失败：{exc}")
            logger.warning("在线导入拉取失败 %s (%s): %s", url, kind, exc)
            self._emit(result)
            return result
        return await self._dispatch(text, kind, origin=url, mode=mode)

    async def import_from_file(self, text: str, kind: str,
                               mode: str = "append") -> ImportResult:
        """本地文件文本导入（复用旧 import_view 书源 JSON 导入，扩展 kind）。

        ``mode`` 见 :meth:`SourceService.import_sources_json`：append 追加 / replace
        整表替换（替换的确认对话框在界面侧把门，服务只管执行）。
        """
        result = await self._dispatch(text, kind, origin="<local>", mode=mode)
        return result

    # ------------------------------------------------------------------ #
    # 分发核心
    # ------------------------------------------------------------------ #
    async def _dispatch(self, text: str, kind: str, origin: str,
                        mode: str = "append") -> ImportResult:
        """按 kind 路由：即时可用类型走域服务，其余登记说明。"""
        if kind in _KIND_PENDING:
            result = ImportResult(kind=kind, count=0, placeholder=True,
                                  warnings=[_KIND_PENDING[kind]])
            self._emit(result)
            return result

        if kind not in _KIND_READY:
            result = ImportResult(
                kind=kind, errors=[f"未知资源类型：{kind!r}"])
            self._emit(result)
            return result

        # 书源 → SourceService（落库一律委托，本服务不写盘）
        result = ImportResult(kind=kind)
        sources = self._parse_source_text(text, result)
        if result.errors:
            self._emit(result)
            return result

        # 导入即审（§3.7 触发点①）：逐条审计，缺口报 warning 不阻断。
        # 这一遍是本门面的职责（warning 要进 ImportResult 给界面看），所以下面落库时
        # 明确告诉 SourceService 别再审第二遍——双份审计不只白烧 CPU，还会往
        # audit_log 塞双行。审计服务没就绪时反过来：让落库层自己试一次。
        audited_here = kind == "book_source" and self._audit is not None
        if audited_here:
            result.warnings.extend(await self._audit_sources(sources))

        if self._source is None:
            result.errors.append("SourceService 尚未就绪，本次导入未落库")
            self._emit(result)
            return result

        try:
            count, errors = await self._source.import_sources_json(
                text, mode, audit=not audited_here)
            result.count = int(count or 0)
            result.errors.extend(str(e) for e in (errors or []))
        except Exception as exc:
            logger.exception("书源导入落库失败")
            result.errors.append(f"导入落库失败：{exc}")
        self._emit(result)
        return result

    # ------------------------------------------------------------------ #
    # 工具：JSON 文本拆分 / 审计汇总（只做传输层解析，不落库）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_source_text(text: str, result: ImportResult) -> list[dict]:
        """把 Legado 书源 JSON 文本拆成单条 dict（顶层数组或单对象均容忍）。"""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            result.errors.append(f"JSON 解析失败：{exc}")
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            result.errors.append("书源 JSON 顶层应为数组或对象")
            return []
        sources = [item for item in data if isinstance(item, dict)]
        if not sources:
            result.errors.append("书源 JSON 中没有有效对象")
        return sources

    async def _audit_sources(self, sources: list[dict]) -> list[str]:
        """逐条过 CompatAuditService.audit_source，汇总兼容性缺口为 warning。"""
        warnings: list[str] = []
        for index, source in enumerate(sources):
            name = source.get("bookSourceName") or source.get("bookSourceUrl") or f"#{index + 1}"
            try:
                report = await self._audit.audit_source(source)
            except Exception as exc:  # 审计失败不阻断导入，只记一条
                warnings.append(f"《{name}》审计失败：{exc}")
                continue
            for field_name in _AUDIT_WARNING_FIELDS:
                bucket = getattr(report, field_name, None)
                if not bucket:
                    continue
                if isinstance(bucket, (list, tuple, set)):
                    items = [str(x) for x in bucket if x]
                else:
                    items = [str(bucket)]
                if items:
                    shown = "、".join(items[:3])
                    more = f" 等{len(items)}项" if len(items) > 3 else ""
                    warnings.append(f"《{name}》{field_name}: {shown}{more}")
        return warnings

    # ------------------------------------------------------------------ #
    # 网络拉取（默认 aiohttp；测试可注入 fetcher）
    # ------------------------------------------------------------------ #
    async def _fetch(self, url: str, headers: dict) -> str:
        if self._fetcher is not None:
            result = self._fetcher(url, headers)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                return await result
            return str(result)
        return await self._aiohttp_fetch(url, headers)

    @staticmethod
    async def _aiohttp_fetch(url: str, headers: dict) -> str:
        """默认拉取器：aiohttp GET（验证码场景 M5 交 BrowserService 渲染）。"""
        import aiohttp  # 懒导入：测试替身注入时无需网络栈

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.text()

    # ------------------------------------------------------------------ #
    def _emit(self, result: ImportResult) -> None:
        """发布 ImportFinishedEvent（kind, count, errors 汇总）。"""
        if self.bus is None:
            return
        payload_errors = list(result.errors)
        payload_errors.extend(result.warnings)
        self.bus.publish(ImportFinishedEvent(
            kind=result.kind, count=result.count, errors=payload_errors))

    def _emit_progress(self, kind: str, phase: str,
                       done: int = 0, total: int = 0) -> None:
        """发布阶段进度（total=0 表示这一拍给不出条数，界面走不定长进度条）。"""
        if self.bus is None:
            return
        self.bus.publish(ImportProgressEvent(
            kind=kind, phase=phase, done=done, total=total))
