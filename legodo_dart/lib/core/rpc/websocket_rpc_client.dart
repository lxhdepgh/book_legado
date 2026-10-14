/// WebSocket RPC 客户端，对接 Go 后端。
///
/// 对齐 Go gateway.RPCEnvelope 格式：
///   { "v": 1, "action": "...", "payload": {...} }
///
/// 响应格式：
///   { "success": true/false, "data": ..., "error": "...", "id": ... }
library;

import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'engine_endpoint.dart';
import 'envelope.dart';

class WebSocketRpcClient {
  WebSocketRpcClient(this.endpoint);

  final EngineEndpoint endpoint;
  WebSocketChannel? _channel;
  final Map<int, Completer<Map<String, dynamic>>> _pendingRequests = {};
  int _seq = 0;
  StreamSubscription? _subscription;

  /// 连接到 Go 后端 WebSocket
  Future<void> connect() async {
    if (_channel != null) {
      return; // 已连接
    }

    final wsUrl = Uri(
      scheme: 'ws',
      host: endpoint.host,
      port: endpoint.port,
      path: '/rpc',
    );

    try {
      _channel = WebSocketChannel.connect(wsUrl);
      
      // 监听消息
      _subscription = _channel!.stream.listen(
        (message) {
          _handleMessage(message);
        },
        onError: (error) {
          _handleError(error);
        },
        onDone: () {
          _handleDisconnect();
        },
        cancelOnError: false,
      );

      // 等待连接建立
      await _channel!.ready;
    } catch (e) {
      throw RpcError('WebSocket 连接失败: $e');
    }
  }

  /// 断开连接
  Future<void> disconnect() async {
    await _subscription?.cancel();
    await _channel?.sink.close();
    _channel = null;
    _subscription = null;
    
    // 取消所有待处理请求
    for (final completer in _pendingRequests.values) {
      if (!completer.isCompleted) {
        completer.completeError(RpcError('连接已关闭'));
      }
    }
    _pendingRequests.clear();
  }

  /// 发送 RPC 请求
  Future<Map<String, dynamic>> call(
    String action, {
    Map<String, dynamic>? payload,
  }) async {
    if (_channel == null) {
      await connect();
    }

    final seq = ++_seq;
    final completer = Completer<Map<String, dynamic>>();
    _pendingRequests[seq] = completer;

    // 构建 Go 信封格式
    final request = {
      'v': 1,
      'action': action,
      'payload': payload ?? <String, dynamic>{},
    };

    try {
      _channel!.sink.add(jsonEncode(request));
      
      // 等待响应（30秒超时）
      final result = await completer.future.timeout(
        const Duration(seconds: 30),
        onTimeout: () => throw RpcError('$action 请求超时'),
      );
      
      return result;
    } catch (e) {
      _pendingRequests.remove(seq);
      rethrow;
    }
  }

  /// 处理收到的消息
  void _handleMessage(dynamic message) {
    try {
      final Map<String, dynamic> response;
      if (message is String) {
        response = jsonDecode(message);
      } else if (message is List<int>) {
        response = jsonDecode(utf8.decode(message));
      } else {
        return;
      }

      // 提取 ID（Go 端返回的 id 字段）
      final id = response['id'] as int?;
      if (id == null) {
        return;
      }

      final completer = _pendingRequests.remove(id);
      if (completer == null) {
        return; // 未知请求或已超时
      }

      // 检查成功标志
      final success = response['success'] as bool? ?? false;
      if (!success) {
        final error = response['error'] as String? ?? '未知错误';
        completer.completeError(RpcError(error));
        return;
      }

      // 返回 data 字段
      final data = response['data'];
      if (data is Map) {
        completer.complete(Map<String, dynamic>.from(data));
      } else if (data is List) {
        completer.complete(data);
      } else {
        completer.complete({'result': data});
      }
    } catch (e) {
      // 解析错误，忽略该消息
    }
  }

  void _handleError(dynamic error) {
    // 出错时取消所有待处理请求
    for (final completer in _pendingRequests.values) {
      if (!completer.isCompleted) {
        completer.completeError(RpcError('WebSocket 错误: $error'));
      }
    }
    _pendingRequests.clear();
  }

  void _handleDisconnect() {
    _handleError('连接已断开');
  }
}

class RpcError implements Exception {
  RpcError(this.message);
  final String message;
  
  @override
  String toString() => 'RpcError: $message';
}
