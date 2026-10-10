#!/usr/bin/env python3
"""一键打包 Flutter 多平台 Release（含 Python 后端）。

WebView + TTS 已改为 Dart 原生实现，但核心引擎仍需 Python。
支持平台：windows / linux / macos / android / ios
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
DART_CLIENT = PROJECT_ROOT / "legodo_dart"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """执行命令并在失败时退出。"""
    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd or PROJECT_ROOT)
    if result.returncode != 0:
        print(f"[ERROR] Command failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(1)


def build_backend() -> None:
    """打包 Python 后端为可执行文件。"""
    print("\n[1/3] Packing Python backend...", flush=True)
    backend_script = PROJECT_ROOT / "build_backend.ps1"
    if backend_script.exists():
        # Windows: 使用 PowerShell
        run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(backend_script)])
    else:
        # 其他平台：直接调用 PyInstaller
        spec_file = PROJECT_ROOT / "LegadoReader.spec"
        if not spec_file.exists():
            print("[WARN] LegadoReader.spec not found, skipping backend packaging", flush=True)
            return
        run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_file)])
        # 复制输出到 backend 目录
        dist_dir = PROJECT_ROOT / "dist" / "LegadoReader"
        backend_dir = DART_CLIENT / "backend"
        if backend_dir.exists():
            shutil.rmtree(backend_dir)
        if dist_dir.exists():
            shutil.copytree(dist_dir, backend_dir)
            print(f"[OK] Backend packed to: {backend_dir}", flush=True)


def build_flutter(target: str) -> Path:
    """构建 Flutter 应用并返回输出目录。"""
    print(f"\n[2/3] Building Flutter {target} Release...", flush=True)

    backend_exe = DART_CLIENT / "backend" / "LegadoReader.exe"
    backend_main = DART_CLIENT / "backend" / "main.py"

    cmd = ["flutter", "build", target, "--release"]
    if backend_exe.exists():
        cmd.extend([
            "--dart-define", f"LEGODO_PYTHON={backend_exe}",
            "--dart-define", f"LEGODO_MAIN={backend_main}",
        ])

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
        sys.exit(1)

    print(f"[OK] Build output: {output_dir}", flush=True)
    return output_dir


def copy_backend(output_dir: Path) -> None:
    """复制 Python 后端到输出目录（仅桌面平台）。"""
    backend_dir = DART_CLIENT / "backend"
    if not backend_dir.exists():
        print("[WARN] Backend directory not found, skipping copy", flush=True)
        return

    # 桌面平台需要后端，移动端不需要
    dest = output_dir / "backend"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(backend_dir, dest)
    print(f"[OK] Backend copied to: {dest}", flush=True)


def create_package(output_dir: Path, target: str) -> Path:
    """创建发布包（zip/apk/ipa）。"""
    print(f"\n[3/3] Generating release package...", flush=True)

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
    zip_path = PROJECT_ROOT / zip_name
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
    args = parser.parse_args()

    print("=" * 60)
    print("  Legodo BookSource Check - Multi-Platform Build")
    print("  (WebView/TTS: Pure Dart)")
    print("=" * 60)

    # Step 1: Pack Python backend (desktop only)
    if args.target in ("windows", "linux", "macos"):
        build_backend()

    # Step 2: Build Flutter
    output_dir = build_flutter(args.target)

    # Step 3: Copy backend and create package (desktop only)
    if args.target in ("windows", "linux", "macos"):
        copy_backend(output_dir)

    package = create_package(output_dir, args.target)

    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    print(f"\nPackage: {package}")
    if args.target in ("windows", "linux", "macos"):
        print("\nFeatures:")
        print("  - WebView browser (platform-native WebView)")
        print("  - TTS text-to-speech (system TTS)")
        print("  - Python backend for core engine services")
    print()


if __name__ == "__main__":
    main()
