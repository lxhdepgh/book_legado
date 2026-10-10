@echo off
REM 打包 Python 后端为独立 exe（供 Flutter 嵌入）
REM 输出到 flutter_client/backend/ 目录

setlocal enabledelayedexpansion

REM 获取项目根目录
set "PROJECT_ROOT=%~dp0"
set "FLUTTER_CLIENT=%PROJECT_ROOT%flutter_client"
set "BACKEND_DIR=%FLUTTER_CLIENT%\backend"

REM 清理旧输出
if exist "%BACKEND_DIR%" rmdir /s /q "%BACKEND_DIR%"
mkdir "%BACKEND_DIR%"

REM 使用 PyInstaller 打包
cd /d "%PROJECT_ROOT%"
call D:\env_manger\minicaoda\envs\legodo_booksource_check\Scripts\activate.bat
python -m PyInstaller --clean --noconfirm LegadoReader.spec

REM 复制输出到 backend 目录
if exist "%PROJECT_ROOT%dist\LegadoReader" (
    xcopy /e /i /y "%PROJECT_ROOT%dist\LegadoReader" "%BACKEND_DIR%"
    echo [OK] Backend packed to: %BACKEND_DIR%
) else (
    echo [ERROR] PyInstaller output not found at dist\LegadoReader
    exit /b 1
)

REM 验证关键文件
if not exist "%BACKEND_DIR%\LegadoReader.exe" (
    echo [ERROR] LegadoReader.exe missing in backend directory
    exit /b 1
)

echo [OK] Backend exe ready: %BACKEND_DIR%\LegadoReader.exe
dir "%BACKEND_DIR%\LegadoReader.exe" | findstr "LegadoReader.exe"
