/// 依赖注入的落点（§6.2：异步数据一律 `AsyncNotifierProvider`，可测 = override）。
///
/// [engineRepositoryProvider] 的默认体是 [UnwiredEngineRepository]：一个方法都没覆写，
/// 于是每一次调用都落回契约面的 throw 默认体。
/// 谁忘了在 `ProviderScope` 上 override，看到的是一屏"当前环境不支持 bookshelf.list"，
/// 而不是一屏 `null` 装成的"书架是空的"——**空书架和没有引擎必须长得不一样**。
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../events/engine_event.dart';
import '../rpc/engine_repository.dart';

final Provider<EngineRepository> engineRepositoryProvider =
    Provider<EngineRepository>((ref) => const UnwiredEngineRepository());

/// 事件流（§4.3）：一根流供所有屏听，别每屏各连一次 `/ws`。
///
/// `retry: null` 与书架那条同源（记忆 `riverpod-provider-default-retry`）：
/// 默认策略会把"这一条流断了"变成背靠背重连 11 次，而**重连本来是 [EngineRepository.events]
/// 那一侧的活**（`EngineEventClient` 自带退避，上界压在服务端空闲自杀之下）。
/// 两层都重试 = 两层互相踩，表现是断线后一阵狂连。
final StreamProvider<EngineEvent> engineEventsProvider =
    StreamProvider<EngineEvent>(
  (ref) => ref.watch(engineRepositoryProvider).events(),
  retry: (_, _) => null,
);
