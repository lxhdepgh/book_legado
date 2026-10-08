/// 依赖注入的落点（§6.2：异步数据一律 `AsyncNotifierProvider`，可测 = override）。
///
/// [engineRepositoryProvider] 的默认体是 [UnwiredEngineRepository]：一个方法都没覆写，
/// 于是每一次调用都落回契约面的 throw 默认体。
/// 谁忘了在 `ProviderScope` 上 override，看到的是一屏"当前环境不支持 bookshelf.list"，
/// 而不是一屏 `null` 装成的"书架是空的"——**空书架和没有引擎必须长得不一样**。
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../rpc/engine_repository.dart';

final Provider<EngineRepository> engineRepositoryProvider =
    Provider<EngineRepository>((ref) => const UnwiredEngineRepository());
