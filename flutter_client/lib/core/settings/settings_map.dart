/// `settings.read` 的回话：一张**只含设过的键**的表。
///
/// 这条形状是服务端定的（`SettingsService.read_keys`：没设过的键不出现在返回里），
/// 所以"键不在"与"键设成了 null"是两件事。把它们混成 `null` 的后果很具体：
/// 阅读器那一屏会把"用户从没设过字号"读成"字号是 0"，
/// 而不是去用它自己的默认值。
///
/// 键名字面量的出处见 `setting_keys.dart`：那一摞有跨语言守卫，这一份只负责形状。
library;

class SettingsMap {
  const SettingsMap(this.values);

  final Map<String, Object?> values;

  static const SettingsMap empty = SettingsMap(<String, Object?>{});

  /// 这个键**设过**吗（值可以是 null）。
  bool isSet(String key) => values.containsKey(key);

  Object? operator [](String key) => values[key];

  /// 取数并回落到调用方给的默认值：只有"没设过"才回落。
  /// 设过而值是 null **不回落**——那是数据本身，不是缺字段。
  double? numOrNull(String key) {
    final value = values[key];
    return value is num ? value.toDouble() : null;
  }

  int? intOrNull(String key) {
    final value = values[key];
    return value is num ? value.toInt() : null;
  }

  bool? boolOrNull(String key) {
    final value = values[key];
    return value is bool ? value : null;
  }

  String? stringOrNull(String key) {
    final value = values[key];
    return value == null ? null : '$value';
  }

  Map<String, Object?>? mapOrNull(String key) {
    final value = values[key];
    return value is Map ? Map<String, Object?>.from(value) : null;
  }

  factory SettingsMap.fromJson(Object? data) => data is Map
      ? SettingsMap(data.map((key, value) => MapEntry('$key', value)))
      : SettingsMap.empty;
}
