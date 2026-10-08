/// 测试替身：拿黄金样本（或手搓的 list）喂界面，**不起进程、不发一个字节**。
///
/// 界面面的所有 widget 测试都从这里进（§10：`FakeEngineRepository` 喂黄金样本）。
/// 它同时是"这一屏在没有引擎时该长什么样"的落点：[failure] 非空时每个方法都抛那条，
/// 于是"引擎没起来"那一屏也有人测，而不是只有人肉看过一眼。
library;

import 'engine_repository.dart';
import 'envelope.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';

class FakeEngineRepository implements EngineRepository {
  FakeEngineRepository({
    this.bookshelf = const [],
    this.info = const GatewayInfo(
      protocolVersion: 1,
      build: 'fake-0.0.0',
      capabilities: ['rpc', 'events'],
      schemaDigest: 'deadbeefcafe',
      actions: ['engine.hello', 'bookshelf.list'],
      pid: 0,
    ),
    this.failure,
    this.delay = Duration.zero,
  });

  /// 从**黄金样本里的原始行**造替身（G1 判据"同书单比对"的入口——喂进去的是真
  /// `--serve` 进程吐出的那批 dict，不是手搓样例；手搓样例只会证明我自己写得对）。
  factory FakeEngineRepository.fromRows(List<Object?> rows,
          {Duration delay = Duration.zero}) =>
      FakeEngineRepository(
        bookshelf: rows
            .map((row) => BookshelfItem.fromJson(
                Map<String, Object?>.from(row as Map)))
            .toList(growable: false),
        delay: delay,
      );

  List<BookshelfItem> bookshelf;
  GatewayInfo info;

  /// 非空 = 这台"没有这个能力"：每个方法都抛它。
  /// 可写：「刷新失败要留着旧数据」那条规矩要在同一发测试里先成功后失败。
  EngineFailure? failure;

  /// 让加载态可测（`pumpAndSettle` 之前数据还没回）。
  final Duration delay;

  /// 打过几发就算几发——**失败的那几发也要数**。
  /// 早先这三个计数器是在成功分支里自增的，于是"失败后不许自动重试"那条测试
  /// 数到的是 0：仪器读 0 的时候分不清"没重试"和"没记账"，那条断言就是空的。
  int listBookshelfCalls = 0;
  int helloCalls = 0;
  int shutdownCalls = 0;
  String? lastShutdownReason;

  Future<T> _guard<T>(T Function() body, String action) async {
    final thrown = failure;
    if (thrown != null) {
      await Future<void>.delayed(delay);
      throw EngineFailure(thrown.kind, thrown.message, action: action);
    }
    final value = body();
    if (delay > Duration.zero) await Future<void>.delayed(delay);
    return value;
  }

  @override
  Future<GatewayInfo> hello() {
    helloCalls++;
    return _guard(() => info, 'engine.hello');
  }

  @override
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) {
    listBookshelfCalls++;
    return _guard(() => bookshelf, 'bookshelf.list');
  }

  @override
  Future<void> shutdown({String reason = 'flutter client'}) {
    shutdownCalls++;
    lastShutdownReason = reason;
    return _guard(() {}, 'engine.shutdown');
  }
}
