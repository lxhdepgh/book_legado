/// RPC 客户端那一发：字节口径、401、超时、以及**同 action 顶替**。
///
/// 替身是 `package:http` 的 `MockClient`：它收的是真 `Request` 对象，所以
/// "中文 payload 变 latin1 乱码"这一类坑能被真拦下来——换成手搓的假
/// `send(String body)` 就永远测不到那条。
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:legodo_client/core/rpc/engine_endpoint.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';

const EngineEndpoint _endpoint = EngineEndpoint(
  host: '127.0.0.1',
  port: 59999,
  token: 'tok-测试',
  protocolVersion: 1,
  build: 'test',
  pid: 1,
);

const Map<String, String> _json = {'content-type': 'application/json'};

/// 真 `Request` → 真解码：这是"客户端到底发出去了什么"的唯一凭据。
Map<String, Object?> _sentAsMap(http.Request request) =>
    jsonDecode(utf8.decode(request.bodyBytes)) as Map<String, Object?>;

http.Response _ok(Object? data, {String id = 'd-1'}) => http.Response(
    jsonEncode(<String, Object?>{
      'v': 1, 'id': id, 'ok': true, 'data': data, 'error': null, 'code': null,
    }), 200, headers: _json);

void main() {
  test('发出去的是 UTF-8 字节，中文书名原样回来', () async {
    Map<String, Object?>? sent;
    final client = RpcClient(_endpoint, client: MockClient((request) async {
      sent = _sentAsMap(request);
      return _ok(['我的功法无限提升']);
    }));
    expect(await client.call('bookshelf.add', payload: {'name': '我的功法无限提升'}),
        ['我的功法无限提升']);
    expect((sent!['payload']! as Map)['name'], '我的功法无限提升',
        reason: 'latin1 编码会把这串变成乱码，那就是"点了没反应"的前一半');
    expect(sent!['v'], kProtocolVersion);
    expect(sent!['action'], 'bookshelf.add');
    client.dispose();
  });

  test('带的头：Bearer 令牌 + content-type + 带 scheme 的 url，一个都不许少', () async {
    http.Request? seen;
    final client = RpcClient(_endpoint, client: MockClient((request) async {
      seen = request;
      return _ok(null);
    }));
    await client.call('engine.ping');
    expect(seen!.headers['authorization'], 'Bearer tok-测试');
    expect(seen!.headers['content-type'], 'application/json; charset=utf-8');
    expect(seen!.url.toString(), 'http://127.0.0.1:59999/rpc',
        reason: '不带 scheme 的 Uri 会被 package:http 直接拒收');
    client.dispose();
  });

  group('不是信封的那些回话都归 transport', () {
    Future<EngineFailure> failWith(http.Response response) async {
      final client = RpcClient(_endpoint,
          client: MockClient((_) async => response));
      try {
        await client.call('bookshelf.list');
        throw StateError('没抛 = 这一臂作废');
      } on EngineFailure catch (exc) {
        expect(exc.kind, EngineFailureKind.transport);
        return exc;
      } finally {
        client.dispose();
      }
    }

    test('401 说的是令牌不对，不是一条失败的 RPC', () async {
      final exc = await failWith(
          http.Response('{"error":"unauthorized"}', 401, headers: _json));
      expect(exc.message, contains('令牌'));
    });

    test('500 带 HTML：截断后仍然报出来', () async {
      final exc = await failWith(
          http.Response('<html><body>boom</body></html>\n' * 40, 500));
      expect(exc.message, contains('500'));
      expect(exc.message.length, lessThan(400));
    });

    test('200 但回来的不是 JSON', () async {
      final exc =
          await failWith(http.Response('not json', 200, headers: _json));
      expect(exc.message, contains('不是 JSON'));
    });

    test('200 但信封 v 不是 1：版本不符不许放行到业务', () async {
      final exc = await failWith(http.Response(
          '{"v":2,"id":"d-1","ok":true,"data":[]}', 200, headers: _json));
      expect(exc.message, contains('信封版本'));
    });

    test('200 但缺 ok：那是形状坏了，不是"这一发失败了"', () async {
      final exc = await failWith(
          http.Response('{"v":1,"id":"d-1","data":[]}', 200, headers: _json));
      expect(exc.message, contains('没有 ok 字段'));
    });
  });

  test('hello 拿引擎回的版本号当判据：不符就拒', () async {
    Future<String?> refuse(int served) async {
      final client = RpcClient(_endpoint, client: MockClient((_) async {
        return _ok({'protocol_version': served, 'build': 'x'});
      }));
      try {
        await client.hello();
        return null;
      } on EngineFailure catch (exc) {
        return exc.message;
      } finally {
        client.dispose();
      }
    }

    expect(await refuse(kProtocolVersion), isNull,
        reason: '版本一致时必须放行，否则这条测试永远在绿');
    final mismatch = await refuse(kProtocolVersion + 1);
    expect(mismatch, contains('版本不一致'));
    expect(mismatch, contains('build=x'));
  });

  test('客户端预算到了报 timeout，并且带上是哪个 action', () async {
    final client = RpcClient(_endpoint,
        timeout: const Duration(milliseconds: 20),
        client: MockClient((_) async {
          await Future<void>.delayed(const Duration(seconds: 5));
          return _ok(null);
        }));
    await expectLater(
        client.call('check.start'),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind',
                EngineFailureKind.timeout)
            .having((EngineFailure e) => e.action, 'action', 'check.start')));
  });

  group('同 action 单飞顶替（服务端没做这条，客户端必须做）', () {
    test('旧那一发的结果算出来了也不交，报 cancelled', () async {
      final gate = Completer<void>();
      final seqs = <int>[];
      final client = RpcClient(_endpoint, client: MockClient((request) async {
        seqs.add(_sentAsMap(request)['seq']! as int);
        await gate.future;
        return _ok('late');
      }));

      final first = client.call('check.start', payload: {'n': 1});
      final second = client.call('check.start', payload: {'n': 2});
      // `call` 是 async：字节要过几拍微任务才真交给 client。不flush就数 = 永远数到 0，
      // 那条"顶替不是不发"的断言会变成一个假绿的对空集合的断言。
      await Future<void>.delayed(const Duration(milliseconds: 5));
      expect(seqs, isNotEmpty, reason: '一条都没发 = 这条测试作废');
      expect(seqs.length, 2, reason: '两发都得真发出去——顶替不是不发');
      gate.complete();

      await expectLater(
          first,
          throwsA(isA<EngineFailure>().having((EngineFailure e) => e.kind,
              'kind', EngineFailureKind.cancelled)));
      expect(await second, 'late');
      // 顶替完必须清账：留着的那格会把下一发正常调用误判成"已被顶掉"。
      expect(client.inFlight, isEmpty);
      client.dispose();
    });

    test('正对照：不同 action 各走各的，谁也不顶谁', () async {
      final gate = Completer<void>();
      final client = RpcClient(_endpoint, client: MockClient((request) async {
        final action = _sentAsMap(request)['action'];
        await gate.future;
        return _ok(action);
      }));
      final a = client.call('bookshelf.list');
      final b = client.call('source.list');
      gate.complete();
      expect(await a, 'bookshelf.list');
      expect(await b, 'source.list');
      expect(client.inFlight, isEmpty);
      client.dispose();
    });

    test('singleFlight=false 不参与顶替（shutdown 那一发不能被刷新顶掉）', () async {
      final gate = Completer<void>();
      final client = RpcClient(_endpoint, client: MockClient((_) async {
        await gate.future;
        return _ok(null);
      }));
      final detached = client.call('engine.shutdown', singleFlight: false);
      final flying = client.call('engine.shutdown');
      gate.complete();
      expect(await detached, isNull);
      expect(await flying, isNull);
      client.dispose();
    });
  });
}
