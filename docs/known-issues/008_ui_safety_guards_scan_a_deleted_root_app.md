---
状态: 未修·待拍板
影响面: 根 tests/ 的 5 个护栏文件 / 34 条 test（Python 侧）
---

# 008 红线护栏扫的是已删除的根 `app/`，扫到空集就报"全绿"

## 现象

`docs/architecture-rules.md` 里"红线 1 / 红线 6"的执法者是 `tests/test_ui_safety.py`。当场跑：

```
python -m pytest tests/test_ui_safety.py -q --basetemp=.tmp_pt/docs_guard
..                                                    [100%]
2 passed in 0.02s
```

**0.02 s 扫不完一棵树**——它其实一个文件也没读到。

## 根因

护栏把根写死成仓库根的 `app/`：

- `tests/test_ui_safety.py:24-25` `_SERVICES = _ROOT / "app" / "services"`、`_VIEWS = _ROOT / "app" / "views"`
- 遍历用 `package.rglob("*.py")`（`:32`）。路径不存在时 `rglob` **不报错、返回空**，
  于是 `offenders` 恒空、断言恒真。

那棵树在 2026-10-12 的清理里搬走了：现在 Python 侧是 `legodo_flet/app/`
（当场数：`app/services` 62 个 py、`app/views` 37 个 py），仓库根 `app/` 已不存在。
同一条搬动还让 `tests/test_gateway_contract_single_source.py` 整模块 `ModuleNotFoundError: app`，
那一发是**响的**，所以被发现并修了（改 `sys.path.insert(0, FLET)`）；
本条是**不响的**，因此一直没人看见。

## 受影响的读点（当场 `grep` 全仓）

5 个文件、34 条 `def test_`：

| 文件 | 死读点 |
|---|---|
| `tests/test_ui_safety.py` | `:24-25` |
| `tests/test_controller_service_surface.py` | `:32-33`、`:88`、`:345`、`:445`、`:478` |
| `tests/test_service_surface_probes.py` | `:21` |
| `tests/test_rss_projection_tables.py` | `:39` |
| `tests/test_storage_handle_discipline.py` | `:45` `APP = ROOT / "app"` |

## 正对照（证"仪器没接上"，不是证"确实没问题"）

把同两条判据现场指向真实存在的 `legodo_flet/app`（当场 grep）：

| 判据 | 该扫到 | 实扫结果 |
|---|---|---|
| 服务层只允许白名单那一个文件 `import flet` | `app/services` 62 个 py | 命中 1 个：`legodo_flet/app/services/web_page_renderer.py`（`:19 import flet as ft`、`:75 import flet_webview as ftw`）——正是白名单本体 |
| views 零 `app.services.*` 直 import | `app/views` 37 个 py | 命中 0 个 |

⇒ 两条红线在今天的 flet 参考树上**确实成立**，问题不是违规，而是**执法者扫错了目录**：
指向真实根时要读 99 个文件，指向已删除的根 `app/` 时读 0 个文件、耗时 0.02 s 报绿。

## 修法（两条要同轮做，否则得到的是一条新红灯）

1. 读点改成现读：`FLET = ROOT / "legodo_flet"`，与 `test_gateway_contract_single_source.py:31` 同一口径。
2. **每条护栏配一根"明知非 0"的对照**：扫到 0 个文件就红，而不是绿。
   只改路径不加这条，下一次目录再搬走时会得到同样的静默。

关联：`007_flet_content_search_resequences_index_then_search.md` 是同一种病的另一例
（那条直接 `open()` 不存在的路径，所以是当场 `FileNotFoundError`——响的版本）。
