/// 检测屏：`开始检测 / 停止 + 进度 + 收尾那一句 + 逐源结果行`（对齐 flet `check_view.py`）。
///
/// 五条形状是从 flet 那边抄的，逐条有守卫（`tests/test_gateway_contract_single_source.py`）：
///
/// - **没有"深度"可选**：分级是检测自己做的（浅遍扫全库、值得花钱的源再补验正文），
///   档位由判据定、不由点击定——第六十二轮那个下拉已随自动分档撤掉，这边不能又长回来。
/// - 开始/停止的**可用性**是反的：跑着的时候开始禁用、停止启用（`check_view.py:102-107`）。
/// - 「尚未检测」是初始值，回放与收尾都不许拿空串盖掉它。
/// - 结果行的标题就是 `source_id`（书源 url 原样），副标题是 `状态 · 摘要`。
/// - 行按 `source_id` 覆盖，不追加第二条（同源重测改的是同一行）。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/events/event_listener.dart';
import '../../../core/events/event_names.dart';
import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../data/check_models.dart';
import '../domain/check_notifier.dart';

class CheckScreen extends ConsumerStatefulWidget {
  const CheckScreen({super.key});

  @override
  ConsumerState<CheckScreen> createState() => _CheckScreenState();
}

class _CheckScreenState extends ConsumerState<CheckScreen> {
  String _status = '';

  /// 一轮结束的那个判断只看一次（`done==total` 会在收尾那几拍连续为真）。
  bool _finishedSeen = false;

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(checkProvider);
    final notifier = ref.read(checkProvider.notifier);
    final snapshot = state.value ?? _blank;

    listenToEngineEvents(
      ref,
      <String>[EngineEventNames.checkFinished],
      (event) {
        notifier.onCheckFinished(event);
        final planned = event.payload['total'];
        final done = event.payload['done'];
        // 收尾那一句要读快照：事件的 payload 里只有逐源结论，没有 usable/blocked 的账。
        if (!_finishedSeen &&
            planned is num &&
            done is num &&
            planned.toInt() > 0 &&
            done.toInt() >= planned.toInt()) {
          _finishedSeen = true;
          notifier.finish().then((_) => setState(() {}));
        }
      },
      onStreamError: (error) => setState(() => _status = '事件流断开：$error'),
    );

    final running = snapshot.running;
    return Scaffold(
      appBar: AppBar(
        title: const Text('书源检测'),
        actions: <Widget>[
          IconButton(
            key: const Key('check.refresh'),
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () => notifier.finish().then((_) => setState(() {})),
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          FailureBanner(stream: notifier.failures),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 6),
            child: Wrap(
              spacing: 8,
              runSpacing: 6,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: <Widget>[
                FilledButton.icon(
                  key: const Key('check.start'),
                  // 在跑就不派第二遍全量联网：那一条在服务侧是同一轮，
                  // 但界面上"点了没反应"必须有个说法（flet 回"检测已在进行中"）。
                  onPressed: running
                      ? null
                      : () {
                          _finishedSeen = false;
                          setState(() => _status = '');
                          notifier
                              .start()
                              .then((text) => setState(() => _status = text));
                        },
                  icon: const Icon(Icons.play_arrow),
                  label: const Text('开始检测'),
                ),
                OutlinedButton.icon(
                  key: const Key('check.stop'),
                  onPressed: running
                      ? () => notifier.cancel().then(
                            (text) => setState(() => _status = text))
                      : null,
                  icon: const Icon(Icons.stop),
                  label: const Text('停止'),
                ),
              ],
            ),
          ),
          LinearProgressIndicator(value: notifier.progressValue),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 6, 12, 6),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(_summaryText(snapshot, running),
                  style: Theme.of(context).textTheme.bodySmall),
            ),
          ),
          const Divider(height: 1),
          Expanded(child: _Rows(rows: snapshot.results)),
          StateBar(
            message: _status,
            state: state,
            failedText: '读取检测进度失败',
            loadingText: '正在读取检测进度…',
            dataText: '',
          ),
        ],
      ),
    );
  }

  /// 中间那行文案的优先级（照 flet：`set_running` / `set_progress` / `set_summary`
  /// 打的都是同一个 `_summary` 槽，谁最后写谁说话）。
  ///
  /// 这里只能给它一个确定顺序，因为 Dart 侧的"最后写"取决于事件到拍的时机：
  /// 跑着 → 进行中的计数；跑完 → 收尾那一句；空串（还没结论）→ 守住初始的「尚未检测」。
  String _summaryText(CheckSnapshot snapshot, bool running) {
    if (running) {
      return '检测进行中：${ref.read(checkProvider.notifier).doneCount}'
          '/${snapshot.total > 0 ? snapshot.total : ref.read(checkProvider.notifier).total}';
    }
    final line = checkSummaryLine(snapshot);
    if (line.isNotEmpty) return line;
    final notifier = ref.read(checkProvider.notifier);
    if (notifier.doneCount > 0 && notifier.total > 0) {
      return '检测进度：${notifier.doneCount}/${notifier.total}';
    }
    return '尚未检测';
  }
}

/// 逐源结果行。**不在屏上也建**（`ListView(children:)` 而不是 `builder`）：
/// 检测跑完时用户多半在别的页签上，这一屏是 `Offstage` 留着的（见
/// `core/widgets/tabbed_group.dart` 第 2 条），切回来要当场看见已经跑完的那几百行，
/// 而不是等它重新滚进视口才补建。行数上限是一轮扫描的源数（现场 920），
/// 一行在这里只有三个文本 + 一个图标，全建的钱 G2-F 会量出来。
class _Rows extends StatelessWidget {
  const _Rows({required this.rows});

  final List<CheckRow> rows;

  @override
  Widget build(BuildContext context) {
    if (rows.isEmpty) {
      return const Center(
        child: Text('还没有一条检测结果',
            style: TextStyle(color: Colors.grey)),
      );
    }
    return ListView(
      children: <Widget>[
        for (final row in rows) _CheckRowTile(key: ValueKey(row.sourceId), row: row),
      ],
    );
  }
}

class _CheckRowTile extends StatelessWidget {
  const _CheckRowTile({required this.row, super.key});

  final CheckRow row;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = switch (row.tone) {
      CheckStateTone.usable => Colors.green,
      CheckStateTone.blocked => scheme.onSurfaceVariant,
      CheckStateTone.pending => Colors.orange,
    };
    final icon = switch (row.tone) {
      CheckStateTone.usable => Icons.check_circle,
      CheckStateTone.blocked => Icons.block,
      CheckStateTone.pending => Icons.info_outline,
    };
    return ListTile(
      dense: true,
      leading: Icon(icon, color: color),
      title: Text(row.sourceId, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text('${row.label} · ${row.summary}',
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: Theme.of(context).textTheme.bodySmall),
    );
  }
}

const CheckSnapshot _blank = CheckSnapshot(
  running: false,
  cancelled: false,
  total: 0,
  depth: '',
  tiering: CheckTiering.empty,
  deepQueueCount: 0,
  summary: null,
  results: <CheckRow>[],
);
