# Legado(Kotlin) vs Python 引擎差分报告

- 参与对比书源: 100（成功求值 74）
- 结果分布: {"MATCH": 73, "LEGADO_ERR": 1}
- 分析器覆盖: {"json": 21, "jsoup": 53}
- 有效 MATCH（两侧均取到非空值）: 53 / 73；其余为两侧同为空的弱一致

## LEGADO_ERR (1)

- [s857] 🎃酷匠网🎃 · 规则 `$.body.list[*]`
  - java.lang.IllegalArgumentException: json string can not be null or empty

## MATCH 明细（前 10）
- [s873] 米读小说 · json · 10 条
- [s844] 🌐漫客栈🌵 · jsoup · 30 条
- [s843] 顶点小说 · json · 15 条
- [s845] ✐ 神漫画 · json · 30 条
- [s815] 梦想家文学 · jsoup · 50 条
- [s876] 🍉 萝莉文学 · jsoup · 0 条
- [s817] 🎨 爱优漫📱  · json · 30 条
- [s877] 偶遇吧 · jsoup · 38 条
- [s875] 笔尖中文 · jsoup · 100 条
- [s847] 🌙 米读小说 · json · 11 条