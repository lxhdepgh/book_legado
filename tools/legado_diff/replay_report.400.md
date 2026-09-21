# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 82（成功求值 82）
- 结果分布: {"MATCH": 81, "FORMAT_DIFF": 1}
- 分析器覆盖: {"json": 19, "jsoup": 62, "xpath": 1}
- 有效 MATCH（两侧均取到非空值）: 70 / 81；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s448] 离线用例 · 规则 `//h2`
  - 第 0 条起不一致
  - python: ["<html:h2 xmlns:html=\"http://www.w3.org/1999/xhtml\" class=\"r\"><html:span>1.</html:span> <html:a class=\"l\" href=\"/chuanyuechongsheng/35815.html\" target=\"_blank\">你是我的快雪时晴</html:a> <html:span style=\"font-size:12px;\">萨摩野</html:span></html:h2>", "<html:h2 xmlns:html=\"http://www.w3.org/1999/
  - legado: ["<h2 class=\"r\"><span>1.</span> <a class=\"l\" href=\"/chuanyuechongsheng/35815.html\" target=\"_blank\">你是我的快雪时晴</a> <span style=\"font-size:12px;\">萨摩野</span></h2>", "<h2 class=\"r\"><span>2.</span> <a class=\"l\" href=\"/chuanyuechongsheng/35813.html\" target=\"_blank\">穿书后我遇见了我的阳光</a> <span st

## MATCH 明细（前 10）
- [s444] 离线用例 · json · 20 条
- [s474] 离线用例 · jsoup · 100 条
- [s475] 离线用例 · jsoup · 50 条
- [s445] 离线用例 · jsoup · 20 条
- [s476] 离线用例 · jsoup · 10 条
- [s417] 离线用例 · jsoup · 13 条
- [s447] 离线用例 · jsoup · 3 条
- [s446] 离线用例 · jsoup · 4223 条
- [s478] 离线用例 · jsoup · 20 条
- [s415] 离线用例 · jsoup · 50 条