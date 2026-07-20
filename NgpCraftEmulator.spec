# -*- mode: python ; coding: utf-8 -*-
#
# One spec, three platforms. PyInstaller never cross-compiles: this file runs ON
# each target OS (locally or in CI) and branches on sys.platform so the same
# command produces a Windows .exe, a Linux binary, or a macOS .app -- see
# .github/workflows/build.yml.

import sys

# The native core keeps the same stem on every OS (CMake strips the `lib` prefix);
# only the extension changes. This MUST match core.native._DLL_NAME.
if sys.platform == "win32":
    _CORE = "ngpc_core.dll"
    _ICON = "assets/icone_ngpcraft.ico"
elif sys.platform == "darwin":
    _CORE = "ngpc_core.dylib"
    # A .icns if one is checked in, else no icon (PyInstaller ignores a missing one
    # only if it is None -- a missing path would error, so gate on the file).
    import os
    _ICON = "assets/icone_ngpcraft.icns" if os.path.exists(
        "assets/icone_ngpcraft.icns") else None
else:
    _CORE = "ngpc_core.so"
    _ICON = None                       # Linux desktop icons come from a .desktop file

# UPX shrinks the Windows build; on macOS it breaks code signing and on Linux it
# buys little for the risk, so keep it to Windows only.
_UPX = sys.platform == "win32"


a = Analysis(
    ['ngpc_shell.py'],
    pathex=[],
    binaries=[],
    # Every runtime resource must be listed here: PyInstaller follows IMPORTS, not
    # file reads, so an asset opened by path is invisible to it and simply goes
    # missing from the build.
    datas=[(f'cpp/build/{_CORE}', 'cpp/build'),
           ('assets/icone_ngpcraft.ico', 'assets'),
           ('assets/ngpc_console.png', 'assets')],
    hiddenimports=[],
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
    name='NgpCraftEmulator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=_UPX,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[_ICON] if _ICON else None,
)

# macOS wants an application bundle, not a bare Unix executable, or Finder and the
# Dock treat it as a terminal tool. The EXE above is what goes inside it.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name='NgpCraftEmulator.app',
        icon=_ICON,
        bundle_identifier='com.ngpcraft.emulator',
    )
