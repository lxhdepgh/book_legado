/// 换源对话框：`状态一句 + 进度条 + 同名书候选（点一行就写回）+ 开始/停止/关闭`
/// （对齐 flet `app/views/reading/source_switch_dialog.py`）。
///
/// 六条形状是从 flet 那边抄的，逐条有守卫
/// （`tests/test_gateway_contract_single_source.py::test_source_switch_wording_is_read_from_flet_into_dart`）：
///
/// - 标题是 `换源 · {书名}`，书名取书架那一行的 `name`（不是候选那一本的）。
/// - **开始与停止是换visible，不是换 enabled**（`set_running`：`_start_btn.visible = not running`、
///   `_cancel_btn.visible = running`）。做成"两个都在、一个灰着"就多了 flet 没有的那一格形状。
/// - 进度条只在跑的那几拍出现（同一条 `set_running` 里 `_progress.visible = running`）。
/// - 「等待开始」是对话框自己的初值；种子那一发回来就换成 `本地已有 N 个结果…`／
///   `点「开始换源」扫描全部书源`，两者都不是"还没开始"。
/// - 候选空着时那一格是**一句说明**，不是一片空白（`render_candidates([])`）。
/// - 行标题 = 候选那一本的书名，副标题 = 来源名（`source_name` 空时 flet 回退到源对象的
///   `bookSourceName`；协议面上候选行本就带 `source_name`，这里不另起回退）。
///
/// 与 flet 的一处**有意**不同：写回成功后 flet 先 pop 对话框、再把「已切换到：X」写到
/// 书架那屏的状态行上。Dart 这边书架那屏的状态条只有 `书架共 N 本` 一个槽
/// （`_ShelfStatus` 的 `message` 是空串），没有地方放这一句，所以句子留在对话框自己的
/// 状态格里、由用户按「关闭」退场。两句都在"用户看得见这一句"这一条上等价，
/// 而少一次自动 pop 就少一条"pop 与在途事件谁先到"的竞态。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/events/event_listener.dart';
import '../../../core/events/event_names.dart';
import '../../../core/widgets/failure_banner.dart';
import '../data/source_switch_models.dart';
import '../domain/source_switch_notifier.dart';

/// 从书架那一行开这一屏。[record] 是 `bookshelf.list` 的原样一行（不是重建的字典）。
///
/// `ProviderScope` 套在这里而不是套在调用方：一本书一套 args，关掉对话框这一套状态机
/// 就随容器散掉，下一次开别的书不会带着上一本的候选（同 `app/router.dart` 里阅读器
/// 那一格的处理）。`sourceSwitchProvider` 必须**一起**覆盖：没被覆盖的 provider 元素
/// 落在根容器里，它读的 `switchArgsProvider` 也是根容器那一份——只覆盖 args 的话，
/// 屏上写的是"换源屏没有收到书"，而 `record` 明明传进来了。
Future<void> openSourceSwitch(
  BuildContext context, {
  required Map<String, Object?> record,
  required String name,
}) =>
    showDialog<void>(
      context: context,
      builder: (_) => ProviderScope(
        overrides: [
          switchArgsProvider.overrideWithValue(
              SwitchArgs(record: record, name: name)),
          sourceSwitchProvider.overrideWith(SourceSwitchNotifier.new),
        ],
        child: const SourceSwitchDialog(),
      ),
    );

class SourceSwitchDialog extends ConsumerStatefulWidget {
  const SourceSwitchDialog({super.key});

  @override
  ConsumerState<SourceSwitchDialog> createState() => _SourceSwitchDialogState();
}

class _SourceSwitchDialogState extends ConsumerState<SourceSwitchDialog> {
  String _status = '';

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(sourceSwitchProvider);
    final notifier = ref.read(sourceSwitchProvider.notifier);
    final snapshot = state.value ?? SourceSwitchState.idle;
    // 打开这一拍已经上屏的那一句（种子句/回放句）比 notifier 返回值更早到；
    // _status 只装**动作的直接回报**（点停止、点候选），谁最后写谁说话同 flet。
    final status = _status.isNotEmpty ? _status : _stateStatus(state, snapshot);
    final running = snapshot.running;

    listenToEngineEvents(
      ref,
      <String>[
        EngineEventNames.sourceSwitchProgress,
        EngineEventNames.sourceSwitchFinished,
      ],
      (event) {
        if (event.name == EngineEventNames.sourceSwitchProgress) {
          notifier.onSwitchProgress(event);
        } else {
          notifier.onSwitchFinished(event);
        }
        // 收尾之后清掉本地那句：状态槽交还给状态机，免得「已停止」永远压着后面的进度句。
        if (event.name == EngineEventNames.sourceSwitchFinished) _status = '';
      },
      onStreamError: (error) => setState(() => _status = '事件流断开：$error'),
    );

    return AlertDialog(
      title: Text(switchDialogTitle(ref.read(switchArgsProvider).bookName)),
      content: SizedBox(
        width: 480,
        height: 420,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            FailureBanner(stream: notifier.failures),
            Text(status,
                style: Theme.of(context)
                    .textTheme
                    .bodySmall
                    ?.copyWith(color: Colors.grey[600])),
            if (running)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: LinearProgressIndicator(value: snapshot.progressValue),
              ),
            const Divider(height: 1),
            Expanded(child: _Candidates(rows: snapshot.candidates)),
          ],
        ),
      ),
      actions: <Widget>[
        if (!running)
          FilledButton.icon(
            key: const Key('switch.start'),
            onPressed: () {
              _status = '';
              notifier.start();
            },
            icon: const Icon(Icons.play_arrow),
            label: const Text(kSwitchStartLabel),
          )
        else
          OutlinedButton.icon(
            key: const Key('switch.stop'),
            onPressed: () async {
              final message = await notifier.cancel();
              if (mounted) setState(() => _status = message);
            },
            icon: const Icon(Icons.stop),
            label: const Text(kSwitchStopLabel),
          ),
        TextButton(
          key: const Key('switch.close'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text(kSwitchCloseLabel),
        ),
      ],
    );
  }

  /// 状态槽在"状态机还没跑出来"那一拍读什么。
  ///
  /// `state` 是 AsyncError 时必须把那一层说出来（忘注入 args 那一支就是它），
  /// 不许拿「等待开始」去盖——那会把"这一屏没收到书"演成"还没点开始"。
  String _stateStatus(AsyncValue<SourceSwitchState> state, SourceSwitchState snapshot) {
    final error = state.error;
    if (error != null) return '$error';
    return snapshot.status;
  }
}

/// 候选那一列。点一行 = 写回那一本（flet 的 `on_confirm`）。
///
/// **不在屏上也建**（`ListView(children:)` 而不是 `builder`）：与检测那屏同一条理由，
/// 一轮扫描的候选行数是有限的（命中同名书的那些站），全建的钱没有"切回来才补建"贵。
class _Candidates extends ConsumerStatefulWidget {
  const _Candidates({required this.rows});

  final List<SwitchCandidate> rows;

  @override
  ConsumerState<_Candidates> createState() => _CandidatesState();
}

class _CandidatesState extends ConsumerState<_Candidates> {
  void _confirm(SwitchCandidate candidate) {
    // 那句「已切换到：X」由 notifier 写进它自己的 state（`apply` 走 `_emitStatus`），
    // 外层对话框 `ref.watch` 的就是那一份——这里不 setState、也不弹 snackbar：
    // 弹一处就长出第二个状态槽，而 flet 全屏只有一个 `_status`。
    ref.read(sourceSwitchProvider.notifier).apply(candidate);
  }

  @override
  Widget build(BuildContext context) {
    if (widget.rows.isEmpty) {
      return Center(
        child: Text(kSwitchEmptyCandidates,
            style: TextStyle(color: Colors.grey[500], fontSize: 12)),
      );
    }
    return ListView(
      children: <Widget>[
        for (final row in widget.rows)
          ListTile(
            key: ValueKey(row.identityKey),
            dense: true,
            leading: const Icon(Icons.radio_button_unchecked),
            title: Text(row.book.name,
                maxLines: 1, overflow: TextOverflow.ellipsis),
            subtitle: Text(row.sourceName,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall),
            onTap: () => _confirm(row),
          ),
      ],
    );
  }
}
