/// 阅读那一屏的状态机（§6.2：`AsyncNotifier`；会话在引擎侧是单例，所以这里只有一条 provider）。
///
/// 照的是 `app/controllers/reader_controller.py`，六条例子在代码里：
///
/// 1. **会话坐标是 `(source_url, book_url)` 这一对，而且以服务端回的那一条为准**
///    （`ReaderSession.sourceUrl` 是引擎认的源，不是请求里传的那一条：换源之后它会变）。
///    后续每一发都用回话里的那一对，对不上引擎就拒答（`reader.toc--no-session` 那份样本）。
/// 2. **章坐标用 `reader.toc` 的行序号**：服务端 `position_save` 就是按
///    `chapters[index]` 认章的（`reading_service.py::_with_toc_url`），而 `books` 表
///    不存 `chapter_url`（设计 §3.9）。flet 那边 `_chapter_index` 数的是**滤掉卷标题之后**
///    的表（`reader_controller.py:832`），两套序号在有卷标题的书上不指同一章——
///    落库跟服务端那把，屏上"第 n/N 章"的读数仍按 flet 的滤卷口径。
/// 3. **段序号优先于像素**（`#88`）：像素属于存档时那一套装尺（字号/行距/版心换过就不指
///    原来那一段），段序号才是两侧都认得的数。所以恢复落点先读 `chapter_paragraph`，
///    没有才按字符偏移算段号（`paragraphAtOffset`）。
/// 4. **换章那一拍不许落库**（flet 的 `_restoring_scroll`）：整篇替换时布局会先发一次
///    "偏移 0"的滚动通知，让它落盘就把上次进度抹成 0。
/// 5. **0.5s 合流落库**（flet `throttle(…, 0.5)`）：一拍一发的话网关的排期会被进度写库占满。
/// 6. **退出顺序：转正/放弃排在关会话之前**（`_finish_session:1470-1484`）——
///    `close_book` 会把当前书与源清空，之后再发 `staged_promote` 服务就认不出那条记录了。
///    而"问不问"由 `reader.staged` 那一发决定：在架的书回 null，
///    否则每次退出都被问一遍"要放入书架吗"（flet `exit_reader:1432` 同一发）。
///
/// 7. **连续阅读：读到章尾接下一章，接上来的那一章不改视口**（flet `_on_reader_scroll`）：
///    整篇文档同时挂着 1~N 章，屏上报的段号是**整篇**口径，而落库必须是**章内**段号与
///    **章内**偏移——那一步折算在 `domain/continuous_reading.dart`（与 flet 的 `track`
///    同源，由 `test/continuous_reading_projection_test.dart` 对着 flet 自己产出的
///    `tests/gateway/projection/reader.continuous.json` 逐发核）。迟滞阈值也照那边：
///    离底部 500px 以上重新武装、不足 180px 且已武装才追加（一条阈值线判定会在边界上
///    反复触发，一章能被接两遍）。
///
/// **两处与 flet 有意不同**（都在这轮的量里说过，不是抄漏）：
/// - 追加过的章**不另立台账**，就看它在不在 `mounts` 里。flet 的 `_continuous_loaded`
///   只在换书时清（`_set_chapters`），于是"先按下一章翻到第 3 章 → 退回第 2 章 → 读到底"
///   那一串会把第 3 章永远拒掉，自动续读静默失效。整篇替换既然已经把文档换成一篇，
///   那张账就该跟着作废。
/// - `scroll_offset` 存的是**章内**像素（`Landing.chapterPixels`）。文档级像素在下一次
///   只挂一章的会话里指不到同一处，那一列本来就不是可跨会话比的数（`ReadingPosition` 文件头）。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/engine_repository.dart';
import '../../../core/rpc/envelope.dart';
import '../../../core/settings/settings_map.dart';
import '../data/bookmark_row.dart';
import '../data/chapter_document.dart';
import '../data/chapter_row.dart';
import '../data/content_search_result.dart';
import '../data/reader_session.dart';
import '../data/reading_position.dart';
import '../data/staged_record.dart';
import 'continuous_reading.dart';
import 'text_layout.dart';

/// 打开阅读器要带的那几格：源、书快照、要不要入架、要不要续读。
///
/// [book] 是**字典快照**（`BookRow.raw`）而不是模型对象：协议面那一发
/// `reader.open` 吃的就是这份字典（服务端按 `_book_item_dict` 归一），
/// 传模型等于在边界上再造一次序列化。
class ReaderArgs {
  const ReaderArgs({
    required this.sourceUrl,
    required this.book,
    this.shelve = true,
    this.resume = true,
  });

  /// 没注入时的那一份（路由漏了 `extra` 就是这个形状，别让它悄悄开一本空书）。
  const ReaderArgs.empty()
      : sourceUrl = '',
        book = const <String, Object?>{},
        shelve = true,
        resume = true;

  final String sourceUrl;
  final Map<String, Object?> book;

  /// `false` = 试读（Legado 的 notShelf）：不入架，退出时要问"放入书架吗"。
  final bool shelve;

  /// `false` = 从第一章重读（不带回存档）。
  final bool resume;

  bool get isEmpty => book.isEmpty && sourceUrl.isEmpty;

  String get bookName => '${book['name'] ?? ''}';
}

/// 由**路由**注入：阅读页不知道自己是"从书架开的"还是"从找书试读的"。
/// 默认体是空的那一份，于是忘注入时屏幕上写的是"没收到书"，不是转圈。
final Provider<ReaderArgs> readerArgsProvider =
    Provider<ReaderArgs>((ref) => const ReaderArgs.empty());

/// 排版偏好。这里只带**会改尺或改画法**的那两格（字号、段首缩进）：
/// 其余八格（行距/版心/背景/划词……）在 G3-C3 这一屏没有对应控件，
/// 读了也用不上——留着就是假装有实现。
///
/// 键与 flet 同源（`reader.font_size` / `reader.indent`，服务端加 `reader.` 前缀落
/// `settings` 表），所以两个客户端共用同一份排版偏好：在 flet 里调到 20 号，
/// Flutter 这边开书就是 20 号。
class ReaderTypography {
  const ReaderTypography({
    this.fontSize = kDefaultFontSize,
    this.indent = true,
  });

  /// flet 的滑块界（`reader_view.py:38-39`）：越界的存档值夹回来，不退回默认。
  static const int kMinFontSize = 13;
  static const int kMaxFontSize = 28;

  /// 与 `ReadingPosition.kDefaultFontSize` 同一个数（那边是服务端 `from_reading` 的默认）。
  static const int kDefaultFontSize = 16;

  static const String kFontSizeMode = 'reader.font_size';
  static const String kIndentMode = 'reader.indent';

  static const ReaderTypography defaults = ReaderTypography();

  final int fontSize;
  final bool indent;

  factory ReaderTypography.fromSettings(SettingsMap settings) => ReaderTypography(
        fontSize: clampFontSize(settings.intOrNull(kFontSizeMode)),
        // 只认 bool 与"非 0 数字"两种存法：字符串 "false" 落进 `!= null` 会读成 true，
        // 而这一格恰恰是设置表里唯一能被手改写坏的排版值。
        indent: settings.boolOrNull(kIndentMode) ??
            ((settings.numOrNull(kIndentMode) ?? 1) != 0),
      );

  static int clampFontSize(int? value) {
    if (value == null) return kDefaultFontSize;
    if (value < kMinFontSize) return kMinFontSize;
    if (value > kMaxFontSize) return kMaxFontSize;
    return value;
  }

  ReaderTypography withFontSize(int delta) => ReaderTypography(
      fontSize: clampFontSize(fontSize + delta), indent: indent);

  ReaderTypography withIndent(bool value) =>
      ReaderTypography(fontSize: fontSize, indent: value);

  /// 落库的那两发（`settings.write` 一键一发）。
  Map<String, Object?> toSettings() => <String, Object?>{
        kFontSizeMode: fontSize,
        kIndentMode: indent,
      };

  @override
  String toString() =>
      'ReaderTypography($fontSize 号, 缩进 ${indent ? '开' : '关'})';
}

/// 屏上挂着的一章（连续阅读时同时挂着 1~N 章，每一章一份）。
///
/// [paragraphStart] 与 [documentStart] 是折算用的两个原点，一个数段、一个数像素：
/// - `paragraphStart` = 本章第一段在**整篇**文档里的段序号，就是接上来那一刻已经挂着的
///   那些段数（flet `reader_controller.py:425` 的 `self._view.paragraph_count()`）。
///   数错这一格（按屏上**行数**累加）整篇段号就整体漂移：媒体块、章名、分隔线都占行不产段；
/// - `documentStart` = 触发追加那一刻的文档总高（像素）。它是**旧**文档的高度，而视图
///   报的段号按**当前**高度缩放，两套尺在章间接缝上会打架——所以选章只认段号，
///   这一格只用来算段内那一截的比例（`continuous_reading.dart` 文件头）。
class MountedChapter {
  const MountedChapter({
    required this.chapterIndex,
    required this.chapterName,
    required this.document,
    required this.paragraphs,
    required this.offsets,
    required this.lines,
    required this.paragraphStart,
    required this.documentStart,
  });

  /// 挂一章并把三把读数算好（段、段首偏移、屏上行）。
  ///
  /// [paragraphStart] 与 [documentStart] 只在追加那一臂给：整篇替换时文档只剩它，
  /// 两个原点天然归零。
  factory MountedChapter.mount({
    required int chapterIndex,
    required String chapterName,
    required ChapterDocument document,
    required bool indent,
    int paragraphStart = 0,
    double documentStart = 0,
  }) {
    final paragraphs = chapterParagraphs(document.blocks);
    return MountedChapter(
      chapterIndex: chapterIndex,
      chapterName: chapterName,
      document: document,
      paragraphs: paragraphs,
      offsets: paragraphOffsets(paragraphs),
      lines: contentPlan(document, indent: indent),
      paragraphStart: paragraphStart,
      documentStart: documentStart,
    );
  }

  final int chapterIndex;

  /// 目录里的那一行的名字（flet 的顶栏与追加章名行用的都是它，不是正文里那行 `<title>`）。
  final String chapterName;

  final ChapterDocument document;

  /// 本章自然段（尺上口径，不缩进）。
  final List<String> paragraphs;

  /// 本章各段段首的**章内**字符偏移。
  final List<int> offsets;

  /// 本章的屏上行，段号是**章内**口径（进整篇之前由 [documentLinesOf] 加上起点）。
  final List<ReaderLine> lines;

  final int paragraphStart;
  final double documentStart;

  String get chapterUrl => document.chapterUrl;

  int get paragraphCount => paragraphs.length;

  /// 段号夹进本章。
  ///
  /// 越界不夹就是把"第 12 段"存进只有 12 段的那一章——服务端会夹回来，
  /// 于是重开时用户从下一章段首被弹回上一章段尾（第九十二轮量到的那发不可能的读数）。
  int clampParagraph(int paragraph) {
    if (paragraphs.isEmpty) return -1;
    if (paragraph < 0) return 0;
    return paragraph >= paragraphs.length ? paragraphs.length - 1 : paragraph;
  }

  /// 改缩进只重画行，尺不动（`paragraphs`/`offsets` 都是没缩进的原文）。
  MountedChapter withIndent(bool indent) => MountedChapter(
        chapterIndex: chapterIndex,
        chapterName: chapterName,
        document: document,
        paragraphs: paragraphs,
        offsets: offsets,
        lines: contentPlan(document, indent: indent),
        paragraphStart: paragraphStart,
        documentStart: documentStart,
      );

  /// 交给折算层的那一格。`textLength` 用的是**带段间换行**的字符流长度
  /// （`chapterTextLength`），与 `offsets` 同一把尺；flet 那边是 `sum(len(line))`，
  /// 少了 n−1 个段间格。那一格只在"段表不可信、退回像素插值"时才乘比例，
  /// 而这一屏每一章都算得出段表，走不到那一条。
  ChapterMount toMount() => ChapterMount(
        chapterUrl: chapterUrl,
        chapterIndex: chapterIndex,
        paragraphStart: paragraphStart,
        paragraphOffsets: offsets,
        textLength: chapterTextLength(paragraphs),
        documentStart: documentStart,
      );

  @override
  String toString() =>
      'MountedChapter(#$chapterIndex $chapterName, 段起点 $paragraphStart/'
      '$paragraphCount 段)';
}

/// 整篇文档里屏上的那一行：正文段、媒体块，或追加进来时补的分隔线/章名。
///
/// [paragraph] 是**整篇**段号（不是章内）：屏上每一行的 GlobalKey 用它当索引，
/// 三章同时挂着时"第 0 段"这种章内口径会撞名，撞了就把上一章的落点报进下一章。
class DocumentLine {
  const DocumentLine.row({
    required this.mountIndex,
    required this.line,
    this.paragraph,
    this.lastInChapter = false,
  }) : name = '';

  const DocumentLine.divider({required this.mountIndex})
      : line = null,
        paragraph = null,
        lastInChapter = false,
        name = '';

  const DocumentLine.title({required this.mountIndex, required this.name})
      : line = null,
        paragraph = null,
        lastInChapter = false;

  final int mountIndex;

  /// 这一行的原行计划；分隔线与章名行是 null。
  final ReaderLine? line;

  /// **整篇**段号；媒体行与补的两行是 null（它们不产段）。
  final int? paragraph;

  /// 本章最后一段（正文行之间留的白不一样，追加之后不能只认整篇那最后一段）。
  final bool lastInChapter;

  /// 章名行那一句（其余行是空串）。
  final String name;

  bool get isContent => line != null;
}

/// 挂着的那几章 → 整篇的屏上行表（每一章前头补分隔线 + 章名，第一章不补）。
///
/// flet 那侧是 `render_plan(append=True)` 往同一个 `_content_host` 后面 extend
/// `ft.Divider` 与 `ft.Text(chapter_name)`，第一章由顶栏报名字。补在第一章前面就与
/// flet 的屏上控件数不同（参照表里 `screen_controls` 27 = 22 段 + 1 图 + 2 章 ×2 行），
/// 那一格正对照钉的就是这件事。
List<DocumentLine> documentLinesOf(List<MountedChapter> mounts) {
  final rows = <DocumentLine>[];
  for (var i = 0; i < mounts.length; i++) {
    final mount = mounts[i];
    if (i > 0) {
      rows.add(DocumentLine.divider(mountIndex: i));
      rows.add(DocumentLine.title(mountIndex: i, name: mount.chapterName));
    }
    for (final line in mount.lines) {
      rows.add(line.isText
          ? DocumentLine.row(
              mountIndex: i,
              line: line,
              paragraph: mount.paragraphStart + (line.paragraphIndex ?? 0),
              lastInChapter: line.paragraphIndex == mount.paragraphCount - 1,
            )
          : DocumentLine.row(mountIndex: i, line: line));
    }
  }
  return rows;
}

/// 一屏读完所需的全部状态（不可变；改一处就 `copyWith` 一份）。
class ReaderPage {
  const ReaderPage({
    required this.session,
    required this.chapters,
    required this.mounts,
    required this.documentLines,
    required this.chapterIndex,
    required this.typography,
    required this.anchorParagraph,
    this.loadSeq = 0,
    this.bookmarks = const <BookmarkRow>[],
    this.search,
    this.echo = '',
    this.chapterLoading = false,
  });

  /// 状态还没建起来那一拍的占位（加载屏与错误屏都会读 `_current`）。
  ///
  /// 它走 `fromJson` 造**真空**而不是手写一份字面量：那两格（`book`/`position`）
  /// 服务端恒给形状，占位若与它们不同形，界面在占位上能过、在真回话上会崩。
  static final ReaderPage idle = ReaderPage(
    session: ReaderSession.fromJson(const <String, Object?>{
      'book': <String, Object?>{},
      'position': <String, Object?>{},
    }),
    chapters: const <ChapterRow>[],
    mounts: const <MountedChapter>[],
    documentLines: const <DocumentLine>[],
    chapterIndex: -1,
    typography: ReaderTypography.defaults,
    anchorParagraph: -1,
  );

  final ReaderSession session;

  /// `reader.toc` 的原样行表（**不滤卷标题**：序号要与服务端的 `chapters[index]` 对齐）。
  final List<ChapterRow> chapters;

  /// 屏上挂着的那几章（挂载顺序 = 从上到下；整篇替换只剩一章）。
  final List<MountedChapter> mounts;

  /// 整篇的屏上行表（[documentLinesOf] 的产物；段号是整篇口径）。
  final List<DocumentLine> documentLines;

  /// **屏幕上在读的那一章**在 [chapters] 里的下标 = 落库的 `chapter_index`。
  ///
  /// 连续阅读时它跟着视口走（flet `_follow_visible_chapter`）：挂着三章而眼睛在第
  /// 二章中段，落库就该落第二章——落"当初点进来的那章"会把进度写成半章之前。
  final int chapterIndex;

  final ReaderTypography typography;

  /// 这一拍要落到的段号（**[chapterIndex] 那一章的章内**段号）；`-1` = 不落（章首）。
  final int anchorParagraph;

  final List<BookmarkRow> bookmarks;

  /// 最近一次书内搜索的回话；`null` = 还没搜过（与"搜了没命中"是两件事）。
  final ContentSearchResult? search;

  /// 底部状态行的动作回声（'' = 这一拍没有刚做完的事要说）。
  final String echo;

  /// 换章在途。
  final bool chapterLoading;

  /// 这是第几次"整篇换章"（`_loadChapter` 每跑一次加一）。
  ///
  /// 屏上要用它分清**落位的两种来路**：这一次落点是刚载入的那一章带来的（要滚过去），
  /// 还是用户自己滚出来的（不许再滚——否则每跨一段就把视口弹到那一段的段首，
  /// 用户会觉得书在跟他抢方向盘）。只按 `chapterUrl` 分不行：同一章可以重开
  /// （点书签跳回本章、目录里再点一次），那时 url 没变而落点变了。
  ///
  /// **追加下一章不减也不加这一格**：追加不动视口，加一次就把刚立好的落位重新跑一遍，
  /// 用户读到哪儿就被弹回哪儿。
  final int loadSeq;

  MountedChapter? mountFor(int index) {
    for (final mount in mounts) {
      if (mount.chapterIndex == index) return mount;
    }
    return null;
  }

  /// 视口那章落在挂载表里时用它，否则用最后一章（跟进在途、表还没换过来的那一拍）。
  MountedChapter? get activeMount =>
      mountFor(chapterIndex) ?? (mounts.isEmpty ? null : mounts.last);

  /// 当前章的文档（顶栏、书签摘要、`totalChars` 都读它）。
  ChapterDocument get document => activeMount?.document ?? _emptyDocument;

  /// 当前章的自然段（章内口径）。
  List<String> get paragraphs => activeMount?.paragraphs ?? const <String>[];

  /// 当前章各段段首的章内字符偏移。
  List<int> get offsets => activeMount?.offsets ?? const <int>[];

  /// 整篇段数（各章段数之和，**不是**屏上行数）。
  int get paragraphTotal =>
      mounts.fold(0, (sum, mount) => sum + mount.paragraphCount);

  /// 折算层吃的那一张表。
  ContinuousDocument get continuous =>
      ContinuousDocument(<ChapterMount>[for (final m in mounts) m.toMount()]);

  /// 整篇替换的判据：键表与落位只认这一格，追加进来的章不许把已挂出的键清掉。
  String get baseChapterUrl => mounts.isEmpty ? '' : mounts.first.chapterUrl;

  ChapterRow? get currentChapter =>
      (chapterIndex >= 0 && chapterIndex < chapters.length)
          ? chapters[chapterIndex]
          : null;

  /// 落点那把尺的右端点。**用服务端的 [ChapterDocument.totalChars]**，不用
  /// `chapterTextLength(paragraphs)`：两个数相等正是本轮要比的那件事，
  /// 在这里当同一个数用就等于把判据写进前提。
  int get totalChars => document.totalChars;

  /// 卷标题不产段，所以"第 n/N 章"的 N 与 flet 一样数滤掉卷标题之后的那批。
  String get progressText {
    final total = readableCount;
    if (total == 0 || chapterIndex < 0) return '未选择章节';
    final before =
        chapters.take(chapterIndex).where((c) => !c.isVolume).length;
    return '${before + 1}/$total · ${currentChapter?.name ?? ''}';
  }

  int get readableCount => chapters.where((c) => !c.isVolume).length;

  /// 底部那一行：`《书名》 · 作者 · 共 N 章`（flet `_update_chapter_navigation`）。
  String get bookLine => <String>[
        if (session.book.name.isNotEmpty) '《${session.book.name}》',
        if (session.book.author.isNotEmpty) session.book.author,
        '共 ${chapters.length} 章',
      ].where((part) => part.isNotEmpty).join(' · ');

  /// 段号 → 章内字符偏移（**存档**方向；这一发只走段号，不从像素猜）。
  int characterOffsetFor(int paragraph) =>
      paragraphs.isEmpty ? 0 : offsetOfParagraph(paragraphs, paragraph);

  /// 按方向找最近一章可读（跳过卷标题与空 url，到边返回 null）。
  int? nextIndexIn(int direction) {
    if (chapters.isEmpty || chapterIndex < 0) return null;
    var index = chapterIndex + direction;
    while (index >= 0 && index < chapters.length) {
      final candidate = chapters[index];
      if (!candidate.isVolume && candidate.url.isNotEmpty) return index;
      index += direction;
    }
    return null;
  }

  /// 段号夹进**当前这一章**（跳书签/跳命中吃的都是章内段号）。
  int clampParagraph(int paragraph) =>
      activeMount?.clampParagraph(paragraph) ?? -1;

  ReaderPage copyWith({
    ReaderSession? session,
    List<ChapterRow>? chapters,
    List<MountedChapter>? mounts,
    List<DocumentLine>? documentLines,
    int? chapterIndex,
    ReaderTypography? typography,
    int? anchorParagraph,
    int? loadSeq,
    List<BookmarkRow>? bookmarks,
    ContentSearchResult? search,
    String? echo,
    bool? chapterLoading,
  }) =>
      ReaderPage(
        session: session ?? this.session,
        chapters: chapters ?? this.chapters,
        mounts: mounts ?? this.mounts,
        documentLines: documentLines ?? this.documentLines,
        chapterIndex: chapterIndex ?? this.chapterIndex,
        typography: typography ?? this.typography,
        anchorParagraph: anchorParagraph ?? this.anchorParagraph,
        loadSeq: loadSeq ?? this.loadSeq,
        bookmarks: bookmarks ?? this.bookmarks,
        search: search ?? this.search,
        echo: echo ?? this.echo,
        chapterLoading: chapterLoading ?? this.chapterLoading,
      );

  @override
  String toString() => 'ReaderPage(${document.chapterName}, '
      '${mounts.length} 章/$paragraphTotal 段, 落点 $anchorParagraph)';
}

const ChapterDocument _emptyDocument = ChapterDocument(
    chapterUrl: '',
    chapterIndex: 0,
    chapterName: '',
    title: '',
    imageStyle: '',
    blocks: <ContentBlock>[],
    paragraphCount: 0,
    totalChars: 0);

/// 退出阅读第一跳的结论：要不要问那一句"放入书架吗"。
class ReaderExit {
  const ReaderExit({required this.staged, required this.closed});

  /// 非 null = 这是一本试读暂存的书，必须问。
  final StagedRecord? staged;

  /// 会话这会儿已经关掉了没。要问的那一臂它是 false——收尾那一发等用户答完才发。
  final bool closed;

  bool get needsAnswer => staged != null && !closed;

  @override
  String toString() => 'ReaderExit(${staged?.displayName ?? '不问'}, closed=$closed)';
}

final AsyncNotifierProvider<ReaderNotifier, ReaderPage> readerProvider =
    AsyncNotifierProvider<ReaderNotifier, ReaderPage>(
  ReaderNotifier.new,
  // 与书架那条同源（记忆 `riverpod-provider-default-retry`）：默认策略会把一发
  // `reader.open` 超时变成背靠背 11 发，而每一发都会重新拉一遍目录与正文。
  retry: (_, _) => null,
);

class ReaderNotifier extends AsyncNotifier<ReaderPage> {
  /// 落库合流窗口：与 flet 的 `throttle(…, 0.5)` 同值。
  static const Duration _saveWindow = Duration(milliseconds: 500);

  /// 离文档底部不足这一格就接下一章（flet `_APPEND_TRIGGER_DISTANCE`）。
  static const double _appendTriggerPx = 180;

  /// 退回这一格以上才把"自动续读"重新武装（flet `_APPEND_REARM_DISTANCE`）。
  ///
  /// 两个数中间那一段是**迟滞带**，不是冗余：只留一条线的话，贴着线的那几帧
  /// 会反复触发，一章能被接两遍（屏上出现两份同样的正文）。
  static const double _appendRearmPx = 500;

  Timer? _saveTimer;

  /// 换章那一拍的布局噪音不许落库（规矩 4）。
  bool _restoring = false;

  /// 当前这一章内的像素偏移（落 `scroll_offset`；由 [Landing.chapterPixels] 折算而来）。
  double _pixels = 0;

  /// 追加下一章在途。只挡"再要一章"，绝不挡定位与落库：文档在取下一章时仍然可滚。
  bool _appending = false;

  /// 自动续读是否武装（见 [_appendRearmPx] 那条迟滞）。整篇替换重新置真。
  bool _autoScrollArmed = true;

  /// 最近一次折算出的**章内字符偏移**（含段内那一截的估算，与 flet `track` 同一份算术）。
  /// `null` = 换章之后还没滚过（那时存档用段首，见 [_writePosition]）。
  int? _landedOffset;

  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  Stream<EngineFailure> get failures => _failures.stream;

  EngineRepository get _repository => ref.read(engineRepositoryProvider);

  ReaderPage get current => state.value ?? ReaderPage.idle;

  @override
  Future<ReaderPage> build() async {
    ref.onDispose(() {
      _saveTimer?.cancel();
      unawaited(_failures.close());
    });
    final args = ref.read(readerArgsProvider);
    // 忘注入 args 时**不许**开一本空书：`reader.open` 会拿空快照建出一个
    // 书名空白、0 章的会话，屏幕上看着像"这本书坏了"，而真因是路由少了一行。
    if (args.isEmpty) {
      throw EngineFailure(EngineFailureKind.badRequest,
          '阅读器没有收到书：路由未注入 readerArgsProvider'
          '（书架或找书那一屏没把书快照递进来）',
          action: 'reader.open');
    }
    final typography = await _loadTypography();
    final session = await _repository.openReader(
      sourceUrl: args.sourceUrl,
      book: args.book,
      shelve: args.shelve,
      resume: args.resume,
    );
    final chapters = await _repository.chapterRows(
        sourceUrl: session.sourceUrl, bookUrl: session.bookUrl);
    final stored = session.position;
    final page = await _loadChapter(
      session: session,
      chapters: chapters,
      chapterIndex: _clampChapter(stored.chapterIndex, chapters),
      typography: typography,
      storedParagraph: stored.paragraphIndex,
      storedOffset: stored.characterOffset,
    );
    // 书签跟着开一次：面板一打开就有现成的那一批，不让用户等第二跳。
    // 这一发失败不许拖垮开书：没书签的阅读器照样能读（flet 那边同样只 warning）。
    final marks = await _attempt<List<BookmarkRow>>(
        'reader.bookmarks', () => _repository.listBookmarks(session.bookUrl),
        or: () => const <BookmarkRow>[]);
    return page.copyWith(bookmarks: marks);
  }

  static int _clampChapter(int index, List<ChapterRow> chapters) {
    if (chapters.isEmpty) return -1;
    if (index < 0) return 0;
    return index >= chapters.length ? chapters.length - 1 : index;
  }

  void _emit(ReaderPage next) => state = AsyncValue<ReaderPage>.data(next);

  /// 一发副作用：失败走 [failures] 出声、屏上留原样，回退值只为让这一发有类型。
  ///
  /// [fallback] 写成 `Future<T> Function()` 而不是 `T`：`reader.toc` 失败要留
  /// **当前那一批目录**，而当前值只能在调用处才拿得到。
  Future<T> _attempt<T>(
      String action, Future<T> Function() body, {required T Function() or}) async {
    try {
      return await body();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return or();
    } catch (exc) {
      _failures.add(EngineFailure(EngineFailureKind.engine, '$exc', action: action));
      return or();
    }
  }

  Future<ReaderTypography> _loadTypography() async {
    final settings = await _attempt<SettingsMap>(
        'settings.read',
        () => _repository.readSettings(
            <String>[ReaderTypography.kFontSizeMode, ReaderTypography.kIndentMode]),
        or: () => SettingsMap.empty);
    return ReaderTypography.fromSettings(settings);
  }

  /// 读一章并**整篇替换**屏上那一篇文档（顶栏点进来的、目录、上下章、书签、命中都走这条）。
  ///
  /// 三把读数（段、段首偏移、屏上行）在这里一次算好挂进 [MountedChapter]：
  /// 追加那一臂（[_appendNextChapter]）走的是同一份挂载，只是不清旧文档、起点不归零。
  Future<ReaderPage> _loadChapter({
    required ReaderSession session,
    required List<ChapterRow> chapters,
    required int chapterIndex,
    required ReaderTypography typography,
    int? storedParagraph,
    int storedOffset = 0,
  }) async {
    final chapter = (chapterIndex >= 0 && chapterIndex < chapters.length)
        ? chapters[chapterIndex]
        : null;
    // 整篇替换：这一拍的布局通知是噪音，不是"用户读到了章首"（规矩 4）。
    _restoring = true;
    _pixels = 0;
    _landedOffset = null;
    // 文档已经换成一篇：接着往下读要重新接一次下一章，武装状态得跟着重来
    // （flet 那边是 `_auto_scroll_armed = True`，在同一处 `if not append`）。
    _autoScrollArmed = true;
    final document = await _attempt<ChapterDocument>('reader.content', () {
      if (chapter == null) return Future<ChapterDocument>.value(_emptyDocument);
      return _repository.chapterDocument(
          sourceUrl: session.sourceUrl,
          bookUrl: session.bookUrl,
          chapterUrl: chapter.url);
    }, or: () => _emptyDocument);
    final mount = MountedChapter.mount(
      chapterIndex: chapterIndex,
      chapterName: chapter?.name ?? document.chapterName,
      document: document,
      indent: typography.indent,
    );
    final anchor = mount.paragraphs.isEmpty
        ? -1
        : pageAnchor(mount.paragraphs, storedParagraph, storedOffset);
    final mounts = <MountedChapter>[mount];
    return ReaderPage(
      session: session,
      chapters: chapters,
      mounts: mounts,
      documentLines: documentLinesOf(mounts),
      chapterIndex: chapterIndex,
      typography: typography,
      anchorParagraph: anchor,
      loadSeq: current.loadSeq + 1,
      bookmarks: current.bookmarks,
      echo: chapter == null ? '这本书没有目录' : '',
    );
  }

  /// 读到章尾：把下一章**接**在当前文档末尾（分隔线 + 章名 + 正文），视口一点都不动。
  ///
  /// 三条不许：
  /// - 不许动 `loadSeq`——动了屏上就会为"新的一拍"重跑落位，用户读到哪儿被弹回哪儿；
  /// - 不许把 `paragraph_start` 数成都市的行数——媒体块、刚补的分隔线与章名都占行不产段，
  ///   数错了整篇段号就整体漂移（[MountedChapter.paragraphStart] 那条）；
  /// - 不许接已经在屏上的那一章（[_maybeAppendNext] 查的是挂载表本身，不是另立的台账）。
  ///
  /// [documentStart] 是触发那一发滚动里的**旧**文档总高：它是本章的像素起点，
  /// 也是两套尺里较不可信的那一套（见 `continuous_reading.dart` 文件头）。
  Future<void> _appendNextChapter({
    required int chapterIndex,
    required double documentStart,
    required int loadSeq,
  }) async {
    _appending = true;
    try {
      final page = current;
      if (page.loadSeq != loadSeq) return; // 在途的路上翻了章：这篇文档已经不是那一篇
      final chapter = page.chapters[chapterIndex];
      final document = await _attempt<ChapterDocument>(
          'reader.content',
          () => _repository.chapterDocument(
              sourceUrl: page.session.sourceUrl,
              bookUrl: page.session.bookUrl,
              chapterUrl: chapter.url),
          // 接失败就挂一章空的：屏上有分隔线与章名、正文空着，用户知道是这一章没读出来。
          // 代价折算层看得见——段表空 = 这张表不可信，落点退回像素口径（那是设计上认的降级）。
          or: () => _emptyDocument);
      // 取正文这一等就是几百毫秒到几秒：这期间用户可能翻了章（上面那条判据管的是
      // **开始之前**），也可能这一章已经被接上了（另一发滚动的判据在途）。
      final now = current;
      if (now.loadSeq != loadSeq || now.mountFor(chapterIndex) != null) return;
      final append = MountedChapter.mount(
        chapterIndex: chapterIndex,
        chapterName: chapter.name,
        document: document,
        indent: now.typography.indent,
        paragraphStart:
            now.paragraphTotal, // 已经挂在屏上的那些**段**，不是行
        documentStart: documentStart,
      );
      _emit(_withMounts(now, <MountedChapter>[...now.mounts, append]));
    } finally {
      _appending = false;
    }
  }

  /// 挂载表变了就要重算整篇行表（段号是整篇口径，起点跟着挂载顺序走）。
  static ReaderPage _withMounts(ReaderPage page, List<MountedChapter> mounts) =>
      page.copyWith(mounts: mounts, documentLines: documentLinesOf(mounts));

  /// 恢复落点：段序号优先，没有才按字符偏移算（规矩 3）。越界夹到最后一段。
  static int pageAnchor(List<String> paragraphs, int? storedParagraph, int storedOffset) {
    if (paragraphs.isEmpty) return -1;
    if (storedParagraph != null && storedParagraph >= 0) {
      return storedParagraph >= paragraphs.length
          ? paragraphs.length - 1
          : storedParagraph;
    }
    return paragraphAtOffset(paragraphs, storedOffset).paragraphIndex;
  }

  /// 翻到第 [index] 章（点目录 / 上下章 / 跳书签 / 跳命中都走这一条：整篇替换）。
  Future<void> openChapter(int index) async {
    final page = current;
    if (index < 0 || index >= page.chapters.length) return;
    _emit(page.copyWith(chapterLoading: true));
    // 换章前把旧章的落点写完：合流窗口还没到点的那一发不写就丢了
    // （滚两下立刻翻章是常规操作，不是意外）。
    await _writePosition();
    _emit(await _loadChapter(
      session: page.session,
      chapters: page.chapters,
      chapterIndex: index,
      typography: page.typography,
    ));
  }

  Future<void> stepChapter(int direction) async {
    final next = current.nextIndexIn(direction);
    if (next == null) return;
    await openChapter(next);
  }

  Future<void> prevChapter() => stepChapter(-1);

  Future<void> nextChapter() => stepChapter(1);

  /// 目录重拉（`refresh: true` 那一臂：源上目录换过才拿得到新行）。
  Future<void> refreshToc() async {
    final page = current;
    var refreshFailed = false;
    final chapters = await _attempt<List<ChapterRow>>(
        'reader.toc',
        () => _repository.chapterRows(
            sourceUrl: page.session.sourceUrl,
            bookUrl: page.session.bookUrl,
            refresh: true),
        // 失败留旧目录：把目录清空看起来像"这本书没目录了"，而真因是那一发没成。
        or: () {
          refreshFailed = true;
          return page.chapters;
        });
    _emit(page.copyWith(
      chapters: chapters,
      chapterIndex: _clampChapter(page.chapterIndex, chapters),
      // 失败时不许在同一帧说"已刷新"：那句与横幅里的错误并存，用户不知道信哪一句
      // （状态行只有一个槽，`StateBar` 文件头那条）。
      echo: refreshFailed
          ? '目录没刷新，留的是原来那 ${chapters.length} 章'
          : '目录已刷新：共 ${chapters.length} 章',
    ));
  }

  /// 屏幕报视口：整篇那一段在顶上 + 文档像素 + 文档总高。
  ///
  /// [documentParagraph] 是**整篇**段号（连续阅读时顶上那一段前面还挂着别的章；
  /// 量不到就传 null）。折算就在这一步：整篇段号 → (哪一章, 章内段号, 章内字符偏移)，
  /// 与 flet 的 `session.track` 同一份算术（`domain/continuous_reading.dart`，
  /// 那张参照表逐发钉着它）。顺带在这一发里判"要不要接下一章"——flet 同一条：
  /// 定位、落库、续读这三件事都挂在滚动通知上。
  void noteViewport({
    required double pixels,
    required double documentExtent,
    int? documentParagraph,
  }) {
    final page = current;
    if (_restoring || page.mounts.isEmpty) return;
    final landing = page.continuous.land(
        pixels: pixels,
        documentExtent: documentExtent,
        documentParagraph: documentParagraph);
    if (landing != null) _followViewport(page, landing);
    _maybeAppendNext(pixels: pixels, documentExtent: documentExtent);
  }

  /// 视口走到已追加的那一章：章号与段号跟着走、落库跟着改，**正文一行都不动**。
  ///
  /// flet 那边叫 `_follow_visible_chapter`（"标题/计数跟着走，但正文一篇都不动"）。
  /// 要紧的是落库那一头：挂着三章而眼睛在第二章中段时，进度必须记第二章——
  /// 记"当初点进来的那一章"就是把半章之后的阅读存成了半章之前。
  void _followViewport(ReaderPage page, Landing landing) {
    final local = landing.localParagraph;
    final mount = page.mountFor(landing.chapterIndex);
    if (local == null || mount == null) {
      // 这一屏量不到段号（顶上只挂着媒体行）或那一章不在表里：像素可以记，段号不许猜。
      _pixels = landing.chapterPixels;
      return;
    }
    // 夹进本章：像素口径那一臂（那张表不可信时走的）能把整篇段号减成越界的数，
    // 越界的段号落库，重开时被服务端夹回来——用户就从这一章的中段被弹到别处。
    final clamped = mount.clampParagraph(local);
    if (clamped < 0) return;
    _pixels = landing.chapterPixels;
    // 段内那一截是估算，但它是**这一发**折算的估算：存档读的是它，屏上顶着的才是
    // 同一段（两个数各存一处就是两套落点）。
    _landedOffset = landing.characterOffset;
    if (clamped == page.anchorParagraph && landing.chapterIndex == page.chapterIndex) {
      return;
    }
    _emit(page.copyWith(chapterIndex: landing.chapterIndex, anchorParagraph: clamped));
    _scheduleSave();
  }

  /// 到底了就接下一章（flet `_on_reader_scroll` 最后那一段）。
  ///
  /// 三道闸缺一不可：
  /// - **迟滞**：先退到 [_appendRearmPx] 之外重新武装，才能在 [_appendTriggerPx] 之内
  ///   触发一次。只留一条阈值线的话，贴线那几帧会反复触发，一章被接两遍；
  /// - **在途**：`_appending` 只挡"再要一章"，绝不挡定位与落库（文档取下一章时仍然可滚，
  ///   一起挡住进度就再也不更新了——flet 的 `_reading_loading` 同一分际）；
  /// - **已在屏上**：查的是挂载表本身。flet 用的是另一本台账 `_continuous_loaded`，
  ///   而那本只在换书时清，于是"翻到第 3 章 → 退回第 2 章 → 读到底"会把第 3 章永远拒掉。
  void _maybeAppendNext(
      {required double pixels, required double documentExtent}) {
    if (documentExtent <= 0 || _appending) return;
    final distance = documentExtent - pixels;
    if (distance > _appendRearmPx) {
      _autoScrollArmed = true;
      return;
    }
    if (distance >= _appendTriggerPx || !_autoScrollArmed) return;
    final page = current;
    final next = page.nextIndexIn(1);
    if (next == null || page.mountFor(next) != null) return;
    _autoScrollArmed = false;
    unawaited(_appendNextChapter(
      chapterIndex: next,
      documentStart: documentExtent,
      loadSeq: page.loadSeq,
    ));
  }

  /// 恢复滚动已经兑现：从这一拍起的视口通知才是用户滚出来的。
  /// 屏幕把 `anchorParagraph` 落位之后调它（规矩 4 的另一半）。
  void anchorApplied() => _restoring = false;

  void _scheduleSave() {
    _saveTimer?.cancel();
    _saveTimer = Timer(_saveWindow, () => unawaited(_writePosition()));
  }

  /// 落一篇进度。回话里那一份**才是存进去的**（越界偏移会被服务端夹回），
  /// 所以界面不拿自己算的那个数当结论。
  Future<void> _writePosition() async {
    _saveTimer?.cancel();
    final page = current;
    if (page.chapterIndex < 0) return;
    final paragraph = page.anchorParagraph < 0 ? 0 : page.anchorParagraph;
    // 滚过就用折算出来的那一个数（段首 + 段内那一截的估算，与 flet `track` 同一份算术，
    // 由 `continuous_reading_projection_test` 对着 flet 的参照表逐发核过）；
    // 还没滚过就存**段首**——那是事实，不是猜出来的。
    final offset = _landedOffset ?? page.characterOffsetFor(paragraph);
    final position = ReadingPosition(
      chapterUrl: page.document.chapterUrl,
      chapterName: page.currentChapter?.name ?? page.document.chapterName,
      chapterIndex: page.chapterIndex,
      scrollOffset: _pixels,
      scrollRatio: page.totalChars <= 0 ? 0 : offset / page.totalChars,
      characterOffset: offset,
      paragraphIndex: paragraph,
      fontSize: page.typography.fontSize,
      lastReadAt: DateTime.now().millisecondsSinceEpoch ~/ 1000,
    );
    final stored = await _attempt<ReadingPosition?>(
        'reader.position_save',
        () => _repository.savePosition(
            sourceUrl: page.session.sourceUrl,
            bookUrl: page.session.bookUrl,
            positionJson: position.toJson()),
        or: () => null);
    if (stored != null && stored.characterOffset != offset) {
      // 服务端夹过就是夹过：把屏上的段号挪到它认的那一段，别让存档与屏幕各说一套。
      // 折算那一格跟着作废——它已经是**上一发**存档时那一段的数，留着就是拿旧估算配新段号。
      _landedOffset = null;
      _emit(page.copyWith(
          anchorParagraph:
              paragraphAtOffset(page.paragraphs, stored.characterOffset).paragraphIndex));
    }
  }

  /// 立刻落库（退出、翻章、以及测试要的那一发"不等合流窗口"）。
  Future<void> flushPosition() => _writePosition();

  // ------------------------------------------------------------------ #
  // 排版偏好
  // ------------------------------------------------------------------ #

  /// 改一项排版：就地重排已挂出的那一批行，再把**夹过之后**的值落库。
  ///
  /// 落的是夹过的值（flet `set_preference` 同一条）：否则设置表里存着 900、
  /// 下次开图又变回 760，用户看到的是"设置没保存"。
  Future<void> setTypography(ReaderTypography next) async {
    final page = current;
    final applied = ReaderTypography(
        fontSize: ReaderTypography.clampFontSize(next.fontSize),
        indent: next.indent);
    if (applied.fontSize == page.typography.fontSize &&
        applied.indent == page.typography.indent) {
      return;
    }
    // 缩进改的是**画法**，尺不动：重排每一章已挂出的那些行，而重新拉正文是白跑一发网络。
    // 只重排"当前那一章"是连续阅读下最容易犯的错：接上来的两章会顶着旧缩进，
    // 整篇行表（段号、`lastInChapter`）就得从这份不一致的各行里拼。
    // 字号那一格不用重排——它由屏上的 `TextStyle` 现读，不落进行表。
    final mounts = applied.indent == page.typography.indent
        ? page.mounts
        : <MountedChapter>[
            for (final mount in page.mounts) mount.withIndent(applied.indent)
          ];
    _emit(_withMounts(page, mounts).copyWith(typography: applied));
    // 一键一发，只写**变了的那一格**（flet `set_preference(key, value)` 同一条）：
    // 把两格都写一遍的话，改缩进会顺带落一发没变过的字号，而那一发在设置表上是
    // 一次真实的写入与一次变更广播。
    final before = page.typography.toSettings();
    for (final entry in applied.toSettings().entries) {
      if (before[entry.key] == entry.value) continue;
      await _attempt<bool>('settings.write', () async {
        await _repository.writeSetting(entry.key, entry.value);
        return true;
      }, or: () => false);
    }
  }

  Future<void> changeFontSize(int delta) =>
      setTypography(current.typography.withFontSize(delta));

  Future<void> toggleIndent() =>
      setTypography(current.typography.withIndent(!current.typography.indent));

  // ------------------------------------------------------------------ #
  // 书签
  // ------------------------------------------------------------------ #

  Future<void> refreshBookmarks() async {
    final page = current;
    final marks = await _attempt<List<BookmarkRow>>(
        'reader.bookmarks', () => _repository.listBookmarks(page.session.bookUrl),
        // 失败留旧那一批：面板清空看起来像"书签都丢了"。
        or: () => page.bookmarks);
    _emit(current.copyWith(bookmarks: marks));
  }

  /// 加一条书签。`offset` 那一格存的是**章内比例**（`chapter_ratio`），不是字符偏移：
  /// 服务端那一列当初给的就是"大概翻到哪儿"，混用会偏半屏（`BookmarkRow` 文件头）。
  Future<void> addBookmark() async {
    final page = current;
    final offset = page.characterOffsetFor(page.anchorParagraph < 0 ? 0 : page.anchorParagraph);
    final ratio = page.totalChars <= 0
        ? 0.0
        : (offset / page.totalChars).clamp(0.0, 1.0);
    final summary = page.currentChapter?.name ?? '';
    final id = await _attempt<int>('reader.bookmark_add',
        () => _repository.addBookmark(
            bookUrl: page.session.bookUrl,
            chapterIndex: page.chapterIndex,
            offset: ratio,
            summary: summary),
        or: () => 0);
    if (id <= 0) return;
    await refreshBookmarks();
    _emit(current.copyWith(echo: '已加书签：$summary'));
  }

  Future<void> removeBookmark(int bookmarkId) async {
    final page = current;
    final removed = await _attempt<bool>(
        'reader.bookmark_remove',
        () => _repository.removeBookmark(
            bookUrl: page.session.bookUrl, bookmarkId: bookmarkId),
        or: () => false);
    await refreshBookmarks();
    _emit(current.copyWith(
        echo: removed ? '书签已删除' : '那条书签已经不在了（可能被另一侧删过）'));
  }

  /// 跳书签：flet 只按 `chapter_index` 重开那一章、**没有落到 offset**
  /// （`reader_controller.py:939-943`）。这里照做——两侧同一张书签表才落同一处。
  Future<void> jumpToBookmark(BookmarkRow row) => openChapter(row.chapterIndex);

  // ------------------------------------------------------------------ #
  // 书内搜索
  // ------------------------------------------------------------------ #

  /// 空关键词不发这一发：发了就是"搜了整本书、0 命中"，而真因是用户没打字。
  Future<void> searchInBook(String keyword) async {
    final trimmed = keyword.trim();
    final page = current;
    if (trimmed.isEmpty) {
      _emit(page.copyWith(echo: '要搜什么总得给两个字'));
      return;
    }
    final result = await _attempt<ContentSearchResult?>(
        'reader.search_content',
        () => _repository.searchBookContent(
            bookUrl: page.session.bookUrl,
            keyword: trimmed,
            sourceUrl: page.session.sourceUrl),
        // null = 那一发没成，与"搜到了 0 条"是两件事（面板据此不画结果列表）。
        or: () => null);
    if (result == null) return;
    _emit(current.copyWith(search: result, echo: searchEcho(result, trimmed)));
  }

  /// 三种 0 命中要说三句话（记忆 `zero-counts-need-a-positive-control`）：
  /// 没 FTS5、没索引、真没有这个词——合成一句就是把故障说成"这本书没有"。
  static String searchEcho(ContentSearchResult result, String keyword) {
    if (result.hits.isNotEmpty) return '「$keyword」命中 ${result.hits.length} 处';
    if (!result.ftsAvailable) return '这台机器的 SQLite 没编 FTS5，书内搜索用不了';
    if (result.indexedChapters <= 0) return '还没有任何一章进了索引（先读几章再搜）';
    return '「$keyword」在已索引的 ${result.indexedChapters} 章里没有';
  }

  /// 跳命中：整篇换成那一章，再落到 `para_index` 那一段。
  /// `para_index` 是**章内**段序号，与这里数段同一个口径（`app.core.text_layout`）。
  Future<void> jumpToHit(ContentHit hit) async {
    await openChapter(hit.chapterIndex);
    final page = current;
    final paragraph = page.clampParagraph(hit.paraIndex);
    if (paragraph < 0) return;
    _emit(page.copyWith(anchorParagraph: paragraph));
    // 命中的段号是索引那边按同一把尺数出来的：落点一起写回去，
    // 用户跳完就退出的话，存档要指在他刚看的那一段。
    _scheduleSave();
  }

  // ------------------------------------------------------------------ #
  // 退出（试读收尾）
  // ------------------------------------------------------------------ #

  /// 退出第一跳：先落一次进度，再**问一句** `reader.staged` 决定要不要弹窗。
  ///
  /// 顺序在这里是硬约束（规矩 6）：`reader.close` 会把会话的当前书与源清空，
  /// 关完之后服务再也认不出那条暂存记录，"放入书架"就变成一次没用的点击。
  /// 所以这一跳**只在不需要问的那一臂**关会话；要问的那一臂把关闭留给 [finishExit]。
  Future<ReaderExit> beginExit() async {
    final page = current;
    await _writePosition();
    // 问不到就当不问（回退 null）：那时会话会被关掉、暂存记录原样留着，
    // 交给启动清理——比"猜一个答案把它删了"轻得多。
    final staged = await _attempt<StagedRecord?>(
        'reader.staged', _repository.stagedRecord, or: () => null);
    if (staged != null) return ReaderExit(staged: staged, closed: false);
    final outcome = await _attempt<CloseOutcome>(
        'reader.close',
        () => _repository.closeReader(
            sourceUrl: page.session.sourceUrl,
            bookUrl: page.session.bookUrl,
            saveProgress: true),
        or: () => const CloseOutcome(closed: false, staged: null));
    return ReaderExit(staged: outcome.staged, closed: outcome.closed);
  }

  /// 退出第二跳：用户答完"放入书架吗"之后（[beginExit] 回 `needsAnswer` 时才会走到这）。
  ///
  /// 顺序是硬的（规矩 6）：转正/放弃排在 `reader.close` **之前**——关会话会把当前
  /// 书与源清空，之后服务再也认不出那条暂存记录。
  Future<bool> finishExit({bool? shelve}) async {
    final page = current;
    if (shelve == true) {
      await _attempt<bool>('reader.staged_promote', _repository.promoteStaged,
          or: () => false);
    } else if (shelve == false) {
      await _attempt<bool>('reader.staged_discard', _repository.discardStaged,
          or: () => false);
    }
    final closed = await _attempt<CloseOutcome>(
        'reader.close',
        () => _repository.closeReader(
            sourceUrl: page.session.sourceUrl,
            bookUrl: page.session.bookUrl,
            // 不放入：进度与缓存由 `staged_discard` 一起删了，这一发别再落一遍。
            saveProgress: shelve != false),
        or: () => const CloseOutcome(closed: false, staged: null));
    return closed.closed;
  }
}
