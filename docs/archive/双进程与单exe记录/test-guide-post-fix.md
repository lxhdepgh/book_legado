# 快速测试指南

## 已修复问题

✅ **flutter_windows.dll 缺失** - 已添加到分发包  
✅ **CMake INSTALL 权限错误** - 脚本自动容错  
✅ **nuget.exe 路径问题** - 自动检测并添加到 PATH  

---

## 测试步骤

### 1. 解压分发包

```bash
# 位置
D:\WorkSpace\PycharmProjects\legodo_booksource_check\legodo_dart\legodo_client_windows.zip

# 解压到任意目录，例如：
D:\test_legodo\
```

### 2. 启动应用

**方法 A：使用启动脚本（推荐）**
```bash
cd D:\test_legodo
start.bat
```

**方法 B：手动启动**
```bash
# 终端 1：启动后端
cd D:\test_legodo
.\legodo_server.exe

# 终端 2：启动前端（新窗口）
cd D:\test_legodo
.\legodo_client.exe
```

### 3. 验证功能

#### 基础功能
- [ ] 主界面正常显示
- [ ] 无崩溃或错误弹窗
- [ ] 后端控制台显示 "Legodo Go 后端已启动"

#### 核心功能
- [ ] 导入书源（JSON/TXT）
- [ ] 书源列表显示
- [ ] 搜索书籍（输入关键词）
- [ ] 搜索结果展示
- [ ] 查看书籍详情
- [ ] 获取章节目录
- [ ] 阅读章节内容

#### WebView 功能
- [ ] 点击书籍封面打开 WebView
- [ ] 网页正常加载
- [ ] 返回按钮可用

#### TTS 功能
- [ ] 朗读按钮可用
- [ ] 系统语音正常播放
- [ ] 暂停/继续/停止控制

---

## 常见问题排查

### Q1: 前端提示"连接失败"

**原因**：后端未启动或端口被占用

**检查**：
```powershell
# 检查后端是否在运行
netstat -ano | findstr :8080

# 检查进程
tasklist | findstr legodo_server
```

**解决**：
- 确保先启动 `legodo_server.exe`
- 如果端口被占用，关闭其他程序或重启电脑

### Q2: 后端启动后立即退出

**原因**：端口冲突或配置错误

**检查**：
- 查看后端控制台输出
- 检查是否有错误信息

**解决**：
- 确认 8080 端口未被占用
- 重新解压到干净目录

### Q3: 搜索无结果

**原因**：书源配置错误或网络问题

**检查**：
- 确认书源已正确导入
- 检查网络连接
- 查看后端日志

**解决**：
- 尝试不同的书源
- 检查书源 URL 是否有效

### Q4: WebView 无法加载

**原因**：WebView2 运行时未安装

**检查**：
```powershell
# 检查 WebView2 是否安装
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
```

**解决**：
- 下载并安装 Microsoft Edge WebView2 Runtime
- https://developer.microsoft.com/en-us/microsoft-edge/webview2/

---

## 性能基准

在以下环境测试：
- Windows 11
- Intel i7-12700K
- 16GB RAM
- SSD

**预期结果**：

| 指标 | 目标值 |
|------|--------|
| 冷启动时间 | < 3s |
| 内存占用 | ~60MB |
| 搜索响应 | < 2s |
| 章节加载 | < 1s |

---

## 反馈清单

测试完成后，请记录：

1. **启动是否正常**：是 / 否
2. **主要功能是否可用**：是 / 否
3. **遇到的错误**：（详细描述）
4. **性能感受**：快 / 一般 / 慢
5. **建议改进**：（可选）

---

## 下一步

如果测试通过，可以考虑：

1. **FFI 动态库模式**（性能提升 6.7x）
   - 安装 MinGW-w64
   - 重新打包：`python build_release.py windows`

2. **自定义图标和名称**
   - 修改 `windows/runner/resources/app_icon.ico`
   - 修改 `pubspec.yaml` 中的 `description`

3. **发布版本**
   - 更新版本号
   - 创建 Release Notes
   - 上传到 GitHub Releases
