/// 路由表（§6.2：go_router + `ShellRoute`；导航脊柱照 `NAV_META` 的"三个分组入口 +
/// 组内页签"平移，见记忆 `navigation-rail-old-architecture-shape`）。
///
/// 只有**分组**是目的地（三条路由）；组内的页签是分组页自己的状态，不进路由表——
/// 这一条跟 flet 是一致的：侧栏只认分组，`_dest_group` 反查也只到分组那一层。
/// 「进阅读先落书架」那条落地规矩在 G3（它要的是开读动作，不是页签顺序）。
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../core/widgets/tabbed_group.dart';
import '../features/bookshelf/presentation/bookshelf_screen.dart';
import '../features/check/presentation/check_screen.dart';
import '../features/find_books/presentation/find_books_screen.dart';
import '../features/settings/presentation/appearance_screen.dart';
import '../features/sources/presentation/sources_screen.dart';
import 'shell.dart';

/// 三条分组路由（顺序与 `NAV_META` 同：书源 / 阅读 / 配置）。
const String sourcesGroupRoute = '/sources';
const String readingGroupRoute = '/reading';
const String settingsGroupRoute = '/settings';

/// 「阅读」组的页签表（`NAV_META` 里 GROUP_READING 的非整页成员）。
///
/// 顺序照 flet：找书在书架之前（`DEST_FIND_BOOKS` 是那一组的第一个）。
/// 阅读器/漫画是**整页目的地**（`hidden=True`：不在页签里，开读时独占内容区），
/// 那一条是 G3 的活，这里不预告。
List<GroupTab> readingTabs() => const <GroupTab>[
      GroupTab(
        label: '找书',
        icon: Icons.travel_explore,
        builder: _findBooks,
      ),
      GroupTab(
        label: '书架',
        icon: Icons.auto_stories,
        builder: _bookshelf,
      ),
    ];

/// 「书源」组：G2 只接「管理」与「检测」两签。
///
/// 导入 / 规则编辑器 / 自愈那三签要的是文件选择器与正文编辑器（G4），
/// 这里**不放占位页签**——占位会被当成"已经接上"，而它连 RPC 都没发过。
List<GroupTab> sourceTabs() => const <GroupTab>[
      GroupTab(
        label: '管理',
        icon: Icons.library_books,
        builder: _sources,
      ),
      GroupTab(
        label: '检测',
        icon: Icons.fact_check_outlined,
        builder: _check,
      ),
    ];

/// 「配置」组：G2 只有外观一签（快捷键/仪表盘在 G4）。
List<GroupTab> settingsTabs() => const <GroupTab>[
      GroupTab(
        label: '外观',
        icon: Icons.palette_outlined,
        builder: _appearance,
      ),
    ];

Widget _findBooks(BuildContext _) => const FindBooksScreen();

Widget _bookshelf(BuildContext _) => const BookshelfScreen();

Widget _sources(BuildContext _) => const SourcesScreen();

Widget _check(BuildContext _) => const CheckScreen();

Widget _appearance(BuildContext _) => const AppearanceScreen();

/// 目的地 → 所属分组（与 flet `NavigationHost._dest_group` 同一张反查表）。
String? groupOfDestination(String destination) => switch (destination) {
      readingGroupRoute => readingGroupRoute,
      sourcesGroupRoute => sourcesGroupRoute,
      settingsGroupRoute => settingsGroupRoute,
      _ => null,
    };

/// 分组的落地页签（flet `_anchor` 里"组内第一个"那一档；
/// 「架上有书先落书架」那条规则属于 G3，不在这里发明）。
String? initialTabOf(String groupRoute) => switch (groupRoute) {
      readingGroupRoute => '找书',
      sourcesGroupRoute => '管理',
      settingsGroupRoute => '外观',
      _ => null,
    };

/// 一条分组路由 → 它的页签表。
List<GroupTab> tabsOf(String groupRoute) => switch (groupRoute) {
      readingGroupRoute => readingTabs(),
      sourcesGroupRoute => sourceTabs(),
      _ => settingsTabs(),
    };

/// `ValueKey( 分组路由 )` 是"切签不退订"的另一半（第七十八轮那条）：
/// **组内**换页签不换这个键（键一变整组重建 = 在途请求与订阅一起丢），
/// 换分组才换。目的地是 shell 的 child，壳自己跟着分组走。
GoRouter buildRouter() => GoRouter(
      initialLocation: readingGroupRoute,
      routes: <RouteBase>[
        ShellRoute(
          builder: (context, state, child) => AppShell(
            key: ValueKey('shell-${state.uri.path}'),
            location: state.uri.path,
            child: child,
          ),
          routes: <RouteBase>[
            GoRoute(
              path: readingGroupRoute,
              name: 'reading',
              pageBuilder: (context, state) => NoTransitionPage<void>(
                child: _group(readingGroupRoute),
              ),
            ),
            GoRoute(
              path: sourcesGroupRoute,
              name: 'sources',
              pageBuilder: (context, state) => NoTransitionPage<void>(
                child: _group(sourcesGroupRoute),
              ),
            ),
            GoRoute(
              path: settingsGroupRoute,
              name: 'settings',
              pageBuilder: (context, state) => NoTransitionPage<void>(
                child: _group(settingsGroupRoute),
              ),
            ),
          ],
        ),
      ],
    );

Widget _group(String route) => TabbedGroup(
      key: ValueKey('group-$route'),
      groupKey: route,
      tabs: tabsOf(route),
      initialLabel: initialTabOf(route),
    );
