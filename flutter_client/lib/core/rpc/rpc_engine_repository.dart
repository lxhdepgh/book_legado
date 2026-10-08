/// [EngineRepository] 的 RPC 实现：把契约面上那几个方法落到 §4.1 那一个动词上。
library;

import 'engine_repository.dart';
import 'envelope.dart';
import 'rpc_client.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';

class RpcEngineRepository implements EngineRepository {
  RpcEngineRepository(this.client);

  final RpcClient client;

  @override
  Future<GatewayInfo> hello() async {
    final data = await client.hello();
    return GatewayInfo.fromJson(Map<String, Object?>.from(data as Map));
  }

  @override
  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) async {
    final data = await client.call('bookshelf.list', payload: <String, Object?>{
      if (includeStaged) 'include_staged': true,
    });
    // 服务端把没有试读暂存的架子回成 `[]`，从来不会回 `null`；回 null 就是形状变了，
    // 那一发要响在这里，不能让调用方拿到空书架还觉得"今天没书"。
    final rows = switch (data) {
      List list => list,
      _ => throw EngineFailure(EngineFailureKind.transport,
          'bookshelf.list 的回话不是数组（收到 ${data.runtimeType}）',
          action: 'bookshelf.list'),
    };
    return rows
        .map((row) => BookshelfItem.fromJson(Map<String, Object?>.from(row as Map)))
        .toList(growable: false);
  }

  @override
  Future<void> shutdown({String reason = 'flutter client'}) async {
    await client.call('engine.shutdown',
        payload: <String, Object?>{'reason': reason}, singleFlight: false);
  }
}
