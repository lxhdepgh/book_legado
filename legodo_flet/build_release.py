#!/usr/bin/env python3
"""Legodo Flet - 多平台打包脚本（使用 flet pack）。

用法：
    conda activate legodo_booksource_check
    cd legodo_flet
    python build_release.py windows   # Windows 桌面版
    python build_release.py macos     # macOS 桌面版
    python build_release.py linux     # Linux 桌面版

功能：
1. 使用 flet pack 打包完整应用（UI + 后端服务，单进程架构）
2. 生成发布包（zip/tar.gz）

架构说明：
- Flet 是纯 Python 项目，UI 和后端在同一个进程（ServiceHub）
- 使用 flet pack 命令自动处理 PyInstaller 配置和依赖收集
- 生成的 exe 包含完整的 Python 运行时和所有依赖
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# 路径计算（全部基于运行时相对路径，避免硬编码）
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent  # 项目根目录
_APP_SOURCE = _SCRIPT_DIR / "app"  # Python app (backend + frontend)
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
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    print(f"  [STDERR] {line}")
        if result.returncode != 0:
            print(f"[ERROR] Command failed with exit code {result.returncode}")
        return result.returncode
    except Exception as exc:
        print(f"[ERROR] Command execution failed: {exc}")
        return 1


def pack_app(python_exe: str):
    """步骤 1：使用 flet pack 打包完整应用。"""
    print("\n[1/2] Packing application with flet pack...")
    
    # 查找 flet 命令
    flet_cmd = None
    flet_candidates = [
        Path(python_exe).parent / "flet.exe",  # Windows conda
        Path(python_exe).parent / "flet",      # Unix conda
        "flet",                                # PATH
    ]
    for candidate in flet_candidates:
        if isinstance(candidate, Path):
            if candidate.exists():
                flet_cmd = str(candidate)
                break
        else:
            path = shutil.which(candidate)
            if path:
                flet_cmd = path
                break
    
    if not flet_cmd:
        print("[ERROR] flet command not found. Please install flet-cli:")
        print("  pip install flet-cli")
        sys.exit(1)
    
    print(f"[INFO] Using flet: {flet_cmd}")
    
    # 构建 flet pack 命令
    main_py = _APP_SOURCE / "main.py"
    if not main_py.exists():
        print(f"[ERROR] Main script not found: {main_py}")
        sys.exit(1)
    
    cmd = [
        flet_cmd,
        "pack",
        str(main_py),
        "--name", "LegadoReader",
        "--distpath", str(_OUTPUT_DIR),
        "--onedir",  # 使用 onedir 模式（避免单文件模式的 DLL 问题）
        "-y",        # 非交互模式
    ]
    
    # 添加隐藏导入（动态加载的服务模块）
    hidden_imports = [
        "app.services.gateway.service",
        "app.gateway.methods",
        "app.core.engine_worker",
        "app.core.service_hub",
        "app.services.reading.book_cache_service",
        "app.services.reading.reading_service",
        "app.services.reading.download_service",
        "app.services.reading.bookmark_service",
        "app.services.reading.content_search_service",
        "app.services.reading.image_book_service",
        "app.services.engine.rule_engine_service",
        "app.services.engine.legado_js",
        "app.services.engine.public_suffix",
        "app.services.sources.source_service",
        "app.services.sources.source_check_service",
        "app.services.sources.rule_repair_service",
        "app.services.sources.compat_audit_service",
        "app.services.sources.online_import_service",
        "app.services.content.rss_service",
        "app.services.content.local_book_service",
        "app.services.content.media_play",
        "app.services.tools.background_service",
        "app.services.tools.book_export_service",
        "app.services.tools.font_service",
        "app.services.tools.qr_code_service",
        "app.services.tools.replace_rule_service",
        "app.services.tools.theme_service",
        "app.services.tools.update_service",
        "app.services.tools.web_dav_service",
        "app.services.tools.web_bridge_service",
        "app.services.tools.browser_service",
        "app.core.storage",
    ]
    
    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])
    
    # 添加数据文件（schema 和资源文件）
    schema_dir = _APP_SOURCE / "gateway" / "schema"
    if schema_dir.exists():
        cmd.extend(["--add-data", f"{schema_dir}:app/gateway/schema"])
    
    resources_dir = _APP_SOURCE / "resources"
    publicsuffix_gz = resources_dir / "publicsuffixes.gz"
    if publicsuffix_gz.exists():
        cmd.extend(["--add-data", f"{publicsuffix_gz}:app/resources"])
    
    ret = run_cmd(cmd, cwd=_SCRIPT_DIR, description="Running flet pack")
    if ret != 0:
        print("[ERROR] flet pack failed")
        sys.exit(1)
    
    dist_dir = _OUTPUT_DIR / "LegadoReader"
    if not dist_dir.exists():
        print(f"[ERROR] Pack output not found: {dist_dir}")
        sys.exit(1)
    
    print(f"[OK] Application packed to: {dist_dir}")
    return dist_dir


def create_package(platform_name: str, app_src: Path):
    """步骤 2：生成发布包。"""
    print(f"\n[2/2] Generating release package for {platform_name}...")
    
    # 根据平台选择压缩格式
    if platform_name == "windows":
        archive_name = f"legodo_flet_{platform_name}.zip"
        archive_format = "zip"
    elif platform_name in ("macos", "linux"):
        archive_name = f"legodo_flet_{platform_name}.tar.gz"
        archive_format = "gztar"
    else:
        print(f"[ERROR] Unsupported platform: {platform_name}")
        sys.exit(1)
    
    archive_path = _SCRIPT_DIR / archive_name
    
    try:
        # 删除旧归档
        if archive_path.exists():
            archive_path.unlink()
        
        # 创建归档（直接打包 PyInstaller 输出目录）
        shutil.make_archive(
            str(_SCRIPT_DIR / "legodo_flet_temp"),
            archive_format,
            root_dir=str(app_src.parent),
            base_dir=app_src.name
        )
        
        # 重命名为最终名称
        temp_archive = _SCRIPT_DIR / f"legodo_flet_temp.{archive_format.replace('gztar', 'tar.gz').replace('zip', 'zip')}"
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
    
    # 执行打包流程（简化为两步：打包 + 生成归档）
    app_src = pack_app(python_exe)
    create_package(platform_name, app_src)
    
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    archive_ext = "zip" if platform_name == "windows" else "tar.gz"
    print(f"\nPackage: {_SCRIPT_DIR / f'legodo_flet_{platform_name}.{archive_ext}'}")
    print("\nArchitecture:")
    print("  - Single executable (UI + backend in one process)")
    print("  - No separate backend needed")
    print("  - Self-contained with Python runtime")
    print("  - Built with flet pack (auto-configured PyInstaller)")


if __name__ == "__main__":
    main()
