/// 把引擎当**子进程**拉起来：`python main.py --serve` → 读它打印的握手行。
///
/// 令牌只在那一行里活一次（见 `GatewayService.token`），所以"连上引擎"这件事
/// 在这台机器上只有两种走法：要么本进程拉起它、要么拉起它的那个父进程把行转交过来。
/// G1 走前者。
///
/// 路径**不硬编码**：`--dart-define` 或环境变量给 `LEGODO_PYTHON`（解释器）与
/// `LEGODO_MAIN`（`main.py` 的位置）。缺任何一个就报"当前环境不支持"——
/// 那是 §6.3 那条 throw-default 的表达方式，不是崩在 `null` 上。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'engine_endpoint.dart';
import 'envelope.dart';

const String _handshakePrefix = 'LEGODO_GATEWAY ';

/// `--dart-define` 的值必须是编译期常量，所以两个键各写一发常量，
/// 再由 [_fromEnvironment] 按运行期传进来的键名挑对应的一个。
const String _pythonFromBuild = String.fromEnvironment('LEGODO_PYTHON');
const String _mainFromBuild = String.fromEnvironment('LEGODO_MAIN');

class StartedEngine {
  StartedEngine({required this.process, required this.endpoint, required this.stderrTail});

  final Process process;
  final EngineEndpoint endpoint;

  /// 子进程的最后几行 stderr：握手超时/绑定失败时，这是唯一能给人看的东西。
  final List<String> stderrTail;
}

class EngineProcess {
  /// 等握手行的上限：今天实测冷启动到握手 640–664 ms（第八十二轮），
  /// 留 60s 是给杀软首跑和磁盘冷读——不是给业务用的预算，业务预算在 RPC 那边。
  static const Duration defaultTimeout = Duration(seconds: 60);

  static String? _fromEnvironment(String key) {
    final fromBuild = switch (key) {
      'LEGODO_PYTHON' => _pythonFromBuild,
      'LEGODO_MAIN' => _mainFromBuild,
      _ => '',
    };
    if (fromBuild.isNotEmpty) return fromBuild;
    final fromRuntime = Platform.environment[key];
    return (fromRuntime == null || fromRuntime.isEmpty) ? null : fromRuntime;
  }

  /// [environment] 只**增补/覆盖**那几个键，父进程的环境照旧传下去
  /// （`Process.start` 的 `includeParentEnvironment` 默认 true）。
  /// 这一条是给基准测量用的：现网语料由 `LEGADO_BOOKSOURCE_DATA_DIR` 选，
  /// R4 的 920 源那份就靠这里注入，不去动用户自己那本库。
  static Future<StartedEngine> start({
    String? python,
    String? mainPy,
    Duration timeout = defaultTimeout,
    List<String> extraArgs = const [],
    Map<String, String> environment = const {},
  }) async {
    final interpreter = python ?? _fromEnvironment('LEGODO_PYTHON');
    final script = mainPy ?? _fromEnvironment('LEGODO_MAIN');
    if (interpreter == null || script == null) {
      throw const EngineFailure(
        EngineFailureKind.unavailable,
        '当前环境不支持拉起引擎：没有 LEGODO_PYTHON / LEGODO_MAIN。'
        '构建时给 --dart-define=LEGODO_PYTHON=…&LEGODO_MAIN=…，'
        '或者先把 `main.py --serve` 起起来再把握手行交给客户端。',
      );
    }
    final process = await Process.start(interpreter, <String>[
      script,
      '--serve',
      '--port',
      '0',
      '--idle-seconds',
      '15',
      ...extraArgs,
    ], workingDirectory: File(script).parent.path, environment: environment);

    final handshake = Completer<EngineEndpoint>();
    final stderrTail = <String>[];
    process.stdout.transform(const Utf8Decoder(allowMalformed: true)).listen((chunk) {
      for (final line in const LineSplitter().convert(chunk)) {
        if (!line.startsWith(_handshakePrefix) || handshake.isCompleted) continue;
        try {
          final json = jsonDecode(line.substring(_handshakePrefix.length));
          if (json is Map<String, Object?>) {
            handshake.complete(EngineEndpoint.fromHandshake(json));
          }
        } on FormatException catch (exc) {
          handshake.completeError(EngineFailure(EngineFailureKind.transport,
              '握手行不是合法 JSON：${exc.message}'));
        }
      }
    });
    process.stderr.transform(const Utf8Decoder(allowMalformed: true)).listen((chunk) {
      for (final line in const LineSplitter().convert(chunk)) {
        stderrTail.add(line);
        if (stderrTail.length > 40) stderrTail.removeAt(0);
      }
    });

    try {
      final endpoint = await handshake.future.timeout(timeout, onTimeout: () {
        throw EngineFailure(
            EngineFailureKind.timeout,
            '${timeout.inSeconds}s 没等到 $_handshakePrefix 那一行'
            '（子进程 stderr 尾部：${stderrTail.join(' | ')}）');
      });
      return StartedEngine(process: process, endpoint: endpoint, stderrTail: stderrTail);
    } catch (_) {
      // 握手失败/超时，需要强制杀死子进程并等待其真正退出
      await _forceKillProcess(process);
      rethrow;
    }
  }
  
  /// 强制杀死子进程并等待其真正退出（与 bootstrap.dart 共用同一套逻辑）
  static Future<void> _forceKillProcess(Process process) async {
    if (process.exitCode != null) return; // 已经退出
    
    // 先尝试温和终止
    process.kill(ProcessSignal.sigterm);
    
    // 给进程 2 秒时间优雅退出
    try {
      await process.exitCode.timeout(const Duration(seconds: 2));
      return; // 已成功退出
    } on TimeoutException {
      // 超时未退出，需要强制杀死
    }
    
    // Windows 上使用 taskkill 强制杀死进程树
    if (Platform.isWindows) {
      try {
        final pid = process.pid;
        await Process.run('taskkill', ['/F', '/T', '/PID', '$pid']);
        // 再次等待确认退出
        await process.exitCode.timeout(const Duration(seconds: 1));
      } on Object {
        // taskkill 失败或进程已不存在，忽略
      }
    } else {
      // Unix 系统使用 SIGKILL
      process.kill(ProcessSignal.sigkill);
      try {
        await process.exitCode.timeout(const Duration(seconds: 1));
      } on TimeoutException {
        // 极端情况：SIGKILL 也无法杀死，记录但不再重试
      }
    }
  }
}
