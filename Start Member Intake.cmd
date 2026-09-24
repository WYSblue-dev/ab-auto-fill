@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup Windows.cmd once before opening Member Intake.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0local_app.py"
if errorlevel 1 (
  echo.
  echo Member Intake stopped. Read the message above.
  pause
)
