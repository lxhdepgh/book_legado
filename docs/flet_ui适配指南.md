# Flet UI 适配当前服务层设计文档

**版本**: v1.0  
**日期**: 2026-10-11  
**目标**: 指导从 git 历史恢复的 Flet UI 代码（commit f867150）适配 G1-G4 迁移后的服务层

---

## 1. 架构概览

### 1.1 当前服务层状态

```
app/services/
├── engine/              # 规则引擎域（4个文件）
│   ├── rule_engine_service.py
│   ├── legado_rule_parser.py
│   ├── java_regex.py
│   └── ...
├── reading/             # 阅读域（6个服务）
│   ├── reading_service.py
│   ├── bookmark_service.py
│   ├── book_cache_service.py
│   ├── image_book_service.py
│   ├── download_service.py
│   └── content_search.py
├── sources/             # 书源域（5个服务）
│   ├── source_service.py
│   ├── source_check_service.py
│   ├── online_import_service.py
│   ├── compat_audit_service.py
│   └── rule_repair_service.py
├── content/             # 内容形态域（4个服务）
│   ├── rss_service.py
│   ├── read_aloud.py
│   ├── media_play.py
│   └── local_book_service.py
└── tools/               # 工具域（13个服务）
    ├── theme_service.py
    ├── update_service.py
    ├── browser_service.py
    ├── replace_rule_service.py
    ├── font_service.py
    ├── qr_code_service.py
    ├── book_export_service.py
    ├── web_dav_service.py
    ├── web_bridge_service.py
    ├── background_service.py
    ├── boss_key_service.py      # ⚠️ 依赖缺失常量
    └── win_tray.py
```

**已删除服务**：
- ❌ `dict_service.py` - 词典服务（任务 #45 删除）

### 1.2 GatewayService Action Table

**位置**: `app/gateway/methods.py`  
**总计**: 60+ actions，分 18 个命名空间

| 命名空间 | Actions 数量 | 示例 |
|---------|------------|------|
| `engine.*` | 3 | hello, status, shutdown |
| `bookshelf.*` | 2 | list, add |
| `source.*` | 10 | list, get, save, delete, enable, group, batch_enable, batch_delete, export, audit |
| `reader.*` | 13 | open, close, toc, content, position_get, save, staged, promote, discard, bookmarks, bookmark_add, remove, search_content |
| `browse.*` | 2 | categories, books |
| `search.*` | 3 | scopes, start, stop |
| `switch.*` | 5 | seeds, scan, snapshot, cancel, apply |
| `rss.*` | 9 | sources, articles, article, add_source, remove_source, refresh, refresh_all, mark_read, star |
| `check.*` | 4 | start, snapshot, cancel, source |
| `settings.*` | 2 | read, write |
| `theme.*` | 2 | preferences, save |
| `readaloud.*` | 6 | speak, stop, set_voice, set_rate, profiles, add_profile, remove_profile |
| `update.*` | 2 | check, ignore |
| `browser.*` | 3 | open_page, navigate, close |
| `replace.*` | 5 | list, add, update, delete, toggle_global |
| `font.*` | 4 | scan_system, add_user, remove, select |
| `qr.*` | 2 | generate, decode |
| `export.*` | 2 | epub, txt |
| `webdav.*` | 3 | backup, restore, list_backups |
| `webbridge.*` | 3 | start, stop, status |

**注意**：❌ **没有 `dict.*` actions**

---

## 2. 关键 API 变更

### 2.1 缺失的快捷键常量

**问题**: `app/core/shortcuts.py` 中以下常量已被删除，但 `BossKeyService` 和 `ShortcutController` 仍在引用：

```python
# ❌ 已删除（需恢复）
BOSS_KEY_ENABLED = "bosskey.enabled"
BOSS_KEY_SHORTCUT = "bosskey.shortcut"
DEFAULT_BOSS_KEY = "ctrl+alt+h"
BOSS_ACTION = ShortcutAction("boss_key", "老板键", "立刻藏起整个窗口，点托盘图标回来")
```

**影响范围**:
- `app/services/tools/boss_key_service.py:30-32` - 导入失败导致模块无法加载
- `app/controllers/settings/shortcut_controller.py:22-25` - 导入失败导致快捷键页崩溃

**修复方案**: 在 `app/core/shortcuts.py` 末尾添加上述常量定义

### 2.2 新事件系统（Frozen Dataclasses）

**位置**: `app/core/events.py`  
**特性**: 所有事件都是不可变数据快照（`@dataclass(frozen=True)`）

**M1-M6 已落地的事件**（18 个）：

```python
# M1 基础
ServiceStateChangedEvent(service_id, state, error)
SettingsChangedEvent(key, value, old_value)

# M2 阅读域
ProgressSavedEvent(book_url, chapter_index, position)
BookmarkChangedEvent(book_url, op)
DownloadProgressEvent(task_id, kind, progress, speed)
SearchFinishedEvent(search_id, items, finished)
ReadingSessionChangedEvent(book_url, chapter_index)

# G3-D3 换源扫描
SourceSwitchProgressEvent(switch_id, done, total, failures, starved)
SourceSwitchFinishedEvent(switch_id, candidates, cancelled, error)

# M3 书源域
SourceChangedEvent(change_type, source_id)
CheckFinishedEvent(source_id, state_class, summary, done, total)
RepairReportReadyEvent(report_id, source_id)
RepairProgressEvent(source_id, stage)
ImportFinishedEvent(kind, count, errors)
ImportProgressEvent(kind, phase, done, total)

# M4 内容形态域
ReadAloudStateEvent(state, book_url, chapter, position)
MediaPlayStateEvent(state, book_url, chapter)
RssRefreshedEvent(source_id, new_count)
RssRefreshProgressEvent(done, total, current, failures, refresh_id)

# M5 工具域
ReplaceRulesChangedEvent(ids)
BackupFinishedEvent(kind, ok, detail)
BackupProgressEvent(kind, stage, done, total)
UpdateAvailableEvent(version, notes, url)
ExportProgressEvent(kind, stage, done, total)
```

**适配要点**:
- 旧 Flet 代码可能使用可变 dict 作为事件载荷
- 新代码必须使用 frozen dataclass
- 事件处理器自动编组回界面循环并 flush

### 2.3 RPC 协议

**请求信封**:
```json
{
  "v": 1,
  "id": "c-17",
  "action": "bookshelf.list",
  "seq": 17,
  "payload": {...}
}
```

**响应信封**:
```json
{
  "v": 1,
  "id": "c-17",
  "ok": true,
  "data": [...],
  "error": null,
  "code": null
}
```

**错误码**:
- `unavailable` - 服务未注册
- `timeout` - 超时
- `cancelled` - 被取消
- `bad_request` - 信封不合法
- `engine` - 业务异常
- `schema` - payload 校验失败

**适配要点**:
- Flet UI 如需通过 HTTP/WebSocket 通信，必须使用 action table 中的动作
- 参数必须与 `app/gateway/schema/<action>.json` 严格对应
- 不在表中的方法无法被外部调用

---

## 3. Controller-Service 接口模式

### 3.1 服务访问模式

**旧模式**（Flet 历史代码）:
```python
# ❌ 直接实例化服务
service = MyService()
result = service.some_method()
```

**新模式**（G1-G4 后）:
```python
# ✅ 通过控制器获取 EngineProxy 包装的句柄
class MyController(BaseController):
    async def do_something(self):
        service = self.service(MyService)  # EngineProxy
        result = await service.some_method()  # 协程方法自动派发
```

**服务间装配**:
```python
# ✅ 服务内部取原始实例
class MyService(Service):
    def on_start(self):
        other = self.hub.require(OtherService)  # 原始实例，非代理
        value = other.get_config()  # 同步调用
```

### 3.2 事件订阅模式

**旧模式**:
```python
# ❌ 直接监听回调
service.on_progress = lambda p: update_ui(p)
```

**新模式**:
```python
# ✅ 通过控制器订阅
class MyController(BaseController):
    def on_attach(self):
        self.subscribe(DownloadProgressEvent, self._on_progress)
    
    def _on_progress(self, event: DownloadProgressEvent):
        # 自动编组回界面循环
        self.update_progress(event.progress)
```

### 3.3 异步编排

**原则**: 所有涉及服务的操作必须经 `controller.ui_call(coro)` 派发

```python
# ✅ 正确做法
async def load_books(self):
    source = self.service(SourceService)
    books = await source.list_enabled()
    self.view.update_books(books)

# ❌ 错误做法（直接在视图回调中 await）
def on_button_click(e):
    asyncio.run(load_books())  # 会阻塞界面循环
```

---

## 4. 具体不兼容点与修复方案

### 4.1 DictService 引用

**受影响文件**:

1. **`app/controllers/reader_controller.py:40`**
   ```python
   # ❌ 硬导入会失败
   from app.services.tools.dict_service import DictService
   
   # ✅ 修复为条件导入
   try:
       from app.services.tools.dict_service import DictService
   except ImportError:
       DictService = None
   ```

2. **`app/views/tools/__init__.py:86`**
   ```python
   # ❌ 要求不存在的服务
   registry.register(DEST_TOOLS_DICT, factory_func,
                     required_services=("DictService",), hidden=True)
   
   # ✅ 修复方案 A：移除依赖要求
   registry.register(DEST_TOOLS_DICT, factory_func,
                     required_services=(), hidden=True)
   
   # ✅ 修复方案 B：完全移除词典路由
   # 删除该行及相关的 DEST_TOOLS_DICT 定义
   ```

3. **`app/controllers/tools/dict_popup_controller.py`**
   - 已有容错处理，无需修改

### 4.2 BossKey 常量缺失

**修复步骤**:

在 `app/core/shortcuts.py` 末尾添加：

```python
# ========== 老板键常量（G4-E 后保留，供 BossKeyService 使用）==========
#: 老板键启用开关偏好键名
BOSS_KEY_ENABLED = "bosskey.enabled"

#: 老板键快捷键偏好键名
BOSS_KEY_SHORTCUT = "bosskey.shortcut"

#: 默认老板键组合
DEFAULT_BOSS_KEY = "ctrl+alt+h"

#: 老板键动作定义
BOSS_ACTION = ShortcutAction(
    "boss_key",
    "老板键",
    "立刻藏起整个窗口，点托盘图标回来"
)
```

### 4.3 导航目的地注册

**检查清单**:

| 目的地常量 | 模块路径 | 状态 | 操作 |
|-----------|---------|------|------|
| `DEST_BOOKSHELF` | `app/views/reading/bookshelf_view.py` | ✅ 正常 | 无 |
| `DEST_SOURCE_LIST` | `app/views/source/book_source_list_view.py` | ✅ 正常 | 无 |
| `DEST_FIND_BOOKS` | `app/views/reading/find_books_view.py` | ✅ 正常 | 无 |
| `DEST_READER` | `app/views/reading/reader_view.py` | ✅ 正常 | 无 |
| `DEST_RSS` | `app/views/reading/rss_view.py` | ✅ 正常 | 无 |
| `DEST_MANGA` | `app/views/reading/manga_reader_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_BROWSER` | `app/views/tools/browser_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_DICT` | `app/views/tools/dict_popup.py` | ❌ 服务缺失 | 移除或改为可选依赖 |
| `DEST_TOOLS_EXPORT` | `app/views/tools/export_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_FONT` | `app/views/tools/font_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_QR` | `app/views/tools/qr_code_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_REPLACE` | `app/views/tools/replace_rule_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_TTS` | `app/views/tools/tts_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_UPDATE` | `app/views/tools/update_view.py` | ✅ 正常 | 无 |
| `DEST_TOOLS_WEBDAV` | `app/views/tools/webdav_view.py` | ✅ 正常 | 无 |
| `DEST_SETTINGS_APPEARANCE` | `app/views/settings/appearance_view.py` | ✅ 正常 | 无 |
| `DEST_SETTINGS_SHORTCUT` | `app/views/settings/shortcut_view.py` | ⚠️ 依赖常量 | 先恢复 shortcuts.py 常量 |

---

## 5. 重构执行计划

### 阶段 1: 紧急修复（阻断性问题）

**预计时间**: 15 分钟

1. **恢复 shortcuts.py 常量** (5 分钟)
   ```bash
   # 编辑 app/core/shortcuts.py，添加 BOSS_KEY_* 常量
   ```

2. **修复 reader_controller 导入** (2 分钟)
   ```python
   # 将硬导入改为条件导入
   try:
       from app.services.tools.dict_service import DictService
   except ImportError:
       DictService = None
   ```

3. **处理词典路由** (8 分钟)
   - 选项 A：从 `app/views/tools/__init__.py` 移除 `DEST_TOOLS_DICT` 注册
   - 选项 B：将 `required_services` 改为空元组，视图内自行处理服务缺失

### 阶段 2: 服务访问模式迁移

**预计时间**: 2-3 小时

**检查清单**（逐个文件核对）:

#### Controllers (24 个文件)
- [ ] `app/controllers/bookshelf_controller.py`
- [ ] `app/controllers/find_books_controller.py`
- [ ] `app/controllers/reader_controller.py`
- [ ] `app/controllers/rss_controller.py`
- [ ] `app/controllers/manga_reader_controller.py`
- [ ] `app/controllers/settings/appearance_controller.py`
- [ ] `app/controllers/settings/shortcut_controller.py`
- [ ] `app/controllers/source/*.py` (5 个文件)
- [ ] `app/controllers/tools/*.py` (10 个文件)

**检查要点**:
- 是否直接实例化服务？→ 改为 `self.service(ServiceType)`
- 是否直接调用服务同步方法？→ 改为 `await service.method()`
- 事件订阅是否使用 `self.subscribe(EventType, handler)`？

#### Views (38 个文件)
- [ ] `app/views/navigation.py`
- [ ] `app/views/reading/*.py` (10 个文件)
- [ ] `app/views/source/*.py` (5 个文件)
- [ ] `app/views/settings/*.py` (2 个文件)
- [ ] `app/views/tools/*.py` (11 个文件)

**检查要点**:
- 是否在视图回调中直接 await 服务方法？→ 改为经控制器编排
- 是否直接访问服务对象属性？→ 改为通过控制器间接访问

### 阶段 3: 事件系统适配

**预计时间**: 1-2 小时

**检查清单**:
- [ ] 所有自定义事件是否改为 frozen dataclass？
- [ ] 事件载荷是否为纯数据（dict/list/scalar）？
- [ ] 事件处理器是否自动编组回界面循环？

**示例**:
```python
# ❌ 旧模式
event = {"type": "progress", "value": 50}
bus.emit(event)

# ✅ 新模式
from app.core.events import DownloadProgressEvent
event = DownloadProgressEvent(task_id="xxx", kind="book", progress=50, speed="1.2MB/s")
bus.emit(event)
```

### 阶段 4: 集成测试

**预计时间**: 1 小时

**测试用例**:
1. 启动应用 → 验证所有服务正常注册
2. 打开书架页 → 验证 bookshelf.list action 可用
3. 打开书源列表 → 验证 source.list action 可用
4. 打开阅读器 → 验证 reader.open/content 等 actions 可用
5. 打开设置页 → 验证 settings.read/write 可用
6. 触发老板键 → 验证 BossKeyService 正常工作
7. 尝试打开词典页 → 应显示"功能暂未实现"而非崩溃

---

## 6. 推荐决策

### 6.1 词典功能去留

**选项 A: 彻底移除**（推荐）
- 从 `app/views/tools/__init__.py` 移除 `DEST_TOOLS_DICT` 注册
- 删除 `app/views/tools/dict_popup.py`
- 删除 `app/controllers/tools/dict_popup_controller.py`
- 清理 `app/core/registry.py` 中的 `DEST_TOOLS_DICT` 常量

**优点**: 减少维护成本，符合 Windows 工具域简化方向  
**缺点**: 失去查词功能

**选项 B: 恢复服务**
- 从 git 历史恢复 `app/services/tools/dict_service.py`
- 修复所有相关引用

**优点**: 功能完整  
**缺点**: 增加依赖复杂度，与任务 #45 决策冲突

### 6.2 老板键功能去留

**现状**: BossKeyService 仍存在，但依赖的快捷键常量已删除

**推荐**: 恢复常量（见 §4.2），保留老板键功能
- 理由：Windows 桌面端核心功能，用户依赖度高
- 成本：仅需添加 4 行常量定义

---

## 7. 附录：完整服务清单

### 7.1 可用服务（25 个）

| 服务类 | 模块路径 | Action 命名空间 | 备注 |
|--------|---------|----------------|------|
| ReadingService | `app.services.reading.reading_service` | reader.* | 核心阅读服务 |
| BookmarkService | `app.services.reading.bookmark_service` | - | 书签管理 |
| ContentSearchService | `app.services.reading.content_search` | reader.search_content | 书内搜索 |
| BookCacheService | `app.services.reading.book_cache_service` | - | 整书缓存 |
| ImageBookService | `app.services.reading.image_book_service` | - | 漫画支持 |
| DownloadService | `app.services.reading.download_service` | - | 下载管理 |
| SourceService | `app.services.sources.source_service` | source.*, bookshelf.* | 书源 CRUD |
| SourceCheckService | `app.services.sources.source_check_service` | check.* | 书源检测 |
| OnlineImportService | `app.services.sources.online_import_service` | - | 在线导入 |
| CompatAuditService | `app.services.sources.compat_audit_service` | source.audit | 兼容性审计 |
| RuleRepairService | `app.services.sources.rule_repair_service` | - | 规则自愈 |
| RssService | `app.services.content.rss_service` | rss.* | RSS 订阅 |
| ReadAloudService | `app.services.content.read_aloud` | readaloud.* | TTS 朗读 |
| MediaPlayService | `app.services.content.media_play` | - | 视频播放 |
| LocalBookService | `app.services.content.local_book_service` | - | 本地书籍 |
| ThemeService | `app.services.tools.theme_service` | theme.* | 主题管理 |
| UpdateService | `app.services.tools.update_service` | update.* | 更新检查 |
| BrowserService | `app.services.tools.browser_service` | browser.* | 浏览器探针 |
| ReplaceRuleService | `app.services.tools.replace_rule_service` | replace.* | 替换规则 |
| FontService | `app.services.tools.font_service` | font.* | 字体管理 |
| QrCodeService | `app.services.tools.qr_code_service` | qr.* | 二维码 |
| BookExportService | `app.services.tools.book_export_service` | export.* | 书籍导出 |
| WebDavService | `app.services.tools.web_dav_service` | webdav.* | WebDAV 备份 |
| WebBridgeService | `app.services.tools.web_bridge_service` | webbridge.* | 网络桥 |
| BossKeyService | `app.services.tools.boss_key_service` | - | 老板键（需恢复常量） |

### 7.2 基础设施服务（4 个）

| 服务类 | 模块路径 | 备注 |
|--------|---------|------|
| ServiceHub | `app.core.hub` | 总控制器 |
| EventBus | `app.core.bus` | 事件总线 |
| StorageService | `app.core.storage` | 持久化存储 |
| SettingsService | `app.core.settings` | 偏好设置 |

### 7.3 已删除服务（1 个）

| 服务类 | 删除原因 | 替代方案 |
|--------|---------|---------|
| DictService | 任务 #45：简化 Windows 工具域 | 无（功能废弃） |

---

**文档维护**: 本随服务层演进而更新，每次增删服务或修改 action 表时同步修订。
