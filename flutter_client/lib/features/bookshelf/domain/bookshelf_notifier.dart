/// 书架那一屏的加载状态（§6.2：`AsyncNotifier`，不裸 `setState`）。
///
/// 只有一条规矩值得写在代码里：**刷新失败要留着旧数据**。
/// flet 那边的控制器在 `list_bookshelf` 抛异常时是 `logger.warning` 之后直接 return
/// （`bookshelf_controller.py::_load`），屏幕上留的还是上一批书——
/// 迁移期这条"看起来还有数据"的行为是用户能感觉到的差别，所以在这儿显式复刻，
/// 而不是让 Riverpod 的 `AsyncValue.error` 把整屏换成错误页。
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';
import '../../../core/rpc/envelope.dart';
import '../data/bookshelf_item.dart';

/// `retry: null` 这一条不是可选的：Riverpod 不给 provider 指定 retry 时用的是
/// `ProviderContainer.defaultRetry`——**指数退避、最多 10 次**。挂上去的表现是
/// 引擎一抖，客户端就背着用户把 `bookshelf.list` 连发 11 发，而且每一发都把
/// 错误屏换回转圈（用户看到的是"闪一下又闪回来"）。flet 那边的控制器失败时
/// 只 `logger.warning` 之后 return，一次都不重试，所以这里必须把默认策略关掉：
/// 要不要再来一发由用户按那颗"重试"决定。
final AsyncNotifierProvider<BookshelfNotifier, List<BookshelfItem>> bookshelfProvider =
    AsyncNotifierProvider<BookshelfNotifier, List<BookshelfItem>>(
  BookshelfNotifier.new,
  retry: (_, _) => null,
);

class BookshelfNotifier extends AsyncNotifier<List<BookshelfItem>> {
  final StreamController<EngineFailure> _failures =
      StreamController<EngineFailure>.broadcast();

  @override
  Future<List<BookshelfItem>> build() {
    // Riverpod 3 的 Notifier 没有 onDispose 可覆写；生命周期挂在这里。
    // 不关的话"刷新失败"那条横幅的流永远有人在线，测试里会留一个没散的 controller。
    ref.onDispose(() => unawaited(_failures.close()));
    return _fetch();
  }

  /// `async` 是有用途的：`engineRepositoryProvider` 没被 override 时，`ref.read` 那一句
  /// **同步**就抛了。不加 `async`，这一发就不是 Future 的错误而是直接打穿 `build()`，
  /// 表现是红屏而不是"书架加载失败"那一屏（契约面上"失败走 Future"那条规矩）。
  Future<List<BookshelfItem>> _fetch() async =>
      ref.read(engineRepositoryProvider).listBookshelf();

  /// 刷新失败的声音（横幅用）。错误不进 `value`，否则一次抖动就把书架换白屏。
  Stream<EngineFailure> get failures => _failures.stream;

  /// 手动刷新（下拉、刷新按钮，以及 G2 那批事件到了以后各调一次）。
  ///
  /// 这一发**不进 loading**：`state = AsyncLoading()` 会把 `value` 清掉，屏幕上那批书
  /// 立刻消失换成转圈——那是"首次加载"的形状，不是"刷新"的。刷新期间旧行留在原位，
  /// 失败只由 [_failures] 出声。
  Future<void> reload() async {
    final previous = state;
    try {
      state = AsyncValue<List<BookshelfItem>>.data(await _fetch());
    } on EngineFailure catch (exc) {
      _failures.add(exc);
      state = previous;
    } catch (exc) {
      // 仓库层之外的炸法（比如 handler 里的类型转换）也得有声音，不许静默变空架。
      _failures.add(EngineFailure(EngineFailureKind.engine, '$exc',
          action: 'bookshelf.list'));
      state = previous;
    }
  }
}
