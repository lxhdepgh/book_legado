/// 每一条动作的**客户端预算**（§4.6：一个 action 一对预算，服务端一条、客户端一条）。
///
/// 为什么要有这张表：客户端原来只有一条 30s 的通用水池，而服务端那张表里有十条
/// 动作的预算比它长（导出/批量/导入/单源检测/开书与取章/书内全文检索）。两条预算
/// 谁先响都行，但**客户端先响**
/// 的那一刻，用户看到的是"超时"，而引擎还在后面把活干完并落库——
/// 于是"导入失败"的屏底下其实导入了。那一发不是超时，是我们没等到。
///
/// 口径：客户端 = 服务端预算 + 2s 余量（余量是给线上往返的，不是给业务用的）。
/// 表里**只列服务端预算超过 30s 的那些**，其余走 [RpcClient.timeout] 那条默认水池；
/// 这条"只列超出的"不许漂——`tests/test_gateway_contract_single_source.py`
/// 拿 `app/gateway/methods.py` 的 timeout 列比对：漏一条长的就是红。
library;

/// `action` → 服务端预算（秒）。数值抄的是那张表，不是这里定的。
const Map<String, int> kServerBudgetSeconds = <String, int>{
  'source.export': 60,
  'source.batch_enable': 60,
  'source.batch_delete': 60,
  'import.text': 120,
  'import.url': 120,
  'check.source': 60,
  // 阅读面那四发：开一本新书要载目录（几百章的网络往返），取一章正文要真去抓那一页，
  // 书内全文检索更是要先把已缓存的章扫进索引。三条都远超默认水池，
  // 客户端先到点就等于用户看着"超时"而引擎还在后面把活干完。
  'reader.open': 60,
  'reader.toc': 60,
  'reader.content': 60,
  'reader.search_content': 120,
};

/// 线上往返余量。
const Duration _slack = Duration(seconds: 2);

/// 这一发的客户端预算；表外的动作回 null，调用方用自己的默认水池。
Duration? clientBudgetFor(String action) {
  final serverSeconds = kServerBudgetSeconds[action];
  return serverSeconds == null ? null : Duration(seconds: serverSeconds) + _slack;
}
