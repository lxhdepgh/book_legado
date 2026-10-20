# 密钥管理策略（secrets-policy）

> 状态：2026-10-20 按现主线校正（原骨架 2026-09-01）。旧文里那句"参见 `app/services/app_paths.py`"
> 指的根 `app/` 已删除——Python 侧现在有两棵同名树（`legodo_flet/app/` 与参考实现 `legodo_dart/app/`）。

## 1. 硬性规定

- **严禁**将令牌、密码、私钥、Cookie、已填充的 `.env` 值写入仓库任何文件（含 `codeConfig.yaml`、文档、日志）。
- 智能体不得在代码或文档中回填任何真实凭证。
- 文档、测试夹具、日志样本里不许出现真实凭据字面量：写入通道会把可疑值改成 `***` 并吞掉引号，
  **落盘即坏语法**。需要凭据形状时用一眼假占位，运行时注入。
- 书源配置本身常携带 `header` 里的 token 与 `bookSourceRule` 里的账号串——贴语料前先脱敏。

## 2. 凭证存放位置

- 运行时入口（Go）：WebDAV 账号密码经 `internal/services/webdav_service.go:58` 的 `SetConfig` 注入，
  请求时 `:113` 组 Basic Auth；不落明文日志。
- 落盘位置由存储目录决定，与仓库隔离：`internal/services/register.go:26-45` 的 `defaultDataDir()` —
  Windows `%LOCALAPPDATA%\LegadoBookSourceCheck`、macOS `~/Library/Application Support/LegadoBookSourceCheck`、
  Linux `$XDG_DATA_HOME`（缺省 `~/.local/share`）。主库 `legado.db` 就在这一目录（`core/storage.go:40`）。
- `.env` / 系统密钥环：未使用（待补充：若引入需先规定谁来读）。

## 3. 日志脱敏（待补充）

- 待补充：请求头 / 书源配置中敏感字段的打印规则。Go 侧 webbridge 与 HTTP 日志的脱敏尚未回写成文，
  现状是"不许把现场日志整段贴进文档"这一条硬规矩兜底。

## 4. 违规处置

- 发现疑似凭证入库：立即提醒开发者，建议 `git rm --cached` 并轮换凭证。
- 已推送到远端的凭证不算"删掉就好"——必须轮换。
