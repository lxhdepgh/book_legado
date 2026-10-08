/// `source.audit` 与 `source.save` 的回话形状。
///
/// 两边共用 [AuditReport]：`source.save` 的 `data.audit` 就是审计报告原样嵌进去
/// （见 `tests/gateway/golden/source.save.json`），另写一份就是同一事实两处写法。
///
/// 键名取自真响应（`source.audit.json` 的 `field_names` 是那 10 个键），
/// 类型照 `app/services/sources/compat_audit_service.py:48-56` 的 `list[str]` / `str`。
library;

/// 一份缺口清单。八列都是"空即无缺口"，[isClean] 那条口径与 Python 侧
/// `AuditReport.is_clean`（`:64-69`）一致：**不含 suggestions**——建议是给人看的话，
/// 不是缺口，把它算进"干净"会让每一条建议都变成"这条源有问题"。
class AuditReport {
  const AuditReport({
    required this.sourceId,
    required this.missingJavaApis,
    required this.missingObjectApis,
    required this.unknownFields,
    required this.unsupportedPseudos,
    required this.jsSyntaxErrors,
    required this.regexErrors,
    required this.ruleSyntaxErrors,
    required this.suggestions,
    required this.notAudited,
  });

  final String sourceId;
  final List<String> missingJavaApis;
  final List<String> missingObjectApis;
  final List<String> unknownFields;
  final List<String> unsupportedPseudos;
  final List<String> jsSyntaxErrors;
  final List<String> regexErrors;
  final List<String> ruleSyntaxErrors;
  final List<String> suggestions;

  /// 非空 = **这条源没被真正审过**（原因写在这里，比如缺规则）。
  /// 它和"审完了、零缺口"是两件事，界面不许把两者都渲染成"通过"。
  final String notAudited;

  bool get isClean => notAudited.isEmpty && _gaps.isEmpty;

  /// 有缺口的列（列名 → 内容），顺序照 Python dataclass 的字段序。
  /// 视图按它逐条列，空列不出现——于是"审过了、没问题"是**没有行**，而不是八行"0"。
  Map<String, List<String>> get gaps => <String, List<String>>{
        '缺失 Java API': missingJavaApis,
        '缺失对象 API': missingObjectApis,
        '未知字段': unknownFields,
        '不支持的伪类': unsupportedPseudos,
        'JS 语法错误': jsSyntaxErrors,
        '正则错误': regexErrors,
        '规则语法错误': ruleSyntaxErrors,
      };

  Map<String, List<String>> get _gaps =>
      gaps..removeWhere((_, rows) => rows.isEmpty);

  factory AuditReport.fromJson(Map<String, Object?> json) => AuditReport(
        sourceId: '${json['source_id'] ?? ''}',
        missingJavaApis: _strings(json['missing_java_apis']),
        missingObjectApis: _strings(json['missing_object_apis']),
        unknownFields: _strings(json['unknown_fields']),
        unsupportedPseudos: _strings(json['unsupported_pseudos']),
        jsSyntaxErrors: _strings(json['js_syntax_errors']),
        regexErrors: _strings(json['regex_errors']),
        ruleSyntaxErrors: _strings(json['rule_syntax_errors']),
        suggestions: _strings(json['suggestions']),
        notAudited: '${json['not_audited'] ?? ''}',
      );

  static List<String> _strings(Object? value) => value is List
      ? value.map((e) => '$e').toList(growable: false)
      : const <String>[];
}

/// `source.save` 的回话：`{saved, source_id, errors, audit}`。
///
/// `saved=false` 时 `errors` 才是原因；**不抛异常**那一手是服务端定的
/// （校验/落盘/发事件在 `SourceService.save_source` 里一次办完），
/// 客户端把"没存上"渲染成错误条，不是崩溃。
class SaveOutcome {
  const SaveOutcome({
    required this.saved,
    required this.sourceId,
    required this.errors,
    required this.audit,
  });

  final bool saved;
  final String sourceId;
  final List<String> errors;
  final AuditReport audit;

  factory SaveOutcome.fromJson(Map<String, Object?> json) {
    final audit = json['audit'];
    return SaveOutcome(
      saved: json['saved'] == true,
      sourceId: '${json['source_id'] ?? ''}',
      errors: AuditReport._strings(json['errors']),
      audit: audit is Map
          ? AuditReport.fromJson(Map<String, Object?>.from(audit))
          : const AuditReport(
              sourceId: '',
              missingJavaApis: [],
              missingObjectApis: [],
              unknownFields: [],
              unsupportedPseudos: [],
              jsSyntaxErrors: [],
              regexErrors: [],
              ruleSyntaxErrors: [],
              suggestions: [],
              notAudited: '这条回话里没有审计报告'),
    );
  }
}
