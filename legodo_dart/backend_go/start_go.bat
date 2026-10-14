@echo off
REM 一键启动 Go 后端（源码直接运行）
REM 用法: start_go.bat

echo ========================================
echo   Legodo Go 后端启动脚本
echo ========================================
echo.

REM 检查 Go 是否安装
where go >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Go 命令
    echo 请先安装 Go: https://go.dev/dl/
    pause
    exit /b 1
)

echo [1/3] 检查 Go 环境...
go version
echo.

echo [2/3] 进入项目目录...
cd /d "%~dp0"
echo 当前目录: %CD%
echo.

echo [3/3] 启动 Go 后端服务...
echo.
echo ========================================
echo   服务地址: http://127.0.0.1:8080
echo   WebSocket: ws://127.0.0.1:8080/rpc
echo   健康检查: http://127.0.0.1:8080/health
echo ========================================
echo.
echo 按 Ctrl+C 停止服务
echo.

go run ./cmd/server/
