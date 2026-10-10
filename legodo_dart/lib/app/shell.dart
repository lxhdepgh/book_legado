/// 导航壳（§6.2：`ShellRoute` 承载导航脊柱；目的地不是 View 而是 shell 的一个 child）。
///
/// G1 只有「书架」一个目的地——它先把**脊柱的形状**立起来：档位（宽/窄）由
/// [Breakpoint] 判、切换目的地不退订、目的地是 shell 的 child。
/// 宽度阈值 **720** 与 `app/views/navigation.py` 的 `NARROW_WIDTH_THRESHOLD` 同值：
/// 那条闸门是第七十八轮实测换来的（同档不重建），迁移期两边的"什么时候换形状"
/// 必须是同一个数，否则同一份书单在两边不是同一个形状，G2 的对照就白量。
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'router.dart';

/// 与 Python 侧 `NARROW_WIDTH_THRESHOLD` 同源（守卫测试见
/// `tests/test_gateway_contract_single_source.py`）。
const double kNarrowWidthThreshold = 720;

enum Breakpoint {
  narrow,
  wide;

  static Breakpoint of(double width) =>
      width < kNarrowWidthThreshold ? Breakpoint.narrow : Breakpoint.wide;
}

/// 一条目的地：文案、路由、图标。
typedef ShellDestination = ({String label, String route, IconData icon});

/// 顶级入口 = **分组**，不是屏（`NAV_META` 的三组，顺序照旧单体的
/// 书源 / 阅读 / 配置，图标照 `navigation.py::_GROUP_ICONS`）。
/// 组内的屏走页签（`core/widgets/tabbed_group.dart`），不在这里各占一行。
const List<ShellDestination> kDestinations = <ShellDestination>[
  (label: '书源', route: sourcesGroupRoute, icon: Icons.library_books_outlined),
  (label: '阅读', route: readingGroupRoute, icon: Icons.menu_book_outlined),
  (label: '配置', route: settingsGroupRoute, icon: Icons.settings_outlined),
];

class AppShell extends StatelessWidget {
  const AppShell({
    required this.child,
    required this.location,
    this.destinations = kDestinations,
    super.key,
  });

  final Widget child;
  final String location;

  /// 递得进来是为了让"只有一条目的地时窄档没有切换栏"这一臂有正对照：
  /// 那条 `? null :` 分支在没有第二条入口的 G1 里永远只走到 null 那一侧，
  /// 不注入就照不出"G2 补上入口之后条真的会出现在窄档"。
  final List<ShellDestination> destinations;

  @override
  Widget build(BuildContext context) {
    final index = destinations.indexWhere((d) => d.route == location);
    final selected = index < 0 ? 0 : index;
    void select(int next) => context.go(destinations[next].route);

    return LayoutBuilder(
      builder: (context, constraints) {
        final band = Breakpoint.of(constraints.maxWidth);
        return switch (band) {
          // 宽档：rail 在左、页面在右。这一臂原先只回了 rail 没带 child——
          // 于是"窗口宽一点就整屏空白"，而窄档是对的。child 必须挂在同一发返回里。
          Breakpoint.wide => Scaffold(
              body: Row(
                children: <Widget>[
                  NavigationRail(
                    labelType: NavigationRailLabelType.all,
                    selectedIndex: selected,
                    destinations: destinations
                        .map((d) => NavigationRailDestination(
                              icon: Icon(d.icon),
                              label: Text(d.label),
                            ))
                        .toList(),
                    onDestinationSelected: select,
                  ),
                  const VerticalDivider(width: 1),
                  Expanded(child: child),
                ],
              ),
            ),
          Breakpoint.narrow => Scaffold(
              body: child,
              // `NavigationBar` 自己 assert `destinations.length >= 2`（material
              // navigation_bar.dart:122）：只有一个目的地时递给它一条"切换栏"就是红屏。
              // G1 恰好只有书架这一条，所以窄档这里是真的没有条可切；
              // §9 的 G2 把第二、第三条入口接上之后，这一发自动出现。
              bottomNavigationBar: destinations.length < 2
                  ? null
                  : NavigationBar(
                      selectedIndex: selected,
                      onDestinationSelected: select,
                      destinations: destinations
                          .map((d) => NavigationDestination(
                              icon: Icon(d.icon), label: d.label))
                          .toList(),
                    ),
            ),
        };
      },
    );
  }
}
