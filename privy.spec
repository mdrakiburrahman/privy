# PyInstaller spec for the self-contained `privy` binary.
# Build with: ./scripts/build_binary.sh

a = Analysis(
    ["src/privy/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        "privy.cli",
        "privy.client",
        "privy.executor",
        "privy.protocol",
        "privy.proxy",
        "privy.server",
        "privy._relay",
        "websocket",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="privy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
