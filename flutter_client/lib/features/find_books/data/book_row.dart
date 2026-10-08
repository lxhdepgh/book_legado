/// 找书那一屏的行：搜索结果与分类书单**共用一个模型**。
///
/// 这不是省事，是服务端定的形状：`ReadingService.search_in_scope` 与 `browse_books`
/// 都按 `_book_item_dict` 归一（`reading_service.py:904-906` 明写"同一批结果走两条路
/// 回客户端却有两种字段，客户端就得写两套 fromJson"），
/// 两边的 `field_names` 也确实是同一套 35 个键
/// （`tests/gateway/golden/browse.books.json` 与 `search.start.json` 的 `event_sample`）。
///
/// 视图读的三格照 `app/views/reading/find_books_view.py:207-220`：
/// 标题 = `name`（**没有兜底**，flet 那边空串就画空串）；
/// 副标题 = `作者 · 来源名`，用 ` · ` 连、缺一段就少一段。
library;

class BookRow {
  const BookRow({
    required this.name,
    required this.author,
    required this.intro,
    required this.url,
    required this.origin,
    required this.originName,
    required this.coverUrl,
    required this.raw,
  });

  final String name;
  final String author;

  /// 简介：详情弹窗那一格用（`book_intro` 可能带 `<usehtml>` 标记，
  /// 渲染契约见记忆 `intro-render-contract`——G2 这一屏只画原文，不解析标记）。
  final String intro;

  /// 书在原站点的 url。
  final String url;

  /// 它来自哪一条书源（`origin` = 书源 url）：入架要按它找回书源
  /// （`find_books_controller.py:354`），所以这一格不能只当显示字段用。
  final String origin;
  final String originName;
  final String coverUrl;

  final Map<String, Object?> raw;

  /// 行标题。flet 那边就是 `book.get("name","")`，空串不替它编一个"未命名"。
  String get title => name;

  String get subtitle =>
      <String>[if (author.isNotEmpty) author, if (originName.isNotEmpty) originName]
          .join(' · ');

  /// 行键：同一次搜索里同一本书可能从多个来源各回一次，`origin|url` 才唯一。
  String get identityKey => '$origin|$url';

  factory BookRow.fromJson(Map<String, Object?> json) {
    final asMap = json.map((key, value) => MapEntry(key.toString(), value));
    return BookRow(
      name: '${asMap['name'] ?? ''}',
      author: '${asMap['author'] ?? ''}',
      intro: '${asMap['intro'] ?? ''}',
      url: '${asMap['url'] ?? ''}',
      origin: '${asMap['origin'] ?? ''}',
      originName: '${asMap['origin_name'] ?? ''}',
      coverUrl: '${asMap['cover_url'] ?? ''}',
      raw: Map.unmodifiable(asMap),
    );
  }

  @override
  String toString() => 'BookRow($name, $author, $originName)';
}
