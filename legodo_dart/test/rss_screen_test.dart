/// 订阅屏的 widget 测试：窄窗那副形状**逐发对上 `tests/gateway/projection/rss.shape.json`**，
/// 加上那几条只能在真控件树上验的界面规矩。
///
/// 与 `rss_projection_test.dart` 的分工：那一只比的是**尺**（Dart 模型与 flet 的
/// `render_sources/render_articles/show_article` 在同一行输入上写出同一串字）；
/// 这一只比的是**屏**——那一级够不着就真置灰、源删光了人真会往前退、点下去真的发出那一发
/// 且 payload 里带着表里那个 id、失败那一拍旧列表真的还挂在屏上。
///
/// 三条仪器规矩（与 `reader_screen_test.dart` 同源）：
/// - 替身喂的是 `tests/gateway/golden/` 里的**真载荷**，不是手搓样例；
/// - 形状比对的另一头是 flet 那一屏自己 snapshot 出来的那一格，不是这里抄一份；
/// - 每一个"0"旁边站着一根明知非 0 的正对照（记忆 `zero-counts-need-a-positive-control`）。
///
/// 语料形状上的一个坑，写在这里免得下一个人以为是 bug：`rss.sources` 的第一行是
/// `127.0.0.1:8799/rss/dead.xml`（故意取不到的那一本），它**一篇文章都没有**，
/// 而 `rss.articles` 那两篇挂在 `:8797/feed.xml` 上。凡是要点文章行的判据都先落到
/// [_feedSource]，不是 [_sources] 的第一行。
///
/// 表里 `wide_default` 那格的 `switch_visible=false` 在 Dart 侧是 **Offstage 而不是摘掉**
/// （`rss_screen.dart` 里那条"不换父节点"），所以这里比的是旗标，同时确认按钮仍在树上。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/di/providers.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/event_names.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/fake_engine_repository.dart';
import 'package:legodo_client/features/rss/data/rss_models.dart';
import 'package:legodo_client/features/rss/presentation/rss_screen.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _json(String relative) => jsonDecode(
        File('${_repoRoot.path}/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _projection(String name) =>
    _json('tests/gateway/projection/$name.json');

Object? _dataOf(String action) =>
    Map<String, Object?>.from(
        _json('tests/gateway/golden/$action.json')['response']! as Map)['data'];

List<Map<String, Object?>> _tableRows(String name) =>
    (_projection(name)['rows']! as List).cast<Map<String, Object?>>();

Map<String, Object?> _expectedOf(List<Map<String, Object?>> rows, String caseName) =>
    Map<String, Object?>.from(
        rows.firstWhere((row) => '${row['case']}' == caseName)['expected']! as Map);

/// 真载荷那三本源（左栏）。第一行就是 `last_update_time=0`、且一篇文章都没有的那一本。
List<RssSourceRow> _sources() => (_dataOf('rss.sources') as List)
    .cast<Map<String, Object?>>()
    .map(RssSourceRow.fromJson)
    .toList(growable: false);

/// 真载荷那两篇文章（中栏，`rss.articles` 那一发**没有正文**）。
List<RssArticleRow> _articles() => (_dataOf('rss.articles') as List)
    .cast<Map<String, Object?>>()
    .map(RssArticleRow.fromJson)
    .toList(growable: false);

/// 文章挂在哪一个源下（`rss.articles` 行的 `source_id`）。
RssSourceRow _feedSource() {
  final id = _articles().first.sourceId;
  return _sources().firstWhere((source) => source.id == id,
      orElse: () => throw StateError('语料变了：$id 不在 rss.sources 的行里，重跑探针'));
}

RssArticleDetail _articleDetail() =>
    RssArticleDetail.fromJson(Map<String, Object?>.from(_dataOf('rss.article')! as Map));

/// 全量刷新那五帧进度（`rss.refresh_all` 样本的 `progress_sample.frames`）里每帧的载荷。
List<Map<String, Object?>> _progressFrames() =>
    (Map<String, Object?>.from(
            _json('tests/gateway/golden/rss.refresh_all.json')['progress_sample']!
                as Map)['frames']! as List)
        .cast<Map<String, Object?>>()
        .map((frame) => Map<String, Object?>.from(frame['payload']! as Map))
        .toList(growable: false);

// -------------------------------------------------------------------- #
// 形状那一只读表器
// -------------------------------------------------------------------- #

bool _hidden(WidgetTester tester, Key key) =>
    tester.widget<Offstage>(find.byKey(key, skipOffstage: false)).offstage;

const List<Key> _paneKeys = <Key>[
  Key('rss.shape.pane.sources'),
  Key('rss.shape.pane.articles'),
  Key('rss.shape.pane.detail'),
];
const List<Key> _dividerKeys = <Key>[
  Key('rss.shape.divider.0'),
  Key('rss.shape.divider.1'),
];

ButtonStyleButton _levelButtonAt(WidgetTester tester, int index) {
  final found = tester
      .widgetList<ButtonStyleButton>(find.byKey(
          Key('rss.level.${kRssLevels[index]}'),
          skipOffstage: false))
      .toList();
  expect(found, hasLength(1), reason: '第 $index 级那颗按钮在树上该只有一个');
  return found.single;
}

/// 第 [index] 级那颗按钮的 tooltip（表里的 `tooltip` 列）。
///
/// 走的是 `rss.level.<名>.tip` 那一颗 Tooltip 自己的 key，不是 `find.ancestor`：
/// 这一版 Flutter 的 `find.ancestor` 没有 `skipOffstage` 参数，而宽窗时整条切换条
/// 挂在 Offstage 里，ancestor 那一发在 `wide_default` 上永远抓不到东西。
String? _levelTooltipAt(WidgetTester tester, int index) => tester
    .widget<Tooltip>(find.byKey(Key('rss.level.${kRssLevels[index]}.tip'),
        skipOffstage: false))
    .message;

/// 表里那一格（flet 的控件名）与 Dart 侧的类型名对上。
String _kindOf(ButtonStyleButton button) =>
    button is FilledButton ? 'FilledTonalButton' : '${button.runtimeType}';

/// 逐格对上 `rss.shape.json` 的一行。
void _expectShape(WidgetTester tester, Map<String, Object?> expected) {
  final levelName = '${expected['level']}';
  final levelIndex = kRssLevels.indexOf(levelName);
  expect(levelIndex, greaterThanOrEqualTo(0),
      reason: '表里的级名「$levelName」不在 Dart 那三级里（kRssLevels=$kRssLevels）');

  final switchVisible = expected['switch_visible']! as bool;
  expect(_hidden(tester, const Key('rss.shape.switch')), isNot(switchVisible),
      reason: '切换条：flet 那格是 visible=$switchVisible');
  // **收起不等于不在树上**：摘掉子树会让下面两栏的 Element 重配、滚动位置全丢。
  expect(find.byKey(Key('rss.level.${kRssLevels[levelIndex]}'), skipOffstage: false),
      findsOneWidget,
      reason: '切换条收起时当前那级仍挂在树上（Offstage 而不是 if）');

  final panes = (expected['panes_visible']! as List).cast<bool>();
  for (var index = 0; index < _paneKeys.length; index++) {
    expect(_hidden(tester, _paneKeys[index]), isNot(panes[index]),
        reason: '第 $index 栏摊开吗（flet 那格 ${panes[index]}，当前级 $levelName）');
  }
  final dividers = (expected['dividers_visible']! as List).cast<bool>();
  for (var index = 0; index < _dividerKeys.length; index++) {
    expect(_hidden(tester, _dividerKeys[index]), isNot(dividers[index]),
        reason: '第 $index 条分隔：窄窗一次只摊一栏，两条都不该在');
  }

  // `switch` 那一串是 按钮/分隔/按钮/分隔/按钮，顺序与 flet 的 `_switch.controls` 同形。
  final entries = (expected['switch']! as List).cast<Map<String, Object?>>();
  expect(entries, hasLength(5), reason: '三颗按钮 + 两个分隔字形');
  var buttonIndex = 0;
  for (final entry in entries) {
    if (entry['kind'] == 'Text') {
      expect('${entry['text']}', kRssLevelSeparator,
          reason: 'flet 用「›」：两侧换成别的字形也算漂');
      expect(entry['tooltip'], isNull, reason: '分隔字形没有 tooltip');
      continue;
    }
    final button = _levelButtonAt(tester, buttonIndex);
    expect(_kindOf(button), entry['kind'],
        reason: '第 $buttonIndex 级的形状（当前那级是实心的那一种）');
    expect((button.child! as Text).data, '${entry['text']}', reason: '第 $buttonIndex 级写的是哪一级');
    expect(button.onPressed == null, entry['disabled'],
        reason: '第 $buttonIndex 级 disabled（flet 那格 ${entry['disabled']}）：'
            '够不着的级只能置灰，不能"按下去报个错"');
    expect(_levelTooltipAt(tester, buttonIndex), '${entry['tooltip']}',
        reason: '第 $buttonIndex 级的 tooltip：当前那级说「当前：X」，其余说「跳到X」');
    buttonIndex++;
  }
  expect(buttonIndex, 3, reason: '表里三颗按钮都被读过才算这一发比完');
}

/// 现在摊开的是第几栏（窄窗那三级的读数）。
int _levelIndexShown(WidgetTester tester) {
  for (var index = 0; index < _paneKeys.length; index++) {
    if (!_hidden(tester, _paneKeys[index])) return index;
  }
  throw StateError('三栏都收起来了——窄窗那副形状至少有一栏该摊开');
}

/// 起一屏。`narrow` 走 surface 尺寸：内容宽 <720 就换窄窗那副形状。
Future<FakeEngineRepository> _pump(
  WidgetTester tester, {
  required bool narrow,
  List<RssSourceRow>? sources,
  List<RssArticleRow>? articles,
  RssArticleDetail? detail,
}) async {
  final fake = FakeEngineRepository()
    ..rssSources = sources ?? const <RssSourceRow>[]
    ..rssArticles = articles ?? const <RssArticleRow>[]
    ..rssArticle = detail;
  await tester.binding.setSurfaceSize(Size(narrow ? 420 : 1200, 1200));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(ProviderScope(
    overrides: [engineRepositoryProvider.overrideWithValue(fake)],
    child: const MaterialApp(home: Scaffold(body: RssScreen())),
  ));
  await tester.pumpAndSettle();
  return fake;
}

Finder _sourceTile(String id) => find.byKey(ValueKey('rss.source.$id'));
Finder _articleTile(int id) => find.byKey(ValueKey('rss.article.$id'));
Finder _menuButton(String id) => find.byKey(ValueKey('rss.menu.$id'));

/// 右栏正文那一格现在上屏的字（`SelectableText` 不是 `Text`，`find.text` 抓不到它；
/// 它的字在 `.data` 那一格，`.text` 是 `Text` 才有的）。
String _detailBody(WidgetTester tester) =>
    tester.widget<SelectableText>(find.byKey(const Key('rss.detail.body'))).data ?? '';

/// 跳到有文章的那一本上（窄窗会顺手跳到「文章」级，返回时按需要自己退回去）。
Future<void> _openFeed(WidgetTester tester, {bool backToSources = false}) async {
  await tester.tap(_sourceTile(_feedSource().id));
  await tester.pumpAndSettle();
  if (backToSources) {
    await tester.tap(find.byKey(const Key('rss.level.sources')));
    await tester.pumpAndSettle();
  }
}

void main() {
  // ------------------------------------------------------------------ #
  // ① 形状：逐发对上 rss.shape.json
  // ------------------------------------------------------------------ #
  group('窄窗那副形状逐发对上 rss.shape.json', () {
    final table = _projection('rss.shape');
    final rows = _tableRows('rss.shape');

    test('表里有货（6 发），答案列是 flet 的 `_switch` snapshot', () {
      expect(rows.length, greaterThanOrEqualTo(6));
      expect(table['renderer'], contains('_apply_shape'));
      expect(table['renderer'], contains('_clamp_level'));
    });

    testWidgets('wide_default：三栏齐亮、切换条收起但仍在树上', (tester) async {
      final expected = _expectedOf(rows, 'wide_default');
      await _pump(tester,
          narrow: false, sources: _sources(), articles: _articles());
      _expectShape(tester, expected);
      // 宽窗的三份份额是 3:4:5（第七十七轮那笔定宽账换来的形状约束）。
      final flexes = <int>[
        for (final name in kRssLevels)
          tester
              .widget<Expanded>(find.byKey(Key('rss.shape.flex.$name'),
                  skipOffstage: false))
              .flex
      ];
      expect(flexes, <int>[3, 4, 5],
          reason: '三栏的 flex 份额就是 flet 那侧 `_col` 的那个比例');
    });

    testWidgets('narrow_no_source：只摊第一栏，后两级置灰', (tester) async {
      final expected = _expectedOf(rows, 'narrow_no_source');
      await _pump(tester, narrow: true);
      _expectShape(tester, expected);
      expect(find.text(kRssEmptySources), findsOneWidget,
          reason: '空左栏那一句是投影表 rss.sources.rows 里 empty_list 那一发');
    });

    testWidgets('narrow_with_source：选中一本之后正文那一级还不够得着', (tester) async {
      final expected = _expectedOf(rows, 'narrow_with_source');
      await _pump(tester, narrow: true, sources: _sources());
      // 落回首行是 notifier 自己的规矩（flet `_reload_sources(keep=False)` 同一句），
      // 这里**没点过任何一行**，级还停在「订阅源」。
      _expectShape(tester, expected);
    });

    testWidgets('narrow_after_article：点一篇文章跳到正文级，前两级仍可跳', (tester) async {
      final expected = _expectedOf(rows, 'narrow_after_article');
      await _pump(tester,
          narrow: true,
          sources: _sources(),
          articles: _articles(),
          detail: _articleDetail());
      await _openFeed(tester);
      await tester.tap(_articleTile(_articleDetail().row.id));
      await tester.pumpAndSettle();
      _expectShape(tester, expected);
      expect(_detailBody(tester), _articleDetail().text,
          reason: '跳到正文级那一拍右栏真画出了正文，不是一句占位');
    });

    testWidgets('clamp_after_sources_gone：停够不着的那一级会自动往前退', (tester) async {
      final expected = _expectedOf(rows, 'clamp_after_sources_gone');
      // 表里那一发的输入是"源全没了"，所以这里只喂那**一本**有文章的源。
      final fake = await _pump(tester,
          narrow: true,
          sources: <RssSourceRow>[_feedSource()],
          articles: _articles());
      await _openFeed(tester);
      // 正对照（探针里那句 `assert before == "articles"` 的 Dart 版）：人**真的**停在过
      // 「文章」级，下面那一发才谈得上"退回来"；没跳成时"退回订阅源"是原地的假读数。
      expect(_levelIndexShown(tester), 1);

      // 源没了这一格**程序化注入**（与 flet 探针同形：它那一发也是直接喂空表
      // `render_sources([], {}, "")`，不是点删除）：窄窗停在文章级时源栏整栏 Offstage，
      // 「⋮→删除」那颗按钮本来就点不到——能点的路径走不到这一格。重读走的仍是真实
      // 那一路：事件到了之后 notifier 按 0.5s 窗口合流出一次 `rss.sources`。
      fake.rssSources = const <RssSourceRow>[];
      fake.emit(EngineEvent(EngineEventNames.rssRefreshed, <String, Object?>{
        'source_id': _feedSource().id,
        'new_count': 0,
        'timestamp': 0,
      }));
      await tester.pump(const Duration(milliseconds: 600)); // 角标重读的合流窗口
      await tester.pumpAndSettle();
      expect(fake.callsFor('rss.sources'), greaterThanOrEqualTo(2),
          reason: '正对照：左栏真的被重读过（起手一次 + 事件合流那一次）；'
              '重读没发生时的"退回订阅源"是原地的假读数');
      _expectShape(tester, expected);
    });

    testWidgets('jump_to_unreachable：点一颗够不着的级，原地不动', (tester) async {
      final expected = _expectedOf(rows, 'jump_to_unreachable');
      // 表里那一发的输入是"有行但没选中"（flet 能把 `_selected` 留空），而 Dart 的
      // notifier 落回首行，所以这里喂**空表**：置灰的那两级与「原地不动」这一条同形。
      // 「文章」那一级比不了（flet 空选中→置灰，Dart 自动落首行→可跳），
      // 这一格的不等价写在 `docs/dev-logs` 第九十七轮里。
      await _pump(tester, narrow: true);
      final before = _levelIndexShown(tester);
      // 表里那一串的开关位是 按钮/分隔/按钮/分隔/按钮，第三颗在 `entries[4]`。
      final entries = (expected['switch']! as List).cast<Map<String, Object?>>();
      expect(_levelButtonAt(tester, 2).onPressed, isNull,
          reason: '正文这一级本来就够不着（表里那发 disabled=${entries[4]['disabled']}）');
      await tester.tap(find.byKey(const Key('rss.level.detail')),
          warnIfMissed: false);
      await tester.pumpAndSettle();
      expect(_levelIndexShown(tester), before,
          reason: 'flet 那边 `_go("detail")` 直接不动：不是报错、也不是跳过去');
    });
  });

  // ------------------------------------------------------------------ #
  // ② 点一行 = 发那一发，payload 带着表里那个 id
  // ------------------------------------------------------------------ #
  group('三栏那条链：点下去真的发出那一发', () {
    testWidgets('点一行源发 rss.articles（带源 id），点一行文章先取正文再写已读',
        (tester) async {
      final sources = _sources();
      final articles = _articles();
      final fake = await _pump(tester,
          narrow: false,
          sources: sources,
          articles: articles,
          detail: _articleDetail());
      expect(fake.callsFor('rss.articles'), 1, reason: '起手落在第一本上读了一次');
      final picked = sources[1].id;

      await tester.tap(_sourceTile(picked));
      await tester.pumpAndSettle();
      expect(fake.callsFor('rss.articles'), 2);
      expect(fake.payloadFor('rss.articles')!['source_id'], picked,
          reason: '源 id 就是规范化后的 feed 地址，删除/刷新/读文章吃的都是它');

      final detail = _articleDetail();
      await tester.tap(_articleTile(detail.row.id));
      await tester.pumpAndSettle();
      expect(
          fake.callOrder.skip(fake.callOrder.length - 2).toList(),
          <String>['rss.article', 'rss.mark_read'],
          reason: '顺序照 flet `open_article`：正文到了之后**才**写已读');
      expect(fake.payloadFor('rss.article')!['article_id'], detail.row.id);
      expect(fake.payloadFor('rss.mark_read')!['article_id'], detail.row.id);
    });

    testWidgets('中栏那一族没有正文：正文只有打开那一发给得出', (tester) async {
      final detail = _articleDetail();
      await _pump(tester,
          narrow: false,
          sources: _sources(),
          articles: _articles(),
          detail: detail);
      expect(_detailBody(tester), isEmpty,
          reason: '打开之前屏上不许出现正文：列表那一发本来就取不到它');
      await tester.tap(_sourceTile(detail.row.sourceId));
      await tester.pumpAndSettle();
      await tester.tap(_articleTile(detail.row.id));
      await tester.pumpAndSettle();
      expect(_detailBody(tester), detail.text);
    });

    testWidgets('已读那一发到手之后本地两格同步（字重回落 + 未读角标减一）', (tester) async {
      final withUnread = _sources().firstWhere((source) => source.unread > 0);
      final articles = _articles()
          .map((row) => RssArticleRow(
              id: row.id,
              sourceId: withUnread.id,
              title: row.title,
              url: row.url,
              author: row.author,
              pubDate: row.pubDate,
              image: row.image,
              read: false,
              starred: false))
          .toList(growable: false);
      final fake = await _pump(tester,
          narrow: false,
          sources: <RssSourceRow>[withUnread],
          articles: articles,
          detail: _articleDetail());
      expect(find.textContaining('${withUnread.unread} 未读'), findsOneWidget,
          reason: '正对照：起手角标是真从行里读出来的那一个数');
      // 点的是**详情样本那一篇**：替身的 `openRssArticle` 按 id 认行（id 不配回 null，
      // 与"没这篇文章"走同一支早退，已读那一发就打不出去），而 `rss.articles` 的行序
      // 是 [id=2, id=1]——点 `articles.first` 恰好是详情样本里没有的那一篇。
      await tester.tap(_articleTile(_articleDetail().row.id));
      await tester.pumpAndSettle();

      expect(fake.callsFor('rss.sources'), 1,
          reason: '已读之后**就地减一**，不重读整表（flet 也是就地改 `_counts`）');
      expect(find.textContaining('${withUnread.unread - 1} 未读'), findsOneWidget);
      expect(find.textContaining('${withUnread.unread} 未读'), findsNothing);
    });

    testWidgets('「只看未读」那个勾折的是服务端那一格，只多发一次读列表', (tester) async {
      final fake = await _pump(tester,
          narrow: false, sources: _sources(), articles: _articles());
      final before = fake.callsFor('rss.articles');
      await tester.tap(find.byKey(const Key('rss.unread.only')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('rss.articles'), before + 1,
          reason: '勾一下只重读中栏，不该连左栏一起读');
      expect(fake.callsFor('rss.sources'), 1, reason: '同上：左栏那一发没有第二次');
      expect(fake.payloadFor('rss.articles')!['unread_only'], isTrue);
      await tester.tap(find.byKey(const Key('rss.unread.only')));
      await tester.pumpAndSettle();
      expect(fake.payloadFor('rss.articles')!['unread_only'], isFalse);
    });
  });

  // ------------------------------------------------------------------ #
  // ③ 收藏那颗：列表翻两格，正文那一级照搬 flet 只翻图标
  // ------------------------------------------------------------------ #
  group('收藏那一发', () {
    testWidgets('列表那颗一按两格都翻，状态句是「已收藏」/「已取消收藏」', (tester) async {
      final row = _articles().first;
      final fake = await _pump(tester,
          narrow: false, sources: _sources(), articles: _articles());
      await tester.tap(_sourceTile(row.sourceId));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(ValueKey('rss.star.${row.id}')));
      await tester.pumpAndSettle();
      expect(fake.payloadFor('rss.star')!['article_id'], row.id);
      expect(fake.payloadFor('rss.star')!['starred'], isTrue);
      expect(find.text(kRssStarred), findsOneWidget);
      expect(
          tester.widget<IconButton>(find.byKey(ValueKey('rss.star.${row.id}'))).tooltip,
          kRssUnstarTooltip,
          reason: '图标与 tooltip 一起翻（flet `rss_view.py:339-340`）');

      await tester.tap(find.byKey(ValueKey('rss.star.${row.id}')));
      await tester.pumpAndSettle();
      expect(fake.payloadFor('rss.star')!['starred'], isFalse);
      expect(find.text(kRssUnstarred), findsOneWidget);
    });

    testWidgets('正文那颗只翻图标、tooltip 停在「收藏」（照搬 flet，不是笔误）', (tester) async {
      final detail = _articleDetail();
      await _pump(tester,
          narrow: false,
          sources: _sources(),
          articles: _articles(),
          detail: detail);
      expect(detail.row.starred, isFalse,
          reason: '正对照：输入是未收藏，翻过之后才看得出只翻了一格');
      await tester.tap(_sourceTile(detail.row.sourceId));
      await tester.pumpAndSettle();
      await tester.tap(_articleTile(detail.row.id));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('rss.detail.star')));
      await tester.pumpAndSettle();
      final button =
          tester.widget<IconButton>(find.byKey(const Key('rss.detail.star')));
      // `button.icon` 那一格是 `Widget`，`Icon.icon` 才是 `IconData`——两边比的得是
      // **同一个口**（`(icon! as Icon).icon`），拿 Widget 比 IconData 恒红。
      expect((button.icon! as Icon).icon, Icons.star, reason: '图标跟着旗标走');
      expect(button.tooltip, kRssStarTooltip,
          reason: 'flet 的 `show_article` 只给 `_detail_star.icon` 赋值（rss_view.py:355），'
              'tooltip 停在 `:117` 那句「收藏」。这一格钉的是"照搬"：要修它得单独一轮');
    });

    testWidgets('文章已被另一侧删掉：那一格布尔是 false，句子是「收藏未保存」', (tester) async {
      final row = _articles().first;
      final fake = await _pump(tester,
          narrow: false, sources: _sources(), articles: _articles());
      await tester.tap(_sourceTile(row.sourceId));
      await tester.pumpAndSettle();
      // 让替身的表里**没有**这一行：服务端那一条 `UPDATE` 就是改不到行。
      fake.rssArticles = const <RssArticleRow>[];
      await tester.tap(find.byKey(ValueKey('rss.star.${row.id}')));
      await tester.pumpAndSettle();
      expect(find.text(kRssArticleGone), findsOneWidget,
          reason: '`false` 说的是"这条本就不在"，不是"删除失败"');
      expect(find.text(kRssStarred), findsNothing);
    });
  });

  // ------------------------------------------------------------------ #
  // ④ 三个对话框与那几条写操作的句子
  // ------------------------------------------------------------------ #
  group('添加 / 刷新本源 / 删除', () {
    testWidgets('添加：名称留空也照样提交，且第一遍抓取不压掉「正在抓取」那句', (tester) async {
      const url = 'http://127.0.0.1:8799/rss/new.xml';
      final fake = await _pump(tester, narrow: false, sources: _sources());
      await tester.tap(find.byKey(const Key('rss.add')));
      await tester.pumpAndSettle();
      expect(find.text(kRssAddDialogTitle), findsOneWidget);
      expect(find.text(kRssRuleNote), findsOneWidget,
          reason: '那句功能边界是**功能边界**不是装饰：带规则的源要改源文件');
      await tester.enterText(find.byKey(const Key('rss.add.url')), url);
      await tester.tap(find.byKey(const Key('rss.add.submit')));
      await tester.pumpAndSettle();

      expect(fake.payloadFor('rss.add_source')!['name'], isEmpty,
          reason: '界面不许加一道"没填不许提交"：那是界面自己发明服务端没有的规矩');
      expect(fake.payloadFor('rss.add_source')!['url'], url);
      expect(fake.callsFor('rss.refresh'), 1, reason: '先落库、再抓第一遍');
      expect(find.text(kRssAdded(url)), findsOneWidget,
          reason: '第一遍抓取是 report=False：紧接着写「刷新完成」会把前一句压掉');
      expect(find.textContaining('刷新完成'), findsNothing);
      expect(find.text(kRssRuleNote), findsNothing, reason: '框已经关了');
    });

    testWidgets('刷新本源：新增篇数上屏，吃的是那一发的返回值', (tester) async {
      final sources = _sources();
      final fake = await _pump(tester, narrow: false, sources: sources)
        ..rssNewArticles = 3;
      final picked = sources.first;
      await tester.tap(_menuButton(picked.id));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('rss.menu.refresh')));
      await tester.pumpAndSettle();

      expect(fake.payloadFor('rss.refresh')!['source_id'], picked.id);
      expect(find.text(kRssRefreshOneDone(picked.name, 3)), findsOneWidget,
          reason: '3 是明知非 0 的那一格：读成 0 就是"这个源没有新文章"');
    });

    testWidgets('抓取失败那一支是错误信封：句子是「刷新失败」，旧列表还在', (tester) async {
      final sources = _sources();
      final fake = await _pump(tester, narrow: false, sources: sources);
      final before = fake.callsFor('rss.sources');
      fake.failure = const EngineFailure(
          EngineFailureKind.engine, '连不上那一本', action: 'rss.refresh');
      await tester.tap(_menuButton(sources.first.id));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('rss.menu.refresh')));
      await tester.pumpAndSettle();

      expect(find.text(kRssRefreshFailed('连不上那一本')), findsOneWidget);
      expect(find.textContaining('刷新完成'), findsNothing,
          reason: '失败读成"新增 0 篇"就是把没连上说成没新文章');
      for (final source in sources) {
        expect(_sourceTile(source.id), findsOneWidget,
            reason: '失败那一拍左栏不许被演成空表（规矩 2）');
      }
      expect(fake.callsFor('rss.sources'), before, reason: '抓取没成就不重读列表');
    });

    testWidgets('删除必须先二次确认；取消那一发一次都不许打', (tester) async {
      final sources = _sources();
      final fake = await _pump(tester, narrow: false, sources: sources);
      final picked = sources.first;
      await tester.tap(_menuButton(picked.id));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('rss.menu.remove')));
      await tester.pumpAndSettle();

      expect(find.text(kRssRemoveDialogTitle), findsOneWidget);
      expect(find.text(kRssRemoveDialogBody(picked.name)), findsOneWidget,
          reason: '级联删除（连文章带已读/收藏标记）不写清楚，用户以为删的只是一行名字');
      await tester.tap(find.byKey(const Key('rss.confirm.cancel')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('rss.remove_source'), 0);
      expect(_sourceTile(picked.id), findsOneWidget);
    });

    testWidgets('那条本就不在列表里：服务端回 false，句子是「已不在列表里」而不是失败',
        (tester) async {
      final sources = _sources();
      final fake = await _pump(tester, narrow: false, sources: sources);
      final picked = sources.first.id;
      await tester.tap(_menuButton(picked));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('rss.menu.remove')));
      await tester.pumpAndSettle();
      // 确认框还开着的时候另一侧把那一本删了：替身里没有这一行 → `removed=false`。
      fake.rssSources =
          fake.rssSources.where((source) => source.id != picked).toList(growable: false);
      await tester.tap(find.byKey(const Key('rss.confirm.delete')));
      await tester.pumpAndSettle();
      expect(fake.callsFor('rss.remove_source'), 1);
      expect(find.text(kRssAlreadyGone), findsOneWidget);
      expect(find.text(kRssRemoved(sources.first.name)), findsNothing);
    });
  });

  // ------------------------------------------------------------------ #
  // ⑤ 全量刷新：进度帧驱动条与句子（喂的是 rss.refresh_all 样本里的真帧）
  // ------------------------------------------------------------------ #
  group('全部刷新那一轮（job + 事件那一路）', () {
    testWidgets('受理那一发只有 {started,action}：条先不亮，句子是「正在刷新全部订阅源…」',
        (tester) async {
      final fake = await _pump(tester, narrow: false, sources: _sources());
      await tester.tap(find.byKey(const Key('rss.refresh.all')));
      await tester.pumpAndSettle();

      expect(fake.callsFor('rss.refresh_all'), 1);
      final sent = fake.payloadFor('rss.refresh_all')!['refresh_id'];
      expect(sent, isNotNull, reason: '本轮身份由客户端生成，每帧原样回带（规矩 6）');
      expect((sent! as int) > 0, isTrue);
      expect(find.text(kRssRefreshingAll), findsOneWidget);
      expect(find.byKey(const Key('rss.progress')), findsNothing,
          reason: '受理回包没有 total/done，进度条不能凭一句"肯起了"就亮');
    });

    testWidgets('帧驱动：条读 done/total、句子读 done+1；别人那一轮不跟', (tester) async {
      final fake = await _pump(tester, narrow: false, sources: _sources());
      await tester.tap(find.byKey(const Key('rss.refresh.all')));
      await tester.pumpAndSettle();
      final mine = fake.payloadFor('rss.refresh_all')!['refresh_id']! as int;
      final frames = _progressFrames();
      expect(frames.length, greaterThanOrEqualTo(3), reason: '样本帧不够撑不起下面三条');
      final total = (frames.first['total']! as num).toInt();
      expect(total, greaterThan(0), reason: '正对照：本轮计划源数非 0');

      // 别的轮次那一帧（同样的数，身份不同）：一条都不许上屏。
      fake.emit(EngineEvent(EngineEventNames.rssRefreshProgress,
          Map<String, Object?>.from(frames[1])..['refresh_id'] = mine + 7));
      await tester.pumpAndSettle();
      expect(find.textContaining('/$total'), findsNothing,
          reason: '认不出本轮的帧会把上一轮的计数叠进这一轮');
      expect(find.byKey(const Key('rss.progress')), findsNothing);

      fake.emit(EngineEvent(EngineEventNames.rssRefreshProgress,
          Map<String, Object?>.from(frames[1])..['refresh_id'] = mine));
      await tester.pumpAndSettle();
      final done = (frames[1]['done']! as num).toInt();
      expect(
          tester.widget<LinearProgressIndicator>(find.byKey(const Key('rss.progress'))).value,
          closeTo(done / total, 1e-9),
          reason: '条读 done/total（已经刷完几条），句子读 done+1（正在刷第几条）——'
              '两者不同值是**有意的**');
      expect(find.textContaining('正在刷新 ${done + 1}/$total'), findsOneWidget);

      // 样本第一帧的 `current` 是空的：那一格折成「未知源」，句子不能被截成冒号收尾。
      expect(frames.first['current'], isEmpty, reason: '样本那一发的确是空串');
      fake.emit(EngineEvent(EngineEventNames.rssRefreshProgress,
          Map<String, Object?>.from(frames.first)..['refresh_id'] = mine));
      await tester.pumpAndSettle();
      expect(find.textContaining('：$kRssUnknownSource'), findsOneWidget);
    });

    testWidgets('收尾那一帧：条收起，句子是累计新增 + 失败源数', (tester) async {
      final fake = await _pump(tester, narrow: false, sources: _sources());
      await tester.tap(find.byKey(const Key('rss.refresh.all')));
      await tester.pumpAndSettle();
      final mine = fake.payloadFor('rss.refresh_all')!['refresh_id']! as int;
      final terminal = _progressFrames().last;
      expect((terminal['done']! as num).toInt(), terminal['total'],
          reason: '样本收尾那一帧的确跑完了');
      final failures = (terminal['failures']! as num).toInt();
      expect(failures, greaterThan(0), reason: '样本里这一轮真有源取失败——那半句才有读数');

      // 本轮新增由 `RssRefreshedEvent` 累加（它不带本轮身份，只在窗口开着时算）。
      fake.emit(EngineEvent(EngineEventNames.rssRefreshed, <String, Object?>{
        'source_id': _sources().first.id,
        'new_count': 2,
        'timestamp': 0,
      }));
      await tester.pumpAndSettle();
      fake.emit(EngineEvent(EngineEventNames.rssRefreshProgress,
          Map<String, Object?>.from(terminal)..['refresh_id'] = mine));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('rss.progress')), findsNothing,
          reason: '留一根满格的条在顶上，用户读到的是"还在刷"');
      expect(find.text(kRssRefreshDoneWithFailures(2, failures)), findsOneWidget);
      expect(fake.callsFor('rss.sources'), greaterThanOrEqualTo(2),
          reason: '收尾之后角标要补一次整表');
    });

    testWidgets('窗外那条 `RssRefreshedEvent` 不累进本轮那句', (tester) async {
      final fake = await _pump(tester, narrow: false, sources: _sources());
      // 还没点「全部刷新」：本轮没开，窗外的事件只补角标。
      fake.emit(EngineEvent(EngineEventNames.rssRefreshed, <String, Object?>{
        'source_id': _sources().first.id,
        'new_count': 5,
        'timestamp': 0,
      }));
      await tester.pump(const Duration(milliseconds: 600));
      await tester.pumpAndSettle();
      expect(find.textContaining('新增'), findsNothing,
          reason: '把别人的新增写进自己那句"新增 N 篇"是最难查的那种好看');

      await tester.tap(find.byKey(const Key('rss.refresh.all')));
      await tester.pumpAndSettle();
      final mine = fake.payloadFor('rss.refresh_all')!['refresh_id']! as int;
      final terminal = _progressFrames().last;
      fake.emit(EngineEvent(EngineEventNames.rssRefreshProgress,
          Map<String, Object?>.from(terminal)..['refresh_id'] = mine));
      await tester.pumpAndSettle();
      expect(find.textContaining('新增 0 篇'), findsOneWidget,
          reason: '本轮自己一篇都没收到，那句就该是 0');
      expect(find.textContaining('新增 5'), findsNothing);
    });
  });

  // ------------------------------------------------------------------ #
  // ⑥ 状态只有一个槽：谁最后写谁说话，读列表失败不清数据
  // ------------------------------------------------------------------ #
  group('那一个槽', () {
    testWidgets('中栏读失败：句子是「文章列表加载失败」，旧文章还挂着', (tester) async {
      final articles = _articles();
      final fake = await _pump(tester,
          narrow: false, sources: _sources(), articles: articles);
      await tester.tap(_sourceTile(articles.first.sourceId));
      await tester.pumpAndSettle();
      expect(_articleTile(articles.first.id), findsOneWidget);

      fake.failure = const EngineFailure(
          EngineFailureKind.engine, '那一本又连不上了', action: 'rss.articles');
      await tester.tap(find.byKey(const Key('rss.unread.only')));
      await tester.pumpAndSettle();

      expect(find.text(kRssArticlesLoadFailed('那一本又连不上了')), findsOneWidget);
      for (final row in articles) {
        expect(_articleTile(row.id), findsOneWidget,
            reason: '把失败演成空表，用户读到的是"我的文章没了"（规矩 2）');
      }
    });

    testWidgets('事件流断了那句话进同一个槽', (tester) async {
      final fake = await _pump(tester, narrow: false, sources: _sources());
      fake.emitStreamError('管道断了');
      await tester.pumpAndSettle();
      expect(find.textContaining('事件流断开：'), findsOneWidget,
          reason: '它是"这一拍的故障"，不是这一屏的数据（同 find_books 那一处）');
    });

    testWidgets('左栏空表那一发：中栏与右栏各退回自己那一句', (tester) async {
      await _pump(tester, narrow: false);
      expect(find.text(kRssEmptySources), findsOneWidget);
      expect(find.text(kRssEmptyArticles), findsOneWidget);
      expect(find.text(kRssNoSourcesAtAll), findsOneWidget,
          reason: '右栏那一句由 notifier 的 detailHint 给（flet `clear_detail` 的三个值之一）');
    });
  });
}
