@echo off
REM 一键启动 Dart 版本（自动拉起 Python 服务）
cd /d "%~dp0legodo_dart"
flutter run -d windows ^
  --dart-define=LEGODO_PYTHON=D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe ^
  --dart-define=LEGODO_MAIN=D:\WorkSpace\PycharmProjects\legodo_booksource_check\main.py
