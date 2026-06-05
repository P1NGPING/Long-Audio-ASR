@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Error] .venv not found. Please create the virtual environment first.
    echo Run: python -m venv .venv
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "asr_gui.py"

if errorlevel 1 (
    echo.
    echo [Error] GUI exited with an error.
    pause
)

endlocal
