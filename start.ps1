# Smart Ledger 起動スクリプト
#   使い方: PowerShell で  .\start.ps1
#   実行ポリシーで止まる場合:  powershell -ExecutionPolicy Bypass -File .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "仮想環境が見つかりません。先に .\setup.ps1 を実行してください。" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env が無かったため .env.example から作成しました。TYPESAFE_API_KEY を設定してください。" -ForegroundColor Yellow
}

$port = 5000
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*SMART_LEDGER_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
}

Write-Host "Smart Ledger を起動します: http://localhost:$port  (終了は Ctrl+C)" -ForegroundColor Cyan
$env:PYTHONUTF8 = "1"
# Flask/werkzeug は stderr にも出力するため、実行中は Stop にしない
$ErrorActionPreference = "Continue"
# --open-browser: サーバー起動後に既定のブラウザで http://localhost:<port> を開く
& $venvPython app.py --open-browser --port $port
