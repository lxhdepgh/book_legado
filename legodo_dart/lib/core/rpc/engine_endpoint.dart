/// 媒体（二进制）出口的地址形状（§4.4：`GET /media/{key}`）。
///
/// FFI 单进程下没有 HTTP 网关：`engineEndpointProvider` 恒回 null，[mediaUri]
/// 也就恒拿不到值——读者屏那一格退回 `url` 格是既定行为。类留着是因为
/// "地址由服务端算、客户端只贴不拼"的形状还要一个落点（第八十九轮那条
/// 「要么上钉常量、要么上钉服务端」）。
///
/// 令牌只活这一次进程，所以它**不许**出现在命令行、配置文件或日志里——
/// 本类的 [toString] 打了掩码，端口和 build 留着，排查"连的是哪个引擎"靠那两个就够。
library;

class EngineEndpoint {
  const EngineEndpoint({
    required this.host,
    required this.port,
    required this.token,
    required this.build,
    required this.pid,
  });

  final String host;
  final int port;
  final String token;
  final String build;
  final int pid;

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

  @override
  String toString() =>
      'EngineEndpoint($host:$port build=$build pid=$pid token=***)';
}
