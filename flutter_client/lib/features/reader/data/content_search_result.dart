/// 书内全文检索的结果：`reader.search_content` 的回话。
///
/// 0 命中**必须能分清两种 0**（记忆 `zero-counts-need-a-positive-control`）：
/// - [ftsAvailable]=false：这台机器的 SQLite 没编 FTS5，一根词都搜不了；
/// - [indexedChapters] < 目录章数：只索引了已缓存的章，没缓存的那部分根本没进表。
/// 界面按这两格说"这本书里没找到"还是"只搜了已缓存的 N 章"——后者不说，
/// 用户会以为书坏了。
///
/// [written] 是这一次**新写进索引的段数**（`ensure_index` 的返回值），不是命中数：
/// 把两个数并成一个计数器就分不清"第一次建索引花了多久"与"这本书里有没有这个词"
/// （记忆 `aggregated-counters-hide-which-call`）。
library;

/// 一条命中。
class ContentHit {
  const ContentHit({
    required this.chapterIndex,
    required this.paraIndex,
    required this.characterOffset,
    required this.snippet,
  });

  final int chapterIndex;

  /// 命中的那一段在**章内**的段序号（索引按段建行，不是按 text block）。
  final int paraIndex;

  /// 段首的**章内字符偏移**——位置契约那把尺导出的同一个数
  /// （`content_search.py:129` 用的就是 `paragraph_offsets`）。
  /// 线上是 float（那一列是 REAL），这里收成 int 因为偏移本来就是整数字格。
  final int characterOffset;

  /// 带 `<mark>` 标记的摘录。**标记原样留着**：flet 那边把它换成
  /// `ft.TextSpan` 的高亮，Dart 这屏同样要在渲染层处理，不在协议面剥掉
  /// （剥掉就没有"命中在哪几个字"了）。
  final String snippet;

  factory ContentHit.fromJson(Map<String, Object?> json) => ContentHit(
        chapterIndex: (json['chapter_index'] as num?)?.toInt() ?? 0,
        paraIndex: (json['para_index'] as num?)?.toInt() ?? 0,
        characterOffset: (json['offset'] as num?)?.toInt() ?? 0,
        snippet: '${json['snippet'] ?? ''}',
      );

  @override
  String toString() => 'ContentHit(ch$chapterIndex 第$paraIndex 段 '
      '@$characterOffset)';
}

/// 一发书内搜索的回话。
class ContentSearchResult {
  const ContentSearchResult({
    required this.hits,
    required this.written,
    required this.indexedChapters,
    required this.ftsAvailable,
  });

  final List<ContentHit> hits;
  final int written;

  /// 索引里现有几章（**不是**这本书几章——那一格在 `reader.open` 的 `chapterCount`）。
  final int indexedChapters;

  /// 这台机器的 FTS5 可用否。false 时 [hits] 恒空，界面得说"这台机器搜不了"。
  final bool ftsAvailable;

  factory ContentSearchResult.fromJson(Map<String, Object?> json) =>
      ContentSearchResult(
        hits: (json['hits'] as List? ?? const <Object?>[])
            .map((row) => row is Map
                ? ContentHit.fromJson(Map<String, Object?>.from(row))
                : throw ArgumentError(
                    'reader.search_content 的某条命中不是对象（收到 ${row.runtimeType}）'))
            .toList(growable: false),
        written: (json['written'] as num?)?.toInt() ?? 0,
        indexedChapters: (json['indexedChapters'] as num?)?.toInt() ?? 0,
        ftsAvailable: json['ftsAvailable'] == true,
      );

  @override
  String toString() => 'ContentSearchResult(${hits.length} 命中, '
      '索引 $indexedChapters 章, FTS ${ftsAvailable ? '可用' : '不可用'})';
}
