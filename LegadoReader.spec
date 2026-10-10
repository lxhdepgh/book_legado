# -*- mode: python ; coding: utf-8 -*-
# M7 PyInstaller spec（集成方执行打包回归）。见 docs/M7打包说明.md §5。
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    # [UI 模式] Flet 桌面 UI 框架（--serve 无头模式不需要这些包）。
    # G4-F1 已让服务层彻底脱离 flet；G4-F2 待 Dart 工具域完成后移除。
    "flet", "flet_audio", "flet_video", "flet_webview",
    # 网络
    "aiohttp", "aiohttp_socks",
    # 规则兼容层
    "quickjs", "elementpath", "jsonpath_ng.ext", "bs4", "html5lib", "pyexpat",
    "PIL", "Crypto", "opencc", "fontTools", "charset_normalizer", "regex",
    # 浏览器
    "playwright.async_api",
    # 服务懒加载
    "feedparser", "ebooklib", "lxml", "fitz",
    "pyttsx3", "edge_tts", "qrcode", "cv2",
    "py7zr", "rarfile",
]
# bs4 / 动态 loader 子包兜底
hiddenimports += collect_submodules("ebooklib")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[
        # conda python 3.12 的 C 扩展依赖 conda 根 VC runtime + UCRT 全套 DLL；
        # 本机系统缺 api-ms-win-crt-* 存根（精简 Windows），PyInstaller 对 conda
        # 环境会漏收集 —— 显式打入避免 "DLL load failed while importing pyexpat"
        (r"D:\env_manger\minicaoda\vcruntime140.dll", "."),
        (r"D:\env_manger\minicaoda\vcruntime140_1.dll", "."),
        (r"D:\env_manger\minicaoda\vcruntime140_threads.dll", "."),
        (r"D:\env_manger\minicaoda\msvcp140.dll", "."),
        (r"D:\env_manger\minicaoda\msvcp140_1.dll", "."),
        (r"D:\env_manger\minicaoda\msvcp140_2.dll", "."),
        (r"D:\env_manger\minicaoda\msvcp140_atomic_wait.dll", "."),
        (r"D:\env_manger\minicaoda\msvcp140_codecvt_ids.dll", "."),
        (r"D:\\env_manger\\minicaoda\\ucrtbase.dll", "."),
        # 钉住与 pyexpat.pyd 同源的 libexpat.dll（env expat 2.8.2）。
        # PyInstaller 误收 conda 根旧版 expat 2.7.1，导致 pyexpat 按序号导入失败
        # （DLL load failed while importing pyexpat，错误 182）。此条目覆盖自动收集。
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\libexpat.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-conio-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-convert-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-environment-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-filesystem-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-heap-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-locale-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-math-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-multibyte-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-private-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-process-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-runtime-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-stdio-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-string-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-time-l1-1-0.dll", "."),
        (r"D:\\env_manger\\minicaoda\\api-ms-win-crt-utility-l1-1-0.dll", "."),
    ],
    datas=[
        # 落点必须是 app/resources/（public_suffix._DATA 按 __file__ 相对定位）
        ("app/resources/publicsuffixes.gz", "app/resources"),
        # action 入参 schema（jsonschema_min.load() 按 __file__ 相对定位）
        ("app/gateway/schema/*.json", "app/gateway/schema"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "base_history", "other_history", "old_code"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LegadoReader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 桌面应用 windowed 形态
    target_arch=None,
)

# onedir 目录化：DLL/pyd 落 exe 旁真实目录，加载器搜索行为最标准
# （单文件模式 _MEIPASS 下 conda 的 libexpat.dll 依赖解析失败，见 docs/M7打包说明 §7）
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=False,
    name="LegadoReader",
)
