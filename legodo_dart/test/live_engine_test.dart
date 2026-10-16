/// FFI 竖切实弹臂：**进程内真 Go 引擎（`legodo_engine.dll`）** → `bookshelf.list`
/// → 与 flet 投影逐行比。
///
/// 这一支和 `bookshelf_screen_test` 的分工是：那些臂用替身证"形状对"，这一支证
/// "接上真的还是一样对"。参照物是 `.probe/g1_flet_projection.py` 无头导出的
/// **flet 侧真投影**（`_to_render_record` + `_row` 的副标题拼法），不是 Dart 自己算的
/// ——同书单比对如果两边都读 Dart 的投影，Dart 错了两边一起绿。
///
/// 为什么这一支**不是** `testWidgets`：引擎在进程内、它的网络是 Go 自己的（不经
/// dart:io 的 `HttpOverrides`，也不经测试的假时钟）——这一臂只需要真异步时间。
/// "这批行挂上屏之后文案对不对"落在 `bookshelf_screen_test`，两边用同一张投影表接起来。
///
/// 三个开关（少一个就整支跳过；`.probe/ffi_live_run.py` 一次配齐、连 DLL 一起编好）：
/// `LEGODO_ENGINE_DLL`（编译好的 `legodo_engine.dll`，`GoFFIClient._findLibraryPath`
/// 的活臂口子）、`LEGADO_BOOKSOURCE_DATA_DIR`（语料目录：真库只读副本 + 3 条回环假源，
/// `r4_service_baseline.seed_dir()` 建的）、Windows（`GoFFIClient` 的内存分配器目前
/// 只实现 kernel32）。全程 127.0.0.1：一行都不出本机。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/rpc/engine_status.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';

final String? _dll = Platform.environment['LEGODO_ENGINE_DLL'];
final String? _dataDir = Platform.environment['LEGADO_BOOKSOURCE_DATA_DIR'];

/// 活臂的开关：DLL 与语料目录都得给，且都得真指到东西上（给个空目录不算给了）。
bool get _live =>
    _dll != null &&
    _dataDir != null &&
    Platform.isWindows &&
    File(_dll!).existsSync() &&
    File('$_dataDir${Platform.pathSeparator}legado.db').existsSync();

Map<String, Object?> _loadJson(String relative) => jsonDecode(
        File('${Directory.current.parent.path}/tests/gateway/$relative')
            .readAsStringSync(encoding: utf8)) as Map<String, Object?>;

List<Object?> _projectionRows() =>
    (_loadJson('projection/bookshelf.list.json')['rows']! as List);

/// 一行的副标题形状：`A` = 有作者，`C` = 有进度，缺的那一段记 `-`。
String _shapeOf(Map<String, Object?> row) =>
    '${(row['author']! as String).isEmpty ? '-' : 'A'}'
    '${(row['chapter_name']! as String).isEmpty ? '-' : 'C'}';

/// 一发真调用量出来的东西。
class _Live {
  const _Live({
    required this.adapter,
    required this.statuses,
    required this.rows,
    required this.rpcMs,
  });

  final FFIEngineAdapter adapter;
  final List<ServiceStatus> statuses;
  final List<BookshelfItem> rows;
  final int rpcMs;

  /// 数据体口径与旧的 HTTP 臂一致：`jsonEncode`（紧凑分隔符）后的字节数。
  /// FFI 没有 HTTP 体，这一格只剩"书架这一发的数据有多大"一个读数。
  int get dataBytes =>
      utf8.encode(jsonEncode(rows.map((row) => row.raw).toList())).length;
}

Future<_Live> _bootOnce() async {
  final adapter = FFIEngineAdapter();
  await adapter.connect();
  try {
    // 先读一遍在册服务：这一格是"引擎真活着"的在岗证据——只看书架行数的话，
    // 一个"啥也没注册但恰好回了空数组"的引擎也能让它绿。
    final statuses = await adapter.serviceStatus();
    final rpc = Stopwatch()..start();
    final rows = await adapter.listBookshelf();
    rpc.stop();
    return _Live(
      adapter: adapter,
      statuses: statuses,
      rows: rows,
      rpcMs: rpc.elapsedMilliseconds,
    );
  } catch (exc) {
    await adapter.disconnect();
    rethrow;
  }
}

/// 收摊：喊一发 `engine.shutdown` 再让 Dart 侧归位。喊不动也算（尽力而为）。
Future<void> _stop(FFIEngineAdapter adapter) async {
  try {
    await adapter.shutdown(reason: 'g1 ffi live arm done');
  } on Object {
    // 引擎已经不理了就别再喊：收尾的错不该把红判在正文那一格上。
  }
  await adapter.disconnect();
}

void main() {
  test('先决：DLL 与语料都真在场（缺一个就整支跳过）', () {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR'
          '（或文件不在）：跑 .probe/ffi_live_run.py 一次配齐');
      return;
    }
    // 环境齐了就必须有投影表：不然下面那条比的是"和一张不存在的表一致"。
    expect(_projectionRows(), isNotEmpty, reason: '先跑 .probe/g1_flet_projection.py');
  });

  test('真引擎竖切：Initialize → bookshelf.list → 每行与 flet 投影一致', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR：这一臂要真引擎');
      return;
    }
    final projection = _loadJson('projection/bookshelf.list.json');
    final expected =
        (projection['rows']! as List).cast<Map<String, Object?>>();
    final live = await _bootOnce();
    addTearDown(() => _stop(live.adapter));

    stdout.writeln('LIVE FFI 书架 ${live.rows.length} 行 ${live.rpcMs}ms'
        ' · 数据体 ${live.dataBytes}B'
        ' · 在册服务 ${live.statuses.length} 个'
        ' · DLL ${live.adapter.ffi.libraryPath}'
        ' · 语料 $_dataDir');

    // ---- 仪器在岗：引擎真注册了哪几位、真在跑 ----
    final byId = {for (final s in live.statuses) s.serviceId: s.state};
    expect(
        byId.keys,
        containsAll(<String>[
          'StorageService', 'BookshelfService', 'ReadingService', 'SourceService',
        ]),
        reason: '在册服务缺了谁 = 那一片能力根本没注册');
    expect(byId['StorageService'], 'RUNNING',
        reason: '存储服务没在跑 = 后面的行读的是空气');

    // ---- 与 flet 投影逐行比：行数、每行的标题与副标题 ----
    expect(live.rows.length, projection['row_count'],
        reason: 'flet 那屏 ${projection['row_count']} 本，Flutter 这屏不许是别的数');
    for (var i = 0; i < expected.length; i++) {
      final want = expected[i];
      final got = live.rows[i];
      expect(got.name, want['name'], reason: '第 $i 行的书名');
      expect(got.subtitle, want['subtitle'],
          reason: '第 $i 行的副标题（作者 · 读到 X）——这一条就是 flet 的 _row');
      expect(got.identityKey, want['url'],
          reason: '第 $i 行的主键：两边都拿 url，行序或键变了会先在这里响');
    }
    // 语料里真出现过的副标题形状都得被这条比对覆盖；没出现过的形状（只有进度、
    // 两段都缺）不许假装证过——那两种在 golden_contract_test 里用手搓形状单独钉。
    final shapes = expected.map(_shapeOf).toSet();
    stdout.writeln('LIVE 副标题形状集合：$shapes');
    expect(shapes, containsAll(<String>['AC', 'A-']),
        reason: '语料里"作者+进度"和"只有作者"两种都该有样本，否则这条比对只剩一半');

    // ---- 状态行文案：这里只比字符串，屏幕那一面在 bookshelf_screen_test ----
    expect('书架共 ${live.rows.length} 本', projection['status_line'],
        reason: 'flet 的 set_status_text 与这屏的状态行必须是同一句');
  }, timeout: const Timeout(Duration(minutes: 3)));

  test('收摊与复用：engine.shutdown 真把引擎放掉，再 Initialize 接得回来', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR');
      return;
    }
    final adapter = FFIEngineAdapter();
    await adapter.connect();
    final before = await adapter.listBookshelf();
    expect(before, isNotEmpty,
        reason: '先证这一趟是活的，否则下面"放掉了"那一条没有对照');

    await adapter.shutdown(reason: 'g1 ffi live arm done');
    // Go 侧 Cleanup 之后 hub 就没了：这一发必须**当场响**（hub not initialized）。
    // 收摊不响 = 没真放掉，或放了之后还有另一条路在替它答。
    await expectLater(
        adapter.listBookshelf(),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
            .having((EngineFailure e) => e.message, 'message',
                contains('hub not initialized'))),
        reason: '收了摊还答得上来 = 这一发不是真引擎在答');

    // `engine_ffi.dart` 那句"Cleanup 后可复用"在这里钉成活的行为：
    // disconnect 把 Dart 侧的 `_ready` 归位（Go 已经 Cleanup 过，这一发是幂等的），
    // 再 connect 走一遍 Initialize，引擎必须整装重来。
    await adapter.disconnect();
    await adapter.connect();
    addTearDown(() => _stop(adapter));
    final after = await adapter.listBookshelf();
    expect(after.length, before.length,
        reason: '重接回来读到的行数与放掉之前不是一个数：状态没从磁盘重来');
  }, timeout: const Timeout(Duration(minutes: 3)));
}
