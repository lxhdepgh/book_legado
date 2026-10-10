# 一键打包 Flutter Windows Release（含 Python 后端）
# WebView + TTS 已改为 Dart 原生实现，但核心引擎仍需 Python
# 输出到 legodo_dart/build/windows/x64/runner/Release/

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DartClient = Join-Path $ProjectRoot "legodo_dart"
$BackendDir = Join-Path $DartClient "backend"
$OutputDir = Join-Path $DartClient "build\windows\x64\runner\Release"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Legodo BookSource Check - Full Build" -ForegroundColor Cyan
Write-Host "  (WebView/TTS: Pure Dart)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Pack Python backend
Write-Host "[1/3] Packing Python backend..." -ForegroundColor Yellow
& (Join-Path $ProjectRoot "build_backend.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Backend packing failed" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Step 2: Flutter build windows --release
Write-Host "[2/3] Building Flutter Windows Release..." -ForegroundColor Yellow
Set-Location $DartClient

$LegodoPython = Join-Path $BackendDir "LegadoReader.exe"
$LegodoMain = Join-Path $BackendDir "main.py"

& flutter build windows --release `
    --dart-define="LEGODO_PYTHON=$LegodoPython" `
    --dart-define="LEGODO_MAIN=$LegodoMain"

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Flutter build failed" -ForegroundColor Red
    exit 1
}

# Verify Flutter exe
$FlutterExe = Join-Path $OutputDir "legodo_client.exe"
if (-not (Test-Path $FlutterExe)) {
    Write-Host "[ERROR] Flutter exe not generated: $FlutterExe" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Flutter exe: $FlutterExe" -ForegroundColor Green
Write-Host ""

# Step 3: Copy backend to output directory
Write-Host "[3/3] Copying Python backend to output directory..." -ForegroundColor Yellow
$OutputBackendDir = Join-Path $OutputDir "backend"
if (Test-Path $OutputBackendDir) {
    Remove-Item -Recurse -Force $OutputBackendDir
}
Copy-Item -Recurse -Force $BackendDir $OutputBackendDir

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to copy backend" -ForegroundColor Red
    exit 1
}

# Final verification
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Build Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Executable:   $FlutterExe" -ForegroundColor White
Write-Host "Built-in backend: $OutputBackendDir\LegadoReader.exe" -ForegroundColor White
Write-Host ""
Write-Host "Launch: Double-click legodo_client.exe (auto-starts Python backend)" -ForegroundColor White
Write-Host ""
Write-Host "Note:" -ForegroundColor White
Write-Host "  - WebView browser uses webview_flutter (WebView2 Runtime required)" -ForegroundColor Gray
Write-Host "  - TTS uses flutter_tts (Windows SAPI, no pyttsx3 needed)" -ForegroundColor Gray
Write-Host ""

Get-ChildItem $FlutterExe | Select-Object Name, Length, LastWriteTime
Get-ChildItem (Join-Path $OutputBackendDir "LegadoReader.exe") | Select-Object Name, Length, LastWriteTime
