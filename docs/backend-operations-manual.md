# 构建与运维手册（backend-operations-manual）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 环境准备

```powershell
# conda 安装于 D:\env_manger\minicaoda
conda activate legodo_booksource_check
python -m pip install -r requirements.txt
python -m playwright install chromium    # 内置无头浏览器内核，须单独下载
```

## 2. 本地运行

```powershell
python main.py
```

## 3. 测试

```powershell
python -m pytest -q
```

## 4. 打包（待补充）

- Windows 打包：原 `build_windows.cmd` / `build_windows.ps1` 已删除，`LegadoReader.spec`（PyInstaller）仍在，打包步骤待补充。
- Android 打包：原 `build_android.cmd` 已删除，步骤待补充。
- 内置无头浏览器：Playwright 的 node 驱动需随包分发，Chromium 内核体积过大不入包。目标机未携带内核时 `available_fetcher()` 返回 `None`，程序照常运行，仅网页渲染阶段不可用；需要该能力时在目标机执行 `python -m playwright install chromium`。

## 5. 常见问题（待补充）

- 遇到问题先查 `docs/known-issues/README.md`。
- 网页阶段全部显示"需浏览器未抽检"：先确认 `%LOCALAPPDATA%\ms-playwright` 下有 `chromium-*` 目录，没有则执行 `python -m playwright install chromium`。
