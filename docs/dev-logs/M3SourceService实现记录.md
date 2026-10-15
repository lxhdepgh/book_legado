# M3 SourceService 外壳化实现记录

> 一线交付：M3 三线并行中的「书源域 SourceService 外壳化 + notShelf + 1.18 改封面
> + ReadingService 获取点切换」。
> 权威接口：`M3书源域契约.md` §2/§3；行为基准：`docs/桌面端重构设计.md` §1.6/1.18、
> §3.8、§4.2/4.3、§9.2；notShelf 语义锁定：`docs/找书与换源逻辑对齐.md` §1.5。

## 1. 交付文件清单

| 交付物 | 落点 | 说明 |
| --- | --- | --- |
| A 外壳服务 | `app/services/sources/source_service.py`（新建） | 继承旧 `app.services.source_service.SourceService` + `Service` |
| B 事件 | `app/core/events.py` | `SourceChangedEvent(change_type, source_id, timestamp)` |
| C 集成点 | `app/services/reading/reading_service.py` | `on_start` 懒取 `SourceService` |
| D 测试 | `tests/test_sources_service_m3.py`（新建） | 17 条服务级测试 |
| E 本文档 | `docs/M3SourceService实现记录.md` | — |

未改动：旧 `app/services/source_service.py`（外壳化源，方法体零改动，仍被旧视图/M1 测试直用）。

## 2. 逐服务落点与改动

### 2.1 `SourceService`（services/sources/source_service.py）

- **继承复用（行为零改动）**：`class SourceService(_LegacySourceService, Service)`。
  书源 CRUD/分组/统计/批量、`import_sources`/`filter`、书架全套
  （`add_to_bookshelf`/`shelved_books`/`discard_unshelved_books`/
  `promote_to_bookshelf`/`switch_bookshelf_source`/`update_bookshelf_*`）
  全部经继承原样保留，方法体不改；M3 仍读写旧 `bookSource.json` / `bookshelf.json`。
- **生命周期**：`name="SourceService"`，`dependencies=(StorageService, SettingsService, RuleEngineService)`；
  `on_start` 经 `hub.require` 补齐未注入协作者，并 `self.load()` 加载书源+书架
  （首启无文件时捕获 `FileNotFoundError` 退化为空库，不崩启动）。
- **异步入口（契约 §3）**：`list_sources / load_source / save_source(source_json)->dict /
  delete_source / set_enabled / set_group / import_sources_json(text)->(count, errors) /
  export_sources_json()->str`。
- **校验保存入口**：`save_source` 与 `import_sources_json` 在落盘前经 `_audit()` 调
  `CompatAuditService.audit_source`（§3.7 触发点①②）。CompatAuditService 由并行子代理 B 落地；
  未注册/未注入时降级为不阻断（`_audit` 返回 None），审计异常只记告警。
- **事件**：CRUD/分组/导入/删除/改封面均发布 `SourceChangedEvent(change_type, source_id)`；
  书源类用 `bookSourceUrl`，改封面/书架类用 `book_url`。
- **notShelf**：`is_shelved` 做双形态适配——传 `dict` 走旧静态语义（兼容旧调用），
  传 `book_url(str)` 按书架记录查找判定；`shelved_books`/`add_to_bookshelf`/
  `discard_unshelved_books` 继承旧实现，§1.5 语义零改动。
- **1.18 改封面**：`update_cover(book_url, cover_url_or_path)`——
  - 本地已存在文件：Pillow `convert("RGB")` + `thumbnail(480)` 归一为 JPEG，
    落 `image-cache/covers/<md5(book_url)[:16]>.jpg`（`_covers_dir` 优先经
    `StorageService.cache_dir("image-cache/covers")` 分配）；
  - 否则按远端 URL 原样记录（M3 不在服务内联网，候选图抓取留给 RuleEngineService 通道）；
  - 回写 bookshelf.json 记录 `cover_url` + SQLite `books.cover_url` 列（M2 v2 列）；
  - 发布 `SourceChangedEvent("cover_updated", book_url)`。

### 2.2 `SourceChangedEvent`（app/core/events.py）

`@dataclass(frozen=True) SourceChangedEvent(AppEvent)`：`change_type: str, source_id: str,
timestamp`。**注**：该事件在我动手前已由并行集成方写入 events.py（连同
CheckFinished/RepairReportReady/ImportFinished），字段与契约 §2 一致，本线未重复添加。

### 2.3 ReadingService 获取点切换（reading_service.py on_start）

在 `_engine` 解析之后追加：

```python
if self._source_service is None and self.hub is not None:
    from app.services.sources.source_service import SourceService
    self._source_service = self.hub.require(SourceService)
```

- 懒 import 避免与 sources 包循环依赖；
- `hub is not None` 守卫：M2 服务测试直接注入 bus、不挂 hub，此分支不触发，注入替身不受影响。

## 3. 测试数字

venv：`D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe`，项目根运行。

- `pytest tests/test_sources_service_m3.py -q --no-header -p no:cacheprovider --basetemp=.cache/pytest/tmp_m3`
  → **17 passed**（CRUD/upsert/缺url拒绝/load/delete/enabled/group/导入去重/坏JSON/导出/
  分组统计/notShelf 转正与清理/本地封面归一/远端URL透传/审计替身被调/事件发布）。
- `pytest tests/test_reading_services.py -q ...` → **15 passed**（M2 服务测试无回归）。

## 4. 与契约偏差（需集成方知悉）

1. **events.py 不重复添加**：`SourceChangedEvent` 已由并行集成方落地，字段一致，未二次追加。
2. **CompatAuditService 懒解析**：契约 §1 中 SourceService 依赖不含 CompatAuditService
   （且 CompatAuditService 反向依赖 SourceService，成环）。故保存即审不进 `dependencies`，
   改为 on_start 中 `try hub.require(CompatAuditService)` 软解析 + 构造期可注入替身；
   未就绪时不阻断保存。行为符合契约 §3「经 CompatAuditService 方法调用」。
3. **reading 测试 schema 断言对账**：并行子代理 B 已将 `SCHEMA_VERSION` 升到 3
   （`audit_log` 表，契约 §8）。`tests/test_reading_services.py::test_schema_v2_tables_exist`
   原硬编码 `SCHEMA_VERSION == 2` 失配。本线做最小对账 `== 2` → `>= 2`（v3 向后兼容，
   v2 表结构不变）。此为跨线共享文件的版本断言失配，非本线 reading_service 改动引入
   （M2 测试中 `hub=None`，新增 require 分支不触发）。
4. **候选封面联网抓取未实现**：契约 §3 提「候选封面抓取复用 RuleEngineService 图片取图通道」。
   M3 本线仅落地「本地上传归一 + 远端 URL 原样记录」写路径与 Pillow 归一工具；联网抓图
   属视图/引擎协同，留待 M4/M5（不在本线测试范围，测试不联网）。
5. **on_start 自动 load()**：旧 app 在 build 时显式调 `load()`；外壳化后由 `on_start`
   统一加载，首启无文件退化为空库。这是外壳装配点，未改 `load()` 本身行为。

## 5. 红线自检

- 服务零 flet import（`app/services/sources/source_service.py` 无 `import flet`/`from flet`）。
- 旧 `app/services/source_service.py` 方法体零改动（纯继承）。
- 书源数据 M3 仍 JSON 落盘，未建新表（audit_log v3 由并行子代理 B 建）。
- 跨平台：Pillow 归一/路径均经 `pathlib`；测试用系统短临时目录规避 Windows MAX_PATH。
- 注释中文；未提交 git；未做 M3 之外工作。
