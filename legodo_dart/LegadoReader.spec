# -*- mode: python ; coding: utf-8 -*-
# M7 PyInstaller spec（集成方执行打包回归）。见 docs/M7打包说明.md §5。
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    # 网络
    "aiohttp", "aiohttp_socks",
    # 规则兼容层
    "quickjs", "elementpath", "jsonpath_ng.ext", "bs4", "html5lib", "pyexpat",
    "PIL", "Crypto", "opencc", "fontTools", "charset_normalizer", "regex",
    # 服务懒加载
    "feedparser", "ebooklib", "lxml", "fitz",
    "pyttsx3", "edge_tts", "qrcode",
    "py7zr", "rarfile",
    
    # 【核心服务层】必须显式声明（PyInstaller 无法静态分析懒加载的服务）
    # Gateway + Engine Hub
    "app.services.gateway.service",
    "app.gateway.methods",  # action 表注册模块（必须导入才能填充 BY_ACTION）
    "app.core.engine_worker",
    "app.core.hub",
    # M1: 阅读核心
    "app.services.reading.book_cache_service",
    "app.services.reading.reading_service",
    "app.services.reading.download_service",
    "app.services.reading.bookmark_service",
    "app.services.reading.content_search",
    "app.services.reading.image_book_service",
    # M2: 引擎层
    "app.services.engine.rule_engine_service",
    "app.services.engine.legado_js",
    "app.services.engine.public_suffix",
    # M3: 书源域
    "app.services.sources.source_service",
    "app.services.sources.source_check_service",
    "app.services.sources.rule_repair_service",
    "app.services.sources.compat_audit_service",
    "app.services.sources.online_import_service",
    # M4: 内容形态域
    "app.services.content.rss_service",
    "app.services.content.local_book_service",
    "app.services.content.read_aloud",
    "app.services.content.media_play",
    # M5: 工具域
    "app.services.tools.background_service",
    "app.services.tools.book_export_service",
    "app.services.tools.font_service",
    "app.services.tools.qr_code_service",
    "app.services.tools.replace_rule_service",
    "app.services.tools.theme_service",
    "app.services.tools.update_service",
    "app.services.tools.web_dav_service",
    "app.services.tools.web_bridge_service",
    "app.services.tools.browser_service",
    # Core utilities
    "app.core.storage",
]
# bs4 / 动态 loader 子包兜底
hiddenimports += collect_submodules("ebooklib")

# 【排除项】Dart版本不需要的重型依赖（节省 ~310MB）
excludes = [
    "playwright",           # 103MB - Dart用webview_flutter替代
    "flet",                 # 97MB - --serve无头模式不需要Flet UI框架
    "flet_desktop",         # Flet桌面组件
    "flet_audio",
    "flet_video",
    "flet_webview",
    "cv2",                  # 112MB - QR码可用更轻量方案（如qrcode库本身就够了）
]

# 计算项目根目录和 app 源码路径（使用运行时相对路径）
_script_dir = os.getcwd()  # legodo_dart/ 目录
_app_source = os.path.join(_script_dir, "app")  # 本地后端副本（与顶层 app/ 分离）

a = Analysis(
    [os.path.join(_app_source, "main.py")],  # 打包入口：本地 app/main.py
    pathex=[_script_dir],  # legodo_dart/ 目录作为搜索路径
    binaries=[
        # conda python 3.12 的 VC runtime + UCRT 全套 DLL（从 conda 根目录）
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
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\libexpat.dll", "."),
        # 【关键DLL】conda 环境 C 扩展必需的动态库
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\sqlite3.dll", "."),
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\libssl-3-x64.dll", "."),
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\libcrypto-3-x64.dll", "."),
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\ffi.dll", "."),
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\liblzma.dll", "."),
        (r"D:\env_manger\minicaoda\envs\legodo_booksource_check\Library\bin\LIBBZ2.dll", "."),
    ],
    datas=[
        # 落点必须是 app/resources/（public_suffix._DATA 按 __file__ 相对定位）
        (os.path.join(_app_source, "resources", "publicsuffixes.gz"), "app/resources"),
        # action 入参 schema（jsonschema_min.load() 按 __file__ 相对定位）
        (os.path.join(_app_source, "gateway", "schema", "*.json"), "app/gateway/schema"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "base_history", "other_history", "old_code"] + excludes,
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
