# Smart Ledger セットアップスクリプト(Windows PowerShell / PowerShell 7)
#   使い方: PowerShell で  .\setup.ps1
#   実行ポリシーで止まる場合:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "=== Smart Ledger セットアップ ===" -ForegroundColor Cyan

# --- Python 3.12+ を探す -------------------------------------------------
function Find-Python {
    $candidates = @(
        @{ Cmd = "py";      Args = @("-3.12") },
        @{ Cmd = "py";      Args = @("-3.13") },
        @{ Cmd = "py";      Args = @("-3") },
        @{ Cmd = "python";  Args = @() },
        @{ Cmd = "python3"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Cmd -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $c.Cmd @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
            $parts = $ver.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                return @{ Cmd = $c.Cmd; Args = $c.Args; Version = $ver.Trim() }
            }
        } catch { continue }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.12 以上が見つかりません。https://www.python.org/downloads/windows/ からインストールしてください。" -ForegroundColor Red
    Write-Host "(インストール時に 'Add python.exe to PATH' にチェックを入れてください)"
    exit 1
}
Write-Host "Python $($py.Version) を使用します ($($py.Cmd) $($py.Args -join ' '))"

# --- 仮想環境 ---------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "仮想環境 .venv を作成しています..."
    $ErrorActionPreference = "Continue"
    & $py.Cmd @($py.Args) -m venv .venv
    $venvExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($venvExit -ne 0) { Write-Host "仮想環境の作成に失敗しました。" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "仮想環境 .venv は既に存在します。"
}
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# --- 依存パッケージ ---------------------------------------------------------
Write-Host "依存パッケージをインストールしています..."
# pip は警告を stderr に出すため、ネイティブコマンド実行中は Stop にしない
$ErrorActionPreference = "Continue"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt
$pipExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($pipExit -ne 0) { Write-Host "pip install に失敗しました。" -ForegroundColor Red; exit 1 }

# --- ディレクトリ・設定ファイル --------------------------------------------
foreach ($dir in @("data", "data\backup", "data\staging", "logs")) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env を作成しました。TYPESAFE_API_KEY と DROPBOX_SMART_LEDGER_PATH を設定してください。" -ForegroundColor Yellow
} else {
    Write-Host ".env は既に存在します。"
}

Write-Host ""
Write-Host "セットアップ完了。 .\start.ps1 でアプリを起動できます。" -ForegroundColor Green
