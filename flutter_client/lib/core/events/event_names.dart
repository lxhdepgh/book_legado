/// 事件名的唯一一份写法（§4.3：事件名 = `app/core/events.py` 那些 dataclass 的类名）。
///
/// 为什么单独一个文件而不是各屏手写字符串：名字打错的后果不是红屏，是**那一屏永远不出结果**
/// （监听器在等 `SearchFinshedEvent`，流里来的是 `SearchFinishedEvent`）。
/// `tests/test_gateway_contract_single_source.py` 拿 Python 侧 `event_types()`
/// 的类名逐个比对本文件的字面量，多一格、少一格、拼错都是红。
///
/// 这里只列**有屏在听的那几条**（找书 / 检测 / 换源 / 订阅 / 书源库变更 / 服务状态）。
/// 其余事件（`ImportProgressEvent`、`ReadAloudStateEvent` 那一族）等到有屏听它们的那一天
/// 再登记——提前列全表会得到一份没人用的名单，而它的漂移比它的价值高。
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

  /// 换源扫描的进度（`switch_id` + `done`/`total` + `failures`/`starved`）。
  /// 它和下一条挂的是同一轮：`switch_id` 认不出自己那轮的帧必须整帧丢掉，
  /// 否则上一轮"扫完 3 个"会把这一轮的"扫完 1 个"盖回去。
  static const String sourceSwitchProgress = 'SourceSwitchProgressEvent';

  /// 换源扫描收尾（`switch_id` + `candidates` + `cancelled` + `error`）。
  /// 候选名单**只在这条里**，进度帧里没有——所以那五格里只有这一条带数据。
  static const String sourceSwitchFinished = 'SourceSwitchFinishedEvent';

  /// 全源刷新的进度（`refresh_id` + `done`/`total` + `current` + `failures`）。
  /// `failures` 只在收尾那一帧读（服务端中途每帧也带，但那是"截至此刻"，
  /// 画在界面上会是一条锯齿状失败数）。
  static const String rssRefreshProgress = 'RssRefreshProgressEvent';

  /// 一条订阅源刷完了（`source_id` + `new_count`）。
  /// 一条一轮里会来 N 条（N=本轮源数），不收尾——认本轮靠 [rssRefreshProgress] 那一发
  /// 的进度帧收尾，这一条只用来数"新增了几篇"。
  static const String rssRefreshed = 'RssRefreshedEvent';
}
