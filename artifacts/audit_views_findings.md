# Legado 桌面端 M1→M7 重构 只读架构审核 — 视图/控制器/导入链路/Flet API/打包

> 审核日期：2026-09-25（CST）。范围：`app/controllers/**`（18 个控制器）、`app/views/**`、导入链路端到端、Flet 0.86.5 API 合规、打包遗漏。**只读**，未修改任何源码。
> 探针解释器：`D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe`（flet 0.86.5）。pytest：`--basetemp=.cache/pytest/tmp_audit_b`，全量 829 passed。

## 探针环境事实（已实测）

- flet **0.86.5**；`ft.run(main, view=ft.AppView.FLET_APP)` 签名合法。
- `ft.Page` 同时有 `services` 与 `overlay` 属性；Service 控件必须走 `services`。
- `ft.Dropdown`：**有 `on_select`，无 `on_change`**（0.86 改名）。
- `ft.NavigationRail` / `ft.NavigationBar`：均有 `on_change`。
- `ft.Colors.GREY_600 / GREY_500 / ORANGE_800 / RED_300` 全部存在。
- **唯一被 deprecation 的按钮类是 `ft.ElevatedButton`**（→ 提示用 `ft.Button`）；Outlined/FilledTonal/Button/Text/Icon/FilledButton 均不告警。
- `app/resources/publicsuffixes.gz` 存在（41394 字节），spec datas 落点正确。

---

## 逐条发现

| 编号 | 分类 | 发现（文件+行号/方法） | 影响 | 修复建议 | 优先级 |
|---|---|---|---|---|---|
| 1 | 已确认问题 | **导航切换不触发 BaseController.detach()，事件订阅泄漏**。`app/views/navigation.py:229` `old = getattr(self.content.content, "detach", None)`——只在视图对象自身暴露 `detach` 时才退订。全仓唯一把 `view.detach` 接到控制器的是 `service_dashboard.py:278 view.detach = controller.detach`。其余 17 个视图工厂（`views/reading/__init__.py:75-96`、`views/source/__init__.py:69-72`、`views/tools/__init__.py:107-110`、`navigation.py:79-83`）都是 `Controller(hub).attach(view); return view`，从未把 `controller.detach` 暴露到 `view` 上；`BaseController.attach`（`base.py:42-54`）只写 `view.controller = self`，没写 `view.detach`。 | 每次离开找书/书架/阅读器/导入/列表/检测/各工具页，旧 controller 的 `self._subs` 永不退订。反复在两页间切换 N 次，就有 N 个老 controller 仍订阅 `SearchFinishedEvent / SourceChangedEvent / CheckFinishedEvent / ImportFinishedEvent / ProgressSavedEvent / DownloadProgressEvent` 等，并继续向已脱离页面的孤儿视图回写。内存与回调累积无界；同一事件被多次渲染。 | 在 `navigation.show()` 里把 detach 解析改成先看 `view.controller.detach`（dashboard 仍可用 `view.detach` 兜底）：`old_ctrl = getattr(self.content.content, "controller", None); detach = getattr(old_ctrl, "detach", None) or getattr(self.content.content, "detach", None)`。一行，低风险。 | **高** |
| 2 | 已确认问题 | **`on_open_reader` 是死槽位：找书/书架点"开读"根本不进阅读器**。`find_books_controller.py:125-130 read_book()`：`on_open_reader is None` 时只 `set_status("打开阅读：…")` 后 return；`bookshelf_controller.py:83-85 open_book()`：`on_open_reader is None` 时**静默什么都不做**。全仓 grep 仅这两处使用，**无任何赋值处**（tests 也 0 命中）。`NavigationHost.show` 只接线 `controller.nav_actions` 字典（`navigation.py:240-244`），而 FindBooksController/BookshelfController 都没声明 `nav_actions`，`on_open_reader` 不在其中。即便把它绑成 `show("reading.reader")`，`_build_reader`（`views/reading/__init__.py:87-90`）只是 new 一个空 ReaderView+ReaderController，**没有任何机制把 (source, book) 载荷传进去**，打开也是空壳。 | 用户从搜索结果或书架点"阅读"无实际效果——找书页只刷一行状态，书架页毫无反馈。阅读器目的地在导航栏可见但永远空开。 | 需要一条"带载荷导航"通道：例如 NavigationHost 持 `self._pending_book = (source, book)`，`open_reader(source, book)` 先存载荷再 `show("reading.reader")`；`ReaderController.on_attach` 检测到 `host.consume_pending_book()` 非空则 `engine_call(self.open_book(...))`。属设计补全，不是一行修复。 | **高** |
| 3 | 已确认问题 | **`future.add_done_callback` 回写视图的 detach 存活判断不一致**。已判 `if self._view is None: return` 的：`book_source_list_controller._render_sources`、`check_controller._on_all_done`、`code_editor_controller._render_audit`、`qr_code_controller._render_image/_render_decoded`、`update_controller._after_check/_after_download`、`replace_rule_controller._render`。**未判**（detach 后 `self._view=None` 会 AttributeError）：`code_editor_controller._after_save`(85) 与 `_render_debug`(173)、`webdav_controller._after`(107)、`replace_rule_controller._after`(96，内部又调 `load_rules()`→`self._view`)、`export_controller._after`(59)、`font_controller._render`(63) 及 remove_font 回调→`load_fonts()`(80)、`browser_controller._after_navigate`(56)、`dict_popup_controller._after_lookup`(50)。 | 当前因发现 #1（detach 根本不跑），`self._view` 永远非 None，这些坑不爆；一旦 #1 修掉，用户在引擎在途时切走页面，EngineHub 线程里就会对 `None.set_xxx()` 抛 AttributeError（被 Future 吞掉，但日志报"未获结果"且 UI 不更新）。 | 给上述每个回调首行补 `if self._view is None: return`。机械改动，低风险；建议与 #1 同批修。 | **中** |
| 4 | 已确认问题 | **`ft.ElevatedButton` deprecation（pytest 报的全部 DeprecationWarning 来源）**。4 处：`service_dashboard.py:282`、`reading/source_switch_dialog.py:33`、`reading/login_dialog.py:28`、`reading/find_books_view.py:244`。实测 warning 文案："ElevatedButton is deprecated since 0.80.0…Use Button instead."。pytest warning summary 去重后落在这 3 个文件位置（dashboard 6 个测试×、dialog 4 个×、login 1 个×），与"22 条"量级吻合（find_books_view:244 在测试里未触达，运行期才会补一条）。 | 1.0 版本移除时即崩；当前仅 warning。 | 机械替换 `ft.ElevatedButton(...)` → `ft.Button(...)`（构造参数兼容）。低风险，建议本轮就修。 | **低** |
| 5 | 已确认问题 | **FilePicker 在 `page.services` 里只增不减**。`navigation.py:248-256`：切到导入页时把 `view.file_picker / image_picker` append 进 `page.services`（有同实例去重 `picker not in services`），但切走旧视图时从不 remove。每次进入导入页都 new 两个新 FilePicker（`import_view.py:39,41`），旧实例永久留在 services。 | 轻微控件/内存累积；无功能错误（picker 无人引用即不触发）。 | 离开旧视图时，若旧 view 暴露 file_picker/image_picker，从 `page.services` 摘除。低优先。 | **低** |
| 6 | 需用户确认 | **`FilePicker.pick_files()` 的 future 被放到 EngineHub loop 上 await**。`source_import_controller.py:58/74` 拿到 `files_awaitable`，再包进 `async def _run()` 经 `self.engine_call(...)` 提交到 EngineHub 守护线程 loop。Flet 的 picker future 通常绑定在 Flet 客户端连接所在 loop；跨 loop await 可能挂起或抛 "got Future attached to a different loop"。现有无头测试用桩 page/桩 picker，不覆盖真实桌面弹窗。 | 真实桌面上选文件/图片后，`await files_awaitable` 可能不返回（导入按钮点了没反应）。 | 需在真桌面手测一次"选 JSON 文件→是否出结果"。若复现，改为在 picker 的 `on_result` 回调里处理结果（Flet 0.86 推荐通道），或把 picker future 用 `asyncio.run_coroutine_threadsafe` 桥回 Flet loop。 | **中** |
| 7 | 已确认（合规面通过） | **Flet 0.86 API 用法整体合规**：`ft.run(lambda page: build_app_shell(...), view=ft.AppView.FLET_APP)` 签名正确；Dropdown 未用 `on_change`（视图只取 `.value`）；NavigationRail/Bar `on_change` 存在；Colors 成员探针全部存在；FilePicker 走 `page.services`（`navigation.py:252` 用 getattr 兜底，无头不炸），**全仓无任何视图把 Service 控件塞 overlay**（grep `overlay` 仅命中 service_dashboard 注释与 rule_engine 注释）。 | — | 无需修。 | — |
| 8 | 已确认（红线通过） | **视图零 `app.services.*` import**：AST 守门 `tests/test_ui_safety.py` 跑通（829 passed）；grep `app.services` 在 views 下仅命中 docstring 叙述，无真实 import 节点。无函数内 import / try-except 包裹 / importlib 字符串绕行（views 下 grep `importlib`、`__import__` 均 0 命中）。 | — | 无需修。 | — |
| 9 | 已确认（导入链路正确） | **导入链路端到端正确**：`with_data=True` 已在 pick_file/pick_image 两处设置（`source_import_controller.py:62,78`），`_read_and_import`/`_run` 均先取 `file.bytes` 再 fallback `file.path`，path 为空时安全 return；`_current_kind()` 读 `view._kind.value`，Dropdown 有默认 `value="book_source"`，可靠；`OnlineImportService._dispatch` 对 pending/未知/解析失败/落库异常**每条路径都 `_emit`**（`online_import_service.py:157,174,180,187,196,206`），errors+warnings 合并进 `ImportFinishedEvent.errors`，`_on_import_finished` 渲染到结果区；`SourceService.import_sources_json` 按 bookSourceUrl 去重、逐条 try、`SourceChangedEvent(change_type="imported")` 落库发布。engine_call 链路不丢异常。 | — | 无需修。 | — |
| 10 | 设计取舍 | **事件 handler 在 EngineHub 线程直接写 Flet 控件属性**。`bus.publish` 把 handler 提交到 EngineHub loop（`bus.py:52`），handler 内直接 `self._view.render_xxx()`。service_dashboard docstring 已明示这是与 check_controller 等同款的既定选择。 | 依赖 Flet 控件 setter 线程安全（序列化到客户端命令流）；若非真线程安全，极端时序可能串扰。 | 记录为已知取舍；本轮不动。 | — |
| 11 | 设计取舍 / 已知 | **spec 硬编码本机绝对 DLL 路径**。`LegadoReader.spec` binaries 钉死 `D:\env_manger\minicaoda\...\vcruntime*.dll / ucrtbase.dll / api-ms-win-crt-* / libexpat.dll(2.8.2)`。M7 文档 §8.3 已记录根因（PyInstaller 误收 conda 根旧版 expat 2.7.1）。 | 换机器/换 env 重打包需改这些路径；非代码缺陷。 | 打包时以报错为准逐条对齐；文档已覆盖。 | **低** |

---

## 按优先级排序的摘要（每条一行）

- **[高] 导航切页不调 controller.detach，事件订阅累积泄漏**（`navigation.py:229` 只认 `view.detach`，全仓仅 dashboard 接线；其余 17 视图控制器订阅永退订不了）。
- **[高] `on_open_reader` 死槽位**：找书/书架点"开读"不进阅读器（`find_books_controller.py:125`、`bookshelf_controller.py:83`，无赋值处；且阅读器工厂无载荷通道）。
- **[中] add_done_callback 存活判断不一致**：8 个回调（code_editor/webdav/replace_rule/export/font/browser/dict_popup）未判 `self._view is None`，修了 #1 后会成崩溃点。
- **[中] 待真桌面验证**：`FilePicker.pick_files` future 被放到 EngineHub loop await（`source_import_controller.py:58,74`），可能跨 loop 挂起。
- **[低] 4 处 `ft.ElevatedButton`**（dashboard:282 / source_switch_dialog:33 / login_dialog:28 / find_books_view:244）→ 换 `ft.Button`，即 pytest 全部 DeprecationWarning 来源。
- **[低] FilePicker 只 append 进 page.services 不 remove**（`navigation.py:248`），切走后旧实例累积。
- **[低] spec 硬编码本机 DLL 绝对路径**（M7 文档已记录）。
- **通过项**：Flet 0.86 API 用法合规、视图零 services import、导入链路 with_data/bytes 兜底全链、错误告警全渲染、publicsuffixes.gz 落点正确。

## "确定能修且低风险"清单

1. 发现 #1：`navigation.show()` 里把 detach 解析改为 `view.controller.detach`（一行）——修完事件泄漏，且让 #3 的既有 guard 真正生效。
2. 发现 #3：给 8 个未 guard 的 done callback 首行补 `if self._view is None: return`（纯机械）。
3. 发现 #4：4 处 `ft.ElevatedButton` → `ft.Button`（构造参数兼容，pytest 全绿即可验）。
4. 发现 #5：切走时从 `page.services` 摘除旧 picker（与 #1 同处改）。

> 发现 #2（on_open_reader 带载荷导航）需补一个小的导航载荷通道，不是一行修复，但改动局限在 navigation + ReaderController.on_attach，建议本轮一并设计。
