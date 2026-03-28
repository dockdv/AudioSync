@echo off
cd /d "%~dp0"
echo Starting Audio Sync ^& Merge -- Web Interface...
echo.
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Installing dependencies...
pip install -r requirements.txt
echo.
python app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to start. Is Python on PATH?
    pause
)
