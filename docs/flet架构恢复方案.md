# Flet 架构恢复方案

## 1. 背景与决策依据

### 1.1 迁移历史回顾

项目于 **2026-10-08** 开始从纯 Flet 架构向 Flutter 迁移（G1-G4 路线），历时三轮完成：

| 阶段 | 日期 | 内容 | 判据 |
|------|------|------|------|
| G0 | 10-08 | GatewayService + action 表 + --serve 无头入口 | 跨进程 RPC p50 80.6ms |
| G1 | 10-08 | Flutter 壳接真引擎，bookshelf 一屏与 flet 投影比对 | 书架 7 行 / 25,012B 逐行对上 |
| G2 | 10-09 | 找书/书源/检测/外观四屏 Dart 化 | source.list 从 6.97MB 收成 351KB |
| G3 | 10-09 | 阅读屏 + 换源 + WebView 反向通道 | 落点字符偏移三头同数 |
| G4 | 10-10 | RSS/漫画/朗读/工具/老板键五族 Dart 化 | 服务层脱离 flet import |

**最后纯 Flet 版本**：commit `f867150`（2026-10-08 16:31）

### 1.2 为何恢复 Flet

当前 Flutter Windows 桌面版本已验证可行（230MB 成品包），但存在以下场景需要 Flet：

1. **Android 打包困境**：Chaquopy 12.0.0 (2022) 与 AGP 8.x 不兼容且已停更，Flutter APK 无法内嵌 Python 后端
2. **Flet 1.0 原生支持 Android**：`flet build apk` 可自动捆绑 Python 解释器，无需额外插件
3. **UI 效果等价性**：Flet 底层使用 Flutter 渲染引擎，Material/Cupertino 控件表现一致
4. **代码复用率**：91 个服务文件零 Flet 依赖，后端逻辑完全通用

### 1.3 技术可行性

| 维度 | Flet 方案 | Flutter 方案 |
|------|----------|-------------|
| **Windows 打包** | PyInstaller → exe (21MB) + Flet runtime | Flutter build windows → exe (89KB) + flutter_windows.dll (21MB) |
| **Android 打包** | `flet build apk` → 单 APK（含 Python） | 需分离架构或放弃 Python |
| **iOS 支持** | ❌ Python 不能进 App Store | ✅ 纯 Dart 实现 |
| **UI 控件** | ft.Icon/ft.Text/ft.ListView 等 200+ 控件 | Material/Cupertino widgets |
| **性能差异** | 首屏 120+10+561ms（装配/show/行落满） | 冷启动 ~600ms（握手）+ UI 渲染 |
| **开发语言** | Python 全栈 | Dart 前端 + Python 后端 |
| **代码量** | app/views/*.py + app/controllers/*.py ≈ 15,407 行 | legodo_dart/lib/**/*.dart ≈ 12,000 行 |

---

## 2. 恢复目标

### 2.1 核心原则

- **服务层不变**：91 个服务文件、32 族服务注册、GatewayService action 表保持现状
- **UI 层替换**：用 Flet views/controllers 替代 Dart screens/providers
- **协议面保留**：POST /rpc + GET /ws 环回通信机制继续可用（供未来混合架构）
- **双轨并存**：legodo_flet/ 与 legodo_dart/ 共存，用户可选启动方式

### 2.2 功能清单

从 commit `f867150` 恢复的完整功能模块：

#### 导航结构（app/views/navigation.py）
- 左侧导航栏：书架 / 书源 / 找书 / 阅读 / 工具 / 设置 六大入口
- 组内页签切换（同类功能合并成 Tab，切签不退订）

#### 书架模块
- `app/views/reading/bookshelf_view.py`：书架列表视图
- `app/controllers/bookshelf_controller.py`：书架数据管理

#### 书源管理
- `app/views/source/book_source_list_view.py`：书源列表
- `app/views/source/code_editor_view.py`：规则编辑器
- `app/views/source/check_view.py`：书源检测面板
- `app/views/source/rule_repair_view.py`：规则修复向导
- `app/views/source/import_view.py`：书源导入

#### 找书与搜索
- `app/views/reading/find_books_view.py`：多源搜书界面
- `app/controllers/find_books_controller.py`：搜书调度

#### 阅读器
- `app/views/reading/reader_view.py`：正文阅读视图（含 <usehtml>/<md>/<useweb> 渲染契约）
- `app/views/reading/manga_reader_view.py`：漫画阅读器
- `app/views/reading/rss_view.py`：RSS 订阅浏览
- `app/views/reading/read_aloud_bar.py`：朗读控制条
- `app/views/reading/bookmark_panel.py`：书签面板
- `app/views/reading/content_search_bar.py`：全文搜索
- `app/views/reading/source_switch_dialog.py`：换源对话框
- `app/views/reading/video_play_bar.py`：视频播放条

#### 工具域（8 族工具）
- `app/views/tools/browser_view.py`：浏览器探针
- `app/views/tools/dict_popup.py`：词典弹窗
- `app/views/tools/export_view.py`：导出工具
- `app/views/tools/font_view.py`：字体分析
- `app/views/tools/overview_view.py`：工具概览
- `app/views/tools/qr_code_view.py`：二维码识别
- `app/views/tools/replace_rule_view.py`：替换规则
- `app/views/tools/tts_view.py`：TTS 朗读
- `app/views/tools/update_view.py`：更新检查
- `app/views/tools/web_bridge_view.py`：Web 桥接
- `app/views/tools/webdav_view.py`：WebDAV 同步

#### 设置与外观
- `app/views/settings/appearance_view.py`：主题/字体/背景图设置
- `app/views/settings/shortcut_view.py`：快捷键配置
- `app/views/service_dashboard.py`：服务状态监控

---

## 3. 实施步骤

### 3.1 从 git 历史提取 Flet 文件

```bash
# 创建 legodo_flet 目录
mkdir -p legodo_flet/app

# 从 f867150 提取所有 Flet UI 文件
git checkout f867150 -- app/views/ app/controllers/ main.py

# 移动到 legodo_flet 目录
mv app/views app/controllers main.py legodo_flet/

# 恢复原始 app/ 目录（服务层）
git restore app/
```

### 3.2 调整目录结构

```
legodo_flet/
├── main.py                    # Flet 应用入口
├── app/
│   ├── controllers/           # 控制器层（事件处理）
│   │   ├── base.py
│   │   ├── bookshelf_controller.py
│   │   ├── find_books_controller.py
│   │   ├── reader_controller.py
│   │   ├── rss_controller.py
│   │   ├── manga_reader_controller.py
│   │   ├── settings/
│   │   │   ├── appearance_controller.py
│   │   │   └── shortcut_controller.py
│   │   ├── source/
│   │   │   ├── book_source_list_controller.py
│   │   │   ├── check_controller.py
│   │   │   ├── code_editor_controller.py
│   │   │   ├── rule_repair_controller.py
│   │   │   └── source_import_controller.py
│   │   └── tools/
│   │       ├── browser_controller.py
│   │       ├── dict_popup_controller.py
│   │       ├── export_controller.py
│   │       ├── font_controller.py
│   │       ├── qr_code_controller.py
│   │       ├── replace_rule_controller.py
│   │       ├── tts_controller.py
│   │       ├── update_controller.py
│   │       ├── web_bridge_controller.py
│   │       └── webdav_controller.py
│   └── views/                 # 视图层（UI 组件）
│       ├── navigation.py
│       ├── service_dashboard.py
│       ├── reading/
│       │   ├── bookshelf_view.py
│       │   ├── find_books_view.py
│       │   ├── reader_view.py
│       │   ├── manga_reader_view.py
│       │   ├── rss_view.py
│       │   ├── read_aloud_bar.py
│       │   ├── bookmark_panel.py
│       │   ├── content_search_bar.py
│       │   ├── source_switch_dialog.py
│       │   └── video_play_bar.py
│       ├── source/
│       │   ├── book_source_list_view.py
│       │   ├── code_editor_view.py
│       │   ├── check_view.py
│       │   ├── rule_repair_view.py
│       │   └── import_view.py
│       ├── settings/
│       │   ├── appearance_view.py
│       │   └── shortcut_view.py
│       └── tools/
│           ├── browser_view.py
│           ├── dict_popup.py
│           ├── export_view.py
│           ├── font_view.py
│           ├── overview_view.py
│           ├── qr_code_view.py
│           ├── replace_rule_view.py
│           ├── tts_view.py
│           ├── update_view.py
│           ├── web_bridge_view.py
│           └── webdav_view.py
├── requirements.txt           # Flet 依赖（标记为可选）
├── LegadoReader.spec          # PyInstaller 配置
└── README.md                  # Flet 架构说明
```

### 3.3 修改 main.py 适配新架构

关键改动点：

1. **移除 ServiceHub 装配逻辑**（服务层已在 `app/services/` 独立运行）
2. **添加 --serve 模式检测**：若检测到后端已在运行，直接连接环回 HTTP
3. **保留平台探测**：`app/platform_support.py` 已改为字符串比较，不再 import flet
4. **启动方式**：
   ```bash
   # 独立模式（Flet 自带后端）
   python legodo_flet/main.py
   
   # 环回模式（连接外部 Python 后端）
   python legodo_flet/main.py --connect http://127.0.0.1:PORT --token TOKEN
   ```

### 3.4 依赖管理

`legodo_flet/requirements.txt`：

```txt
# ========== UI 模式（Flet 前端）==========
# 若要启用 Flet UI，取消下方注释并运行 pip install -r requirements.txt
# G4-F2 决策：flet 依赖标记为可选，等待 Android 打包需求明确后再决定是否真正删除
flet>=0.86.5
flet-desktop>=0.86.5

# ========== 核心引擎依赖（必须）==========
aiohttp>=3.9.0
beautifulsoup4>=4.12.0
charset-normalizer>=3.0.0
html5lib>=1.1
elementpath>=5.0.0
jsonpath-ng>=1.7.0
opencc-python-reimplemented>=0.1.7
regex>=2024.0.0
fonttools>=4.55
pillow>=10.4
pycryptodome>=3.20
quickjs>=1.19.4
py7zr>=0.22
rarfile>=4.2
EbookLib>=0.18
pymupdf>=1.24
feedparser>=6.0.10
pyttsx3>=2.90
edge-tts>=6.1
qrcode>=7.4
opencv-python-headless>=4.8
playwright>=1.40.0  # 浏览器探针（可选）
```

### 3.5 PyInstaller 配置

`legodo_flet/LegadoReader.spec` 关键段：

```python
# hiddenimports 需包含：
hiddenimports=[
    # Flet 核心
    'flet',
    'flet_cli',
    'flet_core',
    
    # 服务层（91 个服务文件）
    'app.services.bookshelf_service',
    'app.services.source_service',
    # ... 其他服务
    
    # GatewayService
    'app.gateway.service',
    'app.gateway.schema',
    
    # 引擎核心
    'app.services.engine.legado_js',
    'app.services.engine.public_suffix',
]

# datas 需包含 schema JSON 文件
datas=[
    ('app/gateway/schema/*.json', 'app/gateway/schema'),
    ('app/services/engine/public_suffix.dat', 'app/services/engine'),
]
```

### 3.6 Android 打包流程

```bash
# 1. 安装 Flet CLI
pip install flet

# 2. 构建 APK（自动捆绑 Python 解释器）
cd legodo_flet
flet build apk --release

# 输出位置：build/app/outputs/flutter-apk/app-release.apk
```

**注意**：Flet 1.0 的 `flet build apk` 会自动：
- 下载对应平台的 Python 解释器（~10MB）
- 将 Python 代码编译为 .pyc
- 打包所有 pip 依赖
- 生成单个 APK 文件（预计 80-120MB）

---

## 4. 风险与应对

### 4.1 已知风险

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| Flet 1.0 API 变更 | 中 | 部分控件参数不兼容 | 对照 f867150 版本的 Flet 0.86.5 API 文档逐项核对 |
| PyInstaller 打包体积过大 | 高 | APK > 150MB | 启用 UPX 压缩、剔除未使用模块 |
| 服务层与 UI 循环耦合 | 低 | EngineHub 专属循环冲突 | 已在 G4-F1 解耦，platform_support.py 不再 import flet |
| 事件流订阅丢失 | 中 | 进度反馈失效 | 复用 G3-D3 的 job.* 异步句柄 + WebSocket 事件 |

### 4.2 回退方案

若 Flet 恢复失败，保留以下选项：

1. **维持 Flutter Windows 版本**：当前 230MB 成品包功能完整，可作为桌面端主力
2. **Android 分离架构**：Flutter APK + Termux Python 后端（通过 localhost 通信）
3. **Go/Java 重写引擎**：评估工作量后决定是否投入（见下文 §5）

---

## 5. Go/Java 重写核心规则引擎工作量评估

### 5.1 当前 Python 引擎复杂度

| 组件 | 文件数 | 代码行数 | 核心功能 |
|------|--------|---------|---------|
| `app/services/engine/` | 8 | ~3,200 | Legado JS 规则执行、QuickJS 集成、公共后缀库 |
| `app/services/legado_parser.py` | 1 | ~1,800 | 规则解析、XPath/CSS/JSONPath 转换 |
| `app/services/engine_worker.py` | 1 | ~900 | 引擎专属循环、任务派发、超时控制 |
| `app/core/events.py` | 1 | ~400 | 22 个 frozen dataclass 事件定义 |
| **合计** | **11** | **~6,300** | |

**关键技术点**：
- QuickJS 原生绑定（`quickjs` PyPI 包，C 扩展）
- BeautifulSoup4 HTML 解析（`bs4` + `html5lib`）
- 正则表达式引擎（Python `re` + `regex` 模块）
- XPath 支持（`lxml` + `elementpath`）
- JSONPath（`jsonpath-ng`）

### 5.2 Go 实现方案

#### 优势
- **原生并发**：goroutine 天然适合引擎专属循环模型
- **单二进制**：`go build` 直接出 exe，无需 PyInstaller
- **Android NDK 友好**：可编译为 `.so` 库嵌入 APK
- **性能**：预期比 Python + QuickJS 快 2-3×（无 GIL、零 GC 停顿）

#### 劣势
- **生态缺失**：无成熟的 JS 引擎绑定（需集成 goja 或 otto）
- **HTML 解析弱**：`goquery` 仅支持 CSS 选择器，XPath 需额外库
- **开发成本**：重写 6,300 行 Python + 适配 32 族服务接口

#### 工作量估算

| 阶段 | 任务 | 预估工时 |
|------|------|---------|
| 1 | 调研 goja/otto JS 引擎，对比 QuickJS API | 2 天 |
| 2 | 重写 LegadoParser（XPath/CSS/JSONPath 转换） | 5 天 |
| 3 | 集成 goja + htmlquery + jsonpath，实现规则执行器 | 7 天 |
| 4 | 实现 EngineHub 专属循环 + 任务派发 + 超时控制 | 3 天 |
| 5 | 适配 32 族服务接口（JSON Schema 校验） | 4 天 |
| 6 | 单元测试 + 黄金样本比对（对齐 Python 版） | 3 天 |
| 7 | 性能基准测试 + 优化 | 2 天 |
| **合计** | | **26 天（约 1.5 个月）** |

**参考项目**：
- [guoapp1](https://github.com/guofei9987/guoapp1)：Go 实现的 Legado 规则引擎（已验证可行）
- [goja](https://github.com/dop251/goja)：纯 Go JS 引擎
- [goquery](https://github.com/PuerkitoBio/goquery)：jQuery-like HTML 解析

### 5.3 Java 实现方案

#### 优势
- **Android 原生**：可直接嵌入 APK，无需 NDK
- **生态成熟**：Rhino/Nashorn JS 引擎、Jsoup HTML 解析、Jackson JSON
- **Legado 官方语言**：Kotlin/Java 是 Legado 原版实现语言，规则语义完全对齐

#### 劣势
- **JVM 开销**：APK 体积增加 ~15MB（精简 JRE）
- **启动延迟**：JVM 冷启动 ~500ms
- **开发成本**：需熟悉 Android Gradle 构建流程

#### 工作量估算

| 阶段 | 任务 | 预估工时 |
|------|------|---------|
| 1 | 移植 LegadoParser（复用原版 Legado 代码） | 3 天 |
| 2 | 集成 Rhino/Nashorn + Jsoup + Jackson | 4 天 |
| 3 | 实现 EngineHub 线程池 + 任务队列 | 2 天 |
| 4 | 封装 JNI/AIDL 接口供 Flutter 调用 | 3 天 |
| 5 | 适配 32 族服务接口 | 3 天 |
| 6 | 单元测试 + 黄金样本比对 | 2 天 |
| 7 | Android Gradle 集成 + ProGuard 混淆 | 2 天 |
| **合计** | | **19 天（约 1 个月）** |

**参考项目**：
- [Legado](https://github.com/gedoor/legado)：原版 Kotlin 实现（可直接复用 70% 代码）
- [Jsoup](https://jsoup.org/)：HTML 解析
- [Rhino](https://github.com/mozilla/rhino)：Mozilla JS 引擎

### 5.4 方案对比

| 维度 | Flet 恢复 | Go 重写 | Java 重写 |
|------|----------|---------|----------|
| **开发周期** | 3-5 天（提取 + 调试） | 26 天 | 19 天 |
| **Android 可行性** | ✅ `flet build apk` | ✅ NDK .so | ✅ 原生 JVM |
| **Windows 可行性** | ✅ PyInstaller | ✅ go build | ❌ 需 JRE |
| **iOS 可行性** | ❌ Python 限制 | ✅ static lib | ❌ JVM 限制 |
| **性能** | 同 Python | 2-3× 提升 | 1.5× 提升 |
| **维护成本** | 低（Python 全栈） | 中（双语言） | 高（三语言） |
| **代码复用率** | 100% 服务层 | 0%（全新实现） | 70%（复用 Legado） |
| **风险** | 低（已验证架构） | 中（生态缺失） | 低（成熟方案） |

---

## 6. 推荐路径

基于当前产品阶段与资源约束，建议按以下优先级执行：

### 短期（1-2 周）：**恢复 Flet 架构**
- 从 `f867150` 提取 UI 文件到 `legodo_flet/`
- 验证 `flet build apk` 在 Android 上的打包流程
- 产出双轨版本：`legodo_client_windows.zip`（Flutter）+ `legodo_flet_android.apk`（Flet）

### 中期（1-2 个月）：**评估 Go 重写必要性**
- 若 Flet APK 体积 > 150MB 或启动延迟 > 2s，启动 Go 重写计划
- 优先实现核心规则引擎（LegadoParser + QuickJS 替代）
- 保留 Python 服务层作为 fallback

### 长期（3-6 个月）：**统一技术栈**
- 若 Go 引擎验证成功，逐步迁移 32 族服务到 Go
- Flutter 前端通过 FFI 直接调用 Go .so 库（消除 HTTP 往返）
- 最终形态：Flutter + Go FFI（单进程、零网络开销）

---

## 7. 下一步行动

1. **立即执行**：从 git 历史提取 Flet 文件到 `legodo_flet/`
2. **验证打包**：在 Android 设备上测试 `flet build apk`
3. **性能基线**：对比 Flet APK 与 Flutter + 远程后端的启动延迟/内存占用
4. **决策点**：根据实测数据决定是否启动 Go/Java 重写

---

**文档版本**：v1.0  
**编写日期**：2026-10-11  
**参考提交**：`f867150`（最后纯 Flet 版本）、`e1c0fce`（Flutter Windows 锁定版）
