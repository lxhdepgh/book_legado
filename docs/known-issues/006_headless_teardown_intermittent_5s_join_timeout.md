# 006 无头收尾偶发多花 5.00s：`EngineHub.shutdown` 的 join 走满

- 记录日期：2026-10-08
- 状态：**未修，归因未定**（本轮只做到"它不是 005 的残留、也不是这次改动带来的"）
- 影响面：`main.py --serve` 的进程消失时刻——空闲自杀与 `engine.shutdown` **两条路都中招**

## 症状

无头引擎收摊时，从"触发收摊"到"进程真没了"通常是 **0.05–0.08s**，但每隔若干发会出现
一次 **≈5.0s**。退出码始终是 0，端口始终放掉，功能上没有坏——只是父进程（Flutter 壳、
探针、测试）要多等 5 秒。

本轮全部读数（都是 rc=0）：

| 仪器 | 发数 | 走满 5s 的 | 慢的那些 |
| --- | --- | --- | --- |
| `tests/test_gateway_idle_exit_live.py`（空闲自杀，含 1.0s 窗口） | 10 | 2 | 6.08 / 6.08 s（扣窗口 = +5.00） |
| `.probe/idle_exit_teardown_probe.py` 第 1 次跑（三栏交替 ×5 轮） | 15 | 2 | 空闲栏 4.80s、shutdown 栏 5.08s，**同一轮** |
| 同探针第 2 次跑（三栏交替 ×8 轮） | 24 | 0 | — |
| 合计 | 49 | 4（≈8%） | |

## 已定的与未定的

**已定**：不是空闲自杀专属。探针第 1 次跑的第 3 轮里，"10 发后闭嘴"这一栏 4.80s、
"`engine.shutdown`"这一栏 5.08s，而夹在中间的"1 发后闭嘴"是 −0.23s（口径修正前的读数，
即正常）。同一次进程启动周期里两条**不同**收尾路径都中招，说明它长在共用的那一段上。

**已定**：出处是 `app/core/engine_worker.py:228`

```python
loop.call_soon_threadsafe(loop.stop)
if thread is not None and thread is not threading.current_thread():
    thread.join(timeout=5.0)
```

+5.00s 这个整数就是这里的 `timeout=5.0` 走满——loop 线程没在 5 秒内退出，主线程等满
超时后继续（`ServiceHub.shutdown` 的 finally 不 join 成功与否都往下走），进程随后正常
退出、rc=0。

**未定**：loop 线程为什么偶发退不出去。候选没有排过序，也没有仪器：
- 杀软/磁盘在那一刻插一脚（本机有过前科：全库联网扫描触发杀软拦截，且"吸洞是间歇的"）；
- loop 上还有在途任务或默认 executor 的线程没落地（`shutdown` 明确"不做任务级取消"）；
- QuickJS/规则引擎那一侧的原生收尾（`RuleEngineService 已停止` 是 stderr 的最后一行，
  5s 花在这行**之后**，也就是日志照不到的那一段）。

要归因得先有仪器：在那 5s 里打印 loop 线程的栈（`faulthandler.dump_traceback_later(2)`
或 `sys._current_frames()`），否则只是在三个候选里挑一个顺眼的。

## 为什么这一轮不顺手改

把 `timeout=5.0` 调小只是把等待换成"不等了"，loop 线程照样在跑，退出码照样 0——
症状消失、真因留下，而且会让"收尾没走干净"这一类故障从此静默。归因之前不动它。

## 怎么复现

```
PYTHONIOENCODING=utf-8 python .probe/idle_exit_teardown_probe.py
```

三栏交替（10 发后闭嘴 / 1 发后闭嘴 / `engine.shutdown`），每轮三次独立启动，
`ROUNDS` 默认 8。按 8% 的频率，一轮跑不出慢发是正常的——本轮 24 发全快。
要更快撞上就把 `ROUNDS` 调大，或者直接反复跑
`pytest tests/test_gateway_idle_exit_live.py -s`（9 发里撞过 2 次）。

## 关联

- [005](005_gateway_idle_exit_never_arms_for_rpc_only_clients.md)：本轮修的是"倒计时
  从没武装"，那条修完之后才**看得见**这条——之前 RPC-only 路线上根本不会自杀，
  也就没有"自杀要多久"这个读数。
- 004 的桌面收尾延迟（已修成 0.4s）是同一族现象、不同真凶：那一发的 5.4s 是
  tts-teardown 同步等 COM，钉在专属线程上就没了。别把两条当成一条。
