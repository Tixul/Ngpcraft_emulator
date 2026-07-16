@echo off
REM ---------------------------------------------------------------------------
REM Build a standalone Windows executable of NgpCraft Emulator.
REM
REM The result is ONE self-contained file, release\NgpCraftEmulator.exe, that
REM end users just double-click -- no Python, no pip, no install of any kind.
REM The native core DLL and the app icon are bundled inside it; ROMs, saves,
REM screenshots and bios.bin live in folders next to the .exe.
REM
REM Requires PyInstaller once:   pip install pyinstaller
REM Then just run:               build_exe.bat
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

pyinstaller --noconfirm --onefile --windowed ^
  --name NgpCraftEmulator ^
  --icon "assets\icone_ngpcraft.ico" ^
  --add-data "cpp\build\ngpc_core.dll;cpp\build" ^
  --add-data "assets\icone_ngpcraft.ico;assets" ^
  --distpath "release" ^
  --workpath "build\pyinstaller" ^
  ngpc_shell.py

echo.
if exist "release\NgpCraftEmulator.exe" (
  echo Done  ^>^>  release\NgpCraftEmulator.exe
) else (
  echo Build FAILED - see the PyInstaller output above.
)
endlocal
