# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 89（成功求值 89）
- 结果分布: {"MATCH": 87, "LEGADO_ERR": 1, "FORMAT_DIFF": 1}
- 分析器覆盖: {"jsoup": 73, "json": 13, "xpath": 3}
- 有效 MATCH（两侧均取到非空值）: 67 / 87；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s936] 离线用例 · 规则 `//div[@class="morelist"]/ul/li`
  - 第 0 条起不一致
  - python: ["<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class=\"date\">1. </html:span><html:a href=\"/booknv/\">女生小说</html:a> / <html:a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</html:a> </html:li>", "<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class
  - legado: ["<li><span class=\"date\">1. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</a></li>", "<li><span class=\"date\">2. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118729.html\" target=\"_blank\"> 我的药膳能治百病(柳十天)</a></li>"]

## LEGADO_ERR (1)

- [s916] 离线用例 · 规则 `.book-list li||$.books`
  - org.jsoup.select.Selector$SelectorParseException: Could not parse query '$.books': unexpected token at '$.books'

## MATCH 明细（前 10）
- [s943] 离线用例 · jsoup · 1 条
- [s914] 离线用例 · jsoup · 15 条
- [s944] 离线用例 · jsoup · 36 条
- [s974] 离线用例 · jsoup · 26 条
- [s915] 离线用例 · jsoup · 10 条
- [s973] 离线用例 · jsoup · 18 条
- [s945] 离线用例 · jsoup · 10 条
- [s946] 离线用例 · jsoup · 20 条
- [s975] 离线用例 · jsoup · 200 条
- [s976] 离线用例 · jsoup · 10 条