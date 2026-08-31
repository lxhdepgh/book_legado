@echo off
setlocal
cd /d "%~dp0"

set "FLET_CLI=D:\env_manger\minicaoda\envs\legodo_booksource_check\Scripts\flet.exe"
if not exist "%FLET_CLI%" set "FLET_CLI=flet"

"%FLET_CLI%" build apk . --yes --clear-cache --python-version 3.12 --split-per-abi ^
  --source-packages quickjs pycryptodomex brotli psutil backports.zstd ^
                    pyppmd pybcj inflate64 ^
  --project legadoreader --artifact LegadoReader --product "Legado Reader" ^
  --exclude base_history build dist .git .idea .pytest_cache __pycache__ ^
            devlog devProblem diff docs tests tools ^
            "test_*.py" "*.spec" "build_windows.*" "build_android.cmd" %*

if errorlevel 1 (
  echo.
  echo Android APK build failed. Review the Flet output above; native Python
  echo dependencies must provide Android wheels for every target ABI.
  exit /b 1
)

echo.
echo Android APK build completed: build\apk
exit /b 0
