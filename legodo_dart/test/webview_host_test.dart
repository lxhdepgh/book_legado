/// 反向 WebView 通道在 Dart 这一侧的管道（§8 R1 / G3-D1）。
///
/// 这一档不起进程：它钉的是**形状与归属**——帧怎么解、答案八格怎么落、
/// 不作答/渲染失败/答晚了各是什么读数。真往返在
/// `webview_reverse_channel_live_arm_test.dart` 那一臂。
///
/// 三条最容易悄悄写错的地方都在这里有名字：
/// - `js_result` 缺键与空串不是一件事（宿主没跑脚本 ≠ 脚本返回了空）；
/// - `engine.webContentReady` 不许走单飞顶替（并发渲染的两条答案会被顶掉一条，
///   而服务端那一侧其实已经收到正文了）；
/// - 一次渲染不许压着下一次（预取连着求时，每条各等自己的 20s）。
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:legodo_client/core/events/engine_event_client.dart';
import 'package:legodo_client/core/events/webview_request.dart';
import 'package:legodo_client/core/rpc/engine_endpoint.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';
import 'package:legodo_client/core/webview/webview_host.dart';

const EngineEndpoint _endpoint = EngineEndpoint(
  host: '127.0.0.1', port: 1, token: 't',
  protocolVersion: 1, build: 'test', pid: 1,
);

/// `service.py` 里 `request_web_content` 那一发请求帧的形状（键名以服务端为准）。
Map<String, Object?> _requestFrame(String requestId,
        {Map<String, Object?>? options}) =>
    <String, Object?>{
      'type': 'request',
      'name': 'webview.content',
      'request_id': requestId,
      'payload': <String, Object?>{
        'url': 'http://127.0.0.1:9/g3web/ch/1.html',
        'source_url': 'http://127.0.0.1:9/g3web',
        'options': options ?? <String, Object?>{'webView': true},
      },
    };

WebviewRequest _request(String id, {String url = 'http://h/ch/1.html'}) =>
    WebviewRequest(
        requestId: id,
        url: url,
        sourceUrl: 'http://h',
        options: const <String, Object?>{'webView': true});

/// 一个只认 `engine.webContentReady` 的回话器：`data` 那格按 [delivered] 给形状。
RpcEngineRepository _repository(List<Map<String, Object?>> sent,
    {int delayMs = 0, Object? delivered = true}) {
  return RpcEngineRepository(RpcClient(_endpoint,
      client: MockClient((request) async {
        final body = jsonDecode(utf8.decode(request.bodyBytes))
            as Map<String, Object?>;
        sent.add(body);
        if (delayMs > 0) {
          await Future<void>.delayed(Duration(milliseconds: delayMs));
        }
        return http.Response(
            jsonEncode(<String, Object?>{
              'v': 1, 'id': body['id'], 'ok': true, 'code': null, 'error': null,
              'data': <String, Object?>{'delivered': delivered},
            }),
            200,
            headers: <String, String>{'content-type': 'application/json'});
      })));
}

/// 只继承、一个方法都不覆写：拿基类的 throw 默认体。
class _ScaffoldRepository extends EngineRepository {
  const _ScaffoldRepository();
}

Future<void> _until(bool Function() condition,
    {Duration timeout = const Duration(seconds: 2)}) async {
  final deadline = DateTime.now().add(timeout);
  while (!condition()) {
    if (DateTime.now().isAfter(deadline)) {
      throw StateError('等不到条件成立（$timeout 内）');
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

void main() {
  group('request 帧解码', () {
    test('服务端的形状解得开：四个字段各归各位', () {
      final request = decodeWebviewRequest(_requestFrame('w7',
          options: <String, Object?>{
            'webView': true, 'javaScript': '@js:document.title', 'charset': 'utf-8',
          }));
      expect(request.requestId, 'w7');
      expect(request.url, 'http://127.0.0.1:9/g3web/ch/1.html');
      expect(request.sourceUrl, 'http://127.0.0.1:9/g3web');
      expect(request.wantsWebView, isTrue);
      expect(request.javaScript, '@js:document.title',
          reason: '脚本要交给宿主跑；读不出来 webJs 那一族就永远没有完成值');
    });

    test('没有 javaScript 时它是 null，不是空串', () {
      expect(decodeWebviewRequest(_requestFrame('w1')).javaScript, isNull,
          reason: 'null=宿主没跑脚本，空串=跑了返回空；服务端读的是有没有这个键');
    });

    test('request_id 缺了/空了/类型不对都当场响，不许默默造一个', () {
      for (final frame in <Map<String, Object?>>[
        <String, Object?>{..._requestFrame('w1'), 'request_id': null},
        <String, Object?>{..._requestFrame('w1'), 'request_id': ''},
        <String, Object?>{..._requestFrame('w1'), 'request_id': 7},
      ]) {
        expect(() => decodeWebviewRequest(frame),
            throwsA(isA<EngineFailure>()
                .having((EngineFailure e) => e.kind, 'kind',
                    EngineFailureKind.transport)),
            reason: '$frame');
      }
    });

    test('payload / options 形状变了要响：缺 options 不许当"没有选项"', () {
      expect(
          () => decodeWebviewRequest(<String, Object?>{
                'type': 'request', 'request_id': 'w1', 'payload': '不是对象'
              }),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)));
      expect(
          () => decodeWebviewRequest(<String, Object?>{
                'type': 'request',
                'request_id': 'w1',
                'payload': <String, Object?>{'url': 'http://h/1'}
              }),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.message, 'message',
                  contains('options'))));
    });

    test('事件解码器不接 request 帧：一帧两个出口迟早飘', () {
      expect(() => decodeFrame(_requestFrame('w1')),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)
              .having((EngineFailure e) => e.message, 'message',
                  contains('webviewRequests'))));
      // 正对照：同一支解码器认事件帧，否则上面那条"响"可能只是"全都不认"。
      expect(decodeFrame(<String, Object?>{
        'type': 'event', 'name': 'SearchFinishedEvent', 'payload': <String, Object?>{}
      }).single.name, 'SearchFinishedEvent');
    });
  });

  group('engine.webContentReady 仓库面', () {
    test('八格落到 payload 上，键名与服务端签名一致', () async {
      final sent = <Map<String, Object?>>[];
      final delivery = await _repository(sent)
          .webContentReady(
              requestId: 'w3', body: '<html>正文</html>',
              url: 'http://h/ch/1.html', cookie: 'a=1', jsResult: '标题');
      expect(delivery.delivered, isTrue);
      expect(sent.single['action'], 'engine.webContentReady');
      expect(sent.single['payload'], <String, Object?>{
        'request_id': 'w3', 'body': '<html>正文</html>', 'status': 200,
        'url': 'http://h/ch/1.html', 'cookie': 'a=1', 'error': '',
        'js_result': '标题',
      });
    });

    test('没跑脚本就不发 js_result 这个键；发了空串是另一件事', () async {
      final sent = <Map<String, Object?>>[];
      await _repository(sent).webContentReady(requestId: 'w1', body: 'x');
      expect((sent.single['payload']! as Map).containsKey('js_result'), isFalse,
          reason: '服务端读"有没有这个键"才分得开没跑与跑了回空；折成空串就混了');
      expect((sent.single['payload']! as Map).containsKey('headers'), isFalse,
          reason: 'schema 那里 additionalProperties:false，多塞一个 null 键是 schema 错');

      await _repository(sent)
          .webContentReady(requestId: 'w2', body: 'x', jsResult: '');
      expect(sent.last['payload'], containsPair('js_result', ''),
          reason: '正对照：脚本真返回了空时这一格必须发得出去');
    });

    test('并发两发都不许被顶掉：顶替会把已经送达的答案读成没答上', () async {
      // 单飞顶替对"同一动作的新值盖旧值"是对的，但这里两条是**不同 request_id 的答案**：
      // 服务端收到第一条时就已经把正文交回引擎了，客户端抛 cancelled 只会让宿主再答一遍。
      final sent = <Map<String, Object?>>[];
      final client = RpcClient(_endpoint,
          client: MockClient((request) async {
            final body = jsonDecode(utf8.decode(request.bodyBytes))
                as Map<String, Object?>;
            sent.add(body);
            await Future<void>.delayed(const Duration(milliseconds: 30));
            return http.Response(
                jsonEncode(<String, Object?>{
                  'v': 1, 'id': body['id'], 'ok': true, 'code': null, 'error': null,
                  'data': <String, Object?>{'delivered': true},
                }),
                200,
                headers: <String, String>{'content-type': 'application/json'});
          }));
      final repository = RpcEngineRepository(client);
      final deliveries = await Future.wait(<Future<WebviewDelivery>>[
        repository.webContentReady(requestId: 'w1', body: 'a'),
        repository.webContentReady(requestId: 'w2', body: 'b'),
      ]);
      expect(deliveries.map((d) => d.delivered), <bool>[true, true]);
      expect(sent.map((e) => (e['payload']! as Map)['request_id']), <String>['w1', 'w2']);
      // 仪器自己的对照：这一发压根没进在飞表——走了单飞的话这里挂着的就是它，
      // 而上面那两条 delivered 会变成一条 cancelled。
      expect(client.inFlight, isEmpty);
    });

    test('回话里 delivered 不是布尔：那是形状变了，读成"答晚了"就是假绿灯', () async {
      for (final delivered in <Object?>[null, 'true', 1]) {
        final sent = <Map<String, Object?>>[];
        await expectLater(
            _repository(sent, delivered: delivered)
                .webContentReady(requestId: 'w1', body: 'a'),
            throwsA(isA<EngineFailure>()
                .having((EngineFailure e) => e.kind, 'kind',
                    EngineFailureKind.transport)
                .having((EngineFailure e) => e.action, 'action',
                    'engine.webContentReady')),
            reason: 'delivered=$delivered');
      }
    });

    test('契约面的默认体仍然抛 unavailable（这一发没接上时必须有声音）', () async {
      await expectLater(
          const _ScaffoldRepository()
              .webContentReady(requestId: 'w1', body: 'a'),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.unavailable)
              .having((EngineFailure e) => e.message, 'message',
                  contains('当前环境不支持 engine.webContentReady'))));
    });
  });

  group('attachWebviewHost', () {
    late StreamController<WebviewRequest> requests;
    late FakeEngineRepository repository;
    late List<Object> heard;
    late StreamSubscription<WebviewRequest> subscription;

    setUp(() {
      requests = StreamController<WebviewRequest>.broadcast();
      repository = FakeEngineRepository();
      heard = <Object>[];
    });
    tearDown(() async {
      await subscription.cancel();
      await requests.close();
    });

    void attach(WebviewHost host) {
      subscription = attachWebviewHost(
        requests: requests.stream,
        repository: repository,
        host: host,
        onError: (error, _) => heard.add(error),
      );
    }

    test('宿主答了：request_id 原样带回去，正文落 body', () async {
      attach(ScriptedWebviewHost(
          (request) async => '正文 for ${request.requestId}'));
      requests.add(_request('w9'));
      await _until(() => repository.callsFor('engine.webContentReady') == 1);
      expect(repository.payloadFor('engine.webContentReady'), <String, Object?>{
        'request_id': 'w9', 'body': '正文 for w9', 'status': 200,
        'url': 'http://h/ch/1.html', 'cookie': '', 'error': '',
      }, reason: 'request_id 是服务端那把 future 的身份证，答错等于没答');
      expect(heard, isEmpty);
    });

    test('不作答那一支：宿主被叫到了，但一发都不交回', () async {
      final host = ScriptedWebviewHost((_) async => null);
      attach(host);
      requests.add(_request('w1'));
      await _until(() => host.calls.length == 1);
      // 交回与否分得开：不作答=让引擎等满 20s，悄悄发一发空 body 会被引擎读成
      // "正文是空的"那一章，两条读数完全不同。
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(host.calls.single.url, 'http://h/ch/1.html');
      expect(repository.callsFor('engine.webContentReady'), 0);
      expect(heard, isEmpty);
    });

    test('宿主抛错折成 error 交回去：引擎据此当场退回非浏览器路径', () async {
      attach(ScriptedWebviewHost((_) async => throw StateError('页面崩了')));
      requests.add(_request('w5'));
      await _until(() => repository.callsFor('engine.webContentReady') == 1);
      final payload = repository.payloadFor('engine.webContentReady')!;
      expect('${payload['error']}', contains('页面崩了'));
      expect(payload['body'], '');
      expect(heard, isEmpty, reason: '渲染失败已经交回服务端了，不该在客户端再报一次');
    });

    test('答晚了（delivered:false）出声但不折成异常', () async {
      repository.webviewDelivered = false;
      attach(ScriptedWebviewHost((_) async => '迟到的正文'));
      requests.add(_request('w6'));
      await _until(() => repository.callsFor('engine.webContentReady') == 1);
      expect(heard, hasLength(1));
      expect(heard.single, isA<EngineFailure>()
          .having((EngineFailure e) => e.action, 'action',
              'engine.webContentReady')
          .having((EngineFailure e) => e.message, 'message', contains('20s')));
    });

    test('空正文又没说原因：出声（引擎会把它当成功的空章）', () async {
      attach(ScriptedWebviewHost((_) async => ''));
      requests.add(_request('w8'));
      await _until(() => repository.callsFor('engine.webContentReady') == 1);
      expect(heard, hasLength(1));
      expect('${heard.single}', contains('空正文'));
      expect(repository.callsFor('engine.webContentReady'), 1,
          reason: '出声归出声，答案照交——折不折成异常是服务端的事');
    });

    test('一次渲染不许压着下一次：慢的那一发在后，快的那先发得出去', () async {
      attach(ScriptedWebviewHost((request) async {
        if (request.requestId == 'slow') {
          await Future<void>.delayed(const Duration(milliseconds: 120));
          return '慢的正文';
        }
        return '快的正文';
      }));
      requests.add(_request('slow'));
      requests.add(_request('fast'));
      await _until(() => repository.callsFor('engine.webContentReady') == 2);
      final payloads = repository.payloadsFor('engine.webContentReady');
      expect(payloads.map((p) => (p! as Map)['request_id']), <String>['fast', 'slow'],
          reason: '在监听回调里 await 会把流压着：预取连着求时后面的帧全排在第一条后面');
    });
  });
}
