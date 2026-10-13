#!/usr/bin/env python3
"""Legodo Flet - 多平台打包脚本（基于 flet pack）。

用法：
    conda activate legodo_booksource_check
    cd legodo_flet
    python build_release.py windows              # Windows 单文件 exe
    python build_release.py windows --onedir     # Windows 目录版（更快启动）
    python build_release.py macos                # macOS .app
    python build_release.py linux                # Linux 可执行文件

特性：
- 使用 flet pack 自动处理 PyInstaller 配置
- 支持 Windows/macOS/Linux 三平台
- Windows 默认单文件 exe，可选 --onedir 目录版
- 自动生成发布包（zip/tar.gz）
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# 路径计算
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).resolve().parent
_APP_SOURCE = _SCRIPT_DIR / "app"
_OUTPUT_DIR = _SCRIPT_DIR / "dist"


def find_flet() -> str:
    """查找 flet 命令。"""
    # 1. 当前 Python 环境同级目录
    candidates = [
        Path(sys.executable).parent / "flet.exe",
        Path(sys.executable).parent / "flet",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    
    # 2. PATH 中查找
    path = shutil.which("flet")
    if path:
        return path
    
    raise RuntimeError(
        "找不到 flet 命令。\n"
        "请安装: pip install flet-cli\n"
        "或激活 conda 环境: conda activate legodo_booksource_check"
    )


def run_cmd(cmd: list[str], cwd: Path | None = None) -> int:
    """运行命令并实时输出。"""
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    
    for line in proc.stdout:
        print(line.rstrip())
    
    proc.wait()
    return proc.returncode


def build_flet_pack(flet_cmd: str, target: str, onedir: bool = False) -> Path:
    """步骤 1：使用 flet pack 打包应用。"""
    print(f"\n[1/2] Building with flet pack ({target})...")
    
    main_py = _APP_SOURCE / "main.py"
    if not main_py.exists():
        print(f"[ERROR] Main script not found: {main_py}")
        sys.exit(1)
    
    # 构建基础命令
    cmd = [
        flet_cmd,
        "pack",
        str(main_py),
        "--name", "LegadoReader",
        "--distpath", str(_OUTPUT_DIR),
        "-y",  # 非交互模式
    ]
    
    # Windows 可选择单文件或目录版
    if target == "windows":
        if onedir:
            cmd.append("--onedir")
            print("[INFO] Mode: onedir (faster startup, larger disk usage)")
        else:
            print("[INFO] Mode: single file (smaller distribution, slower startup)")
    elif target == "macos":
        # macOS 必须是 onedir（.app bundle）
        pass
    elif target == "linux":
        # Linux 默认单文件
        pass
    
    # 隐藏导入（动态加载的服务模块）
    hidden_imports = [
        # Gateway + Engine
        "app.services.gateway.service",
        "app.gateway.methods",
        "app.core.engine_worker",
        "app.core.service_hub",
        # M1: 阅读核心
        "app.services.reading.book_cache_service",
        "app.services.reading.reading_service",
        "app.services.reading.download_service",
        "app.services.reading.bookmark_service",
        "app.services.reading.content_search_service",
        "app.services.reading.image_book_service",
        # M2: 引擎层
        "app.services.engine.rule_engine_service",
        "app.services.engine.legado_js",
        "app.services.engine.public_suffix",
        # M3: 书源域
        "app.services.sources.source_service",
        "app.services.sources.source_check_service",
        "app.services.sources.rule_repair_service",
        "app.services.sources.compat_audit_service",
        "app.services.sources.online_import_service",
        # M4: 内容形态
        "app.services.content.rss_service",
        "app.services.content.local_book_service",
        "app.services.content.media_play",
        # M5: 工具域
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
        # Core
        "app.core.storage",
    ]
    
    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])
    
    # 数据文件
    schema_dir = _APP_SOURCE / "gateway" / "schema"
    if schema_dir.exists():
        cmd.extend(["--add-data", f"{schema_dir}:app/gateway/schema"])
    
    resources_dir = _APP_SOURCE / "resources"
    publicsuffix_gz = resources_dir / "publicsuffixes.gz"
    if publicsuffix_gz.exists():
        cmd.extend(["--add-data", f"{publicsuffix_gz}:app/resources"])
    
    ret = run_cmd(cmd, cwd=_SCRIPT_DIR)
    if ret != 0:
        print("[ERROR] flet pack failed")
        sys.exit(1)
    
    # 确定输出路径
    app_name = "LegadoReader"
    if target == "windows":
        if onedir:
            output = _OUTPUT_DIR / app_name
        else:
            output = _OUTPUT_DIR / f"{app_name}.exe"
    elif target == "macos":
        output = _OUTPUT_DIR / f"{app_name}.app"
    elif target == "linux":
        output = _OUTPUT_DIR / app_name
    else:
        output = _OUTPUT_DIR / app_name
    
    if not output.exists():
        print(f"[ERROR] Build output not found: {output}")
        sys.exit(1)
    
    print(f"[OK] Built: {output}")
    return output


def create_archive(target: str, app_path: Path) -> Path:
    """步骤 2：生成发布包。"""
    print(f"\n[2/2] Creating release archive...")
    
    if target == "windows":
        archive_name = "legodo_flet_windows.zip"
        archive_format = "zip"
    elif target == "macos":
        archive_name = "legodo_flet_macos.tar.gz"
        archive_format = "gztar"
    elif target == "linux":
        archive_name = "legodo_flet_linux.tar.gz"
        archive_format = "gztar"
    else:
        print(f"[ERROR] Unsupported target: {target}")
        sys.exit(1)
    
    archive_path = _SCRIPT_DIR / archive_name
    
    # 删除旧归档
    if archive_path.exists():
        archive_path.unlink()
    
    # 创建归档
    root_dir = app_path.parent
    base_dir = app_path.name
    
    print(f"[INFO] Archiving: {base_dir} → {archive_name}")
    shutil.make_archive(
        str(_SCRIPT_DIR / "_temp_archive"),
        archive_format,
        root_dir=str(root_dir),
        base_dir=base_dir,
    )
    
    # 重命名临时文件
    temp_archive = _SCRIPT_DIR / f"_temp_archive.{archive_format.replace('gztar', 'tar.gz')}"
    if temp_archive.exists():
        temp_archive.rename(archive_path)
    
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"[OK] Archive created: {archive_path} ({size_mb:.1f} MB)")
    return archive_path


def main():
    parser = argparse.ArgumentParser(
        description="Build Legodo Flet multi-platform release"
    )
    parser.add_argument(
        "target",
        choices=["windows", "macos", "linux"],
        help="Target platform",
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Windows only: create one-folder bundle instead of single-file exe",
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("  Legodo Flet - Multi-Platform Build")
    print("=" * 60)
    print(f"[INFO] Target: {args.target}")
    if args.target == "windows" and args.onedir:
        print(f"[INFO] Mode: onedir")
    print(f"[INFO] Platform: {platform.system()} {platform.release()}")
    print()
    
    # 查找 flet 命令
    flet_cmd = find_flet()
    print(f"[INFO] Flet: {flet_cmd}")
    
    # 步骤 1: 打包
    app_path = build_flet_pack(flet_cmd, args.target, args.onedir)
    
    # 步骤 2: 生成归档
    archive = create_archive(args.target, app_path)
    
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    print(f"\nArchive: {archive}")
    
    if args.target == "windows":
        print("\nNext steps:")
        print("  - Extract the zip file")
        if args.onedir:
            print("  - Run: LegadoReader/LegadoReader.exe")
        else:
            print("  - Run: LegadoReader.exe")
        print("  - No installation required")
    elif args.target == "macos":
        print("\nNext steps:")
        print("  - Extract the tar.gz file")
        print("  - Drag LegadoReader.app to Applications")
    elif args.target == "linux":
        print("\nNext steps:")
        print("  - Extract the tar.gz file")
        print("  - chmod +x LegadoReader")
        print("  - Run: ./LegadoReader")


if __name__ == "__main__":
    main()
