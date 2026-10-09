/// G2-F **D1**：`source.list` 那 920 行跨进程那一趟，在 **Dart 客户端这一侧**复量。
///
/// 靶子是第八十二轮同一份 920 行真库副本上的 Python 读数（`.probe/r4_service_baseline.py`
/// 的 S 臂：p50 80.6 ms / 6,969,038 B），§9 的判据是"≥ flet"。两臂必须**同一天、同一份语料**
/// 才有减法可做，所以这一支由 `.probe/g2f_dart_baseline.py` 驱动：它先 `seed_dir()` 造副本、
/// 在同一个副本上把 Python 那臂当天重测，再把数据目录交给我们拉起第二个引擎。
/// 注意那一轮的载荷是**出口投影之前**的 6.9 MB；投影后同一副本今天重测是
/// 20.5 ms / 360,700 B，减法与横比拿当天这一对做。
///
/// 三笔钱分开报，是因为"客户端拿到那 920 行"在两边根本不是同一个跨度：
/// Python 的 S 臂计时**停在 `resp.read()`**，`json.loads` 在表外面；Dart 这边
/// `repository.listSources()` 一发把 socket、解 JSON、建 `SourceRecord` 全算进去。
/// 不拆开就会把"Python 没计的解析"记成"Dart 慢"。所以这里同一发报三个数：
///
/// - **A 全程**：生产路径那一发（调用方真正付的钱）。
/// - **B 只到线**：同一次调用里，`http` 那一层从发出到**响应体读满**的墙钟 + UTF-8 字节。
///   这是与 Python S 臂同跨度的那一个数，减法拿它做。
/// - **C 只解 JSON**：拿 B 收下来的那段字节离线重放（各段取中位，见 `_cRounds`），
///   把 A−B 那笔拆成 C1 `utf8.decode` / C2 `jsonDecode` / C3 建 `SourceRecord` /
///   C4 解+建（与 A−B 同跨度）。四段各自一个循环、一次只让一棵解码图活着，
///   三段之和再对**逐轮** A−B 核账（±50 % 判据）。
///
/// 底价臂：每一轮都夹一发 `engine.status`（同一条管道、几十行状态）。A−H 才是数据那笔钱，
/// H 是管道那笔钱；没有它，"30 ms"分不清是那 920 行的钱还是这台机器的钱。
/// 轮内**满行与状态交替**（记忆 ab-bench-must-interleave），前 `_warmup` 轮不记账。
///
/// 全程 127.0.0.1：语料是真库的**只读副本** + 3 条回环假源，一行都不出本机。
/// 没给 `LEGODO_R4_DATA`（或没有 python）就整支跳过——日常 `flutter test` 不带这些。
library;

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:legodo_client/core/rpc/engine_process.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';

final String? _python = Platform.environment['LEGODO_PYTHON'];
final String? _mainPy = Platform.environment['LEGODO_MAIN'];

/// 驱动那支造好的语料目录（真库副本 + 3 条回环假源）。给了它就不再碰用户自己那本库。
final String? _dataDir = Platform.environment['LEGODO_R4_DATA'];
final bool _live = _python != null && _mainPy != null && _dataDir != null;

/// 语料落在仓库里的位置：D2/D3 那一支不能碰 socket（`TestWidgetsFlutterBinding`
/// 把 `HttpOverrides.global` 换成替身），所以 920 行原文由这里**落盘**再给它读。
final String _corpusPath = Platform.environment['LEGODO_R4_CORPUS'] ??
    '${Directory.current.parent.path}/.probe/out/g2f_source_list.rows.json';

const int _warmup = 3;
const int _rounds = 11;

/// C 那四臂的轮数/暖机：它们是一段纯 CPU 的重放，比跨进程那臂便宜得多，
/// 但**同样不能单发**——`flutter test` 的 isolate 是 JIT，头两轮还在解释执行，
/// 单发读到的 97 ms 里有一部分是"还没热"。第一段实测（单发版）就是这个坑。
const int _cWarmup = 2;
const int _cRounds = 5;
const int _minRows = 900;

/// `source.list` 投影后的键集，**从 action 表现抄**（`app/gateway/methods.py` 的
/// `Method.fields`）。这一臂不再钉字节数：第八十四轮把出口投影成这六格之后，
/// 920 行是 360,700 B（投影前 6,969,038 B），而"读的是不是那一发"只有键集判得了。
const List<String> _projectedKeys = <String>[
  'bookSourceUrl', 'bookSourceName', 'bookSourceGroup',
  'bookSourceComment', 'enabled', 'loginUrl',
];

/// 只计墙钟与字节，别的都原样转给真 client。
///
/// 按 action 分账（记忆 aggregated-counters-hide-which-call）：这一臂每轮至少两发
/// （`source.list` + `engine.status`），一个总数就把 360 KB 与 5.5 KB 混成一个数。
class _SplitClient extends http.BaseClient {
  _SplitClient(this._inner);

  final http.Client _inner;
  final Map<String, int> wireMicros = <String, int>{};
  final Map<String, int> bytes = <String, int>{};

  /// 只在要留样本的那一发打开，而且**只留 `source.list` 那一发**：一个开关留着整轮，
  /// 夹在后面的那发底价会把样本盖掉（第一版就是这样——C 臂解出来 5,507 B，
  /// 正好是 `engine.status` 的大小，还差点把"离线重建行数与生产路径一致"那条
  /// 比对糊过去）。
  bool capture = false;
  Uint8List? capturedBody;

  double wireMs(String action) => (wireMicros[action] ?? 0) / 1000.0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final action = _actionOf(request);
    final outbound = http.Request(request.method, request.url)
      ..followRedirects = request.followRedirects
      ..headers.addAll(request.headers)
      ..bodyBytes = await request.finalize().toBytes();
    final watch = Stopwatch()..start();
    final response = await http.Response.fromStream(await _inner.send(outbound));
    watch.stop();
    // 读满才算线上传完：`fromStream` 已经把 body 收进内存，这一档与 Python 臂的
    // `await resp.read()` 是同一个跨度。
    wireMicros[action] = watch.elapsedMicroseconds;
    bytes[action] = response.bodyBytes.length;
    if (capture && action == 'source.list') capturedBody = response.bodyBytes;
    return http.StreamedResponse(Stream<List<int>>.value(response.bodyBytes),
        response.statusCode,
        headers: response.headers, reasonPhrase: response.reasonPhrase);
  }

  static String _actionOf(http.BaseRequest request) {
    if (request is! http.Request) return '(非 Request)';
    try {
      final decoded = jsonDecode(utf8.decode(request.bodyBytes));
      return decoded is Map<String, Object?> ? '${decoded['action']}' : '(没有 action)';
    } on FormatException {
      return '(解不出 action)';
    }
  }

  @override
  void close() => _inner.close();
}

void main() {
  test('这一臂的开关：跳过只发生在没 python / 没语料目录时', () {
    expect(_live, _python != null && _mainPy != null && _dataDir != null);
    if (!_live) return;
    // `File(path).existsSync()` 对**目录**回 false（第一版就在这条上红：目录明明在、
    // 引擎也真起了，判据却说"目录不在"）。目录用 `Directory`，库文件用 `File`。
    expect(Directory(_dataDir!).existsSync(), isTrue,
        reason: '语料目录不在，下面那臂会去开一个空库');
    expect(File('$_dataDir/legado.db').existsSync(), isTrue,
        reason: '目录在但库里没有 `legado.db`：那还是空库');
  });

  test('D1 920 行跨进程：全程 / 只到线 / 只解 JSON，夹一发小载荷当底价', () async {
    if (!_live) {
      markTestSkipped('需要 LEGODO_PYTHON / LEGODO_MAIN / LEGODO_R4_DATA');
      return;
    }
    final started = await EngineProcess.start(
      python: _python,
      mainPy: _mainPy,
      environment: <String, String>{
        'LEGADO_BOOKSOURCE_DATA_DIR': _dataDir!,
        'LEGADO_BOOKSOURCE_CACHE_DIR': '$_dataDir/cache-dart',
      },
    );
    final split = _SplitClient(http.Client());
    final client = RpcClient(started.endpoint, client: split);
    final repository = RpcEngineRepository(client);
    addTearDown(() {
      split.close();
      started.process.kill(ProcessSignal.sigterm);
    });
    await repository.hello();

    final full = <double>[], wire = <double>[], floor = <double>[], floorWire = <double>[],
        insideCall = <double>[];
    int rows = 0, payloadBytes = 0, floorBytes = 0;
    List<SourceRecord> last = <SourceRecord>[];
    for (var i = 0; i < _rounds + _warmup; i++) {
      final keep = i == _rounds + _warmup - 1;
      split.capture = keep;
      final watch = Stopwatch()..start();
      last = await repository.listSources();
      watch.stop();
      final thisWire = split.wireMs('source.list');
      final thisFloor = Stopwatch()
        ..start();
      final status = await client.call('engine.status');
      thisFloor.stop();
      if (i >= _warmup) {
        final fullMs = watch.elapsedMicroseconds / 1000.0;
        full.add(fullMs);
        wire.add(thisWire);
        // **逐轮差**才是"A−B"：两个中位各取各的，遇到某一发 socket 特别慢（后台那件事、
        // 杀软扫盘）就会把差量摊薄——第一版就在这里读出 92.9 ms，而同一段字符串离线
        // 解一遍要 115 ms，看着像算术矛盾，其实是口径错。
        insideCall.add(fullMs - thisWire);
        floor.add(thisFloor.elapsedMicroseconds / 1000.0);
        floorWire.add(split.wireMs('engine.status'));
        rows = last.length;
        payloadBytes = split.bytes['source.list'] ?? 0;
        floorBytes = split.bytes['engine.status'] ?? 0;
      }
      expect(status, isNotNull, reason: '底价臂那一发要真回话，不然它是空转');
    }

    // ---- 仪器在岗证据（记忆 zero-counts-need-a-positive-control）----
    expect(rows, greaterThanOrEqualTo(_minRows),
        reason: '只拿到 $rows 行 = 语料没接上，后面每个数都是空的');
    // 每行**不许带六格之外的键**：多一个键就是出口投影没生效，那一臂量的还是
    // 6.9 MB 的老形状。反过来"少键"是合法的——`BookSource.to_dict` 按 Gson 丢 null，
    // 没设备注/登录页的源就只有四格或五格（920 行里齐六格的只有 168 行），所以
    // "六格齐不齐"只能在全库并集上判，不能逐行判。
    final strays = last
        .where((record) => record.raw.keys.any((key) => !_projectedKeys.contains(key)))
        .length;
    expect(strays, 0,
        reason: '$strays/$rows 行带着投影之外的键 = 那一格没生效，'
            '这一臂量的就不是现在那份契约');
    final seenKeys = <String>{for (final record in last) ...record.raw.keys};
    expect(_projectedKeys.where((key) => !seenKeys.contains(key)), isEmpty,
        reason: '这一份 920 行里没人带出 $_projectedKeys 之外的这些键 = 语料不是靶子那份');
    expect(floorBytes, lessThan(payloadBytes ~/ 10),
        reason: '底价臂必须**小得多**：两臂载荷同量就是没拆开，减法不成立');
    expect(wire.last, lessThan(full.last),
        reason: '只到线那一档必须真比全程小，否则 A−B 那笔"解析+建模"是假的');

    stdout.writeln('G2F-D1 引擎 pid=${started.process.pid} 端口=${started.endpoint.port} '
        '数据目录=$_dataDir');
    stdout.writeln('G2F-D1 source.list $rows 行 · 响应体 ${_b(payloadBytes)} B · '
        'n=${full.length}（暖机 $_warmup 轮不记账）');
    _report('A 全程（repository.listSources）', full);
    _report('B 只到线（同一次调用里 http 那一层）', wire);
    _report('H 底价 engine.status 全程', floor);
    _report('H 底价只到线', floorWire);

    // ---- C：拿最后一发的字节离线重放，把 A−B 那笔拆开 ----
    // 每一段各占**一个循环**，一次只让一棵解码图活着（生产路径就是这个形状：上一发
    // 那棵在 80 ms 的 socket 等待里早被收走）。四臂挤在同一个循环里时，靠后的臂背着
    // 前面几臂的活对象，回收那笔就算进了表里——第一版就是这样，C4（解+建）读出来
    // 121.0 ms 比 C2（只解）的 125.3 ms 还小，"加建 = C4−C2" 成了 −4.3 ms。
    //
    // 也不是单发：`flutter test` 的 isolate 跑 JIT，头几轮还在解释执行，单发把
    // "还没热"记成稳态的钱（第一版单发读到 97 ms，多轮中位是 115 ms 那一档）。
    final body = split.capturedBody!;
    expect(body.length, payloadBytes, reason: '留样本那一发与记账那一发要同一发');

    // 解析**期间**堆上不许留着上一发那棵解码图：生产路径那时它已经死了（只有上一发
    // 那 920 条 `SourceRecord` 活着，量级差两个数量级）。留着它，每一发解析都得背着
    // 一棵百来 MB 的活对象回收——第一版就背在这里，三段和 146.9 ms 对逐轮 A−B 93.8 ms。
    // 所以每一臂开表前先把上一发的产物置 null，收表后再交出去。
    List<Object?>? dataRows;
    String? keptText;
    // 交出去给 C3/落盘用的那一份：`dataRows` 为了"解析期间不留上一发的图"声明成可空的，
    // 而 CFE 不做跨可空提升（`flutter analyze` 过了不等于编得过，第一版就在落盘那行炸）。
    late List<Object?> dumpRows;

    final decodeOnly = <double>[];
    for (var i = 0; i < _cWarmup + _cRounds; i++) {
      keptText = null;
      dataRows = null;
      final (ms, text) = _timedValue(() => utf8.decode(body));
      keptText = text;
      if (i >= _cWarmup) decodeOnly.add(ms);
    }
    final jsonOnly = <double>[], cTrend = <String>[];
    for (var i = 0; i < _cWarmup + _cRounds; i++) {
      dataRows = null;
      final (ms, again) =
          _timedValue(() => jsonDecode(keptText!) as Map<String, Object?>);
      final againRows = again['data']! as List<Object?>;
      if (i >= _cWarmup) jsonOnly.add(ms);
      cTrend.add('$i:${_ms(ms)}');
      dataRows = againRows;
    }
    expect(dataRows, hasLength(rows), reason: '重解那一臂解出来不是同一发');
    final modelOnly = <double>[], both = <double>[];
    List<SourceRecord>? built;
    for (var i = 0; i < _cWarmup + _cRounds; i++) {
      built = null;
      final (ms, rowsAsRecords) = _timedValue(() => dataRows!
          .cast<Map<String, Object?>>()
          .map(SourceRecord.fromJson)
          .toList(growable: false));
      if (i >= _cWarmup) modelOnly.add(ms);
      built = rowsAsRecords;
    }
    for (var i = 0; i < _cWarmup + _cRounds; i++) {
      dataRows = null;
      built = null;
      final (ms, pairs) = _timedValue(() {
        final Map<String, Object?> again = jsonDecode(keptText!) as Map<String, Object?>;
        final rowsAgain = again['data']! as List<Object?>;
        return (rowsAgain,
            rowsAgain
                .cast<Map<String, Object?>>()
                .map(SourceRecord.fromJson)
                .toList(growable: false));
      });
      dataRows = pairs.$1;
      built = pairs.$2;
      dumpRows = pairs.$1;
      if (i >= _cWarmup) both.add(ms);
    }
    expect(built!.length, rows, reason: '离线重建的行数与生产路径不一致 = 两臂读的不是同一发');
    expect(built.first.url, last.first.url, reason: '同上：连第一行都得是同一行');

    // 读数先报，断言后判：判据红的时候这几个数正是线索，别把它们藏在失败后面。
    _report('C1 只 utf8.decode（生产路径里那一层）', decodeOnly);
    _report('C2 只 jsonDecode（同一段字符串）', jsonOnly);
    _report('C3 只建 $rows 个 SourceRecord', modelOnly);
    _report('C4 解+建（与 A−B 同跨度的那一段）', both);
    stdout.writeln('G2F-D1 C 只 jsonDecode 逐轮：${cTrend.join(' ')} · '
        'UTF-8 ${_b(body.length)} B · 建 $rows 个 SourceRecord 那笔另见 C3');

    // 算术核账：三段之和该落在逐轮 A−B 那一档（±50 %）。对不上就是一条坏仪器读数——
    // 要么还有一段没报，要么重放的条件和生产不一样。
    final segments = _p50(decodeOnly) + _p50(jsonOnly) + _p50(modelOnly);
    final aMinusB = _p50(insideCall);
    final twoMedians = _p50(full) - _p50(wire);
    stdout.writeln('G2F-D1 C 算术核账：转码 ${_ms(_p50(decodeOnly))} + 解 JSON '
        '${_ms(_p50(jsonOnly))} + 建模 ${_ms(_p50(modelOnly))} = ${_ms(segments)} ms '
        '对 A−B（逐轮差的中位）${_ms(aMinusB)} ms，差 ${_ms(segments - aMinusB)} ms');
    expect(segments, inInclusiveRange(aMinusB * 0.5, aMinusB * 1.5),
        reason: '三段和 ${_ms(segments)} ms 与逐轮 A−B ${_ms(aMinusB)} ms 差得超过 50 %：'
            '那笔钱里有一段没报，或重放条件与生产不同');
    stdout.writeln('G2F-D1 换算：A−B（一发之内的解码+信封+建模）中位 ${_ms(aMinusB)} ms'
        '（两个中位相减是 ${_ms(twoMedians)} ms，别用那个：两臂各自的中位不是同一发）· '
        'A−H（数据那笔钱）≈ ${_ms(_p50(full) - _p50(floor))} ms · '
        'B−H（只到线那两臂之差）≈ ${_ms(_p50(wire) - _p50(floorWire))} ms');

    // 语料落盘给 D2/D3（它们在一个不允许真 socket 的 isolate 里）。
    final file = File(_corpusPath);
    file.parent.createSync(recursive: true);
    file.writeAsStringSync(jsonEncode(dumpRows), encoding: utf8);
    stdout.writeln('G2F-D1 语料已落盘：${file.path} '
        '（${_b(file.lengthSync())} B，${dumpRows.length} 行）');
  }, timeout: const Timeout(Duration(minutes: 8)));
}

/// 千分位。自己写是因为 `intl` 不在这个包里，而读数要能跟 Python 那臂的
/// `f"{size:,}"` 对眼，逗号位置必须一致。
String _b(int bytes) {
  final digits = bytes.toString();
  final head = digits.length % 3;
  final parts = <String>[
    if (head > 0) digits.substring(0, head),
    for (var i = head; i < digits.length; i += 3) digits.substring(i, i + 3),
  ];
  return parts.join(',');
}

String _ms(double value) => value.toStringAsFixed(1);

/// 起表跑一段**同步**的 CPU 活，返回 (墙钟 ms, 它的产物)。
/// 产物必须一起交出来：只留计时而不留那份解码结果，下一臂就得重解一遍，
/// "只建 SourceRecord"那一臂就混进了第二次 `jsonDecode` 的钱。
(double, T) _timedValue<T>(T Function() body) {
  final watch = Stopwatch()..start();
  final value = body();
  watch.stop();
  return (watch.elapsedMicroseconds / 1000.0, value);
}

double _p50(List<double> xs) {
  if (xs.isEmpty) return double.nan;
  final sorted = List<double>.of(xs)..sort();
  return sorted[sorted.length ~/ 2];
}

void _report(String label, List<double> samples) {
  final sorted = List<double>.of(samples)..sort();
  // p95 的下标取法与 Python 那臂 `quantiles` 一致（`int(n*0.95)-1`），
  // 两边各按各的写法算出来的"p95"不是同一个分位数，报进同一张表就是假差额。
  final p95 = sorted[((sorted.length * 0.95).floor() - 1).clamp(0, sorted.length - 1)];
  stdout.writeln('G2F-D1 $label：p50=${_ms(_p50(samples))} ms '
      'p95=${_ms(p95)} ms 最差=${_ms(sorted.last)} ms');
}
