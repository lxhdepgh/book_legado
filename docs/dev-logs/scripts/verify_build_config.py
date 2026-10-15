#!/usr/bin/env python3
"""验证构建脚本配置是否正确。

检查项：
1. Go FFI 源码结构
2. 各平台编译配置
3. 依赖文件完整性
4. Enigma Virtual Box 安装状态
"""
import platform
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
GO_BACKEND = SCRIPT_DIR / "backend_go"
GO_FFI_SRC = GO_BACKEND / "pkg" / "ffi" / "engine.go"


def check_ansi_color():
    """检查是否支持 ANSI 颜色。"""
    if platform.system() == "Windows":
        # Windows 10+ 支持 ANSI
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return True


# ANSI 颜色代码
USE_COLOR = check_ansi_color()
GREEN = "\033[92m" if USE_COLOR else ""
RED = "\033[91m" if USE_COLOR else ""
YELLOW = "\033[93m" if USE_COLOR else ""
RESET = "\033[0m" if USE_COLOR else ""


def print_ok(msg: str):
    print(f"{GREEN}[OK]{RESET} {msg}")


def print_error(msg: str):
    print(f"{RED}[ERROR]{RESET} {msg}")


def print_warn(msg: str):
    print(f"{YELLOW}[WARN]{RESET} {msg}")


def check_go_ffi_source():
    """检查 Go FFI 源码结构。"""
    print("\n[1/5] Checking Go FFI source code...")
    
    if not GO_FFI_SRC.exists():
        print_error(f"Go FFI source not found: {GO_FFI_SRC}")
        return False
    
    content = GO_FFI_SRC.read_text(encoding="utf-8")
    
    # 检查必需的元素
    checks = [
        ("package main", "Package declaration"),
        ('import "C"', "CGO import"),
        ("func main()", "Main function (required for c-shared)"),
        ("//export Initialize", "Initialize export"),
        ("//export SearchBooks", "SearchBooks export"),
        ("//export GetChapters", "GetChapters export"),
        ("//export GetContent", "GetContent export"),
        ("//export Cleanup", "Cleanup export"),
    ]
    
    all_ok = True
    for pattern, description in checks:
        if pattern in content:
            print_ok(f"  {description}: found")
        else:
            print_error(f"  {description}: MISSING")
            all_ok = False
    
    return all_ok


def check_compiled_libraries():
    """检查已编译的库文件。"""
    print("\n[2/5] Checking compiled libraries...")
    
    dll = GO_BACKEND / "legodo_engine.dll"
    so = GO_BACKEND / "liblegodo_engine.so"
    dylib = GO_BACKEND / "liblegodo_engine.dylib"
    
    found_any = False
    
    if dll.exists():
        size_mb = dll.stat().st_size / (1024 * 1024)
        print_ok(f"  Windows DLL: {dll.name} ({size_mb:.1f} MB)")
        found_any = True
    
    if so.exists():
        size_mb = so.stat().st_size / (1024 * 1024)
        print_ok(f"  Linux SO: {so.name} ({size_mb:.1f} MB)")
        found_any = True
    
    if dylib.exists():
        size_mb = dylib.stat().st_size / (1024 * 1024)
        print_ok(f"  macOS DYLIB: {dylib.name} ({size_mb:.1f} MB)")
        found_any = True
    
    if not found_any:
        print_warn("  No compiled libraries found (run build to create them)")
    
    return found_any


def check_flutter_dependencies():
    """检查 Flutter 依赖。"""
    print("\n[3/5] Checking Flutter dependencies...")
    
    pubspec = SCRIPT_DIR / "pubspec.yaml"
    if not pubspec.exists():
        print_error(f"pubspec.yaml not found: {pubspec}")
        return False
    
    content = pubspec.read_text(encoding="utf-8")
    
    required_deps = [
        "webview_flutter",
        "flutter_tts",
        "web_socket_channel",
    ]
    
    all_ok = True
    for dep in required_deps:
        if dep in content:
            print_ok(f"  Dependency: {dep}")
        else:
            print_error(f"  Missing dependency: {dep}")
            all_ok = False
    
    return all_ok


def check_windows_dll_files():
    """检查 Windows DLL 文件（Flutter 运行时）。"""
    if platform.system() != "Windows":
        print("\n[4/5] Skipping Windows DLL check (not on Windows)")
        return True
    
    print("\n[4/5] Checking Windows runtime DLLs...")
    
    flutter_dll = SCRIPT_DIR / "windows" / "flutter" / "ephemeral" / "flutter_windows.dll"
    
    if flutter_dll.exists():
        size_mb = flutter_dll.stat().st_size / (1024 * 1024)
        print_ok(f"  flutter_windows.dll: {size_mb:.1f} MB")
    else:
        print_warn(f"  flutter_windows.dll not found (will be created during build)")
    
    return True


def check_enigma_virtual_box():
    """检查 Enigma Virtual Box 安装。"""
    if platform.system() != "Windows":
        print("\n[5/5] Skipping Enigma Virtual Box check (not on Windows)")
        return True
    
    print("\n[5/5] Checking Enigma Virtual Box...")
    
    search_paths = [
        Path("C:/Program Files/Enigma Virtual Box/enigmavbconsole.exe"),
        Path("C:/Program Files (x86)/Enigma Virtual Box/enigmavbconsole.exe"),
    ]
    
    for path in search_paths:
        if path.exists():
            print_ok(f"  Found: {path}")
            return True
    
    print_warn("  Enigma Virtual Box not installed")
    print_warn("  Install it to enable single-file packaging:")
    print_warn("    .\\install_enigma_vbox.ps1")
    
    return False


def print_summary(results: dict[str, bool]):
    """打印总结。"""
    print("\n" + "=" * 60)
    print("  Build Configuration Summary")
    print("=" * 60)
    
    for check, passed in results.items():
        status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        print(f"  {check:.<40} {status}")
    
    all_passed = all(results.values())
    
    print("\n" + "=" * 60)
    if all_passed:
        print(f"{GREEN}  All checks passed! Ready to build.{RESET}")
    else:
        print(f"{RED}  Some checks failed. Please fix the issues above.{RESET}")
    print("=" * 60)
    
    # 打印构建命令建议
    print("\nRecommended build commands:")
    print("  Windows (single exe):  python build_release.py windows --single-exe")
    print("  Windows (multi-file):  python build_release.py windows")
    print("  Linux:                 python build_release.py linux")
    print("  macOS:                 python build_release.py macos")
    print("  Android:               python build_release.py android")
    print()


def main():
    print("=" * 60)
    print("  Legodo BookSource Check - Build Configuration Verifier")
    print("=" * 60)
    
    results = {
        "Go FFI Source Structure": check_go_ffi_source(),
        "Compiled Libraries": check_compiled_libraries(),
        "Flutter Dependencies": check_flutter_dependencies(),
        "Windows Runtime DLLs": check_windows_dll_files(),
        "Enigma Virtual Box": check_enigma_virtual_box(),
    }
    
    print_summary(results)
    
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
