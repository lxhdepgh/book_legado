/// 续读位置：`reader.position_get` / `reader.position_save` 的回话，
/// 也是 `reader.open` 里 `position` 那一格（三处共用一个形状，因为服务端只有一份
/// `as_reading()`——`reading_service.py:199-210`）。
///
/// **键名是蛇形**，而同批发里 `sourceUrl`/`bookUrl` 是驼峰：服务端就在这一个边界上
/// 两种拼法并存。抄错一个下划线不会报错，那一格静默落成默认值——所以
/// `flutter_client/test/golden_contract_test.dart` 拿真样本的键集逐键核这一族。
///
/// 两个"位置"不是同一个量，这一条是 G3 那把尺的全部意义：
/// - [characterOffset]（线上 `chapter_pos`）= **章内字符偏移**，数的是
///   `chapter_paragraphs(display_blocks(…))` 拼起来那一串字（`app/core.text_layout`）。
///   flet 与 Dart 比翻页落点比的正是它；
/// - [scrollOffset]（线上 `scroll_offset`）= 像素。两侧字体、行宽、屏幕都不同，
///   **这个数不许跨端比**，存它只为了下一次在同一端续读时落回原样。
///
/// [paragraphIndex] 可空，且 `null` 与 `0` 是两件事：`null` 是这份存档从来没记过段号
/// （老数据、或只落了像素的调用方），`0` 是"记在第 1 段段首"。把它读成 0 会让
/// 一本读到中间的书在两侧显示成"刚开始读"。
library;

class ReadingPosition {
  const ReadingPosition({
    this.chapterUrl = '',
    this.chapterName = '',
    this.chapterIndex = 0,
    this.scrollOffset = 0,
    this.scrollRatio = 0,
    this.characterOffset = 0,
    this.paragraphIndex,
    this.fontSize = kDefaultFontSize,
    this.lastReadAt = 0,
  });

  /// 服务端 `from_reading` 对缺 `font_size` 那一条的默认值（`reading_service.py:195`）。
  /// 写在这里不是为了"和那边一样"，是因为客户端不填时**落库的就是这个数**，
  /// 界面按 16 起排才对得上。
  static const int kDefaultFontSize = 16;

  final String chapterUrl;
  final String chapterName;
  final int chapterIndex;

  /// 章内像素偏移（不可跨端比，见文件头）。
  final double scrollOffset;

  /// 章内比例，夹在 [0,1]（服务端 `from_reading` 那一夹）。书签存的就是这一格。
  final double scrollRatio;

  /// **章内字符偏移**：两侧比落点用的那把尺。
  final int characterOffset;

  /// 段序号（章内）。`null` = 这份存档没记段号，不是"第 0 段"。
  final int? paragraphIndex;

  final int fontSize;

  /// 落库时刻（epoch **秒**）；`0` = 从来没读过（服务端默认，不是"1970 年"）。
  final int lastReadAt;

  factory ReadingPosition.fromJson(Map<String, Object?> json) => ReadingPosition(
        chapterUrl: '${json['chapter_url'] ?? ''}',
        chapterName: '${json['chapter_name'] ?? ''}',
        chapterIndex: (json['chapter_index'] as num?)?.toInt() ?? 0,
        scrollOffset: (json['scroll_offset'] as num?)?.toDouble() ?? 0,
        scrollRatio: (json['scroll_ratio'] as num?)?.toDouble() ?? 0,
        characterOffset: (json['chapter_pos'] as num?)?.toInt() ?? 0,
        paragraphIndex: (json['chapter_paragraph'] as num?)?.toInt(),
        fontSize: (json['font_size'] as num?)?.toInt() ?? kDefaultFontSize,
        lastReadAt: (json['last_read_at'] as num?)?.toInt() ?? 0,
      );

  /// 送给 `reader.position_save` 的 `position_json`：键名必须与 [fromJson] 那九个一致，
  /// 因为服务端 `from_reading` 按这九个名字取值——少一个下划线就是**静默丢一格**，
  /// 而这一发回的是"落库后的形状"，界面上看不出区别。
  Map<String, Object?> toJson() => <String, Object?>{
        'chapter_url': chapterUrl,
        'chapter_name': chapterName,
        'chapter_index': chapterIndex,
        'scroll_offset': scrollOffset,
        'scroll_ratio': scrollRatio,
        'chapter_pos': characterOffset,
        'chapter_paragraph': paragraphIndex,
        'font_size': fontSize,
        'last_read_at': lastReadAt,
      };

  @override
  String toString() => 'ReadingPosition(ch$chapterIndex'
      '${chapterName.isEmpty ? '' : ' $chapterName'}, '
      '偏移 $characterOffset 字'
      '${paragraphIndex == null ? '' : '/第 $paragraphIndex 段'}, '
      '比例 ${scrollRatio.toStringAsFixed(3)})';
}
