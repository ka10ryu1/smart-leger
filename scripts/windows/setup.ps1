# Smart Ledger セットアップ処理(root の setup.cmd から呼び出す内部スクリプト)
#   Windows PowerShell 5.1 / PowerShell 7 対応
#   実行内容は logs\setup_YYYYMMDD_HHMMSS.log に記録されます(失敗時はこのログを確認してください)
param([switch]$NoPause)

$ErrorActionPreference = "Stop"
# Resolve-Path は UNC パス (\\wsl.localhost\...) に "Microsoft.PowerShell.Core\FileSystem::" を付け ".." も残すため GetFullPath で正規化する
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
# \\wsl.localhost\... など UNC 上のフォルダは WSL 側の .venv(Linux 用)と共有されるため、Windows 用の環境を別名で作る
$venvName = if ($projectRoot -like "\\*") { ".venv-windows" } else { ".venv" }
Set-Location -LiteralPath $projectRoot

# --- ログ(ウィンドウがすぐ閉じても原因を追えるように、出力をファイルにも残す) -----
New-Item -ItemType Directory -Path "logs" -Force | Out-Null
$logPath = Join-Path $projectRoot ("logs\setup_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
try { Start-Transcript -Path $logPath -Append | Out-Null } catch { Write-Host "ログを開始できませんでした: $($_.Exception.Message)" -ForegroundColor Yellow }

function Finish([int]$code) {
    try { Stop-Transcript | Out-Null } catch { }
    if (-not $NoPause) {
        Write-Host ""
        Read-Host "Enter キーを押すとこのウィンドウを閉じます" | Out-Null
    }
    exit $code
}

function Fail([string]$message) {
    Write-Host ""
    Write-Host "エラー: $message" -ForegroundColor Red
    Write-Host "詳細ログ: $logPath" -ForegroundColor Yellow
    Finish 1
}

try {
    Write-Host "=== Smart Ledger セットアップ ===" -ForegroundColor Cyan
    Write-Host "PowerShell $($PSVersionTable.PSVersion) / フォルダ: $projectRoot"

    # --- Python・仮想環境・依存パッケージ ------------------------------------
    $uv = Get-Command "uv" -ErrorAction SilentlyContinue
    if (-not $uv) {
        $installUv = Read-Host "uv が見つかりません。winget でインストールしますか？ [Y/n]"
        if ($installUv -eq "" -or $installUv -match "^[Yy]$") {
            $winget = Get-Command "winget" -ErrorAction SilentlyContinue
            if (-not $winget) {
                Fail "winget が見つかりません。App Installer を更新するか、https://learn.microsoft.com/ja-jp/windows/package-manager/winget/ を確認してください"
            }

            Write-Host "uv をインストールしています..."
            $ErrorActionPreference = "Continue"
            & $winget.Source install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
            $wingetExit = $LASTEXITCODE
            $ErrorActionPreference = "Stop"
            if ($wingetExit -ne 0) { Fail "uv のインストールに失敗しました(終了コード $wingetExit)" }

            # winget が登録した PATH だけを追加し、起動元のシェルで設定された PATH は保持する
            $registeredPath = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
            $activePathEntries = @($env:Path -split ";")
            foreach ($entry in ($registeredPath -split ";")) {
                if ($entry -and $entry -notin $activePathEntries) {
                    $env:Path += ";$entry"
                    $activePathEntries += $entry
                }
            }
            $uv = Get-Command "uv" -ErrorAction SilentlyContinue
            if (-not $uv) { Fail "uv をインストールしましたが、コマンドを検出できません。PowerShell を開き直して setup.cmd を再実行してください" }
        } else {
            Fail "uv のインストールが必要です"
        }
    }
    Write-Host "uv を使用します: $($uv.Source)"
    Write-Host "Python 3.12 と依存パッケージを同期しています(初回は数分かかることがあります)..."
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot $venvName
    # uv は進捗を stderr に出すため、ネイティブコマンド実行中は Stop にしない
    $ErrorActionPreference = "Continue"
    & $uv.Source sync --locked --python 3.12
    $uvExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($uvExit -ne 0) { Fail "uv sync に失敗しました(終了コード $uvExit)。uv が古い場合は uv self update (公式インストーラー版) または winget upgrade --id=astral-sh.uv -e (winget 版) で更新し、ネットワーク接続とプロキシ設定も確認してください" }
    $venvPython = Join-Path $projectRoot "$venvName\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) { Fail "仮想環境の python.exe が見つかりません: $venvPython" }

    # --- ディレクトリ・設定ファイル ------------------------------------------
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
    Write-Host "セットアップ完了。start.cmd でアプリを起動できます。" -ForegroundColor Green
    Finish 0
} catch {
    Fail "予期しないエラー: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
}
