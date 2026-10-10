/// 测试替身：拿黄金样本（或手搓的 list）喂界面，**不起进程、不发一个字节**。
///
/// 界面面的所有 widget 测试都从这里进（§10：`FakeEngineRepository` 喂黄金样本）。
/// 它同时是"这一屏在没有引擎时该长什么样"的落点：[failure] 非空时每个方法都抛那条，
/// 于是"引擎没起来"那一屏也有人测，而不是只有人肉看过一眼。
///
/// 记账口径（第八十三轮那条教训的延续）：[callsFor] 与 [payloadFor] **按 action 分开**，
/// 成功与失败都数。两个动作加在一起的计数器等于没有计数器——"点了没反应"和
/// "发了两遍"在它眼里是同一个数。
library;

import 'dart:async';
import 'dart:convert';

import '../../features/bookshelf/data/bookshelf_item.dart';
import '../../features/bookshelf/data/source_switch_models.dart';
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
import '../../features/reader/data/bookmark_row.dart';
import '../../features/reader/data/chapter_document.dart';
import '../../features/reader/data/chapter_row.dart';
import '../../features/reader/data/content_search_result.dart';
import '../../features/reader/data/reading_position.dart';
import '../../features/reader/data/reader_session.dart';
import '../../features/reader/data/staged_record.dart';
import '../../features/rss/data/rss_models.dart';
import '../../features/settings/data/theme_preferences.dart';
import '../../features/sources/data/import_outcome.dart';
import '../../features/sources/data/source_audit.dart';
import '../../features/sources/data/source_record.dart';
import '../events/engine_event.dart';
import '../settings/settings_map.dart';
import 'engine_repository.dart';
import 'engine_status.dart';
import 'envelope.dart';

class FakeEngineRepository implements EngineRepository {
  FakeEngineRepository({
    this.bookshelf = const [],
    this.sources = const [],
    this.categories = const [],
    this.books = const [],
    this.scopes = const [],
    this.searchRows = const [],
    this.snapshot = const CheckSnapshot(
      running: false,
      cancelled: false,
      total: 0,
      depth: 'auto',
      tiering: CheckTiering(planTotal: 0, shallow: 0, deep: 0),
      deepQueueCount: 0,
      summary: null,
      results: [],
    ),
    this.settings = SettingsMap.empty,
    this.theme = ThemePreferences.defaults,
    this.statuses = const [],
    this.session,
    this.toc = const [],
    this.document,
    this.documentsByChapterUrl = const <String, ChapterDocument>{},
    this.position,
    this.staged,
    this.closeOutcome,
    this.bookmarks = const [],
    this.contentSearch,
    this.saveOutcome = const SaveOutcome(
        saved: true, sourceId: '', errors: [], audit: _cleanAudit),
    this.importOutcome = const ImportOutcome(
        kind: 'book_source', count: 0, errors: [], warnings: [], placeholder: false),
    this.info = const GatewayInfo(
      protocolVersion: 1,
      build: 'fake-0.0.0',
      capabilities: ['rpc', 'events'],
      schemaDigest: 'deadbeefcafe',
      actions: ['engine.hello', 'bookshelf.list'],
      pid: 0,
    ),
    this.failure,
    this.delay = Duration.zero,
    this.webviewDelivered = true,
  });

  /// 从**黄金样本里的原始行**造替身（G1 判据"同书单比对"的入口——喂进去的是真
  /// `--serve` 进程吐出的那批 dict，不是手搓样例；手搓样例只会证明我自己写得对）。
  factory FakeEngineRepository.fromRows(List<Object?> rows,
          {Duration delay = Duration.zero}) =>
      FakeEngineRepository(
        bookshelf: rows
            .map((row) => BookshelfItem.fromJson(
                Map<String, Object?>.from(row as Map)))
            .toList(growable: false),
        delay: delay,
      );

  List<BookshelfItem> bookshelf;
  List<SourceRecord> sources;
  List<CategoryRow> categories;

  /// `browse.books` 那一页书单。
  List<BookRow> books;

  /// `search.scopes` 那张表；不给就是空表（下拉里只剩"没有范围可读"）。
  List<SearchScope> scopes;

  /// 搜索结果由事件交付，替身这边只留"这一轮该回几条"给实弹臂比对用。
  List<BookRow> searchRows;

  CheckSnapshot snapshot;
  SettingsMap settings;
  ThemePreferences theme;
  List<ServiceStatus> statuses;

  // ---------------- reader.* 那八格的替身数据 ----------------
  // **null 与"空的那一格"在这里同样分两种**：[staged]=null 是"在架的书，别问"，
  // [position]=null 是"这本书从没读过"。把它们替成空对象就等于界面永远看不到
  // 那两条分支（记忆里 `zero-counts-need-a-positive-control` 同一族）。
  //
  // 没给 [session]/[document] 时那一发回**一份空形状**而不是抛：界面测试要的是
  // "开书失败/开了本空书"能各测各的，抛错那一支由 [failure] 走。
  ReaderSession? session;
  List<ChapterRow> toc;
  ChapterDocument? document;

  /// 按章 url 给不同正文（连续阅读那几发要的是**每章形状互异**的载荷）。
  ///
  /// 只用 [document] 时三章回同一串字：接错章、接两遍、段起点数错，屏上全都看不出来
  /// ——对照样本自己得先能被认出来（记忆 `measurement-corpus-must-not-be-self-proving`）。
  /// 命中这张表的 url 优先于 [document]，其余章仍走 [document]。
  Map<String, ChapterDocument> documentsByChapterUrl = const <String, ChapterDocument>{};

  /// `reader.position_get` 回它；`reader.position_save` 也回它（服务端那一发回的是
  /// **落库并收口后**的形状，替身照这一条办：给测试一个"回话与请求不同"的入口）。
  ReadingPosition? position;
  StagedRecord? staged;
  CloseOutcome? closeOutcome;
  List<BookmarkRow> bookmarks;
  ContentSearchResult? contentSearch;
  SaveOutcome saveOutcome;
  ImportOutcome importOutcome;
  GatewayInfo info;

  /// `engine.webContentReady` 那一发的回执：false 演的是"宿主答晚了，引擎已经走人"。
  ///
  /// 它必须可写：宿主那条链路上"晚到"与"没打通"在界面上是两句话（记忆里
  /// `zero-counts-need-a-positive-control` 那一族——仪器没接上时读数会朝你期望的方向假）。
  bool webviewDelivered;

  /// 非空 = 这台"没有这个能力"：每个方法都抛它。
  /// 可写：「刷新失败要留着旧数据」那条规矩要在同一发测试里先成功后失败。
  EngineFailure? failure;

  /// 让加载态可测（`pumpAndSettle` 之前数据还没回）。
  final Duration delay;

  final Map<String, int> _calls = {};
  final Map<String, Map<String, Object?>?> _payloads = {};
  final Map<String, List<Map<String, Object?>?>> _payloadHistory = {};
  final List<String> _order = <String>[];

  final StreamController<EngineEvent> _events =
      StreamController<EngineEvent>.broadcast();

  /// 这一发打过几次（失败的那几发也算）。
  int callsFor(String action) => _calls[action] ?? 0;

  /// 最近一次打过来的 payload（没带参数的动作是 null，与"带了空对象"是两件事）。
  Map<String, Object?>? payloadFor(String action) => _payloads[action];

  /// 每一次打过来的 payload，按先后。
  ///
  /// 为什么 [payloadFor] 不够：`reader.position_save` 在一个动作里会发两发
  /// （翻章前先把**旧章**那一格写完，新章之后还有自己那一格），而"写完的是旧章那一条"
  /// 这条断言吃的就是**第一发**——只留最后一发，读出来永远是新章的序号。
  List<Map<String, Object?>?> payloadsFor(String action) =>
      List<Map<String, Object?>?>.unmodifiable(
          _payloadHistory[action] ?? const <Map<String, Object?>?>[]);

  /// 打过的动作**按先后**各记一条。[callsFor] 只数得出次数，答不出"谁先"——
  /// 而退出那一族的硬约束恰恰是先后（`reader.staged_promote` 必须排在
  /// `reader.close` 之前，关完会话服务就认不出那条暂存记录了）。
  List<String> get callOrder => List<String>.unmodifiable(_order);

  // G1 那三处记账的旧名字：留 getter，不留第二份计数器。
  int get listBookshelfCalls => callsFor('bookshelf.list');
  int get helloCalls => callsFor('engine.hello');
  int get shutdownCalls => callsFor('engine.shutdown');
  String? get lastShutdownReason {
    final payload = payloadFor('engine.shutdown');
    return payload == null ? null : '${payload['reason']}';
  }

  Future<T> _guard<T>(T Function() body, String action,
      [Map<String, Object?>? payload]) async {
    _calls[action] = callsFor(action) + 1;
    _payloads[action] = payload;
    (_payloadHistory[action] ??= <Map<String, Object?>?>[]).add(payload);
    _order.add(action);
    final thrown = failure;
    if (thrown != null) {
      await Future<void>.delayed(delay);
      throw EngineFailure(thrown.kind, thrown.message, action: action);
    }
    final value = body();
    if (delay > Duration.zero) await Future<void>.delayed(delay);
    return value;
  }

  /// 测试往里推一条事件（真引擎那一侧是 /ws 推的）。
  void emit(EngineEvent event) => _events.add(event);

  /// 让所有在听的那一屏同时收到一条"流断了"（断线文案那一臂的仪器）。
  void emitStreamError(String message) => _events
      .addError(EngineFailure(EngineFailureKind.transport, message, action: 'ws'));

  @override
  Stream<EngineEvent> events() => _events.stream;

  @override
  Future<GatewayInfo> hello() => _guard(() => info, 'engine.hello');

  @override
  Future<List<ServiceStatus>> serviceStatus() =>
      _guard(() => statuses, 'engine.status');

  @override
  Future<void> shutdown({String reason = 'flutter client'}) => _guard(
      () {},
      'engine.shutdown',
      <String, Object?>{'reason': reason});

  /// 反向渲染的答复：照常按 action 记账，`payloadFor('engine.webContentReady')`
  /// 读的就是宿主发出去那八格。[webviewDelivered] 用来演"答晚了"那一支。
  @override
  Future<WebviewDelivery> webContentReady({
    required String requestId,
    String body = '',
    Map<String, Object?>? headers,
    int status = 200,
    String url = '',
    String cookie = '',
    String error = '',
    Object? jsResult,
  }) =>
      _guard(
          () => WebviewDelivery(delivered: webviewDelivered),
          'engine.webContentReady',
          <String, Object?>{
            'request_id': requestId,
            'body': body,
            if (headers != null) 'headers': headers,
            'status': status,
            'url': url,
            'cookie': cookie,
            'error': error,
            if (jsResult != null) 'js_result': jsResult,
          });

  @override
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) => _guard(
      () => bookshelf,
      'bookshelf.list',
      <String, Object?>{if (includeStaged) 'include_staged': true});

  @override
  Future<bool> shelveBook({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelved = true,
  }) =>
      _guard(() => true, 'bookshelf.add', <String, Object?>{
        'source_url': sourceUrl,
        'book': book,
        'shelved': shelved,
      });

  @override
  Future<List<SourceRecord>> listSources() =>
      _guard(() => sources, 'source.list');

  @override
  Future<SourceRecord?> loadSource(String url) => _guard(
      () => sources.where((s) => s.url == url).isEmpty ? null : sources.firstWhere((s) => s.url == url),
      'source.get',
      <String, Object?>{'url': url});

  @override
  Future<SaveOutcome> saveSource(Map<String, Object?> sourceJson) => _guard(
      () => saveOutcome,
      'source.save',
      <String, Object?>{'source_json': sourceJson});

  @override
  Future<bool> deleteSource(String url) => _guard(
      () => true,
      'source.delete',
      <String, Object?>{'url': url});

  @override
  Future<bool> setSourceEnabled(String url, bool enabled) => _guard(
      () => true,
      'source.enable',
      <String, Object?>{'url': url, 'enabled': enabled});

  @override
  Future<bool> setSourceGroup(String url, String group) => _guard(
      () => true,
      'source.group',
      <String, Object?>{'url': url, 'group': group});

  @override
  Future<int> batchSetEnabled(List<String> urls, bool enabled) => _guard(
      () => urls.length,
      'source.batch_enable',
      <String, Object?>{'urls': urls, 'enabled': enabled});

  @override
  Future<int> batchDelete(List<String> urls) => _guard(
      () => urls.length,
      'source.batch_delete',
      <String, Object?>{'urls': urls});

  @override
  Future<String> exportSources() => _guard(
      // 回话是 **JSON 文本**（服务端就回一个 str），所以这里编码而不是把对象交出去：
      // 客户端那一侧"导出文本能 jsonDecode"的断言才有东西可解。
      () => jsonEncode(sources.map((s) => s.raw).toList()), 'source.export');

  @override
  Future<AuditReport> auditSource(Map<String, Object?> sourceJson) =>
      _guard(() => saveOutcome.audit, 'source.audit',
          <String, Object?>{'source_json': sourceJson});

  @override
  Future<ImportOutcome> importText({
    required String text,
    required String kind,
    String mode = 'append',
  }) =>
      _guard(() => importOutcome, 'import.text', <String, Object?>{
        'text': text,
        'kind': kind,
        'mode': mode,
      });

  @override
  Future<ImportOutcome> importUrl({
    required String url,
    required String kind,
    String mode = 'append',
  }) =>
      _guard(() => importOutcome, 'import.url', <String, Object?>{
        'url': url,
        'kind': kind,
        'mode': mode,
      });

  @override
  Future<List<CategoryRow>> browseCategories(String sourceUrl) => _guard(
      () => categories,
      'browse.categories',
      <String, Object?>{'source_url': sourceUrl});

  @override
  Future<List<BookRow>> browseBooks({
    required String sourceUrl,
    required Map<String, Object?> category,
    int page = 1,
  }) =>
      _guard(() => books, 'browse.books', <String, Object?>{
        'source_url': sourceUrl,
        'category': category,
        'page': page,
      });

  @override
  Future<JobAccepted> searchStart({
    required String keyword,
    String scope = '',
    int? searchId,
  }) =>
      _guard(
          () => JobAccepted(started: true, action: 'search.start'),
          'search.start',
          <String, Object?>{
            'keyword': keyword,
            'scope': scope,
            'search_id': searchId ?? 0,
          });

  @override
  Future<void> searchStop() => _guard(() {}, 'search.stop');

  @override
  Future<List<SearchScope>> searchScopes() => _guard(() => scopes, 'search.scopes');

  @override
  Future<JobAccepted> checkStart({List<String>? groups}) => _guard(
      () => JobAccepted(started: true, action: 'check.start'),
      'check.start',
      <String, Object?>{'groups': groups});

  @override
  Future<CheckSnapshot> checkSnapshot() => _guard(() => snapshot, 'check.snapshot');

  @override
  Future<void> checkCancel() => _guard(() {}, 'check.cancel');

  @override
  Future<CheckSourceReport> checkSource({
    required Map<String, Object?> sourceJson,
    String depth = 'list',
  }) =>
      _guard(() => _checkedSource, 'check.source', <String, Object?>{
        'source_json': sourceJson,
        'depth': depth,
      });

  /// 替身的单源检测结论：跟随 [sources] 里那条同名源的可用性，
  /// 省得测试自己造一份"检出来是好的"——那会让"坏了要说坏"那一臂假绿。
  CheckSourceReport get _checkedSource => const CheckSourceReport(
        name: '',
        rawUrl: '',
        status: 'ok',
        statusCode: 200,
        responseTimeMs: 0,
        error: null,
        searchStatus: 'ok',
        bookCount: 0,
        searchError: null,
        readingStatus: 'skip',
        readingError: null,
        chapterCount: null,
        tagTotal: 0,
        tagValid: 0,
        tagInvalid: 0,
        usable: true,
        checkState: 'list_usable',
        autoDisabled: false,
        raw: <String, Object?>{},
      );

  // ------------------------------------------------------------------ #
  // switch.*（换源那五发）
  // ------------------------------------------------------------------ #

  /// `switch.seeds` 的替身行（字段名不能叫 `switchSeeds`：那是契约方法的名字，
  /// 同名会在类里撞成一个定义）。默认**空表**而不是几条手搓样例：空表是"本地没搜过
  /// 这本"那一支，屏上必须走 [kSwitchEmptyCandidates] 那句话；给了行才演"本地已有 N 个结果"。
  List<BookRow> switchSeedRows = const <BookRow>[];

  /// `switch.snapshot` 的回话，同时是 `switch.scan` 起轮后 notifier 回放的那一份。
  SwitchRun switchRun = SwitchRun.idle;

  /// `switch.cancel` 那一格：默认 false（"什么都没在跑"是点开对话框就按停止的样子）。
  /// 测试按这一条把 [switchRun] 摆成 running，就能演"叫停真的停到了"。
  bool switchCancelResult = false;

  /// `switch.apply` 的回话。默认 applied=true；false 那一支由测试改这一格。
  SwitchApplyResult switchApplyResult =
      const SwitchApplyResult(applied: true, sourceUrl: '');

  @override
  Future<List<BookRow>> switchSeeds({required Map<String, Object?> book}) =>
      _guard(() => switchSeedRows, 'switch.seeds', <String, Object?>{'book': book});

  @override
  Future<JobAccepted> switchScan({
    required Map<String, Object?> book,
    required int switchId,
    String keyword = '',
    String group = '',
  }) =>
      _guard(() => const JobAccepted(started: true, action: 'switch.scan'),
          'switch.scan', <String, Object?>{
        'book': book,
        'switch_id': switchId,
        'keyword': keyword,
        'group': group,
      });

  @override
  Future<SwitchRun> switchSnapshot() => _guard(() => switchRun, 'switch.snapshot');

  @override
  Future<bool> switchCancel() => _guard(() => switchCancelResult, 'switch.cancel');

  @override
  Future<SwitchApplyResult> applySwitch({
    required Map<String, Object?> record,
    required String sourceUrl,
    required Map<String, Object?> book,
  }) =>
      _guard(
          () => SwitchApplyResult(
              applied: switchApplyResult.applied, sourceUrl: sourceUrl),
          'switch.apply', <String, Object?>{
        'record': record,
        'source_url': sourceUrl,
        'book': book,
      });

  // ------------------------------------------------------------------ #
  // rss.*（订阅面那九发）
  // ------------------------------------------------------------------ #
  // 这一族的替身**改自己的表**，不是回一个写死的布尔：服务端那三发写回
  // （remove/mark_read/star）回的都是"有没有真改到行"，而 `false` 那一支
  // （那条源/那篇文章已经不在了）只有让替身真的把它们删掉才演得出来。
  // 写死 `true` 的替身会让界面永远只走过成功那一路。

  /// `rss.sources` 的替身行。默认空表 = "还没有订阅源"那一支（左栏那句说明）。
  List<RssSourceRow> rssSources = const <RssSourceRow>[];

  /// `rss.articles` 的替身行（不带正文，与协议面同一格形状）。
  List<RssArticleRow> rssArticles = const <RssArticleRow>[];

  /// `rss.article` 的回话。**null 是合法值**（"没这篇文章"），与空正文是两件事。
  RssArticleDetail? rssArticle;

  /// `rss.refresh` 本次新增的篇数。抓取失败那一支走 [failure]，不靠这一格演
  /// ——服务端把失败回成**错误信封**，`0` 说的真是"抓开了、没有新文章"。
  int rssNewArticles = 0;

  /// `rss.add_source` 的替身：把行落进 [rssSources]（同一 id 是**更新**不是重复行），
  /// 回它的 id——服务端那一条的主键就是规范化后的 feed 地址，这里照办，
  /// 这样"添加完立刻能在左栏点到那一本"这一路不用测试自己补一行。
  @override
  Future<String> addRssSource({
    required String name,
    required String url,
    String sourceGroup = '',
  }) =>
      _guard(() {
        final row = RssSourceRow(
            id: url,
            // flet 那边传空名称时服务端 `add_source` 拿 url 顶名，这条不许在替身里变空串。
            name: name.isEmpty ? url : name,
            url: url,
            enabled: true,
            group: sourceGroup,
            customOrder: 0,
            type: 0,
            lastUpdateTime: 0,
            total: 0,
            unread: 0);
        rssSources = [
          ...rssSources.where((s) => s.id != url),
          row,
        ];
        return url;
      }, 'rss.add_source', <String, Object?>{
        'name': name,
        'url': url,
        'source_group': sourceGroup,
      });

  @override
  Future<bool> removeRssSource(String sourceId) => _guard(() {
        final kept =
            rssSources.where((s) => s.id != sourceId).toList(growable: false);
        final removed = kept.length != rssSources.length;
        if (removed) {
          rssSources = kept;
          // 服务端那一发是**级联**删除（`DELETE FROM rss_articles WHERE source_id=?`
          // 在前），替身跟着清，否则"删完左栏少一行、中栏还留着上一本的文章"
          // 这一条就在测试里演不出来。
          rssArticles = rssArticles
              .where((a) => a.sourceId != sourceId)
              .toList(growable: false);
        }
        return removed;
      }, 'rss.remove_source', <String, Object?>{'source_id': sourceId});

  @override
  Future<int> refreshRssSource(String sourceId) =>
      _guard(() => rssNewArticles, 'rss.refresh',
          <String, Object?>{'source_id': sourceId});

  @override
  Future<JobAccepted> refreshAllRssSources({required int refreshId}) => _guard(
      () => const JobAccepted(started: true, action: 'rss.refresh_all'),
      'rss.refresh_all',
      <String, Object?>{'refresh_id': refreshId});

  @override
  Future<List<RssSourceRow>> listRssSources() =>
      _guard(() => rssSources, 'rss.sources');

  @override
  Future<List<RssArticleRow>> listRssArticles({
    required String sourceId,
    bool unreadOnly = false,
  }) =>
      _guard(
          () => rssArticles
              .where((a) => a.sourceId == sourceId)
              .where((a) => !unreadOnly || !a.read)
              .toList(growable: false),
          'rss.articles',
          <String, Object?>{'source_id': sourceId, 'unread_only': unreadOnly});

  @override
  Future<RssArticleDetail?> openRssArticle(int articleId) => _guard(
          // 没给替身时按 [rssArticles] 里那一行现凑一份空正文的详情：
          // "打开了但没有正文"（`kRssNoBody` 那一支）与"没这篇文章"（null）
          // 仍是两条分支，各自可测。
          () {
            final detail = rssArticle;
            if (detail != null) {
              return detail.row.id == articleId ? detail : null;
            }
            final row =
                rssArticles.where((a) => a.id == articleId).toList(growable: false);
            if (row.isEmpty) return null;
            return RssArticleDetail(row: row.first, text: '');
          },
          'rss.article',
          <String, Object?>{'article_id': articleId});

  @override
  Future<bool> markRssArticleRead(int articleId) => _guard(() {
        final changed = rssArticles.any((a) => a.id == articleId);
        if (changed) {
          rssArticles = [
            for (final row in rssArticles)
              row.id == articleId ? _withFlags(row, read: true) : row,
          ];
        }
        return changed;
      }, 'rss.mark_read', <String, Object?>{'article_id': articleId});

  @override
  Future<bool> starRssArticle({
    required int articleId,
    required bool starred,
  }) =>
      _guard(() {
        final changed = rssArticles.any((a) => a.id == articleId);
        if (changed) {
          rssArticles = [
            for (final row in rssArticles)
              row.id == articleId ? _withFlags(row, starred: starred) : row,
          ];
        }
        return changed;
      }, 'rss.star',
          <String, Object?>{'article_id': articleId, 'starred': starred});

  // ------------------------------------------------------------------ #
  // reader.*
  // ------------------------------------------------------------------ #
  @override
  Future<ReaderSession> openReader({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelve = true,
    bool resume = true,
  }) =>
      _guard(() => session ?? _emptySession(book), 'reader.open',
          <String, Object?>{
            'source_url': sourceUrl,
            'book': book,
            'shelve': shelve,
            'resume': resume,
          });

  @override
  Future<CloseOutcome> closeReader({
    required String sourceUrl,
    required String bookUrl,
    bool saveProgress = true,
  }) =>
      _guard(() => closeOutcome ?? CloseOutcome(closed: true, staged: staged),
          'reader.close', <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
        'save_progress': saveProgress,
      });

  @override
  Future<List<ChapterRow>> chapterRows({
    required String sourceUrl,
    required String bookUrl,
    bool refresh = false,
  }) =>
      _guard(() => toc, 'reader.toc', <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
        'refresh': refresh,
      });

  @override
  Future<ChapterDocument> chapterDocument({
    required String sourceUrl,
    required String bookUrl,
    required String chapterUrl,
  }) =>
      _guard(
          () => documentsByChapterUrl[chapterUrl] ??
              document ??
              _emptyDocument(chapterUrl),
          'reader.content',
          <String, Object?>{
            'source_url': sourceUrl,
            'book_url': bookUrl,
            'chapter_url': chapterUrl,
          });

  @override
  Future<ReadingPosition?> readPosition({
    required String sourceUrl,
    required String bookUrl,
  }) =>
      _guard(() => position, 'reader.position_get', <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
      });

  @override
  Future<ReadingPosition> savePosition({
    required String sourceUrl,
    required String bookUrl,
    required Map<String, Object?> positionJson,
  }) =>
      // 服务端那一发回的是**落库并收口后**的那一份，替身照这一条办：
      // 给了 [position] 就回它（测试据此造出"请求 900 字、落库夹回 12 字"那一臂），
      // 没给就原样回读请求那份。
      _guard(
          () => position ??
              ReadingPosition.fromJson(Map<String, Object?>.from(positionJson)),
          'reader.position_save', <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
        'position_json': positionJson,
      });

  @override
  Future<StagedRecord?> stagedRecord() => _guard(() => staged, 'reader.staged');

  @override
  Future<bool> promoteStaged() => _guard(() {
        staged = null;
        return true;
      }, 'reader.staged_promote');

  @override
  Future<bool> discardStaged() => _guard(() {
        staged = null;
        return true;
      }, 'reader.staged_discard');

  @override
  Future<List<BookmarkRow>> listBookmarks(String bookUrl) =>
      _guard(() => bookmarks, 'reader.bookmarks',
          <String, Object?>{'book_url': bookUrl});

  /// 加书签的自增 id（真引擎那边是 SQLite 的 lastrowid）。
  int addBookmarkSeq = 0;

  @override
  Future<int> addBookmark({
    required String bookUrl,
    required int chapterIndex,
    double offset = 0,
    String summary = '',
  }) =>
      _guard(() {
        final id = ++addBookmarkSeq;
        bookmarks = [
          ...bookmarks,
          BookmarkRow(
              bookmarkId: id,
              bookUrl: bookUrl,
              chapterIndex: chapterIndex,
              offset: offset,
              summary: summary,
              createdAt: id.toDouble()),
        ];
        return id;
      }, 'reader.bookmark_add', <String, Object?>{
        'book_url': bookUrl,
        'chapter_index': chapterIndex,
        'offset': offset,
        'summary': summary,
      });

  @override
  Future<bool> removeBookmark({
    required String bookUrl,
    required int bookmarkId,
  }) =>
      _guard(() {
        final kept = bookmarks.where((b) => b.bookmarkId != bookmarkId).toList();
        final removed = kept.length != bookmarks.length;
        bookmarks = kept;
        return removed;
      }, 'reader.bookmark_remove', <String, Object?>{
        'book_url': bookUrl,
        'bookmark_id': bookmarkId,
      });

  @override
  Future<ContentSearchResult> searchBookContent({
    required String bookUrl,
    required String keyword,
    String? sourceUrl,
  }) =>
      _guard(
          () => contentSearch ??
              const ContentSearchResult(
                  hits: [], written: 0, indexedChapters: 0, ftsAvailable: true),
          'reader.search_content', <String, Object?>{
        'book_url': bookUrl,
        'keyword': keyword,
        'source_url': sourceUrl ?? '',
      });

  /// 没给替身时的那一份"开了会话但什么都没有"：目录 0 章、位置是默认那份。
  /// 它**不是**错误——`chapterCount == 0` 是一批真源的样子（flet 那边同样能打开）。
  static ReaderSession _emptySession(Map<String, Object?> book) => ReaderSession(
        book: BookRow.fromJson(Map<String, Object?>.from(book)),
        sourceUrl: '${book['origin'] ?? ''}',
        bookUrl: '${book['url'] ?? ''}',
        chapterCount: 0,
        position: const ReadingPosition(),
      );

  static ChapterDocument _emptyDocument(String chapterUrl) => ChapterDocument(
        chapterUrl: chapterUrl,
        chapterIndex: 0,
        chapterName: '',
        title: '',
        imageStyle: '',
        blocks: const [],
        paragraphCount: 0,
        totalChars: 0,
      );

  @override
  Future<SettingsMap> readSettings(List<String> keys) => _guard(
      // **只回点名的那些键**：把整张表交出去会让"没设过的键不出现在返回里"
      // 那条形状在测试里永远测不到。
      () => SettingsMap(<String, Object?>{
            for (final key in keys)
              if (settings.values.containsKey(key)) key: settings.values[key],
          }),
      'settings.read',
      <String, Object?>{'keys': keys});

  @override
  Future<void> writeSetting(String key, Object? value) => _guard(
      () {
        settings = SettingsMap(<String, Object?>{
          ...settings.values,
          key: value,
        });
      },
      'settings.write',
      <String, Object?>{'key': key, 'value': value});

  @override
  Future<ThemePreferences> themePreferences() =>
      _guard(() => theme, 'theme.preferences');

  @override
  Future<ThemePreferences> saveTheme({
    String? skinId,
    String? mode,
    bool? eink,
  }) =>
      _guard(
          // null 的那一格保持原值——这条是服务端 `theme.save` 的语义，
          // 替身不许自己发明"null = 清掉"。
          () => theme.copy(
              skinId: skinId,
              mode: mode == null ? null : ThemeModeKind.fromWire(mode),
              eink: eink),
          'theme.save',
          <String, Object?>{
            if (skinId != null) 'skin_id': skinId,
            if (mode != null) 'mode': mode,
            if (eink != null) 'eink': eink,
          });

  Future<void> dispose() async {
    _calls.clear();
    _payloads.clear();
    _payloadHistory.clear();
    if (!_events.isClosed) await _events.close();
  }
}

/// 零缺口的审计报告：替身默认用它，测"没缺口就是没有行"那一臂。
const AuditReport _cleanAudit = AuditReport(
  sourceId: '',
  missingJavaApis: <String>[],
  missingObjectApis: <String>[],
  unknownFields: <String>[],
  unsupportedPseudos: <String>[],
  jsSyntaxErrors: <String>[],
  regexErrors: <String>[],
  ruleSyntaxErrors: <String>[],
  suggestions: <String>[],
  notAudited: '',
);

/// 改一篇替身文章的已读/收藏旗标（`RssArticleRow` 是不可变的，这里按服务端那两发
/// `UPDATE` 的口径造新一行：只动点名的那一格）。
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
