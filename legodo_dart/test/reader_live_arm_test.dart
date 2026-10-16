/// G3 落点实弹臂（FFI 版）：**进程内真 Go 引擎**（`legodo_engine.dll`）+ 仓库里那条
/// 语料假源，两侧按字符偏移比翻页落点。
///
/// §9 门 G3 的判据是"同一本书翻页落点与 flet 版可比（按字符偏移断言）"。
/// `reader_screen_test` 那一半用替身 + 黄金样本（证形状），这一半证**接上真的还是一样对**：
///
/// - 参照物是 `tests/gateway/projection/reader.content.json`——**flet 自己的函数**算出来的
///   落点表，不是 Dart 自己算的。两边都读 Dart 的投影，Dart 错了两边一起绿
///   （G1 那笔账，见 `live_engine_test.dart` 抬头）。
/// - 靶子是 `tests/gateway/fake_source_reader.py`（跟踪在仓库里的那份语料），由本臂当
///   **子进程**拉起来；它与黄金样本的对齐由 `tests/test_reader_fixture_matches_golden_samples.py`
///   钉住。本臂不抄语料规则：书源字典是问 `/g3/source.json` 要的。
/// - 引擎是**进程内**的（FFI 架构下不再是子进程）：`LEGADO_BOOKSOURCE_DATA_DIR` 在进程
///   启动时就固定了，所以库根由 `.probe/ffi_live_run.py` 预建（空目录）——这一臂建的源、
///   入的书架、落的进度一行都不进真库。结果事件走适配器的游标轮询（`engine_poll`）。
///
/// 三条仪器规矩：全程 127.0.0.1（零外网，记忆 `legado-live-scan-av-block`）；子进程输出
/// 从头抽干（记忆 `subprocess-probe-must-drain-stdout`）；"引擎真来取过页"读**语料自己记
/// 的账**（`/hits.json`），不由引擎或本臂自报（记忆 `zero-counts-need-a-positive-control`）。
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
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/data/reading_position.dart';
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
  _Fixture(
      {required this.process, required this.port, required this.source, required this.lines});

  final Process process;
  final int port;
  final Map<String, Object?> source;
  final List<String> lines;

  String get sourceUrl => '${source['bookSourceUrl']}';
  String get group => '${source['bookSourceGroup']}';

  /// 语料自己记的取页账（路径 + 是否渲染版 → 次数），由被取的一方记账。
  Future<Map<String, Object?>> hits() async {
    final response = await http
        .get(Uri.parse('http://127.0.0.1:$port/hits.json'))
        .timeout(const Duration(seconds: 20));
    return jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, Object?>;
  }
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

/// 一趟实弹：假源 + 进程内真引擎 + 真会话，读回那一章。
class _LiveShot {
  _LiveShot({
    required this.fixture,
    required this.adapter,
    required this.subscription,
    required this.frames,
    required this.book,
    required this.document,
    required this.chapterCount,
  });

  final _Fixture fixture;
  final FFIEngineAdapter adapter;
  final StreamSubscription<EngineEvent> subscription;
  final List<EngineEvent> frames;
  final BookRow book;
  final ChapterDocument document;
  final int chapterCount;

  List<String> get paragraphs => chapterParagraphs(document.blocks);
  List<int> get offsets => paragraphOffsets(paragraphs);

  /// 逐项注册清理：**成功路径**才用 addTearDown（失败路径由 [_shootOnce] 自己收摊，
  /// 否则半趟跑起来的假源子进程会把端口留在盘上）。
  void registerTearDowns() {
    addTearDown(() async {
      await subscription.cancel();
      // disconnect 是幂等的（cleanup 由 _ready 守着）：测试体内若已收过摊，这里再来一次是空操作。
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
    expect(saved.saved, isTrue, reason: 'source_save 没建起语料源：${saved.errors}');
    // 建源的回话只是"这一发没报错"，源真在不在要按 url 读回来——search 那一发会在
    // 找不到源时报"无源可搜"，与"源建坏了"隔着两层，读回来这一发把两层并成一层。
    final loaded = await adapter.loadSource(fixture.sourceUrl);
    expect(loaded, isNotNull,
        reason: 'source_save 之后按 url 读不回这条源 = 建源这一发没落到事情上');
    final accepted = await adapter.searchStart(
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
    final session = await adapter.openReader(
        sourceUrl: fixture.sourceUrl, book: book.raw, shelve: true, resume: true);
    final rows = await adapter.chapterRows(
        sourceUrl: fixture.sourceUrl, bookUrl: session.bookUrl);
    expect(rows.length, session.chapterCount,
        reason: '会话报的章数与目录行数不是一个数：'
            '${session.chapterCount} vs ${rows.length}');
    final document = await adapter.chapterDocument(
        sourceUrl: fixture.sourceUrl,
        bookUrl: session.bookUrl,
        chapterUrl: rows[_chapterIndex].url);
    return _LiveShot(
      fixture: fixture,
      adapter: adapter,
      subscription: subscription,
      frames: frames,
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
  test('靶子自己先要能打：假源子进程起得来、PORT 行与 source.json 同端口', () async {
    if (_python == null) {
      markTestSkipped('没有 LEGODO_PYTHON：这一臂要真起子进程');
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

  test('实弹：进程内真引擎读回的正文，Dart 那把尺与 flet 投影表同偏移', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR / LEGODO_PYTHON：'
          '跑 .probe/ffi_live_run.py 一次配齐');
      return;
    }
    final projection = _loadJson('projection/reader.content.json');
    final live = await _shootOnce();
    live.registerTearDowns();

    final offsets = live.offsets;
    final want =
        (projection['offsets']! as List).cast<num>().map((v) => v.toInt()).toList();
    // 语料自己记的账：这一页真被引擎从回环地址取过。
    final hits = await live.fixture.hits();
    final chapterPath = Uri.parse(live.document.chapterUrl).path;
    final chapterHits = (hits[chapterPath] as num?)?.toInt() ?? 0;
    stdout.writeln('LIVE-FFI 第${_chapterIndex + 1}章 服务端 ${live.document.paragraphCount} 段 '
        '${live.document.totalChars} 字 · Dart ${live.paragraphs.length} 段 '
        '${chapterTextLength(live.paragraphs)} 字 · flet 投影 '
        '${projection['paragraph_count']} 段 ${projection['total_chars']} 字'
        ' · 目录 ${live.chapterCount} 章 · 取页账 $hits');

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

    // ---- 仪器正控：正文那一页真被取过（语料记账，不是本臂自报） ----
    // 没有这一行，上面那串"对上了"分不清是引擎真从回环取回来的，还是读了别处的缓存。
    expect(chapterHits, greaterThanOrEqualTo(1),
        reason: '语料账里 $chapterPath 一次都没有 = 引擎没来取过这页，读数另有来源');
    expect(hits.values.any((v) => (v as num?) != null && v! > 0), isTrue,
        reason: '取页账整个是空的 = 账本本身没记上（与"没被取过"是两件事）');

    // ---- 媒体面：Go 正文管线目前是纯文本（GetContent 把 .list@html 整段 HTMLToText，
    // <img> 被拍平），flet 参考实现会给插图画一条 image 块（投影表 rows 里那条
    // kind=image）。这是 FFI 引擎的已知缺口（不在本轮拆除范围），读数写在这里而不是
    // 断言"没有"：缺口补上那天这里会红，逼着把断言升级成"1 块 + url 非空 + binary 形状"。
    final media = live.document.blocks.where((block) => !block.isText).toList();
    expect(media, isEmpty,
        reason: 'Go 出媒体块了：把这条读数升级成与 flet 同形的断言（对照投影表 kind=image 那条）');

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
    final saved = await live.adapter.savePosition(
        sourceUrl: live.fixture.sourceUrl,
        bookUrl: live.book.url,
        positionJson: position.toJson());
    expect(saved.characterOffset, offsets[_pinnedParagraph],
        reason: '范围内的偏移被改写了：落回 ${saved.characterOffset}，'
            'Dart 发的是 ${offsets[_pinnedParagraph]}');
    final restored = await live.adapter.readPosition(
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
    final overflow = await live.adapter.savePosition(
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
  }, timeout: const Timeout(Duration(minutes: 6)));

  test('收摊与复用：engine_shutdown 真把引擎放掉，重接之后会话数据还在', () async {
    if (!_live) {
      markTestSkipped('没有 LEGODO_ENGINE_DLL / LEGADO_BOOKSOURCE_DATA_DIR / LEGODO_PYTHON');
      return;
    }
    final live = await _shootOnce();
    live.registerTearDowns();
    final rowsBefore = await live.adapter.listBookshelf();
    expect(rowsBefore, isNotEmpty,
        reason: '刚 openReader(shelve:true) 过，书架却一行没有 = 这一臂的书根本没进架');

    // 先退订事件流：poll 定时器还在的话，shutdown 之后每一拍都会往流里塞
    // "hub not initialized"（那个错按设计要继续报），会把收摊顺序里的读数淹掉。
    await live.subscription.cancel();
    await live.adapter.shutdown(reason: 'g3 live arm done');
    await expectLater(
        live.adapter.listBookshelf(),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
            .having((EngineFailure e) => e.message, 'message',
                contains('hub not initialized'))),
        reason: 'engine_shutdown 之后引擎必须真放掉（不是"标志位换了个值"——那种引擎这里会照常回数）');

    // 重接：GoFFIClient.initialize() 在 _ready=true 时早退，所以必须先用 disconnect
    // 把 _ready 归位；直接 connect 会静默接上一个已经死掉的 hub。
    await live.adapter.disconnect();
    await live.adapter.connect();
    final rowsAfter = await live.adapter.listBookshelf();
    expect(rowsAfter.length, rowsBefore.length,
        reason: '重接之后书架读数与收摊前不等：同一份语料目录、同一批行，'
            '${rowsBefore.length} → ${rowsAfter.length}');
  }, timeout: const Timeout(Duration(minutes: 5)));
}
