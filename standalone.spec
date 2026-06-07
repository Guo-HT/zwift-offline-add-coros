# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['standalone.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # COROS upload path does `import coros_client` at runtime (inside
    # coros_upload() in zwift_offline.py), so PyInstaller's static analysis
    # misses it. Add the module and everything it (transitively) needs so
    # standalone.exe doesn't fail with "No module named 'coros_client'".
    hiddenimports=[
        'coros_client',
        'oss',
        'oss.ali_oss_client',
        'oss.aws_oss_client',
        'oss2',
        'oss2.models',
        'boto3',
        'boto3.s3',
        'boto3.s3.transfer',
    ],
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
    name='standalone',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
