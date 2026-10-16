#!/usr/bin/env python3
"""一键打包 Flutter + Go 后端 Release（FFI 单进程架构）。

架构：Flutter (Dart UI) + Go FFI（零网络延迟，单进程）
Go 编译为 C 动态库 → Dart dart:ffi 直接调用。

用法:
    python build_release.py windows       # Windows (.dll)
    python build_release.py linux         # Linux (.so)
    python build_release.py macos         # macOS (.dylib)
    python build_release.py android       # Android APK（纯 Dart/Flutter）
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Global paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DART_CLIENT = SCRIPT_DIR
GO_BACKEND = SCRIPT_DIR / "backend_go"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_command(cmd_name: str) -> Path | None:
    """Look up an executable in PATH and common install locations."""
    if path := shutil.which(cmd_name):
        return Path(path)
    system = platform.system()
    if system == "Windows":
        candidates: list[Path] = [
            Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Programs" / cmd_name,
            Path("C:/Program Files") / cmd_name,
            Path("C:/Program Files (x86)") / cmd_name,
        ]
        if cmd_name == "flutter":
            candidates += [
                Path("D:/env_manger/flutter/bin/flutter.bat"),
                Path("D:/flutter/bin/flutter.bat"),
                Path("C:/flutter/bin/flutter.bat"),
            ]
        if cmd_name == "go":
            candidates += [
                Path("C:/Go/bin/go.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Go" / "bin" / "go.exe",
                Path("D:/env_manger/go/go1.26.3/bin/go.exe"),
                Path("D:/env_manger/go/go1.25.3/bin/go.exe"),
                Path("D:/dev-cache/go/gf.exe"),  # goframe build tool
                Path("D:/dev-cache/go/gopls.exe"),
            ]
        if cmd_name == "gcc":
            candidates += [
                Path("C:/mingw64/bin/gcc.exe"),
                Path("C:/msys64/mingw64/bin/gcc.exe"),
                Path("D:/msys64/mingw64/bin/gcc.exe"),
            ]
    elif system == "Linux":
        candidates = [Path("/usr/local/bin") / cmd_name, Path("/usr/bin") / cmd_name]
    elif system == "Darwin":
        candidates = [Path("/usr/local/bin") / cmd_name, Path("/opt/homebrew/bin") / cmd_name]
    else:
        candidates = []
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Compiler discovery & installation
# ---------------------------------------------------------------------------

MSYS2_ROOTS = [
    Path("C:/mingw64"),
    Path("C:/msys64/mingw64"),
    Path("C:/msys64/usr"),
    Path("D:/msys64/msys64/mingw64"),
    Path("D:/msys64/msys64/usr"),
]

VSCMD_PATHS = [
    Path("C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build/vcvars64.bat"),
    Path("C:/Program Files/Microsoft Visual Studio/2022/Professional/VC/Auxiliary/Build/vcvars64.bat"),
    Path("C:/Program Files/Microsoft Visual Studio/2022/Enterprise/VC/Auxiliary/Build/vcvars64.bat"),
    Path("C:/Program Files (x86)/Microsoft Visual Studio 2019/Community/VC/Auxiliary/Build/vcvars64.bat"),
]


def _find_vcvars() -> Path | None:
    """Find VS Build Tools vcvars64.bat across editions and versions."""
    for p in VSCMD_PATHS:
        if p.exists():
            return p
    vs_base = Path("C:/Program Files/Microsoft Visual Studio")
    if vs_base.exists():
        for edition in sorted(vs_base.iterdir(), reverse=True):
            for ver in sorted(edition.iterdir(), reverse=True):
                bat = ver / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
                if bat.exists():
                    return bat
    return None


def _ensure_msys2_mingw_in_path() -> bool:
    """Add MSYS2 mingw64 bin to PATH so that gcc/cl etc are discoverable."""
    for root in MSYS2_ROOTS:
        mingw_bin = root / "bin"
        if mingw_bin.joinpath("gcc.exe").exists():
            existing = os.environ.get("PATH", "")
            mingw_str = str(mingw_bin)
            if mingw_str not in existing:
                os.environ["PATH"] = f"{mingw_str};{existing}"
            print(f"[OK] Added MSYS2 MinGW to PATH: {mingw_str}", flush=True)
            return True
    return False


def ensure_c_compiler(target_platform: str) -> bool:
    """Check that a C/C++ compiler is available for Go CGO builds."""
    if target_platform != "windows":
        return True  # non-Windows uses system default toolchain

    # 1. Try MSYS2 MinGW-w64 first (auto-install handled above)
    if _ensure_msys2_mingw_in_path():
        if shutil.which("gcc"):
            print("[OK] gcc (MinGW-w64) found via MSYS2", flush=True)
            return True

    # 2. gcc already in PATH?
    if shutil.which("gcc"):
        print("[OK] gcc (MinGW-w64) found in PATH", flush=True)
        return True

    # 3. MSVC via vcvars?
    vscmd = _find_vcvars()
    if vscmd:
        print(f"[OK] Found VS Build Tools: {vscmd.name}", flush=True)
        # Set Go environment vars so the linker picks cl.exe
        env_backup = dict(os.environ)
        os.environ["CGO_ENABLED"] = "1"
        os.environ["CC"] = "cl"
        os.environ["CXX"] = "cl"
        try:
            subprocess.run(
                ["cmd", "/c", f'"{vscmd}" && echo VCSETUP_DONE'],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(env_backup)
        os.environ["CGO_ENABLED"] = "1"
        os.environ["CC"] = "cl"
        os.environ["CXX"] = "cl"
        print("[OK] MSVC configured for Go CGO (cl.exe)", flush=True)
        return True

    # No compiler — print actionable instructions and exit
    print(
        "\n[FATAL] No C compiler found. Go c-shared needs gcc or cl.exe.\n\n"
        "Install one of (pick EITHER):\n"
        "─────────────────────────────────────────────────────\n"
        "  Option A: MinGW-w64 via MSYS2 (recommended)\n"
        "  ───────────────────────────────────────────\n"
        "  1. Download installer: https://www.msys2.org/\n"
        "  2. Run msys2_shell.cmd -mingw64\n"
        "  3. Run: pacman -Syuu (twice, restart between)\n"
        "  4. Then: pacman -S mingw-w64-x86_64-gcc\n"
        "  5. Restart this terminal or add C:\\msys64\\mingw64\\bin to PATH\n"
        "\n"
        "  Option B: Visual Studio Build Tools\n"
        "  ───────────────────────────────────\n"
        "  1. Download: https://visualstudio.microsoft.com/visual-cpp-build-tools/\n"
        "  2. In Installer, check 'Desktop development with C++'\n"
        "     Workload → Install\n"
        "  3. This build script auto-detects vcvars64.bat\n"
        "─────────────────────────────────────────────────────\n"
        "\nOr skip Go compilation:  python build_release.py windows --skip-go\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Toolchain verification
# ---------------------------------------------------------------------------

def ensure_flutter() -> Path:
    flutter = find_command("flutter")
    if not flutter:
        print("[ERROR] Flutter command not found in PATH", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run(
            [str(flutter), "--version"], capture_output=True, timeout=10,
        )
        raw = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        version_line = raw.strip().split("\n")[0]
        # Strip non-ASCII for GBK console safety
        clean = "".join(c for c in version_line if ord(c) < 128)
        print(f"[OK] Found Flutter: {clean}", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Flutter version: {e}", flush=True)
    return flutter


def ensure_go() -> Path:
    go = find_command("go")
    if not go:
        print("[ERROR] Go command not found in PATH", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run([str(go), "version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f"[OK] Found Go: {result.stdout.strip()}", flush=True)
    except Exception as e:
        print(f"[WARN] Could not verify Go version: {e}", flush=True)
    return go


# ---------------------------------------------------------------------------
# Generic run helper
# ---------------------------------------------------------------------------

def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    """Run a command; stream stdout/stderr line by line."""
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd, cwd=cwd or SCRIPT_DIR, env=merged_env, check=True,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        output = result.stdout or ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped:
                print(f"  {stripped}")
    except FileNotFoundError:
        print(f"[ERROR] Command not found: {cmd[0]}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        out = e.output or ""
        for line in str(out).splitlines():
            if line.strip():
                print(f"  {line}")
        print(f"[ERROR] Command failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)


# ---------------------------------------------------------------------------
# Go backend builder
# ---------------------------------------------------------------------------

def build_go_backend(target_platform: str) -> tuple[Path, str]:
    """Compile Go FFI shared library, return (output_path, filename). Android returns (Path(), "")."""
    if target_platform == "android":
        return (Path(), "")

    print(f"\n[1/4] Building Go FFI dynamic library for {target_platform}...", flush=True)
    if not GO_BACKEND.exists():
        print(f"[ERROR] Go backend directory not found at: {GO_BACKEND}", file=sys.stderr)
        sys.exit(1)

    mod_file = GO_BACKEND / "go.mod"
    if not mod_file.exists():
        print(f"[ERROR] go.mod not found at: {mod_file}", file=sys.stderr)
        sys.exit(1)

    go = ensure_go()
    go_bin = str(go)

    # Pre-download deps (fail fast if network broken)
    run([go_bin, "mod", "download"], cwd=GO_BACKEND)

    goos_map = {"windows": "windows", "linux": "linux", "macos": "darwin"}
    goarch_map = {"windows": "amd64", "linux": "amd64", "macos": ("arm64", "amd64")}
    goos = goos_map.get(target_platform)
    arch_list = goarch_map.get(target_platform)
    if not goos or not arch_list:
        print(f"[ERROR] Unsupported platform for Go: {target_platform}", file=sys.stderr)
        sys.exit(1)

    # Ensure C compiler PATH is set *before* we snapshot env for the Go subprocess
    if target_platform == "windows":
        ensure_c_compiler("windows")

    last_err = None

    for goarch in (arch_list if isinstance(arch_list, tuple) else (arch_list,)):
        env = os.environ.copy()
        env["GOOS"] = goos
        env["GOARCH"] = goarch
        ld_flags = "-s -w"
        ext_ld_flags = ""

        if target_platform == "windows":
            output_path = GO_BACKEND / "legodo_engine.dll"
            env["CGO_ENABLED"] = "1"
        elif target_platform == "linux":
            output_path = GO_BACKEND / "liblegodo_engine.so"
            env["CGO_ENABLED"] = "1"
            ext_ld_flags = " -extldflags=-static"
        elif target_platform == "macos":
            output_path = GO_BACKEND / "liblegodo_engine.dylib"
            env["CGO_ENABLED"] = "1"
        else:
            print(f"[ERROR] Unsupported platform: {target_platform}", file=sys.stderr)
            sys.exit(1)

        # Remove stale output so build is forced
        if output_path.exists():
            output_path.unlink()

        build_cmd = [
            go_bin, "build", "-buildmode=c-shared",
            f"-ldflags={ld_flags}{ext_ld_flags}",
            "-o", str(output_path), "./pkg/ffi/",
        ]
        try:
            run(build_cmd, cwd=GO_BACKEND, env=env)
            if output_path.exists():
                size_kb = output_path.stat().st_size // 1024
                print(f"[OK] Go FFI library: {output_path} ({size_kb} KB)", flush=True)
                return (output_path, output_path.name)
        except SystemExit as e:
            last_err = e
            print(f"[WARN] Failed GOOS={goos} GOARCH={goarch}: exit {e.code}", flush=True)
            continue

    print(f"[ERROR] Go build failed for all architectures", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Flutter builder
# ---------------------------------------------------------------------------

def build_flutter(target: str) -> Path:
    """Build Flutter app in release mode; return the output directory."""
    print(f"\n[2/4] Building Flutter {target} Release...", flush=True)
    flutter = ensure_flutter()

    env = os.environ.copy()
    if platform.system() == "Windows":
        nuget_paths = [
            Path.home() / "bin",
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/nuget"),
        ]
        current_path = env.get("PATH", "")
        for p in nuget_paths:
            if (p / "nuget.exe").exists() and str(p) not in current_path:
                env["PATH"] = f"{str(p)};{current_path}"
                print(f"[OK] Added nuget to PATH: {p}", flush=True)
                break

    cmd = [str(flutter), "build", target, "--release"]
    try:
        run(cmd, cwd=DART_CLIENT, env=env)
    except SystemExit as e:
        # Tolerate CMake INSTALL failure — the exe may have been created anyway
        if platform.system() == "Windows" and target == "windows":
            exe = DART_CLIENT / "build" / "windows" / "x64" / "runner" / "Release" / "legodo_client.exe"
            if exe.exists():
                print(f"[WARN] Build exited {e.code} but EXE exists, continuing...", flush=True)
            else:
                raise
        else:
            raise

    output_dir: Path | None = {
        "windows": DART_CLIENT / "build" / "windows" / "x64" / "runner" / "Release",
        "linux": DART_CLIENT / "build" / "linux" / "x64" / "release" / "bundle",
        "macos": DART_CLIENT / "build" / "macos" / "Build" / "Products" / "Release" / "legodo_client.app",
    }.get(target)

    if not output_dir or not output_dir.exists():
        print(f"[ERROR] Output directory not found: {output_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Build output: {output_dir}", flush=True)
    return output_dir


def build_android_apk() -> Path:
    """Build Android APK (pure Dart/Flutter, no Go FFI)."""
    print(f"\n[2/4] Building Android APK...", flush=True)
    flutter = ensure_flutter()

    java_home = os.environ.get("JAVA_HOME", "")
    if not java_home:
        print("[WARN] JAVA_HOME not set, searching…", flush=True)
        for base in [
            Path("C:/Program Files/Java"),
            Path("C:/Program Files (x86)/Java"),
            Path.home() / ".jdks",
            Path("C:/ProgramData/AndroidSDK"),
        ]:
            if not base.exists():
                continue
            for jdk_dir in sorted(base.iterdir()):
                if jdk_dir.is_dir() and (jdk_dir / "bin" / "java.exe").exists():
                    java_home = str(jdk_dir)
                    break
            if java_home:
                break
    if not java_home:
        for candidate in [
            Path("C:/Program Files/Android/Android Studio/jre"),
            Path(os.environ.get("ANDROID_STUDIO_JRE_HOME", "")),
        ]:
            if candidate.exists():
                java_home = str(candidate)
                break

    if java_home:
        os.environ["JAVA_HOME"] = java_home
        print(f"[OK] JAVA_HOME={java_home}", flush=True)
    else:
        print("[ERROR] JAVA_HOME not set and Java not found", file=sys.stderr)
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

    apks = sorted(apk_dir.glob("*.apk"), key=lambda p: p.stat().st_mtime)
    if not apks:
        print(f"[ERROR] No APK files found in: {apk_dir}", file=sys.stderr)
        sys.exit(1)

    latest_apk = apks[-1]
    dist_dir = SCRIPT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)
    dest_apk = dist_dir / f"legodo_{latest_apk.name}"
    shutil.copy2(latest_apk, dest_apk)
    print(f"[OK] APK -> {dest_apk} ({latest_apk.stat().st_size / (1024*1024):.1f} MB)", flush=True)
    return dest_apk


# ---------------------------------------------------------------------------
# Distribution packager — Windows (main workflow)
# ---------------------------------------------------------------------------

def create_distribution_windows(lib_path: Path | None, lib_name: str, flutter_output: Path) -> Path:
    """Assemble all required files into a dist folder and produce a zip.

    Checklist — every item here MUST exist in the final package:
      1. legodo_client.exe          — Flutter desktop release binary
      2. flutter_windows.dll        — Flutter engine runtime (from ephemeral)
      3. data/app.so                — AOT snapshot (required on Windows --release)
      4. data/icudtl.dat            — ICU locale data
      5. data/flutter_assets/       — fonts / MaterialIcons / asset bundles
      6. plugin DLLs                — native plugin libraries (if any)
      7. legodo_engine.dll          — Go FFI dynamic library
      8. README.txt                 — startup instructions

    PDB 调试符号刻意不进发布包：Windows 加载器与运行时都不读它（仅崩溃转储
    符号化用），而 flutter_windows.dll.pdb 单份就有 ~240 MB。符号文件留在
    windows/flutter/ephemeral/ 原地，需要时单独取用即可。
    """
    print(f"\n[3/4] Assembling Windows distribution package...", flush=True)

    dist_name = "legodo_windows"
    dist_dir = SCRIPT_DIR / "dist" / dist_name
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    collected: list[str] = []

    # === 1. Flutter executable ===
    exe_src = flutter_output / "legodo_client.exe"
    if not exe_src.exists():
        print(f"[FATAL] Missing: {exe_src}", file=sys.stderr)
        sys.exit(1)
    shutil.copy2(exe_src, dist_dir / "legodo_client.exe")
    collected.append(f"legodo_client.exe ({exe_src.stat().st_size / (1024*1024):.1f} MB)")

    # === 2. flutter_windows.dll ===
    # 同目录的 flutter_windows.dll.pdb（~240 MB）刻意不拷：运行时从不读符号文件，
    # 只在崩溃转储符号化时用；文件留在 ephemeral/ 原地，需要时单独取。
    ephemeral_dir = DART_CLIENT / "windows" / "flutter" / "ephemeral"
    flutter_dll = ephemeral_dir / "flutter_windows.dll"
    if flutter_dll.exists():
        shutil.copy2(flutter_dll, dist_dir / "flutter_windows.dll")
        collected.append(f"flutter_windows.dll ({flutter_dll.stat().st_size / (1024*1024):.1f} MB)")
    else:
        print(f"[FATAL] Missing critical runtime: {flutter_dll}", file=sys.stderr)
        sys.exit(1)

    # === 3. flutter_assets/ — required at runtime (shaders, fonts, packages) ===
    # Windows runner 的 DartProject(L"data") 只认 <exe>/data/flutter_assets；
    # 拷到 dist 根目录时应用照常起（AOT 在 data/app.so），但字体/资产全丢——
    # 表现是界面文字正常、所有图标变方框（MaterialIcons 找不到，无 fallback）。
    assets_src = DART_CLIENT / "build" / "flutter_assets"
    if assets_src.exists():
        assets_dst = dist_dir / "data" / "flutter_assets"
        shutil.copytree(str(assets_src), str(assets_dst))
        asset_count = sum(1 for _ in assets_dst.rglob("*"))
        print(f"[OK] Copied flutter_assets/ -> data/ ({asset_count} files)", flush=True)
    else:
        print(f"[WARN] Missing flutter_assets/: {assets_src}", flush=True)

    # === 4. data/app.so — AOT snapshot (required on Windows --release) ===
    aot_src = DART_CLIENT / "build" / "windows" / "app.so"
    if aot_src.exists():
        data_dir = dist_dir / "data"
        data_dir.mkdir(exist_ok=True)
        shutil.copy2(aot_src, data_dir / "app.so")
        size_mb = aot_src.stat().st_size / (1024 * 1024)
        collected.append(f"data/app.so ({size_mb:.1f} MB)")
        print(f"[OK] Copied AOT snapshot: app.so ({size_mb:.1f} MB)", flush=True)
    else:
        print(f"[WARN] AOT snapshot not found at: {aot_src}", file=sys.stderr)

    # Also copy icudtl.dat (ICU locale data) into data/
    icu_src = ephemeral_dir / "icudtl.dat"
    if icu_src.exists():
        shutil.copy2(icu_src, data_dir / "icudtl.dat")
        collected.append("data/icudtl.dat")
        print(f"[OK] Copied ICU data: icudtl.dat", flush=True)

    # === 5. Plugin DLLs ===
    plugin_dirs = [
        DART_CLIENT / "build" / "windows" / "x64" / "plugins",
        flutter_output.parent / "plugins",
    ]
    plugin_count = 0
    for pdir in plugin_dirs:
        if not pdir.exists():
            continue
        for entry in pdir.rglob("*.dll"):
            if entry.is_file():
                dest = dist_dir / entry.name
                if not dest.exists():
                    shutil.copy2(entry, dest)
                    collected.append(f"plugin: {entry.name} ({entry.stat().st_size / 1024:.1f} KB)")
                    plugin_count += 1
    if plugin_count > 0:
        print(f"[OK] Copied {plugin_count} plugin DLL(s)", flush=True)
    else:
        print("[INFO] No extra plugin DLLs", flush=True)

    # === 5. Go FFI dynamic library ===
    if lib_path is not None and lib_path.exists():
        shutil.copy2(lib_path, dist_dir / lib_name)
        collected.append(f"{lib_name} ({lib_path.stat().st_size / (1024*1024):.1f} MB)")
    else:
        print("[WARN] Go FFI library NOT included — application cannot load engine!", file=sys.stderr)

    # === 6. Verify critical files ===
    # data/flutter_assets 与 data/app.so/icudtl.dat 是 Windows 运行时的硬依赖：
    # 缺 assets 时应用能起、文字正常、图标全方框——冒烟只看"窗口立起来"照不出来。
    critical = [
        "legodo_client.exe",
        "flutter_windows.dll",
        "data/app.so",
        "data/icudtl.dat",
        "data/flutter_assets/FontManifest.json",
        "data/flutter_assets/fonts/MaterialIcons-Regular.otf",
    ]
    if lib_path and lib_path.exists():
        critical.append(lib_name)

    missing = [c for c in critical if not (dist_dir / c).exists()]
    if missing:
        print(f"\n[FATAL] Critical files MISSING: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    else:
        print("[OK] All critical files verified [PASS]", flush=True)

    # === 7. Print full checklist ===
    print(f"\n{'='*60}")
    print(f"Distribution Checklist ({len(collected)} files):")
    print(f"{'='*60}")
    for i, f in enumerate(collected, 1):
        print(f"  {i:2d}. {f}")
    print(f"{'='*60}")

    # === 8. Write README ===
    readme = dist_dir / "README.txt"
    readme.write_text(
        "Legodo BookSource Check - Windows Release\n"
        "--------------------------------------------\n\n"
        "启动方法:\n"
        "  双击 legodo_client.exe\n\n"
        "架构:\n"
        "  - Flutter (Dart UI) + Go FFI 引擎\n"
        "  - 单进程架构，零网络延迟\n"
        "  - Go 编译为 legodo_engine.dll，通过 dart:ffi 加载\n\n"
        "功能:\n"
        "  - WebView 网页浏览/试读\n"
        "  - TTS 系统朗读\n"
        "  - Go 规则引擎、HTML 解析、JS 执行\n"
        "  - 并发扫描 100+ 源/秒\n\n"
        "注意:\n"
        "  所有 DLL 文件必须与 EXE 在同一目录。\n",
        encoding="utf-8", newline="",
    )
    collected.append("README.txt")

    # === 9. Create zip ===
    zip_path = SCRIPT_DIR / f"{dist_name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    print("[INFO] Creating zip archive...", flush=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(dist_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                arcname = fpath.relative_to(dist_dir.parent)
                zf.write(fpath, arcname)

    zip_size = zip_path.stat().st_size / (1024*1024)
    print(f"[OK] Zip: {zip_path} ({zip_size:.1f} MB)", flush=True)

    return zip_path


# ---------------------------------------------------------------------------
# Distribution packager — Linux / macOS
# ---------------------------------------------------------------------------

def create_distribution_linux_macos(lib_path: Path | None, lib_name: str, flutter_output: Path, target: str) -> Path:
    """Create Linux/macOS bundle (minimal)."""
    print(f"\n[3/4] Assembling {target} package…", flush=True)

    dist_name = f"legodo_{target}"
    dist_dir = SCRIPT_DIR / "dist" / dist_name
    if dist_dir.exists():
        shutil.rmtree(dist_dir)

    zip_path: Path | None = None

    if target == "linux":
        src_bin = flutter_output / "legodo_client"
        if not src_bin.exists():
            print(f"[ERROR] Binary not found: {src_bin}", file=sys.stderr)
            sys.exit(1)
        dist_dir.mkdir(parents=True)
        shutil.copy2(src_bin, dist_dir / "legodo_client")

        # ldd analysis for runtime deps
        result = subprocess.run(
            ["ldd", str(src_bin)], capture_output=True, text=True, timeout=5,
        )
        lib_dir = dist_dir / "libs"
        if result.returncode == 0:
            lib_dir.mkdir(exist_ok=True)
            for line in result.stdout.splitlines():
                parts = line.strip().split("=>")
                if len(parts) >= 2:
                    lib_path_part = parts[1].strip().split()[0]
                    lib_path_obj = Path(lib_path_part)
                    if lib_path_obj.exists():
                        dst = lib_dir / lib_path_obj.name
                        if not dst.exists():
                            shutil.copy2(lib_path_obj, dst)
        if lib_path is not None and lib_path.exists():
            shutil.copy2(lib_path, dist_dir / lib_name)

        zip_path = dist_dir.with_suffix(".tar.gz")
        shutil.make_archive(str(zip_path.with_suffix("")), "gztar", dist_dir.parent, dist_dir.name)

    else:
        # macOS
        src_app = flutter_output / "legodo_client.app"
        if not src_app.exists():
            print(f"[ERROR] .app not found: {src_app}", file=sys.stderr)
            sys.exit(1)
        dist_dir.mkdir(parents=True)
        shutil.copytree(src_app, dist_dir / "legodo_client.app")
        if lib_path is not None and lib_path.exists():
            contents = dist_dir / "legodo_client.app" / "Contents" / "MacOS"
            contents.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib_path, contents / lib_name)
        zip_path = dist_dir.with_suffix(".zip")
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", dist_dir.parent, dist_dir.name)

    print(f"[OK] Package: {zip_path}", flush=True)
    return zip_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Flutter + Go FFI release",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python build_release.py windows          # Full build (Go + Flutter)\n"
            "  python build_release.py windows --skip-go # Skip Go compilation\n"
            "  python build_release.py android           # Android APK\n"
        ),
    )
    parser.add_argument("target", choices=["windows", "linux", "macos", "android"], help="Target platform")
    parser.add_argument("--skip-go", action="store_true", help="Skip Go FFI compilation")
    parser.add_argument("--skip-flutter", action="store_true", help="Skip Flutter build")
    args = parser.parse_args()

    print("=" * 60)
    print("  Legodo BookSource Check - Flutter + Go FFI Build")
    print("=" * 60)
    print(f"[INFO] Platform: {platform.system()} {platform.release()}")
    print(f"[INFO] Python:   {sys.executable}")
    print(f"[INFO] Project:  {PROJECT_ROOT}")
    print()

    # --- Step 1: Go FFI library ---
    go_lib: Path | None = None
    go_lib_name = ""
    if args.target != "android" and not args.skip_go:
        go_out, go_lib_name = build_go_backend(args.target)
        go_lib = go_out
    elif args.skip_go and args.target != "android":
        print("[INFO] Skipping Go FFI compilation (--skip-go)", flush=True)
        lib_map = {
            "windows": "legodo_engine.dll",
            "linux": "liblegodo_engine.so",
            "macos": "liblegodo_engine.dylib",
        }
        existing = GO_BACKEND / lib_map.get(args.target, "")
        if existing.exists():
            go_lib = existing
            go_lib_name = existing.name
            print(f"[OK] Using existing: {go_lib}", flush=True)
        else:
            print(f"[ERROR] Pre-built Go library not found: {existing}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[INFO] Android: skipping Go FFI (pure Dart/Flutter)", flush=True)

    # --- Step 2: Flutter build ---
    package: Path | None = None
    if not args.skip_flutter:
        if args.target == "android":
            package = build_android_apk()
        else:
            flutter_output = build_flutter(args.target)
            if args.target == "windows":
                package = create_distribution_windows(go_lib, go_lib_name, flutter_output)
            else:
                package = create_distribution_linux_macos(go_lib, go_lib_name, flutter_output, args.target)
    else:
        print("[INFO] Skipping Flutter build (--skip-flutter)", flush=True)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("  Build Complete!")
    print("=" * 60)
    if package:
        size_mb = package.stat().st_size / (1024 * 1024) if package.exists() else 0
        print(f"\nOutput: {package} ({size_mb:.1f} MB)")
    print(f"\nPlatform: {args.target}")
    print(f"  - Go FFI: {go_lib_name if go_lib_name else '(skipped)'}")
    print(f"  - Flutter: {'built' if not args.skip_flutter else 'skipped'}")
    print()


if __name__ == "__main__":
    main()
