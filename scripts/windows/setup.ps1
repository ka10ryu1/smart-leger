# Smart Ledger セットアップ処理(root の setup.cmd から呼び出す内部スクリプト)
#   Windows PowerShell 5.1 / PowerShell 7 対応
#   実行内容は logs\setup_YYYYMMDD_HHMMSS.log に記録されます(失敗時はこのログを確認してください)
param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
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

    # --- Python 3.12+ を探す ---------------------------------------------------
    # Microsoft Store の python.exe スタブ(実体なし)や 3.11 以下は候補から外す
    function Find-Python {
        $candidates = @(
            @{ Cmd = "py";      Args = @("-3.12") },
            @{ Cmd = "py";      Args = @("-3.13") },
            @{ Cmd = "py";      Args = @("-3") },
            @{ Cmd = "python";  Args = @() },
            @{ Cmd = "python3"; Args = @() }
        )
        foreach ($c in $candidates) {
            $label = "$($c.Cmd) $($c.Args -join ' ')".Trim()
            $found = Get-Command $c.Cmd -ErrorAction SilentlyContinue
            if (-not $found) { Write-Host "  候補 ${label}: コマンドが見つかりません"; continue }
            try {
                $ErrorActionPreference = "Continue"
                $ver = & $c.Cmd @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                $code = $LASTEXITCODE
                $ErrorActionPreference = "Stop"
                if ($code -ne 0 -or -not $ver) {
                    Write-Host "  候補 ${label}: 実行できません(終了コード $code / $($found.Source))"
                    continue
                }
                $parts = "$ver".Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                    Write-Host "  候補 ${label}: Python $("$ver".Trim()) ($($found.Source))"
                    return @{ Cmd = $c.Cmd; Args = $c.Args; Version = "$ver".Trim() }
                }
                Write-Host "  候補 ${label}: Python $("$ver".Trim()) は 3.12 未満のため対象外"
            } catch {
                $ErrorActionPreference = "Stop"
                Write-Host "  候補 ${label}: 確認に失敗($($_.Exception.Message))"
            }
        }
        # PATH に無い場合(アプリ実行エイリアスが無効、'Add to PATH' 未チェックなど)はレジストリから探す
        foreach ($root in @("HKCU:\Software\Python\PythonCore", "HKLM:\Software\Python\PythonCore")) {
            Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
                $exe = (Get-ItemProperty "$($_.PSPath)\InstallPath" -ErrorAction SilentlyContinue).ExecutablePath
                if ($exe -and (Test-Path $exe)) {
                    $parts = $_.PSChildName.Split(".")
                    if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
                        Write-Host "  レジストリ: Python $($_.PSChildName) ($exe)"
                        return @{ Cmd = $exe; Args = @(); Version = $_.PSChildName }
                    }
                    Write-Host "  レジストリ: Python $($_.PSChildName) は 3.12 未満のため対象外"
                }
            }
        }
        return $null
    }

    Write-Host "Python を探しています..."
    $py = Find-Python
    if (-not $py) {
        Write-Host "Python 3.12 以上が見つかりません。" -ForegroundColor Red
        Write-Host "  1. https://www.python.org/downloads/windows/ から Python 3.12 以上をインストール"
        Write-Host "  2. インストーラーで 'Add python.exe to PATH' にチェック"
        Write-Host "  3. setup.cmd を再実行"
        Write-Host "  (Microsoft Store の 'python' はスタブのため使えません。'py --list' で実体を確認できます)"
        Fail "Python 3.12 以上が必要です"
    }
    Write-Host "Python $($py.Version) を使用します ($($py.Cmd) $($py.Args -join ' '))"

    # --- 仮想環境 -------------------------------------------------------------
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "仮想環境 .venv を作成しています..."
        $ErrorActionPreference = "Continue"
        & $py.Cmd @($py.Args) -m venv .venv
        $venvExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($venvExit -ne 0) { Fail "仮想環境の作成に失敗しました(終了コード $venvExit)" }
    } else {
        Write-Host "仮想環境 .venv は既に存在します。"
    }
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) { Fail "仮想環境の python.exe が見つかりません: $venvPython" }

    # --- 依存パッケージ -------------------------------------------------------
    Write-Host "依存パッケージをインストールしています(数分かかることがあります)..."
    # pip は警告を stderr に出すため、ネイティブコマンド実行中は Stop にしない
    $ErrorActionPreference = "Continue"
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -r requirements.txt
    $pipExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($pipExit -ne 0) { Fail "pip install に失敗しました(終了コード $pipExit)。ネットワーク接続とプロキシ設定を確認してください" }

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
