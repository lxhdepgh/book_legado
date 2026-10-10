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
/// 方法表 = `app/gateway/methods.py` 那张表，**一条不漏**（58 发，含
/// `engine.webContentReady`——那一条方向是反的：引擎在 `<useweb>` 那一族源上求客户端
/// 渲染一次，客户端把结果从 /rpc 交回去；接它的是 `core/webview/webview_host.dart`，
/// 不是某一屏的按钮）。
/// 这条对齐由 `tests/test_gateway_contract_single_source.py` 从 Python 侧扫 Dart 字面量钉住。
library;

import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
import '../../features/reader/data/bookmark_row.dart';
import '../../features/reader/data/chapter_document.dart';
import '../../features/reader/data/chapter_row.dart';
import '../../features/reader/data/content_search_result.dart';
import '../../features/reader/data/reading_position.dart';
import '../../features/reader/data/reader_session.dart';
import '../../features/reader/data/staged_record.dart';
import '../../features/rss/data/rss_models.dart';
import '../../features/settings/data/theme_preferences.dart';
import '../../features/sources/data/import_outcome.dart';
import '../../features/sources/data/source_audit.dart';
import '../../features/sources/data/source_record.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';
import '../../features/bookshelf/data/source_switch_models.dart';
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

/// `mode="job"` 那四发（`check.start` / `search.start` / `switch.scan` /
/// `rss.refresh_all`）的受理凭据（`{"started":true,"action":…}`）。
///
/// **它不是结果**。`mode="job"` 的动作回这个就收摊，真结果一根不落在 RPC 上，
/// 只走 `/ws` 的事件流（`search.start` → `SearchFinishedEvent`，
/// `check.start` → `CheckFinishedEvent`，`switch.scan` → 那两条 `SourceSwitch*`，
/// `rss.refresh_all` → `RssRefreshProgressEvent` + `RssRefreshedEvent`）。
/// 拿它当"任务成功了"是这一发最容易读错的地方：
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

/// 一次反向渲染答复的**收件回执**（`{"delivered":true|false}`）。
///
/// `delivered:false` 不是失败，也不许当失败处理：它是"引擎已经按 20s 预算走人了，
/// 这一份没人要了"（服务端那句日志是"来晚了或没人等"）。客户端把它折成错误，
/// 用户就会在已经退回纯 HTTP 的那一发上看到第二条不相干的错。
///
/// 反过来，`delivered:true` 也只说"解开了那次等待"，不说正文好不好——渲染失败
/// （`error` 非空）同样能 delivered:true，因为"失败"这件事已经作为异常交给引擎了。
class WebviewDelivery {
  const WebviewDelivery({required this.delivered});

  final bool delivered;

  factory WebviewDelivery.fromJson(Map<String, Object?> json) =>
      WebviewDelivery(delivered: json['delivered'] == true);

  @override
  String toString() => 'WebviewDelivery(delivered=$delivered)';
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

  /// 把一次反向渲染的结果交回去（§8 R1，设计里 `<useweb>` 那一族源的唯一出路）。
  ///
  /// 八个参数与服务端那一发逐格对齐（`service.py:web_content_ready`），**包括两个
  /// 不许混的缺省**：
  /// - `jsResult` 不给 = 宿主没跑脚本；给空串 = 脚本跑了、返回了空。服务端只在非 null
  ///   时把这个键塞进返回字典，所以这里也不许把 null 折成 `''` 发出去。
  /// - `error` 非空时服务端把这一次**折成异常**交回引擎（引擎据此退回非浏览器路径），
  ///   而不是当成"正文是空串"的成功。
  ///
  /// 回话里的 `delivered` 说的是"引擎还要不要这一份"：它按 20s 预算走人之后的答复
  /// 是 `false`——那不是失败（见 [WebviewDelivery]）。
  Future<WebviewDelivery> webContentReady({
    required String requestId,
    String body = '',
    Map<String, Object?>? headers,
    int status = 200,
    String url = '',
    String cookie = '',
    String error = '',
    Object? jsResult,
  }) async =>
      throw _missing('engine.webContentReady');

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
  // switch.*（换源：先本地种子，再联网扫描，扫描是 job）
  // ------------------------------------------------------------------ #
  // 这一族与 check.* 是同一个形状：受理凭据里没有结果，逐源进度与整份候选走事件流，
  // 断线/切页期间丢掉的那些由 `switch.snapshot` 补回来。关联靠客户端生成的
  // `switch_id`（服务端原样回带，`search.start` 的 `search_id` 同一套办法）。
  // 排队规则（谁进扫描队列、谁先扫）在**服务端**（`order_switch_sources`）：
  // 客户端不排、不筛，也不自己判"这条源能不能搜"。

  /// 换源对话框打开时先读的**本地种子**：之前搜索落库的同名书，不联网、秒开。
  ///
  /// 回的是裸书行（一行本就来自一条源），所以是 [BookRow] 而不是候选包装。
  /// [book] 传**书架那一行原样那份字典**（服务端只读 name/author 去取同名书）。
  Future<List<BookRow>> switchSeeds({
    required Map<String, Object?> book,
  }) async =>
      throw _missing('switch.seeds');

  /// 起一轮换源联网扫描（`mode="job"`）：这一发只回受理凭据。
  ///
  /// [switchId] 由客户端生成、本轮事件原样回带——**不许省**：`_start_job` 的回包里
  /// 没有任务号，晚到的进度与收尾只能靠它认本轮（服务端把它折成字符串再回带）。
  /// [group] 空串 = 全部启用源；**分组不存在时不回退到全部**（那是转圈而不是答案）。
  /// 已经在跑的一轮会被**合作式叫停**再由这一轮接手，已扫出的候选不扔。
  Future<JobAccepted> switchScan({
    required Map<String, Object?> book,
    required int switchId,
    String keyword = '',
    String group = '',
  }) async =>
      throw _missing('switch.scan');

  /// 当前/最近一轮的回放快照（进度不许只活在事件流里）。
  Future<SwitchRun> switchSnapshot() async => throw _missing('switch.snapshot');

  /// 叫停在跑的那一轮，回的是**这一发有没有真叫停下什么**（`cancelled:false` =
  /// 没有轮次在跑，界面别写"已停止"）。它不是"请求受理了没有"。
  Future<bool> switchCancel() async => throw _missing('switch.cancel');

  /// 选中那条候选写回书架。
  ///
  /// 三个参数各管一件事：[record] 是**书架那一行**（服务端按它的 url/source_url 找回
  /// 记录，找不到就 `applied:false`），[sourceUrl] 是候选那一条源的 url（服务端按它
  /// 取回书源对象——取不到时这一发**报错**，不回 false），
  /// [book] 是候选那一行的书快照（`url` 是新源的那一条）。
  Future<SwitchApplyResult> applySwitch({
    required Map<String, Object?> record,
    required String sourceUrl,
    required Map<String, Object?> book,
  }) async =>
      throw _missing('switch.apply');

  // ------------------------------------------------------------------ #
  // rss.*（订阅面：源 → 文章 → 正文，加两发写回与一发 job）
  // ------------------------------------------------------------------ #
  // 这一族有两条别处没有的形状：
  // 1. **文章列表不带正文**。`rss.articles` 那 9 格里既没有 `content` 也没有 `text`——
  //    feed 的 `description` 可以就是整篇文章，而中栏一根都不显示它。正文只在打开那一条时
  //    由 `rss.article` 单取，回的还是**已去标签**的 `text`（那把尺在服务侧）。
  //    Dart 这边任何时候都不该再剥一次 HTML。
  // 2. **源 id 就是 feed 地址**（`add_source` 回它、删除/刷新都吃它），而文章 id 是自增
  //    整数——同一族里两种主键，混了就删不掉东西。

  /// 订阅列表那一屏：源字段 + 篇数/未读两格角标（服务端 join 好的，客户端不自己数）。
  Future<List<RssSourceRow>> listRssSources() async =>
      throw _missing('rss.sources');

  /// 某源的文章行。[unreadOnly] 折的是服务侧那个 `read_flag`，与左栏角标同源。
  Future<List<RssArticleRow>> listRssArticles({
    required String sourceId,
    bool unreadOnly = false,
  }) async =>
      throw _missing('rss.articles');

  /// 打开一篇文章：**只有这一发给正文**，回的是去掉标签的 `text`。
  ///
  /// 文章没了回 `null`——那是"没有"，不是"正文是空串"，所以返回可空
  /// （与 `source.get`、`reader.position_get` 同一族；把它折成空对象就是让界面
  /// 在一条已删的文章下面画一个空白框）。
  Future<RssArticleDetail?> openRssArticle(int articleId) async =>
      throw _missing('rss.article');

  /// 新增订阅源，回它的 id（= 规范化后的 feed 地址）。同一地址重复添加是**更新**，
  /// 不是多出一行。
  Future<String> addRssSource({
    required String name,
    required String url,
    String sourceGroup = '',
  }) async =>
      throw _missing('rss.add_source');

  /// 删除订阅源**连同它的文章**（含已读与收藏标记），回是否真删到行。
  ///
  /// 界面必须先二次确认：这一发不可撤销，而 flet 那一屏把这句话写在确认框里。
  Future<bool> removeRssSource(String sourceId) async =>
      throw _missing('rss.remove_source');

  /// 抓一个源，回本次**新增**的篇数（不是库里总篇数）。
  ///
  /// 这一发**会真的发网络请求**，而且失败是**错误信封**不是静默 0：抓取失败回
  /// `ok:false`，回 `0` 说的真是"抓开了、没有新文章"。把两者折成一格，
  /// 用户就在"这个 feed 是空的"与"这台机器取不到它"之间分不清。
  Future<int> refreshRssSource(String sourceId) async =>
      throw _missing('rss.refresh');

  /// 起一轮全源刷新（`mode="job"`）：受理回包只有 `{started, action}`。
  ///
  /// [refreshId] 由客户端生成、每帧原样回带（与 `switch_id`/`search_id` 同一套办法）。
  /// 逐源进度走 `RssRefreshProgressEvent`、每源结果走 `RssRefreshedEvent`；
  /// 单源失败不拖垮其余，**收尾那一帧**带 `failures`。
  Future<JobAccepted> refreshAllRssSources({required int refreshId}) async =>
      throw _missing('rss.refresh_all');

  /// 标记已读（点开正文才算读过的那一发），回是否真改到行。
  Future<bool> markRssArticleRead(int articleId) async =>
      throw _missing('rss.mark_read');

  /// 收藏/取消收藏，回是否真改到行（文章可能已被另一侧删了 → `false`，
  /// 那是"这条不在了"，不是"收藏失败"）。
  Future<bool> starRssArticle({
    required int articleId,
    required bool starred,
  }) async =>
      throw _missing('rss.star');

  // ------------------------------------------------------------------ #
  // reader.*（阅读会话）
  // ------------------------------------------------------------------ #
  // 这一族每一条都**显式吃 source_url + book_url**：会话是引擎里的单例，而跨进程
  // 客户端可以带着任意 url 来。对不上当前会话的那一发回错误信封
  // （`reader.toc--no-session` 那份样本）或 `closed:false`，不靠隐式状态猜。
  //
  // [openReader] 的 [book] 是**字典快照**（`BookRow.raw`）：模型对象不许进协议面。
  Future<ReaderSession> openReader({
    required String sourceUrl,
    required Map<String, Object?> book,
    bool shelve = true,
    bool resume = true,
  }) async =>
      throw _missing('reader.open');

  Future<CloseOutcome> closeReader({
    required String sourceUrl,
    required String bookUrl,
    bool saveProgress = true,
  }) async =>
      throw _missing('reader.close');

  /// 目录那一屏的行（8 格白名单，出口在服务侧投影）。**目录不在 `reader.open` 上**。
  Future<List<ChapterRow>> chapterRows({
    required String sourceUrl,
    required String bookUrl,
    bool refresh = false,
  }) async =>
      throw _missing('reader.toc');

  /// 一章正文：回的是**已渲染形态**（`<usehtml>` 在服务侧渲染掉了，因为那一步改字数，
  /// 而段数/总字数是两侧比翻页落点那把尺的读数）。这一发**会真的发网络请求**。
  Future<ChapterDocument> chapterDocument({
    required String sourceUrl,
    required String bookUrl,
    required String chapterUrl,
  }) async =>
      throw _missing('reader.content');

  /// 存档的续读位置。**没记录 = null**，与"记录在第 0 章第 0 字"是两种形状
  /// （后者是"这本书刚开过"，前者是"这本书从没读过"）。
  Future<ReadingPosition?> readPosition({
    required String sourceUrl,
    required String bookUrl,
  }) async =>
      throw _missing('reader.position_get');

  /// 落一篇位置。[positionJson] 用 [ReadingPosition.toJson]（服务端 `from_reading`
  /// 按那九个蛇形键取值）。回的是**落库并收口后的形状**——界面上"存进去了什么"
  /// 以这一发的回话为准，不是以发出去的那份为准（越界的偏移会被夹回来）。
  ///
  /// 必须先 `openReader`：会话不是这本书时这一发拒答，否则会把 A 书的名字写到
  /// B 书那一行，而 `books` 是 `ON CONFLICT DO UPDATE`，写坏是静默的。
  Future<ReadingPosition> savePosition({
    required String sourceUrl,
    required String bookUrl,
    required Map<String, Object?> positionJson,
  }) async =>
      throw _missing('reader.position_save');

  /// 当前书若是试读暂存（Legado notShelf）给出那条记录；**在架的书回 null**
  /// （否则每次退出都被问一遍"要放入书架吗"）。
  Future<StagedRecord?> stagedRecord() async => throw _missing('reader.staged');

  /// 试读转正：暂存记录原地变书架记录，进度/变量/目录缓存不动。
  Future<bool> promoteStaged() async => throw _missing('reader.staged_promote');

  /// 试读放弃：记录、进度与缓存一起删（只删书架行等于删了个寂寞）。
  Future<bool> discardStaged() async => throw _missing('reader.staged_discard');

  /// 按书列书签（服务端按创建时间升序排好；客户端不重排）。
  Future<List<BookmarkRow>> listBookmarks(String bookUrl) async =>
      throw _missing('reader.bookmarks');

  /// 加一条书签，回它的 id（`reader.bookmark_remove` 吃的就是它）。
  ///
  /// [offset] 是**章内比例**（0~1），与 `reader.position_save` 那把字符偏移的尺
  /// 不是同一个量——服务端那一列当初就是给"大概翻到哪儿"用的，别混。
  Future<int> addBookmark({
    required String bookUrl,
    required int chapterIndex,
    double offset = 0,
    String summary = '',
  }) async =>
      throw _missing('reader.bookmark_add');

  /// 回**是否真删到**（可能已被另一侧删过），不是"请求受理了没有"。
  Future<bool> removeBookmark({
    required String bookUrl,
    required int bookmarkId,
  }) async =>
      throw _missing('reader.bookmark_remove');

  /// 书内全文检索：先按需补索引再查。0 命中要拿 `ftsAvailable` + `indexedChapters`
  /// 分清"这台机器搜不了""只搜了已缓存的 N 章""真没有这个词"。
  Future<ContentSearchResult> searchBookContent({
    required String bookUrl,
    required String keyword,
    String? sourceUrl,
  }) async =>
      throw _missing('reader.search_content');

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
