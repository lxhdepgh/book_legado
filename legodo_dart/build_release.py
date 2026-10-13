#!/usr/bin/env python3
"""一键打包 Flutter 多平台 Release（含 Python 后端）。

WebView + TTS 已改为 Dart 原生实现，但核心引擎仍需 Python。
支持平台：windows / linux / macos / android / ios

环境自动检测与修复：
- 自动查找 flutter/python/pyinstaller 命令
- 跨平台路径兼容（Windows/Linux/macOS）
- 自动定位 spec 文件和后端源码

注意：本脚本完全独立于 legodo_flet，所有资源都在 legodo_dart/ 内。
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


def find_spec_file() -> Path | None:
    """查找 LegadoReader.spec 文件（仅在当前目录搜索）。"""
    candidate = SCRIPT_DIR / "LegadoReader.spec"
    return candidate if candidate.exists() else None


def find_command(cmd_name: str, extra_paths: list[Path] | None = None) -> Path | None:
    """在 PATH 和额外路径中查找可执行文件。
    
    Args:
        cmd_name: 命令名（如 'flutter', 'python'）
        extra_paths: 额外的搜索路径列表
    
    Returns:
        找到的可执行文件路径，或 None
    """
    # 1. 先试系统 PATH
    if path := shutil.which(cmd_name):
        return Path(path)
    
    # 2. 尝试 conda 环境（优先当前激活的环境）
    conda_prefix = os.environ.get("CONDA_PREFIX") or os.environ.get("CONDA_DEFAULT_ENV")
    if conda_prefix and not conda_prefix.endswith("base"):
        if platform.system() == "Windows":
            conda_python = Path(conda_prefix) / f"{cmd_name}.exe"
        else:
            conda_python = Path(conda_prefix) / "bin" / cmd_name
        if conda_python.exists():
            return conda_python
    
    # 3. 尝试常见安装位置
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
        # Python 常见位置
        if cmd_name in ("python", "python3"):
            search_paths.extend([
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / cmd_name,
                Path("C:/Python311") / f"{cmd_name}.exe",
                Path("C:/Python312") / f"{cmd_name}.exe",
                # Conda 环境
                Path("D:/env_manger/minicaoda/envs/legodo_booksource_check") / f"{cmd_name}.exe",
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


def ensure_pyinstaller() -> Path:
    """确保 PyInstaller 可用。"""
    pyinstaller = find_command("pyinstaller")
    if pyinstaller:
        return pyinstaller
    
    # 尝试通过 python -m PyInstaller
    python = find_command("python") or find_command("python3")
    if python:
        try:
            result = subprocess.run(
                [str(python), "-m", "PyInstaller", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                print(f"[OK] Found PyInstaller via {python}", flush=True)
                return python  # 返回 python，调用时用 python -m PyInstaller
        except Exception:
            pass
    
    print("[ERROR] PyInstaller not found", file=sys.stderr)
    print("\n[INFO] Install with:", file=sys.stderr)
    print("  pip install pyinstaller", file=sys.stderr)
    print("  or: conda install pyinstaller", file=sys.stderr)
    sys.exit(1)


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


def build_backend(target: str = "windows") -> None:
    """打包 Python 后端为可执行文件。
    
    Android 需要使用 Chaquopy 或 Pydroid3 打包为 .so 库，
    当前简化方案：复用 Windows exe（需后续完善）。
    """
    print(f"\n[1/4] Packing Python backend for {target}...", flush=True)
    
    if target == "android":
        print("[WARN] Android backend packaging requires Chaquopy or custom setup", flush=True)
        print("[INFO] Please configure Python backend manually for Android:", flush=True)
        print("  - Option 1: Use Chaquopy plugin in build.gradle", flush=True)
        print("  - Option 2: Bundle Python runtime with APK", flush=True)
        backend_dir = DART_CLIENT / "backend"
        if backend_dir.exists():
            print(f"[OK] Found existing backend at: {backend_dir}", flush=True)
        return
    
    # 删除旧的后端目录，强制重新打包
    backend_dir = DART_CLIENT / "backend"
    if backend_dir.exists():
        print(f"[INFO] Removing old backend directory: {backend_dir}", flush=True)
        try:
            # Windows 上可能需要多次尝试或特殊处理
            for attempt in range(3):
                try:
                    shutil.rmtree(backend_dir)
                    print(f"[OK] Old backend removed", flush=True)
                    break
                except PermissionError as e:
                    if attempt < 2:
                        print(f"[WARN] Attempt {attempt + 1} failed, retrying...", flush=True)
                        import time
                        time.sleep(1)
                    else:
                        raise
        except PermissionError as e:
            print(f"[WARN] Cannot remove old backend (file locked): {e}", flush=True)
            print("[INFO] Trying to rename and create new directory...", flush=True)
            # 重命名旧目录并创建新目录
            import tempfile
            temp_name = backend_dir.parent / f"backend_old_{tempfile.mkdtemp().split('/')[-1]}"
            try:
                backend_dir.rename(temp_name)
                print(f"[OK] Renamed old backend to: {temp_name}", flush=True)
            except Exception as rename_err:
                print(f"[ERROR] Cannot rename either: {rename_err}", flush=True)
                print("[INFO] Please manually delete or rename the backend directory", flush=True)
                sys.exit(1)
    
    # 查找 spec 文件
    spec_file = find_spec_file()
    if not spec_file:
        print("[WARN] LegadoReader.spec not found at:", flush=True)
        print(f"  {SCRIPT_DIR}/LegadoReader.spec", flush=True)
        print("[INFO] Skipping backend packaging", flush=True)
        return
    
    print(f"[INFO] Using spec file: {spec_file}", flush=True)
    
    # 获取 PyInstaller（优先使用 python -m 方式）
    pyinstaller_cmd = ensure_pyinstaller()
    
    # 始终使用 python -m PyInstaller 以确保使用正确的 conda 环境
    python_cmd = find_command("python") or find_command("python3")
    if python_cmd:
        cmd = [str(python_cmd), "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_file)]
    elif pyinstaller_cmd.name.startswith("python"):
        cmd = [str(pyinstaller_cmd), "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_file)]
    else:
        cmd = [str(pyinstaller_cmd), "--clean", "--noconfirm", str(spec_file)]
    
    run(cmd, cwd=SCRIPT_DIR)
    
    # 复制 PyInstaller 输出到 backend 目录
    dist_dir = SCRIPT_DIR / "dist" / "LegadoReader"
    if not dist_dir.exists():
        print(f"[ERROR] PyInstaller output not found at: {dist_dir}", flush=True)
        sys.exit(1)
    
    backend_dir = DART_CLIENT / "backend"
    if backend_dir.exists():
        shutil.rmtree(backend_dir)
    shutil.copytree(dist_dir, backend_dir)
    print(f"[OK] Backend packed to: {backend_dir}", flush=True)


def build_flutter(target: str, single_file: bool = False) -> Path:
    """构建 Flutter 应用并返回输出目录。
    
    Args:
        target: 目标平台 (windows/linux/macos/apk/ipa)
        single_file: Windows 专用，是否打包为单文件 exe（需要 Enigma Virtual Box）
    """
    print(f"\n[2/4] Building Flutter {target} Release...", flush=True)
    
    # 确保 flutter 可用
    flutter = ensure_flutter()
    
    backend_exe = DART_CLIENT / "backend" / "LegadoReader.exe"
    
    cmd = [str(flutter), "build", target, "--release"]
    
    # 注意：不设置 LEGODO_PYTHON/LEGODO_MAIN，让运行时使用相对路径查找
    # bootstrap.dart 会从 Platform.resolvedExecutable 同级目录查找 backend/
    if backend_exe.exists():
        print(f"[INFO] Backend found, will use relative path at runtime", flush=True)
    else:
        print("[WARN] Backend exe not found, runtime will fail without it", flush=True)
    
    run(cmd, cwd=DART_CLIENT)
    
    # 确定输出目录
    output_map = {
        "windows": DART_CLIENT / "build" / "windows" / "x64" / "runner" / "Release",
        "linux": DART_CLIENT / "build" / "linux" / "x64" / "release" / "bundle",
        "macos": DART_CLIENT / "build" / "macos" / "Build" / "Products" / "Release" / "legodo_client.app",
        "apk": DART_CLIENT / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk",
        "ipa": DART_CLIENT / "build" / "ios" / "iphoneos" / "Release-iphoneos" / "legodo_client.app",
    }
    output_dir = output_map.get(target)
    if not output_dir or not output_dir.exists():
        print(f"[ERROR] Output directory not found for target: {target}", file=sys.stderr)
        print(f"[INFO] Expected: {output_dir}", file=sys.stderr)
        sys.exit(1)
    
    print(f"[OK] Build output: {output_dir}", flush=True)
    
    # Windows 单文件打包（可选）
    if target == "windows" and single_file:
        output_dir = pack_single_file_windows(output_dir)
    
    return output_dir


def find_enigma_virtual_box() -> Path | None:
    """查找 Enigma Virtual Box 可执行文件。"""
    candidates = [
        Path("C:/Program Files/Enigma Virtual Box/enigmavbconsole.exe"),
        Path("C:/Program Files (x86)/Enigma Virtual Box/enigmavbconsole.exe"),
        Path(os.environ.get("PROGRAMFILES", "")) / "Enigma Virtual Box/enigmavbconsole.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Enigma Virtual Box/enigmavbconsole.exe",
    ]
    
    for candidate in candidates:
        if candidate.exists():
            return candidate
    
    # 尝试 PATH
    if path := shutil.which("enigmavbconsole.exe"):
        return Path(path)
    
    return None


def pack_single_file_windows(release_dir: Path) -> Path:
    """使用 Enigma Virtual Box 将 Windows 发布目录打包为单文件 exe。
    
    Args:
        release_dir: Flutter build windows 输出的 Release 目录
    
    Returns:
        单文件 exe 的路径
    """
    print("\n[Packing] Creating single-file executable with Enigma Virtual Box...")
    
    enigma_exe = find_enigma_virtual_box()
    if not enigma_exe:
        print("[WARN] Enigma Virtual Box not found. Skipping single-file packaging.")
        print("[INFO] Install from: https://enigmaprotector.com/en/products/virtualbox.html")
        print("[INFO] Or set ENIGMA_VB_PATH environment variable")
        return release_dir
    
    print(f"[INFO] Using Enigma Virtual Box: {enigma_exe}")
    
    # 主 exe 文件
    main_exe = release_dir / "legodo_client.exe"
    if not main_exe.exists():
        print(f"[WARN] Main exe not found: {main_exe}")
        return release_dir
    
    # 输出单文件 exe
    output_exe = DART_CLIENT / "dist" / "legodo_client_single.exe"
    output_exe.parent.mkdir(parents=True, exist_ok=True)
    
    # 创建 Enigma Virtual Box 配置文件
    config_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<enigmavb>
    <input>{main_exe}</input>
    <output>{output_exe}</output>
    <options>
        <compress>true</compress>
        <upx>false</upx>
        <protect_import_table>false</protect_import_table>
    </options>
    <files>
"""
    
    # 添加所有 DLL 文件
    for dll in release_dir.glob("*.dll"):
        rel_path = dll.relative_to(release_dir)
        config_content += f'        <file source="{dll}" dest="{rel_path}" />\n'
    
    # 添加 data 目录
    data_dir = release_dir / "data"
    if data_dir.exists():
        config_content += f'        <folder source="{data_dir}" dest="data" />\n'
    
    # 添加 backend 目录
    backend_dir = release_dir / "backend"
    if backend_dir.exists():
        config_content += f'        <folder source="{backend_dir}" dest="backend" />\n'
    
    config_content += """    </files>
</enigmavb>
"""
    
    config_file = DART_CLIENT / "dist" / "enigma_config.xml"
    config_file.write_text(config_content, encoding="utf-8")
    
    # 运行 Enigma Virtual Box
    cmd = [str(enigma_exe), str(config_file)]
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时
            encoding="utf-8",
            errors="replace"
        )
        
        if result.returncode != 0:
            print(f"[ERROR] Enigma Virtual Box failed:")
            if result.stderr:
                print(result.stderr)
            return release_dir
        
        if output_exe.exists():
            size_mb = output_exe.stat().st_size / (1024 * 1024)
            print(f"[OK] Single-file exe created: {output_exe} ({size_mb:.1f} MB)")
            return output_exe.parent
        else:
            print("[ERROR] Output exe not created")
            return release_dir
            
    except subprocess.TimeoutExpired:
        print("[ERROR] Enigma Virtual Box timed out (5 minutes)")
        return release_dir
    except Exception as e:
        print(f"[ERROR] Enigma Virtual Box failed: {e}")
        return release_dir


def copy_backend(output_dir: Path) -> None:
    """复制 Python 后端到输出目录（仅桌面平台）。"""
    print(f"\n[3/4] Copying backend to output directory...", flush=True)
    
    backend_dir = DART_CLIENT / "backend"
    if not backend_dir.exists():
        print("[WARN] Backend directory not found, skipping copy", flush=True)
        return
    
    # 桌面平台需要后端，移动端不需要
    dest = output_dir / "backend"
    if dest.exists():
        # Windows 上 .pyd/.dll 可能被占用，尝试强制删除
        try:
            shutil.rmtree(dest)
        except PermissionError as e:
            print(f"[WARN] Cannot remove old backend (file locked): {e}", flush=True)
            print("[INFO] Skipping copy, using existing backend in output", flush=True)
            return
    
    shutil.copytree(backend_dir, dest)
    print(f"[OK] Backend copied to: {dest}", flush=True)


def create_package(output_dir: Path, target: str) -> Path:
    """创建发布包（zip/apk/ipa）。"""
    print(f"\n[4/4] Generating release package...", flush=True)
    
    if target == "apk":
        # APK 已经是单个文件
        apk_file = output_dir
        print(f"[OK] APK ready: {apk_file}", flush=True)
        return apk_file
    
    if target == "ipa":
        print(f"[OK] IPA ready: {output_dir}", flush=True)
        return output_dir
    
    # 桌面平台：创建 zip
    zip_name = f"legodo_client_{target}.zip"
    zip_path = SCRIPT_DIR / zip_name
    if zip_path.exists():
        zip_path.unlink()
    
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", output_dir.parent, output_dir.name)
    print(f"[OK] Package created: {zip_path}", flush=True)
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Flutter multi-platform release")
    parser.add_argument(
        "target",
        choices=["windows", "linux", "macos", "apk", "ipa"],
        help="Target platform",
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="Windows only: pack into single-file exe using Enigma Virtual Box",
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("  Legodo BookSource Check - Multi-Platform Build")
    print("  (WebView/TTS: Pure Dart)")
    print("=" * 60)
    print(f"[INFO] Platform: {platform.system()} {platform.release()}")
    print(f"[INFO] Python: {sys.executable}")
    print(f"[INFO] Script: {SCRIPT_DIR}")
    if args.target == "windows" and args.single_file:
        print(f"[INFO] Mode: single-file exe (requires Enigma Virtual Box)")
    print()
    
    # Step 1: Pack Python backend from source
    if args.target in ("windows", "linux", "macos", "android"):
        build_backend(args.target)
    
    # Step 2: Build Flutter
    output_dir = build_flutter(args.target, single_file=args.single_file)
    
    # Step 3: Copy backend to output（单文件模式跳过，因为已经打包进去了）
    if args.target in ("windows", "linux", "macos", "android") and not args.single_file:
        copy_backend(output_dir)
    
    # Step 4: Create release package
    package = create_package(output_dir, args.target)
    
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    print(f"\nPackage: {package}")
    if args.target in ("windows", "linux", "macos", "android"):
        print("\nFeatures:")
        print("  - WebView browser (platform-native WebView)")
        print("  - TTS text-to-speech (system TTS)")
        print("  - Python backend for core engine services")
        if args.target == "windows" and args.single_file:
            print("  - Single-file executable (no installation required)")
    print()


if __name__ == "__main__":
    main()
