/// `RpcEngineRepository` 那一层：把契约面上三个方法落到信封上。
///
/// 替身仍然是 `MockClient`（真 `Request`/`Response` 形状）。用 `FakeEngineRepository`
/// 去测 `RpcEngineRepository` 是循环论证——那两个是互不相干的实现，各自要有人钉。
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:legodo_client/core/rpc/engine_endpoint.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';

const EngineEndpoint _endpoint = EngineEndpoint(
  host: '127.0.0.1', port: 1, token: 't',
  protocolVersion: 1, build: 'test', pid: 1,
);

RpcEngineRepository _with(String body, {int status = 200}) => RpcEngineRepository(
      RpcClient(_endpoint,
          client: MockClient((_) async =>
              http.Response(body, status,
                  headers: {'content-type': 'application/json; charset=utf-8'}))),
    );

const _okRows = '{"v":1,"id":"d-1","ok":true,"code":null,"error":null,"data":['
    '{"name":"仙子，助我修行","author":"逆时针的圈","url":"https://e/1",'
    '"source_url":"https://e","durChapterTitle":"第一章 金刚萝莉师姐"}]}';

/// 只继承、一个方法都不覆写：拿基类的 throw 默认体，测的就是"没接上"那一手。
class _ScaffoldRepository extends EngineRepository {
  const _ScaffoldRepository();
}

void main() {
  test('抽象仓库的默认体是抛，不是空实现：新 action 没接上必须有声音', () async {
    const bare = _ScaffoldRepository();
    final arms = <String, Future<Object?> Function()>{
      'engine.hello': bare.hello,
      'bookshelf.list': bare.listBookshelf,
      'engine.shutdown': bare.shutdown,
    };
    expect(arms.length, 3, reason: '漏一条 = 那一格的 throw 默认体没人测过');
    for (final entry in arms.entries) {
      await expectLater(
          entry.value(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.unavailable)
              .having((EngineFailure e) => e.message, 'message',
                  contains('当前环境不支持 ${entry.key}'))),
          reason: entry.key);
    }
  });

  test('listBookshelf 把 data 数组变成模型，字段一个不丢', () async {
    final items = await _with(_okRows).listBookshelf();
    expect(items.length, 1);
    expect(items.first.name, '仙子，助我修行');
    expect(items.first.subtitle, '逆时针的圈 · 读到 第一章 金刚萝莉师姐');
    // raw 保留原行：后面的门（换源/进度/缓存）不再逐个复制字段。
    expect(items.first.raw['durChapterTitle'], '第一章 金刚萝莉师姐');
  });

  test('include_staged 真的落到 payload 上（不带就不许发这个键）', () async {
    final sent = <Map<String, Object?>>[];
    final repository = RpcEngineRepository(RpcClient(_endpoint,
        client: MockClient((request) async {
          sent.add(jsonDecode(utf8.decode(request.bodyBytes))
              as Map<String, Object?>);
          return http.Response('{"v":1,"id":"d-1","ok":true,"data":[]}', 200,
              headers: {'content-type': 'application/json'});
        })));
    await repository.listBookshelf();
    expect((sent.first['payload']! as Map).containsKey('include_staged'), isFalse);
    await repository.listBookshelf(includeStaged: true);
    expect(sent.last['payload'], {'include_staged': true});
  });

  test('服务端回 null / 回对象 = 形状变了，那一发必须响而不是渲染成空书架', () async {
    for (final body in <String>[
      '{"v":1,"id":"d-1","ok":true,"data":null}',
      '{"v":1,"id":"d-1","ok":true,"data":{"name":"一条而不是数组"}}',
    ]) {
      await expectLater(
          _with(body).listBookshelf(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)),
          reason: body);
    }
  });

  test('hello 把网关那六个字段读成 GatewayInfo', () async {
    final info = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
            '"protocol_version":1,"build":"0.9","capabilities":["rpc"],'
            '"schema_digest":"abc","actions":["engine.hello"],"pid":4242}}')
        .hello();
    expect(info.protocolVersion, 1);
    expect(info.build, '0.9');
    expect(info.capabilities, ['rpc']);
    expect(info.schemaDigest, 'abc');
    expect(info.actions, ['engine.hello']);
    expect(info.pid, 4242);
  });

  test('shutdown 带原因', () async {
    http.Request? seen;
    final repository = RpcEngineRepository(RpcClient(_endpoint,
        client: MockClient((request) async {
          seen = request;
          return http.Response('{"v":1,"id":"d-1","ok":true,"data":null}', 200,
              headers: {'content-type': 'application/json'});
        })));
    await repository.shutdown(reason: 'window closed');
    expect((jsonDecode(utf8.decode(seen!.bodyBytes)) as Map)['payload'],
        {'reason': 'window closed'});
  });

  test('服务端回的 code 在客户端分支上：engine 不会伪装成 timeout', () async {
    final repository = _with(
        '{"v":1,"id":"d-1","ok":false,"data":null,"error":"炸了","code":"engine"}');
    await expectLater(
        repository.listBookshelf(),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
            .having((EngineFailure e) => e.isTransient, 'isTransient', isFalse)));
  });

  test('握手行的令牌不许出现在 toString 里（它会进日志）', () {
    const endpoint = EngineEndpoint(
        host: '127.0.0.1', port: 1234, token: 'super-secret-token',
        protocolVersion: 1, build: 'b', pid: 9);
    expect('$endpoint', isNot(contains('super-secret-token')));
    expect('$endpoint', contains('127.0.0.1:1234'), reason: '排查靠的是地址与 pid');
  });
}
