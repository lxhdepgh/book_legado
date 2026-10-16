/// 反向 WebView 通道在客户端这一侧的接缝（设计 §8 R1 / §9 门 G3 第二条判据）。
///
/// 链条是这样的：引擎读到 `<useweb>` 或 `url,{"webView":true}` 那一族源，先在自己这边
/// 试无头内核（`BrowserService._silent_render`）；那一页**看着像要人**（登录墙、验证码、
/// `block_pages.looks_like_gate` 那几条）时，内核那一版就不交了，改问远端宿主
/// ——引擎发一条 `request` 帧，等 20s。宿主的答案走 `engine.webContentReady`
/// 回去。（P7 拆掉网关后这条通道两侧都没有落点，见
/// `test/webview_reverse_channel_live_arm_test.dart` 钉的边界。）
///
/// **这一轮产品侧接的不是可见 WebView，是接缝**：G4 要挑 Windows 的 WebView 插件
/// （`flutter_inappwebview` 无 Windows 桌面、`webview_windows` 已停更，见开发日志那份
/// 决策清单），挑之前把"帧怎么解、答案怎么交、答不上来怎么办"钉死，插件来了只是
/// 换 [WebviewHost] 的实现。[ScriptedWebviewHost] 就是那条"按脚本回答"的实现，
/// 实弹臂用它演可见 WebView 那一侧。
library;

import 'dart:async';

import '../events/webview_request.dart';
import '../rpc/engine_repository.dart';
import '../rpc/envelope.dart';

/// 宿主渲染完要交回的那一格。
class WebviewRender {
  const WebviewRender({
    required this.body,
    this.error = '',
    this.url = '',
    this.status = 200,
    this.cookie = '',
    this.headers,
    this.jsResult,
  });

  /// 渲染失败：`error` 非空，服务端据此把这一次折成异常交回引擎
  /// （引擎再退回非浏览器路径），而不是当成"正文是空串的成功"。
  const WebviewRender.failed(this.error)
      : body = '',
        url = '',
        status = 200,
        cookie = '',
        headers = null,
        jsResult = null;

  /// 渲染完的文档（真 WebView 取的是 `document.documentElement.outerHTML`）。
  final String body;

  /// 非空 = 这一次渲染失败的原因。
  final String error;

  /// 最终地址（可能被重定向过）；空串表示没变化。
  final String url;

  final int status;

  /// 渲染后该域的 Cookie，引擎回填进自己的 cookie 罐。
  final String cookie;

  final Map<String, Object?>? headers;

  /// `@webjs:` 那条分支的脚本完成值。**null 与空串不是一件事**：
  /// null 是"宿主没跑脚本"（不发这个键），空串是"跑了，返回了空"。
  final Object? jsResult;
}

/// 谁来渲染这一页。
abstract class WebviewHost {
  const WebviewHost();

  /// 回 null = **这一发不作答**：让引擎在自己的那 20s 预算上等个空。
  ///
  /// 这是一条真实分支，不是偷懒的缺省：没有可见 WebView 的客户端今天就是这个行为，
  /// 而实弹臂要靠它演"宿主在场但不说话"那一臂（那一臂量的是错误归属——
  /// 报错该指着"宿主 20s 没答"，不该指着"reader.content 超过服务端预算 60s"）。
  FutureOr<WebviewRender?> render(WebviewRequest request);
}

/// 按脚本回答的宿主：测试与实弹臂用它，产品侧 G4 换成真插件。
///
/// [bodyFor] 回 null 或抛错都算这一发没答上：前者是"不作答"（上面那条分支），
/// 后者交回 `error`（引擎折成异常）。两条分开是因为它们的读数不同——前者引擎等满预算，
/// 后者引擎当场就退回非浏览器路径。
class ScriptedWebviewHost implements WebviewHost {
  /// 不写 `const`：[calls] 那本账是可变列表，而这一发测的就是"叫到了哪几条"。
  ScriptedWebviewHost(this.bodyFor, {this.delay = Duration.zero});

  /// 请求 → 渲染完的正文；null = 不作答。
  final FutureOr<String?> Function(WebviewRequest request) bodyFor;

  /// 让"宿主还在渲染"那一段可测（真 WebView 是秒级，不是零）。
  final Duration delay;

  /// 被叫到的请求，按先后。[calls] 只数得出次数，答不出"哪一条 url"——
  /// 而预取那一臂要的正是一串 url（记忆 `aggregated-counters-hide-which-call`）。
  final List<WebviewRequest> calls = <WebviewRequest>[];

  @override
  FutureOr<WebviewRender?> render(WebviewRequest request) async {
    calls.add(request);
    if (delay > Duration.zero) await Future<void>.delayed(delay);
    final body = await bodyFor(request);
    return body == null ? null : WebviewRender(body: body, url: request.url);
  }
}

/// 把一条 [WebviewRequest] 流接到仓库上：帧来了就渲染，渲染完就交回去。
///
/// 三条不是风格的取舍：
///
/// 1. **一次渲染一个独立的活，不许串行**。预取一章以上时引擎会连着求好几次，
///    而每一发各等自己的 20s；在监听回调里 await 会把后面那些帧压着
///    （[ScriptedWebviewHost] 那 `delay` 一大就演得出这个形状）。
/// 2. **宿主炸了不许带走这根流**。渲染抛错折成 `error` 交回去，交回本身抛错只出声
///    （[onError]）——事件流与这条口同在一个引擎上，订阅一断，界面上"结果不再往上涨"
///    就再也分不清是没结果还是没声音。
/// 3. **`delivered:false` 不是失败**（引擎已经走人了），所以它只走 [onError] 那条
///    说明，不折成异常。
///
/// 返回的订阅由调用方收（谁 attach 谁 cancel）；产品侧目前**不 attach**——
/// 没有可见 WebView 时接上只会把每一条 `<useweb>` 请求变成一次 20s 空等，
/// 而当场拒了让引擎走 HTTP 是更好的那一版（`service.py` 那句"没人连就别等"）。
StreamSubscription<WebviewRequest> attachWebviewHost({
  required Stream<WebviewRequest> requests,
  required EngineRepository repository,
  required WebviewHost host,
  void Function(Object error, WebviewRequest request)? onError,
}) {
  return requests.listen((request) {
    unawaited(_answer(request, host, repository, onError));
  });
}

Future<void> _answer(
  WebviewRequest request,
  WebviewHost host,
  EngineRepository repository,
  void Function(Object error, WebviewRequest request)? onError,
) async {
  final render = await _render(request, host, onError);
  if (render == null) return;
  try {
    final delivery = await repository.webContentReady(
      requestId: request.requestId,
      body: render.body,
      headers: render.headers,
      status: render.status,
      url: render.url,
      cookie: render.cookie,
      error: render.error,
      jsResult: render.jsResult,
    );
    if (!delivery.delivered) {
      onError?.call(
          EngineFailure(EngineFailureKind.transport,
              '引擎已经不等这一次渲染了（request_id=${request.requestId}）：'
              '宿主答得比 20s 预算晚，正文没人收',
              action: 'engine.webContentReady'),
          request);
    }
  } on Object catch (exc) {
    onError?.call(exc, request);
  }
}

Future<WebviewRender?> _render(
  WebviewRequest request,
  WebviewHost host,
  void Function(Object error, WebviewRequest request)? onError,
) async {
  try {
    final render = await host.render(request);
    if (render == null) return null;
    // 不作答与"不作答是因为这一发压根不该由我答"要分得开：后者是宿主读错了帧。
    if (render.body.isEmpty && render.error.isEmpty) {
      onError?.call(
          EngineFailure(EngineFailureKind.transport,
              '宿主交回一份空正文又没说为什么（request_id=${request.requestId}）：'
              '引擎会把它当成功，正文那一章就成了 0 段',
              action: 'engine.webContentReady'),
          request);
    }
    return render;
  } on Object catch (exc) {
    // 渲染失败要**交回 error**，让引擎当场退回非浏览器路径；
    // 只不出声地不答，等于把一次确定的失败拖成 20s 的空等。
    return WebviewRender.failed('宿主渲染失败：$exc');
  }
}
