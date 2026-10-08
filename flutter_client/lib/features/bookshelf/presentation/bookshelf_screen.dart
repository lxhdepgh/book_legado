/// 书架屏：行内容与 flet 那屏同源（`bookshelf_view.py::_row` 的 list 布局）。
///
/// 三处"看得见的事实"是 G1 判据的比对照（同书单两边的行数与文本必须一致）：
/// 行标题 = `name`；行副标题 = `作者 · 读到 章名`（缺一段就少一段）；
/// 状态行 = `书架共 N 本`。
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/rpc/envelope.dart';
import '../data/bookshelf_item.dart';
import '../domain/bookshelf_notifier.dart';

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
          _FailureBanner(stream: ref.read(bookshelfProvider.notifier).failures),
          Expanded(child: _body(ref, state)),
          _StatusBar(state: state),
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
      // G1 还没有"开读"这条线（§9 的 G3 阅读器）。占位不行——占位会被当成已接上。
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
class _StatusBar extends StatelessWidget {
  const _StatusBar({required this.state});

  final AsyncValue<List<BookshelfItem>> state;

  @override
  Widget build(BuildContext context) {
    final items = state.value;
    final text = switch (state) {
      AsyncError() => '书架加载失败',
      _ when items == null => '正在加载书架…',
      _ => '书架共 ${items.length} 本',
    };
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Text(text, style: Theme.of(context).textTheme.bodySmall),
      ),
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

/// 刷新失败的那条横幅：错误不进 state，所以这里自己收流。
class _FailureBanner extends StatefulWidget {
  const _FailureBanner({required this.stream});

  final Stream<EngineFailure> stream;

  @override
  State<_FailureBanner> createState() => _FailureBannerState();
}

class _FailureBannerState extends State<_FailureBanner> {
  EngineFailure? _last;
  StreamSubscription<EngineFailure>? _sub;

  @override
  void initState() {
    super.initState();
    _listen();
  }

  void _listen() {
    _sub = widget.stream.listen((failure) {
      if (mounted) setState(() => _last = failure);
    });
  }

  @override
  void didUpdateWidget(_FailureBanner old) {
    super.didUpdateWidget(old);
    if (!identical(old.stream, widget.stream)) {
      _sub?.cancel();
      _listen();
    }
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final failure = _last;
    if (failure == null) return const SizedBox.shrink();
    return Material(
      color: Theme.of(context).colorScheme.errorContainer,
      child: ListTile(
        dense: true,
        title: Text('刷新失败：${failure.message}'),
        trailing: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => setState(() => _last = null),
        ),
      ),
    );
  }
}
