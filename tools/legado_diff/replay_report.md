# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 86（成功求值 86）
- 结果分布: {"MATCH": 85, "FORMAT_DIFF": 1}
- 分析器覆盖: {"json": 22, "jsoup": 63, "xpath": 1}
- 有效 MATCH（两侧均取到非空值）: 58 / 85；其余为两侧同为空的弱一致

## FORMAT_DIFF (1)

- [s2] 离线用例 · 规则 `//div[@class='list-group']/div[@class='list-group-item']`
  - 第 0 条起不一致
  - python: ["<div class=\"list-group-item\"> <h5> <a href=\"/novel/32844.html\" target=\"_blank\">1. 我的<em>美母</em>人妻(<em>美母</em><em>为妻</em>)</a> <small class=\"text-muted ms-2\">[连载中]</small> </h5> <p class=\"mb-1 text-muted\"> 作者：<a href=\"/search?q=带刀侍卫&amp;f=author\" target=\"_blank\">带刀侍卫</a> 字数：44.23万 浏览：
  - legado: ["<div class=\"list-group-item\"> <h5><a href=\"/novel/32844.html\" target=\"_blank\">1. 我的<em>美母</em>人妻(<em>美母</em><em>为妻</em>)</a> <small class=\"text-muted ms-2\">[连载中]</small></h5> <p class=\"mb-1 text-muted\">作者：<a href=\"/search?q=带刀侍卫&amp;f=author\" target=\"_blank\">带刀侍卫</a> 字数：44.23万 浏览：252

## MATCH 明细（前 10）
- [s14] 离线用例 · json · 5 条
- [s15] 离线用例 · json · 20 条
- [s75] 离线用例 · jsoup · 80 条
- [s76] 离线用例 · jsoup · 0 条
- [s16] 离线用例 · jsoup · 0 条
- [s17] 离线用例 · jsoup · 20 条
- [s77] 离线用例 · jsoup · 0 条
- [s46] 离线用例 · jsoup · 20 条
- [s47] 离线用例 · jsoup · 10 条
- [s44] 离线用例 · jsoup · 0 条