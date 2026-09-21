# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 88）
- 结果分布: {"MATCH": 85, "VALUE_DIFF": 1, "COUNT_DIFF": 1, "LEGADO_ERR": 1}
- 分析器覆盖: {"jsoup": 80, "json": 8}
- 有效 MATCH（两侧均取到非空值）: 60 / 85；其余为两侧同为空的弱一致

## COUNT_DIFF (1)

- [s531] 看漫画 · 规则 `$.data||$.data.data[*]||$.data[*]`
  - py=1 legado=5
  - python: ["{\"page\":{\"current_page\":1,\"total_page\":1,\"orderby\":\"click\",\"count\":5,\"comic_sort\":\"\",\"search_key\":\"我的\",\"time\":10},\"data\":[{\"comic_id\":106584,\"comic_name\":\"哦，我的宠妃大人\",\"last_chapter_name\":\"最终话 唯一的妻\",\"comic_type\":\"lianai,恋爱|gufeng,古风|chuanyue,穿越\",\"comic_newid\":\
  - legado: ["{\"comic_id\":106584,\"comic_name\":\"哦，我的宠妃大人\",\"last_chapter_name\":\"最终话 唯一的妻\",\"comic_type\":\"lianai,恋爱|gufeng,古风|chuanyue,穿越\",\"comic_newid\":\"ewodechongfeidaren\",\"comic_author\":\"极漫文化\",\"renqi\":7514141308,\"update_time\":1789880667651,\"shoucang\":2724137,\"comic_grade\":\"2\",\"ca

## VALUE_DIFF (1)

- [s581] 🎃海棠书屋🎃#2 · 规则 `.j_list_container li`
  - 第 0 条起不一致
  - python: ["救命！我的厌女症总裁不仅碰瓷还装秒(1v1 调教高h) 其他连山会 江城华秦集团总裁秦聿，是个出了名的厌女总裁。空降来的总秘姜如音是他的头号敌人。他们处处不对付。这天，姜如音在电梯里狠狠地踹了秦聿一脚，没想到他之后硬不起来了……一个为期三十天的“治疗契约”，成了恶狼.. 最新章节: 第144章我的快乐", "予我禁地：我的继子不对劲 都市陌筱欷 【冷漠偏执年上继子 x 清醒沉沦嘴硬小妈｜不洁、拉扯、沉沦】住进宋家的第一天，于琦就知道自己不受欢迎。尤其是宋祁言。他是宋家唯一继承人，冷淡、禁欲、惜字如金，看她的眼神像在看父亲又一次带回来的“麻烦.. 最新章节: 第43章你老公在里面睡觉呢（H）
  - legado: ["救命！我的厌女症总裁不仅碰瓷还装秒(1v1 调教高h) 其他 连山会 江城华秦集团总裁秦聿，是个出了名的厌女总裁。空降来的总秘姜如音是他的头号敌人。他们处处不对付。这天，姜如音在电梯里狠狠地踹了秦聿一脚，没想到他之后硬不起来了……一个为期三十天的“治疗契约”，成了恶狼.. 最新章节: 第144章我的快乐", "予我禁地：我的继子不对劲 都市 陌筱欷 【冷漠偏执年上继子 x 清醒沉沦嘴硬小妈｜不洁、拉扯、沉沦】住进宋家的第一天，于琦就知道自己不受欢迎。尤其是宋祁言。他是宋家唯一继承人，冷淡、禁欲、惜字如金，看她的眼神像在看父亲又一次带回来的“麻烦.. 最新章节: 第43章你老公在里面睡觉呢（

## LEGADO_ERR (1)

- [s533] 漫蛙 · 规则 `.book-list li||$.books`
  - org.jsoup.select.Selector$SelectorParseException: Could not parse query '$.books': unexpected token at '$.books'

## MATCH 明细（前 10）
- [s574] 得间免费小说 - 阅读有滋味，_m.idejian.com · jsoup · 18 条
- [s544] 男人小说 · jsoup · 70 条
- [s515] 爱发电（导入前请备份好自己的发现规则，登录直接点登录，填账号密码，不要改登录规则里的东西，阅读升级最新版，网站只能搜作者） · json · 10 条
- [s514] 言情小说 · jsoup · 21 条
- [s575] 🎃克苏鲁公社🎃 · jsoup · 9 条
- [s516] 神漫画 · json · 30 条
- [s576] 刺猬猫 · jsoup · 10 条
- [s577] 🎃第一版主🎃#11 · jsoup · 0 条
- [s545] 言情小说#书源.com · jsoup · 0 条
- [s547] 🌆连连看漫画 · jsoup · 0 条