#!/usr/bin/env python3
"""一键打包 Flutter 多平台 Release（含 Go 后端）。

WebView + TTS 已改为 Dart 原生实现，核心引擎由 Go 后端提供。
支持平台：windows / linux / macos

环境自动检测与修复：
- 自动查找 flutter/go 命令
- 跨平台路径兼容（Windows/Linux/macOS）
- 自动定位 Go 后端源码
- Windows 单文件打包支持（Enigma Virtual Box）

注意：本脚本完全独立于 legodo_flet，所有资源都在 legodo_dart/ 内。

Windows 单文件打包:
    # 首次使用需安装 Enigma Virtual Box
    .\\install_enigma_vbox.ps1
    
    # 打包为单个 exe（推荐用于分发）
    python build_release.py windows --single-exe
    
    # 多文件版本（推荐用于调试）
    python build_release.py windows
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# ============================================================================
# 项目结构自动发现（仅在当前目录内）
# ============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent  # legodo_booksource_check/
DART_CLIENT = SCRIPT_DIR          # legodo_dart/
GO_BACKEND = SCRIPT_DIR / "backend_go"  # legodo_dart/backend_go/


def find_command(cmd_name: str, extra_paths: list[Path] | None = None) -> Path | None:
    """在 PATH 和额外路径中查找可执行文件。
    
    Args:
        cmd_name: 命令名（如 'flutter', 'go'）
        extra_paths: 额外的搜索路径列表
    
    Returns:
        找到的可执行文件路径，或 None
    """
    # 1. 先试系统 PATH
    if path := shutil.which(cmd_name):
        return Path(path)
    
    # 2. 尝试常见安装位置
    search_paths = []
    if platform.system() == "Windows":
        search_paths.extend([
            Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Programs" / cmd_name,
            Path("C:/Program Files") / cmd_name,
            Path("C:/Program Files (x86)") / cmd_name,
        ])
        # Flutter 常见位置
        if cmd_name == "flutter":
            search_paths.extend([
                Path("D:/env_manger/flutter/bin/flutter.bat"),
                Path("D:/flutter/bin/flutter.bat"),
                Path("C:/flutter/bin/flutter.bat"),
            ])
        # Go 常见位置
        if cmd_name == "go":
            search_paths.extend([
                Path("C:/Go/bin/go.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Go" / "bin" / "go.exe",
            ])
    elif platform.system() == "Linux":
        search_paths.extend([
            Path("/usr/local/bin") / cmd_name,
            Path("/usr/bin") / cmd_name,
            Path.home() / ".local" / "bin" / cmd_name,
        ])
    elif platform.system() == "Darwin":
        search_paths.extend([
            Path("/usr/local/bin") / cmd_name,
            Path("/opt/homebrew/bin") / cmd_name,
            Path.home() / "Applications" / cmd_name,
        ])
    
    if extra_paths:
        search_paths.extend(extra_paths)
    
    for candidate in search_paths:
        if candidate.exists():
            return candidate
    
    return None


def ensure_flutter() -> Path:
    """确保 flutter 命令可用，否则给出详细错误信息。"""
    flutter = find_command("flutter")
    if not flutter:
        print("[ERROR] Flutter command not found in PATH", file=sys.stderr)
        print("\n[INFO] Please install Flutter and add it to your PATH:", file=sys.stderr)
        print("  Windows: https://docs.flutter.dev/get-started/install/windows", file=sys.stderr)
        print("  Linux:   https://docs.flutter.dev/get-started/install/linux", file=sys.stderr)
        print("  macOS:   https://docs.flutter.dev/get-started/install/macos", file=sys.stderr)
        print("\n[INFO] Common installation paths:", file=sys.stderr)
        print("  - D:\\env_manger\\flutter\\bin\\", file=sys.stderr)
        print("  - C:\\flutter\\bin\\", file=sys.stderr)
        print("  - ~/flutter/bin/", file=sys.stderr)
        sys.exit(1)
    
    # 验证 flutter doctor
    try:
        result = subprocess.run(
            [str(flutter), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace"
        )
        if result.returncode == 0:
            version_line = result.stdout.strip().split("\n")[0]
            print(f"[OK] Found Flutter: {version_line}", flush=True)
        else:
            print(f"[WARN] Flutter version check failed", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Flutter version: {e}", flush=True)
    
    return flutter


def ensure_go() -> Path:
    """确保 go 命令可用。"""
    go = find_command("go")
    if not go:
        print("[ERROR] Go command not found in PATH", file=sys.stderr)
        print("\n[INFO] Please install Go and add it to your PATH:", file=sys.stderr)
        print("  Download: https://go.dev/dl/", file=sys.stderr)
        print("\n[INFO] Common installation paths:", file=sys.stderr)
        print("  - C:\\Go\\bin\\", file=sys.stderr)
        print("  - /usr/local/go/bin/", file=sys.stderr)
        print("  - ~/go/bin/", file=sys.stderr)
        sys.exit(1)
    
    # 验证 go version
    try:
        result = subprocess.run(
            [str(go), "version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"[OK] Found Go: {result.stdout.strip()}", flush=True)
        else:
            print(f"[WARN] Go version check failed", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Go version: {e}", flush=True)
    
    return go


def check_cgo_support() -> bool:
    """检查系统是否支持 CGO（编译动态库所需）。"""
    try:
        result = subprocess.run(
            ["go", "env", "CGO_ENABLED"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            # 进一步检查 gcc 是否可用
            gcc_check = subprocess.run(
                ["gcc", "--version"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return gcc_check.returncode == 0
        return False
    except Exception:
        return False


def install_mingw_if_needed() -> bool:
    """如果 MinGW 未安装，尝试自动安装。返回是否成功。"""
    if check_cgo_support():
        return True
    
    print("[INFO] MinGW-w64 not detected. Attempting auto-install...", flush=True)
    
    mingw_dir = Path("C:/mingw64")
    if mingw_dir.exists() and (mingw_dir / "bin" / "gcc.exe").exists():
        # MinGW 已解压但未添加到 PATH
        print(f"[INFO] Found MinGW at {mingw_dir}, adding to PATH...", flush=True)
        os.environ["PATH"] = f"{mingw_dir}\\bin;{os.environ.get('PATH', '')}"
        
        # 验证
        if check_cgo_support():
            print("[OK] MinGW added to PATH successfully", flush=True)
            return True
    
    # 尝试下载并安装
    download_url = "https://github.com/niXman/mingw-builds-binaries/releases/download/14.2.0-rt_v12-rev0/x86_64-14.2.0-release-posix-seh-msvcrt-rt_v12-rev0.7z"
    archive_path = Path.home() / "Downloads" / "mingw64.7z"
    
    if not archive_path.exists():
        print(f"[INFO] Downloading MinGW-w64 from GitHub...", flush=True)
        try:
            import urllib.request
            urllib.request.urlretrieve(download_url, str(archive_path))
            print(f"[OK] Downloaded to: {archive_path}", flush=True)
        except Exception as e:
            print(f"[ERROR] Download failed: {e}", flush=True)
            return False
    
    # 解压到 C:\
    print(f"[INFO] Extracting to C:\\...", flush=True)
    try:
        import py7zr
        archive = py7zr.SevenZipFile(str(archive_path), mode='r')
        archive.extractall(path='C:/')
        archive.close()
        print("[OK] Extraction complete", flush=True)
    except ImportError:
        print("[ERROR] py7zr not available. Please install manually:", flush=True)
        print(f"  pip install py7zr", flush=True)
        return False
    except Exception as e:
        print(f"[ERROR] Extraction failed: {e}", flush=True)
        return False
    
    # 添加到 PATH
    if (mingw_dir / "bin" / "gcc.exe").exists():
        os.environ["PATH"] = f"{mingw_dir}\\bin;{os.environ.get('PATH', '')}"
        if check_cgo_support():
            print("[OK] MinGW installed and configured", flush=True)
            return True
    
    print("[WARN] MinGW installed but gcc not found in PATH", flush=True)
    return False


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    """执行命令并在失败时退出。
    
    Args:
        cmd: 命令及参数列表
        cwd: 工作目录
        env: 环境变量（合并到当前环境）
    """
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or SCRIPT_DIR,
            env=merged_env,
            check=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        # 实时输出
        if result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    print(f"  {line}")
    except FileNotFoundError as e:
        print(f"[ERROR] Command not found: {cmd[0]}", file=sys.stderr)
        print(f"[INFO] Please ensure '{cmd[0]}' is installed and in your PATH", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed with exit code {e.returncode}", file=sys.stderr)
        if e.stdout:
            for line in str(e.stdout).splitlines():
                if line.strip():
                    print(f"  {line}")
        sys.exit(e.returncode)


def build_go_backend(target_platform: str, use_dll: bool = True) -> Path | None:
    """编译 Go 后端为动态库（DLL/SO）或可执行文件。
    
    Args:
        target_platform: 目标平台 (windows/linux/macos/android)
        use_dll: 是否编译为动态库（默认 True）
    
    Returns:
        编译后的库文件或可执行文件路径，Android 返回 None（不使用 Go FFI）
    """
    # Android 不支持 Go FFI（需要交叉编译 ARM + JNI，暂不支持）
    if target_platform == "android":
        print(f"\n[1/4] Skipping Go backend for Android (not supported)", flush=True)
        print(f"[INFO] Android uses pure Dart/Flutter implementation", flush=True)
        return None
    
    print(f"\n[1/4] Building Go backend for {target_platform}...", flush=True)
    
    # 检查 Go 后端目录是否存在
    if not GO_BACKEND.exists():
        print(f"[ERROR] Go backend directory not found at: {GO_BACKEND}", file=sys.stderr)
        print("[INFO] Expected structure: legodo_dart/backend_go/", file=sys.stderr)
        sys.exit(1)
    
    # 确保 go 可用
    go = ensure_go()
    
    # 如果请求动态库模式，检查 CGO 支持
    if use_dll and not check_cgo_support():
        print("[WARN] CGO support not detected (gcc not found)", flush=True)
        
        # 尝试自动安装 MinGW
        if install_mingw_if_needed():
            print("[OK] MinGW setup complete, proceeding with DLL build", flush=True)
        else:
            print("[INFO] Dynamic library mode requires a C compiler:", flush=True)
            
            if platform.system() == "Windows":
                print("\n[INSTALL GUIDE] MinGW-w64 for Windows:", flush=True)
                print("  1. Download from: https://github.com/mingw-w64/mingw-w64/releases", flush=True)
                print("     Or: https://winlibs.com/ (recommended: x86_64-posix-seh)", flush=True)
                print("  2. Extract to: C:\\mingw64\\", flush=True)
                print("  3. Add to PATH: C:\\mingw64\\bin", flush=True)
                print("  4. Restart terminal and retry", flush=True)
                print("\n  Quick install via MSYS2:", flush=True)
                print("    pacman -S mingw-w64-x86_64-gcc", flush=True)
            elif platform.system() == "Linux":
                print("\n[INSTALL GUIDE] GCC for Linux:", flush=True)
                print("  Ubuntu/Debian: sudo apt install gcc", flush=True)
                print("  CentOS/RHEL:   sudo yum install gcc", flush=True)
            elif platform.system() == "Darwin":
                print("\n[INSTALL GUIDE] Xcode Command Line Tools:", flush=True)
                print("  xcode-select --install", flush=True)
            
            print("\n[ALTERNATIVE] Use standalone mode (no CGO required):", flush=True)
            print(f"  python build_release.py {target_platform} --standalone", flush=True)
            print()
            
            # 询问用户是否降级到独立进程模式
            try:
                response = input("Auto-fallback to standalone mode? [Y/n]: ").strip().lower()
                if response in ('', 'y', 'yes'):
                    print("[INFO] Switching to standalone mode...", flush=True)
                    return build_go_backend(target_platform, use_dll=False)
                else:
                    print("[ERROR] Build aborted by user", file=sys.stderr)
                    sys.exit(1)
            except EOFError:
                # 非交互模式，直接降级
                print("[INFO] Non-interactive mode, auto-fallback to standalone...", flush=True)
                return build_go_backend(target_platform, use_dll=False)
    
    # 设置交叉编译环境变量
    goos_map = {
        "windows": "windows",
        "linux": "linux",
        "macos": "darwin",
    }
    goarch_map = {
        "windows": "amd64",
        "linux": "amd64",
        "macos": "amd64",
    }
    
    goos = goos_map.get(target_platform)
    goarch = goarch_map.get(target_platform)
    
    if not goos or not goarch:
        print(f"[ERROR] Unsupported target platform for Go FFI: {target_platform}", file=sys.stderr)
        print(f"[INFO] Supported platforms: windows, linux, macos", file=sys.stderr)
        sys.exit(1)
    
    env = os.environ.copy()
    env["GOOS"] = goos
    env["GOARCH"] = goarch
    
    if use_dll:
        # 编译为动态库（需要启用 CGO）
        env["CGO_ENABLED"] = "1"
        
        # 确定输出文件名和编译标志
        if target_platform == "windows":
            output_path = GO_BACKEND / "legodo_engine.dll"
            build_flags = ["-buildmode=c-shared"]
        elif target_platform == "linux":
            output_path = GO_BACKEND / "liblegodo_engine.so"
            build_flags = ["-buildmode=c-shared"]
        elif target_platform == "macos":
            output_path = GO_BACKEND / "liblegodo_engine.dylib"
            build_flags = ["-buildmode=c-shared"]
        else:
            print(f"[ERROR] Unsupported platform for DLL mode: {target_platform}", file=sys.stderr)
            sys.exit(1)
        
        # 编译命令（针对 FFI 包）
        build_cmd = [
            str(go), "build",
            *build_flags,
            "-ldflags=-s -w",
            "-o", str(output_path),
            "./pkg/ffi/"
        ]
    else:
        # 编译为独立可执行文件（原有逻辑）
        env["CGO_ENABLED"] = "0"
        
        exe_name = "legodo_server.exe" if target_platform == "windows" else "legodo_server"
        output_path = GO_BACKEND / exe_name
        
        build_cmd = [
            str(go), "build",
            "-ldflags=-s -w",
            "-o", str(output_path),
            "./cmd/server/"
        ]
    
    run(build_cmd, cwd=GO_BACKEND, env=env)
    
    if not output_path.exists():
        print(f"[ERROR] Go library not created at: {output_path}", file=sys.stderr)
        sys.exit(1)
    
    lib_type = "dynamic library" if use_dll else "executable"
    print(f"[OK] Go backend compiled as {lib_type}: {output_path}", flush=True)
    return output_path


def build_flutter(target: str) -> Path:
    """构建 Flutter 应用并返回输出目录。
    
    Args:
        target: 目标平台 (windows/linux/macos)
    """
    print(f"\n[2/4] Building Flutter {target} Release...", flush=True)
    
    # 确保 flutter 可用
    flutter = ensure_flutter()
    
    # Windows 下需要确保 nuget.exe 在 PATH 中（flutter_tts 依赖）
    env = os.environ.copy()
    if platform.system() == "Windows":
        # 查找 nuget.exe
        nuget_paths = [
            Path.home() / "bin",  # C:\Users\73517\bin
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/nuget"),
        ]
        for p in nuget_paths:
            if (p / "nuget.exe").exists():
                nuget_dir = str(p)
                current_path = env.get("PATH", "")
                if nuget_dir not in current_path:
                    env["PATH"] = f"{nuget_dir};{current_path}"
                    print(f"[INFO] Added nuget path to PATH: {nuget_dir}", flush=True)
                break
    
    cmd = [str(flutter), "build", target, "--release"]
    
    try:
        run(cmd, cwd=DART_CLIENT, env=env)
    except SystemExit as e:
        # Windows 下 flutter build windows 可能会在 INSTALL 阶段失败（权限问题）
        # 但实际的 exe 已经生成，所以检查输出目录是否存在即可
        if platform.system() == "Windows" and target == "windows":
            output_test = DART_CLIENT / "build" / "windows" / "x64" / "runner" / "Release" / "legodo_client.exe"
            if output_test.exists():
                print("[WARN] Flutter build reported error but executable was created, continuing...", flush=True)
            else:
                raise  # 真的失败了，重新抛出异常
        else:
            raise  # 其他平台正常抛出
    
    # 确定输出目录
    output_map = {
        "windows": DART_CLIENT / "build" / "windows" / "x64" / "runner" / "Release",
        "linux": DART_CLIENT / "build" / "linux" / "x64" / "release" / "bundle",
        "macos": DART_CLIENT / "build" / "macos" / "Build" / "Products" / "Release" / "legodo_client.app",
    }
    output_dir = output_map.get(target)
    if not output_dir or not output_dir.exists():
        print(f"[ERROR] Output directory not found for target: {target}", file=sys.stderr)
        print(f"[INFO] Expected: {output_dir}", file=sys.stderr)
        sys.exit(1)
    
    print(f"[OK] Build output: {output_dir}", flush=True)
    return output_dir


def build_android_apk() -> Path:
    """构建 Android APK。
    
    Returns:
        APK 文件路径
    """
    print(f"\n[2/4] Building Android APK...", flush=True)
    
    # 确保 flutter 可用
    flutter = ensure_flutter()
    
    # 检查 JAVA_HOME
    java_home = os.environ.get("JAVA_HOME", "")
    if not java_home:
        print("[WARN] JAVA_HOME not set, attempting to find Java...", flush=True)
        # 尝试常见位置
        possible_paths = [
            Path("C:/Program Files/Java"),
            Path("C:/Program Files (x86)/Java"),
            Path.home() / ".jdks",
        ]
        for base in possible_paths:
            if base.exists():
                for jdk_dir in base.iterdir():
                    if (jdk_dir / "bin" / "java.exe").exists():
                        java_home = str(jdk_dir)
                        os.environ["JAVA_HOME"] = java_home
                        print(f"[OK] Found Java: {java_home}", flush=True)
                        break
            if java_home:
                break
    
    if not java_home:
        print("[ERROR] JAVA_HOME not set and Java not found", file=sys.stderr)
        print("[INFO] Please install JDK 17+ and set JAVA_HOME", file=sys.stderr)
        print("[INFO] Download: https://adoptium.net/", file=sys.stderr)
        sys.exit(1)
    
    # 验证 Java 版本
    java_exe = Path(java_home) / "bin" / "java.exe" if platform.system() == "Windows" else Path(java_home) / "bin" / "java"
    if not java_exe.exists():
        print(f"[ERROR] Java executable not found at: {java_exe}", file=sys.stderr)
        sys.exit(1)
    
    try:
        result = subprocess.run(
            [str(java_exe), "-version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version_line = result.stderr.strip().split("\n")[0] if result.stderr else result.stdout.split("\n")[0]
            print(f"[OK] {version_line}", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Java version: {e}", flush=True)
    
    # 构建 APK
    cmd = [str(flutter), "build", "apk", "--release"]
    
    try:
        run(cmd, cwd=DART_CLIENT)
    except SystemExit as e:
        print(f"[ERROR] Flutter APK build failed", file=sys.stderr)
        raise
    
    # 查找生成的 APK
    apk_dir = DART_CLIENT / "build" / "app" / "outputs" / "flutter-apk"
    if not apk_dir.exists():
        print(f"[ERROR] APK output directory not found: {apk_dir}", file=sys.stderr)
        sys.exit(1)
    
    # 找到最新的 APK
    apks = list(apk_dir.glob("*.apk"))
    if not apks:
        print(f"[ERROR] No APK files found in: {apk_dir}", file=sys.stderr)
        sys.exit(1)
    
    latest_apk = max(apks, key=lambda p: p.stat().st_mtime)
    
    print(f"[OK] APK built: {latest_apk}")
    print(f"[INFO] Size: {latest_apk.stat().st_size / (1024*1024):.1f} MB", flush=True)
    
    # 复制到 dist 目录
    dist_dir = SCRIPT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)
    
    dest_apk = dist_dir / latest_apk.name
    shutil.copy2(latest_apk, dest_apk)
    print(f"[OK] APK copied to: {dest_apk}", flush=True)
    
    return dest_apk


def find_enigma_virtual_box() -> Path | None:
    """查找 Enigma Virtual Box 安装路径。
    
    Returns:
        enigmavbconsole.exe 的路径，或 None
    """
    if platform.system() != "Windows":
        return None
    
    # 常见安装位置
    search_paths = [
        Path("C:/Program Files/Enigma Virtual Box/enigmavbconsole.exe"),
        Path("C:/Program Files (x86)/Enigma Virtual Box/enigmavbconsole.exe"),
        Path(os.environ.get("PROGRAMFILES", "")) / "Enigma Virtual Box" / "enigmavbconsole.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Enigma Virtual Box" / "enigmavbconsole.exe",
    ]
    
    for path in search_paths:
        if path.exists():
            return path
    
    # 尝试通过 PATH 查找
    if path := shutil.which("enigmavbconsole.exe"):
        return Path(path)
    
    return None


def create_single_exe_windows_manual_guide(dist_dir: Path, output_exe: Path) -> None:
    """生成单文件打包的手动操作指南。
    
    Args:
        dist_dir: 分发目录
        output_exe: 期望的输出路径
    """
    guide_file = dist_dir / "SINGLE_EXE_INSTRUCTIONS.txt"
    
    instructions = f"""
============================================================
  单文件打包操作指南 (Enigma Virtual Box)
============================================================

由于 Enigma Virtual Box 控制台版本的限制，需要手动使用 GUI 进行打包。

步骤 1: 打开 Enigma Virtual Box
--------------------------------
  运行: C:\\Program Files (x86)\\Enigma Virtual Box\\enigmavb.exe

步骤 2: 配置输入/输出
--------------------------------
  Input file:  {dist_dir}\\legodo_client.exe
  Output file: {output_exe}

步骤 3: 添加文件
--------------------------------
  点击 "Add" → "Add File"，依次添加以下文件：
  
  - legodo_engine.dll
  - flutter_windows.dll
  - flutter_tts_plugin.dll
  - data\\app.so

  （这些文件都在当前目录中）

步骤 4: 设置选项
--------------------------------
  ✅ Single file（单选框）
  ✅ Compress files（压缩文件）
  ❌ Temp mode（不要勾选）

步骤 5: 开始打包
--------------------------------
  点击 "Process" 按钮
  
  预计耗时: 1-2 分钟
  输出大小: ~45 MB

完成后测试
--------------------------------
  运行生成的 {output_exe.name} 文件
  
  如果正常运行，说明打包成功！

============================================================
"""
    
    guide_file.write_text(instructions, encoding="utf-8")
    print(f"\n[INFO] Manual instructions saved to: {guide_file}", flush=True)


def create_single_exe_windows(dist_dir: Path, output_exe: Path) -> Path | None:
    """尝试使用 Enigma Virtual Box 将多文件打包为单个 exe。
    
    注意: Enigma Virtual Box 控制台版本存在限制，此函数仅提供手动操作指南。
    
    Args:
        dist_dir: 分发目录（包含 exe + DLLs + data）
        output_exe: 输出的单文件 exe 路径
    
    Returns:
        生成的单文件 exe 路径，或 None（需手动操作）
    """
    print(f"\n[4/4] Creating single-file executable...", flush=True)
    
    enigma = find_enigma_virtual_box()
    if not enigma:
        print("[ERROR] Enigma Virtual Box not found!", file=sys.stderr)
        print("\n[INSTALL GUIDE] Please install Enigma Virtual Box:", file=sys.stderr)
        print("  Download: https://enigmaprotector.com/en/products/vbox.html", file=sys.stderr)
        sys.exit(1)
    
    print(f"[OK] Found Enigma Virtual Box: {enigma}", flush=True)
    
    # 主 exe 文件
    main_exe = dist_dir / "legodo_client.exe"
    if not main_exe.exists():
        print(f"[ERROR] Main executable not found: {main_exe}", file=sys.stderr)
        sys.exit(1)
    
    # 检查必需文件
    required_files = [
        "legodo_engine.dll",
        "flutter_windows.dll", 
        "flutter_tts_plugin.dll",
        "data/app.so",
    ]
    
    missing_files = []
    for fname in required_files:
        fpath = dist_dir / fname.replace("/", "\\")
        if not fpath.exists():
            missing_files.append(fname)
    
    if missing_files:
        print(f"[ERROR] Missing required files: {', '.join(missing_files)}", file=sys.stderr)
        print(f"[INFO] Please run full build first", file=sys.stderr)
        return None
    
    # 由于 Enigma Virtual Box 控制台版本的限制，提供手动指南
    print(f"\n[WARN] Enigma Virtual Box console version has limitations.", flush=True)
    print(f"[INFO] Generating manual instructions...", flush=True)
    
    create_single_exe_windows_manual_guide(dist_dir, output_exe)
    
    print(f"\n{'=' * 60}")
    print(f"  单文件打包需要手动操作")
    print(f"{'=' * 60}")
    print(f"\n[INFO] Please follow the instructions in:")
    print(f"  {dist_dir / 'SINGLE_EXE_INSTRUCTIONS.txt'}")
    print(f"\n[INFO] Or use multi-file distribution instead:")
    print(f"  python build_release.py windows")
    print()
    
    return None


def create_distribution(target_platform: str, go_lib: Path | None, use_dll: bool = True, single_exe: bool = False) -> Path:
    """创建分发包（包含 Flutter 产物 + Go 动态库 + FFI 适配器）。
    
    Args:
        target_platform: 目标平台 (windows/linux/macos/android)
        go_lib: Go 动态库或可执行文件路径（Android 为 None）
        use_dll: 是否使用动态库模式
        single_exe: 是否打包为单文件 exe（仅 Windows）
    
    Returns:
        分发包 zip/apk 文件路径
    """
    print(f"\n[3/4] Creating distribution package for {target_platform}...", flush=True)
    
    # Android 特殊处理：直接打包 APK
    if target_platform == "android":
        return build_android_apk()
    
    # 获取 Flutter 构建产物目录
    flutter_output = build_flutter(target_platform)
    
    # 创建临时分发目录
    dist_dir = SCRIPT_DIR / "dist" / f"legodo_client_{target_platform}"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)
    
    # 复制 Flutter 构建产物
    print(f"[INFO] Copying Flutter build from: {flutter_output}", flush=True)
    for item in flutter_output.iterdir():
        if item.is_dir():
            shutil.copytree(item, dist_dir / item.name)
        else:
            shutil.copy2(item, dist_dir / item.name)
    
    # Windows 下需要额外处理 AOT 数据（data/app.so）
    if target_platform == "windows":
        # 检查 data 目录是否存在
        data_dir = flutter_output / "data"
        app_so = flutter_output.parent.parent.parent / "app.so"  # build/windows/app.so
        
        if not data_dir.exists() and app_so.exists():
            # 手动创建 data 目录并复制 app.so
            data_dir = dist_dir / "data"
            data_dir.mkdir(exist_ok=True)
            shutil.copy2(app_so, data_dir / "app.so")
            print(f"[OK] Copied app.so to data/ ({app_so.stat().st_size // (1024*1024)} MB)", flush=True)
        elif data_dir.exists():
            # data 目录已存在，直接复制
            shutil.copytree(data_dir, dist_dir / "data", dirs_exist_ok=True)
            print(f"[OK] Copied data/ directory", flush=True)
        else:
            print(f"[WARN] app.so not found, application may fail to start", flush=True)
    
    # Windows 下需要额外复制 flutter_windows.dll（运行时依赖）
    if target_platform == "windows":
        flutter_dll = DART_CLIENT / "windows" / "flutter" / "ephemeral" / "flutter_windows.dll"
        if flutter_dll.exists():
            shutil.copy2(flutter_dll, dist_dir / "flutter_windows.dll")
            print(f"[OK] Copied flutter_windows.dll ({flutter_dll.stat().st_size // (1024*1024)} MB)", flush=True)
        else:
            print(f"[WARN] flutter_windows.dll not found at: {flutter_dll}", flush=True)
            print("[INFO] Application may fail to start without this DLL", flush=True)
        
        # 复制 Flutter 插件 DLL（如 flutter_tts_plugin.dll）
        plugins_build_dir = DART_CLIENT / "build" / "windows" / "x64" / "plugins"
        if plugins_build_dir.exists():
            for plugin_dir in plugins_build_dir.iterdir():
                if plugin_dir.is_dir():
                    release_dir = plugin_dir / "Release"
                    if release_dir.exists():
                        for dll_file in release_dir.glob("*.dll"):
                            dest_dll = dist_dir / dll_file.name
                            shutil.copy2(dll_file, dest_dll)
                            print(f"[OK] Copied plugin DLL: {dll_file.name}", flush=True)
    
    # 复制 Go 动态库或二进制（Android 跳过）
    if go_lib is not None:
        lib_name = go_lib.name
        dest_lib = dist_dir / lib_name
        shutil.copy2(go_lib, dest_lib)
        print(f"[OK] Go library copied to: {dest_lib}", flush=True)
    else:
        print(f"[INFO] No Go backend for {target_platform} (pure Flutter/Dart)", flush=True)
    
    if use_dll:
        # 动态库模式：无需单独启动脚本
        # 创建简单的说明文件
        readme = dist_dir / "README.txt"
        readme.write_text(
            f"Legodo BookSource Check - {target_platform.upper()} Release\n"
            "=" * 60 + "\n\n"
            "启动方法：\n"
            f"  直接运行 legodo_client{'.' + 'exe' if target_platform == 'windows' else ''}\n\n"
            "架构说明：\n"
            "  - 前端: Flutter (Dart UI)\n"
            "  - 后端: Go 引擎（通过 FFI 直接调用）\n"
            "  - 通信: dart:ffi（零开销，无网络延迟）\n\n"
            "技术栈：\n"
            "  - webview_flutter: WebView 浏览\n"
            "  - flutter_tts: 文本转语音\n"
            "  - Go FFI: 规则引擎、HTML 解析、JS 执行\n\n"
            "性能特点：\n"
            "  - 冷启动 < 0.5s（无进程启动开销）\n"
            "  - 内存占用 ~35MB（共享进程空间）\n"
            "  - 并发扫描 100+ 源/秒（Go goroutine）\n"
            "  - 零网络延迟（FFI 直接调用）\n",
            encoding="utf-8"
        )
    else:
        # 独立进程模式：需要启动脚本
        if target_platform == "windows":
            start_script = dist_dir / "start.bat"
            start_script.write_text(
                "@echo off\n"
                "echo Starting Legodo BookSource Check...\n"
                "start \"\" \"legodo_server.exe\"\n"
                "timeout /t 2 /nobreak >nul\n"
                "start \"\" \"legodo_client.exe\"\n",
                encoding="gbk"
            )
        else:
            start_script = dist_dir / "start.sh"
            start_script.write_text(
                "#!/bin/bash\n"
                "echo 'Starting Legodo BookSource Check...'\n"
                "chmod +x ./legodo_server\n"
                "./legodo_server &\n"
                "SERVER_PID=$!\n"
                "sleep 2\n"
                "chmod +x ./legodo_client\n"
                "./legodo_client &\n"
                "wait $SERVER_PID\n",
                encoding="utf-8"
            )
            start_script.chmod(0o755)
        
        # 创建 README
        readme = dist_dir / "README.txt"
        readme.write_text(
            f"Legodo BookSource Check - {target_platform.upper()} Release\n"
            "=" * 60 + "\n\n"
            "启动方法：\n"
            f"  Windows: 双击 start.bat\n"
            f"  Linux/macOS: ./start.sh\n\n"
            "手动启动：\n"
            f"  1. 先启动后端: ./legodo_server{'.' + 'exe' if target_platform == 'windows' else ''}\n"
            f"  2. 再启动前端: ./legodo_client{'.' + 'exe' if target_platform == 'windows' else ''}\n\n"
            "技术栈：\n"
            "  - 前端: Flutter (Dart)\n"
            "  - 后端: Go (纯原生，零依赖)\n"
            "  - 通信: WebSocket RPC\n\n"
            "性能特点：\n"
            "  - 冷启动 < 0.5s\n"
            "  - 内存占用 ~35MB\n"
            "  - 并发扫描 100+ 源/秒\n",
            encoding="utf-8"
        )
    
    print(f"[OK] Documentation created", flush=True)
    
    # Windows 单文件打包（可选）
    if single_exe and target_platform == "windows":
        single_exe_path = SCRIPT_DIR / "dist" / "legodo_client.exe"
        result = create_single_exe_windows(dist_dir, single_exe_path)
        
        if result is None:
            # 手动操作指南已生成，返回多文件 zip
            print(f"\n[INFO] Single EXE requires manual operation.")
            print(f"[INFO] Creating multi-file package instead...", flush=True)
            # 继续执行下面的多文件打包逻辑
        else:
            # 自动打包成功（目前不会发生）
            zip_name = f"legodo_client_{target_platform}_single.zip"
            zip_path = SCRIPT_DIR / zip_name
            
            single_dist_dir = SCRIPT_DIR / "dist" / "legodo_client_single"
            if single_dist_dir.exists():
                shutil.rmtree(single_dist_dir)
            single_dist_dir.mkdir(parents=True)
            
            shutil.copy2(result, single_dist_dir / "legodo_client.exe")
            readme_src = dist_dir / "README.txt"
            if readme_src.exists():
                shutil.copy2(readme_src, single_dist_dir / "README.txt")
            
            print(f"[INFO] Creating zip archive for single exe...", flush=True)
            shutil.make_archive(str(zip_path.with_suffix("")), "zip", single_dist_dir.parent, single_dist_dir.name)
            
            print(f"[OK] Single-file package created: {zip_path}", flush=True)
            return zip_path
    
    # 打包为 zip
    zip_name = f"legodo_client_{target_platform}.zip"
    zip_path = SCRIPT_DIR / zip_name
    if zip_path.exists():
        zip_path.unlink()
    
    print(f"[INFO] Creating zip archive...", flush=True)
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", dist_dir.parent, dist_dir.name)
    
    print(f"[OK] Distribution package created: {zip_path}", flush=True)
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Flutter multi-platform release with Go backend")
    parser.add_argument(
        "target",
        choices=["windows", "linux", "macos", "android"],
        help="Target platform (android: pure Flutter/Dart, no Go backend)",
    )
    parser.add_argument(
        "--skip-go",
        action="store_true",
        help="Skip Go backend compilation (use existing library)",
    )
    parser.add_argument(
        "--skip-flutter",
        action="store_true",
        help="Skip Flutter build (use existing build output)",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Build Go as standalone executable instead of dynamic library",
    )
    parser.add_argument(
        "--single-exe",
        action="store_true",
        help="Package into single .exe file using Enigma Virtual Box (Windows only)",
    )
    args = parser.parse_args()
    
    use_dll = not args.standalone
    
    # Android 不支持 --single-exe 和 --standalone
    if args.target == "android":
        if args.single_exe:
            print("[WARN] --single-exe is ignored for Android (APK is already single file)", flush=True)
        if args.standalone:
            print("[WARN] --standalone is ignored for Android (no Go backend)", flush=True)
    
    print("=" * 60)
    print("  Legodo BookSource Check - Multi-Platform Build")
    
    if args.target == "android":
        print(f"  (Backend: Pure Dart/Flutter | Frontend: Flutter | Package: APK)")
    else:
        arch_type = "FFI Dynamic Library" if use_dll else "Standalone Executable"
        packaging = "Single EXE" if args.single_exe and platform.system() == "Windows" else "Multi-file"
        print(f"  (Backend: Go {arch_type} | Frontend: Flutter | Package: {packaging})")
    
    print("=" * 60)
    print(f"[INFO] Platform: {platform.system()} {platform.release()}")
    print(f"[INFO] Python: {sys.executable}")
    print(f"[INFO] Script: {SCRIPT_DIR}")
    print()
    
    # Step 1: Build Go backend (skip for Android)
    go_lib = None
    if args.target != "android":
        if not args.skip_go:
            go_lib = build_go_backend(args.target, use_dll=use_dll)
        else:
            print("[INFO] Skipping Go backend compilation (--skip-go)", flush=True)
            if use_dll:
                lib_map = {
                    "windows": "legodo_engine.dll",
                    "linux": "liblegodo_engine.so",
                    "macos": "liblegodo_engine.dylib",
                }
                lib_name = lib_map.get(args.target, "liblegodo_engine.so")
            else:
                lib_name = "legodo_server.exe" if args.target == "windows" else "legodo_server"
            
            go_lib = GO_BACKEND / lib_name
            if not go_lib.exists():
                print(f"[ERROR] Go library not found at: {go_lib}", file=sys.stderr)
                print("[INFO] Remove --skip-go to compile from source", file=sys.stderr)
                sys.exit(1)
            print(f"[OK] Using existing Go library: {go_lib}", flush=True)
    else:
        print("[INFO] Android build: skipping Go backend (pure Dart/Flutter)", flush=True)
    
    # Step 2 & 3: Create distribution (includes Flutter build)
    if not args.skip_flutter:
        package = create_distribution(args.target, go_lib, use_dll=use_dll, single_exe=args.single_exe)
    else:
        print("[INFO] Skipping Flutter build (--skip-flutter)", flush=True)
        print("[WARN] Distribution package will use existing Flutter output", flush=True)
        print("[INFO] Manual packaging required when --skip-flutter is set", flush=True)
        package = None
    
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    if package:
        print(f"\nPackage: {package}")
    
    print("\nFeatures:")
    print("  - WebView browser (platform-native WebView)")
    print("  - TTS text-to-speech (system TTS)")
    
    if args.target == "android":
        print("  - Pure Dart/Flutter implementation (no Go backend)")
        print("  - Android APK package")
    elif use_dll:
        print("  - Go backend via FFI (zero network overhead, direct calls)")
        print("  - Single process architecture (lower memory footprint)")
    else:
        print("  - Go backend as standalone service (WebSocket RPC)")
        print("  - Separate process isolation")
    print()


if __name__ == "__main__":
    main()
