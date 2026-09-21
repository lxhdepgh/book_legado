# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 86）
- 结果分布: {"MATCH": 84, "FORMAT_DIFF": 1, "COUNT_DIFF": 1}
- 分析器覆盖: {"json": 14, "jsoup": 70, "xpath": 2}
- 有效 MATCH（两侧均取到非空值）: 71 / 84；其余为两侧同为空的弱一致

## COUNT_DIFF (1)

- [s798] ⚡思路客1 · 规则 `class.slide-item list1@children`
  - py=0 legado=50
  - python: []
  - legado: ["1 末日成神：我的我的我的都是我的异能 作者：钱真人 连载 | 最近更新： 第69章 魔师 01-06", "2 我的火影我的路 作者：残城凌梦 连载 | 最近更新： 第155章 宇智波止水…… 05-19"]

## FORMAT_DIFF (1)

- [s745] Nhentai · 规则 `//div[contains(@class,'container')]//a[contains(@class,'group')]`
  - 第 0 条起不一致
  - python: ["<a class=\"group mx-auto w-full max-w-[300px] overflow-hidden rounded-lg transition-opacity duration-300 ease-in-out hover:opacity-70\" href=\"/articles/2485469\"><div class=\"relative mx-auto aspect-[2/3]\"><img class=\"h-full w-full object-cover\" alt=\"姐姐带我转大人, 中文\" src=\"https://cdn.imagedeliv
  - legado: ["<a class=\"group mx-auto w-full max-w-[300px] overflow-hidden rounded-lg transition-opacity duration-300 ease-in-out hover:opacity-70\" href=\"/articles/2485469\"> <div class=\"relative mx-auto aspect-[2/3]\"> <img class=\"h-full w-full object-cover\" alt=\"姐姐带我转大人, 中文\" src=\"https://cdn.imagedel

## MATCH 明细（前 10）
- [s773] 移动阅读 · json · 10 条
- [s713] 🎃混混小说网🎃 · jsoup · 1 条
- [s743] 🎃去看小说网🎃 · jsoup · 20 条
- [s744] 🎨漫客栈 #Haxc1107 · jsoup · 30 条
- [s714] 品书楼 · jsoup · 20 条
- [s716] 🎃第一版主🎃#13 · jsoup · 20 条
- [s775] 🌟去看小说网 · jsoup · 20 条
- [s776] ❤️百度汉语 · jsoup · 23 条
- [s774] ❤️衍墨轩🎃 · jsoup · 20 条
- [s746] 蜻蜓FM · jsoup · 100 条