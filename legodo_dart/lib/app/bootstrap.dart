/// 启动装配：**先连引擎，再决定给不给用户看界面**（§4.6 那条握手规矩的落点）。
///
/// 顺序是有原因的：握不上就是连不上，那一屏要写清"连的是哪个库、哪一步没通"，
/// 而不是先立一个空壳窗口、转圈转到用户以为程序卡了。
///
/// FFI 单进程：Go 引擎以 `legodo_engine.dll` 装进本进程（无子进程、无端口）。
/// 仓库从这里注入（`engineRepositoryProvider` 的默认体是
/// `UnwiredEngineRepository`），于是 widget 测试只要换一个 override 就能跑同一屏。
library;

import 'dart:io' as io;
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../core/di/providers.dart';
import '../core/ffi/engine_ffi.dart';
import '../core/rpc/engine_repository.dart';
import '../core/rpc/envelope.dart';
import '../core/rpc/ffi_engine_adapter.dart';
import '../core/rpc/fake_engine_repository.dart';
import '../features/settings/data/app_theme.dart';
import '../features/settings/data/theme_preferences.dart';
import '../features/settings/domain/appearance_notifier.dart';
import 'router.dart';

class BootstrapResult {
  const BootstrapResult({required this.repository});

  final EngineRepository repository;

  /// 把仓库注进 [ProviderScope]。
  ///
  /// 这里不写 `List<Override>`：Riverpod 3.4.3 没导出 `Override` 这个类型名
  /// （`package:riverpod/riverpod.dart` 的 show 表里没有它），只能让字面量自己推。
  Widget scope(Widget child) => ProviderScope(
        overrides: [
          engineRepositoryProvider.overrideWithValue(repository),
        ],
        child: child,
      );
}

/// `--dart-define=LEGODO_FAKE_ENGINE=1` 时用替身起界面（CI 里没引擎也要能跑 widget 测试）。
const bool _useFakeEngine = bool.fromEnvironment('LEGODO_FAKE_ENGINE');

Future<BootstrapResult> bootstrapEngine() async {
  if (_useFakeEngine) {
    return BootstrapResult(repository: FakeEngineRepository());
  }

  // Go FFI 动态库（legodo_engine.dll）— 单进程架构，零网络延迟
  Object? ffiError;
  try {
    final ffiAdapter = FFIEngineAdapter();
    await ffiAdapter.connect();
    if (ffiAdapter.isConnected) {
      // 书架探针（冷启动落哪一屏用）：连上后顺手问一次，问不出来按"没书"走。
      bool shelfHasBooks = false;
      try {
        shelfHasBooks = (await ffiAdapter.listBookshelf()).isNotEmpty;
      } catch (_) {}
      setShelfProbe(() => shelfHasBooks);

      return BootstrapResult(repository: ffiAdapter);
    }
    ffiError = 'FFI 连接成功但 isConnected 为 false';
  } on Object catch (e) {
    ffiError = e;
    // 诊断：输出 FFI 失败原因到 stderr（发布版保留，方便排查）
    _logEngineFailure('FFI 路径', e);
  }

  // 加载失败 — 拼接 FFI 诊断信息，让用户知道具体哪一步断了
  final ffiDetail = '\nFFI 加载失败：$ffiError';
  // 添加 DLL 路径诊断
  final dllPath = GoFFIClient.instance.libraryPath;
  final dllExists = io.File(dllPath).existsSync();
  final exePath = io.File(io.Platform.resolvedExecutable).path;
  final detail = '$ffiDetail\nDLL 路径：$dllPath\nDLL 存在：$dllExists\n'
      'EXE 路径：$exePath\n工作目录：${io.Directory.current.path}';
  throw EngineFailure(
    EngineFailureKind.unavailable,
    '当前环境不支持拉起引擎：Go 引擎动态库（legodo_engine.dll）加载失败。$detail\n'
    '使用 build_release.py 构建完整版本。',
  );
}

/// 将引擎连接失败写入 stderr，发布版也保留（方便远程排查）。
void _logEngineFailure(String path, Object error) {
  try {
    io.stderr.writeln('[LEGODO_ENGINE] $path 失败：$error');
  } catch (_) {
    // stderr 不可写时静默忽略
  }
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
  /// 路由表**只建一次**：主题现在会让这一发重建（下面 watch 了外观偏好），
  /// 而每次 build 新 GoRouter 会把导航状态冲掉——用户换个皮肤就跳回首页。
  late final GoRouter _router = buildRouter();

  /// 关窗收摊的监听器。**字段不是在 dispose 里才 late 初始化的**：`late final`
  /// 的初始化体是第一次读时才跑，而构造必须发生在 [initState]——否则"退出收摊"
  /// 那一发永远不会被注册，关窗直接放行（bootstrap_test 第一条就是它）。
  late final AppLifecycleListener _lifecycle;

  @override
  void initState() {
    super.initState();
    _lifecycle = AppLifecycleListener(
      // 关窗要主动收引擎：FFI 单进程下这是给 Go 侧发 engine_shutdown（
      // 停服务、关存储）的最后窗口；机器上的 idle 兜底不为它作保。
      // 回 `exit` 才是放行这一次退出；回 `cancel` 会把关窗按回去。
      onExitRequested: () async {
        try {
          await ref
              .read(engineRepositoryProvider)
              .shutdown(reason: 'flutter window closed');
        } on Object {
          // 已经连不上就别再喊：这一手只是尽力而为，不该把关窗卡住。
        }
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
