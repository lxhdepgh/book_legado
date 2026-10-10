/// 找书那一屏的状态机（§6.2：`AsyncNotifier`；三块内部页共用一份状态）。
///
/// 照的是 `app/controllers/find_books_controller.py` 那一份，五条例子在代码里：
///
/// 1. **结果按"累计快照"替换，不 append**（`:250-255`）。服务端每合并一个源就发一条
///    `SearchFinishedEvent`，`items` 是**到目前为止的全部**，不是增量。append 的实现
///    会把每一本按到达次数重复若干遍。
/// 2. **只认最新一轮**（`:251-253` 的 generation 守护）：`search_id` 不是当前那一轮的
///    直接丢。晚到的上一轮结果如果上屏，用户看到的是"停了还在涨"。
/// 3. **0.5 s 合流**（`:77-78` 的 `throttle(…, 0.5)`）：窗口内多条只重画最后一次。
///    这条是 R4 实测换来的（整页 diff 随行数线性变贵），不是审美。
/// 4. **发搜索前先看分母**（`:232-235`）：范围表里那一格是 0 就不发这一发。
///    表本身来自 `search.scopes`，可搜不可搜不在这里判。
/// 5. **"没配分类"与"分类没读回来"是两句话**（`:143-153`）：前者是服务端回了空列表，
///    后者是那一发根本没成。合并成一句就是把故障说成"今天没有数据"。
///
/// 开读（详情框「开始阅读」）与书源登录不在 G2：前者要 G3 的阅读器，后者
/// `login_schema/submit_login` 还没上协议面。这里不放点了没反应的按钮。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/events/engine_event.dart';
import '../../../core/rpc/envelope.dart';
import '../../sources/data/source_record.dart';
import '../data/book_row.dart';
import '../data/category_row.dart';
import '../data/search_scope.dart';

/// 三块内部页：书源列表 / 分类浏览 / 关键词搜索。
enum FindBooksPage { sources, browse, search }

final AsyncNotifierProvider<FindBooksNotifier, FindBooksState> findBooksProvider =
    AsyncNotifierProvider<FindBooksNotifier, FindBooksState>(
  FindBooksNotifier.new,
  retry: (_, _) => null,
);

class FindBooksNotifier extends AsyncNotifier<FindBooksState> {
  /// 事件合流窗口：与检测那一屏同源的一条规矩（flet `throttle(…, 0.5)`）。
  static const Duration _flushWindow = Duration(milliseconds: 500);

  Timer? _flush;

  /// 这一轮搜索的 id：服务端把它原样带回事件里，非本轮的一律丢。
  int _searchId = 0;

  /// 窗口里最后收到的那一批（合流之后才落到 [state]）。
  List<BookRow>? _pendingItems;
  bool _pendingFinished = false;

  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  Stream<EngineFailure> get failures => _failures.stream;

  @override
  Future<FindBooksState> build() async {
    ref.onDispose(() {
      _flush?.cancel();
      unawaited(_failures.close());
    });
    final catalog = await _loadCatalog();
    return FindBooksState.initial(
        sources: catalog.$1, scopes: catalog.$2, status: _sourceStatus(catalog.$1));
  }

  FindBooksState get _current => state.value ?? FindBooksState.idle;

  void _emit(FindBooksState next) => state = AsyncValue<FindBooksState>.data(next);

  /// 一次动作的两种结局：值与错误只能分开拿（第 5 条例子）。
  Future<(T?, String?)> _attempt<T>(
      Future<T> Function() call, String action) async {
    try {
      return (await call(), null);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return (null, exc.message);
    } catch (exc) {
      _failures
          .add(EngineFailure(EngineFailureKind.engine, '$exc', action: action));
      return (null, '$exc');
    }
  }

  static String _sourceStatus(List<SourceRecord> sources) =>
      '共 ${sources.length} 个书源';

  /// 书源表 + 范围表一起拿：范围表的下发点是"进这一屏"和"书源变了"，两处同一发。
  Future<(List<SourceRecord>, List<SearchScope>)> _loadCatalog() async {
    final repo = ref.read(engineRepositoryProvider);
    final (sources, _) = await _attempt(repo.listSources, 'source.list');
    final (scopes, _) = await _attempt(repo.searchScopes, 'search.scopes');
    return (
      sources ?? const <SourceRecord>[],
      scopes ?? const <SearchScope>[]
    );
  }

  // ------------------------------------------------------------------ #
  // 刷新
  // ------------------------------------------------------------------ #
  /// 手动刷新：旧表留在原位，失败只由 [failures] 出声（同书架那一屏那条规矩）。
  /// 状态行改口报数——这是用户按下的那一次刷新，回报该有回声。
  Future<void> reload() async {
    final catalog = await _loadCatalog();
    _emit(_current.copyWith(
        sources: catalog.$1,
        scopes: catalog.$2,
        status: _sourceStatus(catalog.$1)));
  }

  /// `SourceChangedEvent` 驱动的刷新：只换表，**不占状态行**
  /// （`find_books_controller.py:97-114`：否则"已加入书架"这类回报会被盖掉）。
  Future<void> refreshCatalog() async {
    final catalog = await _loadCatalog();
    _emit(_current.copyWith(sources: catalog.$1, scopes: catalog.$2));
  }

  // ------------------------------------------------------------------ #
  // 页间切换
  // ------------------------------------------------------------------ #
  void openPage(FindBooksPage page) => _emit(_current.copyWith(page: page));

  // ------------------------------------------------------------------ #
  // 分类浏览
  // ------------------------------------------------------------------ #
  /// 点开一条书源：切到浏览页并解析分类（`open_source:119-133`）。
  Future<void> openSource(SourceRecord source) async {
    final name = source.title;
    _emit(_current.copyWith(
      page: FindBooksPage.browse,
      currentSourceUrl: source.url,
      currentSourceName: name,
      categories: const <CategoryRow>[],
      categoryIndex: 0,
      browseBooks: const <BookRow>[],
      browsePage: 0,
      status: '正在解析 $name 的分类…',
    ));
    final (categories, error) = await _attempt(
        () => ref.read(engineRepositoryProvider).browseCategories(source.url),
        'browse.categories');
    // 用户已切走（换了源或回了列表）：过期结果丢弃，`_load_categories:148-149`
    if (_current.currentSourceUrl != source.url) return;
    if (error != null) {
      _emit(_current.copyWith(status: '分类解析失败：$error'));
      return;
    }
    final loaded = categories ?? const <CategoryRow>[];
    if (loaded.isEmpty) {
      _emit(_current.copyWith(status: '该书源未配置分类浏览'));
      return;
    }
    _emit(_current.copyWith(
        categories: loaded, status: '共 ${loaded.length} 个分类'));
    await _openCategory(0);
  }

  Future<void> selectCategory(int index) => _openCategory(index);

  Future<void> _openCategory(int index) async {
    final opening = _current;
    final sourceUrl = opening.currentSourceUrl;
    if (sourceUrl == null || !opening.categoryAt(index)) return;
    final category = opening.categories[index];
    _emit(opening.copyWith(
        categoryIndex: index, browseBooks: const <BookRow>[], browsePage: 0));
    final (books, error) = await _attempt(
        () => ref.read(engineRepositoryProvider).browseBooks(
            sourceUrl: sourceUrl, category: category.raw, page: 1),
        'browse.books');
    if (_current.currentSourceUrl != sourceUrl ||
        _current.categoryIndex != index) {
      return; // 在途时换了源/分类
    }
    if (error != null) {
      _emit(_current.copyWith(status: '分类书单加载失败：$error'));
      return;
    }
    final page1 = books ?? const <BookRow>[];
    _emit(_current.copyWith(
        browseBooks: page1,
        browsePage: page1.isEmpty ? 0 : 1,
        status: '${category.label}：${page1.length} 本'));
  }

  /// 浏览页触底：下一页**追加**（`_load_more_browse:197-220`）。
  ///
  /// 与搜索那一侧相反——浏览每页是独立的一页书，追加才对；搜索那批是累计快照，
  /// 追加会把同一本数两遍。两块形状不同是服务端定的，不是这里选的。
  Future<void> loadMoreBrowse() async {
    final opening = _current;
    final sourceUrl = opening.currentSourceUrl;
    // browsePage==0 = 第一页还没成功打开：没有第 1 页就不该去要第 2 页
    if (sourceUrl == null ||
        opening.browseLoading ||
        opening.browsePage == 0 ||
        !opening.categoryAt(opening.categoryIndex)) {
      return;
    }
    _emit(opening.copyWith(browseLoading: true));
    final category = opening.categories[opening.categoryIndex];
    final (books, error) = await _attempt(
        () => ref.read(engineRepositoryProvider).browseBooks(
            sourceUrl: sourceUrl,
            category: category.raw,
            page: opening.browsePage + 1),
        'browse.books');
    final stillHere = _current.currentSourceUrl == sourceUrl &&
        _current.categoryIndex == opening.categoryIndex;
    if (error != null) {
      _emit(_current.copyWith(
          browseLoading: false,
          status: stillHere ? '下一页加载失败：$error' : _current.status));
      return;
    }
    if (!stillHere) {
      _emit(_current.copyWith(browseLoading: false));
      return;
    }
    final next = books ?? const <BookRow>[];
    if (next.isEmpty) {
      _emit(_current.copyWith(browseLoading: false, status: '没有更多了'));
      return;
    }
    _emit(_current.copyWith(
      browseLoading: false,
      browsePage: _current.browsePage + 1,
      browseBooks: <BookRow>[..._current.browseBooks, ...next],
    ));
  }

  // ------------------------------------------------------------------ #
  // 关键词搜索
  // ------------------------------------------------------------------ #
  /// 打开范围弹窗时按当前库现刷一次表（flet 那边表就在控制器手里，这里同一发）。
  Future<List<SearchScope>> refreshScopes() async {
    final (rows, _) =
        await _attempt(ref.read(engineRepositoryProvider).searchScopes, 'search.scopes');
    if (rows != null && rows.isNotEmpty) _emit(_current.copyWith(scopes: rows));
    return _current.scopes;
  }

  /// 换范围只改 `scopeValue`：弹窗那一句文案与状态行是两件事（flet 那边
  /// `set_scope_label` 动的是范围按钮上的标签，不是底部状态行）。
  void selectScope(SearchScope chosen) =>
      _emit(_current.copyWith(scopeValue: chosen.value));

  /// 发一轮搜索（`search:225-242`）。同一关键词再点一次 = 新一轮，回到第 1 页。
  Future<void> search(String keyword) async {
    final trimmed = keyword.trim();
    if (trimmed.isEmpty) return;
    final count = _current.searchableCount;
    if (count == 0) {
      _emit(_current.copyWith(page: FindBooksPage.search,
          keyword: trimmed, status: '当前范围内没有启用且配置了搜索规则的书源'));
      return;
    }
    // 换 id = 服务端取消上一轮并回到第 1 页。id 必须我们自己攥着：
    // 事件里那一条要靠它认"是不是当前那一轮"（第 2 条例子）。
    _searchId = DateTime.now().microsecondsSinceEpoch;
    _pendingItems = null;
    _pendingFinished = false;
    _flush?.cancel();
    _emit(_current.copyWith(
      page: FindBooksPage.search,
      keyword: trimmed,
      searchRunning: true,
      searchResults: const <BookRow>[],
      status: count == null
          ? '正在搜索「$trimmed」…'
          : '正在搜索「$trimmed」（$count 源）…',
    ));
    final ack = await _attempt(
        () => ref.read(engineRepositoryProvider).searchStart(
            keyword: trimmed, scope: _current.scopeValue, searchId: _searchId),
        'search.start');
    if (ack.$2 != null) {
      // 受理都没到手：这一轮根本没起来，进度条不能一直转（那是"在搜"的假象）。
      _searchId = 0;
      _emit(_current.copyWith(
          searchRunning: false, status: '搜索发起失败：${ack.$2}'));
    }
  }

  /// 触底翻页：**同一个 id** 再发一次，服务端页码自增（`load_more:192-195`）。
  Future<void> loadMoreSearch() async {
    if (!_current.searchRunning || _searchId == 0) return;
    await _attempt(
        () => ref.read(engineRepositoryProvider).searchStart(
            keyword: _current.keyword,
            scope: _current.scopeValue,
            searchId: _searchId),
        'search.start');
  }

  /// 翻页那一发按当前在哪一页分流（flet `load_more(flow)` 的同一个入口）。
  Future<void> loadMore() async {
    switch (_current.page) {
      case FindBooksPage.search:
        await loadMoreSearch();
      case FindBooksPage.browse:
        await loadMoreBrowse();
      case FindBooksPage.sources:
        break;
    }
  }

  Future<void> stopSearch() async {
    _flush?.cancel();
    _pendingItems = null;
    _emit(_current.copyWith(searchRunning: false, status: '已停止搜索'));
    await _attempt(ref.read(engineRepositoryProvider).searchStop, 'search.stop');
  }

  // ------------------------------------------------------------------ #
  // 事件
  // ------------------------------------------------------------------ #
  /// 一条 `SearchFinishedEvent`（界面收到就交进来）。
  void onSearchFinished(EngineEvent event) {
    if ('${event.payload['search_id'] ?? ''}' != '$_searchId') return;
    final raw = event.payload['items'];
    _pendingItems = raw is List
        ? raw
            .whereType<Map<Object?, Object?>>()
            .map((row) => BookRow.fromJson(
                row.map((key, value) => MapEntry(key.toString(), value))))
            .toList(growable: false)
        : const <BookRow>[];
    if (event.payload['finished'] == true) _pendingFinished = true;
    if (_flush?.isActive ?? false) return;
    _flush = Timer(_flushWindow, _flushNow);
  }

  void _flushNow() {
    final items = _pendingItems;
    if (items == null) return;
    _pendingItems = null;
    _emit(_current.copyWith(
      searchResults: items,
      searchRunning: !_pendingFinished,
      status: _pendingFinished
          ? '搜索完成：${items.length} 条结果'
          : '已找到 ${items.length} 条结果，继续搜索中…',
    ));
  }

  // ------------------------------------------------------------------ #
  // 详情 / 入架
  // ------------------------------------------------------------------ #
  /// 找书结果直接入书架（不经试读，`_shelve:319-341`）。
  ///
  /// [row.raw] 原样传回：`bookshelf.add` 的 `book` 是整条字典快照，服务端按它落库；
  /// 自己挑几个字段拼就是"存回去的时候丢东西"那一类错。
  Future<String> shelve(BookRow row) async {
    final sourceUrl = row.origin.isNotEmpty ? row.origin : _current.currentSourceUrl;
    if (sourceUrl == null || sourceUrl.isEmpty) {
      final message = '未找到该书来源书源，无法入架：${row.name}';
      _emit(_current.copyWith(status: message));
      return message;
    }
    final (ok, error) = await _attempt(
        () => ref
            .read(engineRepositoryProvider)
            .shelveBook(sourceUrl: sourceUrl, book: row.raw),
        'bookshelf.add');
    final message = error != null
        ? '加入书架失败：$error'
        : (ok == true ? '已加入书架：${row.name}' : '该书已在书架：${row.name}');
    _emit(_current.copyWith(status: message));
    return message;
  }
}

/// 这一屏的全部可变状态。不可变值对象：每一次动作产出新的一个。
class FindBooksState {
  const FindBooksState({
    this.page = FindBooksPage.sources,
    this.sources = const <SourceRecord>[],
    this.scopes = const <SearchScope>[],
    this.status = kIdleStatus,
    this.currentSourceUrl,
    this.currentSourceName = '',
    this.categories = const <CategoryRow>[],
    this.categoryIndex = 0,
    this.browseBooks = const <BookRow>[],
    this.browsePage = 0,
    this.browseLoading = false,
    this.keyword = '',
    this.scopeValue = kScopeAll,
    this.searchResults = const <BookRow>[],
    this.searchRunning = false,
  });

  /// flet 那屏的开屏文案（`find_books_view.py:132`）。
  static const String kIdleStatus = '选择书源浏览分类，或直接搜索书名';

  static FindBooksState initial({
    List<SourceRecord> sources = const <SourceRecord>[],
    List<SearchScope> scopes = const <SearchScope>[],
    String status = kIdleStatus,
  }) =>
      FindBooksState(sources: sources, scopes: scopes, status: status);

  static const FindBooksState idle = FindBooksState();

  final FindBooksPage page;

  /// 书源表：浏览页那一列。
  final List<SourceRecord> sources;

  /// `search.scopes` 那张表（顺序即下拉顺序）。
  final List<SearchScope> scopes;

  /// 底部那一行：flet 全屏只有一个状态槽，动作回报优先占位。
  final String status;

  final String? currentSourceUrl;
  final String currentSourceName;
  final List<CategoryRow> categories;
  final int categoryIndex;

  /// 浏览页的书单。
  final List<BookRow> browseBooks;

  /// 已打开到第几页；0 = 第一页还没成功打开（不给"下一页"用）。
  final int browsePage;
  final bool browseLoading;

  final String keyword;
  final String scopeValue;

  /// 搜索结果：**当前这一轮的累计快照**（不是增量，见文件头第 1 条例子）。
  final List<BookRow> searchResults;
  final bool searchRunning;

  /// 选中范围里的可搜源数。三态：
  /// `null` = 范围表还没拿到（那一发搜索照发，别让"表没读回来"冒充"没有可搜源"）；
  /// `0` = 表里说了这个范围一条都没有；其余就是那个数。
  /// 选中组已不在表里（源被删了）按 `0` 算——那是表给的真答案。
  int? get searchableCount {
    if (scopes.isEmpty) return null;
    for (final row in scopes) {
      if (row.value == scopeValue) return row.sourceCount;
    }
    return 0;
  }

  /// 范围按钮上的那一格。
  String get scopeLabel {
    for (final row in scopes) {
      if (row.value == scopeValue) return row.label;
    }
    return SearchScope(value: scopeValue, sourceCount: 0).label;
  }

  bool categoryAt(int index) => index >= 0 && index < categories.length;

  FindBooksState copyWith({
    FindBooksPage? page,
    List<SourceRecord>? sources,
    List<SearchScope>? scopes,
    String? status,
    String? currentSourceUrl,
    String? currentSourceName,
    List<CategoryRow>? categories,
    int? categoryIndex,
    List<BookRow>? browseBooks,
    int? browsePage,
    bool? browseLoading,
    String? keyword,
    String? scopeValue,
    List<BookRow>? searchResults,
    bool? searchRunning,
  }) =>
      FindBooksState(
        page: page ?? this.page,
        sources: sources ?? this.sources,
        scopes: scopes ?? this.scopes,
        status: status ?? this.status,
        currentSourceUrl: currentSourceUrl ?? this.currentSourceUrl,
        currentSourceName: currentSourceName ?? this.currentSourceName,
        categories: categories ?? this.categories,
        categoryIndex: categoryIndex ?? this.categoryIndex,
        browseBooks: browseBooks ?? this.browseBooks,
        browsePage: browsePage ?? this.browsePage,
        browseLoading: browseLoading ?? this.browseLoading,
        keyword: keyword ?? this.keyword,
        scopeValue: scopeValue ?? this.scopeValue,
        searchResults: searchResults ?? this.searchResults,
        searchRunning: searchRunning ?? this.searchRunning,
      );
}
