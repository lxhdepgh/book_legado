/// 书架屏：行内容与 flet 那屏同源（`bookshelf_view.py::_row` 的 list 布局）。
///
/// 三处"看得见的事实"是 G1 判据的比对照（同书单两边的行数与文本必须一致）：
/// 行标题 = `name`；行副标题 = `作者 · 读到 章名`（缺一段就少一段）；
/// 状态行 = `书架共 N 本`。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../app/router.dart';
import '../../../core/rpc/envelope.dart';
import '../../../core/widgets/failure_banner.dart';
import '../../../core/widgets/state_bar.dart';
import '../../reader/domain/reader_controller.dart';
import '../data/bookshelf_item.dart';
import '../domain/bookshelf_notifier.dart';
import 'source_switch_dialog.dart';

class BookshelfScreen extends ConsumerWidget {
  const BookshelfScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(bookshelfProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('书架'),
        actions: <Widget>[
          IconButton(
            key: const Key('bookshelf.refresh'),
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: state.isLoading ? null : () => ref.read(bookshelfProvider.notifier).reload(),
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          FailureBanner(stream: ref.read(bookshelfProvider.notifier).failures),
          Expanded(child: _body(ref, state)),
          _ShelfStatus(state: state),
        ],
      ),
    );
  }

  Widget _body(WidgetRef ref, AsyncValue<List<BookshelfItem>> state) {
    return switch (state) {
      AsyncData(value: final items) when items.isEmpty => const _EmptyShelf(),
      AsyncData(value: final items) => RefreshIndicator(
          onRefresh: () => ref.read(bookshelfProvider.notifier).reload(),
          child: ListView.separated(
            itemCount: items.length,
            separatorBuilder: (_, _) => const Divider(height: 1),
            // 键用 url 而不是下标：flet 那边同一本书的行会被"换源"原地改写，
            // 下标键在 Flutter 侧会把旧的封面/进度带到新书上。
            itemBuilder: (context, index) =>
                BookshelfRow(key: ValueKey(items[index].identityKey), item: items[index]),
          ),
        ),
      AsyncError(error: final error, stackTrace: final stack) => _LoadFailure(
          error: error, stack: stack, onRetry: () => ref.read(bookshelfProvider.notifier).reload()),
      _ => const Center(child: CircularProgressIndicator()),
    };
  }
}

class BookshelfRow extends StatelessWidget {
  const BookshelfRow({required this.item, super.key});

  final BookshelfItem item;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: _Cover(url: item.coverUrl, name: item.name),
      title: Text(item.name, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: item.subtitle.isEmpty
          ? null
          : Text(item.subtitle, maxLines: 1, overflow: TextOverflow.ellipsis),
      // 换源入口（flet 那边在行管理菜单里，`build_manage_dialog` 的第一项）。
      // **本地书不给这一格**：它没有规则源，"换一个源"无从谈起，而真点下去
      // `switch.apply` 会把 `origin` 从 `localsource://local_book` 改写成某个网络源
      // ——本地文件那条绑定就没了。flet 用控制器注入的 `record["local"]` 收起它，
      // 协议面上没有那一格，所以这边读的是同一个常量的精确相等（`isLocal`，
      // 由跨语言守卫钉住那个字面量）。
      trailing: item.isLocal
          ? null
          : IconButton(
              key: const Key('bookshelf.switch'),
              icon: const Icon(Icons.swap_horiz),
              tooltip: '换源',
              onPressed: () => openSourceSwitch(context,
                  record: item.raw, name: item.name),
            ),
      // 点一行就是开读（§9 门 G3「从书架开一本书」）。`book` 传的是**行快照原样那份字典**：
      // 服务端 `as_book_item` 认字典，界面不许在这里重拼字段（少一格就是少一格，
      // 而 `reader.open` 的回话会告诉你它认成了什么）。
      onTap: () => openReaderBook(
        context,
        ReaderArgs(sourceUrl: item.sourceUrl, book: item.raw),
      ),
    );
  }
}

/// 封面槽位。远程封面按 §4.4 走 `GET /media/{key}`，那是 G2 之后的活；
/// 现在这一格只保证**槽位在、尺寸对**（40×56，与 flet 那屏一致），空槽是字母占位。
class _Cover extends StatelessWidget {
  const _Cover({required this.url, required this.name});

  final String url;
  final String name;

  @override
  Widget build(BuildContext context) {
    if (url.isEmpty) return _placeholder(context);
    return ClipRRect(
      borderRadius: BorderRadius.circular(2),
      child: Image.network(
        url,
        width: 40,
        height: 56,
        fit: BoxFit.cover,
        errorBuilder: (_, _, _) => _placeholder(context),
        loadingBuilder: (context, child, progress) =>
            progress == null ? child : _placeholder(context),
      ),
    );
  }

  Widget _placeholder(BuildContext context) => Container(
        width: 40,
        height: 56,
        alignment: Alignment.center,
        color: Theme.of(context).colorScheme.surfaceContainerHighest,
        child: Text(name.isEmpty ? '？' : name.characters.first),
      );
}

class _EmptyShelf extends StatelessWidget {
  const _EmptyShelf();

  @override
  Widget build(BuildContext context) => const Center(
        child: Text('书架是空的', semanticsLabel: '书架是空的'),
      );
}

/// 状态行：与 flet 的 `set_status_text(f"书架共 {N} 本")` 同文案。
/// 加载失败时那一条**不许**报"书架共 0 本"——那会把"没拿到数据"说成"没有书"。
/// 这一格四屏共用一份实现（`core/widgets/state_bar.dart`），这里只给那三句文案。
class _ShelfStatus extends StatelessWidget {
  const _ShelfStatus({required this.state});

  final AsyncValue<List<BookshelfItem>> state;

  @override
  Widget build(BuildContext context) {
    final items = state.value;
    return StateBar(
      message: '',
      state: state,
      failedText: '书架加载失败',
      loadingText: '正在加载书架…',
      dataText: items == null ? null : '书架共 ${items.length} 本',
    );
  }
}

class _LoadFailure extends StatelessWidget {
  const _LoadFailure({required this.error, required this.stack, required this.onRetry});

  final Object error;
  final StackTrace stack;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final reported = error;
    final failure = reported is EngineFailure ? reported : null;
    final kind = failure?.kind.wireName ?? 'engine';
    final retryable = failure == null || failure.isTransient;
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Text('书架加载失败（$kind）',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            // 详情那一段必须夹住：今天最粗的一条抛法（没注入仓库时框架包出来的那条）
            // 是 74,654 个字符，直接画进 Column 就是把一屏堆栈顶出边界——
            // 用户看到的是黑黄条纹，而不是"为什么没数据"。
            Text(_clip('$error'),
                textAlign: TextAlign.center,
                maxLines: 6,
                overflow: TextOverflow.ellipsis),
            const SizedBox(height: 12),
            if (retryable)
              FilledButton.icon(
                key: const Key('bookshelf.retry'),
                onPressed: () => onRetry(),
                icon: const Icon(Icons.refresh),
                label: const Text('重试'),
              ),
          ],
        ),
      ),
    );
  }

  static String _clip(String text) =>
      text.length <= 400 ? text : '${text.substring(0, 400)}…';
}
