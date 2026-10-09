/// 试读暂存的那条书架记录：`reader.staged` 的载荷，也是 `reader.close` 里那一格。
///
/// **它和 `bookshelf.list` 的行不是同一套键**（实测：多一格 `in_bookshelf`，
/// 少三格 `reading`/`updateError`/`cacheUpdatedAt`）。少的那三格里最要紧的是
/// `reading`——暂存记录**不带进度子对象**，"读到第几章"只能从 `durChapterTitle`
/// 那几格看（`bookshelf_item.dart` 那套归一在这里没有输入）。
///
/// 键名两套拼法并存是服务端 `find_bookshelf_item` 的历史形状（BookItem 的蛇形字段 +
/// 数据库行的驼形字段），不是这里挑的：`golden_contract_test.dart` 拿真样本逐键核。
///
/// 它的用处只有一个——退出试读时那句"是否将《X》放入书架"（flet
/// `reader_controller.py:1438-1458`），以及转正/放弃那两发点谁。所以这里只留
/// 那句话要读的字段，整份字典留在 [raw] 里给"原样传回"用（`bookshelf.add` 吃字典快照）。
library;

class StagedRecord {
  const StagedRecord({
    required this.name,
    required this.author,
    required this.intro,
    required this.coverUrl,
    required this.url,
    required this.sourceUrl,
    required this.sourceName,
    required this.bookUrl,
    required this.tocUrl,
    required this.kind,
    required this.wordCount,
    required this.groupName,
    required this.inBookshelf,
    required this.durChapterTitle,
    required this.durChapterIndex,
    required this.raw,
  });

  final String name;
  final String author;
  final String intro;
  final String coverUrl;

  /// 书在原站点的地址（蛇形那一格）。
  final String url;

  /// 它来自哪条书源：转正/放弃都按这对坐标找记录（`source_url` + `bookUrl`）。
  final String sourceUrl;
  final String sourceName;

  /// 驼形那一对是同一件事的另一个名字（服务端两套并存），界面只用 [url]/[sourceUrl]，
  /// 这两格留着是为了"少一格"能被契约测试看见。
  final String bookUrl;
  final String tocUrl;

  final String kind;
  final String wordCount;
  final String groupName;

  /// `false` = 还没真正入架（Legado 的 notShelf）。在架的书这一发根本回 null，
  /// 所以这一格为 true 是异常形状，界面别把它当"已经入架"省掉询问。
  final bool inBookshelf;

  /// 读到哪一章。暂存记录没有 `reading` 子对象，进度只在这几格里。
  final String durChapterTitle;
  final int durChapterIndex;

  final Map<String, Object?> raw;

  /// 询问框那句《X》：flet 那边空名兜底成"这本书"（`record.get("name") or "这本书"`）。
  String get displayName => name.isNotEmpty ? name : '这本书';

  factory StagedRecord.fromJson(Map<String, Object?> json) {
    final asMap = <String, Object?>{
      for (final entry in json.entries) entry.key.toString(): entry.value,
    };
    return StagedRecord(
      name: '${asMap['name'] ?? ''}',
      author: '${asMap['author'] ?? ''}',
      intro: '${asMap['intro'] ?? ''}',
      coverUrl: '${asMap['cover_url'] ?? ''}',
      url: '${asMap['url'] ?? ''}',
      sourceUrl: '${asMap['source_url'] ?? ''}',
      sourceName: '${asMap['source_name'] ?? ''}',
      bookUrl: '${asMap['bookUrl'] ?? ''}',
      tocUrl: '${asMap['tocUrl'] ?? ''}',
      kind: '${asMap['kind'] ?? ''}',
      wordCount: '${asMap['word_count'] ?? ''}',
      groupName: '${asMap['groupName'] ?? ''}',
      inBookshelf: asMap['in_bookshelf'] == true,
      durChapterTitle: '${asMap['durChapterTitle'] ?? ''}',
      durChapterIndex: (asMap['durChapterIndex'] as num?)?.toInt() ?? 0,
      raw: Map.unmodifiable(asMap),
    );
  }

  @override
  String toString() => 'StagedRecord($displayName, $sourceName)';
}
