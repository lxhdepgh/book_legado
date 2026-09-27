# 已知问题索引（known-issues）

> 遇到报错或异常行为时，先检索本目录是否已有同类记录，避免重复踩坑；解决后在此追加条目。

## 索引

- [001 Flet 0.86 表面 API 变化导致界面点了没反应](001_flet_0_86_surface_breaks.md) — `ChoiceChip`/`no_selection`/`ImageFit`/`src_base64`/图标枚举，静态测试全绿也掩盖不住。
- [002 界面到服务的书条目必须是对象，试读记录「加入书架」要转正](002_findbook_shelve_dict_snapshot_and_staged_promote.md) — dict 快照透传炸 `book.url`；同键暂存行不是重复。
- [003 分层重构把编排从视图搬进服务时，能力被"最小可用版"替换掉](003_refactor_dropped_orchestration_capabilities.md) — 串行换源、进度条不刷、清理没人调：测试全绿的退化；09-26/27 两型复发（服务注册了界面零订阅、进度事件从来没写过却被文档当成有）；补的护栏若只钉"没发生"会假绿。

## 历史参考

- 根目录旧 `devProblem/` 下的问题记录（2026-08-07 / 08-10 / 08-11）已随目录删除；如需找回可从 git 历史恢复（`git log -- devProblem/`）。
