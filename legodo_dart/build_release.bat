@echo off
REM 一键打包 Flutter Windows Release（含 Python 后端）
REM 输出到 flutter_client/build/windows/x64/runner/Release/

setlocal enabledelayedexpansion

echo ========================================
echo   Legado BookSource Check - 完整打包
echo ========================================
echo.

REM 获取项目根目录
set "PROJECT_ROOT=%~dp0"
set "FLUTTER_CLIENT=%PROJECT_ROOT%flutter_client"
set "BACKEND_DIR=%FLUTTER_CLIENT%\backend"
set "OUTPUT_DIR=%FLUTTER_CLIENT%\build\windows\x64\runner\Release"

REM 步骤 1: 打包 Python 后端
echo [1/3] 打包 Python 后端...
call "%PROJECT_ROOT%build_backend.bat"
if errorlevel 1 (
    echo [ERROR] 后端打包失败
    exit /b 1
)
echo.

REM 步骤 2: Flutter build windows --release
echo [2/3] 构建 Flutter Windows Release...
cd /d "%FLUTTER_CLIENT%"

REM 设置编译期常量（指向打包的 backend exe）
set "LEGODO_PYTHON=%BACKEND_DIR%\LegadoReader.exe"
set "LEGODO_MAIN=%BACKEND_DIR%\main.py"

flutter build windows --release ^
  --dart-define=LEGODO_PYTHON=%LEGODO_PYTHON% ^
  --dart-define=LEGODO_MAIN=%LEGODO_MAIN%

if errorlevel 1 (
    echo [ERROR] Flutter 构建失败
    exit /b 1
)

REM 验证 Flutter exe
if not exist "%OUTPUT_DIR%\legodo_client.exe" (
    echo [ERROR] Flutter exe 未生成: %OUTPUT_DIR%\legodo_client.exe
    exit /b 1
)
echo [OK] Flutter exe: %OUTPUT_DIR%\legodo_client.exe
echo.

REM 步骤 3: 复制 backend 到输出目录
echo [3/3] 复制 Python 后端到输出目录...
if exist "%OUTPUT_DIR%\backend" rmdir /s /q "%OUTPUT_DIR%\backend"
xcopy /e /i /y "%BACKEND_DIR%" "%OUTPUT_DIR%\backend"

if errorlevel 1 (
    echo [ERROR] 复制 backend 失败
    exit /b 1
)

REM 验证最终输出
echo.
echo ========================================
echo   打包完成！
echo ========================================
echo.
echo 可执行文件: %OUTPUT_DIR%\legodo_client.exe
echo 内置后端:   %OUTPUT_DIR%\backend\LegadoReader.exe
echo.
echo 启动方式: 双击 legodo_client.exe（自动拉起 Python 后端）
echo.

dir "%OUTPUT_DIR%\legodo_client.exe" | findstr "legodo_client.exe"
dir "%OUTPUT_DIR%\backend\LegadoReader.exe" | findstr "LegadoReader.exe"
