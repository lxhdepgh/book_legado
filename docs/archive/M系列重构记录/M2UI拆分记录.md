# M2 阅读域 UI 拆分记录（§7.2 / §7.4 / §7.5）

> 本记录对应 M2 四线中的「UI 拆分」一线：把冻结在 `app/views/reading_view.py`
> 的旧单体 `ReadingView`（172KB / 209 方法）拆为 `app/views/reading/` 下三视图 +
> 对话框/面板，并为每视图配一个 `app/controllers/` Controller，全部经
> `ServiceHub.get()` 接 M2 阅读域新服务。旧文件保持冻结只读，未改一行。

## 1. 交付文件清单

### 视图层 `app/views/reading/`（红线：零 `app.services.*` import）
| 文件 | 类 | 承接旧单体职责 |
| --- | --- | --- |
| `__init__.py` | `register_destinations(registry, hub)` | 阅读组三目的地工厂绑定（§7.3 装配处） |
| `find_books_view.py` | `FindBooksView` | 找书：书源列表/分类浏览/搜索页/结果流/详情对话框 |
| `bookshelf_view.py` | `BookshelfView` | 书架：网格/列表、最近阅读卡、整书缓存管理、移除确认（换源入口保留） |
| `reader_view.py` | `ReaderView` | 阅读器：目录/正文渲染/滚动续读/偏好/沉浸/快捷键/上下章 |
| `source_switch_dialog.py` | `SourceSwitchDialog` | 换源候选/进度/确认（模态） |
| `login_dialog.py` | `LoginDialog` | 书源登录动作（模态） |
| `bookmark_panel.py` | `BookmarkPanel` | 书签列表/跳转（侧栏面板） |
| `content_search_bar.py` | `ContentSearchBar` | 书内搜索条（侧栏覆盖层） |

### 控制器层 `app/controllers/`（可 import `app.services.*` 类型）
| 文件 | 类 | 订阅事件 / 取服务 |
| --- | --- | --- |
| `base.py` | `BaseController` | attach/detach、subscribe 退订句柄、engine_call、throttle(0.5s) |
| `find_books_controller.py` | `FindBooksController` | SearchFinishedEvent；ReadingService |
| `bookshelf_controller.py` | `BookshelfController` | ProgressSavedEvent、DownloadProgressEvent(节流)；ReadingService/DownloadService；并入 SourceSwitchDialog 编排 |
| `reader_controller.py` | `ReaderController` | BookmarkChangedEvent、ReadingSessionChangedEvent；ReadingService/BookmarkService/ContentSearchService；并入 BookmarkPanel/ContentSearchBar 编排 |

### 注册与测试/文档
- `app/core/registry.py`：新增目的地常量 ~~`DEST_FIND_BOOKSHELF / DEST_READER`~~
  （2026-09-27 复核：`DEST_FIND_BOOKSHELF` 全仓不存在，属笔误；实测常量为
  `DEST_FIND_BOOKS / DEST_BOOKSHELF / DEST_READER`，registry.py:41-43）与
  `READING_DESTINATIONS`，docstring 列出 M2 阅读组已注册、其余分组留 M3+。
- `tests/test_views_reading_m2.py`：13 条冒烟 + import-lint（AST 扫描）。
- 本文件。

## 2. 旧方法 → 新视图 / Controller / 服务映射概要

| 旧 ReadingView 方法（冻结） | 新落点 | 承接服务 |
| --- | --- | --- |
| `refresh_sources` / `render_sources` / `_source_row` | `FindBooksView.render_sources` + `FindBooksController.refresh_sources` | 书源列表（M3 SourceService 外壳化前由装配处 `set_sources` 注入） |
| `_open_category` / `_run_explore` / `_rebuild_category_tabs` | `FindBooksView.render_categories` + `open_source` | 分类浏览（M3 源域 explore 接入） |
| `_search` / `_start_search` / `_run_search` / `_render_search_results` | `FindBooksController.search` + `FindBooksView.render_results` | `ReadingService.search_books` → SearchFinishedEvent |
| `_stop_search` / `_set_search_running` | `FindBooksController.stop_search` + `set_search_running` | `ReadingService.stop_search` |
| `_show_book_detail` / `_load_book_detail` / `build_detail_dialog` | `FindBooksView.build_detail_dialog` + `show_detail` | 详情（book_info 随 M3/M4 闭环） |
| `_show_source_login` / `_open_source_login` / `_run_login_action` | `LoginDialog` + `FindBooksController.open_login` | 登录交互（§4.6 例外通道，随 M3 浏览器服务收编） |
| `_render_bookshelf` / `_shelf_control` / `_shelf_grid_control` | `BookshelfView.render_books/_row/_cell` | 书架记录（装配处 `set_records` 注入） |
| `_filter_bookshelf` / `_toggle_bookshelf_layout` | `BookshelfController._apply_filter/toggle_layout` | 纯视图状态，Controller 编排 |
| `_confirm_remove_shelf_item` / `_remove_shelf_item` | `BookshelfView.confirm_remove` + `BookshelfController.request_remove/_do_remove` | 记录移除 |
| `_toggle_whole_book_cache` / `_run_whole_book_cache` / `_cancel_whole_book_cache` | `BookshelfController.toggle_cache` | `DownloadService.enqueue("whole_cache")` → DownloadProgressEvent |
| `_show_source_switch` / `_run_source_switch` / `_render_source_switch_results` / `_confirm_source_switch` / `_apply_source_switch` | `SourceSwitchDialog` + `BookshelfController.open_source_switch/_run_switch/_confirm_switch` | `ReadingService.switch_source` / `apply_source_switch` |
| `_run_chapters` / `_apply_chapters` / `_chapter_control` | `ReaderController.open_book/_open_index` + `ReaderView.render_chapters` | `ReadingService.open_book/load_chapters` |
| `_run_content` / `_render_content` / `_content_controls` / `_novel_paragraphs` | `ReaderController.open_chapter/_open_index` + `ReaderView.render_content` | `ReadingService.load_content`（鸭子类型遍历 `.blocks`） |
| `_persist_scroll_position` / `_save_scroll_position_after_delay` | `ReaderController._throttled_save_position/_persist_position` | `ReadingService.save_progress`（0.5s 节流在 Controller） |
| `_change_font_size` / `_load/_save_reader_preferences` / `_apply_reader_preferences` | `ReaderController.change_font_size` + `ReaderView.set_font_size` | `ReadingService.save/get_reader_preference` |
| `_toggle_reader_chrome` / `_on_reader_tap` / 沉浸模式 | `ReaderController.toggle_immersive` + `ReaderView.set_immersive` | 视图状态（沉浸由 on_section_change 通知外壳） |
| `_previous_chapter` / `_next_chapter` / `_update_chapter_navigation` | `ReaderController.prev/next_chapter` + `ReaderView.set_nav_enabled` | 会话 chapters 导航 |
| `_on_reader_key` / `_on_reader_key_down`（快捷键） | `ReaderView.on_key` 回调槽（外壳 `page.on_keyboard_event` 接线留 M6 导航全量切换） | 纯视图事件 |
| （旧单体未独立成型）书签 | `BookmarkPanel` + `ReaderController` | `BookmarkService.add/remove/list` → BookmarkChangedEvent |
| （旧单体未独立成型）书内搜索 | `ContentSearchBar` + `ReaderController` | `ContentSearchService.search/rebuild_index` |

## 3. 行为对齐说明

- **架构对齐**：视图只做 Flet 布局与渲染；状态迁移/编排全部在 Controller 与服务层。
  视图经回调槽（`on_*`）把动作上抛 Controller，Controller 经 `hub.get(XxxService)`
  取 EngineProxy 句柄，协程经 `engine_call` 派发到 EngineHub 守护 loop。
- **事件对齐 §4.3**：SearchFinishedEvent → FindBooks；ProgressSavedEvent /
  DownloadProgressEvent(0.5s 节流) → Bookshelf；BookmarkChangedEvent /
  ReadingSessionChangedEvent → Reader。
- **退订对称**：Controller.detach() 批量退订 attach 时登记的全部句柄（测试断言
  attach 后订阅数 >0、detach 后归零）。
- **冻结旧视图**：`app/views/reading_view.py` 当时未改一行（import 行亦未动），继续作为
  行为参照与 test_reader_ux（128 用例）守门基线。
  （2026-09-27 复核：两者均已在 M7 删除——`app/views/reading_view.py`、`tests/test_reader_ux.py`
  实测已不存在，此守门陈述已失效，新阅读视图回归由 `tests/test_views_reading_m2.py` 等承接。）

## 4. 与设计文档的偏差（有意为之，记录备查）

1. **工厂绑定位置**：`core/registry.py` 不 import Flet 视图（§4.6 红线），故三目的地
   的 `register(dest, factory)` 实际由 `app/views/reading/__init__.py:
   register_destinations(registry, hub)` 完成；core 只暴露目的地常量
   （`DEST_*` / `READING_DESTINATIONS`）与分组注释。这与 §7.3「工厂由装配处绑定」
   一致，未在 core 层越界 import 视图。
2. **分类浏览 / 书源登录 / 详情 book_info 为占位接线**：M2 服务契约只冻结了
   open_book/load_chapters/load_content/search_books/switch_source 等；分类 explore、
   书源登录交互（§4.6 浏览器回调）、book_info 分别属 M3 源域服务与 M4 内容形态。
   这些动作在 Controller 内留了入口与状态提示，待对应里程碑闭环——不属 M2 范围。
3. **整书缓存入参**：`DownloadService.enqueue("whole_cache", ...)` 的 source/book 由
   记录 dict 承载；完整 source 对象随 M3 SourceService 外壳化后由书架记录补齐。
4. **import-lint 实现**：按任务「扫描 import 行」语义，用 `ast` 只校验 Import/ImportFrom
   节点的模块名，docstring 中对红线的文字说明不计为命中（冻结根目录文件本就不在
   `views/reading/` 下，天然豁免）。

## 5. 验收数字

- 新测试：`tests/test_views_reading_m2.py` 13 条全绿。
- 全量回归：`python -m pytest tests/ -q` → 682 passed（含既有 641 + 新 13 + 子测试），
  0 failed。
- import-lint：`app/views/reading/` 全部 .py 经 AST 扫描，零 `app.services.*` import 命中。
