# 测试单文件打包功能
$ErrorActionPreference = "Continue"

Write-Host "=" * 60 -ForegroundColor Cyan
Write-Host "  Testing Single EXE Build" -ForegroundColor Cyan
Write-Host "=" * 60 -ForegroundColor Cyan
Write-Host ""

# 切换到项目目录
Set-Location "D:\WorkSpace\PycharmProjects\legodo_booksource_check"

# 激活 conda 环境
& D:\env_manger\minicaoda\Scripts\activate.ps1 legodo_booksource_check

# 运行构建
Write-Host "[INFO] Running build_release.py windows --single-exe --skip-go..." -ForegroundColor Yellow
python legodo_dart/build_release.py windows --single-exe --skip-go

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=" * 60 -ForegroundColor Green
    Write-Host "  Build Succeeded!" -ForegroundColor Green
    Write-Host "=" * 60 -ForegroundColor Green
    
    # 检查输出文件
    $singleExe = "D:\WorkSpace\PycharmProjects\legodo_booksource_check\legodo_dart\dist\legodo_client.exe"
    if (Test-Path $singleExe) {
        $sizeMB = [math]::Round((Get-Item $singleExe).Length / 1MB, 1)
        Write-Host ""
        Write-Host "[OK] Single EXE created: $singleExe" -ForegroundColor Green
        Write-Host "[OK] Size: $sizeMB MB" -ForegroundColor Green
        
        Write-Host ""
        Write-Host "[INFO] You can test it by running:" -ForegroundColor Cyan
        Write-Host "  cd dist" -ForegroundColor Yellow
        Write-Host "  .\legodo_client.exe" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "[ERROR] Single EXE not found at expected location" -ForegroundColor Red
        Write-Host "[INFO] Checking alternative locations..." -ForegroundColor Yellow
        
        $possiblePaths = @(
            "D:\WorkSpace\PycharmProjects\legodo_booksource_check\legodo_dart\dist\legodo_client_windows\legodo_client.exe",
            "D:\WorkSpace\PycharmProjects\legodo_booksource_check\legodo_dart\legodo_client.exe"
        )
        
        foreach ($path in $possiblePaths) {
            if (Test-Path $path) {
                $sizeMB = [math]::Round((Get-Item $path).Length / 1MB, 1)
                Write-Host "[OK] Found at: $path ($sizeMB MB)" -ForegroundColor Green
            }
        }
    }
} else {
    Write-Host ""
    Write-Host "=" * 60 -ForegroundColor Red
    Write-Host "  Build Failed!" -ForegroundColor Red
    Write-Host "=" * 60 -ForegroundColor Red
    exit 1
}
