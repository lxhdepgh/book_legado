# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 87）
- 结果分布: {"MATCH": 84, "FORMAT_DIFF": 1, "COUNT_DIFF": 2}
- 分析器覆盖: {"jsoup": 70, "json": 15, "xpath": 2}
- 有效 MATCH（两侧均取到非空值）: 63 / 84；其余为两侧同为空的弱一致

## COUNT_DIFF (2)

- [s361] 知轩藏书（导） · 规则 `class.ng-star-inserted mio-multi-up mio-multi-up--dynamic mio-multi-up--max mio-`
  - py=0 legado=2
  - python: []
  - legado: ["《从诡秘之主开始拯救世界》（整理未校对精校版全本）作者：菜瘾帝 陈道，一名DID，多重人格障碍患者，为了拯救跳楼的病友，意外身亡，巧之又巧的，意识分裂，真正变成了独立的存在，并各自穿越到了一方世界里。其中主意识陈道穿越到了诡秘之主里，旧日之主即将苏醒；第二人格，周珏穿越到了神秘复苏里，灵异源头若隐若现；第三人格，王动穿越到了遮天世界里，禁区至尊虎视眈眈；自此，他踏上了一条拯救自己，同时也拯救世界的道路…… 2023-06-23 00:30:29", "《诡秘之主》（精校版全本）作者：爱潜水的乌贼 蒸汽与机械的浪潮中，谁能触及非凡？历史和黑暗的迷雾里，又是谁在耳语？我从诡秘中醒来，睁眼看见这
- [s370] 知轩藏书（导入）#书源.com · 规则 `class.ng-star-inserted mio-multi-up mio-multi-up--dynamic mio-multi-up--max mio-`
  - py=0 legado=2
  - python: []
  - legado: ["《从诡秘之主开始拯救世界》（整理未校对精校版全本）作者：菜瘾帝 陈道，一名DID，多重人格障碍患者，为了拯救跳楼的病友，意外身亡，巧之又巧的，意识分裂，真正变成了独立的存在，并各自穿越到了一方世界里。其中主意识陈道穿越到了诡秘之主里，旧日之主即将苏醒；第二人格，周珏穿越到了神秘复苏里，灵异源头若隐若现；第三人格，王动穿越到了遮天世界里，禁区至尊虎视眈眈；自此，他踏上了一条拯救自己，同时也拯救世界的道路…… 2023-06-23 00:30:29", "《诡秘之主》（精校版全本）作者：爱潜水的乌贼 蒸汽与机械的浪潮中，谁能触及非凡？历史和黑暗的迷雾里，又是谁在耳语？我从诡秘中醒来，睁眼看见这

## FORMAT_DIFF (1)

- [s319] ⌨趣书网(qubook) · 规则 `//div[@class="morelist"]/ul/li`
  - 第 0 条起不一致
  - python: ["<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class=\"date\">1. </html:span><html:a href=\"/booknv/\">女生小说</html:a> / <html:a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</html:a> </html:li>", "<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class
  - legado: ["<li><span class=\"date\">1. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</a></li>", "<li><span class=\"date\">2. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118729.html\" target=\"_blank\"> 我的药膳能治百病(柳十天)</a></li>"]

## MATCH 明细（前 10）
- [s314] 起点中文-发现 · jsoup · 20 条
- [s374] ㊕ 乡土小说 #一程#书源.com · jsoup · 50 条
- [s345] 英语阅读（英语） · jsoup · 0 条
- [s316] 爱奇艺漫画 · jsoup · 0 条
- [s376] 五丁 · json · 10 条
- [s375] ㊕✐韩国漫画 #一程▼需挂梯#书源.com · jsoup · 20 条
- [s377] 潇社 · json · 1 条
- [s317] 笔尚小说 · jsoup · 27 条
- [s347] 美图网（可搜索） · jsoup · 13 条
- [s318] 创世中文网 · jsoup · 20 条