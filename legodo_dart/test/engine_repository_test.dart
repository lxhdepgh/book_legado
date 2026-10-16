/// `FFIEngineAdapter` 那一层：契约面怎么落到 `GoFFIClient.call(action, params)` 上。
///
/// 替身是 `_RecordingFFI implements GoFFIClient`：记下每一发的 action + params，
/// 回一只 `{'ok':…,'data':…}` 信封。WS 时代那份 `RpcEngineRepository` + MockClient
/// 的严格形状规矩（回 null → transport 响）随 WS 链退役：FFI 里 Go 的 nil 切片
/// 编出来就是 `null`，适配器各解析点按自己的规矩收（宽容处与仍抛处各自有注释）。
///
/// 这里钉的是**Dart 发出的键面与它读回来的形状**；Go 那半边按同一批键断言
/// （`backend_go/pkg/ffi/bookmark_handlers_test.go` 是同一套方法的 Go 侧）。
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/ffi/engine_ffi.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';
import 'package:legodo_client/features/settings/data/theme_preferences.dart';

/// 记录每一发的 action + params；回话由 [handler] 按发给，不给就回 `data: null`。
class _RecordingFFI implements GoFFIClient {
  final List<({String action, Map<String, dynamic> params})> calls = [];
  Map<String, dynamic> Function(String action, Map<String, dynamic> params)? handler;

  @override
  bool get loaded => true;

  @override
  String get libraryPath => r'Z:\fake\legodo_engine.dll';

  @override
  void load() {}

  @override
  void initialize() {}

  @override
  void cleanup() {}

  @override
  Map<String, dynamic> call(String action, Map<String, dynamic> params) {
    calls.add((action: action, params: params));
    final handler = this.handler;
    if (handler != null) return handler(action, params);
    return <String, dynamic>{'ok': true, 'data': null};
  }
}

FFIEngineAdapter _repo(_RecordingFFI ffi) => FFIEngineAdapter(client: ffi);

/// 只继承、一个方法都不覆写：拿基类的 throw 默认体，测的就是"没接上"那一手。
class _ScaffoldRepository extends EngineRepository {
  const _ScaffoldRepository();
}

void main() {
  test('抽象仓库的默认体是抛，不是空实现：新 action 没接上必须有声音', () async {
    const bare = _ScaffoldRepository();
    final arms = <String, Future<Object?> Function()>{
      'engine.hello': bare.hello,
      'bookshelf.list': bare.listBookshelf,
      'engine.shutdown': bare.shutdown,
    };
    expect(arms.length, 3, reason: '漏一条 = 那一格的 throw 默认体没人测过');
    for (final entry in arms.entries) {
      await expectLater(
          entry.value(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.unavailable)
              .having((EngineFailure e) => e.message, 'message',
                  contains('当前环境不支持 ${entry.key}'))),
          reason: entry.key);
    }
  });

  test('bookshelf_list：行落模型、raw 留原行，include_staged 落在 payload 上', () async {
    final ffi = _RecordingFFI()
      ..handler = (action, params) => <String, dynamic>{
            'ok': true,
            'data': <Object?>[
              <String, Object?>{
                'name': '仙子，助我修行',
                'author': '逆时针的圈',
                'url': 'https://e/1',
                'source_url': 'https://e',
                'durChapterTitle': '第一章 金刚萝莉师姐',
              },
            ],
          };
    final repository = _repo(ffi);
    final items = await repository.listBookshelf();
    expect(items.length, 1);
    expect(items.first.name, '仙子，助我修行');
    expect(items.first.subtitle, '逆时针的圈 · 读到 第一章 金刚萝莉师姐');
    // raw 保留原行：后面的门（换源/进度/缓存）不再逐个复制字段。
    expect(items.first.raw['durChapterTitle'], '第一章 金刚萝莉师姐');
    expect(ffi.calls.single.action, 'bookshelf_list');
    expect(ffi.calls.single.params, {'include_staged': false},
        reason: '缺省 false 也**显式**带：Go 那侧 gb 把缺键读成 false，但键面自己要说清楚');

    await repository.listBookshelf(includeStaged: true);
    expect(ffi.calls.last.params, {'include_staged': true});
  });

  test('hello 在 FFI 面不发线：网关握手那一关已经不在链上了', () async {
    final ffi = _RecordingFFI();
    final info = await _repo(ffi).hello();
    expect(info.build, 'go-ffi');
    expect(ffi.calls, isEmpty, reason: 'hello 回常量：FFI 没有网关握手，别偷偷挂一发调用');
  });

  test('ok:false 折成 EngineFailure(engine)：拒答原句不许伪装成超时', () async {
    final ffi = _RecordingFFI()
      ..handler = (action, params) => <String, dynamic>{
            'ok': false,
            'error': 'ReaderSessionError: 当前会话不是这本书',
          };
    await expectLater(
        _repo(ffi).chapterRows(sourceUrl: 'https://s', bookUrl: 'https://s/别的'),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
            .having((EngineFailure e) => e.isTransient, 'isTransient', isFalse)
            .having((EngineFailure e) => e.message, 'message',
                contains('ReaderSessionError'))
            .having((EngineFailure e) => e.action, 'action', 'reader_toc')));
  });

  test('shutdown 带原因', () async {
    final ffi = _RecordingFFI();
    await _repo(ffi).shutdown(reason: 'window closed');
    expect(ffi.calls.single.action, 'engine_shutdown');
    expect(ffi.calls.single.params, {'reason': 'window closed'});
  });

  // ------------------------------------------------------------------ #
  // reader.*：键面与形状（`null` 合法 / 形状变了要响 / 回话才算数）
  // ------------------------------------------------------------------ #
  group('reader.* 仓库面', () {
    test('键面：三把 url 与五格开关都落在 payload 上（拼错键名 = 服务端按缺省走）', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => switch (action) {
              'reader_content' => <String, dynamic>{
                  'ok': true,
                  'data': <String, Object?>{
                    'chapterUrl': 'https://s/c', 'chapterIndex': 0,
                    'chapterName': '', 'title': '', 'imageStyle': '',
                    'blocks': <Object?>[], 'paragraphCount': 0, 'totalChars': 0,
                  },
                },
              'reader_close' => <String, dynamic>{
                  'ok': true,
                  'data': <String, Object?>{'closed': true, 'staged': null},
                },
              'reader_open' => <String, dynamic>{
                  'ok': true,
                  'data': <String, Object?>{
                    'book': <String, Object?>{'url': 'https://s/b'},
                    'sourceUrl': 'https://s', 'bookUrl': 'https://s/b',
                    'chapterCount': 0, 'position': <String, Object?>{},
                  },
                },
              'reader_position_save' => <String, dynamic>{
                  'ok': true, 'data': <String, Object?>{},
                },
              'reader_search_content' => <String, dynamic>{
                  'ok': true,
                  'data': <String, Object?>{
                    'hits': <Object?>[], 'written': 0, 'indexedChapters': 0,
                    'ftsAvailable': false,
                  },
                },
              _ => <String, dynamic>{'ok': true, 'data': <Object?>[]},
            };
      final repository = _repo(ffi);

      await repository.chapterDocument(
          sourceUrl: 'https://s', bookUrl: 'https://s/b', chapterUrl: 'https://s/c');
      expect(ffi.calls.last.action, 'reader_content');
      expect(ffi.calls.last.params, {
        'source_url': 'https://s', 'book_url': 'https://s/b',
        'chapter_url': 'https://s/c',
      });

      await repository.chapterRows(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(ffi.calls.last.params, {
        'source_url': 'https://s', 'book_url': 'https://s/b', 'refresh': false,
      }, reason: 'refresh 恒带：省键与传 false 同值，但键面只有一种形状');

      await repository.closeReader(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(ffi.calls.last.params, {
        'source_url': 'https://s', 'book_url': 'https://s/b', 'save_progress': true,
      });

      // bookmark_add 的 offset/summary **不许省略**：服务端签名没有默认值。
      await repository.addBookmark(bookUrl: 'https://s/b', chapterIndex: 2);
      expect(ffi.calls.last.params, {
        'book_url': 'https://s/b', 'chapter_index': 2, 'offset': 0.0,
        'summary': '',
      });

      // 开书那一发把**字典快照**原样放进 payload：模型对象不许过协议面。
      await repository.openReader(
          sourceUrl: 'https://s', book: const <String, Object?>{'url': 'https://s/b'});
      expect(ffi.calls.last.params, {
        'source_url': 'https://s', 'book': {'url': 'https://s/b'},
        'shelve': true, 'resume': true,
      }, reason: 'shelve/resume 恒带：试读不入架靠的就是显式传 false');

      await repository.savePosition(sourceUrl: 'https://s', bookUrl: 'https://s/b',
          positionJson: const <String, Object?>{'chapter_index': 1});
      expect(ffi.calls.last.params, {
        'source_url': 'https://s', 'book_url': 'https://s/b',
        'position_json': {'chapter_index': 1},
      });

      await repository.searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(ffi.calls.last.params,
          {'book_url': 'https://s/b', 'keyword': '风起', 'source_url': null},
          reason: '不给 source_url 时这一格送 null（Go 那侧 gs 读成空串）。键名漂了'
              '服务端只当没这一格：搜索会拿空关键词回"这本书里没找到"');
    });

    test('position_get 与 staged 的 null 是合法回话；"第 0 章第 0 字"是另一个形状', () async {
      // Go 在这两发上确实可能回 null（"这本书从没读过"/"这本不是试读，别问"）。
      final nil = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      final repository = _repo(nil);
      expect(await repository.readPosition(sourceUrl: 'https://s', bookUrl: 'https://s/b'),
          isNull);
      expect(await repository.stagedRecord(), isNull);

      // 但"位置在第 0 章第 0 字"必须落回一个**模型**：那是"刚开过这本"，不是"没读过"。
      final zero = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'chapter_index': 0, 'chapter_pos': 0, 'scroll_offset': 0.0,
                'scroll_ratio': 0.0, 'font_size': 16, 'last_read_at': 0,
                'chapter_url': '', 'chapter_name': '', 'chapter_paragraph': null,
              },
            };
      final position = await _repo(zero)
          .readPosition(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(position, isNotNull, reason: 'null 与零位置读成了同一个形状');
      expect(position!.characterOffset, 0);
      expect(position.paragraphIndex, isNull,
          reason: '段号 null 被读成 0 = 一本读到中间的书显示成"刚开始读"');
    });

    test('bookmark 三发：snake_case 行落 BookmarkRow、add 回裸 id、remove 回裸 bool', () async {
      // 键面（book_url / chapter_index / offset / summary / bookmark_id）与 Go 那侧
      // 的读取键是同一批（backend_go/pkg/ffi/bookmark_handlers_test.go 对向钉着）。
      final ffi = _RecordingFFI()
        ..handler = (action, params) => switch (action) {
              'reader_bookmarks' => <String, dynamic>{
                  'ok': true,
                  'data': <Object?>[
                    <String, Object?>{
                      'bookmark_id': 7, 'book_url': 'https://s/b',
                      'chapter_index': 3, 'offset': 0.25, 'summary': '第4章',
                      'created_at': 1791520941.5,
                    },
                  ],
                },
              'reader_bookmark_add' => <String, dynamic>{'ok': true, 'data': 8},
              'reader_bookmark_remove' => <String, dynamic>{'ok': true, 'data': false},
              _ => <String, dynamic>{'ok': true, 'data': null},
            };
      final repository = _repo(ffi);

      final rows = await repository.listBookmarks('https://s/b');
      expect(ffi.calls.last.action, 'reader_bookmarks');
      expect(ffi.calls.last.params, {'book_url': 'https://s/b'});
      expect(rows.single.bookmarkId, 7);
      expect(rows.single.bookUrl, 'https://s/b');
      expect(rows.single.chapterIndex, 3);
      expect(rows.single.offset, 0.25);
      expect(rows.single.label, '第4章');
      expect(rows.single.createdAt, 1791520941.5,
          reason: 'created_at 是 epoch 秒（Go 那侧写 time.time() 同一口径）；'
              '毫秒口径会把 1.7e9 读成 1970 年');

      expect(
          await repository.addBookmark(
              bookUrl: 'https://s/b', chapterIndex: 3, offset: 0.25, summary: '第4章'),
          8);
      expect(ffi.calls.last.params, {
        'book_url': 'https://s/b', 'chapter_index': 3, 'offset': 0.25,
        'summary': '第4章',
      });

      expect(
          await repository.removeBookmark(bookUrl: 'https://s/b', bookmarkId: 7),
          isFalse,
          reason: '删那一发回的是**是否真删到**，可能是"已被另一侧删过"');
      expect(ffi.calls.last.params, {'book_url': 'https://s/b', 'bookmark_id': 7});
    });

    test('position_save 回话才算数：界面拿夹回后的那一份，不拿请求那一份', () async {
      // 服务端把越界的偏移夹回章内字符流末端。客户端如果自己留着请求那份，
      // 界面上"存好了"而库里是另一个数——下一次续读落在别处。
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'chapter_index': 1, 'chapter_pos': 5802, 'chapter_paragraph': 7,
                'scroll_offset': 321.0, 'scroll_ratio': 1.0, 'font_size': 18,
                'last_read_at': 1791520941, 'chapter_url': 'https://s/c2',
                'chapter_name': '第2章',
              },
            };
      final saved = await _repo(ffi).savePosition(
          sourceUrl: 'https://s',
          bookUrl: 'https://s/b',
          positionJson: const <String, Object?>{
            'chapter_index': 1,
            'chapter_pos': 999999,
            'chapter_paragraph': 4242,
          });
      expect(saved.characterOffset, 5802, reason: '回话里那份才是落库的');
      expect(saved.paragraphIndex, 7);
      expect(saved.characterOffset, isNot(999999));
    });

    test('search_content 的 0 命中带着"为什么是 0"那两个读数', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'hits': <Object?>[], 'written': 0, 'indexedChapters': 3,
                'ftsAvailable': true,
              },
            };
      final result = await _repo(ffi)
          .searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(result.hits, isEmpty);
      expect(result.indexedChapters, 3,
          reason: '只索引了 3 章：界面得说"只搜了已缓存的章"，不能说"书里没有"');

      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true,
            'data': <String, Object?>{
              'hits': <Object?>[], 'written': 0, 'indexedChapters': 0,
              'ftsAvailable': false,
            },
          };
      final noFts = await _repo(ffi)
          .searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(noFts.ftsAvailable, isFalse, reason: '这台机器根本搜不了，是第三种 0');
    });

    test('search_content 形状变了要响：非 Map 是"解不开"，不是"没搜到"', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      await expectLater(
          _repo(ffi).searchBookContent(bookUrl: 'https://s/b', keyword: '风起'),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
              .having((EngineFailure e) => e.action, 'action',
                  'reader.search_content')));
    });
  });

  // ------------------------------------------------------------------ #
  // switch.*：换源那一族的形状规矩（键面全在 payload 上）
  // ------------------------------------------------------------------ #
  group('switch.* 仓库面', () {
    test('switch_seeds 回的是裸行数组；空/null 都读成空表', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <Object?>[
                <String, Object?>{
                  'name': '换源目标书', 'author': '作者戊', 'url': 'https://s/a/1',
                  'origin': 'https://s/a', 'origin_name': 'G3换源a',
                },
              ],
            };
      final seeds = await _repo(ffi)
          .switchSeeds(book: const <String, Object?>{'name': '换源目标书'});
      expect(ffi.calls.last.action, 'switch_seeds');
      expect(ffi.calls.last.params, {'book': {'name': '换源目标书'}});
      expect(seeds, hasLength(1));
      // 种子的"来源"就藏在行里的 `origin`：`SwitchCandidate.fromSeed` 认这一格，
      // 空 origin 的行点下去 `switch_apply` 会报出一条取不到的源。
      expect(seeds.first.origin, 'https://s/a');
      expect(seeds.first.originName, 'G3换源a');
      expect(seeds.first.identityKey, 'https://s/a|https://s/a/1');

      ffi.handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      expect(await _repo(ffi).switchSeeds(book: const <String, Object?>{}), isEmpty,
          reason: 'Go 的 nil 切片编成 null；这一发宽收，读成空表');
    });

    test('switch_scan 是 job：四格都落 payload，回话里一根候选都没有', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{'started': true, 'action': 'switch.scan'},
            };
      final repository = _repo(ffi);
      final accepted = await repository.switchScan(
          book: const <String, Object?>{'name': '换源目标书', 'origin': 'https://s/x'},
          switchId: 6379028,
          keyword: '换源目标书',
          group: 'G3换源');
      expect(accepted.started, isTrue);
      expect(accepted.action, 'switch.scan');
      expect(ffi.calls.last.action, 'switch_scan');
      // 四格恒带：`switch_id` 少了服务端按 0 办；`group` 少了就从不联网变成扫全库——
      // 两者都不报错，只是把这一轮变成另一件事。
      expect(ffi.calls.last.params, <String, Object?>{
        'book': {'name': '换源目标书', 'origin': 'https://s/x'},
        'switch_id': 6379028,
        'keyword': '换源目标书',
        'group': 'G3换源',
      });
      // 默认值也必须是**显式**的两格。
      await repository.switchScan(book: const <String, Object?>{}, switchId: 1);
      expect(ffi.calls.last.params, <String, Object?>{
        'book': <String, Object?>{}, 'switch_id': 1, 'keyword': '', 'group': '',
      });
    });

    test('switch_snapshot 的 switch_id 服务端可能回数字，读出来必须是同一个字符串', () async {
      // 契约上把它折成 str。但 JSON 里数字与字符串在信封层是同一条通道——万一哪回
      // 它是数字，按 int 读会得到 6379028，而 notifier 拿 `'$_switchId'` 比的是文本；
      // 两种读法必须收敛到同一个数串，否则"只认本轮"那半边守卫会静默失效。
      for (final wire in <Object?>['6379028', 6379028]) {
        final ffi = _RecordingFFI()
          ..handler = (action, params) => <String, dynamic>{
                'ok': true,
                'data': <String, Object?>{
                  'switch_id': wire,
                  'running': true, 'cancelled': false, 'done': 2, 'total': 3,
                  'failures': 1, 'starved': 0, 'candidates': <Object?>[],
                  'error': '',
                },
              };
        final run = await _repo(ffi).switchSnapshot();
        expect(run.switchId, '6379028', reason: 'wire 形态 $wire');
        expect(run.running, isTrue);
        expect(run.done, 2);
        expect(run.total, 3);
        expect(run.failures, 1, reason: 'failures 读成 0 = 界面把失败源说成"都在跑"');
        expect(run.candidates, isEmpty);
      }
    });

    test('switch_cancel 的两个方向各读各的：裸布尔与对象里那一格', () async {
      final ffi = _RecordingFFI();
      final repository = _repo(ffi);

      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true, 'data': <String, Object?>{'cancelled': false},
          };
      expect(await repository.switchCancel(), isFalse);
      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true, 'data': <String, Object?>{'cancelled': true},
          };
      expect(await repository.switchCancel(), isTrue);
      // 正对照：这一发在 FFI 面上也接受裸布尔（Go 的 handler 回的就是裸布尔）。
      ffi.handler = (action, params) => <String, dynamic>{'ok': true, 'data': true};
      expect(await repository.switchCancel(), isTrue);
      expect(ffi.calls.last.action, 'switch_cancel');
      expect(ffi.calls.last.params, isEmpty);
    });

    test('switch_apply 的三格各管一件事', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'applied': true, 'source_url': 'https://s/b',
              },
            };
      final result = await _repo(ffi).applySwitch(
          record: const <String, Object?>{'url': 'https://s/x/1', 'origin': 'https://s/x'},
          sourceUrl: 'https://s/b',
          book: const <String, Object?>{'url': 'https://s/b/1', 'origin': 'https://s/b'});
      expect(result.applied, isTrue);
      expect(result.sourceUrl, 'https://s/b');
      expect(ffi.calls.last.action, 'switch_apply');
      expect(ffi.calls.last.params, <String, Object?>{
        'record': {'url': 'https://s/x/1', 'origin': 'https://s/x'},
        'source_url': 'https://s/b',
        'book': {'url': 'https://s/b/1', 'origin': 'https://s/b'},
      }, reason: 'record 找回书架那一行、source_url 找回那条源、book 是换成哪一本——'
          '少一格服务端就按缺省办，而三种缺法都回同一句"没换成"');

      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true,
            'data': <String, Object?>{'source_url': 'https://s/b'},
          };
      final missing = await _repo(ffi).applySwitch(
          record: const <String, Object?>{},
          sourceUrl: 'https://s/b',
          book: const <String, Object?>{});
      expect(missing.applied, isFalse,
          reason: 'FFI 面 applied 缺键折成 false（Go 那侧恒回这一键，缺了就是形状漂了）');
    });
  });

  // 这三族原本是 FFI 桩：theme.save 回 true、update.check 回一句写死的"没有更新"、
  // qr.generate 回显入参。服务侧现在接真了，这里钉的是**适配器读回来的那几格键名**——
  // 上一轮就是栽在"服务端回 snake_case、适配器读 camelCase"上：编译与 analyze 全绿。
  group('theme.* / update.* / qr.* 仓库面', () {
    test('theme_preferences 读的是服务端那三格（skin_id 不是 skinId）', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'skin_id': 'warm_amber', 'mode': 'light', 'eink': true,
              },
            };
      final prefs = await _repo(ffi).themePreferences();
      expect(prefs.skinId, 'warm_amber',
          reason: "适配器原先读 'skinId'：服务端从不回这个键，于是存好的皮肤每次重开都折回默认档");
      expect(prefs.mode, ThemeModeKind.light);
      expect(prefs.eink, isTrue);
      expect(ffi.calls.single.action, 'theme_preferences');
    });

    test('theme_save：没动的格发 null，回的是归一后的那一套三格', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'skin_id': 'ink_gray', 'mode': 'dark', 'eink': false,
              },
            };
      final saved = await _repo(ffi).saveTheme(skinId: 'ink_gray');
      expect(ffi.calls.single.params, <String, Object?>{
        'skin_id': 'ink_gray', 'mode': null, 'eink': null,
      }, reason: '缺省发 null（服务端"这一格保持原值"），发空串会被折回默认档');
      expect(saved, const ThemePreferences(
          skinId: 'ink_gray', mode: ThemeModeKind.dark, eink: false),
          reason: '界面把这一份当"已保存态"；回 true 会解不开而退回默认档');
    });

    test('theme 读不回时是声音，不是静默的默认档', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) =>
            <String, dynamic>{'ok': false, 'error': 'ThemeService 未就绪：外观偏好读不回来'};
      await expectLater(
          _repo(ffi).themePreferences(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.message, 'message', contains('ThemeService'))));
    });

    test('update_check：null 才是"已是最新"，对象落那五格', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      expect(await _repo(ffi).updateCheck(), isNull,
          reason: '服务端只在真的比过且不相上下时才回 null；失败回的是错，不是 null');

      ffi.handler = (action, params) => <String, dynamic>{
            'ok': true,
            'data': <String, Object?>{
              'version': '2.3.0', 'notes': '说明', 'url': 'https://d/a.exe',
              'size': 456, 'digest': 'ab',
            },
          };
      final info = await _repo(ffi).updateCheck();
      expect(info!.version, '2.3.0');
      expect(info.notes, '说明');
      expect(info.url, 'https://d/a.exe');
      expect(info.size, 456);
      expect(info.digest, 'ab');
      expect(ffi.calls.first.action, 'update_check');
    });

    test('update_ignore 把 version 发出去，失败不静默', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{'ok': true, 'data': true};
      await _repo(ffi).updateIgnore('2.3.0');
      expect(ffi.calls.single.action, 'update_ignore');
      expect(ffi.calls.single.params, <String, Object?>{'version': '2.3.0'});

      ffi.handler = (action, params) =>
          <String, dynamic>{'ok': false, 'error': 'SettingsService 未就绪：忽略版本没处落库'};
      await expectLater(_repo(ffi).updateIgnore('2.3.0'), throwsA(isA<EngineFailure>()));
    });

    test('qr_generate 读 result 那一格；qr_decode 的"未实现"是声音不是空串', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'result': 'iVBORw0KGgo=', 'content_type': 'image/png',
              },
            };
      expect(await _repo(ffi).qrGenerate('书源文本'), 'iVBORw0KGgo=');
      expect(ffi.calls.single.params, <String, Object?>{'text': '书源文本'});

      ffi.handler = (action, params) => <String, dynamic>{
            'ok': false, 'error': '二维码识别未实现：Go 引擎没有图像解码器',
          };
      await expectLater(
          _repo(ffi).qrDecode('iVBORw0KGgo='),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.message, 'message', contains('未实现'))),
          reason: '回空串在界面上长成"这张图里没有二维码"，那是另一件事');
    });
  });

  // 导入那一链的 Dart 侧。Go 侧同一批键面钉在 `pkg/ffi/import_test.go`，
  // 载荷形状钉在 `tests/gateway/golden/import.text.json`（五键，Python 时代真发过的回话）。
  group('import.* 仓库面', () {
    test('import_text 发 text/kind/mode 三键，读的是回话里那五格', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'kind': 'book_source', 'count': 3,
                'errors': <Object?>['第 2 项缺 bookSourceUrl'],
                'warnings': <Object?>[], 'placeholder': false,
              },
            };
      final outcome =
          await _repo(ffi).importText(text: '[]', kind: 'book_source', mode: 'replace');
      expect(ffi.calls.single.action, 'import_text');
      expect(ffi.calls.single.params, <String, Object?>{
        'text': '[]', 'kind': 'book_source', 'mode': 'replace',
      });
      expect(outcome.kind, 'book_source');
      expect(outcome.count, 3);
      expect(outcome.errors, <String>['第 2 项缺 bookSourceUrl']);
      expect(outcome.warnings, isEmpty);
      expect(outcome.placeholder, isFalse);
      expect(outcome.ok, isFalse, reason: '有 errors 就不算成，界面那句汇总不许长成一片绿');
    });

    test('placeholder 读回来还是 placeholder，不折成"成功 0 条"', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'kind': 'rss', 'count': 0,
                'errors': <Object?>[],
                'warnings': <Object?>['RSS 源请在书源管理里添加'], 'placeholder': true,
              },
            };
      final outcome = await _repo(ffi).importText(text: '{}', kind: 'rss');
      expect(outcome.placeholder, isTrue);
      expect(outcome.ok, isFalse, reason: '占位不是"导入成功"，两者在 count 上同是 0');
      expect(ffi.calls.single.params['mode'], 'append', reason: '缺省模式由 Dart 侧补齐再发');
    });

    test('回话不是那五格时是声音，不是静默的"成功 0 条"', () async {
      final ffi = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      final repo = _repo(ffi);
      await expectLater(
          repo.importText(text: '[]', kind: 'book_source'),
          throwsA(isA<EngineFailure>()),
          reason: '旧代码在这里造了一只 count=0 的成功回话：服务没落库和文件本来就空在界面上一模一样');
      ffi.handler = (action, params) => <String, dynamic>{'ok': true, 'data': null};
      await expectLater(repo.importUrl(url: 'https://x/a.json', kind: 'book_source'),
          throwsA(isA<EngineFailure>()));

      final ok = _RecordingFFI()
        ..handler = (action, params) => <String, dynamic>{
              'ok': true,
              'data': <String, Object?>{
                'kind': 'book_source', 'count': 1,
                'errors': <Object?>[], 'warnings': <Object?>[], 'placeholder': false,
              },
            };
      expect((await _repo(ok).importUrl(url: 'https://x/a.json', kind: 'book_source')).count, 1,
          reason: '正对照：同样的两发只换 data，红的必须只有坏的那一臂');
      expect(ok.calls.single.params, <String, Object?>{
        'url': 'https://x/a.json', 'kind': 'book_source', 'mode': 'append',
      });
    });
  });
}
