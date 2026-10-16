#!/usr/bin/env python3
"""自动下载并安装 Enigma Virtual Box（Windows 单文件打包必需）。

使用方法:
    python install_enigma_vbox.py

会自动：
1. 从官网下载安装包
2. 静默安装到默认位置
3. 验证安装是否成功
"""
import os
import subprocess
import sys
from pathlib import Path


def check_if_installed() -> bool:
    """检查 Enigma Virtual Box 是否已安装。"""
    search_paths = [
        Path("C:/Program Files/Enigma Virtual Box/enigmavbconsole.exe"),
        Path("C:/Program Files (x86)/Enigma Virtual Box/enigmavbconsole.exe"),
    ]
    
    for path in search_paths:
        if path.exists():
            print(f"[OK] Enigma Virtual Box already installed: {path}")
            return True
    
    return False


def download_installer(output_dir: Path) -> Path:
    """下载 Enigma Virtual Box 安装程序。"""
    url = "https://enigmaprotector.com/assets/files/enigmavb.exe"
    output_file = output_dir / "enigmavb_setup.exe"
    
    if output_file.exists():
        print(f"[INFO] Installer already exists: {output_file}")
        print(f"[INFO] Delete it to re-download")
        return output_file
    
    print(f"[INFO] Downloading Enigma Virtual Box from: {url}")
    print(f"[INFO] This may take a few minutes...")
    
    try:
        import urllib.request
        
        def report_progress(block_num, block_size, total_size):
            downloaded = block_num * block_size
            percent = min(downloaded * 100 / total_size, 100)
            mb_downloaded = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            print(f"\r[INFO] Downloading: {mb_downloaded:.1f}/{mb_total:.1f} MB ({percent:.0f}%)", end="", flush=True)
        
        urllib.request.urlretrieve(url, str(output_file), reporthook=report_progress)
        print()  # 换行
        print(f"[OK] Downloaded to: {output_file}")
        return output_file
        
    except Exception as e:
        print(f"\n[ERROR] Download failed: {e}", file=sys.stderr)
        print(f"\n[INFO] Please download manually from:", file=sys.stderr)
        print(f"  {url}", file=sys.stderr)
        print(f"\n[INFO] Then run the installer manually.", file=sys.stderr)
        sys.exit(1)


def install_silently(installer: Path) -> bool:
    """静默安装 Enigma Virtual Box。"""
    print(f"\n[INFO] Installing Enigma Virtual Box...")
    print(f"[INFO] Installer: {installer}")
    
    # Enigma Virtual Box 使用 Inno Setup，支持 /SILENT 参数
    cmd = [str(installer), "/SILENT", "/NORESTART"]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,  # 2分钟超时
            encoding="utf-8",
            errors="replace"
        )
        
        if result.returncode == 0:
            print(f"[OK] Installation completed successfully")
            return True
        else:
            print(f"[ERROR] Installation failed with exit code {result.returncode}", file=sys.stderr)
            if result.stdout:
                print(f"[STDOUT] {result.stdout}", file=sys.stderr)
            if result.stderr:
                print(f"[STDERR] {result.stderr}", file=sys.stderr)
            return False
            
    except subprocess.TimeoutExpired:
        print("[ERROR] Installation timed out (2 minutes)", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[ERROR] Installation failed: {e}", file=sys.stderr)
        return False


def verify_installation() -> bool:
    """验证安装是否成功。"""
    print(f"\n[INFO] Verifying installation...")
    
    search_paths = [
        Path("C:/Program Files/Enigma Virtual Box/enigmavbconsole.exe"),
        Path("C:/Program Files (x86)/Enigma Virtual Box/enigmavbconsole.exe"),
    ]
    
    for path in search_paths:
        if path.exists():
            print(f"[OK] Found enigmavbconsole.exe: {path}")
            
            # 验证可执行文件是否可用
            try:
                result = subprocess.run(
                    [str(path), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 or "--help" in result.stderr.lower():
                    print(f"[OK] Enigma Virtual Box is working correctly")
                    return True
            except Exception as e:
                print(f"[WARN] Could not verify executable: {e}")
                print(f"[INFO] But file exists, assuming OK")
                return True
    
    print(f"[ERROR] Enigma Virtual Box not found after installation", file=sys.stderr)
    return False


def main():
    print("=" * 60)
    print("  Enigma Virtual Box Auto-Installer")
    print("  (Required for single-file Windows packaging)")
    print("=" * 60)
    print()
    
    # 检查是否已安装
    if check_if_installed():
        print("\n[INFO] Enigma Virtual Box is already installed.")
        print("[INFO] You can now run:")
        print("  python build_release.py windows --single-exe")
        return
    
    # 创建下载目录
    download_dir = Path.home() / "Downloads"
    download_dir.mkdir(exist_ok=True)
    
    # 下载安装程序
    installer = download_installer(download_dir)
    
    # 静默安装
    print(f"\n[INFO] Starting silent installation...")
    success = install_silently(installer)
    
    if not success:
        print(f"\n[ERROR] Automatic installation failed.", file=sys.stderr)
        print(f"\n[INFO] Please install manually:")
        print(f"  1. Run: {installer}")
        print(f"  2. Follow the wizard")
        print(f"  3. Re-run this script to verify")
        sys.exit(1)
    
    # 验证安装
    if verify_installation():
        print(f"\n{'=' * 60}")
        print("  Installation Complete!")
        print(f"{'=' * 60}")
        print(f"\n[INFO] You can now create single-file packages:")
        print(f"  cd {Path(__file__).parent}")
        print(f"  python build_release.py windows --single-exe")
    else:
        print(f"\n[ERROR] Installation verification failed.", file=sys.stderr)
        print(f"[INFO] Please check the installation manually.")
        sys.exit(1)


if __name__ == "__main__":
    main()
