@echo off
chcp 65001 >nul
title ProxyScrape Register
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=%PYTHON_EXE%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python or set PYTHON_EXE.
    pause
    exit /b 1
)
"%PY%" -c "import DrissionPage, requests" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python dependencies missing. Run:
    echo   "%PY%" -m pip install --index-url https://pypi.org/simple -r "%~dp0requirements.txt"
    pause
    exit /b 1
)
"%PY%" "%~dp0proxyscrape_register.py"
echo.
echo ============ Done. Press any key to close. ============
pause >nul
