/// 「刷新失败但旧数据还在」那条横幅（四屏共用一份）。
///
/// 为什么在 core：这一条是**跨屏的同一条规矩**（错误不进 `AsyncValue`，所以谁加载谁
/// 自己出声），四屏各抄一份就会飘成四种文案——而"刷新失败"这句话在 flet 那边
/// 也只有一处写法。
///
/// 订阅必须挂在 `initState`：`late final`/build 里首读才构造的监听器，
/// 在 widget 测试的紧约束下照样能收到，但真机上第一次 build 之后流里已经过去的
/// 那条**永远不会重放**（记忆 `dart-late-final-listener-never-registered`）。
library;

import 'dart:async';

import 'package:flutter/material.dart';

import '../rpc/envelope.dart';

class FailureBanner extends StatefulWidget {
  const FailureBanner({required this.stream, super.key});

  final Stream<EngineFailure> stream;

  @override
  State<FailureBanner> createState() => _FailureBannerState();
}

class _FailureBannerState extends State<FailureBanner> {
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
  void didUpdateWidget(FailureBanner old) {
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
