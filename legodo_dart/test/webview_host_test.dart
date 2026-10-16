/// 反向 WebView 通道在 Dart 这一侧的三块：帧解码、答复的键面、宿主接线。
///
/// FFI 面上这条通道**没有落点**（Go 那侧 `doWebContentReady` 恒回
/// `{"delivered":false}`，`engine_poll` 也只驮事件不驮 request 帧），
/// 所以这一档钉的是接缝本身：帧怎么解、答案八格怎么发、宿主答/不答/答晚了各是什么读数。
/// 真边界在 `webview_reverse_channel_live_arm_test.dart` 那一臂。
///
/// 三条最容易悄悄写错的地方都在这里有名字：
/// - `js_result` 的 null 与空串不是一件事（宿主没跑脚本 ≠ 脚本返回了空）；
/// - 答复是**并发**的两发：第二条不许被第一条顶掉；
/// - 一次渲染不许压着下一次（预取连着求时，每条各走自己的活）。
library;

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/events/webview_request.dart';
import 'package:legodo_client/core/ffi/engine_ffi.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';
import 'package:legodo_client/core/webview/webview_host.dart';

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

/// 记录每一发的 action + params；回话由 [handler] 按发给，不给就回 `data: null`。
class _RecordingFFI implements GoFFIClient {
  final List<({String action, Map<String, dynamic> params})> calls = [];
  Map<String, dynamic> Function(String action, Map<String, dynamic> params)? handler;

  @override
  bool get loaded => true;

  @override
  String get libraryPath => r'Z:\fake\legodo_engine.dll';

  @override
  void load() {}

  @override
  void initialize() {}

  @override
  void cleanup() {}

  @override
  Map<String, dynamic> call(String action, Map<String, dynamic> params) {
    calls.add((action: action, params: params));
    final handler = this.handler;
    if (handler != null) return handler(action, params);
    return <String, dynamic>{'ok': true, 'data': null};
  }
}

FFIEngineAdapter _repo(_RecordingFFI ffi) => FFIEngineAdapter(client: ffi);

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
  });

  group('engine.webContentReady 仓库面', () {
    test('八格落到 payload 上，键名与服务端签名一致', () async {
      final ffi = _RecordingFFI();
      final delivery = await _repo(ffi).webContentReady(
          requestId: 'w3', body: '<html>正文</html>',
          url: 'http://h/ch/1.html', cookie: 'a=1', jsResult: '标题');
      expect(delivery.delivered, isFalse,
          reason: 'FFI 面 Go 恒回 delivered:false：引擎那侧没有在等的请求可满足');
      expect(ffi.calls.single.action, 'engine_web_content_ready');
      expect(ffi.calls.single.params, <String, Object?>{
        'request_id': 'w3', 'body': '<html>正文</html>', 'headers': null,
        'status': 200, 'url': 'http://h/ch/1.html', 'cookie': 'a=1',
        'error': '', 'js_result': '标题',
      });
    });

    test('没跑脚本那一格是 null：与"跑了返回空串"是两件事', () async {
      final ffi = _RecordingFFI();
      await _repo(ffi).webContentReady(requestId: 'w1', body: 'x');
      final payload = ffi.calls.single.params;
      expect(payload.containsKey('js_result'), isTrue);
      expect(payload['js_result'], isNull,
          reason: 'null=宿主没跑脚本，空串=跑了返回空——折成同一个值就混了');

      await _repo(ffi).webContentReady(requestId: 'w2', body: 'x', jsResult: '');
      expect(ffi.calls.last.params['js_result'], '',
          reason: '正对照：脚本真返回了空时这一格是空串');
    });

    test('delivered 按 data 那一格读；非布尔是形状漂了，折成 false', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) =>
            <String, dynamic>{'ok': true, 'data': <String, Object?>{'delivered': true}};
      expect((await _repo(ffi).webContentReady(requestId: 'w1', body: 'x')).delivered,
          isTrue);
      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true, 'data': <String, Object?>{'delivered': 'true'},
          };
      expect((await _repo(ffi).webContentReady(requestId: 'w1', body: 'x')).delivered,
          isFalse,
          reason: 'Go 恒回布尔；非布尔就是形状漂了（读成"答上了"是假绿灯）');
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
