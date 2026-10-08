/// 书架行：`bookshelf.list` 那一发的 data 里的一条。
///
/// 字段名**从真响应里取**，不是照 Python dataclass 手抄：本文件的字段集与
/// `tests/gateway/golden/bookshelf.list.json` 的 `field_names` 由
/// `test/golden_contract_test.dart` 逐项比对，服务端加/改字段而 Dart 没跟上就是红。
///
/// 视图要的三个数（书名 / 作者 / 读到哪儿）与今天 flet 那屏同源：
/// flet 侧的投影是 `app/controllers/bookshelf_controller.py::_to_render_record`
/// （它把进度归一交给 `SourceService.reading_state`），这里 port 的就是那一段归一。
library;

class BookshelfItem {
  const BookshelfItem({
    required this.name,
    required this.author,
    required this.chapterName,
    required this.coverUrl,
    required this.url,
    required this.sourceUrl,
    required this.raw,
  });

  final String name;
  final String author;

  /// "读到哪儿"那一章的标题；从没读过是空串（空串**不是**"未知章名"）。
  final String chapterName;
  final String coverUrl;

  /// 书在原站点的 url（今天它同时是书架行的主键）。
  final String url;
  final String sourceUrl;

  /// 原始行：后续门（换源、进度、缓存）要的那些字段不再逐个复制一遍。
  final Map<String, Object?> raw;

  /// flet 那屏的副标题形状：`作者` 与 `读到 X` 用 ` · ` 连，缺一段就少一段。
  String get subtitle => <String>[
        if (author.isNotEmpty) author,
        if (chapterName.isNotEmpty) '读到 $chapterName',
      ].join(' · ');

  /// ListView 的行键。用 `url` 不用下标：换源会原地改写同一行的 `url`，
  /// 下标键会让 Flutter 把旧行的封面/进度滑移到新书上。
  String get identityKey => url.isNotEmpty ? url : '$sourceUrl/$name';

  factory BookshelfItem.fromJson(Map<String, Object?> json) {
    final asMap = json.map((key, value) => MapEntry(key.toString(), value));
    return BookshelfItem(
      name: '${asMap['name'] ?? ''}',
      author: '${asMap['author'] ?? ''}',
      chapterName: _chapterName(asMap),
      coverUrl: '${asMap['cover_url'] ?? ''}',
      url: '${asMap['url'] ?? ''}',
      sourceUrl: '${asMap['source_url'] ?? asMap['origin'] ?? ''}',
      raw: Map.unmodifiable(asMap),
    );
  }

  /// 进度归一（照 `SourceService.reading_state` 的 setdefault 语义）：
  /// 新数据在 `reading` 子对象里，旧/Legado 数据在 `durChapterTitle`。
  /// 两边都没有 → 空串，视图那一条副标题就不显示"读到"。
  static String _chapterName(Map<String, Object?> row) {
    final reading = row['reading'];
    final map = reading is Map
        ? Map<String, Object?>.from(reading)
        : <String, Object?>{};
    final durTitle = '${row['durChapterTitle'] ?? ''}';
    final durTime = row['durChapterTime'];
    final hasTime = durTime != null && durTime != 0 && durTime != '';
    if (map.isEmpty && durTitle.isEmpty && !hasTime) return '';
    // `setdefault` 只在**键不存在**时补：键在而值是空串，那条空串就是数据。
    final fromReading = map.containsKey('chapter_name')
        ? '${map['chapter_name'] ?? ''}'
        : durTitle;
    return fromReading;
  }

  @override
  String toString() => 'BookshelfItem($name, $author, 读到=$chapterName)';
}
