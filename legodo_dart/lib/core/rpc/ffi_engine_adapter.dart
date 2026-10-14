/// FFI 引擎适配器（实现 EngineRepository 接口）
///
/// 通过 dart:ffi 直接调用 Go 动态库，替代原有的 WebSocket RPC 通信。
/// 零网络延迟，单进程架构。
library;

import '../ffi/engine_ffi.dart';
import '../rpc/engine_repository.dart';
import '../../features/bookshelf/data/bookshelf_item.dart';
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/find_books/data/category_row.dart';
import '../../features/find_books/data/search_scope.dart';
import '../../features/reader/data/bookmark_row.dart';
import '../../features/reader/data/chapter_document.dart';
import '../../features/reader/data/chapter_row.dart';
import '../../features/reader/data/content_search_result.dart';
import '../../features/reader/data/reading_position.dart';
import '../../features/reader/data/reader_session.dart';
import '../../features/reader/data/staged_record.dart';
import '../../features/rss/data/rss_models.dart';
import '../../features/settings/data/theme_preferences.dart';
import '../../features/settings/data/update_models.dart';
import '../../features/sources/data/import_outcome.dart';
import '../../features/sources/data/source_audit.dart';
import '../../features/sources/data/source_record.dart';
import '../../features/sources/data/source_switch_models.dart';
import '../../core/events/engine_event.dart';
import '../../core/settings/settings_map.dart';
import '../../core/rpc/engine_status.dart';
import '../../core/rpc/envelope.dart';

class FFIEngineAdapter implements EngineRepository {
  final EngineFFIClient _ffi = EngineFFIClient.instance;
  bool _connected = false;

  @override
  Future<void> connect() async {
    try {
      _ffi.loadLibrary();
      _ffi.initialize();
      _connected = true;
    } catch (e) {
      throw EngineFailure(
        EngineFailureKind.transport,
        'Failed to load Go engine library: $e',
        action: 'connect',
      );
    }
  }

  @override
  Future<void> disconnect() async {
    _ffi.cleanup();
    _connected = false;
  }

  @override
  bool get isConnected => _connected;

  // ============================================================================
  // 核心搜索功能
  // ============================================================================

  @override
  Future<List<BookRow>> searchBooks({
    required Map<String, dynamic> source,
    required String keyword,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.searchBooks(source, keyword);
      
      if (result['success'] == true) {
        final data = result['data'] as List;
        return data.map((item) => BookRow.fromJson(item)).toList();
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'search_books',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Search failed: $e',
        action: 'search_books',
      );
    }
  }

  @override
  Future<Map<String, dynamic>> getBookInfo({
    required Map<String, dynamic> source,
    required String bookUrl,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.getBookInfo(source, bookUrl);
      
      if (result['success'] == true) {
        return result['data'];
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'get_book_info',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Get book info failed: $e',
        action: 'get_book_info',
      );
    }
  }

  @override
  Future<List<Map<String, dynamic>>> getChapters({
    required Map<String, dynamic> source,
    required String bookUrl,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.getChapters(source, bookUrl);
      
      if (result['success'] == true) {
        final data = result['data'] as List;
        return data.cast<Map<String, dynamic>>();
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'get_chapters',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Get chapters failed: $e',
        action: 'get_chapters',
      );
    }
  }

  @override
  Future<String> getContent({
    required Map<String, dynamic> source,
    required String chapterUrl,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.getContent(source, chapterUrl);
      
      if (result['success'] == true) {
        return result['data'] as String;
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'get_content',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Get content failed: $e',
        action: 'get_content',
      );
    }
  }

  @override
  Future<CheckResult> checkSource(Map<String, dynamic> source) async {
    _ensureConnected();
    
    try {
      final result = _ffi.checkSource(source);
      
      if (result['success'] == true) {
        return CheckResult(
          valid: result['valid'] as bool,
          error: result['error'],
        );
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'check_source',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Check source failed: $e',
        action: 'check_source',
      );
    }
  }

  // ============================================================================
  // JS 执行与 HTML 解析
  // ============================================================================

  @override
  Future<Map<String, dynamic>> executeJS({
    required String jsCode,
    required Map<String, dynamic> context,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.executeJS(jsCode, context);
      
      if (result['success'] == true) {
        return result['data'];
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'execute_js',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Execute JS failed: $e',
        action: 'execute_js',
      );
    }
  }

  @override
  Future<List<String>> parseHTML({
    required String html,
    required String selector,
  }) async {
    _ensureConnected();
    
    try {
      final result = _ffi.parseHTML(html, selector);
      
      if (result['success'] == true) {
        final data = result['data'] as List;
        return data.cast<String>();
      } else {
        throw EngineFailure(
          EngineFailureKind.action,
          result['error'] ?? 'Unknown error',
          action: 'parse_html',
        );
      }
    } catch (e) {
      if (e is EngineFailure) rethrow;
      throw EngineFailure(
        EngineFailureKind.transport,
        'Parse HTML failed: $e',
        action: 'parse_html',
      );
    }
  }

  // ============================================================================
  // 未实现的方法（保留接口兼容性）
  // ============================================================================

  void _ensureConnected() {
    if (!_connected) {
      throw EngineFailure(
        EngineFailureKind.transport,
        'FFI engine not connected',
        action: 'unknown',
      );
    }
  }

  // 以下方法在 FFI 模式下暂不支持，抛出未实现错误
  
  @override
  Stream<EngineEvent> subscribeEvents() {
    throw UnimplementedError('Event subscription not supported in FFI mode');
  }

  @override
  Future<GatewayInfo> hello() async {
    throw UnimplementedError('Hello handshake not needed in FFI mode');
  }

  @override
  Future<EngineStatus> getStatus() async {
    return EngineStatus(
      connected: _connected,
      version: 'ffi',
      build: 'go-ffi',
    );
  }

  @override
  Future<Map<String, dynamic>> call(String action, {Map<String, dynamic>? payload}) {
    throw UnimplementedError('Generic RPC call not supported in FFI mode');
  }

  // 其他未实现方法（保持接口完整）
  @override
  Future<void> addBookshelfItem(BookshelfItem item) => throw UnimplementedError();
  
  @override
  Future<List<BookshelfItem>> listBookshelfItems() => throw UnimplementedError();
  
  @override
  Future<void> removeBookshelfItem(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<void> updateBookshelfItem(BookshelfItem item) => throw UnimplementedError();
  
  @override
  Future<void> importSources(List<SourceRecord> sources) => throw UnimplementedError();
  
  @override
  Future<List<SourceRecord>> listSources() => throw UnimplementedError();
  
  @override
  Future<void> removeSource(String sourceUrl) => throw UnimplementedError();
  
  @override
  Future<ImportOutcome> exportSources(List<String> sourceUrls) => throw UnimplementedError();
  
  @override
  Future<void> saveSettings(SettingsMap settings) => throw UnimplementedError();
  
  @override
  Future<SettingsMap> loadSettings() => throw UnimplementedError();
  
  @override
  Future<ThemePreferences> getThemePreferences() => throw UnimplementedError();
  
  @override
  Future<void> saveThemePreferences(ThemePreferences prefs) => throw UnimplementedError();
  
  @override
  Future<UpdateCheckResult> checkForUpdates(String currentVersion) => throw UnimplementedError();
  
  @override
  Future<void> markUpdateSeen(String version) => throw UnimplementedError();
  
  @override
  Future<RssFeed> addRssFeed(RssFeed feed) => throw UnimplementedError();
  
  @override
  Future<List<RssFeed>> listRssFeeds() => throw UnimplementedError();
  
  @override
  Future<void> removeRssFeed(String feedUrl) => throw UnimplementedError();
  
  @override
  Future<RssRefreshProgressEvent> refreshRssFeeds() => throw UnimplementedError();
  
  @override
  Future<List<RssItem>> listRssItems({String? feedUrl}) => throw UnimplementedError();
  
  @override
  Future<void> clearRssItems() => throw UnimplementedError();
  
  @override
  Future<void> saveBookmark(BookmarkRow bookmark) => throw UnimplementedError();
  
  @override
  Future<List<BookmarkRow>> listBookmarks(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<void> removeBookmark(int id) => throw UnimplementedError();
  
  @override
  Future<void> saveReadingPosition(ReadingPosition position) => throw UnimplementedError();
  
  @override
  Future<ReadingPosition?> loadReadingPosition(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<void> saveReaderSession(ReaderSession session) => throw UnimplementedError();
  
  @override
  Future<ReaderSession?> loadReaderSession(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<void> removeReaderSession(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<void> saveStagedRecord(StagedRecord record) => throw UnimplementedError();
  
  @override
  Future<List<StagedRecord>> listStagedRecords() => throw UnimplementedError();
  
  @override
  Future<void> removeStagedRecord(String id) => throw UnimplementedError();
  
  @override
  Future<void> clearStagedRecords() => throw UnimplementedError();
  
  @override
  Future<ChapterDocument> getChapterContent(String bookUrl, int chapterIndex) => throw UnimplementedError();
  
  @override
  Future<List<ChapterRow>> listChaptersForBook(String bookUrl) => throw UnimplementedError();
  
  @override
  Future<ContentSearchResult> searchInBook({
    required String bookUrl,
    required String query,
    int? limit,
  }) => throw UnimplementedError();
  
  @override
  Future<List<CategoryRow>> listCategories() => throw UnimplementedError();
  
  @override
  Future<SearchScope> getSearchScope() => throw UnimplementedError();
  
  @override
  Future<void> startCheck({
    required List<Map<String, dynamic>> sources,
    required String mode,
  }) => throw UnimplementedError();
  
  @override
  Future<void> stopCheck() => throw UnimplementedError();
  
  @override
  Future<void> startSearch({
    required List<Map<String, dynamic>> sources,
    required String keyword,
    required String mode,
  }) => throw UnimplementedError();
  
  @override
  Future<void> stopSearch() => throw UnimplementedError();
  
  @override
  Future<void> startSourceSwitch({
    required Map<String, dynamic> oldSource,
    required List<Map<String, dynamic>> newSources,
    required String bookUrl,
  }) => throw UnimplementedError();
  
  @override
  Future<void> stopSourceSwitch() => throw UnimplementedError();
  
  @override
  Future<SourceSwitchScanResult> scanSourceSwitch({
    required Map<String, dynamic> source,
    required String bookUrl,
  }) => throw UnimplementedError();
  
  @override
  Future<List<SourceAudit>> listSourceAudits({int? limit}) => throw UnimplementedError();
  
  @override
  Future<void> clearSourceAudits() => throw UnimplementedError();
}
