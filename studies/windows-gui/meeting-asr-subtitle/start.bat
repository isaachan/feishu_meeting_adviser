@echo off
setlocal
cd /d "%~dp0"

set "PORT=8066"
set "URL=http://127.0.0.1:%PORT%"
set "PY=C:\Program Files\Python313\python.exe"

if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found. Edit PY in start.bat.
    pause
    exit /b 1
  )
  set "PY=python"
)

call :is_up
if not errorlevel 1 (
  echo Service already running: %URL%
  start "" "%URL%"
  exit /b 0
)

echo Starting local meeting subtitle service...
start "meeting-asr-subtitle :%PORT%" /min "%PY%" main.py

set /a tries=0
:wait
call :is_up
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% geq 30 goto timeout
ping -n 2 127.0.0.1 >nul
goto wait

:ready
echo Service ready: %URL%
start "" "%URL%"
exit /b 0

:timeout
echo [WARN] Port %PORT% did not open within 30 seconds.
echo        Check the minimized "meeting-asr-subtitle" console window for errors.
pause
exit /b 1

:is_up
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; $r=1; try{$c.Connect('127.0.0.1',%PORT%);$r=0}catch{}; $c.Close(); exit $r" >nul 2>nul
exit /b %errorlevel%
