@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-windows.ps1"
if errorlevel 1 (
  echo.
  echo Setup stopped. Read the message above before trying again.
  pause
  exit /b 1
)
echo.
echo Setup is complete. Open Member Intake from your desktop.
pause
