# M4 内容形态域 · LocalBookService 实现记录（本地书导入与解析）

> 里程碑：M4 四线并行之 **B 线 = LocalBookService**（§1.11，契约 §4）。
> 范围：仅本地书导入/解析一线；RSS（A 线）、ReadAloud+MediaPlay（C 线）、漫画通道（D 线）不在本文档。
> 验收命令（venv 解释器、项目根）：
> `python -m pytest tests/test_local_book_m4.py -q --no-header -p no:cacheprovider --basetemp=.cache/pytest/tmp_m4`
> 实测结果：**11 passed**。

## 1. 交付文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `app/services/content/__init__.py` | 新建 | 内容形态域服务包（零 flet import） |
| `app/services/content/local_book_service.py` | 新建 | `LocalBookService` + `ImportResult` + `UnsupportedFormatError` |
| `tests/test_local_book_m4.py` | 新建 | 11 条离线测试 |
| `requirements.txt` | 追加 | `EbookLib>=0.18`、`pymupdf>=1.24` |
| `docs/M4LocalBook实现记录.md` | 新建 | 本文档 |

未改动：`app/core/events.py`（`ImportFinishedEvent` M3 已定义，签名 `kind/count/errors`，直接复用，无需扩展）、
`app/storage/schema.py`（books 表 v2 已满足，无需 v4 迁移）、阅读域/引擎任何文件。

## 2. 归一化设计（硬指标，§1.11 下游零改动）

本地书没有"规则书源"，导入时一次性把阅读所需的全部数据落好，使
`ReadingService.open_book(source, book)` 命中缓存即可读，**不触发规则引擎/网络**：

1. **book_url**：`Path.resolve().as_uri()`，规范 `file:///D:/...` 标识。
2. **source_url（本地伪源常量）**：`localsource://local_book`（`LOCAL_BOOK_SOURCE_URL`）。
   - `books` 表 `source_url` 列、`ReadingCache` 缓存键、返回的伪 `source.bookSourceUrl` 三处同值，
     这是下游缓存命中的关键。
3. **目录落 reader-cache**：`ReadingCache.save_catalog(source_url, book_url, chapters)`，
   `chapters` 为 `list[ChapterItem]`（`rule_engine.ChapterItem`，字段 name/url/index/is_volume/book_url）。
4. **正文落 reader-cache**：逐章 `ReadingCache.save_catalog/save_content(..., chapter.url, ChapterContent)`，
   `ChapterContent.blocks` 为 `ContentBlock(kind="text", value=str)`，与网络书源引擎产物同构。
   下游 `ReadingService.load_chapters/load_content` 先查缓存即命中，**零改动读取**（测试 5.5 用
   `ReadingCache.load_catalog/load_content` 读回作为证据）。
5. **books 表登记**：复用 v2 `books` 列（book_url PK、source_url、name、author、kind=`local_<ext>`、
   cover_url、进度列默认值、in_bookshelf=1、shelf_group="local"），`ON CONFLICT(book_url) DO UPDATE` 幂等。
6. **事件**：成功发 `ImportFinishedEvent(kind="local_book", count=1, errors=[])`；
   失败先发 `count=0, errors=[...]` 再抛异常，视图既收到事件也拿到异常消息。

> 说明：schema v2 的 `books` 表之外**没有独立 chapters 表**——章节列表就是 reader-cache 的
> `catalog.json`，与网络书源完全一致（契约 §8 的 v4 迁移只加 rss/tts 表，不含 chapters 表）。

## 3. 各格式实现

- **txt（自写）**：`read_bytes` → `icu_charset.detect_charset(raw)`（ICU→charset-normalizer 链，复用零改动）
  → 解码（失败回退 utf-8/errors=replace）→ 按可配标题正则切章。默认正则覆盖
  `第X章/回/节/卷/部/篇` 与 `Chapter N`；无命中则整篇一章；首标题前的前言并入第一章。
  标题正则经 `import_local_file(title_pattern=...)` / 构造参数可配。
- **epub（ebooklib，懒加载）**：`epub.read_epub` → DC title/creator 元数据 → 按 `book.spine` 顺序取
  `ITEM_DOCUMENT` 条目 → BeautifulSoup `get_text` 文本化（折叠空行），同时把 `<img>` 记为
  `kind="image"` 块（value 为内嵌图片 href，与网络书源"图片块=URL"同构；图片字节内联留待后续增强）。
  toc 命中条目名作为章名，否则按序命名。
- **pdf（pymupdf，懒加载 `import pymupdf`）**：逐页 `load_page().get_text("text")`，按
  `pdf_pages_per_chapter` 聚合成章（默认自适应：目标约 30 章，小文档逐页）。
- **mobi / umd**：见下节偏差处置。

## 4. 与契约的偏差 / 高风险项处置结论（必读）

1. **mobi —— M4 登记为"不支持"，不硬凑**。ebooklib 不支持 mobi；venv 未引入额外 mobi 解析库
   （契约允许"尝试 mobi 库或兜底"，但纯文本扫描 PalmDOC 二进制会产出乱码正文，违反"不硬凑"红线）。
   处置：`import_local_file` 对 `.mobi` 直接抛 `UnsupportedFormatError("暂不支持该格式（.mobi）…")`，
   并由失败事件带错误信息。后续里程碑若引入可靠 mobi 解析库再放开。
2. **umd —— M4 登记为"不支持"**。冷门中文电子书格式，无成熟纯 Python 解析库。处置同 mobi，
   明确抛 `UnsupportedFormatError`。
3. **拓扑依赖收敛**：契约 §1 列 `LocalBookService` 依赖 StorageService / BookCacheService /
   RuleEngineService。本地解析是**纯本地文件处理，不触网、不走规则引擎**，故本服务
   `dependencies = ()`，正文缓存直接用低层 `ReadingCache`（契约 §4 明确允许"经 BookCacheService
   或 ReadingCache"）。这样避免无谓拉起 RuleEngineService/整书缓存引擎，测试也只需 StorageService。
4. **epub 内嵌图片**：M4 文本优先，图片块以 href 字符串占位（结构对齐网络模型），
   字节级内联（解压 OPF 取图片二进制塞进 ContentBlock.value=bytes）列为后续增强，不影响阅读主路径。

## 5. 跨平台与依赖确认

- `EbookLib 0.20`：`py3-none-any.whl`，**纯 Python**，三端通用。
- `pymupdf 1.28.2`：`cp310-abi3-*.whl`，win/macOS/manylinux **均有 abi3 wheels**。
- `lxml`（ebooklib 传递依赖）：三端均有 wheels。
- 解析层全部纯 Python / wheels，**无 Windows 专属 API**；文件选择在视图层 Flet FilePicker，
  服务只收路径。服务零 flet import；ebooklib/pymupdf 仅在对应解析函数内懒 import。

## 6. 测试清单（11 条，全部离线；epub/pdf 样例现场构造）

1. `test_txt_plain_parse` — txt 基本解析为文本块。
2. `test_txt_charset_detection_gbk` — GBK 编码经探测链正确解码（无替换符、断言 charset）。
3. `test_txt_chapter_split_by_title` — 标题正则切章、章名/索引正确。
4. `test_txt_no_title_single_chapter` — 无标题整篇单章兜底。
5. `test_normalized_model_books_table_and_cache` — **归一化断言**：file:// 前缀、ChapterItem/
   ChapterContent/ContentBlock 字段集合、books 表登记、ReadingCache 读回目录与正文。
6. `test_epub_minimal_sample` — 现场构造最小 epub（含 ncx/nav）解析出元数据与两章文本。
7. `test_pdf_minimal_sample` — 现场构造双页 pdf 并提取文本。
8. `test_mobi_unsupported` — mobi 抛 `UnsupportedFormatError`。
9. `test_umd_unsupported` — umd 抛 `UnsupportedFormatError`。
10. `test_unknown_extension_and_missing_file` — 未知扩展名报错、文件不存在报 FileNotFoundError。
11. `test_import_finished_event_published` — 成功 count=1 / 失败 count=0 且带错误，事件发布。

## 7. 冒烟清单（三端）

- txt/epub/pdf 解析路径纯 Python，无平台分支；GBK 探测依赖 charset-normalizer（已在 requirements）。
- reader-cache 落地为 `<cache>/reader-cache/<sha256(source,book)>/...`，与 ReadingService 共享，三端一致。
- 已知环境差异：ICU 动态库不可用时自动回退 charset-normalizer（已有降级，测试不依赖 ICU）。
