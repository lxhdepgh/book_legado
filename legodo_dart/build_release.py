#!/usr/bin/env python3
"""一键打包 Flutter + Go 后端 Release（单文件分发）。

架构：Flutter (Dart UI) + Go 独立进程 (WebSocket RPC)
分发包：单文件 .exe（Go 二进制嵌入 Flutter 可执行文件中）

用法:
    python build_release.py windows       # Windows 单文件 EXE
    python build_release.py linux         # Linux
    python build_release.py macos         # macOS
    python build_release.py android       # Android APK
    python build_release.py windows --skip-go  # 跳过 Go 编译
"""
from __future__ import annotations

import argparse
import base64
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DART_CLIENT = SCRIPT_DIR
GO_BACKEND = SCRIPT_DIR / "backend_go"


def find_command(cmd_name: str) -> Path | None:
    if path := shutil.which(cmd_name):
        return Path(path)
    search_paths = []
    if platform.system() == "Windows":
        search_paths.extend([
            Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Programs" / cmd_name,
            Path("C:/Program Files") / cmd_name,
            Path("C:/Program Files (x86)") / cmd_name,
        ])
        if cmd_name == "flutter":
            search_paths.extend([
                Path("D:/env_manger/flutter/bin/flutter.bat"),
                Path("D:/flutter/bin/flutter.bat"),
                Path("C:/flutter/bin/flutter.bat"),
            ])
        if cmd_name == "go":
            search_paths.extend([
                Path("C:/Go/bin/go.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Go" / "bin" / "go.exe",
            ])
    elif platform.system() == "Linux":
        search_paths.extend([Path("/usr/local/bin") / cmd_name, Path("/usr/bin") / cmd_name])
    elif platform.system() == "Darwin":
        search_paths.extend([Path("/usr/local/bin") / cmd_name, Path("/opt/homebrew/bin") / cmd_name])
    for candidate in search_paths:
        if candidate.exists():
            return candidate
    return None


def ensure_flutter() -> Path:
    flutter = find_command("flutter")
    if not flutter:
        print("[ERROR] Flutter command not found in PATH", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run(
            [str(flutter), "--version"], capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            version_line = result.stdout.strip().split("\n")[0]
            print(f"[OK] Found Flutter: {version_line}", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Flutter version: {e}", flush=True)
    return flutter


def ensure_go() -> Path:
    go = find_command("go")
    if not go:
        print("[ERROR] Go command not found in PATH", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run(
            [str(go), "version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"[OK] Found Go: {result.stdout.strip()}", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Go version: {e}", flush=True)
    return go


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd, cwd=cwd or SCRIPT_DIR, env=merged_env, check=True,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    print(f"  {line}")
    except FileNotFoundError:
        print(f"[ERROR] Command not found: {cmd[0]}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)


def build_go_backend(target_platform: str) -> tuple[Path, str]:
    """编译 Go 后端并返回 (输出路径, exe 名)。Android 不编译 Go。"""
    if target_platform == "android":
        return (Path(), "")

    print(f"\n[1/4] Building Go backend for {target_platform}...", flush=True)
    if not GO_BACKEND.exists():
        print(f"[ERROR] Go backend directory not found at: {GO_BACKEND}", file=sys.stderr)
        sys.exit(1)

    go = ensure_go()
    goos_map = {"windows": "windows", "linux": "linux", "macos": "darwin"}
    goarch_map = {"windows": "amd64", "linux": "amd64", "macos": "arm64"}
    goos = goos_map.get(target_platform)
    goarch = goarch_map.get(target_platform)
    if not goos or not goarch:
        print(f"[ERROR] Unsupported target platform for Go: {target_platform}", file=sys.stderr)
        sys.exit(1)

    env = os.environ.copy()
    env["GOOS"] = goos
    env["GOARCH"] = goarch
    env["CGO_ENABLED"] = "0"

    exe_name = "legodo_server.exe" if target_platform == "windows" else "legodo_server"
    output_path = GO_BACKEND / exe_name
    build_cmd = [str(go), "build", "-ldflags=-s -w", "-o", str(output_path), "./cmd/server/"]
    run(build_cmd, cwd=GO_BACKEND, env=env)

    if not output_path.exists():
        print(f"[ERROR] Go server not created at: {output_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Go backend: {output_path} ({output_path.stat().st_size // 1024} KB)", flush=True)
    return (output_path, exe_name)


def embed_go_as_define(go_bin_path: Path, exe_name: str, dart_dir: Path) -> str:
    """读取 Go 二进制文件，转 base64，通过环境变量传入 Flutter 构建。

    返回 --dart-define 参数值（GO_BACKEND_BASE64=<base64>），空串表示无嵌入式数据。
    """
    raw = go_bin_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    define_value = f"GO_BACKEND_BASE64={b64}"
    print(f"[OK] Embedded Go binary: {len(raw) / (1024*1024):.2f} MB → base64", flush=True)
    return define_value


def build_flutter_with_embed(target: str, go_b64_define: str) -> Path:
    """构建 Flutter 应用，传入 Go 二进制作为编译期常量。"""
    print(f"\n[2/4] Building Flutter {target} Release...", flush=True)
    flutter = ensure_flutter()

    env = os.environ.copy()
    if platform.system() == "Windows":
        nuget_paths = [
            Path.home() / "bin",
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/nuget"),
        ]
        for p in nuget_paths:
            if (p / "nuget.exe").exists():
                current_path = env.get("PATH", "")
                if str(p) not in current_path:
                    env["PATH"] = f"{str(p)};{current_path}"
                break

    cmd = [str(flutter), "build", target, "--release"]
    if go_b64_define:
        cmd.append(f"--dart-define={go_b64_define}")

    try:
        run(cmd, cwd=dart_dir, env=env)
    except SystemExit as e:
        if platform.system() == "Windows" and target == "windows":
            output_test = dart_dir / "build" / "windows" / "x64" / "runner" / "Release" / "legodo_client.exe"
            if output_test.exists():
                print("[WARN] Flutter build reported error but executable was created, continuing...", flush=True)
            else:
                raise
        else:
            raise

    output_map = {
        "windows": dart_dir / "build" / "windows" / "x64" / "runner" / "Release",
        "linux": dart_dir / "build" / "linux" / "x64" / "release" / "bundle",
        "macos": dart_dir / "build" / "macos" / "Build" / "Products" / "Release" / "legodo_client.app",
    }
    output_dir = output_map.get(target)
    if not output_dir or not output_dir.exists():
        print(f"[ERROR] Output directory not found: {output_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Build output: {output_dir}", flush=True)
    return output_dir


def build_android_apk() -> Path:
    """构建 Android APK（纯 Dart/Flutter，不含 Go 后端）。"""
    print(f"\n[2/4] Building Android APK...", flush=True)
    flutter = ensure_flutter()

    java_home = os.environ.get("JAVA_HOME", "")
    if not java_home:
        print("[WARN] JAVA_HOME not set, attempting to find Java...", flush=True)
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
        print("Install JDK 17+ and set JAVA_HOME. Download: https://adoptium.net/", file=sys.stderr)
        sys.exit(1)

    cmd = [str(flutter), "build", "apk", "--release"]
    try:
        run(cmd, cwd=DART_CLIENT)
    except SystemExit:
        print("[ERROR] Flutter APK build failed", file=sys.stderr)
        raise

    apk_dir = DART_CLIENT / "build" / "app" / "outputs" / "flutter-apk"
    if not apk_dir.exists():
        print(f"[ERROR] APK output directory not found: {apk_dir}", file=sys.stderr)
        sys.exit(1)

    apks = list(apk_dir.glob("*.apk"))
    if not apks:
        print(f"[ERROR] No APK files found in: {apk_dir}", file=sys.stderr)
        sys.exit(1)

    latest_apk = max(apks, key=lambda p: p.stat().st_mtime)
    dist_dir = SCRIPT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)
    dest_apk = dist_dir / latest_apk.name
    shutil.copy2(latest_apk, dest_apk)
    print(f"[OK] APK copied to: {dest_apk} ({latest_apk.stat().st_size / (1024*1024):.1f} MB)", flush=True)
    return dest_apk


def create_distribution_windows(target_platform: str, flutter_output: Path) -> Path:
    """创建 Windows 分发包（复制产物 + README）。"""
    print(f"\n[3/4] Creating distribution package for {target_platform}...", flush=True)

    dist_dir = SCRIPT_DIR / "dist" / f"legodo_{target_platform}"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    # 复制 Flutter 产物
    for item in flutter_output.iterdir():
        if item.is_dir():
            shutil.copytree(item, dist_dir / item.name)
        else:
            shutil.copy2(item, dist_dir / item.name)

    # 复制 AOT data 和运行时依赖
    data_dir = flutter_output / "data"
    app_so = flutter_output.parent.parent.parent / "app.so"
    if not data_dir.exists() and app_so.exists():
        data_dir = dist_dir / "data"
        data_dir.mkdir(exist_ok=True)
        shutil.copy2(app_so, data_dir / "app.so")
        print(f"[OK] Copied app.so ({app_so.stat().st_size // (1024*1024)} MB)", flush=True)
    elif data_dir.exists():
        shutil.copytree(data_dir, dist_dir / "data", dirs_exist_ok=True)
        print("[OK] Copied data/ directory", flush=True)

    flutter_dll = DART_CLIENT / "windows" / "flutter" / "ephemeral" / "flutter_windows.dll"
    if flutter_dll.exists():
        shutil.copy2(flutter_dll, dist_dir / "flutter_windows.dll")
        print("[OK] Copied flutter_windows.dll", flush=True)

    plugins_build_dir = DART_CLIENT / "build" / "windows" / "x64" / "plugins"
    if plugins_build_dir.exists():
        for plugin_dir in plugins_build_dir.iterdir():
            release_dir = plugin_dir / "Release"
            if release_dir.exists():
                for dll_file in release_dir.glob("*.dll"):
                    shutil.copy2(dll_file, dist_dir / dll_file.name)
                    print(f"[OK] Copied plugin: {dll_file.name}", flush=True)

    # 注意：Go 二进制已嵌入 legodo_client.exe，不需要单独复制！

    # start.bat 仅用于说明（实际上一键双击即可）
    start_script = dist_dir / "start.bat"
    start_script.write_text(
        "@echo off\n"
        "echo Starting Legodo BookSource Check...\n"
        "start \"\" \"legodo_client.exe\"\n",
        encoding="gbk"
    )

    readme = dist_dir / "README.txt"
    readme.write_text(
        "Legodo BookSource Check - Windows Release\n"
        "=" * 60 + "\n\n"
        "启动方法:\n"
        "  直接双击 legodo_client.exe\n\n"
        "架构:\n"
        "  - Flutter (Dart UI) + Go 后端（嵌入单一 EXE）\n"
        "  - Go 后端在首次运行时自动提取到同目录\n"
        "  - 通信: WebSocket RPC (:8080)\n"
        "  - 退出时自动清理生成的文件\n\n"
        "功能:\n"
        "  - webview_flutter: 网页浏览/试读\n"
        "  - flutter_tts: 系统 TTS 朗读\n"
        "  - Go 规则引擎、HTML 解析、JS 执行\n"
        "  - 性能: 并发扫描 100+ 源/秒\n",
        encoding="utf-8"
    )

    # 打包为 zip
    zip_path = SCRIPT_DIR / f"legodo_{target_platform}.zip"
    if zip_path.exists():
        zip_path.unlink()
    print("[INFO] Creating zip archive...", flush=True)
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", dist_dir.parent, dist_dir.name)

    print(f"[OK] Distribution package: {zip_path}", flush=True)
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Flutter + Go backend release (single-file EXE)")
    parser.add_argument("target", choices=["windows", "linux", "macos", "android"], help="Target platform")
    parser.add_argument("--skip-go", action="store_true", help="Skip Go backend compilation")
    parser.add_argument("--skip-flutter", action="store_true", help="Skip Flutter build")
    args = parser.parse_args()

    platform_desc = {"windows": "Single-File EXE", "linux": "Portable", "macos": ".app Bundle", "android": "APK"}
    print("=" * 60)
    print("  Legodo BookSource Check - Multi-Platform Build")
    print(f"  Target: {platform_desc[args.target]}")
    print("=" * 60)
    print(f"[INFO] Platform: {platform.system()} {platform.release()}")
    print(f"[INFO] Python: {sys.executable}")
    print()

    go_exe_path: Path | None = None
    exe_name = ""
    go_b64_define = ""

    # Step 1: Build Go backend (if needed for embedding)
    if args.target != "android" and not args.skip_go:
        go_out, exe_name = build_go_backend(args.target)
        go_exe_path = go_out
        # Embed as compile-time constant for Flutter build
        go_b64_define = embed_go_as_define(go_out, exe_name, DART_CLIENT)
    elif args.skip_go and args.target != "android":
        print("[INFO] Skipping Go backend compilation (--skip-go)", flush=True)
    else:
        print("[INFO] Android: skipping Go backend (pure Dart/Flutter)", flush=True)

    # Step 2 & 3: Build Flutter + Create distribution
    package: Path | None = None
    if not args.skip_flutter:
        if args.target == "android":
            package = build_android_apk()
        else:
            flutter_output = build_flutter_with_embed(args.target, go_b64_define)
            if args.target == "windows":
                package = create_distribution_windows(args.target, flutter_output)
            else:
                print(f"[INFO] Package creation for {args.target} needs adaptation", flush=True)
                package = flutter_output
    else:
        print("[INFO] Skipping Flutter build (--skip-flutter)", flush=True)

    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    if package:
        print(f"\nOutput: {package}")
    print("\nFeatures:")
    print("  - WebView browser (native WebView)")
    print("  - TTS text-to-speech (system TTS)")
    print("  - Go backend embedded in single EXE (auto-extract on startup)")
    print("  - Cross-platform (Windows/Linux/macOS/Android)")
    print()


if __name__ == "__main__":
    main()
