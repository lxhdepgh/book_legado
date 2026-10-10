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

/// 本地书的伪源常量（对 `app/services/content/local_book_service.py::LOCAL_BOOK_SOURCE_URL`）。
///
/// 为什么在客户端也留一份：flet 那侧"这行是不是本地书"是控制器算好后塞进
/// `record["local"]` 的，而那一格**不在协议面上**（`bookshelf.list` 的 36 个键里没有它）。
/// 这里不是另起一套判据——`is_local_source_url` 的本体就是与这一个常量的**精确相等**，
/// 读的是同一份数据里的同一个字段。字面量由跨语言守卫
/// `tests/test_gateway_contract_single_source.py::test_local_book_source_url_is_one_constant_two_places`
/// 钉住：那边改了常量而这边没跟上，就是红。
const String kLocalBookSourceUrl = 'localsource://local_book';

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

  /// 这一行是不是**本地书**（导入的文件，没有规则源）。
  ///
  /// 用的地方只有一处：本地书没有"换一个源"可做（换源入口要收起，同
  /// `bookshelf_view.py::build_manage_dialog` 的 `if not record.get("local")`）。
  /// 判据与 flet 同一条精确相等，不认前缀——`file://` 那种"书地址像本地"的形状
  /// 在阅读域另有认领路径（`_local_source_for`），不在这一格。
  bool get isLocal => sourceUrl == kLocalBookSourceUrl;

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
