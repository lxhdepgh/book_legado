/// 数据层的唯一契约（设计 §6.3）：抽象仓库 + 两个实现。
///
/// 默认体一律抛 [EngineFailure]（kind=unavailable）而不是留 `abstract`——这一手
/// 同时解决两件事：新加的 action 还没接上时**编译不报错、运行时有声音**，
/// 以及"这个平台/这个引擎版本没有这项能力"的表达方式（真果鉴用它表 iOS 无后台下载）。
///
/// 这里 import 了几个 feature 的实体（`BookshelfItem`/`SourceRecord`/…）：契约面在 core、
/// 实体在 feature，是 §6.1 那个 feature-first 目录换来的一条代价——
/// 反过来让 core 只认 `Map` 就是今天 RPC 出口那个形状，两侧都得自己解释一遍字段。
///
/// 方法表 = `app/gateway/methods.py` 那张表去掉 `engine.webContentReady`
/// （那一条是服务端**反向**调用客户端，方向不在仓库这一侧；G3 的 WebView 宿主才接）。
/// 这条对齐由 `tests/test_gateway_contract_single_source.py` 从 Python 侧扫 Dart 字面量钉住。
library;

import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
import '../../features/settings/data/theme_preferences.dart';
import '../../features/sources/data/import_outcome.dart';
import '../../features/sources/data/source_audit.dart';
import '../../features/sources/data/source_record.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';
import '../events/engine_event.dart';
import '../settings/settings_map.dart';
import 'engine_status.dart';
import 'envelope.dart';

/// `engine.hello` 的返回（握手）。
class GatewayInfo {
  const GatewayInfo({
    required this.protocolVersion,
    required this.build,
    required this.capabilities,
    required this.schemaDigest,
    required this.actions,
    required this.pid,
  });

  final int protocolVersion;
  final String build;
  final List<String> capabilities;
  final String schemaDigest;
  final List<String> actions;
  final int pid;

  factory GatewayInfo.fromJson(Map<String, Object?> json) => GatewayInfo(
        protocolVersion: (json['protocol_version'] as num?)?.toInt() ?? 0,
        build: '${json['build'] ?? ''}',
        capabilities: (json['capabilities'] as List?)?.map((e) => '$e').toList() ?? const [],
        schemaDigest: '${json['schema_digest'] ?? ''}',
        actions: (json['actions'] as List?)?.map((e) => '$e').toList() ?? const [],
        pid: (json['pid'] as num?)?.toInt() ?? 0,
      );
}

/// 两个 job 动作的受理凭据（`{"started":true,"action":…}`）。
///
/// **它不是结果**。`mode="job"` 的动作回这个就收摊，真结果一根不落在 RPC 上，
/// 只走 `/ws` 的事件流（`search.start` → `SearchFinishedEvent`，
/// `check.start` → `CheckFinishedEvent`）。拿它当"任务成功了"是这一发最容易读错的地方：
/// `started` 只说服务端肯起，起了之后一条源都检测不出来也照样回 `started:true`。
class JobAccepted {
  const JobAccepted({required this.started, required this.action});

  final bool started;
  final String action;

  factory JobAccepted.fromJson(Map<String, Object?> json) => JobAccepted(
        started: json['started'] == true,
        action: '${json['action'] ?? ''}',
      );
}

/// 规矩：**失败以 Future 的错误交付，不同步抛**。
///
/// 这条不是风格问题。`RpcEngineRepository` 的失败一定在 Future 上（网络那一发本来就异步），
/// 而默认体若是 `=> throw`，调用方就会在同一句上分别撞上"同步炸"和"异步炸"两种形状——
/// `AsyncNotifier.build()` 只接得住后一种，前者会打穿界面（表现为整屏红屏而不是错误页）。
abstract class EngineRepository {
  const EngineRepository();

  // ------------------------------------------------------------------ #
  // engine.*
  // ------------------------------------------------------------------ #
  Future<GatewayInfo> hello() async => throw _missing('engine.hello');

  Future<List<ServiceStatus>> serviceStatus() async =>
      throw _missing('engine.status');

  /// 客户端主动收摊（§4.6）：置服务端退出标志，父进程随即走 `hub.shutdown()`。
  Future<void> shutdown({String reason = 'flutter client'}) async =>
      throw _missing('engine.shutdown');

  /// 事件流（§4.3）。默认体给一条**带错误的流**而不是空流：
  /// 空流的意思是"这条引擎一根事件都不发"，而真实情况是"这根流没接上"——
  /// 两者在界面上都表现为"结果不再往上涨"，只有出不出声分得开。
  ///
  /// 用 `Stream.error` 而不是直接 `throw`：这一条是同步返回的接口，
  /// 同步抛会打穿 `build()`（本文件头部那条规矩对 Stream 同样成立，只是交付形状不同）。
  Stream<EngineEvent> events() => Stream<EngineEvent>.error(EngineFailure(
        EngineFailureKind.unavailable,
        '当前环境没有事件流：这根 /ws 没接上，界面上"结果不再往上涨"分不清是没结果还是没声音',
      ));

  // ------------------------------------------------------------------ #
  // bookshelf.*
  // ------------------------------------------------------------------ #
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) async =>
      throw _missing('bookshelf.list');

  /// 把搜索结果那一行放进书架。[book] 是**服务端要的字典快照**
  /// （`bookshelf.add` 的 `book` 参数是 object；模型对象不许进协议面）。
  ///
  /// [shelved]=false 就是移出书架（`notShelf` 那一格，与 `bookshelf.list` 默认过滤同源）。
  Future<bool> shelveBook({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelved = true,
  }) async =>
      throw _missing('bookshelf.add');

  // ------------------------------------------------------------------ #
  // source.* / import.*
  // ------------------------------------------------------------------ #
  Future<List<SourceRecord>> listSources() async => throw _missing('source.list');

  /// 单条书源全文（编辑器打开一条要读回它的全部规则字段）。
  /// 服务端没这条源时回 `null`——**那是"没有"，不是"空的"**，所以返回可空。
  Future<SourceRecord?> loadSource(String url) async => throw _missing('source.get');

  Future<SaveOutcome> saveSource(Map<String, Object?> sourceJson) async =>
      throw _missing('source.save');

  Future<bool> deleteSource(String url) async => throw _missing('source.delete');

  Future<bool> setSourceEnabled(String url, bool enabled) async =>
      throw _missing('source.enable');

  Future<bool> setSourceGroup(String url, String group) async =>
      throw _missing('source.group');

  /// 批量启停：回**改动的条数**，不是"是否成功"（action 表的 summary 就这么写的）。
  Future<int> batchSetEnabled(List<String> urls, bool enabled) async =>
      throw _missing('source.batch_enable');

  Future<int> batchDelete(List<String> urls) async =>
      throw _missing('source.batch_delete');

  /// 全部书源的 JSON **文本**（与 flet 那份导出口径一字不差）。
  Future<String> exportSources() async => throw _missing('source.export');

  Future<AuditReport> auditSource(Map<String, Object?> sourceJson) async =>
      throw _missing('source.audit');

  /// 本地文本导入。`kind` 不是 `book_source` 时服务端只登记说明、不落数据
  /// （回话里 `placeholder:true` 说的就是这件事，见 [ImportOutcome]）。
  Future<ImportOutcome> importText({
    required String text,
    required String kind,
    String mode = 'append',
  }) async =>
      throw _missing('import.text');

  Future<ImportOutcome> importUrl({
    required String url,
    required String kind,
    String mode = 'append',
  }) async =>
      throw _missing('import.url');

  // ------------------------------------------------------------------ #
  // browse.* / search.*（找书）
  // ------------------------------------------------------------------ #
  Future<List<CategoryRow>> browseCategories(String sourceUrl) async =>
      throw _missing('browse.categories');

  /// 分类下一页。[category] 必须是 [browseCategories] 回的那一条**原样**传回
  /// （`CategoryRow.raw`）——自己拼 `{title,url}` 会在服务端给分类加字段时静默丢东西。
  Future<List<BookRow>> browseBooks({
    required String sourceUrl,
    required Map<String, Object?> category,
    int page = 1,
  }) async =>
      throw _missing('browse.books');

  /// 起一轮多源找书（`mode="job"`）：这一发只回受理凭据，结果走事件流。
  ///
  /// [searchId] 同值再调即"翻页"（服务端按 id 续同一轮）；不给就用当前时间戳，
  /// 于是每次点搜索都是一轮新的。
  Future<JobAccepted> searchStart({
    required String keyword,
    String scope = '',
    int? searchId,
  }) async =>
      throw _missing('search.start');

  Future<void> searchStop() async => throw _missing('search.stop');

  /// 搜索范围表（全部 / 未分组 / 各分组）与各自的可搜源数。
  ///
  /// 顺序就是下拉顺序，由服务端排好（`ReadingService.scope_table`）：客户端不重排、
  /// 不自己数——那等于把"哪些源算可搜"这条规则再抄一遍。
  Future<List<SearchScope>> searchScopes() async =>
      throw _missing('search.scopes');

  // ------------------------------------------------------------------ #
  // check.*
  // ------------------------------------------------------------------ #
  /// 起一轮批量检测（`mode="job"`）。[groups] 为 null = 全库。
  Future<JobAccepted> checkStart({List<String>? groups}) async =>
      throw _missing('check.start');

  /// 当前/最近一轮的回放快照：断线重连、切页回来都读它（进度不许只活在事件流里）。
  Future<CheckSnapshot> checkSnapshot() async => throw _missing('check.snapshot');

  Future<void> checkCancel() async => throw _missing('check.cancel');

  /// 只检这一条源（编辑器问"这条源能不能用"）。这一发**会真的发网络请求**。
  Future<CheckSourceReport> checkSource({
    required Map<String, Object?> sourceJson,
    String depth = 'list',
  }) async =>
      throw _missing('check.source');

  // ------------------------------------------------------------------ #
  // settings.* / theme.*
  // ------------------------------------------------------------------ #
  /// 按名单读设置。没设过的键**不出现在返回里**（与"设成 null"是两件事，见 [SettingsMap]）。
  Future<SettingsMap> readSettings(List<String> keys) async =>
      throw _missing('settings.read');

  Future<void> writeSetting(String key, Object? value) async =>
      throw _missing('settings.write');

  Future<ThemePreferences> themePreferences() async =>
      throw _missing('theme.preferences');

  /// 合并更新外观偏好：null 的那一格保持原值（服务端 `theme.save` 的语义）。
  Future<ThemePreferences> saveTheme({
    String? skinId,
    String? mode,
    bool? eink,
  }) async =>
      throw _missing('theme.save');

  EngineFailure _missing(String action) => EngineFailure(
        EngineFailureKind.unavailable,
        '当前环境不支持 $action',
        action: action,
      );
}

/// 一个方法都不覆写的那一发：没人 override 仓库时，界面拿到的就是它。
///
/// 为什么不直接让 provider 的体抛？因为 Riverpod 会把"provider 自己在错误状态"
/// 包成一条 74,654 字符的 `ProviderException` 塞进 `AsyncError.error`，
/// 于是错误屏上那一段详情就是一整屏的框架堆栈——用户要的那一句
/// ("当前环境不支持 bookshelf.list")反而看不见。挂在这里，失败按**每一次调用**
/// 交付，kind 与文案都还是我们自己那句话。
class UnwiredEngineRepository extends EngineRepository {
  const UnwiredEngineRepository();
}
