#!/usr/bin/env python3
"""Legodo Flet - 多平台打包脚本（Python 后端 + Flet UI）。

用法：
    conda activate legodo_booksource_check
    cd legodo_flet
    python build_release.py windows   # Windows 桌面版
    python build_release.py macos     # macOS 桌面版
    python build_release.py linux     # Linux 桌面版

功能：
1. PyInstaller 打包 Python 后端（含完整服务层）
2. 复制后端到输出目录
3. 生成发布包（zip/tar.gz）
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- #
# 路径计算（全部基于运行时相对路径，避免硬编码）
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent  # 项目根目录
_APP_SOURCE = _SCRIPT_DIR / "app_backend"  # Python 后端源码（本地副本）
_OUTPUT_DIR = _SCRIPT_DIR / "dist"


def find_command(names: list[str]) -> str | None:
    """在 PATH 中查找可用的命令。"""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def detect_python() -> str:
    """检测当前 conda 环境的 Python 解释器。"""
    # 优先使用当前运行的 Python
    if "legodo_booksource_check" in sys.executable:
        return sys.executable
    
    # 尝试常见的 conda 环境路径
    candidates = [
        Path(sys.prefix) / "python.exe",  # Windows
        Path(sys.prefix) / "bin" / "python",  # Unix
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    
    # 最后尝试 PATH 中的 python
    py = find_command(["python"])
    if py:
        return py
    
    raise RuntimeError("找不到 Python 解释器，请激活 conda 环境 legodo_booksource_check")


def run_cmd(cmd: list[str], cwd: Path | None = None, description: str = "") -> int:
    """运行命令并返回退出码。"""
    print(f"[INFO] {description or ' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    print(f"  {line}")
        if result.returncode != 0:
            print(f"[ERROR] Command failed with exit code {result.returncode}")
            if result.stderr:
                for line in result.stderr.strip().split("\n"):
                    if line.strip():
                        print(f"  {line}")
        return result.returncode
    except Exception as exc:
        print(f"[ERROR] Command execution failed: {exc}")
        return 1


def remove_old_backend():
    """清理旧的 backend 目录（带重试逻辑）。"""
    backend_dir = _OUTPUT_DIR / "backend"
    if not backend_dir.exists():
        return
    
    print(f"[INFO] Removing old backend directory: {backend_dir}")
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            shutil.rmtree(backend_dir)
            print("[OK] Old backend removed")
            return
        except PermissionError as exc:
            if attempt < max_retries:
                print(f"[WARN] Attempt {attempt} failed, retrying...")
                import time
                time.sleep(1)
            else:
                print(f"[WARN] Cannot remove old backend (file locked): {exc}")
                print("[INFO] Please manually delete the backend directory and retry")
                sys.exit(1)


def pack_backend(python_exe: str):
    """步骤 1：用 PyInstaller 打包 Python 后端。"""
    print("\n[1/3] Packing Python backend...")
    
    spec_file = _SCRIPT_DIR / "LegadoReader.spec"
    if not spec_file.exists():
        print(f"[ERROR] Spec file not found: {spec_file}")
        sys.exit(1)
    
    ret = run_cmd(
        [python_exe, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_file)],
        cwd=_SCRIPT_DIR,
        description="Running PyInstaller"
    )
    if ret != 0:
        print("[ERROR] PyInstaller failed")
        sys.exit(1)
    
    dist_dir = _PROJECT_ROOT / "dist" / "LegadoReader"
    if not dist_dir.exists():
        print(f"[ERROR] PyInstaller output not found: {dist_dir}")
        sys.exit(1)
    
    print(f"[OK] Backend packed to: {dist_dir}")
    return dist_dir


def copy_backend_to_output(backend_src: Path):
    """步骤 2：复制后端到输出目录。"""
    print("\n[2/3] Copying backend to output directory...")
    
    remove_old_backend()
    
    backend_dest = _OUTPUT_DIR / "backend"
    try:
        shutil.copytree(str(backend_src), str(backend_dest))
        print(f"[OK] Backend copied to: {backend_dest}")
    except Exception as exc:
        print(f"[ERROR] Failed to copy backend: {exc}")
        sys.exit(1)
    
    # 验证关键文件
    exe_path = backend_dest / "LegadoReader.exe"
    if not exe_path.exists():
        print(f"[ERROR] LegadoReader.exe missing in: {backend_dest}")
        sys.exit(1)
    
    print(f"[OK] Backend exe ready: {exe_path}")
    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"  - Size: {size_mb:.1f} MB")


def create_package(platform_name: str):
    """步骤 3：生成发布包。"""
    print(f"\n[3/3] Generating release package for {platform_name}...")
    
    if not _OUTPUT_DIR.exists():
        print(f"[ERROR] Output directory not found: {_OUTPUT_DIR}")
        sys.exit(1)
    
    # 根据平台选择压缩格式
    if platform_name == "windows":
        archive_name = "legodo_client_windows.zip"
        archive_format = "zip"
    elif platform_name in ("macos", "linux"):
        archive_name = f"legodo_client_{platform_name}.tar.gz"
        archive_format = "gztar"
    else:
        print(f"[ERROR] Unsupported platform: {platform_name}")
        sys.exit(1)
    
    archive_path = _SCRIPT_DIR / archive_name
    
    try:
        # 删除旧归档
        if archive_path.exists():
            archive_path.unlink()
        
        # 创建归档
        shutil.make_archive(
            str(_SCRIPT_DIR / "legodo_client_temp"),
            archive_format,
            root_dir=str(_OUTPUT_DIR),
            base_dir="."
        )
        
        # 重命名为最终名称
        temp_archive = _SCRIPT_DIR / f"legodo_client_temp.{archive_format.replace('gztar', 'tar.gz').replace('zip', 'zip')}"
        if temp_archive.exists():
            temp_archive.rename(archive_path)
        
        print(f"[OK] Package created: {archive_path}")
        size_mb = archive_path.stat().st_size / (1024 * 1024)
        print(f"  - Size: {size_mb:.1f} MB")
    except Exception as exc:
        print(f"[ERROR] Failed to create package: {exc}")
        sys.exit(1)


def main():
    """主流程。"""
    print("=" * 60)
    print("  Legodo BookSource Check (Flet) - Multi-Platform Build")
    print("=" * 60)
    
    # 解析参数
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <windows|macos|linux>")
        sys.exit(1)
    
    platform_name = sys.argv[1].lower()
    if platform_name not in ("windows", "macos", "linux"):
        print(f"[ERROR] Unsupported platform: {platform_name}")
        print("Supported: windows, macos, linux")
        sys.exit(1)
    
    print(f"[INFO] Platform: {platform_name}")
    print(f"[INFO] Script: {_SCRIPT_DIR}")
    print()
    
    # 检测 Python
    python_exe = detect_python()
    print(f"[INFO] Python: {python_exe}")
    
    # 执行打包流程
    backend_src = pack_backend(python_exe)
    copy_backend_to_output(backend_src)
    create_package(platform_name)
    
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    print(f"\nPackage: {_SCRIPT_DIR / f'legodo_client_{platform_name}'}.{('zip' if platform_name == 'windows' else 'tar.gz')}")
    print("\nFeatures:")
    print("  - Full Python backend with all services")
    print("  - Flet desktop UI")
    print("  - Self-contained executable")


if __name__ == "__main__":
    main()
