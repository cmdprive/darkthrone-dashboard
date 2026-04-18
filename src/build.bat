@echo off
title DarkThrone Suite — Build
echo.
echo  ================================================
echo   DarkThrone Suite — Build
echo  ================================================
echo.

:: This file lives at  <darkthrone>\src\build.bat
:: It compiles from src\  and writes the result to  <darkthrone>\release\
cd /d "%~dp0"

:: ── Step 0: Add Python Scripts dir to PATH so pyinstaller is found ────────
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'Scripts'))"') do set PYBIN=%%i
set PATH=%PYBIN%;%PATH%

:: ── Step 1: Install build tools ───────────────────────────────────────────
echo  [1/5] Installing build tools...
python -m pip install pyinstaller anthropic --quiet
if errorlevel 1 (
    echo  [ERROR] Could not install build tools.
    pause & exit /b 1
)

:: ── Step 2: Compile source files to bytecode (.pyc) ───────────────────────
echo.
echo  [2/5] Compiling source to bytecode...
python -m compileall -b -q ^
    installer\darkthrone_app.py ^
    optimizer.py ^
    claude_strategy.py
if errorlevel 1 (
    echo  [ERROR] Bytecode compilation failed.
    pause & exit /b 1
)

:: ── Step 3: Bundle into .exe with PyInstaller ─────────────────────────────
echo.
echo  [3/5] Compiling to .exe (this takes a few minutes)...
if exist dist  rmdir /s /q dist
if exist build rmdir /s /q build

pyinstaller ^
    --onedir ^
    --noconsole ^
    --name "DarkThrone Suite" ^
    --add-data "optimizer.pyc;." ^
    --add-data "claude_strategy.pyc;." ^
    --add-data "index.html;." ^
    --add-data "_version.py;." ^
    --add-data "installer\updater.py;." ^
    --hidden-import "_version" ^
    --hidden-import "updater" ^
    --hidden-import "claude_strategy" ^
    --hidden-import "anthropic" ^
    --hidden-import "playwright" ^
    --hidden-import "playwright.sync_api" ^
    --hidden-import "playwright.__main__" ^
    --hidden-import "tkinter" ^
    --hidden-import "tkinter.ttk" ^
    --hidden-import "tkinter.scrolledtext" ^
    --collect-all "playwright" ^
    --collect-all "anthropic" ^
    --noconfirm ^
    installer\darkthrone_app.py

if errorlevel 1 (
    echo.
    echo  [ERROR] PyInstaller failed. See above for details.
    pause & exit /b 1
)

:: ── Step 4: Assemble release folder (parent of src\) ──────────────────────
echo.
echo  [4/5] Assembling release folder...
set RELEASE=..\release
if exist "%RELEASE%\DarkThrone Suite" rmdir /s /q "%RELEASE%\DarkThrone Suite"
if not exist "%RELEASE%" mkdir "%RELEASE%"

:: Copy the bundled application
xcopy /e /i /q "dist\DarkThrone Suite" "%RELEASE%\DarkThrone Suite" >nul

:: Copy the dashboard template (also bundled via --add-data, but kept next to
:: the exe for backward compatibility with any scripts that look there).
copy /y index.html "%RELEASE%\DarkThrone Suite\index.html" >nul 2>nul

:: Write install_browser.bat (users run this ONCE to set up Chromium)
(
echo @echo off
echo title DarkThrone Suite — First-time Setup
echo echo.
echo echo  Installing browser for login automation...
echo echo  ^(This downloads Chromium, ~150MB — one time only^)
echo echo.
echo python -m playwright install chromium
echo if errorlevel 1 ^(
echo     echo  [ERROR] Browser install failed. Try running as Administrator.
echo     pause ^& exit /b 1
echo ^)
echo echo.
echo echo  Setup complete! You can now run DarkThrone Suite.exe
echo pause
) > "%RELEASE%\install_browser.bat"

:: Launcher shortcut at the release root
(
echo @echo off
echo start "" "%%~dp0DarkThrone Suite\DarkThrone Suite.exe"
) > "%RELEASE%\DarkThrone Suite.bat"

:: ── Step 5: Build installer via Inno Setup ───────────────────────────────
:: Produces release\installers\DarkThroneSuite-Setup-vX.Y.Z.exe — single-file
:: installer users can double-click. No .bat files, no manual playwright
:: step. Requires Inno Setup 6 installed on the dev machine once.
echo.
echo  [5/5] Building installer via Inno Setup...
:: Inno Setup 6 install path — try x86 then x64. We use call syntax to
:: avoid batch's issues with parentheses inside quoted paths.
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo  [WARN] Inno Setup 6 not found — skipping installer build.
    echo         Install from https://jrsoftware.org/isinfo.php
    echo         then rerun this script to produce the Setup.exe.
    goto :cleanup
)
if not exist "..\release\installers" mkdir "..\release\installers"
"%ISCC%" installer\darkthrone_suite.iss
if errorlevel 1 (
    echo  [ERROR] Inno Setup build failed — check output above.
    goto :cleanup
)
echo  [OK]   Installer at release\installers\DarkThroneSuite-Setup-*.exe

:cleanup
:: Clean up temp files
rmdir /s /q dist
rmdir /s /q build
if exist "DarkThrone Suite.spec" del "DarkThrone Suite.spec"
del /q *.pyc 2>nul
del /q installer\*.pyc 2>nul

echo.
echo  ================================================
echo   Done!  release/ is ready to distribute.
echo.
echo   For public release:
echo     release\installers\DarkThroneSuite-Setup-*.exe  ^← give this to users
echo.
echo   For portable / dev use:
echo     release\DarkThrone Suite\          ^← raw bundled app
echo     release\DarkThrone Suite.bat       ^← quick launch shortcut
echo     release\install_browser.bat        ^← legacy chromium-install helper
echo.
echo  ================================================
echo.
pause
