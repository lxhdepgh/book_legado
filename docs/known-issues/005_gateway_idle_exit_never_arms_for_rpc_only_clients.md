# 005 网关的空闲自杀只认 WS 会话，RPC-only 客户端死后引擎永留

- 记录日期：2026-10-08
- 状态：**未修，等一次决策**（改的是服务语义，不在 G1 的"不动产品语义"范围里）
- 影响面：`main.py --serve` 无头引擎的收尾；G1 的 Flutter 壳（HTTP `/rpc` 那条路）

## 症状

客户端崩在 `engine.shutdown` 之前（Ctrl-C 之外的杀进程、蓝屏、Dart 侧 panic），
引擎进程不会自己走：端口一直 LISTENING，令牌一直有效，直到下一次起服撞端口或人工清理。

现场证据（本轮量到的，`Get-CimInstance` + `Get-NetTCPConnection`）：

```
ProcessId    : 74552
CreationDate : 2026/10/8 16:49:02
CommandLine  : …python.exe -u main.py --serve --port 0 --idle-seconds 5
LocalAddress : 127.0.0.1   LocalPort : 64966
```

那一发的命令行里写着 `--idle-seconds 5`，而它到 19:14 仍活着 = **2.42 小时**，
是那个倒计时的 1700 多倍。

## 根因

`app/services/gateway/service.py::_arm_idle_timer`(:631) 第一行就返回：

```python
if self._sessions_seen == 0 or self._idle_timer is not None:
    return
```

而 `self._sessions_seen` 只在一处自增：WS 会话建立（`_h_ws`:531）。它的两个调用点
也一样只在 WS 那条路上——:557（会话关掉）与 :626（往 WS 广播失败）。也就是说这套自杀
机制的"见过客户端"这件事**只有 WS 会话能证明**。

G0 写它的时候这没有错——`--serve` 的第一个驱动者是 curl 与 flet 侧的 WS 订阅者，那条
docstring 说的就是"纯 curl 试跑不该被一段倒计时打断"。但 G1 的竖切是**纯 HTTP `/rpc`**：
`RpcClient` 一次 WS 都不开（WS 事件面按设计是 G2 的活），于是 `_sessions_seen` 恒 0，
`_arm_idle_timer` 永远在第一行返回，`--idle-seconds` 是一个从没武装的旋钮。

顺带一条同源的：`EngineProcess.start`(:74) 给子进程固定传 `--idle-seconds 15`。这条兜底
在 RPC-only 路线上同样是空的——真正收尾的只有客户端主动发的 `engine.shutdown`
（`bootstrap.dart` 的 `onExitRequested`）。正常关窗有读数（本轮 3/3 臂 rc=0 + 端口拒绝），
不正常的那一类没有任何人兜。

## 为什么不顺手改掉

三种走法各有代价，而选哪个是产品语义的决定，不是机械修复：

1. **把 `_sessions_seen` 改成"见过任何客户端"**（HTTP `/rpc` 也算）：最贴 G1 的现实，
   但它把那条 docstring 的豁免取消了——人手 curl 试跑之后进程会在倒计时到点时自己没，
   这正是 G0 特意避免的表现。
2. **给 RPC 单独一条更长的计时**（比如最后一次 `/rpc` 之后 N 分钟）：保住 curl 的体验，
   代价是两个旋钮，且"N 分钟"这个数现在没有依据。
3. **心跳/租约**：客户端定期 `engine.lease` 续期，超时即退。最准，但要加 action、
   加 Dart 侧的定时器，是 G2 的量。

## 怎么验（改完之后）

现成的仪器都在：`.probe/g1_live_run.py` 起真子进程 → 真握手；`live_engine_test.dart`
第三条臂已经在比"退出码 0 + 端口被拒（带正对照）"。要补的是**不发 `engine.shutdown`
就直接把客户端杀掉**那一臂：断言 N 秒后端口自己放掉。现在这一臂写出来必红——
红得对，但要先定了语义再写，否则守卫钉的是还没选的走法。
