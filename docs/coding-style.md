# 编码规范（coding-style）

> 状态：2026-10-20 按现主线（`legodo_dart` Flutter + `backend_go` Go，单进程 FFI）校正。
> 原骨架生成于 2026-09-01，当时只有 Python/Flet 一门语言；旧自检命令 `python -m compileall -q app main.py`
> 指向的根 `app/`、`main.py` 已删除，作废。

## 1. 语言约定

- 注释、docstring、面向用户的报错文案：**中文**
- 标识符（变量 / 函数 / 类）：**英文**
- 注释只在"为什么"不自明时写（隐藏的约束、为某个 bug 做的 workaround）；不复述代码在做什么。

## 2. 格式与检查

改动前后要跑的门，按改的是哪一侧：

| 侧 | 命令 | 落点 |
|---|---|---|
| Go 引擎 | `go vet ./...` + `go test ./... -count=1` | `legodo_dart/backend_go/`（cgo 需 `C:\mingw64\bin` 在 PATH 前缀里，否则报 exit 2 且不吐错误） |
| Dart | `dart analyze`（不许新增 error/warning）+ `flutter test` | `legodo_dart/` |
| Dart 排版 | `dart format` **只对本轮新写的文件** | 既有文件不跑：本机 SDK 3.13.2 是 tall-style，`lib`+`test` 里 77/102 个文件会被改，整仓归一化是独立一轮的事（口径原文见 `docs/dart补缺方案.md` §9 第 7 步） |
| Go 排版 | `gofmt` | 改到的文件；注意 gofmt 对齐会顺手动到相邻行，diff 里别当成行为改动 |
| Python（仅 `legodo_flet` 参考实现） | `python -m pytest -q --basetemp=<新目录>`（根 `tests/`） | 默认 basetemp 有 ACL 拒绝，会整批假红 |

改到 Go 动作面的，还要重编 DLL 并同步 `legodo_dart/dist/legodo_windows/`（用 `cmp` 核逐字节一致）。

- lint：暂不配置（ruff 未安装）。
- 行宽 / 缩进：Python 遵循 PEP 8（4 空格）；Dart/Go 交给上面的格式化器。

## 3. 日志（待补充）

- 待补充：日志框架与级别约定（print 是否允许、敏感信息脱敏要求）

## 4. 异常处理

- 用户提示文案走中文；协议面（`Call` 回包）的错误形状与 action 契约由 `backend_go/pkg/ffi/route_test.go`
  那张表钉，改发送面必须整表机械重提取，不许手工补条。
- 旧的 `app/ui_feedback.py` 参考点已随根 `app/` 删除，待补充：Dart 侧对应的提示落点。

## 5. 测试约定

- 三门各自就近：Go 护栏跟包（`*_test.go`），Dart 护栏跟 `legodo_dart/test/`，
  flet 侧 Python 契约在根 `tests/`（80 个文件，面向 `legodo_flet/app/`）。
- 新增逻辑要带护栏；替身必须是子类、不许多字段，护栏要钉"没发生"而不是"失败了"。
