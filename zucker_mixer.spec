# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

block_cipher = None
ROOT = Path.cwd()

a = Analysis(
    ["mac_app.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
        ("build/build_metadata.json", "build"),
        ("whisper_transcribe.py", "."),
    ],
    hiddenimports=[
        "flask",
        "werkzeug",
        "webview",
        "numpy",
        "scipy",
        "scipy.signal",
        "scipy.ndimage",
        "soundfile",
        "pyloudnorm",
        "cffi",
        "_cffi_backend",
        "jam_app",
        "jam_mix_pipeline",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZuckerMixer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ZuckerMixer",
)

app = BUNDLE(
    coll,
    name="ZuckerMixer.app",
    icon="zucker.icns",
    bundle_identifier="local.zuckersessions.zuckermixer",
    info_plist={
        "CFBundleDisplayName": "ZuckerMixer",
        "CFBundleName": "ZuckerMixer",
        "CFBundleShortVersionString": os.environ.get("ZUCKER_APP_VERSION", "development"),
        "CFBundleVersion": os.environ.get("ZUCKER_APP_VERSION", "development"),
        "CFBundleGetInfoString": "ZuckerMixer — Built by JM.G (José Manuel García)",
        "NSHumanReadableCopyright": "© José Manuel García (JM.G). Built by JM.G.",
        "Author": "José Manuel García (JM.G)",
        "NSHighResolutionCapable": "True",
        "LSMinimumSystemVersion": "11.0",
        "ZuckerBuildTimestamp": os.environ.get("ZUCKER_BUILD_TIMESTAMP", "development build"),
        "ZuckerSourceRevision": os.environ.get("ZUCKER_SOURCE_REVISION", "unbuilt"),
    },
)
