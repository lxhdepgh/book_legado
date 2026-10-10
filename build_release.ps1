# 一键打包 Flutter Windows Release（纯 Dart 实现，无 Python 依赖）
# WebView + TTS 使用跨平台插件，无需后端服务
# 输出到 legodo_dart/build/windows/x64/runner/Release/

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DartClient = Join-Path $ProjectRoot "legodo_dart"
$OutputDir = Join-Path $DartClient "build\windows\x64\runner\Release"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Legodo BookSource Check - Full Build" -ForegroundColor Cyan
Write-Host "  Pure Dart (WebView + TTS)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Flutter build windows --release
Write-Host "[1/2] Building Flutter Windows Release..." -ForegroundColor Yellow
Set-Location $DartClient

& flutter build windows --release

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

# Step 2: Generate release package
Write-Host "[2/2] Generating release package..." -ForegroundColor Yellow
$ReleaseZip = Join-Path $ProjectRoot "legodo_client_windows.zip"
if (Test-Path $ReleaseZip) {
    Remove-Item -Force $ReleaseZip
}

# Create zip with all files from Release directory
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($OutputDir, $ReleaseZip)

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to create zip package" -ForegroundColor Red
    exit 1
}

# Final verification
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Build Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Executable:   $FlutterExe" -ForegroundColor White
Write-Host "Package:      $ReleaseZip" -ForegroundColor White
Write-Host ""
Write-Host "Features:" -ForegroundColor White
Write-Host "  - WebView browser (WebView2 Runtime required on Win10/11)" -ForegroundColor Gray
Write-Host "  - TTS text-to-speech (Windows SAPI)" -ForegroundColor Gray
Write-Host "  - No Python backend required" -ForegroundColor Gray
Write-Host ""

Get-ChildItem $FlutterExe | Select-Object Name, Length, LastWriteTime
Get-ChildItem $ReleaseZip | Select-Object Name, Length, LastWriteTime
