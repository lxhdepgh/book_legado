# Git 工作流（git-workflow）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 分支策略（待补充）

- 当前仅 `master` 分支；是否需要功能分支 / PR 流程待补充。

## 2. 提交信息规范（待补充）

- 历史提交采用 `type: 描述` 风格（如 `fix:`、`docs:`、`build:`），是否强制 Conventional Commits 待补充。

## 3. 智能体约束

- 智能体不得擅自提交、不得 `checkout` / `reset` / `stash` 开发者未提交的改动。
- 提交前需开发者明确指示。

## 4. 忽略策略

- 见 `.gitignore`；`base_history/`、`other_history/`（第三方参考源码）的忽略策略待补充。
