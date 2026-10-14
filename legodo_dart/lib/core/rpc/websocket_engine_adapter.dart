/// RPC 引擎仓库的 WebSocket 适配器。
///
/// 将现有的 EngineRepository 接口适配到 Go 后端的 WebSocket RPC。
library;

import '../../features/bookshelf/data/bookshelf_item.dart';
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/sources/data/source_record.dart';
import '../rpc/websocket_rpc_client.dart';
import 'engine_repository.dart';

class WebSocketEngineAdapter implements EngineRepository {
  WebSocketEngineAdapter(this.wsClient);

  final WebSocketRpcClient wsClient;

  @override
  Future<void> hello() async {
    await wsClient.connect();
    // Go 后端没有 engine.hello，跳过握手
  }

  @override
  Future<List<BookRow>> searchBooks({
    required Map<String, dynamic> source,
    required String keyword,
  }) async {
    final response = await wsClient.call(
      'search_books',
      payload: {
        'source': source,
        'keyword': keyword,
      },
    );

    if (response is List) {
      return response.map((item) => BookRow.fromJson(item)).toList();
    }
    
    final data = response['data'];
    if (data is List) {
      return data.map((item) => BookRow.fromJson(item)).toList();
    }
    
    return [];
  }

  @override
  Future<Map<String, dynamic>> getBookInfo({
    required Map<String, dynamic> source,
    required String bookUrl,
  }) async {
    final response = await wsClient.call(
      'get_book_info',
      payload: {
        'source': source,
        'bookUrl': bookUrl,
      },
    );

    return response;
  }

  @override
  Future<List<Map<String, dynamic>>> getChapters({
    required Map<String, dynamic> source,
    required String tocUrl,
  }) async {
    final response = await wsClient.call(
      'get_chapters',
      payload: {
        'source': source,
        'tocUrl': tocUrl,
      },
    );

    if (response is List) {
      return response.cast<Map<String, dynamic>>();
    }
    
    final data = response['data'];
    if (data is List) {
      return data.cast<Map<String, dynamic>>();
    }
    
    return [];
  }

  @override
  Future<Map<String, dynamic>> getContent({
    required Map<String, dynamic> source,
    required Map<String, dynamic> chapter,
  }) async {
    final response = await wsClient.call(
      'get_content',
      payload: {
        'source': source,
        'chapter': chapter,
      },
    );

    return response;
  }

  @override
  Future<CheckResult> checkSource({
    required Map<String, dynamic> source,
  }) async {
    final response = await wsClient.call(
      'check_source',
      payload: {
        'source': source,
      },
    );

    return CheckResult.fromJson(response);
  }

  // ========== 其他方法使用默认实现（抛出未实现错误）==========

  @override
  Future<T> call<T>(String action, {Map<String, Object?>? payload}) {
    throw UnimplementedError('Action $action not implemented in WebSocket adapter');
  }

  @override
  Stream<dynamic> stream(String action, {Map<String, Object?>? payload}) {
    throw UnimplementedError('Streaming not supported in WebSocket adapter');
  }

  @override
  Future<void> dispose() async {
    await wsClient.disconnect();
  }
}
