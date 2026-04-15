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
echo  [1/4] Installing build tools...
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo  [ERROR] Could not install build tools.
    pause & exit /b 1
)

:: ── Step 2: Compile source files to bytecode (.pyc) ───────────────────────
echo.
echo  [2/4] Compiling source to bytecode...
python -m compileall -b -q ^
    installer\darkthrone_app.py ^
    optimizer.py
if errorlevel 1 (
    echo  [ERROR] Bytecode compilation failed.
    pause & exit /b 1
)

:: ── Step 3: Bundle into .exe with PyInstaller ─────────────────────────────
echo.
echo  [3/4] Compiling to .exe (this takes a few minutes)...
if exist dist  rmdir /s /q dist
if exist build rmdir /s /q build

pyinstaller ^
    --onedir ^
    --noconsole ^
    --name "DarkThrone Suite" ^
    --add-data "optimizer.pyc;." ^
    --add-data "index.html;." ^
    --hidden-import "playwright" ^
    --hidden-import "playwright.sync_api" ^
    --hidden-import "tkinter" ^
    --hidden-import "tkinter.ttk" ^
    --hidden-import "tkinter.scrolledtext" ^
    --collect-all "playwright" ^
    --noconfirm ^
    installer\darkthrone_app.py

if errorlevel 1 (
    echo.
    echo  [ERROR] PyInstaller failed. See above for details.
    pause & exit /b 1
)

:: ── Step 4: Assemble release folder (parent of src\) ──────────────────────
echo.
echo  [4/4] Assembling release folder...
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

:: Clean up temp files
rmdir /s /q dist
rmdir /s /q build
if exist "DarkThrone Suite.spec" del "DarkThrone Suite.spec"
del /q *.pyc 2>nul
del /q installer\*.pyc 2>nul

echo.
echo  ================================================
echo   Done!  %RELEASE%\ folder is ready to distribute.
echo.
echo   Contents of %RELEASE%\:
echo     install_browser.bat   ^← run once on first use
echo     DarkThrone Suite.bat  ^← launch shortcut
echo     DarkThrone Suite\     ^← the application
echo.
echo  ================================================
echo.
pause
