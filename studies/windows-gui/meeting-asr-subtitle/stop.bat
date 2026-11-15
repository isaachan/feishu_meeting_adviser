@echo off
setlocal
set "PORT=8066"

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%PORT%" ^| findstr /C:"LISTENING"') do (
  set "FOUND=1"
  echo Stopping service, PID %%p ...
  taskkill /PID %%p /F >nul 2>nul
)

if not defined FOUND (
  echo No running service found on port %PORT%.
) else (
  echo Service stopped.
)

ping -n 2 127.0.0.1 >nul
exit /b 0
