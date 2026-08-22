@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo HCS-AI is not installed yet. Run install.bat first.
  pause
  exit /b 1
)

if exist "update_hcs.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_hcs.ps1"
  if errorlevel 1 (
    echo.
    echo HCS-AI updater reported an unrecoverable error.
    echo The application was not started so the installation can be checked safely.
    pause
    exit /b 1
  )
)

if /I "%~1"=="--minimized" (
  ".venv\Scripts\pythonw.exe" -m hcs_ai.desktop_host --minimized
) else (
  ".venv\Scripts\pythonw.exe" -m hcs_ai.desktop_host
)
