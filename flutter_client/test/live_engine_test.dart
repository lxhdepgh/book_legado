/// G1 判据的实弹臂：**真 `--serve` 子进程** → 握手 → `bookshelf.list` → 与 flet 投影逐行比。
///
/// 这一支和 `bookshelf_screen_test` 的分工是：那些臂用替身证"形状对"，这一支证
/// "接上真的还是一样对"。参照物是 `.probe/g1_flet_projection.py` 无头导出的
/// **flet 侧真投影**（`_to_render_record` + `_row` 的副标题拼法），不是 Dart 自己算的
/// ——同书单比对如果两边都读 Dart 的投影，Dart 错了两边一起绿。
///
/// 为什么这一支**不是** `testWidgets`：`TestWidgetsFlutterBinding` 会把
/// `HttpOverrides.global` 换成替身，于是这个 isolate 里所有 dart:io 的 HTTP 一律回
/// 400、一个包都不发（跑第一版时它就在握手那一步静默等到 5 分钟超时）。真环回 socket
/// 和真子进程只能待在纯 `test()` 里；"这批行挂上屏之后文案对不对"落在
/// `bookshelf_screen_test`，两边用同一张投影表接起来。
///
/// 没给 `LEGODO_PYTHON` / `LEGODO_MAIN` 就整支跳过（`flutter test` 的日常跑法不带 python）。
/// 全程 127.0.0.1：语料是真库的**只读副本** + 3 条回环假源，一行都不出本机。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:legodo_client/core/rpc/engine_process.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';

final String? _python = Platform.environment['LEGODO_PYTHON'];
final String? _mainPy = Platform.environment['LEGODO_MAIN'];
final bool _live = _python != null && _mainPy != null;

/// 读 `tests/gateway/` 底下的一份工件：`golden/…` 是真响应样本，
/// `projection/…` 是 flet 侧算出来的参照表——两边都在仓库里，不该各有两条拼路径。
Map<String, Object?> _loadJson(String relative) => jsonDecode(
        File('${Directory.current.parent.path}/tests/gateway/$relative')
            .readAsStringSync(encoding: utf8)) as Map<String, Object?>;

/// 一发真调用量出来的东西。
class _Live {
  const _Live({
    required this.started,
    required this.info,
    required this.rows,
    required this.handshakeMs,
    required this.rpcMs,
    required this.counter,
  });

  final StartedEngine started;
  final GatewayInfo info;
  final List<BookshelfItem> rows;
  final int handshakeMs;
  final int rpcMs;
  final _CountingClient counter;

  int get dataBytes =>
      utf8.encode(jsonEncode(rows.map((row) => row.raw).toList())).length;
}

/// 只数进出的字节，别的都转给真 client。
///
/// 为什么要**按 action** 数：这一臂至少发两发（`engine.hello` + `bookshelf.list`），
/// 一个总数就是把"动作表的响应"和"书架的响应"混成一个数——判据里那句"25,012 B"要的是
/// 书架那一发，混在一起的读数没法跟它对（也拦不住"多发了什么"这件事）。
///
/// 三个口径也各归各的：这里的"入"是 HTTP 响应体（信封含在内），Python 样本里的
/// `bytes` 是 `data` 重新编码的长度，Dart 侧 `dataBytes` 又是 `jsonEncode`（紧凑分隔符）
/// 的口径。三份都报，不换算。
class _CountingClient extends http.BaseClient {
  _CountingClient(this._inner);

  final http.Client _inner;
  int sentBytes = 0;
  int receivedBytes = 0;
  final Map<String, int> sentByAction = <String, int>{};
  final Map<String, int> receivedByAction = <String, int>{};

  String get perAction => sentByAction.keys.map((String action) {
        return '$action 出 ${sentByAction[action]}B 入 ${receivedByAction[action]}B';
      }).join(' · ');

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final body = await request.finalize().toBytes();
    sentBytes += body.length;
    var action = '(没解出 action)';
    try {
      final decoded = jsonDecode(utf8.decode(body));
      if (decoded is Map<String, Object?>) action = '${decoded['action']}';
    } on FormatException {
      // 解不出来就留着那个占位：计数本身不许因为探针而改变行为。
    }
    sentByAction[action] = (sentByAction[action] ?? 0) + body.length;
    // 走 `Request..bodyBytes` 而不是 `Client.post(body: …)`：后者按 latin1 编字节，
    // 而"中文 payload 变乱码"正是这条竖切要拦的第一类坑。
    final outbound = http.Request(request.method, request.url)
      ..followRedirects = request.followRedirects
      ..headers.addAll(request.headers)
      ..bodyBytes = body;
    final response =
        await http.Response.fromStream(await _inner.send(outbound));
    receivedBytes += response.bodyBytes.length;
    receivedByAction[action] =
        (receivedByAction[action] ?? 0) + response.bodyBytes.length;
    return http.StreamedResponse(
        Stream<List<int>>.value(response.bodyBytes), response.statusCode,
        headers: response.headers, reasonPhrase: response.reasonPhrase);
  }

  @override
  void close() => _inner.close();
}

/// 一行的副标题形状：`A` = 有作者，`C` = 有进度，缺的那一段记 `-`。
String _shapeOf(Map<String, Object?> row) =>
    '${(row['author']! as String).isEmpty ? '-' : 'A'}'
    '${(row['chapter_name']! as String).isEmpty ? '-' : 'C'}';

List<Object?> _projectionRows() =>
    (_loadJson('projection/bookshelf.list.json')['rows']! as List);

Future<_Live> _bootOnce() async {
  final cold = Stopwatch()..start();
  final started = await EngineProcess.start(python: _python, mainPy: _mainPy);
  cold.stop();
  final counter = _CountingClient(http.Client());
  final repository = RpcEngineRepository(RpcClient(started.endpoint, client: counter));
  try {
    final info = await repository.hello();
    final rpc = Stopwatch()..start();
    final rows = await repository.listBookshelf();
    rpc.stop();
    return _Live(
      started: started,
      info: info,
      rows: rows,
      handshakeMs: cold.elapsedMilliseconds,
      rpcMs: rpc.elapsedMilliseconds,
      counter: counter,
    );
  } catch (exc) {
    started.process.kill(ProcessSignal.sigterm);
    rethrow;
  }
}

void main() {
  test('这一臂的开关：skip 只在没 python 时发生，比对本身不许被偷偷跳过', () {
    expect(_live, _python != null && _mainPy != null);
    if (!_live) return;
    // 环境齐了就必须有投影表：不然下面那条比的是"和一张不存在的表一致"。
    expect(_projectionRows(), isNotEmpty, reason: '先跑 .probe/g1_flet_projection.py');
  });

  test('真引擎竖切：握手 → bookshelf.list → 每行与 flet 投影一致', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN：这一臂要真起子进程');
      return;
    }
    final projection = _loadJson('projection/bookshelf.list.json');
    final expected = (projection['rows']! as List)
        .cast<Map<String, Object?>>();
    final live = await _bootOnce();
    addTearDown(() async {
      live.counter.close();
      live.started.process.kill(ProcessSignal.sigterm);
    });

    stdout.writeln('LIVE 握手 ${live.handshakeMs}ms · 端口 ${live.started.endpoint.port}'
        ' · bookshelf.list ${live.rows.length} 行 ${live.rpcMs}ms'
        ' · 合计 出 ${live.counter.sentBytes}B 入 ${live.counter.receivedBytes}B'
        ' · ${live.counter.perAction}'
        ' · 数据体(Dart 口径) ${live.dataBytes}B · pid ${live.info.pid}');

    // 计数仪自己也得有在岗证据：只断言"读到的数好看"的话，仪器没接上时照样绿。
    expect(live.counter.sentByAction, hasLength(2),
        reason: '这一臂该正好两发（engine.hello + bookshelf.list），'
            '多出来的那一发是谁发的得在这里响，不该混进字节读数里');
    final shelfBody = live.counter.receivedByAction['bookshelf.list'] ?? 0;
    expect(shelfBody, greaterThan(live.dataBytes),
        reason: '书架那一发的 HTTP 体必须**大于**数据体——信封那一层是要占字节的，'
            '读出来反而小就是计数把两发并成了一发或压根没数到');

    expect(live.info.protocolVersion, kProtocolVersion,
        reason: '握手通了但版本不符 = 客户端该拒，不该继续画界面');
    expect(live.info.actions, contains('bookshelf.list'));

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
  }, timeout: const Timeout(Duration(minutes: 5)));

  test('收摊：engine.shutdown 之后子进程退出码 0，端口跟着放掉', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN');
      return;
    }
    final live = await _bootOnce();
    final port = live.started.endpoint.port;
    final repository = RpcEngineRepository(
        RpcClient(live.started.endpoint, client: http.Client()));
    await repository.shutdown(reason: 'g1 live test done');
    live.counter.close();

    final rc = await live.started.process.exitCode
        .timeout(const Duration(seconds: 60),
            onTimeout: () => throw StateError(
                '发了 engine.shutdown，60s 内进程没退（握手 ${live.handshakeMs}ms 之后挂住）'));
    expect(rc, 0, reason: '非 0 = 无头入口收尾没走干净');

    // 端口真放掉没有：还连着上就是"停掉的只是 shell、python 还占着端口"那一类
    // （这一条的判据是连不上，不是连得上——所以它需要一发对照）。
    var refused = false;
    try {
      final probe = await Socket.connect('127.0.0.1', port,
          timeout: const Duration(seconds: 3));
      probe.destroy();
    } on SocketException {
      refused = true;
    }
    expect(refused, isTrue,
        reason: '退出码 0 但端口还占着 = 还有别的进程没退，正对照见下面那条');
    // 正对照：同一个 client 对**还活着**的端口该连得上（否则上面那条 refused
    // 可能只是探针自己坏了）。
    final live2 = await _bootOnce();
    addTearDown(() {
      live2.counter.close();
      live2.started.process.kill(ProcessSignal.sigterm);
    });
    final socket = await Socket.connect('127.0.0.1', live2.started.endpoint.port,
        timeout: const Duration(seconds: 5));
    socket.destroy();
  }, timeout: const Timeout(Duration(minutes: 5)));
}
