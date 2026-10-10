/// 订阅面四张投影表：**同一行输入，flet 的 `RssView` 与 Dart 的模型各渲染一次，整句逐字比**。
///
/// 为什么还要这一支，而不是靠 `tests/test_gateway_contract_single_source.py` 里那条
/// 文案守卫：那条比的是**源码里的字面量**，它抓得住「已取消收藏」被写成「已取消」，
/// 抓不住**拼法**不同——`unread=0` 时那一段出不出现、Atom 那一发作者整列空串时
/// 尾巴上有没有孤零零的「 · 」、`1700` 这种 1e9 以下的刷新时间是原样吐还是折成
/// 「01-01 08:00」。那些是渲染出来的结果，不是源码里的串。
///
/// 答案列一律由 flet 自己的渲染函数产出（`.probe/g4_rss_projection.py` 直接调
/// `RssView.render_sources/render_articles/show_article/clear_detail`，再从控件树读回
/// `ft.Text.value`）。**输入**可以是从黄金样本改出来的合成行（去掉一个键、换一格），
/// 答案**不许**手填——手填的答案就是"Dart 自己证明自己对"那一族反面样本。
///
/// 表里 `generated.local_utc_offset_minutes` 那一格是硬条件：`_when` 用
/// `time.localtime` 折 epoch 秒，所以「刷新于 10-10 14:50」这类答案带着生成那台机器的
/// 时区。时区不同就当场炸，不许悄悄绿过去（重跑探针是唯一正确的处置）。
///
/// 第五张表 `rss.shape.json`（窄窗那三级的可见/禁用/tooltips）比的是**控件树**，
/// 在 `test/rss_screen_test.dart` 里逐发走，不在这里。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/features/rss/data/rss_models.dart';
import 'package:legodo_client/features/rss/domain/rss_notifier.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _projection(String name) {
  final file = File('${_repoRoot.path}/tests/gateway/projection/$name.json');
  if (!file.existsSync()) {
    throw StateError('缺投影表 ${file.path}\n'
        '先跑 PYTHONIOENCODING=utf-8 PYTHONPATH=. '
        'python -u .probe/g4_rss_projection.py');
  }
  return jsonDecode(file.readAsStringSync(encoding: utf8))
      as Map<String, Object?>;
}

/// 真载荷的行（用来**当场重数**表里那格 `corpus_evidence`，而不是信它抄下来的数）。
List<Map<String, Object?>> _goldenRows(String name) {
  final file = File('${_repoRoot.path}/tests/gateway/golden/$name.json');
  if (!file.existsSync()) {
    throw StateError('缺黄金样本 ${file.path}\n'
        '先跑 .probe/g4_golden_capture.py（第十二族，rss 那十二发）');
  }
  final sample =
      jsonDecode(file.readAsStringSync(encoding: utf8)) as Map<String, Object?>;
  final data = Map<String, Object?>.from(sample['response']! as Map)['data'];
  if (data is! List) {
    throw StateError('$name 的 response.data 不是列表（收到 ${data.runtimeType}）');
  }
  return data.cast<Map<String, Object?>>();
}

List<Map<String, Object?>> _rowsOf(Map<String, Object?> table) =>
    (table['rows']! as List).cast<Map<String, Object?>>();

Map<String, Object?> _expected(Map<String, Object?> row) =>
    Map<String, Object?>.from(row['expected']! as Map);

Map<String, Object?> _rowOf(Map<String, Object?> row) =>
    Map<String, Object?>.from(row['row']! as Map);

/// 时区那格硬条件（见文件头）。三处调用共用，因为三张表里都有日期那几发。
void _requireSameTimeZone(Map<String, Object?> table, String name) {
  final generated = table['generated'];
  if (generated == null) {
    throw StateError('$name 这张表没有 `generated` 那一格——它是旧版探针写的，'
        '重跑 .probe/g4_rss_projection.py');
  }
  final recorded = (Map<String, Object?>.from(generated as Map)
          ['local_utc_offset_minutes']! as num)
      .toInt();
  final here = DateTime.now().timeZoneOffset.inMinutes;
  if (here != recorded) {
    throw StateError('$name 的日期那几格是在 UTC${_utcLabel(recorded)} 的机器上生成的，'
        '这台是 UTC${_utcLabel(here)}。同一个瞬间两边会写出不同的串，'
        '红了不是迁移的问题——先在这台机器上重跑 .probe/g4_rss_projection.py');
  }
}

String _utcLabel(int minutes) {
  final sign = minutes < 0 ? '-' : '+';
  final hours = (minutes.abs() / 60).floor();
  final rest = minutes.abs() % 60;
  return '$sign$hours${rest == 0 ? '' : ':${rest.toString().padLeft(2, '0')}'}';
}

/// 表里钉着的 `total`/`unread` 是 flet 从 `counts` 那本字典读的，而 action 面上这两格
/// 就在行里。两边读同一份真值、只是挂在不同位置——这一格把"同一份"钉住，
/// 否则 Dart 比赢的可能是两份不同的数。
void _requireJoinAgreesWithRow(Map<String, Object?> row) {
  final source = _rowOf(row);
  final counts = Map<String, Object?>.from(row['counts']! as Map);
  final forId =
      Map<String, Object?>.from(counts['${source['id']}']! as Map);
  expect(forId['total'], source['total'],
      reason: '${row['case']}：服务端 join 进行的 total 与 `article_counts()` 那本字典'
          '必须同数，否则两侧比的是两份真值');
  expect(forId['unread'], source['unread'], reason: '${row['case']}：同上，未读那一格');
}

void main() {
  group('左栏源行：flet 的 render_sources 与 Dart 的 RssSourceRow 逐字相同', () {
    final table = _projection('rss.sources.rows');
    final rows = _rowsOf(table);

    test('表里有货（11 发），且答案列是 flet 的渲染函数给的', () {
      _requireSameTimeZone(table, 'rss.sources.rows');
      expect(rows.length, greaterThanOrEqualTo(11));
      expect(table['renderer'], contains('RssView.render_sources'));
    });

    test('语料走到过哪几支：表里那格计数**当场重数**，不抄它的', () {
      // `corpus_evidence` 记的是"真载荷自己走过哪几支"。这一发把它重新数一遍，
      // 于是它既能抓住语料变了而表没重跑，也能抓住"把没走到的那支读成走过"
      // （记忆 `zero-counts-need-a-positive-control`、`comparison-closure-must-reread-live`）。
      final evidence = Map<String, Object?>.from(table['corpus_evidence']! as Map);
      final corpus = _goldenRows('rss.sources');
      int counted(bool Function(Map<String, Object?>) test) =>
          corpus.where(test).length;
      expect(evidence['epoch_rows'],
          counted((r) => (r['last_update_time']! as num).toDouble() > 1000000000),
          reason: '「刷新于 MM-DD HH:MM」那一支的真载荷行数漂了：表没跟着语料重跑');
      expect(evidence['never_rows'],
          counted((r) => (r['last_update_time']! as num).toDouble() == 0),
          reason: '「刷新于 从未」那一支');
      expect(evidence['unread_rows'],
          counted((r) => (r['unread']! as num).toInt() > 0));
      expect(evidence['nameless_rows'], counted((r) => '${r['name']}'.isEmpty));
      expect(evidence['disabled_rows'], counted((r) => r['enabled'] == false));

      // 三条非 0 是正对照：为 0 时下面那几条合成行没有真载荷走过，绿了也不算证。
      expect((evidence['epoch_rows']! as num).toInt(), greaterThan(0));
      expect((evidence['never_rows']! as num).toInt(), greaterThan(0));
      expect((evidence['unread_rows']! as num).toInt(), greaterThan(0));
      // 这一条是**已知空档**，写在明处：语料里没有停用源，「（已停用）」那一发
      // 只是合成行。真加了停用源就重跑探针，这里翻过来是提醒而不是放行。
      expect((evidence['disabled_rows']! as num).toInt(), 0,
          reason: '语料里出现了停用源：表里的这一发不再是合成行，'
              '重跑探针并把它从"已知空档"改成非 0 的对照');
    });

    for (final row in rows) {
      final caseName = '${row['case']}';
      if (row['row'] == null) continue; // empty_list 走下面那发单独的判据
      test('$caseName → 标题、副标题、那一格「N 个」同一句', () {
        final source = RssSourceRow.fromJson(_rowOf(row));
        final expected = _expected(row);
        _requireJoinAgreesWithRow(row);
        expect(source.title, equals('${expected['title']}'),
            reason: '$caseName：${row['why']}');
        expect(source.subtitle, equals('${expected['subtitle']}'),
            reason: '$caseName：${row['why']}'
                '（分隔符是 ` · `，缺一段是少一段，而不是留一个空段）');
        expect(RssState(sources: <RssSourceRow>[source]).sourceCountLabel,
            equals('${expected['count_label']}'),
            reason: '$caseName：flet 的 `_source_count` 写的是 `f"{len(sources)} 个"`');
        expect(source.id == '${row['selected_id']}', expected['selected'],
            reason: '$caseName：选中态两边都按 id 比，这一格钉的是"源 id 就是 feed 地址"');
      });
    }

    test('empty_list → 那一栏只有一句说明，「N 个」是 0 个', () {
      final row = rows.firstWhere((r) => '${r['case']}' == 'empty_list');
      final expected = _expected(row);
      expect(expected['row_count'], 1,
          reason: 'flet 那边空表渲染出的控件只有一个（那句说明），不是一行都没有');
      expect(kRssEmptySources, equals('${expected['title']}'),
          reason: 'Dart 屏上空列表那一路显示的就是这个常量');
      expect(const RssState().sourceCountLabel, equals('${expected['count_label']}'));
      expect(const RssState().sources, isEmpty);
    });

    test('1e9 以下那一支原样吐数，且吐的是 REAL 列的那一份（带 .0）', () {
      final small = rows.firstWhere((r) => '${r['case']}' == 'small_number');
      final source = RssSourceRow.fromJson(_rowOf(small));
      expect(source.subtitle, contains('1700.0'),
          reason: '`last_update_time` 是 REAL 列（schema.py:151），线上到界面的永远是 float；'
              '把它折成「01-01 08:00」是把一个单位错误演成一个日期');
      expect(source.subtitle, isNot(contains('01-01')));
    });
  });

  group('中栏文章行：flet 的 render_articles 与 Dart 的 RssArticleRow 逐字相同', () {
    final table = _projection('rss.articles.rows');
    final rows = _rowsOf(table);

    test('表里有货（13 发），答案来自 RssView.render_articles', () {
      _requireSameTimeZone(table, 'rss.articles.rows');
      expect(rows.length, greaterThanOrEqualTo(13));
      expect(table['renderer'], contains('RssView.render_articles'));
    });

    test('语料里既没有已读也没有收藏的行——那两支只有合成行证人', () {
      // 不是放行，是把"这一支没被真载荷走过"写在明处：表里每一条都自带这两个旗标。
      final samples = <String>{
        'rss.articles',
        'rss.articles--no-author',
        'rss.articles--unread-only',
      };
      for (final name in samples) {
        final corpus = _goldenRows(name);
        expect(corpus.every((r) => r['read'] != true), isTrue,
            reason: '$name 里出现了已读行：表里 `read_in_golden` 那格得跟着重跑');
        expect(corpus.every((r) => r['starred'] != true), isTrue,
            reason: '$name 里出现了收藏行：同上');
      }
      for (final row in rows.where((r) => r['row'] != null)) {
        expect(_expected(row)['read_in_golden'], isFalse,
            reason: '${row['case']} 说的是"这一发吃什么旗标"，不是"语料里有"');
      }
    });

    for (final row in rows) {
      final caseName = '${row['case']}';
      if (row['row'] == null) continue;
      test('$caseName → 标题、副标题、字重同一句', () {
        final item = RssArticleRow.fromJson(_rowOf(row));
        final expected = _expected(row);
        expect(item.shownTitle, equals('${expected['title']}'),
            reason: '$caseName：${row['why']}');
        expect(item.subtitle, equals('${expected['subtitle']}'),
            reason: '$caseName：${row['why']}'
                '（这一族**没有正文**，副标题只有日期与作者两段）');
        // flet 的 `weight`：`None if read else W_500`。Dart 侧 `bold` 是同一个旗标，
        // 屏上那颗 `FontWeight.w500` 由 rss_screen_test 那发真控件兜。
        expect(item.bold, equals(expected['weight'] == 'W_500'),
            reason: '$caseName：字重（${expected['weight']}）——已读不加粗');
        expect(item.starred, equals(expected['star_icon'] == 'STAR'),
            reason: '$caseName：星形翻的是同一个旗标');
        expect(expected['star_tooltip'],
            item.starred ? kRssUnstarTooltip : kRssStarTooltip,
            reason: '$caseName：中栏那颗按钮连 tooltip 一起翻（rss_view.py:339-340）');
      });
    }

    test('Atom 那一发作者整列空串：副标题只剩日期，尾巴不许挂「 · 」', () {
      final atom = rows.firstWhere((r) => '${r['case']}' == 'atom_row_0');
      final item = RssArticleRow.fromJson(_rowOf(atom));
      expect(item.author, isEmpty, reason: '输入真是空串，不是缺键');
      expect(item.subtitle, equals('${_expected(atom)['subtitle']}'));
      expect(item.subtitle, isNot(endsWith(' · ')),
          reason: '分隔符跟着缺席的那一段一起走，否则每行末尾挂一个孤零零的「 · 」');
      expect(item.subtitle, contains('T10:00:00Z'),
          reason: 'Atom 的 ISO 串**原样上屏**：这一屏不解析它（自己解析就是第二把尺）');
    });

    test('pub_date 是能读成数字的串时，两档分法与左栏同一把尺', () {
      final big =
          rows.firstWhere((r) => '${r['case']}' == 'pub_date_numeric_epoch');
      final small =
          rows.firstWhere((r) => '${r['case']}' == 'pub_date_numeric_small');
      expect(_expected(big)['subtitle'], startsWith('09-28 03:06'),
          reason: '>1e9 → 按 epoch 秒格式化（与左栏那一发同一个串）');
      // 这一发考的是 `str(value)` 回到的是**传进来那一份**：协议面上 pub_date 是串，
      // 所以原样吐「1700」而不是 double 打印出来的「1700.0」。
      expect(_expected(small)['subtitle'], startsWith('1700 · '),
          reason: '<=1e9 → 原样吐那一个串；Dart 的 whenFromRaw 与 whenText 因此是两个函数');
      expect(RssArticleRow.fromJson(_rowOf(small)).subtitle,
          equals('${_expected(small)['subtitle']}'));
    });

    test('empty_list → 中栏那一句说明存在，且不写"左边的源"', () {
      final row = rows.firstWhere((r) => '${r['case']}' == 'empty_list');
      final expected = _expected(row);
      expect(expected['row_count'], 1);
      expect(kRssEmptyArticles, equals('${expected['title']}'),
          reason: '窄窗里上一级在切换条上，不在"左边"，这句话在两种形状下都得读得通');
    });
  });

  group('右栏正文：flet 的 show_article/clear_detail 与 Dart 的 RssArticleDetail 逐字相同',
      () {
    final table = _projection('rss.article.detail');
    final rows = _rowsOf(table);

    test('表里有货（9 发），答案来自 show_article / clear_detail', () {
      _requireSameTimeZone(table, 'rss.article.detail');
      expect(rows.length, greaterThanOrEqualTo(9));
      expect(table['renderer'], contains('RssView.show_article'));
    });

    for (final row in rows.where((r) => r['article'] != null)) {
      final caseName = '${row['case']}';
      test('$caseName → 标题、元信息、正文、链接四格同一句', () {
        final json = Map<String, Object?>.from(row['article']! as Map)
          ..['text'] = row['body'];
        final detail = RssArticleDetail.fromJson(json);
        final expected = _expected(row);
        expect(detail.row.shownTitle, equals('${expected['title']}'));
        expect(detail.meta, equals('${expected['meta']}'),
            reason: '$caseName：元信息是「作者 · 日期」，与中栏那行（日期在前）**相反**，'
                '照 flet `show_article`');
        expect(detail.body, equals('${expected['body']}'),
            reason: '$caseName：正文是服务端去掉标签的那一份，Dart 不再剥一次');
        expect(detail.row.url, equals('${expected['link']}'),
            reason: '$caseName：链接空着就是空串，不是自造的「无」');
      });
    }

    test('正文空着那一格是那句说明，不是空白栏', () {
      final row = rows.firstWhere((r) => '${r['case']}' == 'empty_body');
      final json = Map<String, Object?>.from(row['article']! as Map)
        ..['text'] = row['body'];
      expect(RssArticleDetail.fromJson(json).body,
          equals('${_expected(row)['body']}'));
      expect(kRssNoBody, equals('${_expected(row)['body']}'));
    });

    test('右栏退回占位那两发：四格里只有标题有字', () {
      final defaults = rows.where((r) => r['article'] == null).toList();
      expect(defaults.length, 2,
          reason: 'clear_detail 的两个入参值（默认 + 控制器传的那一句）');
      for (final row in defaults) {
        final expected = _expected(row);
        expect(expected['meta'], isEmpty,
            reason: '${row['case']}：没有文章时其余三格是空串');
        expect(expected['body'], isEmpty);
        expect(expected['link'], isEmpty);
      }
      // 两个值就是 Dart 侧那两个常量——屏上没有正文时显示的就是 `view.detailHint`。
      expect(defaults.map((r) => _expected(r)['title']).toList(),
          containsAll(<String>[kRssPickSource, kRssPickArticle]),
          reason: '${defaults.map((r) => r['case']).toList()}');
    });

    test('收藏那一发：图标翻、tooltip 不翻（照搬 flet，不是笔误）', () {
      final row = rows.firstWhere((r) => '${r['case']}' == 'starred');
      final expected = _expected(row);
      expect(expected['star_icon'], 'STAR');
      expect(expected['star_tooltip'], kRssStarTooltip,
          reason: 'flet 的 `show_article` 只给 `_detail_star.icon` 赋值（rss_view.py:355），'
              'tooltip 停在 `:117` 那句「收藏」；列表那一颗是两格一起换的（:339-340）。'
              '这一发把"照搬"钉在表里：要修它得单独一轮，别在迁移里顺手改——'
              '第九十七轮日志挂了号');
    });
  });

  group('那把日期尺：三张表里同一个瞬间必须写出同一个串', () {
    test('左栏 epoch 与中栏数字串 pub_date 折出来同一句（1e9 以上那一支）', () {
      final left = _expected(_rowsOf(_projection('rss.sources.rows'))
              .firstWhere((r) => '${r['case']}' == 'epoch_seconds'))['subtitle']
          as String;
      final middle = _expected(_rowsOf(_projection('rss.articles.rows'))
              .firstWhere(
                  (r) => '${r['case']}' == 'pub_date_numeric_epoch'))['subtitle']
          as String;
      final epoch = left.split('刷新于 ').last;
      expect(middle, startsWith('$epoch · '),
          reason: '两栏吃的是同一个 `_when`：同一个输入写出不同的串，就是有两把尺');
    });

    test('whenText 与 whenFromRaw 的分工：一个吃数、一个吃原串', () {
      expect(whenText(1700.0), '1700.0');
      expect(whenFromRaw('1700'), '1700');
      expect(whenFromRaw('1700.0'), '1700.0');
      expect(whenFromRaw(''), isEmpty, reason: '空段整段不出现，不是「01-01」');
      expect(whenText(0.0), isEmpty, reason: '0 由调用方折成「刷新于 从未」');
      expect(whenText(-1.0), isEmpty, reason: '负数不是"很久以前"，是不成立的那一格');
      expect(whenFromRaw('Mon, 05 Oct 2026 09:00:00 GMT'),
          'Mon, 05 Oct 2026 09:00:00 GMT',
          reason: '解不成数就原样上屏，这一屏不解析 feed 日期');
    });
  });
}
