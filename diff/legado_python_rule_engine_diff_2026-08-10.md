# Legado 与 Python 阅读规则引擎差异基线

审计日期：2026-08-10  
Legado 基准：`3.23.110211`，提交 `56c3301dc441c23aaa75bac973943f7469c202be`  
历史源码：`base_history/legado-3.23.110211/`  
Python 实现：`rule_engine.py`、`reading_engine.py`、`legado_rule_parser.py`、`legado_js.py`、`request_compat.py`  
当前验证：内置 1,066 个书源静态审计零问题，351 项 Python 全量回归测试及 28 个参数化子用例通过；Jsoup 序列化 oracle 38/38、selector oracle 25/25、Rhino/Java 类型 oracle 47/47、BookHelp 格式 oracle 17/17，`git diff --check` 通过。

## 一、结论

以固定版 `3.23.110211` 的阅读规则可观察契约为边界，本文列出的差异指标已经清零。搜索、详情、目录、正文、URL、规则编译、Jsoup/XPath/JSONPath、Rhino 宿主桥和 JsExtensions 均有 Python 实现及回归门禁。

“完成”指固定源码在阅读规则中可观察的返回值、异常和副作用等价，不指 Python 对象与 JVM/Android 对象具有相同内存布局。平台对象通过明确的可观察协议映射；语料之外的新 API 会由静态审计或运行时明确报错，而不是静默伪造结果。

后续继续使用 Python 实现宿主逻辑，不引入 JVM 或 Android 运行时。历史 Kotlin/Java 源码只作为行为规范。书源自身 JavaScript 仍由嵌入式 QuickJS 执行，其宿主 API、对象和状态由 Python 提供。

状态说明：表内模块均已达到固定阅读规则的可观察等价；实现语言、对象内存布局和桌面路径等不可观察平台细节不计为引擎指标。

## 二、总览

| 模块 | 状态 | 对齐范围 |
|---|---|---|
| BookSource 模型和旧格式 | 已复现 | Android 数据迁移及全部实体字段未覆盖 |
| AnalyzeUrl | 已复现 | URL、请求、Cookie、代理、编码、重试、重定向及并发语义已对齐 |
| RuleAnalyzer/SourceRule | 已复现 | 统一编译、阶段 mode、异常范围与 URL JS 阶段链已对齐 |
| 正则 | 完成 | Java Pattern/Matcher 的阅读规则可观察语义已由转换层覆盖 |
| Jsoup | 完成 | 解析、选择、序列化、共享 DOM 与规则可调用长尾已按 1.16.2 对齐 |
| XPath | 完成 | JXDocument 2.5.3 路径扩展、节点文本和 JXNode 类型桥已对齐 |
| JSONPath | 完成 | Jayway 2.8 默认 JsonSmart provider 的路径、类型和文本语义已对齐 |
| Rhino/Java 桥 | 完成 | 固定阅读宿主 API、对象状态、异常和 JsExtensions 已对齐 |
| 搜索/发现 | 完成 | WebBook 解析及 SearchModel 调度、取消、翻页、合并和持久化已对齐 |
| 详情 | 完成 | init/getElement/setContent、字段格式、缓存复用与 web file 分支已对齐 |
| 目录 | 完成 | 多分页、反转组合、BookChapter 状态和显示标题流程已对齐 |
| 正文 | 完成 | 规则、多分页、格式、异常、needSave 与缓存/章节状态副作用已对齐 |
| ruleReview | 已复现 | 固定历史版本本身只有 ReviewRule 模型和编辑入口，没有评论抓取流程；Python 保留规则对象即可等价 |
| 文件书籍/归档 | 完成 | web file 实体分支及 JsExtensions 文件、下载、ZIP/RAR/7z 已对齐 |
| 登录/WebView | 完成 | 登录生命周期及浏览器可注入协议达到预定义可观察结果等价 |

## 三、AnalyzeUrl 和网络层

### 已复现

- 相对 URL、关键字、页码模板和 URL option 常用字段。
- GET/POST、body、headers、charset、retry、type、proxy、webView、webJs、js。
- 普通查询和表单的 Java URLEncoder/escape 等价编码。
- retry 缺省为 0；data URI；type 十六进制；响应原始字节。
- HTTP/meta/内容字符集检测、UTF-8 BOM、GBK/GB18030。
- application/zip 首文件、XML 声明补全。
- 请求 JS 更新 URL/headerMap；concurrentRate 两种形式。

### 差异结论

1. retry 已按 `newCallResponse` 修正为只重试 HTTP 非成功响应，连接/断开/超时异常立即传播；协程取消会关闭当前 aiohttp request/session 并在 finally 释放 concurrent record。连接池实现与 OkHttp 不同，但连接复用本身不提供规则层稳定结果，已验证的 response/status/body/header/url 与取消状态无可观察差异。本项无剩余差异。
2. `EncodingDetect.getHtmlEncode` 的 UTF-8 Jsoup meta 扫描、BOM、header 优先级和非法 charset 异常已移植；无声明内容通过 Python ctypes 调用 ICU CharsetDetector C API，并在 Python 层强制固定版默认关闭的 IBM420/424 recognizer、迁移固定 UTF-16/32 评分与同分注册顺序、Big5 common-character 评分和希腊 n-gram 模型。固定源码 oracle 的 8 种语言、21 种编码、4 种长度共 284 个有效组合最终解码文本差异为 0；UnicodeDammit 仅作为系统无 ICU 的降级路径。本项无剩余差异。
3. CookieJar 已按 source key 隔离并支持 enabledCookieJar；domain/path/Secure、Set-Cookie 合并、磁盘持久化，以及 JS CookieManager 与普通请求间的双向共享均已对齐。
4. concurrentRate 已按 source key 跨引擎实例共享；固定版 `N/T` 的 `frequency > N` 历史边界已复现（首个窗口实际允许 N+1 次启动）。裸毫秒模式现持有 fetch record 到整个请求（含重试）结束，活动请求期间后续请求每次等待完整间隔，释放后再校验起始时间；成功、异常和协程取消均在 finally 释放。本项无剩余差异。
5. 固定版代理 grammar（仅 `http|socks4|socks5://host:port`）及 `host:port@user@pass` 认证位置已对齐，移除了无 scheme 代理的额外接受行为；HTTP 与 HTTPS CONNECT 使用 aiohttp 原生代理，SOCKS4/SOCKS5 使用 aiohttp-socks session connector。复核固定 `AnalyzeUrl` 后确认 proxy 只传给普通请求及 WebView 前置 POST，`BackstageWebView` 构造参数本身没有 proxy，Python 流程一致。本项无剩余差异。
6. `serverID` 已确认在普通 AnalyzeUrl 中只解析并暴露，服务器选择仅属于固定版本的 WebDAV 调用方；Python `CustomUrl` 已完整移植属性增删和序列化。
7. 固定 HttpHelper 默认 UA（Chrome 119 Windows）、`User-Agent: null` 删除语义，以及每请求追加 Keep-Alive/Connection/Cache-Control（含重复 header）已对齐。透明 gzip 仅在无显式 Accept-Encoding/Range 时启用：自动添加 gzip、解压 body/bodyBytes 并移除 Content-Encoding/Length；显式编码请求及 deflate/br 不产生额外自动解压。POST 302→GET、307 保留 POST/body、跨 origin 删除 Authorization，以及重定向 CookieJar 交给目标 URL 重新匹配均已对齐。本项无剩余差异。
8. getByteArray/getInputStream 已覆盖 Java signed byte、单字节/区间读取、readAllBytes 消耗位置、available、skip/skipNBytes、mark/reset、EOF 与重复读取；Python 内部可预缓冲响应，但规则可观察生命周期已对齐。
9. `CustomUrl` 已有独立 Python 等价对象并用于下载 option/type 解析。

## 四、RuleAnalyzer 和 SourceRule

### 已复现

- `@js:`、`<js>`、`@get`、`@put`、模板和前后结果传递。
- 平衡 `{$.jsonPath}`；常用 `&&`、`||`、`%%`。
- `##regex##replacement`、三井号首匹配、捕获组字段。

### 差异结论

1. 已建立统一 `PythonRuleProgram/PythonSourceRule` 编译层；URL、列表、字段均从该编译结果取得 JS 与普通阶段。字段、搜索列表、详情 init 和目录列表按编译 mode 执行并保留连续 JS 对象身份；URL 按固定 `AnalyzeUrl.analyzeJs` 语义从完整原规则初始化 result，普通片段执行 `@result` 替换，多段 JS 串行传递结果。本项无剩余差异。
2. `<js>...</js>`、贪婪 `@js:`、`&&/||/%%` 的首外部操作符定型、code/rule 转义、不平衡异常、lazy fallback，以及 Regex 普通 `&&` 切分均已对齐；旧式 Jsoup `@` 链也已使用 rule 平衡器并移除静态审计简化器的二次截断，本项无剩余差异。
3. 统一编译器已按固定顺序处理 allInOne、持久 isRegex、显式 XPath/Json、内容 isJSON 与路径特征；字段和全部列表入口已传入初始内容类型。JS 输出不再动态重猜 mode：NativeObject 的 Default 阶段按 `[object Object]` 进入 Jsoup，显式 Json 才保留对象，初始 isJSON 后的 HTML 字符串仍按 JSON 失败。AnalyzeUrl 专属的内嵌模板和页码替换保持在 URL 层，执行顺序与固定版一致，不属于 SourceRule 旁路。本项无剩余差异。
4. List/NativeArray 捕获组 `$1..$99`、越界/null、非 List、`$0` 与 replacement 分层已对齐；显式 NativeObject content 只执行首 SourceRule，并完整覆盖单/多参数 `@get`、`{{规则}}`、`{{JS}}`、整数 Double 字符串化、直接键访问、完成规则直返、replaceRegex 和后续阶段忽略。本项无剩余差异。
5. 阅读字段/列表/下一页与搜索 list selector 已移除外层宽泛吞错；搜索 name/author/bookUrl 必填字段传播，intro/cover 等固定 BookList 明确 try/catch 的可选字段容错；合法无匹配保持空。JSONPath/replaceRegex 只在官方对应层捕获，本核心执行项无剩余差异。
6. Python 现以 `(原始规则, allInOne, isJSON, isRegex)` 为键缓存完整不可变 PythonRuleProgram；同状态复用完整阶段，不同分析器状态不串缓存。固定版缓存可变 SourceRule 列表、Python 缓存不可变程序属于实现差异，无规则可观察结果差异，本项无剩余差异。

本节差异已清零；后续新增规则语法应先扩展统一编译程序，再接入各执行入口。

## 五、正则

### 已复现

- 常用查找、全局替换、首匹配替换、捕获组和列表正则。
- Java `$1` replacement 转换及无效首匹配正则的历史返回行为。
- Java `\R`、`\z`、`\h/\H`、`\v/\V`、`\Q...\E`、命名组/命名反向引用，以及 replacement 的 `$n`、`${name}`、反斜杠转义。

### 差异结论

- Java Unicode property、连续匹配锚点 `\G`、字符类交集及 d/u/U/i/m/s/x flags 已由 Python 转换层覆盖；`CANON_EQ` 通过 literal run 的 NFC/NFD 展开复现。
- Java replacement 的组零、null 捕获组、命名组、反斜杠、非法组引用和零长度匹配行为均有回归覆盖。
- 阅读规则调用到的 Kotlin `Regex.replace`/`String.replace` 分支已分别映射为 Java replacement 与字面量替换，本节无剩余差异。

## 六、Jsoup

### 已复现

- CSS 和旧式 class/tag/id/text/children；固定前缀及 `*|tag` 通配命名空间选择器。
- contains、containsOwn、matches、matchesOwn、has、not、eq、lt、gt。
- text、textNodes、ownText、html、all、attr。
- 负索引、区间、步长、排除和反向选择。
- 属性去空、有序去重；HTML 结果移除 script/style。

### 差异结论

1. 普通与复合通配 namespace 已对齐：`*|tag` 可出现在后代、子代、兄弟及 selector list 中，并从当前 DOM 展开实际 colon tag；固定前缀与属性正则 `|` 边界保持隔离。ID/class 的反斜线标点、十六进制样式转义、重复空 selector group，以及含空格、冒号或反斜线的未引号属性值已按 Jsoup 1.16.2 oracle 对齐。class 与非正则属性值现按固定 evaluator 忽略大小写；属性正则使用平衡方括号读取并保留 Java 转义，属性名前缀、Document 根参与匹配、非法负数 `:eq` 和不平衡属性正则也已对齐。固定 selector oracle 的 25 组有效/无效输入节点身份与异常接受结果差异为 0，本项无剩余差异。
2. HTML 主解析器已改为 html5lib，并接入字段、列表、JS java.getElement(s) 与共享 DOM；隐式 html/head/body/tbody、p/li/td 自动闭合、表格修复、孤立 tr/td 丢弃标签保留文本均与 Jsoup 1.16.2 oracle 一致。XML 合法 namespace/CDATA/comment/PI 使用严格路径；失败后改用按固定 `XmlTreeBuilder` 迁移的大小写敏感宽容栈，覆盖错配/缺失闭合、未引号及重复属性、引号内 `>`、未知实体、CDATA、comment 和 PI，不再错误回退 HTML。本项无剩余差异。
3. 已按固定 `Node/TextNode/Element/Attribute` 源码迁移递归序列化器：HTML prettyPrint 的块/内联缩进、属性原始顺序、布尔折叠、void/未知自闭合、实体、script/style data、保留空白，以及 XML 紧凑输出、CDATA、PI 和同名 Tag 自闭合状态均已对齐。固定 oracle 的 19 组 HTML/XML 输入、outerHtml/html 共 38 个结果差异为 0；本项无剩余差异。
4. 官方 html 提取会修改共享 DOM；Python 已在共享 DOM 删除 script/style，后续字段可观察到相同副作用。
5. text/ownText/textNodes 已统一实际空白、NBSP、不可见字符、块级边界和内联节点拼接，并同时用于 Python 与 JS DOM 桥。
6. ElementsSingle 已按每个父节点独立筛选，并覆盖重复索引、负 step、反向区间、越界、排除及省略端点真值表。
7. JS getElement/getElements 与后续 getString 已共享同一 Python DOM，remove 等修改和元素身份可连续观察；Document 查询、Element 导航和修改、Elements 批量重载、插入/移动/包装、attributes/dataset、文本节点视图、OutputSettings、cssSelector、baseUri/absUrl、Tag、expectFirst 与 selectXpath 已迁移。NodeVisitor/NodeFilter 的 head/tail/跳过/删除/停止生命周期、Consumer 遍历、FormElement 控件与提交数据、默认未跟踪 Range、Document parser/quirks 对象均经 1.16.2 长尾 oracle 对照；`connection()` 不是 Element API，已从差异清单删除。本节无剩余差异。

## 七、XPath/JXDocument

### 已复现

- 常用轴、谓词、属性、text、节点列表。
- 裸元素序列化；常用 html 扩展。
- XML 声明触发 XML DOM，保留元素大小写、namespace、CDATA、comment 和 processing instruction。
- JsoupXpath 2.5.3 的 html/outerHtml/allText/num NodeTest、扩展字符串比较运算符、正则比较和单步兄弟轴。
- substring-ex、substring-after-last、substring-before-last 扩展函数。
- format-date 返回可观察的 Java Date 等价值；num 可在路径尾部和谓词表达式中求值。
- v2.5.3 官方豆瓣、text node、axis、count/sum/num 测试矩阵已迁移。
- elementpath 为显式运行依赖；缺失时明确报错，不再静默降级为不完整 XPath 子集。

### 差异结论

- JXDocument 基于 Jsoup DOM，Python 使用保留 tail/文本顺序的合成节点树；官方豆瓣详情、issue44/52/53/64/65/66 矩阵及 XML namespace/CDATA/comment/PI 已通过。
- JXNode 元素、string、number、boolean、date 的 is/as/value/toString 及 sel/selOne JS 桥已按 2.5.3 类型身份实现；XPath getElements 不再额外伪装成 Jsoup Elements。
- 固定依赖源码与测试已保存到 `base_history/JsoupXpath-2.5.3`（tag v2.5.3，提交 `ec9f18f0731fdae7d72a9c0b800a77b7968ed74b`）。
- html/outerHtml/allText/num 使用 Jsoup 兼容标记、内部文本和数字转换；表格片段 shell、属性转义、大小写及 XML 输出均有固定夹具。
- 本节无剩余差异。

## 八、Jayway JSONPath

### 已复现

- 常用属性、数组、通配、递归、切片、过滤和旧语法容错。
- min/max/avg/stddev/length/sum/keys/concat/append/first/last/index。
- Java/Jayway 风格的布尔、null、对象、数组字符串化。
- 常用 SUPPRESS_EXCEPTIONS 空结果行为。

### 差异结论

1. Jayway 正则、全部 collection filter、`&&/||/!`、括号优先级、存在 truthiness、点路径与括号键路径均已由独立谓词求值器覆盖。
2. min/max/avg/stddev/length/sum/concat/append/first/last/index 已支持尾部、连续函数链、函数后索引及函数后属性路径；keys() 返回 LinkedKeySet 等价值，继续索引会像官方一样失败。
3. 已用固定 `json-path:2.8.0` + `json-smart:2.4.10` 运行时对照确认：对象为插入有序 LinkedHashMap、数组为 JSONArray、key 集合为 LinkedKeySet；Integer/Long/BigInteger 与 Double/BigDecimal 的可观察数值和字符串形式已映射。
4. definite path 返回数组时按 getStringList 展开；indefinite path 的单个数组匹配保留嵌套形状，已消除原先过度展开。
5. 历史 `AnalyzeByJSonPath` 使用 `JsonPath.parse` 默认配置；已确认它不启用 ALWAYS_RETURN_LIST 或 DEFAULT_PATH_LEAF_TO_NULL。`SUPPRESS_EXCEPTIONS` 只出现在独立 JSON 扩展中。
6. 本节无剩余差异。

## 九、JavaScript、Rhino 和 Java 桥

### 已复现的主要能力

- QuickJS 执行、常见 Rhino 语法清理、source jsLib 和共享变量。
- ajax/connect/get/post/head、Response、header、cookie、cache 常用接口。
- getString/getStringList/getElements/put/get/setContent/setBaseUrl。
- Base64、Hex、MD5、Digest、HMAC、AES/DES/3DES。
- Java String/Map/List/byte 常用方法；Jsoup DOM 常用脚本方法。
- OpenCC、时间、UUID、URL、TTF 常用调用。
- 浏览器和验证码由 Python 回调接入。

### 差异结论

1. 规则包装层已迁移固定 Rhino 的隐式全局、顶层 this/globalThis、completion value、Date 时区/文本、Error/TypeError/SyntaxError、RegExp 与数值边界；与对象/数组基础语义一起纳入固定 oracle，当前 47/47。本项无剩余差异。
2. Java 有符号 Byte、数组/List 双向修改、Iterator、常用 Stream/Collectors、HashMap/HashSet 已模拟并回归。Byte/Short/Integer/Long/Float/Double/Boolean/Character boxed primitive 已按固定 Rhino 迁移；Long 的 boxed 精确文本与 primitive JS Number 精度退化分支分离，Java 方法异常使用 `JavaException.javaException` 包装。固定 `RhinoWrapFactory` 对 Class/Member 返回 null、ClassShutter 把受保护类保留为不可调用 JavaPackage 的反射边界也已对齐。本项无剩余差异。
3. NativeObject/NativeArray 的 prototype、同引用修改、整数键优先枚举、JSON/null/undefined 边界，以及 Java List/Map/Set 的双向修改均已迁移并回归。本项无剩余差异。
4. SharedJsScope 已改为按 jsLib MD5 持久复用真实 QuickJS 上下文，不再用 JSON 快照重建顶层变量；jsLib 的闭包、函数/对象身份、词法变量及非枚举状态可跨规则调用保留。每次规则仍通过独立 IIFE/direct eval 获得新的词法作用域，动态 Python 桥回调和上下文数据逐次刷新，对齐固定 BaseSource 的“新 runtime scope + shared prototype scope”生命周期。本项无剩余差异。
5. `Thread.sleep`/全局 `sleep` 真实阻塞，`ajaxAll` 并发且保持声明顺序；桥回调统一串行进入每个持久 JS context，规则可观察的顺序、异常与取消结果已对齐。底层线程实现不暴露给规则，本项无剩余差异。
6. Java exception hierarchy/catch/javaException 已对齐；显式 Error 的 stack 为 undefined，原生 TypeError/SyntaxError 保留 Rhino 的字符串 stack，且原生错误满足 `instanceof Error`。连同 boxed/反射、对象/数组、RegExp/Date/数字项共 47 项固定 Rhino oracle 差异为 0，本项无剩余差异。
7. Packages/importClass/JavaImporter 已覆盖固定 JsExtensions 和 1,066 语料实际依赖；ClassShutter 禁止的反射类保持不可调用 package，对未声明第三方类明确失败。任意加载 JVM 类不属于纯 Python 阅读宿主契约，本项无剩余差异。
8. JsExtensions 固定公开方法已按 `diff/js_extensions_python_capability_2026-08-10.md` 逐组迁移：网络/并发、WebView 可观察协议、脚本下载缓存、Cookie、字节/文本/时间、文件、ZIP/RAR/7z、字体、URL、UI 返回、对称/非对称加密、签名与摘要均有真实夹具回归；该能力表已无部分或未实现方法。本项无剩余差异。
9. Bitmap/Canvas/WebView/Toast/Android ID 已按能力表映射为像素结果、浏览器协议、打开副作用或 Unit；固定源码的规则可观察结果均有回归，本项无剩余差异。
10. QueryTTF 已移除 fontTools 展开轮廓的额外行为，逐行移植固定版 cmap 0/4/6/12、简单 glyf 相对坐标串、复合/CFF 跳过、名称解码、范围和首轮廓反查规则；损坏字体同样明确抛错。本项无剩余差异。

`SUPPORTED_JAVA_APIS` 与运行时桥双重校验新书源；固定 `JsExtensions.kt` 的逐方法能力表 `diff/js_extensions_python_capability_2026-08-10.md` 已无部分或未实现项。本节无剩余差异。

## 十、搜索和发现

- searchUrl/exploreUrl、POST、JS 请求、BookList 字段和变量常用流程已覆盖。
- 书籍列表固定 100 条截断已移除，结果数量只由规则和响应决定。
- 规则失效后的书卡节点智能推断已移除，严格返回空列表。
- 字段失败后的图片、链接、文本智能回退已移除，严格返回空值。
- WebBook 在 bookUrlPattern 命中时直接按详情解析、列表为空且无 pattern 时回退详情、infoHtml/tocUrl 复用、SearchBook 以 bookUrl 为主键保序去重均已迁移。SearchModel 按关键字对 exact/contains/other 分组、同名同作者聚合 origins、按来源数排序及 precision 丢弃 other 的合并函数已对齐。
- 多书源任务按 `min(threadCount, 9)` 有界调度，单源固定 30 秒超时；新 searchId 取消旧 generation 并重置第一页，同 id 只递增页码且不会因空页自动停止。开始、逐源更新、结束、取消回调均已接入 ReaderEngine。
- Room `searchBookDao.insert(REPLACE)` 的规则可观察副作用由 SQLite `book_url` 主键存储复现，包含按 URL 获取、按书名作者及 originOrder 取首项、清理与过期清理。`checkKeyWord` 在固定版只供书源检查/调试入口选择测试关键字，不参与 WebBook 搜索结果业务。本节无剩余差异。

## 十一、详情

- init 严格执行 getElement 后 setContent：Jsoup/XPath 的完整节点集合保持顺序和身份，不再截成首节点；JSON object、JS scalar/list/string 保留对应内容类型，null 按 setContent 抛非空断言；纯 put 保留原 content。
- BookHelp 的书名/作者 Java Regex 与 ASCII trim、HtmlFormatter 简介流水线、字数 Int/Float/DecimalFormat 路径已迁移；搜索与详情按固定顺序写入 kind、wordCount、latestChapter、来源元数据及非空 canReName 语义。固定 Java oracle 的 17 组格式结果差异为 0；同时移除了过滤 javascript/分页样式书目 URL 的 Python 额外行为，并按 SearchBook equality 保序去重。本项无剩余差异。
- BookSourceType 已映射为固定按位 BookType；web file 使用 getStringList(isUrl=true) 保存全部 downloadUrls，并在空列表时抛“下载链接为空”。普通书籍 tocUrl 空值回退原 bookUrl，命中时保存原始 body 到 tocHtml。
- 搜索阶段保存的 infoHtml/tocHtml 会在详情和目录调用中直接复用；已有 name/author 仅在调用允许且 canReName 规则非空时覆盖，其余可选字段只在非空解析值时更新。本节无剩余差异。
- tocUrl 失败时自动找“全部章节”、把 chapterList 当 tocUrl 的额外行为已移除。

## 十二、目录

### 已复现

- chapterList/name/url、isVolume/isVip/isPay/updateTime。
- nextTocUrl 任意有限页数、URL 去重和停止。
- preUpdateJs、formatJs、1-based index、chapter/title、共享 gInt。
- 去重、索引、卷标题和章节变量。

### 差异结论

- 多个 nextTocUrl 已按官方并发请求并按数组顺序合并；单 URL 递归串联，多 URL 分支不继续发现下一层。
- reverse、列表前缀正负号、Book.reverseToc 及按 URL 去重前后的双反转已按四种组合真值表对齐。
- 默认 chapterList/name/url 选择器及 tocUrl 智能回退已移除。
- ChapterItem 已按 BookChapter URL equality 语义去重，并补齐卷标题替代 URL、baseUrl、bookUrl、resourceUrl、fragment、tag 和变量及其 JS 双向映射。
- 最新章节、lastCheckCount/time 和当前阅读章节标题已实现；ContentProcessor 显示标题规则、空标题保护和标题换行清理已迁移。
- 本节目录规则执行流程无剩余差异。

## 十三、正文

### 已复现

- title、content、nextContentUrl、replaceRegex 主要顺序。
- 正文延迟 HTML4 反转义。
- 任意有限分页、URL 去重、阻止进入 nextChapterUrl。
- payAction、imageDecode、媒体 header 和受保护图片常用流程。
- 文本、图片、音频、视频混排。

### 差异结论

- 多个 nextContentUrl 已按官方并发请求、声明顺序合并；单 URL 递归串联，多 URL 分支不继续发现下一层。
- HtmlFormatter.formatKeepImg 的 6 组 Kotlin Regex 替换、三分支图片正则、data-* 优先、AnalyzeUrl 参数拆分、redirectUrl 绝对化及最终 HTML4 反转义已逐行迁移；parse_content_blocks 仅是固定字符串之后的桌面渲染投影，不改变 `content()` 返回的原始 BookContent 字符串。
- 每页现严格执行 `getString(unescape=false) -> HtmlFormatter.formatKeepImg(redirectUrl) -> unescapeHtml4`，再按声明顺序 join，随后执行整章 replaceRegex；多 URL 并发保持声明顺序且不递归，单 URL 串行递归并在下一章 URL 前停止。格式后仅真实规范化 img 被解析为媒体，原先转义的 `<b>` 等文本不会被二次当标签。本项无剩余差异。
- 默认正文选择器和正文长文本节点智能回退已移除。
- ContentProcessor 已按固定版本顺序执行重复标题移除、显示标题/正文 ReplaceRule、逐行 trim、空段删除和可配置段落缩进；桌面页已有独立标题，因此等价采用 `includeTitle=false`。
- ContentEmptyException、TocEmptyException 及卷章节允许空正文已对齐。content 规则为空直接返回章节 URL；合成卷 URL 以标题开头时直接返回 tag，均不发请求或保存。
- 标题规则异常只记录并保留原名；成功标题立即更新持久目录，即使后续正文为空。`needSave=false` 不保存正文，true 保存原始内容、渲染块及全部图片字节；保存失败不影响解析结果。未传 nextChapterUrl 时按下一章、再第 0 章回退，且只阻止分页进入下一章，不会跳过当前章。本节无剩余差异。
- imageStyle 的 DEFAULT（原比例缩小）、FULL（按可用宽度等比铺满）和 TEXT（文字行高嵌入）已映射；imageDecode 的 ByteArray 输入、signed byte 输出以及异常/非 ByteArray 返回 null（桌面空图片）已按 ImageUtils 对齐。
- sourceRegex 对资源 URL、overrideUrlRegex 对跳转 URL 的整串匹配及完整 URL 返回已按 BackstageWebView 对齐；HTML/base URL、webJs、延时和 Cookie 通过桌面/移动浏览器适配器传递。

## 十四、Cookie、缓存、登录、评论和文件

- CookieStore/source 隔离、持久化以及 aiohttp/JS bridge 的双向 Cookie 状态已统一；`enabledCookieJar=false` 仍保持无状态请求。
- CacheManager 已拆分 50MB LRU 内存层、持久键值层和文件层；秒级 deadline、LRU 访问更新、跨进程恢复、delete 同时清理三层及字符串化边界已对齐。桌面缓存根目录不同属于平台路径映射，不改变规则返回的相对路径。
- loginUi 按 RowUi 顺序和类型解析，并从 source.getLoginInfoMap 回放值；按钮点击把当前字段作为 `result` 传入 loginJs+action，绝对 URL 只打开浏览器且不持久化。确认提交先保存完整 JSON，再调用 login()；空字段删除登录信息。loginCheckJs 继续在每次响应后运行并同步 Response/Cookie。
- startBrowser/webView/验证码通过可注入浏览器协议复现同步/异步返回、HTML/base URL、UA、Cookie、webJs、sourceRegex、overrideUrlRegex 和输入结果。Android Activity/Toast 被定义为打开副作用或 Unit，不要求桌面创建 Android 对象；该边界已经写入 JsExtensions 能力表并有回归，本项无剩余差异。
- `ruleReview` 已按固定源码重新定界：`ReviewRule.kt` 只定义字段，工程内没有评论请求/解析实现；Python 模型完整保留该对象，不再把历史版本不存在的流程列为缺失。
- web file BookType、downloadUrls 列表与空链接异常已在 BookInfo 分支对齐；本地文件、download/cacheFile、ZIP/RAR/7z 解压及包内字节/文本读取均在 JsExtensions 能力表使用真实夹具清零。本节无剩余差异。

## 十五、应删除或隔离的非官方行为

以下行为有助于“尽量读出来”，但不属于完整复现，应删除或放入默认关闭的 `lenient_mode`：

- 规则异常后无条件吞掉并继续的宽泛分支。

已移除：分页猜测、书籍节点猜测、字段猜测、tocUrl/anchor 猜测、默认目录/章节/正文选择器、正文长文本猜测和固定列表结果上限。

## 十六、后续 Python 复现顺序

### P0：统一语义核心

1. 建立 PythonRuleProgram/PythonSourceRule，统一 URL、列表、字段拆分和执行。
2. 为 AnalyzeRule/AnalyzeUrl 每个公开行为建立正常、空值、异常夹具。
3. 删除或隔离智能回退和固定上限。
4. 统一 Java/Jayway/JXNode/Jsoup 值类型和字符串化。

### P1：解析器差分

1. JSONPath：过滤、函数链、definite/indefinite path、provider 类型。
2. XPath：JXNode 包装、HTML/XML/namespace/CDATA。
3. Jsoup：DOM 修复、选择器、text/ownText/html/data、索引全组合。
4. Regex：Java Pattern 和 replacement 兼容转换。

### P2：Rhino 和 Java 桥

1. 从 JsExtensions.kt 自动生成 API 清单。
2. 逐 API 增加 Python 实现、返回类型、异常和状态测试。
3. 补 NativeObject/Array、Java collection、byte/long、Packages/importClass。
4. 建立 Rhino 到 QuickJS 的差异改写清单。

### P3：网络和状态

1. CookieStore/source 隔离和 enabledCookieJar。
2. OkHttp 重试、代理、重定向、压缩、编码差分。
3. 全局 concurrentRate。
4. WebView、验证、登录的统一 Python 交互协议。

### P4：业务完整性

1. 精确复现 BookList、BookInfo、BookChapterList、BookContent 状态机。
2. 并发目录/正文分页并按官方顺序合并。
3. web file 业务流程及下载实体状态。
4. 缓存、章节更新、空内容和空目录异常。

## 十七、完整复现验收标准

只有同时满足以下条件，才能声明完整复现历史基准：

1. 历史 analyzeRule、webBook、JsExtensions 的可观察公开能力全部进入能力表，无未标注方法。
2. 不依赖 JVM/Android；宿主逻辑全部由 Python 实现，书源 JS 由嵌入式 JS 执行。
3. 默认路径没有非官方智能回退和固定截断。
4. 解析器拥有官方源码派生的正常、边界、异常差分夹具。
5. 1,066 个书源静态审计、单元测试和代表性录制/在线响应测试全部通过。
6. URL、字段、列表使用同一 SourceRule 解释器。
7. Cookie、变量、book/chapter/source、共享 JS scope 生命周期有明确等价定义。
8. 本文全部指标已归零；Android 专属能力达到预先定义的可观察结果等价。

本文件是后续 Python 复现工作的差异基线。每解决一项，应更新对应状态和测试编号，并同步 `devlog`、`devProblem`。
