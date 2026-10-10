/// 更新检查域的数据模型（对应 Python UpdateService.UpdateInfo）。
library;

/// 一次可用更新的纯数据快照（视图层据此弹窗）。
class UpdateInfo {
  const UpdateInfo({
    required this.version,
    required this.notes,
    required this.url,
    this.size = 0,
    this.digest = '',
  });

  factory UpdateInfo.fromJson(Map<String, Object?> json) => UpdateInfo(
        version: '${json['version'] ?? ''}',
        notes: '${json['notes'] ?? ''}',
        url: '${json['url'] ?? ''}',
        size: _asInt(json['size']),
        digest: '${json['digest'] ?? ''}',
      );

  final String version;
  final String notes;
  final String url; // 安装包下载地址
  final int size;
  final String digest; // 期望 sha256（hex），空串表示无可校验摘要

  Map<String, Object?> toJson() => <String, Object?>{
        'version': version,
        'notes': notes,
        'url': url,
        'size': size,
        'digest': digest,
      };

  static int _asInt(Object? value) => switch (value) {
        final int n => n,
        final String s => int.tryParse(s) ?? 0,
        _ => 0,
      };
}
