/// 换源那一族的数据面：轮次快照、候选行、进度帧、写回回执，以及上屏那几句文案。
///
/// 键名从真响应取（第九十三轮抓的那五发）：
/// `tests/gateway/golden/switch.snapshot.json`（顶层 9 键 = `field_names` 那 9 格）、
/// `switch.apply.json`（2 键）、`switch.cancel.json`（1 键）、
/// `switch.seeds.json`（每行 35 键，与 `browse.books` 同一套 `_book_item_dict`），
/// 以及 `switch.scan.json` 的 `event_sample`（finished 的 5 格）与 `progress_sample`
/// （进度的 6 格）。绑定表在 `test/golden_contract_test.dart` 的 `_feeds`。
///
/// 文案那几段**以 flet 源码为准**（`app/views/reading/source_switch_dialog.py` 的槽位
/// 与 `app/controllers/bookshelf_controller.py` 的 `_seed_switch`/`_start_switch`/
/// `_run_switch`/`_stop_switch`/`_do_switch`），跨语言守卫是
/// `tests/test_gateway_contract_single_source.py::test_source_switch_wording_is_read_from_flet_into_dart`。
/// 为什么这里要有文案而不是让界面自己拼：flet 那侧状态、进度、候选**共用一个 `_status`
/// 槽**（谁最后写谁说话），把句子收在一处，两侧的优先级才比得出东西。
library;

import '../../find_books/data/book_row.dart';

/// 一条候选：`{source_url, source_name, book}`（`switch_source` 的返回行）。
class SwitchCandidate {
  const SwitchCandidate({
    required this.sourceUrl,
    required this.sourceName,
    required this.book,
  });

  /// 写回那一发吃的就是它（`switch.apply` 的 `source_url`：跨进程报不出源对象，
  /// 只报得到这个 url——服务端按它 `switch.apply`→`_source_by_url` 取回整条源）。
  final String sourceUrl;
  final String sourceName;

  /// 候选那一本。**共用 [BookRow]**：`switch.seeds` 与 finished 事件里的 `book`
  /// 都是 `_book_item_dict` 那 35 键，与搜索结果/分类书单同形（服务端定的形状）。
  final BookRow book;

  /// 候选行的键。同一站能出两本同名书（黄金样本 `switch.snapshot` 里 a 站就有
  /// `book/1` 与 `book/2` 两行），所以按"站 + 书 url"而不是按站去重。
  String get identityKey => '$sourceUrl|${book.url}';

  factory SwitchCandidate.fromMap(Map<String, Object?> json) {
    final raw = json['book'];
    return SwitchCandidate(
      sourceUrl: '${json['source_url'] ?? ''}',
      sourceName: '${json['source_name'] ?? ''}',
      book: BookRow.fromJson(
          raw is Map ? Map<String, Object?>.from(raw) : const <String, Object?>{}),
    );
  }

  /// 本地种子那一发：回的是**裸书行**（一行本就来自一条源），没有外层包装。
  /// 按 (name, author) 合流的是搜索结果，而 `switch.seeds` 走的是
  /// `search_store.list_by_name_author` 的原始行——于是 `origin` 就是那条候选的源。
  factory SwitchCandidate.fromSeed(BookRow row) => SwitchCandidate(
        sourceUrl: row.origin,
        sourceName: row.originName,
        book: row,
      );

  @override
  String toString() => 'SwitchCandidate($sourceName, ${book.name})';
}

/// 进度帧（`SourceSwitchProgressEvent` 的载荷）。
///
/// 它是**计数**，不带候选：`COALESCED_EVENTS` 里那一族 0.5s 合流、只发计数
/// （`tests/test_gateway_contract.py::test_only_count_carrying_events_are_coalesced`
/// 两个方向都查：带结果的载荷不许合流，否则丢掉的就是某一源的那一本）。
class SwitchProgress {
  const SwitchProgress({
    required this.switchId,
    required this.done,
    required this.total,
    required this.failures,
    required this.starved,
  });

  final String switchId;
  final int done;

  /// 本轮**队列**长度（`order_switch_sources` 排完的那个总数，不是已完成数）。
  final int total;
  final int failures;

  /// 被我们自己的解析挤掉的源（`starved`）。
  ///
  /// 它**不在** `failures` 里（`switch_source` 明写这一条）：那些源是好的，
  /// 把它们报成"失败"会诱导用户去删好源——所以文案也是单独的一句。
  final int starved;

  factory SwitchProgress.fromMap(Map<String, Object?> map) => SwitchProgress(
        switchId: '${map['switch_id'] ?? ''}',
        done: _asInt(map['done']),
        total: _asInt(map['total']),
        failures: _asInt(map['failures']),
        starved: _asInt(map['starved']),
      );
}

/// 一轮换源扫描的快照（`switch.snapshot` 的回话，也是 finished 事件的那五格）。
class SwitchRun {
  const SwitchRun({
    required this.switchId,
    required this.running,
    required this.cancelled,
    required this.done,
    required this.total,
    required this.failures,
    required this.starved,
    required this.candidates,
    required this.error,
  });

  /// 本轮身份。服务端把它从 int 折成**字符串**再回带（`switch_scan` 里
  /// `sid = str(switch_id or 0)`，事件与快照都用它），所以这里也是 String：
  /// 按数字读会永远读到 0，于是"只认最新那一轮"那半边守卫整个失效
  /// （`search.start` 的 `search_id` 同一族）。
  final String switchId;

  /// 还在跑吗。收尾三种（扫完 / 被叫停 / 起扫前就出错）**都会**把它置成 false
  /// 并发一条 finished——没有这一格，跨进程那侧的转圈就永远停不下来。
  final bool running;
  final bool cancelled;

  /// 逐源计数：进度帧才是它们的原乡，快照这一发是**回放**（断线/切页补回来的那份）。
  final int done;
  final int total;
  final int failures;
  final int starved;

  /// 已扫出的候选，按服务侧排好的序（命中判定与排序都在 `switch_source`/
  /// `rank_switch_matches` 里，客户端**不重排**：两侧各算一遍"谁在前"，
  /// 比对就只比得了两份主观）。
  final List<SwitchCandidate> candidates;

  /// 空串 = 没出错。**非空时它不是"零候选"**：`candidates` 为空 + `error` 为空
  /// 说的是"扫完了，没有同名书"，那是两种形状。
  final String error;

  static const SwitchRun idle = SwitchRun(
    switchId: '',
    running: false,
    cancelled: false,
    done: 0,
    total: 0,
    failures: 0,
    starved: 0,
    candidates: <SwitchCandidate>[],
    error: '',
  );

  factory SwitchRun.fromJson(Map<String, Object?> json) => SwitchRun(
        switchId: '${json['switch_id'] ?? ''}',
        running: json['running'] == true,
        cancelled: json['cancelled'] == true,
        done: _asInt(json['done']),
        total: _asInt(json['total']),
        failures: _asInt(json['failures']),
        starved: _asInt(json['starved']),
        candidates: _candidates(json['candidates']),
        error: '${json['error'] ?? ''}',
      );

  /// `SourceSwitchFinishedEvent` 的那一份：**没有** done/total/failures/starved。
  ///
  /// 计数只在进度帧里发（合流那一族），收尾带的是结论。所以这里不读那四格：
  /// 读成 0 会把已经跑完的那一轮在界面上写成"扫了 0 个源"。
  factory SwitchRun.fromFinishedEvent(Map<String, Object?> payload) => SwitchRun(
        switchId: '${payload['switch_id'] ?? ''}',
        running: false,
        cancelled: payload['cancelled'] == true,
        done: 0,
        total: 0,
        failures: 0,
        starved: 0,
        candidates: _candidates(payload['candidates']),
        error: '${payload['error'] ?? ''}',
      );

  /// 把进度帧的计数并进这一份（事件驱动的增量）。候选**不动**：那一份归 finished。
  SwitchRun withProgress(SwitchProgress progress) => SwitchRun(
        switchId: switchId.isEmpty ? progress.switchId : switchId,
        running: running,
        cancelled: cancelled,
        done: progress.done,
        total: progress.total,
        failures: progress.failures,
        starved: progress.starved,
        candidates: candidates,
        error: error,
      );

  SwitchRun copyWith({
    String? switchId,
    bool? running,
    bool? cancelled,
    List<SwitchCandidate>? candidates,
    String? error,
  }) =>
      SwitchRun(
        switchId: switchId ?? this.switchId,
        running: running ?? this.running,
        cancelled: cancelled ?? this.cancelled,
        done: done,
        total: total,
        failures: failures,
        starved: starved,
        candidates: candidates ?? this.candidates,
        error: error ?? this.error,
      );
}

/// 写回那一发的回话（`switch.apply`）。
class SwitchApplyResult {
  const SwitchApplyResult({required this.applied, required this.sourceUrl});

  /// **false 是三种成因共用的一个读数**（记录被另一侧删了 / 与本条冲突 / …），
  /// 而"取不到那条源"服务端是**抛**不是回 false（schema 明写：三种成因不许折成一句）。
  /// 所以界面这里只有两句话可说，分不出成因是服务端的形状决定的，不是客户端偷懒。
  final bool applied;
  final String sourceUrl;

  factory SwitchApplyResult.fromJson(Map<String, Object?> json) =>
      SwitchApplyResult(
        applied: json['applied'] == true,
        sourceUrl: '${json['source_url'] ?? ''}',
      );
}

// -------------------------------------------------------------------- #
// 上屏那几句（flet 的 `_status` 槽写的是同一批句子）
// -------------------------------------------------------------------- #

/// 打开对话框的第一拍（`SourceSwitchDialog.__init__` 里那条初值）。
const String kSwitchIdleStatus = '等待开始';

/// 点了「开始换源」、还没收到第一个进度帧那一格（`_start_switch`）。
const String kSwitchScanningStatus = '正在扫描同名书源…';

/// 停止那一句（`_stop_switch`）。
///
/// 注意它**不等于** `switch.cancel` 的回话：那一发回的是"这一发有没有真叫停下什么"，
/// `cancelled:false` 时什么都没在跑，界面不许写这一句（见 [kSwitchNothingRunningStatus]）。
const String kSwitchStoppedStatus = '已停止换源扫描';

/// `switch.cancel` 回 `cancelled:false` 那一支。
///
/// flet 那侧没有这一句——它取消的是自己手里的 `_switch_task`，叫停必然成立。
/// 跨进程这一发做不到，所以服务端把"有没有叫停到"作为一格回出来；
/// 拿 [kSwitchStoppedStatus] 去盖它，就是界面在说一件没发生过的事。
const String kSwitchNothingRunningStatus = '没有正在扫描的轮次';

/// 候选那一列空着的时候那一格（`render_candidates`）。
const String kSwitchEmptyCandidates = '暂未找到同名书，换源扫描完成后这里会列出候选';

/// 标题那一句的前缀（`f"换源 · {name}"`）。
String switchDialogTitle(String bookName) => '换源 · $bookName';

/// 按钮那三格。
const String kSwitchStartLabel = '开始换源';
const String kSwitchStopLabel = '停止';
const String kSwitchCloseLabel = '关闭';

/// 本地种子那一格（`_seed_switch` 的两条文案，按种子条数分叉）。
String switchSeedStatus(int seedCount) => seedCount > 0
    ? '本地已有 $seedCount 个结果；点「开始换源」扫描全部书源'
    : '点「开始换源」扫描全部书源';

/// 扫描进行中的那一格（`_run_switch.on_progress`：主句 + 两个可选尾巴）。
///
/// 两个尾巴的**条件**照 flet：`failures`/`starved` 为 0 时整段不出现。
/// 空转时写"0 个失败"，用户读到的是"这一轮失败了什么"，而那句话根本没有指涉对象。
String switchProgressStatus(SwitchProgress progress) {
  final bits = <String>[
    if (progress.failures > 0) '${progress.failures} 个失败',
    if (progress.starved > 0) '${progress.starved} 个本机忙不过来',
  ];
  return '正在扫描 ${progress.done}/${progress.total} 个书源'
      '${bits.isEmpty ? '' : '，${bits.join('，')}'}';
}

/// 收尾那一格（`_run_switch` 的 `set_running(False, …)`）。
String switchFinishedStatus(int candidateCount) => '扫描完成，$candidateCount 个候选';

/// 整轮出错那一格（`error` 非空；`_run_switch` 的 except 分支同句式）。
String switchFailedStatus(String error) => '换源失败：$error';

/// 写回成功/失败那两句（`_do_switch`）。
String switchAppliedStatus(String bookName) => '已切换到：$bookName';
const String kSwitchApplyConflictStatus = '换源失败：记录已被移除或与其他书冲突';

/// 进度条那一发的分数：`done / max(total, 1)`，并且**夹在 0~1**
/// （flet `set_progress` 那一条 `max(0.0, min(1.0, …))`）。
///
/// 夹是必要的：`total` 来自队列长度，而 `switch_scan` 会先叫停上一轮再开新一轮，
/// 迟到的那一帧完全可能带着上一轮的 `done > total`。
double switchProgressValue(int done, int total) {
  final fraction = total < 1 ? 0.0 : done / total;
  if (fraction < 0) return 0;
  return fraction > 1 ? 1 : fraction;
}

List<SwitchCandidate> _candidates(Object? rows) => rows is List
    ? rows
        .whereType<Map<Object?, Object?>>()
        .map((row) => SwitchCandidate.fromMap(
            row.map((key, value) => MapEntry('$key', value))))
        .toList(growable: false)
    : const <SwitchCandidate>[];

int _asInt(Object? value) => value is num ? value.toInt() : 0;
