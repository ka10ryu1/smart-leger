@echo off
rem Smart Ledger setup launcher. The PowerShell implementation is kept under scripts\windows.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\setup.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup failed. Check logs\setup_*.log for details.
  pause
  exit /b 1
)
