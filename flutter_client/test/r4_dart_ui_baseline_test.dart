/// G2-F **D2/D3**：920 行上屏那一笔钱，在 Dart 侧复量——首屏与滚动每拍，各带底价臂。
///
/// 靶子来自第八十二轮同一份 920 行副本（`.probe/r4_ui_baseline.py`，`docs/dev-logs/2026-10-08.md` §2）：
/// 首屏"从 show 到 Python 树里挂满 920 行" **561 ms**、同页空列表底价 **3 ms**，
/// 滚动每拍下发 **390 ms**（p90 384 ms，12 拍合计 4,695 ms）。
///
/// **口径必须先说清，否则这三个数不可比**：
///
/// 1. flet 那侧的 561 ms 里含"920 个控件全建出来并推给客户端"；Dart 这侧
///    `ListView.builder` 是虚拟的（`sources_screen.dart` 头注第 1 条），一帧只建
///    看得见的那几行，**永远不会**有"树里挂满 920 行"那一刻。所以这里量的不是
///    "行落满"，而是"那 920 行已经在内存里 → 第一帧画出来"；行数那笔钱落在
///    D3 报的"每拍在屏行数"上。这不是把 flet 的数换算过来，是明说两边量的不是同一件事——
///    判据只看各臂与自己底价之比。
/// 2. widget 测试跑在 `flutter_tester` 里：build/layout/paint 都真做（含真文本排版），
///    **但没有 GPU 光栅化**，所以这里每一个 ms 读数都是真实窗口那笔钱的下界。
/// 3. `TestWidgetsFlutterBinding` 把 `HttpOverrides.global` 换掉了，这个 isolate 里
///    一个包都不发（记忆 flutter-test-instrumentation-traps）。所以语料不来自 socket，
///    来自 D1 那一支落盘的 `.probe/out/g2f_source_list.rows.json`——同一份真行，
///    只换了搬运方式。
///
/// 底价臂的形状（这里比 flet 那一轮更严格）：
/// - D2 用**空列表**（同一屏、同一外壳，只差行数），与 flet 那臂同形。
/// - D3 用**同页同列表、落点不动**（`jumpTo(当前 pixels)`）：滚动机构照跑一帧，
///   但不新建任何行。空列表在 D3 当不了底价——`_body` 会换成"书源列表为空"那一格，
///   `ListView` 根本不在树上，滚无可滚。
///   这一臂另有一条"确实一格没换"的正控（比在屏 url 集合）：只断言"底价臂更便宜"
///   挡不住把底价臂也滚一步的改错，那一版两臂都贵、差量却是 0。
///   比集合的口子全测试只有一处（`floorKeptRows`，另一头现读树），D3 末尾再拿
///   "跳到另一端必须判换了"喂它一发正控——否则那条不变性检查可以被写成恒真的
///   自我比对，两行 expect 照样绿。
///
/// 用法（由 `.probe/g2f_dart_baseline.py` 带齐环境变量调用）：
///     cd flutter_client && flutter test test/r4_dart_ui_baseline_test.dart --no-pub
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';
import 'package:legodo_client/features/sources/presentation/sources_screen.dart';

final String _corpusPath = Platform.environment['LEGODO_R4_CORPUS'] ??
    '${Directory.current.parent.path}/.probe/out/g2f_source_list.rows.json';
final bool _haveCorpus = File(_corpusPath).existsSync();

const int _minRows = 900;
const int _warmup = 2;
const int _firstFrameRounds = 5;
const int _modelRounds = 5;
const int _scrollTicks = 12;
const double _tickStepPx = 700;

/// 屏上那句状态行（`SourceStats.line`，形状 `共 N 条 | 启用 … | 停用 … | M 个分组`）。
/// 两臂的"只差行数"要拿它当证据，所以按 `Text` 现读，不去重算一遍——
/// 重算就是拿 Dart 自己证 Dart。
String _statsLine() {
  final lines = find
      .byType(Text)
      .evaluate()
      .map((element) => (element.widget as Text).data)
      .whereType<String>()
      .where((text) => text.startsWith('共 '))
      .toList(growable: false);
  if (lines.isEmpty) return '(没有状态行)';
  expect(lines, hasLength(1), reason: '屏上有 ${lines.length} 句状态行，读哪句是猜的');
  return lines.single;
}

/// 千分位，与 Python 那臂 `f"{size:,}"` 同写法。
String _b(int bytes) {
  final digits = bytes.toString();
  final head = digits.length % 3;
  return <String>[
    if (head > 0) digits.substring(0, head),
    for (var i = head; i < digits.length; i += 3) digits.substring(i, i + 3),
  ].join(',');
}

List<SourceRecord> _corpus() =>
    (jsonDecode(File(_corpusPath).readAsStringSync(encoding: utf8)) as List)
        .cast<Map<String, Object?>>()
        .map(SourceRecord.fromJson)
        .toList(growable: false);

Widget _host(List<SourceRecord> records) => ProviderScope(
      overrides: [
        engineRepositoryProvider.overrideWithValue(
            FakeEngineRepository(sources: records)),
      ],
      child: const MaterialApp(home: SourcesScreen()),
    );

/// 当前在屏的那几行是哪些源：滚动臂的"真换了内容"就靠这个集合比出来。
Set<String> _visibleUrls() => find
    .byType(SourceRow)
    .evaluate()
    .map((element) => (element.widget as SourceRow).record.url)
    .toSet();

/// 底价臂那一拍换没换行：集合相等才算没换。
/// 不写 `a == b`（`Set` 的 `==` 就是同一实例），也不写"只看长度"——
/// 落点不动却把首行换成末行，长度照样不变。
///
/// 全测试只有这一处比集合，且第二边**必须现读树**：把 `_visibleUrls()` 写成
/// 传进去的那一份，这条检查就恒真、底价臂的"一格没换"不再是证据。所以 D3 末尾
/// 另有一发正控走同一个函数，它必须判"换了"。
bool _sameRows(Set<String> a, Set<String> b) =>
    a.length == b.length && a.containsAll(b);

/// 全屏每条 `Scrollable` 的视口高度，写进体检行：抓错哪一条要当场看得见，
/// 不然"内容 0..0"这种读数只会让人去怀疑语料。
List<String> _viewportHeights(Finder scrollables) => scrollables.evaluate()
    .map((element) => (element as StatefulElement).state as ScrollableState)
    .map((state) => state.position.viewportDimension.toStringAsFixed(0))
    .toList(growable: false);

Future<double> _timed(Future<void> Function() body) async {
  final watch = Stopwatch()..start();
  await body();
  watch.stop();
  return watch.elapsedMicroseconds / 1000.0;
}

double _p50(List<double> xs) {
  final sorted = List<double>.of(xs)..sort();
  return sorted[sorted.length ~/ 2];
}

String _report(String label, List<double> xs) {
  final sorted = List<double>.of(xs)..sort();
  final p90 = sorted[((sorted.length * 0.9).floor()).clamp(0, sorted.length - 1)];
  return '$label p50=${_ms(_p50(xs))} ms p90=${_ms(p90)} ms '
      '最差=${_ms(sorted.last)} ms n=${xs.length}';
}

String _ms(double v) => v.toStringAsFixed(1);

void main() {
  test('语料在不在：不在就整支跳过，在就必须是那份 900+ 行、行键互不相同', () {
    if (!_haveCorpus) return;
    final records = _corpus();
    expect(records.length, greaterThanOrEqualTo(_minRows),
        reason: '$_corpusPath 只有 ${records.length} 行 = D1 那支没把语料落对');
    // 行键必须互不相同：`ValueKey(url)` 撞车的话虚拟化会原地复用行，
    // D3 里"滚了要换内容"那条正控就成了仪器自己的错。
    final urls = records.map((record) => record.url).toSet();
    expect(urls, hasLength(records.length),
        reason: '${records.length} 行里只有 ${urls.length} 个不同 url：'
            '重复的行会让 ValueKey 撞车，两臂量的都不是行数');
  });

  testWidgets('D2 首屏：920 行的第一帧 vs 空列表的第一帧（交替五轮）', (tester) async {
    if (!_haveCorpus) {
      markTestSkipped('缺 $_corpusPath：先跑 .probe/g2f_dart_baseline.py');
      return;
    }
    final records = _corpus();
    final full = <double>[], empty = <double>[];
    var builtOnLastFrame = 0;
    var fullLine = '', emptyLine = '';
    for (var round = 0; round < _firstFrameRounds + _warmup; round++) {
      // 交替：满行先、空列表后。先跑那臂背一次性排布的钱（记忆
      // ab-bench-must-interleave），所以两轮之差本身不许当读数用。
      //
      // 两臂之间必须把整棵树**退掉**：`ProviderScope` 的 `overrides` 在已存在的容器上
      // 换不掉（同位置同类型 = 元素原地更新，容器沿用第一次那份 override），
      // 所以直接 `pumpWidget(_host(const []))` 画出来的还是**上一臂的 920 行**——
      // 第一版就在这里把"空列表底价"量成了"同一屏重画第二遍"，两臂差 0.1 ms 看着像
      // 虚拟化赚大了，其实底价臂根本没空。当场读状态行才抓出来（见下面那条 expect）。
      await tester.pumpWidget(const SizedBox.shrink());
      final tFull = await _timed(() async {
        await tester.pumpWidget(_host(records));
        await tester.pumpAndSettle();
      });
      builtOnLastFrame = find.byType(SourceRow).evaluate().length;
      // 状态行**在这一臂里当场读**：出了这一臂再去读，树已经换成空列表那一屏了。
      fullLine = _statsLine();
      await tester.pumpWidget(const SizedBox.shrink());
      final tEmpty = await _timed(() async {
        await tester.pumpWidget(_host(const <SourceRecord>[]));
        await tester.pumpAndSettle();
      });
      emptyLine = _statsLine();
      if (round >= _warmup) {
        full.add(tFull);
        empty.add(tEmpty);
      }
    }

    // ---- 建模那两臂：与 flet "首屏 561 ms" 同跨度的第一段 ----
    // flet 那 561 ms 里含"取回这 920 行 + 变成能画的行 + 首帧"。Dart 侧这三段
    // 分别是 D1 的 A、这里的 `建模`、和这里的 `首屏`，各自都带自己的底价臂，
    // 合起来才与 561 ms 同跨度——所以这里必须单独量，不能让文档手加。
    final raw = File(_corpusPath).readAsStringSync(encoding: utf8);
    final withModel = <double>[], bareDecode = <double>[];
    var modelCount = 0, bareCount = 0;
    for (var i = 0; i < _modelRounds + _warmup; i++) {
      final tModel = await _timed(() async {
        modelCount = (jsonDecode(raw) as List)
            .cast<Map<String, Object?>>()
            .map(SourceRecord.fromJson)
            .toList(growable: false)
            .length;
      });
      final tBare = await _timed(() async {
        bareCount = (jsonDecode(raw) as List).length;
      });
      if (i >= _warmup) {
        withModel.add(tModel);
        bareDecode.add(tBare);
      }
    }

    // ---- 仪器在岗证据 ----
    expect(full, hasLength(_firstFrameRounds));
    expect(empty, hasLength(_firstFrameRounds));
    expect(builtOnLastFrame, greaterThan(0),
        reason: '满行那一臂一帧都没建行 = 屏幕根本没接上数据，两个数都是空的');
    expect(builtOnLastFrame, lessThan(records.length),
        reason: '一帧建出 $builtOnLastFrame 行（语料 ${records.length}）= 虚拟化没生效，'
            'D2 就成了 flet 那种全建，判据得换口径重写');
    // 两臂的**状态行**必须真的只差行数：这是"同一屏只差行数"的证据，
    // 也是空列表那一臂确为空、满行那一臂确有 920 行的在岗证据。
    expect(fullLine, startsWith('共 ${records.length} 条'),
        reason: '满行那一臂的状态行是 "$fullLine"：屏上不是这 920 行，首屏那两数不作数');
    expect(emptyLine, startsWith('共 0 条'),
        reason: '底价臂的状态行是 "$emptyLine"：它还带着行，就不是底价臂');
    expect(modelCount, records.length);
    expect(bareCount, records.length);
    expect(tester.takeException(), isNull);

    stdout.writeln('G2F-D2 语料 ${records.length} 行 · 一帧在屏 $builtOnLastFrame 行'
        '（虚拟化：不建满）· 状态行 "$fullLine" / "$emptyLine"');
    stdout.writeln('G2F-D2 ${_report('920 行首屏', full)}');
    stdout.writeln('G2F-D2 ${_report('空列表首屏（底价）', empty)}');
    stdout.writeln('G2F-D2 差量（行数那笔钱）≈ '
        '${_ms(_p50(full) - _p50(empty))} ms');
    stdout.writeln('G2F-D2 ${_report('建模 920 行（jsonDecode+fromJson）', withModel)}');
    stdout.writeln('G2F-D2 ${_report('只解 JSON（底价）', bareDecode)}');
    stdout.writeln('G2F-D2 差量（建 SourceRecord 那笔钱）≈ '
        '${_ms(_p50(withModel) - _p50(bareDecode))} ms · '
        '载荷 ${_b(utf8.encode(raw).length)} B');
  }, timeout: const Timeout(Duration(minutes: 6)));

  testWidgets('D3 滚动每拍：真滚 12 拍 vs 同页落点不动 12 拍（交替两轮）', (tester) async {
    if (!_haveCorpus) {
      markTestSkipped('缺 $_corpusPath：先跑 .probe/g2f_dart_baseline.py');
      return;
    }
    final records = _corpus();
    await tester.pumpWidget(_host(records));
    await tester.pumpAndSettle();
    // 挂手必须钉在 `sources.list` 那一发上：这一屏上**不止一个** `Scrollable`
    // （搜索框那一条自己就有一条），`.first` 会抓到视口 228 px、内容 0..0 的那一个，
    // 于是 12 拍 `jumpTo` 全夹在原地、两臂都量成 0 ms——读数漂亮，仪器没接上。
    final all = find.byType(Scrollable);
    final listScrollable = find.descendant(
        of: find.byKey(const Key('sources.list')), matching: all);
    expect(listScrollable, findsOneWidget,
        reason: '`sources.list` 底下找不到 Scrollable = 列表没上屏，滚无可滚');
    final position = tester.state<ScrollableState>(listScrollable).position;
    final extent = position.maxScrollExtent;
    stdout.writeln('G2F-D3 仪器体检：全屏 Scrollable ${all.evaluate().length} 个 · '
        '各条视口 ${_viewportHeights(all).join('/')} px · 选中那条视口 '
        '${position.viewportDimension} px · 内容 '
        '${position.minScrollExtent}..${extent.toStringAsFixed(0)} · '
        '在屏 ${find.byType(SourceRow).evaluate().length} 行');
    expect(extent, greaterThan(_tickStepPx),
        reason: '内容高度只量到 $extent px，jumpTo 全被夹在同一个落点上，'
            '这一臂量的就不是滚动');

    // 只留这一个比较口：基准集合传进去，另一边现读树。正控（下面那发强制换行）
    // 也走它，所以"拿基准跟自己比"这种写法会被当场打红。
    bool floorKeptRows(Set<String> baseline) => _sameRows(baseline, _visibleUrls());

    final moving = <double>[], still = <double>[];
    final onScreen = <int>[];
    final urlSets = <Set<String>>[];
    var movedTicks = 0, clampedTicks = 0, sameRowsTicks = 0, rowsChangedInFloor = 0;
    for (var tick = 0; tick < _scrollTicks * 2 + _warmup; tick++) {
      final before = position.pixels;
      final tMove = await _timed(() async {
        position.jumpTo(before + _tickStepPx);
        await tester.pump();
      });
      final didMove = position.pixels != before;
      final built = find.byType(SourceRow).evaluate().length;
      final urls = _visibleUrls();
      final tStill = await _timed(() async {
        position.jumpTo(position.pixels);
        await tester.pump();
      });
      // 底价臂那一拍**必须一格都没换**：只写"它比滚动臂便宜"的话，把它也偷偷
      // 滚一步（`jumpTo(pixels + step)`）照样更便宜——那一臂就不再是底价了。
      final floorUnchanged = floorKeptRows(urls);
      if (tick >= _warmup) {
        moving.add(tMove);
        still.add(tStill);
        onScreen.add(built);
        urlSets.add(urls);
        if (didMove) {
          movedTicks++;
        } else {
          clampedTicks++;
        }
        if (floorUnchanged) {
          sameRowsTicks++;
        } else {
          rowsChangedInFloor++;
        }
      }
    }

    // ---- 仪器在岗证据 ----
    expect(moving, hasLength(_scrollTicks * 2));
    expect(movedTicks, greaterThanOrEqualTo(_scrollTicks),
        reason: '只有 $movedTicks 拍真动了（夹住 $clampedTicks 拍）：'
            '落点被夹在最大估算偏移上，这一臂量的不是滚动的钱');
    final maxOnScreen = onScreen.reduce((a, b) => a > b ? a : b);
    expect(maxOnScreen, lessThan(records.length),
        reason: '在屏 $maxOnScreen 行已接近全建 ${records.length}，虚拟化前提不成立');
    // 正控：真滚的臂必须**换过内容**。没换就是 jumpTo 只动了数值、行没重建，
    // 那"每拍 X ms"量的就不是新建行的钱。
    final distinct = urlSets.map((s) => s.toList()..sort()).toSet().length;
    expect(distinct, greaterThan(1),
        reason: '滚了 $movedTicks 拍，在屏那几行的 url 集合一次都没变 = 仪器没接上');
    // 底价臂不底价：落点不动的那些拍，在屏行数不许比滚动臂还多。
    expect(still.every((value) => value <= moving.reduce((a, b) => a > b ? a : b)), isTrue,
        reason: '底价臂里出现比滚动臂最差还差的数 = 两臂混进了别的变量');
    // 底价臂那一拍一格都不许换。这一条挡的是"把底价臂也偷偷滚一步"
    // （`jumpTo(pixels + _tickStepPx)`）：那一臂照样比滚动臂便宜（少了 pump 之间的
    // 一次重建），差量却还是行的钱吗——不是了，两臂的钱一样，只是各自都滚了。
    expect(rowsChangedInFloor, 0,
        reason: '底价臂里有 $rowsChangedInFloor 拍换了在屏行 = 那一臂也在滚，'
            '两臂差量不再是"新建+回收行"那笔钱');
    expect(sameRowsTicks, still.length);

    // 正控（在计时之外）：给同一个比较口喂一次"确实换了行"，它必须判"换了"。
    // 挡的是把 `_visibleUrls()` 写成基准集合本身——那一改让上面两条 expect 恒过，
    // 底价臂的"一格没换"就只剩自己跟自己点头。
    final canaryTo = position.pixels <= extent / 2 ? extent : 0.0;
    final canaryBefore = _visibleUrls();
    position.jumpTo(canaryTo);
    await tester.pump();
    expect((position.pixels - canaryTo).abs(), lessThan(1.0),
        reason: '正控那一跳被夹住了（想去 $canaryTo，落在 ${position.pixels}）：'
            '这发验不了比较口，先修仪器');
    expect(floorKeptRows(canaryBefore), isFalse,
        reason: '跳到另一端后 $canaryBefore 那几行居然还在 = `floorKeptRows` '
            '没现读树，上面 $sameRowsTicks 拍"一格没换"不算证据');

    stdout.writeln('G2F-D3 语料 ${records.length} 行 · 内容高 ${extent.toStringAsFixed(0)} px · '
        '每拍步进 $_tickStepPx px · 在屏行数峰值 $maxOnScreen · 换过 $distinct 套在屏行');
    stdout.writeln('G2F-D3 ${_report('每拍真滚', moving)}'
        '（真动 $movedTicks 拍 / 夹住 $clampedTicks 拍）');
    stdout.writeln('G2F-D3 ${_report('每拍落点不动（底价）', still)}'
        '（换行 $rowsChangedInFloor 拍 / 一格没换 $sameRowsTicks 拍）');
    stdout.writeln('G2F-D3 差量（每拍新建+回收行那笔钱）≈ '
        '${_ms(_p50(moving) - _p50(still))} ms');
  }, timeout: const Timeout(Duration(minutes: 6)));
}
