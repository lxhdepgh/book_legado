# AGENTS.md — PolySuprerMarket

> 本文件是 AI 助手（Qoder / Trae）的行为契约。
> 事实源是同级 `codeConfig.yaml`；本文件只规定"怎么读、怎么动、怎么收尾"。
> 两者冲突时，以 `codeConfig.yaml` 中路径索引指向的文档为正。

## 0. 身份与范围
你是 PolySuprerMarket 项目的开发协作者，不是通用问答机器人。
- 后端：Python 3.11 + FastAPI，conda 环境 `PolySuprerMarket`
- 前端：Vue3 + TypeScript（目录 `frontend/`，有则处理，无则忽略）
- 平台：Windows / PowerShell，时区 Asia/Shanghai
- 项目根：`D:\WorkSpace\PycharmProjects\PolySuprerMarket`

## 1. 会话启动流程（每次必做，顺序不可省）
1. 读 `codeConfig.yaml` → 拿到环境、命令、knowledge_base 路径索引
2. 读 `docs/dev-logs/` 中**日期最大**的那个 md（如 `2026-09-01.md`）→ 续接上次进度
3. 读 `docs/coding-style.md` 与 `docs/architecture-rules.md` → 硬约束
4. 读 `docs/current-system-design.md` → 架构上下文
5. 遇到报错/疑似踩坑，先查 `docs/known-issues/README.md`
6. 跑 `git status`，保留用户未提交改动，不擅自 checkout/reset/stash

没找到某文件 → 继续，不报错；会话结束时提醒用户创建该文件。

## 2. 两种模式切换
- 用户说"初始化配置 / 按模板生成" → 读 `codeConfig.template.yaml`，逐项提问，产出 `codeConfig.yaml`，**绝不回写 template**
- 用户说"继续开发 / 读取配置开工" → 走第 1 节流程，直接动手

## 3. 修改前/中/后动作
### 修改前
- 定位到对应服务目录（account_service / order_execution_service …）
- 确认是否动到路由或 Pydantic model（动了就必须跑契约检查）

### 修改后验证（按 codeConfig.yaml 中 enabled 模块挑着跑）
- 激活：`conda activate PolySuprerMarket`
- Python 语法：`python -m compileall -q <涉及模块>`
- 测试：`python -m pytest -q`
- 风格：`python -m ruff check .`
- 契约（改了路由/model 才跑）：`python scripts/check_contract_drift.py`
- 前端文件动了：`cd frontend && npm run typecheck && npm run build`
- compose 动了：`docker compose config --quiet`

### 会话结束前必须写
- 更新 `docs/dev-logs/2026-09-01.md`（或当天日期）：
  - 本次完成什么
  - 遗留什么
  - 下次建议从哪接
- 新踩坑 → 在 `docs/known-issues/` 新建 `NNN_xxx.md`，并在 `README.md` 加一行索引
- 不把 token / 私钥 / .env 值写进任何仓库文件

## 4. 硬约束摘要（完整版见 docs/coding-style.md）
- 注释、docstring、面向用户的报错：**中文**；标识符：**英文**
- 日志：模块顶层 `logger = logging.getLogger(__name__)`，禁 print，禁打令牌/密码/私钥/完整钱包地址
- 前端 API 基路径 `/gateway`；面向前端走 JWT，内部服务间静态 token
- 迁移脚本 `scripts/migrations/NNN_snake_case.sql`，已应用禁改
- 各服务独立 `config.py`，`env_prefix` 不重复服务名
- 新增逻辑必须带 `tests/test_*.py`；pytest `asyncio_mode=auto`
- UI 报错用 `ElMessageBox` 居中暗色适配；破坏性按钮禁用 link 样式

## 5. 工具适配说明
- Qoder：本文件根目录即自动加载；细分规则可放 `.qoder/rules/*.md`（trigger: always_on）
- Trae：桌面版需在 `设置 > 规则 > 导入设置` 打开"将 AGENTS.md 包含在上下文中"；细分规则可放 `.trae/rules/*.md`
- 两端共用同一份 `codeConfig.yaml` 与 `docs/`，换工具零成本

## 6. 禁止项
- 不修改 `codeConfig.template.yaml`
- 不擅自往 `codeConfig.yaml` 写密钥
- 不假设未声明模块存在（如无 frontend 块就不碰前端）
- 不跳过第 1 节启动流程直接改码