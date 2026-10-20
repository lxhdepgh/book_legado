# 本地测试指南（local-testing-guide）

> 状态：2026-10-20 重写。原骨架（2026-09-01）通篇只有 Python 一门口径，且它引用的
> 根 `app/`、`main.py`、`requirements*.txt`、`tools/`、`书源测试方法说明.md`
> **都不在仓库根了**（前两样已删除，后三样要么搬进 `legodo_flet/` 要么整目录移除）。
> 现主线是 Flutter + Go 单进程 FFI，所以守口有四门，只有最后一门是 Python。

---

## 0. 四道门

| 门 | 命令 | 跑在哪 | 现状（当场数，2026-10-20） |
|---|---|---|---|
| **Go 引擎** | `go vet ./...`<br>`go test ./... -count=1` | `legodo_dart/backend_go/` | `go list ./...` = 10 包，其中 **6 包含 `*_test.go`**（core 2 / internal/engine 2 / internal/services 18 / pkg/ffi 12 / pkg/jsruntime 1 / pkg/ruleparser 3） |
| **协议面（发送清单）** | `go test ./pkg/ffi -run Route -count=1` | 同上 | 钉在 `pkg/ffi/route_test.go:141`：同步 88 / 直连 1 / 合计 89；提取器现读 Dart 侧 |
| **Dart** | `dart analyze`<br>`flutter test` | `legodo_dart/` | `test/` 36 个文件；3 条活臂要真 DLL（见 §3） |
| **Python 遗留（flet 契约）** | `python -m pytest -q --basetemp=<新目录>` | 仓库根 | 根 `tests/` 80 个 `test_*.py` / 1547 个 `def test_`，其中 **77 个 import `app.*`** → 面向 `legodo_flet/app/` |

改哪一侧就跑哪一侧的门；改到 Go 动作面的，四门里前两门 + Dart 全量都要跑（接线断了只有界面那门看得见）。

## 1. 环境

- **Python**：项目 conda 环境 `legodo_booksource_check`（解释器 `D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe`，
  见 `codeConfig.yaml`）。不要用系统 Python 跑测试。
- **Go + cgo**：编 `c-shared` 需要 MinGW。**会话 PATH 必须前置 `C:\mingw64\bin`**，
  否则 `go build` 只报 `exit status 2` 而不吐编译错误（`cc1.exe` 找不到，`gcc --version` 却照样正常）。
  引擎单编：`go build -buildmode=c-shared -o legodo_engine.dll ./pkg/ffi/`。
- **出包**：`cd legodo_dart && python build_release.py windows`（参数 `--skip-go` / `--skip-flutter`；
  其他目标 `android` / `linux` / `macos`）。构建权威页是 `legodo_dart/README_BUILD.md`。
- **Android** 需要系统级 `JAVA_HOME` 指向有效 JDK（Flutter 读的是系统变量，会话内 inline 无效）。

## 2. 一轮改动的验证顺序

照 `docs/dart补缺方案.md` §9 第 7 步：

1. `dart analyze` —— 不许新增 error / warning；
2. `flutter test` 全量；
3. `dart format` **只对本轮新写的文件**（既有文件别碰：本机 SDK 3.13.2 是 tall-style，
   `lib`+`test` 里 77/102 个文件会被重排，9 行改动能炸成 1099 行排版噪声）；
4. 改到 Go 的：`go vet ./...` + `go test ./...`，重编 DLL 并同步 `legodo_dart/dist/legodo_windows/`，
   用 `cmp` 核逐字节一致；
5. 动过 action 清单的：`go test ./pkg/ffi -run Route` 必须整表重提取，不许手工补条。

## 3. 静态全绿 ≠ 链路通：活臂

引擎跑在真 DLL 里时才有的一些格子（形状解码、真库投影、越界字符串）静态测试照不出来。
现有三条活臂要真 DLL，经 `LEGODO_ENGINE_DLL` 指过去（`lib/core/ffi/engine_ffi.dart:257`；
`flutter test` 的宿主是 `flutter_tester`，DLL 不在它旁边）：

- `test/live_engine_test.dart`
- `test/reader_live_arm_test.dart`
- `test/webview_reverse_channel_live_arm_test.dart`

跑法（本轮实际用过的那条）：`python .probe/ffi_live_run.py --skip-build live_engine`。
另外 flet↔Dart 的投影对照脚本 `.probe/g1_flet_projection.py` 支持
`--data <现成的语料目录> --out <落点>`——**对照靶子必须现场重导，不许拿仓库里冻结的快照比**
（冻结靶子跟现读真库必假红）。

## 4. Windows 仪器坑（会造出假绿/假红，先排这些再怀疑代码）

| 症状 | 真因 | 处置 |
|---|---|---|
| 全量 pytest 52 条 `PermissionError` | 默认 `basetemp` 目录 ACL 拒绝 | 加 `--basetemp=<新目录>`，不是回归 |
| 全量跑紧跟探针后随机报 `WinError 10055` | 一次全量留约 1100 个 `TIME_WAIT`，端口耗尽 | 先 `netstat` 数 `TIME_WAIT` 再归因 |
| 一批 flet 红线"通过"得异常快（如 `2 passed in 0.02s`） | 护栏读的是**已删除的根 `app/`**，`rglob` 返回空集 | 见 `docs/known-issues/008_...md`——扫到 0 个对象必须判红 |
| 顺序相关的 26 条超时红，卡在"等不到首帧" | 测试里真调了 `hub.shutdown()`，它关的是进程级单例的 loop | 测试不许真调 shutdown |
| 回环复现不了 socket 收尾的 `10054` | 频率是现场量，后果可离线注入"已经抛"来测 | 别指望 127.0.0.1 能复现 |
| 界面探针等不到语义树 | 语义树分帧下发，要等稳定帧 | 见下节 flet 侧口径 |

## 5. flet 侧（参考实现）怎么测

`legodo_flet/` 是 parity 参考，不再演进；它的依赖与内核仍在：

- 依赖两份：`legodo_flet/requirements.txt`（全平台）、`legodo_flet/requirements-desktop.txt`
  （追加 `playwright>=1.49,<2`）。只装前者会没有内置浏览器。
- Chromium 内核不入库：`python -m playwright install chromium`，落 `%LOCALAPPDATA%\ms-playwright`
  （可用 `PLAYWRIGHT_BROWSERS_PATH` 覆盖）。
- `tests/test_headless_browser.py` 的 `_RealRenderTests` / `_PipelineWiringTests`、
  `tests/test_interactive_browser.py` 的 `_RealCastTests` 需要内核，缺时**自动跳过**，不算失败；
  其余用例用假页面驱动，不联网、不起浏览器。
- 界面验证走浏览器探针：`WEB_BROWSER` + 假书源 + Playwright 点语义树
  （语义树分帧要等稳定；下拉只能按 `aria-expanded` 认）。
- 跑 Python 测试统一加 `--basetemp`，见 §4。

## 6. 报数的规矩

写进日志、验收表或设计文档的每个数字都要带口径：**哪条命令、当场还是引用、跑了几次、有没有换过等待条件**。
当场测不出来的数字就删掉那一句，不留旧值、不留估计值。
（这条同时是 `architecture-rules.md` 的 E4。）
