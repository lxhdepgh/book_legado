# 构建与打包

**这一页只是路标。** 构建的权威文档是 **[`legodo_dart/README_BUILD.md`](legodo_dart/README_BUILD.md)**，
本文件不复述它的参数与产物清单（旧版本复述过，结果每次改构建这里都要坏一次——
它当时写的"Dart 通过 RPC 调用 Python 后端""包含 `backend/` 目录""Dart 仅桌面端"三句全已作废，
引用的 `legodo_flet/BUILD.md` 与 `legodo_dart/DEPLOYMENT.md` 也从来不存在）。

## 现状一句话

主线是**单进程 FFI**：`legodo_client.exe`（Flutter AOT）用 `dart:ffi` 加载同目录的
`legodo_engine.dll`（Go `c-shared` 规则引擎）。构建链**不涉及** Python 后端、PyInstaller、
Enigma Virtual Box，也没有服务进程和网络端口。

```bash
cd legodo_dart
python build_release.py windows        # 产物 dist/legodo_windows/ 与 legodo_windows.zip
```

单独重编引擎：`cd legodo_dart/backend_go && go build -buildmode=c-shared -o legodo_engine.dll ./pkg/ffi/`
（Windows 需要 `C:\mingw64\bin` 排在 PATH 前面，否则 `go build` 只报 `exit status 2` 而不吐编译错误）。
其他目标：`android`（Go 编 `.so` + `flutter build apk`）、`linux`、`macos`。
参数表、产物校验清单、PDB 剔除之类的内容都在 `README_BUILD.md`。

## 已废弃的打包路线（别再照这些找）

| 路线 | 结论 | 原始记录 |
|---|---|---|
| Flutter exe + PyInstaller `backend/LegadoReader.exe` 子进程 | 双进程 WS/RPC 链已从双侧拆除（`db778cd`） | `docs/archive/双进程与单exe记录/`（整簇） |
| Go 独立服务 + WebSocket `:8080/rpc` | 同上 | 同上 |
| Enigma Virtual Box 把 FFI 多文件合单 exe | 已从构建链移除 | `docs/archive/双进程与单exe记录/single-exe-changelog.md` |
| Chaquopy 把 Python 打进 Android APK | Chaquopy 12.0.0（2022）停更，与 AGP 8.x 不兼容 | `docs/archive/平台与打包评估/Android后端打包可行性评估.md` |
| `flet build apk/aab` 出 Android 包 | 试过，放弃 | `docs/archive/flet界面优化20261011/flet架构恢复方案.md` |

## `legodo_flet/` 那一侧

`legodo_flet/build_release.py`（基于 `flet pack`）**仍在树内**，但那是参考实现的打包线：
flet 子项目现在只作功能 parity 参照、不再演进。**本轮没有跑过它**，不保证可用；
要用之前先读 `docs/文档总览.md` §2 的裁决表与 `docs/current-system-design.md` §8。
