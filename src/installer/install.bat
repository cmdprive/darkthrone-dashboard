@echo off
title DarkThrone Suite — Installer
echo.
echo  ================================================
echo   DarkThrone Suite — First-time Setup
echo  ================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python is not installed or not in PATH.
    echo.
    echo  Download Python 3.10+ from: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo  [1/3] Installing Python packages...
python -m pip install playwright
if errorlevel 1 (
    echo  [ERROR] pip install failed. Try running as Administrator.
    pause
    exit /b 1
)

echo.
echo  [2/3] Installing Chromium browser (used for login automation)...
python -m playwright install chromium
if errorlevel 1 (
    echo  [ERROR] Playwright browser install failed.
    pause
    exit /b 1
)

echo.
echo  [3/3] Cleaning up folder...
call "%~dp0cleanup.bat"

echo.
echo  ================================================
echo   Setup complete!
echo   Launch the app by double-clicking:
echo     Start DarkThrone.vbs
echo  ================================================
echo.
pause
