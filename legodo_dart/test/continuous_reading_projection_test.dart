/// 连续阅读的折算层对着 **flet 自己产出的那张表**（`tests/gateway/projection/reader.continuous.json`）。
///
/// 这一发算"跨端可比"的证据、而"两侧各读一遍同一个常量"不算：表里的答案列由 flet 的
/// 真 `ReaderController` + 真 `ReaderView` + 真 `ReadingSession` 现场读回
/// （`tests/test_reader_continuous_projection.py`），Dart 这边吃**同样的输入**（表里钉着的
/// 滚动像素、整篇段号、各章起点与段首偏移），自己走 [ContinuousDocument.land] 再算一遍，
/// 两头答案逐格同数。任一侧的折算改了而另一侧没改，这里就红（记忆
/// `measurement-corpus-must-not-be-self-proving`）。
///
/// 输入三样：滚动像素、当前文档总高、屏上顶行的**整篇**段号。
/// 答案五样：落在第几章、章内第几段、章内第几个字、本章比例、章内像素。
/// 浮点这一路不做容差：表里的像素**没有 round 过**，两侧的算术都是 IEEE754 同一串操作，
/// 差一个 bit 就是某一边算错了，不是"精度问题"。
///
/// 表里的语料是三章各一种形状（12 短段 / 3 长段带一张插图 / 7 段且首行是标题），段起点
/// 因此是 0、12、15 三个互不相同的数——两章同形状时"减错起点"与"减对了"会算出同一个偏移，
/// 比了等于没比。第 2 章那张插图占一行不产段，按屏上行数累加起点的人会给第 3 章 18 而不是
/// 15，那一格也在这条判据里。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/features/reader/domain/continuous_reading.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _table() => jsonDecode(File(
            '${_repoRoot.path}/tests/gateway/projection/reader.continuous.json')
        .readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

int _int(Object? value) => (value! as num).toInt();

double _double(Object? value) => (value! as num).toDouble();

List<Map<String, Object?>> _rows(Object? value) =>
    (value! as List).cast<Map<String, Object?>>();

/// 表里那一屏挂着的章 → Dart 的挂载账（起点、段数、偏移、像素 y 全抄表，不自己算）。
List<ChapterMount> mountsOf(Map<String, Object?> table) => <ChapterMount>[
      for (final row in _rows(table['chapters']))
        ChapterMount(
          chapterUrl: '${row['chapter_url']}',
          chapterIndex: _int(row['chapter_index']),
          paragraphStart: _int(row['paragraph_start']),
          paragraphOffsets: <int>[
            for (final item in (row['offsets']! as List)) _int(item)
          ],
          textLength: _int(row['text_length']),
          documentStart: _double(row['document_start']),
        ),
    ];

/// 改挂载账里的某一格（造"表不可信"那几种形状时用，别的字段原样留着）。
ChapterMount _remount(ChapterMount mount, {int? start, List<int>? offsets}) =>
    ChapterMount(
      chapterUrl: mount.chapterUrl,
      chapterIndex: mount.chapterIndex,
      paragraphStart: start ?? mount.paragraphStart,
      paragraphOffsets: offsets ?? mount.paragraphOffsets,
      textLength: mount.textLength,
      documentStart: mount.documentStart,
    );

/// 一发落点探针：输入（整篇段号 + flet 量到的像素）与 flet 自己的答案。
class Probe {
  Probe(Map<String, Object?> row)
      : documentParagraph = _int(row['document_paragraph']),
        pixel = _double(row['flet_pixel']),
        answer = Map<String, Object?>.from(row['answer']! as Map);

  final int documentParagraph;
  final double pixel;
  final Map<String, Object?> answer;
}

void main() {
  final table = _table();
  final base = mountsOf(table);
  final document = ContinuousDocument(base);
  final probes = _rows(table['probes']).map(Probe.new).toList(growable: false);
  final stageHeights =
      (table['layout']! as Map<String, Object?>)['stage_heights']! as List;
  // 探针那一轮滚动是在**三章都挂齐之后**发的，flet 当时报的文档总高就是第三档。
  final documentExtent = _double(stageHeights[stageHeights.length - 1]);

  Landing landOf(Probe probe) => document.land(
      pixels: probe.pixel,
      documentExtent: documentExtent,
      documentParagraph: probe.documentParagraph)!;

  test('表落地了，挂的是三章、22 段', () {
    expect(document.mounts.length, 3);
    expect(document.paragraphTotal, _int((table['document']! as Map)['paragraph_total']));
    expect(document.paragraphTotal, 22);
    expect(base.map((mount) => mount.paragraphCount).toList(), <int>[12, 3, 7]);
    expect(base.map((mount) => mount.paragraphStart).toList(), <int>[0, 12, 15]);
    // 正对照：屏上控件比段多（媒体块、章名、分隔线占行不产段）。少了这一句，
    // "按行数累加起点"那条错路与正确路同形，比不出什么。
    expect(_int((table['document']! as Map)['screen_controls']),
        greaterThan(document.paragraphTotal));
  });

  test('flet 的 9 发落点探针，Dart 逐发同数（含接缝那两格）', () {
    expect(probes.length, 9);
    for (final probe in probes) {
      final landing = document.land(
        pixels: probe.pixel,
        documentExtent: documentExtent,
        documentParagraph: probe.documentParagraph,
      );
      expect(landing, isNotNull, reason: '整篇第 ${probe.documentParagraph} 段没算出没落点');
      expect(landing!.chapterIndex, _int(probe.answer['chapter_index']),
          reason: '整篇第 ${probe.documentParagraph} 段选错了章');
      expect(landing.chapterUrl, '${probe.answer['chapter_url']}');
      expect(landing.localParagraph, _int(probe.answer['chapter_paragraph']),
          reason: '整篇第 ${probe.documentParagraph} 段减完起点不是 '
              '${probe.answer['chapter_paragraph']}');
      expect(landing.characterOffset, _int(probe.answer['character_offset']),
          reason: '整篇第 ${probe.documentParagraph} 段的章内偏移两侧不同数');
      expect(landing.chapterRatio, _double(probe.answer['chapter_ratio']),
          reason: '本章比例两侧不同数——偏移那一步正是拿它乘出来的');
    }
  });

  test('章内段号永远落在本章里（那一发不可能的读数现在读不出来）', () {
    // 本轮修的就是这一格：flet 曾把整篇第 12 段存成"只有 12 段的第一章的第 12 段"。
    for (final probe in probes) {
      final landing = landOf(probe);
      expect(landing.localParagraph, isNotNull);
      expect(
          landing.localParagraph!,
          inInclusiveRange(0, landing.mount.paragraphCount - 1),
          reason: '落库段号越出本章段数，重开时会被夹到段尾');
    }
  });

  test('接缝那两格：像素说还在上一章，段号才认得下一章的段首', () {
    final seam = probes
        .where((probe) =>
            probe.documentParagraph == 12 || probe.documentParagraph == 15)
        .toList();
    expect(seam.length, 2, reason: '表里没留接缝那两格，这一条在测空气');
    final ordered = document.byPixels;
    for (final probe in seam) {
      final byParagraph = landOf(probe);
      expect(byParagraph.localParagraph, 0);
      expect(byParagraph.characterOffset, 0, reason: '章内第 0 段的偏移必须是 0');
      // 同一格像素只看像素：flet 本轮修之前挑出来的就是那一章。
      final byPixels = ordered[document.pixelIndexOf(probe.pixel)];
      expect(byPixels.chapterIndex, isNot(byParagraph.chapterIndex),
          reason: '两把尺这一轮同判，接缝这条臂没测到打架');
      // 而"章按像素挑、段号照视图报"减出来的就是那个不可能的读数（越出本章段数）：
      // 它现在到不了落库那一步，因为选章改成了按段区间。
      expect(probe.documentParagraph - byPixels.paragraphStart,
          greaterThanOrEqualTo(byPixels.paragraphCount),
          reason: '像素口径本该给出越界的段号，这一发才叫"修掉了那个不可能的读数"');
    }
  });

  test('每一发的偏移回位还认同一段（估算不越段尾）', () {
    for (final probe in probes) {
      final landing = landOf(probe);
      final offsets = landing.mount.paragraphOffsets;
      final local = landing.localParagraph!;
      expect(landing.characterOffset, greaterThanOrEqualTo(offsets[local]));
      if (local + 1 < offsets.length) {
        expect(landing.characterOffset, lessThan(offsets[local + 1]),
            reason: '估算越过段尾就把自己送进了下一段，两侧于是各认一个段');
      } else {
        expect(landing.characterOffset, offsets[local],
            reason: '最后一段没有下界可减，那一格只认段首，不猜');
      }
    }
  });

  test('挂载顺序换了不改结论：选章按像素，不按挂载的先后', () {
    final reversed = ContinuousDocument(base.reversed.toList());
    for (final probe in probes) {
      final landing = reversed.land(
          pixels: probe.pixel,
          documentExtent: documentExtent,
          documentParagraph: probe.documentParagraph)!;
      expect(landing.chapterIndex, _int(probe.answer['chapter_index']),
          reason: '同一张账倒着挂就换一个答案——排序漏了');
      expect(landing.characterOffset, _int(probe.answer['character_offset']));
    }
  });

  test('表不可信的三种形状一律退回像素口径', () {
    // ① 某一章没算过坐标：整张表无从算区间。
    final headless = ContinuousDocument(
        <ChapterMount>[_remount(base.first, offsets: const <int>[]), ...base.skip(1)]);
    expect(headless.ownerOf(12), isNull,
        reason: '第一章没有坐标，按段区间选章会把人送进第二章');
    // ② 起点倒挂：后挂上来的第二章自称从整篇第 0 段起。
    final inverted = ContinuousDocument(
        <ChapterMount>[_remount(base.first, start: 12), _remount(base[1], start: 0)]);
    expect(inverted.ownerOf(2), isNull,
        reason: '这张表自相矛盾，认它就是把"还在第一章"滚进第二章');
    // ③ 段号不在任何一章的区间里（脏记录、目录换过）。
    expect(document.ownerOf(99), isNull);
    expect(document.ownerOf(null), isNull);
    expect(document.ownerOf(-1), isNull);
    expect(const ContinuousDocument(<ChapterMount>[]).ownerOf(0), isNull);
    // 空区间（一章 0 段）也算表不可信：那区间谁都"包含"不了，却也说明账是空的。
    expect(
        ContinuousDocument(<ChapterMount>[
          _remount(base.first, offsets: const <int>[]),
        ]).ownerOf(0),
        isNull);
  });

  test('退回像素口径时读出的是旧那一个数，不是段首', () {
    final headless = ContinuousDocument(
        <ChapterMount>[_remount(base.first, offsets: const <int>[]), ...base.skip(1)]);
    // 第一章的右端点是第二章的像素起点（表里 9000），滚到 1500 就是本章的 1/6 处。
    final landing =
        headless.land(pixels: 1500, documentExtent: 24000, documentParagraph: 12)!;
    expect(landing.chapterIndex, 0, reason: '第一章的坐标不在表上，就该按像素落在第一章');
    expect(landing.characterOffset, (base.first.textLength * (1500 / 9000)).toInt(),
        reason: '旧口径是 text_length × 像素比例，这一格要看得见它仍是估算');
    expect(base.first.paragraphOffsets.contains(landing.characterOffset), isFalse,
        reason: '兜底那一支不许悄悄变成段首导出：形状上要看得出它还是猜的');
    expect(landing.localParagraph, 12,
        reason: '兜底那一支不夹段号——段号越界是这张表没坐标的**后果**，不是折算算错');
  });

  test('空文档与负数输入不造假落点', () {
    expect(
        const ContinuousDocument(<ChapterMount>[])
            .land(pixels: 10, documentExtent: 100),
        isNull);
    final first = document.land(pixels: -500, documentExtent: documentExtent)!;
    expect(first.chapterPixels, 0.0);
    expect(first.chapterRatio, 0.0);
    expect(first.characterOffset, 0);
    expect(document.paragraphTotal, document.mounts.fold(
        0, (sum, mount) => sum + mount.paragraphCount));
  });

  test('段间那一格算给前一段的段尾（与单章那一版同一把尺）', () {
    final offsets = base[0].paragraphOffsets;
    // 第 k 段段首 + 段长 = 第 k+1 段段首前那一格；比例打满也不许进下一段。
    expect(characterOffsetAt(offsets, 0, 1.0), lessThan(offsets[1]));
    expect(characterOffsetAt(offsets, 0, 0.0), offsets[0]);
    expect(characterOffsetAt(offsets, offsets.length - 1, 1.0), offsets.last,
        reason: '最后一段没有下界，只认段首');
    expect(characterOffsetAt(offsets, 99, 0.5), offsets.last,
        reason: '越界的段号夹到最后一格，不造出第 len 段');
    expect(() => characterOffsetAt(const <int>[], 0, 0.0), throwsArgumentError);
  });
}
