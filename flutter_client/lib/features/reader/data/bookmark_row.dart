/// 一条书签：`reader.bookmarks` 的行。
///
/// [offset] 存的是**章内比例**（`chapter_ratio`，0~1），不是那把尺——服务端这一列
/// 当初就是给"大概翻到哪儿"用的，而 `ReadingPosition.characterOffset` 才是字符偏移。
/// 两者混用会把书签跳点偏半屏（flet 那边 `add_bookmark` 传的就是 ratio，
/// `reader_controller.py:928-933`）。
///
/// 还有一条要对齐的行为：flet 的"跳书签"只按 [chapterIndex] 重开那一章
/// （`_open_index`），**并没有落到 offset 上**。Dart 那屏要么照做，要么两侧
/// 同一张书签表落点不同——后者不是 bug 报告，是界面不一致。
library;

class BookmarkRow {
  const BookmarkRow({
    required this.bookmarkId,
    required this.bookUrl,
    required this.chapterIndex,
    required this.offset,
    required this.summary,
    required this.createdAt,
  });

  /// 删书签那一发吃的就是它（`reader.bookmark_remove` 的 `bookmark_id`）。
  final int bookmarkId;
  final String bookUrl;
  final int chapterIndex;

  /// 章内比例，见文件头那条与字符偏移的分别。
  final double offset;

  /// 书签的显示名：flet 那边塞的是**章名**（`add_bookmark(..., name)`）。
  final String summary;

  /// epoch 秒（带小数：服务端写的是 `time.time()`）。列表按它升序排（那边排的）。
  final double createdAt;

  factory BookmarkRow.fromJson(Map<String, Object?> json) => BookmarkRow(
        bookmarkId: (json['bookmark_id'] as num?)?.toInt() ?? 0,
        bookUrl: '${json['book_url'] ?? ''}',
        chapterIndex: (json['chapter_index'] as num?)?.toInt() ?? 0,
        offset: (json['offset'] as num?)?.toDouble() ?? 0,
        summary: '${json['summary'] ?? ''}',
        createdAt: (json['created_at'] as num?)?.toDouble() ?? 0,
      );

  /// 面板里那一行：`章名`，没名字时退成"第 N 章"（flet 的
  /// `render_bookmarks` 那一格同样有空名兜底）。
  String get label => summary.isNotEmpty
      ? summary
      : '第 ${chapterIndex + 1} 章';

  @override
  String toString() => 'BookmarkRow($bookmarkId, ch$chapterIndex, $label)';
}
