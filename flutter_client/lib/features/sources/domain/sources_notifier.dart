/// 书源那一屏的数据与派生量（§6.2：`AsyncNotifier`，不裸 `setState`）。
///
/// 三条值得写在代码里的规矩，都来自 flet 那边已经定下的行为
/// （`app/controllers/source/book_source_list_controller.py`）：
///
/// 1. **过滤与统计不再打服务**：一次 `source.list` 是全量快照，
///    搜索/分组/状态那三个筛选都在最近这一份上算（`:121-147`）。
///    每改一个字就打一发 `source.list`（920 行投影后仍 360 KB）是这个迁移最不该带过去的习惯。
/// 2. 刷新失败留着旧数据（同书架那一屏那条）。
/// 3. 统计那一句报的是**全量**的四格（total/enabled/disabled/groups），
///    不是筛选后的条数——flet 的状态行就是这个口径，两边不一致 R4 的对照就没法读。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/envelope.dart';
import '../data/source_record.dart';

final AsyncNotifierProvider<SourcesNotifier, List<SourceRecord>> sourcesProvider =
    AsyncNotifierProvider<SourcesNotifier, List<SourceRecord>>(
  SourcesNotifier.new,
  retry: (_, _) => null,
);

class SourcesNotifier extends AsyncNotifier<List<SourceRecord>> {
  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  @override
  Future<List<SourceRecord>> build() {
    ref.onDispose(() => unawaited(_failures.close()));
    return _fetch();
  }

  Future<List<SourceRecord>> _fetch() async =>
      ref.read(engineRepositoryProvider).listSources();

  Stream<EngineFailure> get failures => _failures.stream;

  /// 刷新：旧行留在原位，失败只由 [failures] 出声（理由同书架那一屏）。
  Future<void> reload() async {
    final previous = state;
    try {
      state = AsyncValue<List<SourceRecord>>.data(await _fetch());
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      state = previous;
    } catch (exc) {
      _failures.add(
          EngineFailure(EngineFailureKind.engine, '$exc', action: 'source.list'));
      state = previous;
    }
  }
}

/// 状态行那四个数（`book_source_list_controller.py:117-119` 的 `set_stats`）。
class SourceStats {
  const SourceStats({
    required this.total,
    required this.enabled,
    required this.disabled,
    required this.groupCount,
  });

  final int total;
  final int enabled;
  final int disabled;
  final int groupCount;

  /// `共 N 条 | 启用 N | 停用 N | N 个分组`——分隔符是 `" | "`，
  /// 而且**只有第一格带"条"**（flet 那屏 `:188-190` 就是这个不对称，照搬）。
  String get line =>
      '共 $total 条 | 启用 $enabled | 停用 $disabled | $groupCount 个分组';

  static SourceStats of(List<SourceRecord> records) {
    final enabled = records.where((s) => s.enabled).length;
    return SourceStats(
      total: records.length,
      enabled: enabled,
      disabled: records.length - enabled,
      groupCount: groupsOf(records).length,
    );
  }
}

/// 分组下拉的选项表：逗号切开、去空白、**排序去重**（`:110-112`）。
/// 与 `SourceRecord.groups` 共用同一个切法——两处切法不同就会出现
/// "下拉里没有这一组，但行上写着这一组"。
List<String> groupsOf(List<SourceRecord> records) {
  final names = <String>{};
  for (final record in records) {
    names.addAll(record.groups);
  }
  return names.toList()..sort();
}

/// 三个筛选条件。不可变值对象：筛选状态是"这一屏的输入"，不是散落着的三个字符串。
class SourceFilters {
  const SourceFilters({this.query = '', this.group = '', this.state = ''});

  final String query;

  /// 选中的组名（'' = 全部分组）。
  final String group;

  /// '' / 'enabled' / 'disabled'（与 flet 那三个选项的 `value` 同源）。
  final String state;

  bool get isDefault => query.isEmpty && group.isEmpty && state.isEmpty;
}

/// 在快照上算筛选（`:121-147` 的 `_apply_filters`）。
///
/// 三条顺序照 flet：状态 → 分组 → 关键词。顺序在这里不影响结果，
/// 但它影响"筛完 0 条"时用户能不能从筛选条件看出是哪一条闸门关上的。
List<SourceRecord> applyFilters(
    List<SourceRecord> records, SourceFilters filters) {
  final query = filters.query.trim().toLowerCase();
  final group = filters.group.trim();
  return records.where((record) {
    if (filters.state == 'enabled' && !record.enabled) return false;
    if (filters.state == 'disabled' && record.enabled) return false;
    if (group.isNotEmpty && !record.groups.contains(group)) return false;
    if (query.isNotEmpty && !record.haystack.contains(query)) return false;
    return true;
  }).toList(growable: false);
}
