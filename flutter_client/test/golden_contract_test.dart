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
import 'package:legodo_client/core/rpc/engine_status.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/features/bookshelf/data/bookshelf_item.dart';
import 'package:legodo_client/features/check/data/check_models.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/find_books/data/category_row.dart';
import 'package:legodo_client/features/find_books/data/search_scope.dart';
import 'package:legodo_client/features/settings/data/theme_preferences.dart';
import 'package:legodo_client/features/sources/data/import_outcome.dart';
import 'package:legodo_client/features/sources/data/source_audit.dart';
import 'package:legodo_client/features/sources/data/source_record.dart';

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
        'python -u .probe/g2_golden_capture.py');
  }
  return jsonDecode(file.readAsStringSync(encoding: utf8)) as Map<String, Object?>;
}

Map<String, Object?> _envelopeOf(Map<String, Object?> sample) =>
    Map<String, Object?>.from(sample['response']! as Map);

List<Map<String, Object?>> _rowsOf(Map<String, Object?> sample) =>
    (_envelopeOf(sample)['data']! as List).cast<Map<String, Object?>>();

List<String> _fieldNamesOf(Map<String, Object?> sample) =>
    (sample['field_names']! as List).cast<String>();

File _libFile(String rel) =>
    File('${Directory.current.path}/lib/$rel');

/// 扫 Dart 源码里**方括号内的字符串字面量**（`json['key']` 的那个 key），按出现序去重。
///
/// 为什么不直接对整份文件正则：`check_models.dart:137` 那种注释里也会写字段名，
/// 拿文本比就是把"说明"当成"读取"——Python 侧那条同族守卫第一次变异核验就这么打不红。
///
/// 为什么是个词法扫描器：模型里满屏 `name: '${asMap['name'] ?? ''}'`，外层模板与插值里的
/// 字面量套在一起。正则会把 `'${asMap['` 读成一个完整字符串，于是真正的键**整个丢掉**
/// （第一次跑 15 个类只抽到 50 个键，其中 BookRow 一个都没剩）。
List<String> _bracketStringKeys(String src) {
  final keys = <String>[];
  final identifier = RegExp(r'^[A-Za-z_][A-Za-z0-9_]*$');

  late final int Function(int, int) scanString;
  late final void Function(int, int) scanCode;

  scanCode = (int i, int stop) {
    while (i < stop) {
      final ch = src[i];
      if (ch == '/' && i + 1 < stop && src[i + 1] == '/') {
        final newline = src.indexOf('\n', i);
        i = newline < 0 ? stop : newline;
        continue;
      }
      if (ch == '/' && i + 1 < stop && src[i + 1] == '*') {
        final end = src.indexOf('*/', i + 2);
        i = end < 0 ? stop : end + 2;
        continue;
      }
      if (ch == "'" || ch == '"') {
        i = scanString(i, stop);
        continue;
      }
      i++;
    }
  };

  /// 返回这个字面量结束后的下标；`${…}` 里的代码递归回去扫。
  scanString = (int start, int stop) {
    final quote = src[start];
    final triple = start + 2 < stop && src[start + 1] == quote && src[start + 2] == quote;
    final width = triple ? 3 : 1;
    final body = StringBuffer();
    var interpolated = false;
    var j = start + width;
    while (j < stop) {
      if (src[j] == r'\') {
        if (j + 1 < stop) body.write(src[j + 1]);
        j += 2;
        continue;
      }
      if (src[j] == quote && (j + width > stop ||
          src.substring(j, j + width) == quote * width)) {
        break;
      }
      if (src[j] == r'$' && j + 1 < stop && src[j + 1] == '{') {
        interpolated = true;
        var depth = 0;
        var k = j + 1;
        while (k < stop) {
          if (src[k] == '{') {
            depth++;
          } else if (src[k] == '}') {
            depth--;
            if (depth == 0) break;
          } else if (src[k] == "'" || src[k] == '"') {
            k = scanString(k, stop) - 1;
          }
          k++;
        }
        scanCode(j + 2, k);
        j = k + 1;
        continue;
      }
      body.write(src[j]);
      j++;
    }
    final literalEnd = (j < stop ? j : stop) + width;
    // 外层含插值时它自己是一句模板、不是键；键由上面那次递归收走。
    if (start == 0 || interpolated || literalEnd >= src.length) return literalEnd;
    if (src[start - 1] != '[' || src[literalEnd] != ']') return literalEnd;
    final text = body.toString();
    if (identifier.hasMatch(text) && !keys.contains(text)) keys.add(text);
    return literalEnd;
  };

  scanCode(0, src.length);
  return keys;
}


/// 一条绑定：Dart 的哪个类，吃哪一发黄金样本的哪个节点。
class _Feed {
  const _Feed(this.library, this.className, this.sample, [this.path = const <String>[]]);

  final String library;
  final String className;

  /// 黄金样本名（不含 `.json`）。
  final String sample;

  /// 从 `data` 往下走的几步；空 = 载荷本身。
  final List<String> path;

  String get label => '$className ← $sample'
      '${path.isEmpty ? '' : '/${path.join('/')}'}';

  /// 扫这个类读的键。类找不到必须炸——静默返回空集就是"这条没在查"。
  List<String> consumedKeys() {
    final file = _libFile(library);
    if (!file.existsSync()) {
      throw StateError('模型文件不见了：${file.path}');
    }
    final source = file.readAsStringSync(encoding: utf8);
    final classes = RegExp(r'^class (\w+)', multiLine: true)
        .allMatches(source)
        .map((m) => (start: m.start, name: m.group(1)!))
        .toList();
    final index = classes.indexWhere((c) => c.name == className);
    if (index < 0) {
      throw StateError('$library 里没有 `class $className`——绑定表指的是一个不存在的类，'
          '这一格不是在放行，是根本没跑');
    }
    final end = index + 1 < classes.length ? classes[index + 1].start : source.length;
    final keys = <String>[];
    keys.addAll(_bracketStringKeys(source.substring(classes[index].start, end)));
    return keys;
  }

  /// 载荷里真出现过的键：样本声明的 `field_names` ∪ 绑定节点自身的键。
  ///
  /// `field_names` 只在 `path` 为空时计入——那是**整发载荷**的键并集
  /// （`source.list` 声明 30 个，截断后的 5 行只带 20 个，`loginUrl` 属于
  /// "这条源设了才有"的那一类：`book_source.py:251-263` 的 `to_dict` 按 Gson 丢 null）。
  ///
  /// 为什么是**整棵树**而不是节点表层：`BookshelfItem` 读的 `chapter_name` 挂在
  /// `reading` 子对象里（进度归一，`bookshelf_item.dart:60-76`）。这一格问的是
  /// "服务端到底会不会吐这个键名"，不问它挂在哪一层——挂错层是死字段，键名错也是死字段，
  /// 但只有键名这一半能由这张表判；层级那一半由下面"每一行都解得开"那组的具体取值兜。
  Set<String> allowedKeys() {
    final sample = _load(sampleName);
    final node = _nodeAt(_envelopeOf(sample)['data'], path);
    if (node == null) {
      throw StateError('$sampleName 的载荷里没有 ${path.join('/')} 这一节——绑定表过期了');
    }
    final allowed = <String>{};
    if (path.isEmpty) allowed.addAll(_fieldNamesOf(sample));
    _keysAnywhere(node, allowed);
    return allowed;
  }

  String get sampleName => sample;
}

void _keysAnywhere(Object? node, Set<String> into) {
  if (node is Map) {
    for (final entry in node.entries) {
      into.add('${entry.key}');
      _keysAnywhere(entry.value, into);
    }
  } else if (node is List) {
    for (final row in node) {
      _keysAnywhere(row, into);
    }
  }
}

Object? _nodeAt(Object? data, List<String> path) {
  var node = data;
  for (final part in path) {
    if (node is! Map) return null;
    node = node[part];
  }
  return node;
}

/// 绑定表：15 个模型类 ↔ 12 发样本。少一格就是有一批键没人核（正对照在下面的计数里）。
const List<_Feed> _feeds = <_Feed>[
  _Feed('features/bookshelf/data/bookshelf_item.dart', 'BookshelfItem', 'bookshelf.list'),
  _Feed('features/find_books/data/book_row.dart', 'BookRow', 'browse.books'),
  _Feed('features/find_books/data/category_row.dart', 'CategoryRow', 'browse.categories'),
  _Feed('features/find_books/data/search_scope.dart', 'SearchScope', 'search.scopes'),
  _Feed('features/sources/data/source_record.dart', 'SourceRecord', 'source.list'),
  _Feed('features/sources/data/source_audit.dart', 'AuditReport', 'source.audit'),
  _Feed('features/sources/data/source_audit.dart', 'SaveOutcome', 'source.save'),
  _Feed('features/sources/data/import_outcome.dart', 'ImportOutcome', 'import.text'),
  _Feed('features/check/data/check_models.dart', 'CheckSnapshot', 'check.snapshot'),
  _Feed('features/check/data/check_models.dart', 'CheckRow', 'check.snapshot', ['results']),
  _Feed('features/check/data/check_models.dart', 'CheckSummary', 'check.snapshot', ['summary']),
  _Feed('features/check/data/check_models.dart', 'CheckTiering', 'check.snapshot', ['tiering']),
  _Feed('features/check/data/check_models.dart', 'CheckSourceReport', 'check.source'),
  _Feed('features/settings/data/theme_preferences.dart', 'ThemePreferences', 'theme.preferences'),
  _Feed('core/rpc/engine_status.dart', 'ServiceStatus', 'engine.status'),
];

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

  test('每一发样本都是合法信封、且服务端认账（31 发全覆盖）', () {
    final dir = Directory('${_repoRoot.path}/tests/gateway/golden');
    final names = dir
        .listSync()
        .whereType<File>()
        .where((f) => f.path.endsWith('.json'))
        .map((f) => f.path.split(Platform.pathSeparator).last.replaceAll('.json', ''))
        .toList()
        ..sort();
    // 正对照：样本面塌了（目录空/改名）这格会假绿，所以先钉数量。
    expect(names.length, greaterThanOrEqualTo(31), reason: '黄金样本只剩 ${names.length} 发');

    final hello = _envelopeOf(_load('engine.hello'))['data']! as Map;
    final advertised = (hello['actions']! as List).cast<String>();
    for (final name in names) {
      final sample = _load(name);
      final envelope = RpcEnvelope.decode(
          Map<String, Object?>.from(sample['response']! as Map), action: name);
      expect(envelope.ok, isTrue, reason: '$name：样本存的是一发失败，客户端没道理拿它当契约');
      expect(envelope.v, kProtocolVersion, reason: name);
      // 样本名 = `action`（带 `--参数` 的那几发除外，它们的 action 是主干）。
      expect(advertised, contains(sample['action']),
          reason: '$name 的 action 不在引擎自报的动作表里——要么样本过期，要么表动过没重抓');
    }
    // 全套样本来自同一个 schema 摘要：动作表一改摘要就变，旧样本当场不作数。
    expect(_staleDigestSamples(names), isEmpty,
        reason: '这些样本与 engine.hello 的 schema_digest 不一致——重抓样本');
  });

  group('模型吃的每一个键都在真载荷里（15 个类 ↔ 12 发样本）', () {
    test('绑定表与扫描器都接上了（正对照）', () {
      expect(_feeds.length, greaterThanOrEqualTo(15),
          reason: '绑定表只剩 ${_feeds.length} 格——删一格就是有一批键没人核');
      final total = _feeds.fold<int>(
          0, (sum, feed) => sum + feed.consumedKeys().length);
      // 实测 91 个键（15 个类各自读的 `['key']` 之和）。留 6 的余量给模型加字段，
      // 但绑定表被人删一格（最少 2 个键）一定跌破。
      expect(total, greaterThanOrEqualTo(85),
          reason: '只抽到 $total 个键——扫描器或类名不对，这条不是在放行');
    });

    for (final feed in _feeds) {
      test(feed.label, () {
        final consumed = feed.consumedKeys();
        expect(consumed, isNotEmpty, reason: '${feed.className} 一个键都没抽到');
        final allowed = feed.allowedKeys();
        for (final key in consumed) {
          expect(allowed, contains(key),
              reason: '${feed.className} 读 json[\'$key\']，但 $feed.sampleName'
                  '${feed.path.isEmpty ? '' : '/${feed.path.join('/')}'} 的真载荷里没有这个键。'
                  '服务端从来不吐它 = 模型里那一格永远是空的（界面画的是自己编的默认值）');
        }
      });
    }
  });

  group('样本的每一行都解得开，行键不许撞', () {
    List<Map<String, Object?>> rows(String name, {List<String> path = const []}) {
      final node = _nodeAt(_envelopeOf(_load(name))['data'], path);
      if (node is! List) {
        throw StateError('$name 的载荷不是数组（path=$path）——这一格测的东西变了');
      }
      final decoded = node.cast<Map<String, Object?>>().toList();
      // 零行会让下面每一条循环都假绿（记忆 `zero-counts-need-a-positive-control`）。
      expect(decoded, isNotEmpty, reason: '$name 一行都没有');
      return decoded;
    }

    test('bookshelf.list → BookshelfItem.identityKey 逐行唯一', () {
      final items = rows('bookshelf.list').map(BookshelfItem.fromJson).toList();
      expect(items.map((i) => i.identityKey).toSet().length, items.length);
    });

    test('source.list → SourceRecord.url 逐行唯一（它就是今天的行键）', () {
      final decoded = rows('source.list');
      final records = decoded.map(SourceRecord.fromJson).toList();
      expect(records.map((r) => r.url).toSet().length, records.length);
      expect(records.every((r) => r.enabled), isTrue,
          reason: '这一发样本里全是启用源——停用的那一支由下面那格合成行走，别把"没走到"读成"走过"');
      // 停用的画法（标题变灰）在 flet 那边是 `color=GREY_400 if not enabled`，
      // 而载荷截断只留了 5 行，全是启用的。用真行改一格 enabled 走另一支。
      final disabled = SourceRecord.fromJson(
          Map<String, Object?>.from(decoded.first)..['enabled'] = false);
      expect(disabled.enabled, isFalse);
      // 键不在 = 启用（`book_source.py` 的 dataclass 默认），这一格两边都得一样。
      expect(SourceRecord.fromJson({'bookSourceUrl': 'https://a'}).enabled, isTrue);
    });

    test('engine.status → serviceId 逐行唯一（34 个无头服务）', () {
      final statuses = rows('engine.status').map(ServiceStatus.fromJson).toList();
      expect(statuses.map((s) => s.serviceId).toSet().length, statuses.length);
      expect(statuses.length, greaterThanOrEqualTo(30));
      // 状态词汇表在服务端（`app/core/lifecycle.py`），客户端只按它决定标不标红。
      const states = <String>{
        'INITIALIZED', 'RUNNING', 'SUSPENDED', 'STOPPED', 'STARTING', 'STOPPING',
      };
      final seen = statuses.map((s) => s.state).toSet();
      expect(seen.difference(states), isEmpty, reason: '服务端回了客户端词汇表外的状态名：$seen');
    });

    test('search.scopes → scope 逐行唯一，且两个哨兵真在表里', () {
      final scopes = rows('search.scopes').map(SearchScope.fromJson).toList();
      expect(scopes.map((s) => s.value).toSet().length, scopes.length);
      expect(scopes.map((s) => s.value), contains(kScopeAll));
      expect(scopes.map((s) => s.value), contains(kScopeUngrouped));
      // 正对照：真有普通分组，否则上面两格只是在比哨兵自己。
      expect(scopes.where((s) => !s.isSentinel), isNotEmpty);
      // **空分组不许被丢掉**：实测 `__ungrouped__` 这一发就是 0 源（真库里每条源都有组），
      // flet 的对话框照样把"未分组（0 源）"画出来。客户端自己 filter 掉就是两个界面两种范围表。
      expect(scopes.where((s) => s.sourceCount == 0), isNotEmpty,
          reason: '载荷里没有 0 源的范围了——这条主张的靶子变了，界面那侧的过滤该重新问');
    });

    test('browse.books → BookRow.identityKey 逐行唯一', () {
      final books = rows('browse.books').map(BookRow.fromJson).toList();
      expect(books.map((b) => b.identityKey).toSet().length, books.length);
      expect(books.every((b) => b.url.isNotEmpty && b.origin.isNotEmpty), isTrue,
          reason: 'url/origin 是空的行：入架与换源都按它找回书源，空=点了没反应');
    });

    test('search.start 的事件载荷也用同一个 BookRow（搜索结果与分类书单共用模型）', () {
      final payload = _load('search.start')['event_sample']! as Map;
      final items = (payload['payload']! as Map)['items']! as List;
      final rows = items.cast<Map<String, Object?>>();
      expect(rows, isNotEmpty, reason: 'search.start 的 event_sample 里没有 items');
      final books = rows.map(BookRow.fromJson).toList();
      expect(books.map((b) => b.identityKey).toSet().length, books.length);
      expect(books.first.title, isNotEmpty);
    });

    test('browse.categories → CategoryRow.url 逐行唯一', () {
      final categories = rows('browse.categories').map(CategoryRow.fromJson).toList();
      expect(categories.map((c) => c.url).toSet().length, categories.length);
    });
  });

  group('对象载荷解得开，且结论落在客户端认识的那一格', () {
    test('check.snapshot：结果行、分档、待深检都能数出来', () {
      final snap = CheckSnapshot.fromJson(
          Map<String, Object?>.from(_envelopeOf(_load('check.snapshot'))['data']! as Map));
      expect(snap.results, isNotEmpty);
      expect(snap.summary, isNotNull, reason: '跑完了还回 null，收尾那一句就没法渲染');
      expect(snap.deepQueueCount, greaterThan(0));
      expect(snap.depth, 'auto');
      // 行的 state_class 必须落在词汇表里：表外的值会让那一格显示英文原词。
      for (final row in snap.results) {
        expect(kCheckStateLabels.keys, contains(row.stateClass),
            reason: '检测结论 ${row.stateClass} 没有中文标签');
      }
    });

    test('check.source：编辑器那一发的 31 键结论在词汇表里', () {
      final report = CheckSourceReport.fromJson(Map<String, Object?>.from(
          _envelopeOf(_load('check.source'))['data']! as Map));
      expect(report.raw, isNotEmpty);
      if (report.checkState.isNotEmpty) {
        expect(kCheckStateLabels.keys, contains(report.checkState),
            reason: '${report.checkState} 不在八态词汇表里');
      }
    });

    test('source.audit / source.save：审计报告原样嵌在保存的回话里', () {
      final audit = AuditReport.fromJson(Map<String, Object?>.from(
          _envelopeOf(_load('source.audit'))['data']! as Map));
      expect(audit.sourceId, isNotEmpty);
      final saved = SaveOutcome.fromJson(Map<String, Object?>.from(
          _envelopeOf(_load('source.save'))['data']! as Map));
      expect(saved.saved, isTrue);
      // `SaveOutcome.fromJson` 在载荷没带 audit 时退回**空审计报告**（不是 null），
      // 所以"嵌得进去"的证人不是 isNotNull（那是恒真），是里面那个 source_id 真落到了格子上。
      expect(saved.audit.sourceId, saved.sourceId,
          reason: '保存回话里的 audit 没解进来——模型注释说的"原样嵌入"不成立了');
    });

    test('import.text / import.url：kind 与计数照真载荷', () {
      for (final name in <String>['import.text', 'import.url']) {
        final outcome = ImportOutcome.fromJson(
            Map<String, Object?>.from(_envelopeOf(_load(name))['data']! as Map));
        expect(outcome.kind, 'book_source', reason: name);
        // 实测：import.text 落了 1 条、import.url 落了 0 条，两发 placeholder 都是 false。
        // 0 条不是失败（errors 为空才是判据），所以这里比的是 `ok` 而不是 `count`。
        expect(outcome.errors, isEmpty, reason: name);
        expect(outcome.ok, isTrue, reason: name);
        if (name == 'import.text') expect(outcome.count, greaterThan(0), reason: name);
      }
      // corpus 里**没有** placeholder=true 的那一发（这批语料全导的是书源 JSON），
      // 所以"未落地（占位）"那一支只能用合成行走，并且明写它不是黄金证据：
      // 界面把占位说成"导入了 0 条"就是记忆 `aggregated-counters-hide-which-call` 的糊法。
      final placeholder = ImportOutcome.fromJson(<String, Object?>{
        'kind': 'subscription', 'count': 0, 'errors': <String>[],
        'warnings': <String>[], 'placeholder': true,
      });
      expect(placeholder.ok, isFalse);
    });

    test('theme.preferences / theme.save：三格回写不丢', () {
      for (final name in <String>['theme.preferences', 'theme.save']) {
        final data = Map<String, Object?>.from(
            _envelopeOf(_load(name))['data']! as Map);
        final prefs = ThemePreferences.fromJson(data);
        expect(prefs.toJson(), equals(data), reason: name);
        expect(kAppSkins.map((s) => s.id), contains(prefs.skinId),
            reason: '$name 回了表外的皮肤 id');
      }
    });

    test('settings.read 的外观藏在 appearance.snapshot 那一格里', () {
      // `settings.read` 回的是**整张设置表**（实测三个点号键：
      // `reader.max_width` / `reader.font_size` / `appearance.snapshot`），
      // 不是三格偏好。把 ThemePreferences 直接喂给它会得到全默认值——
      // 那才是这一格要钉住的东西。
      final data = Map<String, Object?>.from(
          _envelopeOf(_load('settings.read'))['data']! as Map);
      expect(data.keys, containsAll(<String>['reader.max_width', 'reader.font_size',
          'appearance.snapshot']));
      final snapshot = Map<String, Object?>.from(data['appearance.snapshot']! as Map);
      final prefs = ThemePreferences.fromJson(snapshot);
      expect(prefs.toJson(), equals(snapshot), reason: 'settings.read 的那个快照与三格对不上');
    });
  });
}

/// 与 `engine.hello` 的 schema_digest 不一致的样本名（应当为空）。
///
/// 摘要由动作表算出来（`app/gateway/methods.py` 那一表），所以"表动过、样本没重抓"
/// 在这里就红——而不是等到某个模型安静地读到一个已经不存在的键。
List<String> _staleDigestSamples(List<String> names) {
  final digest = _load('engine.hello')['schema_digest'];
  return [
    for (final name in names)
      if (_load(name)['schema_digest'] != digest) name
  ];
}
