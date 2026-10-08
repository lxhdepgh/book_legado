/// 简介标记的解码（对照 `app/services/engine/rule_engine.py` 的 `display_intro`）。
///
/// 这条不是可选的美化。记忆里 `intro-render-contract` 说的就是它：带
/// `<usehtml>`/`<md>`/`<useweb>` 标记的载荷**按原文存**，屏上直接画那串原文，
/// 用户读到的是半页 CSS 或一整段 `<div>`。flet 详情框走的是
/// `display_intro` → 只有 `text` 那一类上屏，其余换成一句实话。这里同一份判断。
///
/// 两份实现的等价由 `test/book_intro_test.dart` 钉：那张用例表的期望值是从 Python
/// `display_intro` 现跑出来的（不是手搓的），Dart 侧逐条比。
/// 两处**已实测**的语言差异写在这里，免得下一个人当 bug 修：
///
/// - `<span/>` 这类自闭合的非块级标签**不剥**：通用那条 `</?[a-zA-Z]+(?=[ >])` 要求
///   标签名后面跟空格或 `>`，斜杠不算。块级的那条用 `[^>]*`，所以 `<p/>` 是剥的。
/// - 只有**开标记、没有闭合标记**的 `<usehtml>xxx` 按 text 原样读（`end < marker.length`
///   那一支），两侧同形。
library;

/// 标记 → 渲染器。顺序照 Python 那份元组（先匹配上的算）。
const List<(String, String)> _introRenderers = <(String, String)>[
  ('<usehtml>', 'html'),
  ('<md>', 'md'),
  ('<useweb>', 'web'),
];

/// 原始载荷前缀：带这三根的**不做**标签剥离（Legado 把它们原样存，交给对应渲染器）。
const List<String> _rawIntroPrefixes = <String>['<usehtml>', '<md>', '<useweb>'];

/// `(kind, text)`：kind 是 `text`/`html`/`md`/`web`，只有 `text` 那一份是能直接读的。
({String kind, String text}) displayIntro(String value) {
  final text = value.trimLeft();
  for (final (marker, kind) in _introRenderers) {
    if (!text.startsWith(marker)) continue;
    final end = text.lastIndexOf('<');
    // 只有开标记、没有闭合标记的简介剥不出载荷：那一份只能按原文当 text 读。
    if (end < marker.length) return (kind: 'text', text: text);
    return (kind: kind, text: text.substring(marker.length, end));
  }
  // 传的是**已 lstrip 的那一份**：Python 的 display_intro 就是把它剥过前导空白的
  // 文本交给 _format_book_intro，这里若传原值，前导空白会先被块级规则折成段首缩进。
  return (kind: 'text', text: formatBookIntro(text));
}

/// 详情框那一格的话：非 text 的载荷不画原文，画一句实话
/// （flet `find_books_controller.py:309-312` 就是这个分流）。
/// 空串留给视图那句"暂无简介"，这里不在空串上编一句"无"。
String introForDisplay(String value) {
  final parsed = displayIntro(value);
  if (parsed.kind == 'text') return parsed.text;
  return '(该书简介需网页/富文本渲染，开始阅读查看)';
}

/// Legado 的 `BookInfo.analyzeBookInfo` 对纯文本简介走的那一遍 HTML 折行。
String formatBookIntro(String value) {
  final trimmed = value.trimLeft();
  if (_rawIntroPrefixes.any(trimmed.startsWith)) return trimmed;
  return _stripHtmlTags(value);
}

/// 标签剥离那八步，逐步对应 `_strip_html_tags`（顺序是有意义的：先换实体，
/// 再把块级标签换成换行，然后才是注释与剩下的标签，最后把散落的空白行折成段首缩进）。
String _stripHtmlTags(String value) {
  var text = value.replaceAll(RegExp(r'(?:&nbsp;)+'), ' ');
  text = text.replaceAll(RegExp(r'&ensp;|&emsp;'), ' ');
  text = text.replaceAll(RegExp(r'&thinsp;|&zwnj;|&zwj;'), '');
  text = text.replaceAll(
      RegExp(r'</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>'), '\n');
  text = text.replaceAll(RegExp(r'<!--[^>]*-->'), '');
  text = text.replaceAll(RegExp(r'</?[a-zA-Z]+(?=[ >])[^<>]*>'), '');
  text = text.replaceAll(RegExp(r'\s*\n+\s*'), '\n　　');
  text = text.replaceFirst(RegExp(r'^[\n\s]+'), '　　');
  return text.replaceFirst(RegExp(r'[\n\s]+$'), '');
}
