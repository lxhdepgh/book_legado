/// 数据层的唯一契约（设计 §6.3）：抽象仓库 + 两个实现。
///
/// 默认体一律抛 [EngineFailure]（kind=unavailable）而不是留 `abstract`——这一手
/// 同时解决两件事：新加的 action 还没接上时**编译不报错、运行时有声音**，
/// 以及"这个平台/这个引擎版本没有这项能力"的表达方式（真果鉴用它表 iOS 无后台下载）。
///
/// 这里 import 了一个 feature 的实体（`BookshelfItem`）：契约面在 core、
/// 实体在 feature，是 §6.1 那个 feature-first 目录换来的一条代价——
/// 反过来让 core 只认 `Map` 就是今天 RPC 出口那个形状，两侧都得自己解释一遍字段。
library;

import '../../features/bookshelf/data/bookshelf_item.dart';
import 'envelope.dart';

/// `engine.hello` 的返回（握手）。
class GatewayInfo {
  const GatewayInfo({
    required this.protocolVersion,
    required this.build,
    required this.capabilities,
    required this.schemaDigest,
    required this.actions,
    required this.pid,
  });

  final int protocolVersion;
  final String build;
  final List<String> capabilities;
  final String schemaDigest;
  final List<String> actions;
  final int pid;

  factory GatewayInfo.fromJson(Map<String, Object?> json) => GatewayInfo(
        protocolVersion: (json['protocol_version'] as num?)?.toInt() ?? 0,
        build: '${json['build'] ?? ''}',
        capabilities: (json['capabilities'] as List?)?.map((e) => '$e').toList() ?? const [],
        schemaDigest: '${json['schema_digest'] ?? ''}',
        actions: (json['actions'] as List?)?.map((e) => '$e').toList() ?? const [],
        pid: (json['pid'] as num?)?.toInt() ?? 0,
      );
}

/// 规矩：**失败以 Future 的错误交付，不同步抛**。
///
/// 这条不是风格问题。`RpcEngineRepository` 的失败一定在 Future 上（网络那一发本来就异步），
/// 而默认体若是 `=> throw`，调用方就会在同一句上分别撞上"同步炸"和"异步炸"两种形状——
/// `AsyncNotifier.build()` 只接得住后一种，前者会打穿界面（表现为整屏红屏而不是错误页）。
abstract class EngineRepository {
  const EngineRepository();

  Future<GatewayInfo> hello() async => throw _missing('engine.hello');

  Future<List<BookshelfItem>> listBookshelf({bool includeStaged = false}) async =>
      throw _missing('bookshelf.list');

  /// 客户端主动收摊（§4.6）：置服务端退出标志，父进程随即走 `hub.shutdown()`。
  Future<void> shutdown({String reason = 'flutter client'}) async =>
      throw _missing('engine.shutdown');

  EngineFailure _missing(String action) => EngineFailure(
        EngineFailureKind.unavailable,
        '当前环境不支持 $action',
        action: action,
      );
}

/// 一个方法都不覆写的那一发：没人 override 仓库时，界面拿到的就是它。
///
/// 为什么不直接让 provider 的体抛？因为 Riverpod 会把"provider 自己在错误状态"
/// 包成一条 74,654 字符的 `ProviderException` 塞进 `AsyncError.error`，
/// 于是错误屏上那一段详情就是一整屏的框架堆栈——用户要的那一句
/// ("当前环境不支持 bookshelf.list")反而看不见。挂在这里，失败按**每一次调用**
/// 交付，kind 与文案都还是我们自己那句话。
class UnwiredEngineRepository extends EngineRepository {
  const UnwiredEngineRepository();
}
