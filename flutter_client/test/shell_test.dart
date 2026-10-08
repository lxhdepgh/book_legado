/// 壳与档位（§6.2 那条"目的地是 shell 的 child"、以及第七十八轮那条 720 阈值）。
///
/// 这一支的存在理由很具体：G1 第一版的宽档分支只回了 `NavigationRail` 没带 `child`，
/// 于是"窗口一宽整屏空白"——而当时跑着的 `bookshelf_screen_test` 是直接把屏幕挂到
/// `MaterialApp` 上的，永远绕过了壳，一点声音都没有。所以这里专门从**壳**进，
/// 并且两档都真渲染一次（读代码不算验证：那一版代码看上去完全正确）。
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

BookshelfItem _row(String name) => BookshelfItem(
      name: name,
      author: '作者',
      chapterName: '',
      coverUrl: '',
      url: 'https://example.com/$name',
      sourceUrl: 'https://example.com',
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
        location: bookshelfRoute,
        destinations: destinations ?? kDestinations,
        child: child,
      ),
    ),
  ));
  await tester.pump();
}

/// 真走路由的那一臂：`AppShell` 由 `ShellRoute` 挂上来，档位由窗口宽度决定。
Widget _routed() => MaterialApp.router(routerConfig: buildRouter());

/// 两条目的地：G2 的形态。用它证"窄档那条栏是**能**出现的"，
/// 否则 `length < 2 ? null :` 那一支在只有一条入口时永远走 null，
/// "窄档没有栏"就分不清是护栏还是坏了。
const List<ShellDestination> _two = <ShellDestination>[
  (label: '书架', route: '/bookshelf', icon: Icons.book_outlined),
  (label: '书源', route: '/sources', icon: Icons.list_alt),
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

  test('G1 的目的地表就一条：下面那条"窄档没栏"的全部前提', () {
    expect(kDestinations.length, 1);
    expect(kDestinations.map((ShellDestination d) => d.route),
        containsAll(<String>[bookshelfRoute]));
  });

  testWidgets('窄档＋一条目的地：不给 NavigationBar（Material 自己 assert >=2），目的地照在',
      (tester) async {
    await _shellAt(tester, 500);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(NavigationRail), findsNothing);
    expect(find.text('目的地'), findsOneWidget);
  });

  testWidgets('窄档＋两条目的地：栏真的会出现（上一条负断言的正对照）', (tester) async {
    await _shellAt(tester, 500, destinations: _two);
    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('书架'), findsOneWidget);
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
    await _shellAt(tester, 1200, destinations: _two);
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

  testWidgets('整条壳在两种窗口宽度下都把书架渲染出来（走路由，不走手挂）', (tester) async {
    final fake = FakeEngineRepository(
        bookshelf: <BookshelfItem>[_row('我的功法无限提升')]);
    for (final width in <double>[500, 1200]) {
      await tester.binding.setSurfaceSize(Size(width, 1200));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.pumpWidget(ProviderScope(
        overrides: [engineRepositoryProvider.overrideWithValue(fake)],
        child: _routed(),
      ));
      await tester.pumpAndSettle();
      expect(find.text('我的功法无限提升'), findsOneWidget, reason: '$width 那一档');
      expect(find.text('书架共 1 本'), findsOneWidget, reason: '$width 那一档');
    }
  });

  test('G1 的目的地常量：书架是第一条，且路由起手就在它上面', () {
    // 路由表的内部形状读不出来（go_router 18 没导出 RouteConfiguration），
    // 所以"起手落哪一屏"在上面那一臂里用真渲染证，不靠反射。
    expect(bookshelfRoute, '/bookshelf');
    expect(buildRouter(), isA<GoRouter>());
  });
}
