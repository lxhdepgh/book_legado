/// `/ws` 那一根只读流：连上、解码、断了自己回来（§4.3「事件流（不轮询）」的 Dart 半边）。
///
/// 三条不是风格的选择：
///
/// 1. **用 `dart:io` 的 WebSocket，不加 `web_socket_channel`**。这一版是桌面客户端
///    （§11 明写不做 iOS），标准库那一发够用，而多一条依赖就多一条要 pin 的供应链。
/// 2. **重连的底色是"必须赶上 15s"**：服务端在最后一个 `/ws` 会话断开时武装空闲定时器
///    （`service.py:566-568`），退避一旦爬到 15s 以上，客户端还活着、引擎已经自杀。
///    所以下面那串退避的上界是 8s，不是"随便等等"。
/// 3. **错误只在这根流上出声，不往屏幕上扔字符串**：断线时界面上"正在搜索"还挂着，
///    没有这条 error 就分不清"没结果"和"收不到结果"（记忆 `zero-counts-need-a-positive-control`
///    的同一条道理：仪器掉线时的 0 不能当成测量值）。
///
/// `/ws` 上有两种出口，因为帧本身有两种方向：`event`/`batch`/`snapshot` 是引擎**报**
/// 出来的（[events]），`request` 是引擎**求**客户端办事的（[webviewRequests]，§8 R1）。
/// 后者不能塞进事件流：事件是"发生过什么"，订阅者爱听不听；反向请求是"现在等你回答"，
/// 漏掉一条 = 引擎那侧白等 20s，而事件流的 broadcast 语义恰恰允许没人听。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../rpc/engine_endpoint.dart';
import '../rpc/envelope.dart';
import 'engine_event.dart';
import 'webview_request.dart';

/// 一帧 → 零或多条事件。帧形状照 `service.py` 的 `_h_ws` / `_make_forwarder` / `_flush_pending`。
///
/// `snapshot` 里带的是 `{name,payload}`（服务端从快照表里重发的），`batch` 里带的是**整帧**
/// （`{"type":"event",…}`）：两种形状都得收，因为客户端看到的顺序是
/// ready → snapshot → batch\* → event\*，只认一种就会在重连那一发丢状态。
List<EngineEvent> decodeFrame(Map<String, Object?> frame) {
  final type = frame['type'];
  switch (type) {
    case 'event':
      return <EngineEvent>[_eventOf(frame)];
    case 'batch':
    case 'snapshot':
      final events = frame['events'];
      if (events is! List) {
        throw EngineFailure(EngineFailureKind.transport,
            '$type 帧里的 events 不是数组（收到 ${events.runtimeType}）');
      }
      return events
          .map((entry) => _eventOf(Map<String, Object?>.from(entry as Map)))
          .toList(growable: false);
    case 'ready':
      // 版本/摘要那一关在 bootstrap 就过了（engine.hello 不符直接不启界面），
      // 这里不重复判：重复判就有两份"什么算不符"，迟早飘。
      return const <EngineEvent>[];
    case 'request':
      // 反向渲染请求不是事件：它有自己那条口（[EngineEventClient.webviewRequests]
      // + `decodeWebviewRequest`）。走到这里说明调用方拿事件解码器去解了请求帧，
      // 这时候**出声**比默默回一条空列表诚实——后者表现为"引擎一根事件都没发"。
      throw EngineFailure(EngineFailureKind.transport,
          'request 帧（request_id=${frame['request_id']}）不当事件解：'
          '它要从 webviewRequests 那条口交给 WebView 宿主，答回去用的是 engine.webContentReady');
    default:
      throw EngineFailure(EngineFailureKind.transport,
          '未知的事件帧类型：$type（帧的键：${frame.keys.join(',')}）');
  }
}

EngineEvent _eventOf(Map<String, Object?> entry) {
  final name = entry['name'];
  if (name is! String || name.isEmpty) {
    throw EngineFailure(EngineFailureKind.transport,
        '事件帧没有 name（键：${entry.keys.join(',')}）');
  }
  final payload = entry['payload'];
  return EngineEvent(
      name, payload is Map ? Map<String, Object?>.from(payload) : <String, Object?>{});
}

class EngineEventClient {
  EngineEventClient(this.endpoint,
      {this.connectTimeout = const Duration(seconds: 5),
      this.backoff = const <Duration>[
        Duration(seconds: 1),
        Duration(seconds: 2),
        Duration(seconds: 4),
        Duration(seconds: 8)
      ]});

  final EngineEndpoint endpoint;
  final Duration connectTimeout;

  /// 退避序列；越界取最后一格。上界压在空闲自杀（15s）以下，理由见文件头第二条。
  final List<Duration> backoff;

  final StreamController<EngineEvent> _events = StreamController<EngineEvent>.broadcast();
  final StreamController<WebviewRequest> _webviews =
      StreamController<WebviewRequest>.broadcast();

  WebSocket? _socket;
  StreamSubscription<dynamic>? _sub;
  bool _closed = false;
  int _attempt = 0;

  /// 只读事件流（broadcast：书架、检测、找书各听各的，互相不挤掉）。
  Stream<EngineEvent> get events => _events.stream;

  /// 引擎求这一次渲染的那条口（broadcast，理由同 [events]）。
  ///
  /// **没有宿主时它一条也不会掉进 [events]**：G3-D1 这一轮产品侧没有可见 WebView，
  /// 所以这一格默认没人听——那时引擎按 20s 预算拿到"宿主在 20s 内没答这次渲染"
  /// 并退回非浏览器路径。把这一格接上的是 `core/webview/webview_host.dart`。
  Stream<WebviewRequest> get webviewRequests => _webviews.stream;

  /// 连上过没有。测试与"断线"那一格文案都读它，不读屏幕上的条数。
  bool get isConnected => _socket != null;

  Future<void> start() async {
    await _connect();
  }

  Future<void> _connect() async {
    if (_closed) return;
    try {
      final socket = await WebSocket.connect(
        endpoint.wsUri.toString(),
        headers: <String, String>{'authorization': 'Bearer ${endpoint.token}'},
      ).timeout(connectTimeout);
      if (_closed) {
        await socket.close();
        return;
      }
      _socket = socket;
      _attempt = 0;
      _sub = socket.listen(_onData, onDone: _onDone, onError: _onError);
    } on Object catch (exc) {
      _fail('连不上 ${endpoint.host}:${endpoint.port}/ws：$exc');
      _scheduleReconnect();
    }
  }

  void _onData(dynamic raw) {
    // 服务端只发文本帧；真来了二进制就是形状变了，别拿 latin1 猜一遍。
    final text = raw is String ? raw : utf8.decode(raw as List<int>);
    Object? decoded;
    try {
      decoded = jsonDecode(text);
    } on FormatException catch (exc) {
      _fail('事件帧不是 JSON：${exc.message} / ${_clip(text)}');
      return;
    }
    if (decoded is! Map<String, Object?>) {
      _fail('事件帧不是 JSON 对象（收到 ${decoded.runtimeType}）');
      return;
    }
    try {
      // 方向先分岔：`request` 是引擎求客户端办事，其余是引擎报出来的。
      // 分岔放在这里而不是让 decodeFrame 也认得 request——那样一帧就有两个出口，
      // "谁负责答"会飘。
      if (decoded['type'] == 'request') {
        _webviews.add(decodeWebviewRequest(decoded));
        return;
      }
      for (final event in decodeFrame(decoded)) {
        _events.add(event);
      }
    } on EngineFailure catch (exc) {
      _fail(exc.message);
    }
  }

  void _onDone() {
    _socket = null;
    _sub = null;
    if (_closed) return;
    _fail('引擎那头的 /ws 关掉了会话');
    _scheduleReconnect();
  }

  void _onError(Object exc) {
    _socket = null;
    _sub = null;
    if (_closed) return;
    _fail('/ws 出错：$exc');
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_closed) return;
    final wait = backoff[_attempt >= backoff.length ? backoff.length - 1 : _attempt];
    _attempt++;
    Timer(wait, _connect);
  }

  void _fail(String message) => _events.addError(
      EngineFailure(EngineFailureKind.transport, message, action: 'ws'));

  static String _clip(String text) =>
      text.length <= 160 ? text : '${text.substring(0, 160)}…';

  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    await _sub?.cancel();
    final socket = _socket;
    _socket = null;
    if (socket != null) await socket.close();
    await _events.close();
    await _webviews.close();
  }
}
