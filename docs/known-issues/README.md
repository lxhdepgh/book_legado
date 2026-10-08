# 已知问题索引（known-issues）

> 遇到报错或异常行为时，先检索本目录是否已有同类记录，避免重复踩坑；解决后在此追加条目。

## 索引

- [001 Flet 0.86 表面 API 变化导致界面点了没反应](001_flet_0_86_surface_breaks.md) — `ChoiceChip`/`no_selection`/`ImageFit`/`src_base64`/图标枚举，静态测试全绿也掩盖不住。
- [002 界面到服务的书条目必须是对象，试读记录「加入书架」要转正](002_findbook_shelve_dict_snapshot_and_staged_promote.md) — dict 快照透传炸 `book.url`；同键暂存行不是重复。
- [003 分层重构把编排从视图搬进服务时，能力被"最小可用版"替换掉](003_refactor_dropped_orchestration_capabilities.md) — 串行换源、进度条不刷、清理没人调：测试全绿的退化；09-26/27 两型复发（服务注册了界面零订阅、进度事件从来没写过却被文档当成有）；补的护栏若只钉"没发生"会假绿。
- [004 长检测后窗口停在 loading + `module.c:99` 原生 abort](004_long_scan_page_frozen_and_quickjs_abort.md) — **真因已定位并改掉（离线）：`Object.json()` 不建 QuickJS 调用帧**。第五十五轮第一次离线复现（现场那条源的真链路 + 127.0.0.1 假源，旧写法第 1 轮即崩 rc=3221226505 / 改后 5/5 活），最小化到一句转换（四句都摘不得，跨线程/jsLib/`<useweb>` 都不是必要条件），读上游 `module.c` 定性：`Object.json()`(:143) 是裸 `JS_JSONStringify` 不建帧，被序列化的元素句柄Proxy 回头调桥函数 → `assert(thread_state)`:99 当场 abort。改法=JS 值转换一律走带帧的 stringify（成本 +8.4 µs/次 ≈ 真求值的 0.09%），护栏 5 条 + 变异 7 条全有红。第五十二～五十四轮的现场遗言日志（13010 条、单线程、`depth` 恒 1）与八个否证形状记录仍在，A 链（整页下发压循环）的否证不变；10054 归因照旧等现场；子进程隔离"拦住崩"仍未开工。同篇另记两族"长检测"问题：**越跑越慢**（响应级整罐 cookie 写盘 0.847→0.001 ms、审计扇出峰值 206 → 2 已改，每源重水化那一头未改）、**一个服务卡住连累别的**（`legado-quickjs` 单条派发线程排队 + 阅读路径 5 处 `js.evaluate()` 无超时，机制已量清、未修）。
- [005 网关的空闲自杀只认 WS 会话，RPC-only 客户端死后引擎永留](005_gateway_idle_exit_never_arms_for_rpc_only_clients.md) — `_arm_idle_timer` 第一行看 `_sessions_seen`，而它只有 `/ws` 会自增；G1 走的是纯 HTTP `/rpc`，所以 `--idle-seconds` 从没武装过（现场：`--idle-seconds 5` 的进程活了 2.42 小时）。三种走法各有语义代价，等拍板。

## 历史参考

- 根目录旧 `devProblem/` 下的问题记录（2026-08-07 / 08-10 / 08-11）已随目录删除；如需找回可从 git 历史恢复（`git log -- devProblem/`）。
