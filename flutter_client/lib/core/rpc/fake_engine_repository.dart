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
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
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
  SaveOutcome saveOutcome;
  ImportOutcome importOutcome;
  GatewayInfo info;

  /// 非空 = 这台"没有这个能力"：每个方法都抛它。
  /// 可写：「刷新失败要留着旧数据」那条规矩要在同一发测试里先成功后失败。
  EngineFailure? failure;

  /// 让加载态可测（`pumpAndSettle` 之前数据还没回）。
  final Duration delay;

  final Map<String, int> _calls = {};
  final Map<String, Map<String, Object?>?> _payloads = {};

  final StreamController<EngineEvent> _events =
      StreamController<EngineEvent>.broadcast();

  /// 这一发打过几次（失败的那几发也算）。
  int callsFor(String action) => _calls[action] ?? 0;

  /// 最近一次打过来的 payload（没带参数的动作是 null，与"带了空对象"是两件事）。
  Map<String, Object?>? payloadFor(String action) => _payloads[action];

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
