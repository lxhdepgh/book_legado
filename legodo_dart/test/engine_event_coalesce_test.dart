/// 事件合流（§4-M1）：一批轮询里同名的多条只发一条给界面。
///
/// 为什么这一条要有护栏：合流的窗口就是"一批"，界面每收到一条 `SourceChangedEvent`
/// 就整表重取一次（实测单价 7.4 ms），所以"60 条变 1 条"不是修辞，是 457 ms 变 8 ms。
/// 反过来说，**把不该合的合了**更贵——进度类事件画的就是逐条，合流会让界面跳号。
/// 于是两臂都钉：白名单里 60→1，白名单外 60→60，两臂走的是同一个口。
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/events/engine_event.dart';
import 'package:legodo_client/core/events/event_names.dart';
import 'package:legodo_client/core/ffi/engine_ffi.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';

/// 只覆写 `call`：把 `engine_poll` 的回答按帧形状喂进去，其余按替身默认。
class _ScriptedFFI implements GoFFIClient {
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
    return handler?.call(action, params) ?? <String, dynamic>{'ok': true, 'data': null};
  }
}

Map<String, dynamic> _frame(List<EngineEvent> events, int lastSeq) => <String, dynamic>{
  'ok': true,
  'data': <String, Object?>{
    'events': <Object?>[
      for (final event in events)
        <String, Object?>{'seq': 0, 'name': event.name, 'payload': event.payload},
    ],
    'last_seq': lastSeq,
    'lost': false,
  },
};

EngineEvent _changed(int index) => EngineEvent(EngineEventNames.sourceChanged,
    <String, Object?>{'change_type': 'enable', 'source_id': 'u$index', 'timestamp': 1.0});

EngineEvent _checked(int index) => EngineEvent(EngineEventNames.checkFinished,
    <String, Object?>{'source_id': 'u$index', 'state_class': 'usable', 'done': index, 'total': 60});

/// 过两三拍轮询，收集这一批发到界面的事件。第一拍带整批，其后各拍回空批
/// （`last_seq` 保持在这一批的末尾——真实的服务端游标就是这么走的）。
Future<List<EngineEvent>> _drain(List<EngineEvent> batch, _ScriptedFFI ffi) async {
  final got = <EngineEvent>[];
  var first = true;
  ffi.handler = (action, params) {
    if (!first) return _frame(const <EngineEvent>[], batch.length);
    first = false;
    return _frame(batch, batch.length);
  };
  final sub = FFIEngineAdapter(client: ffi).events().listen(got.add);
  // 轮询周期 250 ms，等 900 ms 是 3 拍：机器再慢也至少跑到第二拍。
  await Future<void>.delayed(const Duration(milliseconds: 900));
  await sub.cancel();
  return got;
}

void main() {
  test('白名单里 60 条同名收成一条，且留的是最后那条的形状', () async {
    final ffi = _ScriptedFFI();
    final got = await _drain([for (var i = 0; i < 60; i++) _changed(i)], ffi);

    expect(got.length, 1, reason: '60 条 SourceChangedEvent 该只重取一次整表');
    expect(got.single.name, EngineEventNames.sourceChanged);
    expect(got.single['source_id'], 'u59', reason: '库里最新的那条赢，不能留第一发的旧值');
  });

  test('白名单外一根不许合：60 条检测进度照旧 60 发', () async {
    final ffi = _ScriptedFFI();
    final got = await _drain([for (var i = 0; i < 60; i++) _checked(i)], ffi);

    expect(got.length, 60, reason: '进度类合成一条就是界面跳号——这条是合流的边界');
    expect(got.map((e) => e['done']).toList(), [for (var i = 0; i < 60; i++) i]);
  });

  test('合流把同名那条留在它第一次出现的位置，不挤后面的进度帧', () async {
    final ffi = _ScriptedFFI();
    final got = await _drain(<EngineEvent>[
      _changed(0),
      _checked(1),
      _checked(2),
      _changed(3),
    ], ffi);

    expect(got.map((e) => e.name).toList(), <String>[
      EngineEventNames.sourceChanged,
      EngineEventNames.checkFinished,
      EngineEventNames.checkFinished,
    ]);
    expect(got.first['source_id'], 'u3');
  });

  test('游标跟着服务端帧走：合流只少发几条给界面，一条不许漏记', () async {
    final ffi = _ScriptedFFI();
    await _drain([for (var i = 0; i < 60; i++) _changed(i)], ffi);

    final since = ffi.calls.where((c) => c.action == 'engine_poll').map((c) => c.params['since']);
    expect(since, isNotEmpty, reason: '流没跑起来，上面那些读数就都是零');
    // 60 条被合成一条发给界面，但下一拍问服务端时必须从 60 起——
    // 按合流后的条数推进游标会把环里剩下的事件整段跳过。
    expect(since.last, 60);
  });

  test('coalesceBatch 的纯函数面：空批回空、顺序与条数一一对得上', () {
    expect(coalesceBatch(const <EngineEvent>[]), isEmpty);
    expect(coalescableEventNames, <String>{EngineEventNames.sourceChanged});
  });
}
