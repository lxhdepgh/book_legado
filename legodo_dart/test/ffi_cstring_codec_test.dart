/// 跨界字符串那一对（§4-M2 的读写路径）：往返必须一字不差。
///
/// 钉的是**正确性**，不是速度——速度那笔账在探针里当场量（B1 三臂）。
/// 为什么要单独一条：读法从"逐字节攒 List 再 UTF-8 解"换成"扫到终止符再解整段视图"，
/// 换错了不会红，只会让书名正文变成 mojibake；上一轮就为这个踩过一次
/// （按 charCode 逐字节读，一个汉字拆成三个乱码码元）。
/// 中文和 emoji 混着走的是同一条路，所以载荷里两种都要有。
library;

import 'dart:convert';
import 'dart:ffi' as ffi;

import 'package:flutter_test/flutter_test.dart';
import 'package:legodo_client/core/ffi/engine_ffi.dart';

/// 真库那一发的形状：`srcToMap` 每行 8 个短字段，1371 行 ≈ 238 KB。
String _sourceListPayload(int rows) => jsonEncode(<Map<String, Object?>>[
  for (var i = 0; i < rows; i++)
    <String, Object?>{
      'bookSourceUrl': 'https://source-$i.test/search?key=斗破苍穹',
      'bookSourceName': '💯 连尚读书 $i',
      'bookSourceGroup': '书源组$i',
      'enabled': true,
      'customOrder': i,
      'respondTime': 1234,
      'lastUpdateTime': 0,
      'ruleSearch': {'bookList': '.book', 'name': 'a.name@text'},
    },
]);

void main() {
  test('往返：中文 + emoji + 引号反斜杠换行，进去多少回来多少', () {
    const tricky = '{"a":"汉字 💯","b":"引号 \\" 反斜杠 \\\\ 换行\\n结束","c":""}';
    final ptr = toNativeCString(tricky);
    addTearDown(() => freeNativeCString(ptr));
    expect(fromNativeCString(ptr), tricky);
  });

  test('往返：238 KB 那一发整包一字不差（视图读法不许截断、不许多吃）', () {
    final payload = _sourceListPayload(1371);
    expect(utf8.encode(payload).length, greaterThan(100000),
        reason: '载荷本身要够大，否则"扫到终止符"这条根本没被走到');

    final ptr = toNativeCString(payload);
    addTearDown(() => freeNativeCString(ptr));
    final back = fromNativeCString(ptr);

    expect(back.length, payload.length);
    expect(back, payload);
    // 解得开才算读回来的是同一份 JSON：多吃一个字节都是 FormatException。
    expect((jsonDecode(back) as List).length, 1371);
  });

  test('空串往返是空串，不是 null、不是异常', () {
    final ptr = toNativeCString('');
    addTearDown(() => freeNativeCString(ptr));
    expect(fromNativeCString(ptr), '');
  });

  test('写出去的那一段逐字节 = UTF-8 编码 + 一个 0（分配器不替我们兜底）', () {
    // 钉这一格是因为分配器不再清零：漏写终止符不会恰好撞上一个 0，
    // 而是把堆里的旧字节读成"字符串还在继续"。视图长度按 bytes+1 开，不多读一格。
    const s = '汉字 💯 x';
    final bytes = utf8.encode(s);
    final ptr = toNativeCString(s);
    addTearDown(() => freeNativeCString(ptr));
    final view = ptr.cast<ffi.Uint8>().asTypedList(bytes.length + 1);
    expect(view.sublist(0, bytes.length), bytes);
    expect(view[bytes.length], 0);
  });

  test('nullptr 读回空串（Go 那侧万一回了 0，不能把整个进程带进去）', () {
    expect(fromNativeCString(ffi.nullptr.cast<ffi.Int8>()), '');
  });
}
