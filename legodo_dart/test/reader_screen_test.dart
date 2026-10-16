/// 阅读屏的 widget 测试：数据全部从 `reader.*` 黄金样本的**原始 dict** 进，
/// 落点那一串数与 flet 的投影表比。
///
/// 与 `reader_layout_projection_test.dart` 的分工：那一只比的是**尺**（Dart 的
/// `text_layout.dart` 与 flet 的 `content_plan` + 坐标族，两份实现算出同一串数）；
/// 这一只比的是**屏**——那一行真的挂上去了、滚到哪一段就存第几个字、
/// 点下去真的发出那一发且 payload 里带着表里那个数。
///
/// 三条仪器规矩（与 `golden_widget_test.dart` 同源）：
/// - 替身喂的是 `tests/gateway/golden/` 里的原始 dict，不是手搓样例（手搓样例只会
///   证明我自己写得对）；
/// - 落点比对的另一头是 `tests/gateway/projection/reader.content.json` 里 flet 那一屏
///   算出来的 `offsets` / `display` 两列，不是这里抄一份；
/// - 每一个"0"旁边都站着一根明知非 0 的正对照（记忆 `zero-counts-need-a-positive-control`）。
///
/// 弹窗（目录/书签/搜索）走的是**真路由**（`buildRouter()` + `extra`），不是手挂：
/// `app/router.dart` 那一发路由级 `ProviderScope` 正是弹窗容器归属的问题现场，
/// 手挂 `MaterialApp(home: ReaderScreen())` 永远绕不过去（`shell_test.dart` 的同一课）。
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
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/event_names.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/core/settings/settings_map.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';
import 'package:legodo_client/features/bookshelf/presentation/bookshelf_screen.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/find_books/data/search_scope.dart';
import 'package:legodo_client/features/find_books/presentation/find_books_screen.dart';
import 'package:legodo_client/features/reader/data/bookmark_row.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/data/chapter_row.dart';
import 'package:legodo_client/features/reader/data/content_search_result.dart';
import 'package:legodo_client/features/reader/data/reader_session.dart';
import 'package:legodo_client/features/reader/data/reading_position.dart';
import 'package:legodo_client/features/reader/data/staged_record.dart';
import 'package:legodo_client/features/reader/domain/reader_controller.dart';
import 'package:legodo_client/features/reader/presentation/reader_screen.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _json(String relative) => jsonDecode(
        File('${_repoRoot.path}/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

/// 某一发黄金样本信封里的 `data`（对象那一支）。
Map<String, Object?> _golden(String action) {
  final response =
      _json('tests/gateway/golden/$action.json')['response']! as Map<String, Object?>;
  return Map<String, Object?>.from(response['data']! as Map<String, Object?>);
}

List<Map<String, Object?>> _goldenList(String action) {
  final response =
      _json('tests/gateway/golden/$action.json')['response']! as Map<String, Object?>;
  return (response['data']! as List).cast<Map<String, Object?>>();
}

Map<String, Object?> _table(String name) =>
    _json('tests/gateway/projection/$name.json');

int _int(Object? value) => (value! as num).toInt();

/// flet 那一屏在同一份载荷上算出来的行表（9 行：8 段 + 1 张插图）。
List<Map<String, Object?>> _rows() =>
    (_table('reader.content')['rows']! as List).cast<Map<String, Object?>>();

/// flet 的段首偏移列——**这一列就是本轮要跨端比的那把尺**。
List<int> _offsets() => (_table('reader.content')['offsets']! as List)
    .map((Object? value) => _int(value))
    .toList(growable: false);

int _totalChars() => _int(_table('reader.content')['total_chars']);

/// 第 [paragraph] 段在 flet 那屏上画出来的那串字（带段首缩进）。
String _display(int paragraph) => '${_rowOf(paragraph)['display']}';

/// 同一段的**尺上**那串字（不带缩进）——缩进开关关掉后屏上就该是它。
String _ruler(int paragraph) => '${_rowOf(paragraph)['ruler']}';

Map<String, Object?> _rowOf(int paragraph) => _rows().firstWhere(
    (row) => row['paragraph_index'] != null && _int(row['paragraph_index']) == paragraph);

const String _sourceUrl = 'http://127.0.0.1:8797/g3';

Map<String, Object?> _goldenBook() =>
    Map<String, Object?>.from(_golden('reader.open')['book']! as Map);

/// 章 url 按 `reader.toc` 的行序号取（落库的 `chapter_index` 就是这一把坐标）。
String _chapterUrl(int index) => _toc()[index].url;

List<ChapterRow> _toc() =>
    _goldenList('reader.toc').map(ChapterRow.fromJson).toList(growable: false);

/// 默认替身：reader.* 那一族全部现成，数据一律来自黄金样本。
FakeEngineRepository _readerFake({
  Map<String, Object?>? position,
  List<ChapterRow>? toc,
  ChapterDocument? document,
  List<BookmarkRow>? bookmarks,
  ContentSearchResult? contentSearch,
  StagedRecord? staged,
  SettingsMap settings = SettingsMap.empty,
  List<SearchScope> scopes = const <SearchScope>[],
  List<BookshelfItem> bookshelf = const <BookshelfItem>[],
  EngineFailure? failure,
}) {
  final sessionJson = Map<String, Object?>.from(_golden('reader.open'));
  if (position != null) sessionJson['position'] = position;
  return FakeEngineRepository(
    failure: failure,
    settings: settings,
    scopes: scopes,
    bookshelf: bookshelf,
    session: ReaderSession.fromJson(sessionJson),
    toc: toc ?? _toc(),
    document: document ?? ChapterDocument.fromJson(_golden('reader.content')),
    bookmarks: bookmarks ??
        _goldenList('reader.bookmarks').map(BookmarkRow.fromJson).toList(),
    contentSearch: contentSearch,
    staged: staged,
  );
}

/// 存档里那一格的原样（`reader.position_save` 的回话 = 服务端落库并收口后的那一份）。
Map<String, Object?> _savedPositionJson() => Map<String, Object?>.from(
    _json('tests/gateway/golden/reader.position_save.json')['response']!
        as Map<String, Object?>)['data']! as Map<String, Object?>;

/// 真走路由开书：起壳 → `push(readerRoute, extra: args)` → 阅读屏。
Future<GoRouter> _open(WidgetTester tester, FakeEngineRepository fake,
    {ReaderArgs? args}) async {
  final router = buildRouter();
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: MaterialApp.router(routerConfig: router),
  ));
  await tester.pumpAndSettle();
  // **不 await 这一发**：`GoRouter.push` 的那个 Future 回话的是"这一页被 pop 的
  // 那一刻"，不是"这一页建好了"——await 它，测试就挂在开书上（真机里调用方也没等它）。
  unawaited(router.push<void>(readerRoute,
      extra: args ?? ReaderArgs(sourceUrl: _sourceUrl, book: _goldenBook())));
  await tester.pumpAndSettle();
  return router;
}

Finder _row(int paragraph) => find.text(_display(paragraph));

/// 正文视口那一格的顶边（屏内 `_paragraphAtViewportTop` 量的就是同一个边）。
double _viewportTop(WidgetTester tester) =>
    tester.getTopLeft(find.byKey(const Key('reader.body'))).dy;

/// 这一屏此刻滚了多少像素（从 ListView 自己挂的那个 controller 读）。
///
/// 为什么需要它：`_row(n)` 那些"落在上面第几段"的对照要求那一行**还在树上**，
/// 而 ListView 只建视口附近那一截——用"滚过没滚过"当正对照不依赖任何一行建没建。
double _pixels(WidgetTester tester) => _listPosition(tester).pixels;

/// 那一格滚动位的两栏（像素 + 总高）：段内估算的期望值要现读这两栏自己算。
ScrollPosition _listPosition(WidgetTester tester) => tester
    .widget<ListView>(find.byKey(const Key('reader.body')))
    .controller!
    .position;

/// 视口顶那一行是哪一段：把 8 段的行顶与列表顶比，取"已经过顶"里最靠下的那段。
///
/// 这是**外面**另量一次（用 flet 表里的 `display` 串指认每一行），不是读屏自己记的数。
int? _paragraphAtViewportTop(WidgetTester tester) {
  final top = _viewportTop(tester);
  int? best;
  double bestDy = double.negativeInfinity;
  for (var paragraph = 0; paragraph < _offsets().length; paragraph++) {
    final finder = _row(paragraph);
    if (finder.evaluate().isEmpty) continue;
    final dy = tester.getTopLeft(finder).dy;
    if (dy <= top + 2 && dy > bestDy) {
      best = paragraph;
      bestDy = dy;
    }
  }
  return best;
}

/// 落库那一发的 `position_json`（没发过就是 null）。
Map<String, Object?>? _savedPayload(FakeEngineRepository fake) {
  final payload = fake.payloadFor('reader.position_save');
  return payload == null
      ? null
      : Map<String, Object?>.from(payload['position_json']! as Map);
}

/// 屏上此刻的落点段号（从这一屏自己那份容器读，`_scoped` 用的就是它）。
ReaderPage _page(WidgetTester tester) {
  final container = ProviderScope.containerOf(
      tester.element(find.byType(ReaderScreen)), listen: false);
  return container.read(readerProvider).value!;
}

/// 把弹窗子树关掉（抽屉没有关闭按钮，走它自己那一层导航器）。
Future<void> _closeSheet(WidgetTester tester, String listKey) async {
  Navigator.of(tester.element(find.byKey(Key(listKey)))).pop();
  await tester.pumpAndSettle();
}

void main() {
  group('开书：三处读数都吃服务端那一发', () {
    test('投影表与样本先对上（仪器接上了没有）', () {
      // 正对照：行数 0 时下面所有循环都是空转，"全对"是假的。
      expect(_rows().length, 9, reason: '当场数过：8 段 + 1 张受保护插图');
      expect(_offsets(), <int>[0, 168, 1578, 1884, 4053, 4175, 5286, 5799]);
      expect(_totalChars(), _int(_golden('reader.content')['totalChars']),
          reason: 'flet 算的总长与服务端那一发回的不是同一个数');
      expect(_toc().length, 6);
    });

    testWidgets('顶栏/底部两句都是服务端那一发的数', (tester) async {
      await _open(tester, _readerFake());
      // 顶栏是**当前章名**（目录那一行），不是会话书名：两边都给的话就成了两处各说各话。
      expect(find.text('第1章 测试章'), findsOneWidget);
      // 底部一行是 `进度 · 书名行` 一整句（`StateBar` 只有 message 空着才回落到它）：
      // 分母是滤掉卷标题之后的那批（这份样本没有卷标题，所以 6 = 目录条数 = flet 那句）。
      expect(find.text('1/6 · 第1章 测试章 · 《阅读基线书》 · 作者戊 · 共 6 章'),
          findsOneWidget);
    });

    testWidgets('屏上的 9 行逐字对上 flet 投影（图那一行占一行、不产段）',
        (tester) async {
      // 把测试窗口拉高到能一次装下整章：ListView 只建视口附近那一截，
      // 600 高的窗口里后面那几行压根没建出来，逐行比就成了"只比前三行"。
      await tester.binding.setSurfaceSize(const Size(1200, 4000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await _open(tester, _readerFake());

      for (final row in _rows()) {
        if (row['kind'] == 'text') {
          expect(find.text('${row['display']}'), findsOneWidget,
              reason: '第 ${row['row']} 行（段 ${row['paragraph_index']}）没上屏');
        } else {
          // 媒体行没有文本可比（`display` 那一格是空的），按行键认：
          // 键里带 url，就是黄金载荷里那张受保护插图。
          expect(
              find.byKey(const ValueKey(
                  'reader.media.image.$_sourceUrl/pic.jpg')),
              findsOneWidget,
              reason: '插图那一行整条丢了（受保护图只剩 url 是客户端的把手）');
        }
      }
      // 行号不等于段号：图夹在第 3、4 段之间，行 5 才是段 4。
      expect(_int(_rows()[5]['row']), 5);
      expect(_int(_rows()[5]['paragraph_index']), 4);
      expect(find.byKey(const ValueKey('reader.text.4')), findsOneWidget);
    });

    testWidgets('路由没把书递进来：说"没有收到书"，也不发 reader.open',
        (tester) async {
      final unfed = _readerFake();
      // `extra` 漏了 = `app/router.dart` 的 pageBuilder 回落到 `ReaderArgs.empty()`，
      // 控制器**不许**拿这份空快照去开一本 0 章的空书。
      final router = buildRouter();
      await tester.pumpWidget(ProviderScope(
        overrides: [engineRepositoryProvider.overrideWithValue(unfed)],
        child: MaterialApp.router(routerConfig: router),
      ));
      await tester.pumpAndSettle();
      unawaited(router.push<void>(readerRoute)); // 没有 extra
      await tester.pumpAndSettle();

      expect(find.textContaining('这一本书打不开'), findsOneWidget);
      expect(find.textContaining('阅读器没有收到书'), findsOneWidget);
      expect(unfed.callsFor('reader.open'), 0,
          reason: 'args 为空还去开书 = 拿空快照建一个 0 章会话，屏幕上像"这本书坏了"');
      expect(unfed.callsFor('reader.toc'), 0);
      expect(unfed.callsFor('reader.content'), 0);

      // 正对照：换一台同样的替身、这一回带上 args——上面那些 0 是"没发"，
      // 不是"替身没接上"或"路由压根进不去"。
      await _reset(tester);
      final fed = _readerFake();
      await _open(tester, fed);
      expect(fed.callsFor('reader.open'), 1);
      expect(fed.callsFor('reader.content'), 1);
    });
  });

  group('落点：滚到哪一段就存第几个字（与 flet 投影同一把尺）', () {
    testWidgets('续读恢复按段序号落位：存档里那个过期的偏移数不许赢', (tester) async {
      // 用真样本 `reader.position_save` 的回话当会话存档：`chapter_paragraph=2` 而
      // `chapter_pos=137`——137 在这把尺上还落在**第 0 段**里（offsets[1]=168）。
      // 谁赢一眼可见：落在第 2 段才是 flet 那边 `jump` 的口径（段序号优先）。
      final saved = _savedPositionJson();
      expect(_int(saved['chapter_paragraph']), 2);
      expect(_int(saved['chapter_pos']), lessThan(_offsets()[1]));
      final fake = _readerFake(position: saved);
      await _open(tester, fake);

      final top = _viewportTop(tester);
      expect((tester.getTopLeft(_row(2)).dy - top).abs(), lessThanOrEqualTo(2),
          reason: '落点没在第 2 段的段首：恢复那一臂坏了');
      expect(_pixels(tester), greaterThan(0),
          reason: '像素还是 0 = 根本没滚，上一条的"对上了"是章首那一拍的巧合');
      // 规矩 4：整篇替换那一拍的"偏移 0"通知不许落库，否则一开书就把上次进度抹成 0。
      expect(fake.callsFor('reader.position_save'), 0);
    });

    testWidgets('只有字符偏移的旧存档：按偏移算段号，仍落在那一段的段首',
        (tester) async {
      final offsets = _offsets();
      final fake = _readerFake(position: <String, Object?>{
        ..._savedPositionJson(),
        'chapter_paragraph': null, // 老数据：从没记过段号（null 与 0 是两件事）
        'chapter_pos': offsets[5],
      });
      await _open(tester, fake);
      final top = _viewportTop(tester);
      expect((tester.getTopLeft(_row(5)).dy - top).abs(), lessThanOrEqualTo(2),
          reason: '偏移 ${offsets[5]} 该读回第 5 段，Dart 这把尺与 flet 的不同源了');
      expect(_page(tester).anchorParagraph, 5);
    });

    testWidgets('滚出第 n 段就存 offsets[n]：payload 里的数是 flet 表里那一格',
        (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -900));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600));

      final expected = _paragraphAtViewportTop(tester);
      expect(expected, isNotNull, reason: '视口顶一行都认不出来 = 仪器没接上');
      expect(expected, greaterThan(0),
          reason: '还停在第 0 段 = 滚动没被报上去，下面的比对是空的');
      final saved = _savedPayload(fake);
      expect(saved, isNotNull, reason: '滚过了却没落库：合流那一臂断了');
      expect(_int(saved!['chapter_paragraph']), expected);
      // 字符偏移是**段内估算**：段首 + 段内那一截按像素比例插——flet `track` 的
      // 同一份算术（投影表 `reader.continuous.json` 里第 5 段那一发读回的就是 141，
      // 而它的段首是 135；段内那一格本来就是估算，不是事实）。所以这里不钉
      // `offsets[n]`（那是"没滚过"才该存的值）；要钉的是载荷真按同一份算术算：
      // 把屏此刻的像素/总高现读出来，在**测试侧**独立算一遍。
      final offsets = _offsets();
      final paragraph = expected!;
      final position = _listPosition(tester);
      final ratio = position.pixels / position.maxScrollExtent;
      expect(ratio, greaterThan(0.02),
          reason: '这一臂的前提没了：滚动位置贴在段首上，下面两式同值，断言空转');
      final span = offsets[paragraph + 1] - offsets[paragraph] - 1;
      expect(_int(saved['chapter_pos']), offsets[paragraph] + (span * ratio).toInt(),
          reason: '落库的字符偏移不是"段首 + 段内那一截"：这一发没走 flet `track` 的算术');
      expect(_int(saved['chapter_pos']), greaterThanOrEqualTo(offsets[paragraph]),
          reason: '偏移落到了第 $paragraph 段之前：段口径整体漂了');
      expect(_int(saved['chapter_pos']), lessThan(offsets[paragraph + 1]),
          reason: '偏移越过了第 $paragraph 段的段尾：段内那一截没夹在段区间里');
      expect(saved['chapter_index'], 0);
      expect(saved['chapter_url'], _golden('reader.content')['chapterUrl'],
          reason: '章节定位要用**回话里**那一条，不是请求里传的那一条');
      expect(saved['font_size'], 16, reason: '没读排版时服务端那一份的默认字号');
    });

    testWidgets('0.5s 合流：窗口里跨两段也只落一发（flet throttle 同值）',
        (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -500));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -500));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));

      // 正对照：窗口里状态**确实**换了两次，不然"只落一发"是因为压根没滚。
      expect(_page(tester).anchorParagraph, greaterThan(1),
          reason: '两次拖动没换段，这一臂就在测空转');
      expect(fake.callsFor('reader.position_save'), 0,
          reason: '窗口还没到点就落库 = 一拍一发，进度写库会占满网关排期');
      await tester.pump(const Duration(milliseconds: 600));
      expect(fake.callsFor('reader.position_save'), 1);
    });

    testWidgets('服务端夹过的落点以回话为准：屏上的段号跟着改，不拿自己算的当结论',
        (tester) async {
      final offsets = _offsets();
      final clamped = Map<String, Object?>.from(
          _json('tests/gateway/golden/reader.position_save--clamped.json')
                  ['response']! as Map<String, Object?>)['data']!
          as Map<String, Object?>;
      // 替身把 `position` 这一格同时当 `position_save` 的回话（见它的方法注释）：
      // 这一臂要的就是"请求与回话不是同一个数"，样本那份正是服务端夹过的形状。
      final fake = _readerFake()..position = ReadingPosition.fromJson(clamped);
      await _open(tester, fake);
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -2600));
      await tester.pump();
      // 合流窗口要**自己把假时钟推过 0.5s**：`pumpAndSettle` 只在"还有帧排着"的时候
      // 推进时钟，而那个 `Timer` 不排帧——用它就永远等不到落库那一发（本轮量到的形状：
      // 屏上落点已经到第 5 段，`reader.position_save` 却一发都没有）。
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pumpAndSettle(); // position_save → 回话夹到章尾 → 屏上重挂段号

      final saved = _savedPayload(fake);
      expect(saved, isNotNull);
      expect(_int(saved!['chapter_pos']), lessThan(_int(clamped['chapter_pos'])),
          reason: '请求本身就是被夹的那一发才叫"夹"：${saved['chapter_pos']}'
              ' vs 回话 ${clamped['chapter_pos']}');
      expect(_int(clamped['chapter_pos']), _totalChars(),
          reason: '样本那一发夹到的是整章的右端点');
      expect(offsets.last, lessThan(_totalChars()),
          reason: '章尾（总长）不是最后一个段首：两个数当同一个用就是本轮要拆的错');
      // 可分辨的正对照：屏上的段号与**请求里**那个不一样了，说明它吃的是回话，
      // 不是自己算的那个数（若两侧同值，这一条断言就是空转）。
      expect(_int(saved['chapter_paragraph']),
          isNot(_int(clamped['chapter_paragraph'])),
          reason: '请求的段号恰好等于回话段号 = 这一臂没在测"回话赢"');
      expect(_page(tester).anchorParagraph, _int(clamped['chapter_paragraph']),
          reason: '回话夹过之后屏上还挂着请求里那个段号 = 存档与屏幕各说一套');
    });

    testWidgets('翻到下一章：先把旧章的落点写完，再发 reader.content',
        (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -500));
      await tester.pump();
      await tester.tap(find.byKey(const Key('reader.next')));
      await tester.pumpAndSettle();

      final order = fake.callOrder;
      final secondContent = order.lastIndexOf('reader.content');
      expect(secondContent, greaterThan(order.indexOf('reader.content')),
          reason: '压根没换章：两次 reader.content 是同一发的重复');
      final flush = order.indexOf('reader.position_save');
      expect(flush, isNonNegative, reason: '换章前没把旧章落点写完');
      expect(flush, lessThan(secondContent),
          reason: '旧章那一次的 position_save 排在换章之后 = 写到了新章头上');
      // 写完的是**旧章**那一格：翻章之后落库才带新章的 index（下一条臂量的是这个）。
      // 这里必须按**先后**取第一发：`payloadFor` 只留最近那一份，而换章之后合流窗口
      // 里还会再落一发新的（那一份带的是新章的 index，读它会得出"顺序是对的、数却是错的"）。
      final flushPayload = Map<String, Object?>.from(
          fake.payloadsFor('reader.position_save').first!['position_json']! as Map);
      expect(_int(flushPayload['chapter_index']), 0);
      // 这里**不能**拿 url 当新旧章的判据：替身对每一章都回同一份正文载荷，而落库
      // 那一格取的正是回话里的 `document.chapterUrl`（规矩 1）。新旧之分靠
      // `chapter_index` 那一把服务端序号。
      expect(flushPayload['chapter_url'], _golden('reader.content')['chapterUrl']);
      expect(fake.payloadFor('reader.content')!['chapter_url'], _chapterUrl(1));
      expect(find.textContaining('2/6 · 第2章 测试章'), findsOneWidget);
    });

    testWidgets('落库的 chapter_index 是服务端的行序号，屏上那句是滤卷之后的序号',
        (tester) async {
      // 规矩 2 的两把坐标：注入一条卷标题（`is_volume` 是协议面 `reader.toc` 的字段），
      // 翻章跳过它之后，"第 3/5 章"与 `chapter_index=3` 必须同时成立。
      final rows = _toc();
      final volume = rows[2];
      final toc = <ChapterRow>[
        ...rows.take(2),
        ChapterRow(
            name: '第一卷',
            // 卷标题在 Legado 里可以带 url（那是一条真章的 url），这里给空串考的是
            // "不可点"那一半：`is_volume` 单独就该挡住它。
            url: '',
            index: volume.index,
            isVolume: true,
            isVip: false,
            isPay: false,
            tag: '',
            wordCount: ''),
        ...rows.skip(3),
      ];
      final fake = _readerFake(toc: toc);
      await _open(tester, fake);
      // 两次"下一章"：0 → 1（那条卷标题在 2，第一次够不着它），1 → 跳过卷标题到 3。
      // 只点一次就期待 ch/4 是测试自己的坐标算错了——那一臂量的是"跳过"，不是"落在"。
      await tester.tap(find.byKey(const Key('reader.next')));
      await tester.pumpAndSettle();
      expect(fake.payloadFor('reader.content')!['chapter_url'], _chapterUrl(1),
          reason: '正对照：第一发落在紧邻的那一章上，说明翻页真的在走');
      await tester.tap(find.byKey(const Key('reader.next')));
      await tester.pumpAndSettle();

      expect(fake.payloadFor('reader.content')!['chapter_url'], _chapterUrl(3),
          reason: '翻页没跳过卷标题 = 落到一条没有正文的"卷"上');
      expect(find.textContaining('3/5 · 第4章 测试章'), findsOneWidget,
          reason: '分母数了卷标题（flet `_update_chapter_navigation` 滤卷）');
      expect(find.text('第1章 测试章').evaluate().length, 0,
          reason: '顶栏还挂着旧章名 = 没换章');

      // 换章之后滚一下并落库：那一发的 `chapter_index` 要是服务端行序号 3，
      // 而屏上同时写着"3/5"——两套数各干各的事，这里把它们钉在同一帧上。
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -700));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 600));
      final saved = _savedPayload(fake);
      expect(saved, isNotNull, reason: '滚过了没落库：这一臂没有读数');
      expect(_int(saved!['chapter_index']), 3,
          reason: '落库用了滤卷之后的序号（服务端 `chapters[2]` 是那条卷标题）');
      // 这一臂不许拿 `saved['chapter_url']` 做判据：替身对每一章回的是同一份正文
      // 载荷，而那一格取的正是回话里的 url。"翻到的确实是第 4 章"由请求那一侧钉：
      expect(fake.payloadFor('reader.content')!['chapter_url'], _chapterUrl(3),
          reason: '换章那一发要的 url 与序号不同源 = 服务端按 `chapters[index]` 认章会认错');
      expect(find.textContaining('3/5 · 第4章 测试章'), findsOneWidget);
    });

    testWidgets('到头就没有下一发：最后一章的"下一章"是灰的', (tester) async {
      final fake = _readerFake(position: <String, Object?>{
        ..._savedPositionJson(),
        'chapter_index': 5,
      });
      await _open(tester, fake);
      expect(find.textContaining('6/6 · 第6章 测试章'), findsOneWidget);
      expect(
          tester
              .widget<FloatingActionButton>(
                  find.byKey(const Key('reader.next')))
              .onPressed,
          isNull);
      expect(
          tester
              .widget<FloatingActionButton>(
                  find.byKey(const Key('reader.prev')))
              .onPressed,
          isNotNull);
    });
  });

  group('目录 / 书签 / 书内搜索', () {
    testWidgets('目录抽屉照列卷标题，但它不可点', (tester) async {
      final rows = _toc();
      final toc = <ChapterRow>[
        ...rows.take(2),
        ChapterRow(
            name: '第一卷',
            url: rows[2].url,
            index: rows[2].index,
            isVolume: true,
            isVip: false,
            isPay: false,
            tag: '',
            wordCount: ''),
        ...rows.skip(3),
      ];
      await _open(tester, _readerFake(toc: toc));
      await tester.tap(find.byKey(const Key('reader.toc')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('reader.toc.list')), findsOneWidget);
      // 序号要与服务端的 `chapters[index]` 对齐，所以卷标题**照列**，只是不可点。
      expect(find.text('第一卷'), findsOneWidget);
      expect(
          tester
              .widget<ListTile>(find.ancestor(
                  of: find.text('第一卷'), matching: find.byType(ListTile)))
              .onTap,
          isNull);
      // 正对照：同一条抽屉里旁边那一行是可点的，"onTap 为 null"不是整表都禁用了。
      expect(
          tester
              .widget<ListTile>(find.ancestor(
                  of: find.text(rows[3].name), matching: find.byType(ListTile)))
              .onTap,
          isNotNull);
      await _closeSheet(tester, 'reader.toc.list');
    });

    testWidgets('书签那一格是比例不是字数；点它是换章、不落到 offset',
        (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.tap(find.byKey(const Key('reader.bookmarks')));
      await tester.pumpAndSettle();
      // 真样本：`chapter_index=1`、`offset=0.25`（`BookmarkRow` 文件头那条：
      // 这一列存的是 `chapter_ratio`，标成"第几字"就是把两种位置混着给用户）。
      expect(find.text('G3 样本书签'), findsOneWidget);
      expect(find.text('第 2 章 · 25%'), findsOneWidget);
      expect(find.textContaining('1578 字'), findsNothing);

      await tester.tap(find.text('G3 样本书签'));
      await tester.pumpAndSettle();
      expect(fake.payloadFor('reader.content')!['chapter_url'], _chapterUrl(1),
          reason: '跳书签要重开的是那一章（flet `jump_to_bookmark` 只按 chapter_index）');
      final top = _viewportTop(tester);
      expect((tester.getTopLeft(_row(0)).dy - top).abs(), lessThanOrEqualTo(2),
          reason: '跳过去要落在**章首**：offset 那一格从来不是落点尺（flet 同一条）');
      expect(_page(tester).anchorParagraph, 0,
          reason: '把书签的比例当落点段号用了 = 两种位置混着算');
    });

    testWidgets('加书签：比例算自 flet 那把尺，摘要就是当前章名', (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.drag(find.byKey(const Key('reader.body')), const Offset(0, -900));
      await tester.pump();
      final paragraph = _page(tester).anchorParagraph;
      expect(paragraph, greaterThan(0),
          reason: '没滚出去 = 比例恒为 0，下面那条比对成了自证');
      await tester.tap(find.byKey(const Key('reader.bookmarks')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('reader.bookmark.add')));
      await tester.pumpAndSettle();

      final payload = fake.payloadFor('reader.bookmark_add');
      expect(payload, isNotNull);
      final expected = _offsets()[paragraph] / _totalChars();
      expect(payload!['offset'], closeTo(expected, 1e-12),
          reason: '这一格存的是 chapter_ratio（`chapter_pos / total_chars`），'
              '分子分母都得是 flet 表里那两个数');
      expect(payload['chapter_index'], 0);
      expect(payload['summary'], _toc()[_int(payload['chapter_index'])].name);
      expect(find.text('已加书签：${payload['summary']}'), findsOneWidget);
    });

    testWidgets('命中那一发：摘录剥掉 <mark>，句子里报的是命中处数', (tester) async {
      final sample = ContentSearchResult.fromJson(_golden('reader.search_content'));
      // 样本当场数过：3 处命中（`indexedChapters: 3`——只搜了已索引的那三章，
      // 第 2/3/4 章各一处。语料里六章都带"第3段"，命中数是被索引窗口定的，
      // 不是语料定的；这一格跟着样本走）。
      expect(sample.hits.length, 3, reason: '样本当场数过：3 处命中');
      expect(sample.hits.first.snippet, contains('<mark>'),
          reason: '服务端不剥标记，剥是界面那一侧的事');
      final fake = _readerFake(contentSearch: sample);
      await _open(tester, fake);
      await tester.tap(find.byKey(const Key('reader.search')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('reader.search.input')), '第3段');
      await tester.tap(find.byKey(const Key('reader.search.submit')));
      await tester.pumpAndSettle();

      expect(find.text('「第3段」命中 3 处'), findsOneWidget);
      expect(find.text('第2章第3段字句读纸张看灯下山风月夜…'), findsOneWidget,
          reason: '标记没剥或整条摘录丢了：屏上要的是纯文本');
      expect(find.textContaining('<mark>'), findsNothing);
      expect(fake.payloadFor('reader.search_content')!['keyword'], '第3段');
      // 命中行里的坐标是"第几章 · 第几段 · 第几个字"三个数，都来自样本那一发。
      expect(find.text('第 2 章 · 第 3 段 · 1578 字'), findsOneWidget);
    });

    testWidgets('三种 0 命中说三句话，不许合成一句"今天没有数据"', (tester) async {
      ContentSearchResult arm({int hits = 0, int indexed = 4, bool fts = true}) =>
          ContentSearchResult(
              hits: List.generate(hits, (i) => const ContentHit(
                  chapterIndex: 0, paraIndex: 0, characterOffset: 0, snippet: '正文')),
              written: 0,
              indexedChapters: indexed,
              ftsAvailable: fts);
      // 三条各开一次屏：同一屏里换载荷会把上一条的文案留在状态行上。
      final arms = <String, ContentSearchResult>{
        '这台机器的 SQLite 没编 FTS5，书内搜索用不了': arm(fts: false, indexed: 0),
        '还没有任何一章进了索引（先读几章再搜）': arm(indexed: 0),
        '「风月夜」在已索引的 4 章里没有': arm(indexed: 4),
      };
      // 正对照：三句必须互不相同，否则"分开说"是假的。
      expect(arms.keys.toSet().length, 3);
      for (final entry in arms.entries) {
        await _reset(tester);
        final fake = _readerFake(contentSearch: entry.value);
        await _open(tester, fake);
        await tester.tap(find.byKey(const Key('reader.search')));
        await tester.pumpAndSettle();
        await tester.enterText(find.byKey(const Key('reader.search.input')), '风月夜');
        await tester.tap(find.byKey(const Key('reader.search.submit')));
        await tester.pumpAndSettle();
        // 那句要**在弹窗里**：同一句话这会儿还挂在弹窗背后的底部状态行上
        // （`page.echo` 只有一个槽），整屏数就是 2 份——数错的那一份不是断言要的。
        expect(
            find.descendant(
                of: find.byType(AlertDialog), matching: find.text(entry.key)),
            findsOneWidget,
            reason: '说了另一句：${entry.value}');
        expect(find.byKey(const Key('reader.search.results')), findsNothing);
        expect(fake.callsFor('reader.search_content'), 1);
      }
    });

    testWidgets('空关键词不发那一发：那是"用户没打字"，不是"搜了没命中"',
        (tester) async {
      final fake = _readerFake(contentSearch: const ContentSearchResult(
          hits: <ContentHit>[], written: 0, indexedChapters: 4, ftsAvailable: true));
      await _open(tester, fake);
      await tester.tap(find.byKey(const Key('reader.search')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('reader.search.submit')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('reader.search_content'), 0);
      // 同上一条臂：那句在弹窗里（背后状态行上是同一句话的第二个副本）。
      expect(
          find.descendant(of: find.byType(AlertDialog),
              matching: find.text('要搜什么总得给两个字')),
          findsOneWidget);
    });
  });

  group('失败与空，是三句不同的话', () {
    testWidgets('这本书没有目录 ≠ 这一章没有正文 ≠ 引擎没起来', (tester) async {
      // 一：没有目录（chapterIndex = -1，连正文那一发都不该发）
      await _reset(tester);
      var fake = _readerFake(toc: const <ChapterRow>[]);
      await _open(tester, fake);
      expect(find.text('这本书没有目录，没有可读的章'), findsOneWidget);
      expect(find.text('这本书没有目录'), findsOneWidget,
          reason: '底部那一行要说的是"没有目录"这一种（echo 占位，不是进度句）');
      expect(find.textContaining('未选择章节'), findsNothing,
          reason: '进度句占着状态行 = 同一屏两句说同一件事还说得不一样的形状');
      expect(fake.callsFor('reader.content'), 0,
          reason: '没章还发 reader.content = 拿空 url 去要正文');
      expect(fake.callsFor('reader.position_save'), 0);

      // 二：有目录、正文是空的（这一章真没字）
      await _reset(tester);
      fake = _readerFake(document: ChapterDocument.fromJson(<String, Object?>{
        ..._golden('reader.content'),
        'blocks': const <Object?>[],
      }));
      await _open(tester, fake);
      expect(find.text('这一章没有正文（可能这条源读不出来）'), findsOneWidget);
      expect(find.text('这本书没有目录，没有可读的章'), findsNothing);
      expect(fake.callsFor('reader.content'), 1,
          reason: '正对照：这一臂**发了**正文那一发，上一条的 0 才是"没发"');
      expect(find.textContaining('1/6 · 第1章 测试章'), findsOneWidget,
          reason: '目录是在的：这一臂不能说成"没有目录"');

      // 三：那一发压根没成（故障不许报成"这本书是空的"）
      await _reset(tester);
      fake = _readerFake(
          failure: const EngineFailure(EngineFailureKind.unavailable, '引擎没起来'));
      await _open(tester, fake);
      expect(find.textContaining('这一本书打不开'), findsOneWidget);
      expect(find.textContaining('引擎没起来'), findsOneWidget);
      expect(find.textContaining('没有目录'), findsNothing);
      expect(find.textContaining('没有正文'), findsNothing);
      expect(find.textContaining('1/6'), findsNothing);
    });

    testWidgets('目录重取失败：留旧目录、留那句失败，不说"已刷新"', (tester) async {
      final fake = _readerFake();
      await _open(tester, fake);
      await tester.tap(find.byKey(const Key('reader.toc')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('reader.toc'), 1, reason: '开书时那一发是前置条件');
      fake.failure = const EngineFailure(EngineFailureKind.engine, '源挂了');
      await tester.tap(find.byKey(const Key('reader.toc.refresh')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('reader.toc'), 2);
      expect(find.byKey(const Key('reader.toc.list')), findsOneWidget,
          reason: '那一发失败之后抽屉空了 = 把故障说成"没有目录"');
      expect(find.text('第6章 测试章'), findsOneWidget);
      // 失败要出声（横幅走 failures 流），而状态行不许在同一帧说"目录已刷新"。
      expect(find.textContaining('源挂了'), findsOneWidget);
      expect(find.textContaining('目录已刷新'), findsNothing);
      expect(find.textContaining('目录没刷新'), findsOneWidget);
    });
  });

  group('退出：收尾顺序是硬约束（规矩 6）', () {
    // 退出走屏上那颗「退出阅读」（顶栏 `CLOSE`，与 flet 工具栏第一颗同一发）：
    // `GoRouter.pop()` **绕不过** `PopScope(canPop: false)`——它直接减页面列表，
    // 不走 `maybePop`/系统键那条路，于是拿它当"用户退出"会测出一条根本没发生的
    // 时间线（收尾那一发压根没发，屏幕上还挂着阅读屏）。
    Future<void> leave(WidgetTester tester) async {
      await tester.tap(find.byKey(const Key('reader.exit')));
      await tester.pumpAndSettle();
    }

    testWidgets('顶栏那颗「退出」不是摆设：它走的就是这一族收尾', (tester) async {
      // 正对照（`zero-counts-need-a-positive-control`）：上面四条臂都在数"没发生"，
      // 这一条钉"按钮按下去真的接到了那一族"——少这一条，四条臂的 0 也可能是
      // 那颗按钮压根没挂上监听（`router.pop()` 绕不过 `PopScope` 就是同一课）。
      final fake = _readerFake();
      await _open(tester, fake);
      expect(fake.callsFor('reader.close'), 0);
      expect(fake.callsFor('reader.staged'), 0);
      await leave(tester);
      expect(fake.callsFor('reader.staged'), 1);
      expect(fake.callsFor('reader.close'), 1);
    });

    testWidgets('在架的书不问：直接关会话，save_progress=true', (tester) async {
      final fake = _readerFake(); // staged 不给 = 在架的书
      await _open(tester, fake);
      await leave(tester);

      expect(find.byType(AlertDialog), findsNothing);
      expect(fake.callsFor('reader.staged'), 1);
      expect(fake.callsFor('reader.staged_promote'), 0);
      expect(fake.callsFor('reader.staged_discard'), 0);
      expect(fake.callsFor('reader.close'), 1);
      expect(fake.payloadFor('reader.close')!['save_progress'], isTrue);
      expect(find.byType(ReaderScreen), findsNothing);
      expect(find.byType(FindBooksScreen), findsOneWidget, reason: '没落回原来那一屏');
    });

    testWidgets('试读的书要问，而且弹窗还开着就不许关会话', (tester) async {
      final fake = _readerFake(staged: StagedRecord.fromJson(_golden('reader.staged')));
      await _open(tester, fake);
      await leave(tester);

      // 弹窗标题与那颗转正按钮同叫「放入书架」，整屏数文本会是两份——问话在不在，
      // 认弹窗这个类型与那句独有的问句。
      expect(find.byType(AlertDialog), findsOneWidget);
      expect(
          find.text('是否将《G3 试读书》放入书架？\n不放入会删除这本书的阅读进度与缓存。'),
          findsOneWidget);
      // 这一条就是"关完再问"那个错法的护栏：会话一关，服务再也认不出那条暂存记录。
      expect(fake.callsFor('reader.close'), 0,
          reason: '问都没答完就把会话关了：之后那一发 staged_promote 认不出记录');
      expect(fake.callOrder.indexOf('reader.position_save'),
          lessThan(fake.callOrder.indexOf('reader.staged')),
          reason: '先问暂存再落进度 = 关会话时手里那份落点还是旧的');

      await tester.tap(find.byKey(const Key('reader.staged.promote')));
      await tester.pumpAndSettle();
      final order = fake.callOrder;
      expect(order.lastIndexOf('reader.staged_promote'),
          lessThan(order.lastIndexOf('reader.close')),
          reason: '转正必须排在关会话之前（`_finish_session` 同一顺序）');
      expect(fake.payloadFor('reader.close')!['save_progress'], isTrue);
      expect(find.byType(FindBooksScreen), findsOneWidget);
    });

    testWidgets('答"不放入"：discard 在前，且 close 不再落一遍进度', (tester) async {
      final fake = _readerFake(staged: StagedRecord.fromJson(_golden('reader.staged')));
      await _open(tester, fake);
      await leave(tester);
      await tester.tap(find.byKey(const Key('reader.staged.discard')));
      await tester.pumpAndSettle();
      final order = fake.callOrder;
      expect(order.lastIndexOf('reader.staged_discard'),
          lessThan(order.lastIndexOf('reader.close')));
      // 不放入时那一发是 false：暂存记录连同进度与缓存一起删了，再落一遍就是写回去。
      expect(fake.payloadFor('reader.close')!['save_progress'], isFalse);
      expect(fake.callsFor('reader.staged_promote'), 0);
    });

    testWidgets('弹窗被折掉（没答）：既不转正也不丢弃，也不关会话', (tester) async {
      final fake = _readerFake(staged: StagedRecord.fromJson(_golden('reader.staged')));
      await _open(tester, fake);
      await leave(tester);
      // 正对照（这一臂的三个 0 全靠它才不是空转）：问话真的立起来过。
      expect(find.byType(AlertDialog), findsOneWidget);
      // 点弹窗外面 = 用户没给答案（`shelve == null`），弹窗按 Material 的默认
      // `barrierDismissible` 关掉——关掉了就是"没答"，不是"答了不要"。
      await tester.tapAt(const Offset(6, 6));
      await tester.pumpAndSettle();
      expect(fake.callsFor('reader.staged_promote'), 0);
      expect(fake.callsFor('reader.staged_discard'), 0);
      expect(fake.callsFor('reader.close'), 0,
          reason: '猜一个方向就是替用户决定删不删这本书的进度与缓存');
      expect(find.byType(ReaderScreen), findsOneWidget, reason: '没答完就留在这一屏');
      expect(find.byType(AlertDialog), findsNothing,
          reason: '留在这一屏要的是"还能再答一次"：弹窗已经收了，但会话没关');
      // 再按一次退出：那一问会重新立起来（暂存记录还在服务端）。
      await leave(tester);
      expect(find.byType(AlertDialog), findsOneWidget);
      expect(fake.callsFor('reader.close'), 0);
    });
  });

  group('排版：两格偏好与 flet 共用同一份设置表', () {
    testWidgets('缩进开关改的是 display，尺不动（ruler 那一列）', (tester) async {
      await tester.binding.setSurfaceSize(const Size(1200, 4000));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final fake = _readerFake();
      await _open(tester, fake);
      expect(find.text(_display(1)), findsOneWidget);
      expect(_display(1), startsWith('　　'));
      expect(_ruler(1), isNot(_display(1)),
          reason: '表里两列同值 = 这一臂分不出"改的是画法还是尺"');

      await tester.tap(find.byKey(const Key('reader.indent')));
      await tester.pumpAndSettle();
      expect(find.text(_ruler(1)), findsOneWidget);
      expect(find.text(_display(1)), findsNothing);
      // 只写**变了的那一格**（flet `set_preference(key, value)` 一键一发）。
      expect(fake.callsFor('settings.write'), 1,
          reason: '改缩进顺带把没动过的字号也落一遍库 = 一次改动两次广播');
      final payload = fake.payloadFor('settings.write')!;
      expect(payload['key'], 'reader.indent');
      expect(payload['value'], isFalse);
      // 尺不动：偏移列一个都没变（改缩进改的是画法，不是那把尺）。
      expect(_offsets(), <int>[0, 168, 1578, 1884, 4053, 4175, 5286, 5799]);
      expect(fake.callsFor('reader.content'), 1,
          reason: '换个缩进重新拉一遍正文 = 白跑一发网络');
    });

    testWidgets('字号落在 flet 的界内：28 号再点"+",存的还是原来的那一格',
        (tester) async {
      final fake = _readerFake(
          settings: const SettingsMap(<String, Object?>{'reader.font_size': 900}));
      await _open(tester, fake);
      // 越界的存档值夹回界内（flet 那屏的滑块界），不退回默认 16。
      expect(tester.widget<Text>(find.text(_display(0))).style!.fontSize, 28.0);
      await tester.tap(find.byKey(const Key('reader.fontLarger')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('settings.write'), 0,
          reason: '夹过之后与当前同值，不该为一次没发生的改动落库');

      await tester.tap(find.byKey(const Key('reader.fontSmaller')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('settings.write'), 1);
      expect(fake.payloadFor('settings.write')!['key'], 'reader.font_size');
      expect(fake.payloadFor('settings.write')!['value'], 27);
      expect(tester.widget<Text>(find.text(_display(0))).style!.fontSize, 27.0);
    });
  });

  group('入口：谁把书递给这一屏', () {
    testWidgets('书架那一行 → reader.open 吃的是那一行的原始快照', (tester) async {
      final book = _goldenBook();
      final fake = _readerFake(
          bookshelf: <BookshelfItem>[BookshelfItem.fromJson(book)]);
      await tester.pumpWidget(ProviderScope(
        overrides: [engineRepositoryProvider.overrideWithValue(fake)],
        child: MaterialApp.router(routerConfig: buildRouter()),
      ));
      await tester.pumpAndSettle();
      await tester.tap(
          find.byKey(const ValueKey('tab-$readingGroupRoute-书架')));
      await tester.pumpAndSettle();
      expect(find.byType(BookshelfScreen), findsOneWidget);
      await tester.tap(find.text('阅读基线书'));
      await tester.pumpAndSettle();

      expect(find.byType(ReaderScreen), findsOneWidget);
      expect(fake.payloadFor('reader.open')!['book'], book,
          reason: '屏上那一行的原始 dict 要原样过边界，不该在中间再造一次序列化');
      expect(fake.payloadFor('reader.open')!['shelve'], isTrue,
          reason: '从书架开的书本来就是架上的，不该走试读那一臂');
    });

    testWidgets('找书详情框的「试读」→ shelve=false，退出时才会问那一句',
        (tester) async {
      final scopes =
          _goldenList('search.scopes').map(SearchScope.fromJson).toList();
      final eventSample = Map<String, Object?>.from(
          _json('tests/gateway/golden/search.start--reader_book.json')['event_sample']!
              as Map<String, Object?>);
      final payload = Map<String, Object?>.from(eventSample['payload']! as Map);
      expect(payload['finished'], isTrue, reason: '样本那一条是收尾快照');

      final fake = _readerFake(scopes: scopes);
      await tester.pumpWidget(ProviderScope(
        overrides: [engineRepositoryProvider.overrideWithValue(fake)],
        child: MaterialApp.router(routerConfig: buildRouter()),
      ));
      await tester.pumpAndSettle();
      // 阅读组的落地页签是有状态的三档（`initialTabOf`：记住的页签 > 书架探测 >
      // 组内第一个），而"记住的页签"（`_lastTab`）是**模块级**的：上一条用例点过
      // 「书架」，同一进程里这一条再进组就落在「书架」——找书那一屏压根没建，
      // openSearch 当然找不到。显式点回「找书」，别把上一条的停留当这一条的前提。
      await tester.tap(find.byKey(const ValueKey('tab-$readingGroupRoute-找书')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('findbooks.openSearch')));
      await tester.pumpAndSettle();
      await tester.enterText(
          find.byKey(const Key('findbooks.keyword')), '阅读基线书');
      await tester.tap(find.byKey(const Key('findbooks.submit')));
      await tester.pump();
      // 事件里的 search_id 要换成这一轮自己那个（替身记下的），否则守门那条会丢掉。
      final searchId = fake.payloadFor('search.start')!['search_id'];
      fake.emit(EngineEvent(EngineEventNames.searchFinished,
          <String, Object?>{...payload, 'search_id': searchId}));
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('findbooks.results')), findsOneWidget);
      final hit = BookRow.fromJson(
          (payload['items']! as List).cast<Map<String, Object?>>().first);
      expect(hit.name, '阅读基线书');

      // 结果行与关键词输入框上都有"阅读基线书"这四个字（`find.text` 也认 EditableText），
      // 所以点的是**结果列表里**的那一行。
      await tester.tap(find.descendant(
          of: find.byKey(const Key('findbooks.results')),
          matching: find.text('阅读基线书')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('findbooks.detail.preview')));
      await tester.pumpAndSettle();
      expect(find.byType(ReaderScreen), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing, reason: '详情框要先收起来');
      expect(fake.payloadFor('reader.open')!['shelve'], isFalse);
      expect(Map<String, Object?>.from(fake.payloadFor('reader.open')!['book']! as Map)['name'],
          '阅读基线书');

      // 退出这一本：`shelve=false` 的那本才该问"放入书架吗"（屏那一侧的收尾由
      // `reader.staged` 决定，替身这条没给暂存记录 → 不问，直接关会话）。
      expect(fake.callsFor('reader.close'), 0);
      expect(fake.callOrder.contains('reader.close'), isFalse,
          reason: '正对照：这一臂只验入口那一发的 shelve，退出那一族由上面那组验');
    });
  });
}

/// 换一屏就先把上一台 widget 拆掉：同一 `WidgetTester` 里连着 `pumpWidget` 时，
/// 旧树留下的定时器会继续往旧容器里写状态。
Future<void> _reset(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pumpAndSettle();
}
