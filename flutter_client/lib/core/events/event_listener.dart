/// 事件驱动刷新的唯一一处（§4.3 的客户端半边）。
///
/// 各屏自己写 `ref.listen` 会飘成三种形状：有的忘了过滤事件名、有的把错误当数据、
/// 有的一收到事件就整屏 `AsyncLoading`（那是"首次加载"的形状，不是"刷新"的）。
/// 收成一个函数之后，"到了就 reload、断了就出声"这一条只有一份写法。
///
/// 为什么挂在 widget 上而不是 provider 上：provider 之间互相 `ref.listen` 会让
/// 一条 `SourceChangedEvent` 变成 N 条并发 `source.list`（书架 + 书源 + 找书都听它），
/// 而这三屏里只有当前那一屏需要立刻重画。widget 不在树上就不听，天然只有一发。
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../di/providers.dart';
import '../events/engine_event.dart';

/// [names] 里的事件到了就交 [onEvent]；流断了交 [onStreamError]。
void listenToEngineEvents(
  WidgetRef ref,
  List<String> names,
  void Function(EngineEvent event) onEvent, {
  void Function(Object error)? onStreamError,
}) {
  ref.listen<AsyncValue<EngineEvent>>(engineEventsProvider, (_, next) {
    final event = next.value;
    // 一条流上什么事件都在跑，**不匹配名字的必须一根不看**：
    // 检测那一屏要是也响应 SourceChangedEvent，就会在每源检测完时重发一次全库列表。
    if (event != null && names.contains(event.name)) onEvent(event);
    final error = next.error;
    if (error != null) onStreamError?.call(error);
  });
}
