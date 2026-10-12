# Flutter 打包与部署指南

## 目录结构要求

打包后的可执行文件**必须保持以下目录结构**才能正常运行：

```
legodo_client.exe          # Flutter 前端主程序
└── backend/               # Python 后端目录（必需）
    ├── LegadoReader.exe   # PyInstaller 打包的 Python 后端
    └── _internal/         # Python 运行时和依赖库
```

### 关键说明

- **相对路径查找**：程序启动时从 `legodo_client.exe` 所在目录查找 `backend/LegadoReader.exe`
- **禁止移动 backend/**：不要将 `backend/` 目录移到其他位置或单独分发
- **整个文件夹一起复制**：部署时需复制整个输出目录，不能只拷贝 exe

## 打包步骤

### Windows 桌面版

```bash
cd legodo_dart
python build_release.py --target windows
```

输出目录：`build/windows/x64/runner/Release/`

打包产物：
- `legodo_client.exe` - Flutter 前端
- `backend/LegadoReader.exe` - Python 后端
- `backend/_internal/` - Python 运行时（约 200MB）

### 验证打包结果

```bash
# 检查必需文件是否存在
ls build/windows/x64/runner/Release/legodo_client.exe
ls build/windows/x64/runner/Release/backend/LegadoReader.exe
ls build/windows/x64/runner/Release/backend/_internal/
```

## 部署到其他电脑

### ✅ 正确做法

1. **压缩整个 Release 目录**：
   ```bash
   cd build/windows/x64/runner/
   tar -czf legodo_client.zip Release/
   ```

2. **在目标电脑上解压到任意位置**：
   ```
   D:\Apps\Legodo\
   ├── legodo_client.exe
   └── backend/
       ├── LegadoReader.exe
       └── _internal/
   ```

3. **直接运行** `legodo_client.exe`

### ❌ 错误做法

- ~~只拷贝 `legodo_client.exe`~~ （缺少后端会启动失败）
- ~~将 `backend/` 放在其他目录~~ （相对路径查找会失败）
- ~~修改目录结构~~ （如重命名 backend 文件夹）

## 常见问题

### Q: 提示"未找到内置 Python 后端"？

**原因**：`backend/LegadoReader.exe` 不存在或路径错误

**解决**：
1. 确认 `backend/` 目录与 `legodo_client.exe` 在同一级
2. 确认 `backend/LegadoReader.exe` 文件存在
3. 不要移动或重命名 `backend/` 目录

### Q: 为什么不用绝对路径？

**答**：使用绝对路径会导致：
- 开发机器上的路径硬编码到 exe 中
- 移动到其他电脑后路径失效
- 用户无法自定义安装位置

**当前方案**：使用 `Platform.resolvedExecutable` 获取 exe 自身位置，然后相对查找 `backend/`，实现真正的便携性。

### Q: 可以合并成单个 exe 吗？

**答**：技术上可行但复杂度高：
- 需要将 Python 后端作为资源嵌入 Flutter exe
- 启动时解压到临时目录
- 增加启动时间和磁盘占用

当前双 exe 方案更清晰、易维护、易调试。

## 技术细节

### 路径查找逻辑

```dart
// bootstrap.dart 第 99-101 行
final flutterExePath = Platform.resolvedExecutable;  // 获取当前 exe 路径
final flutterDir = File(flutterExePath).parent.path;  // 获取所在目录
final backendExe = '$flutterDir\\backend\\LegadoReader.exe';  // 相对拼接
```

### 编译时配置

```python
# build_release.py 不再设置 --dart-define
# LEGODO_PYTHON 和 LEGODO_MAIN 留空，让运行时使用相对路径
```

---

**最后更新**：2026-10-12  
**架构版本**：G4 单进程双轨（Flutter + PyInstaller 后端，相对路径查找）
