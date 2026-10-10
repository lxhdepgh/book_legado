/// G3-D1 反向 WebView 实弹臂：**Dart 侧当宿主**，真引擎把一次渲染推过来，正文四段过线。
///
/// §9 门 G3 第二条判据「R1 涉及的源在真机上读到正文」。这一臂与 Python 侧那一臂
/// （`.probe/g3d1_reverse_channel_live.py`）读的是**同一份语料、同一张锚点表**，
/// 只是宿主换成了 Dart：
///
/// - 引擎那边照旧走它自己的路：`.list@html` → 无头内核渲染 → 那页**看着像要人**
///   （登录墙：`block_pages.looks_like_gate`）→ 内核那一版不交，改从 `/ws` 推 `request` 帧。
/// - 宿主这边是 [ScriptedWebviewHost]：它按 `?rendered=1` 去语料取"渲染完之后"的那一页
///   ——**引擎永远不会自己去取那一版**，所以"四段/1961 字"这个读数只可能是宿主交回来的。
///   第三条腿是语料自己记的取页账（`/hits.json`），不是宿主自报。
/// - 参照物是 `tests/gateway/projection/reader.content.webview.json`：两种形状各过一遍
///   纯管线算出来的锚点表。两边都读 Dart 自己算的数就成了自证（G1 那笔账）。
///
/// 三条仪器规矩：全程 127.0.0.1（零外网，记忆 `legado-live-scan-av-block`）；
/// 两个子进程的输出从头抽干（记忆 `subprocess-probe-must-drain-stdout`）；
/// 先决读数**当场测**——这台机器没装无头内核时整条反向通道根本不会响
/// （`main.py:_engine_browser_fetchers` 直接退回纯 HTTP），那时候"宿主没被叫到"
/// 与"通道断了"是同一个形状，所以那一臂出声跳过而不是绿着过。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/engine_event_client.dart';
import 'package:legodo_client/core/events/webview_request.dart';
import 'package:legodo_client/core/rpc/engine_process.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';
import 'package:legodo_client/core/webview/webview_host.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/reader/domain/text_layout.dart';

final String? _python = Platform.environment['LEGODO_PYTHON'];
final String? _mainPy = Platform.environment['LEGODO_MAIN'];
final bool _live = _python != null && _mainPy != null;

final String _repoRoot = Directory.current.parent.path;

Map<String, Object?> _loadJson(String relative) => jsonDecode(
        File('$_repoRoot/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _anchor() =>
    _loadJson('tests/gateway/projection/reader.content.webview.json');

/// 先决读数：这台机器有没有无头内核。没有则整条反向通道不会响（见文件头）。
Future<({bool installed, String path})> _kernelReading() async {
  final result = await Process.run(_python!, <String>[
    '-c',
    'import json;from app.services.headless_browser import browsers_path, '
        'chromium_installed;'
        'print("KERNEL"+json.dumps({"installed":chromium_installed(),'
        '"path":str(browsers_path())}))'
  ], workingDirectory: _repoRoot, environment: <String, String>{
    'PYTHONIOENCODING': 'utf-8',
  });
  final line = (stdoutOf(result)).firstWhere((l) => l.startsWith('KERNEL'),
      orElse: () => throw StateError(
          '没读到 KERNEL 行（rc=${result.exitCode}）：${result.stderr}'));
  final reading =
      jsonDecode(line.substring('KERNEL'.length)) as Map<String, Object?>;
  return (
    installed: reading['installed'] == true,
    path: '${reading['path'] ?? ''}',
  );
}

List<String> stdoutOf(ProcessResult result) =>
    LineSplitter().convert('${result.stdout}');

class _Fixture {
  _Fixture({required this.process, required this.port, required this.source});

  final Process process;
  final int port;
  final Map<String, Object?> source;

  String get baseUrl => 'http://127.0.0.1:$port';
  String get sourceUrl => '${source['bookSourceUrl']}';
  String get group => '${source['bookSourceGroup']}';

  /// 语料自己记的取页账（谁在什么时候取过哪一页，由被取的一方记账）。
  Future<Map<String, Object?>> hits() async {
    final response = await http
        .get(Uri.parse('$baseUrl/hits.json'))
        .timeout(const Duration(seconds: 20));
    return jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>;
  }
}

Future<_Fixture> _startFixture() async {
  final process = await Process.start(_python!,
      <String>['$_repoRoot/tests/gateway/fake_source_reader.py', '--port', '0'],
      workingDirectory: _repoRoot);
  final lines = <String>[];
  final portLine = Completer<int>();
  void absorb(List<int> bytes, {required bool fromStdout}) {
    for (final line in const LineSplitter().convert(utf8.decode(bytes, allowMalformed: true))) {
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
      .get(Uri.parse('http://127.0.0.1:$port/g3web/source.json'))
      .timeout(const Duration(seconds: 20));
  return _Fixture(
      process: process,
      port: port,
      source: jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>);
}

/// 只数进出的字节，别的都转给真 client（与 `reader_live_arm_test.dart` 同一形状）。
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

/// 一趟实弹：语料 + 真引擎 + Dart 宿主。[answering] 假 = 宿主在场但不出声。
class _LiveShot {
  _LiveShot({
    required this.fixture,
    required this.started,
    required this.events,
    required this.subscription,
    required this.hostSubscription,
    required this.counter,
    required this.repository,
    required this.dataDir,
    required this.host,
    required this.hostErrors,
    required this.book,
    required this.chapterCount,
    required this.tocRowUrl,
  });

  final _Fixture fixture;
  final StartedEngine started;
  final EngineEventClient events;
  final StreamSubscription<EngineEvent> subscription;
  final StreamSubscription<WebviewRequest> hostSubscription;
  final _CountingClient counter;
  final RpcEngineRepository repository;
  final Directory dataDir;
  final ScriptedWebviewHost host;
  final List<Object> hostErrors;
  final BookRow book;
  final int chapterCount;

  /// 目录那一行**原样**的 url（还挂着 `,{"webView":true}`）：引擎发给宿主的是拆掉
  /// 选项之后的那一条，所以这一格与 [requestedUrl] 不同是**对的**，不同才有得比对。
  final String tocRowUrl;

  /// 宿主被叫到的那一条正文页地址（锚点表里那一个，端口换成本趟的真端口）。
  String get requestedUrl => tocRowUrl.split(',').first;

  /// 逐项注册清理：**成功路径**才用 addTearDown（失败路径由 [_shoot] 自己收摊）。
  ///
  /// 收尾顺序里那一句 `engine.shutdown` 不是礼貌：直接 SIGTERM 会留下**孤儿无头内核**
  /// ——引擎杀了，它拉起来的 chromium 还攥着 cache 目录里的浏览器 profile，于是
  /// "删临时目录"报的是 `另一个程序正在使用此文件`（errno 32），读起来像测试失败、
  /// 其实是收尾抢跑。`hub.shutdown()` 那条路会关掉整棵子进程树。
  void registerTearDowns() {
    addTearDown(() async {
      await subscription.cancel();
      await hostSubscription.cancel();
      await _tryShutdown(repository, 'g3d1 arm done');
      await events.close();
      counter.close();
      started.process.kill(ProcessSignal.sigterm);
      fixture.process.kill(ProcessSignal.sigterm);
      await _settled(started.process);
      await _settled(fixture.process);
      _deleteDataDir(dataDir);
    });
  }
}

/// 优雅收摊：那一发发不出去就退回 SIGTERM，**只打印不抛**。
///
/// 这一臂的读数是正文那四段；把收尾的错抛在 `addTearDown` 上，测试红了读到的却是
/// "engine.shutdown 没成"，与正文没关系。反过来这条链断没断照样能从行里看出来。
Future<void> _tryShutdown(RpcEngineRepository repository, String reason) async {
  try {
    await repository.shutdown(reason: reason).timeout(const Duration(seconds: 25));
  } on Object catch (exc) {
    stdout.writeln('G3D1 收尾：engine.shutdown 没成（$exc），退回直接杀');
  }
}

/// 等退出，等不到也只打印：调用方紧接着要 kill，端口/进程留下的账由 [_deleteDataDir]
/// 那一句和日志一起兜住，不该在这里把测试判红。
Future<void> _settled(Process process) async {
  try {
    await process.exitCode.timeout(const Duration(seconds: 30));
  } on TimeoutException {
    stdout.writeln('G3D1 收尾：杀了之后 30s 没退（pid=${process.pid}）');
  }
}

/// 尽力删，带短重试。
///
/// 两件事分开：`engine.shutdown` 那条路会关掉整棵子进程树（这台机器跑完 `tasklist`
/// 查不到残留的内核），而**目录句柄的释放比引擎进程退出还晚一拍**——退出当场删会读到
/// `另一个程序正在使用此文件（errno 32）`。重试兜的就是那一拍，不是孤儿进程。
/// 删不掉只打印，不抛：这条臂的读数是正文那四段，遗留一个临时目录不该把它判红。
void _deleteDataDir(Directory dataDir, {int attempts = 20}) {
  if (!dataDir.existsSync()) return;
  for (var i = 1;; i++) {
    try {
      dataDir.deleteSync(recursive: true);
      stdout.writeln('G3D1 收尾：临时目录已删 ${dataDir.path}（第 $i 次尝试）');
      return;
    } on FileSystemException catch (exc) {
      if (i >= attempts) {
        stdout.writeln('G3D1 收尾：$attempts 次还删不掉，遗留 ${dataDir.path}（$exc）'
            '——那是无头内核还攥着 profile，与正文读到没有没关系');
        return;
      }
      sleep(const Duration(milliseconds: 500));
    }
  }
}

Future<_LiveShot> _shoot({required bool answering}) async {
  final fixture = await _startFixture();
  final dataDir = await Directory.systemTemp.createTemp('legodo-g3d1-');
  final started = await EngineProcess.start(
      python: _python,
      mainPy: _mainPy,
      environment: <String, String>{
        'LEGADO_BOOKSOURCE_DATA_DIR': dataDir.path,
        'LEGADO_BOOKSOURCE_CACHE_DIR': '${dataDir.path}/cache',
        'PYTHONIOENCODING': 'utf-8',
      });
  final counter = _CountingClient(http.Client());
  final repository =
      RpcEngineRepository(RpcClient(started.endpoint, client: counter));
  final events = EngineEventClient(started.endpoint);
  final frames = <EngineEvent>[];
  final subscription = events.events.listen(frames.add);
  final hostErrors = <Object>[];
  // 宿主那一侧：脚本化作答 = 按 `?rendered=1` 问语料要"渲染完之后"的那一页。
  // 这一发**只有宿主会发**（引擎那侧的墙是另一条 url），所以正文里出现四段
  // 就只能是宿主交回来的，不可能是引擎自己 HTTP 到的。
  final host = ScriptedWebviewHost((request) async {
    if (!answering) return null;
    final page = await http
        .get(Uri.parse('${request.url}?rendered=1'))
        .timeout(const Duration(seconds: 20));
    return utf8.decode(page.bodyBytes);
  });
  StreamSubscription<WebviewRequest>? hostSubscription;
  Future<void> cleanup() async {
    // 失败路径自己收摊：这里每一步都尽力，**绝不在这条路上抛**。半趟断掉的原始那个错
    // （多半是某条 expect）比临时目录值钱得多——被收尾的错顶掉就再也查不着真因了。
    await hostSubscription?.cancel();
    await subscription.cancel();
    await _tryShutdown(repository, 'g3d1 arm 失败路径');
    await events.close();
    counter.close();
    started.process.kill(ProcessSignal.sigterm);
    fixture.process.kill(ProcessSignal.sigterm);
    await _settled(started.process);
    await _settled(fixture.process);
    _deleteDataDir(dataDir);
  }

  try {
    await events.start();
    // attach 必须赶在任何 `reader.content` 之前：那条口是 broadcast，没人听时帧就丢了
    // ——表现是引擎白等 20s，而排查的人会以为是 WebView 那一侧没接上。
    hostSubscription = attachWebviewHost(
      requests: events.webviewRequests,
      repository: repository,
      host: host,
      onError: (error, _) => hostErrors.add(error),
    );
    await repository.hello();
    final saved = await repository.saveSource(fixture.source);
    expect(saved.saved, isTrue, reason: 'source.save 没建起反查源：${saved.errors}');
    final anchor = _anchor();
    final accepted = await repository.searchStart(
        keyword: '${anchor['keyword']}',
        scope: fixture.group,
        searchId: DateTime.now().millisecondsSinceEpoch % 1000000);
    expect(accepted.started, isTrue, reason: 'search.start 没受理：${accepted.action}');
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
    // 目录里那一行的 href 得与锚点表那条**逐字符相同**（选项挂在 url 上，
    // 引擎拆掉它之后才发给宿主）：写法飘了，走的可能就不是反向那一条路。
    final tocRowUrl = rows[anchor['chapter_index'] as int].url;
    expect(tocRowUrl, '${fixture.baseUrl}${anchor['toc_href']}',
        reason: '目录行的 url 与锚点表那条不符（表是从语料目录页按 ruleToc 解析出来的）');
    final shot = _LiveShot(
      fixture: fixture,
      started: started,
      events: events,
      subscription: subscription,
      hostSubscription: hostSubscription,
      counter: counter,
      repository: repository,
      dataDir: dataDir,
      host: host,
      hostErrors: hostErrors,
      book: book,
      chapterCount: session.chapterCount,
      tocRowUrl: tocRowUrl,
    );
    return shot;
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
  test('先决读数：内核在场 + 语料那两种形状各自取得到', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN：这一臂要真起子进程');
      return;
    }
    final kernel = await _kernelReading();
    stdout.writeln('G3D1 内核：installed=${kernel.installed} @ ${kernel.path}');
    expect(kernel.installed, isTrue,
        reason: '没有无头内核时 `_engine_browser_fetchers` 返回 None，webView 那一族直接'
            '退回纯 HTTP，反向通道压根不会响——这一臂的读数与"通道断了"同形');

    final fixture = await _startFixture();
    addTearDown(() => fixture.process.kill(ProcessSignal.sigterm));
    final anchor = _anchor();
    final wall = await http
        .get(Uri.parse('${fixture.baseUrl}/g3web/ch/1.html'))
        .timeout(const Duration(seconds: 20));
    final rendered = await http
        .get(Uri.parse('${fixture.baseUrl}/g3web/ch/1.html?rendered=1'))
        .timeout(const Duration(seconds: 20));
    final wallText = utf8.decode(wall.bodyBytes);
    final renderedText = utf8.decode(rendered.bodyBytes);
    expect(wallText, contains('type="password"'),
        reason: '墙那一版没有登录框，looks_like_gate 就不会把这一发交给宿主');
    expect(renderedText, isNot(wallText),
        reason: '两种形状是同一页的话，"四段"就读不出是宿主交的还是引擎自己取的');
    final hostShape = anchor['host']! as Map<String, Object?>;
    final wallShape = anchor['wall']! as Map<String, Object?>;
    expect(hostShape['paragraph_count'], isNot(wallShape['paragraph_count']),
        reason: '锚点表自己得能分出这两版，否则下面的比对是恒真的');
    expect(renderedText, contains('<usehtml>'),
        reason: '宿主那一版带着渲染标记，正好是"两侧渲染口径同一份"那一臂');
  }, timeout: const Timeout(Duration(minutes: 2)));

  test('实弹：引擎求一次渲染，Dart 宿主答回去，正文四段与锚点表同偏移', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN');
      return;
    }
    if (!(await _kernelReading()).installed) {
      markTestSkipped('这台机器没有无头内核：反向通道不会响（见上一臂的先决读数）');
      return;
    }
    final anchor = _anchor();
    final hostShape = anchor['host']! as Map<String, Object?>;
    final wallShape = anchor['wall']! as Map<String, Object?>;
    final live = await _shoot(answering: true);
    live.registerTearDowns();

    final hitsBefore = await live.fixture.hits();
    final stopwatch = Stopwatch()..start();
    final document = await live.repository.chapterDocument(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        // 发的是目录那一行**原样**的 url（选项还挂在后面）：引擎按这一串认章，
        // 拆掉选项是它取页之前自己那一步——拆完那一条正是发给宿主的地址。
        chapterUrl: live.tocRowUrl);
    stopwatch.stop();
    final hitsAfter = await live.fixture.hits();

    final paragraphs = chapterParagraphs(document.blocks);
    final offsets = paragraphOffsets(paragraphs);
    final wantOffsets =
        (hostShape['offsets']! as List).cast<num>().map((v) => v.toInt()).toList();
    stdout.writeln('G3D1 宿主作答：墙钟 ${stopwatch.elapsedMilliseconds}ms · '
        '服务端 ${document.paragraphCount} 段 ${document.totalChars} 字 · '
        'Dart ${paragraphs.length} 段 ${chapterTextLength(paragraphs)} 字 · '
        '锚点 ${hostShape['paragraph_count']} 段 ${hostShape['total_chars']} 字 · '
        '宿主被叫到 ${live.host.calls.length} 次 · ${live.counter.perAction}');

    // ---- 宿主真被叫到了，而且叫它的是那一条**拆掉选项之后**的正文页地址 ----
    expect(live.host.calls, isNotEmpty,
        reason: '一条 request 帧都没收到：引擎要么没走反向那条路，要么 attach 赶在请求之后了');
    final request = live.host.calls.first;
    expect(request.requestId, isNotEmpty);
    expect(request.url, live.requestedUrl,
        reason: '引擎发给宿主的地址与目录那一行拆出来的不是同一条：${request.url} vs '
            '${live.requestedUrl}（选项挂在 url 上，拆干净了才发得出去）');
    expect(request.wantsWebView, isTrue,
        reason: 'options 里带不出 webView，宿主就没法判断该不该渲染：${request.options}');
    expect(Uri.parse(request.url).path, Uri.parse('${anchor['chapter_url']}').path,
        reason: '宿主被叫去的那一页与锚点表钉的那一页不是同一章——两侧比的就不是同一把尺');
    expect(request.sourceUrl, live.fixture.sourceUrl,
        reason: '宿主按域取 Cookie 靠的就是 source_url 那一格');

    // ---- 三方同数：服务端交付的 / Dart 排出来的 / 锚点表（纯管线）算出来的 ----
    expect(paragraphs.length, document.paragraphCount);
    expect(chapterTextLength(paragraphs), document.totalChars);
    expect(document.paragraphCount, hostShape['paragraph_count'],
        reason: '读回的不是宿主那一版（墙那一版是 ${wallShape['paragraph_count']} 段 '
            '${wallShape['total_chars']} 字）= 引擎自己 HTTP 到了页面');
    expect(document.totalChars, hostShape['total_chars']);
    expect(offsets, wantOffsets, reason: 'Dart 的落点与锚点表不是同一串数');

    // ---- 逐段：锚点表那一行行的原文头与长度，Dart 这边排出来必须一字不差 ----
    final rows = (hostShape['rows']! as List).cast<Map<String, Object?>>();
    expect(rows, hasLength(paragraphs.length));
    for (final row in rows) {
      final index = row['index']! as int;
      expect(paragraphs[index], startsWith('${row['head']}'),
          reason: '第 $index 段两侧不是同一串字');
      expect(paragraphs[index].length, row['length'],
          reason: '第 $index 段长度不等：Dart ${paragraphs[index].length} vs '
              '锚点 ${row['length']}（<usehtml> 那一族渲染改的就是字数）');
    }
    final wallHead =
        '${(wallShape['rows']! as List).cast<Map<String, Object?>>().first['head']}';
    expect(paragraphs.join(), isNot(contains(wallHead)),
        reason: '墙那一版的"$wallHead"混在正文里 = 交回的是内核那一版');
    expect(document.title, hostShape['title'],
        reason: '章名那一格也要是宿主那一版的：读回墙就说明整条路走岔了');

    // ---- 取页账（语料自己记的，不是宿主自报）：宿主那条 `?rendered=1` 真被取过 ----
    final wallHits = _delta(hitsBefore, hitsAfter, '/g3web/ch/1.html');
    final renderedHits = _delta(hitsBefore, hitsAfter, '/g3web/ch/1.html?rendered=1');
    stdout.writeln('G3D1 取页账：墙 +$wallHits · rendered +$renderedHits');
    expect(renderedHits, greaterThanOrEqualTo(1),
        reason: '宿主没去取渲染完的那一版，四段正文就只能来自别处——这条臂就成了自证');
    expect(wallHits, greaterThanOrEqualTo(1),
        reason: '引擎那侧连墙都没渲染过，说明它压根没走到"这页要人"那一步');

    // ---- 交回这一发真出过线，而且带的是正文不是空串 ----
    expect(live.counter.receivedByAction.keys,
        containsAll(<String>['engine.hello', 'source.save', 'search.start',
          'reader.open', 'reader.toc', 'reader.content', 'engine.webContentReady']));
    expect(live.counter.sentByAction['engine.webContentReady']!, greaterThan(2000),
        reason: '宿主那一发出站体还没一章长 = 正文没真过线');
    // 宿主这条链上不该有任何"出声"：答晚了、空正文、交回失败都会进 hostErrors。
    expect(live.hostErrors, isEmpty, reason: '${live.hostErrors.map((e) => '$e').toList()}');
    expect(live.host.calls.length, 1,
        reason: '这一次动作只该求一次渲染（预取那一次是另一档，见 D 臂）');

    // ---- 答给一条没人等的 request_id：那是 delivered:false，不是异常 ----
    // 引擎按 20s 走人之后晚到的答案就是这个形状；折成异常会让人去查 WebView 而不是查预算。
    final late = await live.repository.webContentReady(
        requestId: 'w-没有这一发', body: '<html>没人等</html>');
    expect(late.delivered, isFalse,
        reason: '正对照：delivered 读的是现场（没人等这一发），不是恒 true 的比较口'
            '（记忆 comparison-closure-must-reread-live）');

    // ---- 同一章再读一次：走缓存，不再叫宿主 ----
    final before = live.host.calls.length;
    final again = await live.repository.chapterDocument(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        chapterUrl: live.tocRowUrl);
    expect(again.totalChars, document.totalChars);
    expect(live.host.calls.length, before,
        reason: '同章再读又去叫宿主 = 那一发压根没落到缓存里');

    // ---- 预取那一档：`reader.content` 返回之后引擎会预取后面的章，
    // 它们走的是**同一条**反向通道。这一臂只确认第 2 章也被宿主答过一次。
    await Future<void>.delayed(const Duration(seconds: 3));
    final prefetch =
        live.host.calls.where((call) => call.url.endsWith('/ch/2.html')).toList();
    stdout.writeln('G3D1 预取那一档：宿主共被叫到 ${live.host.calls.length} 次，'
        '其中第 2 章 ${prefetch.length} 次');
    expect(prefetch, isNotEmpty,
        reason: '预取没走反向通道：那条路上的 `<useweb>` 章会一直停在墙上');
    final settled = await live.fixture.hits();
    expect(_delta(hitsAfter, settled, '/g3web/ch/2.html?rendered=1'),
        greaterThanOrEqualTo(1),
        reason: '第 2 章那一发宿主答了却没去取页 = 交回的不是那一章的正文');
  }, timeout: const Timeout(Duration(minutes: 8)));

  test('宿主在场但不出声：报错归给宿主那 20s，不归给 reader.content 的 60s 预算', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_PYTHON / LEGODO_MAIN');
      return;
    }
    if (!(await _kernelReading()).installed) {
      markTestSkipped('这台机器没有无头内核：反向通道不会响');
      return;
    }
    final anchor = _anchor();
    expect(anchor['chapter_count'], 2, reason: '预取那一档要第 2 章在场');
    final live = await _shoot(answering: false);
    live.registerTearDowns();
    final stopwatch = Stopwatch()..start();
    final failure = await _capture(() => live.repository.chapterDocument(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        chapterUrl: live.tocRowUrl));
    stopwatch.stop();
    stdout.writeln('G3D1 不作答：墙钟 ${stopwatch.elapsedMilliseconds}ms · '
        '宿主被叫到 ${live.host.calls.length} 次 · 错："${failure.message}"');

    expect(failure.kind, EngineFailureKind.engine,
        reason: '服务端把这一发折成了引擎错误信封，不是客户端超时');
    expect(failure.message, contains('宿主在'),
        reason: '报错没指着宿主 = 读表的人会去查 reader.content 的预算');
    expect(failure.message, contains('20s'));
    expect(failure.message, isNot(contains('超过服务端预算')),
        reason: '这条错曾经被折成"reader.content 超过服务端预算 60s"（墙钟 21s 报 60s，'
            '指着错的预算）。归错类的修法在服务端 `request_web_content`，这一臂钉它别再回来');
    expect(stopwatch.elapsedMilliseconds, greaterThanOrEqualTo(17000),
        reason: '没等满宿主那条预算就说明响的不是它');
    expect(live.host.calls, hasLength(1),
        reason: '帧得真送到宿主面前——"不作答"是宿主收到了不说话，不是压根没送到');
    expect(live.counter.receivedByAction['engine.webContentReady'] ?? 0, 0,
        reason: '这一臂一发都不该交回（交了就是作答那一臂）');
  }, timeout: const Timeout(Duration(minutes: 7)));
}

/// 把 Future 上的错误取成 [EngineFailure]：这一臂要读的是**消息归属**，不是它抛没抛。
Future<EngineFailure> _capture(Future<Object?> Function() body) async {
  try {
    await body();
  } on EngineFailure catch (exc) {
    return exc;
  }
  throw StateError('宿主不作答那一发本该报错，却成功了');
}

/// 取页账的差量：语料是**跨臂共用**的一本账（同一进程从头记到尾），所以只能比差。
/// Python 侧那条守卫比的是绝对数——那里每一发都是新起的子进程。
int _delta(Map<String, Object?> before, Map<String, Object?> after, String key) {
  int read(Map<String, Object?> ledger) =>
      (ledger[key] as num? ?? 0).toInt();
  return read(after) - read(before);
}
