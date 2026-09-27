# 001 Flet 0.86 表面 API 变化导致界面点了没反应

- 记录日期：2026-09-25
- 状态：已解决（本轮全库扫清 + 加静态护栏）
- 影响面：找书页书源行、阅读器正文渲染、书架长按管理对话框、二维码工具页

## 症状

重构后界面能打开、列表能渲染，但**某些点击毫无反应**，控制台只在被点的那一次打一条
`AttributeError` / `TypeError`。单测全绿（843 passed）完全掩盖不了这类问题，因为它们发生在
渲染函数被真实控件构造调用的瞬间。

## 根因

参考实现（旧 `app/views/reading_view.py`）写在 Flet 0.8x 早期 API 上，重构时按记忆搬了名字，
但 0.86 的公开表面并不一致：

| 旧写法 | 0.86 实际 | 后果 |
| --- | --- | --- |
| `ft.ChoiceChip(...)` | 不存在，用 `ft.Chip(...)` | 点书源行 `AttributeError`，分类芯片整块不渲染 |
| `ft.Text(..., no_selection=False)` | 参数名是 `selectable` | 每章正文构造即 `TypeError`，阅读器永远空白 |
| `ft.Image(..., fit=ft.ImageFit.CONTAIN)` | 枚举是 `ft.BoxFit` | 图片块渲染失败 |
| `ft.Image(..., src_base64=...)` | 只有 `src: str \| bytes` | 二维码永远不出图 |
| `ft.SwapHoriz` / `ft.FileDownload` / `ft.DeleteOutline` | 图标枚举在 `ft.Icons.*` | 书架长按管理对话框打不开 |

## 解决

1. 逐处替换为 0.86 真实表面（见上表右列）。
2. 用 AST 静态扫描全库 `ft.*` 调用：属性是否存在于 `flet` 模块 / 枚举成员是否存在 /
   kwargs 是否在构造器签名里。把结果固化成两个单测：
   `tests/test_controller_service_surface.py::test_flet_surface_names_and_kwargs`、
   `::test_flet_enum_members_exist`。
3. 加一个**离线本地探针**（`WEB_BROWSER` + 127.0.0.1 假书源，零外网）跑真实点击链路，
   39 项断言全过。静态套件证明不了"点了有反应"，这类缺陷只有活会话能暴露。

## 复发时先查

- 报错栈顶是控件构造器（`ft.Xxx(...)`）而不是业务逻辑 → 优先怀疑 API 表面，不是逻辑。
- 先跑 `pytest tests/test_controller_service_surface.py -q`，它会在导入期就把坏名字挑出来。
- `ft.Image` / `ft.Icon` / `ft.Text` 三个构造器是重灾区，参数改动集中在它们身上。
