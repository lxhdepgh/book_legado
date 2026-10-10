/// 落点实弹臂的**离线那一半**：Dart 自己切的段、自己算的偏移，逐格对上
/// **flet 那屏算出来的三张投影表**（`tests/gateway/projection/reader.content*.json`）。
///
/// 为什么这发能算"跨端可比"的证据、而"两侧各跑一遍同一个公式"不能：表里的答案列
/// 由 flet 自己的函数产出（`ReaderView.content_plan` + `app.core.text_layout` 坐标族，
/// 见 `.probe/g3c_flet_projection.py`），Dart 这边是**另一份实现**
/// （`features/reader/domain/text_layout.dart`）。两份实现吃同一份输入（黄金样本的
/// `response.data`，或表里钉着的合成输入），吐出同一串数——这才叫可比。
/// 若两边都读同一个函数的输出，Dart 错了也一起绿（记忆 `measurement-corpus-must-not-be-self-proving`）。
///
/// 三张表各有各的形状，缺一张就有一条臂没人走：
/// - `reader.content`：黄金载荷（2 个正文块 + 1 张受保护插图）——8 段 / 5,802 字 / 屏上 9 行；
/// - `reader.content--media_only`：**整章只有媒体**——尺上 0 段，屏上仍有 1 行；
/// - `reader.content--with_heading`：**章首一行是小标题**——显示认标题（不加缩进），
///   尺上它仍是第 0 段；另带一行"全角空格开头"，考的是 strip 的字符集。
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/features/reader/data/chapter_document.dart';
import 'package:legodo_client/features/reader/domain/text_layout.dart';

final Directory _repoRoot = Directory.current.parent;

Map<String, Object?> _json(String relative) => jsonDecode(
        File('${_repoRoot.path}/$relative').readAsStringSync(encoding: utf8))
    as Map<String, Object?>;

Map<String, Object?> _table(String name) =>
    _json('tests/gateway/projection/$name.json');

/// 表吃的输入：`input_ref` 指回黄金样本的 `response.data`，`input` 是表里钉着的合成载荷。
Map<String, Object?> _inputOf(String name, Map<String, Object?> table) {
  final ref = table['input_ref'];
  if (ref is String && ref.isNotEmpty) {
    final document = _json(ref.split('#').first);
    final response = document['response'];
    final Object? data = response is Map<String, Object?>
        ? response['data']
        : (jsonDecode('$response') as Map<String, Object?>)['data'];
    if (data is! Map<String, Object?>) {
      throw StateError('$name 的 input_ref 指过去不是对象（收到 ${data.runtimeType}）'
          '——先跑 .probe/g3_golden_capture.py');
    }
    return data;
  }
  final inline = table['input'];
  if (inline is! Map<String, Object?>) {
    throw StateError('$name 既没有 input_ref 也没有 input：这张表不能用来比');
  }
  return inline;
}

int _int(Object? value) => (value! as num).toInt();

List<Map<String, Object?>> _rowsOf(Map<String, Object?> table, String column) =>
    (table[column]! as List).cast<Map<String, Object?>>();

/// 表里每段只存头 16 字（够指认是哪一段，不必把正文抄第三遍）。
String _head(String text) => text.length <= 16 ? text : text.substring(0, 16);

const List<String> _tables = <String>[
  'reader.content',
  'reader.content--media_only',
  'reader.content--with_heading',
];

void main() {
  group('Dart 那把尺与 flet 的三张投影表逐格同数', () {
    for (final name in _tables) {
      group(name, () {
        late Map<String, Object?> table;
        late ChapterDocument document;
        late List<String> paragraphs;
        late List<ReaderLine> lines;

        setUp(() {
          table = _table(name);
          document = ChapterDocument.fromJson(_inputOf(name, table));
          final preferences = table['preferences']! as Map<String, Object?>;
          paragraphs = chapterParagraphs(document.blocks);
          lines = contentPlan(document, indent: preferences['indent'] == true);
        });

        test('段数与总字数：Dart 算的 == 表里的 == 服务端那一发回话里的', () {
          expect(paragraphs.length, _int(table['paragraph_count']),
              reason: '$name：Dart 数出 ${paragraphs.length} 段，'
                  'flet 那屏是 ${table['paragraph_count']} 段——切段口径分裂了');
          expect(chapterTextLength(paragraphs), _int(table['total_chars']),
              reason: '$name：字符流总长不一样，段首偏移必然整体漂');
          // 黄金那一发的载荷本来就来自协议面，回话里带着服务端自己算的两个数——
          // 第三份读数也在，而且是同一发产出的，不是这里造的。
          if (table['source_sample'] == 'reader.content') {
            expect(document.paragraphCount, _int(table['paragraph_count']),
                reason: '服务端回话的段数与两屏算的不是一个数');
            expect(document.totalChars, _int(table['total_chars']),
                reason: '服务端回话的总字数与两屏算的不是一个数');
          }
        });

        test('段首偏移整列不差：包括段间那一格', () {
          expect(paragraphOffsets(paragraphs),
              _rowsOffsetColumn(table),
              reason: '$name：漏算段间那一格会让第 1 段起每一段都偏 1，'
                  '而头两段看起来还是"对的"——所以比的是整列，不是头两格');
        });

        test('每一段的序号/段首/长度/头 16 字都对得上', () {
          final expected = _rowsOf(table, 'paragraphs');
          expect(expected.length, paragraphs.length, reason: '$name：段数与段表不同数');
          final offsets = paragraphOffsets(paragraphs);
          for (var i = 0; i < expected.length; i++) {
            final row = expected[i];
            expect(_int(row['index']), i);
            expect(paragraphs[i].length, _int(row['length']),
                reason: '第 $i 段长度不一样：Dart「${_head(paragraphs[i])}」'
                    ' vs 表「${row['head']}」');
            expect(offsets[i], _int(row['offset']), reason: '第 $i 段的段首');
            expect(_head(paragraphs[i]), row['head'], reason: '第 $i 段的前 16 字');
          }
        });

        test('屏上的行：行号不等于段号，媒体行占一行但不产段', () {
          final expected = _rowsOf(table, 'rows');
          expect(lines.length, expected.length,
              reason: '$name：屏上行数 ${lines.length}，flet 那屏 ${expected.length} 行——'
                  'Dart 把某个块整条丢了或多挂了一行');
          for (var i = 0; i < expected.length; i++) {
            final row = expected[i];
            final line = lines[i];
            expect(_int(row['row']), i, reason: '第 $i 行的行号列自己就不连续');
            expect(line.kind, row['kind'], reason: '第 $i 行的类型');
            expect(line.paragraphIndex,
                row['paragraph_index'] == null ? null : _int(row['paragraph_index']),
                reason: '第 $i 行的段号：把行号当段号就是在这一格对不上');
            expect(line.heading, row['heading'], reason: '第 $i 行是不是标题');
            expect(line.display, row['display'],
                reason: '第 $i 行屏上那串字（缩进与标题处理是显示的事，'
                    '但它也得与 flet 一样，否则两侧显示的正文不是同一串）');
            expect(line.ruler, row['ruler'],
                reason: '第 $i 行尺上那串字：display 与 ruler 混用就是在这一格露出来');
            expect(line.meta, row['meta'], reason: '第 $i 行的图源样式');
          }
        });

        test('位置往返：段号算偏移、偏移读回段号（互逆）', () {
          final expected = _rowsOf(table, 'position_round_trip');
          expect(expected.length, paragraphs.length);
          for (final row in expected) {
            final index = _int(row['paragraph_index']);
            final offset = offsetOfParagraph(paragraphs, index);
            expect(offset, _int(row['offset']),
                reason: '第 $index 段算出的偏移与表里的不是一个数');
            final back = paragraphAtOffset(paragraphs, offset);
            final want = (row['back_to']! as List).map(_int).toList();
            expect(back.paragraphIndex, want[0],
                reason: '第 $index 段的偏移读回来不是同一段：存进去与取出来不是同一个落点');
            expect(back.localOffset, want[1]);
          }
        });

        test('落点探针：段首/段尾/段中/章尾之后/负数/越界，一格不差', () {
          final expected = _rowsOf(table, 'offset_probes');
          // 正对照：探针数为 0 时上面那条循环是空转，"全对"是假的。
          expect(expected, isNotEmpty, reason: '$name 的探针列是空的，这一格没跑到');
          for (final probe in expected) {
            final got = paragraphAtOffset(paragraphs, _int(probe['offset']));
            expect(got.paragraphIndex, _int(probe['paragraph_index']),
                reason: '偏移 ${probe['offset']}（${probe['expect']}）落到第 '
                    '${got.paragraphIndex} 段，表里是第 ${probe['paragraph_index']} 段');
            expect(got.localOffset, _int(probe['local']),
                reason: '偏移 ${probe['offset']}（${probe['expect']}）的段内那一格是 '
                    '${got.localOffset}，表里是 ${probe['local']}');
          }
        });
      });
    }
  });

  group('这把尺不是那三把错尺', () {
    final table = _table('reader.content');
    final document = ChapterDocument.fromJson(_inputOf('reader.content', table));

    test('按块数段、按显示形态数、把裸块拼起来数——三种错法都算不出同一个总长', () {
      final paragraphs = chapterParagraphs(document.blocks);
      // 错尺一：块数当段数。这一份载荷的块数与段数得是**不同的数**，否则这种错法没证人。
      expect(document.blocks.length, isNot(paragraphs.length),
          reason: '正对照：实测应当是 3 块 / 8 段');
      // 错尺二：拿屏上那串（带缩进）去量。
      final displayChars = contentPlan(document, indent: true)
          .where((line) => line.isText)
          .fold<int>(0, (sum, line) => sum + line.display.length);
      expect(displayChars, isNot(_int(table['total_chars'])),
          reason: 'display 每段多两个全角空格，拿它算偏移会整体漂——'
              '这一条就是表里为什么分 ruler 与 display 两列的证人');
      // 错尺三：把裸 value 直接拼起来数。
      final rawChars = document.blocks
          .where((b) => b.isText)
          .fold<int>(0, (sum, b) => sum + (b.value?.length ?? 0));
      expect(rawChars, isNot(_int(table['total_chars'])),
          reason: '块原文里有换行、缩进与段间空白，切段之后已经不是那一串字');
    });

    test('喂一整串正文给坐标函数会响，不会安静地逐字切', () {
      // list("第一段第二段") 会给出全 1 的段首：两侧各错各的还能互相绿。
      expect(() => requireParagraphList('第一段第二段'),
          throwsA(isA<ArgumentError>()));
      expect(() => requireParagraphList(<Object>['第一段', 42]),
          throwsA(isA<ArgumentError>()));
    });

    test('空章：段数 0、总长 0、任何偏移都落回 (0, 0)', () {
      const List<String> empty = <String>[];
      expect(chapterTextLength(empty), 0, reason: '空章不许造"至少一个字"的假下限');
      expect(paragraphOffsets(empty), isEmpty);
      expect(offsetOfParagraph(empty, 3), 0);
      expect(paragraphAtOffset(empty, 99), const ParagraphLocation(0, 0));
    });

    test('媒体块不产段，但没有可渲染地址的块整条丢掉', () {
      final onlyImage = ChapterDocument.fromJson(<String, Object?>{
        'chapterUrl': 'https://s/c', 'chapterIndex': 0, 'chapterName': '第一章',
        'title': '', 'imageStyle': '', 'paragraphCount': 1, 'totalChars': 4,
        'blocks': <Object?>[
          <String, Object?>{'kind': 'text', 'value': '正文字', 'url': '',
              'binary': false, 'title': '', 'headers': <String, Object?>{}},
          // 受保护图：字节不过协议面（§4.4），value 是 null，只剩 url。
          <String, Object?>{'kind': 'image', 'value': null,
              'url': 'http://127.0.0.1/pic.jpg', 'binary': true, 'title': '插图',
              'headers': <String, Object?>{}},
          // 既没值又没地址：客户端什么都画不出来，整条不挂。
          <String, Object?>{'kind': 'image', 'value': '', 'url': '',
              'binary': false, 'title': '', 'headers': <String, Object?>{}},
        ],
      });
      final lines = contentPlan(onlyImage);
      expect(chapterParagraphs(onlyImage.blocks), <String>['正文字']);
      expect(lines.map((line) => line.kind).toList(), <String>['text', 'image'],
          reason: '有地址的二进制块必须留下一行（地址是客户端唯一把手），没地址的才丢');
      expect(lines[1].block?.url, 'http://127.0.0.1/pic.jpg');
      expect(lines[1].paragraphIndex, isNull, reason: '媒体行不许占段号');
    });

    test('标题行：显示不加缩进，尺上它仍是第 0 段', () {
      final headingFirst = ChapterDocument.fromJson(<String, Object?>{
        'chapterUrl': '', 'chapterIndex': 0, 'chapterName': '', 'title': '',
        'imageStyle': '', 'paragraphCount': 2, 'totalChars': 0,
        'blocks': <Object?>[
          <String, Object?>{'kind': 'text', 'value': '第一节 风起\n正文第一段。',
              'url': '', 'binary': false, 'title': '', 'headers': <String, Object?>{}},
        ],
      });
      final lines = contentPlan(headingFirst, indent: true);
      expect(lines[0].heading, isTrue);
      expect(lines[0].display, '第一节 风起', reason: '标题行叠上"　　"就不再是标题的形状');
      expect(lines[1].heading, isFalse);
      expect(lines[1].display, '　　正文第一段。');
      expect(paragraphOffsets(chapterParagraphs(headingFirst.blocks)), <int>[0, 7],
          reason: '尺上认段不认标题：标题行照样占第 0 段，第二段从第 7 个字起');
    });
  });
}

/// 表里的 `offsets` 那一列（num → int）。
List<int> _rowsOffsetColumn(Map<String, Object?> table) =>
    (table['offsets']! as List).map(_int).toList();
