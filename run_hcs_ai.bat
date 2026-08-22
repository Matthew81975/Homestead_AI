@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo HCS-AI is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
start "HCS-AI Server" ".venv\Scripts\python.exe" -m hcs_ai.server_tree
timeout /t 2 /nobreak >nul
".venv\Scripts\python.exe" -m hcs_ai.gui_tree
