/// 起手与收摊：`main()` 那条"先连引擎再给界面"的两端，在测试里各真走一遍。
///
/// 为什么值得单开一支：关窗那一发如果没接到 `engine.shutdown`/SIGTERM，
/// 留下的 python 子进程会**继续占着端口**——这个项目为此栽过一整轮
/// （停掉的只是 shell，子进程还活着，下一次起服静默失败）。
/// 而这条线在 widget 测试里天然是断的：那些臂没有子进程，句柄是 null。
/// 所以这里用一个假 `Process` + `MockClient` 把真句柄搭起来，
/// 让"退出"这件事以字节和信号的形式落在地上。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' show AppExitResponse;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:legodo_client/app/bootstrap.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/rpc/engine_endpoint.dart';
import 'package:legodo_client/core/rpc/engine_process.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';

const EngineEndpoint _endpoint = EngineEndpoint(
  host: '127.0.0.1',
  port: 59999,
  token: 'tok',
  protocolVersion: 1,
  build: 'test',
  pid: 1,
);

/// 只记 kill 的假进程：`Process` 是 dart:io 的抽象类，`implements` 就够。
class _FakeProcess implements Process {
  final List<ProcessSignal> kills = <ProcessSignal>[];

  @override
  bool kill([ProcessSignal signal = ProcessSignal.sigterm]) {
    kills.add(signal);
    return true;
  }

  @override
  int get pid => 4242;

  @override
  Future<int> get exitCode => Future.value(0);

  @override
  IOSink get stdin => throw UnimplementedError('退出路径不碰 stdin');

  @override
  Stream<List<int>> get stdout => const Stream<void>.empty().cast<List<int>>();

  @override
  Stream<List<int>> get stderr => const Stream<void>.empty().cast<List<int>>();
}

/// 按 action 回**形状合法**的空载荷：数组类回 `[]`，`theme.preferences` 回 `{}`
/// （= 全默认偏好），其余回 null。
///
/// 空数组是真形状，`data: null` 不是——仓库层会把它当坏形状报 transport，
/// 那条错误路径不该混进收摊那两条臂里（书架那臂同理）。
http.Response _okFor(String action) => http.Response(
    jsonEncode(<String, Object?>{
      'v': 1,
      'id': 'd-1',
      'ok': true,
      'data': switch (action) {
        'theme.preferences' => const <String, Object?>{},
        'bookshelf.list' || 'source.list' || 'search.scopes' || 'category.list' ||
            'engine.status' => const <Object?>[],
        _ => null,
      },
      'error': null,
      'code': null,
    }), 200,
    headers: const {'content-type': 'application/json'});

/// 起手那一批调用（排序后比）：根上的主题偏好 + 落地页「找书」的两发。
///
/// 为什么排序再比：这三发在同一帧里各走自己的 await，谁先落字节没定数；
/// 而"不该有第四条"这条护栏读的是**集合与条数**，不是顺序。
const List<String> _startupActions = <String>[
  'search.scopes',
  'source.list',
  'theme.preferences',
];

/// 把根挂起来：收摊那两臂测的是"退出有没有喊引擎"，不测 `/ws`。
/// 落地页（找书）会订阅事件流，而 [RpcEngineRepository.events] 一被订阅就真去连
/// `ws://127.0.0.1:59999` —— 测试的假时钟里那次 connect 永不完成，
/// 留下的 5s 超时定时器让整条臂以"Pending timers"红掉（不是回归，是仪器没接上）。
/// 所以这一臂把事件流换成空流，`/ws` 自己那一支另有测试。
Widget _app(BootstrapResult boot) => boot.scope(ProviderScope(
      overrides: [
        engineEventsProvider
            .overrideWith((_) => const Stream<EngineEvent>.empty()),
      ],
      child: const LegodoApp(),
    ));

void main() {
  testWidgets('关窗那一发：engine.shutdown 真发出去、进程真收到 SIGTERM', (tester) async {
    final sent = <Map<String, Object?>>[];
    final client = RpcClient(_endpoint, client: MockClient((request) async {
      final body =
          jsonDecode(utf8.decode(request.bodyBytes)) as Map<String, Object?>;
      sent.add(body);
      return _okFor(body['action']! as String);
    }));
    final repository = RpcEngineRepository(client);
    final process = _FakeProcess();
    final handle = EngineHandle(
        StartedEngine(process: process, endpoint: _endpoint, stderrTail: const <String>[]),
        client,
        repository);

    await tester.pumpWidget(_app(
        BootstrapResult(repository: repository, handle: handle)));
    await tester.pumpAndSettle();

    // 起手那一批是"根上的主题偏好 + 落地页找书的两发"；退出前不该有第四条业务调用。
    expect(
        sent.map((Map<String, Object?> m) => m['action']! as String).toList()
          ..sort(),
        _startupActions,
        reason: '多出来的那一条就是收摊前多打的业务调用');
    expect(process.kills, isEmpty);

    final response = await tester.binding.handleRequestAppExit();
    await tester.pump();

    expect(response, AppExitResponse.exit, reason: '回 cancel 就是把关窗按回去');
    final shutdown = sent.last;
    expect(shutdown['action'], 'engine.shutdown',
        reason: '没接到这一发就等于关窗后进程还在跑');
    expect((shutdown['payload']! as Map)['reason'], isNotEmpty,
        reason: 'reason 是服务端日志里唯一说"谁让它退的"那一段');
    expect(process.kills, contains(ProcessSignal.sigterm));
  });

  testWidgets('连上之后没握上手（引擎半路死）：退出发不动也不能把窗口卡住', (tester) async {
    final process = _FakeProcess();
    // 一发都回不了的 client：shutdown 抛 transport，收摊那一手必须照样把进程杀掉。
    final client = RpcClient(_endpoint, client: MockClient((_) async {
      throw http.ClientException('connection reset');
    }));
    final repository = RpcEngineRepository(client);
    final handle = EngineHandle(
        StartedEngine(process: process, endpoint: _endpoint, stderrTail: const <String>[]),
        client,
        repository);

    await tester.pumpWidget(_app(
        BootstrapResult(repository: repository, handle: handle)));
    final response = await tester.binding.handleRequestAppExit();

    expect(response, AppExitResponse.exit);
    expect(process.kills, contains(ProcessSignal.sigterm),
        reason: '喊不动引擎更要杀进程，不然它占着端口等下一次起服');
  });

  test('句柄收摊只收一遍：第二遍不许再发 engine.shutdown', () async {
    var sends = 0;
    final client = RpcClient(_endpoint, client: MockClient((request) async {
      sends++;
      return _okFor('engine.shutdown');
    }));
    final repository = RpcEngineRepository(client);
    final process = _FakeProcess();
    final handle = EngineHandle(
        StartedEngine(process: process, endpoint: _endpoint, stderrTail: const <String>[]),
        client,
        repository);

    await handle.dispose();
    await handle.dispose();
    expect(sends, 1);
    expect(process.kills.length, 1);
  });

  testWidgets('没有句柄那一臂（widget 测试的形状）：退出照放行，不炸', (tester) async {
    // 正对照：上面两条断言的是"有句柄时真收了摊"，这一条证仪器不是自己响的——
    // 默认 provider 给的是 null，`_LegodoAppState` 那一发 `?.` 必须稳稳走过。
    await tester.pumpWidget(ProviderScope(
      overrides: [
        engineRepositoryProvider.overrideWithValue(FakeEngineRepository()),
      ],
      child: const LegodoApp(),
    ));
    await tester.pump();
    expect(find.byType(MaterialApp), findsOneWidget);
    expect(await tester.binding.handleRequestAppExit(), AppExitResponse.exit);
  });

  test('FakeEngineRepository 那一臂不带句柄：provider 默认就是 null', () async {
    final container = ProviderContainer(
        overrides: [engineRepositoryProvider.overrideWithValue(FakeEngineRepository())]);
    addTearDown(container.dispose);
    expect(container.read(engineHandleProvider), isNull);
    final scoped = BootstrapResult(repository: FakeEngineRepository());
    expect(scoped.handle, isNull);
  });
}
