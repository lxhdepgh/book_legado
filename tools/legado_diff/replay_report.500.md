# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 88（成功求值 88）
- 结果分布: {"MATCH": 87, "LEGADO_ERR": 1}
- 分析器覆盖: {"jsoup": 80, "json": 8}
- 有效 MATCH（两侧均取到非空值）: 62 / 87；其余为两侧同为空的弱一致

## LEGADO_ERR (1)

- [s533] 离线用例 · 规则 `.book-list li||$.books`
  - org.jsoup.select.Selector$SelectorParseException: Could not parse query '$.books': unexpected token at '$.books'

## MATCH 明细（前 10）
- [s574] 离线用例 · jsoup · 18 条
- [s544] 离线用例 · jsoup · 70 条
- [s515] 离线用例 · json · 10 条
- [s514] 离线用例 · jsoup · 21 条
- [s575] 离线用例 · jsoup · 9 条
- [s516] 离线用例 · json · 30 条
- [s576] 离线用例 · jsoup · 10 条
- [s577] 离线用例 · jsoup · 0 条
- [s545] 离线用例 · jsoup · 0 条
- [s547] 离线用例 · jsoup · 0 条