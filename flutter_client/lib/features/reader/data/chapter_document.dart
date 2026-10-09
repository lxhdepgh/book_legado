/// 一章正文：`reader.content` 的回话。
///
/// **拿到手的就是可显示形态**：`<usehtml>`/`<md>` 已经在服务侧渲染掉
/// （`chapter_document` 里那一句 `display_document`）。为什么不在客户端各渲染一遍——
/// 渲染会改字数，而 [paragraphCount]/[totalChars] 是两侧比翻页落点那把尺的读数
/// （`app/core/text_layout.py`）；两边各自渲染，数出来的就不是同一串字了。
/// 这一条与记忆 `intro-render-contract` 是同一件事的阅读面版本。
///
/// [paragraphCount] 与 [totalChars] 是**服务端算的**，客户端不许自己再数一遍：
/// 那等于把"什么算一段"这条规则抄第二份，而它刚被 G3-A 用来统一三个读数口。
library;

/// 块类型词汇表：`text` + `_MEDIA_EXTENSIONS` 那三个键
/// （`app/services/engine/reading_engine.py:206-210`）。
/// 对齐由 `tests/test_gateway_contract_single_source.py` 从 Python 侧抽键名核这一条。
const Set<String> kContentBlockKinds = <String>{'text', 'image', 'audio', 'video'};

/// 一个显示块。文本块有值没地址；媒体块反过来——**字节永远不过协议面**（设计 §4.4）。
class ContentBlock {
  const ContentBlock({
    required this.kind,
    required this.value,
    required this.url,
    required this.binary,
    required this.title,
    required this.headers,
  });

  /// 见 [kContentBlockKinds]。表外的值按"未知块"处理（画原文那一段），不崩屏。
  final String kind;

  /// 文本内容。媒体块这里是 `null`（flet 那边曾把它换成字节给 `ft.Image` 画，
  /// 而字节不能过协议面），地址在 [url]。
  final String? value;

  /// 媒体地址。`binary == true` 时它是**唯一**的取内容路径：
  /// 受保护图（要带 cookie/UA 的那种）就是这一格救回来的——`ContentBlock.url`
  /// 那一格是第八十六轮补的，之前过完协议面就永久没地址了。
  final String url;

  /// 服务端把值换成了字节（值这一格因此是 null）。`Image.network` 用的就是
  /// "url + headers"这一对，不是 value。
  final bool binary;

  final String title;

  /// 取这个媒体块要带的请求头（cookie/UA 之类）。空 map = 直接 GET。
  final Map<String, String> headers;

  bool get isText => kind == 'text';

  /// 音视频块在 flet 那边挂"打开源地址"按钮（G3-C3 要对齐这一格）。
  bool get isMedia => kind == 'image' || kind == 'audio' || kind == 'video';

  factory ContentBlock.fromJson(Map<String, Object?> json) => ContentBlock(
        kind: '${json['kind'] ?? ''}',
        value: json['value'] == null ? null : '${json['value']}',
        url: '${json['url'] ?? ''}',
        binary: json['binary'] == true,
        title: '${json['title'] ?? ''}',
        headers: <String, String>{
          for (final entry in (json['headers'] as Map? ?? const <String, Object?>{})
              .entries)
            '${entry.key}': '${entry.value}',
        },
      );

  @override
  String toString() => 'ContentBlock($kind'
      '${isText ? ', ${value?.length ?? 0} 字' : ', url=$url'})';
}

/// 一章的文档。
class ChapterDocument {
  const ChapterDocument({
    required this.chapterUrl,
    required this.chapterIndex,
    required this.chapterName,
    required this.title,
    required this.imageStyle,
    required this.blocks,
    required this.paragraphCount,
    required this.totalChars,
  });

  final String chapterUrl;
  final int chapterIndex;
  final String chapterName;

  /// 章标题（服务端从正文里提出来的那一份，可能与目录名不同）。
  final String title;

  /// 图源给的图片样式串（Legado 的 `imageStyle`）。空串 = 没设置。
  final String imageStyle;

  final List<ContentBlock> blocks;

  /// **服务端按位置契约数出来的段数**（`chapter_paragraphs` 的长度）。
  final int paragraphCount;

  /// 章内字符流总长（段间那个 `"\n"` 算一格，见 `PARAGRAPH_JOINER_LEN`）。
  /// 存进度时字符偏移要夹在它里面，所以它是那把尺的右端点。
  final int totalChars;

  factory ChapterDocument.fromJson(Map<String, Object?> json) {
    final rows = (json['blocks'] as List? ?? const <Object?>[]).map((row) {
      if (row is! Map) {
        throw ArgumentError('reader.content 的某个块不是对象（收到 ${row.runtimeType}）');
      }
      return ContentBlock.fromJson(Map<String, Object?>.from(row));
    }).toList(growable: false);
    return ChapterDocument(
      chapterUrl: '${json['chapterUrl'] ?? ''}',
      chapterIndex: (json['chapterIndex'] as num?)?.toInt() ?? 0,
      chapterName: '${json['chapterName'] ?? ''}',
      title: '${json['title'] ?? ''}',
      imageStyle: '${json['imageStyle'] ?? ''}',
      blocks: rows,
      paragraphCount: (json['paragraphCount'] as num?)?.toInt() ?? 0,
      totalChars: (json['totalChars'] as num?)?.toInt() ?? 0,
    );
  }

  /// 只有文本块产自然段（`chapter_paragraphs` 的口径）——这一句是给"段数对不上"
  /// 那类现场排查用的读数，不是重新数一遍段数。
  int get textBlockCount => blocks.where((b) => b.isText).length;

  @override
  String toString() => 'ChapterDocument($chapterName, ${blocks.length} 块, '
      '$paragraphCount 段 / $totalChars 字)';
}
