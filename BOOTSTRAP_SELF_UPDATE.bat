@echo off
cd /d "%~dp0"
echo Bootstrapping HCS-AI self-update...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Headers @{'User-Agent'='HCS-AI-Bootstrap'} -Uri 'https://raw.githubusercontent.com/Matthew81975/Homestead_AI/main/update_hcs.ps1' -OutFile 'update_hcs.ps1'"
if errorlevel 1 (
  echo Could not download the HCS-AI updater.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_hcs.ps1" -Force
if errorlevel 1 (
  echo Bootstrap update failed.
  pause
  exit /b 1
)
echo.
echo HCS-AI is now self-updating. Future launches will check GitHub automatically.
pause
