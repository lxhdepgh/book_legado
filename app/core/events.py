"""进程内事件定义（§4.3 冻结事件表）。

事件是**不可变数据快照**（frozen dataclass）：禁止在事件里塞可变对象引用，
让接收方改发送方状态（红线 §0.3 第 10 条）。M1 只落地两个事件，
其余 13 个事件随对应业务服务里程碑补齐（命名即约定，不得改名）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AppEvent:
    """所有进程内事件的基类（§9.2）。"""


@dataclass(frozen=True)
class ServiceStateChangedEvent(AppEvent):
    """服务状态迁移事件（§4.3 表第 1 行：service_id, state, error）。

    发布方：Service 基类每次状态迁移时；订阅方：ServiceDashboardView 等。
    ``state`` 取 :class:`~app.core.lifecycle.ServiceState` 的字符串值，
    事件不携带 enum 类型本身，保持纯数据快照。
    """

    service_id: str
    state: str
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SettingsChangedEvent(AppEvent):
    """偏好变更事件（§4.3 表末行：keys）。

    发布方：SettingsService.set()；订阅方：全体关心配置的视图。
    载荷携带键与新旧值，视图自行决定是否全量重读。
    """

    key: str
    value: Any = None
    old_value: Any = None
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# M2 阅读域事件（§4.3 事件表 + 契约 §2；命名即约定，不可变快照）。
# 以下事件随对应阅读域服务里程碑落地：命名/字段顺序以集成契约为准。
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProgressSavedEvent(AppEvent):
    """阅读进度落库事件（§4.3：book_url, chapter_index, position）。

    发布方：ReadingService.save_progress()；订阅方：书架 / 进度卡。
    ``position`` 为 ReadingPosition 的纯数据快照（字典），事件里不塞可变模型引用。
    """

    book_url: str
    chapter_index: int
    position: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BookmarkChangedEvent(AppEvent):
    """书签增删事件（§4.3：book_url, op）。

    发布方：BookmarkService.add/remove；订阅方：阅读器（刷新书签列表/定位）。
    ``op`` 取值 "add" / "remove"。
    """

    book_url: str
    op: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DownloadProgressEvent(AppEvent):
    """整书缓存/下载进度事件（§4.3：task_id, kind, progress, speed）。

    发布方：DownloadService；订阅方：下载中心 / 整书缓存视图。
    ``progress`` 为 0-1；``speed`` 为预留字段（字节/秒），M2 服务层只发原始进度，
    0.5s 节流在 Controller 层做（契约 §7）。
    """

    task_id: str
    kind: str
    progress: float
    speed: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SearchFinishedEvent(AppEvent):
    """找书/换源结果事件（§4.3：search_id, items, finished）。

    发布方：ReadingService.search_books()；订阅方：找书 / 换源视图。
    ``items`` 为 BookItem 字典快照列表（不可变语义：接收方不得回写）。
    ``finished=False`` 是**逐源增量**（SearchModel 每合并一个源推一次），
    ``finished=True`` 表示本轮全部书源已扫完——界面据此决定转圈是否停。
    """

    search_id: str
    items: list
    timestamp: float = field(default_factory=time.time)
    finished: bool = True


@dataclass(frozen=True)
class ReadingSessionChangedEvent(AppEvent):
    """阅读会话切换事件（M2 阅读域内部约定，随实现补入 §4.3 事件表）。

    发布方：ReadingService.open_book()/close_book()；订阅方：阅读器 / 书架。
    载荷携带当前书与章节索引，用于阅读器重建上下文、书架高亮续读。
    """

    book_url: str
    chapter_index: int
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# G3-D3 换源扫描事件（§9 门 G3：换源从进程内回调改走事件流，供跨进程宿主订阅）。
# 命名沿用 §4.3 的两段式：进度一条只带计数，结果一条带整份候选。
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceSwitchProgressEvent(AppEvent):
    """换源联网扫描的逐源进度（§9 G3-D3：switch_id, done, total, failures, starved）。

    发布方：ReadingService.switch_scan()（把 ``switch_source`` 的 ``on_progress``
    转成事件）；订阅方：换源屏 / flet 换源对话框。
    ``switch_id`` 由客户端生成并原样回带（与 ``SearchFinishedEvent`` 同一套关联办法：
    ``mode="job"`` 的受理回包里没有任务号，晚到的事件只能靠它认本轮）。
    ``failures`` 与 ``starved`` 必须**分两格**：后者是"本机忙不过来"——那些源被掐掉
    是我们自己排队挤的，不是站点的错（见 ``reading_service._switch_starved``）。
    合成一个数，界面写"38 个失败"，用户去删的是 38 个好源。
    """

    switch_id: str
    done: int
    total: int
    failures: int = 0
    starved: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SourceSwitchFinishedEvent(AppEvent):
    """一轮换源扫描收口（§9 G3-D3：switch_id, candidates, cancelled, error）。

    发布方：ReadingService.switch_scan()（正常扫完 / 被取消 / 起扫前就出错，三种
    收尾**都发这一条**）；订阅方：换源屏 / flet 换源对话框。
    ``candidates`` 是排好序的整份候选，每行 ``{source_url, source_name, book}``
    ——与 ``switch_source`` 的返回值同一个形状（同一批结果走两条路回客户端却有两种
    字段，客户端就得写两套 fromJson，设计 §4.5 点名要避免的债）。
    为什么只在收尾发一次而不是逐源增量：候选顺序是整轮排出来的（书名精确 > 作者 >
    书源优先级），扫到一半排序会跳，flet 那一侧同样是扫完才渲染。
    ``cancelled=True`` 表示这一轮是被叫停的：界面那句"扫描完成，N 个候选"与
    "已停止换源扫描"分别对应两种文案。``error`` 非空表示这一轮根本没扫成
    （起扫前就抛穿）——不发这条，界面上的转圈就永远停不下来。
    """

    switch_id: str
    candidates: list
    cancelled: bool = False
    error: str = ""
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# M3 书源域事件（§4.3 事件表 + 契约 §2；命名即约定，不可变快照）。
# 以下事件随书源域服务里程碑落地：命名/字段顺序以集成契约为准。
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceChangedEvent(AppEvent):
    """书源数据变更事件（§4.3：change_type, source_id）。

    发布方：SourceService（CRUD/导入导出/启用停用/换源/封面）；
    订阅方：书源列表 / 检测 / 书架 / 找书视图。
    ``change_type`` 取值如 "created"/"updated"/"deleted"/"imported"；
    ``source_id`` 为书源 URL，全量导入时传空串（接收方整体刷新）。
    """

    change_type: str
    source_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CheckFinishedEvent(AppEvent):
    """书源检测完成事件（§4.3：source_id, state_class, summary）。

    发布方：SourceCheckService（每源检测完成发一条）；订阅方：CheckView。
    ``state_class`` 为九态分类结论（fully_usable/list_usable/…/blocked/unusable）；
    ``summary`` 为单行人类可读摘要（连通/搜索/标签/阅读抽检结论拼接）。

    ``done``/``total`` 是**本轮计划规模**给的进度分母：订阅方一度只能拿到逐源结果，
    于是"已完成的条数"被当成分母，界面显示成 ``12/12``（应当是 ``12/936``）。
    流水线那边这两个数本来就有（``on_result(result, done, total)``），只是没往事件上挂。
    单源检测与旧订阅方不看进度时留 0，表示"这条没说分母"。
    """

    source_id: str
    state_class: str
    summary: str
    done: int = 0
    total: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RepairReportReadyEvent(AppEvent):
    """正文规则自愈报告就绪事件（§4.3：report_id, source_id）。

    发布方：RuleRepairService；订阅方：RuleRepairUi。
    报告本体经 ReportId 由服务保存，视图收到事件后凭 report_id 取回候选修复，
    用户确认后才以 old||new 兜底分支写回书源（不自动改源，红线 §3.2）。
    """

    report_id: str
    source_id: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RepairProgressEvent(AppEvent):
    """正文规则自愈的阶段进度（``RuleRepairService.repair()`` 推进时发）。

    发布方：RuleRepairService（把 :func:`rule_repair.diagnose_content_rule` 的
    ``on_stage`` 挂点转成事件）；订阅方：RuleRepairController（写状态行）。
    一次诊断要真抓样本页、再离线回放几十条候选，秒级到十几秒——没有阶段事件，
    界面从点「自动修复」到报告就绪之间就是一潭死水（看起来像没点）。
    ``stage`` 是给人看的一句话，不是枚举，视图不做分支判断。
    """

    source_id: str
    stage: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ImportFinishedEvent(AppEvent):
    """资源导入完成事件（§4.3：kind, count, errors）。

    发布方：OnlineImportService（URL/文件导入分发完成）；订阅方：导入视图。
    ``kind`` 为资源类型（book_source/txt_rules/replace_rules/dict/rss/tts/theme）；
    ``count`` 为成功条数；``errors`` 为逐条错误/告警字符串列表
    （含"该类型功能未就绪"占位提示与导入即审的兼容性缺口告警，不阻断导入）。
    """

    kind: str
    count: int
    errors: list
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ImportProgressEvent(AppEvent):
    """资源导入的阶段性进度（几百条书源的逐条审计+落库是秒级到几十秒级）。

    发布方：OnlineImportService（URL 拉取阶段）与 SourceService（逐条落库阶段）；
    订阅方：导入视图。服务只发原始计数，节流在 Controller 层做（对齐
    :class:`DownloadProgressEvent` 的分工）。
    ``phase`` 是给人看的阶段名（如「拉取在线文件」「校验并落库」）；
    ``total`` 为 0 表示这一阶段条数不可预估（例如整段网络拉取），界面走不定长进度条。
    """

    kind: str
    phase: str
    done: int = 0
    total: int = 0
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# M4 内容形态域事件（§4.3 事件表 + 契约 §2；命名即约定，不可变快照）。
# 以下事件随内容形态域服务里程碑落地：命名/字段顺序以集成契约为准。
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReadAloudStateEvent(AppEvent):
    """朗读状态事件（契约 §2 / §4.3：state, book_url, chapter, position）。

    发布方：ReadAloudService（speak/stop/进度变化时）；订阅方：Flet 迷你播放条。
    ``state`` 取值如 "idle"/"speaking"/"paused"/"error"；
    ``chapter`` 为当前朗读章节索引；``position`` 为朗读进度（0-1 或秒，
    M4 系统 TTS 后端不精确上报时为 0）。事件不携带后端对象引用。
    """

    state: str
    book_url: str
    chapter: int = 0
    position: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class MediaPlayStateEvent(AppEvent):
    """视频书播放状态事件（契约 §2 / §4.3：state, book_url, chapter）。

    发布方：MediaPlayService（open/play/pause/resume/seek 时）；订阅方：视频视图。
    ``state`` 取值如 "idle"/"playing"/"paused"/"ended"/"error"；
    ``chapter`` 为播放列表中的章节索引。播放位置落 books 表
    ``play_position`` 列，不经事件重复传输（视图按需查询）。
    """

    state: str
    book_url: str
    chapter: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RssRefreshedEvent(AppEvent):
    """RSS 源刷新完成事件（契约 §2 / §4.3：source_id, new_count）。

    发布方：RssService.refresh_source()/refresh_all()（每源刷新完成发一条）；
    订阅方：RSS 视图（按 source_id 局部刷新文章列表/角标）。
    ``new_count`` 为本次刷新**新增**文章条数（已存在的文章按 (source_id,url)
    去重不计入）；刷新失败时不发本事件，错误经服务 status.last_error 暴露。
    """

    source_id: str
    new_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RssRefreshProgressEvent(AppEvent):
    """RSS 批量刷新进度（``refresh_all`` 逐源推进时发）。

    发布方：RssService.refresh_all()；订阅方：订阅页进度条。
    进度必须有事件（服务 status.progress 只有仪表盘在轮询）——不发包，多个源
    串行抓取期间界面就是一潭死水，看起来像没点。
    """

    done: int
    total: int
    current: str = ""
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# M5 工具域事件（§4.3 事件表 + 契约 §2；命名即约定，不可变快照）。
# 以下事件随工具域服务里程碑落地：命名/字段顺序以集成契约为准。
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReplaceRulesChangedEvent(AppEvent):
    """用户全局替换规则变更事件（契约 §2 / §6：ids）。

    发布方：ReplaceRuleService（save/delete/set_enabled 之后发）；
    订阅方：阅读链路（收到后重洗正文缓存，让新规则立即生效）/ 替换规则视图。
    ``ids`` 为本次受影响的规则 id 列表；全量导入/清空时传空列表
    （接收方整体重洗，与 SourceChangedEvent 空串语义一致）。
    """

    ids: list
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BackupFinishedEvent(AppEvent):
    """备份/恢复完成事件（契约 §2 / §4.3：kind, ok, detail）。

    发布方：WebDavService.backup()/restore()（每次完成发一条，无论成败）；
    订阅方：备份设置页（按 ok 提示成功/失败、展示 detail 摘要）。
    kind 取 "backup" / "restore"；ok 为整体是否成功；
    detail 为单行人类可读摘要（远端路径 / 条数 / 失败原因）。
    """

    kind: str
    ok: bool
    detail: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BackupProgressEvent(AppEvent):
    """备份/恢复逐阶段进度（``WebDavService`` 导出 / 打包 / 传输 / 落库推进时发）。

    发布方：WebDavService.backup()/restore()；订阅方：备份设置页进度行。
    和 RSS 刷新同一条理由：全量备份要导出六类数据、逐个算 SHA-256、再走网络，
    没有逐阶段事件时界面上就是一句"正在备份"挂十几秒，看起来像卡死。
    stage 是给人看的一句话，不是枚举；done/total 为可计量的成员数
    （无法计量时留 0，视图只显示 stage 不显示百分比）。
    """

    kind: str
    stage: str
    done: int = 0
    total: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class UpdateAvailableEvent(AppEvent):
    """发现新版本事件（契约 §2 / §4.3：version, notes, url）。

    发布方：UpdateService.check_update()（发现较当前更新且未被忽略的版本时发）；
    订阅方：关于/设置页（弹窗提示用户，不自动安装）。
    version 为新版本号（不含前导 v）；notes 为发布说明摘要；
    url 为安装包下载地址（用户手动下载安装，§1.17 不做静默自替换）。
    """

    version: str
    notes: str
    url: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ExportProgressEvent(AppEvent):
    """整本导出 epub/txt 的逐章进度（``BookExportService`` 写包时发）。

    发布方：BookExportService.export_epub()/export_txt()；订阅方：导出页进度行。
    与备份同一条理由：一本几百章的书要逐章读盘再组装，十几秒里界面只有
    一句"导出中…"，看起来像卡死。
    kind 是导出格式（``epub`` / ``txt``），stage 是给人看的一句话；
    done/total 为章数（total 为目录里可导出的章节总数，不可预估时留 0，
    视图只显示 stage 不显示百分比）。
    """

    kind: str
    stage: str
    done: int = 0
    total: int = 0
    timestamp: float = field(default_factory=time.time)
