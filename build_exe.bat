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
REM
REM WHAT goes in the build -- entry point, icon, onefile, windowed, and every
REM bundled resource -- lives in NgpCraftEmulator.spec. This script only says
REM WHERE to put the result. That split is deliberate: this file used to repeat
REM the --add-data list, drifted from the spec, and was quietly shipping an .exe
REM missing resources the spec already bundled. CI builds from the same spec
REM (.github/workflows/build.yml), so local and CI builds cannot diverge again.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

pyinstaller --noconfirm --clean ^
  --distpath "release" ^
  --workpath "build\pyinstaller" ^
  NgpCraftEmulator.spec

REM Check the EXIT CODE, not just whether the file is there: a build that fails
REM (PyInstaller cannot overwrite a running or locked .exe, for one) leaves the
REM PREVIOUS .exe in place, and "if exist" then cheerfully reports success while
REM you ship a stale binary. Ask me how I know.
if errorlevel 1 (
  echo.
  echo Build FAILED - see the PyInstaller output above.
  echo If it says "Acces refuse" / "Access is denied": the .exe is still running,
  echo or your antivirus is holding it. Close it and run this again.
  endlocal
  exit /b 1
)

echo.
if exist "release\NgpCraftEmulator.exe" (
  echo Done  ^>^>  release\NgpCraftEmulator.exe
) else (
  echo Build FAILED - PyInstaller claimed success but produced no .exe.
  endlocal
  exit /b 1
)
endlocal
