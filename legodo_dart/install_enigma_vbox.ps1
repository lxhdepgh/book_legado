# Enigma Virtual Box 自动安装脚本 (PowerShell)
# 使用方法: .\install_enigma_vbox.ps1

$ErrorActionPreference = "Stop"

Write-Host "=" * 60
Write-Host "  Enigma Virtual Box Auto-Installer"
Write-Host "  (Required for single-file Windows packaging)"
Write-Host "=" * 60
Write-Host ""

# 检查是否已安装
$possiblePaths = @(
    "C:\Program Files\Enigma Virtual Box\enigmavbconsole.exe",
    "C:\Program Files (x86)\Enigma Virtual Box\enigmavbconsole.exe"
)

$alreadyInstalled = $false
foreach ($path in $possiblePaths) {
    if (Test-Path $path) {
        Write-Host "[OK] Enigma Virtual Box already installed: $path" -ForegroundColor Green
        $alreadyInstalled = $true
        break
    }
}

if ($alreadyInstalled) {
    Write-Host ""
    Write-Host "[INFO] Enigma Virtual Box is already installed." -ForegroundColor Cyan
    Write-Host "[INFO] You can now run:" -ForegroundColor Cyan
    Write-Host "  python build_release.py windows --single-exe" -ForegroundColor Yellow
    exit 0
}

# 下载安装程序
$downloadUrl = "https://enigmaprotector.com/assets/files/enigmavb.exe"
$downloadDir = "$env:USERPROFILE\Downloads"
$installerPath = "$downloadDir\enigmavb_setup.exe"

if (Test-Path $installerPath) {
    Write-Host "[INFO] Installer already exists: $installerPath" -ForegroundColor Yellow
    Write-Host "[INFO] Delete it to re-download" -ForegroundColor Yellow
} else {
    Write-Host "[INFO] Downloading Enigma Virtual Box from: $downloadUrl" -ForegroundColor Cyan
    Write-Host "[INFO] This may take a few minutes..." -ForegroundColor Cyan
    
    try {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $installerPath -UseBasicParsing
        Write-Host "[OK] Downloaded to: $installerPath" -ForegroundColor Green
    } catch {
        Write-Host ""
        Write-Host "[ERROR] Download failed: $_" -ForegroundColor Red
        Write-Host ""
        Write-Host "[INFO] Please download manually from:" -ForegroundColor Yellow
        Write-Host "  $downloadUrl" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "[INFO] Then run the installer manually." -ForegroundColor Yellow
        exit 1
    }
}

# 静默安装
Write-Host ""
Write-Host "[INFO] Starting silent installation..." -ForegroundColor Cyan
Write-Host "[INFO] Installer: $installerPath" -ForegroundColor Cyan

try {
    $process = Start-Process -FilePath $installerPath -ArgumentList "/SILENT", "/NORESTART" -Wait -PassThru
    
    if ($process.ExitCode -eq 0) {
        Write-Host "[OK] Installation completed successfully" -ForegroundColor Green
    } else {
        Write-Host "[ERROR] Installation failed with exit code $($process.ExitCode)" -ForegroundColor Red
        Write-Host ""
        Write-Host "[INFO] Please install manually:" -ForegroundColor Yellow
        Write-Host "  1. Run: $installerPath" -ForegroundColor Yellow
        Write-Host "  2. Follow the wizard" -ForegroundColor Yellow
        Write-Host "  3. Re-run this script to verify" -ForegroundColor Yellow
        exit 1
    }
} catch {
    Write-Host "[ERROR] Installation failed: $_" -ForegroundColor Red
    exit 1
}

# 验证安装
Write-Host ""
Write-Host "[INFO] Verifying installation..." -ForegroundColor Cyan

$found = $false
foreach ($path in $possiblePaths) {
    if (Test-Path $path) {
        Write-Host "[OK] Found enigmavbconsole.exe: $path" -ForegroundColor Green
        $found = $true
        break
    }
}

if ($found) {
    Write-Host ""
    Write-Host "=" * 60
    Write-Host "  Installation Complete!" -ForegroundColor Green
    Write-Host "=" * 60
    Write-Host ""
    Write-Host "[INFO] You can now create single-file packages:" -ForegroundColor Cyan
    Write-Host "  cd legodo_dart" -ForegroundColor Yellow
    Write-Host "  python build_release.py windows --single-exe" -ForegroundColor Yellow
} else {
    Write-Host "[ERROR] Enigma Virtual Box not found after installation" -ForegroundColor Red
    Write-Host "[INFO] Please check the installation manually." -ForegroundColor Yellow
    exit 1
}
