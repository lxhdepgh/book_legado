/// 四屏的 widget 测试：数据**全部从黄金样本进**，答案与 flet 投影表逐字比。
///
/// 与 `golden_contract_test.dart` 的分工：那一只比"模型吃的键"，这一只比"控件上真的
/// 长出了那一格"。R6 清点里 A 堆（控件形状 / 可见文本 / 交互结果）只有在这一层才收得了钱。
///
/// 三条仪器规矩（都是被咬过之后写下的）：
/// - **替身喂真行**：`FakeEngineRepository` 的输入是 `tests/gateway/golden/` 里的原始 dict，
///   不是手搓样例——手搓样例只会证明我自己写得对。
/// - **比对的另一头是 flet 渲染出来的串**（`tests/gateway/projection/`），不是这里抄一份。
///   抄一份就成了 Dart 与 Dart 比。
/// - 空仪器要有人响：每组都有一条"表里必须有货"的正控，抽到 0 行时循环一条不跑也能绿。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/event_names.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/check/data/check_models.dart';
import 'package:legodo_client/features/check/presentation/check_screen.dart';
import 'package:legodo_client/features/find_books/data/search_scope.dart';
import 'package:legodo_client/features/find_books/presentation/find_books_screen.dart';
import 'package:legodo_client/features/settings/data/theme_preferences.dart';
import 'package:legodo_client/features/settings/presentation/appearance_screen.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';
import 'package:legodo_client/features/sources/presentation/sources_screen.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _readJson(String relative) {
  final file = File('${_repoRoot.path}/tests/gateway/$relative');
  if (!file.existsSync()) {
    throw StateError('缺工件 ${file.path}\n'
        '样本：PYTHONIOENCODING=utf-8 PYTHONPATH=. python -u .probe/g2_golden_capture.py\n'
        '投影表：PYTHONIOENCODING=utf-8 PYTHONPATH=. python -u .probe/g2e_projection_tables.py');
  }
  return jsonDecode(file.readAsStringSync(encoding: utf8)) as Map<String, Object?>;
}

/// 某一发黄金样本的载荷（信封里那一格）。
Object? _payload(String action) =>
    Map<String, Object?>.from(_readJson('golden/$action.json')['response']! as Map)
        ['data'];

Object? _projectionRows(String table) => _readJson('projection/$table.json')['rows'];

List<SourceRecord> _goldenSources() => (_payload('source.list') as List)
    .cast<Map<String, Object?>>()
    .map(SourceRecord.fromJson)
    .toList(growable: false);

Widget _host(EngineRepository repository, Widget screen) => ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(repository)],
      child: MaterialApp(home: screen),
    );

void main() {
  group('书源屏：黄金 5 行上屏，标题与副标题就是 flet 投影那一串', () {
    /// flet `BookSourceListView._row` 渲染出来的那 5 行（case 名 `row_0..row_4`）。
    final projected = (_projectionRows('source.list.rows') as List)
        .cast<Map<String, Object?>>()
        .where((row) => '${row['case']}'.startsWith('row_'))
        .toList(growable: false);
    final names = (_projectionRows('source.batch_delete.names') as List)
        .cast<Map<String, Object?>>();

    test('两张表都有货', () {
      expect(projected.length, greaterThanOrEqualTo(5));
      expect(names.length, greaterThanOrEqualTo(5));
    });

    testWidgets('5 行的标题与副标题逐字对上投影表', (tester) async {
      final fake = FakeEngineRepository(sources: _goldenSources());
      await tester.pumpWidget(_host(fake, const SourcesScreen()));
      await tester.pumpAndSettle();

      for (final row in projected) {
        expect(find.text('${row['title']}'), findsOneWidget,
            reason: '${row['case']} 的标题没上屏');
        expect(find.text('${row['subtitle']}'), findsOneWidget,
            reason: '${row['case']} 的副标题没上屏');
      }
      // 状态行那一句的三个数是从同一批真行数出来的（当场数过：5 条全启用、
      // 逗号切分后 3 个组），分隔符 `" | "` 与"只有第一格带条"都是 flet 的形状。
      expect(find.text('共 5 条 | 启用 5 | 停用 0 | 3 个分组'), findsOneWidget);
    });

    testWidgets('勾满三条再点删除：确认框里那串名字吃的是同一张表', (tester) async {
      final fake = FakeEngineRepository(sources: _goldenSources());
      await tester.pumpWidget(_host(fake, const SourcesScreen()));
      await tester.pumpAndSettle();

      // 前三条**在屏上的**行（黄金顺序），选中集要交给批量动作前按 url 排过序，
      // 所以这里把期望串从表里那条"三条"的样本按同一排序取出来——不是手抄。
      await tester.tap(find.byType(Checkbox).at(0));
      await tester.tap(find.byType(Checkbox).at(1));
      await tester.tap(find.byType(Checkbox).at(2));
      await tester.pumpAndSettle();
      expect(find.text('已选择 3 项'), findsOneWidget);

      final selected = _goldenSources()
          .take(3)
          .map((record) => record.url)
          .toList()
        ..sort();
      final threeRows =
          names.where((row) => (row['urls']! as List).length == 3).toList();
      expect(threeRows, hasLength(1), reason: '表里"三条"那一发应当唯一');
      expect((threeRows.single['urls']! as List).cast<String>(), selected,
          reason: '表里那批 url 必须就是这里勾中的三条，否则比的是两件事');

      await tester.tap(find.byKey(const Key('sources.batchDelete')));
      await tester.pumpAndSettle();
      expect(find.text('删除 3 条书源？'), findsOneWidget);
      expect(
          find.text('将删除：${threeRows.single['expected']}\n此操作不可撤销。'),
          findsOneWidget);
    });

    testWidgets('筛选只改行数，不改那一句合计', (tester) async {
      final fake = FakeEngineRepository(sources: _goldenSources());
      await tester.pumpWidget(_host(fake, const SourcesScreen()));
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('sources.search')), '七猫');
      await tester.pumpAndSettle();
      expect(find.text('🏷七猫小说·API'), findsOneWidget);
      expect(find.text('📂绿色小说'), findsNothing);
      // 状态行读的是整份记录表，不是筛出来的那几条（flet 那边同一口径）。
      expect(find.text('共 5 条 | 启用 5 | 停用 0 | 3 个分组'), findsOneWidget);
    });

    testWidgets('读不到不许画成"共 0 条"：那一屏是失败，不是空库', (tester) async {
      final fake = FakeEngineRepository(
          sources: _goldenSources(),
          failure: const EngineFailure(EngineFailureKind.unavailable, '引擎没起来'));
      await tester.pumpWidget(_host(fake, const SourcesScreen()));
      await tester.pumpAndSettle();

      expect(find.textContaining('共 0 条'), findsNothing);
      expect(find.textContaining('书源列表为空'), findsNothing);
      // 两处都报失败：中间那块面板与底下的状态行（flet 那边是同一句写在两处）。
      expect(find.textContaining('加载书源失败'), findsWidgets);
    });
  });

  group('检测屏：收尾那一句与结果行都从快照来', () {
    /// flet `_summary_line` 在**同一份载荷**上渲染出的那一句（投影表 `as_captured`）。
    String capturedLine() {
      final rows = (_projectionRows('check.snapshot.summary_line') as List)
          .cast<Map<String, Object?>>();
      expect(rows, isNotEmpty);
      return '${rows.firstWhere((row) => row['case'] == 'as_captured')['expected']}';
    }

    testWidgets('黄金快照那一发：结论句与那一行都逐字对上', (tester) async {
      final snapshot =
          CheckSnapshot.fromJson(Map<String, Object?>.from(_payload('check.snapshot')! as Map));
      final fake = FakeEngineRepository(snapshot: snapshot);
      await tester.pumpWidget(_host(fake, const CheckScreen()));
      await tester.pumpAndSettle();

      expect(find.text(capturedLine()), findsOneWidget,
          reason: '界面上那句收尾不是 flet 渲染出来的同一句');
      // 行：标题是 source_id 原样，副标题 `状态 · 摘要`（`check_view.py:88-90`）。
      // 那一格中文**写死**而不写 `row.label`：读被测的那张表来断言那张表，改错了也
      // 不会红（变异核验里这一发真的打不红过）。'列表可用' 的两个出处：
      // `app/views/source/check_view.py:17` 的 `CHECK_STATE_LABELS`，与样本里
      // `results[0].state_class == 'list_usable'`；跨语言那条由
      // `test_check_state_vocabulary_is_one_set_three_places` 双向查。
      final row = snapshot.results.single;
      expect(row.stateClass, 'list_usable', reason: '样本那一行的档位，当场数过');
      expect(find.text(row.sourceId), findsOneWidget);
      expect(
          find.text('列表可用 · G2分类源：list_usable（本轮快速检测，未验正文）'),
          findsOneWidget);
    });

    testWidgets('同一源再来一条事件：覆盖那一行，不追加第二条', (tester) async {
      final snapshot =
          CheckSnapshot.fromJson(Map<String, Object?>.from(_payload('check.snapshot')! as Map));
      final fake = FakeEngineRepository(snapshot: snapshot);
      await tester.pumpWidget(_host(fake, const CheckScreen()));
      await tester.pumpAndSettle();

      final sample = _readJson('golden/check.start.json');
      final eventSample = Map<String, Object?>.from(sample['event_sample']! as Map);
      final payload = Map<String, Object?>.from(eventSample['payload']! as Map);
      expect(payload['source_id'], snapshot.results.single.sourceId,
          reason: '事件样本与快照必须是同一条源，否则去重那支没被碰到');
      fake.emit(EngineEvent(EngineEventNames.checkFinished, payload));
      await tester.pump(const Duration(milliseconds: 600));
      fake.emit(EngineEvent(EngineEventNames.checkFinished, payload));
      await tester.pumpAndSettle();

      expect(find.byType(ListTile), findsOneWidget,
          reason: '同源重测要改的是同一行（`check_view.py:92-97` 的 `_row_at` 覆盖）');
    });

    testWidgets('跑着的时候按钮是反的，中间那行报的是进行中', (tester) async {
      final snapshot =
          CheckSnapshot.fromJson(Map<String, Object?>.from(_payload('check.snapshot')! as Map));
      final fake = FakeEngineRepository(
          snapshot: snapshot.copyWith(running: true));
      await tester.pumpWidget(_host(fake, const CheckScreen()));
      await tester.pumpAndSettle();

      expect(tester.widget<FilledButton>(find.byKey(const Key('check.start'))).onPressed,
          isNull, reason: '在跑就不派第二遍全量联网');
      expect(
          tester.widget<OutlinedButton>(find.byKey(const Key('check.stop'))).onPressed,
          isNotNull);
      expect(find.textContaining('检测进行中'), findsOneWidget);
      expect(find.text(capturedLine()), findsNothing);
    });

    testWidgets('没跑过就说"尚未检测"：空快照不许冒充跑完', (tester) async {
      final fake = FakeEngineRepository();
      await tester.pumpWidget(_host(fake, const CheckScreen()));
      await tester.pumpAndSettle();

      expect(find.text('尚未检测'), findsOneWidget);
      expect(find.textContaining('检测完成'), findsNothing);
      expect(find.text('还没有一条检测结果'), findsOneWidget);
    });
  });

  group('找书屏：搜索范围那张表整个来自 search.scopes', () {
    List<SearchScope> golden() => (_payload('search.scopes') as List)
        .cast<Map<String, Object?>>()
        .map(SearchScope.fromJson)
        .toList(growable: false);

    testWidgets('弹窗里前两条就是表里前两条：全部 920 源、未分组 0 源', (tester) async {
      final rows = golden();
      expect(rows.length, greaterThanOrEqualTo(19));
      // 这两条都是从样本当场数的：920 是 `__all__` 的源数，而 `未分组` 那一档
      // **真的是 0**（库里每条源都带组）——它被留着不是因为算错了，是范围表不删零档。
      expect(rows.first.value, kScopeAll);
      expect(rows.first.sourceCount, 920);
      expect(rows[1].value, kScopeUngrouped);
      expect(rows[1].sourceCount, 0);

      final fake = FakeEngineRepository(sources: _goldenSources(), scopes: rows);
      await tester.pumpWidget(_host(fake, const FindBooksScreen()));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('findbooks.openSearch')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('findbooks.scope')));
      await tester.pumpAndSettle();

      // 「搜索范围」那一串在触发条与弹窗标题上各写一次，所以按对话框收窄。
      final dialog = find.byType(AlertDialog);
      expect(find.descendant(of: dialog, matching: find.text('搜索范围')),
          findsOneWidget);
      expect(find.descendant(of: dialog, matching: find.text('全部书源（920 源）')),
          findsOneWidget);
      expect(find.descendant(of: dialog, matching: find.text('未分组（0 源）')),
          findsOneWidget);
    });
  });

  group('外观屏：theme.preferences 那一发落到控件上', () {
    testWidgets('样本里存的是暖琥珀 / 浅色 / 墨水屏关，控件就长这样', (tester) async {
      final theme = ThemePreferences.fromJson(
          Map<String, Object?>.from(_payload('theme.preferences')! as Map));
      expect(theme, ThemePreferences(
          skinId: 'warm_amber', mode: ThemeModeKind.light, eink: false),
          reason: '载荷当场数过：{skin_id: warm_amber, mode: light, eink: false}');

      final fake = FakeEngineRepository(theme: theme);
      await tester.pumpWidget(_host(fake, const AppearanceScreen()));
      await tester.pumpAndSettle();

      expect(tester.widget<DropdownButton<String>>(
          find.byKey(const Key('appearance.skin'))).value, 'warm_amber');
      expect(find.text('暖琥珀'), findsOneWidget,
          reason: '下拉里选中那格显示的是皮肤名，不是 id');
      expect(tester.widget<RadioGroup<ThemeModeKind>>(
              find.byType(RadioGroup<ThemeModeKind>)).groupValue,
          ThemeModeKind.light);
      expect(tester.widget<Switch>(find.byKey(const Key('appearance.eink'))).value,
          isFalse);
      expect(find.text('有未保存的改动'), findsNothing,
          reason: '刚载入时 pending==saved，不该亮脏标记');
    });
  });
}
