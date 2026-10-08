/// 检测那一屏的状态机（§6.2：`AsyncNotifier`；事件驱动 + 快照回放）。
///
/// 两条从 flet 那边照搬过来的规矩，都写在这里而不是散在界面上
/// （`app/controllers/source/check_controller.py`）：
///
/// 1. **逐源结果一条都不许丢**：一源一条 `CheckFinishedEvent`，按 `source_id`
///    覆盖同名行（`:180-207` 的 `_render` 与 `check_view.py:79-99` 的 `render_result`
///    都是这个形状）。所以这里收流的是**增量**，不是"到了就去打一次 snapshot"——
///    每源一发 6.9 MB 的 `source.list` 那一类错误在检测这条线上同样致命。
/// 2. **分母只能由发布方给**（`check_controller.py:196-199`）：本轮计划规模在事件的
///    `total` 里，本屏自己启动的那一轮不会走快照回放，没拿到分母就不动进度条，
///    **绝不拿完成数冒充**（那就是 12/12 的假收尾）。
///
/// `check.snapshot` 在这里只干一件事：切页回来/断线重连时把已经跑过去的那批结果
/// 接回来（进度不许只活在事件流里，记忆 `ui-progress-must-travel-as-events`）。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/events/engine_event.dart';
import '../../../core/rpc/envelope.dart';
import '../data/check_models.dart';

final AsyncNotifierProvider<CheckNotifier, CheckSnapshot> checkProvider =
    AsyncNotifierProvider<CheckNotifier, CheckSnapshot>(
  CheckNotifier.new,
  retry: (_, _) => null,
);

class CheckNotifier extends AsyncNotifier<CheckSnapshot> {
  /// 收尾那一句的状态：本轮是不是这一屏启动的、有没有收到过结果。
  /// 计划规模（[total]）单独存：它是事件给的，快照回放不一定带得回来。
  final Set<String> _seen = <String>{};
  final Map<String, CheckRow> _rowsByKey = <String, CheckRow>{};
  int total = 0;
  bool running = false;

  /// 事件合流的窗口：0.5 s 内到的多条只下发一次重画。
  ///
  /// 这条不是审美。flet 那边 `subscribe(CheckFinishedEvent, …, flush_interval=0.5)`
  /// 是实测换来的：整页 diff 随行数线性变贵（≈0.024 ms/行，docs/known-issues/004
  /// 的 900 行表），全库扫描时每源一条就会把界面打成一条不断的全量重画。
  static const Duration _flushWindow = Duration(milliseconds: 500);
  Timer? _flush;

  @override
  Future<CheckSnapshot> build() async {
    ref.onDispose(() {
      _flush?.cancel();
      unawaited(_results.close());
      unawaited(_failures.close());
    });
    return _restore();
  }

  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();
  final StreamController<CheckRow> _results =
      StreamController<CheckRow>.broadcast();

  Stream<EngineFailure> get failures => _failures.stream;

  /// 单条结果到了的声音（界面用它把行推给 `ListView`，不等整份快照）。
  Stream<CheckRow> get results => _results.stream;

  /// 首屏/切回来的回放：读快照，但**空快照不许盖掉"尚未检测"**
  /// （`check_controller.py:141-144`：没跑过、没结果、没结论的三格同时为空就直接返回）。
  Future<CheckSnapshot> _restore() async {
    final snapshot = await _fetch();
    if (snapshot == null) return _emptySnapshot;
    if (!snapshot.running && snapshot.results.isEmpty && snapshot.summary == null) {
      return _emptySnapshot;
    }
    _absorb(snapshot);
    return snapshot;
  }

  Future<CheckSnapshot?> _fetch() async {
    try {
      return await ref.read(engineRepositoryProvider).checkSnapshot();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return null;
    } catch (exc) {
      _failures.add(
          EngineFailure(EngineFailureKind.engine, '$exc', action: 'check.snapshot'));
      return null;
    }
  }

  void _absorb(CheckSnapshot snapshot) {
    running = snapshot.running;
    if (snapshot.total > 0) total = snapshot.total;
    for (final row in snapshot.results) {
      _put(row, notify: false);
    }
  }

  void _put(CheckRow row, {bool notify = true}) {
    _seen.add(row.sourceId);
    _rowsByKey[row.sourceId] = row;
    if (notify) _results.add(row);
  }

  /// 当前已经收到的行，按到达顺序（`check_view.py:96-99` 的追加序）。
  List<CheckRow> get rows => _rowsByKey.values.toList(growable: false);

  int get doneCount => _seen.length;

  /// 进度分数：没拿到分母就回 0（不动进度条，也不冒充完成）。
  double get progressValue => total == 0 ? 0 : checkProgressValue(doneCount, total);

  // ------------------------------------------------------------------ #
  // 事件
  // ------------------------------------------------------------------ #
  /// 一条 `CheckFinishedEvent` 的增量（界面收到就交进来）。
  void onCheckFinished(EngineEvent event) {
    final row = CheckRow.fromMap(event.payload);
    final planned = event.payload['total'];
    if (planned is num && planned.toInt() > 0) total = planned.toInt();
    _put(row);
    _scheduleFlush();
  }

  void _scheduleFlush() {
    if (_flush?.isActive ?? false) return;
    _flush = Timer(_flushWindow, () {
      final snapshot = state.value ?? _emptySnapshot;
      state = AsyncValue<CheckSnapshot>.data(snapshot.copyWith(
          results: rows, total: total, running: running));
    });
  }

  // ------------------------------------------------------------------ #
  // 动作
  // ------------------------------------------------------------------ #
  /// 起一轮。`mode="job"`：这一发只回"服务端肯起"，一条结果都不在回话里。
  ///
  /// 所以这里**不**把 `started` 当成成功文案——flet 那边写的是"检测已启动…"，
  /// 真正的结论要等收尾那一行（`_summary_line`）。
  Future<String> start({List<String>? groups}) async {
    if (running) return '检测已在进行中';
    _seen.clear();
    _rowsByKey.clear();
    total = 0;
    running = true;
    try {
      await ref.read(engineRepositoryProvider).checkStart(groups: groups);
    } on EngineFailure catch (exc) {
      running = false;
      _failures.add(exc);
      return '检测中断：${exc.message}';
    }
    state = AsyncValue<CheckSnapshot>.data(_emptySnapshot.copyWith(running: true));
    return '检测已启动…';
  }

  /// 停止 = 取消在跑的那一轮，**不是**退役服务
  /// （`check_controller.py:112-115` 明写：调 `Service.stop()` 会把这轮会话的检测服务起不来）。
  Future<String> cancel() async {
    try {
      await ref.read(engineRepositoryProvider).checkCancel();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return '取消检测失败：${exc.message}';
    }
    running = false;
    return '已请求停止';
  }

  /// 收尾：一轮跑完（`finished` 事件或界面自己按下的停止）之后按快照重画，
  /// 让"收尾那一句"不只被启动它的那一屏看到（`_run` 末尾那句 `self._render(snap)`）。
  Future<void> finish() async {
    running = false;
    final snapshot = await _fetch();
    if (snapshot == null) {
      state = AsyncValue<CheckSnapshot>.data((state.value ?? _emptySnapshot)
          .copyWith(results: rows, total: total, running: false));
      return;
    }
    _absorb(snapshot);
    state = AsyncValue<CheckSnapshot>.data(snapshot);
  }

  /// 单条源的即时检测（编辑器/自愈那条线问"这一条能不能用"）。
  /// 与批量那一轮不同：这一发**是**一次同步等的 RPC，结果就在回话里。
  Future<CheckSourceReport> checkOne(Map<String, Object?> sourceJson) async {
    try {
      return await ref
          .read(engineRepositoryProvider)
          .checkSource(sourceJson: sourceJson);
    } on EngineFailure {
      rethrow;
    }
  }
}

/// 「尚未检测」那一格：`total=0`、没有结果、没有结论。
///
/// 它和"跑完了但一条都没检出来"是两种形状——后者 `summary` 不是 null，
/// `checkSummaryLine` 会回"检测完成：可用 0/0，阻断 0"，界面上必须画那一句。
const CheckSnapshot _emptySnapshot = CheckSnapshot(
  running: false,
  cancelled: false,
  total: 0,
  depth: '',
  tiering: CheckTiering.empty,
  deepQueueCount: 0,
  summary: null,
  results: <CheckRow>[],
);
