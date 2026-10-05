# 004 长检测后窗口停在 loading + `module.c:99` 原生 abort：真因已定位并改掉（离线），A 链早已否证；同族的"越跑越慢"两个累积器已改、"一个服务卡住连累别的"未修

- 记录日期：2026-09-30
- 状态：**症状 B 的真因已定位、已按证据改掉（离线验证），现场待重跑确认**。
  本文后来长成了"长检测"这一整族问题的记录：**"越跑越慢"**那一条改了两个累积器
  （响应级整罐 cookie 写盘、审计扇出压满默认线程池）+ 进度分母口径，
  **"一个服务卡住连累别的"**只量清了机制、未修（见末尾两节）。
  上面的 A/B 两条链只讲"停在 loading + abort"这一族。
  第五十二轮只取证不改；第五十三轮把待修清单里 1/2/3 三项落地（遗言日志、下发合并 + dict 去重、
  两条循环的异常归属）；第五十四轮第一次拿到现场崩溃的遗言日志（13010 条），
  并按上游 `module.c` 源码更正 `:99` 的语义；
  **第五十五轮把这条崩溃离线复现出来了**（第一次尝试就复现），最小化到一句转换，
  读上游 C 源码定性为 **`Object.json()` 不建调用帧**，改掉之后现场那条源的真链路重放
  从"第 1 轮即崩"变成 5/5 活。机制、证据与护栏见下面"§B 的真因"一节。
  症状 A 的否证结论不变：下面两项改动是**降本与可观测**，不是"治好了停在 loading"，
  写注释时不许把它们写成病因。
  **第六十五轮**把待修清单 3（10054 归因）收完：现场身份已判明是 engine 循环的 socket 收尾
  竞态，认得出的两族降成一条带累计计数的短行、族 1 顺手补上被异常跳过的 `sock.close()`，
  认不出的一律照旧两遍——它是**噪声与句柄**的处理，不是"停在 loading"的病因，同上不许混。
  **第六十九轮**只给待修清单 7 量前置账（静态普查 + 动态沙箱实测，两份读数在下面第 7 条），
  仍然**未开工**；同轮顺手量出一条**真桥也打穿进程**的阅读脚本，记成待修清单 9（机制未定）。
- 影响面：书源检测（全库联网扫描，数百至上千条源）；顺带波及所有走
  `BaseController.flush()` 整页下发的常驻大列表（换源扫描、书内索引行）
  `BaseController.flush()` 整页下发的常驻大列表（换源扫描、书内索引行）
- 取证详情：`docs/dev-logs/2026-09-30.md` 第五十二～五十五轮（崩溃链）与第五十八～六十二轮
  （重启按钮、越跑越慢的两个累积器、进度分母、分级检测；探针与输出全在 `.probe/`）；
  10054 那一族见 `docs/dev-logs/2026-10-01.md` 第六十五轮
  （`.probe/loop_teardown_matrix.py` 62 格否证、`.probe/loop_teardown_cleanup_probe.py` 三臂、
  `.probe/mutate_teardown_guards.py` 变异 5/5）；
  现场那份环形文件已改名留底
  `%LOCALAPPDATA%\LegadoBookSourceCheck\diagnostics\crash-20260930-131140.journal`

## 症状

启动命令 `python main.py`（conda 环境 `legodo_booksource_check`），检测跑一段时间后：

1. **窗口还在，但主题变成一个 loading**——不是窗口消失（开发者口径）；
2. 控制台先刷 6 行（部分参数是 `None`）：

   ```
   Exception in callback _ProactorBasePipeTransport._call_connection_lost(ConnectionResetError())
   ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
   ```

3. 最后一行是 C 运行时断言的原生 abort，**Python 侧不可捕获**：

   ```
   Assertion failed: runtime_data->thread_state, file module.c, line 99
   ```

## 为什么必须拆成两条链再查

桌面端是 `FLET_APP`：**Python 进程是服务端，窗口是独立的客户端进程**，客户端靠一条
websocket 收补丁（`session.patch_control`）。于是"停在 loading"有两种成因相反的症状：

| 链 | 机制 | 要修的东西 |
| --- | --- | --- |
| A 界面循环被压住 | 服务端活着，但事件循环长时间顾不上 socket → 客户端超时断开、不再渲染 | 循环上的占用 |
| B 服务端进程被打断 | `module.c:99` 的原生 abort 当场带走进程 → 窗口只剩最后一帧 | 崩溃本身 |

## §A 的结论：不是原因（当场量到否证）

`.probe/check_ui_flush_cost_attr.py`（真 `CheckView` + 真 Flet 会话 + Playwright 真客户端
在线，按生产速率 ~8 源/秒喂 `render_result`，每行后跑一次与 `flush()` 同一支的
无参 `page.update()` = 整棵控件树 diff；循环上挂 5 ms 心跳；钩
`Session._Session__get_update_control_patch` / `_Session__send_message` 数出 patch 操作数
与下发字节——不数就是空测量，因为 `patch_control` 里 `if len(patch) > 1` 才发消息）：

| 行数 | render(ms) | 整页 update(ms) | patch 操作数 | 本次下发(B) |
| --- | --- | --- | --- | --- |
| 1 | 4.92 | 1.55 | 2 | 4466 |
| 300 | 0.19 | 7.81 | 2 | 4486 |
| 900 | 0.32 | 21.69 | 2 | 4486 |

900 行合计墙钟 114.4 s；整页下发合计占用界面循环 **10616 ms = 墙钟 9.3%**；心跳 8304 拍
**最大间隙 37.1 ms**；**客户端全程没掉线**；累计下发 3950 KB（≈ 每条 4.5 KB，**增量**，
不是每次重发整棵树）。下发确实随行数线性变贵（≈ 0.024 ms/行），但这个量级解释不了
"页面再也不渲染"。

顺带查出两个真缺陷（**是"值得做"，不是"病因"**，改的时候别按病因写注释）：

- `CheckController` 是唯一没走 `BaseController.throttle`（`app/controllers/base.py:231`）
  的进度型控制器（`find_books:78`、`bookshelf:84`、`reader:220` 都走了），
  每完成一源就渲染 + 下发一次（`app/controllers/source/check_controller.py:171-184`）；
- `CheckView.render_result` 的去重是**全行线性扫描**
  （`app/views/source/check_view.py:88`，`for index, existing in enumerate(self._rows.controls)`）。

两条在第五十三轮各按自己的形状收掉（下发合并 + dict 索引，见待修清单 2）。

## §B：`:99` 不是 `:83` 的另一种写法（第五十四轮按上游源码更正语义）

本机 `_quickjs.cp312-win_amd64.pyd` 里全库只有一处 `module.c`（偏移 671234），两条断言串
**紧挨着**，后面紧跟 quickjs 运行时统计字段（`malloc_size / memory_used_size / atom_count`），
说明两者在同一对相邻函数里。

第五十四轮把 python-quickjs **1.19.4 的 sdist 从 PyPI 拉下来实读**（仓库里没有
`reference/` 目录，早前记的那条路径是过期的；源码落在 `D:/tmp/qjs1194/quickjs-1.19.4/module.c`），
四个 GIL 记账函数与断言行号是：

| 行 | 函数里的断言 | 语义 |
| --- | --- | --- |
| `:81` `prepare_call_js` → `:83` `assert(!thread_state)` | 进原生段**之前** | "里面不许已有人"——槽位非空就是两条线程同时进同一个 Context |
| `:90` `end_call_js` → `:92` `assert(thread_state)` | 出原生段**之后** | 槽位必须是自己刚进去时那位 |
| **`:98` `prepare_call_python` → `:99` `assert(thread_state)`** | **JS 回头调 Python 那一刻** | **槽位必须非空**，即"必须正处在这条 Context 的原生段里面" |
| `:105` `end_call_python` → `:106` `assert(!thread_state)` | 桥回调返回后 | 回调期间槽位必须已让空 |

所以 `:99` 响的是 **JS→Python 回调这一层**，不是求值进出层；**本文第五十二轮把它写成
"出原生段时 thread_state 已空"是错的**，那句其实描述的是 `:92`/`:106` 那一族。
`prepare_call_python` 全库只有一个调用点：`js_python_function_call`（`:419`），也就是
`ctx.add_callable` 绑进去的那 109 个桥函数被 JS 调用时。`thread_state` 是
**每条 `_quickjs.Context` 一份**的记账位（`RuntimeData` 就是 Context 对象本体，
`:27` 那行 `PyThreadState *thread_state`，注释写着"Used when releasing the GIL"）。

于是"`:99` 为什么会空"只有几条路，全部指向**别的什么东西在这条 eval 还没结束时动了这条
Context**：

1. 另一条线程在**同一个 Context** 上走完 `end_call_js`（`:90`）——它会把槽位读走并置
   `NULL`（`:94`），正在里面跑的那条随后一回调用桥就撞 `:99`。`_quickjs` 一共四个
   原生入口会这样配对：`runtime_eval`(`:507`)、`runtime_module`(`:533`)、
   `object_call`(`:257`)、`runtime_execute_pending_job`(`:536`)；
2. 可调用对象的释放路径 `js_python_function_finalizer`（`:382`，函数体 `:385-408`）——上游自己写着
   `NOTE: This may be called from e.g. runtime_dealloc, but also from e.g. JS_Eval …
   TODO: integrate better with (prepare|end)_call_(python|js)`（`:388`），它在 `Py_XDECREF(node->obj)`
   前后直接翻 `thread_state`；这条路要么由 GC 回收环触发（`runtime_clear` `:371`），
   要么在 eval 里释放旧绑定时触发；
3. 求值线程自己在没进原生段的状态下调了桥函数（`object_call` 从 Python 侧调 JS 函数
   再回到 Python 那一族）。

**下一节的现场证据把 #1 的"两条线程"形状削掉了一半**：13010 次求值全在一条线程上。

## 现场那一崩长什么样（第五十四轮第一次拿到遗言日志）

开发者按 `python main.py` 跑全库扫描，控制台出 `Assertion failed: runtime_data->thread_state,
file module.c, line 99` 后 **Python 进程停了、窗口还在、中间提示 working**——这正是
"Flet 服务端死了、客户端停在最后一帧"（`FLET_APP` 下窗口是独立进程，靠 websocket 收补丁）。
那份环形文件已改名留底：`diagnostics/crash-20260930-131140.journal`。用
`crash_journal_records(path, limit=…)` 读回，**下面的数字全是从那份文件当场重测的**：

| 项 | 实测值 |
| --- | --- |
| 记录条数 | 13010（末条 `#13010` 就是崩那一次进） |
| 时间跨度 | 929.6 s = **15.49 分钟** |
| 进程 | 全部 `pid=74352`（**单进程**） |
| 线程 | `th=legado-quickjs` **13010/13010**（ dispatcher 唯一那条） |
| `depth` | 恒为 1（**没有任何一次重入**） |
| 涉及 Context 数 | 7811 条不同 `runtime=`；`fresh=1` 7811 / `fresh=0` 5199 |
| 复用最多的一条 | `runtime=1481` 被进了 **5112** 次（`jslib=218e9b14`） |
| 最长空档 | `#10391` 前 149 973 ms（网络等待，不是原生段卡住） |

崩前 5 条原文（`rule=` 空是那条源把规则写在 `@js:` 内联里、没有规则名）：

```
#13006 ts=1790743814.421 pid=74352 th=legado-quickjs depth=1 runtime=7810 fresh=1 jslib=ed015ff2 bytes=1424 rule= url=https://manwaza.cc#大改
#13007 ts=1790743814.566 pid=74352 th=legado-quickjs depth=1 runtime=7811 fresh=1 jslib=d41d8cd9 bytes=376 rule= url=https://xiaoshuo.uc.cn
#13008 ts=1790743814.642 pid=74352 th=legado-quickjs depth=1 runtime=7810 fresh=0 jslib=ed015ff2 bytes=1379 rule=ruleBookInfo.intro url=https://manwaza.cc#大改
#13009 ts=1790743814.666 pid=74352 th=legado-quickjs depth=1 runtime=7810 fresh=0 jslib=ed015ff2 bytes=142 rule=ruleBookInfo.tocUrl url=https://manwaza.cc#大改
#13010 ts=1790743814.683 pid=74352 th=legado-quickjs depth=1 runtime=7810 fresh=0 jslib=ed015ff2 bytes=172 rule= url=https://manwaza.cc#大改
```

读得出的三件事：

1. **崩在第 4 次进同一条共享 Context**。`runtime=7810`（`jslib=ed015ff2`）在
   `#13006` 新建、`#13008`/`#13009` 复用，`#13010` 距上一条只有 **17 ms**，中间还夹着
   一条别的源（`#13007`）；
2. **单线程、无重入**（`depth` 恒 1）。所以 `:99` 这次**不是**"`:83` 那一族的变体"，
   也不是重入把配对搞失衡——上面 #1 那条"另一条线程在同一个 Context 上 `end_call_js`"
   在**求值这条路上没有证据**（求值全在一条线程），但 journal 当时**只记录求值**，
   记不到"别的线程在销毁 Context / 在 GC 里收桥闭包"。这正是第五十四轮补配对与
   `thid=` 的理由；
3. 崩的那一次 `bytes=172`、`rule=` 空——**有名字的规则**在尾部排行是
   `ruleToc.chapterUrl` 8272 次 / `ruleExplore.bookUrl` 1009 次 / `ruleSearch.coverUrl` 552 次，
   所以现场不是某条特殊规则，是"内联 `@js:` + 复用共享 Context"这个通用形状。

## §B 的真因：`Object.json()` 进 JS 时不带调用帧（第五十五轮，已改掉）

上面 #3 那条"求值线程自己在没进原生段的状态下碰了这条 Context"就是答案，只是形态比猜的更窄：
**不是另一条线程，是同一条线程用了一条不建帧的原生入口**。

上游会 `prepare_call_js`(:81) 建帧的入口只有三类——`Context.eval` / `Context.module`
（同一支 `runtime_eval_internal`:502，:507 建帧）、`Object()`（:257）、
`execute_pending_job`（:533）；而本引擎在求值收尾时用
**`Object.json()`(:143)** 把规则返回值转成 JSON——它是裸的 `JS_JSONStringify`，**不建帧**
（`Context.get`:566 / `Context.set`:580 同族，函数体里既没 `prepare_call_js` 也没放 GIL；
本引擎一处没用）。于是被序列化的对象一旦属性读取
落到"JS 回头调 Python 桥函数"（`prepare_call_python`:98 → `assert(thread_state)`:99），
槽位是 NULL → 当场 abort，Python 侧不可捕获。本引擎的元素句柄正是 Proxy，陷阱里逐个调
`__dom_*`，所以**"规则返回元素句柄"这一形状天然满足触发条件**。

### 取证顺序（三步都按"先离线复现"走）

1. **复现**：`.probe/qjs_manwaza_chapterlist_attr.py`——现场那条源（`manwaza.cc#大改`）的规则
   原文从本地 `legado.db` 只读捞出，子进程里跑**真检测链路**（`check_service.check_sources`）
   + `127.0.0.1` 假源，零联网。**第一次尝试就打出 rc=3221226505**。
2. **最小化**：`.probe/qjs_manwaza_minimize_attr.py`（每臂 2 个子进程）——
   baseline 2/2 崩；`drop_toast` / `drop_setcontent` / `drop_getelements` / `drop_hexdecode` /
   `only_last` 全 0/2；`no_jslib` 2/2 崩；`no_template` 2/2 崩；`plain_intro` 0/2。
   也就是 `ruleToc.chapterList` 那 172 字符里**四句都摘不得**，而"共享 Context""jsLib
   被 Python 侧调用""简介带 `<useweb>`"都不是必要条件——这把第五十四轮照现场造的那两个
   方向（重绑风暴、跨线程 GC）从根上问歪了。
3. **回调层记账**：`.probe/qjs_manwaza_bridge_trace_attr.py`——把 `quickjs.Context` 换成
   转发代理（C 类型不可子类化，只能代理），`add_callable` 包一层记录器、每行**不经缓冲**
   直接 `os.write`（0xC0000409 绕过 flush）。崩那一刻的位置正好在
   `JSON.stringify({source,book,chapter})` 那次桥回调之后——第一次把矛头指到 stringify。

### 机制：五臂隔离（`.probe/qjs_jsonstringify_no_frame_attr.py`，每臂 3 个子进程）

| 臂 | 写法 | 读数 | 桥函数被调 |
| --- | --- | --- | --- |
| `json_method` | `value.json()` | **3/3 崩**（rc=3221226505，`Assertion failed: runtime_data->thread_state, file module.c, line 99`） | — |
| `eval_stringify` | 同一个对象在 `ctx.eval` 里 `JSON.stringify` | 3/3 活 | 2 次/次 |
| `object_call` | 把 stringify 包成 JS 函数从 Python 调（`Object()`:257） | 3/3 活 | 1 次/次 |
| `control_plain` | 无陷阱的普通对象走 `.json()` | 3/3 活 | 0 次/次 |
| `ctx_get` | `Context.get(...)` | 3/3 活，但**桥函数 0 次** | 0 次/次 |

最后一臂**不能**读成"`ctx.get` 安全"——它压根没走到机制（桥函数一次都没被调），
它只说明那一臂的构造没触发属性读取陷阱。`control_plain` 同理是"必要条件对照"：
没有陷阱对象时 `.json()` 不崩，所以病因是**不建帧 × 被序列化对象回调用桥**这两件事的积。

### 生产路径：六臂前后（`.probe/qjs_element_json_frame_attr.py`，每臂 3 个子进程）

"改前"不是猜的旧行为，是把 `_eval_with_stringify_frame` 返回处**临时装回** `value.json()`
（`.probe/_mut55_pre_arms_attr.py`，整文件备份写回 + 字节一致断言）后重跑同一支探针：

| 臂（规则末句的形状） | 改前 | 改后 |
| --- | --- | --- |
| `elements_after_setcontent`（返回元素句柄数组） | **3/3 崩** | 3/3 活 |
| `elements_from_result`（不 setContent，直接从 result 取元素） | **3/3 崩** | 3/3 活 |
| `elements_nested_object`（`({rows: java.getElements(...)})`，句柄藏在对象里） | **3/3 崩** | 3/3 活 |
| `one_element`（返回单个句柄） | **3/3 崩** | 3/3 活 |
| `plain_object`（`({a:1,b:[1,2]})`） | 3/3 活 | 3/3 活 |
| `plain_count`（只回 `.length`，没有值要转换） | 3/3 活 | 3/3 活 |

同一条源的真链路重放：装回旧写法 → **第 1 轮即崩**；当前代码 → **5/5 活**
（`qjs_manwaza_chapterlist_attr.py 5`）。不联网、不走 aiohttp 之外的真实站点。

### 改法：JS 值的转换一律走"带帧的 stringify"

`app/services/engine/legado_js.py`：

- 新增 `_eval_with_stringify_frame(ctx, program, context)`：先用同一个 Context
  `ctx.eval(_JS_STRINGIFY_PROGRAM)` 取回一段**闭包硬化**的转换函数
  （`(function(stringify){return function(v){return stringify(v);};})(JSON.stringify)`——
  建帧发生在规则跑**之前**，所以规则里 `var JSON = …` 改写不了它，与上游
  `JS_JSONStringify` 语义等价），再跑规则程序，返回值一律经 `_json_of` 转换；
- 新增 `_json_of(value)`：只走线程局部帧里那一段函数（走 `Object()` → `object_call`:257 →
  `prepare_call_js`:81 那条**带帧**的路），**没有帧就抛一条可捕获的 `JsBridgeError`**，
  绝不退回 `value.json()`；
- 帧按线程记（`_JS_STRINGIFY_FRAME = threading.local()`），进出成对：嵌套求值退出时
  **还回外层那一份**而不是置 `None`——嵌套 runtime 的 Context 已经 `close()`，
  外层继续用它就是 use-after-free，比原来那个 abort 更难查；
- 原先四处裸转换全部改掉：`_evaluate_locked` 的返回、`_external_rule_root`、
  `_byte_values`、以及 `_bridge_call` 之外两处 `native = json.loads(...)`。
  现在引擎里 `.json()` 只剩注释与 docstring 里的字面引用（AST 审计用例钉着）。

### 成本（A/B 交替，按记忆 `ab-bench-must-interleave`）

`.probe/qjs_stringify_frame_cost_attr.py`（2000 次 × 9 轮，奇偶轮换先后，取中位数）：

| 形状 | µs/次 | 说明 |
| --- | --- | --- |
| `old`（跑规则 + `value.json()`） | 38.77 | 改前 |
| `new`（建帧 + 跑规则 + 走帧转换） | 47.15 | 改后 |
| `program_only`（只跑规则） | 18.92 | 减法基准 |
| `frame_only`（只建帧那一句） | 7.06 | 新增的唯一一句 |

即每次求值多 **8.37 µs**。放到分母里：`.probe/qjs_engine_eval_cost_attr.py`
（300 次 × 7 轮交替）量到一次**真引擎求值**是 9 094.6 µs（普通对象）/10 642.1 µs（元素句柄），
所以多出来的这 8.4 µs ≈ **一次求值的 0.09%**。

### 还留着的两个口子（本轮没堵）

- 上游**不建帧的原生入口其实有三个**：`Object.json`(:143，本轮修掉的那条)、
  `Context.get`(:566)、`Context.set`(:580)——后两个函数体里既没有 `prepare_call_js` 也没有放
  GIL；再往外数还有 `Context.parse_json`(:551)，它放了 GIL（`Py_BEGIN_ALLOW_THREADS`:557）
  却**同样不建帧**。今天引擎三处都没用（全库 grep 无调用点），
  但下一个用它们的人照样会撞 `:99`；
- **`object_call` 仍是遗言日志的盲区**：日志只覆盖 `ctx.eval`（`ev=E/X/!/C`），
  本轮新加的转换走的就是 `object_call`，它不在记里。现场若再崩在这类入口上，
  journal 依旧只会显示"最后一条 `ev=E` 已经闭合"。



## 已否证的候选形状（全部 0 崩，子进程隔离）

**为什么这八臂一条都没抓到真因**：它们全在"另一条线程动了这条 Context"这个方向上造形状，
而真因是**同一条线程用了一条不建帧的原生入口**（见上一节）。这八臂的否证仍然有效——
它们各自排除的那个假设（跨线程销毁、异常跨界、泄漏、重入、收尾竞态、重绑风暴、跨线程 GC）
都不是病因，只是别把它们当成"`:99` 已经查尽"。

判定一律分三档：`崩`（rc≠0 或 stderr 有 `Assertion failed`）/ `干净` / `探针报错`
（rc=1 且 stderr 是 Python 异常 = 探针自己没跑起来，**不许**混进"崩"）。

| # | 候选形状 | 样本 | 探针 → 输出 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | Context 在非归属线程销毁（同线程/异线程/未用过就丢/逃逸的值/循环引用靠 GC） | 5 形状 | `qjs_free_thread_attr.py` → `out_qjs_free_thread.txt` | 全活 |
| 2 | Python 异常跨 QuickJS 的 C 边界（回调 raise、嵌套求值 raise、raise 后丢帧、值活得比 Context 久、内存上限、JS 栈溢出、200 轮风暴） | 11 形状 | `qjs_raise_across_bridge.py` → `out_qjs_raise.txt` | 全活 |
| 3 | 每源一条 runtime + jsLib 求值累积 | 400 轮 | `qjs_context_leak_attr.py` → `out_qjs_leak.txt` | Context 恒 1、Runtime 恒 1、RSS 37.8→48.4 MB 后平台波动不涨 |
| 4 | 求值进行中释放 runtime | after/during 各 5 子进程 × 4 轮 | `qjs_free_during_eval_attr.py` → `out_qjs_free_during.txt` | 崩 0 |
| 5 | 同一个 Context 在 JS→Python 回调里**重入**（生产 `legado_js.py:548` 的内联重入分支仍用同一个 `self`，会从 `_shared_js_contexts[js_lib_key]` 取到同一个缓存 Context 再 eval 一层） | 1/2/4/8/16 层各 5×300，另加最里层抛异常 + 新建 Context 对照 | `qjs_same_context_reentry_attr.py` → `out_qjs_reentry.txt` | `same_inline_*` / `fresh_*` 各 5/5 存活 |
| 6 | 解释器收尾时守护 JS 线程还在原生 eval 里（`legado-quickjs` 与看门狗都是 daemon，`legado_js.py:149-152`） | 3 形状 × 10 子进程，求值标定 400 ms | `qjs_shutdown_race_attr.py` → `out_qjs_shutdown.txt` | 崩 0 / 干净 10 / 未接上 0 |
| 7 | **一条共享 Context 上反复重绑 109 个桥函数**（生产 `_evaluate_locked` 每次求值都重绑；现场那条 `runtime=1481` 被进 5112 次） | 三臂 × 8000 轮 × 109 = **872 000 次重绑**：`accumulate`（重绑+eval，生产形状）/`bindonce`（只绑一次）/`rebindonly`（只重绑不 eval） | `qjs_shared_context_rebind_attr.py 8000 109` | 三臂 rc=0 全活（`accumulate` 1.2 s、`bindonce` 0.7 s、`rebindonly` 0.5 s） |
| 8 | **循环 GC 在另一条线程上回收"捕获了 Context 所有者的桥闭包"**（109 个 lambda 捕获 `self`、`self._shared_js_contexts` 又持 Context → 引用环，只有循环 GC 能收；而 CPython 的 GC 会在任何正在分配的线程上触发） | 四臂 × 40 轮，每轮绑 109 个环闭包 + eval（40 万次 JS 循环）：`gcaway`（B 线程纯 Python churn 逼 GC 落在 B）/`gconA`（无 B）/`plain`（绑不捕获所有者的 lambda，抽掉"环"）/`nogc`（`gc.disable()`，抽掉"GC"） | `qjs_gc_cross_thread_attr.py 40` | 四臂 rc=0 全活 |

第 7、8 是**现场证据出来后照着它造的**：现场那条 Context 复用 5112 次、每次重绑 109 个
桥函数，而 `module.c:385-408` 那段释放路径正是"重绑/GC 会走的地方"。两条都没把进程带走。
第五十四轮由这两条读数推出来的那句结论——"**剩下活着的假设只有一个**：某条 journal 看不见
的线程在这条 eval 还没结束时碰了这条 Context"——**已被第五十五轮否证**：真因是同一条线程
用了不建帧的 `Object.json()`（见上文"§B 的真因"），既不需要第二条线程，也不需要销毁/GC。
这八臂否证的是"跨线程"这一**家族**，不是"不建帧入口"这一条路；后者只有照机制造的臂才量得到。

另两条继续成立、本轮未推翻：**quickjs 求值期间放 GIL**（`qjs_gil_held.py`：198 ms 求值期间
旁路心跳最大间隔 2.0 ms），所以"一条长 eval 把界面循环饿死"也不成立。

## 10054 归因：定了（第五十三轮装身份 → 第六十五轮按身份收噪声）

### 一、当时的取证为什么停在"未定"

`.probe/connection_reset_who_attr.py` → `out_rst_who.txt`：本地**裸 TCP** 假源（不走
aiohttp.web，免得它先把异常吞成 `Error handling request`）对 `/rst` 写半个响应头后
`SO_LINGER=(1,0)`+`close()`；客户端用生产同构形状（专属线程的 asyncio 循环 +
aiohttp `ClientSession`，即 `check_service` 的 probe/search 那条路），循环上挂
`set_exception_handler`。**本机 0 条循环级异常记录**——aiohttp 把被截断的响应当读完
就返回了，`_call_connection_lost` 没进异常处理器。

所以第五十二~五十三轮既不能断言"10054 是扫描噪声"，也不能断言"Flet 客户端掉过线"。
第五十三轮因此在两条真循环上只装身份、不做过滤（`install_loop_attribution`，
见待修清单 3）：判出来之前滤一条都是抹线索。

### 二、现场那份日志把归属判完了（第六十五轮）

开发者自己跑了一次全库扫描（退出码 0、**请求一条没失败**），两族记录都带
`循环[engine]线程=legado-engines loop=0x…`：

1. `Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)`
   + `ConnectionResetError(10054, …)`，traceback 末行 `proactor_events.py:165
   self._sock.shutdown(socket.SHUT_RDWR)`；
2. `Future exception was never retrieved` + `ClientOSError(1,
   '[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] …')`，经 `sslproto.py:651
   _do_shutdown` → `ssl.py:920 unwrap`。

**结论：都是 engine 循环家的 socket 收尾竞态，与 Flet 的 websocket 无关**——第五十三轮
装的身份这一眼就读出来了。`args=(None)` 那半段还额外钉住路径：`Handle._run` 的
`message` 是 `f'Exception in callback {cb!r}({args!r})'` 拼的，`(None)` 说明这次收尾
是 `close()`/`abort()`/`_force_close(None)` 排进来的（读/写路径会把异常本体带进 args），
也就是**我们自己关的连接**在对端复位之后收尾。

### 三、发生频率离线复现不了，但后果能离线量

* **频率**：`.probe/loop_teardown_matrix.py` 把 状态(idle/paused/silence/unread/tls) ×
  动作(close/abort/io-then-close) × 对端(rst/fin) × 延时(0.05/0.2) 摊成 44 格，再加
  3 个 aiohttp 形状 × 2 延时 = 12 格，**62 格全部 0 条循环级记录**（合计那行：归属行 0 +
  default dump 0）；进程句柄起始 293 → 原始 44 格跑完 288 → 12 格 aiohttp 跑完 294（差 +1，
  **不随事件数涨**）。
  根因当场量清：本机回环上 `sock.shutdown(SHUT_RDWR)` 在四种复位状态下**全部成功**
  （读完响应再等复位、一口不读、写完 200KB 无人读、复位后再发一个请求）——回环上发送
  数据总被对端协议栈立刻吸走，**不存在"在途未确认的发送数据"**，而 10054 撞在
  `shutdown()` 上要的正是那个状态（真链路：请求刚写出去、链路或对端复位；或中间盒注入
  RST）。所以这一族的**次数**只能留在现场，文档里不许写离线测不到的数。
* **后果**：复现不了"会抛"就直接造"已经抛"——`.probe/loop_teardown_cleanup_probe.py`
  给 40 条**活着的** proactor 传输层换上抛 10054 的 `shutdown()`，走完 `transport.close()`
  那条回调。三臂（本轮实测）：

  | 臂 | 日志 | 命中后 `fileno≥0` | 句柄轨迹 | `gc.collect()` 后 | ResourceWarning |
  | --- | --- | --- | --- | --- | --- |
  | A 对照（不抛 + 第 53 轮处理器） | 归属 0 + default 0 | **0/40** | 245 / 245 / 245 | 245 | 0 |
  | B 抛 10054 + 第 53 轮处理器（现场那份） | 归属 40 + default **80** = 每事件 3.0 行 | **40/40** | 251 → 299 → 305 | **303** | 23/40 |
  | C 抛 10054 + 第六十五轮处理器 | **1 行 / 40 事件** | **0/40** | 245 / 245 / 245 | 245 | 0 |

  也就是 `proactor_events.py:154-172` 那句 `finally`：`shutdown()` 一抛，166 行
  `sock.close()`、167 行 `_sock = None`、172 行 `_called_connection_lost = True`
  **三行都不执行**，一次竞态留下一个不归还的 socket 句柄（B 臂 +54～+60，显式
  `gc.collect()` 只收回 2 个——`__del__` 那条兜底路要么没跑、要么跑了也不归还句柄，
  40 次里只有 23 条 ResourceWarning）。
  顺带量的一条：`connection_lost` **被调两次**这件事三臂都是 0/40——每臂一条传输层只
  close 一次，155 行那道闸门失效的代价在这个形状里出不来，不写成"没问题"。

### 四、否掉的补法（都有理由，别再走一遍）

* `TCPConnector(enable_cleanup_closed=True)`：Python 3.12 上 aiohttp 自己把它当**死参数**
  （`_cleanup_closed` 只在 `enable` 且非 3.12.x 的分支里被调用），装上去不改任何东西。
* `force_close=True` 或把 `keepalive_timeout` 调小：那是把"优雅收尾"这条更容易撞上
  的路换成"直接 abort"，且第六十二轮实测过的 keep-alive 收益要一起赔掉。
* `connector.close(abort_ssl=True)`：`ClientSession.close()` 没把这个参数透出来，真要打得穿
  会话层，并且它改的是**连接池策略**——超出本轮边界（本轮只处理噪声与句柄，不动池子）。


## 待修清单（按证据强度排）

1. ~~**致命那条没定位，离线不可复现**~~ —— **第五十三轮已装取证手段**，
   **第五十五轮已离线复现并定位真因、已改**（`Object.json()` 不建帧，见上"§B 的真因"）。
   下面这段是当时那套手段的记录。
   `_evaluate_locked` 进原生 eval 之前落一条**遗言日志**
   （`app/services/engine/legado_js.py` 的 `_CrashJournal` / `crash_journal()` /
   `_journal_eval()`）：4 MiB 定长环形文件（16384 条 × 256 B），字段是
   `ts pid th depth runtime fresh jslib bytes rule url`，默认落在
   `%LOCALAPPDATA%\LegadoBookSourceCheck\diagnostics\quickjs-eval.journal`，
   `LEGADO_JS_JOURNAL=0|off|no` 关、给路径则改写该路径。
   **落点在进原生码之前**，因为 0xC0000409 那条路绕过 Python 异常、信号处理器、
   atexit 与 `faulthandler`（实测见下）。不每行 msync：mmap 纯写 0.38 µs，
   追加+flush 1.74 µs，每行 msync 23.47 µs，而一段真规则中位 49.78 µs
   （`.probe/js_crash_journal_probe.py` → `out_journal_cost.txt`，7 轮交替）；
   整条 `_journal_eval` 3.69 µs（`out_journal_field_ab.txt`），放到真求值路径上
   开/关两臂交替实测 **−0.24%（−21 µs / 8.9 ms），落在噪声里**
   （`out_journal_ab_cost.txt`，5 轮 × 300 次）。
   存活实测（每形状 3 个子进程，`out_journal_fastfail.txt`）：
   `ring_nosync` rc 全是 3221226505、盘上遗言 100/100 条；`plain_buffered` 0 条；
   `faulthandler` 栈 0/3 且文件 0 字节，而同一个 faulthandler 接线在 0xC0000005
   对照臂下落栈 3/3、309 字节——所以那个 0/3 不是探针假阴性。
   `_QuickJsDispatcher` 的"在飞任务"不需要另记：日志里的 `runtime`/`th`/`depth`
   就是最后一条进去没出来的那段。
   **下一轮要做的**：现场再崩时读这份文件，`tail` 出崩前那一段的 `rule=`/`url=`。
   —— **第五十四轮已做到**（见上"现场那一崩长什么样"），并把日志从"只记进"补成
   **一进一出成对**，加了 `thid=`（数字线程 id，线程名会重名而 id 不会）与
   `ev=!` 撞车记录。现在的四种记录（`_CrashJournal` 的 docstring 同步了）：

   | 记录 | 谁写 | 字段 | 用途 |
   | --- | --- | --- | --- |
   | `ev=E` | `_journal_eval`，紧贴 `ctx.eval` 之前 | `ts pid th thid depth runtime fresh jslib bytes rule url`，行首 `#序号` 是这份环里的全局序号 | 崩前最后一条 `E` = 崩在哪一次进 |
   | `ev=X` | `_journal_leave`，`_evaluate_locked` 的 `finally` 第一句 | `ref=` **指回自己那条 `E` 的序号**、`took=`(ms)、`out=` `ok` 或 `err` | 没有对应 `X` 的 `E` 就是"没回来"的那一次 |
   | `ev=!` | `_journal_eval` 里发现这条 `runtime` 已在飞、而**在飞的那位线程 id 不是自己** | `thid other_thid other_seq runtime` | 现场证据专用：`ev=X` 全齐却崩了，而盘上有一条 `ev=!` → 就是两条线程进同一条 Context |
   | `ev=C` | `close()` 第一句 `_journal_close` | `thid inst=0x… shared=<共享 Context 条数>` | 谁在哪条线程上销毁实例——`:385-408` 那条释放路径的嫌疑人 |

   在飞表 `_JOURNAL_INFLIGHT`（`ctx 构造序号 → (线程 id, 进记录序号, 上一层同款)`）是
   进程级字典，`_journal_leave` 负责还回上一层 / 弹出，所以嵌套求值（`depth>1`）也能配对。
   正常**永远不该出现 `ev=!`**（求值都排在 dispatcher 唯一线程上），出现就是现场证据。

   **读法**：`crash_journal_inflight(path)` 回"进了原生码却没出来"的那几条 `ev=E`
   （靠 `ref=` 判配对，不看尾巴——尾巴可能只是被环形容量挤掉的顺序假象），`path` 要显式
   给崩溃那份留底文件；老格式（第五十三轮那种没有 `ev=` 的记录）按 `E` 处理，所以在
   `crash-20260930-131140.journal` 上它会回"最后 8 条都没出来"，那是**格式换代**不是新线索。

   容量因此从 4 MiB 提到 **8 MiB / 32768 条**（`_JOURNAL_SIZE`）：现场 15.49 分钟就写了
   13010 条，成对之后同样时长要写 ~26000 条，4 MiB 会在 8 分钟左右把开头挤掉。
   **配对的代价实测量不出来**：`.probe/qjs_journal_pair_cost_attr.py` 三臂
   （关日志 / 只记进 / 进出成对）× 10 轮 × 600 次真求值交替跑，两次独立读数
   ——配对相对只记进 **+11.57 µs/次（+0.13%）** 与 **−23.54 µs/次（−0.25%）**，相对关日志
   **+34.22 µs（+0.37%）** 与 **+2.31 µs（+0.02%）**，三臂 `min~max` 全程互相压着，
   所以结论是"**在噪声里**"，不是某个百分比。早前 5 轮 × 300 次那次读的 +2.03% 已被这两次
   否证（样本不足），不许再引用。
2. ~~`CheckController` 补合并 + `CheckView.render_result` 改 dict 索引~~ ——
   **第五十三轮已改**。`BaseController.subscribe(..., flush_interval=0.5)` 合并的是
   **下发**不是事件（`app/controllers/base.py` 的 `_coalesced_flush`）：处理器照旧
   一条不漏，只把 `page.update()` 按 0.5 s leading+trailing 窗口压成每秒至多两次。
   当时评估过直接套 `throttle` 到处理器上，那是错的——`CheckFinishedEvent` 一源一条，
   合并处理器等于把中间那些源的结果行抹掉。`render_result` 的去重改成
   `self._row_at: dict[str, int]`（原来 `enumerate(self._rows.controls)` 全行扫，
   900 源一轮就是 ~40 万次 getattr）。
3. ~~**10054 归因**：在两条循环上装只加标识、不改行为的 `set_exception_handler`~~ ——
   **第五十三轮已装**：`install_loop_attribution(loop, tag)` 在
   `EngineHub._serve`（tag=`engine`）与 `set_ui_loop`（tag=`ui`）各接一次，
   日志形如 `循环[engine]线程=legado-engines loop=0x… 报告异常：<message>：<exc>`，
   打完身份**原样转给** `default_exception_handler`（一条记录都不许少）。
   白名单过滤按清单原计划**继续等归因结果再做**，本轮不过滤任何噪声。
   —— **第六十五轮归因到手、这一条收完**：现场两族都落在 `循环[engine]`，且请求一条没失败。
   于是按原计划只把**认得出的两族收尾竞态**降成一条带累计计数的短行，并顺手补上
   `proactor_events.py:166-172` 被异常跳过的那三行（族 1 每事件留一个不归还的 socket 句柄）；
   **认不出的异常一律保持第五十三轮那份两遍待遇**。机制、三臂实测数字与被否掉的补法
   见上"10054 归因：定了"一节，护栏与变异见下"第六十五轮落地的改动与护栏"。
   噪声的**次数**仍是现场量（回环上 `shutdown()` 任何复位状态都不抛，离线造不出触发状态），
   本轮不再追。
4. `BaseController.flush()` 整页 diff 是既定形状（要覆盖 page 级覆盖层/对话框，
   `app/controllers/base.py:211`）。900 行 = 21.7 ms 可接受；一旦出现更大的常驻列表
   （换源扫描、书内索引行），用同一支探针**重量**。
5. 本轮全部结论只是**离线形状 + 生产同构循环**，不代表真源组合：917 条真实源的联网
   扫描受杀软约束不能批量跑（记忆 `legado-live-scan-av-block`）。第五十三轮的改动
   同样全部离线验证（真 quickjs + 子进程 + 假源级离线），**没有**跑过真源。
   第五十五轮也一样：现场那条源的规则原文是从本地 `legado.db` 捞的，跑的是真检测链路 +
   `127.0.0.1` 假源，**没有联网**；"改后现场不再崩"这一步要等开发者真跑一次全库扫描。
6. **日志的盲区换成 `object_call` 了**：`ev=E/X/!/C` 记的全是 `ctx.eval` 与实例 close，
   而第五十五轮新加的转换（以及所有 JS 函数从 Python 被调的那一族）走的是
   `object_call`(:257)，**不在记里**。第五十四轮推的那个"补 Context 建/销各落一条"
   （`module.c:382` 那条释放路径）优先级降下来——真因不在那条路上；
   但如果现场再崩而 `ev=X` 全齐、也没有 `ev=!`，第一个该怀疑的就是这条没记账的入口。
7. **拦住崩**（未开工）：把 QuickJS 放子进程，崩只带走 worker 不带走服务端——
   真因这一类已经改掉，但**未知形状**仍只有这条路能兜；第五十五轮的改动**不是**它，
   写注释时不许混。
   **第六十三轮只推进了边界上的一小格，仍然算未开工**：静态审计里那段
   `quickjs.Context().eval()` 预编译（240 条快照中 154 条含 JS、占审计合计 CPU 的
   89%）现在跑在子进程里，它真 abort 带走的是一个 worker——护栏已经按这个形状写了
   （`BrokenProcessPool` → 摘池、**不重跑那条毒源**、回 `not_audited`）。
   但**检测/阅读的规则求值（`legado_js._evaluate_locked` 那条派发线程）一行都没动**，
   还在主进程里，所以 0xC0000409 照样能带走整个应用。把求值也搬进子进程要先解决
   桥回调（Python↔JS 双向、`add_callable` 那条）跨进程的问题，那是另一轮的事。
   **第六十九轮把"另一轮"的前置账量完了（只取证，仍然未开工）**——两份读数以
   `docs/dev-logs/2026-10-03.md` 第六十九轮为准，这里只留裁决要用的那几个数：
   * 桥一共 **109** 个，按实现落点分五档：**纯计算 38**（不需要父进程）、父侧状态 36、
     DOM 句柄 17、再入求值 6、网络/UI 12（`.probe/reading_bridge_census.py` →
     `.probe/out_bridge_census.txt`）。
   * 真库 917 条源、阅读三段里会进 QuickJS 的脚本 **769** 段：静态按**门面表**（从引擎真正
     交给 QuickJS 的那 4 段程序 165 068 字符里解出来的"门面名 → 桥集合"）判成
     **一次父进程回调用不到的 408/769 = 53.1%**；闭包策略换一种重算仍是 53.1%。
   * 把这 408 段真放进"只有 38 个纯计算桥 + 其余换桩"的 Context 里跑
     （`.probe/reading_sandbox_feasibility.py` → `.probe/out_sandbox_feasibility.txt`，
     零联网，逐条过子进程）：**零回调且跑通 253、零回调但没跑通 112、
     动态却打到回调 42、打穿进程 1**。也就是**全库阅读三段有 365/769 = 47.5% 确实不需要
     父进程回一次**。
   * 但静态判据**单独当闸门不行**：那 42/408 = 10.3% 静态看着干净、动态真打到桥
     （集中在 DOM 句柄上的方法——运行时值，按名字解不到）。所以第一段的形状定成
     "静态只做路由 + 子里给 71 个非纯桥立**墓碑**（被调即置位再抛）+ 回 `touched`，
     非空则父进程原样重跑"：判错的代价是"多跑一趟"，不是"跑错"。
   * 两个不许漏的口径：**引擎自己的引导调用与规则无关**——取回 407 段里 406 段的 no-op
     基线都打了 `__get_json('infoMap')`，"零 RPC"要说成"每趟固定一次，除非 infoMap 随任务
     下发"；**49/408 段是静态无从判断的那一类**（`eval(String(source.bookSourceComment))`
     这种参数非字面量的 eval / `new Function`，真代码运行时才拼出来），门控必须把它们
     排除在外，而下面第 9 条那条真崩正在这一类里。
8. `Context.get`:566 / `Context.set`:580 / `Context.parse_json`:551 与本轮修掉的
   `Object.json`:143 是同一族
   **不建帧**的原生入口，今天引擎一处没用（全库 grep 无调用点）。要堵的话，
   最省的形状是往 `test_no_bare_object_json_conversion_is_left_in_the_engine`
   那条 AST 审计里**一并扫 `ctx.get(`/`ctx.set(`**，而不是等下一个人踩。
9. **一条阅读脚本能把进程原地打穿（真桥也打穿，第六十九轮量到的新形状，机制未定）**：
   真库第 866 条源 `⚡📂猫眼看书` 的 `.ruleContent.content` 只有两行——
   `eval(String(source.bookSourceComment)); run("content");`，真正的代码藏在该源
   `bookSourceComment` 那 4074 字符里。复现：
   `PYTHONIOENCODING=utf-8 python .probe/qjs_reading_hidden_eval_crash_attr.py real`
   → 第一份输入就 **段错误，退出码 139 / 0xC0000005**（`.probe/out_reading_crash.txt`），
   Python 侧 `try/except` 一句接不到。
   **不是沙箱桩造成的**：那一次跑的是**真桥**，只把 `runtime.opener` 换成一根立刻抛
   `URLError` 的假把手（零联网），书源状态写到临时目录。
   已排掉的候选：①单纯深递归——无界递归 / 2000 层 / 200 000 层三种形状引擎都抛
   `InternalError` 且活得很好，所以不是 `JS_REENTRY_LIMIT` 那条路没拦住；
   ②库的顶层代码——只取注释字符串、只 `eval` 那段库、只调 `getHost()` 三种都活得，
   崩在 `run("content")` 那一步。
   要接着查就从 `run()` 里那条 `for (i in $.domains)` 循环 + 请求全失败这条路径起手
   （现场能不能读到这本书的正文没验过，别当已知）。它与待修清单 7 是同一件事的两头：
   未知形状只有"求值在子里"这条路能兜，而这条就是它的一条现成理由。
10. **全库还有 22 处 `selectable=True` 的文字，读屏一个都念不出（第七十一轮量到的形状）**：
   `grep -rn "selectable=True" app | grep -v rule_repair_view | wc -l` = **22**，分布在
   **13** 个视图文件——服务仪表盘（日志行、service_id、错误列）、WebDAV / 更新 / 导出
   三处日志、Web 桥 URL 与日志、字典弹窗释义、二维码结果、代码编辑器高亮与错误、
   导入预览、阅读器简介、RSS 正文与链接、找书结果简介、登录地址。
   浏览器语义树实测（`docs/dev-logs/2026-10-03.md` 第七十一轮 §3）：
   `Text(selectable=True)` 在语义树里是 **role/aria-label/textContent 三样全空**的节点，
   中文英文一样；外面套 `ft.Semantics(label=…)` **救不回来**（连标签自己都不进树）。
   自愈页那三处已按实测改法收掉（去掉 `selectable`，整段包 `ft.SelectionArea`——
   `SelectionArea` 包普通 `Text` 实测**字照样在树里**）；其余 22 处同方子，
   但每改一处都得**当场重跑语义树探针**：这些文字里有的是日志（要看的就是那串英文堆栈），
   有的眼下正靠 selectable 复制出去（改法要保住复制面，而**复制面这一路
   探针量不到**，见下面探针坑那条，只能人工点一次）。
11. **给"起手就藏着"的控件套读屏壳，壳的初值必须跟着一起藏（第七十二轮的现场）**：
   flet 客户端有 `X.content must be provided and visible` **一族**断言（`Semantics` /
   `SelectionArea` / `Banner` / `Dismissible` / `RadioGroup` / `Tabs` …，都在同一份
   `flet_web/web/main.dart.js` 常量表里），意思是壳要求它的 `content` 既给了、又可见。
   自愈页那一处已修（`rule_repair_view.py` 给 `_manual_sem` 补 `visible=False`）并钉了
   不变式 `test_reader_wrapper_and_its_field_always_share_visibility`（壳与芯的 `visible`
   必须**相等**，单向钉不住"藏框不收壳"那一半）。**没做的两件**：① 其余视图照第十轮那条
   补读屏面时同族风险一处没排（`grep -rn "ft.Semantics" app --include='*.py'` 眼下只命中
   自愈页，扩一处就得同拍重跑）；② 桌面 `FLET_APP` 上这条到底怎么触发的**没量到**——
   web 臂的正对照（把壳拨回可见）页面照常起来、控制台也不出这句，所以本轮改的是形状，
   不是复现（取证与缺口见 `docs/dev-logs/2026-10-04.md` 第七十二轮 §3）。
12. **老板键"人手点托盘图标才回得来"这一腿没证（第七十三轮）**：藏窗那条链离线能量到
   最后一寸——真类桌面探针把 `WM_LBUTTONUP` 塞进托盘回调消息环，窗口确实回来了（数字见
   `docs/dev-logs/2026-10-04.md` 第七十三轮）；但**"图标真的出现在通知区里、且点得动"**
   要人眼确认一次，仪器造不出这只鼠标。探针留了 `--human` 口（12 秒人工点击窗口），
   本轮**没跑这一拍**。另外两条同族的没验：① 图标被 Windows 收进溢出区（"^"面板）后
   点得到点不到；② **双击**那一下会连着进 `_ACTIVATE_MESSAGES` 的三拍（左按下/左抬起/
   左双击），实测只量了单击，"回来两次/藏回去又弹出来"这种形状靠 `restore()` 的幂等挡住
   了没有，没有现场证据。右键（0x0205 抬起）是**故意**算激活入口的——本项目没有托盘
   菜单，右键点了什么反应都没有会更难发现"回不来"。
   **怎么补**：带 `--human` 跑一次 `.probe/probe_bosskey_roundtrip73.py` 并肉眼确认，
   顺手各点一次单击/双击/右键。
13. **老板键那道"等 0.3 秒再抢前台"是量出来的常数，客户端一换就可能失效（第七十三轮）**：
    `restore()` 发给客户端的 toFront **晚于**其后那条 `visible=False` 落地，所以"点完图标
    立刻再按一次老板键"会卡住——窗口亮在桌面上而标志说藏着，此后每拍 `handle_key` 都直接
    return（0 秒间隔那一臂 2/2 复现，读数在 `docs/dev-logs/2026-10-04.md` 第七十三轮 §6）。
    修法是**发之前把关**：等 `_FRONT_DELAY = 0.3s` 再看藏窗标志，藏着就不发。
    0.3 这个数是当场量到的（那次 ShowWindow 落在发出后 0.09 ~ 0.23 秒），**不是推导出来的**
    ——flet 客户端换版本、或机器负载把它推到 0.3 之后，这条竞态就回来，而
    `tests/test_shortcut_page.py` 照不出来（替身的 toFront 没有时间维度）。
    **怎么补**：改 flet 版本后重跑 `.probe/probe_bosskey_roundtrip73.py --keys`，
    看 0.0s 那两轮的时间线是不是仍一路"藏"到底。
14. **背景图的三条没证的腿（第七十四轮）**：桌面像素探针
    （`.probe/probe_background_paint74.py`，`page.take_screenshot` + 6×4 分格比图）量到了
    "选图→那一层真出现在屏幕上"（整窗 0.4396、切页与缩放重建后 0.0=还在、清除后回基线 0.0，
    读数在 `docs/dev-logs/2026-10-05.md` 第七十四轮 §7），但三条仍是空的：
    ① **原生 Windows 选图框那一下**——仪器只能替换 `pick_files` 的返回值，证不了"对话框真弹得
    出来、选完路径真给得回来"（FilePicker 必须挂进 `page.overlay` 才工作，那一拍的接线只有
    人工点一次才验到）；
    ② **`_BACKGROUND_OPACITY = 0.25` 压不压得住正文可读性**——像素差只证明"画上了"，
    深浅两面色差够不够、花哨照片上面文字还读不读得清，**要人眼**；
    ③ 探针里"最左 1/6 那一列（导航栏）平均变化 0.0"的解释（图铺在布局底下、Material
    `NavigationRail` 自己涂了不透明面）是**从形状推的**，没单独量过"换成透明面会怎样"。
    **怎么补**：人工走一次外观页选图（顺手看一眼正文），把 ①② 结掉；③ 要么量要么删掉这句解释。
15. **【已修 · 第七十六轮】窄高窗（竖屏）内容出屏，而且拿不到机器判据（第七十五轮）**：桌面像素探针在
    360 与 500 两档各拍 23 页，贴边筛子挑出四页，人眼复核确认——
    `reading.reader` 工具条里"字号 −/16/＋、下一章"**整组出屏**、中间状态文字被挤成一字一行
    竖排；`tools.webdav` 的"列出远端备份"整只按钮出屏（500 档就出一半）；
    `tools.export` 下拉右侧刷新图标切掉一半；`tools.replace` 工具行末尾"共 0 条…"截断。
    结构普查（写死 `width` 对账）只能当**下界筛子**：`sources.repair` 的 `TextField width=520`
    是**误报**（被上游约束夹住，屏幕上没缺），后两处是**漏报**（固定宽 + 自适应宽混排，
    "全固定宽行"这条判据不成立但兄弟照样被挤出去）。
    **判据的天花板先钉死**：桌面 release 客户端对溢出**零输出**（明知溢出的那一拍，
    管子已用子进程 print 正对照证明接上了，日志仍只有那一行；调试条纹像素也全是 0），
    所以竖屏验收**只能像素筛 + 人眼**，不许拿"探针全绿"当"没溢出"。
    **怎么修**：阅读器工具条那行改窄窗降级（把字号/翻页收进溢出菜单或 `ResponsiveRow`）、
    webdav/export/replace 三行给固定宽那位 `expand`/换行；改完重跑
    `.probe/probe_portrait_overflow75.py 360 780` + `.probe/measure_portrait_edges75.py _360`，
    贴边候选要从 4 页降下来，并逐张人看。
    **第七十六轮改完**：五处，不是四处——`measure_portrait_edges75.py` 原先用
    `{"fits","overflows","dict"}` 排参照图，`dict` 把**真页面** `tools.dict`（"查询"按钮
    整只在窗外）一起排掉了，基线其实是 5 页。改法见 `docs/dev-logs/2026-10-05.md`
    第七十六轮 §3 那张表：reader 工具条 `wrap=True` + 进度标签定宽、webdav/export/dict
    三处 `expand=True`、replace/dict 两处 `wrap=True`。
    **读数**（tag 76，两档各重跑一遍）：360 档贴边候选 **5 → 1**、1200 档 0 → 0、
    24 页里拍得出来的 **23 → 24**（`tools.font` 那一页见第 16 条）。剩下那一处
    `reading_reader_360` 末列只采样到 1 个非背景点，人看图确认是正文下面那条通栏分隔线。
    逐张人看：`reader`/`dict`/`export`/`webdav`/`replace`/`font`，两档都看。
    **推翻了一条旧判据**：`test_reader_read_aloud_placement.py` 原本钉"工具栏不许 wrap"
    （第二十八轮栅格教训），而 12 颗 40px 图标在 344px 客户区里怎么排都放不下。
    判据改成"必须 wrap、且行里任何子项不带 `expand`"，代价写明：**工具栏行高随窗宽变**，
    它下面那条朗读条的位置跟着变——这一条本轮没做连续拖窗观察。
    **还留一条没修**：`tools.webdav` 的"备份对象"下拉 `width=160`，floating label 顶到
    上面那条字段的边框上（1200 档同样紧）。它是间距问题不是窄窗降级。
16. **进"配置 → 更多工具 → 字体"整个界面必现锁死，只能杀进程（第七十五轮）**：
    `FontController.load_fonts` 把 `scan_system_fonts()` 与 `list_user_fonts()` 两条协程都
    `ui_call` 到**同一条界面循环**上，再给第一条挂 done 回调，回调里 `_safe(user_future)`
    同步 `.result()` 等第二条——第一条先派发就先落值，回调必然跑在第二条之前，
    循环被回调占着，第二条永远轮不到跑。看门狗打出的栈是
    `base.py:246 → font_controller.py:61 → :66 future.result() → threading.Condition.wait`，
    离线两臂钉死（基线臂心跳满拍 47/1.5s，必现臂 12s 回不来；**不给延迟也照样锁死**，
    不是竞态）。`app/controllers/base.py:225` 的 docstring 早就写着这条规矩
    （"等待用 `wrap_future` 而不是 `future.result()`"），踩的是字体控制器。
    同族扫描：全库控制器 done 回调里 `.result()` 共 30 处，其余 29 处等的都是回调自己那条
    future（值已落，不等），跨 future 的只有这一处。
    **怎么修**：合并成一条协程（一次 `ui_call` 里 `await` 两条），或回调里改 `ui_await` /
    `asyncio.wrap_future`；护栏用**心跳计数**当机器判据（界面循环停跳 = 红）。
    顺带一条否证：钱**不在**字体扫描本身（离线实测 349 文件 / 153 族名 / 0.08s）。
    **第七十六轮改完**：走"合并成一条协程"那条（`font_controller.py:51` 起，两条 await 在 `:72`/`:77`，各兜各的
    异常，沿用改前 `_safe` 的口径）。护栏 `tests/test_font_page_loop_liveness.py` 三条，
    判据是**另一条线程**拿着的秒表读出的心跳断供秒数（`wait_for` 的超时自己就堵在循环上，
    不能当秒表）。变异核验：换回当年那条写法，3 次跑 3 次红。
    **仪器自己栽过两次，都是本轮当场量出来的**（同族："绿灯的那条正是最该红的"）：
    ① 两次读取同速时死锁复现不出来——第一次落值时第二条已经跑完，`.result()` 当场拿到值；
    改前那一跑两条正臂只红了一条。→ 第二次读取放慢到 30 拍，正是上面记的那条真形状。
    ② `concurrent.futures.Future.add_done_callback` 在登记那一刻 future **已经算完**时会
    当场在**主线程**调用回调，那一臂冻的是主线程、界面循环满拍（读数 0.016s，20 次撞见 2 次）。
    → 两道闸门：`scan` 闸在登记回调前不开，`user` 闸整臂不开。改完 8 连跑 8/8 全绿。
    另外判链 `test_controller_service_surface.py` 拦了一次：把服务调用套进取值助手
    （`await self._read(self._svc.xxx(), ...)`）就不认了，它只看紧邻父节点是不是
    `Await`/派发器。这条护栏拦的是改法，不是产品代码。

## 探针/工具坑（同属"探针自己没测到"被当成"被测对象没问题"）

### 第七十六轮新增的一条（wrap×expand：客户端不报错，只把整行画成灰）

- **`Row(wrap=True)` 里任何子项带 `expand`（连 `expand=False` 都算）→ release 桌面客户端
  把整条行画成一块 196,196,196 的灰，并且不再产出后面的兄弟节点**。第一版像素探针五张图
  全灰，看着像"探针写坏了"，其实是每一臂都显式传了 `expand`。五臂对照钉死这条：
  `.probe/probe_wrap_expand76.py`（`nowrap_plain` / `wrap_plain` / `wrap_expand_false` /
  `wrap_expand_true` / `nowrap_expand_true`，只有中间两臂灰）。**不换行的 Row + `expand`
  是好的**，且按 flex 分份——所以"给固定宽那位换 `expand`"和"那一行改 `wrap`"两招
  **不能用在同一行上**。全库普查 `.probe/census_wrap_expand_clash76.py`（24 棵目的地树，
  当前命中 0 处；盲区：槽位表只覆盖常见容器，运行时才改 `expand` 的写法扫不出来）。

### 第七十五轮新增的四条（桌面像素与"停住"这类读数）

- **fd 级捕获客户端输出必须排在 `ft.run` 之前**：`flet_desktop` 是
  `subprocess.Popen(args, env=...)`（不传 stdout），客户端继承的是**被 spawn 那一刻**的
  fd 1/2。写在 `main(page)`/`walk()` 里再 `os.dup2` 只改到探针自己头上——日志 0 行看着
  像"客户端一个字没说"，其实**根本没接上**。接上之后还要补一根明知非 0 的正对照
  （`subprocess.run([sys.executable, "-c", "print('PIPE-OK-75')"])` 落进同一个文件），
  否则"零行"仍然只是"我没量到"。
- **`faulthandler.dump_traceback_later` 在 Windows 上不生效**：进程确实停住了（`Get-Process`
  六秒 CPU 一动不动），dump 文件仍 0 字节，而且它**不抛错**。要看停住的栈只能自己起一条
  纯 Python 看门狗线程 + `sys._current_frames()` + `traceback.print_stack`；
  反过来，如果连看门狗线程都不出字，那读数就变成"有原生代码攥着 GIL"，同样是信息。
- **`asyncio.wait_for` 超时没生效 = 一条读数，不是一条坏掉的探针**：`wait_for` 靠事件循环
  还能转。它不触发而进程又不动，说明那一段是**同步**压在循环上（本轮的字体页死锁就是靠
  这条区分出"同步阻塞"与"某个 await 没回来"）。
- **结构普查（比对写死的 `width`）两头都不准，只能当下界筛子**：固定宽控件会被上游约束
  夹住（`TextField width=520` 在 484px 窗里屏幕上什么都不缺 = 误报），而"固定宽 + 自适应宽"
  混排的行合计不超也照样把兄弟挤出屏幕（`tools.export`/`tools.replace` 两处 = 漏报）。
  另一条同族：窗宽 ≠ 客户区宽（`window.width=500` 截图 484、`=360` 截图 344，差 16px 边框），
  所有竖屏判据要按**截图宽**算。

- **界面探针不能 `page.route("**/*.woff2", 本地 CJK 字体)`**：它把 Flutter web 引擎自己的
  kit 字体一并顶掉 → 客户端会话握不完手 → 服务端 `main` 永远不被调用 → 表现是**零输出**，
  看着既像"没跑起来"又像"跑了没问题"。要中文字形只能 route 具体字体 URL。
  隔离对照：`_mini2.py font`（`out_mini_font.txt`，只有 goto ok）vs
  `nofont`（`out_mini_nofont.txt`，立刻出 `[main]`/`[walk]`）。
- **空重叠窗口**：收尾竞态探针第一版每次 eval 只有几毫秒，主线程置 STOP 后 worker 立刻
  出来，"8/8 干净"量的是**没发生的事**。必须先把求值标定到 400 ms，并在**进原生 eval 前**
  置位、返回前读回，只有接上的样本才算数。
- **假对照组**：重入探针的"新建 Context"对照组一开始没在 inner Context 上注册回调，JS 直接
  `ReferenceError` 被分类成探针报错（0/5 存活）；补上 `add_callable` 后 5/5 活，才可比。
- **手搓 Win32 结构**：`qjs_context_leak_attr.py` 第一版 RSS 恒 0.0 MB——ctypes 的
  `PROCESS_MEMORY_COUNTERS` 多写了 `WorkingSetSizeEx`（那是 `_EX` 结构的字段），`cb` 对不上
  → 调用静默失败。改用 psutil。
- `ft.run` 永不返回，探针收尾要 `os._exit(0)`；`| head -N` 会吃掉不退出程序的全部输出，
  改成重定向到文件再读。
- **但 `os._exit` 只杀 Python 这一侧**（第七十三轮）：桌面 `FLET_APP` 的窗口是它 spawn 出去的
  `flet.exe`，硬退出后**那只窗口还活着**。第二跑按标题 `EnumWindows` 因此量到**两只**同名窗口，
  藏/显时序对不上，量出的延迟全是 `nan`——看着像"这套 API 不可信"，实际是上一轮的遗骸。
  三条一起才干净：① 窗口标题带 PID（每跑唯一）；② 基线那拍**严格要求只有一只可见窗口**，
  不满足就当场判定"仪器没接上、下面读数都不算数"；③ 收尾先 `await page.window.close()`
  再 `os._exit`。已经留场的孤魂用 `netstat`/`tasklist` 认 PID 后 `taskkill`（别乱杀）。
- **一次 `SendInput` 塞一整串键，会把修饰键自己抹掉**（第七十三轮）：把 Ctrl↓ Alt↓ H↓ H↑ Alt↑ Ctrl↑
  六条放进**同一次**调用，flet 只送到 1 个事件而且 `ctrl/alt` 全 False——读起来像"这套客户端
  根本不写修饰键，默认组合键是死的"。改成**一次一键、中间隔 60ms**，并在落主键那一刻用
  `GetAsyncKeyState` 把 OS 键盘状态一起量出来（`ctrl=1 alt=1` 对上事件里的 `ctrl=True`），
  两条都对上才算真读数。顺带两条同族：`INPUT` 的 union 少了 MOUSEINPUT 时 `sizeof` 是 32 不是 40，
  `SendInput` 一条都不注入并返回 0（就是上面"手搓 Win32 结构 cb 对不上"那一族）；
  **全局注入前必须确认前台是自己那扇窗**，抢不到焦点就整段跳过——否则键打进用户正在用的程序里。
- **"事件计数为 0"的对照臂要配一根明知非 0 的正对照**（第七十三轮）：两把只按裸键的对照臂
  （H / Ctrl+F）都读出"没有事件"，而同一条注入路径、带重新抢焦点的那把收到了事件——
  说明那两条空读是**仪器没接上**，不是功能断了。没有这根正对照，就会把"我没量到"写成"它不发生"。
- **探针往控件上挂 handler 会顶掉被测的那只**（第七十一轮）：界面探针 `main()` 里写
  `view._manual.on_change = 记数用的 handler`，视图自己那只"同步读屏标签"的 handler
  就没了——第一遍"改后"读数因此仍是 `正文页地址：还没填`，看着像改动没生效。
  挂之前必须把原 handler 接进链里（记完读数再 `prev(e)`）。
- **Flutter canvas 上的拖选/双击在 Playwright 里量不到**（第七十一轮）：剪贴板管子自证
  是通的（写 `SENTINEL-71` 读回一致），但三格（含线上就在用的 `selectable=True` 那一格）
  拖选 + Ctrl+C 与双击选词全拿不到字，`SelectionArea.on_change` 0 拍。基线也失败 =
  手势没打进去，**不许把三条空读写成"三种写法都不能复制"**（同
  `zero-counts-need-a-positive-control`）。这一面要验只能人工点一次。
- **Semantics 的标签不是万能的**（第七十一轮矩阵）：包 `Dropdown` ✅、包 `TextField` ✅
  （且不打断输入）、包 `Text(selectable=True)` ❌ 标签一起消失。所以"补读屏面"的
  每一步都得在浏览器里重读一次，静态断言 `.label` 字符串只证明 Python 对象改了。
- **探针在 `main()` 里替被测对象把状态推好**（第七十二轮，就是这一条让现场报错漏了一整晚）：
  上一轮那支读屏探针 `main()` 里先 `view.set_manual_visible(True)` 再 `page.update()`，
  于是客户端从来没见过"壳亮着、芯藏着"这一对——三拍全绿，用户一进页就红。
  **起手那一拍必须原样**；要验"亮出来之后长什么样"就再跑一拍，别在挂上页之前替它推状态。
- **界面探针的浏览器线程不能在 `main()` 里起**（第七十二轮）：`main()` 要等客户端连上来才被
  调用，而客户端要有人导航才会连 → 死锁，**零输出**、`timeout` 退 124，看着像"探针没跑起来"。
  照旧写法：`ft.run(...)` 之前 `threading.Thread(target=playwright_thread, ...).start()`。
- **正对照不响就别下结论**（第七十二轮）：把壳拨回 `visible=True`（芯仍藏）那一条臂，web 客户端
  页面照常起来、`console`/`pageerror` 里也没有那句 `Semantics.content must be provided and
  visible`。这说明**仪器没接上**（这条断言在 web  release 面上不落控制台），默认臂的"干净"
  只能证明"这一页原样起手是好的"，不能证明触发条件。缺口写在待修清单 11。
- **桌面探针自己绕过装配入口，就等于把被测的那条线摘了**（第七十四轮）：背景图探针第一版
  手工装外壳、没走 `build_app_shell`，而登记全局宿主句柄（`navigation._APP_HOST`）那一步就在
  `build_app_shell` 里 → `current_host()` 永远 None → 读数"外壳 background 还是 `''`、根还是
  `Row`"，看着像"装配根订阅没通"，实际是产品代码对的、仪器没接上。补法：起手一拍先打
  `current_host() 认不认得这个宿主: True` 这根**明知非 0 的正对照**，不成立就当场声明下面的
  读数都不算数（与待修清单 10 那条同一族）。
- **负向断言的作用域与 Windows 路径分隔符**（第七十四轮，一条放过了变异）：
  `assertNotIn("X:/", 文本)` 在 Windows 上是**空断言**——`Path("X:/a.jpg").name` 会把分隔符
  重写成反斜杠，变异前后都不含 `"X:/"`，那条"状态行不许写整条路径"的变异第一轮就是绿的。
  第二次修反了方向：改成扫**整页原文**找"数据目录"，结果**没变异也红**（这一页的提示话本来就
  写着"存进应用数据目录"）。定稿是只扫那一条控件自己的文本（`view._background_label.value`）。
  两条规矩：**负向断言扫的是被测控件的文本，不是整页原文**；**Windows 上别拿 "/" 当路径判据**。

### 第六十九轮新增的两条（同一家族：读数全绿，但仪器压根没接上）

- **`threading.local` 记账记不到求值那条线程**：`runtime.evaluate` 把活派给引擎的
  `legado-quickjs` 线程，桥桩是在**那条线程**上被调的。沙箱可行性探针第一版把桩的记录
  sink 写成 `threading.local()`，于是父线程每次读回空表，量出"零回调 408/408 段"——
  一份**读数器没接上**的假读数，而它看起来恰好支持本轮想要的那个结论（"一半脚本不需要
  父进程"），所以格外不容易起疑。修法：sink 提到进程级 + 一把粗锁（求值本来就被
  `_QUICKJS_LOCK` 串成一条）。**凡是"计数为 0"的读数，先问那句"明知不是 0 的那根跑了没有"**，
  这就是 `self_check` 那两根对照（一根 `__ajax` 明知要打到、一根纯计算 `__md5` 明知不该打到）
  存在的全部理由；它们上线当天就把这份假读数抓了出来。
- **想按 `ctx.eval` 的文本把"引擎自己的调用"和"规则的调用"分开，这条路走不通**：实测一次
  求值是四段（80 字符 stringify 把手 / **174 264 字符把门面程序和这条规则拼成同一段** /
  53 字符 gInt 取值 / 57 字符收尾 stringify），规则正文就嵌在那 174 KB 里，按长度或按哨兵
  切都会把引导调用记到规则头上（第一版据此把 40/40 段判成"要回调"，与上一条的假读数
  方向正好相反）。切不开就只能**减**：同一条源、同一个输入先跑一根 no-op 规则量基线，
  再按**次数**相减。顺带量出来的一个真口径：`__get_json('infoMap')` 是引导期打的，
  与规则内容无关，写"零 RPC"之前必须先把这种固定开销剥出去。

### 第五十五轮新增的坑（五条，四条还是"探针自己没测到"家族）

- **探针把自己的输入毁了**：桥记账探针把记录路径当参数 `sys.argv[2]`，而子进程里那句
  `os.open(fd, O_WRONLY|O_CREAT|O_TRUNC)` 打的就是**同一个参数位**——于是
  `.probe/_manwaza_source.json`（从 `legado.db` 捞出来的那份现场规则）被清成 0 字节，
  下一支探针 rc=1 报 `JSONDecodeError`，看着像"复现失败"其实是**样本没了**。
  留底数据必须只读打开（`file:...?mode=ro&immutable=1`），并且记账文件的 fd 与样本文件
  不能是同一个 argv 位。
- **`python -c` 下 argv 全体左移一位**（`sys.argv[0] == '-c'`）：子脚本按 `argv[1]` 读探针
  传的第 1 个真参数，写 `argv[2]` 就读到空——症状是"探针跑起来了但拿不到样本"。
- **判定档把"探针报错"记成崩**：`rc != 0` 不等于"被测形状把进程带走了"。第一版因此把
  `JSONDecodeError`（rc=1）统计成崩溃样本，"复现成功"是假的。现在分三档，
  只有 rc∈{3221226505, 0xC0000409} 或 stderr 有 `Assertion failed` 才算崩。
- **记账行里嵌真换行**：桥函数的返回值 `repr` 带规则原文的换行，一条记录被打成四行，
  父进程按 `split()[1]` 取序号直接 IndexError。写行前把空白压平（与遗言日志同一支做法），
  读侧再按"首字段是数字"过滤。
- **`java.getElements()` 返回的是 Legado 的 `Elements`（array-like），没有 `.map`**：
  六臂探针第一版"取文本"那一臂写成 `.map(e => e.text())`，`TypeError: not a function`
  被分类成"探针报错"——白搭一臂。要构造"句柄藏在对象里再整体转换"就写
  `({rows: java.getElements(...)})`。

## 第五十三轮落地的改动与护栏

| 改动 | 落点 | 护栏（红=改动失效） |
| --- | --- | --- |
| 遗言日志 | `app/services/engine/legado_js.py`：`_CrashJournal`、`crash_journal()`、`LegadoJsRuntime._journal_eval()`，调用点紧贴 `ctx.eval` 之前；第五十四轮补 `_journal_leave()`（`finally` 第一句）、`_journal_close()`（`close()` 第一句）、`_JOURNAL_INFLIGHT` 在飞表、`crash_journal_inflight()` 读回 | `tests/test_check_performance.py::QuickJsCrashJournalTests`（**11 条**：每 eval 一进一出成对且带
rule/url、求值失败照旧落出记录、**桥回调里真按 0xC0000409 死掉时盘上留一条没闭合的 `ev=E`**、
**外线程撞进同一条 Context 当场落 `ev=!`**、**在飞表求值完必清空**、**老格式记录按"没出来"算**、
子进程 fast-fail 后遗言还在盘上、日志坏了求值照跑且不再逐条重付、`=0` 一个字节不落、
按源码位置钉"落在 eval 之前"、环形覆盖保持写入序） |
| 下发合并 | `BaseController.subscribe(..., flush_interval=…)` + `_coalesced_flush`；`CheckController.on_attach` 用 0.5 s | `tests/test_core_lifecycle.py`：20 条事件全渲染但只下发 1 次 + 窗口末尾补发 1 次；不传参时逐条下发（对照）；`detach` 撤掉悬挂补发 |
| dict 去重 | `CheckView._row_at` | `tests/test_views_source_m3.py::CheckViewRenderTests::test_repeat_result_replaces_in_place_without_losing_rows` |
| 循环异常归属 | `app/core/engine_worker.py`：`install_loop_attribution`，接在 `EngineHub._serve`（`engine`）与 `set_ui_loop`（`ui`） | `tests/test_core_lifecycle.py`：tag/线程名/loop id 都打出来、原始那条记录照旧转给 `default_exception_handler`、不带 exception 的 context 也要落一条、同一循环只装一层、两条真循环各自报得出自己的线程名（接线核验） |

变异核验（护栏不许空转，逐条把改动改坏后跑对应用例）：摘掉 `_journal_eval` 调用点
→ 4 红（每 eval 一条、fast-fail 存活、坏日志不带崩求值、按源码位置钉落点）；不挂 trailing 补发 → 红；窗口内连处理器一起合掉 → 红；
`render_result` 不再维护 `_row_at` → 红；`_serve` 或 `set_ui_loop` 不装归属、
归属处理器不转默认处理器 → 各红。

**第五十四轮对新记录形状补做的四次变异**（将生产文件先备份到 `D:/tmp/btj/`，逐条改坏跑
`-k QuickJsCrashJournal`，跑完还原并逐字节比对 `还原一致: True`）：

| 变异 | 后果 |
| --- | --- |
| `finally` 里不调 `_journal_leave` | **4 红**（成对、失败也落出记录、回调崩只剩一条 `E`、在飞表清空） |
| 把 `ev=!` 那个分支短路掉 | 1 红（外线程撞车那条） |
| 读回器把老格式当"已闭合" | 1 红（老格式兼容那条） |
| `ev=E` 丢掉 `thid=` 字段 | 1 红（撞车记录要断言 `thid=` 属于后来那条线程） |

全量回归（第五十四轮）：**1553 passed + 147 subtests**（113.24 s，
`--basetemp=D:/tmp/bt_j`）。

顺带一条测试侧约束：`tests/conftest.py` 里 `LEGADO_JS_JOURNAL` 缺省置 `0`。日志默认
落在数据目录，一次全量回归会把开发者手上唯一那份崩溃证据改名成 `*.prev` 再覆盖掉。

## 第五十五轮落地的改动与护栏

| 改动 | 落点 | 护栏（红=改动失效） |
| --- | --- | --- |
| JS 值的 JSON 转换一律走**带帧的 stringify** | `app/services/engine/legado_js.py`：`_JS_STRINGIFY_FRAME`（线程局部）、`_JS_STRINGIFY_PROGRAM`（闭包硬化的转换函数）、`LegadoJsRuntime._eval_with_stringify_frame()`（建帧→跑规则→转换→还帧）、`LegadoJsRuntime._json_of()`（无帧抛 `JsBridgeError`，不退回 `.json()`）；四处调用点改为 `_json_of` | `tests/test_check_performance.py::QuickJsStringifyFrameTests`（**5 条**：①**子进程**跑末句返回元素句柄的规则——断言 rc=0 **且** 8 条元素真转换出来 **且** 桥回调里那次转换跑成 `str`；②记账式假对象：有帧时 `calls == ["frame"]`（一次都不碰 `Object.json()`）、无帧时抛 `JsBridgeError` 且 `calls == []`；③帧进出成对：抛异常要弹干净、嵌套要把**外层那一份**还回来；④AST 审计引擎里不许再有 `X.json()` 调用点；⑤规则改写 `JSON` 之后转换仍走建帧时那一份） |

变异核验（`.probe/_mut55_frame.py`，7 条逐条改坏 → 跑对应 node id → 整文件写回 + 字节一致断言；
判据"执行 ≥1 条且失败 ≥1 条"）：

| 变异 | 执行/失败 | 抓到它的护栏 |
| --- | --- | --- |
| M1 无帧时退回 `return value.json()` | 2/2 | ②（`calls` 里冒出 `object_json`）+ ④（AST） |
| M2 求值返回处又用 `value.json()` | 2/2 | ①（子进程**当场量到 rc=3221226505 + `Assertion failed ... line 99`**，见 `.probe/_mut55_why_m2.py`）+ ④ |
| M3 `finally` 里不还原帧（改成 `pass`） | 1/1 | ③ |
| M4 还原时置 `None`（不还外层那一份） | 1/1 | ③ |
| M5 根本不建帧 | 1/1 | ①（求值整条抛 `JsBridgeError`） |
| M6 不做闭包硬化（函数体里现读全局 `JSON`） | 1/1 | ⑤ |
| M7 桥回调里两处转换退回 `native.json()` | 1/1 | ④（AST 一次扫出两行） |

七条都有红，护栏不空转；跑完**还原逐字节一致**，还原后再跑一遍五条全绿（`执行 5 条 / 失败 0 条`）。

**一条真回归，是全量跑抓出来的**：`QuickJsCrashJournalTests.test_hook_is_placed_before_the_native_call`
按源码位置钉"遗言日志落在原生 eval 之前"，它读的是 `_evaluate_locked` 里的 `.eval(` ——
本轮把原生 eval 搬进了 `_eval_with_stringify_frame`，于是那句在**空文本**上比大小，直接红。
这正是记忆 `refactor-regression-orphan-methods` 的形状：改动把一段代码搬走，
护栏的锚点还在老地方，测试全绿时读的是空气。现已改成读**两段**：
`_evaluate_locked` 里钉"日志句在那句调用之前"（并断言调用点恰好一处），
`_eval_with_stringify_frame` 里钉"里面真有两次原生 eval（建帧 + 跑规则）"，
栽样本（把日志句摘掉再拼到方法末尾）照旧会被扫出来。

全量回归（第五十五轮）：见 `docs/dev-logs/2026-09-30.md` 第五十五轮——数字当场读，不在这抄两份。

## 另一族症状：检测越跑越慢、进度看着不对（第五十九~六十一轮）

上面拆的 A（界面循环被压住）/ B（原生 abort）两条链，与开发者随后报的
"检测越跑越慢"和"统计是 12/12 而不是 1/936"**无关**——那两条落到实处是两个累积器
加一个统计口径，全部按"先离线复现取证再按证据改"量出来再动的手（探针与完整表在
`docs/dev-logs/2026-09-30.md` 第五十九~六十二轮）：

| 累积器 | 形状 | 实测代价 | 改法 |
| --- | --- | --- | --- |
| (a) 响应级整罐 cookie 写盘 | 每个响应把整罐重水化 + 整罐落库，而**绝大多数响应根本没动罐** | 罐 16/512/2048 条时 0.847 / 2.195 / 6.597 ms 每次、写库 20 次；带脏闸门后 0.001 ms、写库 0 次（真的动了罐才 1.092 / 3.557 / 11.980 ms、20 次） | `js.cookie_revision` 对 `engine._cookie_persist_revision`，罐变了才进 |
| (b) 每源复检后扇出的静态审计 | 每条结果 `create_task` 甩进**默认线程池**、没人管并发 | 240 条快照：扇出无上限时**峰值 206 条同时在跑**、返回时 0 条未完；限并发 2 之后峰值 2、返回时还剩 17 条没跑完。**这两栏的墙钟（8167/16186/13596 ms）与"检测自己那条 `to_thread` 排队时长"（8029/13258/8528 ms）已在第六十三轮作废**——探针把"检测自己要的那条 CPU 步"顶替成了 `_audit_one` 本身，三栏背的都是探针自造的 CPU（真那一步中位 0.011 ms、合计 3.6~47.0 ms，`_audit_one` 中位 3.0~3.2 ms、合计 7733~7845 ms，差 290 倍） | 第一档 `AUDIT_CONCURRENCY=2` + 留引用 + 每轮换信号量 + 取消连尾巴一起取消 + `AUDIT_DRAIN_BUDGET=5.0`；**第二档（第六十三轮）把 `_audit_one` 整条搬进进程池**：`run_in_executor(pool,…)`、扇出上限跟着执行位置分两档（子进程 8 / 退回线程 2）、建不出来或被打穿就退回线程池并**给得出"这条没审"**、大轮收尾关池还内存。240 条实测 5697→2542 ms（−55.4%）、审完 159→240 条、前台那条步 p50 10.087→0.923 ms、父进程二代 GC 16~17 次/轮→**0 次** |
| (c) 进度分母 | "12/12"看着像数错了，其实是**订阅方自己数**：本视图自己点"开始"的那一轮从来没人告诉它 total | — | `CheckFinishedEvent.done/total` 带分母，控制器没分母就**不画**进度条，不用完成数冒充 |

三条各带变异核验（6/6、7/7、4/4 全抓到）。**还有一条只记录未处理**：每源新建引擎时把
整罐 cookie **重新水化**（读的那一头，罐 16→2048 条时 0.76→11.45 ms）——闸门堵住的是
"没人动罐就别写"，读这一头还在。

(b) 这一条在第六十轮只做到了"限并发"，**"重 CPU 还在本进程的默认线程池里"那一半**
是第六十三轮补的：静态审计整条 `_audit_one` 搬进 `ProcessPoolExecutor(spawn)`，
扇出上限跟着执行位置分两档，池建不出来/被打穿就退回线程池并显式记"这条没审"
（`AuditReport.not_audited`，导入面、编辑器面、`audit_log` 三个出口都看得见）。
配套 15 个变异（`.probe/_mut63_pool.py` P1~P15）**15/15 抓到**。收益、内存取舍
（`max_tasks_per_child` 被否，改成大轮收尾关池）与 `max_tasks_per_child` 那笔
+6.4% 的实测，都在 `docs/dev-logs/2026-09-30.md` 第六十三轮。

两条测量坑值得单独记：脏臂必须带 `Max-Age`（会话 cookie 本来就不进持久罐，"0 次写库"
看着像闸门生效其实什么都没发生）；空罐不能当基线（`persistent_cookie_snapshot()` 空罐回
`None`，版本号没被填上，"版本永不推进"这种坏写法照样全绿）。
第三条是第六十三轮从第六十轮自己身上量出来的：**探针里"被测方要用到的那条 CPU 步"
不能拿被测对象本身顶替**——那一轮的对照臂把检测自己要的步骤写成 `_audit_one`，
于是三栏墙钟全被探针自造的 7.8 s 淹没，"不跑审计也要 8167 ms"这种话就是这么写进文档的。
要么用真函数（本轮改为 `_unsupported_java_packages`），要么在登数之前先重量一次步长。

## 同一篇里的第四型：一个服务卡住会连累别的（未修）

"书源服务卡住后书架就不能用了"——机制在 `.probe/quickjs_dispatcher_head_of_line.py`
上量清，形状是**排队而不是死锁**：`legado-quickjs` 只有一条派发线程，看门狗只放行
**调用方的** future，`QUEUE_TIMEOUT` 要到出队时才检查，而阅读路径上有 5 处
`js.evaluate()` 根本没带超时。于是一段长求值能把后面**任何服务**的请求一直顶住。
修法（调用侧一律带截止 + `dispatcher_stuck()` 快速失败）**尚未实现**；在此之前，
"书架突然全红"要先看一眼书源检测是不是正卡在一条长规则上。

## 第六十五轮落地的改动与护栏

| 改动 | 落点 | 护栏（红=改动失效） |
| --- | --- | --- |
| 认收尾竞态的两族指纹 | `app/core/engine_worker.py::_teardown_family`：族 1 要 `_call_connection_lost` 在 message 里 **且** 异常本体是 `ConnectionResetError`/`ConnectionAbortedError`；族 2 要 `OSError` 且 `APPLICATION_DATA_AFTER_CLOSE_NOTIFY` 在其字符串里 | `test_recognition_needs_both_halves_of_the_fingerprint`：只有一半（标记 + `ValueError`、别的回调 + 10054）都**不许**折成计数行 |
| 认出就只落一条带累计计数的短行，不再 default dump | 同文件 `install_loop_attribution` 内的 `handler`：按族计数、`_TEARDOWN_MIN_INTERVAL = 10.0` s 节流，短行里保留 tag/线程/loop id/族/累计次数/异常本体 | `test_socket_teardown_race_is_one_counted_line_and_the_socket_gets_closed`（一行、含"累计第 1 次"、**不转 default**）、`test_every_teardown_event_…collapsed`（5 事件只 1 行）、`test_ssl_close_notify_race_is_counted_but_touches_no_socket` |
| 补上被异常跳过的 socket 清理 | 同文件 `_finish_socket_teardown`：从 `context['handle']._callback.__self__` 拿传输层，`fileno() != -1` 就 `close()`，再把 `_sock = None`、`_called_connection_lost = True` 写回 | 上面两条一起断言 `sock.close_calls == 1` / 五个 socket 全关掉 / `_sock is None` / `_called_connection_lost is True`；族 2 那条断言**一个 socket 都不碰** |
| 认不出的一律照旧 | `handler` 的 else 分支保持第五十三轮那份：身份一行 + **原样转给** `default_exception_handler` | `test_attribution_names_the_loop_and_keeps_the_original_record`（改用**认不出**的异常来钉"不许过滤"）、`test_attribution_reports_a_context_without_an_exception_object`、`test_attribution_is_installed_once_per_loop`、`test_both_production_loops_carry_their_own_identity`（接线核验） |

一处设计坑（写下来免得下次再踩）：**补关不能放进"这次要落日志"的分支里**——降频一生效，
被折掉的那些事件就漏补，等于把句柄泄漏换成"看运气"。现在补关在闸门**之外**，每事件必补。

变异核验（`.probe/mutate_teardown_guards.py`，把生产文件逐条改坏跑 `tests/test_core_lifecycle.py`，
跑完还原并 sha256 逐字节比对 `字节一致=True`）：

| 变异 | 后果 |
| --- | --- |
| M1 不补关 socket | **2 红**（一行那条 + 五条那条） |
| M2 把补关挪进降频闸门之后 | 1 红（5 事件只补到 1 个） |
| M3 族 1 只看回调名、不看异常本体 | 1 红（两半指纹那条） |
| M4 降频闸门失效（条件永假） | 1 红（"5 事件只 1 行"） |
| M5 认出的族也照样转 default | **3 红**（三条"不转 default"全断） |

基线 26 passed / 0 failed，变异 5/5 全抓到。

**本轮实测数字**（`.probe/loop_teardown_cleanup_probe.py`，三臂各 40 事件；完整表见上
"10054 归因：定了"第三节）：现场那份处理器 B 臂 = 每事件 3.0 行 + 40/40 个 socket 没关
+ 句柄 251 → 305（`gc.collect()` 后 303）；改后 C 臂 = 40 事件 1 行 + 0/40 没关 + 全程 245；
对照 A 臂 = 0 行 + 全程 245。

全量回归：**1662 passed + 161 subtests，104.33 s**（`--basetemp=D:/tmp/bt65b`；
第六十四轮基线 1658 + 161，本轮净增 4 条）。`main.py --smoke-test` 退出码 0
（`审计进程池：建池 + 首条 169 ms | 子进程 1 条 / 退回线程 0 条`）。
验证全程离线（`127.0.0.1` 假 TCP/TLS 服务 + 真 proactor 传输层 + 本机进程句柄），
**没有**批量访问任何真实书源站点。

## 相关

- python-quickjs 1.19.4 上游 C 源码：仓库里**没有** `reference/` 目录（早前的路径记录已过期），
  第五十四轮从 PyPI 拉 sdist 读，落在 `D:/tmp/qjs1194/quickjs-1.19.4/module.c`。
  本文所有 `module.c:NNN` 引用都按这份文件行号。
- 记忆 `quickjs-native-crash-risk`（本文件记的两型都在那里：`:83` 那一型是两条线程同时进
  同一个 Context（6/6 崩，护栏在 `tests/test_check_performance.py::QuickJsRuntimeOverlapTests`）；
  `:99` 那一型是**同一条线程用了不建帧的原生入口**（`Object.json()`），
  护栏在 `QuickJsStringifyFrameTests`）
- 记忆 `legado-live-scan-av-block`、`guard-tests-assert-absence`、
  `ab-bench-must-interleave`、`docs-numbers-must-be-measured`
- `docs/dev-logs/2026-09-29.md` 第四十一轮（同一文件的上一轮取证）
