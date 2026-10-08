/// 路由表（§6.2：go_router + `ShellRoute`；进阅读先落书架那条规矩在 G3）。
library;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../features/bookshelf/presentation/bookshelf_screen.dart';
import 'shell.dart';

const String bookshelfRoute = '/bookshelf';

/// `ValueKey( uri )` 是第七十八轮那条"切签不退订"的另一半：
/// shell 的 child 换了目的地，壳自己不许被重建（重建 = 导航状态与在飞订阅一起丢）。
GoRouter buildRouter() => GoRouter(
      initialLocation: bookshelfRoute,
      routes: <RouteBase>[
        ShellRoute(
          builder: (context, state, child) => AppShell(
            key: ValueKey('shell-${state.uri.path}'),
            location: state.uri.path,
            child: child,
          ),
          routes: <RouteBase>[
            GoRoute(
              path: bookshelfRoute,
              name: 'bookshelf',
              pageBuilder: (context, state) => const NoTransitionPage<void>(
                child: BookshelfScreen(),
              ),
            ),
          ],
        ),
      ],
    );
