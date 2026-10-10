/// 订阅面的数据层：源行 / 文章行 / 正文详情 / 全量刷新的进度帧，加上屏那几句。
///
/// 键名从真响应取（第九十六轮抓的那十二发）：
/// `tests/gateway/golden/rss.sources.json`（3 行 × 10 键 = `field_names` 那 10 格）、
/// `rss.articles.json`（9 键，**没有正文**）、`rss.article.json`（10 键 = 那 9 格 + `text`）、
/// 以及 `rss.refresh_all.json` 的 `progress_sample`（进度帧 6 格）与 `event_sample`。
/// 绑定表在 `test/golden_contract_test.dart`，两屏同行文本在
/// `tests/gateway/projection/rss.rows.json`（答案列由 flet 的 `RssView` 自己渲染出来）。
///
/// 文案那一段**以 flet 源码为准**（`app/views/reading/rss_view.py` 的槽位 +
/// `app/controllers/rss_controller.py` 的状态句），跨语言守卫是
/// `tests/test_gateway_contract_single_source.py::test_rss_wording_is_read_from_flet_into_dart`。
/// 为什么句子住在这里而不是界面里拼：flet 那一屏**状态、进度、结果共用一个 `_status` 槽**
/// （谁最后写谁说话），把句子收在一处，两侧的优先级才比得出东西。
library;

/// 一条订阅源（`rss.sources` 的行：源字段 + 两格角标）。
///
/// 角标是**服务端 join 好的**（`source_rows` = `list_sources` + `article_counts`），
/// 客户端不许自己逐源数文章：那是把那条 GROUP BY 再抄一遍，两份"几篇"迟早不一样。
class RssSourceRow {
  const RssSourceRow({
    required this.id,
    required this.name,
    required this.url,
    required this.enabled,
    required this.group,
    required this.customOrder,
    required this.type,
    required this.lastUpdateTime,
    required this.total,
    required this.unread,
  });

  /// 源 id **就是**规范化后的 feed 地址（`add_source` 那一发回它，删除/刷新都吃它）。
  final String id;
  final String name;
  final String url;
  final bool enabled;
  final String group;
  final int customOrder;
  final int type;

  /// 上次刷新的 epoch 秒；`0` = 从没刷过（那一格上屏是「刷新于 从未」，见 [whenText]）。
  final double lastUpdateTime;
  final int total;
  final int unread;

  /// 行标题：`name` 空时是「(未命名)」，停用再缀一段（照 flet `render_sources`）。
  String get title {
    final shown = name.isEmpty ? kRssUnnamed : name;
    return enabled ? shown : '$shown$kRssDisabledSuffix';
  }

  /// 副标题：`N 篇 · N 未读 · 刷新于 X`，未读为 0 时**整段不出现**。
  ///
  /// 写在副标题而不是 trailing 是 flet 那边一条像素教训（第七十八轮）：trailing 一旦
  /// 是 Row，客户端把那一坨按不收缩的宽度排在最右，标题被挤成 0 宽——名字整行不见。
  String get subtitle {
    final when = whenText(lastUpdateTime);
    final bits = <String>[
      '$total 篇',
      if (unread > 0) '$unread 未读',
      '刷新于 ${when.isEmpty ? kRssNeverRefreshed : when}',
    ];
    return bits.join(' · ');
  }

  factory RssSourceRow.fromJson(Map<String, Object?> json) => RssSourceRow(
        id: '${json['id'] ?? ''}',
        name: '${json['name'] ?? ''}',
        url: '${json['url'] ?? ''}',
        // **缺键 = 启用**：`rss_sources.enabled` 的默认就是 1，服务端投影带的是布尔，
        // 这里把 null 读成 false 会把一条好源演成"已停用"。
        enabled: json['enabled'] != false,
        group: '${json['group'] ?? ''}',
        customOrder: _asInt(json['custom_order']),
        type: _asInt(json['type']),
        lastUpdateTime: _asDouble(json['last_update_time']),
        total: _asInt(json['total']),
        unread: _asInt(json['unread']),
      );

  @override
  String toString() => 'RssSourceRow($name, $total 篇/$unread 未读)';
}

/// 文章列表里的一行（`rss.articles` 的 9 键）。**这一族没有正文**。
///
/// 少的那一格是服务端裁的：feed 的 `description` 可以就是整篇文章，而中栏一根都不显示它；
/// 列表按行算，一屏几十行就是几十次 HTML 剥离 + 几十倍载荷。正文由 [RssArticleDetail]
/// 单取（`rss.article`）。
class RssArticleRow {
  const RssArticleRow({
    required this.id,
    required this.sourceId,
    required this.title,
    required this.url,
    required this.author,
    required this.pubDate,
    required this.image,
    required this.read,
    required this.starred,
  });

  /// 文章 id 是**自增整数**（与源 id = url 那一族不同），`rss.mark_read`/`rss.star`
  /// 吃的就是它，所以这里保留数字形状而不是折成字符串。
  final int id;
  final String sourceId;
  final String title;
  final String url;
  final String author;

  /// feed 里那一条的原始日期串（RSS 是 `Mon, 05 Oct 2026 09:00:00 GMT`、Atom 是 ISO）。
  /// 服务端不折成 epoch：两族的写法不同，折法就得有两把尺。
  final String pubDate;
  final String image;
  final bool read;
  final bool starred;

  String get shownTitle => title.isEmpty ? kRssUntitled : title;

  /// 副标题：`日期 · 作者`，**空的那一段整段不出现**。
  ///
  /// Atom 那一发（样本 `rss.articles--no-author`）整列 `author` 都是空串——
  /// 那一条尾分隔符要是留着，界面上每行末尾挂一个孤零零的「 · 」。
  ///
  /// 日期走 [whenFromRaw] 而不是原样吐：flet 把 `pub_date` 也过了 `_when`
  /// （`rss_view.py:332`），而那把尺对**能读成数字的串**会格式化成日期。
  /// 两边都用原串才同尺，客户端自己解析 RSS/Atom 两套写法就是第二把尺。
  String get subtitle => <String>[
        whenFromRaw(pubDate),
        author,
      ].where((part) => part.isNotEmpty).join(' · ');

  /// 未读行的字重（flet：`weight = None if read else W_500`）。
  bool get bold => !read;

  factory RssArticleRow.fromJson(Map<String, Object?> json) => RssArticleRow(
        id: _asInt(json['id']),
        sourceId: '${json['source_id'] ?? ''}',
        title: '${json['title'] ?? ''}',
        url: '${json['url'] ?? ''}',
        author: '${json['author'] ?? ''}',
        pubDate: '${json['pub_date'] ?? ''}',
        image: '${json['image'] ?? ''}',
        read: json['read'] == true,
        starred: json['starred'] == true,
      );

  /// 详情那一发是按同一套键解的（多出来的是 `text`），所以复用键名。
  Map<String, Object?> toMap() => <String, Object?>{
        'id': id,
        'source_id': sourceId,
        'title': title,
        'url': url,
        'author': author,
        'pub_date': pubDate,
        'image': image,
        'read': read,
        'starred': starred,
      };
}

/// 打开一篇文章（`rss.article` 的 10 键 = 列表那 9 格 + `text`）。
///
/// **`text` 已经是去掉标签的那一份**：那把尺（`rss_service.plain_text`）在服务侧，
/// 库里存的仍是 HTML 原文。Dart 这边绝不再剥一次——两边各剥一次迟早给出两种字数，
/// 而那时比的是两份主观（设计 §4.2「不许界面自己发明判据」）。
class RssArticleDetail {
  const RssArticleDetail({
    required this.row,
    required this.text,
  });

  final RssArticleRow row;
  final String text;

  String get id => '${row.id}';

  /// 正文空着那一格不是空白屏（flet `show_article` 同句）。
  String get body => text.isEmpty ? kRssNoBody : text;

  /// 元信息行：`作者 · 日期`（顺序照 flet `show_article`，与列表那行**相反**——
  /// 那一行先给日期，这一行先给作者），空段不出现。
  String get meta => <String>[
        row.author,
        whenFromRaw(row.pubDate),
      ].where((part) => part.isNotEmpty).join(' · ');

  factory RssArticleDetail.fromJson(Map<String, Object?> json) => RssArticleDetail(
        row: RssArticleRow.fromJson(json),
        text: '${json['text'] ?? ''}',
      );
}

/// 全量刷新的进度帧（`RssRefreshProgressEvent` 的载荷，样本 6 格）。
///
/// 三条计数口径都在服务端定过（见 `rss_service.refresh_all` 的方法注释）：
/// - `total` 是本轮**计划**源数，开头定下、中途不变；`done` 是已经动手的源数。
/// - `failures` **只在收尾那一帧**有意义：中途每一帧的失败数都只是"截至此刻"，
///   画在进度条上就是一条锯齿状失败数。
/// - `refresh_id` 由客户端生成、每帧原样回带——`rss.refresh_all` 是 job，受理回包
///   只有 `{started, action}`，晚到的帧只能靠它认本轮。
class RssRefreshProgress {
  const RssRefreshProgress({
    required this.refreshId,
    required this.done,
    required this.total,
    required this.current,
    required this.failures,
  });

  final int refreshId;
  final int done;
  final int total;
  final String current;
  final int failures;

  /// 状态行那一句。flet 写的是 `done + 1`（"正在刷的是第几条"），不是 `done`
  /// （"已经刷完几条"）——进度条读 `done/total`，状态行读这一句，两者不同值是**有意的**。
  ///
  /// `total == 0` 时**不许读这一句**：flet 外面套了 `if ev.total:`（`rss_controller.py:300`），
  /// 一个源都没有的那一轮没有"第几条"可说，那句空分母的句子（`1/0`）由调用方跳过。
  String get status => '正在刷新 ${done + 1}/$total：'
      '${current.isEmpty ? kRssUnknownSource : current}';

  factory RssRefreshProgress.fromJson(Map<String, Object?> map) =>
      RssRefreshProgress(
        refreshId: _asInt(map['refresh_id']),
        done: _asInt(map['done']),
        total: _asInt(map['total']),
        current: '${map['current'] ?? ''}',
        failures: _asInt(map['failures']),
      );
}

/// 一条源刷完了（`RssRefreshedEvent`：`source_id` + `new_count`）。
class RssRefreshed {
  const RssRefreshed({required this.sourceId, required this.newCount});

  final String sourceId;

  /// 本次**新增**的篇数，不是总数。非 0 才证明真取到了东西。
  final int newCount;

  factory RssRefreshed.fromJson(Map<String, Object?> map) => RssRefreshed(
        sourceId: '${map['source_id'] ?? ''}',
        newCount: _asInt(map['new_count']),
      );
}

// -------------------------------------------------------------------- #
// 上屏那几句（flet 的 `_status` 槽与那两处渲染写的是同一批句子）
// -------------------------------------------------------------------- #

/// 顶栏标题（`RssView` 那个 `size=22` 的「订阅」）。
const String kRssPageTitle = '订阅';

/// 窄窗那三级的标题（`_LEVEL_TITLES`），顺序就是级切换条上的顺序。
const List<String> kRssLevels = <String>['sources', 'articles', 'detail'];
const Map<String, String> kRssLevelTitles = <String, String>{
  'sources': '订阅源',
  'articles': '文章',
  'detail': '正文',
};

/// 级之间那个分隔字形（flet 用「›」）。
const String kRssLevelSeparator = '›';

/// 右栏还没打开任何文章时那一句（`clear_detail` 的两个入参值）。
const String kRssPickSource = '选一个订阅源';
const String kRssPickArticle = '选一篇文章看正文';
const String kRssNoSourcesAtAll = '还没有订阅源';

/// 左栏空表那一句（`render_sources([])`）。
const String kRssEmptySources =
    '还没有订阅源：点上方「+」填名称与 feed 地址。';

/// 中栏空表那一句（`render_articles([])`）。**不写"左边的源"**：窄窗里上一级在切换条上。
const String kRssEmptyArticles =
    '这里还没有文章：先选一个订阅源，点「⋮」→「刷新本源」抓一次。';

/// 正文空着那一格（`show_article` 的 `body or …`）。
const String kRssNoBody = '(这条没有正文，只有标题和链接)';

/// 进度帧里 `current` 空着那一格（flet `rss_controller.py:301` 的 `ev.current or '未知源'`）。
/// 它不是装饰：队列排到还没命名的那一本时，状态行会写成"正在刷新 2/3："，
/// 后半截空着读起来像句子被截断了。
const String kRssUnknownSource = '未知源';

/// 行名/标题缺值时那两个占位。
const String kRssUnnamed = '(未命名)';
const String kRssUntitled = '(无标题)';

/// 源停用时的后缀、从没刷过时的那一格。
const String kRssDisabledSuffix = '（已停用）';
const String kRssNeverRefreshed = '从未';

/// 顶栏两个按钮的 tooltip。
const String kRssRefreshAllTooltip = '刷新全部订阅源';
const String kRssAddTooltip = '添加订阅源';
const String kRssUnreadOnlyLabel = '只看未读';
const String kRssSourceMenuTooltip = '本源操作';
const String kRssStarTooltip = '收藏';
const String kRssUnstarTooltip = '取消收藏';

/// 级切换条那两个 tooltip 的前缀（`当前：X` / `跳到X`）。
String kRssLevelCurrentTooltip(String title) => '当前：$title';
String kRssLevelJumpTooltip(String title) => '跳到$title';

/// 「⋮」菜单与确认框（`build_source_menu` / `build_confirm_remove`）。
const String kRssMenuItemRefresh = '刷新本源';
const String kRssMenuItemRemove = '删除本源';
const String kRssRemoveDialogTitle = '删除订阅源';
String kRssRemoveDialogBody(String name) =>
    '确定删除「$name」吗？它的文章（含已读与收藏标记）会一起删掉。';
const String kRssCancel = '取消';
const String kRssDelete = '删除';
const String kRssClose = '关闭';
const String kRssAdd = '添加';

/// 添加框（`build_add_dialog`）：标题、两个字段、那句"不提供规则编辑"的说明。
const String kRssAddDialogTitle = '添加订阅源';
const String kRssFieldName = '名称';
const String kRssFieldNameHint = '留空则用地址当名称';
const String kRssFieldUrl = '订阅地址（feed URL）';
const String kRssFieldUrlHint = 'https://example.com/feed';
const String kRssRuleNote = '只抓取标准 RSS/Atom。带规则的订阅源要在源文件里填规则字段，'
    '本页暂不提供规则编辑。';

// ---- 控制器那侧的状态句（全屏只有这一个槽） ----

/// 列表那一发失败（`_reload_sources` 的 except）。
String kRssSourcesLoadFailed(String error) => '订阅源列表加载失败：$error';

/// 文章那一发失败（`_reload_articles` 的 except）。
String kRssArticlesLoadFailed(String error) => '文章列表加载失败：$error';

/// 添加成功、正在抓第一遍（`_add_source`）。
String kRssAdded(String name) => '已添加「$name」，正在抓取…';

/// 添加失败（`_add_source` 的 except）。
String kRssAddFailed(String error) => '添加失败：$error';

/// 点了全部刷新（`refresh_all`）。
const String kRssRefreshingAll = '正在刷新全部订阅源…';

/// 整批跑完（`_run_refresh_all`）：`new` 是**本轮新增总篇数**。
String kRssRefreshDone(int added) => '刷新完成，新增 $added 篇';

/// 整批跑完、且这一轮有源取失败（**flet 没有这一句**，是跨进程才多出来的读数）。
///
/// 为什么要有：flet 那句吃的是 `refresh_all()` 的返回值，而单源失败被服务吞进了
/// `status.last_error`，那一屏读不到条数。跨进程这一侧收尾那一帧带着 `failures`，
/// 只报"新增 0 篇"就是把"三个源都没连上"演成"三个源都没有新文章"
/// （记忆 `zero-counts-need-a-positive-control` 那一族）。守卫把这一句记在
/// 例外表里，不许被当成"flet 里没有所以不该存在"。
String kRssRefreshDoneWithFailures(int added, int failures) =>
    '刷新完成，新增 $added 篇（$failures 个源取失败了）';

/// 整批那一发失败（`_run_refresh_all` 的 except）。
String kRssRefreshAllFailed(String error) => '全部刷新失败：$error';

/// 单源刷新跑完（`_refresh_one` 的 `report=True` 那一支）。
String kRssRefreshOneDone(String name, int added) =>
    '「$name」刷新完成，新增 $added 篇';

/// 任何一发抓取失败（`_refresh_one` 的 except；`_add_source` 之后那一路也走它）。
String kRssRefreshFailed(String error) => '刷新失败：$error';

/// 删除成功/那条本就不在（`_do_remove` 的两个分支——服务端回的是**是否真删到行**）。
String kRssRemoved(String name) => '已删除「$name」';
const String kRssAlreadyGone = '该订阅源已不在列表里';

/// 删除那一发失败（`_do_remove` 的 except）。
String kRssRemoveFailed(String error) => '删除失败：$error';

/// 收藏那三句（`toggle_star`）：成功两句、文章已被删一句、那一发失败一句。
const String kRssStarred = '已收藏';
const String kRssUnstarred = '已取消收藏';
const String kRssArticleGone = '这条文章已经不在了（收藏未保存）';
String kRssStarFailed(String error) => '收藏失败：$error';

/// 打开正文时那一发已读失败（flet 只写日志、不打断阅读；Dart 把它推进横幅那条流，
/// 文案同一句——见 `rss_notifier.dart` 的 `_mark`）。
String kRssMarkReadFailed(String error) => '已读标记失败：$error';

/// 进度条那一发：`done/total`，夹在 0~1，跑完就收起（flet `set_progress`）。
///
/// 收是必要的：留一根满格的条在顶上，用户读到的是"还在刷"。
double rssProgressValue(int done, int total) {
  if (total <= 0) return 0;
  final fraction = done / total;
  if (fraction < 0) return 0;
  return fraction > 1 ? 1 : fraction;
}

/// 进度条**出现**吗（flet 的 `running = total > 0 and done < total`）。
bool rssProgressVisible(int done, int total) => total > 0 && done < total;

// -------------------------------------------------------------------- #
// 日期那把尺（与 flet `rss_view._when` 逐字同形，答案钉在投影表里）
// -------------------------------------------------------------------- #

/// epoch 秒 → `MM-DD HH:MM`；`0`/空 → 空串（调用方自己决定写「从未」还是不写）。
///
/// 三条分支一条都不能少，且**都按本地时区**（flet 用的是 `time.localtime`）：
/// - `stamp > 1e9` 才当 epoch 秒格式化；
/// - `0 < stamp <= 1e9` 原样吐回那个数（1970 年那一带的值多半是别的单位，
///   折成「01-01 08:00」是把一个单位错误演成一个日期）；
/// - 非数字（文章那一发的 `pub_date` 是 feed 原文串）由 `float()` 那一跳失败、
///   同样原样吐回——所以列表那两栏的日期**根本没被格式化**，这一屏不许自己解析它。
String whenText(double? stamp) {
  if (stamp == null || stamp <= 0) return '';
  if (stamp <= 1000000000) return '$stamp';
  return _formatEpochSeconds(stamp);
}

/// epoch 秒 → `MM-DD HH:MM`，**按本地时区**（flet 用的是 `time.localtime`）。
String _formatEpochSeconds(double stamp) {
  final at = DateTime.fromMillisecondsSinceEpoch((stamp * 1000).round());
  return '${_two(at.month)}-${_two(at.day)} ${_two(at.hour)}:${_two(at.minute)}';
}

String _two(int value) => value.toString().padLeft(2, '0');

/// feed 原文里那一格日期 → 界面那一句（flet `_when` 吃**原始值**的那一支）。
///
/// 与 [whenText] 分成两个函数不是为了好看，是 `str(value)` 吐回的是**传进来那一份**：
/// 协议面上 `pub_date` 是字符串，`"1759000000"` 经 flet 的 `float()` 解得开、
/// 大于 1e9 就格式化成日期；而 `str(value)` 回到的是原串 `"1759000000"`，
/// 不是 Dart 的 double 打印出来的 `"1759000000.0"`。按 [whenText] 走会把这一格
/// 多打一个 `.0`，投影表一比就红。
String whenFromRaw(String value) {
  if (value.isEmpty) return '';
  final stamp = double.tryParse(value);
  // 解不开就是 feed 写的那个日期串（RSS 的 `Mon, 05 Oct 2026 09:00:00 GMT`、
  // Atom 的 `2026-10-05T09:00:00Z`），原样上屏——这一屏不解析它。
  if (stamp == null) return value;
  if (stamp <= 0) return '';
  if (stamp <= 1000000000) return value;
  return _formatEpochSeconds(stamp);
}

int _asInt(Object? value) => value is num ? value.toInt() : 0;

double _asDouble(Object? value) => value is num ? value.toDouble() : 0;
