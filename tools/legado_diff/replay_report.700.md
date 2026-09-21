# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 86（成功求值 86）
- 结果分布: {"MATCH": 85, "FORMAT_DIFF": 1}
- 分析器覆盖: {"json": 14, "jsoup": 70, "xpath": 2}
- 有效 MATCH（两侧均取到非空值）: 72 / 85；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s745] 离线用例 · 规则 `//div[contains(@class,'container')]//a[contains(@class,'group')]`
  - 第 0 条起不一致
  - python: ["<a class=\"group mx-auto w-full max-w-[300px] overflow-hidden rounded-lg transition-opacity duration-300 ease-in-out hover:opacity-70\" href=\"/articles/2485469\"><div class=\"relative mx-auto aspect-[2/3]\"><img class=\"h-full w-full object-cover\" alt=\"姐姐带我转大人, 中文\" src=\"https://cdn.imagedeliv
  - legado: ["<a class=\"group mx-auto w-full max-w-[300px] overflow-hidden rounded-lg transition-opacity duration-300 ease-in-out hover:opacity-70\" href=\"/articles/2485469\"> <div class=\"relative mx-auto aspect-[2/3]\"> <img class=\"h-full w-full object-cover\" alt=\"姐姐带我转大人, 中文\" src=\"https://cdn.imagedel

## MATCH 明细（前 10）
- [s773] 离线用例 · json · 10 条
- [s713] 离线用例 · jsoup · 1 条
- [s743] 离线用例 · jsoup · 20 条
- [s744] 离线用例 · jsoup · 30 条
- [s714] 离线用例 · jsoup · 20 条
- [s716] 离线用例 · jsoup · 20 条
- [s775] 离线用例 · jsoup · 20 条
- [s776] 离线用例 · jsoup · 23 条
- [s774] 离线用例 · jsoup · 20 条
- [s746] 离线用例 · jsoup · 100 条