/// `framesToEvents` 的形状钉子：engine_poll 的 data → 事件批（纯函数，无 FFI）。
///
/// 这里钉的是三条跨进程契约（event_names.dart / engine_event.dart 的同源关系）：
///   1. `name` 原样透传（已是 `SearchFinishedEvent` 这一族的类名，不再映射）；
///   2. `payload` 对象按键原样保留（各屏 fromJson 直接读 snake_case 键）；
///   3. `last_seq` 推进游标、`lost` 原样带出。
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/rpc/ffi_engine_adapter.dart';

void main() {
  test('正常一批：类名与 payload 键原样透传，last_seq 推进游标', () {
    final batch = framesToEvents(<String, Object?>{
      'events': <Object?>[
        <String, Object?>{
          'seq': 41,
          'name': 'SearchFinishedEvent',
          'payload': <String, Object?>{
            'search_id': '7',
            'items': <Object?>[],
            'finished': false,
          },
        },
        <String, Object?>{
          'seq': 42,
          'name': 'SourceChangedEvent',
          'payload': <String, Object?>{'change_type': 'save', 'source_id': 'u'},
        },
      ],
      'last_seq': 42,
      'lost': false,
    });

    expect(batch.events.length, 2);
    expect(batch.events[0].name, 'SearchFinishedEvent');
    expect(batch.events[0].payload['search_id'], '7');
    expect(batch.events[0].payload['finished'], false);
    expect(batch.events[1].name, 'SourceChangedEvent');
    expect(batch.lastSeq, 42);
    expect(batch.lost, false);
  });

  test('空批：events 为空数组时不进任何帧，last_seq 照带', () {
    final batch = framesToEvents(<String, Object?>{
      'events': <Object?>[],
      'last_seq': 5,
      'lost': false,
    });
    expect(batch.events, isEmpty);
    expect(batch.lastSeq, 5);
  });

  test('lost 原样带出', () {
    final batch = framesToEvents(<String, Object?>{
      'events': <Object?>[],
      'last_seq': 99,
      'lost': true,
    });
    expect(batch.lost, true);
  });

  test('坏帧不炸：非 Map 的 data、缺 name 的条目、非对象 payload 各归各', () {
    expect(framesToEvents(null).events, isEmpty);
    expect(framesToEvents('nonsense').lastSeq, 0);

    final batch = framesToEvents(<String, Object?>{
      'events': <Object?>[
        'not-a-map',
        <String, Object?>{'seq': 1, 'payload': <String, Object?>{}},
        <String, Object?>{'seq': 2, 'name': '', 'payload': <String, Object?>{}},
        <String, Object?>{'seq': 3, 'name': 'X', 'payload': 'not-a-map'},
      ],
      'last_seq': 3,
    });
    expect(batch.events.length, 1, reason: '只有第 3 条有可用类名');
    expect(batch.events.single.name, 'X');
    expect(batch.events.single.payload, isEmpty);
  });
}
