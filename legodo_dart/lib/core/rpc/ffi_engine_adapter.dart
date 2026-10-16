/// FFI 引擎适配器（实现 EngineRepository 接口）
///
/// 通过 dart:ffi 直接调用 Go 动态库。统一入口：
///   Go.Call("action_name", '{"key":"value"}') → {"ok":true,"data":<raw_result>}
///
/// _syncCall(action, params, parserFn) 已提取 result['data'] 传入 parserFn。
/// [parserFn] 接收的是裸数据（数组/字典/标量），不是再包一层信封。
library;

import 'dart:async';
import 'dart:io' as io;

import '../ffi/engine_ffi.dart';
import '../rpc/engine_repository.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';
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
import '../../features/bookshelf/data/source_switch_models.dart';
import '../../core/events/engine_event.dart';
import '../../core/events/event_names.dart';
import '../../core/settings/settings_map.dart';
import '../../core/rpc/engine_status.dart';
import '../../core/rpc/envelope.dart';

// ---------------------------------------------------------------------------
// Helpers — operate on raw Map<String, dynamic> received from Go
// ---------------------------------------------------------------------------

String _s(Map<Object?, Object?>? m, String k, {String def = ''}) {
  if (m == null) return def;
  final v = m[k];
  return v == null ? def : '$v';
}

bool _b(Map<Object?, Object?>? m, String k, {bool def = false}) {
  if (m == null) return def;
  final v = m[k];
  if (v is bool) return v;
  if (v is int) return v != 0;
  return def;
}

int _n(Map<Object?, Object?>? m, String k, {int def = 0}) {
  if (m == null) return def;
  final v = m[k];
  return v is num ? v.toInt() : def;
}

/// Cast an untyped List<Object?> to List<Map<Object?, Object?>> filtering non-maps.
List<Map<Object?, Object?>> _toMaps(List<Object?>? items) {
  if (items == null) return [];
  return items.whereType<Map<Object?, Object?>>().toList();
}

Map<String, Object?> _toSourceRaw(dynamic obj) {
  if (obj is Map<String, Object?>) return obj;
  if (obj is Map) return obj.cast<String, Object?>();
  return <String, Object?>{};

}

// ---------------------------------------------------------------------------
// engine_poll 帧解码（纯函数，可离屏单测）
// ---------------------------------------------------------------------------

/// 一批轮询事件：事件列表 + 服务端推进游标 + 是否有丢帧。
class PollBatch {
  const PollBatch(this.events, this.lastSeq, this.lost);
  final List<EngineEvent> events;
  final int lastSeq;
  final bool lost;
}

/// `engine_poll` 的 data → [PollBatch]。
///
/// 帧形状：`{"events":[{"seq","name","payload"}], "last_seq": N, "lost": bool}`。
/// `name` 已是契约类名（`SearchFinishedEvent` 这一族），原样透传不做映射。
PollBatch framesToEvents(dynamic data) {
  if (data is! Map) return const PollBatch(<EngineEvent>[], 0, false);
  final events = <EngineEvent>[];
  final rawEvents = data['events'];
  if (rawEvents is List) {
    for (final raw in rawEvents) {
      if (raw is! Map) continue;
      final name = raw['name'];
      if (name is! String || name.isEmpty) continue;
      final payload = raw['payload'];
      events.add(EngineEvent(name,
          payload is Map ? Map<String, Object?>.from(payload) : <String, Object?>{}));
    }
  }
  final lastSeq = data['last_seq'];
  return PollBatch(
      events, lastSeq is num ? lastSeq.toInt() : 0, data['lost'] == true);
}

/// 一批轮询里允许合流的事件名（§4-M1 的白名单，一期就这一条）。
///
/// 能不能进来的判据只有一条：**界面读不读它的 payload**。
/// `SourceChangedEvent` 那两个监听者（书源屏、找书页）都只把它当"库里变了，重取一次"，
/// 一个字都不看——所以 60 条和 1 条对它们等价。
/// 进度类一律不许进来：`CheckFinishedEvent`/`SourceSwitchProgressEvent`/
/// `RssRefreshedEvent`/`SearchFinishedEvent` 那些界面画的就是"跑到第 37 源"这一条条，
/// 合流会把逐条变成跳号。
const Set<String> coalescableEventNames = {EngineEventNames.sourceChanged};

/// 白名单里同名的多条收成一条：留**最后那条**（库里最新的形状），位置按第一次出现处
/// （不把后面的进度帧挤到前面）。白名单之外的一根不动、顺序不变。
List<EngineEvent> coalesceBatch(List<EngineEvent> events) {
  final firstAt = <String, int>{};
  final merged = <EngineEvent>[];
  for (final event in events) {
    if (!coalescableEventNames.contains(event.name)) {
      merged.add(event);
      continue;
    }
    final at = firstAt[event.name];
    if (at == null) {
      firstAt[event.name] = merged.length;
      merged.add(event);
    } else {
      merged[at] = event;
    }
  }
  return merged;
}

// ---------------------------------------------------------------------------
// FFIEngineAdapter adapts GoFFIClient to the EngineRepository contract.
// ---------------------------------------------------------------------------

class FFIEngineAdapter implements EngineRepository {
  /// [client] 只为离屏测试注入替身；生产走 [GoFFIClient.instance] 单例。
  FFIEngineAdapter({GoFFIClient? client}) : _injected = client;

  final GoFFIClient? _injected;
  GoFFIClient get ffi => _injected ?? GoFFIClient.instance;
  bool _connected = false;

  Future<void> connect() async {
    try {
      if (!ffi.loaded) {
        _writeLog('FFI Adapter: Loading FFI library...');
        ffi.load();
        _writeLog('FFI Adapter: FFI library loaded');
      }
      _writeLog('FFI Adapter: DLL path=${GoFFIClient.instance.libraryPath}');
      _writeLog('FFI Adapter: Calling initialize()...');
      ffi.initialize();
      _writeLog('FFI Adapter: initialize() completed');
      // 引擎侧的事件环随 Initialize 从 seq 0 重开，游标必须跟着归零。
      _eventCursor = 0;
      _lastPollError = null;
      _connected = true;
      _writeLog('FFI Adapter: Connected successfully');
    } catch (e) {
      _writeLog('FFI Adapter: Connect failed: $e');
      throw EngineFailure(
        EngineFailureKind.transport,
        'Failed to load Go FFI library: $e',
        action: 'connect',
      );
    }
  }

  Future<void> disconnect() async {
    ffi.cleanup();
    _connected = false;
  }

  bool get isConnected => _connected;

  void _writeLog(String message) {
    try {
      io.stderr.writeln('[LEGODO_FFI] $message');
      final logDir = io.File(io.Platform.resolvedExecutable).parent.path;
      final logFile = io.File('$logDir\\legodo_ffi.log');
      logFile.writeAsStringSync('[${DateTime.now().toIso8601String()}] $message\n', mode: io.FileMode.append);
    } catch (e) {
      io.stderr.writeln('[LEGODO_FFI] Log write failed: $e');
    }
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  Map<String, dynamic> _callGo(String action, Map<String, dynamic> params) {
    final result = ffi.call(action, params);
    if (result['ok'] != true) {
      throw EngineFailure(
        EngineFailureKind.engine,
        (result['error'] ?? 'unknown error') as String,
        action: action,
      );
    }
    return result;
  }

  /// Wraps [_callGo]. [parserFn] receives raw data (already unwrapped from envelope).
  Future<T> _syncCall<T>(
    String action,
    Map<String, dynamic> params,
    T Function(dynamic data) parserFn,
  ) async {
    final result = _callGo(action, params);
    return parserFn(result['data']);
  }

  // ---------------------------------------------------------------------------
  // engine.*
  // ---------------------------------------------------------------------------

  @override
  Future<GatewayInfo> hello() async => const GatewayInfo(
    protocolVersion: 0, build: 'go-ffi', capabilities: [],
    schemaDigest: '', actions: [], pid: 0,
  );

  @override
  Future<List<ServiceStatus>> serviceStatus() async {
    return await _syncCall<List<ServiceStatus>>('engine_status', {}, (data) {
      if (data is! List) return [];
      return data.map((item) {
        if (item is! Map<Object?, Object?>) {
          return ServiceStatus(serviceId: '', state: 'unknown', lastError: null, startedAt: 0, updatedAt: 0, progress: null);
        }
        return ServiceStatus(
          serviceId: _s(item, 'id', def: 'unknown'), state: _s(item, 'state', def: 'unknown'),
          lastError: null, startedAt: 0.0,
          updatedAt: DateTime.now().millisecondsSinceEpoch / 1000, progress: null,
        );
      }).toList();
    });
  }

  @override
  Future<void> shutdown({String reason = 'flutter client'}) async =>
      await _syncCall<void>('engine_shutdown', {'reason': reason}, (_) {});

  @override
  Stream<EngineEvent> events() {
    // FFI 没有推送出口：以游标轮询 `engine_poll`。游标跨流重建保留（_eventCursor
    // 是实例字段），否则每次切页回来都会把环里整段历史重放一遍。
    // 出错时不丢流、继续轮询（下次成功就自愈）；错误按"变了才报"节流，
    // 否则 250ms 一条会同形状地刷爆 AsyncValue。
    // 合流的窗口就是这里的"一批"：一批 = 一个轮询周期，白名单里同名的多条在出口
    // 收成一条（[coalesceBatch]），所以不另起计时器、也不在各屏各写一份 debounce。
    late StreamController<EngineEvent> controller;
    controller = StreamController<EngineEvent>(
      onListen: () {
        _pollTimer?.cancel();
        _pollTimer = Timer.periodic(_pollInterval, (_) {
          if (controller.isClosed) return;
          try {
            final result = ffi.call('engine_poll', {'since': _eventCursor});
            if (result['ok'] != true) {
              _reportPollError(controller,
                  '${result['error'] ?? 'engine_poll failed'}');
              return;
            }
            _lastPollError = null;
            final batch = framesToEvents(result['data']);
            // 游标跟着**服务端帧**走，不跟合流后的条数走：合流只少发几条给界面，
            // 一条都不许漏记（漏记 = 下一批从头重放）。
            _eventCursor = batch.lastSeq;
            // 一批就是一个轮询周期，所以合流的窗口天然对齐批界（§4-M1，不另起计时器）。
            for (final event in coalesceBatch(batch.events)) {
              controller.add(event);
            }
          } on Object catch (error) {
            _reportPollError(controller, '$error');
          }
        });
      },
      onCancel: () {
        _pollTimer?.cancel();
        _pollTimer = null;
      },
    );
    return controller.stream;
  }

  static const Duration _pollInterval = Duration(milliseconds: 250);
  Timer? _pollTimer;
  int _eventCursor = 0;
  String? _lastPollError;

  void _reportPollError(StreamController<EngineEvent> controller, String message) {
    if (message == _lastPollError) return;
    _lastPollError = message;
    controller.addError(EngineFailure(EngineFailureKind.engine,
        '事件通道中断（engine_poll）：$message', action: 'engine_poll'));
  }

  // ---------------------------------------------------------------------------
  // update.*
  // ---------------------------------------------------------------------------

  @override
  Future<UpdateInfo?> updateCheck() async {
    return await _syncCall<UpdateInfo?>('update_check', {}, (data) {
      if (data is! Map<Object?, Object?>) return null;
      return UpdateInfo(version: _s(data, 'version'), notes: _s(data, 'notes'), url: _s(data, 'url'),
          size: _n(data, 'size'), digest: _s(data, 'digest'));
    });
  }

  @override
  Future<void> updateIgnore(String version) async =>
      await _syncCall<void>('update_ignore', {'version': version}, (_) {});

  @override
  Future<WebviewDelivery> webContentReady({
    required String requestId, String body = '', Map<String, Object?>? headers,
    int status = 200, String url = '', String cookie = '', String error = '', Object? jsResult,
  }) async {
    return await _syncCall<WebviewDelivery>('engine_web_content_ready', {
      'request_id': requestId, 'body': body, 'headers': headers,
      'status': status, 'url': url, 'cookie': cookie, 'error': error, 'js_result': jsResult,
    }, (data) {
      if (data is! Map<Object?, Object?>) return const WebviewDelivery(delivered: false);
      return WebviewDelivery(delivered: _b(data, 'delivered'));
    });
  }

  // ---------------------------------------------------------------------------
  // bookshelf.*
  // ---------------------------------------------------------------------------

  @override
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) async {
    return await _syncCall<List<BookshelfItem>>('bookshelf_list', {
      'include_staged': includeStaged,
    }, (data) {
      if (data is! List) return [];
      return _toMaps(data)
          .map((it) => BookshelfItem.fromJson(it.cast<String, Object?>()))
          .toList();
    });
  }

  @override
  Future<bool> shelveBook({required String sourceUrl, required Map<String, Object?> book, bool shelved = true}) async {
    return await _syncCall<bool>('bookshelf_add', {
      'source_url': sourceUrl, 'book': book, 'shelved': shelved,
    }, (data) {
      if (data is Map<Object?, Object?>) return _b(data, 'success', def: shelved);
      return shelved;
    });
  }

  // ---------------------------------------------------------------------------
  // source.*
  // ---------------------------------------------------------------------------

  @override
  Future<List<SourceRecord>> listSources() async {
    return await _syncCall<List<SourceRecord>>('source_list', {}, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) {
        return SourceRecord(url: _s(it, 'bookSourceUrl'), name: _s(it, 'bookSourceName'),
            group: _s(it, 'bookSourceGroup'), comment: _s(it, 'comment'),
            enabled: _b(it, 'enabled'), loginUrl: _s(it, 'loginUrl'), raw: _toSourceRaw(it));
      }).toList();
    });
  }

  @override
  Future<SourceRecord?> loadSource(String url) async {
    return await _syncCall<SourceRecord?>('source_get', {'url': url}, (data) {
      if (data is! Map<Object?, Object?>) return null;
      Map<Object?, Object?> src;
      if (data.containsKey('bookSourceUrl')) {
        src = data;
      } else {
        final inner = data['source'];
        if (inner is! Map<Object?, Object?>) return null;
        src = inner;
      }
      return SourceRecord(url: _s(src, 'bookSourceUrl'), name: _s(src, 'bookSourceName'),
          group: _s(src, 'bookSourceGroup'), comment: _s(src, 'comment'),
          enabled: _b(src, 'enabled'), loginUrl: _s(src, 'loginUrl'), raw: _toSourceRaw(src));
    });
  }

  @override
  Future<SaveOutcome> saveSource(Map<String, Object?> sourceJson) async {
    return await _syncCall<SaveOutcome>('source_save', {'source': sourceJson}, (_) {
      return SaveOutcome(saved: true,
          sourceId: _s(sourceJson, 'bookSourceUrl', def: _s(sourceJson, 'url')), errors: [],
          audit: AuditReport(sourceId: '', missingJavaApis: [], missingObjectApis: [],
              unknownFields: [], unsupportedPseudos: [], jsSyntaxErrors: [], regexErrors: [],
              ruleSyntaxErrors: [], suggestions: [], notAudited: ''));
    });
  }

  @override
  Future<bool> deleteSource(String url) async {
    return await _syncCall<bool>('source_delete', {'url': url}, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'ok');
      return true;
    });
  }

  @override
  Future<bool> setSourceEnabled(String url, bool enabled) async {
    return await _syncCall<bool>('source_enable', {'url': url, 'enabled': enabled}, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'enabled', def: enabled);
      return enabled;
    });
  }

  @override
  Future<bool> setSourceGroup(String url, String group) async {
    return await _syncCall<bool>('source_group', {'url': url, 'group': group},
        (data) => data is bool ? data : true);
  }

  @override
  Future<int> batchSetEnabled(List<String> urls, bool enabled) async {
    return await _syncCall<int>('source_batch_enable', {'urls': urls, 'enabled': enabled}, (data) {
      if (data is int) return data;
      if (data is Map<Object?, Object?>) return _n(data, 'count', def: urls.length);
      return urls.length;
    });
  }

  @override
  Future<int> batchDelete(List<String> urls) async {
    return await _syncCall<int>('source_batch_delete', {'urls': urls}, (data) {
      if (data is int) return data;
      if (data is Map<Object?, Object?>) return _n(data, 'count', def: urls.length);
      return urls.length;
    });
  }

  @override
  Future<String> exportSources() async {
    return await _syncCall<String>('source_export', {}, (data) {
      if (data is String) return data;
      return '';
    });
  }

  @override
  Future<AuditReport> auditSource(Map<String, Object?> sourceJson) async {
    return await _syncCall<AuditReport>('source_audit', {'source': sourceJson}, (data) {
      if (data is! Map<Object?, Object?>) return AuditReport(
          sourceId: '', missingJavaApis: [], missingObjectApis: [], unknownFields: [],
          unsupportedPseudos: [], jsSyntaxErrors: [], regexErrors: [], ruleSyntaxErrors: [],
          suggestions: [], notAudited: '');
      return AuditReport(
        sourceId: _s(data, 'sourceId'),
        missingJavaApis: _toStrings(data['missingJavaApis']),
        missingObjectApis: _toStrings(data['missingObjectApis']),
        unknownFields: _toStrings(data['unknownFields']),
        unsupportedPseudos: _toStrings(data['unsupportedPseudos']),
        jsSyntaxErrors: _toStrings(data['jsSyntaxErrors']),
        regexErrors: _toStrings(data['regexErrors']),
        ruleSyntaxErrors: _toStrings(data['ruleSyntaxErrors']),
        suggestions: _toStrings(data['suggestions']),
        notAudited: _s(data, 'notAudited'),
      );
    });
  }

  // ---------------------------------------------------------------------------
  // import.*
  // ---------------------------------------------------------------------------

  @override
  Future<ImportOutcome> importText({required String text, required String kind, String mode = 'append'}) async {
    return await _syncCall<ImportOutcome>('import_text', {'text': text, 'kind': kind, 'mode': mode}, (data) {
      // 回的不是那五格 = 服务侧没按形状回；这里绝不能落成"成功 0 条"，
      // 那句话和用户自己数错了一次空文件完全同形。
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine, 'invalid response', action: 'import_text');
      return ImportOutcome(kind: _s(data, 'kind', def: kind), count: _n(data, 'count'),
          errors: _toStrings(data['errors']), warnings: _toStrings(data['warnings']),
          placeholder: _b(data, 'placeholder'));
    });
  }

  @override
  Future<ImportOutcome> importUrl({required String url, required String kind, String mode = 'append'}) async {
    return await _syncCall<ImportOutcome>('import_url', {'url': url, 'kind': kind, 'mode': mode}, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine, 'invalid response', action: 'import_url');
      return ImportOutcome(kind: _s(data, 'kind', def: kind), count: _n(data, 'count'),
          errors: _toStrings(data['errors']), warnings: _toStrings(data['warnings']),
          placeholder: _b(data, 'placeholder'));
    });
  }

  // ---------------------------------------------------------------------------
  // browse.* / search.*
  // ---------------------------------------------------------------------------

  @override
  Future<List<CategoryRow>> browseCategories(String sourceUrl) async {
    return await _syncCall<List<CategoryRow>>('browse_categories', {'source_url': sourceUrl}, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) => CategoryRow(title: _s(it, 'title'), url: _s(it, 'url'),
          raw: it.cast<String, Object?>())).toList();
    });
  }

  @override
  Future<List<BookRow>> browseBooks({required String sourceUrl, required Map<String, Object?> category, int page = 1}) async {
    return await _syncCall<List<BookRow>>('browse_books', {
      'source_url': sourceUrl, 'category': category, 'page': page,
    }, (data) {
      if (data is! List) return [];
      // 与 562 行那批书单**同一个入口**：服务端回的是 snake_case（bookToMapFull），
      // 这里原先自拼一套 camelCase 键名，originName/coverUrl 恒为空。
      return _toMaps(data).map((it) => BookRow.fromJson(_toSourceRaw(it))).toList();
    });
  }

  @override
  Future<JobAccepted> searchStart({required String keyword, String scope = '', int? searchId}) async {
    return await _syncCall<JobAccepted>('search_start', {
      'keyword': keyword, 'scope': scope, 'search_id': searchId,
    }, (data) {
      if (data is! Map<Object?, Object?>) return const JobAccepted(started: false, action: 'search.start');
      return JobAccepted(started: _b(data, 'started'), action: _s(data, 'action', def: 'search.start'));
    });
  }

  @override
  Future<void> searchStop() async => await _syncCall<void>('search_stop', {}, (_) {});

  @override
  Future<List<SearchScope>> searchScopes() async {
    return await _syncCall<List<SearchScope>>('search_scopes', {}, (data) {
      if (data is! List) return [];
      // 服务端发 [{scope, count}]（与 Python scope_table 同形），解析走模型自带的 fromJson。
      return _toMaps(data)
          .map((it) => SearchScope.fromJson(
              it.map((key, value) => MapEntry(key.toString(), value))))
          .toList();
    });
  }

  // ---------------------------------------------------------------------------
  // check.*
  // ---------------------------------------------------------------------------

  @override
  Future<JobAccepted> checkStart({List<String>? groups}) async {
    return await _syncCall<JobAccepted>('check_start', {'groups': groups}, (data) {
      if (data is! Map<Object?, Object?>) return const JobAccepted(started: false, action: 'check.start');
      return JobAccepted(started: _b(data, 'started'), action: _s(data, 'action', def: 'check.start'));
    });
  }

  @override
  Future<CheckSnapshot> checkSnapshot() async {
    return await _syncCall<CheckSnapshot>('check_snapshot', {}, (data) {
      // 键口径由 CheckSnapshot.fromJson 一份定（snake_case，golden check.snapshot）；
      // 非 Map 落进 fromJson 的空 map 分支 = 空闲形状，不另造第二份默认值。
      return CheckSnapshot.fromJson(_toSourceRaw(data));
    });
  }

  @override
  Future<void> checkCancel() async => await _syncCall<void>('check_cancel', {}, (_) {});

  @override
  Future<CheckSourceReport> checkSource({required Map<String, Object?> sourceJson, String depth = 'list'}) async {
    return await _syncCall<CheckSourceReport>('check_source', {'source_json': sourceJson, 'depth': depth}, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine, 'invalid response', action: 'check_source');
      return CheckSourceReport.fromJson(_toSourceRaw(data));
    });
  }

  // ---------------------------------------------------------------------------
  // switch.*
  // ---------------------------------------------------------------------------

  @override
  Future<List<BookRow>> switchSeeds({required Map<String, Object?> book}) async {
    return await _syncCall<List<BookRow>>('switch_seeds', {'book': book}, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) => BookRow.fromJson(_toSourceRaw(it))).toList();
    });
  }

  @override
  Future<JobAccepted> switchScan({required Map<String, Object?> book, required int switchId, String keyword = '', String group = ''}) async {
    return await _syncCall<JobAccepted>('switch_scan', {
      'book': book, 'switch_id': switchId, 'keyword': keyword, 'group': group,
    }, (data) {
      if (data is! Map<Object?, Object?>) return const JobAccepted(started: false, action: 'switch.scan');
      return JobAccepted(started: _b(data, 'started'), action: _s(data, 'action', def: 'switch.scan'));
    });
  }

  @override
  Future<SwitchRun> switchSnapshot() async {
    return await _syncCall<SwitchRun>('switch_snapshot', {}, (data) {
      // 非 Map 回 idle 形状；键口径由 SwitchRun.fromJson 一份定（snake_case）。
      return SwitchRun.fromJson(_toSourceRaw(data));
    });
  }

  @override
  Future<bool> switchCancel() async {
    return await _syncCall<bool>('switch_cancel', {}, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'cancelled');
      return false;
    });
  }

  @override
  Future<SwitchApplyResult> applySwitch({required Map<String, Object?> record, required String sourceUrl, required Map<String, Object?> book}) async {
    return await _syncCall<SwitchApplyResult>('switch_apply', {
      'record': record, 'source_url': sourceUrl, 'book': book,
    }, (data) {
      if (data is! Map<Object?, Object?>) return const SwitchApplyResult(applied: false, sourceUrl: '');
      return SwitchApplyResult.fromJson(_toSourceRaw(data));
    });
  }

  // ---------------------------------------------------------------------------
  // rss.*（键名走 snake_case 权威模型，golden rss.sources.json / rss.articles.json
  // 的 field_names 即那 10/9 格；这一族不再手拼 camelCase）
  // ---------------------------------------------------------------------------

  @override
  Future<List<RssSourceRow>> listRssSources() async {
    return await _syncCall<List<RssSourceRow>>('rss_sources', {}, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) => RssSourceRow.fromJson(_toSourceRaw(it))).toList();
    });
  }

  @override
  Future<List<RssArticleRow>> listRssArticles({required String sourceId, bool unreadOnly = false}) async {
    return await _syncCall<List<RssArticleRow>>('rss_articles', {
      'source_id': sourceId, 'unread_only': unreadOnly,
    }, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) => RssArticleRow.fromJson(_toSourceRaw(it))).toList();
    });
  }

  @override
  Future<RssArticleDetail?> openRssArticle(int articleId) async {
    return await _syncCall<RssArticleDetail?>('rss_article', {'article_id': articleId}, (data) {
      // data=null = 没这篇文章（与"正文是空串"是两件事，模型把这一格分开读）。
      if (data is! Map) return null;
      return RssArticleDetail.fromJson(_toSourceRaw(data));
    });
  }

  @override
  Future<String> addRssSource({required String name, required String url, String sourceGroup = ''}) async {
    return await _syncCall<String>('rss_add_source', {'name': name, 'url': url, 'source_group': sourceGroup}, (data) {
      return data is String ? data : url;
    });
  }

  @override
  Future<bool> removeRssSource(String sourceId) async {
    return await _syncCall<bool>('rss_remove_source', {'source_id': sourceId}, (data) => data == true);
  }

  @override
  Future<int> refreshRssSource(String sourceId) async {
    return await _syncCall<int>('rss_refresh', {'source_id': sourceId},
        (data) => data is num ? data.toInt() : 0);
  }

  @override
  Future<JobAccepted> refreshAllRssSources({required int refreshId}) async {
    return await _syncCall<JobAccepted>('rss_refresh_all', {'refresh_id': refreshId}, (data) {
      if (data is! Map<Object?, Object?>) return const JobAccepted(started: false, action: 'rss.refresh_all');
      return JobAccepted(started: _b(data, 'started'), action: _s(data, 'action', def: 'rss.refresh_all'));
    });
  }

  @override
  Future<bool> markRssArticleRead(int articleId) async {
    return await _syncCall<bool>('rss_mark_read', {'article_id': articleId}, (data) => data == true);
  }

  @override
  Future<bool> starRssArticle({required int articleId, required bool starred}) async {
    return await _syncCall<bool>('rss_star', {'article_id': articleId, 'starred': starred},
        (data) => data == true);
  }

  // ---------------------------------------------------------------------------
  // reader.*
  // ---------------------------------------------------------------------------

  @override
  Future<ReaderSession> openReader({required String sourceUrl, required Map<String, Object?> book, bool shelve = true, bool resume = true}) async {
    return await _syncCall<ReaderSession>('reader_open', {
      'source_url': sourceUrl, 'book': book, 'shelve': shelve, 'resume': resume,
    }, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine,
          'invalid reader session response', action: 'reader.open');
      return ReaderSession.fromJson(Map<String, Object?>.from(data));
    });
  }

  @override
  Future<CloseOutcome> closeReader({required String sourceUrl, required String bookUrl, bool saveProgress = true}) async {
    return await _syncCall<CloseOutcome>('reader_close', {
      'source_url': sourceUrl, 'book_url': bookUrl, 'save_progress': saveProgress,
    }, (data) {
      if (data is! Map<Object?, Object?>) return const CloseOutcome(closed: false, staged: null);
      return CloseOutcome.fromJson(Map<String, Object?>.from(data));
    });
  }

  @override
  Future<List<ChapterRow>> chapterRows({required String sourceUrl, required String bookUrl, bool refresh = false}) async {
    return await _syncCall<List<ChapterRow>>('reader_toc', {
      'source_url': sourceUrl, 'book_url': bookUrl, 'refresh': refresh,
    }, (data) {
      if (data is! List) return [];
      return _toMaps(data).map((it) => ChapterRow(name: _s(it, 'name'), url: _s(it, 'url'),
          index: _n(it, 'index'), isVolume: _b(it, 'isVolume'), isVip: _b(it, 'isVip'),
          isPay: _b(it, 'isPay'), tag: _s(it, 'tag'), wordCount: _s(it, 'wordCount'))).toList();
    });
  }

  @override
  Future<ChapterDocument> chapterDocument({required String sourceUrl, required String bookUrl, required String chapterUrl}) async {
    return await _syncCall<ChapterDocument>('reader_content', {
      'source_url': sourceUrl, 'book_url': bookUrl, 'chapter_url': chapterUrl,
    }, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine,
          'invalid chapter content response', action: 'reader.content');
      return ChapterDocument(chapterUrl: _s(data, 'chapterUrl'), chapterIndex: _n(data, 'chapterIndex'),
          chapterName: _s(data, 'chapterName'), title: _s(data, 'title'),
          imageStyle: _s(data, 'imageStyle'), blocks: _parseBlocks(data['blocks'] as List?),
          paragraphCount: _n(data, 'paragraphCount'), totalChars: _n(data, 'totalChars'));
    });
  }

  @override
  Future<ReadingPosition?> readPosition({required String sourceUrl, required String bookUrl}) async {
    return await _syncCall<ReadingPosition?>('reader_position_get', {
      'source_url': sourceUrl, 'book_url': bookUrl,
    }, (data) {
      if (data is! Map<Object?, Object?>) return null;
      return ReadingPosition.fromJson(Map<String, Object?>.from(data));
    });
  }

  @override
  Future<ReadingPosition> savePosition({required String sourceUrl, required String bookUrl, required Map<String, Object?> positionJson}) async {
    return await _syncCall<ReadingPosition>('reader_position_save', {
      'source_url': sourceUrl, 'book_url': bookUrl, 'position_json': positionJson,
    }, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine,
          'invalid position response', action: 'reader.position_save');
      return ReadingPosition.fromJson(Map<String, Object?>.from(data));
    });
  }

  @override
  Future<StagedRecord?> stagedRecord() async {
    return await _syncCall<StagedRecord?>('reader_staged', {}, (data) {
      if (data is! Map<Object?, Object?>) return null;
      return StagedRecord.fromJson(Map<String, Object?>.from(data));
    });
  }

  @override
  Future<bool> promoteStaged() async {
    return await _syncCall<bool>('reader_staged_promote', {}, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'promoted');
      return false;
    });
  }

  @override
  Future<bool> discardStaged() async {
    return await _syncCall<bool>('reader_staged_discard', {}, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'discarded');
      return false;
    });
  }

  @override
  Future<List<BookmarkRow>> listBookmarks(String bookUrl) async {
    return await _syncCall<List<BookmarkRow>>('reader_bookmarks', {'book_url': bookUrl}, (data) {
      if (data is! List) return [];
      return _toMaps(data)
          .map((it) => BookmarkRow.fromJson(it.cast<String, Object?>()))
          .toList();
    });
  }

  @override
  Future<int> addBookmark({required String bookUrl, required int chapterIndex, double offset = 0, String summary = ''}) async {
    return await _syncCall<int>('reader_bookmark_add', {
      'book_url': bookUrl, 'chapter_index': chapterIndex, 'offset': offset, 'summary': summary,
    }, (data) {
      if (data is int) return data;
      if (data is Map<Object?, Object?>) return _n(data, 'id');
      return 0;
    });
  }

  @override
  Future<bool> removeBookmark({required String bookUrl, required int bookmarkId}) async {
    return await _syncCall<bool>('reader_bookmark_remove', {
      'book_url': bookUrl, 'bookmark_id': bookmarkId,
    }, (data) {
      if (data is bool) return data;
      if (data is Map<Object?, Object?>) return _b(data, 'removed');
      return false;
    });
  }

  @override
  Future<ContentSearchResult> searchBookContent({required String bookUrl, required String keyword, String? sourceUrl}) async {
    return await _syncCall<ContentSearchResult>('reader_search_content', {
      'book_url': bookUrl, 'keyword': keyword, 'source_url': sourceUrl,
    }, (data) {
      if (data is! Map<Object?, Object?>) throw EngineFailure(EngineFailureKind.engine,
          'invalid search content response', action: 'reader.search_content');
      return ContentSearchResult.fromJson(Map<String, Object?>.from(data));
    });
  }

  // ---------------------------------------------------------------------------
  // settings.* / theme.*
  // ---------------------------------------------------------------------------

  @override
  Future<SettingsMap> readSettings(List<String> keys) async {
    return await _syncCall<SettingsMap>('settings_read', {'keys': keys}, (data) {
      if (data is Map<Object?, Object?>) return SettingsMap(data.cast<String, Object?>());
      return SettingsMap({});
    });
  }

  @override
  Future<void> writeSetting(String key, Object? value) async =>
      await _syncCall<void>('settings_write', {'key': key, 'value': value}, (_) {});

  @override
  Future<ThemePreferences> themePreferences() async {
    return await _syncCall<ThemePreferences>('theme_preferences', {}, (data) {
      // 键名走线上契约那三格（snake_case；黄金样本 field_names = eink/mode/skin_id）。
      // 这里原先读的是 'skinId'，服务端从不回那个键 → 存好的皮肤/eink 开关
      // 每次重开都折回默认档，而保存那一步照样显示"已保存"。
      if (data is! Map<Object?, Object?>) return ThemePreferences.defaults;
      return ThemePreferences.fromJson(data.cast<String, Object?>());
    });
  }

  @override
  Future<ThemePreferences> saveTheme({String? skinId, String? mode, bool? eink}) async {
    return await _syncCall<ThemePreferences>('theme_save', {'skin_id': skinId, 'mode': mode, 'eink': eink}, (data) {
      // 回的是服务端**归一后的三格**（不是 true）：界面上"已保存态"就用它，
      // 省掉"开关写着 dark、库里存的是 light"那种自相矛盾。
      if (data is! Map<Object?, Object?>) return ThemePreferences.defaults;
      return ThemePreferences.fromJson(data.cast<String, Object?>());
    });
  }

  // ---------------------------------------------------------------------------
  // qr.*
  // ---------------------------------------------------------------------------

  @override
  Future<String?> qrGenerate(String text) async {
    return await _syncCall<String?>('qr_generate', {'text': text}, (data) {
      if (data is String) return data;
      if (data is Map<Object?, Object?>) return _s(data, 'result', def: _s(data, 'image'));
      return null;
    });
  }

  @override
  Future<String?> qrDecode(String imageData) async {
    return await _syncCall<String?>('qr_decode', {'image_data': imageData}, (data) {
      if (data is String) return data;
      if (data is Map<Object?, Object?>) return _s(data, 'text', def: _s(data, 'result'));
      return null;
    });
  }

  // ---------------------------------------------------------------------------
  // browser.*
  // ---------------------------------------------------------------------------

  @override
  Future<String?> browserOpenPage(String url) async {
    return await _syncCall<String?>('browser_open_page', {'url': url}, (data) {
      if (data is String) return data;
      if (data is Map<Object?, Object?>) return _s(data, 'sessionId', def: _s(data, 'session_id'));
      return null;
    });
  }

  @override
  Future<void> browserNavigate(String sessionId, String url) async =>
      await _syncCall<void>('browser_navigate', {'session_id': sessionId, 'url': url}, (_) {});

  @override
  Future<void> browserClose(String sessionId) async =>
      await _syncCall<void>('browser_close', {'session_id': sessionId}, (_) {});

  // ---------------------------------------------------------------------------
  // replace.* / font.* / export.* / webdav.* / webbridge.*
  // ---------------------------------------------------------------------------

  @override
  Future<List<Map<String, Object?>>> replaceList() async {
    return await _syncCall<List<Map<String, Object?>>>('replace_list', {},
        (data) => _toMaps(data).cast<Map<String, Object?>>());
  }
  @override Future<void> replaceAdd({required String name, required String pattern, required String replacement, bool enabled = true}) async =>
      await _syncCall<void>('replace_add', {'name': name, 'pattern': pattern, 'replacement': replacement, 'enabled': enabled}, (_) {});
  @override Future<void> replaceUpdate({required String ruleId, required String name, required String pattern, required String replacement, bool enabled = true}) async =>
      await _syncCall<void>('replace_update', {'rule_id': ruleId, 'name': name, 'pattern': pattern, 'replacement': replacement, 'enabled': enabled}, (_) {});
  @override Future<void> replaceDelete(String ruleId) async =>
      await _syncCall<void>('replace_delete', {'rule_id': ruleId}, (_) {});
  @override Future<void> replaceToggleGlobal(bool enabled) async =>
      await _syncCall<void>('replace_toggle_global', {'enabled': enabled}, (_) {});

  @override
  Future<List<Map<String, Object?>>> fontScanSystem() async {
    return await _syncCall<List<Map<String, Object?>>>('font_scan_system', {},
        (data) => _toMaps(data).cast<Map<String, Object?>>());
  }
  @override Future<void> fontAddUser(String filePath) async =>
      await _syncCall<void>('font_add_user', {'file_path': filePath}, (_) {});
  @override Future<void> fontRemove(String fontId) async =>
      await _syncCall<void>('font_remove', {'font_id': fontId}, (_) {});
  @override Future<void> fontSelect(String family) async =>
      await _syncCall<void>('font_select', {'family': family}, (_) {});

  @override Future<void> exportEpub(String bookKey, String outputPath) async =>
      await _syncCall<void>('export_epub', {'book_key': bookKey, 'output_path': outputPath}, (_) {});
  @override Future<void> exportTxt(String bookKey, String outputPath) async =>
      await _syncCall<void>('export_txt', {'book_key': bookKey, 'output_path': outputPath}, (_) {});

  @override Future<void> webdavBackup() async =>
      await _syncCall<void>('webdav_backup', {}, (_) {});
  @override Future<void> webdavRestore(String backupId) async =>
      await _syncCall<void>('webdav_restore', {'backup_id': backupId}, (_) {});
  @override
  Future<List<Map<String, Object?>>> webdavListBackups() async {
    return await _syncCall<List<Map<String, Object?>>>('webdav_list_backups', {},
        (data) => _toMaps(data).cast<Map<String, Object?>>());
  }

  @override Future<void> webbridgeStart(int port) async =>
      await _syncCall<void>('webbridge_start', {'port': port}, (_) {});
  @override Future<void> webbridgeStop() async =>
      await _syncCall<void>('webbridge_stop', {}, (_) {});
  @override Future<Map<String, Object?>> webbridgeStatus() async {
    return await _syncCall<Map<String, Object?>>('webbridge_status', {}, (data) {
      if (data is Map<Object?, Object?>) return data.cast<String, Object?>();
      return {};
    });
  }

  // ---------------------------------------------------------------------------
  // Parsing helpers
  // ---------------------------------------------------------------------------

  List<ContentBlock> _parseBlocks(List<Object?>? items) {
    return items?.whereType<Map<Object?, Object?>>().map((item) {
      final hdrs = item['headers'] as Map<Object?, Object?>?;
      final headerMap = <String, String>{};
      if (hdrs != null) {
        for (final entry in hdrs.entries) {
          if (entry.key is String) headerMap[entry.key as String] = '${entry.value}';
        }
      }
      return ContentBlock(kind: _s(item, 'kind'), value: _s(item, 'value'), url: _s(item, 'url'),
          binary: _b(item, 'binary'), title: _s(item, 'title'), headers: headerMap);
    }).toList() ?? [];
  }

  List<String> _toStrings(Object? items) {
    if (items is! List) return const [];
    return items.whereType<String>().toList();
  }
}
