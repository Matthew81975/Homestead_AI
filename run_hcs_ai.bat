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

start "HCS-AI Server" ".venv\Scripts\python.exe" -m hcs_ai.server_tree
".venv\Scripts\python.exe" -m hcs_ai.wait_for_server 0.8.3
if errorlevel 1 (
  echo.
  echo HCS-AI server did not become ready. The GUI will not attach to a stale server.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m hcs_ai.gui_tree
