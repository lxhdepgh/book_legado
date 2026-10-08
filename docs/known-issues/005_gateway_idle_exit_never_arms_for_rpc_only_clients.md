# 005 网关的空闲自杀只认 WS 会话，RPC-only 客户端死后引擎永留

- 记录日期：2026-10-08
- 状态：**已修**（同日第八十四轮，走法一收窄版）；修完量到的一个**新**问题另记
  [006](006_headless_teardown_intermittent_5s_join_timeout.md)
- 影响面：`main.py --serve` 无头引擎的收尾；G1 的 Flutter 壳（HTTP `/rpc` 那条路）

## 症状

客户端崩在 `engine.shutdown` 之前（Ctrl-C 之外的杀进程、蓝屏、Dart 侧 panic），
引擎进程不会自己走：端口一直 LISTENING，令牌一直有效，直到下一次起服撞端口或人工清理。

现场证据（当天量到的，`Get-CimInstance` + `Get-NetTCPConnection`）：

```
ProcessId    : 74552
CreationDate : 2026/10/8 16:49:02
CommandLine  : …python.exe -u main.py --serve --port 0 --idle-seconds 5
LocalAddress : 127.0.0.1   LocalPort : 64966
```

那一发的命令行里写着 `--idle-seconds 5`，而它到 19:14 仍活着 = **2.42 小时**，
是那个倒计时的 1700 多倍。（这个进程已在本轮 `taskkill /PID 74552 /F` 收掉，
杀之前复核过命令行就是 `main.py --serve`；四条读数已落在上面，证据不再依赖活体。）

## 根因

`app/services/gateway/service.py::_arm_idle_timer` 第一行就返回：

```python
if self._sessions_seen == 0 or self._idle_timer is not None:
    return
```

而 `self._sessions_seen` 只在一处自增：WS 会话建立（`_h_ws`）。它的两个调用点
也一样只在 WS 那条路上——会话关掉、往 WS 广播失败。也就是说这套自杀
机制的"见过客户端"这件事**只有 WS 会话能证明**。

G0 写它的时候这没有错——`--serve` 的第一个驱动者是 curl 与 flet 侧的 WS 订阅者，那条
docstring 说的就是"纯 curl 试跑不该被一段倒计时打断"。但 G1 的竖切是**纯 HTTP `/rpc`**：
`RpcClient` 一次 WS 都不开（WS 事件面按设计是 G2 的活），于是 `_sessions_seen` 恒 0，
`_arm_idle_timer` 永远在第一行返回，`--idle-seconds` 是一个从没武装的旋钮。

顺带一条同源的：`EngineProcess.start` 给子进程固定传 `--idle-seconds 15`。这条兜底
在 RPC-only 路线上同样是空的——真正收尾的只有客户端主动发的 `engine.shutdown`
（`bootstrap.dart` 的 `onExitRequested`）。正常关窗有读数（3/3 臂 rc=0 + 端口拒绝），
不正常的那一类没有任何人兜。

## 选的是哪一条走法

原来列的三条里选了第 1 条，但**收窄**成"成功办完过一次动作才算见过客户端"：

- `_h_rpc` 进来先 `_cancel_idle_timer()`，回完话在 `finally` 里 `_arm_idle_timer()`
  ——HTTP 客户端没有"会话断开"这个事件，它的空闲只能读成"最后一次成功动作之后
  安静了 N 秒"；
- `_dispatch` 的回信封 `ok` 为真时才 `_clients_seen += 1`（原 `_sessions_seen` 改名）：
  坏 JSON、未知 action、schema 不符的探针**不给**自己上倒计时；令牌不对的连处理器
  都进不来（中间件 401），天然也算不上；
- 原来那条 curl 豁免没有消失，只是搬了家：要一段永不打断的运行就用
  `--idle-seconds 0`（`_arm_idle_timer` 里那条 `<= 0` 判断本来就支持），那才是关倒计时的
  开关。"从没跟人说过话"这一种情况仍然不武装。

没选另两条的理由：第 2 条（RPC 单独一条更长的计时）留下两个生命周期不同的定时器，
将来读日志分不清是谁杀的，而"N 分钟"这个数现在没有依据；第 3 条（租约/心跳）要加
action、加 Dart 侧定时器，是 G2 的量，而本机单用户引擎没有第二个用途需要服务端
知道客户端还活着。

顺手补的一处：`_on_idle_expired` 现在遇到**在途 job** 会推迟一个窗口再看，而不是
当场收摊。job 动作一发就回受理凭据、本体还在跑，这时候自杀等于把用户的活半程杀掉
且没人记账（进度只从事件流走）。代价写在代码注释里：任务本体没有服务端预算
（`mode="job"` 不受 `method.timeout` 约束），所以一个挂死的任务会让这条推迟一直推迟
下去——真要收摊走 `engine.shutdown`。

## 怎么验的

**服务面**（`tests/test_gateway_inproc.py`，断 `exit_event`）五条，都做了变异核验
（`.probe/mut005.py`，五条各改一处、各由对应那条臂变红，改完文件逐字还原）：

| 臂 | 判据 | 变异 |
| --- | --- | --- |
| `…goes_quiet_lets_the_engine_exit` | 一次成功动作之后没人再来 → 置退出标志 | 去掉 `finally` 里的 arm → 红 |
| `…keeps_calling_is_not_cut_off` | 正对照：每 0.1s 一发、跨 6 发，不许被打断 | 去掉入口的 cancel → 红 |
| `…never_succeed_never_arm…` | 坏 JSON + 未知 action + 无令牌，`_clients_seen` 仍是 0 | 失败动作也计数 → 红 |
| `…inflight_job_defers_the_idle_exit` | 在途时不死；任务收尾后仍没人回来就真死 | 去掉 job 推迟 → 红 |
| `…media_reads_keep_the_idle_countdown_at_bay` | 一发 hello 武装 → 8 发封面压住 → 闭嘴后响 | `/media` 不掐表 → 红 |

最后一条是差点漏的：`/media` 起初没跟着改，而一屏封面拉得比窗口久、期间一发 RPC 都没有
的话，只看 `/rpc` 就会把活着的客户端切掉。它同样只掐表/武装、**不给 `_clients_seen`
加数**，所以探针豁免没有被它破坏。

第 2 条是第 1 条的正对照：没有它，把 arm 挪到请求入口（不 cancel）也能让第 1 条绿，
而那样每个活着的客户端都会在 N 秒后被自己掐死。第 4 条两段都断言：只断"在途时不死"
的话，把推迟写成无条件 `return` 也能绿，而那等于永久免死。

**现场面**（`tests/test_gateway_idle_exit_live.py`，真起 `main.py --serve --port 0
--idle-seconds 1`，只用 `POST /rpc`，一次 /ws 都不连——与 G1 的 Flutter 客户端同形）
一次启动量三段：端口连得上（仪器在岗）→ 10 发存活对照跨 2.8s → **不发
`engine.shutdown`** 直接闭嘴，断言 rc=0、延迟不早于窗口、端口随后拒连。

十发读数（从最后一次成功动作到进程真没了，窗口 1.0s）：

```
1.08  6.08  1.08  1.08  1.08  1.08  1.06  1.08  6.08  1.08   （单位 s，rc 全 0）
```

八发是 1.06–1.08s（窗口 1.0s + 收尾 0.06–0.08s），两发是 6.08s。那 **+5.00s** 不是
本条问题的残留：`.probe/idle_exit_teardown_probe.py` 三栏交替（10 发后闭嘴 / 1 发后
闭嘴 / `engine.shutdown`）量到它**在同一轮里同时出现在空闲栏与 shutdown 栏**，
出处是 `app/core/engine_worker.py:228` 的 `thread.join(timeout=5.0)` 走满；
第二次跑 24 发一次都没出现（合计 49 发 4 发，≈8%）。它与 005 无关、也不是这次改动
带来的（`engine.shutdown` 那条路今天一样中招），归因与全部读数见
[006](006_headless_teardown_intermittent_5s_join_timeout.md)。这条臂的上界因此写在
30s 而不是 3s：把 8% 的收尾抖动判成红，只会让人学会重跑。
