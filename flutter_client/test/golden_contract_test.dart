/// 黄金样本契约：Dart 的模型必须跟着**真进程吐出来的字段**走（设计 §10）。
///
/// 这一支是"字段在三处手写重复"那条反面样本的解药：`BookshelfItem` 读的键、
/// 黄金样本里的键、以及服务端 action 表说的形状，三者任一处漂了就得有人红。
/// 样本本身由 `.probe/g2_golden_capture.py` 从真 `main.py --serve` 子进程抓
/// （G1 那一支已被它取代），不在这里手改——手改样本等于把契约改成自己想要的样子，
/// 而 `tests/test_gateway_golden_corpus.py` 会拿样本自己重算的 sha256 当场抓住。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';

/// 仓库根（`flutter_client/` 的上一级）：黄金样本跟着 Python 的 tests/ 走，
/// 因为它是**两侧共用的契约**，不是 Dart 工程的私有物。
final Directory _repoRoot = Directory.current.parent;

File _golden(String name) =>
    File('${_repoRoot.path}/tests/gateway/golden/$name.json');

Map<String, Object?> _load(String name) {
  final file = _golden(name);
  if (!file.existsSync()) {
    throw StateError('缺黄金样本 ${file.path}\n'
        '先跑 PYTHONIOENCODING=utf-8 PYTHONPATH=. '
        'python -u .probe/g1_golden_capture.py');
  }
  return jsonDecode(file.readAsStringSync(encoding: utf8)) as Map<String, Object?>;
}

Map<String, Object?> _envelopeOf(Map<String, Object?> sample) =>
    Map<String, Object?>.from(sample['response']! as Map);

List<Map<String, Object?>> _rowsOf(Map<String, Object?> sample) =>
    (_envelopeOf(sample)['data']! as List).cast<Map<String, Object?>>();

List<String> _fieldNamesOf(Map<String, Object?> sample) =>
    (sample['field_names']! as List).cast<String>();

void main() {
  group('engine.hello', () {
    final sample = _load('engine.hello');

    test('是对象不是数组（客户端按这个形状各自取字段）', () {
      expect(sample['data_shape'], 'object');
      expect(_envelopeOf(sample)['data'], isA<Map<String, Object?>>());
    });

    test('协议版本与 Dart 侧的 kProtocolVersion 同值', () {
      expect(sample['protocol_version'], kProtocolVersion);
    });
  });

  group('bookshelf.list', () {
    final sample = _load('bookshelf.list');
    final rows = _rowsOf(sample);
    final fieldNames = _fieldNamesOf(sample);

    test('行数与样本记录一致（G1 判据那一发的靶子）', () {
      expect(rows.length, sample['row_count']);
      // 正对照：样本必须是**非空**的一批书。零行会让下面每一条断言都假绿。
      expect(rows.length, greaterThan(0));
    });

    test('服务端回的是数组，不是 {items: …} 那种包装', () {
      expect(sample['data_shape'], 'list');
    });

    test('BookshelfItem 读的每一个键都在样本里（不靠拼写猜）', () {
      const consumed = <String>[
        'name', 'author', 'cover_url', 'url', 'source_url', 'origin',
        'durChapterTitle', 'durChapterTime', 'reading',
      ];
      for (final key in consumed) {
        expect(fieldNames, contains(key),
            reason: '样本里没有这个键 = 服务端从来不吐它，模型里那一格就是死的');
      }
    });

    test('每一行都过 fromJson，且行的身份键互不相同', () {
      final items = rows.map(BookshelfItem.fromJson).toList();
      expect(items.map((i) => i.identityKey).toSet().length, items.length,
          reason: 'ListView 拿它当行键；撞键会把两本书渲染成一本');
    });

    test('副标题的三段形状：作者 · 读到 X，缺一段就少一段', () {
      for (final row in rows) {
        final item = BookshelfItem.fromJson(row);
        final author = '${row['author'] ?? ''}';
        final expectedParts = <String>[
          if (author.isNotEmpty) author,
          if (item.chapterName.isNotEmpty) '读到 ${item.chapterName}',
        ];
        expect(item.subtitle, expectedParts.join(' · '));
      }
      // 正对照：这批书里既得有"有进度"的，也得有"没进度"的，
      // 否则上面那条循环只是在比一个恒等式。
      final withProgress = rows
          .map(BookshelfItem.fromJson)
          .where((i) => i.chapterName.isNotEmpty)
          .length;
      expect(withProgress, greaterThan(0), reason: '全都没进度 → "读到 X"那一支没人走过');
      expect(withProgress, lessThan(rows.length), reason: '全都有进度 → 收起那一支没人走过');
    });

    test('进度归一：reading 里键在但值为空串，不许回落到 durChapterTitle', () {
      // 这是 setdefault 的语义边界：`{}` 与 `{'chapter_name': ''}` 是两回事。
      final fromDur = BookshelfItem.fromJson({
        'name': '书', 'durChapterTitle': '第一章',
      });
      expect(fromDur.chapterName, '第一章');

      final emptyReading = BookshelfItem.fromJson({
        'name': '书', 'durChapterTitle': '第一章',
        'reading': {'chapter_name': ''},
      });
      expect(emptyReading.chapterName, '');

      final noProgress = BookshelfItem.fromJson({'name': '书'});
      expect(noProgress.chapterName, '');
      expect(noProgress.subtitle, '');
    });

    test('include_staged 那一发是同一个 action、不同请求形状', () {
      final staged = _load('bookshelf.list--include_staged');
      expect(staged['action'], 'bookshelf.list');
      expect((staged['request']! as Map)['payload'], {'include_staged': true});
      // 两发的**字段集**必须一致：暂存行不是另一种形状，客户端一套模型就够。
      expect(staged['field_names'], sample['field_names']);
    });
  });

  test('样本里的响应确实是合法信封（否则客户端解析器才是被测对象）', () {
    for (final name in <String>[
      'engine.hello', 'bookshelf.list', 'bookshelf.list--include_staged',
    ]) {
      final envelope =
          RpcEnvelope.decode(Map<String, Object?>.from(_load(name)['response']! as Map),
              action: name);
      expect(envelope.ok, isTrue, reason: name);
      expect(envelope.v, kProtocolVersion, reason: name);
    }
  });
}
