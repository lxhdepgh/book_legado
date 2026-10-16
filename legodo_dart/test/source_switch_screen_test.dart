/// 换源那一屏的 widget 测试：替身喂 `switch.*` 那五发的**黄金样本**，不起进程、不发字节。
///
/// 载荷一律从 `tests/gateway/golden/switch.*.json` 现读（`switch.seeds` 的 2 行裸书、
/// `switch.snapshot` 的九键快照、`switch.scan` 的 `progress_sample` 那一帧与
/// `event_sample` 那一条收尾）。样本要变了这里就红——红了去重测，不许就地改成想要的样子。
///
/// 这一支钉的五件事，都是"屏幕看着像"照不出来的那一类：
/// 1. 状态槽的**优先级**（种子句 / 进度句 / 动作回报 / 断线，谁最后写谁说话，全屏只有一个槽）。
/// 2. **进度帧按 `switch_id` 认轮**：别人那一轮的帧上了屏，用户看到的是"停了候选还在换"。
/// 3. 0.5s 合流：窗口里只留最后一帧，且没到窗口时**不许提前**报分数。
/// 4. `switch.cancel` 回 `false` 那一支必须有另一句话。
/// 5. 写回之后**无条件**重读书架（flet 的 `_do_switch` 在 pop 之后无条件 `_load()`）。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/event_names.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';
import 'package:legodo_client/features/bookshelf/data/source_switch_models.dart';
import 'package:legodo_client/features/bookshelf/domain/source_switch_notifier.dart';
import 'package:legodo_client/features/bookshelf/presentation/bookshelf_screen.dart';
import 'package:legodo_client/features/bookshelf/presentation/source_switch_dialog.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _sample(String name) => jsonDecode(
        File('${_repoRoot.path}/tests/gateway/golden/$name.json')
            .readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _envelope(String name) =>
    Map<String, Object?>.from(_sample(name)['response']! as Map);

/// `switch.snapshot` 的载荷（九键）。
Map<String, Object?> _snapshotData() =>
    Map<String, Object?>.from(_envelope('switch.snapshot')['data']! as Map);

/// `switch.seeds` 的载荷：裸书行，两行。
List<BookRow> _seedRows() => (_envelope('switch.seeds')['data']! as List)
    .cast<Map<String, Object?>>()
    .map((row) => BookRow.fromJson(Map<String, Object?>.from(row)))
    .toList(growable: false);

/// `switch.seeds` 请求里那一本——**它就是这一屏该拿到的 `record`**（真发过的那 35 键）。
Map<String, Object?> _recordFromSample() => Map<String, Object?>.from(
    (Map<String, Object?>.from(
            _sample('switch.seeds')['request']! as Map)['payload']! as Map)['book']!
        as Map);

/// 真进度帧那一格（6 键：switch_id/done/total/failures/starved/timestamp）。
Map<String, Object?> _progressFrame() {
  final progressSample =
      Map<String, Object?>.from(_sample('switch.scan')['progress_sample']! as Map);
  return Map<String, Object?>.from(
      Map<String, Object?>.from((progressSample['frames']! as List).first as Map)
          ['payload']! as Map);
}

/// 真收尾帧那一格（5 键，**不带计数**）。
Map<String, Object?> _finishedPayload() => Map<String, Object?>.from(
    Map<String, Object?>.from(_sample('switch.scan')['event_sample']! as Map)['payload']!
        as Map);

/// 收尾那一发的**本轮**版本：守门（`_isCurrentRound`）按 `switch_id` 认轮，
/// 样本里那一格是采集时的轮次号，原样发整条会被丢掉（收尾三种一句都上不了屏）。
/// 身份现读替身记下的 `switch.scan`——就是点「开始换源」那一拍自己攥的那个 int。
EngineEvent _finished(FakeEngineRepository fake,
        [Map<String, Object?> overrides = const <String, Object?>{}]) =>
    EngineEvent(EngineEventNames.sourceSwitchFinished,
        _with(_finishedPayload(), <String, Object?>{
          'switch_id': '${fake.payloadFor('switch.scan')!['switch_id']}',
          ...overrides,
        }));

/// 从样本改一格的替身载荷：形状还是真那一份，只翻被测的那一值。
Map<String, Object?> _with(Map<String, Object?> source, Map<String, Object?> overrides) =>
    Map<String, Object?>.from(source)..addAll(overrides);

/// 一轮"还在跑"的快照（样本里那一轮是 running=false 的收尾态）。
SwitchRun _runningRun() =>
    SwitchRun.fromJson(_with(_snapshotData(), <String, Object?>{'running': true}));

FakeEngineRepository _fake({
  List<BookRow>? seeds,
  SwitchRun? run,
  bool cancelled = false,
  bool applied = true,
  Duration delay = Duration.zero,
}) =>
    FakeEngineRepository(delay: delay)
      ..switchSeedRows = seeds ?? _seedRows()
      ..switchRun = run ?? SwitchRun.fromJson(_snapshotData())
      ..switchCancelResult = cancelled
      ..switchApplyResult = SwitchApplyResult(applied: applied, sourceUrl: '');

const String _bookName = '换源目标书';

Finder get _rows => find.byIcon(Icons.radio_button_unchecked);
Finder get _start => find.byKey(const Key('switch.start'));
Finder get _stop => find.byKey(const Key('switch.stop'));
Finder get _bar => find.byType(LinearProgressIndicator);

/// 走**真入口**的那一臂：`openSourceSwitch` 里那个嵌套 `ProviderScope` 同时覆盖 args 与
/// notifier 两格，漏掉任一格都只会得到"这一屏没收到书"，所以从这里进而不是直接挂 dialog。
Future<void> _open(WidgetTester tester, FakeEngineRepository fake,
    {Map<String, Object?>? record}) async {
  await tester.binding.setSurfaceSize(const Size(1200, 1600));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final given = record ?? _recordFromSample();
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: MaterialApp(
      home: Scaffold(
        body: Center(
          child: Builder(builder: (context) => ElevatedButton(
                onPressed: () =>
                    openSourceSwitch(context, record: given, name: _bookName),
                child: const Text('换源这一本'),
              )),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('换源这一本'));
  await tester.pumpAndSettle();
}

/// 起了手的那一屏（点过「开始换源」、还没收到任何事件）。
Future<void> _started(WidgetTester tester, FakeEngineRepository fake) async {
  await _open(tester, fake);
  await tester.tap(_start);
  await tester.pumpAndSettle();
}

/// 候选那一列的书名，按上屏顺序。
List<String?> _candidateTitles(WidgetTester tester) => tester
    .widgetList<ListTile>(find.byType(ListTile))
    .map((tile) => (tile.title! as Text).data)
    .toList();

void main() {
  // ------------------------------------------------------------------ #
  // 打开那一拍：本地种子 + 快照回放
  // ------------------------------------------------------------------ #
  testWidgets('种子那一发还没到手时是「等待开始」，到手就换成本地已有 N 个结果', (tester) async {
    final fake = _fake(delay: const Duration(milliseconds: 50));
    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(fake)],
      child: MaterialApp(
        home: Scaffold(
          body: Center(
            child: Builder(builder: (context) => ElevatedButton(
                  onPressed: () => openSourceSwitch(context,
                      record: _recordFromSample(), name: _bookName),
                  child: const Text('换源这一本'),
                )),
          ),
        ),
      ),
    ));
    await tester.tap(find.text('换源这一本'));
    await tester.pump();

    // 正对照：flet 的对话框初值就是这一句，种子句是**换上去**的。
    // 加载态直接显示「本地已有 0 个结果」的话，用户读到的是"本地没搜过"，
    // 而那只是"这一发还在路上"（记忆 `zero-counts-need-a-positive-control`）。
    expect(find.text('等待开始'), findsOneWidget);
    expect(find.textContaining('本地已有'), findsNothing);
    expect(_bar, findsNothing, reason: '没在跑就不许有转圈');

    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();
    expect(find.text('等待开始'), findsNothing);
    expect(find.text('本地已有 2 个结果；点「开始换源」扫描全部书源'), findsOneWidget);
  });

  testWidgets('打开这一屏发的是 seeds + snapshot 两发，吃的就是书架那一行原样那份字典', (tester) async {
    final record = _recordFromSample();
    final fake = _fake();
    await _open(tester, fake, record: record);

    expect(fake.callsFor('switch.seeds'), 1);
    expect(fake.callsFor('switch.snapshot'), 1,
        reason: '断线/切页之后那一轮可能还在跑，开屏必须回放一次');
    // 不许在这里重建字典：过一遍模型再倒回去，少的正是服务端认账的那一格。
    expect(fake.payloadFor('switch.seeds')!['book'], same(record));
    expect(fake.callsFor('switch.scan'), 0, reason: '用户没点开始就不许联网扫');

    // 种子行的来源名从行里的 `origin_name` 取（这一发回的是裸书行，没有外层包装）。
    expect(find.text('G3换源a'), findsOneWidget);
    expect(find.text('G3换源x'), findsOneWidget);
    expect(_rows, findsNWidgets(2));
    expect(_start, findsOneWidget);
    expect(_stop, findsNothing);
  });

  testWidgets('本地一颗种子没有：说的是"点开始换源"，候选那一格是一句说明不是一片空白', (tester) async {
    await _open(tester, _fake(seeds: const <BookRow>[]));
    expect(find.text('点「开始换源」扫描全部书源'), findsOneWidget);
    expect(find.text('本地已有'), findsNothing, reason: '0 个不许写成"已有 0 个"');
    expect(find.text(kSwitchEmptyCandidates), findsOneWidget);
    expect(_rows, findsNothing);
  });

  testWidgets('上一轮跑完的那批候选不回放进新对话框：每次打开从种子重新起手', (tester) async {
    final fake = _fake(); // 样本快照：running=false 且带着 3 行候选
    expect(fake.switchRun.running, isFalse);
    expect(fake.switchRun.candidates.length, 3, reason: '样本里的上一轮确实有货');
    await _open(tester, fake);

    expect(_rows, findsNWidgets(2), reason: '列的是本地那 2 行，不是上一轮那 3 行');
    expect(find.text('扫描完成，3 个候选'), findsNothing);
    expect(find.text('本地已有 2 个结果；点「开始换源」扫描全部书源'), findsOneWidget);
    expect(_start, findsOneWidget, reason: '那一轮已经收尾，入口得能再点');
  });

  testWidgets('重开这一屏时那一轮还在跑：接管它的身份，句子用快照里的真计数', (tester) async {
    final fake = _fake(run: _runningRun());
    await _open(tester, fake);

    // 「正在扫描同名书源…」是起手那一句；快照里有 done/total，能报就报——
    // 把已经扫到 3/3 的一轮说成刚起手，是界面在骗人。
    expect(find.text(kSwitchScanningStatus), findsNothing);
    expect(find.text('正在扫描 3/3 个书源'), findsOneWidget);
    expect(_bar, findsOneWidget);
    expect(_stop, findsOneWidget);
    expect(_start, findsNothing, reason: '开始与停止是换 visible，不是灰着一个');

    // 事件按快照那一份身份来认（`_adoptRound`）：本轮身份在服务端折成了字符串。
    // '15035127' 是这版样本里那一轮的号——样本换了这里先红，重测之后再改字面。
    expect(fake.switchRun.switchId, '15035127');
    fake.emit(EngineEvent(EngineEventNames.sourceSwitchProgress,
        _with(_progressFrame(), <String, Object?>{
          'switch_id': '15035127',
          'done': 2,
          'failures': 1,
          'starved': 2
        })));
    await tester.pump(const Duration(milliseconds: 600));
    await tester.pumpAndSettle();
    expect(find.text('正在扫描 2/3 个书源，1 个失败，2 个本机忙不过来'), findsOneWidget,
        reason: 'failures 与 starved 各一句：被自己挤掉的源不是失败的源');
  });

  // ------------------------------------------------------------------ #
  // 开始 → 进度 → 收尾
  // ------------------------------------------------------------------ #
  testWidgets('点「开始换源」：受理包里没有进度，所以那一句不带分数', (tester) async {
    final record = _recordFromSample();
    final fake = _fake();
    await _open(tester, fake, record: record);
    await tester.tap(_start);
    await tester.pumpAndSettle();

    expect(fake.callsFor('switch.scan'), 1);
    final payload = fake.payloadFor('switch.scan')!;
    expect(payload.keys.toSet(), <String>{'book', 'switch_id', 'keyword', 'group'},
        reason: '省一格，服务端就按它自己的默认值猜这一轮扫什么');
    expect(payload['book'], same(record));
    expect(payload['keyword'], _bookName);
    expect(payload['group'], '', reason: '空串 = 全部启用源；分组控件在 G4');
    final switchId = payload['switch_id'];
    expect(switchId, isA<int>(), reason: '本轮身份是自己攥的 int，服务端折成 str 回带');
    expect(switchId, isNot(0));

    // `mode="job"` 的回执只有 `{started, action}`：这里不许读出任何分数。
    expect(find.text(kSwitchScanningStatus), findsOneWidget);
    expect(find.textContaining('个书源'), findsNothing);
    expect(tester.widget<LinearProgressIndicator>(_bar).value, 0.0,
        reason: '条子出现但不装成有进度：分数还没有来源');
  });

  testWidgets('进度帧认本轮身份：别人那一轮的帧一根不上屏，窗口里只留最后一帧', (tester) async {
    final fake = _fake();
    await _started(tester, fake);
    final mine = '${fake.payloadFor('switch.scan')!['switch_id']}';

    fake.emit(EngineEvent(EngineEventNames.sourceSwitchProgress,
        _with(_progressFrame(), <String, Object?>{'switch_id': '999999', 'done': 1})));
    await tester.pump(const Duration(milliseconds: 600));
    await tester.pumpAndSettle();
    expect(find.text(kSwitchScanningStatus), findsOneWidget,
        reason: '上一轮那一帧带着 done=1：守门失效就会把它画成"扫到 1/3"');
    expect(find.textContaining('个书源'), findsNothing);

    // 本轮的帧：窗口没到就不许提前报分数（0.5s 合流是这一屏自己的规矩）。
    fake.emit(EngineEvent(EngineEventNames.sourceSwitchProgress,
        _with(_progressFrame(), <String, Object?>{'switch_id': mine, 'done': 1})));
    await tester.pump();
    expect(find.textContaining('个书源'), findsNothing, reason: '提前落屏就不是合流了');
    fake.emit(EngineEvent(EngineEventNames.sourceSwitchProgress,
        _with(_progressFrame(), <String, Object?>{'switch_id': mine, 'done': 2})));
    await tester.pump(const Duration(milliseconds: 600));
    await tester.pumpAndSettle();
    expect(find.text('正在扫描 2/3 个书源'), findsOneWidget);
    expect(find.text('正在扫描 1/3 个书源'), findsNothing,
        reason: '窗口里两帧只留最后一帧：done=1 那一帧整树都不必重画');
  });

  testWidgets('收尾那一帧：三行候选按服务端排好的序上屏，句子说的是候选不是计数', (tester) async {
    final fake = _fake();
    await _started(tester, fake);

    final finished = _finishedPayload();
    expect(finished.containsKey('done'), isFalse,
        reason: '计数只在进度帧里发；这里读成 0 就会把跑完的一轮写成"扫了 0 个源"');
    fake.emit(_finished(fake));
    await tester.pumpAndSettle();

    expect(find.text('扫描完成，3 个候选'), findsOneWidget);
    expect(find.textContaining('个书源'), findsNothing);
    expect(_rows, findsNWidgets(3));
    // 顺序原样（`rank_switch_matches` 排的那一份）：同站两本——a 站的 book/1 与 book/2 都在。
    expect(_candidateTitles(tester), <String?>[_bookName, _bookName, '$_bookName 卷二'],
        reason: '客户端重排过一次，两侧就只剩两份主观可比');
    expect(_bar, findsNothing, reason: '收尾三种都会把 running 落下来');
    expect(_start, findsOneWidget);
  });

  testWidgets('被叫停那一帧：句子是已停止，转圈跟着停', (tester) async {
    final fake = _fake();
    await _started(tester, fake);
    fake.emit(_finished(fake, <String, Object?>{'cancelled': true}));
    await tester.pumpAndSettle();

    expect(find.text(kSwitchStoppedStatus), findsOneWidget);
    expect(find.text('扫描完成，3 个候选'), findsNothing,
        reason: '被叫停那一轮扫到哪儿算哪儿，写成"扫描完成"是界面在替用户下结论');
    expect(_bar, findsNothing);
  });

  testWidgets('起扫前就出错那一帧：换源失败那一句带原因，不许折成"没找到同名书"', (tester) async {
    final fake = _fake();
    await _started(tester, fake);
    // 出错 + 候选为空是这一屏最容易混的一格：`error` 非空与"扫完了没同名书"是两种形状。
    fake.emit(_finished(fake, <String, Object?>{
      'error': '取不到那条书源',
      'candidates': <Object?>[],
    }));
    await tester.pumpAndSettle();

    expect(find.text('换源失败：取不到那条书源'), findsOneWidget);
    expect(find.text(kSwitchEmptyCandidates), findsOneWidget,
        reason: '列表那一句说的是"这里会列出候选"，它不许顶掉状态槽里的原因');
    expect(find.text('扫描完成，3 个候选'), findsNothing);
    expect(_bar, findsNothing);
  });

  // ------------------------------------------------------------------ #
  // 停止
  // ------------------------------------------------------------------ #
  testWidgets('点「停止」而 switch.cancel 回 false：那一轮不是这一屏叫停的，句子得改口', (tester) async {
    final fake = _fake(run: _runningRun());
    await _open(tester, fake);
    expect(_stop, findsOneWidget);

    await tester.tap(_stop);
    await tester.pumpAndSettle();

    expect(fake.callsFor('switch.cancel'), 1);
    expect(fake.switchCancelResult, isFalse, reason: '样本存的就是 false：什么都没在跑');
    expect(find.text(kSwitchStoppedStatus), findsNothing,
        reason: '那一格说的是"有没有真叫停下什么"，false 时这句是界面在说一件没发生过的事');
    expect(find.text(kSwitchNothingRunningStatus), findsOneWidget);
    expect(_stop, findsOneWidget, reason: '没叫停到就还在跑：入口不许跟着文案换形状');
  });

  testWidgets('点「停止」叫停到了：句子是已停止，转圈就地退场', (tester) async {
    final fake = _fake(cancelled: true, run: _runningRun());
    await _open(tester, fake);

    await tester.tap(_stop);
    await tester.pumpAndSettle();

    expect(find.text(kSwitchStoppedStatus), findsOneWidget);
    expect(find.text(kSwitchNothingRunningStatus), findsNothing);
    expect(_bar, findsNothing);
    expect(_start, findsOneWidget);
  });

  // ------------------------------------------------------------------ #
  // 写回
  // ------------------------------------------------------------------ #
  testWidgets('点一条候选：switch.apply 三格各就各位，句子是已切换到 X，书架跟着重读', (tester) async {
    final record = _recordFromSample();
    final fake = _fake();
    await _open(tester, fake, record: record);
    // 这一屏自己从没读书架；那一发只由写回后面的 reload 打出来。
    expect(fake.callsFor('bookshelf.list'), 0);

    final candidate = SwitchCandidate.fromSeed(fake.switchSeedRows[1]);
    await tester.tap(find.byKey(ValueKey(candidate.identityKey)));
    await tester.pumpAndSettle();

    expect(fake.callsFor('switch.apply'), 1);
    final payload = fake.payloadFor('switch.apply')!;
    expect(payload.keys.toSet(), <String>{'record', 'source_url', 'book'});
    expect(payload['record'], same(record), reason: '找回书架那一行');
    expect(payload['source_url'], candidate.sourceUrl,
        reason: '找回那条源：跨进程只报得到 url，服务端按它取整条源');
    expect(payload['book'], same(candidate.book.raw), reason: '候选那一本的快照');
    expect(find.text('已切换到：${candidate.book.name}'), findsOneWidget);
    // build 起手一发 + reload 一发：换成没换成都要刷（那一行可能已被另一侧删了）。
    expect(fake.callsFor('bookshelf.list'), 2);
  });

  testWidgets('applied=false 是"记录被删或与其他书冲突"，不是"换成功了"', (tester) async {
    final fake = _fake(applied: false);
    await _open(tester, fake);
    await tester.tap(
        find.byKey(ValueKey(SwitchCandidate.fromSeed(fake.switchSeedRows.first).identityKey)));
    await tester.pumpAndSettle();

    expect(find.text(kSwitchApplyConflictStatus), findsOneWidget);
    expect(find.textContaining('已切换到'), findsNothing);
    expect(fake.callsFor('bookshelf.list'), 2,
        reason: '没换成也要重读：那一行的真形状只有服务端知道');
  });

  testWidgets('取不到源那一支服务端是抛：句子与"冲突"分得开', (tester) async {
    final fake = _fake();
    await _open(tester, fake);
    // 开屏之后才置 failure：种子的两行还是真的，被测的只有写回那一发。
    fake.failure =
        EngineFailure(EngineFailureKind.engine, '取不到那条书源', action: 'switch.apply');

    await tester.tap(
        find.byKey(ValueKey(SwitchCandidate.fromSeed(fake.switchSeedRows.first).identityKey)));
    await tester.pumpAndSettle();

    expect(find.text('换源失败：取不到那条书源'), findsOneWidget);
    expect(find.text(kSwitchApplyConflictStatus), findsNothing,
        reason: '三种成因折成一句，用户就不知道该重试还是该重找书');
    expect(fake.callsFor('bookshelf.list'), greaterThanOrEqualTo(1));
  });

  // ------------------------------------------------------------------ #
  // 入口与断线
  // ------------------------------------------------------------------ #
  testWidgets('没注入那一本就上屏：说的是"没收到书"，不许拿「等待开始」盖过去', (tester) async {
    final fake = _fake();
    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(ProviderScope(
      overrides: [
        engineRepositoryProvider.overrideWithValue(fake),
        sourceSwitchProvider.overrideWith(SourceSwitchNotifier.new),
      ],
      child: const MaterialApp(home: Scaffold(body: SourceSwitchDialog())),
    ));
    await tester.pumpAndSettle();

    expect(find.textContaining('换源屏没有收到书'), findsOneWidget);
    expect(find.text('等待开始'), findsNothing,
        reason: '"这一屏没收到书"演成"还没点开始"，用户就会去点那颗按钮');
    expect(fake.callsFor('switch.seeds'), 0, reason: 'args 那一格在发字节之前就判掉了');
  });

  testWidgets('事件流断了要在屏上有声音：那一轮进度只活在这根流上', (tester) async {
    final fake = _fake(run: _runningRun());
    await _open(tester, fake);

    fake.emitStreamError('连接被对端关闭');
    await tester.pumpAndSettle();
    expect(find.textContaining('事件流断开'), findsOneWidget);
  });

  testWidgets('本地那一行没有换源入口：它没有规则源，点下去会把 origin 改写掉', (tester) async {
    final rows = (_envelope('bookshelf.list')['data']! as List)
        .cast<Map<String, Object?>>()
        .map((row) => BookshelfItem.fromJson(Map<String, Object?>.from(row)))
        .toList();
    expect(rows, isNotEmpty);
    // 拿真行改两格演"这一本是本地文件"：协议面上没有 flet 用的那一格 `local`，
    // 而 `url` 也要跟着换——留着原 url 就是两行撞同一个行键。
    final localRow = BookshelfItem.fromJson(
        Map<String, Object?>.from(rows[1].raw)
          ..['source_url'] = kLocalBookSourceUrl
          ..['url'] = 'file:///ebooks/本地一本.txt');
    expect(localRow.isLocal, isTrue);
    expect(rows[1].isLocal, isFalse, reason: '正对照：普通行本来就该有入口');

    final fake = FakeEngineRepository(
        bookshelf: <BookshelfItem>[
          ...rows.take(1),
          localRow,
          ...rows.skip(2),
        ]);
    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(fake)],
      child: const MaterialApp(home: BookshelfScreen()),
    ));
    await tester.pumpAndSettle();

    expect(find.byType(ListTile), findsNWidgets(rows.length));
    expect(find.byKey(const Key('bookshelf.switch')), findsNWidgets(rows.length - 1),
        reason: '只有本地那一行收起入口，其余各行都要有');
  });
}
