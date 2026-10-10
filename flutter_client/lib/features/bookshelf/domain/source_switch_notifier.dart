/// 换源那一屏的状态机（§6.2：`AsyncNotifier`；一本书一套 args，随对话框容器活）。
///
/// 照的是 `app/controllers/bookshelf_controller.py` 的 `_seed_switch`/`_start_switch`/
/// `_run_switch`/`_stop_switch`/`_do_switch` 那一族，五条例子在代码里：
///
/// 1. **先本地种子、后联网扫描**：打开对话框那一拍发的是 `switch.seeds`（不联网、秒开），
///    「开始换源」才发 `switch.scan`（`mode="job"`，受理包只有 `{started, action}`）。
/// 2. **本轮身份自己攥着**（`switch_id`）：进度帧与收尾都靠它认"是不是当前那一轮"，
///    晚到的上一轮如果上屏，用户看到的是"停了候选还在换"。同一族规矩见
///    `find_books_notifier.dart` 的 generation 守护。
/// 3. **候选列表不重排也不过滤**：队列与排序都在服务侧（`order_switch_sources` +
///    `rank_switch_matches`），两侧各算一遍"谁在前"就只剩两份主观可比。
/// 4. **进度 0.5s 合流**：服务端那一族本来就按 `COALESCED_EVENTS` 合流，客户端再合一次
///    是为了把"每一帧一次整树重画"收成一拍一画（R4 实测：整页 diff 随行数线性变贵）。
/// 5. **`switch.cancel` 的那一格不是装饰**：`false` 说的是"什么都没在跑"，界面不许写
///    「已停止换源扫描」——那是 flet 拿不到的分支（它取消的是自己手里的 task），
///    跨进程这一发必须分得开。
///
/// 与 flet 的一处**有意**不同：种子那一发失败时 flet 只 `logger.debug` 之后当空表，
/// 这里除了同一句文案还往 [failures] 里推一条。理由见 `_loadSeeds`。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/events/engine_event.dart';
import '../../../core/rpc/envelope.dart';
import '../data/source_switch_models.dart';
import '../domain/bookshelf_notifier.dart';

/// 开这一屏要吃的那一本：书架那一行**原样那份字典**。
///
/// 为什么不是 `BookshelfItem`：协议面那三发（`switch.seeds` 的 `book`、`switch.scan`
/// 的 `book`、`switch.apply` 的 `record`）吃的都是服务端 `bookshelf.list` 的那一份
/// dict，界面在这里换成模型再倒回去，等于在边界上再造一次编解码（少一格就是少一格）。
class SwitchArgs {
  const SwitchArgs({required this.record, this.name = ''});

  /// 没注入时的那一份（路由/弹窗漏了 `extra` 就是这个形状，别让它悄悄开一本科）。
  const SwitchArgs.empty()
      : record = const <String, Object?>{},
        name = '';

  final Map<String, Object?> record;

  /// 标题那一句用的书名。留空就从快照里读——两处都写死会更早飘。
  final String name;

  bool get isEmpty => record.isEmpty;

  String get bookName =>
      name.isNotEmpty ? name : '${record['name'] ?? ''}';
}

/// 由**对话框容器**注入（不是全局那一份）：一本书一套 args。
/// 默认体是空的那一份，忘注入时屏幕上写的是"没收到书"，不是转圈。
final Provider<SwitchArgs> switchArgsProvider =
    Provider<SwitchArgs>((ref) => const SwitchArgs.empty());

final AsyncNotifierProvider<SourceSwitchNotifier, SourceSwitchState>
    sourceSwitchProvider =
    AsyncNotifierProvider<SourceSwitchNotifier, SourceSwitchState>(
  SourceSwitchNotifier.new,
  // 同 `bookshelfProvider`：Riverpod 默认那条指数退避会把一次失败变成 11 发。
  // 这里更糟的是那 11 发里有 `switch.scan`——用户没点第二遍，库却联网扫了 11 轮。
  retry: (_, _) => null,
);

class SourceSwitchNotifier extends AsyncNotifier<SourceSwitchState> {
  /// 事件合流窗口：与检测/找书那两屏同值（flet `throttle(…, 0.5)`）。
  static const Duration _flushWindow = Duration(milliseconds: 500);

  Timer? _flush;

  /// 窗口里最后收到的那一帧（合流之后才落到 [state]）。
  SwitchProgress? _pendingProgress;

  /// 这一轮的身份证。服务端把它折成**字符串**回带，所以比对两边都转文本
  /// （按数字读那一份永远读到 0，见 [SwitchProgress.switchId] 那条注释）。
  int _switchId = 0;

  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  Stream<EngineFailure> get failures => _failures.stream;

  @override
  Future<SourceSwitchState> build() async {
    ref.onDispose(() {
      _flush?.cancel();
      unawaited(_failures.close());
    });
    final args = ref.read(switchArgsProvider);
    if (args.isEmpty) {
      throw EngineFailure(EngineFailureKind.badRequest,
          '换源屏没有收到书：未注入 switchArgsProvider（书架那一行没把记录快照递进来）',
          action: 'switch.seeds');
    }
    final seeds = await _loadSeeds(args);
    // 快照回放：断线/切页/在途重开这一屏时，那一轮可能还在跑。不读这一发，
    // 界面就是一个永远不停的转圈（`switch.scan` 的受理包里没有进度，进度只在事件流里）。
    final replay = await _replay();
    if (replay != null && replay.running) {
      _adoptRound(replay.switchId);
      // 在途那一轮的计数与候选**整体接管**：服务端手里那份比本地种子新，
      // 而它带着本轮身份，事件帧才认得出"这是我这一轮"。
      return SourceSwitchState(
          run: replay, status: _replaySentence(replay));
    }
    // 没在跑就照 flet 那一屏的开法：候选列 = 本地种子，一句"点开始换源"。
    // 上一轮跑完的那批候选**不回放进新对话框**（flet 每次打开都从种子重新起手）。
    return SourceSwitchState(
        run: SwitchRun.idle.copyWith(candidates: seeds),
        status: switchSeedStatus(seeds.length));
  }

  SourceSwitchState get _current => state.value ?? SourceSwitchState.idle;

  void _emit(SourceSwitchState next) =>
      state = AsyncValue<SourceSwitchState>.data(next);

  /// 打开对话框那一拍的种子（`_seed_switch`：失败当空表，不让它顶掉整屏）。
  Future<List<SwitchCandidate>> _loadSeeds(SwitchArgs args) async {
    try {
      final rows = await ref
          .read(engineRepositoryProvider)
          .switchSeeds(book: args.record);
      return rows.map(SwitchCandidate.fromSeed).toList(growable: false);
    } on EngineFailure catch (exc) {
      // flet 这里只 logger.debug，屏幕上什么都看不出来；而"本地已有 0 个结果"与
      // "这一发没成"在界面上是同一句话——记忆里 `zero-counts-need-a-positive-control`
      // 那一族：仪器没接上时读数会朝你期望的方向假。所以文案照 flet，声音走横幅。
      _failures.add(exc);
      return const <SwitchCandidate>[];
    }
  }

  Future<SwitchRun?> _replay() async {
    try {
      return await ref.read(engineRepositoryProvider).switchSnapshot();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return null;
    }
  }

  /// 回放显示"还在跑"时那句上屏的话。
  ///
  /// 用的是进度那一句而不是 [kSwitchScanningStatus]：快照里有真的计数，能报就报——
  /// 只写"正在扫描同名书源…"会把已经扫到 7/40 的一轮说成刚起手。
  /// `total==0` 是起扫还没排完队列（或队列本就空）那一格，那时没有分数可报。
  String _replaySentence(SwitchRun run) {
    if (run.error.isNotEmpty) return switchFailedStatus(run.error);
    if (run.total <= 0) return kSwitchScanningStatus;
    return switchProgressStatus(SwitchProgress(
        switchId: run.switchId,
        done: run.done,
        total: run.total,
        failures: run.failures,
        starved: run.starved));
  }

  // ------------------------------------------------------------------ #
  // 开始 / 停止
  // ------------------------------------------------------------------ #
  /// 发一轮联网核对（`_start_switch`）。队列、命中判定与排序全在服务侧。
  ///
  /// `keyword` 显式传书名而不是留空串：flet 那侧 `switch_source(…, keyword=record.name)`
  /// 就是这个值，空串走的是服务端"按 book.name"的默认——两条路同一个结果，但把它
  /// 写进 payload 才比得出"这一轮扫的是哪几个字"。
  /// `group` 留空 = 全部启用源（`searchable_sources` 那一份口径）。分组控件在 G4，
  /// 不在这里发明一个没有靶子的下拉。
  Future<void> start() async {
    final args = ref.read(switchArgsProvider);
    _switchId = DateTime.now().microsecondsSinceEpoch;
    _pendingProgress = null;
    _flush?.cancel();
    _emit(_current.copyWith(
      run: _current.run.copyWith(running: true),
      status: kSwitchScanningStatus,
    ));
    try {
      await ref.read(engineRepositoryProvider).switchScan(
          book: args.record,
          switchId: _switchId,
          keyword: args.bookName,
          group: '');
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      // 受理都没到手：这一轮根本没起来，转圈不能一直转（那是"在扫"的假象）。
      _switchId = 0;
      _emit(_current.copyWith(
          run: _current.run.copyWith(running: false),
          status: switchFailedStatus(exc.message)));
    }
  }

  /// 叫停（`_stop_switch`）。回话那一格决定说哪一句：`false` 时什么都没在跑，
  /// 「已停止换源扫描」就是界面在说一件没发生过的事。
  Future<String> cancel() async {
    bool stopped = false;
    try {
      stopped = await ref.read(engineRepositoryProvider).switchCancel();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return switchFailedStatus(exc.message);
    }
    if (!stopped) return _emitStatus(kSwitchNothingRunningStatus);
    _flush?.cancel();
    _pendingProgress = null;
    // 转圈就地停下（flet 的 `set_running(False, …)` 同一形状）；服务端随后那条
    // finished(cancelled=true) 只会把同一句话再写一遍，不会打架。
    _emit(_current.copyWith(
        run: _current.run.copyWith(running: false, cancelled: true),
        status: kSwitchStoppedStatus));
    return kSwitchStoppedStatus;
  }

  // ------------------------------------------------------------------ #
  // 事件
  // ------------------------------------------------------------------ #
  /// 一条 `SourceSwitchProgressEvent`（只带计数，不带候选）。
  void onSwitchProgress(EngineEvent event) {
    if (!_isCurrentRound(event)) return;
    _pendingProgress = SwitchProgress.fromMap(event.payload);
    if (_flush?.isActive ?? false) return;
    _flush = Timer(_flushWindow, _flushProgress);
  }

  /// 一条 `SourceSwitchFinishedEvent`（收尾带候选）。
  ///
  /// 计数**不从这里读**：那四格只在进度帧里发，读成 0 会把已经跑完的一轮在界面上
  /// 写成"扫了 0 个源"（见 [SwitchRun.fromFinishedEvent]）。
  void onSwitchFinished(EngineEvent event) {
    if (!_isCurrentRound(event)) return;
    _flush?.cancel();
    _pendingProgress = null;
    final finished = SwitchRun.fromFinishedEvent(event.payload);
    final run = _current.run.copyWith(
      running: false,
      cancelled: finished.cancelled,
      candidates: finished.candidates,
      error: finished.error,
    );
    _emit(_current.copyWith(
        run: run,
        status: finished.error.isNotEmpty
            ? switchFailedStatus(finished.error)
            : finished.cancelled
                ? kSwitchStoppedStatus
                : switchFinishedStatus(finished.candidates.length)));
  }

  /// 这一帧是不是**当前那一轮**的。`_switchId` 为 0 = 这一屏没起过轮，一律不认。
  ///
  /// 两边都转文本比：服务端把 id 折成 str 再回带（`sid = str(switch_id or 0)`），
  /// 而 JSON 里它看着像个 int——按数字读那份永远读到 0，这条守卫就整个失效。
  bool _isCurrentRound(EngineEvent event) =>
      _switchId != 0 && '${event.payload['switch_id'] ?? ''}' == '$_switchId';

  /// 采纳服务端快照里那一轮的身份（回放路径：这一屏不是发起方，但事件得认）。
  void _adoptRound(String switchId) {
    final parsed = int.tryParse(switchId);
    if (parsed != null && parsed != 0) _switchId = parsed;
  }

  void _flushProgress() {
    final progress = _pendingProgress;
    if (progress == null) return;
    _pendingProgress = null;
    _emit(_current.copyWith(
        run: _current.run.withProgress(progress),
        status: switchProgressStatus(progress)));
  }

  // ------------------------------------------------------------------ #
  // 写回
  // ------------------------------------------------------------------ #
  /// 选中一条候选换进书架（`_confirm_switch` + `_do_switch`）。
  ///
  /// 三格各管一件事：`record` 找回书架那一行，`source_url` 找回那条源（取不到服务端
  /// **报错**而不是回 false），`book` 是候选那一本的快照。
  /// 返回那一句话，由调用方决定是留着还是关掉对话框（flet 是先 pop 再写状态行）。
  Future<String> apply(SwitchCandidate candidate) async {
    final args = ref.read(switchArgsProvider);
    String message;
    try {
      final result = await ref.read(engineRepositoryProvider).applySwitch(
          record: args.record,
          sourceUrl: candidate.sourceUrl,
          book: candidate.book.raw);
      // flet 的 `ok=False` 折的是"记录被删了/与别的书冲突"那一种（apply_source_switch
      // 返回 false），服务端那三种成因里"取不到源"是抛，走下面的 catch。
      message = result.applied
          ? switchAppliedStatus(candidate.book.name)
          : kSwitchApplyConflictStatus;
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      message = switchFailedStatus(exc.message);
    }
    // 无论换成没换成都刷一次书架：flet 的 `_do_switch` 在 pop 之后无条件 `await _load()`
    // ——写回可能改了同一行的 source/进度，而"没换成"的那一行也可能已经被另一侧删了。
    await ref.read(bookshelfProvider.notifier).reload();
    _emitStatus(message);
    return message;
  }

  String _emitStatus(String message) {
    _emit(_current.copyWith(status: message));
    return message;
  }
}

/// 这一屏的全部可变状态：一轮扫描的读数 + 那一句上屏的话。
class SourceSwitchState {
  const SourceSwitchState({
    this.run = SwitchRun.idle,
    this.status = kSwitchIdleStatus,
  });

  final SwitchRun run;

  /// flet 那侧状态、进度、候选**共用一个 `_status` 槽**（谁最后写谁说话），
  /// 所以这里也只有一个字符串槽，而不是 status/progress/error 三格。
  final String status;

  static const SourceSwitchState idle = SourceSwitchState();

  List<SwitchCandidate> get candidates => run.candidates;
  bool get running => run.running;

  /// 进度条那一格（`done/total`，夹在 0~1）。
  double get progressValue => switchProgressValue(run.done, run.total);

  SourceSwitchState copyWith({SwitchRun? run, String? status}) =>
      SourceSwitchState(run: run ?? this.run, status: status ?? this.status);
}
