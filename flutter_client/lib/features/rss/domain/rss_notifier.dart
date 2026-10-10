/// 订阅那一屏的状态机（§6.2：`AsyncNotifier`；源 → 文章 → 正文三栏共用一份状态）。
///
/// 照的是 `app/controllers/rss_controller.py` 那一条链，六条例子在代码里：
///
/// 1. **全屏只有一个状态槽**（flet 的 `_status`：状态、进度、结果谁最后写谁说话）。
///    这里也只有一个 `status` 字符串，而不是 status/progress/error 三格——三格会让
///    "进度句已写完、结果句还没轮到"那种中间拍在上屏时多出一句话。
/// 2. **加载失败留着旧数据**（`_reload_sources` 的 except 只写状态就 return）：
///    把失败演成空表，用户看到的是"我的订阅源没了"，而下一拍成功又会把它们变回来。
/// 3. **已读只在点开正文时写**，写完**同步本地那一份**（flet `open_article` 里的
///    `article["read"]=True` 加未读角标减一）：不本地那一格，中栏的字重要等下一次
///    刷新才变，用户会连点两下。
/// 4. **删除是级联的**（连文章带已读/收藏标记），所以界面必须先二次确认；服务端那发
///    回的是**有没有真删到行**，`false` 说的是"这条本就不在列表里"，不是"删除失败"。
/// 5. **角标不逐条重读**：一轮批量刷新每源一条 `RssRefreshedEvent`，flet 每收一条就读
///    一次 `article_counts()`（进程内 SQL，不要钱）。跨进程那一发是 `rss.sources`
///    整表 RPC，40 个源就是 40 发 + 40 次整树重画。所以这里按 0.5s 窗口**合流成一次**
///    重读：可见行为同一句（角标跟上），发的条数不同——这是与 flet 的一处**有意**不同。
/// 6. **本轮身份自己攥着**（`refresh_id`）：`rss.refresh_all` 是 job，受理包只有
///    `{started, action}`，晚到的进度帧只能靠这一格认本轮（同 `switch_id`）。
///    与换源那屏不同，这里有一条**让步**：动作表上没有 `rss.snapshot`，所以切页回来时
///    本地没有本轮身份，第一帧的 id 就**采纳**为本轮（否则进度条从这一拍起永远不动，
///    那比"可能跟着别人的那一轮走"更坏）。采纳来的那一轮**不报新增篇数**——
///    我们没看见它前面的帧，写"新增 0 篇"就是把没数着说成数着是零。
///
/// 收尾那句也是**有意**不同：flet 的"新增 N 篇"吃的是 `refresh_all()` 的**返回值**，
/// job 那一发没有返回值可吃。这里把本轮收到的 `RssRefreshedEvent.new_count` 累加起来，
/// 只在收尾那一帧写出来；同帧 `failures>0` 时补半句（flet 那一侧读不到失败数——
/// 服务把单源失败吞进了 `status.last_error`，这是跨进程才多出来的一格读数）。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/events/engine_event.dart';
import '../../../core/rpc/envelope.dart';
import '../data/rss_models.dart';

final AsyncNotifierProvider<RssNotifier, RssState> rssProvider =
    AsyncNotifierProvider<RssNotifier, RssState>(
  RssNotifier.new,
  // 同 `bookshelfProvider`（记忆 `riverpod-provider-default-retry`）：默认那条指数退避
  // 会把一次失败变成 11 发，而这一屏的 build 里只有 `rss.sources`——用户没点第二遍，
  // 库却替他把列表读了 11 次。
  retry: (_, _) => null,
);

class RssNotifier extends AsyncNotifier<RssState> {
  /// 角标重读的合流窗口（flet `throttle(…, 0.5)` 同值；理由见文件头第 5 条）。
  static const Duration _flushWindow = Duration(milliseconds: 500);

  Timer? _countsFlush;

  /// 这一轮全源刷新的身份证。服务端把它折成**字符串**回带，所以比对两边都转文本
  /// （同 `SourceSwitchNotifier._switchId`：按数字读那一份永远读到 0）。
  int _refreshId = 0;

  /// 本轮是不是**这一屏**发起的。采纳来的那一轮不报篇数（文件头第 6 条）。
  bool _roundStarted = false;

  /// 本轮累计的新增篇数（只由 `RssRefreshedEvent` 累加，收尾之后清空）。
  int _roundNewArticles = 0;

  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  /// 不打断当前操作、但要有声音的那几发（角标重读失败、已读标记失败）走这里。
  Stream<EngineFailure> get failures => _failures.stream;

  @override
  Future<RssState> build() async {
    ref.onDispose(() {
      _countsFlush?.cancel();
      unawaited(_failures.close());
    });
    // build 期间 [_current] 就是 [RssState.empty]，它的 `detailHint` 是「选一个订阅源」
    // ——正是 flet `RssView.__init__` 给右栏的那一格，读列表这一拍还没回来时该说这句。
    return _loadSources(keep: false);
  }

  RssState get _current => state.value ?? RssState.empty;

  void _emit(RssState next) => state = AsyncValue<RssState>.data(next);

  // ------------------------------------------------------------------ #
  // 加载
  // ------------------------------------------------------------------ #
  /// 读左栏（`_reload_sources`）。[keep] 假 = 落回首项；选中那本被删了也退回首项。
  ///
  /// 这一发读的是 `rss.sources`：角标（`total`/`unread`）已经由服务端 join 好，
  /// 客户端不再逐源数文章。
  Future<RssState> _loadSources({required bool keep}) async {
    final from = _current;
    final List<RssSourceRow> rows;
    try {
      rows = await ref.read(engineRepositoryProvider).listRssSources();
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      // 文案照 flet 那一句，旧数据一格不动（第 2 条规矩）。
      final next = from.copyWith(status: kRssSourcesLoadFailed(exc.message));
      _emit(next);
      return next;
    }
    final ids = rows.map((row) => row.id).toList(growable: false);
    final selected = (!keep || !ids.contains(from.selectedSourceId))
        ? (ids.isEmpty ? '' : ids.first)
        : from.selectedSourceId;
    // 一本源都没有：中栏空表、右栏退回那一格说明（flet 的 clear_detail("还没有订阅源")）。
    if (selected.isEmpty) {
      final next = from
          .copyWith(
              sources: rows,
              selectedSourceId: '',
              articles: const <RssArticleRow>[],
              detailHint: kRssNoSourcesAtAll)
          .clearDetail();
      _emit(next);
      return next;
    }
    final seeded = from.copyWith(sources: rows, selectedSourceId: selected);
    _emit(seeded);
    return _fetchArticles(seeded);
  }

  /// 读中栏（`_reload_articles`）。失败同样是"写那一句、旧列表不动"。
  Future<RssState> _fetchArticles(RssState from) async {
    if (from.selectedSourceId.isEmpty) {
      final next = from.copyWith(articles: const <RssArticleRow>[]);
      _emit(next);
      return next;
    }
    final List<RssArticleRow> rows;
    try {
      rows = await ref.read(engineRepositoryProvider).listRssArticles(
          sourceId: from.selectedSourceId, unreadOnly: from.unreadOnly);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      final next = from.copyWith(status: kRssArticlesLoadFailed(exc.message));
      _emit(next);
      return next;
    }
    final next = from.copyWith(articles: rows);
    _emit(next);
    return next;
  }

  // ------------------------------------------------------------------ #
  // 动作
  // ------------------------------------------------------------------ #
  /// 点一个源（`select_source`）：正文退回占位句，重读中栏。
  Future<void> selectSource(String sourceId) async {
    final next = _current
        .copyWith(selectedSourceId: sourceId, detailHint: kRssPickArticle)
        .clearDetail();
    _emit(next);
    await _fetchArticles(next);
  }

  /// 「只看未读」那个勾（`set_unread_only`）。
  Future<void> setUnreadOnly(bool only) async {
    final next = _current.copyWith(unreadOnly: only);
    _emit(next);
    await _fetchArticles(next);
  }

  /// 添加一个源（`_add_source`）：**先落库、再抓第一遍**，而抓取那一发不报成功句
  /// （`report=False`）——刚写完"已添加…正在抓取"，紧接着再写一句"刷新完成"会把
  /// 前一句压掉，而那两句讲的其实是同一件事。
  Future<void> addSource({required String name, required String url}) async {
    String sourceId;
    try {
      sourceId =
          await ref.read(engineRepositoryProvider).addRssSource(name: name, url: url);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      _emitStatus(kRssAddFailed(exc.message));
      return;
    }
    _emitStatus(kRssAdded(name.isEmpty ? sourceId : name));
    await _loadSources(keep: true);
    await _refreshOne(sourceId, report: false);
  }

  /// 「⋮」→刷新本源（`refresh_source`）。
  Future<void> refreshSource(String sourceId) => _refreshOne(sourceId);

  Future<void> _refreshOne(String sourceId, {bool report = true}) async {
    if (sourceId.isEmpty) return;
    final int added;
    try {
      added = await ref.read(engineRepositoryProvider).refreshRssSource(sourceId);
    } on EngineFailure catch (exc) {
      // 抓取失败在服务端是**错误信封**，不是静默 0（`rss.refresh` 的 summary 明写）。
      _failures.add(exc);
      _emitStatus(kRssRefreshFailed(exc.message));
      return;
    }
    if (report) _emitStatus(kRssRefreshOneDone(_nameOf(sourceId), added));
    // flet 的 `if str(source_id) == self._selected: await self._reload_sources(True)`：
    // 当前那一本要连角标一起重读，别的源的角标由事件那一路合流去补。
    if (sourceId == _current.selectedSourceId) {
      await _loadSources(keep: true);
    } else {
      _scheduleCountsReload();
    }
  }

  /// 顶栏「全部刷新」（`refresh_all` + `_run_refresh_all`）：这一发是 job，
  /// 受理回包只说服务端肯起；新增几篇要等事件流那一轮收完才知道。
  Future<void> refreshAll() async {
    _refreshId = DateTime.now().microsecondsSinceEpoch;
    _roundStarted = true;
    _roundNewArticles = 0;
    _emit(_current.copyWith(status: kRssRefreshingAll, done: 0, total: 0));
    try {
      await ref
          .read(engineRepositoryProvider)
          .refreshAllRssSources(refreshId: _refreshId);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      _refreshId = 0;
      _roundStarted = false;
      // 受理都没到手：进度条不能留在原地转（flet 走的是 except 那一支，同样收条）。
      _emit(_current.copyWith(
          status: kRssRefreshAllFailed(exc.message), done: 0, total: 0));
    }
  }

  /// 「⋮」→删除本源（确认框的「删除」按下去那一条，`_do_remove`）。
  Future<void> removeSource(String sourceId) async {
    if (sourceId.isEmpty) return;
    final name = _nameOf(sourceId);
    final bool removed;
    try {
      removed = await ref.read(engineRepositoryProvider).removeRssSource(sourceId);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      _emitStatus(kRssRemoveFailed(exc.message));
      return;
    }
    _emit(_current.copyWith(
        status: removed ? kRssRemoved(name) : kRssAlreadyGone));
    await _loadSources(keep: true);
  }

  /// 打开一篇文章（`open_article`）：**正文由这一发才取**（列表那一发没有正文）。
  ///
  /// 顺序照 flet：先把"正在看这一篇"落上（中栏选中态、窄窗跳到正文级由界面自己办），
  /// 再取正文，正文到了之后才写已读。取不到（文章已被另一侧删了）就把右栏退回占位句：
  /// flet 那一侧拿得到 dict 所以没有这一支，而"没有这篇文章"本来就该是空栏，
  /// 不该是一个空白正文。
  Future<void> openArticle(RssArticleRow row) async {
    _emit(_current.copyWith(currentArticleId: row.id));
    final RssArticleDetail? detail;
    try {
      detail = await ref.read(engineRepositoryProvider).openRssArticle(row.id);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      return;
    }
    if (detail == null) {
      _emit(_current
          .copyWith(detailHint: kRssPickArticle)
          .clearDetail());
      return;
    }
    _emit(_current.copyWith(detail: detail, detailHint: ''));
    if (row.read) return;
    await _mark(row);
  }

  /// 点开正文那一发的已读（flet 的 `_mark`）：**失败不打断阅读**（flet 只写日志），
  /// 所以这里不动 status 槽，只往 [failures] 里推一条。
  Future<void> _mark(RssArticleRow row) async {
    final bool changed;
    try {
      changed = await ref.read(engineRepositoryProvider).markRssArticleRead(row.id);
    } on EngineFailure catch (exc) {
      _failures.add(EngineFailure(
          exc.kind, kRssMarkReadFailed(exc.message), action: exc.action));
      return;
    }
    if (!changed) return;
    // 本地同步那两格（第 3 条规矩）：文章行的字重 + 左栏未读角标减一。
    // 角标就地减、不重读整表——flet 也是就地改 `_counts`。
    _emit(_current.copyWith(
        articles: [
          for (final item in _current.articles)
            if (item.id == row.id) _withRead(item) else item,
        ],
        sources: [
          for (final source in _current.sources)
            source.id == row.sourceId
                ? _withUnread(source, source.unread - 1)
                : source,
        ]));
  }

  /// 收藏/取消收藏（`toggle_star`）。
  Future<void> toggleStar(RssArticleRow row) async {
    final want = !row.starred;
    final bool ok;
    try {
      ok = await ref
          .read(engineRepositoryProvider)
          .starRssArticle(articleId: row.id, starred: want);
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      _emitStatus(kRssStarFailed(exc.message));
      return;
    }
    if (!ok) {
      // 服务端那一格布尔说的是"有没有真改到行"：false = 这条文章已经不在了。
      _emitStatus(kRssArticleGone);
      return;
    }
    final shown = _current.detail;
    _emit(_current.copyWith(
        status: want ? kRssStarred : kRssUnstarred,
        articles: [
          for (final item in _current.articles)
            if (item.id == row.id) _withStar(item, want) else item,
        ],
        detail: shown != null && shown.row.id == row.id
            ? RssArticleDetail(
                row: _withStar(shown.row, want), text: shown.text)
            : shown));
  }

  /// 源名（状态句里那一格）。flet 的 `_name_of` 取的是 `name or source_id`。
  String _nameOf(String sourceId) {
    for (final source in _current.sources) {
      if (source.id == sourceId) {
        return source.name.isEmpty ? sourceId : source.name;
      }
    }
    return sourceId;
  }

  void _emitStatus(String message) => _emit(_current.copyWith(status: message));

  // ------------------------------------------------------------------ #
  // 事件
  // ------------------------------------------------------------------ #
  /// 一条 `RssRefreshProgressEvent`（`done`/`total`/`current`/`failures`/`refresh_id`）。
  ///
  /// 服务端已经按 0.5s 合流（`COALESCED_EVENTS` 里有这一条），这里不再自己攒窗口：
  /// 进度条读的是长度尺，晚半秒到的 `done` 也是对的那一个。
  void onRefreshProgress(EngineEvent event) {
    final frame = RssRefreshProgress.fromJson(event.payload);
    final terminal = !rssProgressVisible(frame.done, frame.total);
    if (_refreshId == 0) {
      // 这一屏没发起过本轮（切页回来、或另一个客户端起的）。
      if (terminal) return; // 已经收尾的那一轮：没有进度可跟，也没有篇数可报（第 6 条）。
      _refreshId = frame.refreshId;
    } else if (frame.refreshId != _refreshId) {
      return;
    }
    if (terminal) {
      // 收尾那一帧（跑完，或本轮一个源都没有）：进度归零、角标补上
      // （flet 的 `_after_refresh_done`）。
      final started = _roundStarted;
      final added = _roundNewArticles;
      _refreshId = 0;
      _roundStarted = false;
      _roundNewArticles = 0;
      _emit(_current.copyWith(done: 0, total: 0));
      if (started) {
        _emitStatus(frame.failures > 0
            ? kRssRefreshDoneWithFailures(added, frame.failures)
            : kRssRefreshDone(added));
      }
      unawaited(_loadSources(keep: true));
      return;
    }
    _emit(_current.copyWith(
      done: frame.done,
      total: frame.total,
      // flet 的 `if ev.total:`：一个源都没有的那一轮没有"第几条"可说。
      status: frame.total > 0 ? frame.status : _current.status,
    ));
  }

  /// 一条 `RssRefreshedEvent`（某源刷完了：`source_id` + `new_count`）。
  ///
  /// 它**不带本轮身份**（发它的是 `refresh_source`，那时不知道本轮 `refresh_id`），
  /// 所以只在"本轮开着"的时间窗里累加计数；窗外的是别的轮次或别人的单源刷新，
  /// 累进来就是把别人的新增写进自己那句"新增 N 篇"。
  void onRefreshed(EngineEvent event) {
    final refreshed = RssRefreshed.fromJson(event.payload);
    if (_refreshId != 0) _roundNewArticles += refreshed.newCount;
    if (refreshed.sourceId == _current.selectedSourceId) {
      // 当前那一栏的文章变了（flet 就地 `_reload_articles`）。
      unawaited(_fetchArticles(_current));
    }
    _scheduleCountsReload();
  }

  /// 按窗口合一次角标重读：多条 `RssRefreshedEvent` 只发一次 `rss.sources`。
  void _scheduleCountsReload() {
    if (_countsFlush?.isActive ?? false) return;
    _countsFlush = Timer(_flushWindow, () => unawaited(_loadSources(keep: true)));
  }
}

/// copyWith 的一个参数值 = "这一格不改"。`null` 不能兼差这一职，因为"把正文清空"
/// 也是一件真实的事（右栏退回占位句），两者必须分得开。
const Object _keep = Object();

/// 这一屏的全部可变状态：三个列表 + 选中两格 + 右栏那两格 + 一个状态槽 + 进度两格。
class RssState {
  const RssState({
    this.sources = const <RssSourceRow>[],
    this.articles = const <RssArticleRow>[],
    this.selectedSourceId = '',
    this.unreadOnly = false,
    this.detail,
    this.currentArticleId,
    this.detailHint = kRssPickSource,
    this.status = '',
    this.done = 0,
    this.total = 0,
  });

  /// 起手那一格：与 flet `RssView.__init__` 一致（右栏「选一个订阅源」，其余皆空）。
  static const RssState empty = RssState();

  final List<RssSourceRow> sources;
  final List<RssArticleRow> articles;

  /// 左栏选中那一本的 id（= feed 地址）。空串 = 一本都没选中（源表为空时就是这样）。
  final String selectedSourceId;

  /// 「只看未读」那个勾。它折的是服务端 `read_flag` 那一格，与左栏角标同源。
  final bool unreadOnly;

  /// 右栏那一篇的正文（只有 `rss.article` 给得出这一格）。
  final RssArticleDetail? detail;

  /// 中栏选中那一篇的 id（flet 的 `_current["id"]`）。与 [detail] 分两格是因为跨进程
  /// 取正文要一次往返：点下去先把选中态落上，正文晚一拍才到。
  final int? currentArticleId;

  /// 右栏没有正文时那一句占位（flet `clear_detail` 的三个入参值）。
  /// 空串 = 有正文可显示（[detail] 非 null）。
  final String detailHint;

  /// 全屏唯一的状态槽（文件头第 1 条）。
  final String status;

  /// 进度两格：跑完或没源可刷就都归 0，进度条随之收起（flet `set_progress(0,0)`）。
  final int done;
  final int total;

  bool get hasSource => selectedSourceId.isNotEmpty;
  bool get hasArticle => currentArticleId != null;
  bool get progressVisible => rssProgressVisible(done, total);
  double get progressValue => rssProgressValue(done, total);

  /// 左栏标题右侧那一格（flet 的 `f"{len(sources)} 个"`）。
  String get sourceCountLabel => '${sources.length} 个';

  RssState copyWith({
    List<RssSourceRow>? sources,
    List<RssArticleRow>? articles,
    String? selectedSourceId,
    bool? unreadOnly,
    Object? detail = _keep,
    Object? currentArticleId = _keep,
    String? detailHint,
    String? status,
    int? done,
    int? total,
  }) =>
      RssState(
          sources: sources ?? this.sources,
          articles: articles ?? this.articles,
          selectedSourceId: selectedSourceId ?? this.selectedSourceId,
          unreadOnly: unreadOnly ?? this.unreadOnly,
          detail: detail == _keep ? this.detail : detail as RssArticleDetail?,
          currentArticleId: currentArticleId == _keep
              ? this.currentArticleId
              : currentArticleId as int?,
          detailHint: detailHint ?? this.detailHint,
          status: status ?? this.status,
          done: done ?? this.done,
          total: total ?? this.total);

  /// 右栏退回"没有打开的文章"：正文与中栏选中态一起清。
  /// 单独一个方法而不是 `copyWith(detail: null)`，因为那里的 null 是"不改"。
  RssState clearDetail() => copyWith(detail: null, currentArticleId: null);
}

/// 改一行文章的已读旗标（模型不可变，按服务端那条 `UPDATE` 的口径造新一行）。
RssArticleRow _withRead(RssArticleRow row) => _withFlags(row, read: true);

RssArticleRow _withStar(RssArticleRow row, bool starred) =>
    _withFlags(row, starred: starred);

RssArticleRow _withFlags(RssArticleRow row, {bool? read, bool? starred}) =>
    RssArticleRow(
        id: row.id,
        sourceId: row.sourceId,
        title: row.title,
        url: row.url,
        author: row.author,
        pubDate: row.pubDate,
        image: row.image,
        read: read ?? row.read,
        starred: starred ?? row.starred);

/// 改左栏那一格的未读数（点开一篇就减一，不重读整表）。
RssSourceRow _withUnread(RssSourceRow row, int unread) => RssSourceRow(
    id: row.id,
    name: row.name,
    url: row.url,
    enabled: row.enabled,
    group: row.group,
    customOrder: row.customOrder,
    type: row.type,
    lastUpdateTime: row.lastUpdateTime,
    total: row.total,
    // 夹在 0 以上：那一格在服务端是按 `read_flag=0` 数出来的，本地减多了会变成
    // "-1 未读"，那比 0 更难解释。
    unread: unread < 0 ? 0 : unread);
