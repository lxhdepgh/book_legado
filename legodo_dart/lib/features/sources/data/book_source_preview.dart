/// 书源文件导入前的预览：只解析、不落库（对应 `online_import_service.py` 的
/// `preview_book_source_text` + `import_view.py` 里那三行渲染文案）。
///
/// 为什么要有这一步：flet 那边选完文件先给一张卡（可导入几条、都是哪些分组、
/// 追加还是替换），用户看清了再点「确认导入」。分层重构把整块预览删掉后变成
/// "选完文件直接落库"，于是「替换」这个不可撤销的动作只需要一次文件点击。
///
/// 文案逐字取自 flet：`可导入 N 条`、`跳过 N 条缺书源地址`、`… 共 N 个分组`、
/// 以及"一条都导不进来"那句直说——那句是给用户判断"是不是选错文件了"用的，
/// 换成"可导入 0 条"就成了一张需要再点一次确认的空卡。
library;

import 'dart:convert';

/// 一个分组里的条数（[name] 是书源分组名，缺分组归到 `未分组`）。
typedef GroupCount = ({String name, int count});

class BookSourcePreview {
  const BookSourcePreview({
    required this.total,
    required this.valid,
    required this.broken,
    required this.groups,
    required this.error,
  });

  /// 读不了整份文件（JSON 语法错、顶层形状不对）时其余字段全 0、[error] 非空。
  factory BookSourcePreview.parse(String text) {
    Object? decoded;
    try {
      decoded = jsonDecode(text);
    } on FormatException catch (exc) {
      return BookSourcePreview._onlyError('JSON 解析失败：$exc');
    }
    final items = switch (decoded) {
      final Map<Object?, Object?> map => <Object?>[map],
      final List<Object?> list => list,
      _ => null,
    };
    if (items == null) {
      return BookSourcePreview._onlyError('书源 JSON 顶层应为数组或对象');
    }

    final groupCounts = <String, int>{};
    int valid = 0;
    int broken = 0;
    for (final item in items) {
      final source = item is Map<Object?, Object?> ? item : null;
      final url = source == null ? '' : '${source['bookSourceUrl'] ?? ''}'.trim();
      if (url.isEmpty) {
        broken += 1;
        continue;
      }
      valid += 1;
      final group = '${source!['bookSourceGroup'] ?? ''}'.trim();
      final name = group.isEmpty ? '未分组' : group;
      groupCounts[name] = (groupCounts[name] ?? 0) + 1;
    }

    final groups = groupCounts.entries
        .map((entry) => (name: entry.key, count: entry.value))
        .toList(growable: false)
      ..sort((a, b) => b.count != a.count
          ? b.count - a.count
          : a.name.compareTo(b.name));

    if (valid == 0) {
      // 一条都导不进来（典型：把 TXT 目录规则或别的 JSON 当成书源选了）。
      return BookSourcePreview(
        total: items.length,
        valid: 0,
        broken: broken,
        groups: const <GroupCount>[],
        error: '这 ${items.length} 项里没有带 bookSourceUrl 的书源，是不是选错了文件？',
      );
    }
    return BookSourcePreview(
      total: items.length,
      valid: valid,
      broken: broken,
      groups: groups,
      error: '',
    );
  }

  factory BookSourcePreview._onlyError(String error) => BookSourcePreview(
        total: 0,
        valid: 0,
        broken: 0,
        groups: const <GroupCount>[],
        error: error,
      );

  final int total;
  final int valid;
  final int broken;
  final List<GroupCount> groups;
  final String error;

  bool get canImport => error.isEmpty;

  /// 预览卡上那一行数；[blocked] 时给的是"这个文件不能导入"那句。
  String get summaryLine => canImport
      ? '可导入 $valid 条${broken > 0 ? '，跳过 $broken 条缺书源地址' : ''}'
      : '这个文件不能导入：$error';

  /// 分组分布：前 8 组，超出补一句"共 N 个分组"（flet 的口径）。
  String get groupsLine {
    if (!canImport || groups.isEmpty) return canImport ? '（无分组信息）' : '';
    final shown = groups
        .take(8)
        .map((group) => '${group.name} ${group.count} 条')
        .join('、');
    return groups.length > 8 ? '$shown… 共 ${groups.length} 个分组' : shown;
  }
}
