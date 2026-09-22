# 本地测试指南（local-testing-guide）

> 状态：骨架（初始化生成于 2026-09-01），待补充处需开发者确认后补全。

## 1. 单元测试

```powershell
conda activate legodo_booksource_check
python -m pytest -q                 # 全量
python -m pytest tests/test_legado_compat.py -q   # 单文件
```

- `pytest.ini` 已配置 `norecursedirs = base_history`，参考源码目录不参与收集。

## 1.1 内置无头浏览器（Playwright，仅桌面）

`{"webView":true}` 与 `ruleContent.webJs` 只能由真实浏览器渲染。桌面端两种情形分开走：

| 情形 | 机制 | 平台 |
|---|---|---|
| 无人值守渲染 | 内置无头 Chromium（Playwright） | Windows / Linux / macOS |
| 需要人处理（登录、验证码、质询页） | 应用窗口内的 `ftw.WebView` | Android / iOS / macOS |
| 需要人处理（同上） | 投屏进应用窗口的内置 Chromium（CDP screencast） | Windows / Linux |

```powershell
conda activate legodo_booksource_check
pip install -r requirements-desktop.txt   # 桌面端；只装 requirements.txt 会没有内置浏览器
python -m playwright install chromium     # 内核不入库，须单独下载
```

- 依赖分两份：`requirements.txt` 全平台通用（Android/iOS 打包只装这份），`requirements-desktop.txt` 在其上追加 `playwright`。Android 上用系统 WebView，不需要也不支持 Playwright。
- 内核位置：`%LOCALAPPDATA%\ms-playwright`（可用 `PLAYWRIGHT_BROWSERS_PATH` 覆盖）。
- 用户配置目录：缓存目录下的 `chromium-profile/`，登录态和验证结果跨启动保留。
- 内核缺失时 `available_fetcher()` 返回 `None`，检测与阅读都退回到原有行为（网页阶段记为"需浏览器未抽检"），不会报错。
- 投屏窗口用独立配置目录 `chromium-interactive-profile/`（Chromium 的 profile 被持有它的进程锁定，不能与无头共用）。人在投屏里通过验证后，Cookie 会转交无头配置，之后的静默渲染不再重复要验证。
- 投屏只支持点击、右键、滚轮、拖拽和键盘输入这几种操作，滑块类验证码靠拖拽完成；窗口本身停在桌面可视区域外，不会抢占前台。
- `tests/test_headless_browser.py` 的 `_RealRenderTests` 与 `_PipelineWiringTests` 需要内核，未安装时自动跳过；其余用例用假页面驱动，不联网、不启动浏览器。
- `tests/test_interactive_browser.py` 的 `_RealCastTests` 同样需要内核：它用 `data:` 页面验证投屏能出帧、点击能落到页面上。

## 2. 测试目录说明（待补充各文件覆盖范围）

- `tests/`：单元 / 集成测试（书源缓存、兼容性、阅读器体验、进度持久化、主题、UI 安全等）
- `tests/test_headless_browser.py`：内置浏览器（BackstageWebView 语义、等待脚本轮询、嗅探模式）
- `test_book_sources_async.py`、`test_reading_chains_async.py`：根目录的异步书源 / 阅读链路测试
- `tools/`：Java Oracle 对比工具（jsoup / rhino / legado），用于与原版行为对齐

## 3. 书源业务测试

- 见根目录 `书源测试方法说明.md`（两层测试：连通性 + 搜索可用性）。

## 4. 改动后验证清单

- 语法：`python -m compileall -q app main.py`
- 测试：`python -m pytest -q`
- lint：暂不配置
