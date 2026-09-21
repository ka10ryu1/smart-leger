# Exercise the first-run uv install path with Windows PowerShell 5.1.
$ErrorActionPreference = "Stop"
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("smart-ledger-setup-" + [guid]::NewGuid().ToString("N"))
$projectRoot = Join-Path $testRoot "project"
$windowsDir = Join-Path $projectRoot "scripts\windows"
$wingetDir = Join-Path $testRoot "winget"
$uvDir = Join-Path $testRoot "uv"
$sessionDir = Join-Path $testRoot "session-only"
$originalUserPath = [Environment]::GetEnvironmentVariable("Path", "User")

try {
    New-Item -ItemType Directory -Path $windowsDir, $wingetDir, $uvDir, $sessionDir -Force | Out-Null
    Copy-Item (Join-Path $PSScriptRoot "..\..\scripts\windows\setup.ps1") $windowsDir
    Set-Content -Path (Join-Path $projectRoot ".env.example") -Value "TEST=1" -Encoding Ascii

    $fakeWinget = @'
@echo off
echo %* > "%SMART_LEDGER_TEST_WINGET_ARGS%"
exit /b 0
'@
    $fakeUv = @'
@echo off
echo %* > "%SMART_LEDGER_TEST_UV_ARGS%"
set Path > "%SMART_LEDGER_TEST_PATH%"
if not exist ".venv\Scripts" mkdir ".venv\Scripts"
type nul > ".venv\Scripts\python.exe"
exit /b 0
'@
    Set-Content -Path (Join-Path $wingetDir "winget.cmd") -Value $fakeWinget -Encoding Ascii
    Set-Content -Path (Join-Path $uvDir "uv.cmd") -Value $fakeUv -Encoding Ascii

    # Simulate WinGet registering uv in the user PATH before the shell sees it.
    $registeredUserPath = if ($originalUserPath) { "$uvDir;$originalUserPath" } else { $uvDir }
    [Environment]::SetEnvironmentVariable("Path", $registeredUserPath, "User")

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $startInfo.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $windowsDir "setup.ps1") + '" -NoPause'
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables["Path"] = "$wingetDir;$sessionDir;$env:SystemRoot\System32;$env:SystemRoot"
    $wingetArgsPath = Join-Path $testRoot "winget-args.txt"
    $uvArgsPath = Join-Path $testRoot "uv-args.txt"
    $pathCapture = Join-Path $testRoot "uv-path.txt"
    $startInfo.EnvironmentVariables["SMART_LEDGER_TEST_WINGET_ARGS"] = $wingetArgsPath
    $startInfo.EnvironmentVariables["SMART_LEDGER_TEST_UV_ARGS"] = $uvArgsPath
    $startInfo.EnvironmentVariables["SMART_LEDGER_TEST_PATH"] = $pathCapture

    $process = [Diagnostics.Process]::Start($startInfo)
    $process.StandardInput.WriteLine("Y")
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(120000)) {
        $process.Kill()
        throw "setup.ps1 timed out"
    }
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    if ($process.ExitCode -ne 0) { throw "setup.ps1 failed ($($process.ExitCode)):`n$stdout`n$stderr" }

    $wingetArgs = (Get-Content -Raw $wingetArgsPath).Trim()
    $uvArgs = (Get-Content -Raw $uvArgsPath).Trim()
    $pathInsideUv = Get-Content -Raw $pathCapture
    if ($wingetArgs -ne "install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements") {
        throw "unexpected winget arguments: $wingetArgs"
    }
    if ($uvArgs -ne "sync --locked --python 3.12") { throw "unexpected uv arguments: $uvArgs" }
    if ($pathInsideUv -notmatch [regex]::Escape($sessionDir)) { throw "session PATH entry was lost" }
    if ($pathInsideUv -notmatch [regex]::Escape($uvDir)) { throw "installed uv PATH entry was not added" }
    if (-not (Test-Path (Join-Path $projectRoot ".venv\Scripts\python.exe"))) { throw "uv sync was not reached" }
    if (-not (Test-Path (Join-Path $projectRoot ".env"))) { throw "setup did not finish" }

    Write-Host "Windows uv auto-install smoke test passed"
} finally {
    [Environment]::SetEnvironmentVariable("Path", $originalUserPath, "User")
    if (Test-Path $testRoot) { Remove-Item $testRoot -Recurse -Force }
}
