/// 引擎→客户端的那一发反向渲染请求（设计 §8 R1）。
///
/// 为什么要单独一个类型：事件通道平时是**只读的**，只有 `request` 这一种帧是
/// 反着走的——引擎在 `<useweb>` / `url,{"webView":true}` 那一族源上取不到正文
/// （本仓库语料实测 56 条带 webView 选项、4 条带 `ruleContent.webJs`），
/// 就把"谁来渲染这一页"推给客户端，然后**在引擎那一侧等回答**
/// （`app/services/gateway/service.py:request_web_content`，预算 20s）。
/// 客户端不接，那一发就折成"宿主没答"，引擎退回非浏览器路径。
///
/// `request_id` 是这条等待的身份证：服务端按它挂 future，答错 id 等于回答了一条
/// 没人等的请求（回话是 `delivered:false`，不报错）。所以它必须是**原样带回去**的，
/// 不许重新生成、不许按 url 找。
library;

import '../rpc/envelope.dart';

/// `options` 里那一格 `javaScript` 的键名（服务端 `to_json(options)` 用的驼峰，
/// 与 `ruleContent.webJs` 那条分支的完成值同源）。
const String kWebviewJavaScriptOption = 'javaScript';

class WebviewRequest {
  const WebviewRequest({
    required this.requestId,
    required this.url,
    required this.sourceUrl,
    required this.options,
  });

  final String requestId;

  /// 要渲染的那一页的最终地址（选项已经被引擎从 url 上拆掉了）。
  final String url;

  /// 这一发是从哪条书源来的（`bookSourceUrl`）；宿主按域取 Cookie 时要它。
  final String sourceUrl;

  /// 引擎那一侧的取页选项（`webView`/`javaScript`/`method`/`charset`/`headers`…）。
  final Map<String, Object?> options;

  /// 这一发是不是真要求人渲染：`webView` 那一格。
  bool get wantsWebView => options['webView'] == true;

  /// 要给宿主跑的脚本（`@webjs:` 那条分支）。null 是"宿主没东西要跑"，
  /// 与"脚本跑了、返回了空"是两件事——后者要往回带 `jsResult`，前者不许带。
  String? get javaScript {
    final script = options[kWebviewJavaScriptOption];
    return script is String && script.isNotEmpty ? script : null;
  }

  @override
  String toString() =>
      'WebviewRequest($requestId, url=$url, webView=$wantsWebView, '
      'js=${javaScript == null ? 'none' : 'yes'})';
}

/// 把一条 `request` 帧解成 [WebviewRequest]；形状不对当场响。
///
/// 这里不"宽容地"给缺省值：`request_id` 缺了就没法答（服务端只会认成
/// "回答了一条没人等的请求"，而这一次渲染永远等不到人），那种失败表现为
/// 20s 后的一句"宿主没答"，排查的人会去查 WebView，不会查到帧上少了一个键。
WebviewRequest decodeWebviewRequest(Map<String, Object?> frame) {
  final requestId = frame['request_id'];
  if (requestId is! String || requestId.isEmpty) {
    throw EngineFailure(EngineFailureKind.transport,
        'request 帧没有可用的 request_id（键：${frame.keys.join(',')}）：'
        '答不了回去，这一次渲染只能等到引擎那侧的 20s 预算');
  }
  final payload = frame['payload'];
  if (payload is! Map) {
    throw EngineFailure(EngineFailureKind.transport,
        'request 帧的 payload 不是对象（收到 ${payload.runtimeType}）');
  }
  final typed = Map<String, Object?>.from(payload);
  final options = typed['options'];
  return WebviewRequest(
    requestId: requestId,
    url: '${typed['url'] ?? ''}',
    sourceUrl: '${typed['source_url'] ?? ''}',
    options: options is Map
        ? Map<String, Object?>.from(options)
        : throw EngineFailure(EngineFailureKind.transport,
            'request 帧的 options 不是对象（收到 ${options.runtimeType}）：'
            '宿主按它决定要不要渲染、要不要跑脚本，读不出来就只能什么都不做',
            action: 'webview.content'),
  );
}
