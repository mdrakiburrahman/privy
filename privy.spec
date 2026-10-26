# PyInstaller spec for the self-contained `privy` binary.
# Build with: ./scripts/build_binary.sh

import sys

from PyInstaller.utils.hooks import collect_data_files

terminal_data = collect_data_files("winpty", includes=["*.exe"]) if sys.platform == "win32" else []

a = Analysis(
    ["src/privy/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[("src/privy/hub/assets", "privy/hub/assets"), *terminal_data],
    hiddenimports=[
        "privy.batch",
        "privy.cli",
        "privy.client",
        "privy.executor",
        "privy.hub",
        "privy.hub.app",
        "privy.hub.config",
        "privy.hub.console",
        "privy.hub.guide",
        "privy.hub.home",
        "privy.hub.jobs",
        "privy.hub.mcp",
        "privy.hub.registry",
        "privy.hub.render",
        "privy.hub.responses",
        "privy.hub.rest",
        "privy.hub.service",
        "privy.hub.sync",
        "privy.info",
        "privy.protocol",
        "privy.proxy",
        "privy.server",
        "privy.transfer",
        "privy.terminal",
        "privy.terminal_backends",
        "privy.hub.terminals",
        "privy._logfmt",
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
