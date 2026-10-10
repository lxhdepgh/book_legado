/// G3 落点实弹臂：**真 `--serve` 子进程** + 仓库里那条语料假源，两侧按字符偏移比翻页落点。
///
/// §9 门 G3 的判据是"同一本书翻页落点与 flet 版可比（按字符偏移断言）"。
/// `reader_screen_test` 那一半用替身 + 黄金样本（证形状），这一半证**接上真的还是一样对**：
///
/// - 参照物是 `tests/gateway/projection/reader.content.json`——**flet 自己的函数**算出来的
///   落点表，不是 Dart 自己算的。两边都读 Dart 的投影，Dart 错了就两边一起绿
///   （G1 那笔账，见 `live_engine_test.dart` 抬头）。
/// - 靶子是 `tests/gateway/fake_source_reader.py`（跟踪在仓库里的那份语料），由本臂当
///   **子进程**拉起来；它与黄金样本的对齐由 `tests/test_reader_fixture_matches_golden_samples.py`
///   钉住。本臂不抄语料规则：书源字典是问 `/g3/source.json` 要的。
/// - 引擎跑在**空的临时数据目录**里：这一臂会建源、入架、落进度，一行都不许进真库。
///
/// 三条仪器规矩：全程 127.0.0.1（零外网，记忆 `legado-live-scan-av-block`）；两个子进程的
/// 输出从头抽干（记忆 `subprocess-probe-must-drain-stdout`）；字节按 action 数，不留混合
/// 计数（记忆 `aggregated-counters-hide-which-call`）。
///
/// 没给 `LEGODO_PYTHON` / `LEGODO_MAIN` 就整支跳过——与 `live_engine_test.dart` 同一个开关。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:legodo_client/core/events/engine_event_client.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/rpc/engine_process.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/data/reading_position.dart';
import 'package:legodo_client/features/reader/domain/text_layout.dart';

final String? _python = Platform.environment['LEGODO_PYTHON'];
final String? _mainPy = Platform.environment['LEGODO_MAIN'];
final bool _live = _python != null && _mainPy != null;

final String _repoRoot = Directory.current.parent.path;

/// 语料搜索页里那一本书的书名。它与黄金样本、与假源页面三方的对齐钉在
/// `tests/test_reader_fixture_matches_golden_samples.py` 第 2 笔，这里只管用它。
const String _keyword = '阅读基线书';

/// 实弹臂要落点的那一章：`reader.toc` 的第 2 行（索引 1），与黄金样本同一章。
const int _chapterIndex = 1;

/// 这一章里要往返落一次的那一段（0 基）：它的偏移在投影表里是 1884。
const int _pinnedParagraph = 3;

Map<String, Object?> _loadJson(String relative) => jsonDecode(
        File('$_repoRoot/tests/gateway/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

/// 起起来的语料假源。
class _Fixture {
  _Fixture({required this.process, required this.port, required this.source, required this.lines});

  final Process process;
  final int port;
  final Map<String, Object?> source;
  final List<String> lines;

  String get sourceUrl => '${source['bookSourceUrl']}';
  String get group => '${source['bookSourceGroup']}';
}

/// 把假源当子进程拉起来，读它的 `PORT` 行，再问它要书源字典。
///
/// 端口是**内核挑的**（`--port 0`）：8797 那个数是黄金样本那一趟用过的，实弹臂去抢它
/// 只会撞进"上一个探针的 shell 退了、python 还占着端口"那一类（记忆
/// `background-server-keeps-port`）。字典里带的必须是真绑定端口——那一条在 Python 侧
/// 第 5 笔守卫里钉着，这里第 1 臂再对着 Dart 的用法钉一次。
Future<_Fixture> _startFixture() async {
  final process = await Process.start(_python!,
      <String>['$_repoRoot/tests/gateway/fake_source_reader.py', '--port', '0'],
      workingDirectory: _repoRoot);
  final lines = <String>[];
  final portLine = Completer<int>();
  void absorb(List<int> bytes, {required bool fromStdout}) {
    for (final line
        in const LineSplitter().convert(utf8.decode(bytes, allowMalformed: true))) {
      lines.add(line);
      if (fromStdout && line.startsWith('PORT ') && !portLine.isCompleted) {
        portLine.complete(int.parse(line.trim().split(' ').last));
      }
    }
  }

  process.stdout.listen((chunk) => absorb(chunk, fromStdout: true));
  process.stderr.listen((chunk) => absorb(chunk, fromStdout: false));
  final port = await portLine.future.timeout(const Duration(seconds: 60),
      onTimeout: () => throw StateError(
          '60s 没等到 PORT 行；输出尾部：${lines.take(8).join(' | ')}'));
  final response = await http
      .get(Uri.parse('http://127.0.0.1:$port/g3/source.json'))
      .timeout(const Duration(seconds: 20));
  return _Fixture(
      process: process,
      port: port,
      source: jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>,
      lines: lines);
}

/// 只数进出的字节，别的都转给真 client（与 `live_engine_test.dart` 同一形状）。
///
/// **按 action 数**：这一臂发了八发以上，一个总数就没法回答"reader.content 那一发到底
/// 多大"，也就拦不住"多发了什么"。
class _CountingClient extends http.BaseClient {
  _CountingClient(this._inner);

  final http.Client _inner;
  final Map<String, int> sentByAction = <String, int>{};
  final Map<String, int> receivedByAction = <String, int>{};

  String get perAction => sentByAction.keys.map((String action) {
        return '$action 出 ${sentByAction[action]}B 入 ${receivedByAction[action]}B';
      }).join(' · ');

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    final body = await request.finalize().toBytes();
    var action = '(没解出 action)';
    try {
      final decoded = jsonDecode(utf8.decode(body));
      if (decoded is Map<String, Object?>) action = '${decoded['action']}';
    } on FormatException {
      // 解不出来就留着占位：计数不许因为探针而改变行为。
    }
    sentByAction[action] = (sentByAction[action] ?? 0) + body.length;
    // 走 `Request..bodyBytes`：`Client.post(body: …)` 按 latin1 编字节，中文会花。
    final outbound = http.Request(request.method, request.url)
      ..followRedirects = request.followRedirects
      ..headers.addAll(request.headers)
      ..bodyBytes = body;
    final response = await http.Response.fromStream(await _inner.send(outbound));
    receivedByAction[action] =
        (receivedByAction[action] ?? 0) + response.bodyBytes.length;
    return http.StreamedResponse(Stream<List<int>>.value(response.bodyBytes),
        response.statusCode,
        headers: response.headers, reasonPhrase: response.reasonPhrase);
  }

  @override
  void close() => _inner.close();
}

/// 一趟实弹：假源 + 真引擎 + 真会话，读回那一章。
class _LiveShot {
  _LiveShot({
    required this.fixture,
    required this.started,
    required this.events,
    required this.subscription,
    required this.counter,
    required this.repository,
    required this.dataDir,
    required this.book,
    required this.document,
    required this.chapterCount,
  });

  final _Fixture fixture;
  final StartedEngine started;
  final EngineEventClient events;
  final StreamSubscription<EngineEvent> subscription;
  final _CountingClient counter;
  final RpcEngineRepository repository;
  final Directory dataDir;
  final BookRow book;
  final ChapterDocument document;
  final int chapterCount;

  List<String> get paragraphs => chapterParagraphs(document.blocks);
  List<int> get offsets => paragraphOffsets(paragraphs);

  /// 逐项注册清理：**成功路径**才用 addTearDown（失败路径由 [_shootOnce] 自己收摊，
  /// 否则半趟跑起来的两个子进程会把端口和临时目录留在盘上）。
  void registerTearDowns() {
    addTearDown(() async {
      await subscription.cancel();
      await events.close();
      counter.close();
      started.process.kill(ProcessSignal.sigterm);
      fixture.process.kill(ProcessSignal.sigterm);
      if (dataDir.existsSync()) dataDir.deleteSync(recursive: true);
    });
  }
}

Future<_LiveShot> _shootOnce() async {
  final fixture = await _startFixture();
  final dataDir = await Directory.systemTemp.createTemp('legodo-g3live-');
  final started = await EngineProcess.start(
      python: _python,
      mainPy: _mainPy,
      environment: <String, String>{
        // 空目录：这一臂建的源、入的书架、落的进度都不进真库。
        'LEGADO_BOOKSOURCE_DATA_DIR': dataDir.path,
        'LEGADO_BOOKSOURCE_CACHE_DIR': '${dataDir.path}/cache',
        'PYTHONIOENCODING': 'utf-8',
      });
  final counter = _CountingClient(http.Client());
  final repository = RpcEngineRepository(RpcClient(started.endpoint, client: counter));
  final events = EngineEventClient(started.endpoint);
  final frames = <EngineEvent>[];
  final subscription = events.events.listen(frames.add);
  Future<void> cleanup() async {
    await subscription.cancel();
    await events.close();
    counter.close();
    started.process.kill(ProcessSignal.sigterm);
    fixture.process.kill(ProcessSignal.sigterm);
    if (dataDir.existsSync()) dataDir.deleteSync(recursive: true);
  }

  try {
    await events.start();
    await repository.hello();
    final saved = await repository.saveSource(fixture.source);
    expect(saved.saved, isTrue, reason: 'source.save 没建起语料源：${saved.errors}');
    final accepted = await repository.searchStart(
        keyword: _keyword,
        scope: fixture.group,
        searchId: DateTime.now().millisecondsSinceEpoch % 1000000);
    expect(accepted.started, isTrue, reason: 'search.start 没受理：${accepted.action}');
    // 结果是**事件**交回来的（mode="job"：回包里只有受理凭据，没有一行书）。
    final frame = await _waitFor(frames,
        name: 'SearchFinishedEvent',
        where: (payload) =>
            payload['finished'] == true && (payload['items'] as List? ?? []).isNotEmpty);
    final book = BookRow.fromJson(
        (frame.payload['items']! as List).cast<Map<String, Object?>>().first);
    final session = await repository.openReader(
        sourceUrl: fixture.sourceUrl, book: book.raw, shelve: true, resume: true);
    final rows = await repository.chapterRows(
        sourceUrl: fixture.sourceUrl, bookUrl: session.bookUrl);
    expect(rows.length, session.chapterCount,
        reason: '会话报的章数与目录行数不是一个数：'
            '${session.chapterCount} vs ${rows.length}');
    final document = await repository.chapterDocument(
        sourceUrl: fixture.sourceUrl,
        bookUrl: session.bookUrl,
        chapterUrl: rows[_chapterIndex].url);
    return _LiveShot(
      fixture: fixture,
      started: started,
      events: events,
      subscription: subscription,
      counter: counter,
      repository: repository,
      dataDir: dataDir,
      book: book,
      document: document,
      chapterCount: session.chapterCount,
    );
  } catch (_) {
    await cleanup();
    rethrow;
  }
}

Future<EngineEvent> _waitFor(List<EngineEvent> frames,
    {required String name,
    required bool Function(Map<String, Object?> payload) where}) async {
  final deadline = DateTime.now().add(const Duration(seconds: 180));
  while (DateTime.now().isBefore(deadline)) {
    for (final frame in frames) {
      if (frame.name == name && where(frame.payload)) return frame;
    }
    await Future<void>.delayed(const Duration(milliseconds: 100));
  }
  throw StateError('等不到 $name（收到 ${frames.length} 帧：'
      '${frames.map((f) => f.name).take(12).join(',')}）');
}

void main() {
  test('靶子自己先要能打：假源子进程起得来、PORT 行与 source.json 同端口', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN：这一臂要真起子进程');
      return;
    }
    final fixture = await _startFixture();
    addTearDown(() => fixture.process.kill(ProcessSignal.sigterm));
    expect(fixture.port, greaterThan(0));
    expect(fixture.sourceUrl, 'http://127.0.0.1:${fixture.port}/g3',
        reason: '字典里的端口与 PORT 行不符 = 宿主照着连不上语料');
    // 正文页真在服务（这一臂只读它；其余路由由 Python 那侧第 5 笔守卫覆盖）。
    final page = await http
        .get(Uri.parse('${fixture.sourceUrl}/ch/2.html'))
        .timeout(const Duration(seconds: 20));
    expect(utf8.decode(page.bodyBytes), contains('<usehtml>'),
        reason: '页面没带 <usehtml>，这一臂验的就不是"两侧渲染口径是同一份"');
    expect(page.bodyBytes.length, greaterThan(1000),
        reason: '正文页短到这个程度，段切那一头就没东西可切了');
  }, timeout: const Timeout(Duration(minutes: 2)));

  test('实弹：真子进程读回的正文，Dart 那把尺与 flet 投影表同偏移', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN');
      return;
    }
    final projection = _loadJson('projection/reader.content.json');
    final live = await _shootOnce();
    live.registerTearDowns();

    final offsets = live.offsets;
    final want =
        (projection['offsets']! as List).cast<num>().map((v) => v.toInt()).toList();
    stdout.writeln('LIVE 第${_chapterIndex + 1}章 服务端 ${live.document.paragraphCount} 段 '
        '${live.document.totalChars} 字 · Dart ${live.paragraphs.length} 段 '
        '${chapterTextLength(live.paragraphs)} 字 · flet 投影 '
        '${projection['paragraph_count']} 段 ${projection['total_chars']} 字'
        ' · 目录 ${live.chapterCount} 章 · ${live.counter.perAction}');

    // ---- 三个读数必须同时对上：服务端算的、Dart 算的、flet 算的 ----
    expect(live.paragraphs.length, live.document.paragraphCount,
        reason: 'Dart 数出的段数与服务端交付那一格不等 = 两侧的段口径飘了');
    expect(chapterTextLength(live.paragraphs), live.document.totalChars,
        reason: '同一份块列表算出的章内字符流长度必须与 totalChars 相等');
    expect(offsets, want, reason: 'Dart 的落点表与 flet 投影表不是同一串数');
    expect(live.document.paragraphCount, projection['paragraph_count']);
    expect(live.document.totalChars, projection['total_chars']);

    // ---- 逐段：投影表每一行的原文都真能对上 Dart 排出来的那一段 ----
    final rows = (projection['rows']! as List).cast<Map<String, Object?>>();
    final pinned = rows.where((row) => row['paragraph_index'] != null).toList();
    expect(pinned, hasLength(live.paragraphs.length),
        reason: '投影表的段行数与 Dart 排出来的段数不等，逐段比对就只剩一半');
    for (final row in pinned) {
      final index = row['paragraph_index']! as int;
      expect(live.paragraphs[index], row['ruler'],
          reason: '第 $index 段两侧不是同一串字（Dart ${live.paragraphs[index].length} 字 vs '
              'flet ${(row['ruler'] as String).length} 字）');
      expect(offsets[index], offsetOfParagraph(live.paragraphs, index));
    }

    // ---- 交付形态那一头：实弹里媒体块是"字节 + 地址"，地址不许丢 ----
    // 离线守卫只能验到地址还在 value 那一格（不发请求就不会换成字节），这一格是它够不着的那一半。
    final media = live.document.blocks.where((block) => !block.isText).toList();
    expect(media, hasLength(1),
        reason: '插图那条臂没跑到：${live.document.blocks.map((b) => b.kind).toList()}');
    expect(media.single.binary, isTrue);
    expect(media.single.url, startsWith('http://127.0.0.1:'),
        reason: '引擎把图换成字节之后，客户端只能靠 url 那一格渲染它（设计 §4.4）');

    // ---- 落点往返：Dart 算出的偏移交给服务端，再原样读回来 ----
    final position = ReadingPosition(
      chapterUrl: live.document.chapterUrl,
      chapterName: live.document.chapterName,
      chapterIndex: live.document.chapterIndex,
      characterOffset: offsetOfParagraph(live.paragraphs, _pinnedParagraph),
      paragraphIndex: _pinnedParagraph,
      fontSize: 16,
      lastReadAt: DateTime.now().millisecondsSinceEpoch ~/ 1000,
    );
    final saved = await live.repository.savePosition(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        positionJson: position.toJson());
    expect(saved.characterOffset, offsets[_pinnedParagraph],
        reason: '范围内的偏移被改写了：落回 ${saved.characterOffset}，'
            'Dart 发的是 ${offsets[_pinnedParagraph]}');
    final restored = await live.repository.readPosition(
        sourceUrl: live.fixture.sourceUrl, bookUrl: live.book.url);
    expect(restored, isNotNull, reason: '刚落的一发读不回来（那是"从没读过"的形状）');
    expect(restored!.characterOffset, offsets[_pinnedParagraph],
        reason: '读回来的偏移与发出去的不是同一个数');
    expect(restored.paragraphIndex, _pinnedParagraph,
        reason: '段号对不上：偏移与段号是同一把尺的两端，必须互相算得出');
    // 与 flet 那份同一段的偏移并排：判据要的"可比"就是这个数相等，不是"看着差不多"。
    expect(restored.characterOffset, want[_pinnedParagraph],
        reason: '往返读回来的偏移与 flet 投影表第 $_pinnedParagraph 段的落点不是同一个数');

    // ---- 正对照：越界那一发必须真被夹回来，且夹完仍不超过这把尺 ----
    final overflow = await live.repository.savePosition(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        positionJson: <String, Object?>{
          ...position.toJson(),
          'chapter_pos': live.document.totalChars + 9999,
          'chapter_paragraph': live.paragraphs.length + 40,
          'scroll_ratio': 3.5,
        });
    expect(overflow.characterOffset, lessThanOrEqualTo(live.document.totalChars));
    expect(overflow.paragraphIndex, live.paragraphs.length - 1);
    expect(overflow.characterOffset, isNot(saved.characterOffset),
        reason: '越界那发的读数与正常那发一模一样 = 夹回这条臂根本没跑到');

    // ---- 正对照（仪器）：这几发真发出去了，比对不是拿替身糊出来的 ----
    expect(live.counter.receivedByAction.keys, containsAll(<String>[
      'engine.hello', 'source.save', 'search.start', 'reader.open', 'reader.toc',
      'reader.content', 'reader.position_save', 'reader.position_get',
    ]));
    expect(live.counter.receivedByAction['reader.content']!, greaterThan(2000),
        reason: '正文那一发的 HTTP 体比一章的字数还小 = 载荷被截了或发错了 action');
  }, timeout: const Timeout(Duration(minutes: 6)));

  test('收摊：engine.shutdown 之后两个子进程都退，临时目录不留在盘上', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN');
      return;
    }
    final live = await _shootOnce();
    final dataDirPath = live.dataDir.path;
    final port = live.started.endpoint.port;
    await live.repository.deleteSource(live.fixture.sourceUrl);
    await live.repository.shutdown(reason: 'g3 live arm done');
    await live.subscription.cancel();
    await live.events.close();
    live.counter.close();
    live.fixture.process.kill(ProcessSignal.sigterm);

    final rc = await live.started.process.exitCode
        .timeout(const Duration(seconds: 60),
            onTimeout: () => throw StateError('发了 engine.shutdown，60s 内进程没退'));
    expect(rc, 0, reason: '非 0 = 无头入口收尾没走干净');
    // 假源那一发也要有退出凭据，不是"我杀了它"就算收了。
    await live.fixture.process.exitCode
        .timeout(const Duration(seconds: 30),
            onTimeout: () => throw StateError('假源收到 SIGTERM 之后 30s 没退'));

    // 端口真放掉没有：还连着上就是"停掉的只是 shell、python 还占着端口"那一类
    // （记忆 background-server-keeps-port）。
    var refused = false;
    try {
      final probe =
          await Socket.connect('127.0.0.1', port, timeout: const Duration(seconds: 3));
      probe.destroy();
    } on SocketException {
      refused = true;
    }
    expect(refused, isTrue, reason: '引擎退了但端口还占着 = 还有别的进程没退');
    // 正对照：临时目录真在被用（收摊前先确认它存在，否则上面那句 deleteSync 可能删的是空气）。
    expect(Directory(dataDirPath).existsSync(), isTrue,
        reason: '引擎跑完连临时目录都没有 = 这一臂的 LEGADO_BOOKSOURCE_DATA_DIR 没生效');
    Directory(dataDirPath).deleteSync(recursive: true);
    expect(Directory(dataDirPath).existsSync(), isFalse);
  }, timeout: const Timeout(Duration(minutes: 5)));
}
