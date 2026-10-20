# legodo_flet 页面功能清单

> 第一百十七轮盘点，2026-10-17。
>
> **口径**：逐视图读控件 → 找到它绑的控制器方法 → 找到控制器调的服务。"真"的判据是
> **这条链在代码里通到底**（点击 → 服务 → 回话落回界面），不是运行时验收——本轮**没有启动过 flet 应用**，
> 所有"真"都是静态判定。文件数与行数是当场 `find | wc -l` 数出来的，不是引用的旧数。
>
> 同一口径的 Dart 侧见 `docs/dart子项目功能清单.md`；两侧对照出的缺口与待拍板项在 `docs/dart补缺方案.md`。

---

## 1. 总体形状

| 项 | 事实 | 落点 |
|---|---|---|
| 进程模型 | 单进程：UI 与 `EngineHub` 同进程，服务在启动时全量注册 | `app/main.py:150-235` |
| 页面怎么拿数据 | **不走 gateway action**：控制器经 `self.service(X)` 取服务代理直调 | `app/controllers/base.py:8` |
| gateway METHODS 是什么 | 给 Dart/Go 的协议面清单（81 条），不是 flet 自己的调用表 | `app/gateway/methods.py:89-300` |
| 导航定义 | `NAV_META`：目的地 →（分组，标签，是否整页），顺序即侧栏顺序 | `app/core/registry.py:153-185` |
| 侧栏形状 | 3 个分组入口 = 3 个 `NavigationRailDestination`；组内非整页目的地变成那一页顶部的页签 | `app/views/navigation.py:310-313`、`:325` |
| 事件 | 服务发布事件 → 控制器订阅 → 只刷受影响的那一块（不是整表重取） | 各控制器 `:60-90` 一段 |

**因此**：数 METHODS 得不出"flet 用了多少功能"。页面能力必须逐屏读控件——本文件就是这么盘的。

### 规模（当场数）

| 面 | 数量 | 口径 |
|---|---|---|
| `app/` 下 Python 文件 | 150 | `find legodo_flet/app -name "*.py" \| wc -l` |
| 视图文件（`*_view.py` / `*_dialog.py` / `*_panel.py` / `*_bar.py`） | 26 | 同上按名筛 |
| 控制器文件 | 20 | `find app/controllers -name "*_controller.py"` |
| 服务文件 | 62 | `find app/services -name "*.py"` |
| `app/views/` 行数 | 7,902 | `wc -l` |
| 协议面 action | 81 | METHODS 正则去重（见 §5） |
| 依赖 | `flet>=0.86.0`、`flet-audio`/`flet-video`/`flet-webview` 钉 `<0.87` | `requirements.txt:12-15` |

---

## 2. 导航全表（`NAV_META` 逐条）

三个分组：`GROUP_SOURCE`（书源）→ `GROUP_READING`（阅读）→ `GROUP_CONFIG`（配置）。
第三项 `True` = 整页目的地（不进侧栏也不进页签，切换时独占内容区）。

| 分组 | 目的地 | 标签 | 整页 | 状态 |
|---|---|---|---|---|
| 书源 | `DEST_SOURCE_LIST` | 管理 | 否 | 注册 |
| 书源 | `DEST_SOURCE_IMPORT` | 导入 | 否 | 注册 |
| 书源 | ~~`DEST_SOURCE_CHECK`~~ | 检测 | — | **已注释**（`registry.py:157-158`），路由仍注册但无 UI 入口 |
| 书源 | `DEST_SOURCE_EDITOR` | 规则 | 否 | 注册 |
| 书源 | `DEST_SOURCE_REPAIR` | 自愈 | 否 | 注册 |
| 阅读 | `DEST_FIND_BOOKS` | 找书 | 否 | 注册 |
| 阅读 | `DEST_BOOKSHELF` | 书架 | 否 | 注册 |
| 阅读 | `DEST_RSS` | 订阅 | 否 | 注册 |
| 阅读 | `DEST_READER` | 阅读器 | **是** | 注册 |
| 阅读 | `DEST_MANGA_READER` | 漫画 | **是** | 注册 |
| 配置 | `DEST_TOOLS_OVERVIEW` | 更多工具 | 否 | 注册 |
| 配置 | `DEST_SETTINGS_APPEARANCE` | 外观 | 否 | 注册 |
| 配置 | `DEST_SETTINGS_SHORTCUTS` | 快捷键 | 否 | 注册 |
| 配置 | `DEST_SETTINGS_DASHBOARD` | 服务 | 否 | 注册 |
| 配置 | ~~`DEST_TOOLS_BROWSER`~~ | 浏览器 | — | **已注释**（`registry.py:174-175`）：最初用于检测时人工过 Cloudflare，现不再需要 |
| 配置 | ~~`DEST_TOOLS_DICT`~~ | 词典 | — | **已注释**（`registry.py:176-177`，任务 #45 删除） |
| 配置 | `DEST_TOOLS_REPLACE` | 替换规则 | 是 | 注册 |
| 配置 | `DEST_TOOLS_FONT` | 字体 | 是 | 注册 |
| 配置 | `DEST_TOOLS_QR` | 二维码 | 是 | 注册 |
| 配置 | `DEST_TOOLS_EXPORT` | 导出 | 是 | 注册 |
| 配置 | `DEST_TOOLS_WEBDAV` | 备份 | 是 | 注册 |
| 配置 | `DEST_TOOLS_UPDATE` | 更新 | 是 | 注册 |
| 配置 | `DEST_TOOLS_WEB_BRIDGE` | 网络桥 | 是 | 注册 |

页签容器那条"切签不退订、不重建子视图"的规矩在 `app/views/navigation.py:8-10`；
`show()` 对未注册目的地抛 `KeyError`（`navigation.py:455`）——这是 §4 那两张死卡片的机制。

---

## 3. 逐页清单

### 3.1 阅读组

| 页面 | 控件 → 下游 | 订阅事件 | 判定 |
|---|---|---|---|
| **书架** `app/views/reading/bookshelf_view.py`(344 行) | 搜索框(:39) / 分组下拉(:47) / 排序下拉三档 最近阅读·最近更新·书名(:51-57) / 列表↔网格切换(:58-60) / 刷新(:61-63) / **导入本地书籍 txt·epub·pdf**(:64-66) | `ProgressSavedEvent`、`DownloadProgressEvent`、`SourceChangedEvent`（ctrl `:82-85`） | 真 |
| ↑ 行菜单 | 换源(:281-286) / **整书缓存·取消缓存**(:287-292，本地书不给这一格) / **更换封面**(:293-296，本地书也能换) / **移出书架**(:297-301) | ↑ | 真 |
| ↑ 在途缓存任务区 | 任务行 + 取消(:90-153) → `DownloadService.enqueue('whole_cache')`(ctrl:318) | `DownloadProgressEvent` | 真 |
| **找书** `find_books_view.py`(385 行) | 搜索框 / 停止 / 范围 / 翻页 / 分类 / 详情 / 阅读 / **问题反馈**(:22-33, :55, :96-107) → `ReadingService.search_books`(ctrl:195/242)、`explore_categories/books`(ctrl:144/175)、`shelve_book`/`set_enabled`(ctrl:327/669) | `SearchFinishedEvent`、`SourceChangedEvent`(ctrl:79-81) | 真，**一处例外**：「应用修复」是假的——`find_books_controller.py:646` 写着 `# TODO: 调用 RuleRepairService.apply_fix(report_id)`，只弹一句"修复已应用"(:648) |
| ↑ 书源行 | **登录入口** → `RuleEngineService.login_schema`/`run_login_action`/`submit_login`(ctrl:394/444/462/494) | — | 真 |
| **阅读器** `reader_view.py`(1,163 行) | 工具栏 退出/目录/书签/书内搜索/设置/字号±(:227-245)；目录页搜索 + 倒序 + 分批懒加载(:147-174)；沉浸模式(:221, :346)；侧抽屉双页签(:318)；**修复条**(:269)；**划词条**(:296)；**视频面板**(:280)；阅读设置弹层(:884-961) | `BookmarkChangedEvent`、`ReadingSessionChangedEvent`、`MediaPlayStateEvent`(ctrl:177-179) | 真；**划词查词典是死链**：`reader_controller.py:705` 见 `self._dict is None` 就弹"词典服务未就绪，查不了词"（`DictService` 已从 `main.py:205/212` 摘掉），而 `:744` 还让用户去"更多工具→词典"——那个目的地也没注册 |
| ↑ 排版偏好 | **10 个键**：`font_size` 16 / `font_family` / `line_height` 1.7 / `paragraph_spacing` 12 / `max_width` 760（只认 620·700·760 三档，:58-59）/ `indent` / `text_align` / `background`（follow·paper·eye·night·eink 五色，:71-77）/ `show_status` / `text_selection` | — | `reader_view.py:42-57`；其中 7 键会触发正文重排（`_REFLOW_KEYS`，:68-69） |
| **漫画阅读器** `manga_reader_view.py`(259 行) | 滤镜 原图/灰度/墨水(:63-67) / 上一章·下一章(:70-74) / 目录(:79) / 分块懒加载(:38) / 缓存命中提示(:103, :236) → `ImageBookService.chapter_images`/`fetch_page_image`(ctrl:111/211)、`ReadingService.open_book`/`save_progress`(ctrl:75/253) | `ReadingSessionChangedEvent`(ctrl:66) | 真 |
| **订阅 RSS** `rss_view.py`(453 行) | 刷新全部 / 添加源 / 未读勾选 / 源三点菜单 / 文章行 / 收藏 / 三级跳转(:75-117, :214-219, :305-337) → `RssService.*`(ctrl:170-290) | `RssRefreshedEvent`、`RssRefreshProgressEvent`(ctrl:67-68) | 真 |
| 书签面板 `bookmark_panel.py`(73) | 加 / 跳 / 删 / 关(:33-70) | 由 reader 控制器驱动 | 真 |
| 书内搜索条 `content_search_bar.py`(75) | 搜索 + 跳命中(:36-74) → `ContentSearchService.ensure_index`/`search`(ctrl:898-899) | ↑ | 真 |
| 换源对话框 `source_switch_dialog.py`(100) | 开始 / 取消 / 确认候选(:35-95) → `ReadingService.switch_seeds`/`switch_source`/`apply_source_switch`(ctrl:404/435/502) | ↑ | 真 |
| **登录对话框** `login_dialog.py`(119) | 表单字段 + **自定义 loginAction 按钮** + 清除登录(:36-114) | — | 真 |
| **视频播放条** `video_play_bar.py`(268) | 播放 / 停 / 上 / 下 / 列表 / 收起(:86-108，后三键初值 disabled) → `MediaPlayService`(ctrl:1006/1011) | `MediaPlayStateEvent` | 真 |

### 3.2 书源组

| 页面 | 控件 → 下游 | 事件 | 判定 |
|---|---|---|---|
| **书源管理** `book_source_list_view.py`(254) | 搜索(:49) / 分组筛选(:53) / 状态筛选 / 刷新(:65) / 全选(:69) / **导入**(:72) / **导出**(:75) / **自愈**(:82) / 批量启停删(:95, :100-122) / 行勾选(:218) / **行内「编辑规则」按钮**(:229) / 删除(:231) / 启用开关(:234) | `SourceChangedEvent`(ctrl:77) | 真；「检测」按钮**已移除**(:34, :77-79 注释：改为找书页问题反馈流程)，路由仍在 `source/__init__.py:61` `hidden=True`，只有 `nav_actions["on_open_check"]`(ctrl:39) 能程序化跳过去 |
| ↑ 下游 | `SourceService.list_sources`/`load_source`/`delete_source`/`set_enabled`/`batch_*`/`export_sources_json`(ctrl:57-77)、`CompatAuditService`(:268) | | |
| **导入** `import_view.py`(248) | 选文件(:50-) / **选图片走二维码**(:-) / URL(:70) / 追加·替换单选 / 确认 / 取消(:95) → `OnlineImportService.import_from_file`/`import_from_url`，二维码 `QrCodeService.decode_image`(ctrl:253) | `ImportProgressEvent`、`ImportFinishedEvent`(ctrl:67-68) | 真；`kind` 只有 `book_source` 真落库，其余 kind 仅登记说明（`online_import_service.py:50`） |
| **规则代码编辑器** `code_editor_view.py`(347) | 书源下拉 + 字段下拉 + 重载(:157-166) / 编辑区(:169) / **示例正文**(:185) / 保存 / **试跑**(:206-209) → `load_source`/`list_sources`(ctrl:112/150)、`save_source` + `audit_source` | — | 真，试跑是真求值(ctrl:312-334) |
| **规则自愈** `rule_repair_view.py`(294) | 书源·章节下拉 + 重载(:57-73) / 开始诊断(:74) / 手动规则输入(:84) / 报告卡 忽略·应用(:210-214) → `RuleRepairService.repair`/`apply_fix`(ctrl:450/493) | `RepairProgressEvent`、`RepairReportReadyEvent`(ctrl:84-85) | 真 |
| **检测** `check_view.py`(113) | 开始 / 停止(:49-54) → `SourceCheckService.check_all`/`cancel`/`run_snapshot`(ctrl:99/150/162) | `CheckFinishedEvent`(ctrl:70) | 代码真但**用户不可达**（无导航入口） |

### 3.3 配置组

| 页面 | 控件 → 下游 | 判定 |
|---|---|---|
| **外观** `settings/appearance_view.py`(250) | 模式单选(:99-106) / 墨水屏开关(:108) / 皮肤下拉 `BUILTIN_SKINS`(:112) / **背景图 选图·清除**(:134, :176) / 保存·恢复默认(:186-189) → `ThemeService.get/set_preferences` + `BackgroundService.install/clear`(ctrl:195/219) | 真 |
| **快捷键** `settings/shortcut_view.py`(154) | 7 个阅读动作键位(:95-99 录制/清空) / 老板键开关(:44) / 保存·恢复(:71-74) → `ReadingService.get/save_reader_preference("shortcuts")`(ctrl:92/191)；键位常量表 `core/shortcuts.py:151-157` | 键位真；**老板键是死的**：`BossKeyService` 未注册（`main.py:204` 已注释、`:225-226` 已注释），`_wire_boss_key` 直接返回 None（`main.py:302-305`），界面自己也写明"BossKeyService 未注册，老板键不会生效"（`shortcut_controller.py:169`） |
| **服务仪表盘** `service_dashboard.py`(440) | 服务 DataTable 行 + 起/停/重启按钮(:242-243, :269-313) / 事件日志(:331-351) / 定时重读快照(:157-165)；状态非 READY 时按钮禁用(:107) | 真 |
| **更多工具** `tools/overview_view.py`(71) | 9 张卡片(:20-30) → `on_open_destination` | **两张死卡片**：`DEST_TOOLS_BROWSER`(:21) 与 `DEST_TOOLS_DICT`(:22) 未注册（`tools/__init__.py:80-87` 两段已注释）→ 点击进 `navigation.py:455` 抛 `KeyError` |
| 替换规则 `tools/replace_rule_view.py`(80) | 新建 / 全局开关 / 行开关 / 编辑 / 删除(:25-75) → `ReplaceRuleService.*`(ctrl:71-195) | 真 |
| 字体 `tools/font_view.py`(82) | 扫描系统 / 添加文件 / 删除 / 选中(:25-81) → `FontService.scan_system_fonts`/`add_font_file`/`remove_font`/**`list_user_fonts`**(ctrl:72-141)，选中写 `SettingsService` 的 `reader.font_family`(ctrl:167) | 真（用户字体区有独立读口 `list_user_fonts`） |
| 二维码 `tools/qr_code_view.py` | 生成 / **从图片识别** / 手输 JSON 导入(:29-45) → `QrCodeService.generate`/`decode_image`(ctrl:55/114) | 真 |
| 导出 `tools/export_view.py`(114) | **下拉选已缓存的书** + 刷新 + EPUB/TXT（未选书时 disabled，:30-42）/ 进度 / 结果(:64-114) → `BookExportService.list_exportable_books`/`export_epub`/`export_txt`(ctrl:83/119/121) | 真，但依赖先做整书缓存（ctrl:98 有那句提示） |
| WebDAV 备份 `tools/webdav_view.py`(134) | 地址/账号/令牌/保存(:32-54) / **备份对象 7 项选择**(:18-20) / 立即备份·列出远端·从云端恢复(:55-61) / 逐阶段进度(:113) → `WebDavService.backup`/`restore`/`list_remote`(ctrl:127/137/188) | 真；事件 `BackupProgressEvent`/`BackupFinishedEvent` |
| 更新 `tools/update_view.py`(73) | 检查更新 / 版本卡 / **下载到下载目录**(:24-34) → `UpdateService.check_update`/**`download`**(ctrl:56/88)，不自替换 | 真 |
| 网络桥 `tools/web_bridge_view.py`(116) | 启用开关 / 端口保存 / 刷新 / 重新生成令牌 / 端点清单(:33-53) → `WebBridgeService.set_enabled`/`set_port`/`regenerate_token`/`bridge_status`(ctrl:51-88) | 真 |
| 词典弹窗 `tools/dict_popup.py`(124) | 查词 / 加规则 / 编辑 / 删除(:34-120) | **不可达**：目的地未注册 + `DictService` 未注册 |

---

## 4. 已知死链与孤儿（flet 自己坏掉的那几处）

盘点对标时这些**不算"flet 有"**，别照抄。（另一类"别照抄"记在 §7：那些不是坏掉，是**靠 Flet 框架机制才成立**的形状，Dart 侧没有同一个机制。）

1. **词典**——目的地、服务、卡片三处只剩卡片。`registry.py:176-177` 注释、`tools/__init__.py:84-87` 注释、`main.py:205/212` 摘掉 `DictService`。剩下的两个后果：`reader_controller.py:705` 划词必弹"词典服务未就绪"，而 `:744` 指向的"更多工具→词典"是 `overview_view.py:22` 那张死卡片（点了抛 `KeyError`）。
2. **浏览器页**——`DEST_TOOLS_BROWSER` 未注册（`tools/__init__.py:80-83`）。CDP 服务本体还在（`services/interactive_browser.py:123`、`services/tools/browser_service.py:199`），但现在只当规则引擎的 webview fetcher 用，没有用户页。
3. **老板键**——见 §3.3 快捷键那一行。
4. **检测页**——代码与路由都在，导航入口被删（`registry.py:157-158`、`book_source_list_view.py:77-79`）。Dart 侧反而把它做成了页签（对照见缺口文档）。
5. **`views/common/` 那批组件**——`FailureBanner`/`EmptyState`/`LoadingSpinner`/`StatusChip`/`StateBar`/`make_card` 除自身包（`common/__init__.py:5-17` 导出）外**在 app/ 内零引用**：孤儿组件。
6. **找书页「应用修复」**——`find_books_controller.py:646` 是 TODO，弹的是假成功。真修复在 `rule_repair_view.py` 那一屏。

---

## 5. 协议面 METHODS（81 条，按域）

`app/gateway/methods.py:89-300`。这一张表是**给 Dart/Go 看的契约**，不是 flet 页面的调用表（§1）。

| 域 | 条数 | 明细（行号） |
|---|---|---|
| engine | 4 | `hello`:90 / `status`:92 / `shutdown`:94 / `webContentReady`:237 |
| bookshelf | 2 | `list`:96 / `add`:98 |
| source | 10 | `list` `get` `save` `delete` `enable` `group` `batch_enable` `batch_delete` `export`:101-119 / `audit`:121 |
| import | 2 | `text`:123 / `url`:125 |
| reader | 13 | `open` `close` `toc` `content` `position_get` `position_save` `staged` `staged_promote` `staged_discard` `bookmarks` `bookmark_add` `bookmark_remove` `search_content`:127-164 |
| browse | 2 | `categories`:168 / `books`:170 |
| search | 3 | `scopes` `start` `stop`:172-177 |
| switch | 5 | `seeds` `scan` `snapshot` `cancel` `apply`:179-190 |
| rss | 10 | `sources` `articles` `article` `add_source` `remove_source` `refresh` `refresh_all` `mark_read` `star`:194-220 |
| check | 4 | `start` `snapshot` `cancel` `source`:221-228 |
| settings | 2 | `read`:229 / `write`:231 |
| theme | 2 | `preferences`:233 / `save`:235 |
| update | 2 | `check`:242 / `ignore`:244 |
| browser | 3 | `open_page` `navigate` `close`:248-253 |
| replace | 5 | `list` `add` `update` `delete` `toggle_global`:255-266 |
| font | 4 | `scan_system` `add_user` `remove` `select`:268-275 |
| qr | 2 | `generate`:277 / `decode`:279 |
| export | 2 | `epub` `txt`:282/284 |
| webdav | 3 | `backup` `restore` `list_backups`:287-292 |
| webbridge | 3 | `start` `stop` `status`:294-299 |

作业（job）模式四条：`search.start`、`switch.scan`、`rss.refresh_all`、`check.start`。

**注意 flet 页面并不受这张表约束**：书架的"移出书架 / 更换封面 / 整书缓存 / 导入本地书"、找书的"登录"、阅读器的"划词 / 视频 / 修复 / 10 键排版"都直调服务，其中大部分**在 METHODS 里没有对应 action**。拿 METHODS 当功能清单，会正好漏掉 flet 比 Dart 多的那批能力。

---

## 6. 运行状态（本轮未验证）

- `requirements.txt:12` 仍钉 `flet>=0.86.0`，`flet-audio`/`flet-video`/`flet-webview` 带着「G4-F2 待 Dart 工具域完成后移除」的注释。
- `legodo_flet/README.md` 给 4 种启动模式：`python app/main.py`、`--serve`、`--connect`、`--smoke-test`。
- `docs/dev-logs/2026-10-12.md:395/403` 写明"当前产品 UI 仍是 flet""Python 侧仍有 32 个 flet views/controllers 在跑，直接删会让整个 UI 无法启动"，删除动作排到 G4-F3。
- Go 后端重实现提交 `7372ebf` 之后 flet 仍有 `fa02ad6` / `d8f78f7` / `74b72d5` 等功能提交。

**结论：flet 是能跑的旧应用、并且一直作为功能基准在被维护，不是空壳。** 本轮没有执行过 `--smoke-test` 或任何启动验证，所以上面每条"真"都只是静态判定；要拿某一屏做补缺验收的对照，需要先当场跑一次那一屏。

---

## 7. 框架机制依赖（这一批"有"不是能力清单）

§5 末尾那句"注意 flet 页面并不受这张表约束"只说了事实的一半。另一半是：**下面这几处的"有"，成立条件写在 Flet/Python 这一侧的机制上**，不是写在"用户要办的那件事"上。对照 Dart 时它们要么根本不构成缺口，要么只能作为"按 Flutter 自己的方式另立功能"——逐条的归类与处置在 `docs/dart补缺方案.md` §3（架构差 X / 结构差 S）。

| 那一屏 | 它靠的框架机制（当场核） | 编号 |
|---|---|---|
| 服务仪表盘上的**起停/重启** | 控制器同进程直接调 hub 编排方法：`self._submit(self._hub.restart_service(service_id))`（`views/service_dashboard.py:301`）；`base.py:8` 那句 docstring 就写着 `self.service(X)` 返回的是**对象**（EngineProxy），不是 action 名 | X1 |
| 更新页的**当前版本** | 直接读服务属性 `self._svc.current_version`（`controllers/tools/update_controller.py:40`，那里还专门写了注释禁止 `getattr` 探测） | X2 |
| 正文插图、缓存封面、本地图片怎么画出来 | aiohttp 网关上的一条二进制出口 `web.get("/media/{key}", self._h_media)`（`services/gateway/service.py:228`、`:714-746`：两种 key 形状 + Range 语义）。字节走 HTTP，**不走 METHODS 那 81 条** | X3 |
| 阅读器的**视频条** | `views/reading/video_play_bar.py:129,136` 在视图层**直接 `import flet_video` 自建控件**——文件头 :4 写明"绕过服务"，:20 那条红线写明这个文件不许 import 任何 `app.services.*`；服务侧只有一套 `VideoBackend` 抽象管会话与位置（`services/content/media_play.py` 文件头）。协议面里 `media` 零命中 | S1 |
| 漫画页的**取图与调色** | `ImageBookService` 三条能力：`chapter_images` 解析 URL 列表、`fetch_page_image` 走引擎网络栈取**字节**、`apply_color_filter` 走 Pillow 预渲染（文件头 :1-7）。后两条分别依赖 X3 的字节出口与 Pillow | S2 |
| 外观页的**背景图** | 服务只管文件与偏好（`services/tools/background_service.py` 文件头：Pillow 归一 + 拷进 `<data>/backgrounds/`）；把图落成窗口那一层在 `views/navigation.py:410-422` `set_background`——**重建整棵根控件树再挂回页面**，注释里那条"路径没变时什么都不做，因为重建根树会掀掉在途渲染"就是这个机制的代价 | S3 |
| 快捷键页与**按键** | `page.on_keyboard_event` 在装配根接线、由导航层分发给视图（`views/navigation.py:697-700`、`controllers/reader_controller.py:184,234`、`services/tools/boss_key_service.py:136`）。老板键本身 flet 也是死的（§4.3） | S4 / X5 |
| 远程书源管理 / 调试面板 | `WebBridgeService` 用 aiohttp **再起一个 HTTP + WebSocket 监听**（默认绑 127.0.0.1、令牌校验、EventBus 推给浏览器）——文件头四条红线全在讲这个监听（`services/tools/web_bridge_service.py:1-20`） | X6 |
| 每一屏的**进度与局部刷新** | `subscribe(Event, cb)` 让服务把回调直接挂到控件上、`ui_call` 把编排协程排进界面循环后再 `flush` 一次（`app/controllers/base.py:5-8`）。所以它可以"只刷受影响那一块"，不需要轮询 | S6 |

一张表里最该记住的是最后一行的**反面**：Dart 侧唯一的门是 `Call(action, paramsJSON)`，没有对象引用、没有推送、也没有 HTTP。把上面任何一行当成"dart 少做的一屏"来补，都会补出一个没有消费方的控件。

