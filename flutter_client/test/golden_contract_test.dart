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
import 'package:legodo_client/features/bookshelf/data/source_switch_models.dart';
import 'package:legodo_client/features/check/data/check_models.dart';
import 'package:legodo_client/features/find_books/data/book_row.dart';
import 'package:legodo_client/features/find_books/data/category_row.dart';
import 'package:legodo_client/features/find_books/data/search_scope.dart';
import 'package:legodo_client/features/reader/data/bookmark_row.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/data/chapter_row.dart';
import 'package:legodo_client/features/reader/data/content_search_result.dart';
import 'package:legodo_client/features/reader/data/reading_position.dart';
import 'package:legodo_client/features/reader/data/reader_session.dart';
import 'package:legodo_client/features/reader/data/staged_record.dart';
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

/// 绑定表：30 个模型类 ↔ 23 发样本。少一格就是有一批键没人核（正对照在下面的计数里）。
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
  // reader 那一族（G3-C2）：13 发动作、7 个模型类，每个类都钉在自己那一发的真载荷上。
  _Feed('features/reader/data/reading_position.dart', 'ReadingPosition', 'reader.position_get'),
  _Feed('features/reader/data/chapter_row.dart', 'ChapterRow', 'reader.toc'),
  _Feed('features/reader/data/chapter_document.dart', 'ChapterDocument', 'reader.content'),
  _Feed('features/reader/data/chapter_document.dart', 'ContentBlock', 'reader.content', ['blocks']),
  _Feed('features/reader/data/bookmark_row.dart', 'BookmarkRow', 'reader.bookmarks'),
  _Feed('features/reader/data/staged_record.dart', 'StagedRecord', 'reader.staged'),
  _Feed('features/reader/data/reader_session.dart', 'ReaderSession', 'reader.open'),
  _Feed('features/reader/data/reader_session.dart', 'CloseOutcome', 'reader.close'),
  _Feed('features/reader/data/content_search_result.dart', 'ContentSearchResult', 'reader.search_content'),
  _Feed('features/reader/data/content_search_result.dart', 'ContentHit', 'reader.search_content', ['hits']),
  // 换源那一族（G3-D3）：模型都钉在 `switch.*` 那五发的真载荷上。
  // `SwitchProgress` 绑的是快照而不是进度帧样本——进度帧的六格是快照那九格的子集，
  // 而载荷样本里唯一带这组键名的常驻节点就是快照（`switch.scan` 的 progress_sample
  // 由下面那一格具体取值兜）。
  _Feed('features/bookshelf/data/source_switch_models.dart', 'SwitchCandidate', 'switch.snapshot', ['candidates']),
  _Feed('features/bookshelf/data/source_switch_models.dart', 'SwitchProgress', 'switch.snapshot'),
  _Feed('features/bookshelf/data/source_switch_models.dart', 'SwitchRun', 'switch.snapshot'),
  _Feed('features/bookshelf/data/source_switch_models.dart', 'SwitchApplyResult', 'switch.apply'),
  // 种子那一发回的是**裸书行**，与 `browse.books` 同一套 35 键（服务端都走 `_book_item_dict`）。
  _Feed('features/find_books/data/book_row.dart', 'BookRow', 'switch.seeds'),
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

  test('每一发样本都是合法信封、且服务端认账（61 发全覆盖）', () {
    final dir = Directory('${_repoRoot.path}/tests/gateway/golden');
    final names = dir
        .listSync()
        .whereType<File>()
        .where((f) => f.path.endsWith('.json'))
        .map((f) => f.path.split(Platform.pathSeparator).last.replaceAll('.json', ''))
        .toList()
        ..sort();
    // 正对照：样本面塌了（目录空/改名）这格会假绿，所以先钉数量。
    // 56 → 61 这一跳是 G3-D3 那五发（`switch.*`）；改这一格之前先数一遍目录。
    expect(names.length, greaterThanOrEqualTo(61), reason: '黄金样本只剩 ${names.length} 发');

    // 语料里有一族**故意失败**的信封：协议面必须有一发让客户端看清"错误信封长什么样"
    // （会话不是这本书 → 拒答而不是回空目录，回空目录看起来像书坏了）。
    // 这一族必须**具名**：表外的样本一律按成功信封要求，所以"整批都成了失败"
    // 这条路是走不通的；反过来，具名的那一发如果哪天真的成功了，这里也红。
    const errorSamples = <String>{'reader.toc--no-session'};
    for (final name in errorSamples) {
      expect(names, contains(name), reason: '欠着的错误样本 $name 不在语料里');
    }

    final hello = _envelopeOf(_load('engine.hello'))['data']! as Map;
    final advertised = (hello['actions']! as List).cast<String>();
    for (final name in names) {
      final sample = _load(name);
      final envelope = RpcEnvelope.decode(
          Map<String, Object?>.from(sample['response']! as Map), action: name);
      if (errorSamples.contains(name)) {
        expect(envelope.ok, isFalse, reason: '$name：这一发存的就是失败信封');
        expect(envelope.error, isNotNull, reason: '$name：失败信封没带原因');
        expect(envelope.error!.isNotEmpty, isTrue, reason: '$name：失败信封的原因是空串');
        expect(envelope.data, isNull, reason: '$name：失败信封还带着 data');
        expect(() => envelope.dataOrThrow(action: name), throwsA(isA<EngineFailure>()),
            reason: '$name：客户端拿它不当失败处理');
      } else {
        expect(envelope.ok, isTrue,
            reason: '$name：样本存的是一发失败，客户端没道理拿它当契约');
      }
      expect(envelope.v, kProtocolVersion, reason: name);
      // 样本名 = `action`（带 `--参数` 的那几发除外，它们的 action 是主干）。
      expect(advertised, contains(sample['action']),
          reason: '$name 的 action 不在引擎自报的动作表里——要么样本过期，要么表动过没重抓');
    }
    // 全套样本来自同一个 schema 摘要：动作表一改摘要就变，旧样本当场不作数。
    expect(_staleDigestSamples(names), isEmpty,
        reason: '这些样本与 engine.hello 的 schema_digest 不一致——重抓样本');
  });

  group('模型吃的每一个键都在真载荷里（30 个类 ↔ 23 发样本）', () {
    test('绑定表与扫描器都接上了（正对照）', () {
      expect(_feeds.length, greaterThanOrEqualTo(30),
          reason: '绑定表只剩 ${_feeds.length} 格——删一格就是有一批键没人核');
      final total = _feeds.fold<int>(
          0, (sum, feed) => sum + feed.consumedKeys().length);
      // 这一格是**仪器接上没接上**的读数，不是契约：下限留得比实测低一截，
      // 但绑定表被人删一格（最少 2 个键）一定跌破。实测总数在批量验证那一轮
      // （任务 #29）现量现写，这里不存旧值——存了就成了"没人测也像个数"。
      expect(total, greaterThanOrEqualTo(150),
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

  group('reader.* 那一族解得开，且那把尺跨端是同一个数（G3-C2）', () {
    Map<String, Object?> data(String name) =>
        Map<String, Object?>.from(_envelopeOf(_load(name))['data']! as Map);

    Map<String, Object?> requestPayload(String name) =>
        Map<String, Object?>.from((_load(name)['request']! as Map)['payload']! as Map);

    List<Map<String, Object?>> rows(String name) {
      final node = _envelopeOf(_load(name))['data'];
      if (node is! List) {
        throw StateError('$name 的载荷不是数组——这一格测的东西变了');
      }
      final decoded = node.cast<Map<String, Object?>>().toList();
      expect(decoded, isNotEmpty, reason: '$name 一行都没有');
      return decoded;
    }

    test('reader.open 的 chapterCount 与 reader.toc 的行数对上', () {
      final chapters = rows('reader.toc');
      for (final name in <String>[
        'reader.open', 'reader.open--reopen', 'reader.open--preview'
      ]) {
        final session = ReaderSession.fromJson(data(name));
        expect(session.bookUrl, session.book.url,
            reason: '$name：会话坐标的两格对不上。后续每一发都按 (source_url, book_url) '
                '找会话，界面拿 book.url 拼请求会撞上 reader.toc--no-session 那份错误信封');
        expect(session.sourceUrl, isNotEmpty, reason: name);
        // 三发 open 都对着同一份目录（假源只有一本 6 章的书），所以这一格是跨发比对，不是自证。
        expect(session.chapterCount, chapters.length,
            reason: '$name：会话自报的章数与目录行数不一致 = 投影那边丢了行或多算了行');
        expect(chapters.length, greaterThan(0));
      }
    });

    test('位置的九键往返：fromJson→toJson 与样本逐格同形，段号 null 不读成 0', () {
      final positions = <String, Map<String, Object?>>{
        for (final name in <String>[
          'reader.position_get', 'reader.position_save', 'reader.position_save--clamped'
        ])
          name: data(name),
        'reader.open.position': Map<String, Object?>.from(
            data('reader.open')['position']! as Map),
      };
      expect(positions.length, greaterThanOrEqualTo(4), reason: '正对照：位置样本得有两真一夹一回话');
      for (final entry in positions.entries) {
        final json = entry.value;
        expect(ReadingPosition.fromJson(json).toJson(), equals(json),
            reason: '${entry.key}：往返少一格或改一格 = 下一次 position_save 会把服务端那一格覆盖成默认值。'
                'from_reading 按这九个名字取值，键名漂了不报错');
      }
      // null 与 0 是两件事：reader.open 那一发的 chapter_paragraph 实测就是 null（新会话没段号），
      // 而 position_save 那发是 2。少这一格证人，上面那条往返就成了唯一的一条腿。
      expect(ReadingPosition.fromJson(positions['reader.open.position']!).paragraphIndex, isNull);
      expect(ReadingPosition.fromJson(positions['reader.position_save']!).paragraphIndex, isNotNull);
      // 键名逐一对上样本的 field_names（蛇形那九个，不是驼形）。
      expect(positions['reader.position_get']!.keys.toSet(),
          Set<String>.from(_fieldNamesOf(_load('reader.position_get'))));
    });

    test('存档没有 chapter_url：续读按 chapter_index 认章，右端点是 content 的 totalChars', () {
      final saved = ReadingPosition.fromJson(data('reader.position_save'));
      final clamped = ReadingPosition.fromJson(data('reader.position_save--clamped'));
      final stored = ReadingPosition.fromJson(data('reader.position_get'));
      final chapters = rows('reader.toc').map(ChapterRow.fromJson).toList();
      final doc = ChapterDocument.fromJson(data('reader.content'));

      expect(stored.chapterUrl, isEmpty,
          reason: 'books 表不存 chapter_url（设计 §3.9），读档那一发实测回空串。'
              '客户端按 chapter_url 找章会在重启后落到空 url，reader.content 读到另一章');
      // 三发样本对着同一章：落库回话里的 url == 目录里那个 index 的 url。
      expect(doc.chapterUrl, chapters[clamped.chapterIndex].url,
          reason: '正文样本与位置样本不是同一章，下面那三条比对就成了自证');
      expect(saved.chapterUrl, chapters[saved.chapterIndex].url);
      expect(stored.characterOffset, clamped.characterOffset,
          reason: '读回来的与落库回话在尺上必须是同一个数，否则界面显示的位置不是存进去的位置');
      expect(stored.paragraphIndex, clamped.paragraphIndex);

      // 收口在**回话**里：请求送 3.5/15801/48，服务端夹完整数落库再回话（实测）。
      final sent = Map<String, Object?>.from(requestPayload('reader.position_save--clamped')
          ['position_json']! as Map);
      expect((sent['scroll_ratio']! as num).toDouble(), greaterThan(1.0));
      expect(clamped.scrollRatio, 1.0);
      expect((sent['chapter_pos']! as num).toInt(), greaterThan(doc.totalChars));
      expect(clamped.characterOffset, doc.totalChars,
          reason: '那把尺的右端点就是 reader.content 的 totalChars（实测夹完 5802=5802）。'
              '进度条与夹偏移用这一格，别拿 blocks.length 或段数当右端点');
    });

    test('reader.content：字数这把尺只有服务端量得准，现取与走缓存同形同数', () {
      final liveSample = _load('reader.content');
      final cachedSample = _load('reader.content--from-cache');
      expect(cachedSample['field_names'], equals(liveSample['field_names']),
          reason: '走缓存那一发换了形状：客户端只认一套键');
      final live = ChapterDocument.fromJson(data('reader.content'));
      final cached = ChapterDocument.fromJson(data('reader.content--from-cache'));
      expect(live.blocks, isNotEmpty);
      expect(cached.totalChars, live.totalChars);
      expect(cached.paragraphCount, live.paragraphCount);

      final rawChars = live.blocks
          .where((b) => b.isText)
          .fold<int>(0, (sum, b) => sum + (b.value?.length ?? 0));
      expect(rawChars, greaterThan(0),
          reason: '正对照：一个文本块都没有的话，下面那条比的是 0 与 0');
      expect(rawChars, isNot(live.totalChars),
          reason: '客户端把 value 直接拼起来自己数是 $rawChars 字，服务端那把尺 ${live.totalChars} 字'
              '（实测差 ${rawChars - live.totalChars}：切段时去掉的空白与段首缩进）。'
              '两侧各自数一遍，翻页落点就不是同一个数——这一条不等号就是"别在 Dart 侧数"的证人');
      expect(live.paragraphCount, isNot(live.blocks.length),
          reason: '一个 text 块里是好几个段（实测 3 块 8 段），把块数当段数会把偏移算短');
    });

    test('内容块 kind 全在客户端词汇表里，且 text 与 media 两支都有活样本', () {
      final doc = ChapterDocument.fromJson(data('reader.content'));
      final kinds = doc.blocks.map((b) => b.kind).toSet();
      expect(kinds.difference(kContentBlockKinds), isEmpty,
          reason: '服务端吐了表外的 kind：$kinds——那一种在界面上根本没有画法');
      expect(kinds, contains('text'), reason: '正对照：没有文本块的话 isText 那一支没人走过');
      expect(kinds, contains('image'), reason: '正对照：没有媒体块的话 isMedia/url 那一支没人走过');
      final image = doc.blocks.firstWhere((b) => b.isMedia);
      expect(image.binary, isTrue);
      expect(image.value, isNull,
          reason: 'binary=true 的块 value 是 null（字节不过协议面，设计 §4.4），取内容只认 url+headers');
      expect(image.headers, isEmpty);
      expect(Uri.tryParse(image.url)?.hasScheme, isTrue,
          reason: '相对地址过完协议面就找不回根：Image.network 直接挂（第八十六轮补 url 那一格救的就是它）');
      // audio/video 两支语料里没有证人（假源只挂了一张 .jpg）；表里有它们是为了对齐
      // 服务端 `_MEDIA_EXTENSIONS` 那三个键。那一支的画法在 G3-C3/G4 现场再对。
      expect(kinds.intersection(<String>{'audio', 'video'}), isEmpty);
    });

    test('目录行的八键白名单就是全部：index 连续，付费与卷标题两支在语料里没有证人', () {
      final raw = rows('reader.toc');
      const allowed = <String>{
        'name', 'url', 'index', 'isVolume', 'isVip', 'isPay', 'tag', 'wordCount'
      };
      for (final row in raw) {
        expect(row.keys.toSet(), allowed,
            reason: '样本行的键与 action 表 `fields=` 不一致——要么投影改了没重抓样本，要么这格测的东西变了');
      }
      final chapters = raw.map(ChapterRow.fromJson).toList();
      expect(chapters.map((c) => c.index).toList(),
          equals(List.generate(chapters.length, (i) => i)),
          reason: '进度按 index 认章（books 不存 chapter_url），跳号或重号会读到另一章');
      expect(chapters.every((c) => c.url.isNotEmpty), isTrue,
          reason: 'reader.content 按 chapter_url 取正文，空 url = 点了没反应');
      expect(chapters.every((c) => !c.paywalled && !c.isVolume), isTrue,
          reason: '语料全是免费真章 → 下面那格用合成行走，别把"没走到"读成"走过"');
      // 合成行（非黄金证人）：两旗任一都算付费墙，卷标题不是真章。
      final flagged = ChapterRow.fromJson(Map<String, Object?>.from(raw.first)
        ..['isVip'] = true
        ..['isVolume'] = true);
      expect(flagged.paywalled, isTrue);
      expect(flagged.isVolume, isTrue);
      // wordCount 服务端给的是字符串（有的源根本不填）：空串不许显示成"0 字"。
      expect(chapters.first.wordCount, isEmpty);
    });

    test('书签的 offset 是章内比例，不是那把尺；写进去与读回来是同一个数', () {
      final bookmarks = rows('reader.bookmarks').map(BookmarkRow.fromJson).toList();
      final sent = requestPayload('reader.bookmark_add');
      expect(bookmarks.first.offset, greaterThan(0.0));
      expect(bookmarks.first.offset, lessThan(1.0),
          reason: '这一列是 chapter_ratio（0~1）。当成字符偏移用会差整屏');
      expect(sent['offset'], bookmarks.first.offset,
          reason: 'add 送的比例与 bookmarks 读回来的不是同一个数：这一列是界面唯一拿得到的落点');
      expect(sent['summary'], bookmarks.first.summary);
      expect(sent['chapter_index'], bookmarks.first.chapterIndex);
      expect(sent['book_url'], bookmarks.first.bookUrl);

      final position = ReadingPosition.fromJson(data('reader.position_save'));
      expect(position.characterOffset, greaterThan(1),
          reason: '正对照：那把尺上的数与比例是两个量纲，两回事都得有证人');
      // 空 summary 那一支语料里没有（样本全是"G3 样本书签"），合成行走（非黄金证人）。
      final anonymous = BookmarkRow.fromJson(
          Map<String, Object?>.from(rows('reader.bookmarks').first)..['summary'] = '');
      expect(anonymous.label, '第 ${anonymous.chapterIndex + 1} 章');
    });

    test('暂存那一发：回 null 是合法答案，close 的 closed 有真假两支', () {
      for (final name in <String>['reader.staged--shelved', 'reader.staged--after_promote']) {
        final envelope = RpcEnvelope.decode(_envelopeOf(_load(name)), action: name);
        expect(envelope.ok, isTrue, reason: name);
        expect(envelope.dataOrThrow(action: name), isNull,
            reason: '$name：这一发存的就是"没有待询问记录"。把它当形状变了报错，'
                '退出试读那一屏每次都得弹一条错误');
      }
      final outcomes = <String, CloseOutcome>{
        for (final name in <String>[
          'reader.close', 'reader.close--preview', 'reader.close--mismatch',
          'reader.close--before_reopen'
        ])
          name: CloseOutcome.fromJson(data(name)),
      };
      expect(outcomes['reader.close--mismatch']!.closed, isFalse,
          reason: 'url 对不上会话时 closed=false 且什么都不动——界面不许读成"关失败了"');
      expect(outcomes.values.where((o) => o.closed).length, greaterThan(0));
      expect(outcomes.values.every((o) => o.staged == null), isTrue,
          reason: '语料里 close 的 staged 四发全是 null：下面那条"同形状"主张在黄金样本里没有证人，'
              '靠合成行 + `tests/test_reader_rpc_surface.py` 那条活体守卫（记忆 zero-counts…）');
    });

    test('close 的 staged 与 reader.staged 同形状：一条记录两处回，键集不许漂（合成行，非黄金证人）', () {
      final stagedRow = data('reader.staged');
      final direct = StagedRecord.fromJson(stagedRow);
      final outcome = CloseOutcome.fromJson(<String, Object?>{
        'closed': true, 'staged': stagedRow
      });
      expect(outcome.staged, isNotNull,
          reason: '那一格是对象却读成 null = 退出试读不弹询问，那本书静默丢了');
      expect(outcome.staged!.raw, equals(stagedRow),
          reason: '转正/放弃要把整份记录原样传回 bookshelf.add，读的时候丢格就传回一份残缺快照');
      expect(outcome.staged!.name, direct.name);
      expect(outcome.staged!.url, stagedRow['url']);
      expect(outcome.staged!.sourceUrl, stagedRow['source_url']);
      expect(outcome.staged!.displayName, 'G3 试读书');
      expect(outcome.staged!.inBookshelf, isFalse,
          reason: '在架的书那一发根本回 null；这里为 true 是异常形状，界面不许省掉询问');

      // 它和 bookshelf.list 的行**不是同一套键**（staged_record.dart 文件头的主张）。
      final shelf = _fieldNamesOf(_load('bookshelf.list'));
      final stagedFields = _fieldNamesOf(_load('reader.staged'));
      expect(stagedFields, contains('in_bookshelf'));
      expect(shelf, isNot(contains('in_bookshelf')));
      for (final key in <String>['reading', 'updateError', 'cacheUpdatedAt']) {
        expect(shelf, contains(key), reason: '$key：bookshelf 那侧的形状变了，这条比对要重看');
        expect(stagedFields, isNot(contains(key)),
            reason: '$key：暂存记录开始带这一格了——那"进度归一"在暂存这一支就不是没输入，'
                'staged_record.dart 的注释和界面都得重看');
      }
      // 空名兜底那一支：语料那发有名字，合成行走（非黄金证人）。
      expect(StagedRecord.fromJson(Map<String, Object?>.from(stagedRow)..['name'] = '')
          .displayName, '这本书');
    });

    test('书内搜索：命中偏移落在那把尺上，written 与命中数是两个计数器', () {
      final result = ContentSearchResult.fromJson(data('reader.search_content'));
      final doc = ChapterDocument.fromJson(data('reader.content'));
      expect(result.hits, isNotEmpty, reason: '正对照：零命中时下面每条循环都在比 0');
      expect(result.ftsAvailable, isTrue);
      expect(result.indexedChapters, greaterThan(0));
      for (final hit in result.hits) {
        expect(hit.characterOffset, greaterThan(0),
            reason: 'offset 全 0 = 那一列没导出来，跳过去会每次都落到段首');
        expect(hit.snippet, contains('<mark>'),
            reason: '高亮标记在服务端打，客户端在协议面剥掉就没有"命中在哪几个字"了');
      }
      final overlap = result.hits.where((h) => h.chapterIndex == doc.chapterIndex).toList();
      expect(overlap, isNotEmpty, reason: '搜索样本与正文样本不再重叠同一章：下面两条比对就成了自证');
      for (final hit in overlap) {
        expect(hit.characterOffset, lessThanOrEqualTo(doc.totalChars),
            reason: '命中偏移与 chapter_pos 是同一把尺，右端点就是 reader.content 的 totalChars');
        expect(hit.paraIndex, lessThan(doc.paragraphCount));
      }
      expect(result.written, isNot(equals(result.hits.length)),
          reason: '实测 written=32（新写进索引的段数 = 4 章 × 8 段）与 4 条命中不是一回事；'
              '并成一个计数器就分不清"建索引花了多久"与"这本书里有没有这个词"');
      expect(requestPayload('reader.search_content')['keyword'], '第3段',
          reason: '关键词是请求里那一格原文，服务端不猜');
      // 0 命中的两种解释语料里没有（这一发命中 4 条），合成行走（非黄金证人）：
      // 界面必须能分清"这台机器没 FTS5"与"只搜了已缓存的 N 章"。
      final noFts = ContentSearchResult.fromJson(<String, Object?>{
        'hits': <Object?>[], 'written': 0, 'indexedChapters': 0, 'ftsAvailable': false,
      });
      expect(noFts.hits, isEmpty);
      expect(noFts.ftsAvailable, isFalse);
    });
  });

  group('switch.* 那一族解得开：轮次、候选、两份事件样本与写回（G3-D3）', () {
    Map<String, Object?> data(String name) =>
        Map<String, Object?>.from(_envelopeOf(_load(name))['data']! as Map);

    Map<String, Object?> requestPayload(String name) =>
        Map<String, Object?>.from((_load(name)['request']! as Map)['payload']! as Map);

    test('switch.snapshot：九键快照，候选三行里同站两本都在', () {
      final run = SwitchRun.fromJson(data('switch.snapshot'));
      // 本轮身份：服务端 `sid = str(switch_id or 0)` 折成**字符串**回带，样本里就是 `"6379028"`。
      // 模型这一格必须是 String——按数字读会永远读到 0，"只认当前那一轮"那半边守卫整个失效。
      expect(run.switchId, '6379028');
      expect(run.running, isFalse);
      expect(run.cancelled, isFalse);
      expect(run.done, 3);
      expect(run.total, 3);
      expect(run.failures, 0);
      expect(run.starved, 0);
      expect(run.error, isEmpty);
      expect(run.candidates, hasLength(3));
      for (final candidate in run.candidates) {
        expect(candidate.sourceUrl, isNotEmpty,
            reason: 'switch.apply 的 source_url 就是这一格，空的行点下去什么都没换');
        expect(candidate.sourceName, isNotEmpty);
        expect(candidate.book.url, isNotEmpty,
            reason: '空的 book.url 让 identityKey 只剩站点名，同站两本会并成一行');
      }
      // **同站两本**是真形状：a 站有 book/1 与 book/2 两条候选。
      // 行键少了 `book.url` 那一半就会把两本渲染成一本（少一本可选的书）。
      expect(run.candidates.map((c) => c.sourceUrl).toSet().length, lessThan(3),
          reason: '正对照：来源 url 本来就会撞——不撞的话下面那条 identityKey 唯一性就是自证');
      expect(run.candidates.map((c) => c.identityKey).toSet(), hasLength(3));
      // 顺序原样保留（`rank_switch_matches` 排的那一份），客户端不重排。
      expect(run.candidates.map((c) => c.identityKey).toList(),
          <String>[
            'http://127.0.0.1:8797/g3sw/a|http://127.0.0.1:8797/g3sw/a/book/1.html',
            'http://127.0.0.1:8797/g3sw/b|http://127.0.0.1:8797/g3sw/b/book/1.html',
            'http://127.0.0.1:8797/g3sw/a|http://127.0.0.1:8797/g3sw/a/book/2.html',
          ],
          reason: '候选顺序在服务侧（同站优先 + url 字典序）；这里读出来变了序就是 Dart 侧重排过');
    });

    test('switch.scan 的两份事件样本：收尾那五格不带计数，进度那一帧只带计数', () {
      final finished =
          Map<String, Object?>.from((_load('switch.scan')['event_sample']! as Map)['payload']! as Map);
      expect(finished.keys.toSet(),
          <String>{'switch_id', 'candidates', 'cancelled', 'error', 'timestamp'});
      // 这一格是 `SwitchRun.fromFinishedEvent` **不读** done/total/failures/starved 的证人：
      // 载荷里本就没有那四格（计数只在进度帧里发）。哪天服务端把它们加进收尾事件，
      // 这条就红——红的意思是"模型那边可以开始读了，注释要重看"。
      expect(finished.containsKey('done'), isFalse);
      expect(finished.containsKey('total'), isFalse);
      final run = SwitchRun.fromFinishedEvent(finished);
      expect(run.switchId, '6379028', reason: '收尾与快照带的是同一个本轮身份');
      expect(run.candidates, hasLength(3));
      expect(run.running, isFalse, reason: '收尾事件之后没有"还在跑"这一说');
      expect(run.cancelled, isFalse);
      expect(run.error, isEmpty);

      final progressSample = _load('switch.scan')['progress_sample']! as Map;
      final frames = (progressSample['frames']! as List)
          .cast<Map<String, Object?>>();
      expect(frames, isNotEmpty, reason: '正对照：没有进度帧，下面读的全是空表');
      final progress = SwitchProgress.fromMap(frames.first);
      expect(progress.switchId, '6379028');
      expect(progress.done, 3);
      expect(progress.total, 3);
      expect(progress.failures, 0);
      expect(progress.starved, 0);
      // 模型读的五个键名必须是事件载荷键名的子集（`timestamp` 除外：那是丢旧不回用的）。
      final wireFields =
          (progressSample['payload_field_names']! as List).cast<String>();
      for (final key in <String>['switch_id', 'done', 'total', 'failures', 'starved']) {
        expect(wireFields, contains(key),
            reason: '进度帧从来不吐 $key——模型里那一格是死的，界面读到的是默认 0');
      }
    });

    test('switch.seeds：裸书行两条，候选的站点取自行里的 origin', () {
      final rows = (_envelopeOf(_load('switch.seeds'))['data']! as List)
          .cast<Map<String, Object?>>();
      expect(rows, hasLength(2));
      final seeds = rows.map((row) => SwitchCandidate.fromSeed(BookRow.fromJson(row))).toList();
      for (final seed in seeds) {
        expect(seed.sourceUrl, isNotEmpty,
            reason: '种子的 source_url = 那一行的 origin（服务端 `origin_order`/`origins` 都在这套键里）');
        expect(seed.sourceName, isNotEmpty);
      }
      expect(seeds.map((s) => s.identityKey).toSet(), hasLength(2));
      // 种子**不排除当前源**（flet 也只让扫描队列排自己：`order_switch_sources`）。
      // 这一格钉的是"客户端不许在这里替服务端多做一道过滤"——样本里就有一条是当前源。
      final ownOrigin =
          '${(Map<String, Object?>.from(requestPayload('switch.seeds')['book']! as Map))['origin'] ?? ''}';
      expect(ownOrigin, isNotEmpty);
      expect(seeds.map((s) => s.sourceUrl), contains(ownOrigin),
          reason: '种子行里已经没有当前源了——那条排除规则改过地方，界面这侧的比对要重看');
    });

    test('switch.apply：回带的 source_url 就是请求那一格，且三格各有职守', () {
      final result = SwitchApplyResult.fromJson(data('switch.apply'));
      final payload = requestPayload('switch.apply');
      expect(result.applied, isTrue);
      expect(result.sourceUrl, '${payload['source_url']}',
          reason: '回话里的 url 与送出去的那一格对不上 = 换到的不是点的那条源');
      final record = Map<String, Object?>.from(payload['record']! as Map);
      final book = Map<String, Object?>.from(payload['book']! as Map);
      expect(result.sourceUrl, '${book['origin']}');
      // 三格的分工在这一发里看得见：record 还在旧源上，book 已经在新源上。
      expect('${record['origin']}', isNot(result.sourceUrl),
          reason: 'record 是"要换的那一行"、book 是"换成哪一本"，两者同源时这一发没演到换');
      expect('${record['url']}', isNot('${book['url']}'));
      expect('${record['name']}', '${book['name']}', reason: '换源的前提就是同名书');
    });

    test('switch.cancel：false 是真的"没在跑"，那一格不许缺', () {
      final sample = data('switch.cancel');
      expect(sample.keys, <String>{'cancelled'});
      expect(sample['cancelled'], isFalse,
          reason: '抓样那一拍没有轮次在跑（空闲支）。在跑那一支由 pytest 的 SWITCH_CONCURRENCY=1 臂出证据，'
              '界面那两句话的分叉在 source_switch_screen_test.dart 里按替身走');
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
