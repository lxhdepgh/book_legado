/// 书架屏的 widget 测试：数据全部从 `FakeEngineRepository` 进，**不起进程、不发字节**。
///
/// 这一支替的是 R6 清点里 A 堆那 1,315 行的形状（控件树形状 / 可见文本 / 交互结果）；
/// 它明确**不**声称覆盖了像素与滚动 —— 那批仪器在 `.probe/` 里，flet 那侧也从来没有
/// 一条测试真起过客户端。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/app/bootstrap.dart';
import 'package:legodo_client/app/router.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';
import 'package:legodo_client/features/bookshelf/presentation/bookshelf_screen.dart';

/// 黄金样本里的 7 行——不是手搓样例：手搓只会证明我自己写得对。
List<BookshelfItem> _goldenRows() {
  final sample = _loadSample('golden/bookshelf.list.json');
  final envelope = Map<String, Object?>.from(sample['response']! as Map);
  return (envelope['data']! as List)
      .map((row) => BookshelfItem.fromJson(Map<String, Object?>.from(row as Map)))
      .toList();
}

/// 读 `tests/gateway/` 底下的一份工件，路径相对那一层（`golden/…` 真响应样本、
/// `projection/…` flet 侧投影表）：拼法只许有一处，两处各写各的迟早指错目录。
Map<String, Object?> _loadSample(String relative) => jsonDecode(
        File('${Directory.current.parent.path}/tests/gateway/$relative')
            .readAsStringSync(encoding: utf8)) as Map<String, Object?>;

Widget _host(EngineRepository repository) => ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(repository)],
      child: const MaterialApp(home: BookshelfScreen()),
    );

/// 走**路由 + 壳**的那一臂：`_host` 直接把屏幕挂上去，绕过了 AppShell，
/// 于是"宽档那臂没带 child"这一类壳自己的 bug 它永远照不出来。
Widget _shellHost(EngineRepository repository) => ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(repository)],
      child: MaterialApp.router(routerConfig: buildRouter()),
    );

void main() {
  testWidgets('没注入仓库就不许装成"书架是空的"：那一屏是错的形状', (tester) async {
    // 默认仓库是 `UnwiredEngineRepository`（`当前环境不支持 bookshelf.list`）——
    // 这一臂就是它的正对照：没有引擎 ≠ 没有书，两条文案必须不同。
    await tester.pumpWidget(
        const ProviderScope(child: MaterialApp(home: BookshelfScreen())));
    await tester.pumpAndSettle();

    expect(find.text('书架是空的'), findsNothing);
    expect(find.textContaining('书架共 0 本'), findsNothing);
    // 两处都得报"失败"：面板标题带码名，状态行是短的那条。
    // 用 textContaining('书架加载失败') 会一次撞到两个 widget —— 那不是重复，
    // 是这一屏本来就在两个位置说同一件事，所以按精确文本各数一次。
    expect(find.text('书架加载失败（unavailable）'), findsOneWidget);
    expect(find.text('书架加载失败'), findsOneWidget);
  });

  testWidgets('黄金样本的 7 行全落在树上，文本与 flet 那屏同源', (tester) async {
    final rows = _goldenRows();
    final fake = FakeEngineRepository(bookshelf: rows);
    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(_host(fake));
    await tester.pumpAndSettle();

    expect(find.byType(ListTile), findsNWidgets(rows.length));
    for (final row in rows) {
      expect(find.text(row.name), findsOneWidget);
      if (row.subtitle.isNotEmpty) {
        expect(find.text(row.subtitle), findsOneWidget);
      }
    }
    // 状态行与 flet 的 `set_status_text(f"书架共 {N} 本")` 同文案。
    expect(find.text('书架共 ${rows.length} 本'), findsOneWidget);
    expect(fake.listBookshelfCalls, 1);
  });

  testWidgets('空书架说的是"空的"，不是"加载失败"', (tester) async {
    await tester.pumpWidget(_host(FakeEngineRepository()));
    await tester.pumpAndSettle();
    expect(find.text('书架是空的'), findsOneWidget);
    expect(find.textContaining('加载失败'), findsNothing);
    expect(find.text('书架共 0 本'), findsOneWidget);
  });

  testWidgets('首屏没数据就炸 → 错误屏 + 重试按钮，绝不报"共 0 本"', (tester) async {
    final fake = FakeEngineRepository(
        failure: const EngineFailure(EngineFailureKind.unavailable, '引擎没起来'));
    await tester.pumpWidget(_host(fake));
    await tester.pumpAndSettle();

    expect(find.textContaining('书架加载失败（unavailable）'), findsOneWidget);
    expect(find.text('书架加载失败'), findsOneWidget);
    expect(find.text('书架共 0 本'), findsNothing,
        reason: '"没拿到数据"说成"今天没书"是这一屏最坏的一种谎');
    expect(find.byKey(const Key('bookshelf.retry')), findsOneWidget);

    fake.failure = null;
    fake.bookshelf = _goldenRows();
    await tester.tap(find.byKey(const Key('bookshelf.retry')));
    await tester.pumpAndSettle();
    expect(find.text('书架共 7 本'), findsOneWidget);
  });

  testWidgets('不可重试的失败（schema）不把重试按钮递到用户手里', (tester) async {
    await tester.pumpWidget(_host(FakeEngineRepository(
        failure: const EngineFailure(EngineFailureKind.schema, '入参不符'))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('bookshelf.retry')), findsNothing);
  });

  testWidgets('刷新失败留着旧数据：屏幕不许被换成错误页', (tester) async {
    final rows = _goldenRows();
    final fake = FakeEngineRepository(bookshelf: rows);
    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(_host(fake));
    await tester.pumpAndSettle();
    expect(find.text('书架共 ${rows.length} 本'), findsOneWidget);

    fake.failure =
        const EngineFailure(EngineFailureKind.timeout, '这一发超时了');
    await tester.tap(find.byKey(const Key('bookshelf.refresh')));
    await tester.pumpAndSettle();

    // 旧行还在原位（这是 flet 那侧 `logger.warning` + return 的等价形状）。
    expect(find.text('书架共 ${rows.length} 本'), findsOneWidget);
    expect(find.textContaining('书架加载失败'), findsNothing);
    // 但它有声音：横幅。
    expect(find.textContaining('刷新失败：这一发超时了'), findsOneWidget);

    // 横幅关得掉——关不掉的那条横幅会在用户下一次进同一屏时还挂着。
    await tester.tap(find.byIcon(Icons.close));
    await tester.pumpAndSettle();
    expect(find.textContaining('刷新失败'), findsNothing);
  });

  testWidgets('加载中那一拍有形状，且刷新按钮在那一拍是disabled', (tester) async {
    final fake = FakeEngineRepository(
        bookshelf: _goldenRows(), delay: const Duration(milliseconds: 120));
    await tester.pumpWidget(_host(fake));
    await tester.pump();
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.text('正在加载书架…'), findsOneWidget);
    final button = tester.widget<IconButton>(
        find.byKey(const Key('bookshelf.refresh')));
    expect(button.onPressed, isNull, reason: '在飞的时候再点一发 = 自己顶自己');
    await tester.pumpAndSettle();
    expect(find.byType(ListTile), findsNWidgets(7));
  });

  testWidgets('失败之后退避窗口内一发都不许多发（Riverpod 默认会自己重 10 次）',
      (tester) async {
    // 这一条是拿真网关打出来的：第八十三轮的探针里，一次失败后面跟着 **11** 发
    // `bookshelf.list`——Riverpod 没指定 retry 时用的是 `ProviderContainer.defaultRetry`
    // （200ms×2^n，封顶 6.4s，共 10 次）。表现有两层坏：客户端背着用户打网关，
    // 以及每一发都把"书架加载失败"那一屏换回转圈，用户只看到闪。
    final fake = FakeEngineRepository(
        failure: const EngineFailure(EngineFailureKind.timeout, '引擎没起来'));
    await tester.pumpWidget(_host(fake));
    await tester.pump();
    expect(fake.listBookshelfCalls, 1);

    // 推到 60s：defaultRetry 那 10 发的累计窗口约 20s，60s 还没有第二发才算真关掉。
    for (var i = 0; i < 60; i++) {
      await tester.pump(const Duration(seconds: 1));
    }
    expect(fake.listBookshelfCalls, 1,
        reason: '要不要再来一发由用户按那颗"重试"决定，不由框架决定');
    expect(find.text('书架加载失败（timeout）'), findsOneWidget,
        reason: '错误屏得留在原地，不许被自动重试换回加载态');
  });

  testWidgets('窄档与宽档都得看得见书架：壳的那一臂修的是"宽屏整屏空白"', (tester) async {
    final fake = FakeEngineRepository(bookshelf: _goldenRows());
    for (final size in const <Size>[Size(500, 1200), Size(1200, 1200)]) {
      await tester.binding.setSurfaceSize(size);
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.pumpWidget(_shellHost(fake));
      await tester.pumpAndSettle();
      // G2 之后起手落在阅读组的「找书」那一签，所以这一臂必须自己切过来：
      // 少这一跳，两条断言量的是找书页，"宽屏整屏空白"那条护栏就静默失效了。
      await tester.tap(find.byKey(const ValueKey('tab-$readingGroupRoute-书架')));
      await tester.pumpAndSettle();
      expect(find.text('书架共 7 本'), findsOneWidget, reason: '$size 那一档');
      expect(find.byType(ListTile), findsNWidgets(7), reason: '$size 那一档');
    }
  });

  testWidgets('屏幕文本逐行 = flet 投影表：书名、副标题、状态行三样都不许是 Dart 自己算的',
      (tester) async {
    // 比对的**另一方**是 `.probe/g1_flet_projection.py` 从 flet 那套真投影
    // （`_to_render_record` + `_row` 的副标题拼法 + `_load` 的状态行）导出的表。
    // 输入还是黄金样本那批原始行——于是这条链是：
    //   原始行 →(Python 投影)→ 这张表 ‖ 原始行 →(Dart 投影)→ 屏幕上这些字。
    // Dart 的 ` · `/`读到 ` 拼法错了、或者进度归一取错键，这里当场对不上。
    final projection = _loadSample('projection/bookshelf.list.json');
    final expected =
        (projection['rows']! as List).cast<Map<String, Object?>>();
    expect(expected, hasLength(7), reason: '投影表与黄金样本同一批行，7 本');

    await tester.binding.setSurfaceSize(const Size(1200, 1600));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(_host(FakeEngineRepository(bookshelf: _goldenRows())));
    await tester.pumpAndSettle();

    expect(find.text(projection['status_line']! as String), findsOneWidget,
        reason: 'flet 的 set_status_text 与这屏的状态行必须同一句');
    for (final row in expected) {
      expect(find.text(row['name']! as String), findsOneWidget,
          reason: '书名那一格：${row['name']}');
      expect(find.text(row['subtitle']! as String), findsOneWidget,
          reason: '副标题那一格整条照抄 flet：${row['subtitle']}');
    }
    expect(find.textContaining('加载失败'), findsNothing);
  });

  testWidgets('连不上引擎那一屏：文案里必须带上是哪一步没通', (tester) async {
    await tester.pumpWidget(bootFailureScope(
        const EngineFailure(EngineFailureKind.timeout, '60s 没等到 LEGODO_GATEWAY')));
    await tester.pumpAndSettle();
    expect(find.textContaining('连不上引擎（unavailable）'), findsNothing);
    expect(find.textContaining('连不上引擎（timeout）'), findsOneWidget);
    expect(find.textContaining('60s 没等到'), findsOneWidget);
  });
}
