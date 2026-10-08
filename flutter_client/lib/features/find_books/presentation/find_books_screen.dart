/// 找书那一屏（对齐 flet `app/views/reading/find_books_view.py`）。
///
/// 三块内部页**共用一个 Scaffold**（flet 那边是一个 Stack 里三张页，切页就是换 visible）：
/// 书源列表 → 点一条源进分类浏览；顶部那颗搜索框 → 关键词搜索页。
/// 状态行只有一个槽，动作回报优先占位（都在 `domain/find_books_notifier.dart` 里算）。
///
/// 三处"这里没有"是**协议面定的**，不是漏接：
///
/// - **没有「登录书源」按钮**：`login_schema` / `submit_login` 还没上 action 表
///   （G2 那 31 发里没有这一条），放上去就是一颗点了不发任何请求的按钮。
/// - **没有「开始阅读」**：详情框那一格要的是 G3 的阅读器。flet 那边长按结果是试读，
///   这里同样不接——接了就是"动作发生在一个还不存在的地方"。
/// - **结果行没有封面**：`BookRow.coverUrl` 有值，但远程封面要走 `GET /media/{key}`
///   （§4.4），那条线在 G2 之后。书架那一屏已经立着同一个槽位，形状照它。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/events/event_listener.dart';
import '../../../core/events/event_names.dart';
import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../../sources/data/source_record.dart';
import '../data/book_intro.dart';
import '../data/book_row.dart';
import '../domain/find_books_notifier.dart';

class FindBooksScreen extends ConsumerStatefulWidget {
  const FindBooksScreen({super.key});

  @override
  ConsumerState<FindBooksScreen> createState() => _FindBooksScreenState();
}

class _FindBooksScreenState extends ConsumerState<FindBooksScreen> {
  final TextEditingController _keyword = TextEditingController();
  final ScrollController _browseScroll = ScrollController();
  final ScrollController _searchScroll = ScrollController();

  /// 事件流断了那句话只写在这里：它是"这一拍的故障"，不是这一屏的数据。
  String _streamError = '';

  @override
  void initState() {
    super.initState();
    // 触底翻页挂在这里而不是 ListView 的 onScroll 上：flet 那边是
    // `on_scroll=self._on_load_more`（每次滚动事件都发，靠控制器里的闸门挡着），
    // 这里用"到过头了才发一发"，闸门还是那两道（browseLoading / searchRunning）。
    _browseScroll.addListener(
        () => _maybeLoadMore(_browseScroll, () => ref.read(findBooksProvider.notifier).loadMoreBrowse()));
    _searchScroll.addListener(
        () => _maybeLoadMore(_searchScroll, () => ref.read(findBooksProvider.notifier).loadMoreSearch()));
  }

  void _maybeLoadMore(ScrollController controller, Future<void> Function() load) {
    if (!controller.hasClients) return;
    final position = controller.position;
    if (position.pixels >= position.maxScrollExtent - 200) {
      // 空列表时 maxScrollExtent 也是 0，`pixels >= 0` 会当场成立：
      // 那一发交给 notifier 的闸门挡（第一页还没打开时它自己 return）。
      load();
    }
  }

  @override
  void dispose() {
    _keyword.dispose();
    _browseScroll.dispose();
    _searchScroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(findBooksProvider);
    final notifier = ref.read(findBooksProvider.notifier);
    final view = state.value ?? FindBooksState.idle;

    listenToEngineEvents(
      ref,
      <String>[
        EngineEventNames.searchFinished,
        EngineEventNames.sourceChanged,
      ],
      (event) {
        if (event.name == EngineEventNames.searchFinished) {
          notifier.onSearchFinished(event);
        } else if (event.name == EngineEventNames.sourceChanged) {
          // 书源变了只换表，**不占状态行**（flet `_load_sources(report=False)`）。
          notifier.refreshCatalog();
        }
      },
      onStreamError: (error) => setState(() => _streamError = '事件流断开：$error'),
    );

    return Scaffold(
      body: Column(
        children: <Widget>[
          FailureBanner(stream: notifier.failures),
          Expanded(
            child: switch (view.page) {
              FindBooksPage.sources => _sourcesPage(notifier, view),
              FindBooksPage.browse => _browsePage(notifier, view),
              FindBooksPage.search => _searchPage(notifier, view),
            },
          ),
          StateBar(
            message: _streamError.isNotEmpty ? _streamError : view.status,
            state: state,
            failedText: '找书页加载失败',
            loadingText: '正在加载书源…',
          ),
        ],
      ),
    );
  }

  // ------------------------------------------------------------------ #
  // ① 书源列表
  // ------------------------------------------------------------------ #
  Widget _sourcesPage(FindBooksNotifier notifier, FindBooksState view) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('找书', style: Theme.of(context).textTheme.headlineMedium),
                const SizedBox(height: 8),
                _TappableBox(
                  key: const Key('findbooks.openSearch'),
                  onTap: () => {
                    setState(() => _streamError = ''),
                    notifier.openPage(FindBooksPage.search),
                  },
                  child: const Row(
                    children: <Widget>[
                      Icon(Icons.search, size: 18),
                      SizedBox(width: 8),
                      Expanded(child: Text('搜索书名或作者')),
                    ],
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: <Widget>[
                    Text('书源', style: Theme.of(context).textTheme.titleSmall),
                    IconButton(
                      key: const Key('findbooks.refresh'),
                      icon: const Icon(Icons.refresh),
                      tooltip: '刷新书源列表',
                      onPressed: notifier.reload,
                    ),
                  ],
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: ListView.builder(
              key: const Key('findbooks.sourceList'),
              itemCount: view.sources.length,
              itemBuilder: (context, index) => _SourceTile(
                row: view.sources[index],
                onOpen: notifier.openSource,
              ),
            ),
          ),
        ],
      );

  // ------------------------------------------------------------------ #
  // ② 分类浏览
  // ------------------------------------------------------------------ #
  Widget _browsePage(FindBooksNotifier notifier, FindBooksState view) {
    // 标题跟的是**当前分类**（flet `_open_category:169-172` 那句
    // `set_browse_context(source, categories[index].title, …)`），
    // 分类还没回来时是刚点开的那条源名。
    final title = view.categoryAt(view.categoryIndex)
        ? view.categories[view.categoryIndex].label
        : view.currentSourceName;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 2, 6, 2),
          child: Row(
            children: <Widget>[
              _BackToSources(notifier),
              Expanded(
                child: Text(title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.titleMedium),
              ),
            ],
          ),
        ),
        SizedBox(
          height: 40,
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 8),
            child: Row(
              children: <Widget>[
                for (var i = 0; i < view.categories.length; i++)
                  ChoiceChip(
                    // 空标题的分类退回「分类N」（`render_categories` 里那句 `t or f"分类{i}"`）：
                    // 一个不能点也读不出名字的签，比写着"分类3"的签更难用。
                    label: Text(view.categories[i].label.isEmpty
                        ? '分类${i + 1}'
                        : view.categories[i].label),
                    selected: i == view.categoryIndex,
                    onSelected: (_) => notifier.selectCategory(i),
                  ),
              ],
            ),
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: _BookList(
            scrollController: _browseScroll,
            listKey: const Key('findbooks.browseList'),
            rows: view.browseBooks,
            onOpen: _showDetail,
            busy: view.browseLoading,
          ),
        ),
      ],
    );
  }

  // ------------------------------------------------------------------ #
  // ③ 关键词搜索
  // ------------------------------------------------------------------ #
  Widget _searchPage(FindBooksNotifier notifier, FindBooksState view) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Padding(
            padding: const EdgeInsets.fromLTRB(4, 2, 8, 2),
            child: Row(
              children: <Widget>[
                _BackToSources(notifier),
                Expanded(
                  child: TextField(
                    key: const Key('findbooks.keyword'),
                    controller: _keyword,
                    decoration: const InputDecoration(labelText: '书名或关键词'),
                    onSubmitted: (value) => notifier.search(value),
                  ),
                ),
                const SizedBox(width: 4),
                FilledButton.icon(
                  key: const Key('findbooks.submit'),
                  onPressed: () => notifier.search(_keyword.text),
                  icon: const Icon(Icons.search),
                  label: const Text('搜索'),
                ),
                // 「停止」只在跑着的时候存在（flet 那边是 visible=False 起步）。
                if (view.searchRunning)
                  TextButton.icon(
                    key: const Key('findbooks.stop'),
                    onPressed: () => notifier.stopSearch(),
                    icon: const Icon(Icons.stop),
                    label: const Text('停止'),
                  ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
            child: _TappableBox(
              key: const Key('findbooks.scope'),
              onTap: () => _pickScope(notifier, view.scopeValue),
              child: Row(
                children: <Widget>[
                  const Icon(Icons.tune, size: 16),
                  const SizedBox(width: 6),
                  const Text('搜索范围', style: TextStyle(fontSize: 12)),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(view.scopeLabel,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 12)),
                  ),
                  const Icon(Icons.expand_more, size: 16),
                ],
              ),
            ),
          ),
          // 进度条：flet 那边是 `ft.ProgressBar(value=0)`（不确定态）+ visible=running。
          if (view.searchRunning) const LinearProgressIndicator(minHeight: 3),
          Expanded(
            child: _BookList(
              scrollController: _searchScroll,
              listKey: const Key('findbooks.results'),
              rows: view.searchResults,
              onOpen: _showDetail,
            ),
          ),
        ],
      );

  // ------------------------------------------------------------------ #
  // 弹窗
  // ------------------------------------------------------------------ #
  void _showDetail(BookRow row) {
    final notifier = ref.read(findBooksProvider.notifier);
    final intro = introForDisplay(row.intro);
    showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(row.name.isEmpty ? '书籍详情' : row.name),
        content: SizedBox(
          width: 420,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              if (row.author.isNotEmpty) Text(row.author),
              if (row.originName.isNotEmpty)
                Text(row.originName, style: const TextStyle(fontSize: 12)),
              Text(intro.isEmpty ? '暂无简介' : intro,
                  maxLines: 8, overflow: TextOverflow.ellipsis),
            ],
          ),
        ),
        actions: <Widget>[
          TextButton(
            key: const Key('findbooks.detail.close'),
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('关闭'),
          ),
          TextButton.icon(
            key: const Key('findbooks.detail.shelve'),
            onPressed: () async {
              await notifier.shelve(row);
              if (dialogContext.mounted) Navigator.pop(dialogContext);
            },
            icon: const Icon(Icons.bookmark_add),
            label: const Text('加入书架'),
          ),
        ],
      ),
    );
  }

  /// 搜索范围弹窗：打开前按当前库现刷一次表（`open_scope` 那边表在控制器手里，
  /// 这里是同一发 `search.scopes`）。选中的那条只改 `scopeValue`，不写状态行。
  Future<void> _pickScope(FindBooksNotifier notifier, String selected) async {
    final rows = await notifier.refreshScopes();
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('搜索范围'),
        content: SizedBox(
          width: 360,
          child: ListView(
            shrinkWrap: true,
            children: <Widget>[
              for (final row in rows)
                ListTile(
                  key: ValueKey('findbooks.scope.${row.value}'),
                  dense: true,
                  leading: Icon(row.value == selected
                      ? Icons.radio_button_checked
                      : Icons.radio_button_unchecked),
                  title: Text(row.rowLabel),
                  onTap: () {
                    notifier.selectScope(row);
                    Navigator.pop(dialogContext);
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 返回书源列表那颗按钮（浏览页与搜索页共用，tooltip 文案同源）。
class _BackToSources extends StatelessWidget {
  const _BackToSources(this.notifier);

  final FindBooksNotifier notifier;

  @override
  Widget build(BuildContext context) => IconButton(
        key: const Key('findbooks.back'),
        icon: const Icon(Icons.arrow_back),
        tooltip: '返回书源列表',
        onPressed: () => notifier.openPage(FindBooksPage.sources),
      );
}

/// 书源行。
///
/// 标题的兜底与书架那一屏**不同**：这里是 `(未命名)`（`find_books_view.py:171`），
/// 那边是退回 URL。两屏读的是同一格 `bookSourceName`，但这一屏点的是"进哪条源"，
/// 一长串 URL 当标题会把整行读成"这是一条链接"，所以照 flet 各写各的。
class _SourceTile extends StatelessWidget {
  const _SourceTile({required this.row, required this.onOpen});

  final SourceRecord row;
  final void Function(SourceRecord) onOpen;

  @override
  Widget build(BuildContext context) => ListTile(
        leading: const Icon(Icons.language),
        title: Text(row.name.isEmpty ? '(未命名)' : row.name,
            maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(row.url,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 11)),
        onTap: () => onOpen(row),
      );
}

/// 结果行（搜索页与浏览页共用一个形状，`_book_row` 那一发）。
class _BookTile extends StatelessWidget {
  const _BookTile({required this.row, required this.onOpen, super.key});

  final BookRow row;
  final void Function(BookRow) onOpen;

  @override
  Widget build(BuildContext context) => ListTile(
        leading: const Icon(Icons.book_outlined),
        title: Text(row.name, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: row.subtitle.isEmpty
            ? null
            : Text(row.subtitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 11)),
        onTap: () => onOpen(row),
      );
}

/// 结果流：空列表画那一句「暂无结果」（flet `render_results` 末尾那一发），
/// 而不是留一片白——白屏读起来像"还没搜"，那句读起来像"搜了，没有"。
class _BookList extends StatelessWidget {
  const _BookList({
    required this.rows,
    required this.onOpen,
    required this.scrollController,
    required this.listKey,
    this.busy = false,
  });

  final List<BookRow> rows;
  final void Function(BookRow) onOpen;
  final ScrollController scrollController;
  final Key listKey;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    if (rows.isEmpty) {
      return busy
          ? const Center(child: CircularProgressIndicator())
          : Center(
              child: Text('暂无结果',
                  style: TextStyle(
                      color: Theme.of(context).colorScheme.onSurfaceVariant)),
            );
    }
    return ListView.builder(
      key: listKey,
      controller: scrollController,
      itemCount: rows.length,
      itemBuilder: (context, index) => _BookTile(
        // 键用 origin|url：同一次搜索里同一本书可能从多个来源各回一次，
        // 下标键会把上一行的书名带到新落下来的行上。
        key: ValueKey(rows[index].identityKey),
        row: rows[index],
        onOpen: onOpen,
      ),
    );
  }
}

/// 那一颗"长得像输入框"的可点盒（搜索入口与搜索范围共用一种形状：
/// 圆角 + `surfaceContainerHighest` 底，flet 那边是同一个 `ft.Container` 写法）。
class _TappableBox extends StatelessWidget {
  const _TappableBox({
    required this.onTap,
    required this.child,
    super.key,
  });

  final VoidCallback? onTap;
  final Widget child;

  @override
  Widget build(BuildContext context) => Material(
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 9, 12, 9),
            child: DefaultTextStyle.merge(
              style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurfaceVariant),
              child: child,
            ),
          ),
        ),
      );
}
