/// 投影表契约：**同一份载荷，两边各渲染一次，整句逐字比**。
///
/// 为什么要有这一支，而不是"两边源码里含同一个中文串"就算接上：
/// `tests/test_gateway_contract_single_source.py` 里那几条文案守卫比的是**字面量**，
/// 它抓得住"补验正文"被写成"补验"，抓不住**拼法**不同——比如分档尾巴的
/// 先后顺序、`plan_total == 0` 时那一段到底出不出现、副标题里 `group` 与 URL 谁在前。
/// 那些是渲染出来的结果，不是源码里的串。
///
/// 表的**答案列**一律由 flet 自己的渲染函数产出（`.probe/g2e_projection_tables.py`
/// 直接调 `_summary_line` / `BookSourceListView()._row` 再从控件树读回 `ft.Text.value`），
/// 输入可以是从黄金样本改出来的合成行，答案**不许**手填——手填的答案就是"Dart 自己
/// 证明自己对"那一族反面样本（记忆里 `measurement-corpus-must-not-be-self-proving`）。
///
/// 正控在每张表上都给了：抽到 0 行的表会让下面的循环一条都不跑，然后绿。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/features/check/data/check_models.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';
import 'package:legodo_client/features/sources/presentation/sources_screen.dart';

/// 仓库根（`flutter_client/` 的上一级）：投影表跟着黄金样本放在 Python 的 tests/ 下，
/// 因为它是两侧共用的契约。
final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _projection(String name) {
  final file =
      File('${_repoRoot.path}/tests/gateway/projection/$name.json');
  if (!file.existsSync()) {
    throw StateError('缺投影表 ${file.path}\n'
        '先跑 PYTHONIOENCODING=utf-8 PYTHONPATH=. '
        'python -u .probe/g2e_projection_tables.py');
  }
  return jsonDecode(file.readAsStringSync(encoding: utf8)) as Map<String, Object?>;
}

List<Map<String, Object?>> _rowsOf(Map<String, Object?> table) =>
    (table['rows']! as List).cast<Map<String, Object?>>();

void main() {
  group('收尾那一句：flet 的 _summary_line 与 Dart 的 checkSummaryLine 逐字相同', () {
    final table = _projection('check.snapshot.summary_line');
    final rows = _rowsOf(table);

    test('表里有货（7 发覆盖：原样 / 取消 / 没结论 / 无分档 / 队列空 / 分档只在 summary / plan_total=0）', () {
      // 实测 7 发。少了就是投影探针没跑到那几支，循环会一条不跑地绿过去。
      expect(rows.length, greaterThanOrEqualTo(7));
      expect(table['renderer'], contains('_summary_line'));
    });

    for (final row in rows) {
      final caseName = '${row['case']}';
      test('$caseName → 两边同一句', () {
        final payload =
            Map<String, Object?>.from(row['payload']! as Map);
        expect(
          checkSummaryLine(CheckSnapshot.fromJson(payload)),
          equals('${row['expected']}'),
          reason: '$caseName：flet 那侧的结论是 '
              '${row['why']}（${row['expected']}）',
        );
      });
    }

    test('空串那一发不许被当成"检测已停止"：它是"还没结论"', () {
      final running = rows.firstWhere((r) => '${r['case']}' == 'running');
      expect(running['expected'], isEmpty);
      final snap = CheckSnapshot.fromJson(
          Map<String, Object?>.from(running['payload']! as Map));
      expect(snap.summary, isNull, reason: 'summary 缺席才是空串的来源');
      expect(snap.cancelled, isFalse);
    });
  });

  group('书源行：flet 的 _row 与 Dart 的 title/subtitle 逐字相同', () {
    final table = _projection('source.list.rows');
    final rows = _rowsOf(table);

    test('表里有货（8 发：5 条真行 + 无名 / 无组 / 长 URL 三种退化）', () {
      // 实测 8 发。那三支退化行是**合成**输入（去掉一个键、换一条长 URL），
      // 答案仍是 flet 渲染的——真样本只有"有名有组短 URL"那一种形状。
      expect(rows.length, greaterThanOrEqualTo(8));
      expect(table['renderer'], contains('BookSourceListView'));
    });

    for (final row in rows) {
      final caseName = '${row['case']}';
      test('$caseName → 标题与副标题同一句', () {
        final record = SourceRecord.fromJson(
            Map<String, Object?>.from(row['row']! as Map));
        expect(record.title, equals('${row['title']}'),
            reason: '$caseName 的标题（flet：bookSourceName or bookSourceUrl）');
        expect(record.subtitle, equals('${row['subtitle']}'),
            reason: '$caseName 的副标题（flet：" · ".join(group, _short_url(url))，'
                '缺一段就少一段，顺序不能反）');
      });
    }

    test('长 URL 那一发确实长过了 48：不然缩短那支等于没测', () {
      final long = rows.firstWhere((r) => '${r['case']}' == 'long_url');
      final url = SourceRecord.fromJson(
              Map<String, Object?>.from(long['row']! as Map))
          .url;
      expect(url.length, greaterThan(48));
      expect(long['subtitle'] as String, endsWith('…'),
          reason: 'flet 的 _short_url 尾巴是省略号，不是三个点也不是截断');
    });

    test('无名那支退回 URL，无组那支副标题少一段', () {
      final nameless = rows.firstWhere((r) => '${r['case']}' == 'nameless');
      final groupless = rows.firstWhere((r) => '${r['case']}' == 'groupless');
      final namelessRow =
          Map<String, Object?>.from(nameless['row']! as Map);
      final grouplessRow =
          Map<String, Object?>.from(groupless['row']! as Map);
      expect(nameless['title'], equals(namelessRow['bookSourceUrl']),
          reason: 'name 空 → 标题就是 url 本身（flet 那个 `or`）');
      expect(groupless['subtitle'],
          equals(grouplessRow['bookSourceUrl']),
          reason: 'group 空 → 副标题只剩 URL，前面不带分隔符');
    });
  });

  group('批量删除那串名字：flet 的 _names_of 与 Dart 的 namesForBatchDeletion 逐字相同',
      () {
    final table = _projection('source.batch_delete.names');
    final rows = _rowsOf(table);

    /// 记录表就是 flet 那边的 `self._records`：**整份**黄金书源表，不是选中那几条。
    /// 两边都传全表，`等 N 条` 那个 N 才比的是同一件事（N 取的是 urls.length，
    /// 但"表外 url 退回 url 本体"那一支要求 byUrl 查得到/查不到都对得上）。
    final records = (_goldenPayload('source.list') as List)
        .cast<Map<String, Object?>>()
        .map((row) => SourceRecord.fromJson(row))
        .toList(growable: false);

    test('表里有货（5 发：1/2/3/5 条 + 表外 url），且记录表非空', () {
      expect(rows.length, greaterThanOrEqualTo(5));
      expect(records.length, greaterThanOrEqualTo(5));
      expect(table['renderer'], contains('_names_of'));
    });

    for (final row in rows) {
      final urls = (row['urls']! as List).cast<String>();
      test('${urls.length} 条 → ${row['expected']}', () {
        expect(namesForBatchDeletion(urls, records),
            equals('${row['expected']}'),
            reason: '${row['why'] ?? '连接符是 `、`，超过三条补 " 等 N 条"'}');
      });
    }

    test('" 等 N 条" 的 N 是选中条数，不是记录表条数', () {
      final five = rows.firstWhere((r) => (r['urls']! as List).length == 5);
      expect(five['expected'] as String, contains(' 等 5 条'),
          reason: '表里那 5 条是 urls 的长度；records 有 920 条时这句不许变 920');
    });
  });
}

/// `source.list` 那一发的载荷是**列表**，按对象取的那份helper用不上，这一支单独走。
Object? _goldenPayload(String name) {
  final file =
      File('${_repoRoot.path}/tests/gateway/golden/$name.json');
  if (!file.existsSync()) {
    throw StateError('缺黄金样本 ${file.path}\n'
        '先跑 PYTHONIOENCODING=utf-8 PYTHONPATH=. '
        'python -u .probe/g2_golden_capture.py');
  }
  final sample = jsonDecode(file.readAsStringSync(encoding: utf8))
      as Map<String, Object?>;
  final envelope = Map<String, Object?>.from(sample['response']! as Map);
  return envelope['data'];
}
