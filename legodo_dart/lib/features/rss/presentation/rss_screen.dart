/// 订阅那一屏（对齐 flet `app/views/reading/rss_view.py`）。
///
/// 两副形状、一批栏控件（这是本页最要紧的一条形状约束，flet 那边是实测换来的）：
///
/// - **宽窗**：三栏并排，flex **3:4:5**（源:文章:正文）。不用定宽：第七十七轮在 344
///   那一档量过定宽版（260+340+剩余）的像素——第二栏被切掉半截、第三栏整个在窗外
///   （`out_portrait75_reading_rss_360.png`）。
/// - **窄窗**（[kNarrowWidthThreshold] 以下）：**一次只摊开一栏**，顶部一条
///   「订阅源 › 文章 › 正文」级切换跟着走。三栏并排在窄窗本来就是错的形状，
///   不是宽度没给够（同一轮量到每栏只剩 ~90–120px：看得见但读不了）。
/// - 换档**不换父节点、不重排**：这里两副形状共用同一批栏控件，切换只改两个量
///   （flex 份额与 offstage 旗标）。flet 那条理由（两个 ListView 正逐条落文章，中途被
///   摘走就白丢一截渲染）在 Flutter 侧落在另一半上：栏子树被摘走 = Element 重建 =
///   滚动位置与在途布局全丢，所以这里同样不换结构。
///
/// 三条与 flet 同口径的界面规矩，都写在状态槽那侧（`domain/rss_notifier.dart`）：
/// 正文只在打开时取（列表那一发没有正文）、删除必须二次确认、状态只有一个槽。
///
/// 状态行这里**没有**用 `core/widgets/state_bar.dart`（其余四屏用了）：StateBar 那一个
/// `AsyncValue` 回落档是给"加载失败报什么"造的，而这一屏的失败句在 notifier 里就写进了
/// `status`（flet 的 `_reload_sources` 正是只写状态、不清数据），两格一起留等于同一个
/// 事实两个持有者——而且这一屏在失败那一拍手里**还有**旧列表要画，`AsyncError` 那一档
/// 会把"没拿到数据"画成整屏空。
///
/// 档位这一处是**第一次**用到 `Breakpoint`（`app/shell.dart` 那份 720 阈值）：
/// 壳那一层早就按档换导航形状，但屏内两副形状在此之前还没有第二个读者。
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../app/shell.dart';
import '../../../core/events/event_listener.dart';
import '../../../core/events/event_names.dart';
import '../../../core/widgets/failure_banner.dart';
import '../data/rss_models.dart';
import '../domain/rss_notifier.dart';

class RssScreen extends ConsumerStatefulWidget {
  const RssScreen({super.key});

  @override
  ConsumerState<RssScreen> createState() => _RssScreenState();
}

class _RssScreenState extends ConsumerState<RssScreen> {
  /// 窄窗当前摊开哪一栏。**视图局部的状态**（flet 把它放在 `RssView._level`，
  /// 控制器不知道有这一格）：它是"我看哪儿"而不是"库里有啥"，
  /// 进 notifier 就等于给同一个事实造第二个持有者。
  int _level = 0;

  /// 事件流断了那句话只写在这里：它是"这一拍的故障"，不是这一屏的数据
  /// （同 `find_books_screen.dart` 那一处）。
  String _streamError = '';

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(rssProvider);
    final notifier = ref.read(rssProvider.notifier);
    final view = state.value ?? RssState.empty;

    listenToEngineEvents(
      ref,
      <String>[
        EngineEventNames.rssRefreshProgress,
        EngineEventNames.rssRefreshed,
      ],
      (event) {
        if (event.name == EngineEventNames.rssRefreshProgress) {
          notifier.onRefreshProgress(event);
        } else {
          notifier.onRefreshed(event);
        }
      },
      onStreamError: (error) => setState(() => _streamError = '事件流断开：$error'),
    );

    final status = _streamError.isNotEmpty ? _streamError : view.status;

    return LayoutBuilder(
      builder: (context, constraints) {
        final narrow = Breakpoint.of(constraints.maxWidth) == Breakpoint.narrow;
        // 够不着的级自动往前退（源被删光时人还停在"文章"，那一栏已经空了）。
        // 这里**就地算**而不是 setState：build 期间改状态是禁的，而 flet 的
        // `_clamp_level` 正是在每次渲染尾部跑的同一件事。
        final level = narrow ? _clampLevel(view) : 0;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            FailureBanner(stream: notifier.failures),
            _header(notifier),
            if (view.progressVisible)
              LinearProgressIndicator(
                key: const Key('rss.progress'),
                minHeight: 6,
                value: view.progressValue,
              ),
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 4),
              child: Text(status,
                  style: Theme.of(context)
                      .textTheme
                      .bodySmall
                      ?.copyWith(color: Colors.grey[600])),
            ),
            const Divider(height: 1),
            // **永远挂在树里、宽窗时 `Offstage`**（对应 flet 的 `_switch.visible=False`）：
            // 写成 `if (narrow) …` 会在跨档那一拍往 Column 里插/删一个子项，
            // 下面那条 `Expanded` 的槽位跟着挪 → Element 重配 → 两个 ListView 的
            // 滚动位置全丢。顶部这条注释说的"不换父节点、不重排"包括这一格。
            _offstage(!narrow, _levelBar(view, level),
                key: const Key('rss.shape.switch')),
            Expanded(
                child: _panes(
                    notifier: notifier,
                    view: view,
                    narrow: narrow,
                    level: level,
                    state: state)),
          ],
        );
      },
    );
  }

  // ------------------------------------------------------------------ #
  // 顶栏
  // ------------------------------------------------------------------ #
  Widget _header(RssNotifier notifier) => Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 4),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: <Widget>[
            Text(kRssPageTitle,
                style: Theme.of(context)
                    .textTheme
                    .headlineSmall
                    ?.copyWith(fontWeight: FontWeight.bold)),
            Row(children: <Widget>[
              IconButton(
                key: const Key('rss.add'),
                icon: const Icon(Icons.add),
                tooltip: kRssAddTooltip,
                onPressed: () => _openAddDialog(notifier),
              ),
              IconButton(
                key: const Key('rss.refresh.all'),
                icon: const Icon(Icons.refresh),
                tooltip: kRssRefreshAllTooltip,
                onPressed: notifier.refreshAll,
              ),
            ]),
          ],
        ),
      );

  /// 级切换条：「订阅源 › 文章 › 正文」，当前那级实心、够不着的置灰。
  ///
  /// 三格都在投影表的证人位上（`tests/gateway/projection/rss.shape.json` 的
  /// `switch`：`text` / `kind` / `disabled` / `tooltip`）——flet 那边当前那级是一颗
  /// **可点但没有 on_click** 的实心按钮（`disabled = not reachable`），所以这里
  /// 不能图省事写成 `onPressed: null`：那会把它渲染成置灰，而表里那一格是 `false`。
  Widget _levelBar(RssState view, int level) => Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: <Widget>[
            for (var index = 0; index < kRssLevels.length; index++) ...<Widget>[
              if (index > 0)
                Text(kRssLevelSeparator,
                    style: TextStyle(fontSize: 13, color: Colors.grey[500])),
              _levelButton(view, index, level),
            ],
          ],
        ),
      );

  Widget _levelButton(RssState view, int index, int level) {
    final title = kRssLevelTitles[kRssLevels[index]]!;
    final isCurrent = index == level;
    return Tooltip(
      // 这一格也挂名：表里的 `tooltip` 列要在控件树上有一个读点。不给 key 的话，
      // 读它只能走 `find.ancestor`，而这一版 Flutter 的 `find.ancestor` 没有
      // `skipOffstage` 参数——宽窗时整条切换条是 Offstage 的，ancestor 那一发永远抓不到。
      key: Key('rss.level.${kRssLevels[index]}.tip'),
      message: isCurrent ? kRssLevelCurrentTooltip(title) : kRssLevelJumpTooltip(title),
      child: isCurrent
          ? FilledButton.tonal(
              key: Key('rss.level.${kRssLevels[index]}'),
              // 按当前那一级 = 停在原地（flet 那一颗没有 on_click，按下什么都不发生）。
              onPressed: () => setState(() => _level = index),
              child: Text(title),
            )
          : TextButton(
              key: Key('rss.level.${kRssLevels[index]}'),
              onPressed: _reachable(view, index)
                  ? () => setState(() => _level = index)
                  : null,
              child: Text(title),
            ),
    );
  }

  bool _reachable(RssState view, int index) => switch (index) {
        0 => true,
        1 => view.hasSource,
        _ => view.hasArticle,
      };

  int _clampLevel(RssState view) {
    for (var index = math.min(_level, kRssLevels.length - 1);
        index >= 0;
        index--) {
      if (_reachable(view, index)) return index;
    }
    return 0;
  }

  // ------------------------------------------------------------------ #
  // 三栏
  // ------------------------------------------------------------------ #
  /// 两副形状共用同一条 `Row`：宽窗三份 flex 3/4/5，窄窗把当前那栏的份额抬到压倒性
  /// （其余两栏留 1 ≈ 不到 1px，并且整栏 `Offstage`：不画、不接收点击，但**树还在**）。
  ///
  /// 三颗 `Expanded` 各带一个 `rss.shape.flex.*` 的 key：份额这一格靠 `find.ancestor`
  /// 读不稳（栏外面还套着 build 里那一条 `Expanded`，一次抓出两颗），而这两副形状差别的
  /// 一半就写在份额上，读不到等于没测。
  Widget _panes({
    required RssNotifier notifier,
    required RssState view,
    required bool narrow,
    required int level,
    required AsyncValue<RssState> state,
  }) {
    int flex(int index) =>
        !narrow ? <int>[3, 4, 5][index] : (index == level ? 1000 : 1);
    bool shown(int index) => !narrow || index == level;

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Expanded(
            key: const Key('rss.shape.flex.sources'),
            flex: flex(0),
            child: Padding(
                padding: const EdgeInsets.all(8),
                child: _offstage(
                    !shown(0),
                    _sourcePane(
                        notifier: notifier,
                        view: view,
                        narrow: narrow),
                    key: const Key('rss.shape.pane.sources')))),
        Offstage(
            key: const Key('rss.shape.divider.0'),
            offstage: narrow,
            child: const VerticalDivider(width: 1, thickness: 1)),
        Expanded(
            key: const Key('rss.shape.flex.articles'),
            flex: flex(1),
            child: Padding(
                padding: const EdgeInsets.all(8),
                child: _offstage(
                    !shown(1),
                    _articlePane(
                        notifier: notifier,
                        view: view,
                        state: state,
                        narrow: narrow),
                    key: const Key('rss.shape.pane.articles')))),
        Offstage(
            key: const Key('rss.shape.divider.1'),
            offstage: narrow,
            child: const VerticalDivider(width: 1, thickness: 1)),
        Expanded(
            key: const Key('rss.shape.flex.detail'),
            flex: flex(2),
            child: Padding(
                padding: const EdgeInsets.all(12),
                child: _offstage(!shown(2),
                    _detailPane(notifier: notifier, view: view),
                    key: const Key('rss.shape.pane.detail')))),
      ],
    );
  }

  /// `Offstage` 而不是 `if (shown) … else SizedBox.shrink()`：后者会把栏子树摘掉，
  /// Element 跟着没（滚动位置与在途布局全丢）；前者树一直在，只是不画、不接收点击。
  ///
  /// 那几颗 `Offstage` 各带一个 `rss.shape.*` 的 key：投影表 `rss.shape.json` 有五列
  /// （`switch_visible` / `panes_visible[3]` / `dividers_visible[2]`）读的就是这六个旗标，
  /// 没有名字的话测试只能靠"树上有几个 Offstage"猜哪一个是哪一栏。
  Widget _offstage(bool hidden, Widget child, {Key? key}) =>
      Offstage(key: key, offstage: hidden, child: child);

  // ------------------------------------------------------------------ #
  // ① 左栏：订阅源
  // ------------------------------------------------------------------ #
  Widget _sourcePane({
    required RssNotifier notifier,
    required RssState view,
    required bool narrow,
  }) =>
      Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        spacing: 4,
        children: <Widget>[
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: <Widget>[
              Text(kRssLevelTitles['sources']!,
                  style: Theme.of(context).textTheme.titleSmall),
              Text(view.sourceCountLabel,
                  style: Theme.of(context)
                      .textTheme
                      .bodySmall
                      ?.copyWith(color: Colors.grey[600])),
            ],
          ),
          Expanded(
            child: view.sources.isEmpty
                ? _empty(kRssEmptySources)
                : ListView.builder(
                    key: const Key('rss.sources'),
                    itemCount: view.sources.length,
                    itemBuilder: (context, index) {
                      final source = view.sources[index];
                      return ListTile(
                        key: ValueKey('rss.source.${source.id}'),
                        selected: source.id == view.selectedSourceId,
                        selectedTileColor:
                            Theme.of(context).colorScheme.surfaceContainerHighest,
                        leading: const Icon(Icons.rss_feed),
                        title: Text(source.title,
                            maxLines: 1, overflow: TextOverflow.ellipsis),
                        subtitle: Text(source.subtitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(context)
                                .textTheme
                                .bodySmall
                                ?.copyWith(
                                    fontSize: 11, color: Colors.grey[500])),
                        // 点一行 = 交给控制器选源；窄窗顺手跳到文章（不跳就只能再点一次切换条）。
                        onTap: () => _pickSource(notifier, source.id, narrow),
                        trailing: IconButton(
                          key: ValueKey('rss.menu.${source.id}'),
                          icon: const Icon(Icons.more_vert),
                          tooltip: kRssSourceMenuTooltip,
                          onPressed: () => _openSourceMenu(notifier, source),
                        ),
                      );
                    },
                  ),
          ),
        ],
      );

  void _pickSource(RssNotifier notifier, String sourceId, bool narrow) {
    notifier.selectSource(sourceId);
    // 窄窗顺手跳到文章栏（不跳就只能再点一次切换条）。
    // narrow 是从 build 那一路递下来的，不在这里第二次量宽：这一屏的档位量的是
    // **内容区**约束，而 `MediaQuery.size` 量的是整个窗口——宽档下两个数差一条导航栏，
    // "跳到哪一栏"就会和"摊开哪一栏"按不同的尺走。
    if (narrow) setState(() => _level = 1);
  }

  void _pickArticle(RssNotifier notifier, RssArticleRow row, bool narrow) {
    notifier.openArticle(row);
    if (narrow) setState(() => _level = 2);
  }

  // ------------------------------------------------------------------ #
  // ② 中栏：文章
  // ------------------------------------------------------------------ #
  Widget _articlePane({
    required RssNotifier notifier,
    required RssState view,
    required AsyncValue<RssState> state,
    required bool narrow,
  }) {
    // 「正在读列表」那一拍不等于「这里没有文章」：旧行还挂着就继续画旧行，
    // 空表那句只在**真的读回来是空**的时候出现（flet 那边由 render_articles([]) 给）。
    final loading = state.isLoading && view.articles.isEmpty;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      spacing: 4,
      children: <Widget>[
        Row(
          spacing: 8,
          children: <Widget>[
            Text(kRssLevelTitles['articles']!,
                style: Theme.of(context).textTheme.titleSmall),
            Checkbox(
              key: const Key('rss.unread.only'),
              value: view.unreadOnly,
              visualDensity: VisualDensity.compact,
              onChanged: (value) => notifier.setUnreadOnly(value ?? false),
            ),
            Flexible(
                child: Text(kRssUnreadOnlyLabel,
                    style: Theme.of(context).textTheme.bodySmall)),
          ],
        ),
        Expanded(
          child: loading
              ? const Center(child: CircularProgressIndicator())
              : view.articles.isEmpty
                  ? _empty(kRssEmptyArticles)
                  : ListView.builder(
                      key: const Key('rss.articles'),
                      itemCount: view.articles.length,
                      itemBuilder: (context, index) {
                        final row = view.articles[index];
                        final starred = row.starred;
                        return ListTile(
                          key: ValueKey('rss.article.${row.id}'),
                          selected: row.id == view.currentArticleId,
                          selectedTileColor:
                              Theme.of(context).colorScheme.surfaceContainerHighest,
                          title: Text(row.shownTitle,
                              // 未读那一篇加粗（flet `rss_view.py:326-330`：
                              // `size=14, weight=None if read else W_500`）。
                              // 字号必须写死那一个数：`ListTile` 的标题在 M3 里默认是
                              // `bodyLarge`（16sp），不覆盖就把 flet 的 14 演成 16。
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  fontSize: 14,
                                  fontWeight:
                                      row.bold ? FontWeight.w500 : null)),
                          subtitle: Text(row.subtitle,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: Theme.of(context)
                                  .textTheme
                                  .bodySmall
                                  ?.copyWith(
                                      fontSize: 11, color: Colors.grey[500])),
                          onTap: () => _pickArticle(notifier, row, narrow),
                          trailing: IconButton(
                            key: ValueKey('rss.star.${row.id}'),
                            icon: Icon(starred ? Icons.star : Icons.star_border),
                            tooltip: starred ? kRssUnstarTooltip : kRssStarTooltip,
                            onPressed: () => notifier.toggleStar(row),
                          ),
                        );
                      },
                    ),
        ),
      ],
    );
  }

  // ------------------------------------------------------------------ #
  // ③ 右栏：正文
  // ------------------------------------------------------------------ #
  Widget _detailPane({
    required RssNotifier notifier,
    required RssState view,
  }) {
    final detail = view.detail;
    return ListView(
      key: const Key('rss.detail'),
      padding: EdgeInsets.zero,
      children: <Widget>[
        Row(
          spacing: 4,
          children: <Widget>[
            Expanded(
              child: Text(
                detail == null ? view.detailHint : detail.row.shownTitle,
                style: Theme.of(context)
                    .textTheme
                    .titleMedium
                    ?.copyWith(fontWeight: FontWeight.w500),
              ),
            ),
            if (detail != null)
              IconButton(
                key: const Key('rss.detail.star'),
                icon: Icon(detail.row.starred ? Icons.star : Icons.star_border),
                // **照搬，不是笔误**：flet 在 `show_article` 里只换图标、不换 tooltip
                // （`rss_view.py:355` 对 `:117` 那颗按钮只赋 `.icon`），所以收藏之后
                // 悬停仍写「收藏」。列表那一颗是两格一起换的（`:339-340`），投影表把
                // 两种形状各记了一行。留在原地是因为这一屏的判据是"与 flet 同数"，
                // 顺手修它=同时改尺和被测物；要修得单独一轮，第九十七轮日志里挂了号。
                tooltip: kRssStarTooltip,
                onPressed: () => notifier.toggleStar(detail.row),
              ),
          ],
        ),
        Text(detail?.meta ?? '',
            style: Theme.of(context)
                .textTheme
                .bodySmall
                ?.copyWith(fontSize: 12, color: Colors.grey[600])),
        SelectableText(detail?.body ?? '',
            key: const Key('rss.detail.body'),
            style: const TextStyle(fontSize: 14)),
        SelectableText(detail?.row.url ?? '',
            key: const Key('rss.detail.url'),
            maxLines: 2,
            selectionColor: Colors.grey[300],
            // `SelectableText` 没有 `overflow` 参数（`Text` 有），裁尾只能写在样式里。
            style: TextStyle(
                fontSize: 11, color: Colors.grey[500], overflow: TextOverflow.ellipsis)),
      ],
    );
  }

  Widget _empty(String message) => Center(
      child: Text(message,
          style: TextStyle(color: Colors.grey[600], fontSize: 12)));

  // ------------------------------------------------------------------ #
  // 三个对话框（flet 那边由视图搭、控制器挂到 page 上）
  // ------------------------------------------------------------------ #
  /// 「⋮」那一条：两个动作 + 关闭。**先关框再动手**（flet `_close_and`），
  /// 否则动作里再弹一个确认框就变成两层对话框叠着，退不干净。
  Future<void> _openSourceMenu(RssNotifier notifier, RssSourceRow source) async {
    final action = await showDialog<_SourceMenuAction>(
      context: context,
      builder: (context) => AlertDialog(
        key: const Key('rss.menu.dialog'),
        title: Text(_clipped(source.name.isEmpty ? source.id : source.name)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            ListTile(
              key: const Key('rss.menu.refresh'),
              leading: const Icon(Icons.refresh),
              title: const Text(kRssMenuItemRefresh),
              onTap: () => Navigator.of(context).pop(_SourceMenuAction.refresh),
            ),
            ListTile(
              key: const Key('rss.menu.remove'),
              leading: const Icon(Icons.delete_outline),
              title: const Text(kRssMenuItemRemove,
                  style: TextStyle(color: Colors.redAccent)),
              onTap: () => Navigator.of(context).pop(_SourceMenuAction.remove),
            ),
          ],
        ),
        actions: <Widget>[
          TextButton(
            key: const Key('rss.menu.close'),
            onPressed: () => Navigator.of(context).pop(),
            child: const Text(kRssClose),
          ),
        ],
      ),
    );
    if (!mounted || action == null) return;
    switch (action) {
      case _SourceMenuAction.refresh:
        await notifier.refreshSource(source.id);
      case _SourceMenuAction.remove:
        await _confirmRemove(notifier, source);
    }
  }

  /// 删除的二次确认：连文章（含已读与收藏标记）一起删，且**不可撤销**。
  Future<void> _confirmRemove(RssNotifier notifier, RssSourceRow source) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        key: const Key('rss.confirm'),
        title: const Text(kRssRemoveDialogTitle),
        content: Text(kRssRemoveDialogBody(
            source.name.isEmpty ? source.id : source.name)),
        actions: <Widget>[
          TextButton(
            key: const Key('rss.confirm.cancel'),
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text(kRssCancel),
          ),
          TextButton(
            key: const Key('rss.confirm.delete'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text(kRssDelete,
                style: TextStyle(color: Colors.redAccent)),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await notifier.removeSource(source.id);
  }

  /// 添加框：两个字段 + 那句"不提供规则编辑"的说明。
  ///
  /// 说明那一句是**功能边界**，不是装饰：带规则的订阅源要在源文件里填规则字段，
  /// 这一页只抓取标准 RSS/Atom。不写，用户就会以为在这里改了能生效。
  ///
  /// 两个输入框的控制器由 [_AddSourceDialog] 自己持有并释放（理由在那个类头上）：
  /// 这一发只拿回框里的两个字串。
  Future<void> _openAddDialog(RssNotifier notifier) async {
    final values = await showDialog<(String, String)>(
      context: context,
      builder: (context) => const _AddSourceDialog(),
    );
    if (values == null || !mounted) return;
    // 两个字段**空着也照样提交**：flet 那边 `submit` 不看空，名称留空由服务端
    // 拿 url 顶名，地址留空服务端会抛"RSS 源 url 不能为空"——那一句走状态槽。
    // 在这里加一道"没填不许提交"就是界面自己发明了一条服务端没有的规矩。
    await notifier.addSource(name: values.$1, url: values.$2);
  }

  /// 菜单标题那 40 个字：flet 是 `str(name or sid)[:40]`（超长源名会把这个小对话框
  /// 顶成整屏，而标题这一格本来只是"你在操作哪一条"）。
  String _clipped(String value) =>
      value.length <= 40 ? value : value.substring(0, 40);
}

enum _SourceMenuAction { refresh, remove }

/// 添加框的内容（两个输入框自己的 `TextEditingController` 住在 [State] 里）。
///
/// 控制器**必须**由这里持有：写在外层 `_openAddDialog` 里、`showDialog` 的 future
/// 一回来就 `dispose()` 会撞上退出动画——路由 pop 只让那个 future 完成，框本身还在
/// 树上重建一拍，那两个 `TextField` 会用到已经释放的控制器
/// （`A TextEditingController was used after being disposed`）。提交时把两个字串
/// 经 `Navigator.pop` 带出框，框外的代码再也碰不到控制器。
class _AddSourceDialog extends StatefulWidget {
  const _AddSourceDialog();

  @override
  State<_AddSourceDialog> createState() => _AddSourceDialogState();
}

class _AddSourceDialogState extends State<_AddSourceDialog> {
  final TextEditingController _name = TextEditingController();
  final TextEditingController _url = TextEditingController();

  @override
  void dispose() {
    _name.dispose();
    _url.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        key: const Key('rss.add.dialog'),
        title: const Text(kRssAddDialogTitle),
        content: SizedBox(
          width: 360,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            spacing: 8,
            children: <Widget>[
              TextField(
                key: const Key('rss.add.name'),
                controller: _name,
                decoration: const InputDecoration(
                    labelText: kRssFieldName, hintText: kRssFieldNameHint),
              ),
              TextField(
                key: const Key('rss.add.url'),
                controller: _url,
                decoration: const InputDecoration(
                    labelText: kRssFieldUrl, hintText: kRssFieldUrlHint),
              ),
              Text(kRssRuleNote,
                  style: TextStyle(fontSize: 11, color: Colors.grey[600])),
            ],
          ),
        ),
        actions: <Widget>[
          TextButton(
            key: const Key('rss.add.cancel'),
            onPressed: () => Navigator.of(context).pop(null),
            child: const Text(kRssCancel),
          ),
          TextButton(
            key: const Key('rss.add.submit'),
            onPressed: () => Navigator.of(context).pop((_name.text, _url.text)),
            child: const Text(kRssAdd),
          ),
        ],
      );
}
