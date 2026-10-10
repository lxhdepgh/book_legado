/// 事件流的一帧（§4.3）：服务端 EventBus 上那 22 个 dataclass 各发一种。
///
/// `name` 就是 `app/core/events.py` 里的**类名**，`payload` 就是它的字段——
/// 这条同源关系是有钉的：`.probe/g2_golden_capture.py` 抓下来的样本里，
/// 两个 job 动作各自带一份 `event_sample`，事件名与 payload 的键都从真回话抄。
library;

/// 一条事件。`payload` 原样留着，不在这里挑字段——挑字段是各屏的活
/// （书架要 `items`、检测要 `state_class`），这层只负责"到了没、什么名字"。
class EngineEvent {
  const EngineEvent(this.name, this.payload);

  final String name;
  final Map<String, Object?> payload;

  Object? operator [](String key) => payload[key];

  /// 服务端每个事件都带 `timestamp`（`time.time()` 的浮点秒）。
  /// 它用来丢旧不回（同一 search_id 的晚到批次），不是用来显示时间。
  double get timestampAsDouble {
    final value = payload['timestamp'];
    return value is num ? value.toDouble() : 0;
  }

  @override
  String toString() => 'EngineEvent($name, ${payload.keys.length} 字段)';
}
