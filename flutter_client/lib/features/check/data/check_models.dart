/// 检测那一屏的三块数据：逐源结果行、运行快照、收尾文案。
///
/// 键名从真响应取：`tests/gateway/golden/check.snapshot.json`（9 个顶层键）、
/// `check.source.json`（31 个）、以及样本里 `check.start` 那份 `event_sample`
/// （`CheckFinishedEvent` 的 6 个字段）。
///
/// [checkSummaryLine] 是 flet `app/controllers/source/check_controller.py:30-54`
/// 的 `_summary_line` 的 port——**逐条对照**，包括"空串表示还没结论、别盖掉尚未检测"
/// 那条：那一格文案在两边都出现过"把没跑过说成跑完了"的形状，所以它值得原样搬。
library;

/// 九态中文标签（照 `app/views/source/check_view.py:16-25` 的 `CHECK_STATE_LABELS`；
/// 跨语言守卫在 `tests/test_gateway_contract_single_source.py` 逐项比对）。
const Map<String, String> kCheckStateLabels = <String, String>{
  'fully_usable': '完整可用',
  'list_usable': '列表可用',
  'search_only': '仅搜索可用',
  'explore_only': '仅发现可用',
  'content_unavailable': '阅读链路失败',
  'unknown': '待确认',
  'blocked': '访问被拦截',
  'unusable': '不可用',
};

/// 状态那一格的着色档位（flet `_state_color`：GREEN / GREY / ORANGE 三档）。
/// 这里是语义档，色板由主题面翻成实际颜色——两侧不许各有第三种档。
enum CheckStateTone {
  usable,
  blocked,
  pending;

  static CheckStateTone of(String stateClass) => switch (stateClass) {
        'fully_usable' => CheckStateTone.usable,
        'blocked' => CheckStateTone.blocked,
        _ => CheckStateTone.pending,
      };
}

/// 一行检测结果（事件那一条的形状：`source_id` / `state_class` / `summary`）。
class CheckRow {
  const CheckRow({
    required this.sourceId,
    required this.stateClass,
    required this.summary,
  });

  /// 书源 url 原样当行文本用——flet 那边标题就是它（`check_view.py:88`）。
  final String sourceId;
  final String stateClass;
  final String summary;

  /// 表外的 `state_class` 退回原值，原值是空串才说"未知"
  /// （`check_view.py:82` 的 `state_class or "未知"`）：**不许**退回"待确认"，
  /// 那是 `unknown` 这一档自己的话，混用会把"服务端没给分类"说成"分类结果是不确定"。
  String get label => kCheckStateLabels[stateClass] ?? (stateClass.isEmpty ? '未知' : stateClass);

  CheckStateTone get tone => CheckStateTone.of(stateClass);

  factory CheckRow.fromMap(Map<String, Object?> map) {
    final asMap = map.map((key, value) => MapEntry(key.toString(), value));
    return CheckRow(
      sourceId: '${asMap['source_id'] ?? ''}',
      stateClass: '${asMap['state_class'] ?? ''}',
      summary: '${asMap['summary'] ?? ''}',
    );
  }

  @override
  String toString() => 'CheckRow($sourceId, $stateClass)';
}

/// 一轮批量检测的结论计数（`run_snapshot()["summary"]`）。
///
/// 它**不是**按九态各数一遍：只有 `usable`/`blocked` 两档有人数
/// （`source_check_service.py:235-243`），所以收尾那句只报这两个数。
class CheckSummary {
  const CheckSummary({
    required this.total,
    required this.finished,
    required this.usable,
    required this.blocked,
  });

  final int total;
  final int finished;
  final int usable;
  final int blocked;

  factory CheckSummary.fromMap(Map<Object?, Object?> map) => CheckSummary(
        total: _asInt(map['total']),
        finished: _asInt(map['finished']),
        usable: _asInt(map['usable']),
        blocked: _asInt(map['blocked']),
      );
}

/// 自动分档的账（`tiering`）：这一轮实际怎么分的。
class CheckTiering {
  const CheckTiering({required this.planTotal, required this.shallow, required this.deep});

  final int planTotal;
  final int shallow;
  final int deep;

  factory CheckTiering.fromMap(Map<Object?, Object?> map) => CheckTiering(
        planTotal: _asInt(map['plan_total']),
        shallow: _asInt(map['shallow']),
        deep: _asInt(map['deep']),
      );

  static const CheckTiering empty = CheckTiering(planTotal: 0, shallow: 0, deep: 0);
}

/// `check.snapshot` 的整份快照（断线重连补进度用）。
class CheckSnapshot {
  const CheckSnapshot({
    required this.running,
    required this.cancelled,
    required this.total,
    required this.depth,
    required this.tiering,
    required this.deepQueueCount,
    required this.summary,
    required this.results,
  });

  final bool running;
  final bool cancelled;

  /// 本轮**计划**规模（不是已完成条数——`events.py:155-158` 记的就是把这两个数弄混的代价）。
  final int total;

  /// 档位是服务侧自动判的（今天恒 `auto`）；界面上没有"深度"可选。
  final String depth;
  final CheckTiering tiering;

  /// 还欠着没补验正文的源条数。
  final int deepQueueCount;

  /// 还没跑完是 `null`：空串与 null 是两件事，`checkSummaryLine` 分得开。
  final CheckSummary? summary;
  final List<CheckRow> results;

  /// 增量合并用：事件流只改得动"结果行 + 计划规模 + 运行态"这三格，
  /// 分档与结论仍归快照（界面上不该自己编）。
  CheckSnapshot copyWith({
    bool? running,
    bool? cancelled,
    int? total,
    String? depth,
    CheckTiering? tiering,
    int? deepQueueCount,
    CheckSummary? summary,
    List<CheckRow>? results,
  }) =>
      CheckSnapshot(
        running: running ?? this.running,
        cancelled: cancelled ?? this.cancelled,
        total: total ?? this.total,
        depth: depth ?? this.depth,
        tiering: tiering ?? this.tiering,
        deepQueueCount: deepQueueCount ?? this.deepQueueCount,
        summary: summary ?? this.summary,
        results: results ?? this.results,
      );

  factory CheckSnapshot.fromJson(Map<String, Object?> json) {
    final summary = json['summary'];
    final tiering = json['tiering'];
    final deepQueue = json['deep_queue'];
    final results = json['results'];
    return CheckSnapshot(
      running: json['running'] == true,
      cancelled: json['cancelled'] == true,
      total: _asInt(json['total']),
      depth: '${json['depth'] ?? ''}',
      tiering: tiering is Map
          ? CheckTiering.fromMap(tiering)
          : CheckTiering.empty,
      deepQueueCount: deepQueue is List ? deepQueue.length : 0,
      summary: summary is Map ? CheckSummary.fromMap(summary) : null,
      results: results is List
          ? results
              .whereType<Map<Object?, Object?>>()
              .map((row) => CheckRow.fromMap(
                  row.map((key, value) => MapEntry('$key', value))))
              .toList(growable: false)
          : const <CheckRow>[],
    );
  }
}

/// 快照 → 收尾那一句。空串 = "还没结论"，调用方**不许**拿它盖掉"尚未检测"。
String checkSummaryLine(CheckSnapshot snap) {
  if (snap.cancelled) return '检测已停止';
  final summary = snap.summary;
  if (summary == null) return '';
  final tiering = snap.tiering;
  final bits = <String>[
    // `plan_total` 为 0 时整段分档说明都不出现（flet 那里是 `if tiering.get("plan_total")`）。
    if (tiering.planTotal > 0) '快检 ${tiering.shallow} 源、补验正文 ${tiering.deep} 源',
    if (snap.deepQueueCount > 0) '待深检 ${snap.deepQueueCount} 源',
  ];
  final tail = bits.isEmpty ? '' : '（${bits.join('，')}）';
  return '检测完成：可用 ${summary.usable}/${summary.total}，'
      '阻断 ${summary.blocked}$tail';
}

/// `ProgressBar` 那一发的分数：`done / max(total, 1)`（`check_view.py:107`）。
double checkProgressValue(int done, int total) => done / (total < 1 ? 1 : total);

int _asInt(Object? value) => value is num ? value.toInt() : 0;

double? _asDouble(Object? value) => value is num ? value.toDouble() : null;

/// `check.source` 那一发的整份结果（编辑器问"这条源能不能用"）。
///
/// 键名取自真响应（`tests/gateway/golden/check.source.json` 的 `field_names`，
/// 31 个键）。这里只把界面要问的四件事做成字段——**结论 / 耗时 / 错在哪 / 有没有被自动停用**，
/// 其余（分类抽检 `category_results`、样本章 `sample_chapter_urls` 那一整棵）留在
/// [raw]：它们在 G2 的四个屏上一条都不显示，而自愈那一屏（G4）要的是原文逐层展开。
class CheckSourceReport {
  const CheckSourceReport({
    required this.name,
    required this.rawUrl,
    required this.status,
    required this.statusCode,
    required this.responseTimeMs,
    required this.error,
    required this.searchStatus,
    required this.bookCount,
    required this.searchError,
    required this.readingStatus,
    required this.readingError,
    required this.chapterCount,
    required this.tagTotal,
    required this.tagValid,
    required this.tagInvalid,
    required this.usable,
    required this.checkState,
    required this.autoDisabled,
    required this.raw,
  });

  final String name;

  /// 被检的那条源的书源 url。
  final String rawUrl;
  final String status;
  final int? statusCode;
  final double? responseTimeMs;
  final String? error;

  final String searchStatus;
  final int bookCount;
  final String? searchError;

  final String readingStatus;
  final String? readingError;
  final int? chapterCount;

  final int tagTotal;
  final int tagValid;
  final int tagInvalid;

  final bool usable;

  /// 九态那一格（同一套词汇，标签走 [kCheckStateLabels]）。
  final String checkState;

  /// 服务端把这条例自动停用了（`auto_disabled`）。
  /// 界面必须单独说出来——"不可用"和"因为这轮不可用被停用"是两件事。
  final bool autoDisabled;

  final Map<String, Object?> raw;

  String get stateLabel =>
      kCheckStateLabels[checkState] ?? (checkState.isEmpty ? '未知' : checkState);

  CheckStateTone get tone => CheckStateTone.of(checkState);

  factory CheckSourceReport.fromJson(Map<String, Object?> json) {
    // 只有"空回话"走那条保留构造函数。判据是**键的总数**而不是 `raw_url` 为空：
    // 后者在"源没有 raw_url"这一发也是空串，那样一个正常结果会被说成"这一轮没检"
    // （记忆 `aggregated-counters-hide-which-call` 的同一条：两种 0 不许共用一个读数）。
    if (json.isEmpty) return CheckSourceReport.none;
    return CheckSourceReport(
        name: '${json['name'] ?? ''}',
        rawUrl: '${json['raw_url'] ?? ''}',
        status: '${json['status'] ?? ''}',
        statusCode: _asIntOrNull(json['status_code']),
        responseTimeMs: _asDouble(json['response_time_ms']),
        error: json['error'] == null ? null : '${json['error']}',
        searchStatus: '${json['search_status'] ?? ''}',
        bookCount: _asInt(json['book_count']),
        searchError: json['search_error'] == null ? null : '${json['search_error']}',
        readingStatus: '${json['reading_status'] ?? ''}',
        readingError: json['reading_error'] == null ? null : '${json['reading_error']}',
        chapterCount: _asIntOrNull(json['chapter_count']),
        tagTotal: _asInt(json['tag_total']),
        tagValid: _asInt(json['tag_valid']),
        tagInvalid: _asInt(json['tag_invalid']),
        usable: json['usable'] == true,
        checkState: '${json['check_state'] ?? ''}',
        autoDisabled: json['auto_disabled'] == true,
        raw: Map.unmodifiable(json));
  }

  /// 一条都没检到的那一发（服务端 `check_source` 结果为空时回 `{}`）。
  ///
  /// 这个值存在的理由：`{}` 是合法回话，`fromJson` 一路走下去会造出
  /// 一条"名字空、状态空、不可用"的报告，界面把它渲染成"这条源坏了"——
  /// 而真事是**这一轮根本没检出结果**。调用方按 `identity == CheckSourceReport.none` 分叉。
  static const CheckSourceReport none = CheckSourceReport(
    name: '',
    rawUrl: '',
    status: '',
    statusCode: null,
    responseTimeMs: null,
    error: null,
    searchStatus: '',
    bookCount: 0,
    searchError: null,
    readingStatus: '',
    readingError: null,
    chapterCount: null,
    tagTotal: 0,
    tagValid: 0,
    tagInvalid: 0,
    usable: false,
    checkState: '',
    autoDisabled: false,
    raw: <String, Object?>{},
  );
}

int? _asIntOrNull(Object? value) => value is num ? value.toInt() : null;
