/// 书源行：`source.list` / `source.get` 的那一条。
///
/// 字段名从真响应取（`tests/gateway/golden/source.list.json` 的 `field_names`，
/// 30 个键），视图真正读的四格照 `app/views/source/book_source_list_view.py:208-211`：
/// `bookSourceName` / `bookSourceUrl` / `bookSourceGroup` / `enabled`，
/// 外加搜索框要扫的备注 `bookSourceComment`（`book_source_list_controller.py:137-141`）。
/// 规则那一堆（`ruleSearch`/`ruleContent`/…）不进模型，留在 [raw] 里——
/// 编辑器那一屏（G2 之外）要的是原文，不是我们挑剩下的子集。
library;

/// URL 摘要的长度：flet 那屏 `_short_url` 的 48（`book_source_list_view.py:250-253`）。
/// 副标题是单行省略号，但"省略 URL 的头"和"省略尾"给人读的是两件事，所以照搬。
const int _urlShortenLimit = 48;

class SourceRecord {
  const SourceRecord({
    required this.url,
    required this.name,
    required this.group,
    required this.comment,
    required this.enabled,
    required this.loginUrl,
    required this.raw,
  });

  /// `bookSourceUrl`：今天它就是主键（删除/启停/改分组全按它寻源）。
  final String url;
  final String name;

  /// 逗号分隔的组名串（`"通常书源 📂,备用"`）。原样留着，切分走 [groups]。
  final String group;
  final String comment;
  final bool enabled;

  /// `loginUrl`：非空才谈"登录书源"（flet 那边按钮可见性就是这一格，
  /// `find_books_controller.py:124-126`）。
  final String loginUrl;

  /// 原始行：编辑器与"按原样存回去"（`source.save` 要整条 JSON）用它。
  final Map<String, Object?> raw;

  /// 行标题：书名没了就退回 URL（flet `book_source_list_view.py:208` 的 `or`）。
  String get title => name.isNotEmpty ? name : url;

  /// 副标题：**分组在前、URL 在后**，用 ` · ` 连，缺一段就少一段
  /// （`book_source_list_view.py:224`）。顺序是有出处的：那边就是 `group, _short_url(url)`。
  String get subtitle =>
      <String>[if (group.isNotEmpty) group, if (shortUrl.isNotEmpty) shortUrl]
          .join(' · ');

  String get shortUrl => url.length <= _urlShortenLimit
      ? url
      : '${url.substring(0, _urlShortenLimit - 1)}…';

  /// 逗号切开、去空白、丢空项（`book_source_list_controller.py:110-112`）。
  /// 分组下拉的选项表和"这条源属不属于选中那一组"都用它，两边必须是同一个切法。
  List<String> get groups => group
      .split(',')
      .map((part) => part.trim())
      .where((part) => part.isNotEmpty)
      .toList(growable: false);

  bool get hasGroup => groups.isNotEmpty;

  /// 搜索框扫的那一片：名称 / URL / 分组 / 备注，小写，空格连
  /// （`book_source_list_controller.py:137-141`）。
  String get haystack =>
      <String>[name, url, group, comment].join(' ').toLowerCase();

  factory SourceRecord.fromJson(Map<String, Object?> json) {
    final asMap = json.map((key, value) => MapEntry(key.toString(), value));
    return SourceRecord(
      url: '${asMap['bookSourceUrl'] ?? ''}',
      name: '${asMap['bookSourceName'] ?? ''}',
      group: '${asMap['bookSourceGroup'] ?? ''}',
      comment: '${asMap['bookSourceComment'] ?? ''}',
      // 键不在 = 启用：flet 那侧 `getattr(record, "enabled", True)` 就是这个默认
      // （book_source 模型里 enabled 默认 True，样本行里也确实在）。
      enabled: asMap['enabled'] == null ? true : asMap['enabled'] == true,
      loginUrl: '${asMap['loginUrl'] ?? ''}',
      raw: Map.unmodifiable(asMap),
    );
  }

  @override
  String toString() => 'SourceRecord($title, ${enabled ? '启用' : '停用'})';
}
