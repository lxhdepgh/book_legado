/// 信封那一发的 Dart 半边（`lib/core/rpc/envelope.dart`）。
///
/// 这里钉的是**形状**，不是文案：客户端只按 `code` 分支，所以六种码各自的映射、
/// 表外码的降级、以及"信封缺键就当场响"这三条必须有人测。
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/rpc/envelope.dart';

Map<String, Object?> env(Object? json) => Map<String, Object?>.from(json as Map);

void main() {
  group('成功信封', () {
    test('ok=true 交 data，键与 Python 的 ok_envelope 同形', () {
      final envelope = RpcEnvelope.decode(env(jsonDecode('{"v":1,"id":"c-1",'
          '"ok":true,"data":[1,2],"error":null,"code":null}')));
      expect(envelope.ok, isTrue);
      expect(envelope.data, [1, 2]);
      expect(envelope.error, isNull);
    });

    test('data=null 也是成功：不许把它读成"没有数据所以是失败"', () {
      final envelope = RpcEnvelope.decode(
          env(jsonDecode('{"v":1,"id":"c-2","ok":true,"data":null}')));
      expect(envelope.ok, isTrue);
      expect(envelope.dataOrThrow(), isNull);
    });
  });

  group('失败信封：六种码各自进各自的 kind', () {
    // 与 app/gateway/protocol.py 的 ERROR_CODES 逐项对齐（顺序也一致）。
    const wire = <String>[
      'unavailable', 'timeout', 'cancelled', 'bad_request', 'engine', 'schema',
    ];
    for (final code in wire) {
      test(code, () {
        final envelope = RpcEnvelope.decode(env(jsonDecode(
            '{"v":1,"id":"c-3","ok":false,"data":null,"error":"x","code":"$code"}')));
        // dataOrThrow 是**抛**，不是返回：拿它当返回值写，测试会在异常那一步就红。
        Object? caught;
        try {
          envelope.dataOrThrow(action: 'a.b');
        } on EngineFailure catch (exc) {
          caught = exc;
        }
        expect(caught, isA<EngineFailure>(), reason: '$code 必须抛');
        final failure = caught! as EngineFailure;
        expect(failure.kind.wireName, code);
        expect(failure.action, 'a.b');
        expect(failure.message, 'x');
      });
    }

    test('表外的码降级成 engine（Python 侧 err_envelope 同一手）', () {
      final envelope = RpcEnvelope.decode(env(jsonDecode(
          '{"v":1,"id":"c-4","ok":false,"error":"x","code":"impossible_code"}')));
      expect(() => envelope.dataOrThrow(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.engine)));
    });

    test('badRequest 的线上拼写下划线、Dart 枚举名小驼峰，两边不许混', () {
      expect(EngineFailureKind.badRequest.wireName, 'bad_request');
      expect(EngineFailureKind.fromWire('bad_request'),
          EngineFailureKind.badRequest);
    });
  });

  group('信封不合法 = 传输层失败，不是业务失败', () {
    for (final entry in <String, String>{
      '缺 ok': '{"v":1,"id":"c-5","data":null}',
      'v 不是整数': '{"v":"1","id":"c-6","ok":true,"data":null}',
    }.entries) {
      test(entry.key, () {
        expect(() => RpcEnvelope.decode(env(jsonDecode(entry.value))),
            throwsA(isA<EngineFailure>()
                .having((EngineFailure e) => e.kind, 'kind',
                    EngineFailureKind.transport)));
      });
    }
  });

  test('可重试性只给 timeout/transport/unavailable：schema/engine 是改代码的事', () {
    for (final kind in <EngineFailureKind>[
      EngineFailureKind.timeout,
      EngineFailureKind.transport,
      EngineFailureKind.unavailable,
    ]) {
      expect(EngineFailure(kind, 'x').isTransient, isTrue, reason: '$kind');
    }
    for (final kind in <EngineFailureKind>[
      EngineFailureKind.engine,
      EngineFailureKind.schema,
      EngineFailureKind.badRequest,
      EngineFailureKind.cancelled,
    ]) {
      expect(EngineFailure(kind, 'x').isTransient, isFalse, reason: '$kind');
    }
  });

  test('失败信封自己不许抛：kind 表外也要落成一条能显示的话', () {
    const failure = EngineFailure(EngineFailureKind.engine, '引擎炸了');
    expect('$failure', contains('engine'));
    expect('$failure', contains('引擎炸了'));
  });
}
