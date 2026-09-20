@echo off
rem Smart Ledger 起動(ダブルクリック用)。実行ポリシーに関係なく start.ps1 を実行します
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
