/// 一次 RPC 的发送与**单飞顶替**（设计 §4.6）。
///
/// 两条本地规矩，都是踩过"点了没反应"那一类坑之后定的：
///
/// 1. **收发一律按字节**：`Request.body` 的默认编码是 latin1，中文 payload 直接
///    变乱码；`Response.body` 又按 latin1 解。两侧都显式走 `utf8`，
///    第八十二轮量过的那对数字（S=6,969,038 **B**）就是这么来的口径。
/// 2. **同 action 只许在飞一发**：新的顶掉旧的。旧回话晚到时**丢掉、不交给调用方**——
///    否则"旧请求晚到把新结果盖掉"（那边给这条起的名字）会真的发生。
///    注意这条目前只做到"丢结果"，没做到"掐连接"：服务端该发还是发完了。
///    掐连接（`Client.abort`）留给 G2 那四个高频域一起接，因为它会一次掐掉同一
///    client 上的所有在飞请求，得先有 per-action client 池才好做。
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'action_budgets.dart';
import 'engine_endpoint.dart';
import 'envelope.dart';

/// 信封里的 `id` 前缀：客户端侧标一下是谁发的，服务端原样带回（`ok_envelope` 用）。
const String _idPrefix = 'd';

class RpcClient {
  RpcClient(this.endpoint, {http.Client? client, this.timeout = const Duration(seconds: 30)})
      : _client = client ?? http.Client();

  final EngineEndpoint endpoint;
  final http.Client _client;

  /// 客户端**默认**预算：`action_budgets.dart` 那张表之外的动作都走它。
  /// 服务端对每个 action 另有一条自己的预算（`methods.py` 的 timeout 列），
  /// **谁先响都行**：客户端这条只管"别再干等"，真断靠服务端那条。
  final Duration timeout;

  int _seq = 0;

  /// `action` → 当前在飞那一发的 seq。新的一发把它顶掉。
  final Map<String, int> _inFlight = {};

  /// 握手（`engine.hello`）。版本不符**当场拒**，不放行到业务调用（§4.6）。
  Future<Map<String, Object?>> hello() async {
    final data = await call('engine.hello') as Map<String, Object?>;
    final version = data['protocol_version'];
    if (version != kProtocolVersion) {
      throw EngineFailure(
          EngineFailureKind.transport,
          '应用与引擎版本不一致：客户端讲 v$kProtocolVersion，'
          '引擎回的是 v$version（build=${data['build']}）',
          action: 'engine.hello');
    }
    return data;
  }

  /// 发一发 RPC，返回信封里的 `data`。
  ///
  /// [singleFlight] 为真时同 action 的旧在飞会被顶掉（顶掉表现为旧那一发抛
  /// `cancelled`，与新那一发的 `cancelled` 错误码同源）。
  ///
  /// [budget] 覆盖这一发的预算；不给就走 [clientBudgetFor]（那条长预算的表），
  /// 表外才是构造时那条默认水池——见 `action_budgets.dart` 文件头那条"谁先响都行"。
  Future<RpcData> call(String action,
      {Map<String, Object?>? payload,
      bool singleFlight = true,
      Duration? budget}) async {
    final effective = budget ?? clientBudgetFor(action) ?? timeout;
    final seq = ++_seq;
    if (singleFlight) {
      _inFlight[action] = seq;
    }
    final request = <String, Object?>{
      'v': kProtocolVersion,
      'id': '$_idPrefix-$seq',
      'action': action,
      'seq': seq,
      'payload': payload ?? const <String, Object?>{},
    };
    RpcData data;
    try {
      final response = await _post(jsonEncode(request))
          .timeout(effective,
              onTimeout: () => throw EngineFailure(
                  EngineFailureKind.timeout,
                  '客户端预算 $effective 内没等到 $action 的回话',
                  action: action));
      data = response;
    } on EngineFailure {
      rethrow;
    } catch (exc) {
      throw EngineFailure(EngineFailureKind.transport,
          '$action 传输失败：$exc', action: action);
    }
    if (singleFlight && _inFlight[action] != seq) {
      // 我已经被更新的一发顶掉了：结果算出来也不交。
      throw EngineFailure(EngineFailureKind.cancelled,
          '$action 被同 action 更新的 seq（${_inFlight[action]}）顶掉',
          action: action);
    }
    if (singleFlight && _inFlight[action] == seq) _inFlight.remove(action);
    return data;
  }

  /// 当前在飞的 action 快照（测试用：顶替发生没发生，看这里）。
  Map<String, int> get inFlight => Map.unmodifiable(_inFlight);

  Future<RpcData> _post(String body) async {
    final response = await _client.post(
      endpoint.rpcUri,
      headers: <String, String>{
        'content-type': 'application/json; charset=utf-8',
        'authorization': 'Bearer ${endpoint.token}',
      },
      body: utf8.encode(body),
    );
    final text = utf8.decode(response.bodyBytes);
    if (response.statusCode != 200) {
      // 401 那一发回的是 `{"error":"unauthorized"}`，**不是信封**：令牌错了就是
      // 令牌错了，别伪装成一次失败的 RPC（信封里的六种码没有"你没资格"这一种）。
      throw EngineFailure(
          EngineFailureKind.transport,
          response.statusCode == 401
              ? '令牌不对：引擎回 401（一次性令牌只在 `LEGODO_GATEWAY` 那一行里给）'
              : 'HTTP ${response.statusCode}：${_trim(text)}',
          action: null);
    }
    final Object? decoded;
    try {
      decoded = jsonDecode(text);
    } on FormatException catch (exc) {
      throw EngineFailure(EngineFailureKind.transport,
          '回话不是 JSON：${exc.message} / ${_trim(text)}');
    }
    if (decoded is! Map<String, Object?>) {
      throw EngineFailure(EngineFailureKind.transport,
          '信封不是 JSON 对象（收到 ${decoded.runtimeType}）');
    }
    final envelope = RpcEnvelope.decode(decoded);
    if (envelope.v != kProtocolVersion) {
      throw EngineFailure(EngineFailureKind.transport,
          '信封版本不符：期望 v$kProtocolVersion，收到 v${envelope.v}');
    }
    return envelope.dataOrThrow();
  }

  static String _trim(String text) =>
      text.length <= 160 ? text : '${text.substring(0, 160)}…';

  void dispose() => _client.close();
}
