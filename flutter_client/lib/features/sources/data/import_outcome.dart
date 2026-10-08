/// `import.text` / `import.url` 的回话：`{kind, count, errors, warnings, placeholder}`。
///
/// 键名取自真响应（`tests/gateway/golden/import.text.json` 的 `field_names`）。
/// [placeholder] 这一格是这份契约里最容易看错的一条：`kind` 不是 `book_source` 时
/// 服务侧**只登记说明、一行数据都不落**（`OnlineImportService.import_from_file`），
/// 于是 `count=0` 不是"导入失败"而是"这类东西今天就没有可落的形状"。
/// 界面必须把它显示成"未落地（占位）"，不许显示成"导入了 0 条"——
/// 那是记忆里 `aggregated-counters-hide-which-call` 的同一种糊法。
library;

class ImportOutcome {
  const ImportOutcome({
    required this.kind,
    required this.count,
    required this.errors,
    required this.warnings,
    required this.placeholder,
  });

  final String kind;
  final int count;
  final List<String> errors;
  final List<String> warnings;

  /// 真落了库还是只登记了说明。
  final bool placeholder;

  bool get ok => errors.isEmpty && !placeholder;

  factory ImportOutcome.fromJson(Map<String, Object?> json) => ImportOutcome(
        kind: '${json['kind'] ?? ''}',
        count: (json['count'] as num?)?.toInt() ?? 0,
        errors: _strings(json['errors']),
        warnings: _strings(json['warnings']),
        placeholder: json['placeholder'] == true,
      );

  static List<String> _strings(Object? value) => value is List
      ? value.map((e) => '$e').toList(growable: false)
      : const <String>[];
}
