/// 书源导入那一屏：预览（纯解析）+ 结果卡 + 二次确认的文案。
///
/// 载荷读 `tests/gateway/golden/import.text.json` 的 `response.data`——那五键是
/// Python 网关真发过的回话，Go 侧 `pkg/ffi/import_test.go` 钉的是同一批键名；
/// 两边都从这一份读，改了形状两头一起红，不许在这儿手写一只"想要的样子"。
///
/// 这一支钉的四件事，都是"屏幕看着像"照不出来的那一类：
/// 1. **URL 那一路发的 mode 是 append**：预览卡上的追加/替换只属于文件那一路，
///    在线地址没有预览（文本还没到手），发 replace 等于让用户没确认过就清库。
/// 2. **`placeholder` 不许长成「成功 0 条」**（同 [importOutcomeLine] 那条注释）。
/// 3. **`EngineFailure` 要在卡上成一句话**，不是静默：导入是写库动作，
///    没声音的失败会让人以为已经导完了。
/// 4. **count>0 之后无条件重取列表**：那一发 `SourceChangedEvent` 经轮询上来有延迟，
///    用户点了导入就该立刻看见新行数（flet 的 `_load()` 同一条）。
///
/// 文件那一路（`FilePicker`）不在这里测：它要的是原生平台通道，widget 测试里只能拿到
/// 替身；预览卡的解析规矩由 [BookSourcePreview] 那组纯函数测试钉。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/sources/data/book_source_preview.dart';
import 'package:legodo_client/features/sources/data/import_outcome.dart';
import 'package:legodo_client/features/sources/presentation/import_dialog.dart';

final Directory _repoRoot = Directory.current.parent;

/// 黄金样本里那只五格回话（原样，不就地改）。
final Map<String, Object?> _goldenData = () {
  final sample = jsonDecode(File(
          '${_repoRoot.path}/tests/gateway/golden/import.text.json')
      .readAsStringSync(encoding: utf8)) as Map<String, Object?>;
  return Map<String, Object?>.from(
      (sample['response']! as Map<String, Object?>)['data']! as Map);
}();

ImportOutcome _outcomeFromGolden() => ImportOutcome(
      kind: '${_goldenData['kind']}',
      count: (_goldenData['count']! as num).toInt(),
      errors: const <String>[],
      warnings: const <String>[],
      placeholder: _goldenData['placeholder']! as bool,
    );

Future<void> _pumpDialog(WidgetTester tester, FakeEngineRepository fake) async {
  await tester.binding.setSurfaceSize(const Size(900, 1200));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: MaterialApp(
      home: Scaffold(
        body: SourceImportDialog(existingCount: 2),
      ),
    ),
  ));
  await tester.pumpAndSettle();
}

Future<void> _importUrl(WidgetTester tester, String url) async {
  await tester.enterText(find.byKey(const Key('sources.import.urlField')), url);
  await tester.tap(find.byKey(const Key('sources.import.urlButton')));
  await tester.pumpAndSettle();
}

void main() {
  group('BookSourcePreview.parse（只解析，不落库）', () {
    test('分组按条数降序、同数按名，缺分组归「未分组」', () {
      final preview = BookSourcePreview.parse(jsonEncode(<Object?>[
        <String, Object?>{'bookSourceUrl': 'https://a/', 'bookSourceGroup': '零九'},
        <String, Object?>{'bookSourceUrl': 'https://b/', 'bookSourceGroup': '主推'},
        <String, Object?>{'bookSourceUrl': 'https://c/', 'bookSourceGroup': '主推'},
        <String, Object?>{'bookSourceUrl': 'https://d/'},
      ]));
      expect(preview.total, 4);
      expect(preview.valid, 4);
      expect(preview.groups, <GroupCount>[(name: '主推', count: 2), (name: '未分组', count: 1), (name: '零九', count: 1)]);
      expect(preview.groupsLine, '主推 2 条、未分组 1 条、零九 1 条');
      expect(preview.canImport, isTrue);
      expect(preview.summaryLine, '可导入 4 条');
    });

    test('缺 bookSourceUrl 的条目计入跳过，不进分组', () {
      final preview = BookSourcePreview.parse(jsonEncode(<Object?>[
        <String, Object?>{'bookSourceUrl': '  ', 'bookSourceGroup': '主推'},
        <String, Object?>{'name': '整条都没有地址'},
        <String, Object?>{'bookSourceUrl': 'https://a/', 'bookSourceGroup': '主推'},
      ]));
      expect((preview.valid, preview.broken, preview.total), (1, 2, 3));
      expect(preview.groups, <GroupCount>[(name: '主推', count: 1)],
          reason: '坏条目也算分组的话，那句"共几条"就比可导入的多，读的人以为漏了');
      expect(preview.summaryLine, '可导入 1 条，跳过 2 条缺书源地址');
    });

    test('一条都导不进来时不给一张要点确认的空卡', () {
      final preview = BookSourcePreview.parse(jsonEncode(<Object?>[
        <String, Object?>{'txtDir': 'a'},
        <String, Object?>{'txtDir': 'b'},
      ]));
      expect(preview.canImport, isFalse);
      expect(preview.error, '这 2 项里没有带 bookSourceUrl 的书源，是不是选错了文件？');
      expect(preview.summaryLine, startsWith('这个文件不能导入：'));
      expect(preview.groupsLine, isEmpty);
    });

    test('顶层单对象算一条；标量与语法错各说各话', () {
      expect(BookSourcePreview.parse('{"bookSourceUrl":"https://a/"}').valid, 1,
          reason: 'Legado 允许直接导一条，折成数组是一条而不是"顶层应为数组"');
      expect(BookSourcePreview.parse('"就是一个字符串"').error, '书源 JSON 顶层应为数组或对象');
      expect(BookSourcePreview.parse('[{"bookSourceUrl":').error, startsWith('JSON 解析失败：'));
    });

    test('多于 8 组只列前 8 并补一句总数', () {
      final preview = BookSourcePreview.parse(jsonEncode(<Object?>[
        for (int i = 0; i < 9; i++)
          <String, Object?>{'bookSourceUrl': 'https://$i/', 'bookSourceGroup': '组$i'},
      ]));
      expect(preview.groups.length, 9);
      expect(preview.groupsLine, contains('… 共 9 个分组'));
      expect(preview.groupsLine.split('、').length, 8);
    });
  });

  group('importOutcomeLine（结果卡那句）', () {
    test('占位那一支不并进「成功 N 条」句式', () {
      const placeholder = ImportOutcome(
          kind: 'book_source',
          count: 0,
          errors: <String>[],
          warnings: <String>['RSS 源请在书源管理里添加'],
          placeholder: true);
      expect(importOutcomeLine(placeholder), contains('书源未落地（占位）'));
      expect(importOutcomeLine(placeholder), isNot(contains('成功 0 条')));
    });

    test('真落库的那句报条数', () {
      expect(importOutcomeLine(_outcomeFromGolden()), '书源导入完成：成功 1 条',
          reason: '数字来自黄金样本，不是这里写的');
    });
  });

  group('导入对话框', () {
    testWidgets('在线地址：发 append、成功后那句报条数，并且重取列表', (tester) async {
      final fake = FakeEngineRepository()
        ..importOutcome = _outcomeFromGolden();
      await _pumpDialog(tester, fake);

      await _importUrl(tester, '  https://abc/bookSource.json  ');
      expect(fake.callsFor('import.url'), 1);
      expect(fake.payloadFor('import.url'), <String, Object?>{
        'url': 'https://abc/bookSource.json',
        'kind': 'book_source',
        'mode': 'append',
      }, reason: '在线那一路没有预览卡，模式只能是追加——发 replace 等于没确认就清库');
      expect(find.text('书源导入完成：成功 1 条'), findsOneWidget);
      expect(fake.callsFor('source.list'), greaterThanOrEqualTo(1),
          reason: 'count>0 就要重取；那一发 SourceChangedEvent 经轮询上来有延迟');
    });

    testWidgets('空白地址不发字节', (tester) async {
      final fake = FakeEngineRepository();
      await _pumpDialog(tester, fake);
      await _importUrl(tester, '   ');
      expect(fake.callsFor('import.url'), 0);
      expect(find.textContaining('导入完成'), findsNothing);
    });

    // 这一屏写死 `kind=book_source`，服务侧对 book_source 永不回 placeholder；
    // 这一发是把**线上真能带的那一格**注入进来，钉的是渲染而不是请求。
    testWidgets('服务侧回"未落地（占位）"时不许演成成功', (tester) async {
      final fake = FakeEngineRepository()
        ..importOutcome = const ImportOutcome(
            kind: 'book_source',
            count: 0,
            errors: <String>[],
            warnings: <String>['RSS 源请在书源管理里添加'],
            placeholder: true);
      await _pumpDialog(tester, fake);
      await _importUrl(tester, 'https://abc/rss.json');

      expect(find.textContaining('书源未落地（占位）'), findsOneWidget);
      expect(find.textContaining('导入完成'), findsNothing,
          reason: '落库一行都没写和"文件里本来就没东西"在界面上必须不同形');
      expect(find.text('· RSS 源请在书源管理里添加'), findsOneWidget);
      expect(fake.callsFor('source.list'), 0, reason: 'count=0 不重取，占位那发没有新行数');
    });

    testWidgets('EngineFailure 在卡上是一句话，不是静默', (tester) async {
      final fake = FakeEngineRepository()
        ..failure = const EngineFailure(EngineFailureKind.engine,
            '未知资源类型："rsss"', action: 'import.text');
      await _pumpDialog(tester, fake);
      await _importUrl(tester, 'https://abc/a.json');

      expect(find.text('导入失败：未知资源类型："rsss"'), findsOneWidget);
      expect(find.textContaining('导入完成'), findsNothing,
          reason: '失败那一路不能同时给一句成功汇总');
    });

    testWidgets('errors 逐条带「· 」前缀，与 warnings 分开两批', (tester) async {
      final fake = FakeEngineRepository()
        ..importOutcome = const ImportOutcome(
            kind: 'book_source',
            count: 1,
            errors: <String>['跳过非法书源: 第 2 项'],
            warnings: <String>['替换前请备份'],
            placeholder: false);
      await _pumpDialog(tester, fake);
      await _importUrl(tester, 'https://abc/a.json');

      expect(find.text('书源导入完成：成功 1 条'), findsOneWidget);
      expect(find.text('· 跳过非法书源: 第 2 项'), findsOneWidget);
      expect(find.text('· 替换前请备份'), findsOneWidget);
    });

    testWidgets('在途锁住发起入口：重复点第二下不会叠着灌两份', (tester) async {
      final fake = FakeEngineRepository(
        delay: const Duration(milliseconds: 50),
      )..importOutcome = _outcomeFromGolden();
      await _pumpDialog(tester, fake);

      await tester.enterText(find.byKey(const Key('sources.import.urlField')),
          'https://abc/bookSource.json');
      await tester.tap(find.byKey(const Key('sources.import.urlButton')));
      await tester.pump(); // 在途：进度条上屏、按钮置灰
      expect(find.byType(LinearProgressIndicator), findsOneWidget);
      await tester.tap(find.byKey(const Key('sources.import.urlButton')),
          warnIfMissed: false);
      await tester.pumpAndSettle();
      expect(fake.callsFor('import.url'), 1,
          reason: '两份文件叠着往库里灌，第二份还是 replace 的话第一份就白写了');
    });
  });
}
