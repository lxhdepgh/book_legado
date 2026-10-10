/// 目录里的一章：`reader.toc` 的那八格（`bookshelf` 之外的第二张行列表）。
///
/// 白名单在**服务端 action 表**上（`app/gateway/methods.py` 里 `reader.toc` 的
/// `fields=`），不在这里：那边的 `_chapter_row` 其实给 16 格，投影在出口裁到 8 格。
/// 客户端读表外的那一格（比如 `start`/`end`/`variables`）**今天解得开、明天静默变空**——
/// 那正是 `refactor-regression-orphan-methods` 那一族。所以本类只有这八格，
/// 而 `golden_contract_test.dart` 拿样本的 `field_names` 逐键核它。
///
/// 目录**不挂在 `reader.open` 的回包上**（几千章挂在"点开一本书"那一发的大小上，
/// 就是 G2 那笔载荷账的原样重演），所以这一发是独立的、可翻页、可 `refresh` 的。
library;

class ChapterRow {
  const ChapterRow({
    required this.name,
    required this.url,
    required this.index,
    required this.isVolume,
    required this.isVip,
    required this.isPay,
    required this.tag,
    required this.wordCount,
  });

  final String name;

  /// 取正文那一发的 `chapter_url` 就是它（`reader.content` 按 url 认章，不按 index）。
  final String url;

  /// **目录里的序号**（服务端 `ChapterItem.index`）。落库的进度按这一格认章——
  /// `books` 表不存 chapter_url（设计 §3.9），所以"第几章"才是两侧的公共坐标。
  final int index;

  /// 卷标题（Legado 的 `isVolume`）：那一条不是真章，点了没有正文可读。
  final bool isVolume;

  final bool isVip;
  final bool isPay;

  /// 章级标记（`第 N 章 · 免费` 那一格的原值，也可能是"VIP"字样）。
  final String tag;

  /// 字数：服务端给的是**字符串**（有的源根本不填），不是数字。
  /// 界面原样显示，不 parse、不补 0——补 0 会让"这条源没给字数"显示成"这章 0 字"。
  final String wordCount;

  /// 付费墙：两种旗子指向同一种"读不了"，界面只用一个判据。
  bool get paywalled => isVip || isPay;

  factory ChapterRow.fromJson(Map<String, Object?> json) => ChapterRow(
        name: '${json['name'] ?? ''}',
        url: '${json['url'] ?? ''}',
        index: (json['index'] as num?)?.toInt() ?? 0,
        isVolume: json['isVolume'] == true,
        isVip: json['isVip'] == true,
        isPay: json['isPay'] == true,
        tag: '${json['tag'] ?? ''}',
        wordCount: '${json['wordCount'] ?? ''}',
      );

  @override
  String toString() => 'ChapterRow($index, $name)';
}
