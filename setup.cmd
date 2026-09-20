@echo off
rem Smart Ledger セットアップ(ダブルクリック用)。実行ポリシーに関係なく setup.ps1 を実行し、終了後もウィンドウを残します
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 (
  echo.
  echo setup.ps1 がエラーで終了しました。logs フォルダ内の setup_*.log を確認してください。
  pause
)
