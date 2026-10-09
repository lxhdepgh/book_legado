/// 自然段切分与章内字符坐标：Dart 侧那一份"尺"。
///
/// 这是 `app/core/text_layout.py` 的**独立实现**，不是翻译调用：协议面只发块
/// （`reader.content` 的 `blocks`），不发段列表，也不发段首偏移——所以客户端要排版、
/// 要知道"当前屏幕在第几段"、要把落点报回服务端，只能自己切。跨端可比的那把尺
/// （章内字符偏移）因此有**两份实现**，而它们的相等由三处外部读数钉着：
/// 1. 服务端同一发回话里的 `paragraphCount` / `totalChars`（`reader.content`）；
/// 2. flet 那屏自己算出来的三张投影表 `tests/gateway/projection/reader.content*.json`
///    （段首偏移、屏上第几行、位置往返、26 发落点探针），见 `test/reader_layout_projection_test.dart`；
/// 3. 真 `--serve` 子进程那一臂（`test/reader_live_arm_test.dart`）。
/// 两边各数各的、又都朝期望方向绿，是记忆 `measurement-corpus-must-not-be-self-proving`
/// 拦的那件事——所以第 2 处用的是**对面**的读数，不是这里算出来的数。
///
/// 口径三条（错一条整把尺就歪）：
/// - 段 = `chapterParagraphs(displayBlocks(document))` 的那一串，媒体块**不产段**；
/// - 偏移 = 下标进 `"\n".join(段落)`，段间那一格算 `kParagraphJoinerLen = 1`；
/// - 标题识别与段首缩进是**显示**的事，不进尺（缩进每段多两字，偏移整体漂）。
library;

import '../data/chapter_document.dart';

/// 段与段之间在字符流里占一格（与 Python 侧 `PARAGRAPH_JOINER_LEN` 同值同义）。
const int kParagraphJoinerLen = 1;

/// 行内连续空白压成一个空格。**不含** `\r`：Python 那侧先把 `\r\n`/`\r` 归一成 `\n`
/// 再切行，所以字符类里没有 `\r` 的活要干；照抄时多加一个 `\r` 就会与那边不一致。
final RegExp _inlineWhitespace = RegExp('[ \t\f\v]+');

/// 小节标题：与 flet 那屏同一个正则（`reader_view.py::_HEADING_PATTERN`）。
/// 只影响画法（加粗、不缩进），不影响尺——标题行照样是第 k 段。
final RegExp _headingLine = RegExp(
    r'^(?:第[0-9０-９零〇一二三四五六七八九十百千万两]+[章节卷回部篇集]'
    r'|卷[0-9０-９零〇一二三四五六七八九十百千万两]+'
    r'|序章|序言|前言|楔子|引子|后记|尾声|番外)'
    r'(?:\s|$|[：:、.-])');

/// 标题行最长多少字（超过就当我们是正文，flet 那侧同值）。
const int kMaxHeadingLength = 48;

/// 已经自带缩进/项目符号的行不再叠一层"　　"（`_INDENT_KEEP_PREFIXES`）。
const List<String> kIndentKeepPrefixes = <String>['　　', '•', '·', '- ', '— '];

String _strip(String value) {
  var start = 0;
  var end = value.length;
  while (start < end && _isSpaceUnit(value.codeUnitAt(start))) {
    start++;
  }
  while (end > start && _isSpaceUnit(value.codeUnitAt(end - 1))) {
    end--;
  }
  return value.substring(start, end);
}

/// Python `str.isspace()` 的等价判据（按 UTF-16 码元；辅助平面那批空白字符
/// 语料里没有，真出现时要连同代理对一起判，别半个半个剥）。
bool _isSpaceUnit(int unit) =>
    (unit >= 0x00 && unit <= 0x20) ||
    unit == 0x85 ||
    unit == 0xA0 ||
    unit == 0x1680 ||
    (unit >= 0x2000 && unit <= 0x200A) ||
    unit == 0x2028 ||
    unit == 0x2029 ||
    unit == 0x202F ||
    unit == 0x205F ||
    unit == 0x3000;

/// 正文原文 → 自然段文本列表（不缩进、不认标题）。
List<String> paragraphLines(Object? value) {
  final normalized = (value ?? '')
      .toString()
      .replaceAll('\r\n', '\n')
      .replaceAll('\r', '\n')
      .replaceAll('\xa0', ' ');
  final lines = <String>[];
  for (final raw in normalized.split('\n')) {
    final collapsed =
        raw.replaceAllMapped(_inlineWhitespace, (Match _) => ' ');
    final line = _strip(collapsed);
    if (line.isNotEmpty) lines.add(line);
  }
  return lines;
}

/// 一个块的"能不能上屏"。
///
/// flet 那侧的判据是 `if not value: continue`——它手里的二进制块 value 是字节串，
/// 恒真。Dart 这边字节**不过协议面**（设计 §4.4），二进制块的 value 是 null、
/// 内容只有 `url`+`headers`。所以这里的等价判据是 `value 非空 || url 非空`：
/// 丢块的标准是"客户端什么都画不出来"，而不是"值那一格空着"。
/// 这一条不改尺：媒体块本来就不产段（[chapterParagraphs] 只认 text）。
bool _isRenderable(ContentBlock block) =>
    (block.value != null && block.value!.isNotEmpty) ||
    block.url.isNotEmpty;

/// 正文块 → 本章自然段列表（口径与 flet 的 `content_plan` + `chapter_paragraphs` 一致）。
List<String> chapterParagraphs(List<ContentBlock> blocks) {
  final paragraphs = <String>[];
  for (final block in blocks) {
    if (!block.isText) continue;
    final value = block.value;
    if (value == null || value.isEmpty) continue;
    paragraphs.addAll(paragraphLines(value));
  }
  return paragraphs;
}

/// 段列表的形状闸门：吃一整串正文是要防的那件错事——Dart 的 `split('')` 会把整章
/// 变成逐字列表，算出来的"段首"全是 1，两边各错各的还能互相绿。
///
/// 这一格是**公开的**：坐标族的入参类型写成 `List<String>` 已经挡住了误用，而那层
/// 动态形状检查（比如从 `Map<String, Object?>` 里取出来的那一格）只有函数体里这道
/// 判据管得到，测试要能直接打它才照得见。
List<String> requireParagraphList(Object? paragraphs) {
  if (paragraphs is String) {
    throw ArgumentError('坐标吃的是自然段列表，不是整章正文：先调 chapterParagraphs');
  }
  if (paragraphs is! Iterable) {
    throw ArgumentError('自然段必须是字符串列表，收到 ${paragraphs.runtimeType}');
  }
  return paragraphs.map((Object? item) {
    if (item is! String) {
      throw ArgumentError('自然段必须是 String，收到 ${item.runtimeType}');
    }
    return item;
  }).toList(growable: false);
}

/// 每段段首在章内字符流里的偏移。
List<int> paragraphOffsets(List<String> paragraphs) {
  final texts = requireParagraphList(paragraphs);
  final offsets = <int>[];
  var cursor = 0;
  for (final text in texts) {
    offsets.add(cursor);
    cursor += text.length + kParagraphJoinerLen;
  }
  return offsets;
}

/// 本章字符流总长（空章 0，不造"至少一个字"的假下限）。
int chapterTextLength(List<String> paragraphs) {
  final texts = requireParagraphList(paragraphs);
  if (texts.isEmpty) return 0;
  return texts.fold<int>(0, (sum, t) => sum + t.length) +
      kParagraphJoinerLen * (texts.length - 1);
}

/// 段序号 → 段首字符偏移（**存档**方向）；越界夹到末尾，负数回 0。
int offsetOfParagraph(List<String> paragraphs, int index) {
  final texts = requireParagraphList(paragraphs);
  if (texts.isEmpty) return 0;
  if (index <= 0) return 0;
  if (index >= texts.length) return chapterTextLength(texts);
  var cursor = 0;
  for (var i = 0; i < index; i++) {
    cursor += texts[i].length + kParagraphJoinerLen;
  }
  return cursor;
}

/// 字符偏移 → `(段序号, 段内偏移)`（**回位**方向，与 [offsetOfParagraph] 互逆）。
///
/// 落在段间那一格换行上算**前一段的段尾**（`local == 段长`），不算下一段的段首——
/// 否则"刚好压着换行"那一帧会把用户弹到下一段开头。
ParagraphLocation paragraphAtOffset(List<String> paragraphs, int offset) {
  final texts = requireParagraphList(paragraphs);
  if (texts.isEmpty) return const ParagraphLocation(0, 0);
  final offsets = paragraphOffsets(texts);
  final position = offset < 0 ? 0 : offset;
  var index = _bisectRight(offsets, position) - 1;
  if (index < 0) index = 0;
  final local = position - offsets[index];
  return ParagraphLocation(index, local > texts[index].length ? texts[index].length : local);
}

int _bisectRight(List<int> sorted, int value) {
  var low = 0;
  var high = sorted.length;
  while (low < high) {
    final mid = (low + high) >> 1;
    if (value < sorted[mid]) {
      high = mid;
    } else {
      low = mid + 1;
    }
  }
  return low;
}

/// 字符偏移落在哪一段的哪一格。
class ParagraphLocation {
  const ParagraphLocation(this.paragraphIndex, this.localOffset);

  final int paragraphIndex;
  final int localOffset;

  @override
  String toString() => 'ParagraphLocation($paragraphIndex, +$localOffset)';

  @override
  bool operator ==(Object other) => other is ParagraphLocation &&
      other.paragraphIndex == paragraphIndex &&
      other.localOffset == localOffset;

  @override
  int get hashCode => Object.hash(paragraphIndex, localOffset);
}

/// 一屏要挂的一行：正文段或媒体块。
///
/// [ruler] 是**尺上那串字**（没缩进、没标题处理），[display] 是**屏上那串字**。
/// 投影表里这两列是分开的，分开是因为它们本来就该分开：拿 display 去算偏移，
/// 每段多两字，整章的落点跟着漂。
class ReaderLine {
  const ReaderLine({
    required this.kind,
    required this.paragraphIndex,
    required this.heading,
    required this.display,
    required this.ruler,
    required this.meta,
    this.block,
  });

  /// `text` / `image` / `audio` / `video`（词汇表见 `kContentBlockKinds`）。
  final String kind;

  /// 媒体行没有段号（不产段），所以是 null。
  final int? paragraphIndex;

  /// 标题行：画法加粗、且不叠段首缩进。
  final bool heading;

  final String display;

  /// 这一段的原文形状（尺上的那一串）。媒体行是 null。
  final String? ruler;

  /// 媒体行的图源样式（`imageStyle` 大写）；正文行是空串。
  final String meta;

  /// 媒体行的原块（取 `url`/`headers`/`title` 用）。正文行是 null。
  final ContentBlock? block;

  bool get isText => kind == 'text';
}

/// 一章文档 → 屏幕行计划（对应 flet 的 `ReaderView.content_plan` + `render_plan` 的切法）。
List<ReaderLine> contentPlan(ChapterDocument document, {bool indent = true}) {
  // 段号要**跟着块走**：一个 text 块产好几段，媒体块产 0 段但仍占一行。
  // 所以这里按出现顺序配段号，而不是按 blocks 的下标，也不去读整章的段表
  // （`chapterParagraphs(document.blocks)` 与这里的逐块拼接是同一个数列，
  // 那条相等由 `test/reader_layout_projection_test.dart` 对着 flet 的投影表核）。
  var next = 0;
  final lines = <ReaderLine>[];
  for (final block in document.blocks) {
    if (!_isRenderable(block)) continue;
    if (block.isText) {
      final texts = paragraphLines(block.value);
      if (texts.isEmpty) continue;
      for (final text in texts) {
        final isHeading = text.length <= kMaxHeadingLength && _headingLine.hasMatch(text);
        lines.add(ReaderLine(
          kind: 'text',
          paragraphIndex: next,
          heading: isHeading,
          display: _display(text, isHeading: isHeading, indent: indent),
          ruler: text,
          meta: '',
        ));
        next++;
      }
    } else {
      lines.add(ReaderLine(
        kind: block.kind,
        paragraphIndex: null,
        heading: false,
        display: '',
        ruler: null,
        meta: document.imageStyle.toUpperCase(),
        block: block,
      ));
    }
  }
  return lines;
}

/// 段首缩进（`　　`）与"已带符号的行不叠"。
String _display(String text, {required bool isHeading, required bool indent}) {
  if (!indent || isHeading) return text;
  for (final prefix in kIndentKeepPrefixes) {
    if (text.startsWith(prefix)) return text;
  }
  return '　　${text.replaceFirst(RegExp('^　+'), '')}';
}
