# Windows 下编译 Go 动态库指南

## 问题

编译 Go DLL 时出现错误：
```
cgo: C compiler "gcc" not found
```

这是因为 Go 的 CGO 功能需要 C 编译器支持。

---

## 解决方案

### 方案 1：安装 MinGW-w64（推荐）

#### 步骤 1：下载 MinGW-w64

访问以下任一地址下载：
- **官方 GitHub**: https://github.com/mingw-w64/mingw-w64/releases
- **WinLibs**: https://winlibs.com/
- **MSYS2**: https://www.msys2.org/

**推荐下载**：
```
mingw-w64-v12.2.0.zip (或更新版本)
选择：x86_64-posix-seh
```

#### 步骤 2：解压到固定目录

```bash
# 推荐路径（无空格、无中文）
C:\mingw64\
```

目录结构应类似：
```
C:\mingw64\
├── bin\
│   ├── gcc.exe
│   ├── g++.exe
│   └── ...
├── include\
├── lib\
└── ...
```

#### 步骤 3：添加到系统 PATH

**方法 A：图形界面**
1. 右键"此电脑" → 属性 → 高级系统设置
2. 环境变量 → 系统变量 → Path → 编辑
3. 新建：`C:\mingw64\bin`
4. 确定 → 重启终端

**方法 B：PowerShell（管理员）**
```powershell
$oldPath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$newPath = "$oldPath;C:\mingw64\bin"
[Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")
```

#### 步骤 4：验证安装

```bash
# 重启终端后执行
gcc --version
# 应输出：gcc.exe (MinGW-W64 ...) 12.2.0

go env CGO_ENABLED
# 应输出：1
```

---

### 方案 2：使用 MSYS2

如果你已经安装了 MSYS2：

```bash
# 在 MSYS2 终端中
pacman -S mingw-w64-x86_64-gcc

# 添加路径到系统 PATH
C:\msys64\mingw64\bin
```

---

### 方案 3：使用 TDM-GCC（简化版）

下载地址：https://jmeubank.github.io/tdm-gcc/

1. 下载安装程序
2. 选择 `TDM-GCC 64-bit`
3. 安装到 `C:\TDM-GCC-64`
4. 自动添加到 PATH

---

## 安装完成后重新编译

```bash
cd legodo_dart
python build_release.py windows
```

预期输出：
```
[1/4] Building Go backend for windows...
[OK] Found Go: go version go1.26.3 windows/amd64
$ go build -buildmode=c-shared -ldflags=-s -w -o legodo_engine.dll ./pkg/ffi/
[OK] Go backend compiled as dynamic library: legodo_engine.dll
```

---

## 如果不想安装 GCC

使用独立进程模式（无需 CGO）：

```bash
python build_release.py windows --standalone
```

这会编译为独立的可执行文件，通过 WebSocket 通信，不需要 C 编译器。

---

## 常见问题

### Q: 安装后仍然提示 gcc not found

**A:** 
1. 确认终端已重启
2. 检查 PATH 是否正确：`echo %PATH%`
3. 验证 gcc 位置：`where gcc`

### Q: CGO_ENABLED 仍然是 0

**A:**
```bash
go env -w CGO_ENABLED=1
```

### Q: 编译成功但运行时找不到 dll

**A:** 确保 dll 和 exe 在同一目录，或使用绝对路径加载。

---

## 性能对比

| 模式 | 编译要求 | 启动时间 | 内存占用 | 适用场景 |
|------|---------|---------|---------|---------|
| FFI (DLL) | 需 MinGW | ~0.3s | ~35MB | 桌面发布 |
| Standalone | 仅需 Go | ~2s | ~60MB | 开发调试 |
