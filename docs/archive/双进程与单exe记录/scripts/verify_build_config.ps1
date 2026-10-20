# 构建配置验证脚本 (PowerShell)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GoBackend = Join-Path $ScriptDir "backend_go"
$GoFfiSrc = Join-Path $GoBackend "pkg\ffi\engine.go"

Write-Host "=" * 60
Write-Host "  Legodo BookSource Check - Build Configuration Verifier" -ForegroundColor Cyan
Write-Host "=" * 60

$results = @{}

# [1/5] 检查 Go FFI 源码
Write-Host "`n[1/5] Checking Go FFI source code..." -ForegroundColor Yellow

if (Test-Path $GoFfiSrc) {
    $content = Get-Content $GoFfiSrc -Raw -Encoding UTF8
    
    $checks = @(
        @("package main", "Package declaration"),
        @('import "C"', "CGO import"),
        @("func main()", "Main function"),
        @("//export Initialize", "Initialize export"),
        @("//export SearchBooks", "SearchBooks export"),
        @("//export GetChapters", "GetChapters export"),
        @("//export GetContent", "GetContent export"),
        @("//export Cleanup", "Cleanup export")
    )
    
    $allOk = $true
    foreach ($check in $checks) {
        $pattern = $check[0]
        $desc = $check[1]
        if ($content -match [regex]::Escape($pattern)) {
            Write-Host "[OK]   $desc" -ForegroundColor Green
        } else {
            Write-Host "[ERROR] ${desc}: MISSING" -ForegroundColor Red
            $allOk = $false
        }
    }
    $results["Go FFI Source"] = $allOk
} else {
    Write-Host "[ERROR] Go FFI source not found: $GoFfiSrc" -ForegroundColor Red
    $results["Go FFI Source"] = $false
}

# [2/5] 检查已编译的库
Write-Host "`n[2/5] Checking compiled libraries..." -ForegroundColor Yellow

$dll = Join-Path $GoBackend "legodo_engine.dll"
$so = Join-Path $GoBackend "liblegodo_engine.so"
$dylib = Join-Path $GoBackend "liblegodo_engine.dylib"

$foundAny = $false

if (Test-Path $dll) {
    $sizeMB = [math]::Round((Get-Item $dll).Length / 1MB, 1)
    Write-Host "[OK]   Windows DLL: legodo_engine.dll ($sizeMB MB)" -ForegroundColor Green
    $foundAny = $true
}

if (Test-Path $so) {
    $sizeMB = [math]::Round((Get-Item $so).Length / 1MB, 1)
    Write-Host "[OK]   Linux SO: liblegodo_engine.so ($sizeMB MB)" -ForegroundColor Green
    $foundAny = $true
}

if (Test-Path $dylib) {
    $sizeMB = [math]::Round((Get-Item $dylib).Length / 1MB, 1)
    Write-Host "[OK]   macOS DYLIB: liblegodo_engine.dylib ($sizeMB MB)" -ForegroundColor Green
    $foundAny = $true
}

if (-not $foundAny) {
    Write-Host "[WARN]  No compiled libraries found (run build to create them)" -ForegroundColor Yellow
}

$results["Compiled Libraries"] = $foundAny

# [3/5] 检查 Flutter 依赖
Write-Host "`n[3/5] Checking Flutter dependencies..." -ForegroundColor Yellow

$pubspec = Join-Path $ScriptDir "pubspec.yaml"
if (Test-Path $pubspec) {
    $content = Get-Content $pubspec -Raw -Encoding UTF8
    
    $requiredDeps = @("webview_flutter", "flutter_tts", "web_socket_channel")
    $allOk = $true
    
    foreach ($dep in $requiredDeps) {
        if ($content -match [regex]::Escape($dep)) {
            Write-Host "[OK]   Dependency: $dep" -ForegroundColor Green
        } else {
            Write-Host "[ERROR] Missing dependency: $dep" -ForegroundColor Red
            $allOk = $false
        }
    }
    $results["Flutter Dependencies"] = $allOk
} else {
    Write-Host "[ERROR] pubspec.yaml not found" -ForegroundColor Red
    $results["Flutter Dependencies"] = $false
}

# [4/5] 检查 Windows DLL
Write-Host "`n[4/5] Checking Windows runtime DLLs..." -ForegroundColor Yellow

$flutterDll = Join-Path $ScriptDir "windows\flutter\ephemeral\flutter_windows.dll"
if (Test-Path $flutterDll) {
    $sizeMB = [math]::Round((Get-Item $flutterDll).Length / 1MB, 1)
    Write-Host "[OK]   flutter_windows.dll: $sizeMB MB" -ForegroundColor Green
    $results["Windows Runtime DLLs"] = $true
} else {
    Write-Host "[WARN]  flutter_windows.dll not found (will be created during build)" -ForegroundColor Yellow
    $results["Windows Runtime DLLs"] = $true
}

# [5/5] 检查 Enigma Virtual Box
Write-Host "`n[5/5] Checking Enigma Virtual Box..." -ForegroundColor Yellow

$enigmaPaths = @(
    "C:\Program Files\Enigma Virtual Box\enigmavbconsole.exe",
    "C:\Program Files (x86)\Enigma Virtual Box\enigmavbconsole.exe"
)

$enigmaFound = $false
foreach ($path in $enigmaPaths) {
    if (Test-Path $path) {
        Write-Host "[OK]   Found: $path" -ForegroundColor Green
        $enigmaFound = $true
        break
    }
}

if (-not $enigmaFound) {
    Write-Host "[WARN]  Enigma Virtual Box not installed" -ForegroundColor Yellow
    Write-Host "[INFO]  Install it to enable single-file packaging:" -ForegroundColor Yellow
    Write-Host "          .\install_enigma_vbox.ps1" -ForegroundColor Yellow
}

$results["Enigma Virtual Box"] = $enigmaFound

# 打印总结
Write-Host "`n" + ("=" * 60)
Write-Host "  Build Configuration Summary" -ForegroundColor Cyan
Write-Host "=" * 60

foreach ($key in $results.Keys) {
    $status = if ($results[$key]) { "PASS" } else { "FAIL" }
    $color = if ($results[$key]) { "Green" } else { "Red" }
    Write-Host ("  {0,-40} " -f $key) -NoNewline
    Write-Host $status -ForegroundColor $color
}

$allPassed = ($results.Values | Where-Object { -not $_ }).Count -eq 0

Write-Host "`n" + ("=" * 60)
if ($allPassed) {
    Write-Host "  All checks passed! Ready to build." -ForegroundColor Green
} else {
    Write-Host "  Some checks failed. Please fix the issues above." -ForegroundColor Red
}
Write-Host "=" * 60

Write-Host "`nRecommended build commands:" -ForegroundColor Cyan
Write-Host "  Windows (single exe):  python build_release.py windows --single-exe" -ForegroundColor Yellow
Write-Host "  Windows (multi-file):  python build_release.py windows" -ForegroundColor Yellow
Write-Host "  Linux:                 python build_release.py linux" -ForegroundColor Yellow
Write-Host "  macOS:                 python build_release.py macos" -ForegroundColor Yellow
Write-Host "  Android:               python build_release.py android" -ForegroundColor Yellow
Write-Host ""
