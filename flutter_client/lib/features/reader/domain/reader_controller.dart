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
/// **G3-C3 不含连续阅读（到底自动接下一章）**：flet 那条是 `append` 文档 +
/// 章节视口表（`_mount_chapter`），落点要按"本章在整篇文档里的段起点"折算；
/// 而 Dart 这一版一页一章、翻页走整篇替换，段号天然从 0 起，不需要那张表。
/// 追加那一臂与换源同批进 G3-D（都要 `mode="job"` 的在途快照）。
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

/// 一屏读完所需的全部状态（不可变；改一处就 `copyWith` 一份）。
class ReaderPage {
  const ReaderPage({
    required this.session,
    required this.chapters,
    required this.chapterIndex,
    required this.document,
    required this.paragraphs,
    required this.offsets,
    required this.lines,
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
    chapterIndex: -1,
    document: _emptyDocument,
    paragraphs: const <String>[],
    offsets: const <int>[],
    lines: const <ReaderLine>[],
    typography: ReaderTypography.defaults,
    anchorParagraph: -1,
  );

  final ReaderSession session;

  /// `reader.toc` 的原样行表（**不滤卷标题**：序号要与服务端的 `chapters[index]` 对齐）。
  final List<ChapterRow> chapters;

  /// 当前章在 [chapters] 里的下标 = 落库的 `chapter_index`。
  final int chapterIndex;

  final ChapterDocument document;

  /// 尺上的自然段（`chapterParagraphs(document.blocks)`，不带缩进）。
  final List<String> paragraphs;

  /// 各段段首的章内字符偏移（`paragraphOffsets(paragraphs)`）。
  final List<int> offsets;

  /// 屏上那一批行（[contentPlan] 的产物：正文段 + 媒体块，带缩进与标题画法）。
  final List<ReaderLine> lines;

  final ReaderTypography typography;

  /// 这一拍要落到的段号；`-1` = 不落（章首）。
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
  final int loadSeq;

  ChapterRow? get currentChapter =>
      (chapterIndex >= 0 && chapterIndex < chapters.length)
          ? chapters[chapterIndex]
          : null;

  /// 落点那把尺的右端点。**用服务端的 [ChapterDocument.totalChars]**，不用
  /// `chapterTextLength(paragraphs)`：两个数相等正是本轮要比的那件事，
  /// 在这里当同一个数用就等于把判据写进前提。
  int get totalChars => document.totalChars;

  /// 卷标题不产段，所以"第 n/N 章"的 N 与 flet 一样数滤掉卷标题之后的那批。
  int get readableCount => chapters.where((c) => !c.isVolume).length;

  /// 顶栏那一句：`第 n/N · 章名`（n 与分母同一把尺：都按滤卷之后的序号）。
  String get progressText {
    final total = readableCount;
    if (total == 0 || chapterIndex < 0) return '未选择章节';
    final before =
        chapters.take(chapterIndex).where((c) => !c.isVolume).length;
    return '${before + 1}/$total · ${currentChapter?.name ?? ''}';
  }

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

  /// 段号夹进本章。
  int clampParagraph(int paragraph) {
    if (paragraphs.isEmpty) return -1;
    if (paragraph < 0) return 0;
    return paragraph >= paragraphs.length ? paragraphs.length - 1 : paragraph;
  }

  ReaderPage copyWith({
    ReaderSession? session,
    List<ChapterRow>? chapters,
    int? chapterIndex,
    ChapterDocument? document,
    List<String>? paragraphs,
    List<int>? offsets,
    List<ReaderLine>? lines,
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
        chapterIndex: chapterIndex ?? this.chapterIndex,
        document: document ?? this.document,
        paragraphs: paragraphs ?? this.paragraphs,
        offsets: offsets ?? this.offsets,
        lines: lines ?? this.lines,
        typography: typography ?? this.typography,
        anchorParagraph: anchorParagraph ?? this.anchorParagraph,
        loadSeq: loadSeq ?? this.loadSeq,
        bookmarks: bookmarks ?? this.bookmarks,
        search: search ?? this.search,
        echo: echo ?? this.echo,
        chapterLoading: chapterLoading ?? this.chapterLoading,
      );

  @override
  String toString() =>
      'ReaderPage(${document.chapterName}, ${paragraphs.length} 段/'
      '$totalChars 字, 落点 $anchorParagraph)';
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

  Timer? _saveTimer;

  /// 换章那一拍的布局噪音不许落库（规矩 4）。
  bool _restoring = false;

  /// 屏幕报来的像素偏移。留着只为了把它一起写进 `scroll_offset`
  /// （下次在**同一个客户端**续读时落回原样），它从不参与跨端比对。
  double _pixels = 0;

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

  /// 读一章，并把三把读数一起算好（段、段首偏移、屏上行）。
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
    final document = await _attempt<ChapterDocument>('reader.content', () {
      if (chapter == null) return Future<ChapterDocument>.value(_emptyDocument);
      return _repository.chapterDocument(
          sourceUrl: session.sourceUrl,
          bookUrl: session.bookUrl,
          chapterUrl: chapter.url);
    }, or: () => _emptyDocument);
    final paragraphs = chapterParagraphs(document.blocks);
    final anchor = paragraphs.isEmpty
        ? -1
        : pageAnchor(paragraphs, storedParagraph, storedOffset);
    return ReaderPage(
      session: session,
      chapters: chapters,
      chapterIndex: chapterIndex,
      document: document,
      paragraphs: paragraphs,
      offsets: paragraphOffsets(paragraphs),
      lines: contentPlan(document, indent: typography.indent),
      typography: typography,
      anchorParagraph: anchor,
      loadSeq: current.loadSeq + 1,
      bookmarks: current.bookmarks,
      echo: chapter == null ? '这本书没有目录' : '',
    );
  }

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

  /// 屏幕报视口：哪一段在顶部 + 像素（像素只进 `scroll_offset`，不参与比对）。
  ///
  /// [paragraph] 是**章内**段序号（与 `reader.content` 数段同一个口径）。
  void noteViewport({required int paragraph, double pixels = 0}) {
    if (_restoring) return;
    _pixels = pixels < 0 ? 0 : pixels;
    final page = current;
    final clamped = page.clampParagraph(paragraph);
    if (clamped < 0 || clamped == page.anchorParagraph) return;
    _emit(page.copyWith(anchorParagraph: clamped));
    _scheduleSave();
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
    final offset = page.characterOffsetFor(paragraph);
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
    // 缩进改的是**画法**，尺不动：重算 lines 而重新拉正文是白跑一发网络。
    _emit(page.copyWith(
        typography: applied,
        lines: contentPlan(page.document, indent: applied.indent)));
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
