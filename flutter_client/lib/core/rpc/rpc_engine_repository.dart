/// [EngineRepository] 的 RPC 实现：把契约面上那 29 发落到 §4.1 那一个动词上。
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
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
import '../../features/settings/data/theme_preferences.dart';
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
