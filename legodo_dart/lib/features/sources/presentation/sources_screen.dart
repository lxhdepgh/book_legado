/// 书源屏：`source.list` 那 920 行的形状、筛选与批量动作（G2 判据的主角）。
///
/// 文案与投影逐条对照 flet 那屏（`app/views/source/book_source_list_view.py` +
/// `app/controllers/source/book_source_list_controller.py`），
/// 由 `tests/test_gateway_contract_single_source.py` 从 flet 源码抽字面量比对本文件。
///
/// 三条不是风格的选择：
///
/// 1. **行用 `ListView.builder`**：R4 界面面那组读数（滚动每拍下发 390 ms / 空列表底价
///    3 ms）量的是"920 行全建出来"的钱。Flutter 侧的对照臂要的是同一件事——
///    一次只建看得见的那几行，所以 G2-F 那一发如果比 flet 快，归因归的是虚拟化，
///    而不是"Dart 比 Python 快"。
/// 2. **筛选不发 RPC**：三个筛选都在最近一次全量快照上算（同 domain 那条注释）。
/// 3. **删除必须二次确认**，且批量删除那一句把带走的名字列出来（flet 的口径）。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/events/event_listener.dart';
import '../../../core/events/event_names.dart';
import '../../../core/rpc/envelope.dart';
import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../../../core/widgets/tabbed_group.dart';
import '../data/source_record.dart';
import '../domain/sources_notifier.dart';
import 'import_dialog.dart';

class SourcesScreen extends ConsumerStatefulWidget {
  const SourcesScreen({super.key});

  @override
  ConsumerState<SourcesScreen> createState() => _SourcesScreenState();
}

class _SourcesScreenState extends ConsumerState<SourcesScreen> {
  final TextEditingController _query = TextEditingController();
  SourceFilters _filters = const SourceFilters();
  Set<String> _selected = <String>{};
  String _status = '';

  @override
  void initState() {
    super.initState();
    _query.addListener(_onQueryChanged);
  }

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  void _onQueryChanged() =>
      setState(() => _filters = _filters.copyWith(query: _query.text));

  void _onGroupChanged(String? group) =>
      setState(() => _filters = _filters.copyWith(group: group ?? ''));

  void _onStateChanged(String? state) =>
      setState(() => _filters = _filters.copyWith(state: state ?? ''));

  /// 选中/取消一条（按 `bookSourceUrl`，与 flet 的 `_selected_urls` 同源）。
  void _onSelected(String url, bool checked) => setState(() {
        if (checked) {
          _selected = {..._selected, url};
        } else {
          _selected = {..._selected}..remove(url);
        }
      });

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(sourcesProvider);
    // 书源库变了就重取：这一条正是 `SourceChangedEvent` 的用处（flet 那边同样只订阅它）。
    listenToEngineEvents(ref, <String>[EngineEventNames.sourceChanged], (_) {
      ref.read(sourcesProvider.notifier).reload();
    }, onStreamError: (error) => setState(() => _status = '事件流断开：$error'));

    final records = state.value ?? const <SourceRecord>[];
    final filtered = applyFilters(records, _filters);
    // 选择集里可能有已经不在快照里的 url（刚被删掉的那几条）：报数与批量都只算还在的。
    final alive = filtered.map((r) => r.url).toSet();
    final selectedInView = _selected.where(alive.contains).toList()..sort();

    return Scaffold(
      appBar: AppBar(
        title: const Text('书源'),
        actions: <Widget>[
          IconButton(
            key: const Key('sources.refresh'),
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: state.isLoading
                ? null
                : () => ref.read(sourcesProvider.notifier).reload(),
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          FailureBanner(stream: ref.read(sourcesProvider.notifier).failures),
          _ToolBar(
            controller: _query,
            groups: groupsOf(records),
            filters: _filters,
            onGroupChanged: _onGroupChanged,
            onStateChanged: _onStateChanged,
            onSelectAll: filtered.isEmpty
                ? null
                : () => setState(() => _selected = filtered.map((r) => r.url).toSet()),
            // 「检测」是同组的另一签：切页签的动作问组的把手，不在这里拼路由
            // （flet 那边控制器只发 `on_open_check` 这个动作，目的地由导航外壳给）。
            onOpenCheck: () =>
                GroupTabsScope.maybeOf(context)?.select('检测'),
            // 空列表那句话（「点『导入』从 JSON 文件或 URL 添加」）指的就是这颗按钮：
            // 没有它，那句话是在承诺一个不存在的入口。
            onImport: () => showSourceImportDialog(context,
                existingCount: records.length),
          ),
          if (selectedInView.isNotEmpty)
            _BatchBar(
              count: selectedInView.length,
              onClear: () => setState(() => _selected = <String>{}),
              onBatch: (enabled) => _batchSetEnabled(selectedInView, enabled),
              onDelete: () => _confirmBatchDelete(selectedInView, records),
            ),
          Expanded(child: _body(state, filtered)),
          StateBar(
            message: _status,
            state: state,
            failedText: '加载书源失败',
            loadingText: '正在加载书源…',
            dataText: SourceStats.of(records).line,
          ),
        ],
      ),
    );
  }

  Widget _body(AsyncValue<List<SourceRecord>> state, List<SourceRecord> filtered) {
    // 失败态要在原位画一句能重试的话，**不许画成"共 0 条"**（记忆 §4.5 那条不对称）。
    // 用 sealed 模式而不是 `is` + `.error`：后者在这里不收窄，`error` 会退化成 `Object?`。
    if (state case final AsyncError<List<SourceRecord>> failure) {
      return _LoadFailure(
          error: failure.error,
          onRetry: () => ref.read(sourcesProvider.notifier).reload());
    }
    if (state.value == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (filtered.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Text('书源列表为空，点「导入」从 JSON 文件或 URL 添加',
              textAlign: TextAlign.center),
        ),
      );
    }
    return RefreshIndicator(
      onRefresh: () => ref.read(sourcesProvider.notifier).reload(),
      child: ListView.builder(
        key: const Key('sources.list'),
        itemCount: filtered.length,
        itemBuilder: (context, index) => SourceRow(
          // 键用 url：下标键会让"改一条源"把旧的勾选状态带到另一条源上。
          key: ValueKey(filtered[index].url),
          record: filtered[index],
          checked: _selected.contains(filtered[index].url),
          onSelected: (checked) => _onSelected(filtered[index].url, checked),
          onToggled: (enabled) => _toggleEnabled(filtered[index], enabled),
          onDeleted: () => _confirmDelete(filtered[index]),
        ),
      ),
    );
  }

  // ------------------------------------------------------------------ #
  // 动作
  // ------------------------------------------------------------------ #
  Future<void> _toggleEnabled(SourceRecord record, bool enabled) async {
    final url = record.url;
    try {
      final ok = await ref.read(engineRepositoryProvider).setSourceEnabled(url, enabled);
      if (!ok) setState(() => _status = '书源已不存在，切换未生效');
    } on EngineFailure catch (exc) {
      setState(() => _status = '切换启用状态失败：${exc.message}');
      return;
    }
    await ref.read(sourcesProvider.notifier).reload();
  }

  Future<void> _batchSetEnabled(List<String> urls, bool enabled) async {
    final int changed;
    try {
      changed = await ref.read(engineRepositoryProvider).batchSetEnabled(urls, enabled);
    } on EngineFailure catch (exc) {
      setState(() => _status = '批量${enabled ? '启用' : '停用'}失败：${exc.message}');
      return;
    }
    setState(() {
      _selected = <String>{};
      _status = '已${enabled ? '启用' : '停用'} $changed 条';
    });
    await ref.read(sourcesProvider.notifier).reload();
  }

  Future<void> _confirmDelete(SourceRecord record) async {
    final name = record.title;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('删除书源「$name」？'),
        content: const Text('删除后该书源的所有规则将被移除，此操作不可撤销。'),
        actions: <Widget>[
          TextButton(
              key: const Key('sources.cancelDelete'),
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消')),
          FilledButton.tonalIcon(
            key: const Key('sources.confirmDelete'),
            onPressed: () => Navigator.pop(context, true),
            icon: const Icon(Icons.delete_forever),
            label: const Text('删除'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      final ok = await ref.read(engineRepositoryProvider).deleteSource(record.url);
      setState(() => _status = ok ? '已删除：$name' : '未找到书源：$name');
    } on EngineFailure catch (exc) {
      setState(() => _status = '删除失败：${exc.message}');
      return;
    }
    await ref.read(sourcesProvider.notifier).reload();
  }

  Future<void> _confirmBatchDelete(
      List<String> urls, List<SourceRecord> records) async {
    final names = _namesOf(urls, records);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('删除 ${urls.length} 条书源？'),
        content: Text('将删除：$names\n此操作不可撤销。'),
        actions: <Widget>[
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消')),
          FilledButton.icon(
            key: const Key('sources.confirmBatchDelete'),
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
                backgroundColor: Theme.of(context).colorScheme.error),
            icon: const Icon(Icons.delete_forever),
            label: const Text('确认删除'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final int removed;
    try {
      removed = await ref.read(engineRepositoryProvider).batchDelete(urls);
    } on EngineFailure catch (exc) {
      setState(() => _status = '批量删除失败：${exc.message}');
      return;
    }
    setState(() {
      _selected = <String>{};
      _status = '已删除 $removed 条书源';
    });
    await ref.read(sourcesProvider.notifier).reload();
  }

  /// 前三个名字（没有名字的源退回 url），超出三条补 ` 等 N 条`
  /// （`book_source_list_controller.py:389-401`：连接符是 `、`，不是 `，`）。
  static String _namesOf(List<String> urls, List<SourceRecord> records) =>
      namesForBatchDeletion(urls, records);
}

/// 批量删除确认里那串名字：前三条（没名字的源退回 url），超出三条补 ` 等 N 条`。
///
/// 放在顶层而不是 State 的私有静态里，是为了让
/// `tests/gateway/projection/source.batch_delete.names.json` 能直接喂给它——
/// 那张表是 flet 的 `_names_of` 跑同一批 url 得到的，比对不许隔一层 UI。
/// （`book_source_list_controller.py:389-401`：连接符是 `、`，不是 `，`。）
String namesForBatchDeletion(List<String> urls, List<SourceRecord> records) {
  final byUrl = <String, SourceRecord>{
    for (final record in records) record.url: record,
  };
  final names = urls.take(3).map((url) {
    final record = byUrl[url];
    return record == null ? url : (record.name.isNotEmpty ? record.name : url);
  }).toList();
  final text = names.join('、');
  return urls.length > 3 ? '$text 等 ${urls.length} 条' : text;
}

extension on SourceFilters {
  SourceFilters copyWith({String? query, String? group, String? state}) =>
      SourceFilters(
          query: query ?? this.query,
          group: group ?? this.group,
          state: state ?? this.state);
}

class _ToolBar extends StatelessWidget {
  const _ToolBar({
    required this.controller,
    required this.groups,
    required this.filters,
    required this.onGroupChanged,
    required this.onStateChanged,
    required this.onSelectAll,
    required this.onOpenCheck,
    required this.onImport,
  });

  final TextEditingController controller;
  final List<String> groups;
  final SourceFilters filters;
  final ValueChanged<String?> onGroupChanged;
  final ValueChanged<String?> onStateChanged;
  final VoidCallback? onSelectAll;
  final VoidCallback onOpenCheck;
  final VoidCallback onImport;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 6),
      child: Wrap(
        spacing: 8,
        runSpacing: 6,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: <Widget>[
          SizedBox(
            width: 260,
            child: TextField(
              key: const Key('sources.search'),
              controller: controller,
              decoration: const InputDecoration(
                  isDense: true,
                  border: OutlineInputBorder(),
                  hintText: '搜索书源名称 / URL / 分组 / 备注'),
            ),
          ),
          SizedBox(
            width: 150,
            child: DropdownButtonHideUnderline(
              child: DropdownButton<String>(
                key: const Key('sources.groupFilter'),
                isExpanded: true,
                hint: const Text('全部分组'),
                value: filters.group.isEmpty ? null : filters.group,
                // 「全部分组」那一格靠 hint 而不是塞一个 value:'' 的 item：
                // DropdownButton 的 `value: null` 就是"显示 hint"，
                // 而 items 里再放一条 value:'' 会让选中后那行文字重复一次。
                items: <DropdownMenuItem<String>>[
                  for (final group in groups)
                    DropdownMenuItem(value: group, child: Text(group)),
                ],
                onChanged: onGroupChanged,
              ),
            ),
          ),
          SizedBox(
            width: 130,
            child: DropdownButtonHideUnderline(
              child: DropdownButton<String>(
                key: const Key('sources.stateFilter'),
                isExpanded: true,
                hint: const Text('全部状态'),
                value: filters.state.isEmpty ? null : filters.state,
                items: const <DropdownMenuItem<String>>[
                  DropdownMenuItem(value: 'enabled', child: Text('仅启用')),
                  DropdownMenuItem(value: 'disabled', child: Text('仅停用')),
                ],
                onChanged: onStateChanged,
              ),
            ),
          ),
          Tooltip(
            message: '选中当前筛选结果中的全部书源',
            child: TextButton.icon(
              key: const Key('sources.selectAll'),
              onPressed: onSelectAll,
              icon: const Icon(Icons.checklist),
              label: const Text('全选筛选结果'),
            ),
          ),
          OutlinedButton.icon(
            key: const Key('sources.import'),
            onPressed: onImport,
            icon: const Icon(Icons.file_open),
            label: const Text('导入'),
          ),
          OutlinedButton.icon(
            key: const Key('sources.openCheck'),
            onPressed: onOpenCheck,
            icon: const Icon(Icons.fact_check_outlined),
            label: const Text('检测'),
          ),
        ],
      ),
    );
  }
}

class _BatchBar extends StatelessWidget {
  const _BatchBar({
    required this.count,
    required this.onClear,
    required this.onBatch,
    required this.onDelete,
  });

  final int count;
  final VoidCallback onClear;
  final ValueChanged<bool> onBatch;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 2, 12, 2),
        child: Wrap(
          spacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            Text('已选择 $count 项'),
            TextButton(
                key: const Key('sources.batchEnable'),
                onPressed: () => onBatch(true),
                child: const Text('启用')),
            TextButton(
                key: const Key('sources.batchDisable'),
                onPressed: () => onBatch(false),
                child: const Text('禁用')),
            TextButton(
                key: const Key('sources.batchDelete'),
                onPressed: onDelete,
                style: TextButton.styleFrom(
                    foregroundColor: Theme.of(context).colorScheme.error),
                child: const Text('删除')),
            TextButton(onPressed: onClear, child: const Text('取消选择')),
          ],
        ),
      ),
    );
  }
}

/// 一条书源行：勾选 + 标题/副标题 + 删除 + 启用开关。
///
/// 停用那一格**只把标题变灰**，不加"已停用"那种文字徽标——flet 那屏就是这个形状
/// （`book_source_list_view.py:223` 的 `color=GREY_400`），加徽标会让两边的行数一样
/// 而内容不一样，R4 的对照就落在一个 flet 根本不画的串上。
class SourceRow extends StatelessWidget {
  const SourceRow({
    required this.record,
    required this.checked,
    required this.onSelected,
    required this.onToggled,
    required this.onDeleted,
    super.key,
  });

  final SourceRecord record;
  final bool checked;
  final ValueChanged<bool> onSelected;
  final ValueChanged<bool> onToggled;
  final VoidCallback onDeleted;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return ListTile(
      dense: true,
      leading: Tooltip(
        message: '选择以批量操作',
        child: Checkbox(
          value: checked,
          onChanged: (value) => onSelected(value ?? false),
        ),
      ),
      title: Text(record.title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: record.enabled
              ? null
              : TextStyle(color: scheme.onSurfaceVariant)),
      subtitle: record.subtitle.isEmpty
          ? null
          : Text(record.subtitle,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodySmall),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          IconButton(
            icon: const Icon(Icons.delete_outline),
            tooltip: '删除书源',
            onPressed: onDeleted,
          ),
          Tooltip(
            message: '启用/停用',
            child: Switch(value: record.enabled, onChanged: onToggled),
          ),
        ],
      ),
      onTap: () => onSelected(!checked),
    );
  }
}

class _LoadFailure extends StatelessWidget {
  const _LoadFailure({required this.error, required this.onRetry});

  final Object error;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Text('加载书源失败', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Text('$error',
                  textAlign: TextAlign.center,
                  maxLines: 6,
                  overflow: TextOverflow.ellipsis),
              const SizedBox(height: 12),
              FilledButton.icon(
                  key: const Key('sources.retry'),
                  onPressed: onRetry,
                  icon: const Icon(Icons.refresh),
                  label: const Text('重试')),
            ],
          ),
        ),
      );
}
