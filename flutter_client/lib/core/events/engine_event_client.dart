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
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../rpc/engine_endpoint.dart';
import '../rpc/envelope.dart';
import 'engine_event.dart';

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
      // 服务端→客户端的反向 WebView 渲染请求（§8 R1）。G2 这四个屏一条都不触发它；
      // 真触发了说明有 `<useweb>` 源在跑，这时候**出声**比默默吞掉诚实。
      throw EngineFailure(EngineFailureKind.unavailable,
          '引擎发来反向 WebView 请求（request_id=${frame['request_id']}），'
          '这一条要 G3 的 WebView 宿主才接得住');
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

  WebSocket? _socket;
  StreamSubscription<dynamic>? _sub;
  bool _closed = false;
  int _attempt = 0;

  /// 只读事件流（broadcast：书架、检测、找书各听各的，互相不挤掉）。
  Stream<EngineEvent> get events => _events.stream;

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
  }
}
