# 构建与运维手册（backend-operations-manual）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 环境准备

```powershell
# conda 安装于 D:\env_manger\minicaoda
conda activate legodo_booksource_check
python -m pip install -r requirements.txt
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

## 5. 常见问题（待补充）

- 遇到问题先查 `docs/known-issues/README.md`。
