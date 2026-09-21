# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 92）
- 结果分布: {"MATCH": 90, "LEGADO_ERR": 1, "FORMAT_DIFF": 1}
- 分析器覆盖: {"jsoup": 82, "json": 9, "xpath": 1}
- 有效 MATCH（两侧均取到非空值）: 69 / 90；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s672] 🎃看脸啊🎃 · 规则 `//div[@class='catagory-list']/ul/li`
  - 第 0 条起不一致
  - python: ["<li><span>2022-06-10</span> <a href=\"https://dwkm.xyz/school/13602.html\" title=\"我的淫蕩女友（淫蕩学姐瑶瑶）\" target=\"_blank\">我的淫蕩女友（淫蕩学姐瑶瑶）</a> </li>", "<li><span>2022-05-20</span> <a href=\"https://dwkm.xyz/family/13573.html\" title=\"我的第ㄧ次网路出轨\" target=\"_blank\">我的第ㄧ次网路出轨</a> </li>"]
  - legado: ["<li><span>2022-06-10</span> <a href=\"https://dwkm.xyz/school/13602.html\" title=\"我的淫蕩女友（淫蕩学姐瑶瑶）\" target=\"_blank\">我的淫蕩女友（淫蕩学姐瑶瑶）</a></li>", "<li><span>2022-05-20</span> <a href=\"https://dwkm.xyz/family/13573.html\" title=\"我的第ㄧ次网路出轨\" target=\"_blank\">我的第ㄧ次网路出轨</a></li>"]

## LEGADO_ERR (1)

- [s616] 漫蛙可直连 · 规则 `.book-list li||$.books`
  - org.jsoup.select.Selector$SelectorParseException: Could not parse query '$.books': unexpected token at '$.books'

## MATCH 明细（前 10）
- [s613] MANHUA · jsoup · 10 条
- [s643] 漫小肆韩漫 · jsoup · 20 条
- [s614] 🎃我的书阁🎃 · jsoup · 0 条
- [s615] 海马书屋 · jsoup · 18 条
- [s644] 🎃猩猩小说🎃 · jsoup · 80 条
- [s645] 🎃365小说网🎃#2 · jsoup · 50 条
- [s675] 🌐 书海阁网 · jsoup · 20 条
- [s673] 🎃酷书阁小说网🎃 · jsoup · 20 条
- [s676] 🎃一流书院🎃 · jsoup · 20 条
- [s647] ❤️笔趣阁🎃 · jsoup · 49 条