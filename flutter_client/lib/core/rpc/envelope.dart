/// 信封与错误码：设计 §4.1 那个固定形状的 **Dart 半边**。
///
/// 同源关系是双向钉住的：Python 那半边在 `app/gateway/protocol.py`，
/// 两侧常量不许各自漂——`tests/test_gateway_contract_single_source.py`
/// 拿 `PROTOCOL_VERSION` 与 `ERROR_CODES` 逐项比对本文件的字面量。
///
/// 客户端**只按 `code` 分支，绝不按 `error` 文案分支**（那边文件里那条规矩的用处
/// 就在这儿：文案是给人在日志里看的，分支是要跨语言稳的）。
library;

/// 协议版本。版本不符就不放行到业务调用（§4.6：直接拒绝启动 UI）。
const int kProtocolVersion = 1;

/// 错误码冻结词汇表，照 `ERROR_CODES` 的顺序与拼写。
///
/// [transport] 是 **Dart 侧独有**的一格：服务端根本没机会回信封的那些情况
/// （连不上、401、回来的不是 JSON、信封版本不对）。它不在 Python 的表里，
/// 契约测试只比对前面六格。
enum EngineFailureKind {
  unavailable,
  timeout,
  cancelled,
  badRequest,
  engine,
  schema,
  transport;

  /// 线上拼写（`bad_request` 而不是 `badRequest`）。
  String get wireName => switch (this) {
        EngineFailureKind.badRequest => 'bad_request',
        _ => name,
      };

  static EngineFailureKind fromWire(String? code) {
    for (final kind in EngineFailureKind.values) {
      if (kind.wireName == code) return kind;
    }
    // 表外的 code 一律降级成 engine：服务端 err_envelope 自己也是这个兜底，
    // 两侧不许有一种"客户端不知道怎么处理"的形状。
    return EngineFailureKind.engine;
  }
}

/// 一次 RPC 的失败。`UnimplementedError` 式的"这一屏没有这条数据"也用它表达。
class EngineFailure implements Exception {
  const EngineFailure(this.kind, this.message, {this.action});

  final EngineFailureKind kind;
  final String message;
  final String? action;

  /// 能不能让用户重试一次就好（超时/传输抖动算可重试，schema/engine 是改代码的事）。
  bool get isTransient =>
      kind == EngineFailureKind.timeout ||
      kind == EngineFailureKind.transport ||
      kind == EngineFailureKind.unavailable;

  @override
  String toString() =>
      'EngineFailure(${kind.wireName}'
      '${action == null ? '' : ' @action=$action'}): $message';
}

/// 成功信封的 `data` 之外什么都没带的那种回话（`{"ok":true,"data":null}`）。
typedef RpcData = Object?;

/// 解出来的信封：`{v,id,ok,data,error,code}`。
class RpcEnvelope {
  const RpcEnvelope({
    required this.v,
    required this.id,
    required this.ok,
    required this.data,
    required this.error,
    required this.code,
  });

  final int v;
  final Object? id;
  final bool ok;
  final RpcData data;
  final String? error;
  final String? code;

  /// 从**已解码的 JSON** 构造；形状不对当场抛 [EngineFailure]（transport 那一格），
  /// 而不是让 `null` 一路渗进业务字段——那才是"点了没反应"的成因。
  factory RpcEnvelope.decode(Map<String, Object?> json, {String? action}) {
    if (!json.containsKey('ok')) {
      throw EngineFailure(EngineFailureKind.transport,
          '信封里没有 ok 字段（收到的键：${json.keys.join(',')}）',
          action: action);
    }
    final version = json['v'];
    if (version is! int) {
      throw EngineFailure(EngineFailureKind.transport,
          '信封版本不是整数（收到 ${json['v'].runtimeType}）',
          action: action);
    }
    return RpcEnvelope(
      v: version,
      id: json['id'],
      ok: json['ok'] == true,
      data: json['data'],
      error: json['error'] == null ? null : '${json['error']}',
      code: json['code'] == null ? null : '${json['code']}',
    );
  }

  /// 成功就交 `data`，失败就抛带 kind 的 [EngineFailure]。
  RpcData dataOrThrow({String? action}) {
    if (ok) return data;
    throw EngineFailure(EngineFailureKind.fromWire(code),
        error ?? '服务端没说原因',
        action: action);
  }
}
