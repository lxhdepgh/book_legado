# 打包 Python 后端为独立 exe（供 Flutter 嵌入）
# 输出到 flutter_client/backend/ 目录

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$FlutterClient = Join-Path $ProjectRoot "flutter_client"
$BackendDir = Join-Path $FlutterClient "backend"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Packing Python Backend" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Clean old output
if (Test-Path $BackendDir) {
    Remove-Item -Recurse -Force $BackendDir
}
New-Item -ItemType Directory -Path $BackendDir | Out-Null

# Activate conda environment and run PyInstaller
$CondaActivate = "D:\env_manger\minicaoda\envs\legodo_booksource_check\Scripts\activate.bat"
$PythonExe = "D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe"

Write-Host "[1/2] Running PyInstaller..." -ForegroundColor Yellow
Set-Location $ProjectRoot
& $PythonExe -m PyInstaller --clean --noconfirm LegadoReader.spec

# Copy output to backend directory
$DistDir = Join-Path $ProjectRoot "dist\LegadoReader"
if (Test-Path $DistDir) {
    Copy-Item -Recurse -Force $DistDir\* $BackendDir
    Write-Host "[OK] Backend packed to: $BackendDir" -ForegroundColor Green
} else {
    Write-Host "[ERROR] PyInstaller output not found at: $DistDir" -ForegroundColor Red
    exit 1
}

# Verify key files
$BackendExe = Join-Path $BackendDir "LegadoReader.exe"
if (-not (Test-Path $BackendExe)) {
    Write-Host "[ERROR] LegadoReader.exe missing in backend directory" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] Backend exe ready: $BackendExe" -ForegroundColor Green
Get-ChildItem $BackendExe | Select-Object Name, Length, LastWriteTime
