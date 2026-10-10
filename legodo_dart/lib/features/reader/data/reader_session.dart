/// 阅读会话的两发回话：`reader.open`（会话快照）与 `reader.close`（收尾结论）。
///
/// **会话是引擎里的单例**（`ReadingService` 只有一个 `current_session`），而跨进程客户端
/// 可以带着任意 `(source_url, book_url)` 来。所以这一族的每一条都**显式吃这两个 url**，
/// 对不上当前会话就回错误信封（`reader.toc--no-session` 那份样本）或 `closed:false`——
/// 不靠隐式状态猜。界面上这意味着："我开的书"要以 `reader.open` 的回话为准，
/// 而不是以"我上一发请求过什么"为准。
///
/// 目录不挂在 [ReaderSession] 上（几千章不能挂在"点开一本书"的回包 size 上），
/// 另走 `chapterRows`。
library;

import '../../find_books/data/book_row.dart';
import 'reading_position.dart';
import 'staged_record.dart';

/// `reader.open` 的回话：会话建起来了，这是它的位置与快照。
class ReaderSession {
  const ReaderSession({
    required this.book,
    required this.sourceUrl,
    required this.bookUrl,
    required this.chapterCount,
    required this.position,
  });

  /// 服务端按 `_book_item_dict` 归一的书快照（与 `browse.books`/搜索结果**同一套键**，
  /// 所以复用 [BookRow]）。它的 [BookRow.raw] 就是下一次 `reader.open`
  /// 与 `bookshelf.add` 要原样传回的那份字典。
  final BookRow book;

  /// 会话实际落在哪条源上——**这是服务端认的那一条**，不是请求里传的那一条
  /// （换源之后它会变）。后续每一发的 `source_url` 用这一格。
  final String sourceUrl;
  final String bookUrl;

  /// 目录条数（`reader.toc` 拿到的行数应当与它一致；不一致就是投影那边丢了行）。
  final int chapterCount;

  /// 会话当前的续读位置（`resume:false` 时它是默认那一份：第 0 章、偏移 0）。
  final ReadingPosition position;

  factory ReaderSession.fromJson(Map<String, Object?> json) {
    final bookNode = json['book'];
    final positionNode = json['position'];
    if (bookNode is! Map) {
      throw ArgumentError('reader.open 的 book 不是对象（收到 ${bookNode.runtimeType}）');
    }
    if (positionNode is! Map) {
      throw ArgumentError(
          'reader.open 的 position 不是对象（收到 ${positionNode.runtimeType}）：'
          '这一格服务端恒给一份形状，缺了它就是形状变了，不是"没有进度"');
    }
    return ReaderSession(
      book: BookRow.fromJson(Map<String, Object?>.from(bookNode)),
      sourceUrl: '${json['sourceUrl'] ?? ''}',
      bookUrl: '${json['bookUrl'] ?? ''}',
      chapterCount: (json['chapterCount'] as num?)?.toInt() ?? 0,
      position: ReadingPosition.fromJson(Map<String, Object?>.from(positionNode)),
    );
  }

  @override
  String toString() => 'ReaderSession($bookUrl, $chapterCount 章, '
      '停在第 ${position.chapterIndex} 章 ${position.characterOffset} 字)';
}

/// `reader.close` 的回话。
class CloseOutcome {
  const CloseOutcome({required this.closed, required this.staged});

  /// url 对不上当前会话时是 `false`，而且**什么都不动**（协议面那条承诺：
  /// 关错书不能把 A 的进度落到 B 那一行，`books` 是 `ON CONFLICT DO UPDATE`）。
  /// 界面不能把它读成"关失败了"——本来就是"不该我关"。
  final bool closed;

  /// 需要收尾询问的那条暂存记录；`null` = 不用问（在架的书、或根本没开会话）。
  ///
  /// 它的形状与 `reader.staged` **是同一份**（同一条数据库记录）。G3-C2 之前这里
  /// 走的是服务端 `_book_item_dict` 的 getattr 兜底分支、回一整份"键齐全但全空"的壳，
  /// 界面上表现为退出试读时那句"《》放入书架"——现在由
  /// `tests/test_reader_rpc_surface.py` 那条同形状守卫钉住（语料里四发 close 的
  /// `staged` 全是 null，那一支在黄金样本里没有证人）。
  final StagedRecord? staged;

  factory CloseOutcome.fromJson(Map<String, Object?> json) {
    final staged = json['staged'];
    return CloseOutcome(
      closed: json['closed'] == true,
      staged: staged is Map
          ? StagedRecord.fromJson(Map<String, Object?>.from(staged))
          : null,
    );
  }

  @override
  String toString() =>
      'CloseOutcome(closed=$closed${staged == null ? '' : ', 待询问'})';
}
