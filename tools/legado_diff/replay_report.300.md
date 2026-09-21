# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 87（成功求值 87）
- 结果分布: {"MATCH": 86, "FORMAT_DIFF": 1}
- 分析器覆盖: {"jsoup": 70, "json": 15, "xpath": 2}
- 有效 MATCH（两侧均取到非空值）: 65 / 86；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s319] 离线用例 · 规则 `//div[@class="morelist"]/ul/li`
  - 第 0 条起不一致
  - python: ["<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class=\"date\">1. </html:span><html:a href=\"/booknv/\">女生小说</html:a> / <html:a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</html:a> </html:li>", "<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class
  - legado: ["<li><span class=\"date\">1. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</a></li>", "<li><span class=\"date\">2. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118729.html\" target=\"_blank\"> 我的药膳能治百病(柳十天)</a></li>"]

## MATCH 明细（前 10）
- [s314] 离线用例 · jsoup · 20 条
- [s374] 离线用例 · jsoup · 50 条
- [s345] 离线用例 · jsoup · 0 条
- [s316] 离线用例 · jsoup · 0 条
- [s376] 离线用例 · json · 10 条
- [s375] 离线用例 · jsoup · 20 条
- [s377] 离线用例 · json · 1 条
- [s317] 离线用例 · jsoup · 27 条
- [s347] 离线用例 · jsoup · 13 条
- [s318] 离线用例 · jsoup · 20 条