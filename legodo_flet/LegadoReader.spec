# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\WorkSpace\\PycharmProjects\\legodo_booksource_check\\legodo_flet\\app\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\WorkSpace\\PycharmProjects\\legodo_booksource_check\\legodo_flet\\app\\gateway\\schema', 'app/gateway/schema'), ('D:\\WorkSpace\\PycharmProjects\\legodo_booksource_check\\legodo_flet\\app\\resources\\publicsuffixes.gz', 'app/resources')],
    hiddenimports=['app.services.gateway.service', 'app.gateway.methods', 'app.core.engine_worker', 'app.core.service_hub', 'app.services.reading.book_cache_service', 'app.services.reading.reading_service', 'app.services.reading.download_service', 'app.services.reading.bookmark_service', 'app.services.reading.content_search_service', 'app.services.reading.image_book_service', 'app.services.engine.rule_engine_service', 'app.services.engine.legado_js', 'app.services.engine.public_suffix', 'app.services.sources.source_service', 'app.services.sources.source_check_service', 'app.services.sources.rule_repair_service', 'app.services.sources.compat_audit_service', 'app.services.sources.online_import_service', 'app.services.content.rss_service', 'app.services.content.local_book_service', 'app.services.content.media_play', 'app.services.tools.background_service', 'app.services.tools.book_export_service', 'app.services.tools.font_service', 'app.services.tools.qr_code_service', 'app.services.tools.replace_rule_service', 'app.services.tools.theme_service', 'app.services.tools.update_service', 'app.services.tools.web_dav_service', 'app.services.tools.web_bridge_service', 'app.services.tools.browser_service', 'app.core.storage'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LegadoReader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='C:\\Users\\73517\\AppData\\Local\\Temp\\fd7764e7-95aa-4e50-9382-76db134c919c',
)
