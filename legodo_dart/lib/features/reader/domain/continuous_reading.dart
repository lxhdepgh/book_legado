/// 连续阅读（读到章尾自动接下一章）的落点折算：整篇段号 → (哪一章, 章内段号, 章内字符偏移)。
///
/// 一页一章时这把尺的零点天然就是本章第一段——G3-C3 那一版翻页走整篇替换，减不减起点
/// 是同一个数。flet 不是这么读的：它把下一章**接在同一篇文档后面**，屏幕上同时挂着
/// 2~3 章，视图报的是**整篇**段号，而落库必须是**章内**段号与**章内**字符偏移
/// （#88 取证二实测：不减起点差 259 段）。这一层就是那一步折算。
///
/// Python 侧的同源实现是 `ReadingSession.track`（`app/services/reading/reading_service.py`），
/// 两边的相等由 `test/continuous_reading_projection_test.dart` 对着 flet 自己产出的
/// `tests/gateway/projection/reader.continuous.json` 核——那张表里的答案列由 flet 现场读回，
/// 不是这里算出来的数（记忆 `measurement-corpus-must-not-be-self-proving`）。
///
/// **两套尺在章间接缝上会打架**，这是这一层存在的主要理由：
/// - [ChapterMount.documentStart] 是触发追加那一刻客户端量到的**旧**文档总高（像素）；
/// - 整篇段号是视图把文档按**当前**高度等比缩放之后数出来的。
///
/// 两者对各章高度占比的判断不一样，接缝那几格就会出现"像素说还在上一章、段号说已经是
/// 下一章的第 0 段"。第九十二轮的参照表把这件事量成了一个**不可能的读数**：整篇第 12 段
/// 被存成"只有 12 段的第一章的第 12 段"，那个数重开时被夹到 11，用户从第二章段首被弹回
/// 第一章段尾。所以规矩是：**整篇段号在场、且这张表的段区间可信时按段号选章**，
/// 像素只用来算段内那一截的比例（那是估算，不是事实）。
library;

/// 一章接上屏之后，折算要用到的那几格。
///
/// 与 flet 的 `ChapterViewport` 同形状同名次：那张表是服务端会话里的账，这一份是
/// 客户端屏上的账，两边的数由投影表对齐。
class ChapterMount {
  const ChapterMount({
    required this.chapterUrl,
    required this.chapterIndex,
    required this.paragraphStart,
    required this.paragraphOffsets,
    required this.textLength,
    required this.documentStart,
  });

  final String chapterUrl;

  /// 落库的章序号（与 `reader.toc` 的行序号同一把尺，见 `reader_controller.dart` 规矩 2）。
  final int chapterIndex;

  /// 本章第一段在**整篇文档**里的段序号：追加时就是已经挂在屏幕上的那些段数。
  final int paragraphStart;

  /// 本章各段段首的**章内**字符偏移（[paragraphOffsets] 的产物）。
  ///
  /// 空 = 这一章没算过坐标。那时整张表的段区间无从算起，[ContinuousDocument.ownerOf]
  /// 会拒绝按段号选章（老记录、只挂控件不读正文的替身都走这一支）。
  final List<int> paragraphOffsets;

  /// 本章字符流总长：段号用不上时旧的像素插值要拿它乘比例。
  final int textLength;

  /// 这一章在整篇文档里的像素起点（**追加时刻的旧文档高**，见文件头那两套尺）。
  final double documentStart;

  int get paragraphCount => paragraphOffsets.length;

  @override
  String toString() =>
      'ChapterMount(#$chapterIndex $chapterUrl, 段起点 $paragraphStart/'
      '$paragraphCount 段, y=${documentStart.toStringAsFixed(0)}px)';
}

/// 一屏挂着 1~N 章的那一篇文档。
class ContinuousDocument {
  const ContinuousDocument(this.mounts);

  /// **挂载顺序**（屏上从上到下）。
  ///
  /// 折算一律先在 [byPixels]（按像素起点稳定排序）上做：flet 那侧的视口表是按 url 存的、
  /// 取用时按 `document_start` 排，两份账的插入顺序不保证一样，而选章与算比例都只认像素。
  final List<ChapterMount> mounts;

  /// 空文档（还没挂出第一章）：所有折算都回 null，不造一个"第 0 章"。
  bool get isEmpty => mounts.isEmpty;

  /// 整篇段数 = 各章段数之和（flet 那侧是 `ReaderView.paragraph_count()`）。
  ///
  /// **不是**屏上控件数：媒体块占一行不产段，章名与分隔线也不算段。按控件数累加起点
  /// 的人会把第 3 章的起点写成 18 而不是 15（参照表里那条正对照钉的就是这一格）。
  int get paragraphTotal =>
      mounts.fold(0, (sum, mount) => sum + mount.paragraphCount);

  /// 按像素起点排好的那一批（与起点相同的保持挂载顺序——Python 的 `sorted` 是稳定的）。
  List<ChapterMount> get byPixels {
    final order = <int>[for (var i = 0; i < mounts.length; i++) i]
      ..sort((a, b) {
        final byStart = mounts[a].documentStart.compareTo(mounts[b].documentStart);
        return byStart != 0 ? byStart : a.compareTo(b);
      });
    return [for (final index in order) mounts[index]];
  }

  /// 整篇段号落在第几个视口（[byPixels] 的下标）；选不出就回 null。
  ///
  /// 三种"选不出"，每一种都必须退回像素口径而不是硬认一章（第九十二轮那三发兜底守卫）：
  /// ① 段号不在场或为负；② 某一章没算过坐标（[ChapterMount.paragraphOffsets] 空）；
  /// ③ 段区间**重叠或倒挂**（后挂上来的章自称从更早的段起）——这张表自相矛盾；
  /// ④ 那一段不落在任何一章的区间里（脏记录、目录换过）。
  int? ownerIndexOf(int? documentParagraph) {
    if (documentParagraph == null || documentParagraph < 0) return null;
    final ordered = byPixels;
    if (ordered.isEmpty) return null;
    final starts = <int>[];
    final ends = <int>[];
    for (final mount in ordered) {
      if (mount.paragraphCount == 0) return null;
      final start = mount.paragraphStart;
      final end = start + mount.paragraphCount;
      if (end <= start) return null;
      if (ends.isNotEmpty && start < ends.last) return null;
      starts.add(start);
      ends.add(end);
    }
    for (var i = 0; i < starts.length; i++) {
      if (starts[i] <= documentParagraph && documentParagraph < ends[i]) return i;
    }
    return null;
  }

  /// 认领整篇段号的那一章；null 的含义见 [ownerIndexOf]。
  ///
  /// [land] 要的是**下标**（它还得拿同一张表按像素算段内比例），所以生产路径调的是
  /// [ownerIndexOf]，这一格是守卫那一侧的读数口：它能指着"这一章"说段号选章拒绝了，
  /// 而不必自己再实现一遍区间判断（同 [pixelIndexOf] 那条理由）。
  ChapterMount? ownerOf(int? documentParagraph) {
    final index = ownerIndexOf(documentParagraph);
    return index == null ? null : byPixels[index];
  }

  /// 只看像素的选章结果：滚动位置落在哪一章的像素带里（[land] 在段号不在场、或表
  /// 不可信时走的就是这一条）。
  ///
  /// 把它单独露出来是为了让守卫能指着**同一格**说"像素说这一章、段号说那一章"——
  /// 两套尺打架这件事得能被读出来，而不是只在注释里声称。
  int pixelIndexOf(double pixels) {
    final ordered = byPixels;
    if (ordered.isEmpty) return -1;
    final offset = pixels < 0 ? 0.0 : pixels;
    var index = 0;
    for (var i = 0; i < ordered.length; i++) {
      if (ordered[i].documentStart > offset) break;
      index = i;
    }
    return index;
  }

  /// 一发滚动 → 落点。这是 `ReadingSession.track` 的那一步，同一份算术。
  ///
  /// [pixels] 是文档内的滚动位置，[documentExtent] 是当前文档总高（客户端量到的），
  /// [documentParagraph] 是屏上顶部那一行的**整篇**段号（量不到就传 null）。
  Landing? land({
    required double pixels,
    required double documentExtent,
    int? documentParagraph,
  }) {
    final ordered = byPixels;
    if (ordered.isEmpty) return null;
    final offset = pixels < 0 ? 0.0 : pixels;
    final extent = documentExtent < 0 ? 0.0 : documentExtent;
    var selectedIndex = pixelIndexOf(pixels);
    final byParagraph = ownerIndexOf(documentParagraph);
    if (byParagraph != null) selectedIndex = byParagraph;
    final selected = ordered[selectedIndex];
    final nextStart = selectedIndex + 1 < ordered.length
        ? ordered[selectedIndex + 1].documentStart
        : extent;
    final chapterExtent = nextStart - selected.documentStart;
    // 与 Python 同一处 clamp：这一格存进 `scroll_offset`，负数落库就是脏读数。
    final localOffset = offset - selected.documentStart;
    final chapterPixels = localOffset > 0 ? localOffset : 0.0;
    // 比例上界 1.0：越过本章右端点不是误差，是"这一帧已经该算下一章了"。
    final rawRatio = chapterExtent > 0 ? chapterPixels / chapterExtent : 0.0;
    final ratio = rawRatio > 1.0 ? 1.0 : rawRatio;
    // 视图给的是整篇段号：减掉本章起点才是章内段号（不减，重开时这个数指到别人章里）。
    int? localParagraph;
    if (documentParagraph != null && documentParagraph >= 0) {
      localParagraph = documentParagraph - selected.paragraphStart;
      if (localParagraph < 0) localParagraph = 0;
    }
    final characterOffset =
        localParagraph != null && selected.paragraphOffsets.isNotEmpty
            ? characterOffsetAt(selected.paragraphOffsets, localParagraph, ratio)
            : (selected.textLength * ratio).toInt();
    return Landing(
      mount: selected,
      localParagraph: localParagraph,
      characterOffset: characterOffset < 0 ? 0 : characterOffset,
      chapterRatio: ratio,
      chapterPixels: chapterPixels,
    );
  }

  @override
  String toString() => 'ContinuousDocument(${mounts.length} 章/'
      '$paragraphTotal 段)';
}

/// 段号 → 章内字符偏移：段首是事实，段内那一截按像素比例估且**不越过段尾**。
///
/// 越过段尾就不是估算误差了——它会把人送进下一段，两侧于是各认一个段。
/// 最后一段没有下界（后面没有别的段可减），那一格只认段首，不猜。
/// 段间那一格算 `kParagraphJoinerLen`（这里是 1），所以区间长度要减它。
int characterOffsetAt(List<int> offsets, int paragraph, double ratio) {
  if (offsets.isEmpty) {
    throw ArgumentError('空段表没有段首可言：这一章没算过坐标，走像素插值那一条');
  }
  final anchor = paragraph < 0 ? 0 : (paragraph >= offsets.length ? offsets.length - 1 : paragraph);
  final start = offsets[anchor];
  final span = anchor + 1 < offsets.length ? offsets[anchor + 1] - start - 1 : 0;
  final clamped = ratio < 0 ? 0.0 : (ratio > 1 ? 1.0 : ratio);
  final value = start + (span * clamped).toInt();
  return value < 0 ? 0 : value;
}

/// 一次折算的结论：落在哪一章的第几段、章内第几个字。
class Landing {
  const Landing({
    required this.mount,
    required this.localParagraph,
    required this.characterOffset,
    required this.chapterRatio,
    required this.chapterPixels,
  });

  final ChapterMount mount;

  /// 章内段号；`null` = 这一屏量不到段号（落库就是"段序号未知"，重开退回像素）。
  final int? localParagraph;

  /// 章内字符偏移——跨端比对的那一个数（§9 G3 第一条判据）。
  final int characterOffset;

  /// 本章内的像素比例（段内那一截的估算依据）。
  final double chapterRatio;

  /// 本章内的像素偏移（存档 `scroll_offset` 用，不参与跨端比对）。
  final double chapterPixels;

  int get chapterIndex => mount.chapterIndex;
  String get chapterUrl => mount.chapterUrl;

  @override
  String toString() => 'Landing(第 ${mount.chapterIndex} 章/'
      '段 ${localParagraph ?? '?'}/字 $characterOffset, '
      '比例 ${chapterRatio.toStringAsFixed(4)})';
}
