/// 路由表（§6.2：go_router + `ShellRoute`；导航脊柱照 `NAV_META` 的"三个分组入口 +
/// 组内页签"平移，见记忆 `navigation-rail-old-architecture-shape`）。
///
/// 只有**分组**是目的地（三条路由）；组内的页签是分组页自己的状态，不进路由表——
/// 这一条跟 flet 是一致的：侧栏只认分组，`_dest_group` 反查也只到分组那一层。
/// 「进阅读先落书架」那条落地规矩还没接（见下面 `initialTabOf` 那段）：它要的是
/// 组内页签的落点，不是路由表——真正的缺口在"谁来持有那两档"，在删 flet 之前补。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/widgets/tabbed_group.dart';
import '../features/bookshelf/presentation/bookshelf_screen.dart';
import '../features/check/presentation/check_screen.dart';
import '../features/find_books/presentation/find_books_screen.dart';
import '../features/reader/domain/reader_controller.dart';
import '../features/reader/presentation/reader_screen.dart';
import '../features/rss/presentation/rss_screen.dart';
import '../features/settings/presentation/appearance_screen.dart';
import '../features/sources/presentation/sources_screen.dart';
import 'shell.dart';

/// 三条分组路由（顺序与 `NAV_META` 同：书源 / 阅读 / 配置）。
const String sourcesGroupRoute = '/sources';
const String readingGroupRoute = '/reading';
const String settingsGroupRoute = '/settings';

/// 阅读器：`NAV_META` 里 `hidden=True` 的那一位**整页目的地**（`DEST_READER`）。
/// 它不是分组、也不在页签表里——开读时独占内容区，所以它是 ShellRoute 的兄弟，
/// 用根导航器压在壳之上（`context.push`，返回就落回原来那一屏）。
const String readerRoute = '/reader';

/// 开一本书：把 [ReaderArgs] 作为 `extra` 递进路由。
///
/// 为什么不走 `go_named` 的 query 参数：`reader.open` 吃的是一份**字典快照**
/// （书名/作者/封面/origin 十几格），把它压进 url 字符串等于在边界上再造一次编解码，
/// 而中文书名还要先过 URL 转义——那正是 `legado-http-session-pooling` 那一族
/// "编码口径不一致"的错法。
Future<void> openReaderBook(BuildContext context, ReaderArgs args) =>
    context.push(readerRoute, extra: args);

/// 「阅读」组的页签表（`NAV_META` 里 GROUP_READING 的非整页成员）。
///
/// 顺序照 flet：找书 → 书架 → 订阅（`DEST_FIND_BOOKS` / `DEST_BOOKSHELF` / `DEST_RSS`
/// 在 `NAV_META` 里的声明顺序就是导航顺序，`registry.py:165-167`）。
/// 阅读器是**整页目的地**（`hidden=True`：不在页签里，开读时独占内容区），
/// 走 `readerRoute` 那条与壳同级的路由；漫画阅读器还在 G4。
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
      GroupTab(
        label: '订阅',
        icon: Icons.rss_feed,
        builder: _rss,
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

Widget _rss(BuildContext _) => const RssScreen();

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

/// 分组的落地页签：这里只实现 flet `_anchor` 三级里的**第三档**（组内第一个）。
///
/// 前两档（记住的页签 > 书架探测）在 Dart 侧还**没有人持有**：`TabbedGroup` 的当前页签
/// 不跨分组进出留存，书架探测要的是"架上非空"这一个布尔（flet 由装配根注入
/// `shelf_probe` 可调用，视图层不 import 服务那条红线不变）。这一条是 G4-E0 的活，
/// **必须在删 flet 之前接上**，否则参照实现一没，这条行为就永久丢了。
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
        // 阅读器：与壳同级的整页目的地（上面 openReaderBook 那条推它）。
        // `ProviderScope` 套在这里而不是套在 App 根：一本书一套 args，
        // 退出时这一发随路由一起 dispose，下一次开别的书不会带着上一本的快照。
        GoRoute(
          path: readerRoute,
          name: 'reader',
          pageBuilder: (context, state) => MaterialPage<void>(
            name: 'reader',
            child: ProviderScope(
              // 不写 `List<Override>`：Riverpod 3.4.3 没导出 `Override` 这个类型名
              // （同 `app/bootstrap.dart` 那处），字面量自己推。
              overrides: [
                readerArgsProvider.overrideWithValue(state.extra is ReaderArgs
                    ? state.extra as ReaderArgs
                    : const ReaderArgs.empty()),
                // **这一行不是多余的**：Riverpod 把没被覆盖的 provider 元素放在
                // **根容器**里，而那个元素的 `ref.read(readerArgsProvider)` 读的也是根容器
                // ——只覆盖 args 的话，控制器读到的永远是默认那份空快照
                // （屏幕上写"阅读器没有收到书"，而 `extra` 明明传进来了）。
                // 连着 `readerProvider` 一起覆盖，这一本书的状态机才落在**这个**容器里，
                // 上面注释那句"随路由一起 dispose"也才真的成立。
                readerProvider.overrideWith(ReaderNotifier.new),
              ],
              child: const ReaderScreen(),
            ),
          ),
        ),
      ],
    );

Widget _group(String route) => TabbedGroup(
      key: ValueKey('group-$route'),
      groupKey: route,
      tabs: tabsOf(route),
      initialLabel: initialTabOf(route),
    );
