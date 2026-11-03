@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
  echo [VRAM Monitor] pythonw not found.
  echo Install Python 3.9+ or run: python app.py
  pause
  exit /b 1
)

start "" pythonw "%~dp0app.py"
exit /b 0
