@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0.."

echo ========================================
echo   PyInstaller Compilation Script (webGUI)
echo ========================================
echo.

set "SCRIPT_NAME=src\webGUI\app.py"
set "VENV_DIR=%TEMP%\audiosync-web-build"
set "BASE_NAME=webGUI"
set "EXE_NAME=%BASE_NAME%.exe"

if not exist "%SCRIPT_NAME%" (
    echo [ERROR] Script "%SCRIPT_NAME%" not found.
    goto :end
)

if not exist "src\webGUI\fflib.py" (
    echo [ERROR] "src\webGUI\fflib.py" not found.
    goto :end
)
if not exist "src\webGUI\sync_engine.py" (
    echo [ERROR] "src\webGUI\sync_engine.py" not found.
    goto :end
)
if not exist "src\webGUI\templates\index.html" (
    echo [ERROR] "src\webGUI\templates\index.html" not found.
    goto :end
)
if not exist "src\webGUI\static\style.css" (
    echo [ERROR] "src\webGUI\static\style.css" not found.
    goto :end
)
if not exist "src\webGUI\static\app.js" (
    echo [ERROR] "src\webGUI\static\app.js" not found.
    goto :end
)

if not exist bin mkdir bin
if exist "bin\!EXE_NAME!" (
    echo Warning: "bin\!EXE_NAME!" already exists.
    set /p USER_INPUT="Do you want to overwrite it? (Y/N): "

    if /I not "!USER_INPUT!"=="Y" (
        echo.
        echo Compilation cancelled by user.
        goto :end
    )
)

echo.
if not exist "%VENV_DIR%" (
    echo [Step 1/3] Creating a fresh, isolated virtual environment...
    python -m venv "%VENV_DIR%"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to create virtual environment. Is Python on PATH?
        goto :end
    )
) else (
    echo [Step 1/3] Found existing virtual environment. Reusing it to save time...
)

echo.
echo [Step 2/3] Installing PyInstaller and dependencies...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >nul
"%VENV_DIR%\Scripts\python.exe" -m pip install pyinstaller >nul
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to install PyInstaller.
    goto :cleanup
)

echo Installing dependencies...
"%VENV_DIR%\Scripts\python.exe" -m pip install -r src\webGUI\requirements.txt
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to install dependencies.
    goto :cleanup
)

echo.
echo [Step 3/3] Compiling "%SCRIPT_NAME%"...
echo This may take several minutes on the first run...
echo.

"%VENV_DIR%\Scripts\python.exe" -m PyInstaller ^
    --onefile ^
    --console ^
    --name "%BASE_NAME%" ^
    --distpath bin ^
    --workpath bin\build ^
    --specpath . ^
    --hidden-import=sync_engine ^
    --hidden-import=fflib ^
    --hidden-import=_version ^
    --hidden-import=probe ^
    --hidden-import=ctx ^
    --hidden-import=mkvmerge ^
    --hidden-import=audio ^
    --hidden-import=visual ^
    --hidden-import=merger ^
    --hidden-import=waitress ^
    --add-data "src\webGUI\templates;templates" ^
    --add-data "src\webGUI\static;static" ^
    --exclude-module=cv2 ^
    --exclude-module=PIL ^
    --exclude-module=tkinter ^
    "%SCRIPT_NAME%"

if !ERRORLEVEL! NEQ 0 (
    echo.
    echo [ERROR] Compilation failed! Check output above for details.
    goto :cleanup
)

echo.
echo [SUCCESS] Compilation finished! "%EXE_NAME%" is ready in the bin folder.
echo.
echo NOTE: Place ffmpeg.exe and ffprobe.exe next to %EXE_NAME% or ensure
echo       they are on the system PATH.

:cleanup
echo.
if exist "%VENV_DIR%" (
    set /p DEL_BUILD="Delete temporary venv and build artifacts to save space? (Y/N): "
    if /I "!DEL_BUILD!"=="Y" (
        echo Cleaning up...
        if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
        if exist "bin\build" rmdir /s /q "bin\build"
        if exist "%BASE_NAME%.spec" del /q "%BASE_NAME%.spec"
        echo Cleanup complete.
    ) else (
        echo Keeping build files for faster future compilations.
    )
)

:end
echo.
pause
