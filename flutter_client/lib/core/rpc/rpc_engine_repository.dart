/// [EngineRepository] 的 RPC 实现：把契约面上那 58 发落到 §4.1 那一个动词上。
///
/// 每一发都做**形状检查**（数组就该是数组、对象就该是对象），检查不过抛
/// `EngineFailureKind.transport`。这不是防御癖：服务端回 `null` 与回 `[]`
/// 是两件事——`source.list` 回 `null` 是形状变了，让它变成"空书架/空库"
/// 就是把故障说成"今天没有数据"（G1 那条规矩的完整版）。
library;

import '../events/engine_event.dart';
import '../events/engine_event_client.dart';
import '../settings/settings_map.dart';
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
import '../../features/settings/data/update_models.dart';
import '../../features/sources/data/import_outcome.dart';
import '../../features/sources/data/source_audit.dart';
import '../../features/sources/data/source_record.dart';
import 'engine_repository.dart';
import 'engine_status.dart';
import 'envelope.dart';
import 'rpc_client.dart';

class RpcEngineRepository implements EngineRepository {
  RpcEngineRepository(this.client);

  final RpcClient client;

  EngineEventClient? _eventClient;

  @override
  Future<GatewayInfo> hello() async {
    final data = await client.hello();
    return GatewayInfo.fromJson(_asMap(data, 'engine.hello'));
  }

  @override
  Future<List<ServiceStatus>> serviceStatus() async =>
      _rows(await client.call('engine.status'), 'engine.status')
          .map(ServiceStatus.fromJson)
          .toList(growable: false);

  @override
  Future<void> shutdown({String reason = 'flutter client'}) async {
    await client.call('engine.shutdown',
        payload: <String, Object?>{'reason': reason}, singleFlight: false);
  }

  // ------------------------------------------------------------------ #
  // update.* - 更新检查域（M5，契约 §1.17）
  // ------------------------------------------------------------------ #
  @override
  Future<UpdateInfo?> updateCheck() async {
    final data = await client.call('update.check');
    if (data == null) return null;
    return UpdateInfo.fromJson(_asMap(data, 'update.check'));
  }

  @override
  Future<void> updateIgnore(String version) async {
    await client.call('update.ignore',
        payload: <String, Object?>{'version': version});
  }

  // -- G4-E: 工具域 --
  @override
  Future<String?> qrGenerate(String text) async {
    final data = await client.call('qr.generate',
        payload: <String, Object?>{'text': text});
    return data is String ? data : null;
  }

  @override
  Future<String?> qrDecode(String imageData) async {
    final data = await client.call('qr.decode',
        payload: <String, Object?>{'image_data': imageData});
    return data is String ? data : null;
  }

  /// 反向渲染的答复（§8 R1）。两处与全表不同，都不是风格：
  ///
  /// 1. **`singleFlight: false`**。同 action 只许在飞一发"这条是**答两条不同的渲染**
  ///    用的——预取一章以上时引擎会连着求两次。顶替发生时，被顶掉那一发的答案
  ///    在服务端**已经解开了等待**（正文已经交出去了），客户端却抛 `cancelled`，
  ///    宿主于是以为"我没答上"而再答一遍。收摊那一发同样是"发出去就别再顶"。
  /// 2. **`js_result` 缺键与空串是两件事**，所以它按 null 不进 payload
  ///    （契约面那条规矩的实现处；服务端读的是返回字典里有没有这个键）。
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
  }) async {
    final data = _asMap(
        await client.call('engine.webContentReady', payload: <String, Object?>{
          'request_id': requestId,
          'body': body,
          if (headers != null) 'headers': headers,
          'status': status,
          'url': url,
          'cookie': cookie,
          'error': error,
          if (jsResult != null) 'js_result': jsResult,
        }, singleFlight: false),
        'engine.webContentReady');
    final delivered = data['delivered'];
    if (delivered is! bool) {
      throw EngineFailure(EngineFailureKind.transport,
          'engine.webContentReady 的回话里没有布尔 delivered（收到 ${data.keys.join(',')}）：'
          '那一格分的是"引擎还收不收这份"与"这一发根本没打通"，缺了宿主只能猜',
          action: 'engine.webContentReady');
    }
    return WebviewDelivery(delivered: delivered);
  }

  /// 第一次有人听才连（bootstrap 时连一次 = 一个没人在听的流；
  /// 关窗时由 [EngineHandle] 调 [closeEvents] 收）。
  @override
  Stream<EngineEvent> events() {
    final created = _eventClient ??= EngineEventClient(client.endpoint);
    created.start();
    return created.events;
  }

  /// 句柄收摊用；测试里没连过也不炸。
  void closeEvents() {
    final client = _eventClient;
    _eventClient = null;
    client?.close();
  }

  @override
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) async =>
      _rows(await client.call('bookshelf.list', payload: <String, Object?>{
        if (includeStaged) 'include_staged': true,
      }), 'bookshelf.list')
          .map(BookshelfItem.fromJson)
          .toList(growable: false);

  @override
  Future<bool> shelveBook({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelved = true,
  }) async =>
      _asBool(await client.call('bookshelf.add', payload: <String, Object?>{
        'source_url': sourceUrl,
        'book': book,
        'shelved': shelved,
      }), 'bookshelf.add');

  // ------------------------------------------------------------------ #
  // source.* / import.*
  // ------------------------------------------------------------------ #
  @override
  Future<List<SourceRecord>> listSources() async =>
      _rows(await client.call('source.list'), 'source.list')
          .map(SourceRecord.fromJson)
          .toList(growable: false);

  @override
  Future<SourceRecord?> loadSource(String url) async {
    final data = await client.call('source.get',
        payload: <String, Object?>{'url': url});
    // 这一发的 `null` 是**合法回话**（`SourceService.load_source` 没找到就回 None），
    // 所以它不走 _asMap：形状检查会把"没有这条源"变成一次传输错误。
    if (data == null) return null;
    return SourceRecord.fromJson(_asMap(data, 'source.get'));
  }

  @override
  Future<SaveOutcome> saveSource(Map<String, Object?> sourceJson) async =>
      SaveOutcome.fromJson(_asMap(
          await client.call('source.save',
              payload: <String, Object?>{'source_json': sourceJson}),
          'source.save'));

  @override
  Future<bool> deleteSource(String url) async => _asBool(
      await client.call('source.delete',
          payload: <String, Object?>{'url': url}),
      'source.delete');

  @override
  Future<bool> setSourceEnabled(String url, bool enabled) async => _asBool(
      await client.call('source.enable',
          payload: <String, Object?>{'url': url, 'enabled': enabled}),
      'source.enable');

  @override
  Future<bool> setSourceGroup(String url, String group) async => _asBool(
      await client.call('source.group',
          payload: <String, Object?>{'url': url, 'group': group}),
      'source.group');

  @override
  Future<int> batchSetEnabled(List<String> urls, bool enabled) async => _asInt(
      await client.call('source.batch_enable',
          payload: <String, Object?>{'urls': urls, 'enabled': enabled}),
      'source.batch_enable');

  @override
  Future<int> batchDelete(List<String> urls) async => _asInt(
      await client.call('source.batch_delete',
          payload: <String, Object?>{'urls': urls}),
      'source.batch_delete');

  @override
  Future<String> exportSources() async {
    final data = await client.call('source.export');
    if (data is! String) {
      throw EngineFailure(EngineFailureKind.transport,
          'source.export 的回话不是字符串（收到 ${data.runtimeType}）：'
          '那边导出的是 JSON 文本，不是已解析的对象',
          action: 'source.export');
    }
    return data;
  }

  @override
  Future<AuditReport> auditSource(Map<String, Object?> sourceJson) async =>
      AuditReport.fromJson(_asMap(
          await client.call('source.audit',
              payload: <String, Object?>{'source_json': sourceJson}),
          'source.audit'));

  @override
  Future<ImportOutcome> importText({
    required String text,
    required String kind,
    String mode = 'append',
  }) async =>
      ImportOutcome.fromJson(_asMap(
          await client.call('import.text', payload: <String, Object?>{
            'text': text,
            'kind': kind,
            'mode': mode,
          }),
          'import.text'));

  @override
  Future<ImportOutcome> importUrl({
    required String url,
    required String kind,
    String mode = 'append',
  }) async =>
      ImportOutcome.fromJson(_asMap(
          await client.call('import.url', payload: <String, Object?>{
            'url': url,
            'kind': kind,
            'mode': mode,
          }),
          'import.url'));

  // ------------------------------------------------------------------ #
  // browse.* / search.*
  // ------------------------------------------------------------------ #
  @override
  Future<List<CategoryRow>> browseCategories(String sourceUrl) async =>
      _rows(
              await client.call('browse.categories',
                  payload: <String, Object?>{'source_url': sourceUrl}),
              'browse.categories')
          .map(CategoryRow.fromJson)
          .toList(growable: false);

  @override
  Future<List<BookRow>> browseBooks({
    required String sourceUrl,
    required Map<String, Object?> category,
    int page = 1,
  }) async =>
      _rows(
              await client.call('browse.books', payload: <String, Object?>{
                'source_url': sourceUrl,
                'category': category,
                'page': page,
              }),
              'browse.books')
          .map(BookRow.fromJson)
          .toList(growable: false);

  @override
  Future<JobAccepted> searchStart({
    required String keyword,
    String scope = '',
    int? searchId,
  }) async =>
      JobAccepted.fromJson(_asMap(
          await client.call('search.start', payload: <String, Object?>{
            'keyword': keyword,
            'scope': scope,
            'search_id': searchId ?? DateTime.now().microsecondsSinceEpoch,
          }),
          'search.start'));

  @override
  Future<void> searchStop() async {
    await client.call('search.stop');
  }

  @override
  Future<List<SearchScope>> searchScopes() async =>
      _rows(await client.call('search.scopes'), 'search.scopes')
          .map(SearchScope.fromJson)
          .toList(growable: false);

  // ------------------------------------------------------------------ #
  // check.*
  // ------------------------------------------------------------------ #
  @override
  Future<JobAccepted> checkStart({List<String>? groups}) async =>
      JobAccepted.fromJson(_asMap(
          await client.call('check.start', payload: <String, Object?>{
            'groups': groups,
          }),
          'check.start'));

  @override
  Future<CheckSnapshot> checkSnapshot() async => CheckSnapshot.fromJson(
      _asMap(await client.call('check.snapshot'), 'check.snapshot'));

  @override
  Future<void> checkCancel() async {
    await client.call('check.cancel');
  }

  @override
  Future<CheckSourceReport> checkSource({
    required Map<String, Object?> sourceJson,
    String depth = 'list',
  }) async =>
      CheckSourceReport.fromJson(
          _asMap(await client.call('check.source', payload: <String, Object?>{
            'source_json': sourceJson,
            'depth': depth,
          }), 'check.source'));

  // ------------------------------------------------------------------ #
  // switch.*
  // ------------------------------------------------------------------ #
  // 两处形状规矩与全表不同，都不是风格：
  // 1. `switch.scan` 是 **job**：回话里只有受理凭据，候选一根不在这发上。
  //    所以这里不许"顺手"把 `data` 当候选解——`JobAccepted.fromJson` 只认两格。
  // 2. `switch.cancel` 回的是**对象里那一格布尔**（`{cancelled: bool}`），不是裸布尔：
  //    那一格说的是"有没有真叫停下什么"，`false` 时界面别写"已停止"。
  @override
  Future<List<BookRow>> switchSeeds({
    required Map<String, Object?> book,
  }) async =>
      _rows(await client.call('switch.seeds',
          payload: <String, Object?>{'book': book}), 'switch.seeds')
          .map(BookRow.fromJson)
          .toList(growable: false);

  @override
  Future<JobAccepted> switchScan({
    required Map<String, Object?> book,
    required int switchId,
    String keyword = '',
    String group = '',
  }) async =>
      JobAccepted.fromJson(_asMap(await client.call('switch.scan', payload: <String, Object?>{
        'book': book,
        'switch_id': switchId,
        'keyword': keyword,
        'group': group,
      }), 'switch.scan'));

  @override
  Future<SwitchRun> switchSnapshot() async => SwitchRun.fromJson(
      _asMap(await client.call('switch.snapshot'), 'switch.snapshot'));

  @override
  Future<bool> switchCancel() async {
    final data =
        _asMap(await client.call('switch.cancel'), 'switch.cancel');
    final cancelled = data['cancelled'];
    if (cancelled is! bool) {
      throw EngineFailure(EngineFailureKind.transport,
          'switch.cancel 的回话里没有布尔 cancelled（收到 ${data.keys.join(',')}）：'
              '那一格分的是"有没有真叫停下一轮"与"这一发没打通"，'
              '缺了它界面就只能把"什么都没在跑"写成"已停止"',
          action: 'switch.cancel');
    }
    return cancelled;
  }

  @override
  Future<SwitchApplyResult> applySwitch({
    required Map<String, Object?> record,
    required String sourceUrl,
    required Map<String, Object?> book,
  }) async {
    final data = _asMap(await client.call('switch.apply', payload: <String, Object?>{
      'record': record,
      'source_url': sourceUrl,
      'book': book,
    }), 'switch.apply');
    final applied = data['applied'];
    if (applied is! bool) {
      // 这一格不许读成 false：服务端把"没换成"的三种成因（记录被删、与本条冲突、
      // 源取不到——那一种它是**抛**）压在同一个布尔上，界面上已经只有两句话可说。
      // 缺键再折成 false 就是给那两句话之外又造出第三种假话。
      throw EngineFailure(EngineFailureKind.transport,
          'switch.apply 的回话里没有布尔 applied（收到 ${data.keys.join(',')}）',
          action: 'switch.apply');
    }
    return SwitchApplyResult.fromJson(data);
  }

  // ------------------------------------------------------------------ #
  // rss.*
  // ------------------------------------------------------------------ #
  // 这一族三条形状规矩，两条与全表不同：
  // 1. **`rss.article` 的 `null` 是合法回话**（`article_detail` 没这篇文章就回 None），
  //    所以它不走 `_asMap`——与 `source.get`、`reader.position_get` 同一族。
  // 2. **`rss.refresh` 的新增篇数走 `_asInt`，而失败根本到不了这里**：抓取失败服务端
  //    回的是错误信封（`ok:false`），客户端在 `client.call` 那一层就抛。于是 `0`
  //    说的真是"抓开了、没有新文章"，不许把失败折成 0（那是 `zero-counts` 那一族）。
  // 3. `rss.add_source` 回的是**字符串**（源 id = feed 地址），不是对象也不是布尔。
  @override
  Future<List<RssSourceRow>> listRssSources() async =>
      _rows(await client.call('rss.sources'), 'rss.sources')
          .map(RssSourceRow.fromJson)
          .toList(growable: false);

  @override
  Future<List<RssArticleRow>> listRssArticles({
    required String sourceId,
    bool unreadOnly = false,
  }) async =>
      _rows(await client.call('rss.articles', payload: <String, Object?>{
        'source_id': sourceId,
        'unread_only': unreadOnly,
      }), 'rss.articles')
          .map(RssArticleRow.fromJson)
          .toList(growable: false);

  @override
  Future<RssArticleDetail?> openRssArticle(int articleId) async {
    final data = await client.call('rss.article',
        payload: <String, Object?>{'article_id': articleId});
    if (data == null) return null;
    return RssArticleDetail.fromJson(_asMap(data, 'rss.article'));
  }

  @override
  Future<String> addRssSource({
    required String name,
    required String url,
    String sourceGroup = '',
  }) async {
    final data = await client.call('rss.add_source', payload: <String, Object?>{
      'name': name,
      'url': url,
      'source_group': sourceGroup,
    });
    if (data is! String) {
      throw EngineFailure(EngineFailureKind.transport,
          'rss.add_source 的回话不是字符串（收到 ${data.runtimeType}）：'
              '那一格回的是源 id（就是规范化后的 feed 地址），删除与刷新都吃它，'
              '读不到就只能自己按 url 猜一遍',
          action: 'rss.add_source');
    }
    return data;
  }

  @override
  Future<bool> removeRssSource(String sourceId) async => _asBool(
      await client.call('rss.remove_source',
          payload: <String, Object?>{'source_id': sourceId}),
      'rss.remove_source');

  @override
  Future<int> refreshRssSource(String sourceId) async => _asInt(
      await client.call('rss.refresh',
          payload: <String, Object?>{'source_id': sourceId}),
      'rss.refresh');

  @override
  Future<JobAccepted> refreshAllRssSources({required int refreshId}) async =>
      JobAccepted.fromJson(_asMap(await client.call('rss.refresh_all',
          payload: <String, Object?>{'refresh_id': refreshId}),
          'rss.refresh_all'));

  @override
  Future<bool> markRssArticleRead(int articleId) async => _asBool(
      await client.call('rss.mark_read',
          payload: <String, Object?>{'article_id': articleId}),
      'rss.mark_read');

  @override
  Future<bool> starRssArticle({
    required int articleId,
    required bool starred,
  }) async =>
      _asBool(await client.call('rss.star', payload: <String, Object?>{
        'article_id': articleId,
        'starred': starred,
      }), 'rss.star');

  // ------------------------------------------------------------------ #
  // reader.*
  // ------------------------------------------------------------------ #
  // 这一族的两条形状规矩：
  // 1. **没记录与空记录是两件事**——`reader.position_get` 与 `reader.staged` 的
  //    `null` 都是合法回话（"这本书从没读过"/"这本书不是试读，别问"），所以它们
  //    不走 `_asMap`；与 `source.get` 同一族。
  // 2. **发出去的坐标不算数**——`reader.position_save` 回的是服务端夹回、认章之后
  //    的那一份，界面拿回话更新自己，而不是拿请求那份直接落。
  @override
  Future<ReaderSession> openReader({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelve = true,
    bool resume = true,
  }) async =>
      ReaderSession.fromJson(_asMap(await client.call('reader.open',
          payload: <String, Object?>{
            'source_url': sourceUrl,
            'book': book,
            'shelve': shelve,
            'resume': resume,
          }), 'reader.open'));

  @override
  Future<CloseOutcome> closeReader({
    required String sourceUrl,
    required String bookUrl,
    bool saveProgress = true,
  }) async =>
      CloseOutcome.fromJson(_asMap(await client.call('reader.close', payload: <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
        'save_progress': saveProgress,
      }), 'reader.close'));

  @override
  Future<List<ChapterRow>> chapterRows({
    required String sourceUrl,
    required String bookUrl,
    bool refresh = false,
  }) async =>
      _rows(await client.call('reader.toc', payload: <String, Object?>{
        'source_url': sourceUrl,
        'book_url': bookUrl,
        'refresh': refresh,
      }), 'reader.toc')
          .map(ChapterRow.fromJson)
          .toList(growable: false);

  @override
  Future<ChapterDocument> chapterDocument({
    required String sourceUrl,
    required String bookUrl,
    required String chapterUrl,
  }) async =>
      ChapterDocument.fromJson(_asMap(await client.call('reader.content',
          payload: <String, Object?>{
            'source_url': sourceUrl,
            'book_url': bookUrl,
            'chapter_url': chapterUrl,
          }), 'reader.content'));

  @override
  Future<ReadingPosition?> readPosition({
    required String sourceUrl,
    required String bookUrl,
  }) async {
    final data = await client.call('reader.position_get',
        payload: <String, Object?>{'source_url': sourceUrl, 'book_url': bookUrl});
    if (data == null) return null;
    return ReadingPosition.fromJson(_asMap(data, 'reader.position_get'));
  }

  @override
  Future<ReadingPosition> savePosition({
    required String sourceUrl,
    required String bookUrl,
    required Map<String, Object?> positionJson,
  }) async =>
      ReadingPosition.fromJson(_asMap(await client.call('reader.position_save',
          payload: <String, Object?>{
            'source_url': sourceUrl,
            'book_url': bookUrl,
            'position_json': positionJson,
          }), 'reader.position_save'));

  @override
  Future<StagedRecord?> stagedRecord() async {
    final data = await client.call('reader.staged');
    if (data == null) return null;
    return StagedRecord.fromJson(_asMap(data, 'reader.staged'));
  }

  @override
  Future<bool> promoteStaged() async =>
      _asBool(await client.call('reader.staged_promote'), 'reader.staged_promote');

  @override
  Future<bool> discardStaged() async =>
      _asBool(await client.call('reader.staged_discard'), 'reader.staged_discard');

  @override
  Future<List<BookmarkRow>> listBookmarks(String bookUrl) async =>
      _rows(await client.call('reader.bookmarks',
          payload: <String, Object?>{'book_url': bookUrl}), 'reader.bookmarks')
          .map(BookmarkRow.fromJson)
          .toList(growable: false);

  @override
  Future<int> addBookmark({
    required String bookUrl,
    required int chapterIndex,
    double offset = 0,
    String summary = '',
  }) async =>
      // offset/summary **不许省略**：服务端那一发的签名没有默认值
      // （`bookmark_service.py:45`），少传一个键是 TypeError、不是"用默认值"。
      _asInt(await client.call('reader.bookmark_add', payload: <String, Object?>{
        'book_url': bookUrl,
        'chapter_index': chapterIndex,
        'offset': offset,
        'summary': summary,
      }), 'reader.bookmark_add');

  @override
  Future<bool> removeBookmark({
    required String bookUrl,
    required int bookmarkId,
  }) async =>
      _asBool(await client.call('reader.bookmark_remove', payload: <String, Object?>{
        'book_url': bookUrl,
        'bookmark_id': bookmarkId,
      }), 'reader.bookmark_remove');

  @override
  Future<ContentSearchResult> searchBookContent({
    required String bookUrl,
    required String keyword,
    String? sourceUrl,
  }) async =>
      ContentSearchResult.fromJson(_asMap(
          await client.call('reader.search_content', payload: <String, Object?>{
            'book_url': bookUrl,
            'keyword': keyword,
            // 不给 = 服务端那一发的默认值（用当前会话那条源建索引），传空串与省键同值。
            'source_url': sourceUrl ?? '',
          }),
          'reader.search_content'));

  // ------------------------------------------------------------------ #
  // settings.* / theme.*
  // ------------------------------------------------------------------ #
  @override
  Future<SettingsMap> readSettings(List<String> keys) async => SettingsMap
      .fromJson(await client.call('settings.read',
          payload: <String, Object?>{'keys': keys}));

  @override
  Future<void> writeSetting(String key, Object? value) async {
    // `value` 可以是 null：`settings.write` 的 schema 里 value 就是允许 null 的
    // （"把这个键设回没有值"是一件真实的事，不是缺参数）。
    await client.call('settings.write',
        payload: <String, Object?>{'key': key, 'value': value});
  }

  @override
  Future<ThemePreferences> themePreferences() async => ThemePreferences.fromJson(
      _asMap(await client.call('theme.preferences'), 'theme.preferences'));

  @override
  Future<ThemePreferences> saveTheme({
    String? skinId,
    String? mode,
    bool? eink,
  }) async =>
      ThemePreferences.fromJson(_asMap(
          await client.call('theme.save', payload: <String, Object?>{
            if (skinId != null) 'skin_id': skinId,
            if (mode != null) 'mode': mode,
            if (eink != null) 'eink': eink,
          }),
          'theme.save'));

  // ------------------------------------------------------------------ #
  // 形状检查
  // ------------------------------------------------------------------ #
  List<Map<String, Object?>> _rows(RpcData data, String action) {
    if (data is! List) {
      throw EngineFailure(EngineFailureKind.transport,
          '$action 的回话不是数组（收到 ${data.runtimeType}）',
          action: action);
    }
    return data
        .map((row) => row is Map
            ? Map<String, Object?>.from(row)
            : throw EngineFailure(EngineFailureKind.transport,
                '$action 的某一页不是对象（收到 ${row.runtimeType}）',
                action: action))
        .toList(growable: false);
  }

  Map<String, Object?> _asMap(RpcData data, String action) {
    if (data is! Map) {
      throw EngineFailure(EngineFailureKind.transport,
          '$action 的回话不是对象（收到 ${data.runtimeType}）',
          action: action);
    }
    return Map<String, Object?>.from(data);
  }

  bool _asBool(RpcData data, String action) {
    if (data is! bool) {
      throw EngineFailure(EngineFailureKind.transport,
          '$action 的回话不是布尔（收到 ${data.runtimeType}）：'
          '这一发回的是"改动生效没有"，缺了它界面就不能报"已删除/已切换"',
          action: action);
    }
    return data;
  }

  int _asInt(RpcData data, String action) {
    if (data is! num) {
      throw EngineFailure(EngineFailureKind.transport,
          '$action 的回话不是数字（收到 ${data.runtimeType}）：'
          '那一条的 summary 明写"回改动的条数，不是是否成功"，所以它不可能是布尔',
          action: action);
    }
    return data.toInt();
  }
}
