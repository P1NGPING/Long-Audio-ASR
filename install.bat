@echo off
setlocal

cd /d "%~dp0"

echo [Install] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv ".venv"
    if errorlevel 1 (
        echo [Error] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [Install] .venv already exists.
)

echo [Install] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [Error] Failed to upgrade pip.
    pause
    exit /b 1
)

echo [Install] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [Error] Failed to install dependencies.
    pause
    exit /b 1
)

echo [Install] Checking ffmpeg...
ffmpeg -version >nul 2>nul
if errorlevel 1 (
    echo [Warning] ffmpeg was not found in PATH.
    echo Please install ffmpeg and add it to PATH before processing video files.
) else (
    echo [Install] ffmpeg is available.
)

echo.
echo [Done] Installation finished.
echo Run GUI with: run_gui.bat
pause

endlocal
