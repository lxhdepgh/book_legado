/// 阅读那一屏：正文行的形状与 flet 那屏同源（`reader_view.py::content_plan/render_plan`）。
///
/// 屏上的行**不自己切段**：走 [contentPlan]（`domain/text_layout.dart` 那份 Dart 尺），
/// 于是"第 n 段"这一格与 `reader.content` 回的 `paragraphCount`/`totalChars`、
/// 与 flet 的三张投影表数出来的是同一串字。为什么要紧：这一屏滚到哪一段，
/// 就是要落库的那个字符偏移（`offsetOfParagraph`）——行与尺不同源，落点就整体漂。
///
/// 连续阅读（G3-D2）之后"第 n 段"要说是**哪一篇**的第 n 段：读到章尾自动接下一章，
/// 屏上同时挂着 1~N 章，行表由 [ReaderPage.documentLines] 给（整篇口径的段号），
/// 每一章前面补分隔线 + 章名（flet 同两行，占行不产段）。滚到哪一段报回
/// `noteViewport(documentParagraph: …)`，由 `domain/continuous_reading.dart` 折算成章内。
///
/// 三处对齐看得见的文案（判据见 `test/reader_screen_test.dart`）：
/// - 顶栏进度 = `第 n/N · 章名`（flet `set_progress`，N 数**滤掉卷标题**之后的那批，
///   n 跟着**视口那一章**走，不是"当初点进来的那一章"）；
/// - 底部状态 = `《书名》 · 作者 · 共 N 章`（flet `set_status_bar`）；
/// - 段首缩进 `　　`、标题行不缩进且加粗、媒体块挂在正文之间但**不产段**。
///
/// 这一版仍不含：其余八格排版偏好（行距/版心/背景/划词……这一屏没有它们的控件，
/// 读了也用不上）、音视频的外部播放器（没有 url_launcher 依赖，这里只把地址显示出来）。
/// 这里不放点了没反应的按钮——占位会被当成已接上。反过来说，屏上每一颗按钮都发得出一发。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/envelope.dart';
import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../data/bookmark_row.dart';
import '../data/chapter_document.dart';
import '../data/chapter_row.dart';
import '../data/content_search_result.dart';
import '../domain/reader_controller.dart';
import '../domain/text_layout.dart';

/// 正文视口的顶端在这里往下让多少像素算"已经滚过"。
///
/// 不是审美：`localToGlobal` 拿到的行顶与视口顶在浮点布局下会差亚像素，
/// 严格 `<=` 会让"正好在顶端的那一段"报不出来，落点就退到上一段。
const double _viewportTolerancePx = 2;

/// 落位闭环收敛到这一条边之内就算到位（亚像素：布局自己就有 0.x 的舍入）。
const double _anchorTolerancePx = 0.5;

/// 往下"揭出"目标那一行的最多步数（一步 = 一屏）。
///
/// 上限是给"目标永远建不出来"那一臂兜底的（正文只有一屏时不会有下一行，
/// 但那一行的段号本该在表内、走的是 `ensureVisible` 那一支）。按本章字数与
/// 一屏的高度量，真到第 7 段那种深度也就十几步，120 是十几倍余量。
const int _maxRevealSteps = 120;

class ReaderScreen extends ConsumerStatefulWidget {
  const ReaderScreen({super.key});

  @override
  ConsumerState<ReaderScreen> createState() => _ReaderScreenState();
}

class _ReaderScreenState extends ConsumerState<ReaderScreen> {
  final ScrollController _controller = ScrollController();

  /// 段号 → 那一行的 GlobalKey。**只给建出来的那批行**（ListView 一屏十几个），
  /// 换章时整表丢掉——留着旧章的段号会把上一章的落点报进这一章里。
  ///
  /// 键是**整篇**段号：连续阅读时三章同时挂在屏上，而"第 0 段"每一章都有一个，
  /// 拿章内口径当键就是撞名（撞了就把上一章那一段报成这一章那一段，落点整体漂）。
  final Map<int, GlobalKey> _paragraphKeys = <int, GlobalKey>{};

  /// 上一次为其重建过键表的那一篇文档（判据是**第一个挂载章**的 url）。
  ///
  /// 不能用当前章的 url：视口跟着已追加的下一章走时 url 变了，而键表里的行还挂在
  /// 屏上——照着 url 清表就是"追加一章，前面所有段的落位能力全丢"。
  String _documentKey = '';

  /// 已经为第几次"整篇换章"落过位（与 [ReaderPage.loadSeq] 比）。
  ///
  /// 只数"章换了几次"而不数"落点变了几次"：用户自己滚出来的段号变化不能再回头看，
  /// 否则每跨一段就被弹回那一段的段首，像是书在跟他抢方向盘。
  int _anchoredLoad = -1;

  /// 这一屏自己那一份 Riverpod 容器（路由级 `ProviderScope` 注入 `readerArgsProvider` 的那一份）。
  late final ProviderContainer _container;

  @override
  void initState() {
    super.initState();
    _container = ProviderScope.containerOf(context, listen: false);
    _controller.addListener(_onScroll);
  }

  @override
  void dispose() {
    _controller.removeListener(_onScroll);
    _controller.dispose();
    super.dispose();
  }

  /// 滚动通知到达时**不许当场量几何**。
  ///
  /// 滚动的像素是先更新的：这一帧的 `pixels` 已经变了，而行（`GlobalKey` 那些）
  /// 还挂着上一帧的位置。当场读 `_paragraphAtViewportTop()` 读到的是**滚之前**顶上
  /// 那一段，落点于是永远慢一次滚动（本轮量到的形状：滚 -900 之后屏上报第 0 段，
  /// 再滚一小下才把第 1 段补上来）。合流窗口只有 0.5s，慢一次就是存错一整段。
  /// 推迟到这一帧布局走完再量，读到的才是屏幕上真顶着的那一段。
  bool _reportScheduled = false;

  void _onScroll() {
    if (_reportScheduled) return;
    _reportScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _reportScheduled = false;
      if (!mounted || !_controller.hasClients) return;
      final page = ref.read(readerProvider).value;
      if (page == null) return;
      final paragraph = _paragraphAtViewportTop();
      if (paragraph == null) return;
      ref.read(readerProvider.notifier).noteViewport(
            pixels: _controller.offset,
            documentExtent: _controller.position.maxScrollExtent,
            documentParagraph: paragraph,
          );
    });
  }

  /// 正文那一格（视口）顶边的屏幕 y——"哪一段在顶上"和落位收敛量的都是这一条边。
  double? _listTop() {
    final listBox =
        _controller.position.context.storageContext.findRenderObject();
    if (listBox is! RenderBox || !listBox.attached) return null;
    return listBox.localToGlobal(Offset.zero).dy;
  }

  /// 第 [paragraph] 段那一行顶边的屏幕 y；那一行这会儿没建出来就是 null。
  double? _rowTop(int paragraph) {
    final context = _paragraphKeys[paragraph]?.currentContext;
    final box = context?.findRenderObject();
    if (box is! RenderBox || !box.attached) return null;
    return box.localToGlobal(Offset.zero).dy;
  }

  /// 视口顶下面那一批行里，**顶边最靠近视口顶且已过顶**的那一行是哪一段。
  ///
  /// 为什么不按 `pixels / 行高` 算：正文行高不定（一段可能占 1 行也可能占 9 行），
  /// 等分出来的"第 n 行"与真实那一段最坏差七段——`#88` 取证一量的就是这种错尺。
  /// 连续阅读之后这一格更省不掉：章名行与分隔线插在中间，行号与段号的差每一章都不一样。
  int? _paragraphAtViewportTop() {
    final top = _listTop();
    if (top == null) return null;
    int? best;
    double bestDy = double.negativeInfinity;
    for (final entry in _paragraphKeys.entries) {
      final dy = _rowTop(entry.key);
      if (dy == null) continue;
      if (dy <= top + _viewportTolerancePx && dy > bestDy) {
        best = entry.key;
        bestDy = dy;
      }
    }
    return best;
  }

  /// 换整篇文档：键表整表作废（段号是新一篇自己的）。追加不走这一条。
  void _resetKeys(String documentKey) {
    if (_documentKey == documentKey) return;
    _documentKey = documentKey;
    _paragraphKeys.clear();
  }

  GlobalKey _keyFor(int paragraph) =>
      _paragraphKeys.putIfAbsent(paragraph, GlobalKey.new);

  /// 首帧之后把视口落到存档那一段，然后告诉控制器"恢复完了，可以开始记账"。
  ///
  /// 为什么不是"一帧 `ensureVisible` 就完事"：`ListView` 只把**视口附近那一截**建出来
  /// （5,802 字那一章约 9 屏高），存到第 7 段时那一行的 `GlobalKey` 还没有 context，
  /// 直接 ensureVisible 就什么都找不到——于是"恢复落点"对读到中间的书**静默失效**，
  /// 屏上还是章首，而用户以为进度丢了。所以先往目标那一边的方向一屏一屏跳，
  /// 跳到那一行被建出来，再精确落位（像素只是"够得着这一行"的手段，
  /// 落点本身仍按段号定，见规矩 3）。
  void _scheduleAnchor(ReaderPage page) {
    if (_anchoredLoad == page.loadSeq) return;
    _anchoredLoad = page.loadSeq;
    final documentKey = _documentKey;
    WidgetsBinding.instance.addPostFrameCallback(
        (_) => unawaited(_anchorTo(documentKey, page.anchorParagraph)));
  }

  /// 目标那一行没建出来时该往哪一跳：-1 = 它在上边，1 = 在下边，0/null = 别再跳。
  ///
  /// 方向不能写死成"总是往下"：换章时正文整篇替换，而 `ScrollController` 的像素
  /// **跟着留下**（上一本读到第 9 屏，翻一章就在第 9 屏打开）。那一屏里建出来的行
  /// 段号全都大于目标，往下跳只是越跳越远——最后停在半章上，还把这一章的进度
  /// 存成了那半章的段号。
  int _revealDirection(int paragraph) {
    int? low;
    int? high;
    for (final entry in _paragraphKeys.entries) {
      if (_rowTop(entry.key) == null) continue;
      if (low == null || entry.key < low) low = entry.key;
      if (high == null || entry.key > high) high = entry.key;
    }
    if (low == null || high == null) return 0;
    if (paragraph < low) return -1;
    if (paragraph > high) return 1;
    return 0;
  }

  Future<void> _anchorTo(String documentKey, int paragraph) async {
    for (var step = 0; step < _maxRevealSteps; step++) {
      if (!mounted || _documentKey != documentKey || !_controller.hasClients) {
        return;
      }
      final top = _listTop();
      if (top == null) return;
      final rowTop = _rowTop(paragraph);
      final delta = rowTop == null
          ? _revealDirection(paragraph) * _controller.position.viewportDimension
          : rowTop - top;
      // 落了位就收手；跳不动了（夹在滚动边界上，再跳像素不变）也收手——
      // 继续跳只是在等一帧什么都不会变的布局。
      if (delta.abs() <= _anchorTolerancePx) break;
      final before = _controller.offset;
      _controller.jumpTo(before + delta);
      await WidgetsBinding.instance.endOfFrame;
      if (_controller.hasClients &&
          (_controller.offset - before).abs() <= _anchorTolerancePx) {
        break;
      }
    }
    if (!mounted || _documentKey != documentKey) return;
    // 落位兑现（或已经到不该再滚的地方）：从这一拍起的滚动通知才是用户滚出来的
    // （规矩 4 的另一半——这一发不喊，之后用户滚到哪都不会落库）。
    ref.read(readerProvider.notifier).anchorApplied();
    // 窗口一关就要**补报一次当前视口**：规矩 4 会把恢复期间到达的通知丢掉，而
    // "开书后立刻往下 swipe"正是最常见的动作——不补报，那一次滚动永远不落库，
    // 用户以为进度没存（屏上没有第二次滚动通知来救它，因为滚动已经停在原地了）。
    // 用户没滚的那一臂补报的就是刚落位那一段，与 `anchorParagraph` 相等 → 不多发一发。
    _onScroll();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(readerProvider);
    final notifier = ref.read(readerProvider.notifier);
    return PopScope(
      // 退出要收尾（落进度 + 试读询问），所以先把关窗那一下拦下来自己走。
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        unawaited(_exit(notifier));
      },
      child: Scaffold(
        appBar: AppBar(
          // flet 那一屏工具栏的第一颗就是 `CLOSE`「退出阅读」（`reader_view.py:241`）。
          // 必须有这一颗：系统返回键之外，`PopScope(canPop: false)` 挡不住
          // go_router 自己那发 pop（它直接减页面列表，不走 `maybePop`），
          // 而退出要收尾（落进度 + 试读询问）——没有按钮，收尾就只剩系统键那一条路。
          leading: IconButton(
            key: const Key('reader.exit'),
            icon: const Icon(Icons.close),
            tooltip: '退出阅读',
            onPressed: () => unawaited(_exit(notifier)),
          ),
          title: Text(switch (state) {
            AsyncData(value: final page) =>
              page.currentChapter?.name ?? page.session.book.name,
            AsyncError() => '打开失败',
            _ => '正在开书…',
          }),
          actions: <Widget>[
            // 字号：flet 那屏是一条滑块，这里是一减一加两颗图标（同一条 `set_preference`，
            // 同一个夹框）。步长 1 与 flet 滑块的整数刻度一致。
            IconButton(
              key: const Key('reader.fontSmaller'),
              icon: const Icon(Icons.text_decrease_outlined),
              tooltip: '小一号',
              onPressed: state.hasValue
                  ? () => unawaited(notifier.changeFontSize(-1))
                  : null,
            ),
            IconButton(
              key: const Key('reader.fontLarger'),
              icon: const Icon(Icons.text_increase_outlined),
              tooltip: '大一号',
              onPressed: state.hasValue
                  ? () => unawaited(notifier.changeFontSize(1))
                  : null,
            ),
            IconButton(
              key: const Key('reader.indent'),
              icon: const Icon(Icons.format_indent_increase),
              tooltip: '段首缩进',
              onPressed: state.hasValue
                  ? () => unawaited(notifier.toggleIndent())
                  : null,
            ),
            IconButton(
              key: const Key('reader.search'),
              icon: const Icon(Icons.search),
              tooltip: '书内搜索',
              onPressed: state.hasValue ? () => _openSearch(notifier) : null,
            ),
            IconButton(
              key: const Key('reader.bookmarks'),
              icon: const Icon(Icons.bookmarks_outlined),
              tooltip: '书签',
              onPressed: state.hasValue ? () => _openBookmarks(notifier) : null,
            ),
            IconButton(
              key: const Key('reader.toc'),
              icon: const Icon(Icons.list),
              tooltip: '目录',
              onPressed: state.hasValue ? () => _openToc(notifier) : null,
            ),
          ],
        ),
        body: switch (state) {
          AsyncData(value: final page) => _body(page, notifier),
          AsyncError(error: final error, stackTrace: final _) =>
            _LoadFailure(error: error, onRetry: () => ref.refresh(readerProvider.future)),
          _ => const Center(child: CircularProgressIndicator()),
        },
      ),
    );
  }

  Widget _body(ReaderPage page, ReaderNotifier notifier) {
    _resetKeys(page.baseChapterUrl);
    _scheduleAnchor(page);
    return Column(
      children: <Widget>[
        FailureBanner(stream: notifier.failures),
        Expanded(child: _content(page, notifier)),
        StateBar(
          message: page.echo,
          state: AsyncValue<ReaderPage>.data(page),
          failedText: '阅读加载失败',
          loadingText: '正在读…',
          dataText: '${page.progressText} · ${page.bookLine}',
        ),
      ],
    );
  }

  Widget _content(ReaderPage page, ReaderNotifier notifier) {
    if (page.documentLines.isEmpty) {
      // 空正文有两种来路，必须分开说：这一章真没字（源给的正文空），
      // 和"这本书根本没有目录"（chapterIndex 是 -1）。后者报"这一章是空的"
      // 就是让用户以为书坏了。
      return Center(
        child: Text(page.chapterIndex < 0
            ? '这本书没有目录，没有可读的章'
            : '这一章没有正文（可能这条源读不出来）'),
      );
    }
    return Stack(
      children: <Widget>[
        ListView.builder(
          key: const Key('reader.body'),
          controller: _controller,
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
          itemCount: page.documentLines.length,
          itemBuilder: (context, index) =>
              _row(page.documentLines[index], page),
        ),
        // 翻页按钮浮在正文上（flet 那屏的上下章按钮也在正文区，不在顶栏）：
        // 单手滚动时够得着，顶栏那排图标已经挤满了。
        Positioned(
          right: 8,
          bottom: 8,
          child: Column(
            children: <Widget>[
              FloatingActionButton(
                key: const Key('reader.prev'),
                heroTag: 'reader.prev',
                mini: true,
                tooltip: '上一章',
                onPressed: page.nextIndexIn(-1) == null
                    ? null
                    : () => unawaited(notifier.prevChapter()),
                child: const Icon(Icons.arrow_drop_up),
              ),
              const SizedBox(height: 6),
              FloatingActionButton(
                key: const Key('reader.next'),
                heroTag: 'reader.next',
                mini: true,
                tooltip: '下一章',
                onPressed: page.nextIndexIn(1) == null
                    ? null
                    : () => unawaited(notifier.nextChapter()),
                child: const Icon(Icons.arrow_drop_down),
              ),
            ],
          ),
        ),
      ],
    );
  }

  /// 整篇行表里的一行 → 屏上那一个控件。
  ///
  /// 三种行：正文段（有整篇段号，挂 GlobalKey 供落点量）、媒体块（占行不产段），
  /// 以及追加章节时补的分隔线与章名（flet `render_plan(append=True)` 那两行）。
  /// 后两类**都不许进键表**：它们没有段号，进了就把上一段的落点报成它们。
  Widget _row(DocumentLine row, ReaderPage page) {
    if (!row.isContent) {
      return _ChapterDivider(
        key: ValueKey('reader.title.${row.mountIndex}.${row.name}'),
        row: row,
      );
    }
    final line = row.line!;
    if (!line.isText) {
      return _MediaLine(
        key: ValueKey('reader.media.${line.kind}.${line.block?.url ?? ''}'),
        block: line.block,
        style: line.meta,
        mediaUri: _mediaUriFor(line.block),
      );
    }
    // 键里的段号是**整篇**口径：追加之后每一章都有自己的第 0 段，撞了键就把
    // 上一章那一行报成本章的落点。
    final paragraph = row.paragraph!;
    return Padding(
      key: _keyFor(paragraph),
      padding: EdgeInsets.only(bottom: row.lastInChapter ? 12 : 6),
      child: Text(
        line.display,
        key: ValueKey('reader.text.$paragraph'),
        style: TextStyle(
          fontSize: page.typography.fontSize.toDouble(),
          height: 1.7,
          fontWeight: line.heading ? FontWeight.w700 : FontWeight.normal,
        ),
      ),
    );
  }

  /// 服务端发下来的那条二进制地址（§4.4）：只有 `mediaKey` 非空的块有。
  ///
  /// 两种 null 是两件事，屏上却都退回 `url` 那一格：这一格本来就没地址（公开图，
  /// 服务端没把它换成字节）、或者这一程根本没有网关子进程（替身界面/widget 测试）。
  /// 地址是**服务端算的**（`ReadingCache.media_key` 那两支摘要），这里只贴不拼——
  /// 客户端手拼摘要就是第二条键空间（第八十九轮那条「要么上钉常量、要么上钉服务端」）。
  Uri? _mediaUriFor(ContentBlock? block) {
    final key = block?.mediaKey ?? '';
    if (key.isEmpty) return null;
    // read 不是 watch：这一格跟着进程活，握手之后不会变，变了也是连不上那一屏的事。
    return ref.read(engineEndpointProvider)?.mediaUri(key);
  }

  /// 退出：先问 `reader.staged`，需要问的那一臂等用户答完再关会话（规矩 6）。
  Future<void> _exit(ReaderNotifier notifier) async {
    final exit = await notifier.beginExit();
    if (!mounted) return;
    if (exit.staged != null) {
      final record = exit.staged!;
      final shelve = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: const Text('放入书架'),
          content: Text('是否将《${record.displayName}》放入书架？\n'
              '不放入会删除这本书的阅读进度与缓存。'),
          actions: <Widget>[
            TextButton(
              key: const Key('reader.staged.discard'),
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('不放入'),
            ),
            FilledButton(
              key: const Key('reader.staged.promote'),
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('放入书架'),
            ),
          ],
        ),
      );
      // 问不出来（用户按返回把弹窗关了）就什么都不做：`shelve == null` 时
      // 既不转正也不丢弃，暂存记录原样留给下次。猜一个方向就是替用户决定删不删数据。
      if (shelve == null) return;
      await notifier.finishExit(shelve: shelve);
      if (!mounted) return;
      // 退出这一屏：`context.pop()` 走的是**根导航器**，落回推它进来的那一屏
      // （书架或找书）——与 flet 那两个 `on_back_*` 回调同一条。
      context.pop();
      return;
    }
    if (mounted) context.pop();
  }

  /// 把弹窗子树挂回**这一屏那一份容器**。
  ///
  /// 要紧不是因为 `showModalBottomSheet`/`showDialog` 的 entry 进的是 App 层那个
  /// `Overlay`——它在路由级 `ProviderScope`（`app/router.dart` 里注入
  /// `readerArgsProvider` 的那一份）**之上**。弹窗里再 `ref.watch(readerProvider)`
  /// 就落到根容器：那里没有 args，provider 重新 build 一次、当场投出"没有收到书"，
  /// 抽屉里只剩一片空白——看起来是"书签面板坏了"，真因是它站在另一棵依赖树上。
  /// [UncontrolledProviderScope] 只把容器交出去，不负责解散它（容器随 `ProviderScope` 活）。
  Widget _scoped(Widget child) =>
      UncontrolledProviderScope(container: _container, child: child);

  Future<void> _openToc(ReaderNotifier notifier) async {
    final page = ref.read(readerProvider).value;
    if (page == null) return;
    await showModalBottomSheet<void>(
      context: context,
      // 目录可以几百条：默认那道 9/16 屏高的约束会把抽屉夹住，放松之后
      // `Flexible` + `shrinkWrap` 仍按内容收，长列表才滚得起来。
      isScrollControlled: true,
      builder: (sheetContext) => _scoped(_TocSheet(
        page: page,
        onSelect: (index) {
          Navigator.of(sheetContext).pop();
          unawaited(notifier.openChapter(index));
        },
        onRefresh: () => unawaited(notifier.refreshToc()),
      )),
    );
  }

  Future<void> _openBookmarks(ReaderNotifier notifier) async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (sheetContext) => _scoped(Consumer(
        builder: (context, ref, _) {
          final page = ref.watch(readerProvider).value;
          if (page == null) return const SizedBox.shrink();
          return BookmarkSheet(
            page: page,
            onAdd: () => unawaited(notifier.addBookmark()),
            onRemove: (id) => unawaited(notifier.removeBookmark(id)),
            onJump: (row) {
              Navigator.of(sheetContext).pop();
              unawaited(notifier.jumpToBookmark(row));
            },
          );
        },
      )),
    );
  }

  Future<void> _openSearch(ReaderNotifier notifier) async {
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => _scoped(Consumer(
        builder: (context, ref, _) {
          final page = ref.watch(readerProvider).value;
          return ContentSearchSheet(
            page: page,
            onSubmit: (keyword) => unawaited(notifier.searchInBook(keyword)),
            onJump: (hit) {
              Navigator.of(dialogContext).pop();
              unawaited(notifier.jumpToHit(hit));
            },
          );
        },
      )),
    );
  }
}

/// 追加一章时补在正文中间的那两行：分隔线 + 章名。
///
/// 形状照 flet `render_plan`：`ft.Divider(height=32)` + `ft.Text(name, size=22, BOLD)`。
/// 章名用**目录里**的那一句而不是正文里的 `<title>`（顶栏同一个来源）：两边不同形时
/// 用户会在同一屏看到两个章名。这两行占行不产段，所以不进段号键表。
class _ChapterDivider extends StatelessWidget {
  const _ChapterDivider({required this.row, super.key});

  final DocumentLine row;

  @override
  Widget build(BuildContext context) {
    if (row.name.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 16),
        child: Divider(height: 1),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 16),
      child: Column(
        children: <Widget>[
          const Divider(height: 1),
          const SizedBox(height: 8),
          Text(row.name,
              style: const TextStyle(
                  fontSize: 22, fontWeight: FontWeight.w700, height: 1.4)),
        ],
      ),
    );
  }
}

/// 一个媒体块那一行。
///
/// 图片有两个取处，按 [mediaUri] 有没有来分：**服务端发过地址**（`mediaKey` 非空，
/// 也就是那块字节真在缓存里）就走 `GET /media/{key}`（§4.4），这一路不需要请求头，
/// cookie 在服务侧已经用过了；没发地址就退回 `url + headers` 那一对（公开图）。
/// `value` 那一格在 Dart 这边恒空——字节不过协议面。
/// 音视频只把地址显示出来——这一版没有 url_launcher 依赖，放一颗"打开"按钮
/// 就是点了没反应的占位。
class _MediaLine extends StatelessWidget {
  const _MediaLine(
      {required this.block,
      required this.style,
      this.mediaUri,
      super.key});

  final ContentBlock? block;
  final String style;

  /// 服务端为这一块发下来的 `GET /media/{key}` 地址；null = 这一块没有可取的字节。
  final Uri? mediaUri;

  /// 加载态与失败态占同一个高度的盒子（成功加载的图片仍按原图高）。
  ///
  /// 为什么钉死：正文里图片那一行的上下都是**要数段号的正文段**，占位与失败态差
  /// 几十像素，图片到位/失败那一刻整块正文就跳一次——"恢复落点"落在图片之后的那一章，
  /// 跳完屏上顶着的段已经不是存档那一段（本轮量到的 70 像素偏移就是这么来的）。
  static const double _reservedHeight = 120;

  static Widget _box(Widget child) =>
      SizedBox(height: _reservedHeight, child: child);

  @override
  Widget build(BuildContext context) {
    final source = block;
    if (source == null) return const SizedBox.shrink();
    final target = mediaUri?.toString() ??
        (source.url.isNotEmpty ? source.url : null);
    if (source.kind == 'image' && target != null) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Image.network(target,
            // 走 /media 那一路时不带 headers：令牌已经在地址里，而把书源的
            // cookie 递给回环网关是一次没有读者的转发。
            headers: mediaUri != null || source.headers.isEmpty
                ? null
                : source.headers,
            errorBuilder: (context, _, _) =>
                _box(_MediaFallback(block: source, style: style)),
            loadingBuilder: (context, child, progress) => progress == null
                ? child
                : _box(const _MediaSkeleton())),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: _box(_MediaFallback(block: source, style: style)),
    );
  }
}

class _MediaSkeleton extends StatelessWidget {
  const _MediaSkeleton();

  @override
  Widget build(BuildContext context) =>
      Container(color: Theme.of(context).colorScheme.surfaceContainerHighest);
}

class _MediaFallback extends StatelessWidget {
  const _MediaFallback({required this.block, required this.style});

  final ContentBlock block;
  final String style;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
          border: Border.all(color: scheme.outlineVariant),
          borderRadius: BorderRadius.circular(4)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Icon(_iconFor(block.kind), size: 18, color: scheme.primary),
              const SizedBox(width: 6),
              Expanded(
                  child: Text(block.title.isEmpty ? block.kind : block.title,
                      maxLines: 1, overflow: TextOverflow.ellipsis)),
              if (style.isNotEmpty)
                Text(style,
                    style: Theme.of(context)
                        .textTheme
                        .labelSmall
                        ?.copyWith(color: scheme.outline)),
            ],
          ),
          if (block.url.isNotEmpty)
            // 夹进行数：这一格外面是 `_MediaLine` 钉死的那只盒子，
            // 一条超长 url 换行会把内容顶出去（溢出是画出来的错）。
            SelectableText(block.url,
                maxLines: 3,
                style: Theme.of(context)
                    .textTheme
                    .bodySmall
                    ?.copyWith(color: scheme.onSurfaceVariant)),
        ],
      ),
    );
  }

  static IconData _iconFor(String kind) => switch (kind) {
        'audio' => Icons.audiotrack_outlined,
        'video' => Icons.movie_outlined,
        _ => Icons.image_outlined,
      };
}

/// 目录抽屉：原样那一批行（**含卷标题**，它们只是不可点）。
class _TocSheet extends StatelessWidget {
  const _TocSheet({
    required this.page,
    required this.onSelect,
    required this.onRefresh,
  });

  final ReaderPage page;
  final void Function(int index) onSelect;
  final VoidCallback onRefresh;

  @override
  Widget build(BuildContext context) {
    final chapters = page.chapters;
    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          ListTile(
            leading: const Icon(Icons.folder_open),
            title: Text('目录（共 ${chapters.length} 条）'),
            trailing: IconButton(
              key: const Key('reader.toc.refresh'),
              icon: const Icon(Icons.refresh),
              tooltip: '重取目录',
              onPressed: onRefresh,
            ),
          ),
          const Divider(height: 1),
          Flexible(
            child: chapters.isEmpty
                ? const Padding(
                    padding: EdgeInsets.all(24),
                    child: Text('这本书没有目录'),
                  )
                : ListView.builder(
                    key: const Key('reader.toc.list'),
                    shrinkWrap: true,
                    itemCount: chapters.length,
                    itemBuilder: (context, index) =>
                        _TocRow(row: chapters[index],
                            current: index == page.chapterIndex,
                            onSelect: () => onSelect(index)),
                  ),
          ),
        ],
      ),
    );
  }
}

class _TocRow extends StatelessWidget {
  const _TocRow({required this.row, required this.current, required this.onSelect});

  final ChapterRow row;
  final bool current;
  final VoidCallback onSelect;

  @override
  Widget build(BuildContext context) {
    final subtitle = <String>[
      if (row.tag.isNotEmpty) row.tag,
      // 字数原样显示：这一格服务端给的是**字符串**，有的源根本不填。
      // 补 0 会把"这条源没给字数"显示成"这章 0 字"。
      if (row.wordCount.isNotEmpty) row.wordCount,
    ].join(' · ');
    return ListTile(
      dense: true,
      selected: current,
      enabled: !row.isVolume && row.url.isNotEmpty,
      title: Text(row.name),
      subtitle: subtitle.isEmpty ? null : Text(subtitle),
      trailing: current ? const Icon(Icons.play_arrow) : null,
      onTap: (!row.isVolume && row.url.isNotEmpty) ? onSelect : null,
    );
  }
}

/// 书签面板。行的文案与 flet 的 `render_bookmarks` 同一条（空名兜底"第 N 章"）。
class BookmarkSheet extends StatelessWidget {
  const BookmarkSheet({
    required this.page,
    required this.onAdd,
    required this.onRemove,
    required this.onJump,
    super.key,
  });

  final ReaderPage page;
  final VoidCallback onAdd;
  final void Function(int bookmarkId) onRemove;
  final void Function(BookmarkRow row) onJump;

  @override
  Widget build(BuildContext context) {
    final marks = page.bookmarks;
    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          ListTile(
            leading: const Icon(Icons.bookmarks_outlined),
            title: Text('书签（${marks.length} 条）'),
            trailing: IconButton(
              key: const Key('reader.bookmark.add'),
              icon: const Icon(Icons.add),
              tooltip: '在这一章加书签',
              onPressed: onAdd,
            ),
          ),
          const Divider(height: 1),
          Flexible(
            child: marks.isEmpty
                ? const Padding(
                    padding: EdgeInsets.all(24),
                    child: Text('还没有书签'),
                  )
                : ListView.builder(
                    key: const Key('reader.bookmark.list'),
                    shrinkWrap: true,
                    itemCount: marks.length,
                    itemBuilder: (context, index) {
                      final row = marks[index];
                      return ListTile(
                        dense: true,
                        title: Text(row.label),
                        // 比例那一格是 `chapter_ratio`，**不是**字符偏移：
                        // 标成"第几字"就是把两种位置混给用户提供（BookmarkRow 文件头）。
                        subtitle: Text('第 ${row.chapterIndex + 1} 章 · '
                            '${(row.offset * 100).toStringAsFixed(0)}%'),
                        trailing: IconButton(
                          icon: const Icon(Icons.delete_outline),
                          tooltip: '删除',
                          onPressed: () => onRemove(row.bookmarkId),
                        ),
                        onTap: () => onJump(row),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}

/// 书内搜索面板。0 命中要说的是**哪一种** 0（三种，见控制器 `searchEcho`）。
class ContentSearchSheet extends StatefulWidget {
  const ContentSearchSheet({
    required this.page,
    required this.onSubmit,
    required this.onJump,
    super.key,
  });

  final ReaderPage? page;
  final void Function(String keyword) onSubmit;
  final void Function(ContentHit hit) onJump;

  @override
  State<ContentSearchSheet> createState() => _ContentSearchSheetState();
}

class _ContentSearchSheetState extends State<ContentSearchSheet> {
  final TextEditingController _input = TextEditingController();

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final page = widget.page;
    final search = page?.search;
    final hits = search?.hits ?? const <ContentHit>[];
    return AlertDialog(
      title: const Text('书内搜索'),
      content: SizedBox(
        width: 360,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: TextField(
                    key: const Key('reader.search.input'),
                    controller: _input,
                    decoration: const InputDecoration(labelText: '关键词'),
                    onSubmitted: widget.onSubmit,
                  ),
                ),
                IconButton(
                  key: const Key('reader.search.submit'),
                  icon: const Icon(Icons.search),
                  tooltip: '搜索',
                  onPressed: () => widget.onSubmit(_input.text),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (page?.echo.isNotEmpty == true && search == null)
              Text(page!.echo),
            if (search != null && hits.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(page?.echo ?? ''),
              ),
            Flexible(
              child: hits.isEmpty
                  ? const SizedBox.shrink()
                  : ListView.builder(
                      key: const Key('reader.search.results'),
                      shrinkWrap: true,
                      itemCount: hits.length,
                      itemBuilder: (context, index) {
                        final hit = hits[index];
                        return ListTile(
                          dense: true,
                          title: Text(_plain(hit.snippet)),
                          subtitle: Text('第 ${hit.chapterIndex + 1} 章 · '
                              '第 ${hit.paraIndex + 1} 段 · ${hit.characterOffset} 字'),
                          onTap: () => widget.onJump(hit),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('关闭'),
        ),
      ],
    );
  }

  /// 命中摘录带 `<mark>` 标记：那是一句"命中在哪几个字"的话，界面上剥掉标记
  /// 只留文本（服务端不剥，见 `ContentHit.snippet` 文件头）。
  static String _plain(String snippet) =>
      snippet.replaceAll(RegExp(r'</?mark>'), '');
}

class _LoadFailure extends StatelessWidget {
  const _LoadFailure({required this.error, required this.onRetry});

  final Object error;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final failure = error is EngineFailure ? error as EngineFailure : null;
    final kind = failure?.kind.wireName ?? 'engine';
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text('这一本书打不开（$kind）',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            // 详情必须夹住：没注入仓库时框架包出来的那一条是七万字，
            // 直接画就是把一屏堆栈顶出边界（同 `bookshelf_screen.dart` 那一处）。
            Text(_clip('$error'),
                textAlign: TextAlign.center,
                maxLines: 6,
                overflow: TextOverflow.ellipsis),
            if (failure?.action == 'reader.open')
              const SizedBox(height: 8),
            if (failure?.action == 'reader.open')
              const Text('这一发是 `reader.open`：多半是路由没把书快照递进来。'),
            const SizedBox(height: 12),
            if (failure == null || failure.isTransient)
              FilledButton.icon(
                key: const Key('reader.retry'),
                onPressed: () => onRetry(),
                icon: const Icon(Icons.refresh),
                label: const Text('重试'),
              ),
          ],
        ),
      ),
    );
  }

  static String _clip(String text) =>
      text.length <= 400 ? text : '${text.substring(0, 400)}…';
}
