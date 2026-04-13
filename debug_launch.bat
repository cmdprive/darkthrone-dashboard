@echo off
title DarkThrone — Debug Launch
cd /d "%~dp0"

echo Working directory: %CD%
echo.

echo Checking Python...
python --version
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo Try: py --version
    py --version
    if errorlevel 1 (
        echo [ERROR] Neither python nor py found. Install Python 3.10+
        pause
        exit /b 1
    )
    echo Using "py" launcher instead.
    py installer\darkthrone_app.py
) else (
    echo Launching app...
    python installer\darkthrone_app.py
)

echo.
echo App exited with code %errorlevel%
pause
