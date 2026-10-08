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

/// 按 action 回合法信封：`bookshelf.list` 回**空数组**（空书架是真形状，`data: null`
/// 不是——仓库层会把它当坏形状报 transport，那条错误路径不该混进收摊那两条臂里）。
http.Response _okFor(String action) => http.Response(
    jsonEncode(<String, Object?>{
      'v': 1,
      'id': 'd-1',
      'ok': true,
      'data': action == 'bookshelf.list' ? <Object?>[] : null,
      'error': null,
      'code': null,
    }), 200,
    headers: const {'content-type': 'application/json'});

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

    await tester.pumpWidget(
        BootstrapResult(repository: repository, handle: handle).scope(const LegodoApp()));
    await tester.pumpAndSettle();

    // 起手那一发是书架（路由落在 /bookshelf）；退出前不该有第二条业务调用。
    expect(sent.map((Map<String, Object?> m) => m['action']), ['bookshelf.list']);
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

    await tester.pumpWidget(
        BootstrapResult(repository: repository, handle: handle).scope(const LegodoApp()));
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
