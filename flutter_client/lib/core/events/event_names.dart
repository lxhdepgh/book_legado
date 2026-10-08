/// 事件名的唯一一份写法（§4.3：事件名 = `app/core/events.py` 那些 dataclass 的类名）。
///
/// 为什么单独一个文件而不是各屏手写字符串：名字打错的后果不是红屏，是**那一屏永远不出结果**
/// （监听器在等 `SearchFinshedEvent`，流里来的是 `SearchFinishedEvent`）。
/// `tests/test_gateway_contract_single_source.py` 拿 Python 侧 `event_types()`
/// 的类名逐个比对本文件的字面量，多一格、少一格、拼错都是红。
///
/// 这里只列**G2 那四个屏真正在听的**那几条。其余事件（`ImportProgressEvent`、
/// `ReadAloudStateEvent` 那一族）等到有屏听它们的那一天再登记——
/// 提前列全表会得到一份没人用的名单，而它的漂移比它的价值高。
library;

abstract final class EngineEventNames {
  /// 多源找书的一批结果（`items` + `search_id` + `finished`）。
  static const String searchFinished = 'SearchFinishedEvent';

  /// 一条源检测完了（`source_id` + `state_class` + `summary` + `done`/`total`）。
  static const String checkFinished = 'CheckFinishedEvent';

  /// 书源库变了（增删改启停都发这一条）。
  static const String sourceChanged = 'SourceChangedEvent';

  /// 某个服务的状态迁移（仪表盘那一行读它）。
  static const String serviceStateChanged = 'ServiceStateChangedEvent';
}
