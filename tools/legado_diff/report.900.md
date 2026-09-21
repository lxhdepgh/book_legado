# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 89）
- 结果分布: {"MATCH": 86, "LEGADO_ERR": 1, "FORMAT_DIFF": 1, "VALUE_DIFF": 1}
- 分析器覆盖: {"jsoup": 73, "json": 13, "xpath": 3}
- 有效 MATCH（两侧均取到非空值）: 66 / 86；其余为两侧同为空的弱一致

## VALUE_DIFF (1)

- [s972] 海棠书屋 · 规则 `.j_list_container li`
  - 第 0 条起不一致
  - python: ["救命！我的厌女症总裁不仅碰瓷还装秒(1v1 调教高h) 其他连山会 江城华秦集团总裁秦聿，是个出了名的厌女总裁。空降来的总秘姜如音是他的头号敌人。他们处处不对付。这天，姜如音在电梯里狠狠地踹了秦聿一脚，没想到他之后硬不起来了……一个为期三十天的“治疗契约”，成了恶狼.. 最新章节: 第144章我的快乐", "予我禁地：我的继子不对劲 都市陌筱欷 【冷漠偏执年上继子 x 清醒沉沦嘴硬小妈｜不洁、拉扯、沉沦】住进宋家的第一天，于琦就知道自己不受欢迎。尤其是宋祁言。他是宋家唯一继承人，冷淡、禁欲、惜字如金，看她的眼神像在看父亲又一次带回来的“麻烦.. 最新章节: 第43章你老公在里面睡觉呢（H）
  - legado: ["救命！我的厌女症总裁不仅碰瓷还装秒(1v1 调教高h) 其他 连山会 江城华秦集团总裁秦聿，是个出了名的厌女总裁。空降来的总秘姜如音是他的头号敌人。他们处处不对付。这天，姜如音在电梯里狠狠地踹了秦聿一脚，没想到他之后硬不起来了……一个为期三十天的“治疗契约”，成了恶狼.. 最新章节: 第144章我的快乐", "予我禁地：我的继子不对劲 都市 陌筱欷 【冷漠偏执年上继子 x 清醒沉沦嘴硬小妈｜不洁、拉扯、沉沦】住进宋家的第一天，于琦就知道自己不受欢迎。尤其是宋祁言。他是宋家唯一继承人，冷淡、禁欲、惜字如金，看她的眼神像在看父亲又一次带回来的“麻烦.. 最新章节: 第43章你老公在里面睡觉呢（

## FORMAT_DIFF (1)

- [s936] 趣书网吧 · 规则 `//div[@class="morelist"]/ul/li`
  - 第 0 条起不一致
  - python: ["<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class=\"date\">1. </html:span><html:a href=\"/booknv/\">女生小说</html:a> / <html:a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</html:a> </html:li>", "<html:li xmlns:html=\"http://www.w3.org/1999/xhtml\"><html:span class
  - legado: ["<li><span class=\"date\">1. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118813.html\" target=\"_blank\"> 全汴京在等我的外卖(探窗)</a></li>", "<li><span class=\"date\">2. </span><a href=\"/booknv/\">女生小说</a> / <a href=\"/booknv/118729.html\" target=\"_blank\"> 我的药膳能治百病(柳十天)</a></li>"]

## LEGADO_ERR (1)

- [s916] 🎃漫蛙可直连🎃 · 规则 `.book-list li||$.books`
  - org.jsoup.select.Selector$SelectorParseException: Could not parse query '$.books': unexpected token at '$.books'

## MATCH 明细（前 10）
- [s943] 轻菠萝包 · jsoup · 1 条
- [s914] 独行小说 · jsoup · 15 条
- [s944] 🎨51漫画🔞 · jsoup · 36 条
- [s974] 🔰 po18文学 · jsoup · 26 条
- [s915] 飘天文学 · jsoup · 10 条
- [s973] 小红书 · jsoup · 18 条
- [s945] 火星小说 · jsoup · 10 条
- [s946] 集 书 阁-◾️ · jsoup · 20 条
- [s975] 闪爵小说网 · jsoup · 200 条
- [s976] 趣书网 · jsoup · 10 条