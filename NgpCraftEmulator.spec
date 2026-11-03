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
    # Reuse the existing official icon. PyInstaller converts .ico to .icns on
    # macOS with Pillow unless a native .icns is provided in the future.
    import os
    _ICON = next((path for path in ("assets/icone_ngpcraft.icns",
                                    "assets/icone_ngpcraft.ico")
                  if os.path.isfile(path)), None)
else:
    _CORE = "ngpc_core.so"
    _ICON = None                       # Linux desktop icons come from a .desktop file

# Keep Windows/Linux as onefile builds. A macOS .app is already a directory;
# onefile needlessly unpacks Python and Qt on every launch.
_MACOS = sys.platform == "darwin"
# UPX is kept Windows-only; on macOS it interferes with code signing.
_UPX = sys.platform == "win32"


a = Analysis(
    ['ngpc_shell.py'],
    pathex=[],
    binaries=[],
    # Every runtime resource must be listed here: PyInstaller follows IMPORTS, not
    # file reads, so an asset opened by path is invisible to it and simply goes
    # missing from the build.
    # 'lang' is a whole directory on purpose: languages are discovered by
    # globbing it at import time, so a new lang/pt.json must ship with no edit
    # here either -- naming the files one by one would silently drop it.
    datas=[(f'cpp/build/{_CORE}', 'cpp/build'),
           # The clean-room HLE BIOS: without it the packaged app has NO BIOS at
           # all and "no bios.bin needed" — the README's first promise — is not
           # true of the thing people download. It was missing here for months.
           ('hle_bios/bios_hle.bin', 'hle_bios'),
           ('assets/icone_ngpcraft.ico', 'assets'),
           ('assets/ngpc_console.png', 'assets'),
           ('lang', 'lang')],
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
    *([] if _MACOS else [a.binaries, a.datas]),
    [],
    name='NgpCraftEmulator',
    exclude_binaries=_MACOS,
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
if _MACOS:
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='NgpCraftEmulator',
    )
    app = BUNDLE(
        collected,
        name='NgpCraftEmulator.app',
        icon=_ICON,
        bundle_identifier='com.ngpcraft.emulator',
    )
