@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo Installation failed.
  pause
  exit /b 1
)
call "%~dp0run_hcs_ai.bat"
