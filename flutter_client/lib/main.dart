/// 入口：**先连引擎，连上了才谈界面**（§4.6）。
///
/// 这一条顺序是 G1 判据的一部分：连不上时用户看到的是一屏写明"连的哪个地址、
/// 哪一步没通"的话，不是一个永远转圈的壳。flet 那边没有这一层（服务在同一进程里），
/// 拆进程之后"服务没起来"第一次成为用户能看见的状态，所以它得有自己的屏与测试。
library;

import 'package:flutter/widgets.dart';

import 'app/bootstrap.dart';
import 'core/rpc/envelope.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    final boot = await bootstrapEngine();
    runApp(boot.scope(const LegodoApp()));
  } on EngineFailure catch (exc) {
    runApp(bootFailureScope(exc));
  } on Object catch (exc) {
    runApp(bootFailureScope(
        EngineFailure(EngineFailureKind.transport, '起手失败：$exc')));
  }
}
