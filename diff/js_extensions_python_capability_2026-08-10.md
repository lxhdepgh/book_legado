# JsExtensions Python 能力对照表（Legado 3.23.110211）

基准文件：`base_history/legado-3.23.110211/app/src/main/java/io/legado/app/help/JsExtensions.kt` 及其继承的 `JsEncodeUtils.kt`。
状态只表示 Python 桥接是否真实执行对应语义，不以静态审计白名单为完成依据。

| 能力组 | 历史公开方法 | Python 状态 | 回归/说明 |
|---|---|---|---|
| 来源与网络 | `getSource`, `ajax`, `connect`, `get`, `head`, `post` | 已完成 | 请求、响应、禁重定向、header 重载及 `ajax/connect` 失败时返回异常栈的行为均已回归 |
| 并发网络 | `ajaxAll` | 已完成 | Python 线程池并发请求并按输入顺序返回；覆盖并发发生和结果排序 |
| WebView/交互 | `webView`, `webViewGetSource`, `webViewGetOverrideUrl`, `startBrowser`, `startBrowserAwait`, `getVerificationCode` | 已完成 | 以可注入 Python 协议传递 html/url/js/sourceRegex/overrideRegex，提供同步等待、非阻塞打开副作用及验证码输入的可观察等价 |
| 脚本与下载缓存 | `importScript`, `cacheFile`, `downloadFile` 两个重载 | 已完成 | 网络、URI、外部存储和缓存文件分流、空脚本异常、有效期以及两参数重载必须显式 `type` 的历史边界均已覆盖 |
| Cookie | `getCookie` 两个重载 | 已完成 | Cookie jar、键读取、替换与删除已有回归 |
| 字节/Base64/Hex | `strToBytes`, `bytesToStr`, `base64Decode`, `base64DecodeToByteArray`, `base64Encode`, `hexDecodeToByteArray`, `hexDecodeToString`, `hexEncodeToString` | 已完成 | Android flags 的换行、CRLF、URL-safe、padding/null 边界及 Java 有符号 `byte[]` 已覆盖 |
| 时间与文本 | `timeFormatUTC`, `timeFormat`, `utf8ToGbk`, `encodeURI`, `htmlFormat`, `t2s`, `s2t` | 已完成 | 固定时间格式/毫秒偏移、历史字符集行为及 `HtmlFormatter.formatKeepImg` 主流程已按源码迁移 |
| 本地文件 | `getFile`, `readFile`, `readTxtFile`, `deleteFile` | 已完成 | 已限定到运行时 external-cache 根，覆盖相对路径、存在性、文本/字节读取和递归删除；`test_js_extensions_download_and_external_cache_file_operations` |
| 解压目录 | `unzipFile`, `un7zFile`, `unrarFile`, `unArchiveFile`, `getTxtInFolder` | 已完成 | ZIP/7z/RAR 均按 `ArchiveTemp\\md5` 安全解压；目录文本拼接及读取后删除已覆盖，真实 7z/RAR 夹具通过 |
| 压缩包内读取 | `getZipStringContent`, `getZipByteArrayContent` | 已完成 | URL/十六进制输入、字符串/字节及 charset 重载；`test_js_extensions_s2t_and_zip_content_overloads` |
| RAR/7z 包内读取 | `getRarStringContent`, `getRarByteArrayContent`, `get7zStringContent`, `get7zByteArrayContent` | 已完成 | py7zr 与 conda libarchive/bsdtar 后端；URL/十六进制、字符串/字节和 charset 重载均复用真实夹具验证 |
| 字体 | `queryBase64TTF`, `queryTTF`, `replaceFont` | 已完成 | `QueryTTF` 名称、范围、字形/编码双向查询及对象身份要求已覆盖，含真实最小 TTF |
| 章节与 URL | `toNumChapter`, `toURL` | 已完成 | `第…章` 固定替换范围、中文/全角数字及历史 `JsURL` 字段、nullable searchParams、origin 异常格式均已覆盖 |
| UI/日志/设备 | `toast`, `longToast`, `log`, `logType`, `randomUUID`, `androidId` | 已完成 | UUID/持久设备值及日志返回类型已覆盖；Toast 在无 Android UI 的预定义可观察结果为 Unit/no-op |
| 继承：对称加密 | `createSymmetricCrypto` 四个重载、AES/DES/3DES 旧接口族 | 已完成 | AES/DES/3DES/Blowfish/RC2/RC4、CBC/ECB、PKCS/Zero/NoPadding、随机 key、严格 key/IV 长度及所有公开输出形态已覆盖 |
| 继承：摘要/HMAC | `digestHex`, `digestBase64Str`, `HMacHex`, `HMacBase64` | 已完成 | hashlib/hmac 与 Base64 NO_WRAP；有 SHA-256/Base64 回归 |
| 继承：非对称/签名 | `createAsymmetricCrypto`, `createSign` | 已完成 | 固定依赖可用的 RSA PKCS1/OAEP 各 SHA/NoPadding、公私钥双向分块、key setter，以及 RSA/DSA/ECDSA 签名验证均已覆盖；未知 JCA 算法明确失败 |

## 当前结论

固定 `JsExtensions.kt` 与继承的 `JsEncodeUtils.kt` 公开能力已经逐项进入 Python 桥并有真实夹具或预定义平台可观察结果；本表无部分、占位或未实现项。Java/Rhino 宿主对象差异仍由主差异文档单独跟踪，不再错误归入 JsExtensions 能力缺失。
