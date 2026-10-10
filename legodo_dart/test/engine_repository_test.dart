/// `RpcEngineRepository` 那一层：把契约面落到信封上（G1 那三发 + G3 那 13 发 reader）。
///
/// 替身仍然是 `MockClient`（真 `Request`/`Response` 形状）。用 `FakeEngineRepository`
/// 去测 `RpcEngineRepository` 是循环论证——那两个是互不相干的实现，各自要有人钉。
///
/// reader 那一族在这里钉的是**三条形状规矩**（`null` 合法的那两发、形状变了要响、
/// `position_save` 只有回话才算数），字段名那半边在 `golden_contract_test.dart`。
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:legodo_client/core/rpc/engine_endpoint.dart';
import 'package:legodo_client/core/rpc/engine_repository.dart';
import 'package:legodo_client/core/rpc/envelope.dart';
import 'package:legodo_client/core/rpc/rpc_client.dart';
import 'package:legodo_client/core/rpc/rpc_engine_repository.dart';

const EngineEndpoint _endpoint = EngineEndpoint(
  host: '127.0.0.1', port: 1, token: 't',
  protocolVersion: 1, build: 'test', pid: 1,
);

RpcEngineRepository _with(String body, {int status = 200}) => RpcEngineRepository(
      RpcClient(_endpoint,
          client: MockClient((_) async =>
              http.Response(body, status,
                  headers: {'content-type': 'application/json; charset=utf-8'}))),
    );

const _okRows = '{"v":1,"id":"d-1","ok":true,"code":null,"error":null,"data":['
    '{"name":"仙子，助我修行","author":"逆时针的圈","url":"https://e/1",'
    '"source_url":"https://e","durChapterTitle":"第一章 金刚萝莉师姐"}]}';

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

  test('listBookshelf 把 data 数组变成模型，字段一个不丢', () async {
    final items = await _with(_okRows).listBookshelf();
    expect(items.length, 1);
    expect(items.first.name, '仙子，助我修行');
    expect(items.first.subtitle, '逆时针的圈 · 读到 第一章 金刚萝莉师姐');
    // raw 保留原行：后面的门（换源/进度/缓存）不再逐个复制字段。
    expect(items.first.raw['durChapterTitle'], '第一章 金刚萝莉师姐');
  });

  test('include_staged 真的落到 payload 上（不带就不许发这个键）', () async {
    final sent = <Map<String, Object?>>[];
    final repository = RpcEngineRepository(RpcClient(_endpoint,
        client: MockClient((request) async {
          sent.add(jsonDecode(utf8.decode(request.bodyBytes))
              as Map<String, Object?>);
          return http.Response('{"v":1,"id":"d-1","ok":true,"data":[]}', 200,
              headers: {'content-type': 'application/json'});
        })));
    await repository.listBookshelf();
    expect((sent.first['payload']! as Map).containsKey('include_staged'), isFalse);
    await repository.listBookshelf(includeStaged: true);
    expect(sent.last['payload'], {'include_staged': true});
  });

  test('服务端回 null / 回对象 = 形状变了，那一发必须响而不是渲染成空书架', () async {
    for (final body in <String>[
      '{"v":1,"id":"d-1","ok":true,"data":null}',
      '{"v":1,"id":"d-1","ok":true,"data":{"name":"一条而不是数组"}}',
    ]) {
      await expectLater(
          _with(body).listBookshelf(),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)),
          reason: body);
    }
  });

  test('hello 把网关那六个字段读成 GatewayInfo', () async {
    final info = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
            '"protocol_version":1,"build":"0.9","capabilities":["rpc"],'
            '"schema_digest":"abc","actions":["engine.hello"],"pid":4242}}')
        .hello();
    expect(info.protocolVersion, 1);
    expect(info.build, '0.9');
    expect(info.capabilities, ['rpc']);
    expect(info.schemaDigest, 'abc');
    expect(info.actions, ['engine.hello']);
    expect(info.pid, 4242);
  });

  test('shutdown 带原因', () async {
    http.Request? seen;
    final repository = RpcEngineRepository(RpcClient(_endpoint,
        client: MockClient((request) async {
          seen = request;
          return http.Response('{"v":1,"id":"d-1","ok":true,"data":null}', 200,
              headers: {'content-type': 'application/json'});
        })));
    await repository.shutdown(reason: 'window closed');
    expect((jsonDecode(utf8.decode(seen!.bodyBytes)) as Map)['payload'],
        {'reason': 'window closed'});
  });

  test('服务端回的 code 在客户端分支上：engine 不会伪装成 timeout', () async {
    final repository = _with(
        '{"v":1,"id":"d-1","ok":false,"data":null,"error":"炸了","code":"engine"}');
    await expectLater(
        repository.listBookshelf(),
        throwsA(isA<EngineFailure>()
            .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
            .having((EngineFailure e) => e.isTransient, 'isTransient', isFalse)));
  });

  test('握手行的令牌不许出现在 toString 里（它会进日志）', () {
    const endpoint = EngineEndpoint(
        host: '127.0.0.1', port: 1234, token: 'super-secret-token',
        protocolVersion: 1, build: 'b', pid: 9);
    expect('$endpoint', isNot(contains('super-secret-token')));
    expect('$endpoint', contains('127.0.0.1:1234'), reason: '排查靠的是地址与 pid');
  });

  // ------------------------------------------------------------------ #
  // reader.*：这一族的三条形状规矩（`null` 合法 / 形状变了要响 / 回话才算数）
  // ------------------------------------------------------------------ #
  group('reader.* 仓库面', () {
    test('position_get 与 staged 的 null 是合法回话，不是"形状变了"', () async {
      // 服务端在这两发上确实回 null（"这本书从没读过"/"这本不是试读，别问"）。
      // 如果这里走了 `_asMap`，用户看到的是"连接出问题"而不是"没进度"——
      // 与 `source.get` 同一族，那一发的注释就是这条规矩的出处。
      final position = await _with('{"v":1,"id":"d-1","ok":true,"data":null}')
          .readPosition(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(position, isNull);
      expect(await _with('{"v":1,"id":"d-1","ok":true,"data":null}')
          .stagedRecord(), isNull);

      // 但"位置在第 0 章第 0 字"必须是**另一个形状**：那是"刚开过这本"，不是"没读过"。
      final zero = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
              '"chapter_index":0,"chapter_pos":0,"scroll_offset":0.0,'
              '"scroll_ratio":0.0,"font_size":16,"last_read_at":0,'
              '"chapter_url":"","chapter_name":"","chapter_paragraph":null}}')
          .readPosition(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(zero, isNotNull, reason: 'null 与零位置读成了同一个形状');
      expect(zero!.characterOffset, 0);
      expect(zero.paragraphIndex, isNull,
          reason: '段号 null 被读成 0 = 一本读到中间的书显示成"刚开始读"');
    });

    test('toc / bookmarks 回 null 或回对象 = 形状变了，必须响而不是渲染成空目录', () async {
      // 目录空与目录读不到是两件事：后者界面该说"再试一次"，前者该说"这本没章"。
      final toc = RpcEngineRepository(RpcClient(_endpoint,
          client: MockClient((_) async => http.Response(
              '{"v":1,"id":"d-1","ok":true,"data":null}', 200,
              headers: {'content-type': 'application/json'}))));
      await expectLater(
          toc.chapterRows(sourceUrl: 'https://s', bookUrl: 'https://s/b'),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)
              .having((EngineFailure e) => e.action, 'action', 'reader.toc')));
      await expectLater(
          toc.listBookmarks('https://s/b'),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.action, 'action',
                  'reader.bookmarks')));
    });

    test('bookmark_add 回的是 id：回成布尔就是形状变了', () async {
      final id = await _with('{"v":1,"id":"d-1","ok":true,"data":7}')
          .addBookmark(bookUrl: 'https://s/b', chapterIndex: 3,
              offset: 0.25, summary: '第4章');
      expect(id, 7);
      await expectLater(
          _with('{"v":1,"id":"d-1","ok":true,"data":true}')
              .addBookmark(bookUrl: 'https://s/b', chapterIndex: 3),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)));
      // 删那一发反过来：回的是**是否真删到**，不是受理凭据。
      expect(await _with('{"v":1,"id":"d-1","ok":true,"data":false}')
          .removeBookmark(bookUrl: 'https://s/b', bookmarkId: 7), isFalse);
    });

    test('position_save 回话才算数：界面拿夹回后的那一份，不拿请求那一份', () async {
      // 服务端把越界的偏移夹回章内字符流末端（`_clamped`）。客户端如果自己留着
      // 请求那份，界面上"存好了"而库里是另一个数——下一次续读落在别处。
      final saved = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
              '"chapter_index":1,"chapter_pos":5802,"chapter_paragraph":7,'
              '"scroll_offset":321.0,"scroll_ratio":1.0,"font_size":18,'
              '"last_read_at":1791520941,"chapter_url":"https://s/c2",'
              '"chapter_name":"第2章"}}')
          .savePosition(
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

    test('三把 url 与开关都落在 payload 上（拼错键名 = 服务端按缺省走）', () async {
      final sent = <Map<String, Object?>>[];
      final repository = RpcEngineRepository(RpcClient(_endpoint,
          client: MockClient((request) async {
            final body = jsonDecode(utf8.decode(request.bodyBytes))
                as Map<String, Object?>;
            sent.add(body);
            // 回话按这一发的形状给：随便回一个 `[]` 会让上面那三条形状检查先抛出来，
            // 于是这一格测的其实是"解不开信封"，不是"键名对不对"。
            final data = switch (body['action']) {
              'reader.content' => <String, Object?>{
                  'chapterUrl': 'https://s/c', 'chapterIndex': 0,
                  'chapterName': '', 'title': '', 'imageStyle': '',
                  'blocks': <Object?>[], 'paragraphCount': 0, 'totalChars': 0,
                },
              'reader.close' => <String, Object?>{
                  'closed': true, 'staged': null,
                },
              'reader.open' => <String, Object?>{
                  'book': <String, Object?>{'url': 'https://s/b'},
                  'sourceUrl': 'https://s', 'bookUrl': 'https://s/b',
                  'chapterCount': 0, 'position': <String, Object?>{},
                },
              'reader.position_save' => <String, Object?>{},
              'reader.search_content' => <String, Object?>{
                  'hits': <Object?>[], 'written': 0, 'indexedChapters': 0,
                  'ftsAvailable': false,
                },
              'reader.bookmark_add' => 1,
              _ => <Object?>[],
            };
            return http.Response(
                jsonEncode(<String, Object?>{
                  'v': 1, 'id': 'd-1', 'ok': true, 'code': null,
                  'error': null, 'data': data,
                }), 200,
                headers: {'content-type': 'application/json'});
          })));
      await repository.chapterDocument(
          sourceUrl: 'https://s', bookUrl: 'https://s/b', chapterUrl: 'https://s/c');
      expect(sent.last['action'], 'reader.content');
      expect(sent.last['payload'], {
        'source_url': 'https://s', 'book_url': 'https://s/b',
        'chapter_url': 'https://s/c',
      });

      await repository.chapterRows(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(sent.last['payload'], {
        'source_url': 'https://s', 'book_url': 'https://s/b', 'refresh': false,
      },
          reason: 'refresh 恒带：服务端那一发的默认值靠它，省键与传 false 同值但要一致');

      await repository.closeReader(sourceUrl: 'https://s', bookUrl: 'https://s/b');
      expect(sent.last['payload'], {
        'source_url': 'https://s', 'book_url': 'https://s/b',
        'save_progress': true,
      });

      // bookmark_add 的 offset/summary **不许省略**：服务端签名没有默认值，
      // 少一个键是 TypeError（引擎错误信封），不是"用默认值"。
      await repository.addBookmark(bookUrl: 'https://s/b', chapterIndex: 2);
      expect(sent.last['payload'],
          {'book_url': 'https://s/b', 'chapter_index': 2, 'offset': 0.0,
            'summary': ''});

      // 开书那一发把**字典快照**原样放进 payload：模型对象不许过协议面。
      await repository.openReader(
          sourceUrl: 'https://s', book: const <String, Object?>{'url': 'https://s/b'});
      expect(sent.last['payload'], {
        'source_url': 'https://s', 'book': {'url': 'https://s/b'},
        'shelve': true, 'resume': true,
      }, reason: 'shelve/resume 恒带：试读不入架靠的就是显式传 false，省键会跟着服务端的默认值走');

      await repository.savePosition(sourceUrl: 'https://s', bookUrl: 'https://s/b',
          positionJson: const <String, Object?>{'chapter_index': 1});
      expect(sent.last['payload'], {
        'source_url': 'https://s', 'book_url': 'https://s/b',
        'position_json': {'chapter_index': 1},
      });

      await repository.searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(sent.last['payload'],
          {'book_url': 'https://s/b', 'keyword': '风起', 'source_url': ''},
          reason: '不给 source_url 时送空串（服务端那一发的默认值就是空串）。'
              '键名漂了服务端只当没这一格：搜索会拿空关键词回"这本书里没找到"');
    });

    test('search_content 的 0 命中带着"为什么是 0"那两个读数', () async {
      final result = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
              '"hits":[],"written":0,"indexedChapters":3,"ftsAvailable":true}}')
          .searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(result.hits, isEmpty);
      expect(result.indexedChapters, 3,
          reason: '只索引了 3 章：界面得说"只搜了已缓存的章"，不能说"书里没有"');
      expect(result.ftsAvailable, isTrue);

      final noFts = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
              '"hits":[],"written":0,"indexedChapters":0,"ftsAvailable":false}}')
          .searchBookContent(bookUrl: 'https://s/b', keyword: '风起');
      expect(noFts.ftsAvailable, isFalse, reason: '这台机器根本搜不了，是第三种 0');
    });

    test('reader.toc--no-session 那种错误信封：拒答交成 EngineFailure，不是空目录', () async {
      // 语料里那份样本就是这一发的形状（会话不是这本书 → 服务端具名拒答）。
      final repository = _with('{"v":1,"id":"d-1","ok":false,"data":null,'
          '"error":"ReaderSessionError: 当前会话不是这本书","code":"engine"}');
      await expectLater(
          repository.chapterRows(sourceUrl: 'https://s', bookUrl: 'https://s/别的'),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind', EngineFailureKind.engine)
              .having((EngineFailure e) => e.message, 'message',
                  contains('ReaderSessionError'))));
    });
  });

  // ------------------------------------------------------------------ #
  // switch.*：换源那一族的四条形状规矩（G3-D3）
  // ------------------------------------------------------------------ #
  group('switch.* 仓库面', () {
    test('switch.seeds 回的是裸行数组；回对象或 null 都是形状变了', () async {
      final seeds = await _with('{"v":1,"id":"d-1","ok":true,"data":['
              '{"name":"换源目标书","author":"作者戊","url":"https://s/a/1",'
              '"origin":"https://s/a","origin_name":"G3换源a"}]}')
          .switchSeeds(book: const <String, Object?>{'name': '换源目标书'});
      expect(seeds, hasLength(1));
      // 种子的"来源"就藏在行里的 `origin`：`SwitchCandidate.fromSeed` 认这一格，
      // 空 origin 的行点下去 `switch.apply` 会报出一条取不到的源。
      expect(seeds.first.origin, 'https://s/a');
      expect(seeds.first.originName, 'G3换源a');
      expect(seeds.first.identityKey, 'https://s/a|https://s/a/1');

      for (final body in <String>[
        '{"v":1,"id":"d-1","ok":true,"data":null}',
        '{"v":1,"id":"d-1","ok":true,"data":{"name":"一行而不是数组"}}',
      ]) {
        await expectLater(
            _with(body).switchSeeds(book: const <String, Object?>{}),
            throwsA(isA<EngineFailure>()
                .having((EngineFailure e) => e.kind, 'kind',
                    EngineFailureKind.transport)
                .having((EngineFailure e) => e.action, 'action',
                    'switch.seeds')),
            reason: body);
      }
    });

    test('switch.scan 是 job：四格都落 payload，回话里一根候选都没有', () async {
      final sent = <Map<String, Object?>>[];
      final repository = RpcEngineRepository(RpcClient(_endpoint,
          client: MockClient((request) async {
            sent.add(jsonDecode(utf8.decode(request.bodyBytes))
                as Map<String, Object?>);
            return http.Response(
                '{"v":1,"id":"d-1","ok":true,"data":{"started":true,'
                '"action":"switch.scan"}}', 200,
                headers: {'content-type': 'application/json'});
          })));
      final accepted = await repository.switchScan(
          book: const <String, Object?>{'name': '换源目标书', 'origin': 'https://s/x'},
          switchId: 6379028,
          keyword: '换源目标书',
          group: 'G3换源');
      expect(accepted.started, isTrue);
      expect(accepted.action, 'switch.scan');
      // 四格恒带（黄金样本 `switch.scan.json` 的 request.payload 就是这四格）。
      // `switch_id` 少了服务端按 0 办；`group` 少了就从不联网变成扫全库——
      // 两者都不报错，只是把这一轮变成另一件事。
      expect(sent.last['payload'], <String, Object?>{
        'book': {'name': '换源目标书', 'origin': 'https://s/x'},
        'switch_id': 6379028,
        'keyword': '换源目标书',
        'group': 'G3换源',
      });
      // 默认值也必须是**显式**的两格：只传 book/switch_id 时 keyword/group 送空串，
      // 而不是省键（省键等于让服务端猜它自己的默认值）。
      await repository.switchScan(
          book: const <String, Object?>{}, switchId: 1);
      expect(sent.last['payload'], <String, Object?>{
        'book': <String, Object?>{}, 'switch_id': 1, 'keyword': '', 'group': '',
      });
    });

    test('switch.snapshot 的 switch_id 服务端可能回数字，读出来必须是同一个字符串', () async {
      // 契约上服务端把它折成 str（`sid = str(switch_id or 0)`），黄金样本里就是
      // `"6379028"`。但 JSON 里数字与字符串在信封层是同一条通道——万一哪回它是数字，
      // 模型按 int 读会得到 6379028，而 notifier 拿 `'$_switchId'` 比的是文本；
      // 两种读法必须收敛到同一个数串，否则"只认本轮"那半边守卫会静默失效。
      for (final raw in <String>['"6379028"', '6379028']) {
        final run = await _with('{"v":1,"id":"d-1","ok":true,"data":{'
                '"switch_id":$raw,"running":true,"cancelled":false,'
                '"done":2,"total":3,"failures":1,"starved":0,'
                '"candidates":[],"error":""}}')
            .switchSnapshot();
        expect(run.switchId, '6379028', reason: 'wire 形态 $raw');
        expect(run.running, isTrue);
        expect(run.done, 2);
        expect(run.total, 3);
        expect(run.failures, 1, reason: 'failures 读成 0 = 界面把失败源说成"都在跑"');
        expect(run.candidates, isEmpty);
      }
    });

    test('switch.cancel 的那一格必须是布尔：缺键要响，false 与 true 各读各的', () async {
      expect(await _with('{"v":1,"id":"d-1","ok":true,"data":{"cancelled":false}}')
          .switchCancel(), isFalse);
      expect(await _with('{"v":1,"id":"d-1","ok":true,"data":{"cancelled":true}}')
          .switchCancel(), isTrue);
      // 缺键与裸布尔都得响：前者会让界面把"什么都没在跑"写成「已停止换源扫描」，
      // 后者是形状变了（这一发回的是**对象里那一格**，不是受理凭据）。
      for (final body in <String>[
        '{"v":1,"id":"d-1","ok":true,"data":{}}',
        '{"v":1,"id":"d-1","ok":true,"data":true}',
        '{"v":1,"id":"d-1","ok":true,"data":null}',
      ]) {
        await expectLater(
            _with(body).switchCancel(),
            throwsA(isA<EngineFailure>()
                .having((EngineFailure e) => e.kind, 'kind',
                    EngineFailureKind.transport)
                .having((EngineFailure e) => e.action, 'action',
                    'switch.cancel')),
            reason: body);
      }
    });

    test('switch.apply 的三格各管一件事，applied 缺键不许读成"没换成"', () async {
      final sent = <Map<String, Object?>>[];
      final repository = RpcEngineRepository(RpcClient(_endpoint,
          client: MockClient((request) async {
            sent.add(jsonDecode(utf8.decode(request.bodyBytes))
                as Map<String, Object?>);
            return http.Response(
                '{"v":1,"id":"d-1","ok":true,"data":{"applied":true,'
                '"source_url":"https://s/b"}}', 200,
                headers: {'content-type': 'application/json'});
          })));
      final result = await repository.applySwitch(
          record: const <String, Object?>{'url': 'https://s/x/1', 'origin': 'https://s/x'},
          sourceUrl: 'https://s/b',
          book: const <String, Object?>{'url': 'https://s/b/1', 'origin': 'https://s/b'});
      expect(result.applied, isTrue);
      expect(result.sourceUrl, 'https://s/b');
      expect(sent.last['action'], 'switch.apply');
      expect(sent.last['payload'], <String, Object?>{
        'record': {'url': 'https://s/x/1', 'origin': 'https://s/x'},
        'source_url': 'https://s/b',
        'book': {'url': 'https://s/b/1', 'origin': 'https://s/b'},
      }, reason: 'record 找回书架那一行、source_url 找回那条源、book 是换成哪一本——'
          '少一格服务端就按缺省办，而三种缺法都回同一句"没换成"');

      await expectLater(
          _with('{"v":1,"id":"d-1","ok":true,"data":{"source_url":"https://s/b"}}')
              .applySwitch(
                  record: const <String, Object?>{},
                  sourceUrl: 'https://s/b',
                  book: const <String, Object?>{}),
          throwsA(isA<EngineFailure>()
              .having((EngineFailure e) => e.kind, 'kind',
                  EngineFailureKind.transport)
              .having((EngineFailure e) => e.action, 'action', 'switch.apply')),
          reason: 'applied 缺键读成 false = 把"这一发没成"说成"记录被删了或与其他书冲突"');
    });
  });
}
