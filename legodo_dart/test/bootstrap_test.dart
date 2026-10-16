/// 起手与收摊：`main()` 那条"先连引擎再给界面"的收摊端，在测试里真走一遍。
///
/// FFI 单进程后收摊只剩一件落地的事：关窗前把 `engine.shutdown` 发出去
/// （Go 侧 `Cleanup()` 停服务、关存储）。对照臂是"喊不动引擎也不许卡窗"——
/// 这个项目为"关不干净"栽过整轮（停掉的只是 shell，子进程还活着占端口，
/// 下一次起服静默失败），所以这一族的规矩照旧不许松动。
library;

import 'dart:ui' show AppExitResponse;

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/app/bootstrap.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';

/// 起手那一批调用（排序后比）：根上的主题偏好 + 落地页「找书」的两发。
///
/// 为什么排序再比：这三发在同一帧里各走自己的 await，谁先落字节没定数；
/// 而"不该有第四条"这条护栏读的是**集合与条数**，不是顺序。
const List<String> _startupActions = <String>[
  'search.scopes',
  'source.list',
  'theme.preferences',
];

void main() {
  testWidgets('关窗那一发：engine.shutdown 真发出去、窗口放行', (tester) async {
    final fake = FakeEngineRepository();
    await tester.pumpWidget(
        BootstrapResult(repository: fake).scope(const LegodoApp()));
    await tester.pumpAndSettle();

    // 起手那一批是"根上的主题偏好 + 落地页找书的两发"；退出前不该有第四条业务调用。
    final startup = List<String>.of(fake.callOrder)..sort();
    expect(startup, _startupActions,
        reason: '多出来的那一条就是收摊前多打的业务调用');
    expect(fake.shutdownCalls, 0, reason: '还没关窗就不该已经收过摊');

    final response = await tester.binding.handleRequestAppExit();
    await tester.pump();

    expect(response, AppExitResponse.exit, reason: '回 cancel 就是把关窗按回去');
    expect(fake.shutdownCalls, 1,
        reason: '没接到这一发就等于 Go 侧服务与存储没被收干净');
    expect(fake.lastShutdownReason, isNotEmpty,
        reason: 'reason 是服务端日志里唯一说"谁让它退的"那一段');
  });

  testWidgets('喊不动引擎（shutdown 抛）：退出发不动也不能把窗口卡住', (tester) async {
    final fake = FakeEngineRepository();
    await tester.pumpWidget(
        BootstrapResult(repository: fake).scope(const LegodoApp()));
    await tester.pumpAndSettle();

    // 收摊那一发才坏：起手是好的，这样红只可能红在"退出不卡窗"这一下。
    fake.failure =
        EngineFailure(EngineFailureKind.transport, 'connection reset');
    final response = await tester.binding.handleRequestAppExit();
    await tester.pump();

    expect(response, AppExitResponse.exit);
    expect(fake.shutdownCalls, 1, reason: '喊过就算数，成败不论');
  });
}
