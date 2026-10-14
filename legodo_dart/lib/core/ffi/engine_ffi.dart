/// Go 引擎 FFI 绑定（动态库模式）
///
/// 通过 dart:ffi 直接调用 Go 编译的动态库，无需网络通信。
/// 支持 Windows (.dll)、Linux (.so)、macOS (.dylib)。
library;

import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io';

import 'package:ffi/ffi.dart';

/// FFI 函数签名定义
typedef InitializeFunc = ffi.Void Function();
typedef FreeStringFunc = ffi.Void Function(ffi.Pointer<ffi.Char>);
typedef SearchBooksFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetBookInfoFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetChaptersFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetContentFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef CheckSourceFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>);
typedef ExecuteJSFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef ParseHTMLFunc = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef CleanupFunc = ffi.Void Function();

/// Dart 层函数类型
typedef InitializeDart = void Function();
typedef FreeStringDart = void Function(ffi.Pointer<ffi.Char>);
typedef SearchBooksDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetBookInfoDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetChaptersDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef GetContentDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef CheckSourceDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>);
typedef ExecuteJSDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef ParseHTMLDart = ffi.Pointer<ffi.Char> Function(
    ffi.Pointer<ffi.Char>, ffi.Pointer<ffi.Char>);
typedef CleanupDart = void Function();

/// Go 引擎 FFI 客户端
class EngineFFIClient {
  static EngineFFIClient? _instance;
  late ffi.DynamicLibrary _dl;
  late InitializeDart _initialize;
  late FreeStringDart _freeString;
  late SearchBooksDart _searchBooks;
  late GetBookInfoDart _getBookInfo;
  late GetChaptersDart _getChapters;
  late GetContentDart _getContent;
  late CheckSourceDart _checkSource;
  late ExecuteJSDart _executeJS;
  late ParseHTMLDart _parseHTML;
  late CleanupDart _cleanup;

  bool _initialized = false;

  EngineFFIClient._();

  /// 获取单例
  static EngineFFIClient get instance {
    _instance ??= EngineFFIClient._();
    return _instance!;
  }

  /// 加载动态库并绑定函数
  void loadLibrary() {
    final libPath = _findLibraryPath();
    
    if (!File(libPath).existsSync()) {
      throw Exception('Go engine library not found at: $libPath');
    }

    try {
      _dl = ffi.DynamicLibrary.open(libPath);
    } catch (e) {
      throw Exception('Failed to load Go engine library: $e');
    }

    // 绑定函数
    _initialize = _dl.lookupFunction<InitializeFunc, InitializeDart>('Initialize');
    _freeString = _dl.lookupFunction<FreeStringFunc, FreeStringDart>('FreeString');
    _searchBooks = _dl.lookupFunction<SearchBooksFunc, SearchBooksDart>('SearchBooks');
    _getBookInfo = _dl.lookupFunction<GetBookInfoFunc, GetBookInfoDart>('GetBookInfo');
    _getChapters = _dl.lookupFunction<GetChaptersFunc, GetChaptersDart>('GetChapters');
    _getContent = _dl.lookupFunction<GetContentFunc, GetContentDart>('GetContent');
    _checkSource = _dl.lookupFunction<CheckSourceFunc, CheckSourceDart>('CheckSource');
    _executeJS = _dl.lookupFunction<ExecuteJSFunc, ExecuteJSDart>('ExecuteJS');
    _parseHTML = _dl.lookupFunction<ParseHTMLFunc, ParseHTMLDart>('ParseHTML');
    _cleanup = _dl.lookupFunction<CleanupFunc, CleanupDart>('Cleanup');
  }

  /// 初始化引擎（幂等）
  void initialize() {
    if (!_initialized) {
      _initialize();
      _initialized = true;
    }
  }

  /// 搜索书籍
  Map<String, dynamic> searchBooks(Map<String, dynamic> source, String keyword) {
    _ensureInitialized();
    
    final sourceJson = jsonEncode(source);
    final sourcePtr = sourceJson.toNativeUtf8().cast<ffi.Char>();
    final keywordPtr = keyword.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _searchBooks(sourcePtr, keywordPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(sourcePtr);
      calloc.free(keywordPtr);
    }
  }

  /// 获取书籍详情
  Map<String, dynamic> getBookInfo(Map<String, dynamic> source, String bookUrl) {
    _ensureInitialized();
    
    final sourceJson = jsonEncode(source);
    final sourcePtr = sourceJson.toNativeUtf8().cast<ffi.Char>();
    final urlPtr = bookUrl.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _getBookInfo(sourcePtr, urlPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(sourcePtr);
      calloc.free(urlPtr);
    }
  }

  /// 获取章节目录
  Map<String, dynamic> getChapters(Map<String, dynamic> source, String bookUrl) {
    _ensureInitialized();
    
    final sourceJson = jsonEncode(source);
    final sourcePtr = sourceJson.toNativeUtf8().cast<ffi.Char>();
    final urlPtr = bookUrl.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _getChapters(sourcePtr, urlPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(sourcePtr);
      calloc.free(urlPtr);
    }
  }

  /// 获取章节内容
  Map<String, dynamic> getContent(Map<String, dynamic> source, String chapterUrl) {
    _ensureInitialized();
    
    final sourceJson = jsonEncode(source);
    final sourcePtr = sourceJson.toNativeUtf8().cast<ffi.Char>();
    final urlPtr = chapterUrl.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _getContent(sourcePtr, urlPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(sourcePtr);
      calloc.free(urlPtr);
    }
  }

  /// 检查书源
  Map<String, dynamic> checkSource(Map<String, dynamic> source) {
    _ensureInitialized();
    
    final sourceJson = jsonEncode(source);
    final sourcePtr = sourceJson.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _checkSource(sourcePtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(sourcePtr);
    }
  }

  /// 执行 JavaScript
  Map<String, dynamic> executeJS(String jsCode, Map<String, dynamic> context) {
    _ensureInitialized();
    
    final contextJson = jsonEncode(context);
    final jsPtr = jsCode.toNativeUtf8().cast<ffi.Char>();
    final ctxPtr = contextJson.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _executeJS(jsPtr, ctxPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(jsPtr);
      calloc.free(ctxPtr);
    }
  }

  /// 解析 HTML
  Map<String, dynamic> parseHTML(String html, String selector) {
    _ensureInitialized();
    
    final htmlPtr = html.toNativeUtf8().cast<ffi.Char>();
    final selPtr = selector.toNativeUtf8().cast<ffi.Char>();
    
    try {
      final resultPtr = _parseHTML(htmlPtr, selPtr);
      final result = resultPtr.cast<Utf8>().toDartString();
      _freeString(resultPtr);
      return jsonDecode(result);
    } finally {
      calloc.free(htmlPtr);
      calloc.free(selPtr);
    }
  }

  /// 清理资源
  void cleanup() {
    if (_initialized) {
      _cleanup();
      _initialized = false;
    }
  }

  void _ensureInitialized() {
    if (!_initialized) {
      initialize();
    }
  }

  String _findLibraryPath() {
    final exeDir = File(Platform.resolvedExecutable).parent.path;
    
    if (Platform.isWindows) {
      return '$exeDir\\legodo_engine.dll';
    } else if (Platform.isLinux) {
      return '$exeDir/liblegodo_engine.so';
    } else if (Platform.isMacOS) {
      return '$exeDir/liblegodo_engine.dylib';
    }
    
    throw UnsupportedError('Unsupported platform: ${Platform.operatingSystem}');
  }
}
