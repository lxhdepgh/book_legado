/// 分类行：`browse.categories` 的一条（`field_names` = `title` / `url`）。
///
/// [raw] 必须原样留着：`browse.books` 的入参 `category` 是**一个对象**，
/// action 表的说明写的是"用 browse.categories 回的那一条原样传回"
/// （`app/gateway/methods.py:96-97`）。挑两个字段自己拼一个 category 字典，
/// 服务端将来在分类上多带一格（比如 `filter`）就会在这里静默丢掉。
library;

class CategoryRow {
  const CategoryRow({
    required this.title,
    required this.url,
    required this.raw,
  });

  final String title;

  /// 分类的 url，`{{page}}` 占位还在原子里没展开——翻页是引擎的活，不是界面的活。
  final String url;

  final Map<String, Object?> raw;

  String get label => title;

  factory CategoryRow.fromJson(Map<String, Object?> json) {
    final asMap = json.map((key, value) => MapEntry(key.toString(), value));
    return CategoryRow(
      title: '${asMap['title'] ?? ''}',
      url: '${asMap['url'] ?? ''}',
      raw: Map.unmodifiable(asMap),
    );
  }

  @override
  String toString() => 'CategoryRow($title)';
}
