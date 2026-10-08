/// 分组内页签容器（NAV_META 那个"一个分组 = 一个入口，组内目的地合成页顶 Tab"的形状）。
///
/// 两条行为是从 flet 那边量出来搬的（`app/views/navigation.py::SectionView`）：
///
/// 1. **惰性装配**：第一次切到某一签才建那一屏——`书源` 分组的「管理」一屏要打一发
///    6.9 MB 的 `source.list`，进组就顺手建等于每次进组都白拉一遍全库。
/// 2. **装配后不销毁**（切走的页签只是 `Offstage`）：flet 那边切签**不退订控制器**，
///    在途的搜索/检测照常往里灌，切回来就是最新结果。这一条在这里是**语义要求**，
///    不是优化：本项目的事件订阅挂在屏上（`core/events/event_listener.dart` 里那份理由），
///    页签一销毁订阅就跟着散，"检测跑完时我正看着别的页签"会变成"切回来什么都没有"，
///    而 flet 那边切回来是满的。
///
/// 页签条自己画（不用 `TabBar`）：`TabBar` 要 `TabController` + `SingleTickerProviderStateMixin`，
/// 而这里的"当前页签"是**路由派生量**（切签就是换目的地），塞一个动画控制器进去就是
/// 给同一个事实造第二个持有者——它还会和 `Offstage` 的保活互相打架。
library;

import 'package:flutter/material.dart';

/// 分组里的一页签。
class GroupTab {
  const GroupTab({
    required this.label,
    required this.icon,
    required this.builder,
  });

  final String label;
  final IconData icon;

  /// 建那一屏。只在第一次被选到时调用一次（上面第 1 条）。
  final WidgetBuilder builder;
}

/// 组内页签的切换把手：让某一签里的按钮能说"切到同组的另一签"，
/// 而不必知道组外的路由形状。flet 那边是控制器只发 `on_open_check` 这个动作，
/// 目的地由导航外壳给（`book_source_list_controller.py` 的 `nav_actions`）。
abstract class GroupTabs {
  /// 按页签标签切过去；没有这一签时不动（不抛：一处文案改动不该打穿一屏）。
  void select(String label);

  String? get currentLabel;
}

class GroupTabsScope extends InheritedWidget {
  const GroupTabsScope({
    required this.controller,
    required super.child,
    super.key,
  });

  final GroupTabs controller;

  static GroupTabs? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<GroupTabsScope>()?.controller;

  @override
  bool updateShouldNotify(GroupTabsScope old) => false;
}

class TabbedGroup extends StatefulWidget {
  const TabbedGroup({
    required this.groupKey,
    required this.tabs,
    this.initialLabel,
    super.key,
  });

  /// 分组自己的路由名：页签换到哪一签都不许换掉这个键
  /// （键一变整组重建 = 上面第 2 条白写，在途的请求一起丢）。
  final String groupKey;

  final List<GroupTab> tabs;

  /// 落地页签的标签：目的地路由给不出组内位置，所以由装配处点名
  /// （flet 的 `_anchor`：阅读组落「找书」，书源组落「管理」）。
  final String? initialLabel;

  @override
  State<TabbedGroup> createState() => _TabbedGroupState();
}

class _TabbedGroupState extends State<TabbedGroup> implements GroupTabs {
  late int _index = _resolveInitial();
  final Set<int> _mounted = <int>{};

  int _resolveInitial() {
    final label = widget.initialLabel;
    final wanted = label == null ? -1 : widget.tabs.indexWhere((t) => t.label == label);
    return wanted < 0 ? 0 : wanted;
  }

  @override
  void initState() {
    super.initState();
    _mounted.add(_index);
  }

  void _select(int next) {
    if (next < 0 || next >= widget.tabs.length) return;
    if (next == _index) return;
    setState(() {
      _index = next;
      _mounted.add(next);
    });
  }

  @override
  void select(String label) =>
      _select(widget.tabs.indexWhere((tab) => tab.label == label));

  @override
  String? get currentLabel => widget.tabs[_index].label;

  @override
  void didUpdateWidget(TabbedGroup old) {
    super.didUpdateWidget(old);
    // 外部点名换了落地页签（同组换目的地走的是 initialLabel）：跟着切，别停在原页签上。
    final label = widget.initialLabel;
    if (label != null) {
      final wanted = widget.tabs.indexWhere((t) => t.label == label);
      if (wanted >= 0 && wanted != _index) _select(wanted);
    }
  }

  @override
  Widget build(BuildContext context) {
    // 页签顺序就是装配顺序：Offstage 的那些留在树里（不布局、不绘制，但状态与订阅
    // 都在），所以 Stack 里每一格的先后不许随当前页签变。
    final mounted = _mounted.toList()..sort();
    return GroupTabsScope(
      controller: this,
      child: Column(
        children: <Widget>[
          if (widget.tabs.length > 1) _buildStrip(context),
          Expanded(
            child: Stack(
              children: <Widget>[
                for (final i in mounted)
                  Offstage(
                    offstage: i != _index,
                    child: widget.tabs[i].builder(context),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildStrip(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.surface,
      child: Column(
        children: <Widget>[
          SizedBox(
            height: 46,
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Row(
                children: <Widget>[
                  for (var i = 0; i < widget.tabs.length; i++)
                    _buildTab(i, i == _index ? scheme.primary : scheme.onSurfaceVariant),
                ],
              ),
            ),
          ),
          // 页签条与内容之间那一条线：flet 那边是 `ft.Divider(height=1)`。
          const Divider(height: 1),
        ],
      ),
    );
  }

  Widget _buildTab(int i, Color color) {
    final tab = widget.tabs[i];
    return InkWell(
      key: ValueKey('tab-${widget.groupKey}-${tab.label}'),
      onTap: () => _select(i),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(tab.icon, size: 18, color: color),
            const SizedBox(width: 6),
            Text(tab.label, style: TextStyle(color: color)),
          ],
        ),
      ),
    );
  }
}
