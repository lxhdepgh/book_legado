/// 连续阅读那一臂的**屏上**守卫：读到章尾真的接上下一章、接上来的那一章真的不改视口、
/// 而落库的三格（章序号 / 章内段号 / 章内字符偏移）真的换成了本章口径。
///
/// 与 `continuous_reading_projection_test.dart` 的分工：那一只比的是**折算层**
/// （`ContinuousDocument.land` 逐发对上 flet 现场读回的九发探针）；这一只比的是**屏**——
/// 行真的挂上去了、GlobalKey 真的按整篇段号编、滚到接缝那一段真的把章号挪了一位。
/// 折算层对了而屏上报错段号（或键表撞名）是两回事，两边都要有人钉。
///
/// 语料是 flet 那支假源 `/g3cont` 的**同一串字**（三章形状互异：12 短段 / 3 长段带一张插图 /
/// 7 段且首行是标题），而期望值抄的是 `tests/gateway/projection/reader.continuous.json`
/// 里 flet 自己算出来的那几列（各章段起点、整篇段数）。两章同形状时"接错章""按行数累加起点"
/// 都看不出来（记忆 `measurement-corpus-must-not-be-self-proving`），所以第一条守卫先把
/// "Dart 造的语料与 flet 那张表吃的是同一串字"钉住——对不上，后面每一条都在测空气。
///
/// 三条仪器规矩（与 `reader_screen_test.dart` 同源）：
/// - 走**真路由**开书（`buildRouter()` + `extra`），弹窗那一段容器归属的教训不在这里重演；
/// - 落库那一发要**自己把假时钟推过 0.5s**（`pumpAndSettle` 不推 Timer，本轮量过的形状：
///   屏上落点已经到第二章，`reader.position_save` 却一发都没有）；
/// - 每一个"没接第二遍"旁边都站着一根明知会接的对照（第 3 章那一发真的接上来了）。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:legodo_client/app/router.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/data/chapter_row.dart';
import 'package:legodo_client/features/reader/data/reader_session.dart';
import 'package:legodo_client/features/reader/domain/reader_controller.dart';
import 'package:legodo_client/features/reader/domain/text_layout.dart';
import 'package:legodo_client/features/reader/presentation/reader_screen.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _json(String relative) => jsonDecode(
        File('${_repoRoot.path}/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _table() => _json('tests/gateway/projection/reader.continuous.json');

int _int(Object? value) => (value! as num).toInt();

List<Map<String, Object?>> _rows(Object? value) =>
    (value! as List).cast<Map<String, Object?>>();

const String _sourceUrl = 'http://127.0.0.1:8797/g3cont';
const String _stem = '字句读纸张看灯下山风';

/// 三章的段构造——与 `tests/gateway/fake_source_reader.py::CONT_SHAPES` 逐项同值。
/// 第 2 章那一张插图插在**第 1 段之后**（占行不产段），第 3 章首行是认得出的标题。
const Map<int, List<int>> _counts = <int, List<int>>{
  1: [1, 2, 1, 3, 2, 1, 4, 2, 3, 1, 2, 1],
  2: [24, 31, 18],
  3: [5, 2, 9, 1, 6, 3],
};

String _chapterUrl(int index) => '$_sourceUrl/ch/$index.html';

/// 第 [index] 章（1 起）的自然段列表，与 flet 那支假源吐出的字逐字相同。
List<String> _paragraphsOf(int index) {
  final paragraphs = <String>[
    for (final entry in _counts[index]!.indexed)
      '连续第$index章第${entry.$1 + 1}段${_stem * entry.$2}'
  ];
  if (index == 3) paragraphs.insert(0, '第$index章 收梢');
  return paragraphs;
}

/// 第 [index] 章的载荷。插图块切在第 1 段与第 2 段之间（flet 那侧是同一个位置）。
ChapterDocument _documentOf(int index) {
  final paragraphs = _paragraphsOf(index);
  final blocks = <ContentBlock>[];
  if (index == 2) {
    blocks.add(_textBlock(paragraphs[0]));
    blocks.add(ContentBlock(
        kind: 'image',
        value: null,
        url: '$_sourceUrl/pic.jpg',
        binary: true,
        title: '',
        headers: const <String, String>{}));
    blocks.add(_textBlock(paragraphs.sublist(1).join('\n')));
  } else {
    blocks.add(_textBlock(paragraphs.join('\n')));
  }
  // `paragraphCount`/`totalChars` 这两格在真回话里是**服务端**数的；替身这里只能自己数
  // 一遍（那一族跨端读数由 `reader_live_arm_test` 的实弹臂核，不属这一只的范围）。
  return ChapterDocument(
    chapterUrl: _chapterUrl(index),
    chapterIndex: index - 1,
    chapterName: '第$index章 连续章',
    title: '',
    imageStyle: '',
    blocks: blocks,
    paragraphCount: paragraphs.length,
    totalChars: chapterTextLength(paragraphs),
  );
}

ContentBlock _textBlock(String value) => ContentBlock(
    kind: 'text',
    value: value,
    url: '',
    binary: false,
    title: '',
    headers: const <String, String>{});

/// 空正文的那一章（演"接上来的一章没长高"：迟滞那一臂只有它才照得见）。
ChapterDocument _emptyDocumentOf(int index) => ChapterDocument(
    chapterUrl: _chapterUrl(index),
    chapterIndex: index - 1,
    chapterName: '第$index章 连续章',
    title: '',
    imageStyle: '',
    blocks: const <ContentBlock>[],
    paragraphCount: 0,
    totalChars: 0);

List<ChapterRow> _toc() => <ChapterRow>[
      for (var i = 1; i <= 3; i++)
        ChapterRow(
            name: '第$i章 连续章',
            url: _chapterUrl(i),
            index: i - 1,
            isVolume: false,
            isVip: false,
            isPay: false,
            tag: '',
            wordCount: ''),
    ];

FakeEngineRepository _contFake({Map<String, ChapterDocument>? byUrl}) {
  final sessionJson = Map<String, Object?>.from(
      _json('tests/gateway/golden/reader.open.json')['response']!
          as Map<String, Object?>)['data']! as Map<String, Object?>;
  // 从第一章开：`resume` 带回的那一格会把起点章挪走，"接到第二章"就变成"接到第 N+1 章"。
  sessionJson['position'] = <String, Object?>{'chapter_index': 0};
  return FakeEngineRepository(
    session: ReaderSession.fromJson(sessionJson),
    toc: _toc(),
    document: _documentOf(1),
    documentsByChapterUrl: byUrl ??
        <String, ChapterDocument>{
          for (var i = 2; i <= 3; i++) _chapterUrl(i): _documentOf(i),
        },
  );
}

Future<GoRouter> _open(WidgetTester tester, FakeEngineRepository fake) async {
  // 视口收小：正文要**真的滚得动**才接得出下一章（默认 800×600 下第 1 章 12 短段
  // 几乎一屏装下，`maxScrollExtent` 接近 0，追加判据根本走不到）。
  //
  // 360×**400** 而不是 360×300：300 高时根路由上首屏那一页（阅读分组的初始页签）
  // 自己会 RenderFlex 溢出 18px——那是另一屏在极小高度下的排版，与这一屏无关，
  // 而 flutter_test 把任何一次渲染异常都记成"这一条失败"，八条用例全被它带红。
  // 400 高时正文照样滚得动（第 1 章 maxScrollExtent ≈ 549），前提一个不少。
  await tester.binding.setSurfaceSize(const Size(360, 400));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final router = buildRouter();
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: MaterialApp.router(routerConfig: router),
  ));
  await tester.pumpAndSettle();
  unawaited(router.push<void>(readerRoute,
      extra: ReaderArgs(sourceUrl: _sourceUrl, book: <String, Object?>{
        'name': '连续阅读书',
        'author': '作者戊',
        'coverUrl': '',
        'bookUrl': '$_sourceUrl/book/1.html',
      })));
  await tester.pumpAndSettle();
  return router;
}

ReaderPage _page(WidgetTester tester) {
  final container = ProviderScope.containerOf(
      tester.element(find.byType(ReaderScreen)), listen: false);
  return container.read(readerProvider).value!;
}

ReaderNotifier _notifier(WidgetTester tester) => ProviderScope.containerOf(
        tester.element(find.byType(ReaderScreen)), listen: false)
    .read(readerProvider.notifier);

ScrollController _list(WidgetTester tester) => tester
    .widget<ListView>(find.byKey(const Key('reader.body')))
    .controller!;

double _viewportTop(WidgetTester tester) =>
    tester.getTopLeft(find.byKey(const Key('reader.body'))).dy;

Finder _row(int documentParagraph) =>
    find.byKey(ValueKey('reader.text.$documentParagraph'));

/// 树上已建出来的正文行段号（升序）。`ListView.builder` 只建视口附近那几行，
/// `_revealRow` 要往哪边揭、跳过几屏都靠它当眼睛（段号是离散的，能直接从键上读）。
List<int> _builtRange(WidgetTester tester) {
  final values = <int>[];
  for (final text in tester.widgetList<Text>(find.byWidgetPredicate((widget) =>
      widget is Text &&
      widget.key is ValueKey<String> &&
      (widget.key! as ValueKey<String>).value.startsWith('reader.text.')))) {
    final key = (text.key! as ValueKey<String>).value;
    values.add(int.parse(key.substring('reader.text.'.length)));
  }
  return values..sort();
}

/// 让下一发滚动通知落在**距底不足 180px** 那一格：一屏一屏跳到底。
///
/// 为什么跳好几次而不是 `jumpTo(maxScrollExtent)` 一发到位：`ListView.builder` 的
/// `maxScrollExtent` 是按**已建出来的**那些行的均值估的，跳到"估出来的底"之后布局又建出
/// 新的一截，真正的底还在下面——一次到位时通知里的 distance 可能是几百像素，
/// 追加就不会触发（仪器自己的错，不是屏的错）。
///
/// 返回最后一发 `jumpTo` 之后**当场**读到的像素：追加那一臂的原则是"视口一像素不动"，
/// 判据要拿触发那一发的停靠值比，不能拿"接完之后"的读数比（接完再读，等于让屏自己
/// 给自己当证词）。
Future<double> _driveToBottom(WidgetTester tester) async {
  var docked = _list(tester).position.pixels;
  for (var step = 0; step < 8; step++) {
    final controller = _list(tester);
    controller.jumpTo(controller.position.maxScrollExtent);
    docked = controller.position.pixels;
    await tester.pump();
    if (controller.position.pixels >=
        controller.position.maxScrollExtent - _triggerBand()) {
      break;
    }
  }
  // 追加那一臂是"通知里发起一发网络请求"：替身即时回话，但 future 仍要走一拍微任务，
  // 再补两泵才看得见接上来的行。
  await tester.pump();
  await tester.pump();
  return docked;
}

/// 与 `ReaderNotifier._appendTriggerPx` 同一个数——**不 import 那个私有常量**：
/// 判据读被测对象的常量的话，把 180 改成 18000 也照样全绿（记忆那一条"不许读被测常量"）。
double _triggerBand() => 180;

/// 把整篇第 [paragraph] 段的段顶对齐到视口顶（屏外就往那一侧一屏一屏揭）。
///
/// 方向按**已建出来的段号区间**判：目标比最小已建段号还小就往上看，否则往下看。
/// 不能一律往下跳：接缝那一臂要先往上看（从第二章退回第一章那一段），
/// 只会向下揭的话那一问永远够不着。
///
/// 这里用的是**外部**量到的行顶（`getTopLeft`），不是屏自己记的那一套：两边各量各的
/// 才叫对照。
Future<void> _revealRow(WidgetTester tester, int paragraph) async {
  for (var step = 0; step < 60 && _row(paragraph).evaluate().isEmpty; step++) {
    final controller = _list(tester);
    final built = _builtRange(tester);
    final upward = built.isNotEmpty && paragraph < built.first;
    final span = controller.position.viewportDimension * 0.8;
    final toward = (controller.position.pixels + (upward ? -span : span))
        .clamp(0.0, controller.position.maxScrollExtent);
    if (toward == controller.position.pixels) break;
    controller.jumpTo(toward);
    await tester.pump();
  }
  expect(_row(paragraph), findsOneWidget,
      reason: '第 $paragraph 段始终没被建出来：这一臂走不到落点');
  final controller = _list(tester);
  final delta =
      tester.getTopLeft(_row(paragraph)).dy - _viewportTop(tester);
  controller.jumpTo(controller.position.pixels + delta);
  await tester.pump();
}

/// 落库那一发 `position_json`（先把假时钟推过 0.5s 合流窗口，不等它就是 null）。
Future<Map<String, Object?>?> _saved(FakeEngineRepository fake) async {
  return fake.payloadFor('reader.position_save') == null
      ? null
      : Map<String, Object?>.from(
          fake.payloadFor('reader.position_save')!['position_json']! as Map);
}

void main() {
  final table = _table();
  final tableChapters = _rows(table['chapters']);

  test('Dart 造的语料与 flet 那张表吃的是同一串字（不然下面全在测空气）', () {
    for (var i = 0; i < tableChapters.length; i++) {
      final row = tableChapters[i];
      final index = i + 1;
      expect(_int(row['chapter_index']), index - 1);
      final want = <int>[for (final value in (row['offsets']! as List)) _int(value)];
      expect(paragraphOffsets(_paragraphsOf(index)), want,
          reason: '第 $index 章的段首偏移两侧不同数：Dart 造的语料不是 flet 那支假源的语料');
      expect(_paragraphsOf(index).length, _int(row['paragraph_count']));
    }
    // flet 的 `text_length` 数的是**显示行**（`novel_paragraphs` 缩进之后）：
    // `sum(len(段)) + 2 × 非标题段数`（缩进 `　　` 两个字符、标题不缩、段间格不进）。
    // Dart 尺上那一格（`chapterTextLength`，`MountedChapter.toMount` 用的）是**没缩进**的
    // 原文 + 段间换行——两个量本来就不同。这里拿 Dart 的**显示口径**（`contentPlan` 的
    // `display` 列）与 flet 抓下的数对，比的是"同一章渲染完之后两边字符流同长"。
    for (var i = 0; i < tableChapters.length; i++) {
      final displaySum = contentPlan(_documentOf(i + 1), indent: true)
          .where((line) => line.isText)
          .fold<int>(0, (sum, line) => sum + line.display.length);
      expect(_int(tableChapters[i]['text_length']), displaySum,
          reason: '第 ${i + 1} 章的显示行总长两侧不同数：切段或缩进口径有一边改了');
    }
  });

  testWidgets('开书只挂一章：12 段、屏上 12 行、没有章名行', (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    final page = _page(tester);
    expect(page.mounts.length, 1);
    expect(page.paragraphTotal, 12);
    // 正对照：目录里有 3 章，"只挂一章"不是"一章都没挂"。
    expect(page.chapters.length, 3);
    expect(page.documentLines.length, 12, reason: '第一章不补分隔线与章名（flet 同一形状）');
    expect(find.byKey(const ValueKey('reader.title.1.第2章 连续章')), findsNothing);
  });

  testWidgets('读到底自动接第二章：起点按段不按行、视口一像素不动、loadSeq 不涨',
      (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    final loadSeq = _page(tester).loadSeq;
    final atBottom = await _driveToBottom(tester);

    final page = _page(tester);
    expect(page.mounts.length, 2, reason: '没接上下一章：追加那一臂整条断了');
    expect(fake.callsFor('reader.content'), 2);
    final appended = page.mounts[1];
    // 这一格是本轮的核心：接上来的时候已经挂着 12 **段**，而屏上是 12 行 + 补的两行。
    // 按行数累加会给第 2 章 14（再加插图那一行就是 15/16），整篇段号从此整体漂移。
    expect(appended.paragraphStart, 12);
    expect(appended.paragraphCount, 3);
    expect(page.paragraphTotal, _int((table['document']! as Map)['paragraph_total']) - 7,
        reason: '两章挂着时整篇该是 15 段（表里三章齐挂才是 22）');
    expect(page.documentLines.length, 18,
        reason: '12 段 + 分隔线 + 章名 + 3 段 + 1 张插图：占行的比产段的多三行');
    expect(page.loadSeq, loadSeq, reason: '追加动了 loadSeq = 屏会为"新一拍"重跑落位，把人弹回去');
    expect(_list(tester).position.pixels, atBottom,
        reason: '接一章就把视口挪了一格，用户看到的是书自己跳');
    // 追加进来的那一章在屏上真有行：撞名的话这里会指着第一章那第 0 段。
    await _revealRow(tester, 12);
    expect(tester.widget<Text>(_row(12)).data, startsWith('　　连续第2章第1段'),
        reason: '整篇第 12 段不是第二章第 0 段 = 键表没按整篇段号编');
    // 往上看：整篇第 0 段还是第一章那一句（往上揭那一臂由 `_revealRow` 自己判方向）。
    _list(tester).jumpTo(0);
    await tester.pump();
    expect(_row(0), findsOneWidget);
    expect(tester.widget<Text>(_row(0)).data, startsWith('　　连续第1章第1段'),
        reason: '整篇第 0 段还是第一章那一句：键表按整篇段号编，没有撞名');
  });

  testWidgets('接缝那一格落库是第二章口径：段号 0，不是整篇的 12', (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    await _driveToBottom(tester);
    expect(_page(tester).mounts.length, 2);

    await _revealRow(tester, 12);
    final page = _page(tester);
    expect(page.chapterIndex, 1, reason: '顶行已经是第二章第 0 段，章号却还留在第一章');
    expect(page.anchorParagraph, 0);
    expect(page.anchorParagraph, isNot(12),
        reason: '整篇段号没减起点就落库 = 第九十二轮那个"不可能的读数"（12 段的章报第 12 段）');

    await tester.pump(const Duration(milliseconds: 600));
    final saved = await _saved(fake);
    expect(saved, isNotNull, reason: '合流窗口推过了还没落库：落点整条没写出去');
    expect(_int(saved!['chapter_index']), 1);
    expect(_int(saved['chapter_paragraph']), 0);
    // 章内偏移**不钉 0**：探头表上那一格是 0，是因为那张表的像素是折算模型自己算的
    // （模型像素 8336 < document_start 9000，比例夹到 0）；真屏上"第 12 段在视口顶"
    // 对应的像素必然 > document_start（视口 + 分隔线 + 章名那几行），比例 > 0，
    // 估出来的偏移就落在第 0 段**里面**。要钉的是"它是本章口径的段内量"：
    // 落在第 0 段的 [起点, 下一段起点) 里；钉 0 就把屏上正确的估算判成错。
    final chapter2Offsets = <int>[
      for (final value in (tableChapters[1]['offsets']! as List)) _int(value)
    ];
    expect(_int(saved['chapter_pos']),
        allOf(greaterThanOrEqualTo(chapter2Offsets[0]),
            lessThan(chapter2Offsets[1])),
        reason: '章内偏移越出第 0 段的字符范围 = 整篇口径漏进了落库那一格');
    expect(saved['chapter_url'], _chapterUrl(2));

    // 反向对照：同一篇文档往上滚，回到第一章里面（整篇第 5 段）时章号要退回来。
    await _revealRow(tester, 5);
    final back = _page(tester);
    expect(back.chapterIndex, 0);
    expect(back.anchorParagraph, 5);
    expect(back.anchorParagraph, lessThan(back.mounts[0].paragraphCount));
  });

  testWidgets('每一发滚动之后，落库段号都不越出本章的段数', (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    await _driveToBottom(tester);
    var seen = 0;
    for (var step = 0; step < 12; step++) {
      final controller = _list(tester);
      final next = step.isEven
          ? controller.position.maxScrollExtent
          : controller.position.maxScrollExtent * 0.45;
      controller.jumpTo(next);
      await tester.pump();
      final page = _page(tester);
      final mount = page.mountFor(page.chapterIndex);
      if (mount == null) continue;
      seen++;
      expect(page.anchorParagraph, inInclusiveRange(0, mount.paragraphCount - 1),
          reason: '第 $step 跳：章 ${page.chapterIndex} 只有 ${mount.paragraphCount} 段，'
              '却把落点存成第 ${page.anchorParagraph} 段');
    }
    expect(seen, greaterThan(0), reason: '一发段号都没量到 = 这一臂什么都没测（假绿的形状）');
    expect(fake.callsFor('reader.content'), lessThanOrEqualTo(3),
        reason: '滚到底之外不该再拉正文：接两遍就是屏上出现两份同样的章');
  });

  testWidgets('迟滞：接上来的一章没让文档长高时，同一章不接第二遍', (tester) async {
    // 第 2 章给空正文：接上来之后文档总高几乎不变，"距底不足 180"这一格会**一直**成立。
    // 没有迟滞（或迟滞写反）的话，每一发滚动通知都会再去要一次第 2 章。
    final fake = _contFake(byUrl: <String, ChapterDocument>{
      _chapterUrl(2): _emptyDocumentOf(2),
      _chapterUrl(3): _documentOf(3),
    });
    await _open(tester, fake);
    await _driveToBottom(tester);
    expect(_page(tester).mounts.length, 2, reason: '空正文那一章也得挂上（屏上要有分隔线与章名）');
    final controller = _list(tester);
    final distance = controller.position.maxScrollExtent - controller.position.pixels;
    expect(distance, lessThan(_triggerBand()),
        reason: '这一臂的前提没了：距底已经超阈值，"没接第二遍"就成了自证');

    controller.jumpTo(controller.position.pixels - 20);
    await tester.pump();
    controller.jumpTo(controller.position.maxScrollExtent);
    await tester.pump();
    await tester.pump();
    expect(fake.callsFor('reader.content'), 2,
        reason: '还没退到 500px 之外就重新触发 = 贴线那几帧会把一章接两遍');
    expect(_page(tester).mounts.length, 2);
  });

  testWidgets('退开 500px 之外重新武装：再接上的是第三章，挂载账与 flet 同数',
      (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    await _driveToBottom(tester);
    expect(_page(tester).mounts.length, 2);

    // 回顶部：这一发报的 distance 必须超过迟滞带（长正文那一臂才给得出这个前提）。
    final controller = _list(tester);
    controller.jumpTo(0);
    await tester.pump();
    expect(controller.position.maxScrollExtent, greaterThan(500),
        reason: '文档没长到 500px 之外，重新武装这一臂走不到（前提要当场证）');

    await _driveToBottom(tester);
    final page = _page(tester);
    expect(page.mounts.length, 3);
    expect(page.paragraphTotal, _int((table['document']! as Map)['paragraph_total']));
    expect(page.mounts.map((mount) => mount.paragraphStart).toList(),
        <int>[for (final row in tableChapters) _int(row['paragraph_start'])],
        reason: '三章的段起点两侧不同数（flet 表里钉的是 0/12/15）');
    expect(fake.callsFor('reader.content'), 3);
    // 第三章的正文是在视口**下方**接上来的：当前像素还停在两章时的底，新行没进过
    // 视口就还没建出来——先揭到那一行再断言（这一发量的仍是"挂载账对"）。
    await _revealRow(tester, 15);
    expect(tester.widget<Text>(_row(15)).data, contains('第3章 收梢'));
  });

  testWidgets('整篇替换之后原点归零：回到第一章，段号又从 0 数、挂载表只剩一章',
      (tester) async {
    final fake = _contFake();
    await _open(tester, fake);
    await _driveToBottom(tester);
    // 迟滞要先把视口退到 500px 之外才重新武装（与上一条同一套路）：连着两次
    // `_driveToBottom` 只会接出一章，"换整篇之前挂着三章"这个前提就没了。
    _list(tester).jumpTo(0);
    await tester.pump();
    await _driveToBottom(tester);
    final before = _page(tester);
    expect(before.mounts.length, 3, reason: '前提：换整篇之前文档里真的接了两章');
    final loadSeq = before.loadSeq;

    // 点目录 / 上一章 / 跳书签 / 跳命中走的都是这一条整篇替换。
    // **不 pump 就当场读**：追加那一臂是"下一拍的滚动通知里发起的"，等到下一帧之后
    // 再看账，读到的可能是它又接了一章的账，而不是"归零"这一格。
    await _notifier(tester).openChapter(0);
    final after = _page(tester);
    expect(after.mounts.length, 1,
        reason: '整篇替换还留着接上来的两章：屏上出现两份同样的正文，段号也接着旧账数');
    expect(after.paragraphTotal, 12,
        reason: '整篇段数没从 22 归回 12 = 起点是从旧挂载账上继续累加的');
    expect(after.mounts.single.paragraphStart, 0);
    expect(after.documentLines.length, 12,
        reason: '分隔线与章名行留在行表里，下一拍就按它数 itemcount');
    expect(after.chapterIndex, 0);
    expect(after.loadSeq, loadSeq + 1,
        reason: '整篇替换不加 loadSeq = 屏不重跑落位，用户停在三章底的像素上读第一章');
    expect(fake.callsFor('reader.content'), 4,
        reason: '换章只该多拉这一发正文；前两章是接上来的，不是重拉的');

    // 归零之后的行表在屏上就是第一章那 12 段：整篇第 0 段又指回第一章那一句。
    // 这一发读的是**树**，不是状态——行表换了而屏还挂着旧行，用户看到的才是事故。
    //
    // 落位不是一帧的事：整篇替换之后屏要按第 0 段重跑落位（从旧像素一截一截贴回去），
    // 单泵一帧时树里只有落位途中的那几行。给一个有界的泵循环，等目标行真被建出来。
    for (var step = 0; step < 40 && _row(0).evaluate().isEmpty; step++) {
      await tester.pump();
    }
    expect(tester.widget<Text>(_row(0)).data, startsWith('　　连续第1章第1段'));
    expect(find.byKey(const ValueKey('reader.title.1.第2章 连续章')), findsNothing,
        reason: '接上来的那一章的章名行还在树上：itemCount 与行表不是同一份');
  });
}
