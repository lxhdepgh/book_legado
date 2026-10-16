/// G3-D1 webView 边界臂（FFI 版）：那一族章在 FFI 下读到的是**墙**，反向通道不存在
/// ——这一臂把这条边界钉死，不再演旧的"宿主作答"那条戏。
///
/// 历史：网关时代这一臂演的是反向通道（`.probe/g3d1_reverse_channel_live.py` 的
/// Dart 翻版）：引擎无头渲染撞上登录墙 → 事件通道推 `request` 帧 → 脚本化宿主按
/// `?rendered=1` 取"渲染完"的那一页交回 → 正文四段 1961 字。FFI 单进程之后这条
/// 通道在**两侧**都没有落点：Go 侧 `doWebContentReady` 恒回 `{"delivered": false}`
/// （没有在等的请求可满足），Dart 侧也没有 `webviewRequests` 那条口可 attach。
///
/// 所以判据反过来钉边界：
/// - 正文读回来的**只能是墙**（锚点表的 wall：1 段 8 字「阅读正文需要登录」）——
///   引擎把挂在 url 上的 `,{"webView":true}` 原样带去 HTTP 取页（语料按数字路由，
///   容忍没拆干净的整条地址；`fake_source_reader.py` 那一段注释点名了这用法），
///   读到什么交什么：不渲染、不问人。
/// - 语料自己记的取页账里 `?rendered=1` 那一族**全程为 0**，而正控（同章的墙那一发
///   ≥1）在**同一本账**里——谁哪天在 FFI 里把反向通道做活了，这个 0 当场变非 0，
///   逼着把"宿主作答 → 四段"那组断言续回来。
/// - `engine_web_content_ready` 交一发没人等的答案 → `delivered:false`，不抛。
///
/// 三条仪器规矩：全程 127.0.0.1（零外网，记忆 `legado-live-scan-av-block`）；假源子
/// 进程输出从头抽干（记忆 `subprocess-probe-must-drain-stdout`）；"没人渲染过"这个 0
/// 由"墙真被取过"撑着（记忆 `zero-counts-need-a-positive-control`）。
///
/// 开关：`LEGODO_ENGINE_DLL` + `LEGADO_BOOKSOURCE_DATA_DIR` + `LEGODO_PYTHON` + Windows
/// （FFI 的内存分配器目前只实现 kernel32）。少一个就整支跳过——`.probe/ffi_live_run.py`
/// 一次配齐（连 DLL 一起编好）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/domain/text_layout.dart';

final String? _dll = Platform.environment['LEGODO_ENGINE_DLL'];
final String? _dataDir = Platform.environment['LEGADO_BOOKSOURCE_DATA_DIR'];
final String? _python = Platform.environment['LEGODO_PYTHON'];

/// 活臂开关：DLL + 语料目录 + python（起假源子进程）都得给，且都得真指到东西上。
bool get _live =>
    _dll != null &&
    _dataDir != null &&
    _python != null &&
    Platform.isWindows &&
    File(_dll!).existsSync() &&
    Directory(_dataDir!).existsSync();

final String _repoRoot = Directory.current.parent.path;

Map<String, Object?> _loadJson(String relative) => jsonDecode(
        File('$_repoRoot/tests/gateway/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _anchor() =>
    _loadJson('projection/reader.content.webview.json');

/// 起起来的语料假源。
class _Fixture {
  _Fixture(
      {required this.process, required this.port, required this.source, required this.lines});

  final Process process;
  final int port;
  final Map<String, Object?> source;
  final List<String> lines;

  String get baseUrl => 'http://127.0.0.1:$port';
  String get sourceUrl => '${source['bookSourceUrl']}';

  /// 语料自己记的取页账（路径 + 是否渲染版 → 次数），由被取的一方记账。
  Future<Map<String, Object?>> hits() async {
    final response = await http
        .get(Uri.parse('$baseUrl/hits.json'))
        .timeout(const Duration(seconds: 20));
    return jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>;
  }
}

/// 把假源当子进程拉起来，读它的 `PORT` 行，再问它要 `/g3web` 那一支的书源字典。
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
      .get(Uri.parse('http://127.0.0.1:$port/g3web/source.json'))
      .timeout(const Duration(seconds: 20));
  return _Fixture(
      process: process,
      port: port,
      source: jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>,
      lines: lines);
}

/// 取页账里某一页的两栏：墙（无查询串）与渲染版（`?rendered=1`）各被取了几次。
///
/// 键按**前缀**归族、不钉整串：引擎带的地址尾巴上还挂着 `,{"webView":true}`
/// （% 转义与否是传输层的事，语料按解码后的 `request.path` 记账），钉形状会把
/// "仪器没接上"误读成"这个形状没人取"。
Map<String, int> _hitsFor(Map<String, Object?> ledger, String pagePath) {
  var wall = 0;
  var rendered = 0;
  ledger.forEach((key, value) {
    if (!key.startsWith(pagePath)) return;
    final count = (value as num?)?.toInt() ?? 0;
    if (key.contains('?rendered=1')) {
      rendered += count;
    } else {
      wall += count;
    }
  });
  return <String, int>{'wall': wall, 'rendered': rendered};
}

/// 一趟实弹：假源 + 进程内真引擎 + 真会话，读回锚点那一章。
class _LiveShot {
  _LiveShot({
    required this.fixture,
    required this.adapter,
    required this.subscription,
    required this.frames,
    required this.book,
    required this.tocRowUrl,
    required this.document,
  });

  final _Fixture fixture;
  final FFIEngineAdapter adapter;
  final StreamSubscription<EngineEvent> subscription;
  final List<EngineEvent> frames;
  final BookRow book;

  /// 目录那一行**原样**的 url（`,{"webView":true}` 还挂在后面，只是被 %-转义）。
  /// FFI 这条路不拆它：检查的正是"引擎原样带着它去取页"。
  final String tocRowUrl;

  final ChapterDocument document;

  List<String> get paragraphs => chapterParagraphs(document.blocks);

  /// 逐项注册清理：**成功路径**才用 addTearDown（失败路径由 [_shootOnce] 自己收摊）。
  /// 先退订再 disconnect：poll 定时器还活着的话，收摊后每一拍都会往流里塞
  /// "hub not initialized"，把读数淹掉。
  void registerTearDowns() {
    addTearDown(() async {
      await subscription.cancel();
      await adapter.disconnect();
      fixture.process.kill(ProcessSignal.sigterm);
    });
  }
}

Future<_LiveShot> _shootOnce() async {
  final fixture = await _startFixture();
  final adapter = FFIEngineAdapter();
  await adapter.connect();
  final frames = <EngineEvent>[];
  final subscription = adapter.events().listen(frames.add);
  Future<void> cleanup() async {
    await subscription.cancel();
    await adapter.disconnect();
    fixture.process.kill(ProcessSignal.sigterm);
  }

  try {
    final saved = await adapter.saveSource(fixture.source);
    expect(saved.saved, isTrue, reason: 'source_save 没建起反查源：${saved.errors}');
    // 建源的回话只是"这一发没报错"，源真在不在要按 url 读回来。
    final loaded = await adapter.loadSource(fixture.sourceUrl);
    expect(loaded, isNotNull,
        reason: 'source_save 之后按 url 读不回这条源 = 建源这一发没落到事情上');
    final anchor = _anchor();
    final accepted = await adapter.searchStart(
        keyword: '${anchor['keyword']}',
        scope: '${anchor['group']}',
        searchId: DateTime.now().millisecondsSinceEpoch % 1000000);
    expect(accepted.started, isTrue, reason: 'search_start 没受理：${accepted.action}');
    final frame = await _waitFor(frames,
        name: 'SearchFinishedEvent',
        where: (payload) =>
            payload['finished'] == true && (payload['items'] as List? ?? []).isNotEmpty);
    final book = BookRow.fromJson(
        (frame.payload['items']! as List).cast<Map<String, Object?>>().first);
    final session = await adapter.openReader(
        sourceUrl: fixture.sourceUrl, book: book.raw, shelve: true, resume: true);
    final rows = await adapter.chapterRows(
        sourceUrl: fixture.sourceUrl, bookUrl: session.bookUrl);
    final tocRowUrl = rows[anchor['chapter_index'] as int].url;
    final document = await adapter.chapterDocument(
        sourceUrl: fixture.sourceUrl,
        bookUrl: session.bookUrl,
        chapterUrl: tocRowUrl);
    return _LiveShot(
      fixture: fixture,
      adapter: adapter,
      subscription: subscription,
      frames: frames,
      book: book,
      tocRowUrl: tocRowUrl,
      document: document,
    );
  } catch (_) {
    await cleanup();
    rethrow;
  }
}

Future<EngineEvent> _waitFor(List<EngineEvent> frames,
    {required String name,
    required bool Function(Map<String, Object?> payload) where}) async {
  final deadline = DateTime.now().add(const Duration(seconds: 120));
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
  test('先决读数：墙/渲染两版同时可取且可辨，目录行挂着 webView 选项', () async {
    if (_python == null) {
      markTestSkipped('没有 LEGODO_PYTHON：这一臂要真起子进程');
      return;
    }
    final fixture = await _startFixture();
    addTearDown(() => fixture.process.kill(ProcessSignal.sigterm));
    final anchor = _anchor();
    final chapterPath = Uri.parse('${anchor['chapter_url']}').path;
    final wall = await http
        .get(Uri.parse('${fixture.baseUrl}$chapterPath'))
        .timeout(const Duration(seconds: 20));
    final rendered = await http
        .get(Uri.parse('${fixture.baseUrl}$chapterPath?rendered=1'))
        .timeout(const Duration(seconds: 20));
    final wallText = utf8.decode(wall.bodyBytes);
    final renderedText = utf8.decode(rendered.bodyBytes);
    expect(wallText, contains('type="password"'),
        reason: '墙那一版没有登录框 = 这一族源在 FFI 下与别的源没两样，边界臂钉的东西不存在');
    expect(wallText, isNot(contains('<usehtml>')),
        reason: '墙那一版带了渲染标记 = 两版混了，"读到的是墙"读不出是谁的');
    expect(renderedText, contains('<usehtml>'),
        reason: '渲染那一版没有渲染标记 = 两版的分界不在语料这一侧');
    expect(renderedText, isNot(wallText),
        reason: '两种形状是同一页的话，"没人渲染过"这个 0 就没有对照面');
    final hostShape = anchor['host']! as Map<String, Object?>;
    final wallShape = anchor['wall']! as Map<String, Object?>;
    expect(hostShape['paragraph_count'], isNot(wallShape['paragraph_count']),
        reason: '锚点表自己得能分出这两版，否则下面的比对是恒真的');
    expect(hostShape['total_chars'], isNot(wallShape['total_chars']),
        reason: '两版的字数相同 = 段数那一处对照会在下一步失效');
    // 选项挂在 href 上而不是写在规则里（真源就是这么写的，语料普查：56 条带选项）。
    final toc = await http
        .get(Uri.parse('${fixture.baseUrl}/g3web/toc.html'))
        .timeout(const Duration(seconds: 20));
    expect(utf8.decode(toc.bodyBytes), contains(',{"webView":true}'),
        reason: '目录页的 href 没挂选项 = 这一臂的靶子（引擎怎么处置挂在 url 上的选项）没有子弹');
  }, timeout: const Timeout(Duration(minutes: 2)));

  test('实弹边界：webView 章读到的是墙（账上渲染版 0 有正控），答一发没人等的 delivered:false',
      () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR / LEGODO_PYTHON：'
          '跑 .probe/ffi_live_run.py 一次配齐');
      return;
    }
    final anchor = _anchor();
    final hostShape = anchor['host']! as Map<String, Object?>;
    final wallShape = anchor['wall']! as Map<String, Object?>;
    final live = await _shootOnce();
    live.registerTearDowns();
    final chapterPath = Uri.parse('${anchor['chapter_url']}').path;

    // ---- 第 0 格：目录行原样带着选项（转义与否是传输层的事，还原后与锚点表逐字符相同） ----
    expect(Uri.decodeFull(live.tocRowUrl), '${live.fixture.baseUrl}${anchor['toc_href']}',
        reason: '目录行的 url 与锚点表那条对不上（表是语料目录页按 ruleToc 解析出来的，'
            'FFI 这条路选项原样挂在 url 上）');
    final hitsAfterRead = await live.fixture.hits();
    final ch1 = _hitsFor(hitsAfterRead, chapterPath);
    final paragraphs = live.paragraphs;
    final offsets = paragraphOffsets(paragraphs);
    final wantOffsets =
        (wallShape['offsets']! as List).cast<num>().map((v) => v.toInt()).toList();
    stdout.writeln('G3WEB-FFI 正文：服务端 ${live.document.paragraphCount} 段 '
        '${live.document.totalChars} 字 · 墙锚点 ${wallShape['paragraph_count']} 段 '
        '${wallShape['total_chars']} 字 · 渲染锚点 ${hostShape['paragraph_count']} 段 '
        '${hostShape['total_chars']} 字 · 取页账 $hitsAfterRead');

    // ---- 正文 == 墙形状（读回来的是 HTTP 那一版，不渲染、不问人） ----
    expect(paragraphs, hasLength(wallShape['paragraph_count']),
        reason: 'Dart 数出的段数与墙锚点不等');
    expect(live.document.paragraphCount, wallShape['paragraph_count'],
        reason: '读回的不是墙那一版（FFI 下没人渲染，读到什么是什么）');
    expect(live.document.totalChars, wallShape['total_chars'],
        reason: '字数与墙锚点不等');
    expect(chapterTextLength(paragraphs), live.document.totalChars,
        reason: '同一份块列表算出的章内字符流长度必须与 totalChars 相等');
    final wallRow = ((wallShape['rows']! as List).first as Map)['head'];
    expect(paragraphs.first, wallRow,
        reason: '第一段该是墙的登录提示语（墙那一版只有一段）');
    expect(offsets, wantOffsets, reason: 'Dart 的落点与墙锚点不是同一串数');

    // ---- 非宿主形状：渲染版那一版一行都不许混进来 ----
    final hostHead = '${((hostShape['rows']! as List).first as Map)['head']}';
    expect(paragraphs.join(), isNot(contains(hostHead)),
        reason: '渲染版第一段的头混在正文里 = 有人把渲染那一版弄回来了，「没人渲染过」这条边界破了');
    expect(live.document.paragraphCount, isNot(hostShape['paragraph_count']),
        reason: '段数与渲染锚点相同 = 这一臂的两种形状分不开了');
    expect(live.document.title, wallShape['title'],
        reason: '章名也该是目录那一行的：读回别的形状说明整条路走岔');

    // ---- 取页账：墙 ≥1（正控，账本接上了） / 渲染版 ==0（本臂的判据） ----
    expect(ch1['wall'], greaterThanOrEqualTo(1),
        reason: '语料账里 $chapterPath 的墙一次都没被取过 = 账本没接上或引擎没走 HTTP，'
            '那下面"渲染版 0"就不是读数而是仪器没接上（记忆 zero-counts-need-a-positive-control）');
    expect(ch1['rendered'], 0,
        reason: '有人取过渲染版 = FFI 的反向通道活了（或有人偷偷渲染）：把旧三腿断言'
            '（宿主作答 → 4 段 1961 字）续回来，别停在"读到墙"这一版');
    final renderedAnywhere = hitsAfterRead.entries
        .where((entry) =>
            entry.key.contains('?rendered=1') && ((entry.value as num?)?.toInt() ?? 0) > 0)
        .map((entry) => entry.key)
        .toList();
    expect(renderedAnywhere, isEmpty, reason: '整本账里渲染版被别人取过：$renderedAnywhere');

    // ---- 交一发没人等的答案：delivered 读的是现场，不是恒 true 的比较口 ----
    final late = await live.adapter.webContentReady(
        requestId: 'w-没有这一发', body: '<html>没人等</html>');
    expect(late.delivered, isFalse,
        reason: 'FFI 下没有在等的反向请求，delivered 必须回 false；回 true = Go 侧在假装收下'
            '（而没人消费那一格），通道断了照样报成功');

    // ---- 事件面：没有反向通道，就不该有 webview 帧 ----
    final webviewFrames =
        live.frames.where((f) => f.name.toLowerCase().contains('webview')).map((f) => f.name).toSet();
    expect(webviewFrames, isEmpty,
        reason: '事件环里出现了 webview 帧：$webviewFrames —— 有人把请求面做活了，'
            '这一臂该升级成"宿主作答"那组断言');

    // ---- 同章再读：走内容缓存，不再取页 ----
    final again = await live.adapter.chapterDocument(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        chapterUrl: live.tocRowUrl);
    expect(again.totalChars, live.document.totalChars);
    final hitsAfterAgain = await live.fixture.hits();
    expect(_hitsFor(hitsAfterAgain, chapterPath)['wall'], ch1['wall'],
        reason: '同章再读又去取了一次页 = 那一发压根没落到内容缓存里');

    // ---- 预取那一档：第 2 章同为 webView 族，也走 HTTP 墙路、也没人渲染 ----
    Map<String, int> ch2 = _hitsFor(hitsAfterAgain, '/g3web/ch/2.html');
    final deadline = DateTime.now().add(const Duration(seconds: 10));
    while (ch2['wall'] == 0 && DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 200));
      ch2 = _hitsFor(await live.fixture.hits(), '/g3web/ch/2.html');
    }
    stdout.writeln('G3WEB-FFI 预取：第 2 章 墙 ${ch2['wall']} 次 · 渲染版 ${ch2['rendered']} 次');
    expect(ch2['wall'], greaterThanOrEqualTo(1),
        reason: '预取没走 HTTP = 后面那些章的路子与第 1 章不同了');
    expect(ch2['rendered'], 0,
        reason: '预取的章被渲染过 = 反向通道在预取那一档活着');
  }, timeout: const Timeout(Duration(minutes: 6)));
}
