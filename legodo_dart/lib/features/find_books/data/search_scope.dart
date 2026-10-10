/// 搜索范围那一条：`search.scopes` 回的样子（`{scope, count}`）。
///
/// 两个哨兵是**服务端定义**的字符串，不是这一屏发明的：
/// `reading_service.py` 的 `_SCOPE_ALL` / `_SCOPE_UNGROUPED`
/// （`tests/test_gateway_contract_single_source.py` 拿这两个常量逐项比对本文件的字面量）。
/// 拼错的后果不是报错——是"未分组"变成"一个都不搜"或"全部都搜"，
/// 因为分组名里确实没有 `__ungrouped`（少两个下划线）这种组。
///
/// **这张表不是客户端算的**：范围有哪些、每个范围里几条源，全在服务端
/// `ReadingService.scope_table` 一份算法里（它同时喂 flet 那个对话框）。
/// 客户端自己按 `enabled`+`searchUrl` 数一遍，就是同一规则的第二份实现——
/// 改一处要记得改两处，而那两处不会同时改。所以 [SearchScope] 只有线上值与数，
/// 连分组名的切分都不在这里做。
library;

const String kScopeAll = '__all__';
const String kScopeUngrouped = '__ungrouped__';

/// 范围下拉/弹窗里的一条：线上值 + 这个范围里有多少条**可搜**源。
class SearchScope {
  const SearchScope({required this.value, required this.sourceCount});

  final String value;

  /// **服务端口径的可搜源数**：停用与没配搜索规则的源不进来，
  /// 这解释了"为什么这个组只有 3 源而我记得有 9 条"。
  final int sourceCount;

  bool get isSentinel => value == kScopeAll || value == kScopeUngrouped;

  /// 哨兵的展示名在客户端，线上值在服务端：服务端发 "全部书源" 的话，
  /// `search.start` 的 `scope` 就没法原样传回了。
  String get label => switch (value) {
        kScopeAll => '全部书源',
        kScopeUngrouped => '未分组',
        _ => value,
      };

  /// 弹窗里那一行的文案：`分组名（N 源）`（flet `find_books_controller.py` 同一句）。
  String get rowLabel => '$label（$sourceCount 源）';

  factory SearchScope.fromJson(Map<String, Object?> json) => SearchScope(
        value: '${json['scope'] ?? ''}',
        sourceCount: (json['count'] as num?)?.toInt() ?? 0,
      );

  @override
  String toString() => 'SearchScope($value, $sourceCount 源)';
}
