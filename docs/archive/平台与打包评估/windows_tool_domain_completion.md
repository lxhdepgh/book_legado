# Windows 工具域功能完善报告

**日期**: 2026-10-10
**架构**: Python 后端 + Flutter 前端双进程（RPC 通信）

## 一、架构决策

### 1.1 放弃 flutter_js 集成
- **原因**: `flutter_js` 依赖 QuickJS C 桥接库（`quickjs_c_bridge.dll`），该文件在 Windows 上缺失导致所有测试失败
- **影响**: 无法在 Dart 侧直接运行 Legado JavaScript 规则
- **替代方案**: 保持当前架构，所有 JS 规则执行仍由 Python 后端的 RuleEngineService 处理

### 1.2 引入 file_picker 插件
- **版本**: `file_picker: ^6.2.1`
- **用途**: 
  - 字体管理：选择 .ttf/.otf/.woff 字体文件
  - 书籍导出：选择输出目录
- **平台支持**: Windows/macOS/Linux/Android/iOS 全平台

## 二、已完成功能清单

### 2.1 二维码工具（qr_code_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 生成二维码 | ✅ 完成 | 调用 `repo.qrGenerate(text)` 返回 Base64 PNG |
| 识别二维码 | ✅ 改进 | 改为手动输入图片 Base64 数据，调用 `repo.qrDecode(base64)` |

**改进点**: 原计划使用图片选择器，现改为文本输入框粘贴 Base64 数据，避免引入额外插件依赖

### 2.2 字体管理（font_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 扫描系统字体 | ✅ 完成 | 调用 `repo.fontScanSystem()` 枚举 Windows/macOS/Linux 系统字体 |
| 添加用户字体 | ✅ 完成 | 集成 FilePicker 选择字体文件，调用 `repo.fontAddUser(path)` |
| 移除用户字体 | ✅ 完成 | 调用 `repo.fontRemove(fontId)` |
| 切换当前字体 | ✅ 完成 | 调用 `repo.fontSelect(family)`，写入 SettingsService `reader.font_family` |

**支持格式**: .ttf, .otf, .ttc, .woff, .woff2

### 2.3 书籍导出（export_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 导出 epub | ✅ 完成 | 调用 `repo.exportEpub(bookKey, outputPath)` |
| 导出 txt | ✅ 完成 | 调用 `repo.exportTxt(bookKey, outputPath)` |
| 选择输出目录 | ✅ 完成 | 集成 FilePicker 文件夹选择器，自动拼接文件名 |

**UI 改进**: 
- 增加"选 epub 目录" / "选 txt 目录"快捷按钮
- 选择目录后自动填充完整路径（`目录\bookKey.epub`）

### 2.4 替换规则（replace_rules_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 规则列表 | ✅ 完成 | 调用 `repo.replaceList()` |
| 新增规则 | ✅ 完成 | 调用 `repo.replaceAdd(...)` |
| 编辑规则 | ✅ 完成 | 调用 `repo.replaceUpdate(...)` |
| 删除规则 | ✅ 完成 | 调用 `repo.replaceDelete(ruleId)` |
| 全局开关 | ✅ 完成 | 调用 `repo.replaceToggleGlobal(enabled)` |

### 2.5 WebDAV 备份（webdav_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 备份数据 | ✅ 完成 | 调用 `repo.webdavBackup()` |
| 恢复备份 | ✅ 完成 | 调用 `repo.webdavRestore(backupId)` |
| 备份列表 | ✅ 完成 | 调用 `repo.webdavListBackups()` |

### 2.6 网络桥（web_bridge_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 启动服务器 | ✅ 完成 | 调用 `repo.webbridgeStart(port)` |
| 停止服务器 | ✅ 完成 | 调用 `repo.webbridgeStop()` |
| 状态监控 | ✅ 完成 | 调用 `repo.webbridgeStatus()` |

### 2.7 浏览器（browser_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| CDP 投屏占位 | ⚠️ 待实现 | 当前为说明页面，需后续实现 WebSocket 帧传输 |

**待实现内容**:
- WebSocket 连接 Python 侧 BrowserService
- JPEG 帧接收与显示
- 手势捕获与坐标换算
- 会话管理（open/navigate/close）

### 2.8 TTS 语音配置（tts_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 占位页面 | ⚠️ 待实现 | 当前为空白页面 |

**待实现内容**:
- 语音列表加载（调用 Python 后端 TTS 服务）
- 语速/音调调节 UI
- 试听功能

### 2.9 词典管理（dictionary_screen.dart）
| 功能 | 状态 | 说明 |
|------|------|------|
| 基础 UI | ⚠️ 待实现 | 当前仅有单词查询输入框 |

**待实现内容**:
- 词典规则 CRUD
- 在线词典 API 集成

## 三、Python 后端服务状态

### 3.1 已实现服务（app/services/tools/）
| 服务类 | 文件 | 状态 |
|--------|------|------|
| QrCodeService | qr_code_service.py | ✅ 完成 |
| BrowserService | browser_service.py | ✅ 完成 |
| ReplaceRuleService | replace_rule_service.py | ✅ 完成 |
| FontService | font_service.py | ✅ 完成 |
| BookExportService | book_export_service.py | ✅ 完成 |
| WebDavService | web_dav_service.py | ✅ 完成 |
| WebBridgeService | web_bridge_service.py | ✅ 完成 |
| DictService | dict_service.py | ✅ 完成 |
| ThemeService | theme_service.py | ✅ 完成 |
| UpdateService | update_service.py | ✅ 完成 |
| BossKeyService | boss_key_service.py | ✅ 完成 |
| BackgroundService | background_service.py | ✅ 完成 |

### 3.2 Gateway 动作表（app/gateway/methods.py）
工具域共注册 **35 个动作**，覆盖 9 个服务领域：
- `qr.*`: 2 个（generate, decode）
- `browser.*`: 3 个（open_page, navigate, close）
- `replace.*`: 5 个（list, add, update, delete, toggle_global）
- `font.*`: 4 个（scan_system, add_user, remove, select）
- `export.*`: 2 个（epub, txt）
- `webdav.*`: 3 个（backup, restore, list_backups）
- `webbridge.*`: 3 个（start, stop, status）
- `dict.*`: 2 个（lookup, add_rule）
- `theme.*`: 3 个（preferences, save, apply）
- `update.*`: 3 个（check, ignore, download）
- `bosskey.*`: 2 个（toggle, set_hotkey）
- `background.*`: 3 个（start, stop, status）

## 四、Dart RPC 接口层

### 4.1 抽象接口（engine_repository.dart）
- 定义 35+ 个工具域方法签名
- 默认体抛 `EngineFailure(kind=unavailable)` 确保编译不报错、运行时有声音

### 4.2 RPC 实现（rpc_engine_repository.dart）
- 所有方法通过 HTTP POST `/rpc` 调用 Python 后端
- JSON-RPC 2.0 风格信封格式

### 4.3 测试替身（fake_engine_repository.dart）
- 所有方法返回 null/空列表/空 Map
- 用于单元测试和 Golden 测试

## 五、待完善功能优先级

### P0 - 核心功能缺失
1. **浏览器 CDP 投屏**（browser_screen.dart）
   - 需要实现 WebSocket 客户端
   - 需要 JPEG 帧解码与渲染
   - 需要手势事件捕获与坐标换算

### P1 - 用户体验优化
2. **TTS 语音配置**（tts_screen.dart）
   - 需要读取 Python 后端 TTS 服务配置
   - 需要音频播放能力

3. **词典管理**（dictionary_screen.dart）
   - 需要规则 CRUD 界面
   - 需要在线词典 API 集成

### P2 - 可选增强
4. **二维码图片选择器**（可选）
   - 如需从本地图片文件识别二维码，可引入 `image_picker` 插件
   - 当前方案：手动粘贴 Base64 数据

## 六、技术约束与注意事项

### 6.1 跨线程限制
- EngineHub 的 loop 只能在外层线程收尾
- 碰控件的回调须经 `_call_ui` 回界面循环
- 循环上绝不释放 COM/原生对象

### 6.2 Windows 平台特性
- 系统字体目录：`C:\Windows\Fonts`
- 文件路径分隔符：`\`（FilePicker 返回的路径需正确处理）
- 退出码：正常关窗 rc=0，外部杀 rc=-1

### 6.3 Riverpod 注意事项
- provider 默认重试指数退避 10 次，需在构造时显式设置 `retry: 0`
- late final 监听器必须尽早注册，否则 dispose 时读不到

## 七、提交记录

```
commit d71fc4f
feat: 完善 Windows 工具域功能——二维码识别改 Base64、字体/导出加文件选择器

- 移除 flutter_js 依赖（Windows QuickJS 桥接库缺失导致测试失败）
- 添加 file_picker 插件支持文件/目录选择
- 二维码识别：改为手动输入图片 Base64 数据解码（无需摄像头/图片选择器）
- 字体管理：用户字体添加集成 FilePicker，支持 .ttf/.otf/.woff 等格式
- 书籍导出：输出路径改为文件夹选择器，自动拼接文件名
- 保持当前 Python 后端 + Flutter 前端双进程架构，所有功能通过 RPC 调用
```

## 八、下一步工作建议

1. **验证 Python 后端依赖**：确认 qrcode、opencv-python-headless、fontTools 等库已安装
2. **实机测试**：在 Windows 桌面环境启动应用，逐一验证各工具域功能
3. **浏览器 CDP 投屏**：如需要可见 WebView 功能，需实现 WebSocket 帧传输层
4. **性能基线测量**：对工具域各功能的响应时间进行 A/B 交替测量
