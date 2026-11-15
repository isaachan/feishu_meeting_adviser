@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo Install Python 3.11+ from https://www.python.org/downloads/windows/
  echo and check "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed. Check network/proxy and try again.
  pause
  exit /b 1
)

echo.
echo Done. Double-click start.bat to run.
pause
