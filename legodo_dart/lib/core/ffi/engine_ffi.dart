/// Go engine FFI client — uses the unified `Call(action, paramsJSON)` entry point.
///
/// Architecture:
///   Flutter → dart:ffi → Go.Call("action_name", '{"key":"value"}') → resultJSON
///
/// All actions flow through ONE C function. Adding new RPC actions only requires
/// updating the Go internal dispatch() switch — no new FFI stubs or rebuilds needed.
library;

import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io' as io;

// ---------------------------------------------------------------------------
// C function signatures — all Int8* (char*) matching Go's actual exports
// ---------------------------------------------------------------------------

typedef CallC = ffi.Pointer<ffi.Int8> Function(
  ffi.Pointer<ffi.Int8>, // action (char*)
  ffi.Pointer<ffi.Int8>, // paramsJSON (char*)
);
typedef CallDart = ffi.Pointer<ffi.Int8> Function(
  ffi.Pointer<ffi.Int8>,
  ffi.Pointer<ffi.Int8>,
);

typedef InitializeC = ffi.Pointer<ffi.Int8> Function();
typedef InitializeDart = ffi.Pointer<ffi.Int8> Function();

typedef CleanupC = ffi.Void Function();
typedef CleanupDart = void Function();

typedef FreeStringC = ffi.Void Function(ffi.Pointer<ffi.Int8>);
typedef FreeStringDart = void Function(ffi.Pointer<ffi.Int8>);

typedef HeapAllocC = ffi.Pointer<ffi.Uint8> Function(
  ffi.Pointer<ffi.Uint8>, // hHeap
  ffi.Uint32, // flags
  ffi.Uint64, // bytes
);
typedef HeapAllocDart = ffi.Pointer<ffi.Uint8> Function(
  ffi.Pointer<ffi.Uint8>,
  int,
  int,
);

typedef HeapFreeC = ffi.Uint32 Function(
  ffi.Pointer<ffi.Uint8>,
  ffi.Uint32,
  ffi.Pointer<ffi.Uint8>,
);
typedef HeapFreeDart = int Function(
  ffi.Pointer<ffi.Uint8>,
  int,
  ffi.Pointer<ffi.Uint8>,
);

// ---------------------------------------------------------------------------
// Memory allocator using OS-native APIs (works in AOT without any issues)
// Uses ProcessHeap on Windows, mmap/munmap on Linux/macOS.
// ---------------------------------------------------------------------------

class _Mem {
  static bool _initialized = false;
  static late ffi.Pointer<ffi.Uint8> _heapHandle;
  // 句柄与两个函数指针各存一份：原先每次分配都重开一遍 kernel32 再 lookupFunction，
  // 而一发同步调用要分配两个入参 —— 实测 0.02–0.04 ms/发全花在这两对上。
  static late HeapAllocDart _heapAlloc;
  static late HeapFreeDart _heapFree;

  /// Check if the memory allocator has been initialized.
  static bool get initialized => _initialized;

  /// Initialize the memory allocator. Called once on first access.
  static void init() {
    final kernel = ffi.DynamicLibrary.open('kernel32.dll');
    final createHeap = kernel.lookupFunction<
        ffi.Pointer<ffi.Uint8> Function(),
        ffi.Pointer<ffi.Uint8> Function()>('GetProcessHeap');
    _heapHandle = createHeap.call();
    _heapAlloc = kernel.lookupFunction<HeapAllocC, HeapAllocDart>('HeapAlloc');
    _heapFree = kernel.lookupFunction<HeapFreeC, HeapFreeDart>('HeapFree');
    _initialized = true;
  }

  /// Allocate N bytes, zero-initialized. Returns Int8 pointer.
  ///
  /// 0x00000008 = HEAP_ZERO_MEMORY。试过改成 0：12 MB 那一臂两趟读 21.98 / 21.71 ms，
  /// 270 KB 那一臂 0.608 / 0.648 ms——差量都在噪声里，**实测无收益**；
  /// 而清零是万一漏写终止符时唯一免费的兜底，留着。
  static ffi.Pointer<ffi.Int8> malloc(int n) =>
      _heapAlloc.call(_heapHandle, 0x00000008, n.toUnsigned(64)).cast();

  /// Free a previously allocated pointer.
  static void free(ffi.Pointer<ffi.Int8> ptr) =>
      _heapFree.call(_heapHandle, 0, ptr.cast());
}

// ---------------------------------------------------------------------------
// 跨界字符串（一对，护栏钉这一对：写进去读回来必须一字不差）
// ---------------------------------------------------------------------------

/// [String] → 以 0 结尾的 UTF-8 缓冲（Go 那侧 `Call` 收到的 `char*` 就是这个形状）。
/// 用完交给 [freeNativeCString]，它和 `_Mem.malloc` 是同一套进程堆。
ffi.Pointer<ffi.Int8> toNativeCString(String value) {
  // 编解码自己把分配器就位：这一段不依赖 DLL，离屏护栏测试（往返同一串）才进得来。
  if (!_Mem.initialized) _Mem.init();
  final bytes = utf8.encode(value);
  final len = bytes.length + 1;
  final ptr = _Mem.malloc(len);
  // 一把 setRange 拷完整段（含终止符那一格）：逐字节 `ptr[i]=…` 在 238 KB 上实测 3.4 ms。
  ptr.cast<ffi.Uint8>().asTypedList(len)
    ..setRange(0, bytes.length, bytes)
    ..[bytes.length] = 0;
  return ptr;
}

void freeNativeCString(ffi.Pointer<ffi.Int8> ptr) {
  if (ptr.address != 0) _Mem.free(ptr);
}

/// 以 0 结尾的 UTF-8 缓冲 → [String]。
///
/// 读法只有一条规矩：**扫描窗口不许越过终止符**。Go 回来的缓冲长度我们不知道
/// （`C.CString` 自己 malloc 的），所以既不能按"上次那发多大"开视图，也不能按
/// 固定块整块 `indexOf`——多读到的字节可能落在未提交的页上，那是一记 access violation
/// 而不是一个异常。逐字节只走到 0 为止，然后整段一次解码。
String fromNativeCString(ffi.Pointer<ffi.Int8> ptr) {
  if (ptr.address == 0) return '';
  var len = 0;
  while (ptr[len] != 0) {
    len++;
  }
  // Go 那侧 json.Marshal 出的中文是**原样 UTF-8 字节**（不转 \uXXXX）：
  // 按 charCode 逐字节读（Latin-1）会把每个汉字拆成 3 个乱码码元，
  // 活臂读回的书名/正文全成 mojibake。这里按 UTF-8 还原。
  return utf8.decode(ptr.cast<ffi.Uint8>().asTypedList(len), allowMalformed: true);
}

// ---------------------------------------------------------------------------
// FFI client singleton
// ---------------------------------------------------------------------------

class GoFFIClient {
  static GoFFIClient? _instance;

  ffi.DynamicLibrary? _dl;
  late InitializeDart _initialize;
  late CallDart _call;
  late CleanupDart _cleanup;
  late FreeStringDart _freeString;

  bool _ready = false;

  bool get loaded => _dl != null;

  /// The resolved path to the Go engine DLL (for diagnostics).
  String get libraryPath => _findLibraryPath();

  GoFFIClient._();

  static GoFFIClient get instance {
    _instance ??= GoFFIClient._();
    return _instance!;
  }

  /// Load the Go dynamic library and resolve all FFI symbols.
  void load() {
    final libPath = _findLibraryPath();
    final resolvedExe = io.File(io.Platform.resolvedExecutable);
    final exeDir = resolvedExe.parent.path;
    final exeName = resolvedExe.path.split(io.Platform.pathSeparator).last;

    // 诊断：记录 DLL 路径到日志文件
    _writeLog('FFI: EXE=$exeName');
    _writeLog('FFI: EXE dir=$exeDir');
    _writeLog('FFI: DLL path=$libPath');
    _writeLog('FFI: DLL exists=${io.File(libPath).existsSync()}');

    if (!io.File(libPath).existsSync()) {
      throw Exception(
          'Go engine library not found at: $libPath\n'
          'EXE directory: $exeDir\n'
          'DLL should be in the same folder as $exeName\n'
          'Run build_release.py to compile it.');
    }

    try {
      _dl = ffi.DynamicLibrary.open(libPath);
      _writeLog('FFI: DLL opened successfully');
    } catch (e) {
      _writeLog('FFI: Load failed: $e');
      throw Exception('Failed to open Go FFI library: $e');
    }

    _resolveSymbols();
    _writeLog('FFI: Symbols resolved successfully');
  }

  /// Write diagnostic log to a file in the exe directory.
  void _writeLog(String message) {
    try {
      // Write to stderr (visible in terminal)
      io.stderr.writeln('[LEGODO_FFI] $message');
      // Write to log file (for later inspection)
      final logDir = io.File(io.Platform.resolvedExecutable).parent.path;
      final logFile = io.File('$logDir\\legodo_ffi.log');
      logFile.writeAsStringSync('[${DateTime.now().toIso8601String()}] $message\n', mode: io.FileMode.append);
    } catch (e) {
      // Log write failed
      io.stderr.writeln('[LEGODO_FFI] Log write failed: $e');
    }
  }

  void _resolveSymbols() {
    final dl = _dl!;

    _initialize = dl.lookupFunction<InitializeC, InitializeDart>('Initialize');
    _call = dl.lookupFunction<CallC, CallDart>('Call');
    _cleanup = dl.lookupFunction<CleanupC, CleanupDart>('Cleanup');
    _freeString = dl.lookupFunction<FreeStringC, FreeStringDart>('FreeString');
  }

  void initialize() {
    if (_ready) return;
    _initialize();
    _ready = true;
  }

  Map<String, dynamic> call(String action, Map<String, dynamic> params) {
    if (!_ready) initialize();

    final actionPtr = toNativeCString(action);
    final paramsPtr = toNativeCString(jsonEncode(params));

    try {
      final resultPtr = _call(actionPtr, paramsPtr);
      final result = fromNativeCString(resultPtr);
      if (resultPtr.address != 0) _freeString(resultPtr);
      return jsonDecode(result) as Map<String, dynamic>;
    } finally {
      freeNativeCString(actionPtr);
      freeNativeCString(paramsPtr);
    }
  }

  void cleanup() {
    if (_ready) {
      _cleanup();
      _ready = false;
    }
  }

  String _findLibraryPath() {
    // 活臂从测试进程加载（flutter test 的 exe 是 flutter_tester，DLL 不在它旁边）：
    // 用 LEGODO_ENGINE_DLL 把要测的那份指过来；生产路径仍只认 exe 目录。
    final override = io.Platform.environment['LEGODO_ENGINE_DLL'];
    if (override != null && override.isNotEmpty) return override;

    final exeDir = io.File(io.Platform.resolvedExecutable).parent.path;

    if (io.Platform.isWindows) {
      return '$exeDir\\legodo_engine.dll';
    } else if (io.Platform.isLinux) {
      return '$exeDir/liblegodo_engine.so';
    } else if (io.Platform.isMacOS) {
      return '$exeDir/liblegodo_engine.dylib';
    }

    throw UnsupportedError('Unsupported platform: ${io.Platform.operatingSystem}');
  }
}
