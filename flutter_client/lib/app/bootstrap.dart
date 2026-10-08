/// 启动装配：**先连引擎，再决定给不给用户看界面**（§4.6 那条握手规矩的落点）。
///
/// 顺序是有原因的：握不上就是连不上，那一屏要写清"连的是哪个地址、哪一步没通"，
/// 而不是先立一个空壳窗口、转圈转到用户以为程序卡了。
///
/// 仓库和句柄都从这里注入（`engineRepositoryProvider` 的默认体是
/// `UnwiredEngineRepository`），于是 widget 测试只要换一个 override 就能跑同一屏。
library;

import 'dart:io';
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/di/providers.dart';
import '../core/rpc/engine_process.dart';
import '../core/rpc/engine_repository.dart';
import '../core/rpc/envelope.dart';
import '../core/rpc/fake_engine_repository.dart';
import '../core/rpc/rpc_client.dart';
import '../core/rpc/rpc_engine_repository.dart';
import '../features/settings/data/app_theme.dart';
import '../features/settings/data/theme_preferences.dart';
import '../features/settings/domain/appearance_notifier.dart';
import 'router.dart';

/// 已经拉起来的那个引擎子进程：跟着 App 活，退出时负责发 `engine.shutdown`。
class EngineHandle {
  EngineHandle(this.started, this.client, this.repository);

  final StartedEngine started;
  final RpcClient client;
  final RpcEngineRepository repository;

  bool _done = false;

  Future<void> dispose() async {
    if (_done) return;
    _done = true;
    try {
      await repository.shutdown(reason: 'flutter window closed');
    } on Object {
      // 已经连不上就别再喊：这时候进程多半自己没了（idle 兜底在 `--idle-seconds` 那边）。
    }
    client.dispose();
    started.process.kill(ProcessSignal.sigterm);
  }
}

class BootstrapResult {
  const BootstrapResult({required this.repository, this.handle});

  final EngineRepository repository;
  final EngineHandle? handle;

  /// 把仓库注进 [ProviderScope]。
  ///
  /// 这里不写 `List<Override>`：Riverpod 3.4.3 没导出 `Override` 这个类型名
  /// （`package:riverpod/riverpod.dart` 的 show 表里没有它），只能让字面量自己推。
  Widget scope(Widget child) => ProviderScope(
        overrides: [
          engineRepositoryProvider.overrideWithValue(repository),
          engineHandleProvider.overrideWithValue(handle),
        ],
        child: child,
      );
}

/// 拉起的那一发句柄。widget 测试里它是 `null`（没有子进程可收），
/// 所以 [LegodoApp] 收摊那一手必须能处理"没有句柄"。
final Provider<EngineHandle?> engineHandleProvider =
    Provider<EngineHandle?>((ref) => null);

/// `--dart-define=LEGODO_FAKE_ENGINE=1` 时用替身起界面（CI 里没引擎也要能跑 widget 测试）。
const bool _useFakeEngine = bool.fromEnvironment('LEGODO_FAKE_ENGINE');

Future<BootstrapResult> bootstrapEngine() async {
  if (_useFakeEngine) {
    return BootstrapResult(repository: FakeEngineRepository());
  }
  final started = await EngineProcess.start();
  final client = RpcClient(started.endpoint);
  // 句柄与界面共用这一个仓库实例：各 new 一个的话 `engine.shutdown` 发两遍，
  // 第二遍打在已经退场的进程上，表现是退出时多一条没来由的 transport 错。
  final repository = RpcEngineRepository(client);
  return BootstrapResult(
    repository: repository,
    handle: EngineHandle(started, client, repository),
  );
}

/// 连不上那一屏用的根：仍然要有 `ProviderScope`（widget 测试与错误屏 itself 都读 provider）。
Widget bootFailureScope(EngineFailure failure) => ProviderScope(
      child: MaterialApp(
        title: 'Legado 书源管理工具',
        debugShowCheckedModeBanner: false,
        // 连不上引擎时**不读偏好**（读了也只会再抛一遍），走默认那一套：
        // 这一屏要的是"连的是哪儿、哪一步没通"，配色不在它的活里。
        theme: lightThemeFor(skinById(kDefaultSkinId)),
        darkTheme: darkThemeFor(skinById(kDefaultSkinId)),
        home: EngineUnavailablePage(failure: failure),
      ),
    );

/// 握手失败/版本不符那一屏。文案里必须带**连的是哪儿**和**哪一步没通**——
/// "应用与核心版本不一致"这类话不带地址就是让人去猜。
class EngineUnavailablePage extends StatelessWidget {
  const EngineUnavailablePage({required this.failure, super.key});

  final EngineFailure failure;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              Icon(Icons.cloud_off,
                  size: 40, color: Theme.of(context).colorScheme.error),
              const SizedBox(height: 12),
              Text('连不上引擎（${failure.kind.wireName}）',
                  style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              Text(failure.message, textAlign: TextAlign.center),
            ],
          ),
        ),
      ),
    );
  }
}

class LegodoApp extends ConsumerStatefulWidget {
  const LegodoApp({super.key});

  @override
  ConsumerState<LegodoApp> createState() => _LegodoAppState();
}

class _LegodoAppState extends ConsumerState<LegodoApp> {
  /// 句柄从 [engineHandleProvider] 读、不从构造参数走：`boot.scope()` 已经是
  /// 唯一的注入口，再开第二条线就等于同一件事在两个地方各写一遍。
  late final AppLifecycleListener _lifecycle;

  /// 路由表**只建一次**：主题现在会让这一发重建（下面 watch 了外观偏好），
  /// 而每次 build 新 GoRouter 会把导航状态冲掉——用户换个皮肤就跳回首页。
  late final GoRouter _router = buildRouter();

  @override
  void initState() {
    super.initState();
    // 必须在这里就构造：`late final` 的初始化体是**第一次读**时才跑的，
    // 而这条唯一的读发生在 [dispose]——于是"退出收摊"那一发永远不会被注册，
    // 关窗直接放行，python 子进程留在后台占端口（bootstrap_test 第一条就是它）。
    _lifecycle = AppLifecycleListener(
      // 关窗要主动收引擎：`--idle-seconds` 那条兜底等的是 15s，而用户期待的是"关了就走"。
      // 回 `exit` 才是放行这一次退出；回 `cancel` 会把关窗按回去。
      onExitRequested: () async {
        await ref.read(engineHandleProvider)?.dispose();
        return AppExitResponse.exit;
      },
    );
  }

  @override
  void dispose() {
    _lifecycle.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // 主题的**唯一**持有者在这里，不在外观那一屏里：flet 那边是
    // `apply_page_theme(page, data)` 打到整页（三件套一起下发，只设一面在深色桌面上
    // 等于什么都没改）。待存的那一份赢过已存的（预览即所见），所以 watch 的是
    // pending 而不是 saved —— 见 `features/settings/domain/appearance_notifier.dart`。
    final pref = ref.watch(appearanceProvider).value?.pending ??
        ThemePreferences.defaults;
    final theme = appThemeFor(pref);
    return MaterialApp.router(
      title: 'Legado 书源管理工具',
      debugShowCheckedModeBanner: false,
      theme: theme.theme,
      darkTheme: theme.darkTheme,
      themeMode: theme.themeMode,
      routerConfig: _router,
    );
  }
}
