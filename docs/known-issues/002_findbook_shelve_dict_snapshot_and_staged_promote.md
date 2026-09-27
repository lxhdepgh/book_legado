# 002 界面到服务的书条目必须是对象，试读记录「加入书架」要转正

- 记录日期：2026-09-25
- 状态：已解决（补了回归测试）
- 影响面：找书页「加入书架」、书架试读行、换源

## 症状

1. 找书页点结果行 → 详情对话框 →「加入书架」，状态栏报
   `AttributeError: 'dict' object has no attribute 'url'`，书进不了书架（死链）。
2. 修掉第 1 条后，同一次操作变成 `入架失败：秘境测试`——明明刚才试读过，却被判"重复"拒绝。

## 根因

两处契约在重构中丢了：

- **UI↔服务边界传的是字典快照**。搜索事件、详情对话框携带的书是 `dict`（`{"name":…, "url":…}`），
  而 `SourceService` 按属性读取（`book.url`）。控制器直接透传，服务侧就炸在
  `getattr(book, "url")` 上。还原点必须是控制器的服务边界，不是视图。
- **Legado 的 notShelf 语义**：找书点开即"试读"，落的是 `in_bookshelf=False` 的暂存行，
  key 与正式行完全相同（同一 `(source_url, url)`）。所以「加入书架」在已有暂存行时
  不是"新增"，而是把那一行**转正**（`promote_to_bookshelf`）。按"新增"实现就会被幂等逻辑
  判成重复而拒绝。

## 解决

- `app/controllers/find_books_controller.py::_do_shelve()`：进服务前 `as_book_item(book)` 还原成书条目。
- `app/services/sources/source_service.py::shelve_book()`：`shelved=True` 时先尝试幂等新增，
  再对该 `(source_url, url)` 调 `promote_to_bookshelf()`，任一步生效即返回 `True` 并发布
  `SourceChangedEvent`。
- 回归测试：
  `tests/test_views_reading_m2.py::FindBooksShelveSnapshotTests.test_shelve_passes_book_object_not_dict`
  （断言服务收到的是对象而非 dict）、
  `tests/test_offline_source_chain.py::test_staged_trial_book_promoted_when_shelved`
  （试读→转正→幂等，二转返回 False 且书架只剩 1 行）。

## 复发时先查

- 加/改任何"把 UI 数据交给服务"的调用点，先看服务签名要的是对象还是 dict；
  跨界面/事件边界一律 `as_book_item()`。
- 判定"是否已在书架"必须区分 `shelved` 与 `staged`：同 key 的暂存行不是重复。
- 换源、删书、转正共用同一 `(source_url, url)` key，改一处要同步想另外三处。
