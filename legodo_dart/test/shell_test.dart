/// 壳与档位（§6.2 那条"目的地是 shell 的 child"、以及第七十八轮那条 720 阈值）。
///
/// 这一支的存在理由很具体：G1 第一版的宽档分支只回了 `NavigationRail` 没带 `child`，
/// 于是"窗口一宽整屏空白"——而当时跑着的 `bookshelf_screen_test` 是直接把屏幕挂到
/// `MaterialApp` 上的，永远绕过了壳，一点声音都没有。所以这里专门从**壳**进，
/// 并且两档都真渲染一次（读代码不算验证：那一版代码看上去完全正确）。
///
/// G2 之后目的地从一条变三条（分组入口），这一支跟着改三处：
/// 窄档那条 `destinations.length < 2 ? null :` 现在**会**走上有栏那一侧，
/// 于是"没栏"那一臂必须自己注入一条目的地才还成立（它就是那条负断言的正对照）。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:legodo_client/app/router.dart';
import 'package:legodo_client/app/shell.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';
import 'package:legodo_client/features/find_books/presentation/find_books_screen.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';

BookshelfItem _row(String name) => BookshelfItem(
      name: name,
      author: '作者',
      chapterName: '',
      coverUrl: '',
      url: 'https://example.com/$name',
      sourceUrl: 'https://example.com',
      raw: const {},
    );

/// 一条书源记录：落地页（找书）的状态行按 `source.list` 的条数说话，
/// 喂 0 条时那一句与"仓库没接上"长得太像，所以这里给一条真的。
SourceRecord _source(String name) => SourceRecord(
      url: 'https://example.com/$name',
      name: name,
      group: '',
      comment: '',
      enabled: true,
      loginUrl: '',
      raw: const {},
    );

/// 把壳挂到**指定窗口宽度**下渲染一次。
///
/// 为什么不是 `SizedBox(width: 500)` 包一层：`MaterialApp` 的 home 拿到的是**夹紧了**
/// 的根约束（测试窗口默认 800×600），SizedBox 的 500 会被那对约束吃掉——
/// 我第一版就是这么写的，于是"窄档那两臂"从头到尾渲染的是宽档，
/// 断言 `NavigationBar` 找不到时我以为是护栏生效，其实是档位压根没换过。
Future<void> _shellAt(WidgetTester tester, double width,
    {Widget child = const Text('目的地'),
    List<ShellDestination>? destinations}) async {
  await tester.binding.setSurfaceSize(Size(width, 900));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(ProviderScope(
    overrides: [
      engineRepositoryProvider.overrideWithValue(
          FakeEngineRepository(bookshelf: <BookshelfItem>[_row('一本')])),
    ],
    child: MaterialApp(
      home: AppShell(
        location: readingGroupRoute,
        destinations: destinations ?? kDestinations,
        child: child,
      ),
    ),
  ));
  await tester.pump();
}

/// 真走路由的那一臂：`AppShell` 由 `ShellRoute` 挂上来，档位由窗口宽度决定。
Widget _routed(FakeEngineRepository fake) => ProviderScope(
      overrides: [engineRepositoryProvider.overrideWithValue(fake)],
      child: MaterialApp.router(routerConfig: buildRouter()),
    );

/// 一条目的地：注入它才能证"窄档没有栏"是 Material 的 assert 在挡，
/// 而不是"这一臂压根没跑到"。真表在 G2 之后已经是三条了，拿不到这个形状。
const List<ShellDestination> _one = <ShellDestination>[
  (label: '书架', route: '/bookshelf', icon: Icons.book_outlined),
];

void main() {
  group('Breakpoint.of 的三拍', () {
    test('阈值 720 与 Python 的 NARROW_WIDTH_THRESHOLD 同值', () {
      expect(kNarrowWidthThreshold, 720);
      expect(Breakpoint.of(719.9), Breakpoint.narrow);
      // 正对照：阈值本身是"小于"才算窄，720 整必须落宽档。
      expect(Breakpoint.of(720), Breakpoint.wide);
      expect(Breakpoint.of(1440), Breakpoint.wide);
    });
  });

  test('G2 的目的地表是三条分组入口（不是四屏）', () {
    expect(kDestinations.length, 3);
    expect(kDestinations.map((ShellDestination d) => d.route),
        containsAll(<String>[
          sourcesGroupRoute,
          readingGroupRoute,
          settingsGroupRoute,
        ]));
    // 顶级只认**组**：组内的屏走页签，一条都不许出现在这里
    // （flet 的 NAV_META 里 `DEST_*` 是成员、`GROUP_*` 才是入口）。
    expect(kDestinations.map((ShellDestination d) => d.label),
        <String>['书源', '阅读', '配置']);
  });

  testWidgets('窄档＋一条目的地：不给 NavigationBar（Material 自己 assert >=2），目的地照在',
      (tester) async {
    await _shellAt(tester, 500, destinations: _one);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(NavigationRail), findsNothing);
    expect(find.text('目的地'), findsOneWidget);
  });

  testWidgets('窄档＋真表（三条）：栏真的会出现（上一条负断言的正对照）', (tester) async {
    await _shellAt(tester, 500);
    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('书源'), findsOneWidget);
    expect(find.text('目的地'), findsOneWidget);
  });

  testWidgets('宽档：左侧 NavigationRail，**child 也得在**（这一臂就是那个 bug 的护栏）',
      (tester) async {
    await _shellAt(tester, 1200);
    expect(find.byType(NavigationRail), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.text('目的地'), findsOneWidget,
        reason: '宽档漏挂 child = 窗口一宽整屏空白');
  });

  testWidgets('跨阈值改尺寸：换的是导航形状，目的地不消失', (tester) async {
    await _shellAt(tester, 1200);
    expect(find.byType(NavigationRail), findsOneWidget);
    expect(find.text('目的地'), findsOneWidget);

    await tester.binding.setSurfaceSize(const Size(500, 900));
    await tester.pump();
    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.byType(NavigationRail), findsNothing);
    expect(find.text('目的地'), findsOneWidget);

    await tester.binding.setSurfaceSize(const Size(1200, 900));
    await tester.pump();
    expect(find.byType(NavigationRail), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.text('目的地'), findsOneWidget);
  });

  testWidgets('起手落「找书」，切到「书架」那一签要当场看得见（走路由，不走手挂）',
      (tester) async {
    final fake = FakeEngineRepository(
        bookshelf: <BookshelfItem>[_row('我的功法无限提升')],
        sources: <SourceRecord>[_source('一条书源')]);
    for (final width in <double>[500, 1200]) {
      await tester.binding.setSurfaceSize(Size(width, 1200));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.pumpWidget(_routed(fake));
      await tester.pumpAndSettle();

      // 阅读组的落地页签是「找书」（NAV_META 里 DEST_FIND_BOOKS 在那一组的第一个）。
      // 「找书」在树上有两处（侧栏那一格 + 页内标题），只数总数等于什么都没断言：
      // 页签对了而落地页错了，总数照样是 2。所以按**页内**那一处认落地页。
      expect(
          find.descendant(
              of: find.byType(FindBooksScreen), matching: find.text('找书')),
          findsOneWidget,
          reason: '$width 那一档没落找书页');
      // 底部状态行读的是**载入之后**那一句，不是 idle：`pumpAndSettle` 之后
      // source.list 已经落了值，状态行被 `_sourceStatus` 覆盖（idle 那一句只在
      // 首帧在，用工程那侧的探针量到的是"0 条"还是"1 条"，这里喂一条）。
      expect(find.text('共 1 个书源'), findsOneWidget,
          reason: '$width 那一档的底部状态行');

      // 切签不退订、也不重新装配一遍已经建好的那一屏：书架那一行必须**点一下就出现**。
      await tester.tap(find.byKey(const ValueKey('tab-$readingGroupRoute-书架')));
      await tester.pumpAndSettle();
      expect(find.text('我的功法无限提升'), findsOneWidget, reason: '$width 那一档');
      expect(find.text('书架共 1 本'), findsOneWidget, reason: '$width 那一档');
    }
  });

  test('三条分组路由的字面量：侧栏反查表就按这三个字符串认门', () {
    // 路由表的内部形状读不出来（go_router 18 没导出 RouteConfiguration），
    // 所以"起手落哪一屏"在上面那一臂里用真渲染证，不靠反射。
    expect(readingGroupRoute, '/reading');
    expect(sourcesGroupRoute, '/sources');
    expect(settingsGroupRoute, '/settings');
    expect(buildRouter(), isA<GoRouter>());
  });
}
