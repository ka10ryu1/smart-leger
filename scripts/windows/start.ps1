# Smart Ledger 起動処理(root の start.cmd から呼び出す内部スクリプト)
param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $projectRoot

function Finish([int]$code) {
    if (-not $NoPause) {
        Write-Host ""
        Read-Host "Enter キーを押すとこのウィンドウを閉じます" | Out-Null
    }
    exit $code
}

function Fail([string]$message) {
    Write-Host ""
    Write-Host "エラー: $message" -ForegroundColor Red
    Finish 1
}

try {
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Fail "仮想環境が見つかりません。先に setup.cmd を実行してください。"
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
    if ($projectRoot -like "\\*") {
        # \\wsl.localhost\... などネットワーク越しのフォルダでは .venv の読み込みが極端に遅くなる
        Write-Host "注意: ネットワーク上のフォルダ($projectRoot)から起動しています。ライブラリの読み込みに数分かかることがあります。Windows のローカルディスク(例: C:\Users\<名前>\smart-leger)に置くと数秒で起動します。" -ForegroundColor Yellow
    }
    $env:PYTHONUTF8 = "1"
    # Flask/werkzeug は stderr にも出力するため、実行中は Stop にしない
    $ErrorActionPreference = "Continue"
    # --open-browser: サーバー起動後に既定のブラウザで http://localhost:<port> を開く
    & $venvPython app.py --open-browser --port $port
    $appExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($appExit -ne 0) {
        Fail "アプリが終了コード $appExit で停止しました。上のメッセージと logs\smart_ledger.log を確認してください。"
    }
    Finish 0
} catch {
    Fail "予期しないエラー: $($_.Exception.Message)"
}
