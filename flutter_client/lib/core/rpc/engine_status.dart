/// `engine.status` 的一行：一个服务的状态快照。
///
/// 键名取自真响应（`tests/gateway/golden/engine.status.json` 的 `field_names`
/// = `service_id` / `state` / `last_error` / `started_at` / `updated_at` / `progress`，
/// 样本里是 34 行 = 无头装配的 34 个服务）。
///
/// [state] 原样留着字符串，不折成枚举：状态机的词汇表在
/// `app/core/lifecycle.py:23-32` 的 `ServiceState` 上（七个值，注释明写"字符串值不得改动"），
/// 客户端自己再列一遍就是第二份写法（§4.5 点名的债）。这里只回答界面要的那一句
/// ——"要不要给这一行标红"。
library;

class ServiceStatus {
  const ServiceStatus({
    required this.serviceId,
    required this.state,
    required this.lastError,
    required this.startedAt,
    required this.updatedAt,
    required this.progress,
  });

  final String serviceId;
  final String state;

  /// 出错时才有内容；没出错是 null（**不是**空串）。
  final String? lastError;

  /// 时间戳：`time.time()` 的浮点秒，null = 服务没记。
  final double? startedAt;
  final double? updatedAt;

  /// 进度那一格由服务自己定形状（有的给 0..1，有的给 null）。
  final Object? progress;

  /// 那台服务挂了。状态机里没有 `FAILED` 这一格：启动/恢复失败进 `SUSPENDED`
  /// 并记 `last_error`（`lifecycle.py:98`、`:133`），所以标红的条件是"停在 SUSPENDED
  /// 且带着错误"——光看状态名会把"人为挂起"（暂停服务）也标成坏了。
  bool get isFailed => state == 'SUSPENDED' && (lastError?.isNotEmpty ?? false);

  factory ServiceStatus.fromJson(Map<String, Object?> json) {
    double? asDouble(Object? value) => value is num ? value.toDouble() : null;
    return ServiceStatus(
      serviceId: '${json['service_id'] ?? ''}',
      state: '${json['state'] ?? ''}',
      lastError: json['last_error'] == null ? null : '${json['last_error']}',
      startedAt: asDouble(json['started_at']),
      updatedAt: asDouble(json['updated_at']),
      progress: json['progress'],
    );
  }

  @override
  String toString() => 'ServiceStatus($serviceId, $state)';
}
