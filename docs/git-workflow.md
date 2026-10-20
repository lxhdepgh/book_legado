# Git 工作流（git-workflow）

> 状态：2026-10-20 校正（原骨架 2026-09-01）。

## 1. 分支策略

- 单主干：本地 `main` + `remotes/origin/main`。历史上那句"当前仅 `master` 分支"已过期。
- 是否需要功能分支 / PR 流程：待补充。

## 2. 提交信息规范（待补充）

- 历史提交采用 `type: 描述` 风格（如 `fix:`、`docs:`、`perf:`、`chore:`），是否强制 Conventional Commits 待补充。

## 3. 智能体约束

- 智能体不得擅自提交、不得 `checkout` / `reset` / `stash` 开发者未提交的改动。
- 提交前需开发者明确指示。
- 提交只含本次任务的文件：`git mv` / `git rm` 会先把改名写进暂存区，容易被卷进下一发无关提交；
  文档归档这类批量改名默认走文件系统 `mv`，由开发者单独一次提交。

## 4. 忽略策略

- 见 `.gitignore`。
- `other_history/`（第三方参考源码）已列入忽略；`base_history/` 目录已不存在，那一行只是遗留措辞。
- `reference_code/` 下的具体克隆仓库逐个列在忽略表里，不是整目录忽略。
