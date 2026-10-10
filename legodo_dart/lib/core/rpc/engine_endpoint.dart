/// 网关握手行里那四个连接参数（`main.py --serve` 打的那行 `LEGODO_GATEWAY {json}`）。
///
/// 令牌只活这一次进程（见 `GatewayService.token` 的注释：不落库、不读设置），
/// 所以它**不许**出现在命令行、配置文件或日志里——本类的 [toString] 打了掩码，
/// 端口和 pid 留着，排查"连的是哪个引擎"靠那两个就够。
library;

class EngineEndpoint {
  const EngineEndpoint({
    required this.host,
    required this.port,
    required this.token,
    required this.protocolVersion,
    required this.build,
    required this.pid,
  });

  final String host;
  final int port;
  final String token;
  final int protocolVersion;
  final String build;
  final int pid;

  /// `scheme` 必须写死 `http`：`Uri(host:…)` 不带 scheme 时 `package:http` 直接拒收，
  /// 而"点了没反应"就是那一发的表现。回环上明文，令牌只在这台机器上活一次（§4.3）。
  Uri get rpcUri => Uri(scheme: 'http', host: host, port: port, path: '/rpc');
  Uri get wsUri =>
      Uri(scheme: 'ws', host: host, port: port, path: '/ws');

  /// 一条二进制出口的地址（§4.4：`GET /media/{key}`）。
  ///
  /// 令牌走 query 而不是 header，是因为这条地址最终交出去的是别人的控件
  /// （`Image.network`/`media_kit`）——它们各自能带的东西不一样，URL 本身是唯一
  /// 两边都认的把手。**因此这一串的每一个字符都是可渲染的凭据**：不许打进日志、
  /// 不许进 widget key、不许当"连的是哪儿"那一句读数的素材（[toString] 打了掩码，
  /// 这条方法的返回值可没有）。key 是服务端发下来的（`ContentBlock.mediaKey`），
  /// 这里只贴，不解释、不拼摘要。
  Uri mediaUri(String key) => Uri(
        scheme: 'http',
        host: host,
        port: port,
        path: '/media/$key',
        queryParameters: <String, String>{'token': token},
      );

  factory EngineEndpoint.fromHandshake(Map<String, Object?> json) {
    int asInt(Object? value) => switch (value) {
          final int n => n,
          final String s => int.tryParse(s) ?? 0,
          _ => 0,
        };
    return EngineEndpoint(
      host: '${json['host'] ?? '127.0.0.1'}',
      port: asInt(json['port']),
      token: '${json['token'] ?? ''}',
      protocolVersion: asInt(json['protocol_version']),
      build: '${json['build'] ?? ''}',
      pid: asInt(json['pid']),
    );
  }

  @override
  String toString() =>
      'EngineEndpoint($host:$port build=$build pid=$pid token=***)';
}
